import os
import re
import json
import logging
import asyncio
import random
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Poll,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PollAnswerHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types

# ============================================================
# 1. RENDER HEALTH SERVER (LIGHTWEIGHT BACKGROUND THREAD)
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - QuizBot Live")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# ============================================================
# 2. CONFIGURATION & LOGGING
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

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
        logger.error(f"Gemini init error: {e}")

TITLE, DESCRIPTION, NEGATIVE, SHUFFLE, TIMER, ADD_QUESTIONS = range(6)

quizzes_store = {}
active_polls = {}

def md_escape(text: str) -> str:
    if not text:
        return ""
    for ch in ["_", "*", "`", "["]:
        text = str(text).replace(ch, "\\" + ch)
    return text

# ============================================================
# 3. BULK QUESTION PARSER (REGEX + GEMINI FALLBACK)
# ============================================================

def parse_questions_regex(text: str):
    questions = []
    blocks = re.split(r'\n(?=(?:Q\d*[\.:\-]|Question\s*\d*[\.:\-]|Sawál|\d+[\.\)]\s+))', text, flags=re.IGNORECASE)
    
    for block in blocks:
        lines = [l.strip() for l in block.strip().split('\n') if l.strip()]
        if len(lines) < 3:
            continue
        
        q_line = lines[0]
        q_text = re.sub(r'^(?:Q\d*[\.:\-]|Question\s*\d*[\.:\-]|Sawál|\d+[\.\)]\s*)', '', q_line).strip()
        
        opts = []
        ans_idx = 0
        ans_found = False
        
        for l in lines[1:]:
            ans_match = re.search(r'(?:Ans|Answer|Uttar|उत्तर)[\s\:\-\)]*\(?([A-Da-d1-4])\)?', l, re.IGNORECASE)
            if ans_match:
                key = ans_match.group(1).upper()
                mapping = {'A': 0, 'B': 1, 'C': 2, 'D': 3, '1': 0, '2': 1, '3': 2, '4': 3}
                ans_idx = mapping.get(key, 0)
                ans_found = True
                continue
            
            opt_match = re.match(r'^\(?([A-Da-d1-4])[\.\)\-\:]\s*(.+)', l)
            if opt_match:
                opts.append(opt_match.group(2).strip()[:90])
            elif not ans_found and len(opts) < 4 and len(l) < 90:
                opts.append(l)

        if len(opts) >= 2:
            while len(opts) < 4:
                opts.append(f"Option {len(opts)+1}")
            questions.append({
                "question": q_text[:280],
                "options": opts[:4],
                "answer": min(ans_idx, len(opts[:4])-1)
            })
    return questions

def parse_with_gemini(raw_text: str):
    if not gemini_client:
        return []
    prompt = """Extract all multiple choice questions from this text. 
Strict JSON format:
[
  {"question": "text", "options": ["A", "B", "C", "D"], "answer": 0}
]
Answer must be integer 0-3. Max 4 options. Return ONLY valid JSON."""
    try:
        res = gemini_client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[raw_text, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        data = json.loads(res.text)
        if isinstance(data, dict):
            data = data.get("questions", [])
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Gemini parse error: {e}")
        return []

# ============================================================
# 4. CONVERSATION HANDLERS (/creat_quiz)
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 *Welcome to Advance MCQ Quiz Bot!*\n\n"
        "✨ *Main Features:*\n"
        "🔹 Create quizzes from Text or `.txt` bulk file\n"
        "🔹 **Auto-Split Bulk Quizzes:** 100, 200, 500 sawal ko automatic 50-50 sets me divide karega\n"
        "🔹 Negative marking aur Timer support\n"
        "🔹 Apne quizzes manage karein: `/my_store`\n\n"
        "👉 Quiz banane ke liye click karein: /creat_quiz"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")

async def creat_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["creating_quiz"] = {"questions": []}
    await update.message.reply_text(
        "📝 *Start creating a new quiz:*\n\nPlease send the *Title* of your quiz:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return TITLE

async def quiz_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["creating_quiz"]["title"] = update.message.text.strip()
    kb = ReplyKeyboardMarkup([["Skip ⏭️"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "📝 *Description:*\n\nProvide a short description (or press Skip):",
        parse_mode="Markdown",
        reply_markup=kb
    )
    return DESCRIPTION

async def quiz_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["creating_quiz"]["description"] = "" if text == "Skip ⏭️" else text
    
    neg_kb = ReplyKeyboardMarkup([["0", "0.25"], ["0.33", "0.66"]], resize_keyboard=True)
    await update.message.reply_text(
        "⚖️ *Negative Marking:*\n\nNegative marking select karein:",
        parse_mode="Markdown",
        reply_markup=neg_kb
    )
    return NEGATIVE

async def quiz_negative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip())
    except ValueError:
        val = 0.0
    context.user_data["creating_quiz"]["negative"] = val
    
    shuffle_kb = ReplyKeyboardMarkup([
        ["No Shuffle", "Answer Only"],
        ["Question Only", "Both (Ans & Que)"]
    ], resize_keyboard=True)
    
    await update.message.reply_text(
        f"✅ *Negative marking set to {val}.*\n\n🔀 *Shuffle Options:*",
        parse_mode="Markdown",
        reply_markup=shuffle_kb
    )
    return SHUFFLE

async def quiz_shuffle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sh_text = update.message.text.strip()
    context.user_data["creating_quiz"]["shuffle"] = sh_text
    
    timer_kb = ReplyKeyboardMarkup([["10", "15", "30"], ["45", "60", "Bina Timer (0)"]], resize_keyboard=True)
    await update.message.reply_text(
        f"✅ *Shuffle type set to {sh_text}.*\n\n⏱ *Timer:*\nNiche se timer duration select karein (seconds):",
        parse_mode="Markdown",
        reply_markup=timer_kb
    )
    return TIMER

async def quiz_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t_text = update.message.text.strip()
    timer_val = 0 if "Bina Timer" in t_text else int(re.sub(r'\D', '', t_text) or 15)
    context.user_data["creating_quiz"]["timer"] = timer_val
    
    done_kb = ReplyKeyboardMarkup([["/done", "/cancel"]], resize_keyboard=True)
    await update.message.reply_text(
        f"✅ *Timer set to {timer_val}s.*\n\n"
        "📊 *Send Questions (Bulk or Single):*\n"
        "Ab aap yahan 50, 100, 200, 300 ya 600 sawal text ya `.txt` file me bhejein.\n"
        "*(Note: 50 se zyada questions hone par bot 50-50 ke sets bana dega)*\n\n"
        "Jab saare sawal bhej chuke hon, tab niche **/done** dabayein.",
        parse_mode="Markdown",
        reply_markup=done_kb
    )
    return ADD_QUESTIONS

async def add_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_content = ""
    if update.message.document:
        doc = update.message.document
        t_file = await doc.get_file()
        raw = await t_file.download_as_bytearray()
        text_content = bytes(raw).decode("utf-8", errors="ignore")
    elif update.message.text:
        text_content = update.message.text

    if not text_content.strip():
        await update.message.reply_text("⚠️ Kripya valid text ya `.txt` file upload karein.")
        return ADD_QUESTIONS

    status = await update.message.reply_text("⏳ Processing questions...")
    extracted = parse_questions_regex(text_content)
    if not extracted:
        extracted = await asyncio.to_thread(parse_with_gemini, text_content)

    if not extracted:
        await status.edit_text("❌ Sawal read nahi ho sake. Format check karein.")
        return ADD_QUESTIONS

    context.user_data["creating_quiz"]["questions"].extend(extracted)
    total = len(context.user_data["creating_quiz"]["questions"])
    await status.edit_text(
        f"✅ *+{len(extracted)} sawal add hue! Total: {total} questions.*\n\n"
        "Aur add karna chahein toh bhejein, warna **/done** dabayein.",
        parse_mode="Markdown"
    )
    return ADD_QUESTIONS

async def quiz_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    qdata = context.user_data.get("creating_quiz", {})
    questions = qdata.get("questions", [])

    if not questions:
        await update.message.reply_text("❌ Koi sawal add nahi kiya gaya.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if user_id not in quizzes_store:
        quizzes_store[user_id] = []

    title = qdata.get("title", "Exam Quiz")
    sh_mode = qdata.get("shuffle", "No Shuffle")
    timer = qdata.get("timer", 15)
    negative = qdata.get("negative", 0.0)

    CHUNK_SIZE = 50
    chunks = [questions[i:i + CHUNK_SIZE] for i in range(0, len(questions), CHUNK_SIZE)]
    
    created_list = []
    for idx, chunk in enumerate(chunks):
        part_suffix = f" [Part {idx+1}]" if len(chunks) > 1 else ""
        part_title = f"{title}{part_suffix}"
        
        quiz_obj = {
            "title": part_title,
            "description": qdata.get("description", ""),
            "negative": negative,
            "shuffle": sh_mode,
            "timer": timer,
            "questions": chunk
        }
        quizzes_store[user_id].append(quiz_obj)
        created_list.append(f"• *{md_escape(part_title)}* ({len(chunk)} Qs)")

    summary = (
        f"🎉 *Quiz Successfully Created!*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total Questions: *{len(questions)}*\n"
        f"📦 Total Sets (50 each): *{len(chunks)}*\n\n"
        + "\n".join(created_list) +
        f"\n━━━━━━━━━━━━━━━━━━━\n"
        f"Khelne ke liye click karein: /my_store"
    )

    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    context.user_data.pop("creating_quiz", None)
    return ConversationHandler.END

async def quiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("creating_quiz", None)
    await update.message.reply_text("❌ Quiz creation process cancel kar diya gaya.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ============================================================
# 5. /my_store & QUIZ ENGINE
# ============================================================

async def my_store_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_quizzes = quizzes_store.get(user_id, [])
    
    if not user_quizzes:
        await update.message.reply_text("📂 Koi saved quiz nahi mila. Naya quiz banane ke liye /creat_quiz dabayein.")
        return

    buttons = []
    for i, q in enumerate(user_quizzes):
        buttons.append([InlineKeyboardButton(f"▶️ {q['title']} ({len(q['questions'])} Qs)", callback_data=f"play_quiz:{i}")])

    await update.message.reply_text(
        "📚 *Aapke Saved Quizzes:* (Click karke shuru karein)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def on_quiz_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    idx = int(query.data.split(":")[1])
    quiz = quizzes_store.get(user_id, [])[idx]
    
    questions = [dict(q) for q in quiz["questions"]]
    if "Both" in quiz["shuffle"] or "Question" in quiz["shuffle"]:
        random.shuffle(questions)
    if "Both" in quiz["shuffle"] or "Answer" in quiz["shuffle"]:
        for q in questions:
            opts = list(q["options"])
            ans_val = opts[q["answer"]]
            random.shuffle(opts)
            q["options"] = opts
            q["answer"] = opts.index(ans_val)

    context.user_data["active_quiz"] = questions
    context.user_data["quiz_meta"] = quiz
    context.user_data["q_index"] = 0
    context.user_data["score"] = 0.0
    context.user_data["correct_count"] = 0
    context.user_data["wrong_count"] = 0
    context.user_data["skipped_count"] = 0

    await query.edit_message_text(
        f"🏁 *Ready for '{md_escape(quiz['title'])}'?*\n\n"
        f"• Questions: *{len(questions)}*\n"
        f"• Timer: *{quiz['timer']}s*\n"
        f"• Negative Marking: *{quiz['negative']}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start Now", callback_data="run_first_poll")]])
    )

async def run_first_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.delete_message()
    except Exception:
        pass
    await send_exam_poll(query.message.chat_id, context)

async def send_exam_poll(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    questions = context.user_data.get("active_quiz", [])
    index = context.user_data.get("q_index", 0)
    meta = context.user_data.get("quiz_meta", {})

    if index >= len(questions):
        correct = context.user_data.get("correct_count", 0)
        wrong = context.user_data.get("wrong_count", 0)
        skipped = context.user_data.get("skipped_count", 0)
        final_score = context.user_data.get("score", 0.0)
        total = len(questions)

        report = (
            f"🏁 *QUIZ COMPLETE REPORT*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Quiz: *{md_escape(meta.get('title','Quiz'))}*\n"
            f"📊 Total Questions: *{total}*\n"
            f"✅ Correct: *{correct}*\n"
            f"❌ Wrong: *{wrong}*\n"
            f"⏳ Skipped: *{skipped}*\n"
            f"🎯 Final Marks: *{final_score:.2f} / {total}*\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
        context.user_data.pop("active_quiz", None)
        return

    q = questions[index]
    timer_sec = meta.get("timer", 15)

    poll_args = {
        "chat_id": chat_id,
        "question": f"[{index + 1}/{len(questions)}] {q['question']}"[:290],
        "options": q["options"][:4],
        "type": Poll.QUIZ,
        "correct_option_id": q["answer"],
        "is_anonymous": False,
    }
    if timer_sec >= 5:
        poll_args["open_period"] = timer_sec

    msg = await context.bot.send_poll(**poll_args)
    active_polls[msg.poll.id] = {
        "chat_id": chat_id,
        "correct_id": q["answer"],
        "index": index,
        "answered": False
    }

    if timer_sec >= 5:
        asyncio.create_task(auto_advance_poll(msg.poll.id, chat_id, index, timer_sec, context))

async def auto_advance_poll(poll_id: str, chat_id: int, exp_idx: int, timer_sec: int, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(timer_sec)
    if poll_id in active_polls:
        data = active_polls.pop(poll_id)
        if not data["answered"] and context.user_data.get("q_index") == exp_idx:
            context.user_data["skipped_count"] = context.user_data.get("skipped_count", 0) + 1
            context.user_data["q_index"] = exp_idx + 1
            await send_exam_poll(chat_id, context)

async def handle_poll_ans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    if not ans or ans.poll_id not in active_polls:
        return

    data = active_polls.pop(ans.poll_id)
    data["answered"] = True
    meta = context.user_data.get("quiz_meta", {})
    neg = meta.get("negative", 0.0)

    selected = ans.option_ids[0] if ans.option_ids else -1
    if selected == data["correct_id"]:
        context.user_data["score"] = context.user_data.get("score", 0.0) + 1.0
        context.user_data["correct_count"] = context.user_data.get("correct_count", 0) + 1
    else:
        context.user_data["score"] = context.user_data.get("score", 0.0) - neg
        context.user_data["wrong_count"] = context.user_data.get("wrong_count", 0) + 1

    context.user_data["q_index"] = data["index"] + 1
    await asyncio.sleep(0.8)
    await send_exam_poll(data["chat_id"], context)

async def stop_quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("active_quiz", None)
    await update.message.reply_text("🛑 Active quiz session band kar diya gaya hai.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception handling update:", exc_info=context.error)

# ============================================================
# 6. MAIN LAUNCHER
# ============================================================

def main():
    if not BOT_TOKEN:
        print("❌ Error: BOT_TOKEN set nahi hai.")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    conv = ConversationHandler(
        entry_points=[CommandHandler("creat_quiz", creat_quiz_start)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_title)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_description)],
            NEGATIVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_negative)],
            SHUFFLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_shuffle)],
            TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_timer)],
            ADD_QUESTIONS: [
                CommandHandler("done", quiz_finish),
                MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, add_questions_handler)
            ],
        },
        fallbacks=[CommandHandler("cancel", quiz_cancel)],
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("my_store", my_store_cmd))
    app.add_handler(CommandHandler("stop_quiz", stop_quiz_cmd))
    app.add_handler(conv)

    app.add_handler(CallbackQueryHandler(on_quiz_select, pattern=r"^play_quiz:\d+$"))
    app.add_handler(CallbackQueryHandler(run_first_poll, pattern=r"^run_first_poll$"))
    app.add_handler(PollAnswerHandler(handle_poll_ans))

    print("🚀 Exam Quiz Bot is Running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
