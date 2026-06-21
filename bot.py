import re
import sqlite3
import logging
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz

TOKEN = "8246510815:AAF77G8ScTm1WNhSLbv5amdYik3eH_IkB5o"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
DB_PATH = "reports.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN)


# ── База данных ────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            object_name TEXT,
            chat_id INTEGER,
            chat_title TEXT,
            people_today INTEGER,
            people_planned INTEGER,
            completed_work TEXT,
            problems TEXT,
            needs_management TEXT,
            needs_customer TEXT,
            equipment_problems TEXT,
            raw_text TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bosses (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_report(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Удаляем старый отчёт по этому объекту за сегодня если есть (обновление)
    c.execute("DELETE FROM reports WHERE date=? AND object_name=? AND chat_id=?",
              (data["date"], data["object_name"], data["chat_id"]))
    c.execute("""
        INSERT INTO reports
        (date, object_name, chat_id, chat_title, people_today, people_planned,
         completed_work, problems, needs_management, needs_customer, equipment_problems, raw_text, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data.get("date"),
        data.get("object_name"),
        data.get("chat_id"),
        data.get("chat_title"),
        data.get("people_today"),
        data.get("people_planned"),
        data.get("completed_work"),
        data.get("problems"),
        data.get("needs_management"),
        data.get("needs_customer"),
        data.get("equipment_problems"),
        data.get("raw_text"),
        datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    ))
    conn.commit()
    conn.close()


def get_today_reports():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now(MOSCOW_TZ).strftime("%d.%m")
    c.execute("SELECT * FROM reports WHERE date=? ORDER BY object_name", (today,))
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def get_bosses():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM bosses")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_boss(chat_id, username, full_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bosses (chat_id, username, full_name) VALUES (?,?,?)",
              (chat_id, username, full_name))
    conn.commit()
    conn.close()


# ── Парсинг отчёта ─────────────────────────────────────────────────────────────

def parse_report(text, chat_id, chat_title):
    result = {"chat_id": chat_id, "chat_title": chat_title, "raw_text": text}

    # Название объекта из заголовка
    m = re.search(r"ЕЖЕДНЕВНЫЙ ОТЧЁТ\s*[—\-–]\s*(.+)", text, re.IGNORECASE)
    if m:
        result["object_name"] = m.group(1).strip()
    else:
        result["object_name"] = chat_title or "Неизвестный объект"

    # Дата
    m = re.search(r"дата:\s*(\d{1,2}\.\d{1,2})", text, re.IGNORECASE)
    if m:
        result["date"] = m.group(1)
    else:
        result["date"] = datetime.now(MOSCOW_TZ).strftime("%d.%m")

    # Люди работало сегодня
    m = re.search(r"Работало сегодня:\s*(\d+)\s*чел", text, re.IGNORECASE)
    if m:
        result["people_today"] = int(m.group(1))

    # Люди на завтра/следующий день
    m = re.search(r"Планируется на \w+:\s*(\d+)\s*чел", text, re.IGNORECASE)
    if m:
        result["people_planned"] = int(m.group(1))

    # Раздел 3 — выполненный объём по смете
    m = re.search(r"3\.\s*ВЫПОЛНЕННЫЙ ОБЪЕМ.*?(?=\n4\.|\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        result["completed_work"] = clean_section(m.group(0))

    # Раздел 5 — сложности
    m = re.search(r"5\.\s*СЛОЖНОСТИ.*?(?=\n6\.|\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        result["problems"] = clean_section(m.group(0))

    # Раздел 6 — нужно от руководителей
    m = re.search(r"6\.\s*ЧТО НУЖНО ОТ РУКОВОДИТЕЛЕЙ.*?(?=\n7\.|\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        result["needs_management"] = clean_section(m.group(0))

    # Раздел 7 — нужно от заказчика (только пункты с ❗)
    m = re.search(r"7\.\s*ЧТО НУЖНО ОТ ЗАКАЗЧИКА.*?(?=\n8\.|\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        urgent = re.findall(r"❗[^\n]+", m.group(0))
        if urgent:
            result["needs_customer"] = "\n".join(urgent)

    # Раздел 8 — проблемы с техникой (пункты с ‼️)
    m = re.search(r"8\.\s*ТЕХНИКА.*?\Z", text, re.DOTALL | re.IGNORECASE)
    if m:
        equip = re.findall(r"‼️[^\n]+(?:\n---[^\n]+)*", m.group(0))
        if equip:
            result["equipment_problems"] = "\n".join(equip)

    return result


def clean_section(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines[1:])  # убираем заголовок раздела


def is_report(text):
    return bool(re.search(r"ЕЖЕДНЕВНЫЙ ОТЧЁТ", text, re.IGNORECASE))


# ── Формирование сводки ────────────────────────────────────────────────────────

def build_summary(reports, date=None):
    if not date:
        date = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")

    if not reports:
        return f"📊 <b>СВОДКА ЗА {date}</b>\n\nОтчётов за сегодня не поступало."

    lines = [f"📊 <b>СВОДКА ЗА {date}</b>\n"]

    for r in reports:
        lines.append(f"🏗 <b>{r['object_name']}</b>")

        if r.get("people_today"):
            lines.append(f"👷 Работало: <b>{r['people_today']} чел.</b>")
        if r.get("people_planned"):
            lines.append(f"📅 Планируется: {r['people_planned']} чел.")

        if r.get("completed_work"):
            lines.append(f"\n✅ <b>Выполнено по смете:</b>\n{r['completed_work']}")

        if r.get("problems"):
            lines.append(f"\n⚠️ <b>Сложности:</b>\n{r['problems']}")

        if r.get("needs_management") and "ничего" not in r["needs_management"].lower():
            lines.append(f"\n📋 <b>Нужно от руководителей:</b>\n{r['needs_management']}")

        if r.get("needs_customer"):
            lines.append(f"\n❗ <b>Нужно от заказчика:</b>\n{r['needs_customer']}")

        if r.get("equipment_problems"):
            lines.append(f"\n‼️ <b>Проблемы с техникой:</b>\n{r['equipment_problems']}")

        lines.append("─" * 30)

    return "\n".join(lines)


# ── Отправка сводки всем боссам ────────────────────────────────────────────────

def send_daily_summary():
    bosses = get_bosses()
    if not bosses:
        log.warning("Нет получателей сводки (никто не нажал /start)")
        return

    reports = get_today_reports()
    date = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    text = build_summary(reports, date)

    for chat_id in bosses:
        try:
            bot.send_message(chat_id, text, parse_mode="HTML")
            log.info(f"Сводка отправлена в {chat_id}")
        except Exception as e:
            log.error(f"Ошибка отправки в {chat_id}: {e}")


# ── Обработчики команд ─────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
    add_boss(message.chat.id, message.from_user.username, name)
    bot.reply_to(message,
        "✅ Вы зарегистрированы как получатель сводки.\n\n"
        "Ежедневная сводка будет приходить в <b>12:00 по Москве</b>.\n\n"
        "Команды:\n"
        "/svod — получить сводку прямо сейчас\n"
        "/help — справка",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["svod"])
def cmd_svod(message):
    reports = get_today_reports()
    date = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    text = build_summary(reports, date)
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(message,
        "📖 <b>Как пользоваться ботом:</b>\n\n"
        "1. Добавь бота в чаты где руководители присылают отчёты\n"
        "2. Напиши <b>/start</b> в личку боту — чтобы получать сводку\n"
        "3. Руководители отправляют отчёты по шаблону (ЕЖЕДНЕВНЫЙ ОТЧЁТ — ...)\n"
        "4. В 12:00 по Москве сводка придёт автоматически\n"
        "5. /svod — получить сводку в любой момент",
        parse_mode="HTML"
    )


# ── Обработчик сообщений (парсинг отчётов) ────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and is_report(m.text))
def handle_report(message):
    chat_title = message.chat.title or message.chat.username or "Личный чат"
    data = parse_report(message.text, message.chat.id, chat_title)
    save_report(data)
    log.info(f"Получен отчёт: {data.get('object_name')} за {data.get('date')}")
    bot.reply_to(message, f"✅ Отчёт по объекту <b>{data.get('object_name')}</b> принят.", parse_mode="HTML")


# ── Запуск ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(send_daily_summary, "cron", hour=12, minute=0)
    scheduler.start()
    log.info("Бот запущен. Сводка отправляется в 12:00 по Москве.")

    bot.infinity_polling(timeout=30, long_polling_timeout=30)
