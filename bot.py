import os
import json
import logging
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Current Gemini vision model
GEMINI_MODEL = "gemini-3.8-flash"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# GEMINI CLIENT
# ============================================================

if GEMINI_API_KEY:
    gemini_client = genai.Client(
        api_key=GEMINI_API_KEY
    )
else:
    gemini_client = None


# ============================================================
# USER QUIZ DATA
# ============================================================

# context.user_data में हर user का अलग quiz रहेगा.


# ============================================================
# AI PROMPT
# ============================================================

MCQ_PROMPT = r"""
तुम एक बहुत सख्त परीक्षा-प्रश्न निर्माता हो।

तुम्हें एक किताब के पेज की IMAGE दी गई है।

तुम्हारा काम:

1. IMAGE को ध्यान से पढ़ो।
2. केवल IMAGE में दिखाई देने वाली जानकारी का उपयोग करो।
3. IMAGE के बाहर की कोई जानकारी जोड़कर प्रश्न मत बनाओ।
4. IMAGE में दिए गए facts, definitions, names, dates, classifications,
   scientific terms, examples और statements से अधिकतम अच्छे objective
   questions बनाओ।
5. हर प्रश्न के ठीक 4 options होने चाहिए।
6. केवल एक option सही होना चाहिए।
7. सही answer का index 0, 1, 2 या 3 होना चाहिए।
8. प्रश्न हिंदी में बनाओ।
9. Options हिंदी/अंग्रेजी उसी तरह रखो जैसा किताब में आवश्यक हो।
10. यदि किसी fact को IMAGE से निश्चित रूप से समझना संभव नहीं है,
    तो उस fact से प्रश्न मत बनाओ।
11. कोई duplicate question मत बनाओ।
12. एक ही fact को बार-बार अलग शब्दों में पूछकर questions की संख्या
    कृत्रिम रूप से मत बढ़ाओ।
13. जितने meaningful MCQ वास्तव में IMAGE से बन सकते हैं,
    उतने ही बनाओ।
14. अगर 20 बनते हैं तो 20 बनाओ।
15. अगर केवल 7 अच्छे प्रश्न बनते हैं तो केवल 7 बनाओ।
16. अनुमान लगाकर प्रश्न मत बनाओ।
17. सही answer को दोबारा IMAGE के content से verify करो।
18. विशेष रूप से नाम, वर्ष, वैज्ञानिक नाम और वर्गीकरण में गलती मत करो।

बहुत महत्वपूर्ण:

IMAGE में अगर कोई highlighted/marked text है तो उसे भी पढ़ो,
लेकिन केवल highlighted text तक सीमित मत रहो।
पूरे visible page के relevant content से प्रश्न बनाओ।

OUTPUT में केवल valid JSON दो।
कोई explanation, markdown या ```json block मत देना।

JSON format:

{
  "questions": [
    {
      "question": "प्रश्न",
      "options": [
        "विकल्प 1",
        "विकल्प 2",
        "विकल्प 3",
        "विकल्प 4"
      ],
      "answer": 0
    }
  ]
}
"""


# ============================================================
# GEMINI IMAGE -> MCQ
# ============================================================

def generate_mcqs_from_image(image_bytes: bytes):

    if not gemini_client:
        raise RuntimeError(
            "GEMINI_API_KEY सेट नहीं है।"
        )

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(
                data=image_bytes,
                mime_type="image/jpeg",
            ),
            MCQ_PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    if not response.text:
        raise RuntimeError(
            "Gemini ने कोई response नहीं दिया।"
        )

    return json.loads(response.text)


# ============================================================
# VALIDATE MCQS
# ============================================================

def validate_mcqs(data):

    if not isinstance(data, dict):
        return []

    questions = data.get("questions", [])

    if not isinstance(questions, list):
        return []

    valid = []

    for item in questions:

        if not isinstance(item, dict):
            continue

        question = str(
            item.get("question", "")
        ).strip()

        options = item.get(
            "options",
            []
        )

        answer = item.get(
            "answer",
            -1
        )

        if not question:
            continue

        if not isinstance(options, list):
            continue

        options = [
            str(x).strip()
            for x in options
            if str(x).strip()
        ]

        # Exactly 4 options
        if len(options) != 4:
            continue

        try:
            answer = int(answer)
        except Exception:
            continue

        if answer not in (0, 1, 2, 3):
            continue

        valid.append(
            {
                "question": question,
                "options": options,
                "answer": answer,
            }
        )

    return valid


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📚 *Image → MCQ Quiz Bot*\n\n"
        "किताब के किसी भी पेज की साफ़ फोटो भेजें।\n\n"
        "मैं उसी पेज के content से objective questions "
        "बनाकर Telegram में quiz कराऊँगा।\n\n"
        "📷 बस फोटो भेजें।",
        parse_mode="Markdown",
    )


# ============================================================
# IMAGE HANDLER
# ============================================================

async def handle_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    status = await update.message.reply_text(
        "🔍 फोटो पढ़ी जा रही है...\n\n"
        "कृपया थोड़ा इंतज़ार करें।"
    )

    try:

        # Highest resolution Telegram photo
        photo = update.message.photo[-1]

        telegram_file = await photo.get_file()

        image_bytes = await telegram_file.download_as_bytearray()

        image_bytes = bytes(image_bytes)

    except Exception as e:

        logger.exception(
            "Telegram image download failed"
        )

        await status.edit_text(
            "❌ फोटो डाउनलोड नहीं हो सकी।\n\n"
            f"Error: {e}"
        )

        return

    # --------------------------------------------------------
    # AI PROCESSING
    # --------------------------------------------------------

    try:

        await status.edit_text(
            "🧠 AI पेज को पढ़ रहा है...\n\n"
            "सिर्फ इसी फोटो के content से MCQ बनाए जा रहे हैं।"
        )

        data = await asyncio.to_thread(
            generate_mcqs_from_image,
            image_bytes
        )

        questions = validate_mcqs(data)

    except Exception as e:

        logger.exception(
            "Gemini processing failed"
        )

        await status.edit_text(
            "❌ फोटो से MCQ बनाने में समस्या हुई।\n\n"
            f"Error: {e}"
        )

        return

    # --------------------------------------------------------
    # NO QUESTIONS
    # --------------------------------------------------------

    if not questions:

        await status.edit_text(
            "⚠️ इस फोटो से कोई भरोसेमंद MCQ नहीं बन पाया।\n\n"
            "कृपया साफ़ और सीधी फोटो भेजें।"
        )

        return

    # --------------------------------------------------------
    # SAVE QUIZ
    # --------------------------------------------------------

    context.user_data["questions"] = questions
    context.user_data["q_index"] = 0
    context.user_data["score"] = 0

    # --------------------------------------------------------
    # START QUIZ
    # --------------------------------------------------------

    await status.delete()

    await update.message.reply_text(
        f"✅ *{len(questions)} MCQ तैयार हैं!*\n\n"
        "अब Quiz शुरू करते हैं 👇",
        parse_mode="Markdown",
    )

    await send_question(
        update.message,
        context
    )


# ============================================================
# SEND QUESTION
# ============================================================

async def send_question(
    message,
    context: ContextTypes.DEFAULT_TYPE
):

    questions = context.user_data.get(
        "questions",
        []
    )

    index = context.user_data.get(
        "q_index",
        0
    )

    if not questions:
        return

    # --------------------------------------------------------
    # QUIZ FINISHED
    # --------------------------------------------------------

    if index >= len(questions):

        score = context.user_data.get(
            "score",
            0
        )

        total = len(questions)

        percentage = round(
            (score / total) * 100
        ) if total else 0

        await message.reply_text(
            "🏁 *QUIZ COMPLETE!*\n\n"
            f"📊 कुल प्रश्न: {total}\n"
            f"✅ सही: {score}\n"
            f"❌ गलत: {total - score}\n"
            f"📈 प्रतिशत: {percentage}%\n\n"
            "📷 नया पेज भेजकर नया Quiz बना सकते हैं।",
            parse_mode="Markdown",
        )

        return

    q = questions[index]

    keyboard = []

    for i, option in enumerate(
        q["options"]
    ):

        # Callback में केवल छोटा index भेजें
        # ताकि Telegram callback_data limit से समस्या न हो।

        keyboard.append([
            InlineKeyboardButton(
                f"{chr(65 + i)}. {option}",
                callback_data=f"answer:{index}:{i}",
            )
        ])

    markup = InlineKeyboardMarkup(
        keyboard
    )

    await message.reply_text(
        f"📝 *प्रश्न {index + 1}/{len(questions)}*\n\n"
        f"{q['question']}",
        reply_markup=markup,
        parse_mode="Markdown",
    )


# ============================================================
# ANSWER HANDLER
# ============================================================

async def answer_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    data = query.data

    if not data.startswith("answer:"):
        return

    try:

        parts = data.split(":")

        question_index = int(parts[1])
        selected_index = int(parts[2])

    except Exception:

        return

    questions = context.user_data.get(
        "questions",
        []
    )

    current_index = context.user_data.get(
        "q_index",
        0
    )

    # पुराने button पर click रोकना
    if question_index != current_index:
        await query.answer(
            "यह सवाल पहले ही पूरा हो चुका है।",
            show_alert=True,
        )
        return

    if question_index >= len(questions):
        return

    q = questions[question_index]

    correct_index = q["answer"]

    # --------------------------------------------------------
    # CORRECT
    # --------------------------------------------------------

    if selected_index == correct_index:

        context.user_data["score"] = (
            context.user_data.get(
                "score",
                0
            ) + 1
        )

        result_text = (
            "✅ *सही उत्तर!*\n\n"
            f"✔️ {q['options'][correct_index]}"
        )

    # --------------------------------------------------------
    # WRONG
    # --------------------------------------------------------

    else:

        result_text = (
            "❌ *गलत उत्तर!*\n\n"
            f"✅ सही उत्तर: "
            f"*{q['options'][correct_index]}*"
        )

    # --------------------------------------------------------
    # REMOVE OLD BUTTONS
    # --------------------------------------------------------

    try:

        await query.edit_message_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    await query.message.reply_text(
        result_text,
        parse_mode="Markdown",
    )

    # Next question
    context.user_data["q_index"] = (
        current_index + 1
    )

    await send_question(
        query.message,
        context
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Unhandled bot error:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        print(
            "❌ BOT_TOKEN नहीं मिला!"
        )

        return

    if not GEMINI_API_KEY:

        print(
            "❌ GEMINI_API_KEY नहीं मिला!"
        )

        return

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # Photo
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_image
        )
    )

    # Answer buttons
    app.add_handler(
        CallbackQueryHandler(
            answer_handler,
            pattern=r"^answer:"
        )
    )

    # Error
    app.add_error_handler(
        error_handler
    )

    print(
        "🤖 Image → MCQ Telegram Bot Started!"
    )

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
