import os
import json
import hmac
import hashlib
import logging
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timedelta
import pytz
import sqlite3

from config import BOT_TOKEN, DATABASE_URL, ALLOWED_USER_IDS

UTC5 = pytz.timezone("Asia/Tashkent")

app = Flask(__name__, static_folder='webapp', static_url_path='')

_pg_pool = None
def get_pg_pool():
    global _pg_pool
    if _pg_pool is None and DATABASE_URL:
        from psycopg2.pool import ThreadedConnectionPool
        # bot.py + server.py birgalikda Heroku'ning 20 talik ulanish
        # cheklovidan oshmasligi uchun har biriga maksimum 5 ta
        _pg_pool = ThreadedConnectionPool(1, 5, DATABASE_URL, sslmode='require')
    return _pg_pool

class PostgresConnWrapper:
    def __init__(self, conn):
        self._conn = conn
    def cursor(self):
        return self._conn.cursor()
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        if DATABASE_URL:
            get_pg_pool().putconn(self._conn)
        else:
            self._conn.close()

def get_conn():
    if DATABASE_URL:
        conn = get_pg_pool().getconn()
        return PostgresConnWrapper(conn)
    else:
        return sqlite3.connect("finance.db")

def init_db():
    conn = get_conn()
    c = conn.cursor()
    try:
        if DATABASE_URL:
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                display_name TEXT,
                created_at TEXT
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS settings (
                id SERIAL PRIMARY KEY,
                key TEXT UNIQUE NOT NULL,
                value TEXT
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS wallets (
                id SERIAL PRIMARY KEY,
                user_name TEXT NOT NULL,
                wallet_type TEXT NOT NULL,
                balance REAL DEFAULT 0,
                UNIQUE(user_name, wallet_type)
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS cards (
                id SERIAL PRIMARY KEY,
                user_name TEXT NOT NULL,
                card_name TEXT,
                card_number TEXT,
                balance REAL DEFAULT 0
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_name TEXT NOT NULL,
                type TEXT,
                payment_method TEXT,
                category TEXT,
                amount REAL,
                comment TEXT,
                created_at TEXT
            )""")
        else:
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                display_name TEXT,
                created_at TEXT
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                wallet_type TEXT NOT NULL,
                balance REAL DEFAULT 0,
                UNIQUE(user_name, wallet_type)
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                card_name TEXT,
                card_number TEXT,
                balance REAL DEFAULT 0
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                type TEXT,
                payment_method TEXT,
                category TEXT,
                amount REAL,
                comment TEXT,
                created_at TEXT
            )""")
        conn.commit()
    finally:
        conn.close()

init_db()


def validate_telegram_data(init_data: str) -> dict:
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None
        hash_val = parsed_data.pop("hash")
        
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash == hash_val:
            user_data = json.loads(parsed_data.get("user", "{}"))
            return user_data
        return None
    except Exception as e:
        print("Validation error:", e)
        return None

def get_user_name(telegram_id):
    conn = get_conn()
    c = conn.cursor()
    query = "SELECT display_name FROM users WHERE telegram_id=%s" if DATABASE_URL else "SELECT display_name FROM users WHERE telegram_id=?"
    try:
        c.execute(query, (telegram_id,))
        row = c.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def require_auth(f):
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("tma "):
            return jsonify({"error": "Unauthorized"}), 401
        
        init_data = auth_header[4:]
        user_data = validate_telegram_data(init_data)
        if not user_data:
            return jsonify({"error": "Invalid init data"}), 401
            
        telegram_id = user_data.get("id")
        if telegram_id not in ALLOWED_USER_IDS:
            return jsonify({"error": "Sizga bu botdan foydalanishga ruxsat yo'q."}), 403
            
        user_name = get_user_name(telegram_id)
        if not user_name:
            user_name = user_data.get("first_name", "Foydalanuvchi")
            now_str = datetime.now(UTC5).isoformat()
            conn2 = get_conn()
            c2 = conn2.cursor()
            try:
                q_user = "INSERT INTO users (telegram_id, display_name, created_at) VALUES (%s, %s, %s) ON CONFLICT (telegram_id) DO UPDATE SET display_name = EXCLUDED.display_name" if DATABASE_URL else "INSERT INTO users (telegram_id, display_name, created_at) VALUES (?, ?, ?) ON CONFLICT (telegram_id) DO UPDATE SET display_name = EXCLUDED.display_name"
                c2.execute(q_user, (telegram_id, user_name, now_str))
                q_setting = "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value" if DATABASE_URL else "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                c2.execute(q_setting, (f'user_{telegram_id}', user_name))
                q_wallet = "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (%s, %s, %s) ON CONFLICT (user_name, wallet_type) DO NOTHING" if DATABASE_URL else "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (?, ?, ?) ON CONFLICT (user_name, wallet_type) DO NOTHING"
                c2.execute(q_wallet, (user_name, 'naqd', 0))
                conn2.commit()
            finally:
                conn2.close()
            
        return f(user_name, *args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

@app.route('/')
def index():
    return send_from_directory('webapp', 'index.html')

@app.route('/api/dashboard', methods=['GET'])
@require_auth
def dashboard(user_name):
    conn = get_conn()
    c = conn.cursor()
    try:
        # AVO Balance
        q_wallet = "SELECT balance FROM wallets WHERE user_name=%s AND wallet_type=%s" if DATABASE_URL else "SELECT balance FROM wallets WHERE user_name=? AND wallet_type=?"
        c.execute(q_wallet, ("SHARED", "avo"))
        avo_row = c.fetchone()
        avo_bal = avo_row[0] if avo_row else 0
        
        # Naqd Balance
        c.execute(q_wallet, (user_name, "naqd"))
        naqd_row = c.fetchone()
        naqd_bal = naqd_row[0] if naqd_row else 0
        
        # Cards
        q_cards = "SELECT id, card_name, card_number, balance FROM cards WHERE user_name=%s ORDER BY id" if DATABASE_URL else "SELECT id, card_name, card_number, balance FROM cards WHERE user_name=? ORDER BY id"
        c.execute(q_cards, (user_name,))
        cards = [{"id": r[0], "name": r[1], "number": r[2], "balance": r[3]} for r in c.fetchall()]
        
        # Today stats
        today = datetime.now(UTC5).strftime("%Y-%m-%d")
        q_today = "SELECT type, SUM(amount) FROM transactions WHERE user_name=%s AND created_at LIKE %s GROUP BY type" if DATABASE_URL else "SELECT type, SUM(amount) FROM transactions WHERE user_name=? AND created_at LIKE ? GROUP BY type"
        c.execute(q_today, (user_name, f"{today}%"))
        today_stats = {"harajat": 0, "kirim": 0}
        for r in c.fetchall():
            today_stats[r[0]] = r[1]
            
        # Recent txns
        q_recent = "SELECT id, type, payment_method, category, amount, comment, created_at FROM transactions WHERE user_name=%s ORDER BY created_at DESC LIMIT 5" if DATABASE_URL else "SELECT id, type, payment_method, category, amount, comment, created_at FROM transactions WHERE user_name=? ORDER BY created_at DESC LIMIT 5"
        c.execute(q_recent, (user_name,))
        recent = [{"id": r[0], "type": r[1], "payment": r[2], "category": r[3], "amount": r[4], "comment": r[5], "date": r[6]} for r in c.fetchall()]
        
        return jsonify({
            "avo": avo_bal,
            "naqd": naqd_bal,
            "cards": cards,
            "today": today_stats,
            "recent": recent
        })
    finally:
        conn.close()

@app.route('/api/stats', methods=['GET'])
@require_auth
def stats(user_name):
    period = request.args.get('period', 'week')
    days = 7 if period == 'week' else 30
    start_date = (datetime.now(UTC5) - timedelta(days=days-1)).strftime("%Y-%m-%d")
    
    conn = get_conn()
    c = conn.cursor()
    try:
        q = "SELECT created_at, type, amount, category FROM transactions WHERE user_name=%s AND created_at >= %s ORDER BY created_at" if DATABASE_URL else "SELECT created_at, type, amount, category FROM transactions WHERE user_name=? AND created_at >= ? ORDER BY created_at"
        c.execute(q, (user_name, start_date))
        rows = c.fetchall()
        
        dates = [(datetime.now(UTC5) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
        data = {d: {"harajat": 0, "kirim": 0} for d in dates}
        categories = {}
        
        for r in rows:
            date_str = r[0][:10]
            if date_str in data:
                data[date_str][r[1]] += r[2]
            
            if r[1] == "harajat":
                categories[r[3]] = categories.get(r[3], 0) + r[2]
                
        return jsonify({
            "labels": [d[-5:] for d in dates], # MM-DD
            "expenses": [data[d]["harajat"] for d in dates],
            "incomes": [data[d]["kirim"] for d in dates],
            "categories": categories
        })
    finally:
        conn.close()

@app.route('/api/transaction', methods=['POST'])
@require_auth
def add_transaction(user_name):
    data = request.get_json(silent=True) or {}
    trans_type = data.get('type')
    payment_method = data.get('payment_method') or ''
    category = data.get('category')
    comment = data.get('comment', '')
    card_id = data.get('card_id')

    try:
        amount = float(data.get('amount') or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400
    if trans_type not in ("harajat", "kirim"):
        return jsonify({"error": "Invalid type"}), 400
    if not category:
        return jsonify({"error": "Invalid category"}), 400

    # Balans qayerdan o'zgarishini oldindan aniqlaymiz — aks holda
    # tranzaksiya saqlanib, hech qanday balans o'zgarmay qolishi mumkin
    wtype = None
    if card_id is not None and str(card_id).strip():
        if not str(card_id).isdigit():
            return jsonify({"error": "Invalid card_id"}), 400
        card_id = int(card_id)
    else:
        card_id = None
        pm = payment_method.lower()
        wtype = "avo" if "avo" in pm else ("naqd" if "naqd" in pm or "cash" in pm else None)
        if wtype is None:
            return jsonify({"error": f"Noma'lum to'lov usuli: {payment_method}"}), 400

    created_at = datetime.now(UTC5).isoformat()

    conn = get_conn()
    c = conn.cursor()
    try:
        # Save transaction
        q_ins = "INSERT INTO transactions (user_name, type, payment_method, category, amount, comment, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)" if DATABASE_URL else "INSERT INTO transactions (user_name, type, payment_method, category, amount, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        c.execute(q_ins, (user_name, trans_type, payment_method, category, amount, comment, created_at))

        # Adjust balances
        delta = amount if trans_type == "kirim" else -amount

        if card_id is not None:
            q_upd_card = "UPDATE cards SET balance = balance + %s WHERE id = %s AND user_name = %s" if DATABASE_URL else "UPDATE cards SET balance = balance + ? WHERE id = ? AND user_name = ?"
            c.execute(q_upd_card, (delta, card_id, user_name))
            if c.rowcount == 0:
                conn.rollback()
                return jsonify({"error": "Karta topilmadi"}), 404
        else:
            w_user = "SHARED" if wtype == "avo" else user_name
            q_upd_wallet = "UPDATE wallets SET balance = balance + %s WHERE user_name = %s AND wallet_type = %s" if DATABASE_URL else "UPDATE wallets SET balance = balance + ? WHERE user_name = ? AND wallet_type = ?"
            c.execute(q_upd_wallet, (delta, w_user, wtype))
            if c.rowcount == 0:
                q_ins_wallet = "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (%s, %s, %s)" if DATABASE_URL else "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (?, ?, ?)"
                c.execute(q_ins_wallet, (w_user, wtype, delta))

        conn.commit()
        return jsonify({"success": True})
    except Exception:
        conn.rollback()
        logging.exception("Transaction save failed")
        return jsonify({"error": "Server xatosi"}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
