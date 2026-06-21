"""
Анализатор акций (локальный сервер)
Скоринг «30 секунд» + чек-лист 16 пунктов + квартальные отчёты + графики цены.

Установка:
    pip install flask yfinance flask-cors

Запуск:
    python app.py

Открыть:
    http://localhost:5001
"""

import os
import json
import uuid
import threading
import traceback
import requests
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, send_from_directory, Response, request
from flask_cors import CORS

app = Flask(__name__, static_folder=".")
CORS(app)

PORT = int(os.environ.get("PORT", 5001))

# ---------- кэш (5 минут) ----------
import time as _time
_CACHE: dict = {}
_CACHE_TTL = 300  # секунд

def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and _time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key: str, val):
    _CACHE[key] = (_time.time(), val)

def _yf_info(ticker: str, retries: int = 3):
    """Получить info с retry при rate-limit."""
    for attempt in range(retries):
        try:
            info = yf.Ticker(ticker).info or {}
            if info:
                return info
        except Exception as e:
            msg = str(e).lower()
            if "rate" in msg or "429" in msg or "too many" in msg:
                _time.sleep(2 ** attempt)   # 1 → 2 → 4 с
            else:
                raise
    return {}


# ---------- helpers ----------
def g(info, *keys):
    """Первое не-None значение из info по списку ключей."""
    for k in keys:
        v = info.get(k)
        if v is not None:
            return v
    return None


def safe_float(v):
    """Безопасное приведение к float, None при NaN."""
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def cap_class(mcap):
    if mcap is None:
        return None
    if mcap >= 10e9:
        return "Large Cap"
    if mcap >= 2e9:
        return "Mid Cap"
    return "Small Cap"


def build_quarters(tk):
    """Последние 4 квартала: прогноз vs факт EPS, beat/miss, сюрприз %."""
    rows = []
    beats = 0
    try:
        eh = tk.earnings_history  # DataFrame
        if eh is not None and not eh.empty:
            df = eh.tail(4).iloc[::-1]
            for idx, r in df.iterrows():
                est = r.get("epsEstimate")
                act = r.get("epsActual")
                sur = r.get("surprisePercent")
                est = float(est) if est == est and est is not None else None  # NaN check
                act = float(act) if act == act and act is not None else None
                sur = float(sur) if sur == sur and sur is not None else None
                beat = (act >= est) if (act is not None and est is not None) else None
                if beat:
                    beats += 1
                rows.append({
                    "quarter": str(idx),
                    "estimate": est,
                    "actual": act,
                    "beat": beat,
                    "surprise": sur,
                })
    except Exception:
        pass
    return rows, beats


def _calc_change(info: dict):
    """Вычисляем изменение за день сами — regularMarketChangePercent от yfinance
    часто возвращает мусор для европейских акций."""
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    prev  = info.get("previousClose") or info.get("regularMarketPreviousClose")
    if price and prev and prev != 0:
        chg = (price - prev) / prev
        # Санитарная проверка: дневное изменение > 25% — скорее всего мусор
        if abs(chg) <= 0.25:
            return chg
    return None


@app.route("/")
def index():
    return send_from_directory(".", "analyzer.html")


# ══════════════════════════════════════════════════════════════════════════════
# SCREENER
# ══════════════════════════════════════════════════════════════════════════════

_SCRHDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

SCREEN_CRITERIA = [
    ("Рост выр. ≥10%", lambda d: d["rev_g"]  is not None and d["rev_g"]  >= 0.10),
    ("P/E < 25",       lambda d: d["pe"]      is not None and d["pe"]      < 25),
    ("PEG < 2",        lambda d: d["peg"]     is not None and d["peg"]     < 2),
    ("ROE > 5%",       lambda d: d["roe"]     is not None and d["roe"]     > 0.05),
    ("Ликвид. > 1.5",  lambda d: d["quick"]   is not None and d["quick"]   > 1.5),
]

_screen_tasks: dict = {}


def _scr_get_tickers(index_name: str) -> list:
    urls = {
        "sp500":    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "nasdaq100":"https://en.wikipedia.org/wiki/Nasdaq-100",
        "ftse100":  "https://en.wikipedia.org/wiki/FTSE_100_Index",
        "dax40":    "https://en.wikipedia.org/wiki/DAX",
        "cac40":    "https://en.wikipedia.org/wiki/CAC_40",
    }
    url = urls[index_name]
    resp = requests.get(url, headers=_SCRHDR, timeout=20)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))

    if index_name == "sp500":
        return [t.replace(".", "-") for t in tables[0]["Symbol"].tolist()]
    col_names = ("Ticker", "Symbol", "EPIC")
    suffix = {"ftse100": ".L", "dax40": ".DE", "cac40": ".PA"}.get(index_name, "")
    for t in tables:
        for col in col_names:
            if col in t.columns:
                ticks = t[col].dropna().tolist()
                return [str(x) + suffix for x in ticks] if suffix else ticks
    return []


def _scr_fetch_one(ticker: str) -> dict | None:
    try:
        info = yf.Ticker(ticker).info or {}
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None
        rev_g = info.get("revenueGrowth")
        pe    = info.get("trailingPE")
        peg   = info.get("pegRatio") or info.get("trailingPegRatio")
        roe   = info.get("returnOnEquity")
        quick = info.get("quickRatio") or info.get("currentRatio")
        div_y = info.get("dividendYield")
        if div_y and div_y > 0.5:
            div_y /= 100.0

        d = dict(rev_g=rev_g, pe=pe, peg=peg, roe=roe, quick=quick)
        checks = [fn(d) for _, fn in SCREEN_CRITERIA]
        score  = sum(checks)

        return {
            "ticker":  ticker,
            "name":    (info.get("longName") or info.get("shortName") or ticker)[:40],
            "sector":  info.get("sector", ""),
            "price":   round(float(price), 2),
            "currency":info.get("currency", "USD"),
            "mcap_b":  round(info.get("marketCap", 0) / 1e9, 1),
            "rev_g":   round(rev_g * 100, 1) if rev_g is not None else None,
            "pe":      round(pe,    1)        if pe    is not None else None,
            "peg":     round(peg,   2)        if peg   is not None else None,
            "roe":     round(roe   * 100, 1)  if roe   is not None else None,
            "quick":   round(quick, 2)        if quick is not None else None,
            "div_y":   round(div_y * 100, 2)  if div_y else 0,
            "score":   score,
            "checks":  checks,
        }
    except Exception:
        return None


def _scr_run_task(task_id: str, index_name: str, min_score: int):
    task = _screen_tasks[task_id]
    try:
        tickers = _scr_get_tickers(index_name)
        if not tickers:
            task["status"] = "error"
            task["error"]  = "Не удалось загрузить список тикеров"
            return
        task["total"]  = len(tickers)
        task["status"] = "running"

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_scr_fetch_one, t): t for t in tickers}
            for fut in as_completed(futs):
                task["done"] += 1
                row = fut.result()
                if row and row["score"] >= min_score:
                    task["results"].append(row)
                    task["results"].sort(key=lambda x: (-x["score"], -(x["rev_g"] or 0)))
                    task["results"] = task["results"][:50]

        task["status"] = "done"
    except Exception as e:
        traceback.print_exc()
        task["status"] = "error"
        task["error"]  = str(e)


@app.route("/api/screen/start", methods=["POST"])
def screen_start():
    data       = request.get_json() or {}
    index_name = data.get("index", "sp500")
    min_score  = int(data.get("min_score", 4))
    if index_name not in ("sp500", "nasdaq100", "ftse100", "dax40", "cac40"):
        return jsonify({"error": "Unknown index"}), 400
    task_id = str(uuid.uuid4())[:8]
    _screen_tasks[task_id] = {
        "status": "loading", "done": 0, "total": 0,
        "results": [], "error": None, "index": index_name,
    }
    threading.Thread(
        target=_scr_run_task, args=(task_id, index_name, min_score), daemon=True
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/api/screen/status/<task_id>")
def screen_status(task_id):
    task = _screen_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Not found"}), 404
    return jsonify(task)


# ──────────────────────────────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    data = {
        "name": "Анализатор акций",
        "short_name": "Акции",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0c1614",
        "theme_color": "#0c1614",
        "icons": [
            {"src": "/icon192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon180.png", "sizes": "180x180", "type": "image/png"},
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/json")


@app.route("/icon192.png")
def icon192():
    return send_from_directory(".", "icon192.png")


@app.route("/icon180.png")
def icon180():
    return send_from_directory(".", "icon180.png")


@app.route("/sw.js")
def sw():
    js = """
self.addEventListener('fetch', function(event) {
  event.respondWith(fetch(event.request));
});
"""
    return Response(js, mimetype="application/javascript")


@app.route("/api/analyze/<ticker>")
def analyze(ticker):
    ticker = ticker.strip().upper()
    # Возвращаем из кэша если свежий
    cached = _cache_get(f"analyze:{ticker}")
    if cached:
        return jsonify(cached)
    try:
        tk   = yf.Ticker(ticker)
        info = _yf_info(ticker)
        if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
            return jsonify({"error": "Нет данных по тикеру"}), 404

        mcap = g(info, "marketCap")
        revenue_growth = g(info, "revenueGrowth")
        earnings_growth = g(info, "earningsGrowth", "earningsQuarterlyGrowth")
        pe = g(info, "trailingPE")
        fwd_pe = g(info, "forwardPE")
        peg = g(info, "pegRatio", "trailingPegRatio")
        beta = g(info, "beta")
        roe = g(info, "returnOnEquity")
        quick = g(info, "quickRatio")
        current = g(info, "currentRatio")
        d2e = g(info, "debtToEquity")
        profit_margin = g(info, "profitMargins")
        div_yield = g(info, "dividendYield")
        eps = g(info, "trailingEps")
        cash = g(info, "totalCash")
        debt = g(info, "totalDebt")
        roa_assets = g(info, "returnOnAssets")
        sector = g(info, "sector")
        industry = g(info, "industry")
        dividend_rate = g(info, "dividendRate")
        payout_ratio = g(info, "payoutRatio")

        # нормализуем dividendYield — yfinance иногда даёт 0.0096, иногда 0.96 (оба = 0.96%)
        # порог 0.5: реальная доходность выше 50% невозможна, значит это уже "percent/100"
        if div_yield is not None and div_yield > 0.5:
            div_yield = div_yield / 100.0

        quarters, beats = build_quarters(tk)

        data = {
            "ticker": ticker,
            "name": g(info, "longName", "shortName") or ticker,
            "currency": g(info, "currency") or "",
            "exchange": g(info, "fullExchangeName", "exchange") or "",
            "price": g(info, "currentPrice", "regularMarketPrice"),
            "changePercent": _calc_change(info),
            "marketCap": mcap,
            "capClass": cap_class(mcap),
            "pe": pe,
            "forwardPe": fwd_pe,
            "peg": peg,
            "beta": beta,
            "roe": roe,
            "quickRatio": quick,
            "currentRatio": current,
            "debtToEquity": d2e,
            "revenueGrowth": revenue_growth,
            "earningsGrowth": earnings_growth,
            "profitMargin": profit_margin,
            "dividendYield": div_yield,
            "eps": eps,
            "totalCash": cash,
            "totalDebt": debt,
            "roaAssets": roa_assets,
            "sector": sector,
            "industry": industry,
            "dividendRate": dividend_rate,
            "payoutRatio": payout_ratio,
            "recommendation":       g(info, "recommendationKey"),
            "recommendationMean":   g(info, "recommendationMean"),
            "numberOfAnalysts":     g(info, "numberOfAnalystOpinions"),
            "targetMeanPrice":      g(info, "targetMeanPrice"),
            "targetHighPrice":      g(info, "targetHighPrice"),
            "targetLowPrice":       g(info, "targetLowPrice"),
            "targetMedianPrice":    g(info, "targetMedianPrice"),
            "quarters": quarters,
            "beats": beats,
            "quartersCount": len(quarters),
        }
        _cache_set(f"analyze:{ticker}", data)
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if "rate" in msg.lower() or "429" in msg or "too many" in msg.lower():
            msg = "Yahoo Finance ограничил запросы — подождите 30–60 секунд и попробуйте снова."
        return jsonify({"error": msg}), 500


@app.route("/api/news/<ticker>")
def news(ticker):
    ticker = ticker.strip().upper()
    try:
        items = yf.Ticker(ticker).news or []
        result = []
        for n in items[:6]:
            ts = n.get("providerPublishTime") or n.get("published") or 0
            # Новый формат yfinance возвращает вложенный объект content
            content = n.get("content", {})
            title = (content.get("title") or n.get("title") or "").strip()
            link  = (content.get("canonicalUrl", {}).get("url")
                     or content.get("clickThroughUrl", {}).get("url")
                     or n.get("link") or n.get("url") or "")
            pub   = (content.get("provider", {}) or {}).get("displayName") or n.get("publisher") or ""
            if not title:
                continue
            result.append({"title": title, "link": link,
                           "publisher": pub, "time": ts})
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify([])


@app.route("/api/chart/<ticker>/<period>")
def chart_data(ticker, period):
    ticker = ticker.strip().upper()
    cfg = {
        "1d":  ("1d",  "5m"),
        "5d":  ("5d",  "30m"),
        "1mo": ("1mo", "1d"),
        "6mo": ("6mo", "1d"),
        "ytd": ("ytd", "1d"),
        "1y":  ("1y",  "1d"),
        "5y":  ("5y",  "1wk"),
        "max": ("max", "1mo"),
    }
    if period not in cfg:
        return jsonify({"error": "Invalid period"}), 400
    p, ivl = cfg[period]
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period=p, interval=ivl)
        if hist.empty:
            return jsonify({"error": "No historical data"}), 404
        data = []
        for idx, row in hist.iterrows():
            data.append({
                "t": idx.isoformat(),
                "o": safe_float(row.get("Open")),
                "c": safe_float(row.get("Close")),
            })
        return jsonify({"ticker": ticker, "period": period, "data": data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
