import os
import logging
import tempfile
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я *Стенограф* — твой помощник для расшифровки разговоров.\n\n"
        "📤 Отправь мне *голосовое сообщение* или *аудиофайл*, и я:\n\n"
        "🎙 Расшифрую что было сказано\n"
        "👥 Определю кто говорил\n"
        "📝 Сделаю краткую выжимку\n\n"
        "Просто запиши голосовое и отправь!",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Как пользоваться:*\n\n"
        "1. Запиши голосовое сообщение прямо в Telegram\n"
        "2. Отправь его мне\n"
        "3. Подожди 20-60 секунд\n"
        "4. Получи транскрипцию и выжимку\n\n"
        "Также можно отправить аудиофайл (MP3, WAV, OGG, M4A).\n\n"
        "Работает лучше всего с чёткой речью и без сильного шума.",
        parse_mode="Markdown"
    )


async def process_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # Определяем тип аудио
    if message.voice:
        file = message.voice
        file_ext = "ogg"
    elif message.audio:
        file = message.audio
        file_ext = "mp3"
    elif message.document and message.document.mime_type and "audio" in message.document.mime_type:
        file = message.document
        file_ext = "mp3"
    else:
        await message.reply_text("❌ Пожалуйста, отправь голосовое сообщение или аудиофайл.")
        return

    # Уведомляем что обрабатываем
    processing_msg = await message.reply_text("⏳ Получил! Расшифровываю...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        # Скачиваем файл
        tg_file = await context.bot.get_file(file.file_id)

        with tempfile.NamedTemporaryFile(suffix=f".{file_ext}", delete=False) as tmp:
            tmp_path = tmp.name

        await tg_file.download_to_drive(tmp_path)

        # Шаг 1: Whisper транскрипция
        await processing_msg.edit_text("🎙 Расшифровываю речь...")

        async with httpx.AsyncClient(timeout=120) as client:
            with open(tmp_path, "rb") as audio_file:
                whisper_response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data={
                        "model": "whisper-1",
                        "language": "ru",
                        "response_format": "text",
                    },
                    files={"file": (f"audio.{file_ext}", audio_file, f"audio/{file_ext}")},
                )

        if whisper_response.status_code != 200:
            raise Exception(f"Whisper error: {whisper_response.text}")

        transcript_text = whisper_response.text.strip()

        if not transcript_text:
            await processing_msg.edit_text("❌ Не удалось распознать речь. Попробуй записать чище.")
            return

        # Шаг 2: GPT — диаризация и выжимка
        await processing_msg.edit_text("🧠 Анализирую разговор...")

        async with httpx.AsyncClient(timeout=60) as client:
            gpt_response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Ты помогаешь анализировать транскрипции разговоров на русском языке. "
                                "Получишь текст расшифровки разговора.\n\n"
                                "Твоя задача:\n"
                                "1. Разбить текст по спикерам (Спикер А, Спикер Б и т.д.) — определи по смене темы, вопросам/ответам, логике диалога\n"
                                "2. Написать краткую выжимку: о чём говорили, ключевые решения, договорённости\n\n"
                                "Отвечай строго в формате:\n\n"
                                "👥 ДИАЛОГ:\n"
                                "[Спикер А]: текст\n"
                                "[Спикер Б]: текст\n"
                                "...\n\n"
                                "📋 ВЫЖИМКА:\n"
                                "текст выжимки"
                            )
                        },
                        {
                            "role": "user",
                            "content": f"Вот транскрипция:\n\n{transcript_text}"
                        }
                    ],
                    "max_tokens": 2000,
                },
            )

        if gpt_response.status_code != 200:
            raise Exception(f"GPT error: {gpt_response.text}")

        gpt_data = gpt_response.json()
        result = gpt_data["choices"][0]["message"]["content"]

        # Удаляем временный файл
        os.unlink(tmp_path)

        # Отправляем результат
        await processing_msg.delete()

        # Сначала оригинальный текст
        transcript_msg = f"📝 *Исходный текст:*\n\n{transcript_text}"
        if len(transcript_msg) > 4000:
            transcript_msg = transcript_msg[:4000] + "..."

        await message.reply_text(transcript_msg, parse_mode="Markdown")

        # Потом анализ
        result_msg = f"🤖 *Анализ разговора:*\n\n{result}"
        if len(result_msg) > 4000:
            result_msg = result_msg[:4000] + "..."

        await message.reply_text(result_msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error: {e}")
        try:
            os.unlink(tmp_path)
        except:
            pass
        await processing_msg.edit_text(
            f"❌ Произошла ошибка при обработке. Попробуй ещё раз.\n\nДетали: {str(e)[:200]}"
        )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, process_audio))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
