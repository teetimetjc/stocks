import urllib.request
import urllib.parse
import urllib.error
import json
import time
import datetime
import random
import os
import http.client
from http.cookiejar import CookieJar

# -------------------------------------------------------------------
# HOLDINGS
# -------------------------------------------------------------------
HOLDINGS = {
    "VOO":   {"shares": 4.33,    "avg": 604.54,   "never_sell_all": True},
    "QQQ":   {"shares": 0.9284,  "avg": 592.39,   "never_sell_all": True},
    "VTI":   {"shares": 1.55,    "avg": 323.13,   "never_sell_all": True},
    "SPY":   {"shares": 0.7624,  "avg": 655.81,   "never_sell_all": True},
    "VOOG":  {"shares": 1.16,    "avg": 429.40,   "never_sell_all": True},
    "SCHD":  {"shares": 2.85,    "avg": 27.02,    "never_sell_all": True},
    "DIA":   {"shares": 0.1718,  "avg": 464.47,   "never_sell_all": True},
    "GLD":   {"shares": 0.1394,  "avg": 365.43,   "never_sell_all": True},
    "SMH":   {"shares": 0.1500,  "avg": 333.31,   "never_sell_all": True},
    "JPM":   {"shares": 0.0862,  "avg": 304.51,   "never_sell_all": True},
    "XLK":   {"shares": 0.3495,  "avg": 143.03,   "never_sell_all": True},
    "F":     {"shares": 3.81,    "avg": 13.23,    "never_sell_all": True},
    "XLV":   {"shares": 0.3469,  "avg": 145.54,   "never_sell_all": True},
    "NVDA":  {"shares": 0.2455,  "avg": 205.65,   "never_sell_all": True},
    "META":  {"shares": 0.0791,  "avg": 638.41,   "never_sell_all": True},
    "VOOV":  {"shares": 0.2473,  "avg": 202.18,   "never_sell_all": True},
}

CRYPTO_HOLDINGS = {
    "bitcoin":  {"symbol": "BTC", "units": 0.0, "avg": 0.0},
    "ethereum": {"symbol": "ETH", "units": 0.0, "avg": 0.0},
    "solana":   {"symbol": "SOL", "units": 0.0, "avg": 0.0},
}

# -------------------------------------------------------------------
# Pushover
# -------------------------------------------------------------------
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")

def send_push(title, message, priority=0):
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        print(f"⚠️  Pushover not configured — skipping: {title}")
        return
    try:
        conn = http.client.HTTPSConnection("api.pushover.net:443")
        conn.request(
            "POST", "/1/messages.json",
            urllib.parse.urlencode({
                "token":    PUSHOVER_TOKEN,
                "user":     PUSHOVER_USER,
                "title":    title,
                "message":  message,
                "priority": priority,
                "sound":    "echo",
            }),
            {"Content-type": "application/x-www-form-urlencoded"}
        )
        resp = conn.getresponse()
        if resp.status == 200:
            print(f"📲 Pushover sent: {title}")
        else:
            print(f"⚠️  Pushover HTTP {resp.status}")
    except Exception as e:
        print(f"⚠️  Pushover error: {e}")

# -------------------------------------------------------------------
# Google Sheets logger
# -------------------------------------------------------------------
def log_to_sheet(ticker, name, price, vwap_diff, rsi5, vol_ratio, pl_pct,
                 quick_signal, quick_why, rsi14_daily, ma50, sma200,
                 macd_daily, atr_pct, long_signal, long_why):
    url = "https://script.google.com/macros/s/AKfycbzGjU2QIiOlEtIHxMnFks1DYXDwuDRwPzun_BXZnLVp2iW8AeV4Up1jT7QMkiJJARHvUA/exec"
    params = urllib.parse.urlencode({
        "ticker":       ticker,
        "name":         name,
        "price":        round(price, 6),
        "vwap_diff":    round(vwap_diff, 2)   if vwap_diff  is not None else "",
        "rsi5":         round(rsi5, 1)         if rsi5       is not None else "",
        "vol_ratio":    round(vol_ratio, 2),
        "pl_pct":       round(pl_pct, 1),
        "quick_signal": quick_signal,
        "quick_why":    quick_why,
        "rsi14_daily":  round(rsi14_daily, 1)  if rsi14_daily is not None else "",
        "ma50":         round(ma50, 2)          if ma50        is not None else "",
        "sma200":       round(sma200, 2)        if sma200      is not None else "",
        "macd_daily":   macd_daily              if macd_daily  is not None else "",
        "atr_pct":      round(atr_pct, 2)       if atr_pct     is not None else "",
        "long_signal":  long_signal,
        "long_why":     long_why,
    }).encode()
    try:
        req = urllib.request.urlopen(url + "?" + params.decode(), timeout=15)
        req.read()
        print(f"📊 Logged → {ticker} | Quick: {quick_signal} | Long: {long_signal}")
    except Exception as e:
        print(f"⚠️  Sheet log failed for {ticker}: {e}")

# -------------------------------------------------------------------
# HTTP helpers
# -------------------------------------------------------------------
cookie_jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
]

def safe_get(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": random.choice(AGENTS)})
            with opener.open(req, timeout=30) as r:
                data = json.loads(r.read().decode())
                if isinstance(data, dict) and data.get("finance", {}).get("error"):
                    print(f"Yahoo error: {data['finance']['error']}")
                    return None
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = (2 ** attempt) * 10 + random.uniform(10, 25)
                print(f"429 — waiting {wait:.0f}s...")
                time.sleep(wait)
            else:
                print(f"HTTP {e.code}: {url}")
                return None
        except Exception as e:
            if attempt == retries - 1:
                print(f"Failed after {retries} attempts: {e}")
                return None
            time.sleep((2 ** attempt) * 2 + random.uniform(2, 5))
    return None

# -------------------------------------------------------------------
# Indicators
# -------------------------------------------------------------------
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        chg = closes[i] - closes[i - 1]
        if chg > 0: gains += chg
        else:       losses -= chg
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        chg = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(chg, 0))  / period
        avg_loss = (avg_loss * (period - 1) + max(-chg, 0)) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)

# -------------------------------------------------------------------
# Ticker categories & signal logic
# -------------------------------------------------------------------
BROAD_ETFS = ["VOO", "QQQ", "VTI", "SPY", "VOOG", "SCHD", "DIA", "GLD", "XLK", "XLV", "VOOV"]
MEGA_CAPS  = ["AAPL", "TSLA", "AMZN", "MSFT", "GOOGL", "NVDA", "META"]
HIGH_BETA  = ["SMH", "JPM", "F", "TQQQ", "SOXL"]

DAILY_RISK_BUDGET = 100
daily_spent = 0

def get_vwap_threshold(ticker):
    if ticker in BROAD_ETFS: return -0.6
    if ticker in MEGA_CAPS:  return -0.8
    if ticker in HIGH_BETA:  return -1.2
    return -1.0

def is_market_hours():
    now = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=-5)))
    if now.weekday() >= 5: return False
    return now.replace(hour=9, minute=30) <= now <= now.replace(hour=16, minute=0)

def in_quick_window():
    now = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=-5)))
    return now.replace(hour=9, minute=40) <= now <= now.replace(hour=15, minute=55)

def generate_quick_signal(ticker, price, vwap_diff_pct, rsi_5, vol_ratio, pl_pct, atr_pct):
    global daily_spent
    if not in_quick_window() or not is_market_hours():
        return "QUICK HOLD", "Outside quick window"
    if rsi_5 < 20 and vwap_diff_pct < get_vwap_threshold(ticker) and vol_ratio > 1.5:
        amt = 20 / 2 if (atr_pct and atr_pct > 2) else 20
        if daily_spent + amt > DAILY_RISK_BUDGET:
            return "QUICK HOLD", "Risk budget exceeded"
        daily_spent += amt
        return f"QUICK BUY ${amt}", "RSI5 oversold + VWAP dip + vol spike"
    if ticker in HOLDINGS:
        if pl_pct > 20:
            pct, reason = 0.15, "20%+ quick profit → trim 15%"
        elif pl_pct > 10:
            pct, reason = 0.10, "10%+ quick profit → trim 10%"
        else:
            return "QUICK HOLD", "No quick edge"
        keep   = 0.05 if HOLDINGS[ticker]["never_sell_all"] else 0
        shares = min(HOLDINGS[ticker]["shares"] * pct, HOLDINGS[ticker]["shares"] - keep)
        dollars = shares * price
        if dollars > 10:
            return f"QUICK SELL ${dollars:,.0f}", reason
    return "QUICK HOLD", "No quick edge"

def generate_long_signal(ticker, price, rsi_14, ma50_pullback_pct, vol_ratio, pl_pct, atr_pct, sma200):
    global daily_spent
    if not is_market_hours():
        return "LONG HOLD", "Market closed"
    uptrend = (price > sma200) if sma200 else True
    if not (uptrend or (rsi_14 and rsi_14 < 25)):
        return "LONG HOLD", "Not in uptrend"
    if (30 <= (rsi_14 or 50) <= 45 and
            -6 <= (ma50_pullback_pct or 0) <= -3 and
            vol_ratio > 1.2):
        amt = 100 / 2 if (atr_pct and atr_pct > 2) else 100
        if daily_spent + amt > DAILY_RISK_BUDGET:
            return "LONG HOLD", "Risk budget exceeded"
        daily_spent += amt
        return f"LONG BUY ${amt}", "RSI14 30-45 + MA50 pullback + vol confirm"
    if ticker in HOLDINGS:
        if pl_pct > 200:
            pct, reason = 0.50, "200%+ profit → taking half"
        elif pl_pct > 120:
            pct, reason = 0.35, "120%+ profit → trimming 35%"
        elif pl_pct > 70:
            pct, reason = 0.25, "70%+ profit → trimming 25%"
        else:
            return "LONG HOLD", "No long edge"
        keep   = 0.05 if HOLDINGS[ticker]["never_sell_all"] else 0
        shares = min(HOLDINGS[ticker]["shares"] * pct, HOLDINGS[ticker]["shares"] - keep)
        dollars = shares * price
        if dollars > 30:
            return f"LONG SELL ${dollars:,.0f}", reason
    return "LONG HOLD", "No long edge"

# -------------------------------------------------------------------
# Stock / ETF scan — BATCHED (one API call for all quotes)
# -------------------------------------------------------------------
SCAN_TICKERS = list(HOLDINGS.keys()) + [
    "AAPL", "TSLA", "AMZN", "MSFT", "GOOGL", "SCHG", "TQQQ", "SOXL"
]

def fetch_all_quotes(tickers):
    """Fetch all quotes in a single API call. Returns dict of symbol -> quote."""
    symbols = ",".join(tickers)
    url  = f"https://query2.finance.yahoo.com/v7/finance/quote?symbols={symbols}"
    data = safe_get(url)
    if not data:
        return {}
    results = data.get("quoteResponse", {}).get("result", [])
    return {r["symbol"]: r for r in results}

def fetch_chart(symbol):
    """Fetch 5m chart for a single symbol (needed for RSI calculation)."""
    url  = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=5d"
    return safe_get(url)

def run_stock_scan():
    global daily_spent
    daily_spent = 0
    print("\n📈 Stock/ETF scan starting (batched quotes)...\n")
    action_lines = []

    # ── Step 1: fetch ALL quotes in one shot ──────────────────────
    print(f"Fetching quotes for {len(SCAN_TICKERS)} tickers in one call...")
    all_quotes = fetch_all_quotes(SCAN_TICKERS)
    print(f"✅ Got {len(all_quotes)} quotes")
    time.sleep(2)  # single short pause after the batch call

    # ── Step 2: fetch charts individually (needed for RSI) ────────
    for symbol in SCAN_TICKERS:
        try:
            quote = all_quotes.get(symbol, {})
            price = quote.get("regularMarketPrice") or quote.get("previousClose", 0)
            name  = quote.get("shortName", symbol)

            if not price:
                print(f"⚠️  No price for {symbol}, skipping")
                continue

            # Chart for RSI (still needed per-ticker but we skip if 429)
            rsi_5     = 50.0
            vol_ratio = 1.0
            chart_data = fetch_chart(symbol)
            if chart_data and chart_data.get("chart", {}).get("result"):
                res = chart_data["chart"]["result"][0]
                q   = res["indicators"]["quote"][0]
                closes  = [x for x in q.get("close",  []) if x is not None]
                volumes = [x for x in q.get("volume", []) if x is not None]
                if len(closes) >= 6:
                    rsi_5 = calculate_rsi(closes[-30:], 5)
                if volumes:
                    today_vol = sum(volumes[-78:]) if len(volumes) >= 78 else sum(volumes)
                    avg_vol   = (sum(volumes) / len(volumes)) * 78
                    vol_ratio = today_vol / avg_vol if avg_vol else 1.0

            pl_pct = ((price / HOLDINGS[symbol]["avg"]) - 1) * 100 if symbol in HOLDINGS else 0.0
            vwap_diff_pct = -0.5  # simplified placeholder

            quick_signal, quick_why = generate_quick_signal(
                symbol, price, vwap_diff_pct, rsi_5, vol_ratio, pl_pct, None)
            long_signal,  long_why  = "LONG HOLD", "Daily indicators skipped (rate limit protection)"

            log_to_sheet(symbol, name, price, vwap_diff_pct, rsi_5, vol_ratio, pl_pct,
                         quick_signal, quick_why, None, None, None, "", None,
                         long_signal, long_why)

            if "BUY" in quick_signal or "SELL" in quick_signal:
                line = f"{quick_signal} {symbol} @ ${price:,.2f}\n{quick_why}"
                action_lines.append(line)
                print(f"🚨 {line}")

            # Much shorter delay — just enough to avoid chart endpoint rate limits
            time.sleep(2 + random.uniform(1, 3))

        except Exception as e:
            print(f"Error on {symbol}: {e}")
            time.sleep(5)

    if action_lines:
        send_push("📈 Stock Signals", "\n\n".join(action_lines), priority=1)
    else:
        print("✅ Stock scan complete — no actionable signals")

    print("\n📈 Stock/ETF scan complete.\n")

# -------------------------------------------------------------------
# Crypto scan (CoinGecko — no API key, all coins in one call)
# -------------------------------------------------------------------
CRYPTO_IDS = ["bitcoin", "ethereum", "solana"]

def run_crypto_scan():
    print("\n₿ Crypto scan starting...\n")
    action_lines = []

    # ── Fetch all crypto prices in one call ───────────────────────
    ids = ",".join(CRYPTO_IDS)
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd"
           f"&include_24hr_change=true&include_24hr_vol=true")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            prices = json.loads(r.read().decode())
    except Exception as e:
        print(f"⚠️  CoinGecko price fetch failed: {e}")
        prices = {}

    time.sleep(2)

    for coin_id in CRYPTO_IDS:
        try:
            coin_data  = prices.get(coin_id, {})
            price      = coin_data.get("usd", 0)
            change_24h = coin_data.get("usd_24h_change", 0)
            holding    = CRYPTO_HOLDINGS.get(coin_id, {})
            symbol     = holding.get("symbol", coin_id.upper())
            avg_cost   = holding.get("avg", 0)
            units      = holding.get("units", 0)

            pl_pct = ((price / avg_cost) - 1) * 100 if avg_cost > 0 else 0.0
            value  = units * price if units else 0.0

            # RSI from daily history
            hist_url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
                        f"?vs_currency=usd&days=20&interval=daily")
            rsi_14 = rsi_7 = 50.0
            try:
                req = urllib.request.Request(hist_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    hist = json.loads(r.read().decode())
                    closes = [p[1] for p in hist.get("prices", [])]
                    if len(closes) >= 15:
                        rsi_14 = calculate_rsi(closes, 14)
                    if len(closes) >= 8:
                        rsi_7  = calculate_rsi(closes, 7)
            except Exception as e:
                print(f"⚠️  History fetch failed for {coin_id}: {e}")

            # Signal
            signal = reason = None
            if rsi_14 < 30 and change_24h < -5:
                signal = "BUY — oversold dip"
                reason = f"RSI14={rsi_14:.0f}, 24h={change_24h:.1f}%"
            elif rsi_14 > 75 and change_24h > 5:
                signal = "CONSIDER TRIM"
                reason = f"RSI14={rsi_14:.0f}, 24h={change_24h:.1f}%"
            elif pl_pct > 100:
                signal = "CONSIDER TRIM"
                reason = f"Up {pl_pct:.0f}% from avg cost"

            status = (f"${price:,.2f}  24h: {change_24h:+.1f}%  "
                      f"RSI14: {rsi_14:.0f}  RSI7: {rsi_7:.0f}")
            if units > 0:
                status += f"\n  {units} {symbol} = ${value:,.2f}  (P/L: {pl_pct:+.1f}%)"

            print(f"{symbol}: {status}" + (f" → {signal}" if signal else " → HOLD"))

            if signal:
                action_lines.append(f"₿ {symbol} — {signal}\n{reason}\n{status}")

            time.sleep(2)

        except Exception as e:
            print(f"Error on {coin_id}: {e}")

    if action_lines:
        send_push("₿ Crypto Signals", "\n\n".join(action_lines), priority=1)
    else:
        print("✅ Crypto scan complete — no actionable signals")

    print("\n₿ Crypto scan complete.\n")

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    print(f"🚀 Scan started at {datetime.datetime.now()}")

    if not is_market_hours():
        print("⚠️  Market is closed — running crypto scan only")
        run_crypto_scan()
    else:
        run_stock_scan()
        run_crypto_scan()

    print(f"✅ All scans complete at {datetime.datetime.now()}")
