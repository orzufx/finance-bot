import calendar
import csv
import io
import logging
import os
import re
import sqlite3
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, time as dt_time, timezone
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

from config import BOT_TOKEN, DATABASE_URL, ALLOWED_USER_IDS, ADMIN_CHAT_ID

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

# ─── Bot sozlamalari ─────────────────────────────────────────────────────────
# BOT_TOKEN, DATABASE_URL, ALLOWED_USER_IDS — config.py dan (env / .env)
CHANNEL_ID = -1003863923798
UTC5       = pytz.timezone("Asia/Tashkent")   # Toshkent vaqti

# ─── Conversation States ─────────────────────────────────────────────────────
(
    REGISTER_NAME,
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
    # Takrorlanuvchi to'lovlar (eslatmalar)
    REC_MENU,               # Eslatmalar ro'yxati
    REC_ADD_TITLE,          # Nomi (Kredit, Internet, ...)
    REC_ADD_AMOUNT,         # Summasi
    REC_ADD_DAY,            # Oyning qaysi kuni
    REC_ADD_PAYMENT,        # To'lov usuli (AVO/Naqd/karta)
    REC_ADD_CATEGORY,       # Harajat kategoriyasi
) = range(28)


# ─── Database Abstraction ────────────────────────────────────────────────────

_pg_pool = None
def get_pg_pool():
    global _pg_pool
    if _pg_pool is None and DATABASE_URL:
        from psycopg2.pool import ThreadedConnectionPool
        # bot.py + server.py birgalikda Heroku'ning 20 talik ulanish
        # cheklovidan oshmasligi uchun har biriga maksimum 5 ta.
        # Timeout/keepalive'larsiz o'lik ulanishdagi so'rov event loop'ni
        # 15-30 daqiqa muzlatib qo'yishi mumkin (TCP retransmission).
        _pg_pool = ThreadedConnectionPool(
            1, 5, DATABASE_URL,
            sslmode='require',
            connect_timeout=5,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            options='-c statement_timeout=15000',
        )
    return _pg_pool

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
        self._returned = False
    def cursor(self):
        return PostgresCursorWrapper(self._conn.cursor())
    def commit(self): self._conn.commit()
    def close(self):
        # Idempotent: ikki marta chaqirilsa ham pool buzilmaydi
        if self._returned:
            return
        self._returned = True
        try:
            get_pg_pool().putconn(self._conn)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
    def execute(self, query, params=()):
        c = self.cursor()
        c.execute(query, params)
        return c

def get_conn():
    if DATABASE_URL:
        pool = get_pg_pool()
        conn = pool.getconn()
        # Pool'dan olingan ulanish o'lik bo'lishi mumkin (PG failover,
        # tungi uzilish) — arzon ping bilan tekshirib, yangisiga almashtiramiz
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.rollback()
        except Exception:
            logger.warning("O'lik PG ulanish topildi — yangisiga almashtirildi")
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = pool.getconn()
        return PostgresConnWrapper(conn)
    else:
        return sqlite3.connect("finance.db")


@contextmanager
def db_conn():
    """Ulanishni xato bo'lsa ham albatta pool'ga qaytaradi (leak'ka qarshi)."""
    _c = get_conn()
    try:
        yield _c
    finally:
        try:
            _c.close()
        except Exception:
            logger.warning("DB ulanishni qaytarib bo'lmadi", exc_info=True)

def init_db():
    with db_conn() as conn:
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
                    created_at     TEXT    NOT NULL,
                    card_id        INTEGER
                )
            """)
            c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS card_id INTEGER")
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS recurring_payments (
                    id             SERIAL PRIMARY KEY,
                    user_name      TEXT    NOT NULL,
                    title          TEXT    NOT NULL,
                    amount         REAL    NOT NULL,
                    day            INTEGER NOT NULL,
                    payment_method TEXT    NOT NULL DEFAULT '💵 Naqd',
                    category       TEXT    NOT NULL DEFAULT '📦 Boshqa',
                    card_id        INTEGER,
                    last_paid_ym   TEXT    NOT NULL DEFAULT '',
                    created_at     TEXT    NOT NULL
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
                    created_at     TEXT    NOT NULL,
                    card_id        INTEGER
                )
            """)
            try:
                c.execute("ALTER TABLE transactions ADD COLUMN payment_method TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                c.execute("ALTER TABLE transactions ADD COLUMN card_id INTEGER")
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS recurring_payments (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name      TEXT    NOT NULL,
                    title          TEXT    NOT NULL,
                    amount         REAL    NOT NULL,
                    day            INTEGER NOT NULL,
                    payment_method TEXT    NOT NULL DEFAULT '💵 Naqd',
                    category       TEXT    NOT NULL DEFAULT '📦 Boshqa',
                    card_id        INTEGER,
                    last_paid_ym   TEXT    NOT NULL DEFAULT '',
                    created_at     TEXT    NOT NULL
                )
            """)

        conn.commit()



# ── Transactions ──
def save_transaction(user_name, trans_type, payment_method, category,
                     amount, comment="", created_at=None, card_id=None):
    """Yozuvni saqlab, yangi tranzaksiya id sini qaytaradi (bekor qilish uchun)."""
    with db_conn() as conn:
        c = conn.cursor()
        if created_at is None:
            created_at = datetime.now(UTC5).isoformat()
        q = (
            "INSERT INTO transactions "
            "(user_name, type, payment_method, category, amount, comment, created_at, card_id) "
            "VALUES (?,?,?,?,?,?,?,?)"
        )
        params = (user_name, trans_type, payment_method, category, amount, comment, created_at, card_id)
        if DATABASE_URL:
            txn_id = c.execute(q + " RETURNING id", params).fetchone()[0]
        else:
            c.execute(q, params)
            txn_id = c.lastrowid
        conn.commit()
    return txn_id


def delete_transaction_and_refund(txn_id: int, user_name: str):
    """Tranzaksiyani o'chirib, karta/hamyon balansini qaytaradi.

    Muvaffaqiyatda o'chirilgan yozuvni, topilmasa (yoki begona bo'lsa) None qaytaradi.
    """
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT id, user_name, type, payment_method, category, amount, card_id, created_at "
            "FROM transactions WHERE id=?",
            (txn_id,),
        ).fetchone()
        if not row or row[1] != user_name:
            return None

        _, _, trans_type, payment_method, _, amount, card_id, _ = row
        # Saqlashda qo'llangan o'zgarishning teskarisi
        delta = amount if trans_type == "kirim" else -amount
        if card_id:
            c.execute("UPDATE cards SET balance = balance - ? WHERE id=?", (delta, card_id))
        else:
            wtype = payment_to_wallet_type(payment_method)
            if wtype:
                w_user = AVO_USER if wtype == "avo" else user_name
                c.execute(
                    "UPDATE wallets SET balance = balance - ? WHERE user_name=? AND wallet_type=?",
                    (delta, w_user, wtype),
                )
        c.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        conn.commit()
    return row


def get_summary(user_name: str, period: str) -> dict:
    with db_conn() as conn:
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
    result = {"harajat": 0, "kirim": 0}
    for tp, s in rows:
        result[tp] = s or 0
    return result


def get_transactions_for_period(user_name: str, period: str) -> list:
    """Returns list of (type, payment_method, category, amount, comment, created_at)."""
    with db_conn() as conn:
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
    return rows


# ── Cards ──
def get_cards(user_name: str) -> list:
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id, card_name, card_number, balance FROM cards "
            "WHERE user_name=? ORDER BY id",
            (user_name,),
        ).fetchall()
    return rows  # [(id, name, number, balance), ...]


def add_card(user_name: str, card_name: str, card_number: str, balance: float):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO cards (user_name, card_name, card_number, balance, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_name, card_name, card_number, balance, datetime.now(UTC5).isoformat()),
        )
        conn.commit()


def update_card_balance(card_id: int, new_balance: float):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE cards SET balance=? WHERE id=?", (new_balance, card_id))
        conn.commit()


def delete_card(card_id: int):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM cards WHERE id=?", (card_id,))
        conn.commit()


def get_card_by_id(card_id: int):
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT id, card_name, card_number, balance FROM cards WHERE id=?", (card_id,)
        ).fetchone()
    return row


def adjust_card_balance(card_id: int, delta: float):
    """Karta balansini atomik o'zgartiradi (delta < 0 — ayirish)."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE cards SET balance = balance + ? WHERE id=?", (delta, card_id))
        conn.commit()


# ─── Recurring Payments (Eslatmalar) ─────────────────────────────────────────
def add_recurring(user_name, title, amount, day, payment_method, category, card_id=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO recurring_payments "
            "(user_name, title, amount, day, payment_method, category, card_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_name, title, amount, day, payment_method, category, card_id,
             datetime.now(UTC5).isoformat()),
        )
        conn.commit()


def get_recurring(user_name: str) -> list:
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id, user_name, title, amount, day, payment_method, category, card_id, last_paid_ym "
            "FROM recurring_payments WHERE user_name=? ORDER BY day, id",
            (user_name,),
        ).fetchall()
    return rows


def get_recurring_by_id(rec_id: int):
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT id, user_name, title, amount, day, payment_method, category, card_id, last_paid_ym "
            "FROM recurring_payments WHERE id=?",
            (rec_id,),
        ).fetchone()
    return row


def delete_recurring(rec_id: int):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM recurring_payments WHERE id=?", (rec_id,))
        conn.commit()


def set_recurring_paid(rec_id: int, ym: str):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE recurring_payments SET last_paid_ym=? WHERE id=?", (ym, rec_id))
        conn.commit()


def get_recurring_due(today_day: int, last_day: int, ym: str) -> list:
    """Bugun eslatilishi kerak bo'lganlar: kuni to'g'ri kelgan yoki
    (oy qisqa bo'lsa) oyning oxirgi kunida day > last_day bo'lganlar."""
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id, user_name, title, amount, day, payment_method, category, card_id, last_paid_ym "
            "FROM recurring_payments "
            "WHERE last_paid_ym <> ? AND (day = ? OR (? = ? AND day > ?))",
            (ym, today_day, today_day, last_day, last_day),
        ).fetchall()
    return rows


def record_recurring_payment(rec, ym: str) -> int:
    """Eslatma to'lovini harajat sifatida yozadi, balansni kamaytiradi.
    Yangi tranzaksiya id sini qaytaradi."""
    rec_id, user_name, title, amount, _, payment_method, category, card_id, _ = rec
    txn_id = save_transaction(user_name, "harajat", payment_method, category,
                              amount, title, None, card_id)
    if card_id:
        adjust_card_balance(card_id, -amount)
    else:
        wtype = payment_to_wallet_type(payment_method)
        if wtype:
            w_user = AVO_USER if wtype == "avo" else user_name
            adjust_wallet(w_user, wtype, -amount)
    set_recurring_paid(rec_id, ym)
    return txn_id


# ─── Wallet (AVO / Naqd) Helpers ─────────────────────────────────────────────
def get_wallet_balance(user_name: str, wallet_type: str) -> float:
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT balance FROM wallets WHERE user_name=? AND wallet_type=?",
            (user_name, wallet_type),
        ).fetchone()
    return row[0] if row else 0.0


def set_wallet_balance(user_name: str, wallet_type: str, balance: float):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (?,?,?) "
            "ON CONFLICT(user_name, wallet_type) DO UPDATE SET balance=excluded.balance",
            (user_name, wallet_type, balance),
        )
        conn.commit()


def adjust_wallet(user_name: str, wallet_type: str, delta: float):
    """delta < 0 harajat, delta > 0 kirim uchun. Atomik UPDATE."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE wallets SET balance = balance + ? WHERE user_name=? AND wallet_type=?",
            (delta, user_name, wallet_type),
        )
        conn.commit()


def payment_to_wallet_type(payment_method: str):
    """To'lov usuli stringidan wallet type qaytaradi yoki None."""
    pm = payment_method.lower()
    if "avo" in pm:
        return "avo"
    if "naqd" in pm or "cash" in pm:
        return "naqd"
    return None




def save_setting(key: str, value: str):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
        conn.commit()


def get_setting(key: str):
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def register_user(telegram_id: int, display_name: str):
    """Settings, users va naqd hamyonini bitta ulanish/tranzaksiyada yozadi."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                  (f"user_{telegram_id}", display_name))
        now_str = datetime.now(UTC5).isoformat()
        if DATABASE_URL:
            c.execute(
                "INSERT INTO users (telegram_id, display_name, created_at) VALUES (%s,%s,%s) "
                "ON CONFLICT (telegram_id) DO UPDATE SET display_name = EXCLUDED.display_name",
                (telegram_id, display_name, now_str),
            )
            c.execute(
                "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (%s,%s,0) "
                "ON CONFLICT (user_name, wallet_type) DO NOTHING",
                (display_name, "naqd"),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, display_name, created_at) VALUES (?,?,?)",
                (telegram_id, display_name, now_str),
            )
            c.execute(
                "INSERT OR IGNORE INTO wallets (user_name, wallet_type, balance) VALUES (?,?,0)",
                (display_name, "naqd"),
            )
        conn.commit()


def get_user_name(telegram_id: int) -> str:
    """Returns display_name or None if not registered."""
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute("SELECT display_name FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    return row[0] if row else None


def get_user_telegram_id(display_name: str):
    """display_name bo'yicha telegram_id (eslatma yuborish uchun)."""
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT telegram_id FROM users WHERE display_name=?", (display_name,)
        ).fetchone()
    return row[0] if row else None


def get_all_user_names() -> list:
    """Returns list of all registered user display names."""
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT display_name FROM users ORDER BY created_at").fetchall()
    return [r[0] for r in rows]


# ─── Channel Data Helpers ─────────────────────────────────────────────────────
def get_all_cards_summary() -> dict:
    """Returns {user_name: [(card_name, card_number, balance), ...]}."""
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT user_name, card_name, card_number, balance FROM cards ORDER BY user_name, id"
        ).fetchall()
    result: dict = {}
    for user, name, number, balance in rows:
        result.setdefault(user, []).append((name, number, balance))
    return result


def get_month_totals(ym: str) -> dict:
    """{user: {'kirim': x, 'harajat': y}} — YYYY-MM oyi bo'yicha."""
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT user_name, type, SUM(amount) FROM transactions "
            "WHERE created_at LIKE ? GROUP BY user_name, type",
            (f"{ym}%",),
        ).fetchall()
    result: dict = {}
    for user, ttype, total in rows:
        result.setdefault(user, {"kirim": 0, "harajat": 0})[ttype] = total or 0
    return result


def get_month_categories(ym: str) -> list:
    """[(category, total)] — harajatlar, kamayish tartibida."""
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT category, SUM(amount) FROM transactions "
            "WHERE type='harajat' AND created_at LIKE ? "
            "GROUP BY category ORDER BY SUM(amount) DESC",
            (f"{ym}%",),
        ).fetchall()
    return rows


def get_month_top_expenses(ym: str, limit: int = 5) -> list:
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT user_name, category, amount, comment FROM transactions "
            "WHERE type='harajat' AND created_at LIKE ? "
            "ORDER BY amount DESC LIMIT ?",
            (f"{ym}%", limit),
        ).fetchall()
    return rows


def get_transactions_for_month(user_name: str, ym: str) -> list:
    """CSV eksport uchun: bitta user, YYYY-MM oyi."""
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT created_at, type, payment_method, category, amount, comment "
            "FROM transactions WHERE user_name=? AND created_at LIKE ? ORDER BY created_at",
            (user_name, f"{ym}%"),
        ).fetchall()
    return rows


def get_today_transactions_all(target_date: str = None) -> dict:
    """Returns {user_name: [(type, payment, category, amount, comment), ...]}."""
    with db_conn() as conn:
        c = conn.cursor()
        date_key = target_date if target_date else datetime.now(UTC5).strftime("%Y-%m-%d")
        rows = c.execute(
            "SELECT user_name, type, payment_method, category, amount, comment "
            "FROM transactions WHERE created_at LIKE ? ORDER BY user_name, created_at",
            (f"{date_key}%",),
        ).fetchall()
    result: dict = {}
    for user, typ, pay, cat, amt, cmt in rows:
        result.setdefault(user, []).append((typ, pay, cat, amt, cmt or ""))
    return result


# ─── Channel Message Builder ──────────────────────────────────────────────────
AVO_USER = "SHARED"   # AVO barcha userlar uchun umumiy


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
                cmt_str = f"\n   └ _{escape_markdown(cmt)}_" if cmt else ""
                lines.append(f"*{i}.* `{format_amount(amt)}` — {cat}  ·  _[{pay}]_{cmt_str}")
                user_exp      += amt
                grand_expense += amt
            lines.append(f"🔴 *Jami harajat: `{format_amount(user_exp)}`*")
        if incomes:
            user_inc = 0
            lines.append("📥 _Kirimlar:_")
            for i, (pay, cat, amt, cmt) in enumerate(incomes, 1):
                cmt_str = f"\n   └ _{escape_markdown(cmt)}_" if cmt else ""
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


UZ_MONTHS = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
             "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"]


def build_monthly_report_text(ym: str, prev_ym: str) -> str:
    """Oy yakuni hisoboti: user kesimi, kategoriya (o'tgan oyga nisbatan), TOP-5."""
    y, m = ym.split("-")
    title = f"{UZ_MONTHS[int(m) - 1]} {y}"
    lines = [f"📊 *{title} — Oylik hisobot*\n"]

    totals = get_month_totals(ym)
    if not totals:
        lines.append("_Bu oyda hech qanday yozuv yo'q_")
        return "\n".join(lines)

    grand_inc = grand_exp = 0
    for user in get_all_user_names():
        d = totals.get(user)
        if not d:
            continue
        inc, exp = d.get("kirim", 0), d.get("harajat", 0)
        grand_inc += inc
        grand_exp += exp
        lines.append(f"👤 *{user}*")
        lines.append(f"  ├ 💚 Kirim:    `{format_amount(inc)}`")
        lines.append(f"  └ 🔴 Harajat: `{format_amount(exp)}`")
    balance = grand_inc - grand_exp
    sign    = "+" if balance >= 0 else ""
    lines.append("")
    lines.append(f"💚 *Jami kirim:*    `{format_amount(grand_inc)}`")
    lines.append(f"🔴 *Jami harajat:* `{format_amount(grand_exp)}`")
    lines.append(f"{'💚' if balance >= 0 else '🔴'} *Balans:* `{sign}{format_amount(balance)}`")
    lines.append("━" * 22)

    cats = get_month_categories(ym)
    if cats:
        prev = dict(get_month_categories(prev_ym))
        lines.append("📂 *Kategoriyalar (o'tgan oyga nisbatan):*\n")
        for cat, total in cats:
            p = prev.get(cat)
            if p:
                pct = (total - p) / p * 100
                cmp = f"📈 +{pct:.0f}%" if pct > 0.5 else (f"📉 {pct:.0f}%" if pct < -0.5 else "→ 0%")
            else:
                cmp = "🆕"
            lines.append(f"{cat}  `{format_amount(total)}`  _{cmp}_")
        lines.append("")

    top = get_month_top_expenses(ym)
    if top:
        lines.append("🏆 *TOP-5 eng katta harajat:*\n")
        for i, (user, cat, amount, cmt) in enumerate(top, 1):
            cmt_str = f" · _{escape_markdown(cmt)}_" if cmt else ""
            lines.append(f"*{i}.* `{format_amount(amount)}` — {cat} ({user}){cmt_str}")

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


async def send_recurring_reminders(context):
    """Har kuni 09:00 (Toshkent) — bugungi kunga tegishli eslatmalarni yuboradi."""
    now      = datetime.now(UTC5)
    ym       = now.strftime("%Y-%m")
    last_day = calendar.monthrange(now.year, now.month)[1]

    for rec in get_recurring_due(now.day, last_day, ym):
        rec_id, user_name, title, amount, _day, pm, cat, _cid, _ym = rec
        tg_id = get_user_telegram_id(user_name)
        if not tg_id:
            continue
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ To'landi — yozish", callback_data=f"rec_pay_{rec_id}")],
            [InlineKeyboardButton("⏭ Bu oy emas",        callback_data=f"rec_skip_{rec_id}")],
        ])
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"⏰ *Eslatma: {title}*\n\n"
                    f"💵 `{format_amount(amount)}`\n"
                    f"┌─ 💳 {pm}"
                    f"\n└─ 📂 {cat}\n\n"
                    f"Bugun to'lash kuni. To'lagan bo'lsangiz, tugma bilan yozib qo'ying:"
                ),
                parse_mode="Markdown",
                reply_markup=kb,
            )
            logger.info(f"[RECURRING] Eslatma yuborildi: {user_name} — {title}")
        except Exception as e:
            logger.warning(f"[RECURRING] {user_name} ({tg_id}) ga yuborilmadi: {e}")


async def send_monthly_report(context):
    """Har oyning 1-kuni 00:15 (Toshkent) — o'tgan oy hisobotini kanalga yuboradi.
    run_daily bilan chaqiriladi, kun tekshiruvi shu yerda."""
    now = datetime.now(UTC5)
    if now.day != 1:
        return
    last_month_end = now.replace(day=1) - timedelta(days=1)
    ym      = last_month_end.strftime("%Y-%m")
    prev_ym = (last_month_end.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    try:
        text = build_monthly_report_text(ym, prev_ym)
        await context.bot.send_message(
            chat_id=CHANNEL_ID, text=text, parse_mode="Markdown"
        )
        logger.info(f"[CHANNEL] Monthly report sent for {ym}")
    except Exception as e:
        logger.error(f"[CHANNEL] Monthly report failed: {e}")


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
        "undo_btn":             "🗑 Oxirgi yozuvni bekor qilish",
        "undone":               "bekor qilindi, balans qaytarildi",
        "undo_gone":            "Bu yozuv allaqachon bekor qilingan",
        # Recurring payments
        "recurring_menu":       "⏰ Eslatmalar",
        "rec_none":             "📭 Hali eslatma yo'q\n\n_Kredit, kommunal, obuna kabi oylik to'lovlarni qo'shing — bot o'z kunida eslatadi va bir bosishda yozib qo'yadi_",
        "rec_add_btn":          "➕ Yangi eslatma",
        "rec_title_prompt":     "✏️ *To'lov nomini kiriting:*\n_Masalan: Kredit, Internet, Obuna_",
        "rec_amount_prompt":    "💵 *Oylik summani kiriting* (so'm):",
        "rec_day_prompt":       "📅 *Oyning qaysi kuni eslatilsin?* (1–31)\n_31 kiritsangiz — qisqa oylarda oxirgi kunida eslatadi_",
        "rec_pm_prompt":        "💳 *Qaysi manbadan to'lanadi?*",
        "rec_cat_prompt":       "📂 *Harajat kategoriyasini tanlang:*",
        "rec_saved":            "✅ Eslatma saqlandi!",
        "rec_deleted":          "🗑 Eslatma o'chirildi",
        "rec_del_confirm":      "o'chirishni tasdiqlaysizmi?",
        "rec_day_label":        "har oyning {day}-kuni",
        "rec_monthly_total":    "Jami oylik",
        "invalid_day":          "❌ 1 dan 31 gacha son kiriting",
        "rec_already":          "Bu oy allaqachon yozilgan",
        "rec_not_found":        "Eslatma topilmadi",
        "rec_paid":             "yozildi, balansdan ayirildi",
        "rec_skipped":          "bu oy o'tkazib yuborildi",
        "quick_hint":           "✍️ *Tez kiritish*\n\n`50000 taksi` → 💸 harajat\n`+500000 maosh` → 💰 kirim\n\n_Summa va izoh yozing — qolganini tugmalar bilan tanlaysiz. Yoki menyudan foydalaning_ 👇",
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
        "undo_btn":             "🗑 Undo last entry",
        "undone":               "cancelled, balance restored",
        "undo_gone":            "This entry has already been undone",
        # Recurring payments
        "recurring_menu":       "⏰ Reminders",
        "rec_none":             "📭 No reminders yet\n\n_Add monthly payments like loans, utilities, subscriptions — the bot reminds you on the right day and records them in one tap_",
        "rec_add_btn":          "➕ New reminder",
        "rec_title_prompt":     "✏️ *Enter payment name:*\n_e.g. Loan, Internet, Subscription_",
        "rec_amount_prompt":    "💵 *Enter monthly amount* (UZS):",
        "rec_day_prompt":       "📅 *Which day of the month?* (1–31)\n_31 means the last day in shorter months_",
        "rec_pm_prompt":        "💳 *Which source pays for it?*",
        "rec_cat_prompt":       "📂 *Choose expense category:*",
        "rec_saved":            "✅ Reminder saved!",
        "rec_deleted":          "🗑 Reminder deleted",
        "rec_del_confirm":      "delete this reminder?",
        "rec_day_label":        "day {day} of every month",
        "rec_monthly_total":    "Monthly total",
        "invalid_day":          "❌ Enter a number from 1 to 31",
        "rec_already":          "Already recorded this month",
        "rec_not_found":        "Reminder not found",
        "rec_paid":             "recorded, balance updated",
        "rec_skipped":          "skipped this month",
        "quick_hint":           "✍️ *Quick entry*\n\n`50000 taxi` → 💸 expense\n`+500000 salary` → 💰 income\n\n_Type amount and comment — pick the rest with buttons. Or use the menu_ 👇",
    },
}

# ─── Categories ──────────────────────────────────────────────────────────────
PAYMENT_METHODS = {
    "uz": ["🏛 AVO", "💳 Shaxsiy karta", "💵 Naqd"],
    "en": ["🏛 AVO", "💳 Personal card", "💵 Cash"],
}


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


def sanitize_md_input(text: str, maxlen: int = 64) -> str:
    """Foydalanuvchi kiritadigan nom/sarlavhalardan Markdown belgilarini
    olib tashlaydi — aks holda shu nom ishtirok etgan har bir xabar
    'Can't parse entities' bilan yiqiladi."""
    return re.sub(r"[*_`\[\]]", "", text).strip()[:maxlen]


def ensure_user_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Restartdan keyin ctx.user_data bo'shab qoladi — ismni DB'dan tiklaydi."""
    name = ctx.user_data.get("user_name")
    if not name and update.effective_user:
        name = get_user_name(update.effective_user.id)
        if name:
            ctx.user_data["user_name"] = name
    return name


_QUICK_RE = re.compile(r"^(\+?)\s*(\d[\d\s.,]*)\s*(.*)$", re.S)

def parse_quick_entry(text: str):
    """Tez kiritish: '50000 taksi' -> ('harajat', 50000.0, 'taksi'),
    '+500000 maosh' -> ('kirim', 500000.0, 'maosh'). Mos kelmasa None."""
    m = _QUICK_RE.match(text.strip())
    if not m:
        return None
    sign, num, comment = m.groups()
    try:
        amount = float(num.replace(" ", "").replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    trans_type = "kirim" if sign == "+" else "harajat"
    return trans_type, amount, comment.strip()


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
                comment_str = f"\n    └ _{escape_markdown(comment)}_" if comment else ""
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
                comment_str = f"\n    └ _{escape_markdown(comment)}_" if comment else ""
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
def kb_main(ctx, undo_txn_id=None):
    l         = lang(ctx)
    user_name = ctx.user_data.get("user_name", "")
    web_app_url = "https://orzu-finance-bot-ab68cb0dfc9f.herokuapp.com"
    if l == "uz":
        rows = [
            [InlineKeyboardButton("📊  Web Dashboard (Yangi!)", web_app=WebAppInfo(url=web_app_url))],
            [InlineKeyboardButton("💸  Harajat", callback_data="menu_expense"),
             InlineKeyboardButton("💰  Kirim",   callback_data="menu_income")],
            [InlineKeyboardButton("📅  Boshqa kun uchun kiritish", callback_data="menu_another_date")],
            [InlineKeyboardButton("💳  Kartalarim", callback_data="menu_cards")],
            [InlineKeyboardButton("🔄  O'tkazma (Transfer)", callback_data="menu_transfer")],
            [InlineKeyboardButton("⏰  Eslatmalar", callback_data="menu_recurring")],
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
            [InlineKeyboardButton("⏰  Reminders", callback_data="menu_recurring")],
            [InlineKeyboardButton("📈  Today", callback_data="report_today"),
             InlineKeyboardButton("📊  Week",  callback_data="report_week"),
             InlineKeyboardButton("📆  Month", callback_data="report_month")],
            [InlineKeyboardButton("🌐  Language", callback_data="change_language")],
        ]
    if undo_txn_id:
        rows.insert(0, [InlineKeyboardButton(
            t("undo_btn", ctx), callback_data=f"undo_{undo_txn_id}"
        )])
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


def parse_transfer_ref(data: str):
    """'trf_card_5' -> ('card', 5); 'trf_wallet_avo' -> ('wallet', 'avo')."""
    if data.startswith("trf_card_"):
        return ("card", int(data.split("trf_card_")[1]))
    if data.startswith("trf_wallet_"):
        return ("wallet", data.split("trf_wallet_")[1])
    return None


def transfer_ref_info(ref, user_name: str, ctx):
    """(label, balans) qaytaradi; karta o'chirilgan bo'lsa None."""
    kind, val = ref
    if kind == "card":
        card = get_card_by_id(val)
        if not card:
            return None
        return (f"🏦 {card[1]}", card[3])
    if val == "avo":
        return ("🏛 AVO", get_wallet_balance(AVO_USER, "avo"))
    label = "💵 Naqd" if lang(ctx) == "uz" else "💵 Cash"
    return (label, get_wallet_balance(user_name, "naqd"))


def apply_transfer_delta(ref, user_name: str, delta: float):
    kind, val = ref
    if kind == "card":
        adjust_card_balance(val, delta)
    else:
        # Upsert — hamyon qatori hali yaratilmagan bo'lishi mumkin
        w_user = AVO_USER if val == "avo" else user_name
        set_wallet_balance(w_user, val, get_wallet_balance(w_user, val) + delta)


def kb_transfer_targets(ctx, exclude_ref=None):
    """O'tkazma manbalari: AVO, Naqd va kartalar."""
    user_name = ctx.user_data.get("user_name", "")
    l = lang(ctx)
    buttons = []
    if exclude_ref != ("wallet", "avo"):
        avo_bal = get_wallet_balance(AVO_USER, "avo")
        buttons.append([InlineKeyboardButton(
            f"🏛 AVO — {format_amount(avo_bal)}", callback_data="trf_wallet_avo"
        )])
    if exclude_ref != ("wallet", "naqd"):
        naqd_lbl = "💵 Naqd" if l == "uz" else "💵 Cash"
        naqd_bal = get_wallet_balance(user_name, "naqd")
        buttons.append([InlineKeyboardButton(
            f"{naqd_lbl} — {format_amount(naqd_bal)}", callback_data="trf_wallet_naqd"
        )])
    for card_id, name, number, balance in get_cards(user_name):
        if exclude_ref == ("card", card_id):
            continue
        num_str = f" ({mask_number(number)})" if number else ""
        buttons.append([InlineKeyboardButton(
            f"🏦 {name}{num_str} — {format_amount(balance)}",
            callback_data=f"trf_card_{card_id}",
        )])
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


def kb_back(ctx, callback_data: str = "menu_back_main"):
    """Bitta '⬅️ Orqaga' tugmali klaviatura."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("back", ctx), callback_data=callback_data)]
    ])


def build_recurring_text(user_name: str, ctx) -> str:
    recs  = get_recurring(user_name)
    title = f"*{t('recurring_menu', ctx)}*\n👤 {user_name}\n"
    if not recs:
        return title + "\n" + t("rec_none", ctx)
    lines = [title]
    total = 0
    for _id, _u, rtitle, amount, day, pm, _cat, _cid, _ym in recs:
        day_str = t("rec_day_label", ctx).format(day=day)
        lines.append(f"⏰ *{rtitle}* — `{format_amount(amount)}`\n└─ 📅 {day_str} · {pm}")
        total += amount
    lines.append(f"\n💼 *{t('rec_monthly_total', ctx)}: `{format_amount(total)}`*")
    return "\n".join(lines)


def kb_recurring_menu(user_name: str, ctx):
    buttons = []
    for rec_id, _u, rtitle, _amt, day, *_ in get_recurring(user_name):
        buttons.append([InlineKeyboardButton(
            f"🗑 {rtitle} ({day})", callback_data=f"rec_item_{rec_id}"
        )])
    buttons.append([InlineKeyboardButton(t("rec_add_btn", ctx), callback_data="rec_add")])
    buttons.append([InlineKeyboardButton(t("back", ctx), callback_data="menu_back_main")])
    return InlineKeyboardMarkup(buttons)


def kb_rec_payment(user_name: str, ctx):
    """Eslatma uchun to'lov manbasi: AVO / Naqd / kartalar."""
    l = lang(ctx)
    buttons = [
        [InlineKeyboardButton("🏛 AVO", callback_data="rec_pm_avo"),
         InlineKeyboardButton("💵 Naqd" if l == "uz" else "💵 Cash",
                              callback_data="rec_pm_naqd")],
    ]
    for card_id, name, number, _bal in get_cards(user_name):
        num_str = f" ({mask_number(number)})" if number else ""
        buttons.append([InlineKeyboardButton(
            f"🏦 {name}{num_str}", callback_data=f"rec_pm_card_{card_id}"
        )])
    buttons.append([InlineKeyboardButton(t("back", ctx), callback_data="rec_back")])
    return InlineKeyboardMarkup(buttons)


def kb_rec_categories(ctx):
    cats = EXPENSE_CATEGORIES[lang(ctx)]
    buttons = []
    for i in range(0, len(cats), 2):
        row = [InlineKeyboardButton(cats[i], callback_data=f"rec_cat_{cats[i]}")]
        if i + 1 < len(cats):
            row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"rec_cat_{cats[i + 1]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(t("back", ctx), callback_data="rec_back")])
    return InlineKeyboardMarkup(buttons)


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
    display_name = sanitize_md_input(update.message.text, maxlen=32).upper()
    if not display_name:
        await update.message.reply_text(
            "Iltimos, ismingizni oddiy harflar bilan kiriting (masalan: ORZU):"
        )
        return REGISTER_NAME

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
    # Entry point sifatida ham chaqiriladi (restartdan keyin eski tugmalar) —
    # shuning uchun ruxsat va user_name shu yerda tekshiriladi/tiklanadi
    if update.effective_user.id not in ALLOWED_USER_IDS:
        await query.answer("🚫 Kirish taqiqlangan!", show_alert=True)
        return ConversationHandler.END
    if not ensure_user_name(update, ctx):
        await query.answer("Avval /start bosing", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    data = query.data

    # Chala qolgan tez-kiritish oqimi keyingi oqimga aralashmasin
    ctx.user_data.pop("quick_entry", None)
    ctx.user_data.pop("quick_comment", None)

    if data == "menu_expense":
        ctx.user_data["trans_type"] = "harajat"
        ctx.user_data.pop("custom_date", None)
        ctx.user_data.pop("custom_date_display", None)
        await query.edit_message_text(
            t("payment_method", ctx),
            parse_mode="Markdown",
            reply_markup=kb_payment_methods(ctx, "exp_pay_"),
        )
        return EXPENSE_PAYMENT

    elif data == "menu_income":
        ctx.user_data["trans_type"] = "kirim"
        ctx.user_data.pop("custom_date", None)
        ctx.user_data.pop("custom_date_display", None)
        await query.edit_message_text(
            t("payment_method", ctx),
            parse_mode="Markdown",
            reply_markup=kb_payment_methods(ctx, "inc_pay_"),
        )
        return INCOME_PAYMENT

    elif data == "menu_transfer":
        l    = lang(ctx)
        text = (
            "🔄 *O'tkazma*\n\n📤 _Qayerdan_ o'tkazmoqchisiz?"
            if l == "uz" else
            "🔄 *Transfer*\n\n📤 Transfer _from where_?"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=kb_transfer_targets(ctx),
        )
        return TRANSFER_FROM

    elif data == "menu_another_date":
        back_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton(t("back", ctx), callback_data="menu_back_main")]
        ])
        await query.edit_message_text(t("enter_date", ctx), parse_mode="Markdown", reply_markup=back_btn)
        return ANOTHER_DATE_INPUT

    elif data == "menu_cards":
        user_name = ctx.user_data.get("user_name", "?")
        await query.edit_message_text(
            build_cards_text(user_name, ctx),
            parse_mode="Markdown",
            reply_markup=kb_cards_menu(user_name, ctx),
        )
        return CARD_MENU

    elif data == "menu_recurring":
        user_name = ctx.user_data.get("user_name", "?")
        await query.edit_message_text(
            build_recurring_text(user_name, ctx),
            parse_mode="Markdown",
            reply_markup=kb_recurring_menu(user_name, ctx),
        )
        return REC_MENU

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

    elif data == "change_language":
        current_lang = lang(ctx)
        new_lang = "en" if current_lang == "uz" else "uz"
        ctx.user_data["lang"] = new_lang
        lang_name = "English 🇬🇧" if new_lang == "en" else "O'zbek 🇺🇿"
        user_name = ctx.user_data.get("user_name", "?")
        menu_label = TEXTS[new_lang]["main_menu"]
        await query.edit_message_text(
            f"🌐 Til o'zgartirildi: *{lang_name}*\n\n"
            f"👤 *{user_name}*\n━━━━━━━━━━━━━━━━━━━━━━\n📋 {menu_label}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    return MAIN_MENU


# ── Quick Entry ──
async def msg_quick_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bosh menyuda oddiy matn: '50000 taksi' → tez kiritish oqimi."""
    if not ensure_user_name(update, ctx):
        await update.message.reply_text("Avval /start bosib ro'yxatdan o'ting.")
        return ConversationHandler.END

    parsed = parse_quick_entry(update.message.text)
    if not parsed:
        await update.message.reply_text(
            t("quick_hint", ctx), parse_mode="Markdown", reply_markup=kb_main(ctx)
        )
        return MAIN_MENU

    trans_type, amount, comment = parsed
    ctx.user_data["trans_type"]    = trans_type
    ctx.user_data["amount"]        = amount
    ctx.user_data["quick_comment"] = comment
    ctx.user_data["quick_entry"]   = True
    ctx.user_data.pop("custom_date", None)
    ctx.user_data.pop("custom_date_display", None)

    type_emoji = "💰" if trans_type == "kirim" else "💸"
    cmt_note   = f" · _{escape_markdown(comment)}_" if comment else ""
    prefix     = "inc_pay_" if trans_type == "kirim" else "exp_pay_"
    await update.message.reply_text(
        f"{type_emoji} *{format_amount(amount)}*{cmt_note}\n\n{t('payment_method', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_payment_methods(ctx, prefix),
    )
    return INCOME_PAYMENT if trans_type == "kirim" else EXPENSE_PAYMENT


# ── Another Date ──
async def msg_another_date_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    dt = parse_date(date_str)
    if not dt:
        await update.message.reply_text(
            t("invalid_date", ctx), parse_mode="Markdown", reply_markup=kb_back(ctx)
        )
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
            parse_mode="Markdown",
            reply_markup=kb_payment_methods(ctx, "exp_pay_"),
        )
        return EXPENSE_PAYMENT

    elif data == "adate_income":
        ctx.user_data["trans_type"] = "kirim"
        await query.edit_message_text(
            t("payment_method", ctx),
            parse_mode="Markdown",
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
            parse_mode="Markdown",
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

    # Tez kiritishda summa allaqachon bor → darhol saqlaymiz
    if ctx.user_data.pop("quick_entry", False):
        _save_and_deduct(ctx, ctx.user_data.pop("quick_comment", ""))
        return await _finalize_save_from_callback(query, ctx)

    # No card needed → ask amount
    await query.edit_message_text(
        f"💳 *{payment}* › 📂 *{category}*{date_note}\n\n{t('enter_amount', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_back(ctx, "back_to_categories"),
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
            parse_mode="Markdown",
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

    if ctx.user_data.pop("quick_entry", False):
        _save_and_deduct(ctx, ctx.user_data.pop("quick_comment", ""))
        return await _finalize_save_from_callback(query, ctx)

    await query.edit_message_text(
        f"💰 *{payment}* › 📂 *{category}*{date_note}\n\n{t('enter_amount', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_back(ctx, "back_to_categories"),
    )
    return ENTER_AMOUNT



# ── Select Card For Expense ──
async def cb_select_payment_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "paycard_back":
        trans_type = ctx.user_data.get("trans_type", "harajat")
        if trans_type == "kirim":
            await query.edit_message_text(
                t("income_category", ctx),
                parse_mode="Markdown",
                reply_markup=kb_income_categories(ctx),
            )
            return INCOME_CATEGORY
        else:
            await query.edit_message_text(
                t("expense_category", ctx),
                parse_mode="Markdown",
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

    if ctx.user_data.pop("quick_entry", False):
        _save_and_deduct(ctx, ctx.user_data.pop("quick_comment", ""))
        return await _finalize_save_from_callback(query, ctx)

    await query.edit_message_text(
        f"💳 *{payment}* › 📂 *{category}*{date_note}\n"
        f"🏦 *{name}* — `{format_amount(balance)}`\n\n"
        f"{t('enter_amount', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_back(ctx, "back_to_categories"),
    )
    return ENTER_AMOUNT


# ── Amount / Comment ──
async def cb_amount_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ENTER_AMOUNT dan kategoriya tanlashga qaytish."""
    query = update.callback_query
    await query.answer()
    payment   = ctx.user_data.get("payment_method", "")
    date_str  = ctx.user_data.get("custom_date_display", "")
    date_note = f"\n📅 _{date_str}_" if date_str else ""

    if ctx.user_data.get("trans_type") == "kirim":
        await query.edit_message_text(
            f"💰 *{payment}*{date_note}\n\n{t('income_category', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_income_categories(ctx),
        )
        return INCOME_CATEGORY

    await query.edit_message_text(
        f"💳 *{payment}*{date_note}\n\n{t('expense_category', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_expense_categories(ctx),
    )
    return EXPENSE_CATEGORY


async def cb_comment_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Izoh bosqichidan summa kiritishga qaytish."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        t("enter_amount", ctx),
        parse_mode="Markdown",
        reply_markup=kb_back(ctx, "back_to_categories"),
    )
    return ENTER_AMOUNT


async def msg_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text(
            t("invalid_number", ctx),
            reply_markup=kb_back(ctx, "back_to_categories"),
        )
        return ENTER_AMOUNT

    ctx.user_data["amount"] = amount
    await update.message.reply_text(
        t("enter_comment", ctx),
        parse_mode="Markdown",
        reply_markup=kb_back(ctx, "back_to_amount"),
    )
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
    card_id        = ctx.user_data.get("expense_card_id")

    txn_id = save_transaction(user_name, trans_type, payment_method, category,
                              amount, comment, created_at, card_id)
    ctx.user_data["_last_txn_id"] = txn_id

    # ── Shaxsiy karta uchun balans ayirish
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


def _build_saved_text(ctx) -> str:
    """Saqlangan tranzaksiya tasdiq matni (reply va edit uchun umumiy)."""
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
    return (
        f"✅ *{format_amount(amount)}* {saved_msg}!\n"
        f"┌─ {type_emoji} {payment}"
        f"\n└─ 📂 {category}{card_note}"
        f"\n\n👤 *{user_name}* · 📋 {menu_label}"
    )


async def _update_channel_after_save(bot, ctx):
    """Saqlashdan keyin kanal xabarini yangilaydi (custom_date ni tozalab)."""
    saved_custom_date = ctx.user_data.pop("custom_date", None)
    ctx.user_data.pop("custom_date_display", None)
    try:
        await update_channel_message(bot, target_date=saved_custom_date)
    except Exception as e:
        logger.warning(f"[CHANNEL] Update skipped: {e}")


async def _show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = _build_saved_text(ctx)
    last_txn_id = ctx.user_data.pop("_last_txn_id", None)
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=kb_main(ctx, undo_txn_id=last_txn_id),
    )
    await _update_channel_after_save(ctx.bot, ctx)
    return MAIN_MENU


async def _finalize_save_from_callback(query, ctx):
    """Tez kiritish oqimida callbackdan saqlashni yakunlaydi."""
    text = _build_saved_text(ctx)
    last_txn_id = ctx.user_data.pop("_last_txn_id", None)
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=kb_main(ctx, undo_txn_id=last_txn_id),
    )
    await _update_channel_after_save(ctx.bot, ctx)
    return MAIN_MENU


async def cb_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Saqlangan tranzaksiyani bekor qiladi: yozuv o'chadi, balans qaytadi."""
    query = update.callback_query
    # Entry point sifatida ham ishlaydi (restartdan keyingi eski tugma)
    if update.effective_user.id not in ALLOWED_USER_IDS:
        await query.answer("🚫 Kirish taqiqlangan!", show_alert=True)
        return ConversationHandler.END
    user_name = ensure_user_name(update, ctx)
    if not user_name:
        await query.answer("Avval /start bosing", show_alert=True)
        return ConversationHandler.END
    # Avval javob beramiz — DB sekinlashsa ham tugma "osilib" qolmasin
    await query.answer()
    txn_id = int(query.data.split("undo_")[1])

    row = delete_transaction_and_refund(txn_id, user_name)
    if row is None:
        try:
            await query.edit_message_text(
                f"ℹ️ {t('undo_gone', ctx)}\n\n"
                f"👤 *{user_name}* · 📋 {t('main_menu', ctx)}",
                parse_mode="Markdown",
                reply_markup=kb_main(ctx),
            )
        except Exception:
            pass  # xabar allaqachon o'zgartirilgan bo'lishi mumkin
        return MAIN_MENU

    _, _, trans_type, payment_method, category, amount, _, created_at = row
    type_emoji = "💸" if trans_type == "harajat" else "💰"
    menu_label = t("main_menu", ctx)
    await query.edit_message_text(
        f"🗑 *{format_amount(amount)}* {t('undone', ctx)}\n"
        f"┌─ {type_emoji} {payment_method}"
        f"\n└─ 📂 {category}"
        f"\n\n👤 *{user_name}* · 📋 {menu_label}",
        parse_mode="Markdown",
        reply_markup=kb_main(ctx),
    )
    # Kanalni tranzaksiya sanasi bo'yicha yangilaymiz (o'tgan kun bo'lsa ham)
    txn_date = (created_at or "")[:10] or None
    try:
        await update_channel_message(ctx.bot, target_date=txn_date)
    except Exception as e:
        logger.warning(f"[CHANNEL] Update skipped: {e}")
    return MAIN_MENU


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/export [YYYY-MM] — oy tranzaksiyalarini CSV qilib yuboradi."""
    user_name = get_user_name(update.effective_user.id)
    if not user_name:
        await update.message.reply_text("Avval /start bosib ro'yxatdan o'ting.")
        return

    args = ctx.args or []
    if args and re.fullmatch(r"\d{4}-\d{2}", args[0]):
        ym = args[0]
    else:
        ym = datetime.now(UTC5).strftime("%Y-%m")

    rows = get_transactions_for_month(user_name, ym)
    if not rows:
        await update.message.reply_text(f"📭 {ym} uchun yozuv topilmadi")
        return

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Sana", "Turi", "To'lov usuli", "Kategoriya", "Summa", "Izoh"])
    total_exp = total_inc = 0
    for created_at, ttype, pm, cat, amount, cmt in rows:
        w.writerow([created_at[:16].replace("T", " "), ttype, pm, cat, amount, cmt or ""])
        if ttype == "harajat":
            total_exp += amount
        else:
            total_inc += amount

    # utf-8-sig — Excel kirillcha/emoji ni to'g'ri ochishi uchun
    data = buf.getvalue().encode("utf-8-sig")
    await update.message.reply_document(
        document=data,
        filename=f"finance_{user_name}_{ym}.csv",
        caption=(
            f"📄 *{user_name} — {ym}*\n"
            f"{len(rows)} ta yozuv · 💚 `{format_amount(total_inc)}` · 🔴 `{format_amount(total_exp)}`"
        ),
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current_lang = ctx.user_data.get("lang", "uz")
    ctx.user_data.clear()
    msg = ("❌ Bekor qilindi. /start ni bosing."
           if current_lang == "uz" else "❌ Cancelled. Press /start.")
    await update.message.reply_text(msg)
    return ConversationHandler.END


# ── Transfer Handlers ──
async def cb_transfer_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qayerdan — FROM manbani tanlash (AVO/Naqd/karta)."""
    query = update.callback_query
    await query.answer()
    data      = query.data
    user_name = ctx.user_data.get("user_name", "?")

    if data == "menu_back_main":
        await query.edit_message_text(
            f"👫 *{user_name}* — {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    ref  = parse_transfer_ref(data)
    info = transfer_ref_info(ref, user_name, ctx) if ref else None
    if info is None:
        await query.edit_message_text(
            "🔄 *O'tkazma*\n\n📤 _Qayerdan_ o'tkazmoqchisiz?"
            if lang(ctx) == "uz" else
            "🔄 *Transfer*\n\n📤 Transfer _from where_?",
            parse_mode="Markdown",
            reply_markup=kb_transfer_targets(ctx),
        )
        return TRANSFER_FROM

    ctx.user_data["trf_from_ref"] = ref
    label, bal = info
    l = lang(ctx)
    text = (
        f"🔄 *O'tkazma*\n"
        f"📤 _Dan:_ *{label}*  `{format_amount(bal)}`\n\n"
        f"📥 _Qayerga_ o'tkazmoqchisiz?"
        if l == "uz" else
        f"🔄 *Transfer*\n"
        f"📤 _From:_ *{label}*  `{format_amount(bal)}`\n\n"
        f"📥 Transfer _to where_?"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=kb_transfer_targets(ctx, exclude_ref=ref),
    )
    return TRANSFER_TO


async def cb_transfer_to(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qayerga — TO manbani tanlash."""
    query = update.callback_query
    await query.answer()
    data      = query.data
    user_name = ctx.user_data.get("user_name", "?")

    if data == "menu_back_main":
        # FROM ga qaytish
        await query.edit_message_text(
            "🔄 *O'tkazma*\n\n📤 _Qayerdan_ o'tkazmoqchisiz?"
            if lang(ctx) == "uz" else
            "🔄 *Transfer*\n\n📤 Transfer _from where_?",
            parse_mode="Markdown",
            reply_markup=kb_transfer_targets(ctx),
        )
        return TRANSFER_FROM

    to_ref    = parse_transfer_ref(data)
    from_ref  = ctx.user_data.get("trf_from_ref")
    to_info   = transfer_ref_info(to_ref, user_name, ctx) if to_ref else None
    from_info = transfer_ref_info(from_ref, user_name, ctx) if from_ref else None
    if to_info is None or from_info is None:
        await query.edit_message_text(
            "🔄 *O'tkazma*\n\n📤 _Qayerdan_ o'tkazmoqchisiz?"
            if lang(ctx) == "uz" else
            "🔄 *Transfer*\n\n📤 Transfer _from where_?",
            parse_mode="Markdown",
            reply_markup=kb_transfer_targets(ctx),
        )
        return TRANSFER_FROM

    ctx.user_data["trf_to_ref"] = to_ref
    (f_label, f_bal), (t_label, t_bal) = from_info, to_info
    l = lang(ctx)
    text = (
        f"🔄 *O'tkazma*\n"
        f"📤 _Dan:_ *{f_label}*  `{format_amount(f_bal)}`\n"
        f"📥 _Ga:_  *{t_label}*  `{format_amount(t_bal)}`\n\n"
        f"💵 *Miqdorni kiriting* (so'm):"
        if l == "uz" else
        f"🔄 *Transfer*\n"
        f"📤 _From:_ *{f_label}*  `{format_amount(f_bal)}`\n"
        f"📥 _To:_   *{t_label}*  `{format_amount(t_bal)}`\n\n"
        f"💵 *Enter amount* (UZS):"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=kb_back(ctx, "trf_back_to_select"),
    )
    return TRANSFER_AMOUNT


async def cb_transfer_amount_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """TRANSFER_AMOUNT dan TO tanlashga qaytish."""
    query = update.callback_query
    await query.answer()
    user_name = ctx.user_data.get("user_name", "?")
    from_ref  = ctx.user_data.get("trf_from_ref")
    from_info = transfer_ref_info(from_ref, user_name, ctx) if from_ref else None

    if from_info is None:
        await query.edit_message_text(
            f"👤 *{user_name}* — {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    label, bal = from_info
    l = lang(ctx)
    text = (
        f"🔄 *O'tkazma*\n"
        f"📤 _Dan:_ *{label}*  `{format_amount(bal)}`\n\n"
        f"📥 _Qayerga_ o'tkazmoqchisiz?"
        if l == "uz" else
        f"🔄 *Transfer*\n"
        f"📤 _From:_ *{label}*  `{format_amount(bal)}`\n\n"
        f"📥 Transfer _to where_?"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=kb_transfer_targets(ctx, exclude_ref=from_ref),
    )
    return TRANSFER_TO


async def msg_transfer_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """O'tkazma miqdorini kiritish va bajarilishi."""
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text(
            t("invalid_number", ctx),
            reply_markup=kb_back(ctx, "trf_back_to_select"),
        )
        return TRANSFER_AMOUNT

    user_name = ctx.user_data.get("user_name", "?")
    from_ref  = ctx.user_data.pop("trf_from_ref", None)
    to_ref    = ctx.user_data.pop("trf_to_ref",   None)
    from_info = transfer_ref_info(from_ref, user_name, ctx) if from_ref else None
    to_info   = transfer_ref_info(to_ref, user_name, ctx) if to_ref else None

    if from_info and to_info:
        apply_transfer_delta(from_ref, user_name, -amount)
        apply_transfer_delta(to_ref,   user_name,  amount)
        (f_label, f_bal), (t_label, t_bal) = from_info, to_info
        new_from = f_bal - amount
        new_to   = t_bal + amount
        l = lang(ctx)
        msg = (
            f"✅ *O'tkazma amalga oshdi!*\n\n"
            f"📤 *{f_label}*\n"
            f"   `{format_amount(f_bal)}` ➜ `{format_amount(new_from)}`\n\n"
            f"📥 *{t_label}*\n"
            f"   `{format_amount(t_bal)}` ➜ `{format_amount(new_to)}`\n\n"
            f"💸 *Miqdor: `{format_amount(amount)}`*"
            if l == "uz" else
            f"✅ *Transfer completed!*\n\n"
            f"📤 *{f_label}*\n"
            f"   `{format_amount(f_bal)}` ➜ `{format_amount(new_from)}`\n\n"
            f"📥 *{t_label}*\n"
            f"   `{format_amount(t_bal)}` ➜ `{format_amount(new_to)}`\n\n"
            f"💸 *Amount: `{format_amount(amount)}`*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        # Kanal xabarini yangilash
        try:
            await update_channel_message(update.get_bot())
        except Exception as e:
            logger.warning(f"[CHANNEL] Update skipped: {e}")

    await update.message.reply_text(
        f"👫 *{user_name}* — {t('main_menu', ctx)}",
        parse_mode="Markdown",
        reply_markup=kb_main(ctx),
    )
    return MAIN_MENU


# ── Recurring Payments (Eslatmalar) ──
async def _render_recurring_list(query, ctx, header: str = ""):
    """Eslatmalar ro'yxatini chizadi (ixtiyoriy sarlavha bilan)."""
    user_name = ctx.user_data.get("user_name", "?")
    text = build_recurring_text(user_name, ctx)
    if header:
        text = f"{header}\n\n{text}"
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=kb_recurring_menu(user_name, ctx),
    )
    return REC_MENU


async def cb_rec_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_back_main":
        user_name = ctx.user_data.get("user_name", "?")
        await query.edit_message_text(
            f"👤 *{user_name}*\n━━━━━━━━━━━━━━━━━━━━━━\n📋 {t('main_menu', ctx)}",
            parse_mode="Markdown",
            reply_markup=kb_main(ctx),
        )
        return MAIN_MENU

    elif data == "rec_add":
        await query.edit_message_text(
            t("rec_title_prompt", ctx), parse_mode="Markdown",
            reply_markup=kb_back(ctx, "rec_back"),
        )
        return REC_ADD_TITLE

    elif data.startswith("rec_item_"):
        rec_id = int(data.split("rec_item_")[1])
        rec = get_recurring_by_id(rec_id)
        if not rec:
            return await _render_recurring_list(query, ctx)
        await query.edit_message_text(
            f"🗑 *{rec[2]}* — `{format_amount(rec[3])}`\n\n{t('rec_del_confirm', ctx)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t("yes_delete", ctx), callback_data=f"rec_del_{rec_id}"),
                InlineKeyboardButton(t("no_cancel", ctx),  callback_data="rec_back"),
            ]]),
        )
        return REC_MENU

    elif data.startswith("rec_del_"):
        rec_id = int(data.split("rec_del_")[1])
        delete_recurring(rec_id)
        return await _render_recurring_list(query, ctx, header=t("rec_deleted", ctx))

    elif data == "rec_back":
        return await _render_recurring_list(query, ctx)

    return REC_MENU


async def cb_rec_back_to_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qo'shish bosqichlaridan ro'yxatga qaytish."""
    query = update.callback_query
    await query.answer()
    for key in ("new_rec_title", "new_rec_amount", "new_rec_day",
                "new_rec_pm", "new_rec_card_id"):
        ctx.user_data.pop(key, None)
    return await _render_recurring_list(query, ctx)


async def msg_rec_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    title = sanitize_md_input(update.message.text)
    if not title:
        await update.message.reply_text(
            t("rec_title_prompt", ctx), parse_mode="Markdown",
            reply_markup=kb_back(ctx, "rec_back"),
        )
        return REC_ADD_TITLE
    ctx.user_data["new_rec_title"] = title
    await update.message.reply_text(
        t("rec_amount_prompt", ctx), parse_mode="Markdown",
        reply_markup=kb_back(ctx, "rec_back"),
    )
    return REC_ADD_AMOUNT


async def msg_rec_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            t("invalid_number", ctx), reply_markup=kb_back(ctx, "rec_back")
        )
        return REC_ADD_AMOUNT

    ctx.user_data["new_rec_amount"] = amount
    await update.message.reply_text(
        t("rec_day_prompt", ctx), parse_mode="Markdown",
        reply_markup=kb_back(ctx, "rec_back"),
    )
    return REC_ADD_DAY


async def msg_rec_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        day = int(update.message.text.strip())
        if not 1 <= day <= 31:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            t("invalid_day", ctx), reply_markup=kb_back(ctx, "rec_back")
        )
        return REC_ADD_DAY

    ctx.user_data["new_rec_day"] = day
    user_name = ctx.user_data.get("user_name", "?")
    await update.message.reply_text(
        t("rec_pm_prompt", ctx), parse_mode="Markdown",
        reply_markup=kb_rec_payment(user_name, ctx),
    )
    return REC_ADD_PAYMENT


async def cb_rec_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    l    = lang(ctx)

    if data == "rec_pm_avo":
        pm, card_id = "🏛 AVO", None
    elif data == "rec_pm_naqd":
        pm, card_id = ("💵 Naqd" if l == "uz" else "💵 Cash"), None
    else:  # rec_pm_card_<id>
        card_id = int(data.split("rec_pm_card_")[1])
        card = get_card_by_id(card_id)
        if not card:
            user_name = ctx.user_data.get("user_name", "?")
            await query.edit_message_text(
                t("rec_pm_prompt", ctx), parse_mode="Markdown",
                reply_markup=kb_rec_payment(user_name, ctx),
            )
            return REC_ADD_PAYMENT
        pm = f"💳 {card[1]}"

    ctx.user_data["new_rec_pm"]      = pm
    ctx.user_data["new_rec_card_id"] = card_id
    await query.edit_message_text(
        t("rec_cat_prompt", ctx), parse_mode="Markdown",
        reply_markup=kb_rec_categories(ctx),
    )
    return REC_ADD_CATEGORY


async def cb_rec_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category  = query.data[len("rec_cat_"):]
    user_name = ctx.user_data.get("user_name", "?")

    add_recurring(
        user_name,
        ctx.user_data.pop("new_rec_title", "?"),
        ctx.user_data.pop("new_rec_amount", 0),
        ctx.user_data.pop("new_rec_day", 1),
        ctx.user_data.pop("new_rec_pm", "💵 Naqd"),
        category,
        ctx.user_data.pop("new_rec_card_id", None),
    )
    return await _render_recurring_list(query, ctx, header=t("rec_saved", ctx))


async def cb_recurring_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Eslatma xabaridagi tugmalar — suhbat holatidan mustaqil (global) ishlaydi."""
    query   = update.callback_query
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await query.answer("🚫 Kirish taqiqlangan!", show_alert=True)
        return

    user_name = get_user_name(user_id)
    rec_id    = int(query.data.rsplit("_", 1)[1])
    rec       = get_recurring_by_id(rec_id)

    if not rec or rec[1] != user_name:
        await query.answer(t("rec_not_found", ctx), show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    title, amount = rec[2], rec[3]

    if query.data.startswith("rec_skip_"):
        await query.answer()
        await query.edit_message_text(
            f"⏭ *{title}* — {t('rec_skipped', ctx)}", parse_mode="Markdown"
        )
        return

    ym = datetime.now(UTC5).strftime("%Y-%m")
    if rec[8] == ym:
        await query.answer(t("rec_already", ctx), show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    record_recurring_payment(rec, ym)
    await query.answer()
    await query.edit_message_text(
        f"✅ *{title}* — `{format_amount(amount)}` {t('rec_paid', ctx)}\n"
        f"┌─ 💳 {rec[5]}"
        f"\n└─ 📂 {rec[6]}",
        parse_mode="Markdown",
    )
    try:
        await update_channel_message(ctx.bot)
    except Exception as e:
        logger.warning(f"[CHANNEL] Update skipped: {e}")


# ── Card Menu ──
async def cb_card_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data      = query.data
    user_name = ctx.user_data.get("user_name", "?")

    if data.startswith("wallet_edit_"):
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
        await query.edit_message_text(
            prompt, parse_mode="Markdown",
            reply_markup=kb_back(ctx, "card_back_list"),
        )
        return WALLET_EDIT

    elif data == "card_add":
        await query.edit_message_text(
            t("card_add_name", ctx), parse_mode="Markdown",
            reply_markup=kb_back(ctx, "card_back_list"),
        )
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
    name = sanitize_md_input(update.message.text)
    if not name:
        await update.message.reply_text(
            t("card_add_name", ctx), parse_mode="Markdown",
            reply_markup=kb_back(ctx, "card_back_list"),
        )
        return CARD_ADD_NAME
    ctx.user_data["new_card_name"] = name
    await update.message.reply_text(
        t("card_add_number", ctx), parse_mode="Markdown",
        reply_markup=kb_back(ctx, "card_back_list"),
    )
    return CARD_ADD_NUMBER


async def msg_card_add_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = sanitize_md_input(update.message.text, maxlen=20)
    ctx.user_data["new_card_number"] = text
    await update.message.reply_text(
        t("card_add_balance", ctx), parse_mode="Markdown",
        reply_markup=kb_back(ctx, "card_back_list"),
    )
    return CARD_ADD_BALANCE


async def cmd_skip_card_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_card_number"] = ""
    await update.message.reply_text(
        t("card_add_balance", ctx), parse_mode="Markdown",
        reply_markup=kb_back(ctx, "card_back_list"),
    )
    return CARD_ADD_BALANCE


async def msg_card_add_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        balance = float(text)
    except ValueError:
        await update.message.reply_text(
            t("invalid_number", ctx),
            reply_markup=kb_back(ctx, "card_back_list"),
        )
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
    try:
        await update_channel_message(ctx.bot)
    except Exception as e:
        logger.warning(f"[CHANNEL] Update skipped: {e}")
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
            reply_markup=kb_back(ctx, "card_back_list"),
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
        await update.message.reply_text(
            t("invalid_number", ctx),
            reply_markup=kb_back(ctx, "card_back_list"),
        )
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
    try:
        await update_channel_message(ctx.bot)
    except Exception as e:
        logger.warning(f"[CHANNEL] Update skipped: {e}")
    return CARD_MENU


async def msg_wallet_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """AVO yoki Naqd uchun yangi balans kiritish."""
    text = update.message.text.strip().replace(",", "").replace(" ", "")
    try:
        new_balance = float(text)
    except ValueError:
        await update.message.reply_text(
            t("invalid_number", ctx),
            reply_markup=kb_back(ctx, "card_back_list"),
        )
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
    try:
        await update_channel_message(ctx.bot)
    except Exception as e:
        logger.warning(f"[CHANNEL] Update skipped: {e}")
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
        try:
            await update_channel_message(ctx.bot)
        except Exception as e:
            logger.warning(f"[CHANNEL] Update skipped: {e}")
        return CARD_MENU

    elif data == "card_back_list":
        await query.edit_message_text(
            build_cards_text(user_name, ctx),
            parse_mode="Markdown",
            reply_markup=kb_cards_menu(user_name, ctx),
        )
        return CARD_MENU

    return CARD_DELETE_CONFIRM


async def cb_session_expired(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Restartdan keyin suhbat holati yo'qolgan eski tugmalar uchun entry point:
    spinner qoldirmasdan javob berib, yangi bosh menyu ochadi."""
    query = update.callback_query
    if update.effective_user.id not in ALLOWED_USER_IDS:
        await query.answer("🚫 Kirish taqiqlangan!", show_alert=True)
        return ConversationHandler.END
    user_name = ensure_user_name(update, ctx)
    if not user_name:
        await query.answer("Avval /start bosing", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    text = (
        f"👤 *{user_name}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"♻️ Bot yangilangan edi — menyu qaytadan ochildi\n\n"
        f"📋 {t('main_menu', ctx)}"
    )
    try:
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb_main(ctx)
        )
    except Exception:
        # Juda eski xabarni edit qilib bo'lmaydi — yangisini yuboramiz
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id, text=text,
            parse_mode="Markdown", reply_markup=kb_main(ctx),
        )
    return MAIN_MENU


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


async def error_handler(update, context):
    err = context.error
    if err is not None:
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    else:
        tb = traceback.format_exc()
    logger.error(f"Exception while handling an update:\n{tb}")
    # Stack trace maxfiy ma'lumot (fayl yo'llari, DB URL) sizdirishi mumkin —
    # chatga faqat qisqa xabar, to'liq matn logda qoladi
    try:
        err_name = type(err).__name__ if err is not None else "Unknown"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Botda xatolik: {err_name}\nBatafsil: finance_bot.log",
        )
    except Exception as notify_err:
        logger.warning(f"[ERROR] Admin xabari yuborilmadi: {notify_err}")


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN topilmadi! Uni .env fayliga yoki "
            "Heroku Settings → Config Vars ga qo'shing."
        )
    # Prod bot Heroku'da polling qilib turibdi; ikkinchi instansiya bir xil
    # token bilan ishga tushsa Telegram Conflict beradi (bot "qotadi")
    if not os.environ.get("DYNO") and os.environ.get("FORCE_LOCAL") != "1":
        raise SystemExit(
            "Bot prod'da (Heroku worker) ishlayapti — lokal polling u bilan "
            "to'qnashadi. Baribir kerak bo'lsa: FORCE_LOCAL=1 python bot.py"
        )
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_error_handler(error_handler)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start, filters=filters.User(list(ALLOWED_USER_IDS))),
            # Restart suhbat holatini o'chiradi (persistence yo'q) — eski
            # tugmalar va matnlar shu entry'lar orqali qayta ishlay boshlaydi
            CallbackQueryHandler(cb_main_menu, pattern="^(menu_|report_|change_)"),
            CallbackQueryHandler(cb_undo, pattern=r"^undo_\d+$"),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.User(list(ALLOWED_USER_IDS)),
                msg_quick_entry,
            ),
            # Oqim o'rtasidagi eski tugmalar (exp_/trf_/card_...) — davom
            # ettirib bo'lmaydi (ma'lumot yo'qolgan), yangi menyu ochamiz.
            # rec_pay/rec_skip bundan mustasno — ular global handlerda
            CallbackQueryHandler(cb_session_expired, pattern=r"^(?!rec_(?:pay|skip)_)"),
        ],
        states={
            REGISTER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_register_name),
            ],
            MAIN_MENU: [
                CallbackQueryHandler(
                    cb_main_menu,
                    pattern="^(menu_|report_|change_)",
                ),
                CallbackQueryHandler(cb_undo, pattern=r"^undo_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_quick_entry),
            ],
            ANOTHER_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_another_date_input),
                CallbackQueryHandler(cb_main_menu, pattern="^menu_back_main$"),
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
                CallbackQueryHandler(cb_amount_back, pattern="^back_to_categories$"),
            ],
            ENTER_COMMENT: [
                CommandHandler("skip", cmd_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_comment),
                CallbackQueryHandler(cb_comment_back, pattern="^back_to_amount$"),
            ],
            # Card states
            CARD_MENU: [
                CallbackQueryHandler(
                    cb_card_menu,
                    pattern="^(card_add$|card_select_|wallet_edit_|menu_back_main$)",
                ),
            ],
            CARD_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_add_name),
                CallbackQueryHandler(cb_card_action, pattern="^card_back_list$"),
            ],
            CARD_ADD_NUMBER: [
                CommandHandler("skip", cmd_skip_card_number),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_add_number),
                CallbackQueryHandler(cb_card_action, pattern="^card_back_list$"),
            ],
            CARD_ADD_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_add_balance),
                CallbackQueryHandler(cb_card_action, pattern="^card_back_list$"),
            ],
            CARD_ACTION: [
                CallbackQueryHandler(
                    cb_card_action,
                    pattern="^(card_upd_|card_del_|card_back_list$)",
                ),
            ],
            CARD_UPDATE_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_card_update_balance),
                CallbackQueryHandler(cb_card_action, pattern="^card_back_list$"),
            ],
            WALLET_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_wallet_edit),
                CallbackQueryHandler(cb_card_action, pattern="^card_back_list$"),
            ],
            CARD_DELETE_CONFIRM: [
                CallbackQueryHandler(
                    cb_card_delete_confirm,
                    pattern="^(card_del_confirm_|card_back_list$)",
                ),
            ],
            # Transfer states
            TRANSFER_FROM: [
                CallbackQueryHandler(
                    cb_transfer_from,
                    pattern="^(trf_card_|trf_wallet_|menu_back_main$)",
                ),
            ],
            TRANSFER_TO: [
                CallbackQueryHandler(
                    cb_transfer_to,
                    pattern="^(trf_card_|trf_wallet_|menu_back_main$)",
                ),
            ],
            TRANSFER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_transfer_amount),
                CallbackQueryHandler(cb_transfer_amount_back, pattern="^trf_back_to_select$"),
            ],
            # Recurring payment states
            REC_MENU: [
                CallbackQueryHandler(
                    cb_rec_menu,
                    pattern="^(rec_add$|rec_item_|rec_del_|rec_back$|menu_back_main$)",
                ),
            ],
            REC_ADD_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_rec_title),
                CallbackQueryHandler(cb_rec_back_to_list, pattern="^rec_back$"),
            ],
            REC_ADD_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_rec_amount),
                CallbackQueryHandler(cb_rec_back_to_list, pattern="^rec_back$"),
            ],
            REC_ADD_DAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_rec_day),
                CallbackQueryHandler(cb_rec_back_to_list, pattern="^rec_back$"),
            ],
            REC_ADD_PAYMENT: [
                CallbackQueryHandler(cb_rec_payment, pattern="^rec_pm_"),
                CallbackQueryHandler(cb_rec_back_to_list, pattern="^rec_back$"),
            ],
            REC_ADD_CATEGORY: [
                CallbackQueryHandler(cb_rec_category, pattern="^rec_cat_"),
                CallbackQueryHandler(cb_rec_back_to_list, pattern="^rec_back$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    app.add_handler(conv)

    # Eslatma tugmalari suhbatdan tashqarida ham ishlashi kerak
    # (eslatma xabari istalgan paytda keladi, hatto bot restartidan keyin ham)
    app.add_handler(CallbackQueryHandler(cb_recurring_action, pattern=r"^rec_(pay|skip)_\d+$"))
    # /export ham istalgan holatda ishlaydi
    app.add_handler(CommandHandler("export", cmd_export,
                                   filters=filters.User(list(ALLOWED_USER_IDS))))

    # Ruxsatsiz foydalanuvchilar uchun yopuvchi handler
    async def _unauthorized(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id not in ALLOWED_USER_IDS:
            if update.callback_query:
                await update.callback_query.answer("🚫 Kirish taqiqlangan!", show_alert=True)
            elif update.message:
                await update.message.reply_text("🚫 Sizda bu botdan foydalanish huquqi yo'q.")
        elif update.callback_query:
            # Ruxsatli user'ning egasiz tugmasi — spinner qolib ketmasin
            await update.callback_query.answer()

    app.add_handler(
        MessageHandler(~filters.User(list(ALLOWED_USER_IDS)), _unauthorized)
    )
    app.add_handler(CallbackQueryHandler(_unauthorized))  # hamma callbacklar

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
    # 09:00 Toshkent (UTC+5) = 04:00 UTC — takrorlanuvchi to'lov eslatmalari
    jq.run_daily(
        send_recurring_reminders,
        time=dt_time(hour=4, minute=0, second=0, tzinfo=timezone.utc),
        name="recurring_reminders",
    )
    # 00:15 Toshkent = 19:15 UTC — oy boshida o'tgan oy hisoboti (kun tekshiruvi ichkarida)
    jq.run_daily(
        send_monthly_report,
        time=dt_time(hour=19, minute=15, second=0, tzinfo=timezone.utc),
        name="monthly_report",
    )
    logger.info("Finance Bot ishga tushdi ✅ | Kanal xabarlari rejalashtirildi")
    # drop_pending_updates=False: restart oynasida yozilgan xabarlar yo'qolmasin
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
