import os
import json
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - QuizBot is Live")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()


# ============================================================
# 2. CONFIGURATION & CREDENTIALS
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.6-flash"
QUIZ_TIMER_SECONDS = 15  # Official QuizBot standard timer

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    gemini_client = None

# Active tracking dictionaries
active_polls = {}
saved_quizzes_db = {}


# ============================================================
# 3. AI PROMPTS
# ============================================================

IMAGE_PROMPT = r"""
Extract all objective questions from this image for a Telegram Quiz.
Rules:
1. Valid JSON format only.
2. Max 4 options per question.
3. Telegram character constraint: Question max 280 characters, Options max 90 characters.
4. Correct answer must be index 0, 1, 2, or 3.
JSON Output Format:
{
  "title": "OCR Scanned Quiz",
  "questions": [
    {
      "question": "Question text here",
      "options": ["Option 1", "Option 2", "Option 3", "Option 4"],
      "answer": 0
    }
  ]
}
"""

BULK_TEXT_PROMPT = r"""
Extract and convert all questions from the provided text into a structured multiple choice quiz.
Rules:
1. Parse every single question present (e.g. 50, 100, 200+ questions).
2. Ensure each question has exactly 4 options and 1 correct answer index (0, 1, 2, or 3).
3. Question text under 280 characters, each option text under 90 characters.
4. Valid JSON output only.
JSON Output Format:
{
  "title": "Bulk Practice Quiz",
  "questions": [
    {
      "question": "Question text here",
      "options": ["Opt 1", "Opt 2", "Opt 3", "Opt 4"],
      "answer": 0
    }
  ]
}
"""

def parse_with_gemini(prompt: str, part: types.Part):
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY environment variable missing!")
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[part, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)


def sanitize_quiz_data(data: dict):
    title = str(data.get("title", "Practice Quiz")).strip()[:100]
    raw_questions = data.get("questions", [])
    valid = []

    for item in raw_questions:
        q = str(item.get("question", "")).strip()
        opts = item.get("options", [])
        ans = item.get("answer", 0)

        if not q or not isinstance(opts, list) or len(opts) < 2:
            continue

        clean_opts = [str(x).strip()[:95] for x in opts if str(x).strip()]
        while len(clean_opts) < 4:
            clean_opts.append(f"Option {len(clean_opts) + 1}")
        clean_opts = clean_opts[:4]

        try:
            ans = int(ans)
        except Exception:
            ans = 0
        if ans not in (0, 1, 2, 3):
            ans = 0

        valid.append({
            "question": q[:290],
            "options": clean_opts,
            "answer": ans,
        })
    return title, valid


# ============================================================
# 4. COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🎲 *Welcome to Quiz Master Bot!*\n\n"
        "Ye bot Telegram QuizBot style me kaam karta hai:\n\n"
        "📸 */scanquiz* - Photo scan karke direct quiz banayein\n"
        "✍️ */newquiz* - 50, 100, 200+ questions ka text/file bhejkar quiz banayein\n"
        "📁 */myquizzes* - Apne saved quizzes dekhein aur share karein\n"
        "🛑 */stop* - Chalu quiz ko turant rokein"
    )
    keyboard = [
        [InlineKeyboardButton("✍️ Create Text Quiz (/newquiz)", callback_data="cmd_newquiz")],
        [InlineKeyboardButton("📸 Scan Image Quiz (/scanquiz)", callback_data="cmd_scanquiz")],
        [InlineKeyboardButton("📁 View My Quizzes (/myquizzes)", callback_data="cmd_myquizzes")],
    ]
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def scanquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "AWAITING_IMAGE"
    await update.message.reply_text("📸 *Scan Mode Active:*\nAbhi question paper ya book page ki saaf photo bhejiye.", parse_mode="Markdown")


async def newquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "AWAITING_TEXT"
    await update.message.reply_text(
        "✍️ *Bulk Quiz Mode Active:*\n"
        "Apne questions text me yahan paste karein ya `.txt` file upload karein (100, 200, 500 jitne chahein).",
        parse_mode="Markdown"
    )


async def myquizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    quizzes = saved_quizzes_db.get(user_id, [])

    if not quizzes:
        await update.message.reply_text("📂 Aapke paas koi saved quiz nahi hai. /newquiz ya /scanquiz se banayein.")
        return

    keyboard = []
    for idx, qz in enumerate(quizzes):
        keyboard.append([InlineKeyboardButton(f"▶️ {qz['title']} ({len(qz['questions'])} Qs)", callback_data=f"load_quiz:{idx}")])

    await update.message.reply_text("📁 *Aapke Saved Quizzes:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("active_quiz", None)
    context.user_data.pop("q_index", None)
    await update.message.reply_text("🛑 Current quiz band ho gaya hai. Naya shuru karne ke liye /start dabayein.")


# ============================================================
# 5. INPUT PROCESSOR (IMAGE / TEXT / TXT FILE)
# ============================================================

async def handle_incoming_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 1. PHOTO INPUT
    if update.message.photo:
        status = await update.message.reply_text("🔍 Page scan ho raha hai...")
        try:
            photo = update.message.photo[-1]
            t_file = await photo.get_file()
            img_bytes = bytes(await t_file.download_as_bytearray())

            part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            data = await asyncio.to_thread(parse_with_gemini, IMAGE_PROMPT, part)
            title, questions = sanitize_quiz_data(data)

            if not questions:
                await status.edit_text("⚠️ Questions clear nahi the. Kripya dusri saaf photo bhejein.")
                return

            await status.delete()
            await setup_quiz_session(update.message, context, user_id, title, questions)
        except Exception as e:
            logger.exception("OCR Quiz failed")
            await status.edit_text(f"❌ Error: {e}")
        return

    # 2. DOCUMENT (.txt file) INPUT
    if update.message.document:
        doc = update.message.document
        if not doc.file_name.endswith((".txt", ".text")):
            await update.message.reply_text("⚠️ Kripya `.txt` text file upload karein.")
            return

        status = await update.message.reply_text("⚙️ File se questions parse ho rahe hain...")
        try:
            t_file = await doc.get_file()
            content = (await t_file.download_as_bytearray()).decode("utf-8", errors="ignore")
            part = types.Part.from_text(text=content)
            data = await asyncio.to_thread(parse_with_gemini, BULK_TEXT_PROMPT, part)
            title, questions = sanitize_quiz_data(data)

            if not questions:
                await status.edit_text("⚠️ File me valid questions nahi mile.")
                return

            await status.delete()
            await setup_quiz_session(update.message, context, user_id, title, questions)
        except Exception as e:
            logger.exception("File parse failed")
            await status.edit_text(f"❌ Error: {e}")
        return

    # 3. TEXT MESSAGE INPUT
    if update.message.text and not update.message.text.startswith("/"):
        status = await update.message.reply_text("⚙️ Text analyze ho raha hai...")
        try:
            part = types.Part.from_text(text=update.message.text)
            data = await asyncio.to_thread(parse_with_gemini, BULK_TEXT_PROMPT, part)
            title, questions = sanitize_quiz_data(data)

            if not questions:
                await status.edit_text("⚠️ Koi question nahi ban paya. Proper text paste karein.")
                return

            await status.delete()
            await setup_quiz_session(update.message, context, user_id, title, questions)
        except Exception as e:
            logger.exception("Text quiz failed")
            await status.edit_text(f"❌ Error: {e}")


async def setup_quiz_session(message, context: ContextTypes.DEFAULT_TYPE, user_id: int, title: str, questions: list):
    if user_id not in saved_quizzes_db:
        saved_quizzes_db[user_id] = []
    saved_quizzes_db[user_id].append({"title": title, "questions": questions})

    context.user_data["active_quiz"] = questions
    context.user_data["q_index"] = 0
    context.user_data["score"] = 0

    bot_username = context.bot.username or "Ocrquiz_bot"
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_username}?start=quiz&text=Play%20Quiz:%20{title}"
    group_url = f"https://t.me/{bot_username}?startgroup=true"

    share_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start this quiz", callback_data="start_loaded_quiz")],
        [InlineKeyboardButton("👥 Start quiz in group", url=group_url)],
        [InlineKeyboardButton("↗️ Share quiz", url=share_url)],
    ])

    ready_text = (
        f"🎲 Get ready for the quiz *'{title}'*\n\n"
        f"🖊 *{len(questions)} questions*\n"
        f"⏱ *{QUIZ_TIMER_SECONDS} seconds per question*\n"
        f"🗳 Votes are *visible* to the quiz owner\n\n"
        f"🏁 Press the button below when you are ready.\n"
        f"Send /stop to stop it."
    )
    ready_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("I am ready!", callback_data="start_loaded_quiz")]
    ])

    await message.reply_text(
        f"🎲 Quiz *'{title}'*\n🖊 *{len(questions)} questions*  •  ⏱ *{QUIZ_TIMER_SECONDS} sec*",
        reply_markup=share_markup,
        parse_mode="Markdown"
    )
    await message.reply_text(ready_text, reply_markup=ready_markup, parse_mode="Markdown")


# ============================================================
# 6. QUIZ RUNNER (TELEGRAM NATIVE POLLS + TIMER)
# ============================================================

async def send_next_poll(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    questions = context.user_data.get("active_quiz", [])
    index = context.user_data.get("q_index", 0)

    if index >= len(questions):
        score = context.user_data.get("score", 0)
        total = len(questions)
        pct = round((score / total) * 100) if total else 0

        summary = (
            "🏁 *QUIZ COMPLETE!*\n\n"
            f"📊 Total Questions: {total}\n"
            f"✅ Correct: {score}\n"
            f"❌ Wrong: {total - score}\n"
            f"📈 Accuracy: {pct}%\n\n"
            "Naya quiz start karne ke liye /scanquiz ya /newquiz karein."
        )
        await context.bot.send_message(chat_id=chat_id, text=summary, parse_mode="Markdown")
        return

    q = questions[index]

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"[{index + 1}/{len(questions)}] {q['question']}",
        options=q["options"],
        type=Poll.QUIZ,
        correct_option_id=q["answer"],
        open_period=QUIZ_TIMER_SECONDS,
        is_anonymous=False,
    )

    active_polls[msg.poll.id] = {
        "chat_id": chat_id,
        "correct_id": q["answer"],
        "index": index
    }

    # Auto-advance if timer runs out without answer
    asyncio.create_task(auto_advance_poll(msg.poll.id, chat_id, index, context))


async def auto_advance_poll(poll_id: str, chat_id: int, expected_index: int, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(QUIZ_TIMER_SECONDS + 1)
    if poll_id in active_polls:
        active_polls.pop(poll_id, None)
        if context.user_data.get("q_index") == expected_index:
            context.user_data["q_index"] = expected_index + 1
            await send_next_poll(chat_id, context)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id

    if poll_id not in active_polls:
        return

    poll_info = active_polls.pop(poll_id)
    selected = answer.option_ids[0] if answer.option_ids else -1

    if selected == poll_info["correct_id"]:
        context.user_data["score"] = context.user_data.get("score", 0) + 1

    context.user_data["q_index"] = poll_info["index"] + 1
    await asyncio.sleep(1.2)
    await send_next_poll(poll_info["chat_id"], context)


# ============================================================
# 7. BUTTON CALLBACK HANDLER
# ============================================================

async def on_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "start_loaded_quiz":
        if not context.user_data.get("active_quiz"):
            await query.message.reply_text("⚠️ Koi active quiz nahi mila. Naya banayein.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await send_next_poll(query.message.chat_id, context)

    elif data == "cmd_newquiz":
        context.user_data["mode"] = "AWAITING_TEXT"
        await query.message.reply_text("✍️ Yahan text questions paste karein ya `.txt` file upload karein.")

    elif data == "cmd_scanquiz":
        context.user_data["mode"] = "AWAITING_IMAGE"
        await query.message.reply_text("📸 Kitab ke page ki photo bhejiye.")

    elif data == "cmd_myquizzes":
        quizzes = saved_quizzes_db.get(user_id, [])
        if not quizzes:
            await query.message.reply_text("📂 Koi saved quiz nahi mila.")
            return
        keyboard = [[InlineKeyboardButton(f"▶️ {qz['title']}", callback_data=f"load_quiz:{i}")] for i, qz in enumerate(quizzes)]
        await query.message.reply_text("📁 Aapke Quizzes:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("load_quiz:"):
        idx = int(data.split(":")[1])
        qz = saved_quizzes_db[user_id][idx]
        context.user_data["active_quiz"] = qz["questions"]
        context.user_data["q_index"] = 0
        context.user_data["score"] = 0
        ready_markup = InlineKeyboardMarkup([[InlineKeyboardButton("I am ready!", callback_data="start_loaded_quiz")]])
        await query.message.reply_text(f"✅ Loaded: *{qz['title']}*\n\nPress below when ready:", reply_markup=ready_markup, parse_mode="Markdown")


# ============================================================
# 8. BOT COMMAND REGISTRATION (MENU TOGGLE)
# ============================================================

async def setup_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Main menu open karein"),
        BotCommand("scanquiz", "Photo scan karke quiz banayein"),
        BotCommand("newquiz", "100-500 Questions paste/upload karein"),
        BotCommand("myquizzes", "Saved quizzes dekhein"),
        BotCommand("stop", "Current quiz terminate karein"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("✅ Menu Commands successfully setup!")


def main():
    if not BOT_TOKEN or not GEMINI_API_KEY:
        print("❌ Error: BOT_TOKEN ya GEMINI_API_KEY set nahi hai!")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(setup_bot_commands).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scanquiz", scanquiz_command))
    app.add_handler(CommandHandler("newquiz", newquiz_command))
    app.add_handler(CommandHandler("myquizzes", myquizzes_command))
    app.add_handler(CommandHandler("stop", stop_command))

    # Handlers
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), handle_incoming_messages))
    app.add_handler(CallbackQueryHandler(on_button_click))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    print("🤖 QuizBot System Started Successfully!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
