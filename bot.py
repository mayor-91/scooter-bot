import os
import json
import base64
import hashlib
import logging
import time
import asyncio
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_FILE = "/app/data/bot_state.json"
STATUS_EVERY = 10  # Статус каждые N обработанных фото

# Хранилище
stats = defaultdict(lambda: defaultdict(int))
photo_log = {}
total_photos = 0
total_defects_found = 0
seen_hashes = set()
counting_active = False
test_mode = False
test_limit = 0
test_count = 0
bot_chat_id = None
start_time = None        # Время начала подсчёта
photos_per_minute = []   # Список временных меток для расчёта скорости


def save_state():
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        state = {
            "stats": {k: dict(v) for k, v in stats.items()},
            "photo_log": {str(k): v for k, v in photo_log.items()},
            "total_photos": total_photos,
            "total_defects_found": total_defects_found,
            "seen_hashes": list(seen_hashes),
            "bot_chat_id": bot_chat_id,
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")


def load_state():
    global stats, photo_log, total_photos, total_defects_found, seen_hashes, bot_chat_id
    try:
        if not os.path.exists(DATA_FILE):
            logger.info("Файл состояния не найден — начинаем с нуля.")
            return
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        for loc, types in state.get("stats", {}).items():
            for typ, cnt in types.items():
                stats[loc][typ] = cnt
        photo_log = {int(k): v for k, v in state.get("photo_log", {}).items()}
        total_photos = state.get("total_photos", 0)
        total_defects_found = state.get("total_defects_found", 0)
        seen_hashes = set(state.get("seen_hashes", []))
        bot_chat_id = state.get("bot_chat_id")
        logger.info(f"Загружено: {total_photos} фото, {len(seen_hashes)} хэшей.")
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")


def get_speed_and_eta(remaining):
    """Считает скорость и оставшееся время"""
    now = time.time()
    # Берём метки за последние 5 минут
    recent = [t for t in photos_per_minute if now - t < 300]
    if len(recent) < 2:
        return None, None
    elapsed = now - recent[0]
    speed = len(recent) / elapsed * 60  # фото в минуту
    if speed <= 0:
        return None, None
    eta_minutes = remaining / speed
    return round(speed, 1), round(eta_minutes)


def format_eta(eta_minutes):
    if eta_minutes is None:
        return "считаю..."
    if eta_minutes < 1:
        return "меньше минуты"
    if eta_minutes < 60:
        return f"~{eta_minutes} мин"
    hours = eta_minutes // 60
    mins = eta_minutes % 60
    return f"~{hours}ч {mins}мин"


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


async def send_status(context, text):
    global bot_chat_id
    if bot_chat_id:
        try:
            await context.bot.send_message(chat_id=bot_chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Статус не отправлен: {e}")


async def analyze_photo(image_data: bytes) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_base64 = base64.standard_b64encode(image_data).decode("utf-8")
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64}},
                {"type": "text", "text": "Проанализируй дефекты на этом фото самоката."}
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
    global total_photos, total_defects_found, counting_active, test_mode, test_count

    if not counting_active:
        return

    # Тест режим: проверяем лимит
    if test_mode:
        if test_count >= test_limit:
            counting_active = False
            test_mode = False
            elapsed = round((time.time() - start_time) / 60, 1) if start_time else "?"
            await send_status(context,
                f"🏁 *Тест завершён!*\n"
                f"Обработано: {test_count} фото за {elapsed} мин\n"
                f"Найдено дефектов: {total_defects_found}\n\n"
                f"Нажми /menu для результатов."
            )
            return
        test_count += 1

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_data = await file.download_as_bytearray()

    photo_hash = hashlib.md5(bytes(image_data)).hexdigest()
    if photo_hash in seen_hashes:
        return  # Дубли пропускаем тихо

    try:
        result = await analyze_photo(bytes(image_data))

        if not result.get("is_scooter", True):
            return  # Не самокат — тихо пропускаем

        defects = result.get("defects", [])
        seen_hashes.add(photo_hash)
        photos_per_minute.append(time.time())

        if not defects:
            save_state()
            return

        total_photos += 1
        total_defects_found += len(defects)

        photo_log[total_photos] = {
            "file_id": photo.file_id,
            "defects": [{"location": d["location"], "type": d["type"]} for d in defects]
        }
        for d in defects:
            stats[d["location"]][d["type"]] += 1

        save_state()

        # Периодический статус каждые STATUS_EVERY фото
        if total_photos % STATUS_EVERY == 0:
            remaining = test_limit - test_count if test_mode else None
            speed, eta = get_speed_and_eta(remaining or 0)
            eta_str = format_eta(eta) if remaining else ""
            progress = f"{test_count}/{test_limit}" if test_mode else f"{total_photos}"

            msg = (
                f"📊 *Статус* — обработано {progress} фото\n"
                f"Дефектов найдено: {total_defects_found}\n"
                f"Скорость: {speed} фото/мин\n"
            )
            if remaining and eta:
                msg += f"Осталось: ~{remaining} фото, {eta_str}"
            await send_status(context, msg)

        # Каждое фото с дефектом — короткое уведомление
        else:
            lines = [f"📸 *#{total_photos}* — {len(defects)} деф."]
            for d in defects:
                lines.append(f"• {d['location']}: {d['type']}")
            await send_status(context, "\n".join(lines))

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await send_status(context, f"⚠️ Ошибка на фото #{total_photos + 1}: `{str(e)[:100]}`\nБот продолжает работу.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_chat_id
    bot_chat_id = update.effective_chat.id
    save_state()
    await _show_menu(update)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_chat_id
    bot_chat_id = update.effective_chat.id
    await _show_menu(update)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запросить текущий статус"""
    if not counting_active:
        await update.message.reply_text(
            f"⏹ *Бот остановлен*\nФото обработано: {total_photos}\nДефектов: {total_defects_found}",
            parse_mode="Markdown"
        )
        return

    remaining = test_limit - test_count if test_mode else None
    speed, eta = get_speed_and_eta(remaining or 0)
    progress = f"{test_count}/{test_limit}" if test_mode else str(total_photos)
    eta_str = format_eta(eta) if (remaining and eta) else "считаю..."

    elapsed_min = round((time.time() - start_time) / 60, 1) if start_time else "?"

    msg = (
        f"▶️ *Бот работает*\n"
        f"Обработано: {progress} фото\n"
        f"Дефектов: {total_defects_found}\n"
        f"Скорость: {speed if speed else '...'} фото/мин\n"
        f"Времени прошло: {elapsed_min} мин\n"
    )
    if remaining:
        msg += f"Осталось: ~{remaining} фото, {eta_str}"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def _show_menu(update):
    status = "▶️ активен" if counting_active else "⏹ остановлен"
    mode = f" (тест {test_count}/{test_limit})" if test_mode else ""
    keyboard = [
        [InlineKeyboardButton("▶️ Начать подсчёт", callback_data="start_count")],
        [InlineKeyboardButton("⏹ Остановить", callback_data="stop_count")],
        [InlineKeyboardButton("📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton("🔄 Сбросить всё", callback_data="reset")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ]
    await update.message.reply_text(
        f"🤖 *Бот анализа самокатов JET*\n"
        f"Статус: {status}{mode}\n"
        f"Фото: {total_photos} | Дефектов: {total_defects_found}\n"
        f"💾 Хэшей в памяти: {len(seen_hashes)}\n\n"
        f"/status — текущий прогресс",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global counting_active, test_mode, test_limit, test_count, bot_chat_id, start_time
    bot_chat_id = update.effective_chat.id
    try:
        n = int(context.args[0])
        test_limit = n
        test_count = 0
        test_mode = True
        counting_active = True
        start_time = time.time()
        photos_per_minute.clear()
        await update.message.reply_text(
            f"🧪 *Тест запущен!*\n"
            f"Обработаю первые *{n} фото* и остановлюсь.\n"
            f"Статус буду присылать каждые {STATUS_EVERY} фото.\n"
            f"Напиши /status чтобы узнать прогресс в любой момент.",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: `/test 20`", parse_mode="Markdown")


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(context.args[0])
        if n not in photo_log:
            await update.message.reply_text(f"Фото #{n} не найдено в логе.")
            return
        entry = photo_log[n]
        lines = [f"📸 *Фото #{n}*\n*Найденные дефекты:*"]
        for i, d in enumerate(entry["defects"], 1):
            lines.append(f"{i}. {d['location']}: {d['type']}")
        lines.append(f"\n✏️ Чтобы исправить:\n`/fix {n} Место: Тип | Место2: Тип2`")
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=entry["file_id"],
            caption="\n".join(lines),
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: `/photo 42`", parse_mode="Markdown")


async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global total_defects_found
    try:
        n = int(context.args[0])
        if n not in photo_log:
            await update.message.reply_text(f"Фото #{n} не найдено.")
            return
        raw = " ".join(context.args[1:])
        if not raw:
            await update.message.reply_text(f"Использование:\n`/fix {n} Место: Тип | Место2: Тип2`", parse_mode="Markdown")
            return
        new_defects = []
        for part in raw.split("|"):
            part = part.strip()
            if ":" in part:
                loc, typ = part.split(":", 1)
                new_defects.append({"location": loc.strip(), "type": typ.strip()})
        if not new_defects:
            await update.message.reply_text("Не удалось распознать формат.")
            return
        for d in photo_log[n]["defects"]:
            stats[d["location"]][d["type"]] -= 1
            if stats[d["location"]][d["type"]] <= 0:
                del stats[d["location"]][d["type"]]
            if not stats[d["location"]]:
                del stats[d["location"]]
        total_defects_found -= len(photo_log[n]["defects"])
        for d in new_defects:
            stats[d["location"]][d["type"]] += 1
        total_defects_found += len(new_defects)
        photo_log[n]["defects"] = new_defects
        save_state()
        lines = [f"✅ *Фото #{n} исправлено!*\n*Новые дефекты:*"]
        for d in new_defects:
            lines.append(f"• {d['location']}: {d['type']}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except (IndexError, ValueError) as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global counting_active, test_mode, bot_chat_id, stats, total_photos, total_defects_found, start_time

    query = update.callback_query
    await query.answer()
    bot_chat_id = update.effective_chat.id

    if query.data == "start_count":
        counting_active = True
        test_mode = False
        start_time = time.time()
        photos_per_minute.clear()
        await query.edit_message_text(
            "▶️ *Подсчёт начат!*\n"
            "Отправляй фото в группу — статусы буду слать сюда.\n"
            f"Статус каждые {STATUS_EVERY} фото, или напиши /status.\n\n"
            "Нажми /menu для управления.",
            parse_mode="Markdown"
        )

    elif query.data == "stop_count":
        counting_active = False
        test_mode = False
        await query.edit_message_text("⏹ *Подсчёт остановлен.*\nНажми /menu чтобы продолжить.", parse_mode="Markdown")

    elif query.data == "show_stats":
        await _send_stats(query.message.chat_id, context)

    elif query.data == "help":
        await query.edit_message_text(
            "ℹ️ *Инструкция*\n\n"
            "*Основные команды:*\n"
            "/menu — главное меню\n"
            "/status — текущий прогресс и скорость\n"
            "/test 20 — тест на 20 фото\n\n"
            "*Отладка:*\n"
            "`/photo 42` — показать фото #42 и дефекты\n"
            "`/fix 42 Место: Тип` — исправить дефекты\n"
            "Несколько через `|`:\n"
            "`/fix 42 Верхняя часть стойки: Царапины | Диск колеса: Облупившаяся краска`\n\n"
            "*Места дефектов:*\n"
            "• Диск колеса (передний/задний)\n"
            "• Тормозной диск/механизм\n"
            "• Верхняя часть стойки\n"
            "• Нижняя часть стойки\n"
            "• Дека (платформа)\n"
            "• Заднее/Переднее крыло\n"
            "• Фонарь (передний/задний)\n"
            "• Номерная табличка\n"
            "• Ручки руля\n\n"
            "*Типы дефектов:*\n"
            "• Облупившаяся краска\n"
            "• Царапины\n"
            "• Трещина/скол\n"
            "• Вмятина/деформация\n"
            "• Механическая поломка\n"
            "• Отсутствие детали\n\n"
            "Нажми /menu чтобы вернуться.",
            parse_mode="Markdown"
        )

    elif query.data == "reset":
        stats.clear()
        seen_hashes.clear()
        photo_log.clear()
        total_photos = 0
        total_defects_found = 0
        save_state()
        await query.edit_message_text("🔄 Всё сброшено. Нажми /menu чтобы начать заново.")


async def _send_stats(chat_id, context):
    if not stats:
        await context.bot.send_message(chat_id=chat_id, text="📊 Статистика пуста.")
        return
    lines = [f"📊 *Статистика дефектов*\nФото: {total_photos} | Дефектов: {total_defects_found}\n"]
    for location, types in sorted(stats.items()):
        loc_total = sum(types.values())
        lines.append(f"\n📍 *{location}* — {loc_total}")
        for defect_type, count in sorted(types.items(), key=lambda x: -x[1]):
            lines.append(f"  • {defect_type}: {count}")
    lines.append(f"\n\n🔢 *Итого: {total_defects_found}*")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


def main():
    load_state()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("photo", cmd_photo))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))
    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
