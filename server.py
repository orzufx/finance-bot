import os
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timedelta
import pytz
import sqlite3
import urllib.parse

UTC5 = pytz.timezone("Asia/Tashkent")

app = Flask(__name__, static_folder='webapp', static_url_path='')

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8607311771:AAHSFXsq9usGf4GxQcvhf-PNbB0I_vrf0X4")

def get_conn():
    if DATABASE_URL:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    else:
        return sqlite3.connect("finance.db")

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

ALLOWED_USER_IDS = {5701684264, 6392413373, 7064655656}

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
            from bot import register_user
            register_user(telegram_id, user_name)
            
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
    data = request.json
    trans_type = data.get('type')
    payment_method = data.get('payment_method')
    category = data.get('category')
    amount = float(data.get('amount', 0))
    comment = data.get('comment', '')
    card_id = data.get('card_id')
    
    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400
        
    created_at = datetime.now(UTC5).isoformat()
    
    conn = get_conn()
    c = conn.cursor()
    try:
        # Save transaction
        q_ins = "INSERT INTO transactions (user_name, type, payment_method, category, amount, comment, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)" if DATABASE_URL else "INSERT INTO transactions (user_name, type, payment_method, category, amount, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        c.execute(q_ins, (user_name, trans_type, payment_method, category, amount, comment, created_at))
        
        # Adjust balances
        delta = amount if trans_type == "kirim" else -amount
        
        if card_id and str(card_id).isdigit():
            q_upd_card = "UPDATE cards SET balance = balance + %s WHERE id = %s AND user_name = %s" if DATABASE_URL else "UPDATE cards SET balance = balance + ? WHERE id = ? AND user_name = ?"
            c.execute(q_upd_card, (delta, card_id, user_name))
        else:
            wtype = "avo" if "avo" in payment_method.lower() else ("naqd" if "naqd" in payment_method.lower() or "cash" in payment_method.lower() else None)
            if wtype:
                w_user = "SHARED" if wtype == "avo" else user_name
                q_upd_wallet = "UPDATE wallets SET balance = balance + %s WHERE user_name = %s AND wallet_type = %s" if DATABASE_URL else "UPDATE wallets SET balance = balance + ? WHERE user_name = ? AND wallet_type = ?"
                c.execute(q_upd_wallet, (delta, w_user, wtype))
                if c.rowcount == 0:
                    q_ins_wallet = "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (%s, %s, %s)" if DATABASE_URL else "INSERT INTO wallets (user_name, wallet_type, balance) VALUES (?, ?, ?)"
                    c.execute(q_ins_wallet, (w_user, wtype, delta))
                    
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
