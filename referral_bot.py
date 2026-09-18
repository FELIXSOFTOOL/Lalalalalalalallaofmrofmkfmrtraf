# -*- coding: utf-8 -*-
"""
Referral bot — начисление за приглашение в группу, баланс, вывод через заявку + CryptoBot чек.
Написан на python-telegram-bot (PTB), не на aiogram.

Как работает:
  0. Обязательная подписка: пока юзер не подписан на все каналы/чаты из CHANNELS —
     бот не даёт пользоваться меню (баланс, ссылка, вывод), только кнопки "подписаться" + "Я подписался".
  1. Юзер жмёт /start в боте -> (если подписан) бот создаёт ему ПЕРСОНАЛЬНЫЕ инвайт-ссылки
     в каждый чат из REFERRAL_TARGETS (сейчас — Основной канал и Чат услуг)
     (bot.create_chat_invite_link, бот должен быть админом каждого с правом invite_users).
  2. Юзер кидает эти ссылки друзьям. Когда кто-то заходит в ЛЮБОЙ из REFERRAL_TARGETS по ЕГО ссылке —
     бот ловит chat_member апдейт, матчит invite_link -> находит реферера -> +0.20$ на баланс
     (один и тот же приглашённый засчитывается рефереру только один раз, в какой бы из чатов он ни зашёл).
  3. Вывод — заявкой:
       - юзер жмёт "Вывести" -> баланс сразу списывается (чтобы не наспамили заявками),
         создаётся pending-заявка, тебе (админу) летит карточка "юзер / сумма" с кнопкой "Оплачено"
       - ты жмёшь "Оплачено" -> ТОЛЬКО В ЭТОТ МОМЕНТ бот создаёт чек в CryptoBot (createCheck,
         пинится на telegram id юзера) -> юзеру прилетает "вывод успешно прошёл" + ссылка на чек

ВАЖНО про обязательную подписку:
  Чтобы бот мог ПРОВЕРЯТЬ подписку (а не просто показывать кнопку), боту нужен ЧИСЛОВОЙ chat_id
  каждого канала/чата — сама инвайт-ссылка (t.me/+...) для проверки не годится, она только для кнопки.
  Как получить chat_id:
    1) Добавь бота админом в канал/чат.
    2) Отправь команду /groupid прямо в этом канале/чате (постом, если это канал).
    3) Бот ответит числом вида -100xxxxxxxxxx — впиши его в CHANNELS ниже, поле "chat_id".
  Пока chat_id не заполнен (None) — проверка по этому каналу пропускается, чтобы не заблокировать всех.

Нужно поставить ТОЛЬКО: pip install python-telegram-bot
(httpx для запросов к CryptoBot и так ставится вместе с ним, sqlite3 — встроенный в питон)
"""

import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from telegram import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

# ======================= CONFIG — ПРАВИШЬ ПОД СЕБЯ =======================
# Не храните реальные токены в файле. Перед запуском задайте переменные
# окружения BOT_TOKEN и CRYPTOBOT_TOKEN (пример — в инструкции в конце файла).
BOT_TOKEN = "8772586746:AAGyF7XQyhUcOpZCekZo8eIk9snPb9eW3Qo"
CRYPTOBOT_TOKEN = "634234:AAONBeEfhnpnGIRmnstytP6kn60VRPZ1B4a"  # @CryptoBot -> Crypto Pay -> Create App
CRYPTOBOT_API = "https://pay.crypt.bot/api"
WITHDRAW_ASSET = "USDT"            # в чём выводим (баланс считаем 1 у.е. = 1 USDT)

REFERRAL_REWARD = 0.20             # $ за приглашённого
REFERRAL_LEAVE_PENALTY = 0.20      # $ штрафа рефереру, если приглашённый вышел хотя бы из одного чата
MIN_WITHDRAW = 1.0                 # минимальная сумма на вывод
MAX_BALANCE_ADJUSTMENT = 100_000.0 # защита от случайного лишнего нуля в админке
# Ночной стоп выводов. Переключается из /admin и хранится в SQLite.
WITHDRAW_STOP_TIMEZONE = "Europe/Moscow"
DEFAULT_WITHDRAW_STOP_START = "00:00"
DEFAULT_WITHDRAW_STOP_END = "07:00"

ADMIN_IDS = {5630634554, 7421864926}            # твой telegram id (и других админов) сюда
# Канал, куда бот публикует все заявки на вывод. Бот должен иметь право публиковать посты.
PAYOUT_CHANNEL_ID = -1004331872766

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "referral_bot.db")

# --- обязательная подписка на каналы/чаты перед тем как бот "заработает" ---
# url — ссылка для кнопки (можно инвайт-ссылку), chat_id — числовой id для ПРОВЕРКИ подписки
# (как получить chat_id — см. инструкцию в шапке файла, команда /groupid внутри канала/чата).
CHANNEL_MAIN = {"title": "Основной канал", "url": "https://t.me/+MJrX8O5KsMkyOGNi", "chat_id": -1004364291380}
CHANNEL_RESERVE = {"title": "Резервный канал", "url": "https://t.me/+LKug62c65MFhMTUy", "chat_id": -1004496481186}
CHANNEL_SERVICES = {"title": "Чат услуг", "url": "https://t.me/+gdWLdLTFIBhkMjAy", "chat_id": -1004455723735}

CHANNELS = [CHANNEL_MAIN, CHANNEL_RESERVE, CHANNEL_SERVICES]  # подписка обязательна на все три

# --- рефералка: персональные инвайт-ссылки создаются именно в эти чаты (не во все CHANNELS,
# а только в эти два) — засчитывается, только когда дроп заходит по ОБЕИМ ссылкам этого реферера ---
REFERRAL_TARGETS = [CHANNEL_MAIN, CHANNEL_SERVICES]

# видео на стартовом экране — приоритетнее аватарки, если задано (файл рядом со скриптом
# ИЛИ прямая ссылка). Если не задано — используется AVATAR_* (фото). Если и её нет — просто текст.
# Можно задать абсолютный путь на хостинге через START_VIDEO_PATH.
# По умолчанию бот ищет start_video.mp4 рядом с этим .py файлом.
START_VIDEO_PATH = os.getenv("START_VIDEO_PATH", "start_video.mp4")
START_VIDEO_URL = ""

# аватарка (фото) — запасной вариант, если видео не настроено
AVATAR_PATH = "avatar.jpg"
AVATAR_URL = ""
# ===========================================================================

# Telegram выдаёт file_id после первой загрузки медиа. Повторно используем его
# при /start, чтобы экран не превращался в текст, если локальный файл временно
# недоступен или повторная загрузка не прошла.
START_MEDIA_FILE_ID: str | None = None
START_MEDIA_KIND: str | None = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("referral_bot")


# ============================== DATABASE ==================================
# sqlite3 синхронный, но бот маленький — держим соединение простым и быстрым,
# без лишних async-обвязок типа aiosqlite (чтобы не тянуть лишнюю библиотеку).

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_database_backup() -> str:
    """Создаёт согласованную SQLite-копию, пригодную для скачивания."""
    with tempfile.NamedTemporaryFile(prefix="referral_backup_", suffix=".db", dir=BASE_DIR, delete=False) as tmp:
        backup_path = tmp.name
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def validate_database_backup(path: str) -> None:
    """Проверяет SQLite-файл до замены рабочей базы."""
    with open(path, "rb") as file:
        if file.read(16) != b"SQLite format 3\x00":
            raise ValueError("Это не SQLite-база")
    conn = sqlite3.connect(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("Проверка целостности SQLite не пройдена")
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        required = {"users", "invite_links", "withdrawals"}
        if not required.issubset(tables):
            raise ValueError("В базе нет обязательных таблиц referral-бота")
    finally:
        conn.close()


def replace_database_from_backup(upload_path: str) -> str:
    """Атомарно заменяет БД и оставляет предыдущую копию для восстановления."""
    validate_database_backup(upload_path)
    previous_path = f"{DB_PATH}.before_import.db"
    moved_previous = False
    try:
        if os.path.exists(DB_PATH):
            os.replace(DB_PATH, previous_path)
            moved_previous = True
        os.replace(upload_path, DB_PATH)
        init_db()  # добавляет отсутствующие таблицы/колонки для старого бэкапа
    except Exception:
        if moved_previous and os.path.exists(previous_path):
            os.replace(previous_path, DB_PATH)
        raise
    return previous_path


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL NOT NULL DEFAULT 0,
            referral_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invite_links (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            link TEXT NOT NULL UNIQUE,
            join_request INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    try:
        conn.execute("ALTER TABLE invite_links ADD COLUMN join_request INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже есть
    conn.execute("""
        CREATE TABLE IF NOT EXISTS target_joins (
            referred_user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            referrer_id INTEGER NOT NULL,
            created_at TEXT,
            PRIMARY KEY (referred_user_id, chat_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_user_id INTEGER NOT NULL UNIQUE,
            penalized INTEGER NOT NULL DEFAULT 0,
            created_at TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE referrals ADD COLUMN penalized INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже есть (обычный запуск на существующей базе)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            check_url TEXT,
            created_at TEXT,
            processed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS balance_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS join_verifications (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            referrer_id INTEGER NOT NULL,
            invite_link TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('night_withdraw_stop_enabled', '0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('withdraw_stop_start', ?)" ,
        (DEFAULT_WITHDRAW_STOP_START,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('withdraw_stop_end', ?)" ,
        (DEFAULT_WITHDRAW_STOP_END,),
    )
    conn.commit()
    conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_setting(key: str, default: str = "") -> str:
    conn = db()
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = db()
    conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def night_withdraw_stop_enabled() -> bool:
    return get_setting("night_withdraw_stop_enabled", "0") == "1"


def get_withdraw_stop_schedule() -> tuple[int, int, str, str]:
    """Возвращает начало/конец как минуты от полуночи и строки HH:MM."""
    start_text = get_setting("withdraw_stop_start", DEFAULT_WITHDRAW_STOP_START)
    end_text = get_setting("withdraw_stop_end", DEFAULT_WITHDRAW_STOP_END)

    def to_minutes(value: str, fallback: str) -> tuple[int, str]:
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
        if not match:
            value = fallback
            match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
        return int(match.group(1)) * 60 + int(match.group(2)), value

    start_minutes, start_text = to_minutes(start_text, DEFAULT_WITHDRAW_STOP_START)
    end_minutes, end_text = to_minutes(end_text, DEFAULT_WITHDRAW_STOP_END)
    return start_minutes, end_minutes, start_text, end_text


def withdrawal_stop_status() -> tuple[bool, str]:
    """Возвращает, разрешён ли вывод, и понятное объяснение текущего режима."""
    if not night_withdraw_stop_enabled():
        return True, "Ночное расписание остановки выключено."
    try:
        local_now = datetime.now(ZoneInfo(WITHDRAW_STOP_TIMEZONE))
    except ZoneInfoNotFoundError:
        # Редкий случай для урезанных сборок Python на Android: используем время устройства.
        log.warning("Timezone %s not found; using device local time", WITHDRAW_STOP_TIMEZONE)
        local_now = datetime.now().astimezone()
    start_minutes, end_minutes, start_text, end_text = get_withdraw_stop_schedule()
    current_minutes = local_now.hour * 60 + local_now.minute
    # Интервал может пересекать полночь: например 22:30–07:00.
    is_stopped = (
        start_minutes <= current_minutes < end_minutes
        if start_minutes < end_minutes
        else current_minutes >= start_minutes or current_minutes < end_minutes
    )
    if is_stopped:
        return False, (
            f"Вывод временно остановлен до {end_text} "
            f"({WITHDRAW_STOP_TIMEZONE})."
        )
    return True, (
        f"Стоп выводов включён: заявки блокируются с {start_text} "
        f"до {end_text} ({WITHDRAW_STOP_TIMEZONE})."
    )


def get_or_create_user(user_id: int, username: str | None) -> None:
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
        (user_id, username or "", now()),
    )
    conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (username or "", user_id))
    conn.commit()
    conn.close()


def get_user(user_id: int) -> sqlite3.Row | None:
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_invite_link(user_id: int, chat_id: int) -> str | None:
    conn = db()
    row = conn.execute(
        "SELECT link FROM invite_links WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)
    ).fetchone()
    conn.close()
    return row["link"] if row else None


def is_join_request_invite_link(user_id: int, chat_id: int) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT join_request FROM invite_links WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)
    ).fetchone()
    conn.close()
    return bool(row and row["join_request"])


def save_invite_link(user_id: int, chat_id: int, link: str, join_request: bool = False) -> None:
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO invite_links (user_id, chat_id, link, join_request) VALUES (?, ?, ?, ?)",
        (user_id, chat_id, link, int(join_request)),
    )
    conn.commit()
    conn.close()


def get_referrer_id_by_invite_link(link: str) -> int | None:
    conn = db()
    row = conn.execute("SELECT user_id FROM invite_links WHERE link = ?", (link,)).fetchone()
    conn.close()
    return row["user_id"] if row else None


def record_target_join(referred_user_id: int, chat_id: int, referrer_id: int) -> bool:
    """Отмечает, что этот юзер зашёл в этот чат по ссылке этого реферера.
    Если уже был отмечен (перезаходил) — запись не трогаем, первый переход в приоритете."""
    conn = db()
    cur = conn.execute(
        "INSERT OR IGNORE INTO target_joins (referred_user_id, chat_id, referrer_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        (referred_user_id, chat_id, referrer_id, now()),
    )
    conn.commit()
    conn.close()
    return cur.rowcount == 1


def get_referral_progress(referred_user_id: int) -> list[sqlite3.Row]:
    """Состояние двух переходов: полезно для диагностики рефералки админом."""
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, referrer_id, created_at FROM target_joins WHERE referred_user_id = ? ORDER BY created_at",
        (referred_user_id,),
    ).fetchall()
    conn.close()
    return rows


def save_join_verification(chat_id: int, user_id: int, referrer_id: int, invite_link: str) -> None:
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO join_verifications "
        "(chat_id, user_id, referrer_id, invite_link, status, requested_at, verified_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, NULL)",
        (chat_id, user_id, referrer_id, invite_link, now()),
    )
    conn.commit()
    conn.close()


def get_join_verification(chat_id: int, user_id: int) -> sqlite3.Row | None:
    conn = db()
    row = conn.execute(
        "SELECT * FROM join_verifications WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    ).fetchone()
    conn.close()
    return row


def mark_join_verified(chat_id: int, user_id: int) -> bool:
    conn = db()
    cur = conn.execute(
        "UPDATE join_verifications SET status = 'verified', verified_at = ? "
        "WHERE chat_id = ? AND user_id = ? AND status = 'pending'",
        (now(), chat_id, user_id),
    )
    conn.commit()
    changed = cur.rowcount == 1
    conn.close()
    return changed


def count_completed_targets(referred_user_id: int, referrer_id: int) -> int:
    """Сколько из REFERRAL_TARGETS этот юзер прошёл именно по ссылкам ЭТОГО реферера."""
    conn = db()
    row = conn.execute(
        "SELECT COUNT(DISTINCT chat_id) c FROM target_joins WHERE referred_user_id = ? AND referrer_id = ?",
        (referred_user_id, referrer_id),
    ).fetchone()
    conn.close()
    return row["c"]


def credit_referral(referrer_id: int, referred_user_id: int) -> bool:
    """Начисляет рефереру награду. Возвращает False, если этот юзер уже был засчитан раньше."""
    conn = db()
    try:
        conn.execute(
            "INSERT INTO referrals (referrer_id, referred_user_id, created_at) VALUES (?, ?, ?)",
            (referrer_id, referred_user_id, now()),
        )
    except sqlite3.IntegrityError:
        conn.close()
        return False  # уже был засчитан (например, вышел и зашёл заново)
    conn.execute(
        "UPDATE users SET balance = ROUND(balance + ?, 2), referral_count = referral_count + 1 WHERE user_id = ?",
        (REFERRAL_REWARD, referrer_id),
    )
    conn.commit()
    conn.close()
    return True


def apply_leave_penalty(referred_user_id: int) -> int | None:
    """Если этот юзер был засчитанным рефералом и штраф ещё не применялся — снимает
    REFERRAL_LEAVE_PENALTY с баланса реферера (один раз, даже если выйдет из обоих чатов).
    Возвращает id реферера, если штраф применили, иначе None."""
    conn = db()
    row = conn.execute(
        "SELECT referrer_id, penalized FROM referrals WHERE referred_user_id = ?",
        (referred_user_id,),
    ).fetchone()
    if not row or row["penalized"]:
        conn.close()
        return None
    referrer_id = row["referrer_id"]
    conn.execute("UPDATE referrals SET penalized = 1 WHERE referred_user_id = ?", (referred_user_id,))
    conn.execute(
        "UPDATE users SET balance = ROUND(balance - ?, 2), referral_count = MAX(referral_count - 1, 0) WHERE user_id = ?",
        (REFERRAL_LEAVE_PENALTY, referrer_id),
    )
    conn.commit()
    conn.close()
    return referrer_id


def create_withdrawal_request(user_id: int, username: str, amount: float) -> int | None:
    """Атомарно резервирует деньги и создаёт заявку.

    Возвращает None, если другой callback уже успел списать баланс.
    """
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "UPDATE users SET balance = ROUND(balance - ?, 2) WHERE user_id = ? AND balance >= ?",
        (amount, user_id, amount),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return None
    cur = conn.execute(
        "INSERT INTO withdrawals (user_id, username, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (user_id, username or "", amount, now()),
    )
    conn.commit()
    withdrawal_id = cur.lastrowid
    conn.close()
    return withdrawal_id


def get_withdrawal(withdrawal_id: int) -> sqlite3.Row | None:
    conn = db()
    row = conn.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)).fetchone()
    conn.close()
    return row


def get_pending_withdrawals(limit: int = 20) -> list[sqlite3.Row]:
    conn = db()
    rows = conn.execute(
        "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def reserve_withdrawal(withdrawal_id: int) -> bool:
    """Не даёт нескольким админам создать два чека по одной заявке."""
    conn = db()
    cur = conn.execute(
        "UPDATE withdrawals SET status = 'processing' WHERE id = ? AND status = 'pending'",
        (withdrawal_id,),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def complete_withdrawal(withdrawal_id: int, check_url: str) -> bool:
    conn = db()
    cur = conn.execute(
        "UPDATE withdrawals SET status = 'completed', check_url = ?, processed_at = ? "
        "WHERE id = ? AND status = 'processing'",
        (check_url, now(), withdrawal_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def release_withdrawal(withdrawal_id: int) -> None:
    """Возвращает заявку в pending, если Crypto Pay не создал чек."""
    conn = db()
    conn.execute("UPDATE withdrawals SET status = 'pending' WHERE id = ? AND status = 'processing'", (withdrawal_id,))
    conn.commit()
    conn.close()


def reject_withdrawal(withdrawal_id: int) -> sqlite3.Row | None:
    """Отклоняет ещё не обрабатываемую заявку и атомарно возвращает резерв."""
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM withdrawals WHERE id = ? AND status = 'pending'", (withdrawal_id,)).fetchone()
    if not row:
        conn.rollback()
        conn.close()
        return None
    conn.execute("UPDATE withdrawals SET status = 'rejected', processed_at = ? WHERE id = ?", (now(), withdrawal_id))
    conn.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?", (row["amount"], row["user_id"]))
    conn.commit()
    conn.close()
    return row


def change_balance(user_id: int, admin_id: int, amount: float, reason: str) -> float | None:
    """Меняет баланс с аудитом; отрицательный остаток не допускается."""
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, '', ?)", (user_id, now()))
    cur = conn.execute(
        "UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ? AND balance + ? >= 0",
        (amount, user_id, amount),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return None
    conn.execute(
        "INSERT INTO balance_operations (user_id, admin_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, admin_id, amount, reason, now()),
    )
    balance = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()["balance"]
    conn.commit()
    conn.close()
    return balance


def admin_stats() -> dict:
    conn = db()
    users_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    refs_count = conn.execute("SELECT COUNT(*) c FROM referrals").fetchone()["c"]
    pending_count = conn.execute(
        "SELECT COUNT(*) c FROM withdrawals WHERE status = 'pending'"
    ).fetchone()["c"]
    total_paid = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status = 'completed'"
    ).fetchone()["s"]
    debt_users = conn.execute("SELECT COUNT(*) c FROM users WHERE balance < 0").fetchone()["c"]
    total_debt = conn.execute("SELECT COALESCE(SUM(-balance),0) s FROM users WHERE balance < 0").fetchone()["s"]
    pending_verifications = conn.execute(
        "SELECT COUNT(*) c FROM join_verifications WHERE status = 'pending'"
    ).fetchone()["c"]
    conn.close()
    return {
        "users": users_count,
        "refs": refs_count,
        "pending": pending_count,
        "paid": total_paid,
        "debt_users": debt_users,
        "debt": total_debt,
        "pending_verifications": pending_verifications,
    }


def get_user_admin_info(user_id: int) -> dict | None:
    """Сводка пользователя для админ-панели, без выдачи приватных данных другим людям."""
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return None
    withdrawals = {
        row["status"]: {"count": row["count"], "amount": row["amount"]}
        for row in conn.execute(
            "SELECT status, COUNT(*) count, COALESCE(SUM(amount), 0) amount "
            "FROM withdrawals WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
    }
    referrals = conn.execute(
        "SELECT COUNT(*) total, COALESCE(SUM(penalized), 0) penalized FROM referrals WHERE referrer_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return {"user": user, "withdrawals": withdrawals, "referrals": referrals}


# ============================== CRYPTOBOT ==================================

async def cryptobot_create_check(amount: float, pin_user_id: int) -> dict | None:
    return await cryptobot_api_call(
        "createCheck",
        {
            "asset": WITHDRAW_ASSET,
            "amount": f"{amount:.2f}",
            "pin_to_user_id": pin_user_id,
        },
    )


async def cryptobot_api_call(method: str, payload: dict | None = None) -> dict | list | None:
    """Единая безопасная обёртка для Crypto Pay API."""
    if not CRYPTOBOT_TOKEN:
        log.error("CRYPTOBOT_TOKEN is not configured; cannot call %s", method)
        return None
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(f"{CRYPTOBOT_API}/{method}", json=payload or {}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.error("CryptoBot %s request failed: %s", method, e)
        return None
    if not data.get("ok") or "result" not in data:
        log.error("CryptoBot %s error: %s", method, data)
        return None
    return data["result"]


async def cryptobot_create_invoice(amount: float, description: str) -> dict | None:
    result = await cryptobot_api_call(
        "createInvoice",
        {
            "asset": WITHDRAW_ASSET,
            "amount": f"{amount:.2f}",
            "description": description[:1024],
            "allow_anonymous": False,
            "allow_comments": True,
        },
    )
    return result if isinstance(result, dict) else None


async def cryptobot_get_balances() -> list[dict] | None:
    result = await cryptobot_api_call("getBalance")
    return result if isinstance(result, list) else None


async def cryptobot_get_checks() -> list[dict] | None:
    result = await cryptobot_api_call("getChecks", {"asset": WITHDRAW_ASSET, "count": 1000})
    return result if isinstance(result, list) else None


# ======================== ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ============================

async def get_missing_channels(bot, user_id: int) -> list[dict]:
    """Возвращает список каналов, на которые юзер ещё НЕ подписан."""
    missing = []
    for ch in CHANNELS:
        if ch["chat_id"] is None:
            continue  # chat_id не настроен — пропускаем, чтобы не заблокировать всех
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if not is_active_member(member):
                missing.append(ch)
        except Exception as e:
            log.warning("Не смог проверить подписку юзера %s на '%s' (chat_id=%s): %s",
                        user_id, ch["title"], ch["chat_id"], e)
            missing.append(ch)  # не смогли проверить (не подписан / бот не админ / неверный chat_id)
    return missing


def is_active_member(member) -> bool:
    """RESTRICTED считается подпиской только когда Telegram помечает is_member=True."""
    if member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))


def parse_amount(raw: str) -> float:
    """Деньги принимаем только с двумя знаками после запятой и без NaN/Infinity."""
    try:
        value = Decimal(raw.replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise ValueError("invalid amount")
    if not value.is_finite() or value.as_tuple().exponent < -2:
        raise ValueError("invalid amount")
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


GATE_TEXT = (
    "🔒 Чтобы пользоваться ботом, подпишись на всё ниже, потом нажми «✅ Я подписался»."
)


def start_media():
    """Что показать на стартовом экране: видео (если настроено), иначе фото-аватарка, иначе ничего.
    Возвращает (kind, source), kind — 'video' / 'photo' / None."""
    video_path = resolve_media_path(START_VIDEO_PATH)
    avatar_path = resolve_media_path(AVATAR_PATH)
    if video_path and os.path.exists(video_path):
        return "video", open(video_path, "rb")
    if START_VIDEO_URL:
        return "video", START_VIDEO_URL
    if avatar_path and os.path.exists(avatar_path):
        return "photo", open(avatar_path, "rb")
    if AVATAR_URL:
        return "photo", AVATAR_URL
    return None, None


def resolve_media_path(path: str) -> str:
    """Относительный путь ищем рядом со скриптом, абсолютный используем как есть."""
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def start_media_status() -> str:
    """Диагностика для админа: почему стартовое медиа не отправляется."""
    video_path = resolve_media_path(START_VIDEO_PATH)
    avatar_path = resolve_media_path(AVATAR_PATH)
    if video_path and os.path.isfile(video_path):
        size = os.path.getsize(video_path) / 1024 / 1024
        return f"✅ Видео найдено: <code>{escape(video_path)}</code> ({size:.1f} MB)"
    if START_VIDEO_URL:
        return "✅ Используется START_VIDEO_URL."
    if avatar_path and os.path.isfile(avatar_path):
        return f"⚠️ Видео не найдено, используется фото: <code>{escape(avatar_path)}</code>"
    return (
        "❌ Стартовое видео не найдено. Загрузите <code>start_video.mp4</code> "
        f"в папку со скриптом: <code>{escape(BASE_DIR)}</code>"
    )


async def send_start_screen(update: Update, text: str, kb: InlineKeyboardMarkup):
    """Стартовый экран (гейт подписки или главное меню) — с видео/аватаркой, если настроены."""
    global START_MEDIA_FILE_ID, START_MEDIA_KIND
    kind, source = start_media()
    if kind is None and START_MEDIA_FILE_ID and START_MEDIA_KIND:
        kind, source = START_MEDIA_KIND, START_MEDIA_FILE_ID
    try:
        # file_id действует только для того же типа медиа (video/photo).
        media = START_MEDIA_FILE_ID if START_MEDIA_KIND == kind and START_MEDIA_FILE_ID else source
        if kind == "video":
            try:
                message = await update.message.reply_video(video=media, caption=text, reply_markup=kb)
            except TelegramError as e:
                log.exception("Не удалось отправить стартовое видео: %s", e)
                await update.message.reply_text(text, reply_markup=kb)
                return
            if message.video:
                START_MEDIA_FILE_ID = message.video.file_id
                START_MEDIA_KIND = "video"
        elif kind == "photo":
            message = await update.message.reply_photo(photo=media, caption=text, reply_markup=kb)
            if message.photo:
                START_MEDIA_FILE_ID = message.photo[-1].file_id
                START_MEDIA_KIND = "photo"
        else:
            await update.message.reply_text(text, reply_markup=kb)
    finally:
        if hasattr(source, "close"):
            source.close()


async def safe_edit(query, text: str, kb: InlineKeyboardMarkup):
    """Редактирует сообщение меню независимо от того, с аватаркой оно (фото) или без (текст).
    Если контент не поменялся (юзер тыкнул кнопку, а статус тот же) — Telegram кидает
    'Message is not modified', это не реальная ошибка, просто нечего обновлять."""
    try:
        await query.edit_message_caption(caption=text, reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except BadRequest as e2:
            if "Message is not modified" in str(e2):
                return
            raise


async def require_subscription(query, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверка перед любым действием в меню. Если не подписан — показывает гейт и возвращает False."""
    missing = await get_missing_channels(context.bot, query.from_user.id)
    if missing:
        await query.answer("Сначала подпишись на все каналы!", show_alert=True)
        await safe_edit(query, GATE_TEXT, subscribe_kb(missing))
        return False
    return True


# ============================== KEYBOARDS ==================================

def subscribe_kb(missing_channels: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📢 {ch['title']}", url=ch["url"])] for ch in missing_channels]
    rows.append([InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


def human_verification_kb(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я человек — подтвердить", callback_data=f"verify_join:{chat_id}:{user_id}")],
    ])


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton("🔗 Моя реф. ссылка", callback_data="get_link")],
        [InlineKeyboardButton("💸 Вывести", callback_data="withdraw")],
    ])


def admin_withdraw_kb(withdrawal_id: int, include_back: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💸 Создать Crypto Pay чек", callback_data=f"pay_confirm:{withdrawal_id}")],
        [InlineKeyboardButton("↩️ Отклонить и вернуть", callback_data=f"pay_reject:{withdrawal_id}")],
    ]
    if include_back:
        rows.append([InlineKeyboardButton("← К заявкам", callback_data="admin_pending")])
    return InlineKeyboardMarkup(rows)


def payout_request_text(withdrawal_id: int, user_id: int, username: str, amount: float) -> str:
    """Текст карточки, которая публикуется в закрытом канале выплат."""
    who = f"@{username}" if username else "без username"
    return (
        f"🧾 <b>Заявка на вывод #{withdrawal_id}</b>\n"
        f"Пользователь: {who} (id: <code>{user_id}</code>)\n"
        f"Сумма: <b>{amount:.2f} {WITHDRAW_ASSET}</b>\n\n"
        f"🆕 Создана — <code>{now()}</code>\n"
        "⏳ Ожидает создания Crypto Pay чека"
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Изменить баланс", callback_data="admin_balance")],
        [InlineKeyboardButton("👤 Информация о пользователе", callback_data="admin_user_info")],
        [InlineKeyboardButton("🧾 Заявки на вывод", callback_data="admin_pending")],
        [InlineKeyboardButton("⏰ Ночной стоп выводов", callback_data="admin_withdraw_stop")],
        [InlineKeyboardButton("💳 Crypto Pay", callback_data="admin_crypto")],
        [InlineKeyboardButton("💾 База данных", callback_data="admin_database")],
    ])


def admin_pending_kb(withdrawals: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"№{w['id']} — ${w['amount']:.2f}", callback_data=f"admin_open:{w['id']}")]
        for w in withdrawals
    ]
    rows.append([InlineKeyboardButton("↩️ В админку", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(rows)


def admin_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin_cancel")]])


def admin_crypto_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать счёт", callback_data="crypto_create_invoice")],
        [InlineKeyboardButton("💰 Баланс Crypto Pay", callback_data="crypto_balance")],
        [InlineKeyboardButton("📊 Статистика выплат", callback_data="crypto_payout_stats")],
        [InlineKeyboardButton("← В админку", callback_data="admin_cancel")],
    ])


def admin_withdraw_stop_kb() -> InlineKeyboardMarkup:
    enabled = night_withdraw_stop_enabled()
    label = "🔴 Выключить ночной стоп" if enabled else "🟢 Включить ночной стоп"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="withdraw_stop_toggle")],
        [InlineKeyboardButton("🕒 Настроить время", callback_data="withdraw_stop_set_time")],
        [InlineKeyboardButton("← В админку", callback_data="admin_cancel")],
    ])


def admin_database_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Скачать базу", callback_data="database_export")],
        [InlineKeyboardButton("⬆️ Загрузить базу", callback_data="database_import")],
        [InlineKeyboardButton("← В админку", callback_data="admin_cancel")],
    ])


# ============================== HANDLERS ==================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)

    missing = await get_missing_channels(context.bot, user.id)
    if missing:
        await send_start_screen(update, GATE_TEXT, subscribe_kb(missing))
        return

    await send_start_screen(
        update,
        f"Привет, {user.full_name}!\n\n"
        f"За каждого друга, который подпишется по ОБЕИМ твоим ссылкам — "
        f"<b>${REFERRAL_REWARD:.2f}</b> на баланс.\n"
        f"Минимум на вывод: <b>${MIN_WITHDRAW:.2f}</b>.",
        main_menu_kb(),
    )


async def cmd_groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кинь эту команду внутри целевой группы, чтобы узнать её chat_id для CONFIG."""
    await update.message.reply_text(f"chat_id этого чата: <code>{update.effective_chat.id}</code>")


async def cmd_mediastatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка пути к стартовому видео; доступна только админу в личке."""
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != "private":
        return
    await update.message.reply_text(start_media_status())


async def cmd_refstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает, какие переходы приглашённого Telegram реально прислал боту.

    Использование в личке: /refstatus <telegram_user_id>
    """
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != "private":
        return
    if len(context.args) != 1:
        await update.message.reply_text("Использование: <code>/refstatus 123456789</code>")
        return
    try:
        referred_id = int(context.args[0])
        if referred_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Нужен корректный Telegram ID пользователя.")
        return
    progress = get_referral_progress(referred_id)
    targets = {target["chat_id"]: target["title"] for target in REFERRAL_TARGETS}
    lines = [f"🔎 <b>Реферальная проверка</b>\nПользователь: <code>{referred_id}</code>"]
    for chat_id, title in targets.items():
        row = next((item for item in progress if item["chat_id"] == chat_id), None)
        if row:
            lines.append(
                f"✅ {title}: реферер <code>{row['referrer_id']}</code>\n"
                f"   зафиксировано: <code>{row['created_at']}</code>"
            )
        else:
            lines.append(f"❌ {title}: вход по личной ссылке бот не зафиксировал")
    if len(progress) >= len(targets):
        referrers = {row["referrer_id"] for row in progress if row["chat_id"] in targets}
        if len(referrers) > 1:
            lines.append("⚠️ Ссылки относятся к разным реферерам, поэтому награда не начислится.")
        else:
            lines.append("ℹ️ Если оба шага есть, но награды нет — перезапусти бота и пришли этот результат.")
    await update.message.reply_text("\n\n".join(lines))


async def cb_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await require_subscription(query, context):
        return
    user_id = query.from_user.id
    user = get_user(user_id)
    if not user:
        get_or_create_user(user_id, query.from_user.username)
        user = get_user(user_id)
    await safe_edit(
        query,
        f"💰 Баланс: <b>${user['balance']:.2f}</b>\n"
        f"👥 Приглашено: <b>{user['referral_count']}</b>",
        main_menu_kb(),
    )
    await query.answer()


async def cb_get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await require_subscription(query, context):
        return
    user_id = query.from_user.id
    get_or_create_user(user_id, query.from_user.username)

    lines = ["🔗 Твои персональные ссылки:\n"]
    for target in REFERRAL_TARGETS:
        link = get_invite_link(user_id, target["chat_id"])
        # Старые ссылки без заявки заменяются на ссылки с проверкой человека.
        if not link or not is_join_request_invite_link(user_id, target["chat_id"]):
            invite = await context.bot.create_chat_invite_link(
                chat_id=target["chat_id"],
                name=f"verify_{user_id}_{abs(target['chat_id']) % 100000}",
                creates_join_request=True,
            )
            link = invite.invite_link
            save_invite_link(user_id, target["chat_id"], link, join_request=True)
        lines.append(f"📢 {target['title']}:\n<code>{link}</code>")

    lines.append(
        f"\n⚠️ Друг отправит заявку по каждой ссылке, подтвердит проверку «Я человек» в боте — "
        f"и после входа по <b>обеим</b> ссылкам реферал засчитается.\n"
        f"Тогда — ${REFERRAL_REWARD:.2f} на баланс.\n\n"
        f"❗ Если он потом выйдет хотя бы из одного чата — с тебя спишут ${REFERRAL_LEAVE_PENALTY:.2f}."
    )

    await safe_edit(query, "\n\n".join(lines), main_menu_kb())
    await query.answer()


async def cb_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await require_subscription(query, context):
        return
    allowed, stop_message = withdrawal_stop_status()
    if not allowed:
        await query.answer(f"⏰ {stop_message}", show_alert=True)
        return
    user_id = query.from_user.id
    user = get_user(user_id)
    if not user or user["balance"] < MIN_WITHDRAW:
        cur = user["balance"] if user else 0.0
        await query.answer(f"Минимум на вывод ${MIN_WITHDRAW:.2f}, у тебя ${cur:.2f}", show_alert=True)
        return

    amount = user["balance"]
    username = query.from_user.username or ""
    withdrawal_id = create_withdrawal_request(user_id, username, amount)
    if withdrawal_id is None:
        await query.answer("Баланс уже изменился. Открой баланс и попробуй снова.", show_alert=True)
        return

    await safe_edit(
        query,
        f"🧾 Заявка на вывод <b>${amount:.2f}</b> отправлена.\n"
        f"Как только оплатим — пришлём чек сюда.",
        main_menu_kb(),
    )
    await query.answer()

    try:
        await context.bot.send_message(
            PAYOUT_CHANNEL_ID,
            payout_request_text(withdrawal_id, user_id, username, amount),
            reply_markup=admin_withdraw_kb(withdrawal_id),
        )
    except Exception as e:
        # Заявка не потеряна: её всё равно можно открыть из /admin.
        log.error("Не смог опубликовать заявку #%s в канале выплат %s: %s", withdrawal_id, PAYOUT_CHANNEL_ID, e)


async def cb_check_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    missing = await get_missing_channels(context.bot, user.id)
    if missing:
        await query.answer("Похоже, подписался не на всё. Проверь ещё раз.", show_alert=True)
        await safe_edit(query, GATE_TEXT, subscribe_kb(missing))
        return

    get_or_create_user(user.id, user.username)
    await query.answer("Готово, бот разблокирован ✅")
    await safe_edit(
        query,
        f"Привет, {user.full_name}!\n\n"
        f"За каждого друга, который подпишется по ОБЕИМ твоим ссылкам — "
        f"<b>${REFERRAL_REWARD:.2f}</b> на баланс.\n"
        f"Минимум на вывод: <b>${MIN_WITHDRAW:.2f}</b>.",
        main_menu_kb(),
    )


async def cb_pay_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Не для тебя кнопка 🙂", show_alert=True)
        return

    withdrawal_id = int(context.match.group(1))
    withdrawal = get_withdrawal(withdrawal_id)
    if not withdrawal:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    if withdrawal["status"] != "pending":
        await query.answer("Заявка уже обрабатывается или закрыта", show_alert=True)
        return

    if not reserve_withdrawal(withdrawal_id):
        await query.answer("Заявка уже обрабатывается другим админом", show_alert=True)
        return

    # Чек создаётся только после атомарной резервации заявки.
    result = await cryptobot_create_check(withdrawal["amount"], pin_user_id=withdrawal["user_id"])
    if not result:
        release_withdrawal(withdrawal_id)
        await query.answer("Ошибка CryptoBot, попробуй ещё раз", show_alert=True)
        return

    check_url = result.get("bot_check_url") or result.get("check_url")
    if not check_url:
        release_withdrawal(withdrawal_id)
        log.error("CryptoBot createCheck returned no check URL: %s", result)
        await query.answer("CryptoBot не вернул ссылку на чек", show_alert=True)
        return

    ok = complete_withdrawal(withdrawal_id, check_url)
    if not ok:
        await query.answer("Статус заявки изменился — проверьте её вручную", show_alert=True)
        return

    await query.edit_message_text(
        query.message.text + f"\n\n✅ Оплачено — <code>{now()}</code>\n"
        "💳 Crypto Pay чек создан и отправлен пользователю в личные сообщения.",
        reply_markup=None,
    )
    await query.answer("Чек создан и отправлен юзеру")

    try:
        await context.bot.send_message(
            withdrawal["user_id"],
            f"✅ Вывод <b>${withdrawal['amount']:.2f}</b> успешно прошёл!\n\n"
            f"Забери чек: {check_url}",
        )
    except Exception as e:
        log.warning("Не смог уведомить юзера %s: %s", withdrawal["user_id"], e)


async def cb_pay_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Не для тебя кнопка 🙂", show_alert=True)
        return
    withdrawal_id = int(context.match.group(1))
    withdrawal = reject_withdrawal(withdrawal_id)
    if not withdrawal:
        await query.answer("Можно отменить только новую заявку", show_alert=True)
        return
    await query.edit_message_text(
        query.message.text + "\n\n↩️ Отклонена, средства возвращены на баланс.",
        reply_markup=None,
    )
    await query.answer("Средства возвращены")
    try:
        await context.bot.send_message(
            withdrawal["user_id"],
            f"↩️ Заявка на вывод ${withdrawal['amount']:.2f} отклонена. Деньги возвращены на баланс.",
        )
    except Exception as e:
        log.warning("Не смог уведомить юзера %s: %s", withdrawal["user_id"], e)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос на вход по личной ссылке: отправляем проверку в ЛС, не одобряем сразу."""
    request = update.chat_join_request
    if request.chat.id not in REFERRAL_TARGET_CHAT_IDS or not request.invite_link:
        return
    referrer_id = get_referrer_id_by_invite_link(request.invite_link.invite_link)
    if not referrer_id or referrer_id == request.from_user.id:
        return
    save_join_verification(
        request.chat.id, request.from_user.id, referrer_id, request.invite_link.invite_link
    )
    try:
        await context.bot.send_message(
            request.from_user.id,
            "🛡 <b>Проверка перед вступлением</b>\n\n"
            "Нажми кнопку ниже, чтобы подтвердить, что ты человек, а не бот. "
            "После этого бот одобрит твою заявку на вход.",
            reply_markup=human_verification_kb(request.chat.id, request.from_user.id),
        )
    except TelegramError as e:
        # Для join request Telegram обычно даёт боту короткое окно написать в ЛС.
        # Если пользователь отменил заявку или окно истекло, заявка остаётся pending.
        log.warning("Не смог отправить проверку user=%s chat=%s: %s", request.from_user.id, request.chat.id, e)


async def cb_verify_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(context.match.group(1))
    user_id = int(context.match.group(2))
    if query.from_user.id != user_id:
        await query.answer("Эта проверка предназначена другому пользователю.", show_alert=True)
        return
    verification = get_join_verification(chat_id, user_id)
    if not verification or verification["status"] != "pending":
        await query.answer("Заявка уже обработана или устарела.", show_alert=True)
        return
    try:
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
    except TelegramError as e:
        log.warning("Не смог одобрить заявку user=%s chat=%s: %s", user_id, chat_id, e)
        await query.answer("Не удалось одобрить заявку. Возможно, она отменена — отправь её снова.", show_alert=True)
        return
    mark_join_verified(chat_id, user_id)
    # Фиксируем шаг сразу после успешного одобрения. Это надёжнее, чем ждать
    # отдельный chat_member update, который на некоторых хостах может прийти с задержкой.
    record_target_join(user_id, chat_id, verification["referrer_id"])
    await maybe_credit_referral(context, verification["referrer_id"], user_id)
    await query.edit_message_text(
        "✅ Проверка пройдена. Твоя заявка на вступление одобрена.\n\n"
        "Если это реферальная ссылка, пройди такую же проверку по второй ссылке.",
    )
    await query.answer("Готово — заявка одобрена")


REFERRAL_TARGET_CHAT_IDS = {t["chat_id"] for t in REFERRAL_TARGETS}


async def maybe_credit_referral(context: ContextTypes.DEFAULT_TYPE, referrer_id: int, referred_id: int) -> bool:
    """Начисляет награду, когда есть оба шага одного реферера; повтор безопасен."""
    done = count_completed_targets(referred_id, referrer_id)
    total = len(REFERRAL_TARGETS)
    if done < total:
        log.info(
            "Referral incomplete: invited=%s referrer=%s completed=%s/%s",
            referred_id, referrer_id, done, total,
        )
        return False
    credited = credit_referral(referrer_id, referred_id)
    if credited:
        try:
            await context.bot.send_message(
                referrer_id,
                f"🎉 По твоим ссылкам подписался новый юзер (все {total} из {total})! "
                f"+${REFERRAL_REWARD:.2f} на баланс.",
            )
        except TelegramError as e:
            log.warning("Не смог уведомить реферера %s: %s", referrer_id, e)
    return credited


async def on_target_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event: ChatMemberUpdated = update.chat_member
    if event.chat.id not in REFERRAL_TARGET_CHAT_IDS:
        return

    joined = not is_active_member(event.old_chat_member) and is_active_member(event.new_chat_member)
    left = is_active_member(event.old_chat_member) and not is_active_member(event.new_chat_member)

    if left:
        referred_id = event.old_chat_member.user.id
        referrer_id = apply_leave_penalty(referred_id)
        if referrer_id:
            try:
                await context.bot.send_message(
                    referrer_id,
                    f"⚠️ Один из твоих рефералов вышел из чата. -${REFERRAL_LEAVE_PENALTY:.2f} с баланса.",
                )
            except Exception as e:
                log.warning("Не смог уведомить реферера %s: %s", referrer_id, e)
        return

    if not joined:
        return

    invite_link_obj = event.invite_link
    if not invite_link_obj:
        return  # зашёл не по личной ссылке (добавили руками / по обычной публичной ссылке)

    referrer_id = get_referrer_id_by_invite_link(invite_link_obj.invite_link)
    if not referrer_id:
        return

    referred_id = event.new_chat_member.user.id
    if referred_id == referrer_id:
        return  # сам себя не засчитываем

    recorded = record_target_join(referred_id, event.chat.id, referrer_id)
    if recorded:
        log.info(
            "Referral step recorded: invited=%s referrer=%s chat=%s",
            referred_id, referrer_id, event.chat.id,
        )
    else:
        log.info(
            "Referral step already existed: invited=%s chat=%s; keeping original attribution",
            referred_id, event.chat.id,
        )
    await maybe_credit_referral(context, referrer_id, referred_id)


async def cmd_addbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-команда: /addbalance <сумма> или /addbalance <user_id> <сумма>.

    Сумма может быть отрицательной, но баланс не уйдёт ниже нуля.
    """
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != "private":
        return

    args = context.args
    if not args or len(args) > 2:
        await update.message.reply_text(
            "Использование:\n"
            "/addbalance 1  — накинуть себе $1\n"
            "/addbalance 123456789 1  — накинуть юзеру 123456789 сумму $1"
        )
        return

    try:
        if len(args) == 1:
            target_id = update.effective_user.id
            amount = parse_amount(args[0])
        else:
            target_id = int(args[0])
            amount = parse_amount(args[1])
        if amount == 0 or abs(amount) > MAX_BALANCE_ADJUSTMENT:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text("Неверные id/сумма. Пример: /addbalance 123456789 1.50")
        return

    balance = change_balance(target_id, update.effective_user.id, amount, "Команда /addbalance")
    if balance is None:
        await update.message.reply_text("Нельзя списать больше текущего баланса.")
        return
    await update.message.reply_text(f"✅ Готово. Баланс юзера {target_id}: ${balance:.2f}")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or update.effective_chat.type != "private":
        return
    stats = admin_stats()
    await update.message.reply_text(
        f"👤 Юзеров: {stats['users']}\n"
        f"👥 Рефералов засчитано: {stats['refs']}\n"
        f"🧾 Заявок в ожидании: {stats['pending']}\n"
        f"⏳ Проверок вступления: {stats['pending_verifications']}\n"
        f"💸 Всего выплачено: ${stats['paid']:.2f}\n"
        f"⚠️ Долг после выходов: ${stats['debt']:.2f} ({stats['debt_users']} чел.)\n\n"
        "Баланс можно изменить через кнопку или командой:\n"
        "<code>/addbalance user_id сумма</code>",
        reply_markup=admin_menu_kb(),
    )


async def cb_admin_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    context.user_data.pop("awaiting_crypto_invoice", None)
    context.user_data.pop("awaiting_user_info", None)
    context.user_data["awaiting_balance_change"] = True
    await safe_edit(
        query,
        "➕ Пришли одним сообщением: <code>user_id сумма</code>\n\n"
        "Например: <code>123456789 5.50</code>\n"
        "Для списания используй отрицательную сумму: <code>123456789 -1.00</code>.",
        admin_cancel_kb(),
    )
    await query.answer()


async def cb_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    context.user_data.pop("awaiting_balance_change", None)
    context.user_data.pop("awaiting_crypto_invoice", None)
    context.user_data.pop("awaiting_withdraw_stop_time", None)
    context.user_data.pop("awaiting_user_info", None)
    context.user_data.pop("awaiting_database_import", None)
    await safe_edit(query, "Действие отменено.", admin_menu_kb())
    await query.answer()


async def cb_admin_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    context.user_data.pop("awaiting_balance_change", None)
    context.user_data.pop("awaiting_crypto_invoice", None)
    context.user_data.pop("awaiting_withdraw_stop_time", None)
    context.user_data["awaiting_user_info"] = True
    await safe_edit(
        query,
        "👤 Пришли Telegram ID пользователя.\n\nПример: <code>123456789</code>",
        admin_cancel_kb(),
    )
    await query.answer()


async def cb_admin_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(
        query,
        "💾 <b>База данных</b>\n\n"
        "Скачивание создаёт резервную копию текущей базы. При загрузке бот сначала проверит файл, "
        "сохранит текущую версию как <code>referral_bot.db.before_import.db</code>, затем применит загруженную базу.",
        admin_database_kb(),
    )
    await query.answer()


async def cb_database_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        backup_path = create_database_backup()
        with open(backup_path, "rb") as backup:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=backup,
                filename="referral_bot_backup.db",
                caption="💾 Резервная копия базы Referral Bot.",
            )
    except (OSError, sqlite3.Error, TelegramError) as e:
        log.exception("Не смог создать/отправить резервную копию: %s", e)
        await query.answer("Не удалось подготовить копию базы", show_alert=True)
        return
    finally:
        if "backup_path" in locals() and os.path.exists(backup_path):
            os.remove(backup_path)
    await query.answer("База отправлена файлом")


async def cb_database_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    context.user_data["awaiting_database_import"] = True
    await safe_edit(
        query,
        "⬆️ Отправь сюда файл резервной копии <code>.db</code>.\n\n"
        "⚠️ После загрузки текущая база будет заменена, а предыдущая сохранится как "
        "<code>referral_bot.db.before_import.db</code>. Для отмены нажми «Отмена».",
        admin_cancel_kb(),
    )
    await query.answer()


async def cb_admin_withdraw_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    allowed, status = withdrawal_stop_status()
    _, _, start_text, end_text = get_withdraw_stop_schedule()
    state = "🔴 Сейчас выводы заблокированы." if not allowed else "🟢 Сейчас выводы доступны."
    await safe_edit(
        query,
        "⏰ <b>Ночной стоп выводов</b>\n"
        f"{state}\n{status}\n\n"
        f"Расписание: ежедневно {start_text}–{end_text} "
        f"({WITHDRAW_STOP_TIMEZONE}).",
        admin_withdraw_stop_kb(),
    )
    await query.answer()


async def cb_withdraw_stop_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    enabled = not night_withdraw_stop_enabled()
    set_setting("night_withdraw_stop_enabled", "1" if enabled else "0")
    allowed, status = withdrawal_stop_status()
    state = "🔴 Сейчас выводы заблокированы." if not allowed else "🟢 Сейчас выводы доступны."
    await safe_edit(
        query,
        "⏰ <b>Ночной стоп выводов</b>\n"
        f"{'Включён' if enabled else 'Выключен'}.\n{state}\n{status}",
        admin_withdraw_stop_kb(),
    )
    await query.answer("Настройка сохранена")


async def cb_withdraw_stop_set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    context.user_data.pop("awaiting_balance_change", None)
    context.user_data.pop("awaiting_crypto_invoice", None)
    context.user_data["awaiting_withdraw_stop_time"] = True
    _, _, start_text, end_text = get_withdraw_stop_schedule()
    await safe_edit(
        query,
        "🕒 <b>Настройка стопа выводов</b>\n"
        f"Сейчас: <b>{start_text}–{end_text}</b> ({WITHDRAW_STOP_TIMEZONE}).\n\n"
        "Пришли начало и конец в формате <code>HH:MM HH:MM</code>.\n"
        "Например: <code>00:00 07:00</code> или <code>22:30 07:00</code>.",
        admin_cancel_kb(),
    )
    await query.answer()


async def cb_admin_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(query, "💳 <b>Crypto Pay</b>\nВыбери действие:", admin_crypto_kb())
    await query.answer()


async def cb_crypto_create_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    context.user_data.pop("awaiting_balance_change", None)
    context.user_data["awaiting_crypto_invoice"] = True
    await safe_edit(
        query,
        f"➕ Пришли сумму счёта в <b>{WITHDRAW_ASSET}</b> и, при желании, описание через <code>|</code>.\n\n"
        "Примеры:\n<code>5</code>\n<code>10.50 | Пополнение баланса</code>",
        admin_cancel_kb(),
    )
    await query.answer()


async def cb_crypto_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    balances = await cryptobot_get_balances()
    if balances is None:
        await query.answer("Crypto Pay не ответил — проверь токен", show_alert=True)
        return
    lines = ["💰 <b>Баланс Crypto Pay</b>"]
    for item in balances:
        code = item.get("currency_code", "?")
        available = item.get("available", "0")
        onhold = item.get("onhold", "0")
        lines.append(f"{code}: <b>{available}</b> доступно · {onhold} в холде")
    await safe_edit(query, "\n".join(lines), admin_crypto_kb())
    await query.answer()


async def cb_crypto_payout_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    checks = await cryptobot_get_checks()
    if checks is None:
        await query.answer("Crypto Pay не ответил — проверь токен", show_alert=True)
        return
    try:
        active_total = sum(Decimal(str(c.get("amount", "0"))) for c in checks if c.get("status") == "active")
        activated_total = sum(Decimal(str(c.get("amount", "0"))) for c in checks if c.get("status") == "activated")
    except (InvalidOperation, ValueError):
        await query.answer("Crypto Pay вернул некорректные данные", show_alert=True)
        return
    local_paid = Decimal(str(admin_stats()["paid"]))
    await safe_edit(
        query,
        f"📊 <b>Выплаты в {WITHDRAW_ASSET}</b>\n"
        f"✅ Активировано пользователями: <b>{activated_total:.2f}</b>\n"
        f"⏳ Чеки в ожидании активации: <b>{active_total:.2f}</b>\n"
        f"🗃 Учтено ботом как выданное: <b>{local_paid:.2f}</b>\n\n"
        "Данные Crypto Pay показаны максимум по последним 1000 чекам этого приложения.",
        admin_crypto_kb(),
    )
    await query.answer()


async def cb_admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    withdrawals = get_pending_withdrawals()
    text = "🧾 Нет заявок в ожидании." if not withdrawals else "🧾 Выберите заявку для обработки:"
    await safe_edit(query, text, admin_pending_kb(withdrawals) if withdrawals else admin_menu_kb())
    await query.answer()


async def cb_admin_open_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    withdrawal = get_withdrawal(int(context.match.group(1)))
    if not withdrawal or withdrawal["status"] != "pending":
        await query.answer("Заявка больше не доступна", show_alert=True)
        return
    username = f"@{withdrawal['username']}" if withdrawal["username"] else "без username"
    await safe_edit(
        query,
        f"🧾 <b>Заявка на вывод #{withdrawal['id']}</b>\n"
        f"Юзер: {username} (id: <code>{withdrawal['user_id']}</code>)\n"
        f"Сумма: <b>${withdrawal['amount']:.2f}</b>",
        admin_withdraw_kb(withdrawal["id"], include_back=True),
    )
    await query.answer()


async def admin_balance_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод из кнопки админ-панели только у ожидающего админа."""
    if (
        update.effective_user.id not in ADMIN_IDS
        or update.effective_chat.type != "private"
        or not context.user_data.get("awaiting_balance_change")
    ):
        return
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("Нужно ровно два значения: <code>user_id сумма</code>.")
        return
    try:
        target_id = int(parts[0])
        amount = parse_amount(parts[1])
        if target_id <= 0 or amount == 0 or abs(amount) > MAX_BALANCE_ADJUSTMENT:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text("Неверные данные. Пример: <code>123456789 5.50</code>.")
        return
    balance = change_balance(target_id, update.effective_user.id, amount, "Админ-панель")
    if balance is None:
        await update.message.reply_text("Операция отменена: баланс нельзя сделать отрицательным.")
        return
    context.user_data.pop("awaiting_balance_change", None)
    await update.message.reply_text(
        f"✅ Баланс пользователя <code>{target_id}</code>: <b>${balance:.2f}</b>",
        reply_markup=admin_menu_kb(),
    )


async def admin_crypto_invoice_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создаёт Crypto Pay invoice после ввода суммы в приватной админке."""
    if (
        update.effective_user.id not in ADMIN_IDS
        or update.effective_chat.type != "private"
        or not context.user_data.get("awaiting_crypto_invoice")
    ):
        return
    amount_raw, separator, description = update.message.text.strip().partition("|")
    try:
        amount = parse_amount(amount_raw.strip())
        if amount <= 0 or amount > MAX_BALANCE_ADJUSTMENT:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text(
            f"Неверная сумма. Пример: <code>10.50 | Пополнение баланса</code> ({WITHDRAW_ASSET})."
        )
        return
    description = description.strip() if separator else "Пополнение через Referral Bot"
    if not description:
        description = "Пополнение через Referral Bot"
    result = await cryptobot_create_invoice(amount, description)
    if not result:
        await update.message.reply_text("Crypto Pay не создал счёт. Проверь токен и доступный лимит.")
        return
    url = result.get("bot_invoice_url") or result.get("pay_url")
    if not url:
        log.error("Crypto Pay createInvoice returned no payment URL: %s", result)
        await update.message.reply_text("Crypto Pay создал счёт, но не вернул ссылку. Проверь его в @CryptoBot → Crypto Pay.")
        return
    context.user_data.pop("awaiting_crypto_invoice", None)
    invoice_id = result.get("invoice_id", "?")
    await update.message.reply_text(
        f"✅ Счёт <b>#{invoice_id}</b> создан: <b>{amount:.2f} {WITHDRAW_ASSET}</b>\n"
        f"Описание: {escape(description)}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Открыть счёт", url=url)],
                                           [InlineKeyboardButton("← Crypto Pay", callback_data="admin_crypto")]]),
    )


async def admin_withdraw_stop_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет часы остановки вывода после ввода из админки."""
    if (
        update.effective_user.id not in ADMIN_IDS
        or update.effective_chat.type != "private"
        or not context.user_data.get("awaiting_withdraw_stop_time")
    ):
        return
    parts = update.message.text.strip().split()
    valid_time = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    if len(parts) != 2 or not all(valid_time.fullmatch(value) for value in parts):
        await update.message.reply_text("Неверный формат. Пример: <code>00:00 07:00</code>.")
        return
    if parts[0] == parts[1]:
        await update.message.reply_text("Начало и конец не могут совпадать — это заблокирует вывод на 24 часа.")
        return
    set_setting("withdraw_stop_start", parts[0])
    set_setting("withdraw_stop_end", parts[1])
    context.user_data.pop("awaiting_withdraw_stop_time", None)
    await update.message.reply_text(
        f"✅ Расписание сохранено: <b>{parts[0]}–{parts[1]}</b> ({WITHDRAW_STOP_TIMEZONE}).",
        reply_markup=admin_withdraw_stop_kb(),
    )


async def admin_user_info_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает админу баланс, приглашения, выплаты и возможный долг пользователя."""
    if (
        update.effective_user.id not in ADMIN_IDS
        or update.effective_chat.type != "private"
        or not context.user_data.get("awaiting_user_info")
    ):
        return
    try:
        user_id = int(update.message.text.strip())
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Нужен Telegram ID числом. Пример: <code>123456789</code>.")
        return
    info = get_user_admin_info(user_id)
    if not info:
        await update.message.reply_text("Пользователь ещё не запускал бота и не найден в базе.")
        return
    context.user_data.pop("awaiting_user_info", None)
    user = info["user"]
    withdrawals = info["withdrawals"]
    completed = withdrawals.get("completed", {"count": 0, "amount": 0})
    pending = withdrawals.get("pending", {"count": 0, "amount": 0})
    balance = Decimal(str(user["balance"]))
    available = max(balance, Decimal("0"))
    debt = max(-balance, Decimal("0"))
    username = f"@{escape(user['username'])}" if user["username"] else "без username"
    await update.message.reply_text(
        f"👤 <b>Пользователь</b> {username}\n"
        f"ID: <code>{user['user_id']}</code>\n\n"
        f"💰 Баланс: <b>${balance:.2f}</b>\n"
        f"💸 Доступно к следующей выплате: <b>${available:.2f}</b>\n"
        f"⚠️ Долг после выходов рефералов: <b>${debt:.2f}</b>\n\n"
        f"👥 Активных приглашений: <b>{user['referral_count']}</b>\n"
        f"📈 Всего засчитано: <b>{info['referrals']['total']}</b>\n"
        f"↩️ Выходов со штрафом: <b>{info['referrals']['penalized']}</b>\n\n"
        f"🧾 Выплачено: <b>{completed['count']}</b> заявок на ${Decimal(str(completed['amount'])):.2f}\n"
        f"⏳ Ожидает: <b>{pending['count']}</b> заявок на ${Decimal(str(pending['amount'])):.2f}",
        reply_markup=admin_menu_kb(),
    )


async def admin_database_import_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает SQLite-бэкап только от ожидающего админа в личке."""
    if (
        update.effective_user.id not in ADMIN_IDS
        or update.effective_chat.type != "private"
        or not context.user_data.get("awaiting_database_import")
        or not update.message.document
    ):
        return
    document = update.message.document
    if document.file_size and document.file_size > 25 * 1024 * 1024:
        await update.message.reply_text("Файл слишком большой: максимум 25 MB.")
        return
    uploaded_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="referral_import_", suffix=".db", dir=BASE_DIR, delete=False) as tmp:
            uploaded_path = tmp.name
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(custom_path=uploaded_path)
        previous_path = replace_database_from_backup(uploaded_path)
        uploaded_path = ""  # файл перемещён в DB_PATH
    except (OSError, sqlite3.Error, ValueError, TelegramError) as e:
        log.warning("Не удалось импортировать базу от admin=%s: %s", update.effective_user.id, e)
        await update.message.reply_text(f"❌ База не импортирована: {escape(str(e))}")
        return
    finally:
        if uploaded_path and os.path.exists(uploaded_path):
            os.remove(uploaded_path)
    context.user_data.pop("awaiting_database_import", None)
    await update.message.reply_text(
        "✅ База успешно загружена и применена.\n"
        f"Предыдущая версия сохранена на хосте: <code>{escape(previous_path)}</code>",
        reply_markup=admin_database_kb(),
    )


# ================================ MAIN =====================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")
    if not CRYPTOBOT_TOKEN:
        raise RuntimeError("Переменная окружения CRYPTOBOT_TOKEN не задана")
    if not ADMIN_IDS:
        raise RuntimeError("Укажите хотя бы один Telegram ID в ADMIN_IDS")
    init_db()

    unconfigured = [ch["title"] for ch in CHANNELS if ch["chat_id"] is None]
    if unconfigured:
        log.warning(
            "chat_id НЕ настроен для каналов: %s — проверка подписки по ним ПРОПУСКАЕТСЯ. "
            "Получи chat_id командой /groupid внутри канала/чата и впиши в CHANNELS.",
            ", ".join(unconfigured),
        )

    defaults = Defaults(parse_mode=ParseMode.HTML)
    application = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("groupid", cmd_groupid))
    application.add_handler(CommandHandler("mediastatus", cmd_mediastatus))
    application.add_handler(CommandHandler("refstatus", cmd_refstatus))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("addbalance", cmd_addbalance))
    application.add_handler(CallbackQueryHandler(cb_check_sub, pattern="^check_sub$"))
    application.add_handler(CallbackQueryHandler(cb_balance, pattern="^balance$"))
    application.add_handler(CallbackQueryHandler(cb_get_link, pattern="^get_link$"))
    application.add_handler(CallbackQueryHandler(cb_withdraw, pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(cb_pay_confirm, pattern=r"^pay_confirm:(\d+)$"))
    application.add_handler(CallbackQueryHandler(cb_pay_reject, pattern=r"^pay_reject:(\d+)$"))
    application.add_handler(CallbackQueryHandler(cb_verify_join, pattern=r"^verify_join:(-?\d+):(\d+)$"))
    application.add_handler(CallbackQueryHandler(cb_admin_balance, pattern="^admin_balance$"))
    application.add_handler(CallbackQueryHandler(cb_admin_user_info, pattern="^admin_user_info$"))
    application.add_handler(CallbackQueryHandler(cb_admin_database, pattern="^admin_database$"))
    application.add_handler(CallbackQueryHandler(cb_database_export, pattern="^database_export$"))
    application.add_handler(CallbackQueryHandler(cb_database_import, pattern="^database_import$"))
    application.add_handler(CallbackQueryHandler(cb_admin_cancel, pattern="^admin_cancel$"))
    application.add_handler(CallbackQueryHandler(cb_admin_pending, pattern="^admin_pending$"))
    application.add_handler(CallbackQueryHandler(cb_admin_open_withdrawal, pattern=r"^admin_open:(\d+)$"))
    application.add_handler(CallbackQueryHandler(cb_admin_withdraw_stop, pattern="^admin_withdraw_stop$"))
    application.add_handler(CallbackQueryHandler(cb_withdraw_stop_toggle, pattern="^withdraw_stop_toggle$"))
    application.add_handler(CallbackQueryHandler(cb_withdraw_stop_set_time, pattern="^withdraw_stop_set_time$"))
    application.add_handler(CallbackQueryHandler(cb_admin_crypto, pattern="^admin_crypto$"))
    application.add_handler(CallbackQueryHandler(cb_crypto_create_invoice, pattern="^crypto_create_invoice$"))
    application.add_handler(CallbackQueryHandler(cb_crypto_balance, pattern="^crypto_balance$"))
    application.add_handler(CallbackQueryHandler(cb_crypto_payout_stats, pattern="^crypto_payout_stats$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_balance_input))
    # Второй MessageHandler — в другой группе: иначе первый обработчик текста
    # перехватит сообщение, даже если ждёт не баланс, а Crypto Pay счёт.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_crypto_invoice_input), group=1)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_withdraw_stop_time_input), group=2)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_info_input), group=3)
    application.add_handler(MessageHandler(filters.Document.ALL, admin_database_import_input), group=4)
    application.add_handler(ChatJoinRequestHandler(on_join_request))
    application.add_handler(ChatMemberHandler(on_target_join, ChatMemberHandler.CHAT_MEMBER))

    log.info("Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
