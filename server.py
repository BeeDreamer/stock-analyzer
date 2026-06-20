"""
Entry point для Railway.
Запускает Flask (web) + Telegram-бот в одном процессе.
"""
import os
import threading
import time

# ── Flask ──────────────────────────────────────────────────────────────────────
from app import app as flask_app

PORT = int(os.environ.get("PORT", 8080))

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

threading.Thread(target=run_flask, daemon=True).start()
time.sleep(2)
print(f"✅ Flask запущен на порту {PORT}")

# ── Mini App URL ────────────────────────────────────────────────────────────────
# Railway автоматически устанавливает RAILWAY_PUBLIC_DOMAIN
domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
webapp_url = os.environ.get("WEBAPP_URL", "")

if not webapp_url and domain:
    webapp_url = f"https://{domain}"
    os.environ["WEBAPP_URL"] = webapp_url

if webapp_url:
    print(f"🌐 Mini App URL: {webapp_url}")
else:
    print("ℹ️  WEBAPP_URL не задан — кнопки Mini App отключены")

# ── Telegram Bot ───────────────────────────────────────────────────────────────
import bot
bot.main()
