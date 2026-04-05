import os
import json
import base64
import hashlib
import logging
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic

# ===== ВСТАВЬ СЮДА СВОИ КЛЮЧИ =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# ====================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Хранилище статистики
stats = defaultdict(lambda: defaultdict(int))
total_photos = 0
total_defects_found = 0
seen_hashes = set()
counting_active = False  # Режим подсчёта вкл/выкл
bot_chat_id = None  # ID чата с самим ботом (куда слать статусы)

SYSTEM_PROMPT = """Ты эксперт по анализу дефектов электросамокатов JET (Segway).
Анализируй фото и определяй дефекты по следующим правилам:

СНАЧАЛА ОПРЕДЕЛИ ТИП ФОТО:
- Если на фото батарея/аккумулятор отдельно (не на самокате) → is_scooter: false
- Если на фото более двух самокатов одновременно (общий вид, фура, склад) → is_scooter: false
- Если на фото 1-2 самоката крупно с видимыми дефектами → анализируй дефекты
- Если фото не связано с самокатами → is_scooter: false

МЕСТА дефектов:
- Диск колеса (передний/задний)
- Тормозной диск/механизм
- Верхняя часть стойки
- Нижняя часть стойки
- Дека (платформа)
- Заднее крыло
- Переднее крыло
- Фонарь (передний/задний)
- Номерная табличка
- Ручки руля
- Аккумулятор/батарея

ТИПЫ дефектов:
- Облупившаяся краска
- Царапины
- Трещина/скол
- Вмятина/деформация
- Механическая поломка
- Отсутствие детали (номера, крышки и т.д.)
- Загрязнение (только если мешает функции)

ВАЖНЫЕ ПРАВИЛА:
- Грязь/загрязнение НЕ считается дефектом если это просто грязь
- Тормозной диск: смотри на деформацию и механические повреждения
- Отсутствие номерной таблички = отдельный дефект "Отсутствие номера"
- Не путай термины: просто "стойка верхняя/нижняя", без "складной механизм"
- Если на фото нет самоката или дефектов — верни пустой список

Отвечай ТОЛЬКО в формате JSON, без лишнего текста:
{
  "defects": [
    {"location": "место", "type": "тип дефекта"}
  ],
  "is_scooter": true/false
}"""


async def send_status(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Отправить статус боту в личку (в чат с самим собой)"""
    global bot_chat_id
    if bot_chat_id:
        try:
            await context.bot.send_message(chat_id=bot_chat_id, text=text)
        except Exception as e:
            logger.error(f"Не удалось отправить статус: {e}")


async def analyze_photo(image_data: bytes) -> dict:
    """Анализ фото через Claude API"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_base64 = base64.standard_b64encode(image_data).decode("utf-8")
    
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_base64,
                    },
                },
                {
                    "type": "text",
                    "text": "Проанализируй дефекты на этом фото самоката."
                }
            ],
        }],
    )
    
    response_text = message.content[0].text.strip()
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()
    
    return json.loads(response_text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящего фото — тихо в группе, статусы себе"""
    global total_photos, total_defects_found, counting_active

    # Если подсчёт не активен — игнорируем
    if not counting_active:
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_data = await file.download_as_bytearray()

    # Проверка на дубль
    photo_hash = hashlib.md5(bytes(image_data)).hexdigest()
    if photo_hash in seen_hashes:
        await send_status(context, "🔁 Дубль — пропускаю.")
        return

    try:
        result = await analyze_photo(bytes(image_data))

        if not result.get("is_scooter", True):
            await send_status(context, "⏭ Пропускаю: батарея, общий вид или не самокат.")
            return

        defects = result.get("defects", [])
        seen_hashes.add(photo_hash)

        if not defects:
            await send_status(context, f"✅ Фото #{total_photos + 1} — дефектов нет.")
            return

        total_photos += 1
        total_defects_found += len(defects)

        lines = [f"📸 Фото #{total_photos} — дефектов: {len(defects)}\n"]
        for d in defects:
            location = d["location"]
            defect_type = d["type"]
            stats[location][defect_type] += 1
            lines.append(f"• {location}: {defect_type}")

        lines.append(f"\n📊 Итого дефектов: {total_defects_found}")
        await send_status(context, "\n".join(lines))

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await send_status(context, "❌ Ошибка при анализе фото.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видео — молча игнорируем"""
    pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — регистрирует чат бота для статусов"""
    global bot_chat_id
    bot_chat_id = update.effective_chat.id

    keyboard = [
        [InlineKeyboardButton("▶️ Начать подсчёт", callback_data="start_count")],
        [InlineKeyboardButton("⏹ Остановить подсчёт", callback_data="stop_count")],
        [InlineKeyboardButton("📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton("🔄 Сбросить всё", callback_data="reset")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "👋 Привет! Я анализирую дефекты самокатов JET.\n\n"
        "📌 Добавь меня в группу где публикуются фото.\n"
        "📲 Статусы буду присылать сюда — в этот чат.\n\n"
        "Нажми *▶️ Начать подсчёт* когда готов:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок"""
    global counting_active, bot_chat_id, stats, total_photos, total_defects_found

    query = update.callback_query
    await query.answer()
    bot_chat_id = update.effective_chat.id

    if query.data == "start_count":
        counting_active = True
        await query.edit_message_text(
            "▶️ *Подсчёт начат!*\n\n"
            "Отправляй фото в группу — я буду присылать статусы сюда.\n"
            "Нажми /menu чтобы вернуть кнопки управления.",
            parse_mode="Markdown"
        )

    elif query.data == "stop_count":
        counting_active = False
        await query.edit_message_text(
            "⏹ *Подсчёт остановлен.*\n\nНажми /menu чтобы продолжить.",
            parse_mode="Markdown"
        )

    elif query.data == "show_stats":
        await _send_stats(query.message.chat_id, context)

    elif query.data == "reset":
        stats.clear()
        seen_hashes.clear()
        total_photos = 0
        total_defects_found = 0
        await query.edit_message_text("🔄 Статистика сброшена. Нажми /menu чтобы начать заново.")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню с кнопками"""
    global bot_chat_id
    bot_chat_id = update.effective_chat.id

    status = "▶️ активен" if counting_active else "⏹ остановлен"
    keyboard = [
        [InlineKeyboardButton("▶️ Начать подсчёт", callback_data="start_count")],
        [InlineKeyboardButton("⏹ Остановить подсчёт", callback_data="stop_count")],
        [InlineKeyboardButton("📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton("🔄 Сбросить всё", callback_data="reset")],
    ]
    await update.message.reply_text(
        f"🤖 Бот анализа самокатов\nСтатус: {status}\nОбработано фото: {total_photos}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _send_stats(chat_id, context):
    """Внутренняя функция отправки статистики"""
    if not stats:
        await context.bot.send_message(chat_id=chat_id, text="📊 Статистика пуста.")
        return

    lines = [f"📊 *Статистика дефектов*\nОбработано фото: {total_photos} | Дефектов: {total_defects_found}\n"]
    for location, types in sorted(stats.items()):
        lines.append(f"\n📍 *{location}*")
        for defect_type, count in sorted(types.items(), key=lambda x: -x[1]):
            lines.append(f"  • {defect_type}: {count}")

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
