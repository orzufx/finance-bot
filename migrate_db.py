import sqlite3
import psycopg2
import os
import sys

def migrate():
    # 1. Connect to SQLite
    if not os.path.exists("finance.db"):
        print("finance.db not found. Nothing to migrate.")
        return

    sqlite_conn = sqlite3.connect("finance.db")
    sqlite_cursor = sqlite_conn.cursor()

    # 2. Connect to PostgreSQL
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL environment variable is not set.")
        print("Usage: DATABASE_URL=postgres://... python migrate_db.py")
        sys.exit(1)

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    pg_conn = psycopg2.connect(db_url, sslmode='require')
    pg_cursor = pg_conn.cursor()

    # 3. Read & Write Settings
    print("Migrating settings...")
    sqlite_cursor.execute("SELECT key, value FROM settings")
    settings = sqlite_cursor.fetchall()
    for key, value in settings:
        pg_cursor.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))

    # 4. Read & Write Wallets
    print("Migrating wallets...")
    sqlite_cursor.execute("SELECT user_name, wallet_type, balance FROM wallets")
    wallets = sqlite_cursor.fetchall()
    # Delete existing to prevent duplicate conflicts during migration
    pg_cursor.execute("DELETE FROM wallets")
    for user_name, wallet_type, balance in wallets:
        pg_cursor.execute("""
            INSERT INTO wallets (user_name, wallet_type, balance)
            VALUES (%s, %s, %s)
        """, (user_name, wallet_type, balance))

    # 5. Read & Write Cards
    print("Migrating cards...")
    sqlite_cursor.execute("SELECT user_name, card_name, card_number, balance, created_at FROM cards")
    cards = sqlite_cursor.fetchall()
    pg_cursor.execute("DELETE FROM cards")
    for row in cards:
        pg_cursor.execute("""
            INSERT INTO cards (user_name, card_name, card_number, balance, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, row)

    # 6. Read & Write Transactions
    print("Migrating transactions...")
    sqlite_cursor.execute("SELECT user_name, type, payment_method, category, amount, comment, created_at FROM transactions")
    txns = sqlite_cursor.fetchall()
    pg_cursor.execute("DELETE FROM transactions")
    for row in txns:
        pg_cursor.execute("""
            INSERT INTO transactions (user_name, type, payment_method, category, amount, comment, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, row)

    # Commit and close
    pg_conn.commit()
    sqlite_conn.close()
    pg_conn.close()
    print("Migration completed successfully!")

if __name__ == "__main__":
    migrate()
