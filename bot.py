import os
import json
import base64
import hashlib
import logging
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic

# ===== ВСТАВЬ СЮДА СВОИ КЛЮЧИ =====
TELEGRAM_TOKEN = "ВСТАВЬ_ТОКЕН_БОТА_СЮДА"
ANTHROPIC_API_KEY = "ВСТАВЬ_API_КЛЮЧ_СЮДА"
# ====================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Хранилище статистики
stats = defaultdict(lambda: defaultdict(int))
total_photos = 0
seen_hashes = set()  # Для защиты от дублей

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
    {"location": "место", "type": "тип дефекта"},
    {"location": "место", "type": "тип дефекта"}
  ],
  "is_scooter": true/false
}"""


async def analyze_photo(image_data: bytes) -> dict:
    """Анализ фото через Claude API"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    image_base64 = base64.standard_b64encode(image_data).decode("utf-8")
    
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[
            {
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
            }
        ],
    )
    
    response_text = message.content[0].text.strip()
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()
    
    return json.loads(response_text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящего фото"""
    global total_photos
    
    # Берём фото наилучшего качества
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_data = await file.download_as_bytearray()
    
    # Проверка на дубль по MD5 хэшу
    photo_hash = hashlib.md5(bytes(image_data)).hexdigest()
    if photo_hash in seen_hashes:
        await update.message.reply_text("🔁 Дубль — это фото уже было, пропускаю.")
        return
    
    await update.message.reply_text("🔍 Анализирую...")
    
    try:
        result = await analyze_photo(bytes(image_data))
        
        if not result.get("is_scooter", True):
            await update.message.reply_text("⏭ Пропускаю: батарея, общий вид или не самокат.")
            return
        
        defects = result.get("defects", [])
        
        if not defects:
            await update.message.reply_text("✅ Дефектов не обнаружено.")
            seen_hashes.add(photo_hash)
            return
        
        # Фото принято — сохраняем хэш
        seen_hashes.add(photo_hash)
        total_photos += 1
        
        # Сохраняем в статистику
        response_lines = [f"📸 Фото #{total_photos} — найдено дефектов: {len(defects)}\n"]
        for d in defects:
            location = d["location"]
            defect_type = d["type"]
            stats[location][defect_type] += 1
            response_lines.append(f"• {location}: {defect_type}")
        
        await update.message.reply_text("\n".join(response_lines))
        
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await update.message.reply_text("❌ Ошибка при анализе фото. Попробуй ещё раз.")


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать текущую статистику"""
    if not stats:
        await update.message.reply_text("📊 Статистика пуста. Отправь фото самокатов!")
        return
    
    lines = [f"📊 *Статистика дефектов* (обработано фото: {total_photos})\n"]
    
    total_defects = 0
    for location, types in sorted(stats.items()):
        lines.append(f"\n📍 *{location}*")
        for defect_type, count in sorted(types.items(), key=lambda x: -x[1]):
            lines.append(f"  • {defect_type}: {count}")
            total_defects += count
    
    lines.append(f"\n\n🔢 *Всего дефектов: {total_defects}*")
    
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def reset_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс статистики"""
    global total_photos
    stats.clear()
    seen_hashes.clear()
    total_photos = 0
    await update.message.reply_text("🔄 Статистика и список дублей сброшены.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я анализирую дефекты самокатов JET.\n\n"
        "📸 Отправляй фото — я определю дефекты и место.\n"
        "🔁 Дубли автоматически пропускаются.\n\n"
        "Команды:\n"
        "/stats — показать статистику\n"
        "/reset — сбросить статистику"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", show_stats))
    app.add_handler(CommandHandler("reset", reset_stats))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
