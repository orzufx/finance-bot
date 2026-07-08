"""Umumiy sozlamalar — bot.py va server.py uchun bitta manba.

Qiymatlar environment variable'lardan olinadi; lokal ishlatish uchun
loyiha papkasidagi .env fayli ham o'qiladi (mavjud env ustidan yozmaydi).
Heroku'da esa Settings → Config Vars ishlatiladi.
"""
import os


def _load_env_file():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Vergul bilan ajratilgan Telegram ID lar, masalan: "123,456"
ALLOWED_USER_IDS = {
    int(x)
    for x in os.environ.get(
        "ALLOWED_USER_IDS", "5701684264,6392413373,7064655656"
    ).replace(" ", "").split(",")
    if x
}

# Xatolik haqida qisqa xabar yuboriladigan admin chat
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "7064655656"))
