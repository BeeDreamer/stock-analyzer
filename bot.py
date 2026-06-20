#!/usr/bin/env python3
"""
Telegram-бот: Анализатор акций
================================
Установка:
    pip install python-telegram-bot matplotlib

Запуск:
    TG_TOKEN=ваш_токен python bot.py
    -- или --
    python bot.py  (запросит токен интерактивно)

Токен получить у @BotFather в Telegram.
"""

import os
import io
import re
import html
import asyncio
import traceback

import yfinance as yf

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, WebAppInfo, MenuButtonWebApp
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─── Токен ───────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("TG_TOKEN", "").strip()
if not TOKEN:
    TOKEN = input("Введи токен бота (от @BotFather): ").strip()

# URL публичного сервера (ngrok или хостинг). Если задан — бот добавляет кнопку Mini App.
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")

# ─── Helpers ─────────────────────────────────────────────────────────────────

def g(info, *keys):
    for k in keys:
        v = info.get(k)
        if v is not None:
            return v
    return None

def safe_float(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None

def fmt(v, d=2):
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", " ")

def pct(v):
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"

def bn(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.2f} трлн"
    if a >= 1e9:
        return f"{v / 1e9:.2f} млрд"
    if a >= 1e6:
        return f"{v / 1e6:.1f} млн"
    return fmt(v, 0)

def cap_class(mcap):
    if mcap is None:
        return ""
    if mcap >= 10e9:
        return "Large Cap"
    if mcap >= 2e9:
        return "Mid Cap"
    return "Small Cap"

# ─── Данные ──────────────────────────────────────────────────────────────────

def get_analysis(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        price = g(info, "currentPrice", "regularMarketPrice")
        if not info or price is None:
            return {"error": f"Нет данных по тикеру {ticker}"}

        mcap = g(info, "marketCap")
        div_yield = g(info, "dividendYield")

        # yfinance иногда возвращает 0.96 вместо 0.0096
        if div_yield is not None and div_yield > 0.5:
            div_yield = div_yield / 100.0

        return {
            "ticker":        ticker,
            "name":          g(info, "longName", "shortName") or ticker,
            "currency":      g(info, "currency") or "USD",
            "exchange":      g(info, "fullExchangeName", "exchange") or "",
            "price":         price,
            "changePercent": g(info, "regularMarketChangePercent"),
            "marketCap":     mcap,
            "capClass":      cap_class(mcap),
            "pe":            g(info, "trailingPE"),
            "forwardPe":     g(info, "forwardPE"),
            "peg":           g(info, "pegRatio", "trailingPegRatio"),
            "beta":          g(info, "beta"),
            "roe":           g(info, "returnOnEquity"),
            "roa":           g(info, "returnOnAssets"),
            "quickRatio":    g(info, "quickRatio") or g(info, "currentRatio"),
            "debtToEquity":  g(info, "debtToEquity"),
            "revenueGrowth": g(info, "revenueGrowth"),
            "earningsGrowth":g(info, "earningsGrowth", "earningsQuarterlyGrowth"),
            "profitMargin":  g(info, "profitMargins"),
            "dividendYield": div_yield,
            "dividendRate":  g(info, "dividendRate"),
            "payoutRatio":   g(info, "payoutRatio"),
            "eps":           g(info, "trailingEps"),
            "totalCash":     g(info, "totalCash"),
            "totalDebt":     g(info, "totalDebt"),
            "recommendation":g(info, "recommendationKey"),
            "sector":        g(info, "sector"),
            "industry":      g(info, "industry"),
        }
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}

# ─── График (matplotlib) ─────────────────────────────────────────────────────

PERIOD_CFG = {
    "1d":  ("1d",  "5m"),
    "5d":  ("5d",  "30m"),
    "1mo": ("1mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y":  ("1y",  "1d"),
    "5y":  ("5y",  "1wk"),
}

PERIOD_LABELS = {
    "1d": "1 день", "5d": "5 дней", "1mo": "1 месяц",
    "6mo": "6 месяцев", "1y": "1 год", "5y": "5 лет",
}

BG, PANEL = "#0c1614", "#132622"

def generate_chart(ticker: str, period: str = "1d") -> io.BytesIO | None:
    p, ivl = PERIOD_CFG.get(period, ("1d", "5m"))
    try:
        hist = yf.Ticker(ticker).history(period=p, interval=ivl)
        if hist.empty:
            return None

        prices = hist["Close"].values.astype(float)
        dates  = hist.index.to_pydatetime()

        is_up  = prices[-1] >= prices[0]
        color  = "#34d399" if is_up else "#f87171"
        ret    = (prices[-1] - prices[0]) / prices[0] * 100
        ret_s  = f"+{ret:.2f}%" if ret >= 0 else f"{ret:.2f}%"

        fig, ax = plt.subplots(figsize=(12, 5))
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(PANEL)

        # Grid
        ax.yaxis.grid(True, color="#1f3d36", linewidth=0.6, linestyle="--", alpha=0.7)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(False)

        # Chart line + fill
        ax.fill_between(dates, prices, prices.min() * 0.999, alpha=0.18, color=color)
        ax.plot(dates, prices, color=color, linewidth=1.8, solid_capstyle="round")

        # Axes style
        ax.tick_params(colors="#6b8079", labelsize=9)
        ax.yaxis.set_label_position("right")
        ax.yaxis.tick_right()

        # X-axis date format
        if period == "1d":
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        elif period == "5d":
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d"))
        elif period in ("1mo", "6mo"):
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        else:
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        fig.autofmt_xdate(rotation=0, ha="center")

        ax.set_title(
            f"  {ticker}  ·  {PERIOD_LABELS.get(period, period)}  ·  {ret_s}",
            color=color, fontsize=13, fontweight="bold", pad=10,
            loc="left",
        )

        plt.tight_layout(pad=1.5)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150, facecolor=BG, bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)
        return buf

    except Exception:
        traceback.print_exc()
        return None

# ─── Форматирование сообщения ─────────────────────────────────────────────────

REC_MAP = {
    "strong_buy":  "🟢 Strong Buy",
    "buy":         "🟢 Buy",
    "hold":        "🟡 Hold",
    "sell":        "🔴 Sell",
    "strong_sell": "🔴 Strong Sell",
}

def format_analysis(d: dict) -> str:
    lines = []

    # ── Заголовок ──
    chg   = d.get("changePercent") or 0
    chg_s = f"+{chg:.2f}%" if chg >= 0 else f"{chg:.2f}%"
    chg_e = "📈" if chg >= 0 else "📉"
    cur   = html.escape(d.get("currency", ""))
    name  = html.escape(d.get("name", d["ticker"]))
    exch  = html.escape(d.get("exchange", ""))

    lines.append(f"<b>{name}</b>")
    lines.append(f"<code>{d['ticker']}</code>  {exch}  ·  {cur}")
    lines.append(
        f"{chg_e} <b>{fmt(d.get('price', 0))} {cur}</b>  <i>{chg_s} за 1Д</i>"
    )

    mcap = d.get("marketCap")
    if mcap:
        cc = f"  ·  {html.escape(d['capClass'])}" if d.get("capClass") else ""
        lines.append(f"💼 Кап: <b>{bn(mcap)} {cur}</b>{cc}")

    rec = d.get("recommendation")
    if rec:
        lines.append(f"👁 Аналитики: {REC_MAP.get(rec, html.escape(rec))}")

    lines.append("")

    # ── Дерево «30 секунд» ──
    def tri(cond):
        if cond is None:  return "⬜"
        return "✅" if cond else "❌"

    rv  = d.get("revenueGrowth")
    pe  = d.get("pe")
    peg = d.get("peg")
    roe = d.get("roe")
    qr  = d.get("quickRatio")

    checks = [
        (rv  is not None and rv  >= 0.10,  rv  is None, f"Выручка ≥10%:    {pct(rv)}"),
        (pe  is not None and pe  <  25,    pe  is None, f"P/E &lt; 25:     {fmt(pe)}"),
        (peg is not None and peg <  2,     peg is None, f"PEG &lt; 2:      {fmt(peg)}"),
        (roe is not None and roe >  0.05,  roe is None, f"ROE &gt; 5%:     {pct(roe)}"),
        (qr  is not None and qr  >  1.5,   qr is None, f"Ликв. &gt; 1.5:  {fmt(qr)}"),
    ]

    lines.append("🔍 <b>АНАЛИЗ 30 СЕКУНД</b>")
    stopped = False
    unknowns = 0
    for ok, unk, label in checks:
        if unk:
            lines.append(f"  ⬜ {label}")
            unknowns += 1
        elif ok:
            lines.append(f"  ✅ {label}")
        else:
            lines.append(f"  ❌ {label}")
            if not stopped:
                stopped = True

    if stopped:
        lines.append("\n⛔ <b>Не проходит проверку</b>")
    elif unknowns >= 4:
        lines.append("\n⚠️ Недостаточно данных")
    else:
        lines.append("\n✅ <b>ПРОХОДИТ ПРОВЕРКУ</b>")

    lines.append("")

    # ── Ключевые метрики ──
    lines.append("📋 <b>Метрики</b>")

    d2e = d.get("debtToEquity")
    d2e_s = fmt(d2e / 100) if d2e is not None else "—"

    metrics = [
        ("EPS",    fmt(d.get("eps"))),
        ("P/E",    fmt(d.get("pe"))),
        ("PEG",    fmt(d.get("peg"))),
        ("Beta",   fmt(d.get("beta"))),
        ("ROE",    pct(d.get("roe"))),
        ("ROA",    pct(d.get("roa"))),
        ("Маржа",  pct(d.get("profitMargin"))),
        ("D/E",    d2e_s),
        ("Рост вyr", pct(d.get("revenueGrowth"))),
    ]
    # Выводим парами
    for i in range(0, len(metrics), 3):
        row = "  " + "   ".join(
            f"<b>{k}</b>: {v}" for k, v in metrics[i:i+3]
        )
        lines.append(row)

    # ── Дивиденды ──
    dy = d.get("dividendYield")
    dr = d.get("dividendRate")
    po = d.get("payoutRatio")
    if dy:
        div_line = f"\n💰 Дивиденды: <b>{pct(dy)}</b>"
        if dr:
            div_line += f"  ({fmt(dr)} {cur}/год)"
        if po:
            div_line += f"  · payout {pct(po)}"
    else:
        div_line = "\n💰 Дивиденды: нет"
    lines.append(div_line)

    # ── Отрасль ──
    ind = d.get("industry")
    sec = d.get("sector")
    if ind or sec:
        parts = [html.escape(x) for x in filter(None, [ind, sec])]
        lines.append(f"🏭 {' · '.join(parts)}")

    return "\n".join(lines)

# ─── Клавиатура периодов ──────────────────────────────────────────────────────

PERIOD_BTNS = [
    ("1Д", "1d"), ("5Д", "5d"), ("1М", "1mo"),
    ("6М", "6mo"), ("1Г", "1y"), ("5Г", "5y"),
]

def make_keyboard(ticker: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(label, callback_data=f"chart:{ticker}:{p}")
        for label, p in PERIOD_BTNS
    ]]
    if WEBAPP_URL:
        rows.append([
            InlineKeyboardButton(
                "🖥  Открыть полный анализ",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}?ticker={ticker}")
            )
        ])
    return InlineKeyboardMarkup(rows)

# ─── Handlers ────────────────────────────────────────────────────────────────

TICKER_RE = re.compile(r"^[A-Z]{1,6}([.\-][A-Z0-9]{1,5})?$")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Анализатор акций</b>\n\n"
        "Просто отправь тикер:\n"
        "<code>AAPL</code>  <code>MSFT</code>  <code>NVDA</code>  <code>KO</code>\n"
        "<code>MC.PA</code> (LVMH)  <code>BMW.DE</code>  <code>TSCO.L</code>\n\n"
        "Получишь анализ + график с выбором периода.\n\n"
        "/help — справка",
        parse_mode="HTML",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Справка</b>\n\n"
        "<b>Как использовать:</b> отправь тикер, например <code>AAPL</code>\n\n"
        "<b>Биржевые суффиксы:</b>\n"
        "  .PA — Euronext Paris\n"
        "  .DE — Xetra Frankfurt\n"
        "  .L  — London Stock Exchange\n"
        "  .ME — Московская биржа\n\n"
        "<b>После анализа</b> — кнопки для смены периода графика:\n"
        "1Д · 5Д · 1М · 6М · 1Г · 5Г\n\n"
        "<b>Данные:</b> Yahoo Finance (yfinance, без ключей)",
        parse_mode="HTML",
    )

async def handle_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().upper()
    if not TICKER_RE.match(text):
        return  # не тикер — игнорируем

    # Если задан Mini App URL — сразу открываем его с нужным тикером
    if WEBAPP_URL:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"📊 Открыть анализ {text}",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}?ticker={text}")
            )
        ]])
        await update.message.reply_text(
            f"<b>{text}</b> — нажми кнопку для полного анализа:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    # Без Mini App — показываем анализ текстом (старый режим)
    msg = await update.message.reply_text(
        f"⏳ Загружаю <b>{text}</b>…", parse_mode="HTML"
    )
    loop = asyncio.get_event_loop()

    try:
        data = await loop.run_in_executor(None, get_analysis, text)
        if "error" in data:
            await msg.edit_text(f"❌ {data['error']}", parse_mode="HTML")
            return

        analysis = format_analysis(data)
        keyboard  = make_keyboard(text)
        chart_buf = await loop.run_in_executor(None, generate_chart, text, "1d")

        await msg.delete()

        if chart_buf:
            caption = analysis if len(analysis) <= 1024 else analysis[:1020] + "…"
            await update.message.reply_photo(
                photo=chart_buf,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            if len(analysis) > 1024:
                await update.message.reply_text(analysis, parse_mode="HTML")
        else:
            await update.message.reply_text(
                analysis, parse_mode="HTML", reply_markup=keyboard
            )

    except Exception as e:
        traceback.print_exc()
        try:
            await msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass

async def handle_chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, ticker, period = query.data.split(":", 2)
    except ValueError:
        return

    # Показываем индикатор загрузки
    try:
        await query.edit_message_caption(
            caption=f"⏳ Обновляю график {ticker} · {PERIOD_LABELS.get(period, period)}…",
            parse_mode="HTML",
        )
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    try:
        data, chart_buf = await asyncio.gather(
            loop.run_in_executor(None, get_analysis, ticker),
            loop.run_in_executor(None, generate_chart, ticker, period),
        )

        if "error" in data or chart_buf is None:
            await query.edit_message_caption(
                caption="❌ Не удалось получить данные", parse_mode="HTML"
            )
            return

        analysis = format_analysis(data)
        caption  = analysis if len(analysis) <= 1024 else analysis[:1020] + "…"
        keyboard = make_keyboard(ticker)

        await query.edit_message_media(
            media=InputMediaPhoto(
                media=chart_buf,
                caption=caption,
                parse_mode="HTML",
            ),
            reply_markup=keyboard,
        )

    except Exception as e:
        traceback.print_exc()
        try:
            await query.edit_message_caption(caption=f"❌ Ошибка: {e}")
        except Exception:
            pass

# ─── Main ─────────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Устанавливает Menu Button если задан WEBAPP_URL."""
    if WEBAPP_URL:
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="📊 Анализатор",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            )
            print(f"✅ Menu Button → {WEBAPP_URL}")
        except Exception as e:
            print(f"⚠️  Menu Button не установлен: {e}")

def main():
    if not TOKEN:
        print("Укажи токен: TG_TOKEN=xxx python bot.py")
        return

    if WEBAPP_URL:
        print(f"🌐 Mini App URL: {WEBAPP_URL}")
    else:
        print("ℹ️  WEBAPP_URL не задан — Mini App кнопки отключены.")
        print("   Запусти ngrok и передай URL: WEBAPP_URL=https://xxx.ngrok-free.app")

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help",  cmd_help))
    application.add_handler(
        CallbackQueryHandler(handle_chart_callback, pattern=r"^chart:")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ticker)
    )

    print("🤖 Бот запущен. Ctrl+C для остановки.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
