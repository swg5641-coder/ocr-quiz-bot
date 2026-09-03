import os
import json
import logging
import asyncio
import random
import threading
import time
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Poll,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PollAnswerHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types

# ============================================================
# 1. RENDER HEALTH-CHECK SERVER (PREVENTS PORT TIMEOUT)
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK - QuizBot Live")
        except Exception:
            pass

    def log_message(self, format, *args):
        return

def start_health_server():
    while True:
        try:
            port = int(os.environ.get("PORT", 8080))
            server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
            server.serve_forever()
        except Exception as e:
            logging.getLogger(__name__).error(f"Health server crashed, restarting: {e}")
            time.sleep(2)

threading.Thread(target=start_health_server, daemon=True).start()

# ============================================================
# 2. CONFIG & CREDENTIALS
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash"]
FAST_GK_MODEL = "gemini-2.5-flash-lite"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

gemini_client = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Failed to init Gemini client: {e}")
        gemini_client = None

# Active tracking structures
active_polls = {}       # poll_id -> {chat_id, correct_id, index, answered}
saved_quizzes_db = {}   # user_id -> list of quizzes

# Default User Settings: timer=15 (0 means No Timer / Instant on click), shuffle=True, negative=False
user_settings = {}

def get_settings(user_id):
    if user_id not in user_settings:
        # language: "en" = English quiz, "hi" = Hindi quiz
        user_settings[user_id] = {"timer": 15, "shuffle": True, "negative": False, "language": "en"}
    if "language" not in user_settings[user_id]:
        user_settings[user_id]["language"] = "en"
    return user_settings[user_id]

def md_escape(text: str) -> str:
    """Escape characters that break legacy Markdown parsing when embedding
    dynamic/AI-generated text into a Markdown-formatted message."""
    if text is None:
        return ""
    text = str(text)
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, "\\" + ch)
    return text

async def safe_reply(message_or_query, text: str, reply_markup=None, parse_mode="Markdown"):
    """Reply with Markdown, but never let a formatting error crash the flow."""
    try:
        return await message_or_query.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"Markdown send failed, retrying as plain text: {e}")
        try:
            return await message_or_query.reply_text(text, reply_markup=reply_markup)
        except Exception as e2:
            logger.error(f"Plain text send also failed: {e2}")
            return None

# ============================================================
# 3. PERMANENT SEARCH-BAR KEYBOARD (Chat Box Ke Upar)
# ============================================================

def get_main_keyboard():
    keyboard = [
        ["📸 Scan Quiz", "✍️ Text Bulk Quiz"],
        ["🧠 GK/GS & Static GK Doubt", "⚙️ Quiz Settings"],
        ["📁 My Quizzes", "🛑 Stop Quiz"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ============================================================
# 4. AI PROMPTS
# ============================================================

EXTRACT_PROMPT_BASE = r"""
Extract multiple-choice questions from the provided input (image or text).
Rules:
1. Valid JSON only.
2. Max 4 options per question.
3. Question length max 280 chars, option length max 90 chars.
4. Correct answer index must be 0, 1, 2, or 3.
{LANGUAGE_RULE}
Output format:
{
  "title": "Quiz Assessment",
  "questions": [
    {
      "question": "Question text",
      "options": ["Opt 1", "Opt 2", "Opt 3", "Opt 4"],
      "answer": 0
    }
  ]
}
"""

LANGUAGE_RULES = {
    "en": "5. Write the title, every question, and every option strictly in English. If the source material is in Hindi or any other language, translate it into clear English.",
    "hi": "5. Title, har question aur har option strictly हिंदी (Devanagari script) me likho. Agar source material English ya kisi aur bhasha me hai, to use pure Hindi me translate karke likho.",
}

def get_extract_prompt(language: str) -> str:
    rule = LANGUAGE_RULES.get(language, LANGUAGE_RULES["en"])
    return EXTRACT_PROMPT_BASE.replace("{LANGUAGE_RULE}", rule)

GK_PROMPT_HI = r"""
तुम एक बहुत तेज़ और सटीक GK/GS और Static GK शिक्षक हो।
उपयोगकर्ता द्वारा पूछे गए सामान्य ज्ञान/सामान्य अध्ययन प्रश्न का उत्तर अत्यंत संक्षिप्त, बिंदुवार और सीधे शब्दों में 2-3 वाक्यों में हिंदी में दो।
अनावश्यक भूमिका मत बनाओ। सीधे मुख्य तथ्य, वर्ष, नाम या कारण बताओ।
"""

GK_PROMPT_EN = r"""
You are a very fast and accurate GK/GS and Static GK teacher.
Answer the user's General Knowledge / General Studies question in extremely concise, point-wise, plain English in 2-3 sentences.
Do not add unnecessary intro. Directly state the key facts, year, name, or reason.
"""

def get_gk_prompt(language: str) -> str:
    return GK_PROMPT_EN if language == "en" else GK_PROMPT_HI

def parse_with_gemini(part: types.Part, language: str = "en"):
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY is not configured!")

    prompt = get_extract_prompt(language)
    last_error = None
    for model_name in FALLBACK_MODELS:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=[part, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            if response and getattr(response, "text", None):
                return json.loads(response.text)
        except Exception as e:
            last_error = e
            logger.warning(f"Model {model_name} failed: {e}. Trying fallback...")
            continue

    raise RuntimeError(f"All AI models busy: {last_error}")

def ask_gk_fast(question_text: str, language: str = "hi") -> str:
    if not gemini_client:
        return "❌ GEMINI_API_KEY set nahi hai."

    prompt = get_gk_prompt(language)
    last_error = None

    # Try the fast model first, then fall back through the same models
    # used for scanning so a single busy/invalid model doesn't fail everything.
    models_to_try = [FAST_GK_MODEL] + [m for m in FALLBACK_MODELS if m != FAST_GK_MODEL]

    for model_name in models_to_try:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=[prompt, question_text],
            )
            if response and getattr(response, "text", None):
                return response.text.strip()
        except Exception as e:
            last_error = e
            logger.warning(f"GK model {model_name} failed: {e}. Trying fallback...")
            continue

    logger.error(f"GK fast error, all models failed: {last_error}")
    return "⚠️ Server busy hai, kripya dobara puchein."

def sanitize_and_prepare(data: dict, shuffle_enabled: bool):
    if not isinstance(data, dict):
        return "Practice Quiz", []

    title = str(data.get("title") or "Practice Quiz").strip()[:100] or "Practice Quiz"
    raw_questions = data.get("questions", [])
    if not isinstance(raw_questions, list):
        raw_questions = []

    valid = []

    for item in raw_questions:
        if not isinstance(item, dict):
            continue

        q = str(item.get("question", "") or "").strip()
        opts = item.get("options", [])
        ans = item.get("answer", 0)

        if not q or not isinstance(opts, list) or len(opts) < 2:
            continue

        clean_opts = [str(x).strip()[:90] for x in opts if str(x).strip()]
        if len(clean_opts) < 2:
            continue

        while len(clean_opts) < 4:
            clean_opts.append(f"Option {len(clean_opts) + 1}")
        clean_opts = clean_opts[:4]

        try:
            ans = int(ans)
        except Exception:
            ans = 0
        if ans not in (0, 1, 2, 3) or ans >= len(clean_opts):
            ans = 0

        if shuffle_enabled:
            correct_val = clean_opts[ans]
            random.shuffle(clean_opts)
            ans = clean_opts.index(correct_val)

        valid.append({
            "question": q[:280],
            "options": clean_opts,
            "answer": ans
        })

    if shuffle_enabled:
        random.shuffle(valid)

    return title, valid

# ============================================================
# 5. COMMANDS & BUTTON HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cfg = get_settings(user_id)
    timer_display = f"{cfg['timer']}s" if cfg['timer'] > 0 else "Bina Timer (Direct Click)"

    welcome_text = (
        "👋 *Swagat hai Quiz Master Bot me!*\n\n"
        "Aapke typing bar ke upar saare options diye gaye hain:\n"
        "• 📸 *Scan Quiz* - Photo se instant quiz\n"
        "• ✍️ *Text Bulk Quiz* - 50-200 questions paste karein\n"
        "• 🧠 *GK/GS & Static GK* - Koi bhi GK question puchein (1-2s fast reply)\n"
        "• ⚙️ *Quiz Settings* - Timer (15s ya No Timer) badlein\n\n"
        f"⚙️ *Current Setting:* Timer: *{timer_display}* | Negative: *{'-0.25' if cfg['negative'] else 'OFF'}*"
    )
    await safe_reply(update.message, welcome_text, reply_markup=get_main_keyboard())

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cfg = get_settings(user_id)
    timer_display = f"{cfg['timer']}s Countdown" if cfg['timer'] > 0 else "Bina Timer (Answer Click karte hi Next)"

    lang_display = "English 🇬🇧" if cfg["language"] == "en" else "हिंदी 🇮🇳"

    text = (
        "⚙️ *Quiz Settings Configuration:*\n\n"
        f"⏱ *Current Mode:* {timer_display}\n"
        f"🔀 *Shuffle Questions:* {'✅ ON' if cfg['shuffle'] else '❌ OFF'}\n"
        f"⚠️ *Negative Marking (-0.25):* {'✅ ON' if cfg['negative'] else '❌ OFF'}\n"
        f"🌐 *Quiz/GK Language:* {lang_display}\n\n"
        "Neeche buttons se apna pasandida mode select karein:"
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ 15s Timer", callback_data="set_timer:15"),
            InlineKeyboardButton("⚡ Bina Timer (Direct Next)", callback_data="set_timer:0"),
        ],
        [
            InlineKeyboardButton("⏱ 10s Timer", callback_data="set_timer:10"),
            InlineKeyboardButton("⏱ 30s Timer", callback_data="set_timer:30"),
        ],
        [InlineKeyboardButton(f"Shuffle: {'ON ✅' if cfg['shuffle'] else 'OFF ❌'}", callback_data="toggle_shuffle")],
        [InlineKeyboardButton(f"Negative Marking: {'ON ✅' if cfg['negative'] else 'OFF ❌'}", callback_data="toggle_negative")],
        [
            InlineKeyboardButton("🇬🇧 English" + (" ✅" if cfg["language"] == "en" else ""), callback_data="set_lang:en"),
            InlineKeyboardButton("🇮🇳 हिंदी" + (" ✅" if cfg["language"] == "hi" else ""), callback_data="set_lang:hi"),
        ],
    ])
    await safe_reply(update.message, text, reply_markup=markup)

async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cfg = get_settings(user_id)
    lang_display = "English 🇬🇧" if cfg["language"] == "en" else "हिंदी 🇮🇳"

    text = (
        f"🌐 *Current Quiz/GK Language:* {lang_display}\n\n"
        "Neeche button se apni pasandida language choose karein:"
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🇬🇧 English" + (" ✅" if cfg["language"] == "en" else ""), callback_data="set_lang:en"),
        InlineKeyboardButton("🇮🇳 हिंदी" + (" ✅" if cfg["language"] == "hi" else ""), callback_data="set_lang:hi"),
    ]])
    await safe_reply(update.message, text, reply_markup=markup)

async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("active_quiz", None)
    context.user_data.pop("q_index", None)
    context.user_data["mode"] = None
    await update.message.reply_text("🛑 Quiz stop kar diya gaya hai.", reply_markup=get_main_keyboard())

# ============================================================
# 6. INPUT DISPATCHER (PHOTOS, FILES, SEARCH-BAR TEXT)
# ============================================================

async def handle_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    cfg = get_settings(user_id)
    text = update.message.text or ""

    # Search Bar Ke Upar Wale Buttons Handle Karna
    if text == "📸 Scan Quiz":
        context.user_data["mode"] = "AWAITING_IMAGE"
        await safe_reply(update.message, "📸 *Book page ya question paper ki saaf photo bhejiye.*")
        return
    elif text == "✍️ Text Bulk Quiz":
        context.user_data["mode"] = "AWAITING_TEXT"
        await safe_reply(update.message, "✍️ *Questions yahan text me paste karein ya `.txt` file upload karein.*")
        return
    elif text == "🧠 GK/GS & Static GK Doubt":
        context.user_data["mode"] = "GK_DOUBT"
        await safe_reply(update.message, "🧠 *GK/GS Fast Assistant Active:*\nKoi bhi GK, GS ya Static GK ka sawal yahan type karein, 1-2 second me jawab aayega.")
        return
    elif text == "⚙️ Quiz Settings":
        await settings_command(update, context)
        return
    elif text == "📁 My Quizzes":
        quizzes = saved_quizzes_db.get(user_id, [])
        if not quizzes:
            await update.message.reply_text("📂 Koi saved quiz nahi mila.")
            return
        buttons = [
            [InlineKeyboardButton(f"▶️ {q['title']} ({len(q['questions'])} Qs)", callback_data=f"load_quiz:{i}")]
            for i, q in enumerate(quizzes)
        ]
        await safe_reply(update.message, "📁 *Aapke Saved Quizzes:*", reply_markup=InlineKeyboardMarkup(buttons))
        return
    elif text == "🛑 Stop Quiz":
        await stop_quiz(update, context)
        return

    # Mode: Direct GK/GS Fast Doubt Answering (1-2s Response)
    if context.user_data.get("mode") == "GK_DOUBT" and text:
        ans = await asyncio.to_thread(ask_gk_fast, text, cfg["language"])
        await safe_reply(update.message, f"💡 *Jawab:*\n{md_escape(ans)}", reply_markup=get_main_keyboard())
        return

    # 1. PHOTO INPUT
    if update.message.photo:
        status = await update.message.reply_text("🔍 Analyzing page & extracting questions...")
        try:
            photo = update.message.photo[-1]
            t_file = await photo.get_file()
            img_bytes = bytes(await t_file.download_as_bytearray())

            part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            data = await asyncio.to_thread(parse_with_gemini, part, cfg["language"])
            title, questions = sanitize_and_prepare(data, cfg["shuffle"])

            if not questions:
                await status.edit_text("⚠️ Image saaf nahi thi. GK Doubt puchhne ke liye '🧠 GK/GS' button dabayein.")
                return

            await status.delete()
            await setup_ready_card(update.message, context, user_id, title, questions)
        except Exception as e:
            logger.exception("Image parse error")
            try:
                await status.edit_text(f"❌ Scan failed: {md_escape(str(e))[:200]}")
            except Exception:
                pass
        return

    # 2. DOCUMENT (.txt file)
    if update.message.document:
        doc = update.message.document
        file_name = doc.file_name or ""
        if not file_name.lower().endswith((".txt", ".text")):
            await update.message.reply_text("⚠️ Kripya `.txt` text file upload karein.")
            return

        status = await update.message.reply_text("⚙️ Bulk questions read ho rahe hain...")
        try:
            t_file = await doc.get_file()
            raw_bytes = await t_file.download_as_bytearray()
            content = bytes(raw_bytes).decode("utf-8", errors="ignore")
            if not content.strip():
                await status.edit_text("⚠️ File khaali hai.")
                return

            part = types.Part.from_text(text=content)
            data = await asyncio.to_thread(parse_with_gemini, part, cfg["language"])
            title, questions = sanitize_and_prepare(data, cfg["shuffle"])

            if not questions:
                await status.edit_text("⚠️ File me valid questions nahi mile.")
                return

            await status.delete()
            await setup_ready_card(update.message, context, user_id, title, questions)
        except Exception as e:
            logger.exception("File parse error")
            try:
                await status.edit_text(f"❌ Error: {md_escape(str(e))[:200]}")
            except Exception:
                pass
        return

    # 3. TEXT MESSAGE (Agar user ne text questions bheje hain)
    if text and not text.startswith("/"):
        if context.user_data.get("mode") == "AWAITING_TEXT":
            status = await update.message.reply_text("⚙️ AI text analyze kar raha hai...")
            try:
                part = types.Part.from_text(text=text)
                data = await asyncio.to_thread(parse_with_gemini, part, cfg["language"])
                title, questions = sanitize_and_prepare(data, cfg["shuffle"])

                if not questions:
                    await status.edit_text("⚠️ Questions extract nahi ho sake.")
                    return

                await status.delete()
                context.user_data["mode"] = None
                await setup_ready_card(update.message, context, user_id, title, questions)
            except Exception as e:
                logger.exception("Text parse error")
                try:
                    await status.edit_text(f"❌ Error: {md_escape(str(e))[:200]}")
                except Exception:
                    pass
        else:
            # Automatic Fallback: User ne seedha text pucha hai toh instant GK answer de do!
            ans = await asyncio.to_thread(ask_gk_fast, text, cfg["language"])
            await safe_reply(update.message, f"💡 *Jawab:*\n{md_escape(ans)}")

async def setup_ready_card(message, context: ContextTypes.DEFAULT_TYPE, user_id: int, title: str, questions: list):
    if not questions:
        await message.reply_text("⚠️ Koi valid question nahi mila.")
        return

    cfg = get_settings(user_id)

    if user_id not in saved_quizzes_db:
        saved_quizzes_db[user_id] = []
    saved_quizzes_db[user_id].append({"title": title, "questions": questions})

    context.user_data["active_quiz"] = questions
    context.user_data["quiz_title"] = title
    context.user_data["q_index"] = 0
    context.user_data["score"] = 0.0
    context.user_data["correct_count"] = 0
    context.user_data["wrong_count"] = 0
    context.user_data["unanswered_count"] = 0
    context.user_data["user_id"] = user_id

    timer_str = f"{cfg['timer']} seconds per question" if cfg['timer'] > 0 else "⚡ Bina Timer (Click karte hi direct agla sawal)"

    ready_text = (
        f"🎲 Get ready for the quiz *'{md_escape(title)}'*\n\n"
        f"🖊 *{len(questions)} questions*\n"
        f"⏱ *{timer_str}*\n"
        f"🔀 Shuffle: *{'ON' if cfg['shuffle'] else 'OFF'}*\n"
        f"⚠️ Negative Marking: *{'-0.25' if cfg['negative'] else 'OFF'}*\n\n"
        f"🏁 Press the button below when you are ready.\n"
        f"Send /stop to cancel."
    )
    ready_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("I am ready!", callback_data="start_quiz_session")]
    ])

    await safe_reply(message, ready_text, reply_markup=ready_markup)

# ============================================================
# 7. QUIZ RUNNER (15s TIMER & DIRECT CLICK MODES)
# ============================================================

async def send_next_poll(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    questions = context.user_data.get("active_quiz", [])
    index = context.user_data.get("q_index", 0)
    user_id = context.user_data.get("user_id", chat_id)
    cfg = get_settings(user_id)

    # Quiz Complete / no questions available
    if not questions or index >= len(questions):
        final_score = context.user_data.get("score", 0.0)
        correct = context.user_data.get("correct_count", 0)
        wrong = context.user_data.get("wrong_count", 0)
        skipped = context.user_data.get("unanswered_count", 0)
        total = len(questions)

        percentage = max(0, round((correct / total) * 100, 1)) if total else 0

        if percentage >= 75:
            congrats_header = "🏆 *Congratulations! 👏🎉 You are Rank #1 Outstanding!*"
        elif percentage >= 40:
            congrats_header = "👏 *Nice Attempt! Keep it up!*"
        else:
            congrats_header = "📚 *Keep Practicing! You will score better next time!*"

        summary = (
            f"{congrats_header}\n\n"
            f"🏁 *QUIZ COMPLETE REPORT*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Total Questions: *{total}*\n"
            f"✅ Correct Answers: *{correct}*\n"
            f"❌ Wrong Answers: *{wrong}*\n"
            f"⏳ Skipped/Time Out: *{skipped}*\n"
            f"🎯 Final Score: *{final_score:.2f} / {total}*\n"
            f"📈 Accuracy: *{percentage}%*\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            "Naya quiz shuru karne ke liye photo bhejein ya niche diye gaye button use karein."
        )
        try:
            await context.bot.send_message(chat_id=chat_id, text=summary, parse_mode="Markdown", reply_markup=get_main_keyboard())
        except Exception as e:
            logger.warning(f"Summary markdown send failed: {e}")
            await context.bot.send_message(chat_id=chat_id, text=summary.replace("*", ""), reply_markup=get_main_keyboard())

        context.user_data.pop("active_quiz", None)
        context.user_data.pop("q_index", None)
        return

    q = questions[index]

    # Guard: malformed question entry
    if not isinstance(q, dict) or not q.get("question") or not q.get("options"):
        context.user_data["q_index"] = index + 1
        await send_next_poll(chat_id, context)
        return

    timer_sec = cfg["timer"]

    options = q.get("options") or []
    if len(options) < 2:
        context.user_data["q_index"] = index + 1
        await send_next_poll(chat_id, context)
        return

    correct_id = q.get("answer", 0)
    if not isinstance(correct_id, int) or correct_id < 0 or correct_id >= len(options):
        correct_id = 0

    question_text = f"[{index + 1}/{len(questions)}] {q['question']}"[:290]

    # Agar timer 0 hai toh Telegram open_period parameter nahi bhejenge (No Timer Mode)
    poll_kwargs = {
        "chat_id": chat_id,
        "question": question_text,
        "options": options,
        "type": Poll.QUIZ,
        "correct_option_id": correct_id,
        "is_anonymous": False,
    }

    if timer_sec and timer_sec > 0:
        poll_kwargs["open_period"] = timer_sec

    try:
        msg = await context.bot.send_poll(**poll_kwargs)
    except Exception as e:
        logger.exception(f"send_poll failed at index {index}: {e}")
        # Skip the broken question instead of stalling the quiz
        context.user_data["q_index"] = index + 1
        await send_next_poll(chat_id, context)
        return

    active_polls[msg.poll.id] = {
        "chat_id": chat_id,
        "correct_id": correct_id,
        "index": index,
        "answered": False
    }

    # Timer Mode me: 15s khatam hote hi agla sawal apne aap
    if timer_sec and timer_sec > 0:
        asyncio.create_task(timer_auto_advance(msg.poll.id, chat_id, index, timer_sec, context))

async def timer_auto_advance(poll_id: str, chat_id: int, expected_index: int, timer_sec: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.sleep(timer_sec)

        if poll_id in active_polls:
            poll_info = active_polls.pop(poll_id)
            if not poll_info["answered"] and context.user_data.get("q_index") == expected_index:
                context.user_data["unanswered_count"] = context.user_data.get("unanswered_count", 0) + 1
                context.user_data["q_index"] = expected_index + 1
                await asyncio.sleep(0.3)
                await send_next_poll(chat_id, context)
    except Exception:
        logger.exception("timer_auto_advance error")

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        answer = update.poll_answer
        if not answer:
            return
        poll_id = answer.poll_id

        if poll_id not in active_polls:
            return

        poll_info = active_polls.pop(poll_id)
        poll_info["answered"] = True
        user_id = answer.user.id if answer.user else poll_info["chat_id"]
        cfg = get_settings(user_id)

        selected = answer.option_ids[0] if answer.option_ids else -1

        if selected == poll_info["correct_id"]:
            context.user_data["score"] = context.user_data.get("score", 0.0) + 1.0
            context.user_data["correct_count"] = context.user_data.get("correct_count", 0) + 1
        else:
            if cfg["negative"]:
                context.user_data["score"] = context.user_data.get("score", 0.0) - 0.25
            context.user_data["wrong_count"] = context.user_data.get("wrong_count", 0) + 1

        # Option select karte hi direct agla sawal
        context.user_data["q_index"] = poll_info["index"] + 1
        await asyncio.sleep(0.8)
        await send_next_poll(poll_info["chat_id"], context)
    except Exception:
        logger.exception("handle_poll_answer error")

# ============================================================
# 8. INLINE CALLBACK BUTTONS
# ============================================================

async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""
    user_id = query.from_user.id
    cfg = get_settings(user_id)

    try:
        if data == "start_quiz_session":
            if not context.user_data.get("active_quiz"):
                await query.message.reply_text("⚠️ Koi active quiz nahi mila.")
                return
            context.user_data["user_id"] = user_id
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await send_next_poll(query.message.chat_id, context)

        # Timer selection
        elif data.startswith("set_timer:"):
            try:
                sec = int(data.split(":")[1])
            except (IndexError, ValueError):
                return
            cfg["timer"] = sec
            status_msg = f"⏱ Timer set to: *{sec} Seconds*" if sec > 0 else "⚡ Set to: *Bina Timer (Click karte hi Direct Next)*"
            await safe_reply(query.message, status_msg)

        elif data == "toggle_shuffle":
            cfg["shuffle"] = not cfg["shuffle"]
            state = "ON ✅" if cfg["shuffle"] else "OFF ❌"
            await safe_reply(query.message, f"🔀 Questions Shuffle: *{state}*")

        elif data == "toggle_negative":
            cfg["negative"] = not cfg["negative"]
            state = "ON ✅ (-0.25)" if cfg["negative"] else "OFF ❌"
            await safe_reply(query.message, f"⚠️ Negative Marking: *{state}*")

        elif data.startswith("set_lang:"):
            try:
                lang = data.split(":")[1]
            except IndexError:
                return
            if lang not in ("en", "hi"):
                return
            cfg["language"] = lang
            state = "English 🇬🇧" if lang == "en" else "हिंदी 🇮🇳"
            await safe_reply(query.message, f"🌐 Quiz/GK Language set to: *{state}*")

        elif data.startswith("load_quiz:"):
            try:
                idx = int(data.split(":")[1])
            except (IndexError, ValueError):
                await query.message.reply_text("⚠️ Invalid quiz selection.")
                return

            user_quizzes = saved_quizzes_db.get(user_id, [])
            if idx < 0 or idx >= len(user_quizzes):
                await query.message.reply_text("⚠️ Ye quiz ab available nahi hai.")
                return

            qz = user_quizzes[idx]
            title, questions = sanitize_and_prepare(
                {"title": qz.get("title"), "questions": qz.get("questions", [])},
                cfg["shuffle"],
            )
            await setup_ready_card(query.message, context, user_id, title, questions)
    except Exception:
        logger.exception("on_button_click error")
        try:
            await query.message.reply_text("⚠️ Kuch gadbad ho gayi, dobara try karein.")
        except Exception:
            pass

# ============================================================
# 9. MAIN APP INITIALIZER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled bot exception:", exc_info=context.error)

def main():
    if not BOT_TOKEN or not GEMINI_API_KEY:
        print("❌ Error: BOT_TOKEN ya GEMINI_API_KEY set nahi hai!")
        return

    # Default Menu Toggle button ko bypass karke standard Application build
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CommandHandler("stop", stop_quiz))

    # Error handler
    app.add_error_handler(error_handler)

    # Handlers for search bar keyboard buttons, text, photos & polls
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), handle_inputs))
    app.add_handler(CallbackQueryHandler(on_button_click))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    print("🤖 Ultra-Fast QuizBot & GK Assistant Running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
