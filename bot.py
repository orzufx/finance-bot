import logging
import sqlite3
from datetime import datetime, timedelta, time as dt_time, timezone, timedelta as td
from datetime import timezone as tz
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("finance_bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ─── Bot Token ───────────────────────────────────────────────────────────────
BOT_TOKEN  = "8607311771:AAHSFXsq9usGf4GxQcvhf-PNbB0I_vrf0X4"
CHANNEL_ID = -1003863923798
UTC5       = pytz.timezone("Asia/Tashkent")   # Toshkent vaqti

# ─── Conversation States ─────────────────────────────────────────────────────
(
    REGISTER_NAME,
    _UNUSED_STATE_1,
    MAIN_MENU,
    ANOTHER_DATE_INPUT,       # "Boshqa kun" — sana kiritish
    ANOTHER_DATE_TYPE,        # Sana kiritilgandan keyin harajat/kirim tanlash
    EXPENSE_PAYMENT,          # To'lov usuli: AVO / Shaxsiy karta / Naqd
    EXPENSE_CATEGORY,         # Korzinka / Mini market / Ovqatlanish / ...
    INCOME_PAYMENT,           # To'lov usuli: AVO / Shaxsiy karta / Naqd
    INCOME_CATEGORY,          # Oylik maosh / Qarz / Kredit / ...
    SELECT_CARD_FOR_EXPENSE,  # Qaysi karta orqali to'landi
    ENTER_AMOUNT,
    ENTER_COMMENT,
    # Card states
    CARD_MENU,
    CARD_ADD_NAME,
    CARD_ADD_NUMBER,
    CARD_ADD_BALANCE,
    CARD_ACTION,
    CARD_UPDATE_BALANCE,
    CARD_DELETE_CONFIRM,
    WALLET_EDIT,            # AVO yoki Naqd balansini o'zgartirish
    TRANSFER_FROM,          # O'tkazma: qaysi kartadan
    TRANSFER_TO,            # O'tkazma: qaysi kartaga
    TRANSFER_AMOUNT,        # O'tkazma: miqdor
) = range(23)


# ─── Database Abstraction ────────────────────────────────────────────────────
import os

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

class PostgresCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor
    def execute(self, query, params=()):
        query = query.replace("?", "%s")
        if "INSERT OR REPLACE INTO settings" in query:
            query = """
                INSERT INTO settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """
        self._cursor.execute(query, params)
        return self
    def fetchall(self): return self._cursor.fetchall()
    def fetchone(self): return self._cursor.fetchone()
    def __iter__(self): return iter(self._cursor)

class PostgresConnWrapper:
    def __init__(self, conn):
        self._conn = conn
    def cursor(self):
        return PostgresCursorWrapper(self._conn.cursor())
    def commit(self): self._conn.commit()
    def close(self): self._conn.close()
    def execute(self, query, params=()):
        c = self.cursor()
        c.execute(query, params)
        return c

def get_conn():
    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return PostgresConnWrapper(conn)
    else:
        return sqlite3.connect("finance.db")

def init_db():
    conn = get_conn()
    c = conn.cursor()
    
    if DATABASE_URL:
        # PostgreSQL Schema
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id             SERIAL PRIMARY KEY,
                user_name      TEXT    NOT NULL,
                type           TEXT    NOT NULL,
                payment_method TEXT    NOT NULL DEFAULT '',
                category       TEXT    NOT NULL,
                amount         REAL    NOT NULL,
                comment        TEXT,
                created_at     TEXT    NOT NULL
            )
        """)
        c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT ''")
        c.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id          SERIAL PRIMARY KEY,
                user_name   TEXT    NOT NULL,
                card_name   TEXT    NOT NULL,
                card_number TEXT    NOT NULL DEFAULT '',
                balance     REAL    NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id          SERIAL PRIMARY KEY,
                user_name   TEXT    NOT NULL,
                wallet_type TEXT    NOT NULL,
                balance     REAL    NOT NULL DEFAULT 0,
                UNIQUE(user_name, wallet_type)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
    else:
        # SQLite Schema
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name      TEXT    NOT NULL,
                type           TEXT    NOT NULL,
                payment_method TEXT    NOT NULL DEFAULT '',
                category       TEXT    NOT NULL,
                amount         REAL    NOT NULL,
                comment        TEXT,
                created_at     TEXT    NOT NULL
            )
        """)
        try:
            c.execute("ALTER TABLE transactions ADD COLUMN payment_method TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
    
        c.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name   TEXT    NOT NULL,
                card_name   TEXT    NOT NULL,
                card_number TEXT    NOT NULL DEFAULT '',
                balance     REAL    NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name   TEXT    NOT NULL,
                wallet_type TEXT    NOT NULL,
                balance     REAL    NOT NULL DEFAULT 0,
                UNIQUE(user_name, wallet_type)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
    
    conn.commit()
    conn.close()



# ── Transactions ──
def save_transaction(user_name, trans_type, payment_method, category,
                     amount, comment="", created_at=None):
    conn = get_conn()
    c = conn.cursor()
    if created_at is None:
        created_at = datetime.now(UTC5).isoformat()
    c.execute(
        "INSERT INTO transactions "
        "(user_name, type, payment_method, category, amount, comment, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_name, trans_type, payment_method, category, amount, comment, created_at),
    )
    conn.commit()
    conn.close()


def get_summary(user_name: str, period: str) -> dict:
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now(UTC5)
    if period == "today":
        since = now.strftime("%Y-%m-%d")
        rows = c.execute(
            "SELECT type, SUM(amount) FROM transactions "
            "WHERE user_name=? AND created_at LIKE ? GROUP BY type",
            (user_name, f"{since}%"),
        ).fetchall()
    elif period == "week":
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = c.execute(
            "SELECT type, SUM(amount) FROM transactions "
            "WHERE user_name=? AND created_at >= ? GROUP BY type",
            (user_name, week_start.isoformat()),
        ).fetchall()
    else:  # month
        since = now.strftime("%Y-%m")
        rows = c.execute(
            "SELECT type, SUM(amount) FROM transactions "
            "WHERE user_name=? AND created_at LIKE ? GROUP BY type",
            (user_name, f"{since}%"),
        ).fetchall()
    conn.close()
    result = {"harajat": 0, "kirim": 0}
    for tp, s in rows:
        result[tp] = s or 0
    return result


def get_transactions_for_period(user_name: str, period: str) -> list:
    """Returns list of (type, payment_method, category, amount, comment, created_at)."""
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now(UTC5)
    if period == "today":
        since = now.strftime("%Y-%m-%d")
        rows = c.execute(
            "SELECT type, payment_method, category, amount, comment, created_at "
            "FROM transactions WHERE user_name=? AND created_at LIKE ? ORDER BY created_at",
            (user_name, f"{since}%"),
        ).fetchall()
    elif period == "week":
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = c.execute(
            "SELECT type, payment_method, category, amount, comment, created_at "
            "FROM transactions WHERE user_name=? AND created_at >= ? ORDER BY created_at",
            (user_name, week_start.isoformat()),
        ).fetchall()
    else:  # month
        since = now.strftime("%Y-%m")
        rows = c.execute(
            "SELECT type, payment_method, category, amount, comment, created_at "
            "FROM transactions WHERE user_name=? AND created_at LIKE ? ORDER BY created_at",
            (user_name, f"{since}%"),
        ).fetchall()
    conn.close()
    return rows


# ── Cards ──
def get_cards(user_name: str) -> list:
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, card_name, card_number, balance FROM cards "
        "WHERE user_name=? ORDER BY id",
        (user_name,),
    ).fetchall()
    conn.close()
    return rows  # [(id, name, number, balance), ...]


def add_card(user_name: str, card_name: str, card_number: str, balance: float):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO cards (user_name, card_name, card_number, balance, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_name, card_name, card_number, balance, datetime.now(UTC5).isoformat()),
    )
    conn.commit()
    conn.close()


def update_card_balance(card_id: int, new_balance: float):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE cards SET balance=? WHERE id=?", (new_balance, card_id))
    conn.commit()
    conn.close()


def delete_card(card_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM cards WHERE id=?", (card_id,))
    conn.commit()
    conn.close()


def get_card_by_id(card_id: int):
    conn = get_conn()
    c = conn.cursor()
    row = c.execute(
        "SELECT id, card_name, card_number, balance FROM cards WHERE id=?", (card_id,)
    ).fetchone()
    conn.close()
    return row


# ─── Wallet (AVO / Naqd) Helpers ─────────────────────────────────────────────
def get_wallet_balance(user_name: str, wallet_type: str) -> float:
    conn = get_conn()
    c = conn.cursor()
    row = c.execute(
        "SELECT balance FROM wallets WHERE user_name=? AND wallet_type=?",
        (user_name, wallet_type),
    ).fetchone()
    conn.close()
    return row[0] if row else 0.0


def set_wallet_balance(user_name: str, wallet_type: str, balance: float):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (?,?,?) "
        "ON CONFLICT(user_name, wallet_type) DO UPDATE SET balance=excluded.balance",
        (user_name, wallet_type, balance),
    )
    conn.commit()
    conn.close()


def adjust_wallet(user_name: str, wallet_type: str, delta: float):
    """delta < 0 harajat, delta > 0 kirim uchun."""
    current = get_wallet_balance(user_name, wallet_type)
    set_wallet_balance(user_name, wallet_type, current + delta)


def payment_to_wallet_type(payment_method: str):
    """To'lov usuli stringidan wallet type qaytaradi yoki None."""
    pm = payment_method.lower()
    if "avo" in pm:
        return "avo"
    if "naqd" in pm or "cash" in pm:
        return "naqd"
    return None




def save_setting(key: str, value: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


def get_setting(key: str):
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def register_user(telegram_id: int, display_name: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
              (f"user_{telegram_id}", display_name))
    conn.commit()
    conn.close()
    # Also insert into users table
    conn2 = get_conn()
    c2 = conn2.cursor()
    now_str = datetime.now(UTC5).isoformat()
    if DATABASE_URL:
        c2.execute(
            "INSERT INTO users (telegram_id, display_name, created_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (telegram_id) DO UPDATE SET display_name = EXCLUDED.display_name",
            (telegram_id, display_name, now_str),
        )
    else:
        c2.execute(
            "INSERT OR REPLACE INTO users (telegram_id, display_name, created_at) VALUES (?,?,?)",
            (telegram_id, display_name, now_str),
        )
    conn2.commit()
    conn2.close()
    # Also ensure wallets exist
    set_wallet_balance(display_name, "naqd", get_wallet_balance(display_name, "naqd"))


def get_user_name(telegram_id: int) -> str:
    """Returns display_name or None if not registered."""
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("SELECT display_name FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def get_all_user_names() -> list:
    """Returns list of all registered user display names."""
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute("SELECT display_name FROM users ORDER BY created_at").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ─── Channel Data Helpers ─────────────────────────────────────────────────────
def get_all_cards_summary() -> dict:
    """Returns {user_name: [(card_name, card_number, balance), ...]}."""
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute(
        "SELECT user_name, card_name, card_number, balance FROM cards ORDER BY user_name, id"
    ).fetchall()
    conn.close()
    result: dict = {}
    for user, name, number, balance in rows:
        result.setdefault(user, []).append((name, number, balance))
    return result


def get_today_transactions_all(target_date: str = None) -> dict:
    """Returns {user_name: [(type, payment, category, amount, comment), ...]}."""
    conn = get_conn()
    c = conn.cursor()
    date_key = target_date if target_date else datetime.now(UTC5).strftime("%Y-%m-%d")
    rows = c.execute(
        "SELECT user_name, type, payment_method, category, amount, comment "
        "FROM transactions WHERE created_at LIKE ? ORDER BY user_name, created_at",
        (f"{date_key}%",),
    ).fetchall()
    conn.close()
    result: dict = {}
    for user, typ, pay, cat, amt, cmt in rows:
        result.setdefault(user, []).append((typ, pay, cat, amt, cmt or ""))
    return result


# ─── Channel Message Builder ──────────────────────────────────────────────────
AVO_USER          = "SHARED"   # AVO barcha userlar uchun umumiy
ALLOWED_USER_IDS  = {5701684264, 6392413373, 7064655656}


def build_channel_text(is_evening: bool = False, target_date: str = None) -> str:
    if target_date:
        try:
            display_date = datetime.strptime(target_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            display_date = target_date
    else:
        display_date = datetime.now(UTC5).strftime("%d.%m.%Y")
    icon  = "🌙" if is_evening else "🌅"
    title = "Yakuniy hisobot" if is_evening else "Kanal hisoboti"
    lines = [f"{icon} *{display_date} — {title}*\n"]

    # ── Mablag'lar bo'limi
    lines.append("💳 *Mablag'lar holati:*\n")
    all_cards     = get_all_cards_summary()
    grand_balance = 0

    # AVO umumiy (barcha userlar uchun bir xil)
    avo_bal = get_wallet_balance(AVO_USER, "avo")
    lines.append(f"🏛 *AVO (umumiy):*  `{format_amount(avo_bal)}`")
    grand_balance += avo_bal
    lines.append("")

    # Har bir user uchun Naqd + kartalar
    for user in get_all_user_names():
        naqd_bal = get_wallet_balance(user, "naqd")
        cards = all_cards.get(user, [])
        lines.append(f"👤 *{user}*")
        lines.append(f"  ├ 💵 Naqd  ➤  `{format_amount(naqd_bal)}`")
        grand_balance += naqd_bal
        if not cards:
            lines.append("  └ 🏦 _karta yo'q_")
        else:
            for i, (name, number, balance) in enumerate(cards):
                num_str = f" · `{mask_number(number)}`" if number else ""
                prefix  = "└" if i == len(cards) - 1 else "├"
                lines.append(f"  {prefix} 🏦 {name}{num_str}  ➤  `{format_amount(balance)}`")
                grand_balance += balance
        lines.append("")
    lines.append(f"💼 *Jami balans: `{format_amount(grand_balance)}`*")
    lines.append("━" * 22)

    # ── Tranzaksiyalar bo'limi
    txn_label = f"📋 *{display_date} — harajatlar / kirimlar:*\n"
    lines.append(txn_label)
    all_txns      = get_today_transactions_all(target_date=target_date)
    grand_expense = 0
    grand_income  = 0
    any_txn       = False

    for user in get_all_user_names():
        txns     = all_txns.get(user, [])
        expenses = [(p, c, a, cm) for t, p, c, a, cm in txns if t == "harajat"]
        incomes  = [(p, c, a, cm) for t, p, c, a, cm in txns if t == "kirim"]
        if not expenses and not incomes:
            continue
        any_txn = True
        lines.append(f"👤 *{user}*")
        if expenses:
            user_exp = 0
            lines.append("📤 _Harajatlar:_")
            for i, (pay, cat, amt, cmt) in enumerate(expenses, 1):
                cmt_str = f"\n   └ _{cmt}_" if cmt else ""
                lines.append(f"*{i}.* `{format_amount(amt)}` — {cat}  ·  _[{pay}]_{cmt_str}")
                user_exp      += amt
                grand_expense += amt
            lines.append(f"🔴 *Jami harajat: `{format_amount(user_exp)}`*")
        if incomes:
            user_inc = 0
            lines.append("📥 _Kirimlar:_")
            for i, (pay, cat, amt, cmt) in enumerate(incomes, 1):
                cmt_str = f"\n   └ _{cmt}_" if cmt else ""
                lines.append(f"*{i}.* `{format_amount(amt)}` — {cat}  ·  _[{pay}]_{cmt_str}")
                user_inc     += amt
                grand_income += amt
            lines.append(f"💚 *Jami kirim: `{format_amount(user_inc)}`*")
        lines.append("")

    if not any_txn:
        lines.append("_— bugun hali yozuv yo'q_")

    lines.append("━" * 22)
    balance   = grand_income - grand_expense
    sign      = "+" if balance >= 0 else ""
    bal_emoji = "💚" if balance >= 0 else "🔴"
    lines.append(
        f"💚 *Umumiy kirim:*    `{format_amount(grand_income)}`\n"
        f"🔴 *Umumiy harajat:* `{format_amount(grand_expense)}`\n"
        f"{bal_emoji} *Balans:*          `{sign}{format_amount(balance)}`"
    )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n\n_...va boshqalar_"
    return text


# ─── Channel Async Functions ──────────────────────────────────────────────────
async def send_morning_message(context):
    """06:00 da kanalga kartalar va bo'sh harajat xabarini yuboradi."""
    try:
        text = build_channel_text(is_evening=False)
        msg  = await context.bot.send_message(
            chat_id=CHANNEL_ID, text=text, parse_mode="Markdown"
        )
        save_setting("ch_msg_id",   str(msg.message_id))
        save_setting("ch_msg_date", datetime.now(UTC5).strftime("%Y-%m-%d"))
        logger.info(f"[CHANNEL] Morning message sent: {msg.message_id}")
    except Exception as e:
        logger.error(f"[CHANNEL] Morning send failed: {e}")


async def update_channel_message(bot, target_date: str = None):
    """Har tranzaksiyadan keyin kanal xabarini edit qiladi.
    Agar target_date bugundan farqli bo'lsa, o'sha kun uchun alohida xabar yuboradi."""
    today = datetime.now(UTC5).strftime("%Y-%m-%d")

    # Boshqa kun uchun kiritilgan bo'lsa — alohida xabar yuboramiz
    if target_date and target_date != today:
        try:
            text = build_channel_text(is_evening=False, target_date=target_date)
            await bot.send_message(
                chat_id=CHANNEL_ID, text=text, parse_mode="Markdown"
            )
            logger.info(f"[CHANNEL] Past-date message sent for {target_date}")
        except Exception as e:
            logger.error(f"[CHANNEL] Past-date send failed: {e}")
        return

    # Bugungi xabarni yangilash
    msg_id   = get_setting("ch_msg_id")
    msg_date = get_setting("ch_msg_date")
    text     = build_channel_text(is_evening=False, target_date=today)

    if msg_id and msg_date == today:
        try:
            await bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=int(msg_id),
                text=text,
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.warning(f"[CHANNEL] Edit failed, sending new: {e}")

    # Yangi xabar yuborish (edit mumkin bo'lmasa yoki kun o'zgargan bo'lsa)
    try:
        msg = await bot.send_message(
            chat_id=CHANNEL_ID, text=text, parse_mode="Markdown"
        )
        save_setting("ch_msg_id",   str(msg.message_id))
        save_setting("ch_msg_date", today)
        logger.info(f"[CHANNEL] New message sent: {msg.message_id}")
    except Exception as e:
        logger.error(f"[CHANNEL] Send failed: {e}")


async def send_evening_summary(context):
    """23:55 da ertalabki xabarni o'chirib, kunlik yakuniy hisobot yuboradi."""
    # 1) Ertalabki xabarni o'chirish
    msg_id   = get_setting("ch_msg_id")
    msg_date = get_setting("ch_msg_date")
    today    = datetime.now(UTC5).strftime("%Y-%m-%d")
    if msg_id and msg_date == today:
        try:
            await context.bot.delete_message(
                chat_id=CHANNEL_ID, message_id=int(msg_id)
            )
            logger.info(f"[CHANNEL] Morning message deleted: {msg_id}")
        except Exception as e:
            logger.warning(f"[CHANNEL] Delete morning msg failed: {e}")
        # ID ni tozalaymiz — ertaga yangi xabar bo'ladi
        save_setting("ch_msg_id",   "")
        save_setting("ch_msg_date", "")

    # 2) Yakuniy hisobotni yuborish
    try:
        text = build_channel_text(is_evening=True)
        await context.bot.send_message(
            chat_id=CHANNEL_ID, text=text, parse_mode="Markdown"
        )
        logger.info("[CHANNEL] Evening summary sent")
    except Exception as e:
        logger.error(f"[CHANNEL] Evening send failed: {e}")


# ─── Texts ───────────────────────────────────────────────────────────────────
TEXTS = {
    "uz": {
        "welcome":              "🏦 *Finance Bot*\n\nAssalomu alaykum!\nIltimos, tilni tanlang:",
        "who_are_you":          "👤 *Siz kimsiz?*",
        "main_menu":            "Bosh menyu",
        "payment_method":       "💳 *To'lov usulini tanlang:*",
        "expense_category":     "📂 *Harajat turini tanlang:*",
        "income_category":      "📂 *Kirim turini tanlang:*",
        "enter_amount":         "💵 *Miqdorni kiriting* (so'm):",
        "enter_comment":        "📝 Izoh kiriting\n_(yoki /skip bosing)_",
        "saved":                "saqlandi",
        "income_label":         "Kirim",
        "expense_label":        "Harajat",
        "balance_label":        "Balans",
        "back":                 "⬅️ Orqaga",
        "cancel":               "❌ Bekor qilish",
        "another_date":         "📅 Boshqa kun",
        "enter_date":           "📅 *Sanani kiriting* `KK.OO.YYYY`\n\n_Masalan:_ `05.04.2026`",
        "invalid_date":         "❌ Noto'g'ri format!\nKK.OO.YYYY ko'rinishida kiriting\n_Masalan:_ `05.04.2026`",
        "another_date_choose":  "📅 *{date}* sanasi uchun:\nNimani kiritmoqchisiz?",
        "change_language":      "🌐 Til",
        # Card texts
        "cards_menu":           "Kartalarim",
        "no_cards":             "📭 Sizda hali karta yo'q\n\n_Yangi karta qo'shish uchun tugmani bosing_",
        "card_add_name":        "✏️ *Karta nomini kiriting:*\n_Masalan: Kapitalbank, Uzcard, Humo_",
        "card_add_number":      "🔢 *Karta raqamining so'nggi 4 raqamini* kiriting\n_(yoki /skip — raqamsiz)_",
        "card_add_balance":     "💰 *Kartadagi mavjud summani* kiriting (so'm):",
        "card_added":           "muvaffaqiyatli qo'shildi!",
        "card_action":          "Karta amali:",
        "card_update_balance":  "💰 *Yangi balansni kiriting* (so'm):",
        "card_updated":         "✅ Balans yangilandi!",
        "card_delete_confirm":  "🗑 Kartani o'chirishni tasdiqlaysizmi?",
        "card_deleted":         "Karta o'chirildi",
        "yes_delete":           "✅ Ha, o'chir",
        "no_cancel":            "❌ Yo'q",
        "add_card":             "➕ Yangi karta qo'shish",
        "update_balance_btn":   "✏️ Balansni o'zgartirish",
        "delete_card_btn":      "🗑 Kartani o'chirish",
        "total_balance":        "Jami balans",
        "invalid_number":       "❌ Iltimos, to'g'ri raqam kiriting",
        "select_payment_card":  "💳 *Qaysi karta orqali to'ladingiz?*",
        "no_cards_for_payment": "📭 Sizda karta yo'q\n_Kartalar bo'limidan avval karta qo'shing_",
        "card_deducted":        "yangi balans →",
    },
    "en": {
        "welcome":              "🏦 *Finance Bot*\n\nHello!\nPlease choose a language:",
        "who_are_you":          "👤 *Who are you?*",
        "main_menu":            "Main menu",
        "payment_method":       "💳 *Choose payment method:*",
        "expense_category":     "📂 *Choose expense type:*",
        "income_category":      "📂 *Choose income type:*",
        "enter_amount":         "💵 *Enter amount* (UZS):",
        "enter_comment":        "📝 Enter a comment\n_(or press /skip)_",
        "saved":                "saved",
        "income_label":         "Income",
        "expense_label":        "Expense",
        "balance_label":        "Balance",
        "back":                 "⬅️ Back",
        "cancel":               "❌ Cancel",
        "another_date":         "📅 Another date",
        "enter_date":           "📅 *Enter date* `DD.MM.YYYY`\n\n_Example:_ `05.04.2026`",
        "invalid_date":         "❌ Invalid format!\nUse DD.MM.YYYY\n_Example:_ `05.04.2026`",
        "another_date_choose":  "📅 For *{date}*:\nWhat would you like to add?",
        "change_language":      "🌐 Language",
        # Card texts
        "cards_menu":           "My Cards",
        "no_cards":             "📭 You have no cards yet\n\n_Press the button below to add one_",
        "card_add_name":        "✏️ *Enter card name:*\n_e.g. Kapitalbank, Uzcard, Humo_",
        "card_add_number":      "🔢 *Enter last 4 digits* of the card\n_(or /skip — without number)_",
        "card_add_balance":     "💰 *Enter current card balance* (UZS):",
        "card_added":           "successfully added!",
        "card_action":          "Card action:",
        "card_update_balance":  "💰 *Enter new balance* (UZS):",
        "card_updated":         "✅ Balance updated!",
        "card_delete_confirm":  "🗑 Are you sure you want to delete this card?",
        "card_deleted":         "Card deleted",
        "yes_delete":           "✅ Yes, delete",
        "no_cancel":            "❌ No",
        "add_card":             "➕ Add new card",
        "update_balance_btn":   "✏️ Update balance",
        "delete_card_btn":      "🗑 Delete card",
        "total_balance":        "Total balance",
        "invalid_number":       "❌ Please enter a valid number",
        "select_payment_card":  "💳 *Which card did you pay with?*",
        "no_cards_for_payment": "📭 You have no cards\n_Add a card in the Cards section first_",
        "card_deducted":        "new balance →",
    },
}

# ─── Categories ──────────────────────────────────────────────────────────────
PAYMENT_METHODS = {
    "uz": ["🏛 AVO", "💳 Shaxsiy karta", "💵 Naqd"],
    "en": ["🏛 AVO", "💳 Personal card", "💵 Cash"],
}
# BIRGALIKDA uchun ATTO yo'q


EXPENSE_CATEGORIES = {
    "uz": ["🛒 Korzinka", "🏪 Mini market", "🍽 Ovqatlanish", "🛍 Bozor", "💸 Qarz", "🏦 Kredit", "🚌 ATTO", "📦 Boshqa"],
    "en": ["🛒 Korzinka", "🏪 Mini market", "🍽 Dining", "🛍 Bazaar", "💸 Debt", "🏦 Credit", "🚌 ATTO", "📦 Other"],
}

INCOME_CATEGORIES = {
    "uz": ["💼 Oylik maosh", "🤝 Qarz", "🏦 Kredit", "📦 Boshqa"],
    "en": ["💼 Monthly salary", "🤝 Debt", "🏦 Credit", "📦 Other"],
}

# Payment methods that trigger card deduction
CARD_PAYMENT_METHODS = {"💳 Shaxsiy karta", "💳 Personal card"}


# ─── Helpers ─────────────────────────────────────────────────────────────────
def lang(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("lang", "uz")


def t(key: str, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return TEXTS[lang(ctx)][key]


def format_amount(amount: float) -> str:
    return f"{amount:,.0f} so'm"


def mask_number(number: str) -> str:
    if not number:
        return ""
    return f"**** {number[-4:]}" if len(number) >= 4 else number


def parse_date(date_str: str):
    """Parse DD.MM.YYYY -> datetime or None."""
    try:
        return datetime.strptime(date_str.strip(), "%d.%m.%Y")
    except ValueError:
        return None


def build_summary_text(user_name: str, period: str, period_label: str, ctx) -> str:
    data    = get_summary(user_name, period)
    income  = data["kirim"]
    expense = data["harajat"]
    balance = income - expense
    sign    = "+" if balance >= 0 else ""
    l       = lang(ctx)
    texts   = TEXTS[l]
    bal_emoji = "💚" if balance >= 0 else "🔴"
    return (
        f"📊 *{period_label}*"
        f"\n👤 {user_name}"
        f"\n\n"
        f"╔══════════════════════╗\n"
        f"║ 💚 {texts['income_label']:10}  `{format_amount(income)}`\n"
        f"║ 🔴 {texts['expense_label']:10}  `{format_amount(expense)}`\n"
        f"╠══════════════════════╣\n"
        f"║ {bal_emoji} {texts['balance_label']:10}  `{sign}{format_amount(balance)}`\n"
        f"╚══════════════════════╝"
    )


def build_detailed_report(user_name: str, period: str, period_label: str, ctx) -> str:
    """Detailed report: each transaction with category and comment, grouped by date."""
    rows = get_transactions_for_period(user_name, period)
    l     = lang(ctx)
    texts = TEXTS[l]

    header = f"📊 *{period_label}*  ·  👤 *{user_name}*\n"
    no_data = "Hech qanday ma'lumot yo'q" if l == "uz" else "No transactions found"

    if not rows:
        return header + f"\n_{no_data}_"

    # Group rows by display date (DD.MM.YYYY)
    by_date: dict = {}
    for trans_type, payment, category, amount, comment, created_at in rows:
        date_key = created_at[:10]
        try:
            display_date = datetime.strptime(date_key, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            display_date = date_key
        if display_date not in by_date:
            by_date[display_date] = {"harajat": [], "kirim": []}
        by_date[display_date][trans_type].append((payment, category, amount, comment or ""))

    exp_label      = "Harajatlar"   if l == "uz" else "Expenses"
    inc_label      = "Kirimlar"     if l == "uz" else "Income"
    total_exp_lbl  = "Jami harajat" if l == "uz" else "Total expenses"
    total_inc_lbl  = "Jami kirim"   if l == "uz" else "Total income"

    lines = [header]
    grand_income  = 0
    grand_expense = 0

    for display_date in sorted(by_date.keys(),
                               key=lambda d: datetime.strptime(d, "%d.%m.%Y")):
        day_data = by_date[display_date]
        expenses = day_data["harajat"]
        incomes  = day_data["kirim"]

        # Date header (always shown)
        lines.append(f"━━━━━━━━━━━━━━\n📅 *{display_date}*")

        if expenses:
            lines.append(f"\n📤 *{exp_label}:*")
            day_exp = 0
            for i, (payment, category, amount, comment) in enumerate(expenses, 1):
                comment_str = f"\n    └ _{comment}_" if comment else ""
                lines.append(
                    f"*{i}.* `{format_amount(amount)}` — {category}"
                    f"  ·  _[{payment}]_{comment_str}"
                )
                day_exp    += amount
                grand_expense += amount
            lines.append(f"🔴 *{total_exp_lbl}: `{format_amount(day_exp)}`*")

        if incomes:
            lines.append(f"\n📥 *{inc_label}:*")
            day_inc = 0
            for i, (payment, category, amount, comment) in enumerate(incomes, 1):
                comment_str = f"\n    └ _{comment}_" if comment else ""
                lines.append(
                    f"*{i}.* `{format_amount(amount)}` — {category}"
                    f"  ·  _[{payment}]_{comment_str}"
                )
                day_inc   += amount
                grand_income += amount
            lines.append(f"💚 *{total_inc_lbl}: `{format_amount(day_inc)}`*")

        lines.append("")

    # Grand total summary
    balance   = grand_income - grand_expense
    sign      = "+" if balance >= 0 else ""
    bal_emoji = "💚" if balance >= 0 else "🔴"
    lines.append(
        f"━━━━━━━━━━━━━━"
        f"\n💚 *{texts['income_label']}:*   `{format_amount(grand_income)}`"
        f"\n🔴 *{texts['expense_label']}:*  `{format_amount(grand_expense)}`"
        f"\n{bal_emoji} *{texts['balance_label']}:*  `{sign}{format_amount(balance)}`"
    )

    text = "\n".join(lines)
    # Telegram 4096 char limit
    if len(text) > 3900:
        note = "\n\n_...va boshqa yozuvlar_" if l == "uz" else "\n\n_...and more transactions_"
        text = text[:3800] + note
    return text


def build_cards_text(user_name: str, ctx) -> str:
    l     = lang(ctx)
    title = f"💳 *{TEXTS[l]['cards_menu']}*\n👤 {user_name}\n\n"

    naqd_bal = get_wallet_balance(user_name, "naqd")
    wallet_lines = (
        f"💵 *Naqd:*          `{format_amount(naqd_bal)}`"
    )
    total = naqd_bal
    cards = get_cards(user_name)
    lines = []
    for card_id, name, number, balance in cards:
        num_str = f" · `{mask_number(number)}`" if number else ""
        lines.append(
            f"┌─ 🏦 *{name}*{num_str}\n"
            f"└─ 💰 `{format_amount(balance)}`"
        )
        total += balance

    footer = (
        f"\n\n━━━━━━━━━━━━━━━━━━━━━━"
        f"\n💼 *{TEXTS[l]['total_balance']}:*  `{format_amount(total)}`"
    )

    if lines:
        body = wallet_lines + "\n\n" + "\n\n".join(lines)
    else:
        body = wallet_lines
    return title + body + footer


# ─── Keyboards ───────────────────────────────────────────────────────────────
def kb_main(ctx):
    l         = lang(ctx)
    user_name = ctx.user_data.get("user_name", "")
    web_app_url = "https://orzu-finance-bot.herokuapp.com"
    if l == "uz":
        rows = [
            [InlineKeyboardButton("📊  Web Dashboard (Yangi!)", web_app=WebAppInfo(url=web_app_url))],
            [InlineKeyboardButton("💸  Harajat", callback_data="menu_expense"),
             InlineKeyboardButton("💰  Kirim",   callback_data="menu_income")],
            [InlineKeyboardButton("📅  Boshqa kun uchun kiritish", callback_data="menu_another_date")],
            [InlineKeyboardButton("💳  Kartalarim", callback_data="menu_cards")],
            [InlineKeyboardButton("🔄  O'tkazma (Transfer)", callback_data="menu_transfer")],
            [InlineKeyboardButton("📈  Bugun", callback_data="report_today"),
             InlineKeyboardButton("📊  Hafta", callback_data="report_week"),
             InlineKeyboardButton("📆  Oy",    callback_data="report_month")],
            [InlineKeyboardButton("🌐  Til", callback_data="change_language")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("📊  Web Dashboard (New!)", web_app=WebAppInfo(url=web_app_url))],
            [InlineKeyboardButton("💸  Expense", callback_data="menu_expense"),
             InlineKeyboardButton("💰  Income",  callback_data="menu_income")],
            [InlineKeyboardButton("📅  Add for another date", callback_data="menu_another_date")],
            [InlineKeyboardButton("💳  My Cards", callback_data="menu_cards")],
            [InlineKeyboardButton("🔄  Transfer", callback_data="menu_transfer")],
            [InlineKeyboardButton("📈  Today", callback_data="report_today"),
             InlineKeyboardButton("📊  Week",  callback_data="report_week"),
             InlineKeyboardButton("📆  Month", callback_data="report_month")],
            [InlineKeyboardButton("🌐  Language", callback_data="change_language")],
        ]
    return InlineKeyboardMarkup(rows)


def kb_payment_methods(ctx, prefix: str):
    """To'lov usuli."""
    l         = lang(ctx)
    methods   = PAYMENT_METHODS[l]
    row = [InlineKeyboardButton(m, callback_data=f"{prefix}{m}") for m in methods]
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton(t("back", ctx), callback_data="menu_back_main")],
    ])


def kb_all_cards_for_transfer(ctx, exclude_card_id=None):
    """Faqat joriy foydalanuvchi kartalarini ko'rsatadi (o'tkazma uchun)."""
    user_name = ctx.user_data.get("user_name", "")
    buttons = []
    cards = get_cards(user_name)
    for card_id, name, number, balance in cards:
        if card_id == exclude_card_id:
            continue
        num_str = f" ({mask_number(number)})" if number else ""
        label   = f"🏦 {name}{num_str} — {format_amount(balance)}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"trf_card_{card_id}")])
    buttons.append([InlineKeyboardButton(t("back", ctx), callback_data="menu_back_main")])
    return InlineKeyboardMarkup(buttons)


def kb_expense_categories(ctx):
    """2 categories per row."""
    l = lang(ctx)
    cats = EXPENSE_CATEGORIES[l]
    buttons = []
    for i in range(0, len(cats), 2):
        row = [InlineKeyboardButton(cats[i], callback_data=f"exp_cat_{cats[i]}")]
        if i + 1 < len(cats):
            row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"exp_cat_{cats[i + 1]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(t("back", ctx), callback_data="back_to_expense_pay")])
    return InlineKeyboardMarkup(buttons)


def kb_income_categories(ctx):
    """2 categories per row for a compact grid layout."""
    cats = INCOME_CATEGORIES[lang(ctx)]
    buttons = []
    for i in range(0, len(cats), 2):
        row = [InlineKeyboardButton(cats[i], callback_data=f"inc_cat_{cats[i]}")]
        if i + 1 < len(cats):
            row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"inc_cat_{cats[i + 1]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(t("back", ctx), callback_data="back_to_income_pay")])
    return InlineKeyboardMarkup(buttons)


def kb_select_payment_card(user_name: str, ctx):
    buttons = []
    cards = get_cards(user_name)
    for card_id, name, number, balance in cards:
        num_str = f" ({mask_number(number)})" if number else ""
        label   = f"🏦 {name}{num_str} — {format_amount(balance)}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"paycard_{card_id}")])
    buttons.append([InlineKeyboardButton(t("back", ctx), callback_data="paycard_back")])
    return InlineKeyboardMarkup(buttons)


def kb_another_date_type(ctx):
    l = lang(ctx)
    if l == "uz":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➖ Harajat", callback_data="adate_expense"),
             InlineKeyboardButton("➕ Kirim",   callback_data="adate_income")],
            [InlineKeyboardButton(t("back", ctx), callback_data="adate_back")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➖ Expense", callback_data="adate_expense"),
             InlineKeyboardButton("➕ Income",  callback_data="adate_income")],
            [InlineKeyboardButton(t("back", ctx), callback_data="adate_back")],
        ])


def kb_cards_menu(user_name: str, ctx):
    l        = lang(ctx)
    avo_bal  = get_wallet_balance(AVO_USER, "avo")   # umumiy
    avo_lbl  = f"🏛 AVO (umumiy) — {format_amount(avo_bal)}"
    naqd_bal = get_wallet_balance(user_name, "naqd")
    naqd_lbl = f"💵 {'Naqd' if l == 'uz' else 'Cash'} — {format_amount(naqd_bal)}"
    buttons  = [
        [InlineKeyboardButton(avo_lbl,  callback_data="wallet_edit_avo"),
         InlineKeyboardButton(naqd_lbl, callback_data="wallet_edit_naqd")],
    ]
    for card_id, name, number, balance in get_cards(user_name):
        num_str = f" ({mask_number(number)})" if number else ""
        label   = f"🏦 {name}{num_str} — {format_amount(balance)}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"card_select_{card_id}")])
    buttons.append([InlineKeyboardButton(t("add_card", ctx), callback_data="card_add")])
    buttons.append([InlineKeyboardButton(t("back", ctx),     callback_data="menu_back_main")])
    return InlineKeyboardMarkup(buttons)


def kb_card_action(card_id: int, ctx):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("update_balance_btn", ctx), callback_data=f"card_upd_{card_id}")],
        [InlineKeyboardButton(t("delete_card_btn", ctx),    callback_data=f"card_del_{card_id}")],
        [InlineKeyboardButton(t("back", ctx),               callback_data="card_back_list")],
    ])


def kb_delete_confirm(card_id: int, ctx):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("yes_delete", ctx), callback_data=f"card_del_confirm_{card_id}"),
         InlineKeyboardButton(t("no_cancel",  ctx), callback_data="card_back_list")],
    ])


# ─── Handlers ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["lang"] = "uz"
    
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Kechirasiz, siz ushbu botdan foydalana olmaysiz.")
        return ConversationHandler.END

    user_name = get_user_name(user_id)
    if user_name:
        ctx.user_data["user_name"] = user_name
        await update.message.reply_text(
            f"👤 *{user_name}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Xush kelibsiz! 🎉\n\n"
            f"📋 Asosiy menyu",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    await update.message.reply_text("Xush kelibsiz! Iltimos, ismingizni kiriting (masalan: ORZU):")
    return REGISTER_NAME

async def msg_register_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    display_name = update.message.text.strip().upper()
    
    register_user(user_id, display_name)
    ctx.user_data["user_name"] = display_name
    
    await update.message.reply_text(
        f"✅ Muvaffaqiyatli ro'yxatdan o'tdingiz, *{display_name}*!\n\n"
        f"📋 Asosiy menyu",
        parse_mode="Markdown",
        reply_markup=kb_main(ctx),
    )
    return MAIN_MENU


# ── Main Menu ──
async def cb_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_expense":
        ctx.user_data["trans_type"] = "harajat"
        ctx.user_data.pop("custom_date", None)
        ctx.user_data.pop("custom_date_display", None)
        await query.edit_message_text(
            t("payment_method", ctx),
            reply_markup=kb_payment_methods(ctx, "exp_pay_"),
        )
        return EXPENSE_PAYMENT

    elif data == "menu_income":
        ctx.user_data["trans_type"] = "kirim"
        ctx.user_data.pop("custom_date", None)
        ctx.user_data.pop("custom_date_display", None)
        await query.edit_message_text(
            t("payment_method", ctx),
            reply_markup=kb_payment_methods(ctx, "inc_pay_"),
        )
        return INCOME_PAYMENT

    elif data == "menu_transfer":
        l    = lang(ctx)
        text = (
            "🔄 *O'tkazma*\n\n📤 _Qaysi kartadan_ o'tkazmoqchisiz?"
            if l == "uz" else
            "🔄 *Transfer*\n\n📤 _Which card_ to transfer FROM?"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=kb_all_cards_for_transfer(ctx),
        )
        return TRANSFER_FROM

    elif data == "menu_another_date":
        await query.edit_message_text(t("enter_date", ctx), parse_mode="Markdown")
        return ANOTHER_DATE_INPUT

    elif data == "menu_cards":
        user_name = ctx.user_data.get("user_name", "?")
        await query.edit_message_text(
            build_cards_text(user_name, ctx),
            parse_mode="Markdown",
            reply_markup=kb_cards_menu(user_name, ctx),
        )
        return CARD_MENU

    elif data.startswith("report_"):
        period = data.split("_")[1]
        period_labels = {
            "uz": {"today": "Bugun", "week": "Hafta", "month": "Oy"},
            "en": {"today": "Today", "week": "This week", "month": "This month"},
        }
        label     = period_labels[lang(ctx)][period]
        user_name = ctx.user_data.get("user_name", "?")
        text      = build_detailed_report(user_name, period, label, ctx)
        back_btn  = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("back", ctx), callback_data="menu_back_main")
        ]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_btn)
        return MAIN_MENU

    elif data == "menu_back_main":
        user_name  = ctx.user_data.get("user_name", "?")
        menu_label = t("main_menu", ctx)
        await query.edit_message_text(
            f"👤 *{user_name}*\n━━━━━━━━━━━━━━━━━━━━━━\n📋 {menu_label}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    elif data == "change_user":
        await query.edit_message_text(t("who_are_you", ctx), parse_mode="Markdown", reply_markup=kb_users())
        return USER_SELECT

    elif data == "change_language":
        await query.edit_message_text(
            "🌐 *Tilni tanlang / Choose language:*",
            parse_mode="Markdown",
            reply_markup=kb_lang(),
        )
        return LANG_SELECT

    return MAIN_MENU


# ── Another Date ──
async def msg_another_date_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    dt = parse_date(date_str)
    if not dt:
        await update.message.reply_text(t("invalid_date", ctx), parse_mode="Markdown")
        return ANOTHER_DATE_INPUT

    ctx.user_data["custom_date"]         = dt.strftime("%Y-%m-%d")
    ctx.user_data["custom_date_display"] = date_str

    choose_text = t("another_date_choose", ctx).format(date=date_str)
    await update.message.reply_text(
        choose_text,
        parse_mode="Markdown",
        reply_markup=kb_another_date_type(ctx),
    )
    return ANOTHER_DATE_TYPE


async def cb_another_date_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adate_back":
        # Go back to main menu, clear custom date
        ctx.user_data.pop("custom_date", None)
        ctx.user_data.pop("custom_date_display", None)
        user_name = ctx.user_data.get("user_name", "?")
        await query.edit_message_text(
            f"*{user_name}* — {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    elif data == "adate_expense":
        ctx.user_data["trans_type"] = "harajat"
        await query.edit_message_text(
            t("payment_method", ctx),
            reply_markup=kb_payment_methods(ctx, "exp_pay_"),
        )
        return EXPENSE_PAYMENT

    elif data == "adate_income":
        ctx.user_data["trans_type"] = "kirim"
        await query.edit_message_text(
            t("payment_method", ctx),
            reply_markup=kb_payment_methods(ctx, "inc_pay_"),
        )
        return INCOME_PAYMENT

    return ANOTHER_DATE_TYPE


# ── Expense Payment Method ──
async def cb_expense_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_back_main":
        user_name = ctx.user_data.get("user_name", "?")
        ctx.user_data.pop("custom_date", None)
        ctx.user_data.pop("custom_date_display", None)
        await query.edit_message_text(
            f"*{user_name}* — {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    payment = data[len("exp_pay_"):]
    ctx.user_data["payment_method"]  = payment
    ctx.user_data["expense_card_id"] = None

    date_str = ctx.user_data.get("custom_date_display", "")
    date_note = f"\n📅 _{date_str}_" if date_str else ""

    await query.edit_message_text(
        f"💳 *{payment}*{date_note}\n\n{t('expense_category', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_expense_categories(ctx),
    )
    return EXPENSE_CATEGORY


async def cb_expense_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_to_expense_pay":
        await query.edit_message_text(
            t("payment_method", ctx),
            reply_markup=kb_payment_methods(ctx, "exp_pay_"),
        )
        return EXPENSE_PAYMENT

    category = data[len("exp_cat_"):]
    ctx.user_data["category"] = category
    payment   = ctx.user_data.get("payment_method", "")
    date_str  = ctx.user_data.get("custom_date_display", "")
    date_note = f"\n📅 _{date_str}_" if date_str else ""

    # If Shaxsiy karta → select which card
    if payment in CARD_PAYMENT_METHODS:
        user_name = ctx.user_data.get("user_name", "?")
        all_cards = get_cards(user_name)
        if not all_cards:
            await query.edit_message_text(
                f"💳 *{payment}* › 📂 *{category}*{date_note}\n\n"
                f"{t('no_cards_for_payment', ctx)}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t("back", ctx), callback_data="paycard_back")
                ]]),
            )
            return SELECT_CARD_FOR_EXPENSE
        await query.edit_message_text(
            f"💳 *{payment}* › 📂 *{category}*{date_note}\n\n"
            f"{t('select_payment_card', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_select_payment_card(user_name, ctx),
        )
        return SELECT_CARD_FOR_EXPENSE

    # No card needed → ask amount
    await query.edit_message_text(
        f"💳 *{payment}* › 📂 *{category}*{date_note}\n\n{t('enter_amount', ctx)}",
        parse_mode="Markdown",
    )
    return ENTER_AMOUNT


# ── Income Payment Method ──
async def cb_income_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_back_main":
        user_name = ctx.user_data.get("user_name", "?")
        ctx.user_data.pop("custom_date", None)
        ctx.user_data.pop("custom_date_display", None)
        await query.edit_message_text(
            f"*{user_name}* — {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    payment = data[len("inc_pay_"):]
    ctx.user_data["payment_method"] = payment

    date_str  = ctx.user_data.get("custom_date_display", "")
    date_note = f"\n📅 _{date_str}_" if date_str else ""

    await query.edit_message_text(
        f"💰 *{payment}*{date_note}\n\n{t('income_category', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_income_categories(ctx),
    )
    return INCOME_CATEGORY


async def cb_income_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_to_income_pay":
        await query.edit_message_text(
            t("payment_method", ctx),
            reply_markup=kb_payment_methods(ctx, "inc_pay_"),
        )
        return INCOME_PAYMENT

    category  = data[len("inc_cat_"):]
    ctx.user_data["category"] = category
    payment   = ctx.user_data.get("payment_method", "")
    date_str  = ctx.user_data.get("custom_date_display", "")
    date_note = f"\n📅 _{date_str}_" if date_str else ""

    # Shaxsiy karta → qaysi kartaga tushdi?
    if payment in CARD_PAYMENT_METHODS:
        user_name = ctx.user_data.get("user_name", "?")
        all_cards = get_cards(user_name)
        if not all_cards:
            await query.edit_message_text(
                f"💰 *{payment}* › 📂 *{category}*{date_note}\n\n"
                f"{t('no_cards_for_payment', ctx)}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t("back", ctx), callback_data="paycard_back")
                ]]),
            )
            return SELECT_CARD_FOR_EXPENSE
        await query.edit_message_text(
            f"💰 *{payment}* › 📂 *{category}*{date_note}\n\n"
            f"{t('select_payment_card', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_select_payment_card(user_name, ctx),
        )
        return SELECT_CARD_FOR_EXPENSE

    await query.edit_message_text(
        f"💰 *{payment}* › 📂 *{category}*{date_note}\n\n{t('enter_amount', ctx)}",
        parse_mode="Markdown",
    )
    return ENTER_AMOUNT



# ── Select Card For Expense ──
async def cb_select_payment_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "paycard_back":
        await query.edit_message_text(
            t("expense_category", ctx),
            reply_markup=kb_expense_categories(ctx),
        )
        return EXPENSE_CATEGORY

    card_id  = int(data.split("paycard_")[1])
    ctx.user_data["expense_card_id"] = card_id
    card     = get_card_by_id(card_id)
    name     = card[1] if card else "?"
    balance  = card[3] if card else 0
    category = ctx.user_data.get("category", "?")
    payment  = ctx.user_data.get("payment_method", "")
    date_str = ctx.user_data.get("custom_date_display", "")
    date_note = f"\n📅 _{date_str}_" if date_str else ""

    await query.edit_message_text(
        f"💳 *{payment}* › 📂 *{category}*{date_note}\n"
        f"🏦 *{name}* — `{format_amount(balance)}`\n\n"
        f"{t('enter_amount', ctx)}",
        parse_mode="Markdown",
    )
    return ENTER_AMOUNT


# ── Amount / Comment ──
async def msg_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text(t("invalid_number", ctx))
        return ENTER_AMOUNT

    ctx.user_data["amount"] = amount
    await update.message.reply_text(t("enter_comment", ctx))
    return ENTER_COMMENT


async def msg_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if comment == "/skip":
        comment = ""
    _save_and_deduct(ctx, comment)
    return await _show_main_menu(update, ctx)


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _save_and_deduct(ctx, "")
    return await _show_main_menu(update, ctx)


def _save_and_deduct(ctx, comment):
    amount         = ctx.user_data.get("amount", 0)
    payment_method = ctx.user_data.get("payment_method", "")
    category       = ctx.user_data.get("category", "-")
    trans_type     = ctx.user_data.get("trans_type", "harajat")
    user_name      = ctx.user_data.get("user_name", "unknown")
    custom_date    = ctx.user_data.get("custom_date")
    created_at     = f"{custom_date}T12:00:00" if custom_date else None

    save_transaction(user_name, trans_type, payment_method, category, amount, comment, created_at)

    # ── Shaxsiy karta uchun balans ayirish
    card_id = ctx.user_data.get("expense_card_id")
    if card_id:
        card = get_card_by_id(card_id)
        if card:
            new_balance = card[3] + (amount if trans_type == "kirim" else -amount)
            update_card_balance(card_id, new_balance)
            ctx.user_data["_last_deducted_card_id"]      = card_id
            ctx.user_data["_last_deducted_card_new_bal"] = new_balance
        ctx.user_data["expense_card_id"] = None

    # ── AVO yoki Naqd hamyoni uchun balans o'zgartirish
    wallet_type = payment_to_wallet_type(payment_method)
    if wallet_type and not card_id:
        delta  = amount if trans_type == "kirim" else -amount
        w_user = AVO_USER if wallet_type == "avo" else user_name
        adjust_wallet(w_user, wallet_type, delta)

    # custom_date ni _show_main_menu uchun saqlaymiz (kanal yangilanishi kerak)
    # _show_main_menu chaqirilgandan keyin o'chiriladi


async def _show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_name  = ctx.user_data.get("user_name", "?")
    amount     = ctx.user_data.get("amount", 0)
    trans_type = ctx.user_data.get("trans_type", "harajat")
    payment    = ctx.user_data.get("payment_method", "")
    category   = ctx.user_data.get("category", "")
    saved_msg  = t("saved", ctx)
    card_note  = ""

    deducted_card_id  = ctx.user_data.pop("_last_deducted_card_id", None)
    deducted_card_bal = ctx.user_data.pop("_last_deducted_card_new_bal", None)
    if deducted_card_id is not None:
        card = get_card_by_id(deducted_card_id)
        if card:
            card_note = (
                f"\n└─ 🏦 *{card[1]}*  {t('card_deducted', ctx)} "
                f"`{format_amount(deducted_card_bal)}`"
            )

    type_emoji = "💸" if trans_type == "harajat" else "💰"
    menu_label = t("main_menu", ctx)
    text = (
        f"✅ *{format_amount(amount)}* {saved_msg}!\n"
        f"┌─ {type_emoji} {payment}"
        f"\n└─ 📂 {category}{card_note}"
        f"\n\n👤 *{user_name}* · 📋 {menu_label}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_main(ctx))
    # Kanal xabarini yangilash (fon, xato bo'lsa ham davom etadi)
    # custom_date hali ctx.user_data da bor (endi o'chiramiz)
    saved_custom_date = ctx.user_data.pop("custom_date", None)
    ctx.user_data.pop("custom_date_display", None)
    try:
        await update_channel_message(ctx.bot, target_date=saved_custom_date)
    except Exception as e:
        logger.warning(f"[CHANNEL] Update skipped: {e}")
    return MAIN_MENU


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    msg = ("❌ Bekor qilindi. /start ni bosing."
           if lang(ctx) == "uz" else "❌ Cancelled. Press /start.")
    await update.message.reply_text(msg)
    return ConversationHandler.END


# ── Transfer Handlers ──
async def cb_transfer_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qaysi kartadan — FROM kartani tanlash."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_back_main":
        user_name = ctx.user_data.get("user_name", "?")
        await query.edit_message_text(
            f"👫 *{user_name}* — {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    from_card_id = int(data.split("trf_card_")[1])
    from_card    = get_card_by_id(from_card_id)
    ctx.user_data["trf_from_id"] = from_card_id
    l = lang(ctx)
    text = (
        f"🔄 *O'tkazma*\n"
        f"📤 _Dan:_ 🏦 *{from_card[1]}*  `{format_amount(from_card[3])}`\n\n"
        f"📥 _Qaysi kartaga_ o'tkazmoqchisiz?"
        if l == "uz" else
        f"🔄 *Transfer*\n"
        f"📤 _From:_ 🏦 *{from_card[1]}*  `{format_amount(from_card[3])}`\n\n"
        f"📥 _Which card_ to transfer TO?"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=kb_all_cards_for_transfer(ctx, exclude_card_id=from_card_id),
    )
    return TRANSFER_TO


async def cb_transfer_to(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qaysi kartaga — TO kartani tanlash."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_back_main":
        # FROM ga qaytish
        l    = lang(ctx)
        text = (
            "🔄 *O'tkazma*\n\n📤 _Qaysi kartadan_ o'tkazmoqchisiz?"
            if l == "uz" else
            "🔄 *Transfer*\n\n📤 _Which card_ to transfer FROM?"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=kb_all_cards_for_transfer(ctx),
        )
        return TRANSFER_FROM

    to_card_id = int(data.split("trf_card_")[1])
    to_card    = get_card_by_id(to_card_id)
    ctx.user_data["trf_to_id"] = to_card_id

    from_card_id = ctx.user_data.get("trf_from_id")
    from_card    = get_card_by_id(from_card_id)
    l = lang(ctx)
    text = (
        f"🔄 *O'tkazma*\n"
        f"📤 _Dan:_ 🏦 *{from_card[1]}*  `{format_amount(from_card[3])}`\n"
        f"📥 _Ga:_  🏦 *{to_card[1]}*   `{format_amount(to_card[3])}`\n\n"
        f"💵 *Miqdorni kiriting* (so'm):"
        if l == "uz" else
        f"🔄 *Transfer*\n"
        f"📤 _From:_ 🏦 *{from_card[1]}*  `{format_amount(from_card[3])}`\n"
        f"📥 _To:_   🏦 *{to_card[1]}*   `{format_amount(to_card[3])}`\n\n"
        f"💵 *Enter amount* (UZS):"
    )
    await query.edit_message_text(text, parse_mode="Markdown")
    return TRANSFER_AMOUNT


async def msg_transfer_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """O'tkazma miqdorini kiritish va bajarilishi."""
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text(t("invalid_number", ctx))
        return TRANSFER_AMOUNT

    from_id   = ctx.user_data.pop("trf_from_id", None)
    to_id     = ctx.user_data.pop("trf_to_id",   None)
    from_card = get_card_by_id(from_id)
    to_card   = get_card_by_id(to_id)

    if from_card and to_card:
        new_from = from_card[3] - amount
        new_to   = to_card[3]  + amount
        update_card_balance(from_id, new_from)
        update_card_balance(to_id,   new_to)
        l = lang(ctx)
        msg = (
            f"✅ *O'tkazma amalga oshdi!*\n\n"
            f"📤 🏦 *{from_card[1]}*\n"
            f"   `{format_amount(from_card[3])}` ➜ `{format_amount(new_from)}`\n\n"
            f"📥 🏦 *{to_card[1]}*\n"
            f"   `{format_amount(to_card[3])}` ➜ `{format_amount(new_to)}`\n\n"
            f"💸 *Miqdor: `{format_amount(amount)}`*"
            if l == "uz" else
            f"✅ *Transfer completed!*\n\n"
            f"📤 🏦 *{from_card[1]}*\n"
            f"   `{format_amount(from_card[3])}` ➜ `{format_amount(new_from)}`\n\n"
            f"📥 🏦 *{to_card[1]}*\n"
            f"   `{format_amount(to_card[3])}` ➜ `{format_amount(new_to)}`\n\n"
            f"💸 *Amount: `{format_amount(amount)}`*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        # Kanal xabarini yangilash
        try:
            await update_channel_message(update.get_bot())
        except Exception:
            pass

    user_name = ctx.user_data.get("user_name", "?")
    await update.message.reply_text(
        f"👫 *{user_name}* — {t('main_menu', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_main(ctx),
    )
    return MAIN_MENU


# ── Card Menu ──
async def cb_card_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data      = query.data
    user_name = ctx.user_data.get("user_name", "?")

    if data == "wallet_info_naqd":
        # BIRGALIKDA uchun: tahrirlash mumkin emas, faqat ma'lumot
        combined = get_birgalikda_naqd()
        await query.answer(
            f"💵 Naqd (ORZU+SHIRIN): {format_amount(combined)}",
            show_alert=True
        )
        return CARD_MENU

    elif data.startswith("wallet_edit_"):
        wtype   = data.split("wallet_edit_")[1]   # 'avo' or 'naqd'
        w_user  = AVO_USER if wtype == "avo" else user_name
        cur_bal = get_wallet_balance(w_user, wtype)
        ctx.user_data["wallet_edit_type"] = wtype
        icon    = "🏛" if wtype == "avo" else "💵"
        name    = "AVO" if wtype == "avo" else ("Naqd" if lang(ctx) == "uz" else "Cash")
        prompt  = (
            f"{icon} *{name}*"
            f"\n💰 Joriy balans: `{format_amount(cur_bal)}`"
            f"\n\n📝 *Yangi balansni kiriting* (so'm):"
        ) if lang(ctx) == "uz" else (
            f"{icon} *{name}*"
            f"\nCurrent balance: `{format_amount(cur_bal)}`"
            f"\n\n📝 *Enter new balance* (UZS):"
        )
        await query.edit_message_text(prompt, parse_mode="Markdown")
        return WALLET_EDIT

    elif data == "card_add":
        await query.edit_message_text(t("card_add_name", ctx), parse_mode="Markdown")
        return CARD_ADD_NAME

    elif data.startswith("card_select_"):
        card_id = int(data.split("card_select_")[1])
        ctx.user_data["selected_card_id"] = card_id
        card = get_card_by_id(card_id)
        if not card:
            await query.answer(
                "Karta topilmadi!" if lang(ctx) == "uz" else "Card not found!"
            )
            return CARD_MENU
        _, name, number, balance = card
        num_str = f"\n🔢 `{mask_number(number)}`" if number else ""
        text = (
            f"🏦 *{name}*{num_str}\n"
            f"💰 *Balans:* `{format_amount(balance)}`\n\n"
            f"{t('card_action', ctx)}"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb_card_action(card_id, ctx)
        )
        return CARD_ACTION

    elif data == "menu_back_main":
        await query.edit_message_text(
            f"*{user_name}* — {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    return CARD_MENU


async def msg_card_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    ctx.user_data["new_card_name"] = name
    await update.message.reply_text(t("card_add_number", ctx), parse_mode="Markdown")
    return CARD_ADD_NUMBER


async def msg_card_add_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["new_card_number"] = text
    await update.message.reply_text(t("card_add_balance", ctx))
    return CARD_ADD_BALANCE


async def cmd_skip_card_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_card_number"] = ""
    await update.message.reply_text(t("card_add_balance", ctx))
    return CARD_ADD_BALANCE


async def msg_card_add_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        balance = float(text)
    except ValueError:
        await update.message.reply_text(t("invalid_number", ctx))
        return CARD_ADD_BALANCE

    user_name = ctx.user_data.get("user_name", "unknown")
    card_name = ctx.user_data.get("new_card_name", "Karta")
    card_num  = ctx.user_data.get("new_card_number", "")
    add_card(user_name, card_name, card_num, balance)

    await update.message.reply_text(
        f"✅ *{card_name}* {t('card_added', ctx)}\n💰 `{format_amount(balance)}`",
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        build_cards_text(user_name, ctx),
        parse_mode="Markdown",
        reply_markup=kb_cards_menu(user_name, ctx),
    )
    return CARD_MENU


async def cb_card_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data      = query.data
    user_name = ctx.user_data.get("user_name", "?")

    if data.startswith("card_upd_"):
        card_id = int(data.split("card_upd_")[1])
        ctx.user_data["selected_card_id"] = card_id
        card = get_card_by_id(card_id)
        name = card[1] if card else "?"
        await query.edit_message_text(
            f"🏦 *{name}*\n\n{t('card_update_balance', ctx)}\n"
            f"_(Hozirgi: `{format_amount(card[3] if card else 0)}`)_",
            parse_mode="Markdown",
        )
        return CARD_UPDATE_BALANCE

    elif data.startswith("card_del_") and "confirm" not in data:
        card_id = int(data.split("card_del_")[1])
        ctx.user_data["selected_card_id"] = card_id
        card = get_card_by_id(card_id)
        name = card[1] if card else "?"
        await query.edit_message_text(
            f"🗑 *{name}*\n\n{t('card_delete_confirm', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_delete_confirm(card_id, ctx),
        )
        return CARD_DELETE_CONFIRM

    elif data == "card_back_list":
        await query.edit_message_text(
            build_cards_text(user_name, ctx),
            parse_mode="Markdown",
            reply_markup=kb_cards_menu(user_name, ctx),
        )
        return CARD_MENU

    return CARD_ACTION


async def msg_card_update_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        new_balance = float(text)
    except ValueError:
        await update.message.reply_text(t("invalid_number", ctx))
        return CARD_UPDATE_BALANCE

    card_id = ctx.user_data.get("selected_card_id")
    if card_id:
        update_card_balance(card_id, new_balance)

    user_name = ctx.user_data.get("user_name", "?")
    await update.message.reply_text(
        f"✅ {t('card_updated', ctx)}\n💰 `{format_amount(new_balance)}`",
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        build_cards_text(user_name, ctx),
        parse_mode="Markdown",
        reply_markup=kb_cards_menu(user_name, ctx),
    )
    return CARD_MENU


async def msg_wallet_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """AVO yoki Naqd uchun yangi balans kiritish."""
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        new_balance = float(text)
    except ValueError:
        await update.message.reply_text(t("invalid_number", ctx))
        return WALLET_EDIT

    user_name = ctx.user_data.get("user_name", "?")
    wtype     = ctx.user_data.pop("wallet_edit_type", "naqd")
    w_user    = AVO_USER if wtype == "avo" else user_name
    set_wallet_balance(w_user, wtype, new_balance)

    icon = "🏛" if wtype == "avo" else "💵"
    name = "AVO" if wtype == "avo" else ("Naqd" if lang(ctx) == "uz" else "Cash")
    await update.message.reply_text(
        f"✅ {icon} *{name}* yangilandi!\n💰 `{format_amount(new_balance)}`",
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        build_cards_text(user_name, ctx),
        parse_mode="Markdown",
        reply_markup=kb_cards_menu(user_name, ctx),
    )
    return CARD_MENU


async def cb_card_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data      = query.data
    user_name = ctx.user_data.get("user_name", "?")

    if data.startswith("card_del_confirm_"):
        card_id = int(data.split("card_del_confirm_")[1])
        delete_card(card_id)
        await query.edit_message_text(
            f"🗑 {t('card_deleted', ctx)}\n\n" + build_cards_text(user_name, ctx),
            parse_mode="Markdown",
            reply_markup=kb_cards_menu(user_name, ctx),
        )
        return CARD_MENU

    elif data == "card_back_list":
        await query.edit_message_text(
            build_cards_text(user_name, ctx),
            parse_mode="Markdown",
            reply_markup=kb_cards_menu(user_name, ctx),
        )
        return CARD_MENU

    return CARD_DELETE_CONFIRM


# ─── Main ────────────────────────────────────────────────────────────────────
async def _post_init(application):
    """Bot ishga tushganda har bir foydalanuvchiga welcome xabar yuboradi."""
    for user_id in ALLOWED_USER_IDS:
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    "🏦 *Finance Bot*\n\n"
                    "Assalomu alaykum! / Hello!\n"
                    "────────────────────\n"
                    "Bot qayta ishga tushdi ✅\n"
                    "Davom etish uchun /start bosing"
                ),
                parse_mode="Markdown",
            )
            logger.info(f"[STARTUP] Welcome yuborildi: {user_id}")
        except Exception as e:
            logger.warning(f"[STARTUP] {user_id} ga yuborib bo'lmadi: {e}")


def main():
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start, filters=filters.User(list(ALLOWED_USER_IDS)))],
        states={
            REGISTER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_register_name),
            ],
            MAIN_MENU: [
                CallbackQueryHandler(
                    cb_main_menu,
                    pattern="^(menu_|report_|change_)",
                ),
            ],
            ANOTHER_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_another_date_input),
            ],
            ANOTHER_DATE_TYPE: [
                CallbackQueryHandler(
                    cb_another_date_type,
                    pattern="^(adate_)",
                ),
            ],
            EXPENSE_PAYMENT: [
                CallbackQueryHandler(
                    cb_expense_payment,
                    pattern="^(exp_pay_|menu_back_main$)",
                ),
            ],
            EXPENSE_CATEGORY: [
                CallbackQueryHandler(
                    cb_expense_category,
                    pattern="^(exp_cat_|back_to_expense_pay$)",
                ),
            ],
            INCOME_PAYMENT: [
                CallbackQueryHandler(
                    cb_income_payment,
                    pattern="^(inc_pay_|menu_back_main$)",
                ),
            ],
            INCOME_CATEGORY: [
                CallbackQueryHandler(
                    cb_income_category,
                    pattern="^(inc_cat_|back_to_income_pay$)",
                ),
            ],
            SELECT_CARD_FOR_EXPENSE: [
                CallbackQueryHandler(cb_select_payment_card, pattern="^paycard_"),
            ],
            ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_amount),
            ],
            ENTER_COMMENT: [
                CommandHandler("skip", cmd_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_comment),
            ],
            # Card states
            CARD_MENU: [
                CallbackQueryHandler(
                    cb_card_menu,
                    pattern="^(card_add$|card_select_|wallet_edit_|wallet_info_naqd$|menu_back_main$)",
                ),
            ],
            CARD_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_add_name),
            ],
            CARD_ADD_NUMBER: [
                CommandHandler("skip", cmd_skip_card_number),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_add_number),
            ],
            CARD_ADD_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_add_balance),
            ],
            CARD_ACTION: [
                CallbackQueryHandler(
                    cb_card_action,
                    pattern="^(card_upd_|card_del_|card_back_list$)",
                ),
            ],
            CARD_UPDATE_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_update_balance),
            ],
            WALLET_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_wallet_edit),
            ],
            CARD_DELETE_CONFIRM: [
                CallbackQueryHandler(
                    cb_card_delete_confirm,
                    pattern="^(card_del_confirm_|card_back_list$)",
                ),
            ],
            # Transfer states (faqat BIRGALIKDA uchun)
            TRANSFER_FROM: [
                CallbackQueryHandler(
                    cb_transfer_from,
                    pattern="^(trf_card_|menu_back_main$)",
                ),
            ],
            TRANSFER_TO: [
                CallbackQueryHandler(
                    cb_transfer_to,
                    pattern="^(trf_card_|menu_back_main$)",
                ),
            ],
            TRANSFER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_transfer_amount),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    app.add_handler(conv)

    # Ruxsatsiz foydalanuvchilar uchun yopuvchi handler
    async def _unauthorized(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id not in ALLOWED_USER_IDS:
            if update.callback_query:
                await update.callback_query.answer("🚫 Kirish taqiqlangan!", show_alert=True)
            elif update.message:
                await update.message.reply_text("🚫 Sizda bu botdan foydalanish huquqi yo'q.")

    app.add_handler(
        MessageHandler(~filters.User(list(ALLOWED_USER_IDS)), _unauthorized)
    )
    app.add_handler(
        CallbackQueryHandler(_unauthorized,
                             pattern=None if True else "")  # hamma callbacklar
    )

    # ─── Rejalashtirilgan kanal xabarlari ───────────────────────────────────
    jq = app.job_queue
    # 00:04 Toshkent (UTC+5) = 19:04 UTC (oldingi kun)
    jq.run_daily(
        send_morning_message,
        time=dt_time(hour=19, minute=4, second=0, tzinfo=timezone.utc),
        name="morning_message",
    )
    # 23:55 Toshkent (UTC+5) = 18:55 UTC
    jq.run_daily(
        send_evening_summary,
        time=dt_time(hour=18, minute=55, second=0, tzinfo=timezone.utc),
        name="evening_summary",
    )
    logger.info("Finance Bot ishga tushdi ✅ | Kanal xabarlari rejalashtirildi")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
