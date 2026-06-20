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
import traceback
import yfinance as yf
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".")
CORS(app)

PORT = int(os.environ.get("PORT", 5001))


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


@app.route("/")
def index():
    return send_from_directory(".", "analyzer.html")


@app.route("/api/analyze/<ticker>")
def analyze(ticker):
    ticker = ticker.strip().upper()
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
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
            "changePercent": g(info, "regularMarketChangePercent"),
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
            "recommendation": g(info, "recommendationKey"),
            "quarters": quarters,
            "beats": beats,
            "quartersCount": len(quarters),
        }
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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
            return jsonify({"error": "Нет исторических данных"}), 404
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
    print(f"\n  Анализатор акций → http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
