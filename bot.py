# Telegram Quiz Bot with Dual OCR (API + Local Tesseract Fallback)
import logging
import random
import io
import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from PIL import Image
import pytesseract
import cv2
import numpy as np

# ============ CONFIG ============
TOKEN = os.environ.get("BOT_TOKEN", "8707481629:AAECuS2yPsbBv1bB42QVmC2uIxQw0aYBDU")
OCR_API_KEY = os.environ.get("OCR_API_KEY", "")

QUESTIONS = {
    "Science": [
        {"q": "पानी का रासायनिक सूत्र क्या है?", "options": ["H2O", "CO2", "NaCl", "HCl"], "ans": 0},
        {"q": "प्रकाश की गति कितनी है?", "options": ["3×10^8 m/s", "3×10^6 m/s", "3×10^10 m/s", "3×10^4 m/s"], "ans": 0},
    ],
    "Math": [
        {"q": "2+2 = ?", "options": ["3", "4", "5", "6"], "ans": 1},
        {"q": "√16 = ?", "options": ["2", "3", "4", "5"], "ans": 2},
    ],
    "History": [
        {"q": "भारत कब आज़ाद हुआ?", "options": ["1945", "1947", "1950", "1942"], "ans": 1},
    ]
}

# ============ SETUP ============
logging.basicConfig(level=logging.INFO)

# ============ HELPERS ============
def get_all_chapters():
    return list(QUESTIONS.keys())

def get_questions(chapter, shuffle=False):
    qs = QUESTIONS.get(chapter, [])
    if shuffle:
        random.shuffle(qs)
        for q in qs:
            random.shuffle(q['options'])
    return qs

def extract_mcq_from_text(text):
    lines = text.split('\n')
    options = []
    question = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith(('a)', 'b)', 'c)', 'd)', '1)', '2)', '3)', '4)')):
            options.append(line[2:].strip())
        elif not options and len(line) > 5:
            question = line
    return question, options

# ============ COMMANDS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chapters = get_all_chapters()
    keyboard = []
    row = []
    for ch in chapters:
        row.append(InlineKeyboardButton(ch, callback_data=f"chapter_{ch}"))
    keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🔍 सर्च करें", callback_data="search")])
    keyboard.append([InlineKeyboardButton("📷 इमेज स्कैन करें", callback_data="scan_image")])
    keyboard.append([InlineKeyboardButton("🔀 शफ़ल मोड", callback_data="toggle_shuffle")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📚 *क्विज़ बॉट में आपका स्वागत है!*\n\n"
        "नीचे किसी भी चैप्टर पर क्लिक करें या सर्च/स्कैन करें:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("chapter_"):
        chapter = data.replace("chapter_", "")
        context.user_data['current_chapter'] = chapter
        shuffle = context.user_data.get('shuffle_mode', False)
        questions = get_questions(chapter, shuffle)
        context.user_data['questions'] = questions
        context.user_data['q_index'] = 0
        await send_question(query.message, context)
    
    elif data == "search":
        await query.message.reply_text("🔍 *सर्च करें:* कोई भी चैप्टर या सवाल टाइप करें")
    
    elif data == "scan_image":
        await query.message.reply_text("📷 *कृपया एक इमेज भेजें* (किताब का पेज / सवाल)")
        context.user_data['scan_mode'] = True
    
    elif data == "toggle_shuffle":
        current = context.user_data.get('shuffle_mode', False)
        context.user_data['shuffle_mode'] = not current
        status = "✅ ऑन" if context.user_data['shuffle_mode'] else "❌ ऑफ"
        await query.message.reply_text(f"🔀 *शफ़ल मोड:* {status}")
        
        if 'current_chapter' in context.user_data:
            chapter = context.user_data['current_chapter']
            questions = get_questions(chapter, context.user_data['shuffle_mode'])
            context.user_data['questions'] = questions
            context.user_data['q_index'] = 0
            await send_question(query.message, context)
    
    elif data.startswith("opt_"):
        parts = data.split("_")
        opt_index = int(parts[1])
        questions = context.user_data.get('questions', [])
        q_index = context.user_data.get('q_index', 0)
        if q_index < len(questions):
            q = questions[q_index]
            correct = q['ans']
            if opt_index == correct:
                await query.message.reply_text("✅ *सही उत्तर!* 🎉", parse_mode="Markdown")
            else:
                await query.message.reply_text(f"❌ *गलत!* सही उत्तर: {q['options'][correct]}", parse_mode="Markdown")
            context.user_data['q_index'] = q_index + 1
            await send_question(query.message, context)

async def send_question(message, context):
    questions = context.user_data.get('questions', [])
    index = context.user_data.get('q_index', 0)
    
    if index >= len(questions):
        await message.reply_text("🏁 *क्विज़ खत्म!* सभी सवाल हो गए।", parse_mode="Markdown")
        return
    
    q = questions[index]
    keyboard = []
    for i, opt in enumerate(q['options']):
        keyboard.append([InlineKeyboardButton(opt, callback_data=f"opt_{i}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text(
        f"📝 *प्रश्न {index+1}/{len(questions)}:*\n{q['q']}",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

# ============ SEARCH ============
async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.startswith('/'):
        return
    
    found = []
    for ch, qs in QUESTIONS.items():
        if ch.lower() in text.lower():
            found.append(ch)
        for q in qs:
            if text.lower() in q['q'].lower():
                found.append(ch)
    
    if found:
        found = list(set(found))
        keyboard = []
        for ch in found:
            keyboard.append([InlineKeyboardButton(ch, callback_data=f"chapter_{ch}")])
        await update.message.reply_text(
            f"🔍 *सर्च रिजल्ट:* {len(found)} चैप्टर मिले",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ कोई रिजल्ट नहीं मिला।")

# ============ DUAL IMAGE SCAN ============
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('scan_mode', False):
        return
    
    status_msg = await update.message.reply_text("⏳ स्कैनिंग शुरू (Cloud OCR)...")
    photo_file = await update.message.photo[-1].get_file()
    extracted_text = ""
    
    # Method 1: OCR.space Cloud API
    if OCR_API_KEY:
        try:
            payload = {
                'apikey': OCR_API_KEY,
                'url': photo_file.file_path,
                'language': 'hin',
                'isOverlayRequired': False
            }
            res = requests.post('https://api.ocr.space/parse/image', data=payload, timeout=15).json()
            if res.get('ParsedResults'):
                extracted_text = res['ParsedResults'][0].get('ParsedText', '').strip()
        except Exception:
            pass

    # Method 2: Local Tesseract Fallback
    if not extracted_text:
        await status_msg.edit_text("⏳ Cloud OCR विफल, Local Tesseract से प्रोसेस हो रहा है...")
        try:
            image_bytes = await photo_file.download_as_bytearray()
            image = Image.open(io.BytesIO(image_bytes))
            img_np = np.array(image)
            gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
            _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
            extracted_text = pytesseract.image_to_string(thresh, lang='hin+eng').strip()
        except Exception as e:
            await status_msg.edit_text(f"❌ दोनों इंजन से स्कैन फेल: {str(e)}")
            return

    # MCQ Parser
    question, options = extract_mcq_from_text(extracted_text)
    
    if options and question:
        await status_msg.delete()
        context.user_data['questions'] = [{
            'q': question,
            'options': options[:4],
            'ans': 0
        }]
        context.user_data['q_index'] = 0
        context.user_data['scan_mode'] = False
        await send_question(update.message, context)
    else:
        await status_msg.edit_text(
            "⚠️ *इमेज से सवाल/ऑप्शन नहीं बना पाया।*\n"
            "कृपया साफ़ टेक्स्ट वाली इमेज भेजें।"
        )

# ============ MAIN ============
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    
    print("🤖 Bot started with Dual OCR Engine...")
    app.run_polling()

if __name__ == "__main__":
    main()
          
