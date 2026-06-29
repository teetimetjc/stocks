"""
Stock Scanner — buy/sell signal generator
-----------------------------------------
Scans a watchlist every ~20 minutes during market hours (driven by the
"Stock Scanner" GitHub Actions workflow on a cron schedule — each run
does exactly one scan; there is no in-process loop).

Generates two signal types per ticker:
  Quick — intraday scalp   (RSI5, real VWAP, volume ratio)
  Long  — swing/position   (RSI14 daily, MA50 pullback, volume ratio)

BUY signals fire for the full STOCKS watchlist (not just things you hold).
SELL/trim signals fire only for tickers you've logged as holdings.

Holdings are not hardcoded. Log individual purchases in the "Buy Log"
tab of the Google Sheet (Date, Ticker, Shares, Price, Notes). Each scan,
the script reads Buy Log, computes an average-cost position per ticker,
and writes the result to the "Holdings" tab automatically — you never
have to do the averaging math by hand.

Outputs:
  • Pushover push notification for any actionable signal
  • Google Sheets log via gspread + service account
    - Sheet1      : full scan log (unchanged format)
    - Buy Log     : you add a row here each time you buy (Date, Ticker, Shares, Price, Notes)
    - Holdings    : auto-computed average-cost summary, written by this script

Env vars required:
  PUSHOVER_USER       — Pushover user key
  PUSHOVER_TOKEN      — Pushover app token
  GOOGLE_CREDENTIALS  — Google service account JSON (string)
  GOOGLE_SHEET_ID     — Google Sheet ID
"""

import urllib.request
import urllib.parse
import urllib.error
import json
import time
import datetime
import random
import os
import http.client
from collections import defaultdict
from http.cookiejar import CookieJar
from zoneinfo import ZoneInfo

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
PUSHOVER_USER      = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_TOKEN     = os.environ.get("PUSHOVER_TOKEN", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GOOGLE_SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "")

EASTERN = ZoneInfo("America/New_York")

DAILY_RISK_BUDGET = 100
daily_spent = 0

BROAD_ETFS = ["VOO", "QQQ", "VTI", "SPY", "VOOG", "SCHD", "DIA", "GLD", "XLK", "XLV", "VOOV",
              "SCHG", "ARKK", "IWM", "EFA", "TLT"]
MEGA_CAPS  = ["AAPL", "TSLA", "AMZN", "MSFT", "GOOGL", "NVDA", "META", "AMD", "PLTR", "COIN"]
HIGH_BETA  = ["SMH", "JPM", "F", "TQQQ", "SOXL", "SOFI", "MSTR"]
CRYPTO     = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "BNB-USD", "AVAX-USD"]

# Full watchlist that BUY signals scan — this is independent of what you hold.
STOCKS = [
    # Broad ETFs
    "VOO", "QQQ", "VTI", "SPY", "VOOG", "SCHD", "DIA", "GLD", "XLK", "XLV", "VOOV",
    "SCHG", "ARKK", "IWM", "EFA", "TLT",
    # Mega caps
    "AAPL", "TSLA", "AMZN", "MSFT", "GOOGL", "NVDA", "META", "AMD", "PLTR", "COIN",
    # High beta
    "SMH", "JPM", "F", "TQQQ", "SOXL", "SOFI", "MSTR",
    # Crypto
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "BNB-USD", "AVAX-USD",
]

SHEET_HEADERS = [
    "Timestamp", "Ticker", "Name", "Price",
    "VWAP Diff %", "RSI5", "Vol Ratio", "P&L %",
    "Quick Signal", "Quick Why",
    "RSI14 Daily", "MA50", "SMA200", "MACD", "ATR %",
    "Long Signal", "Long Why",
]

BUY_LOG_HEADERS  = ["Date", "Ticker", "Shares", "Price", "Notes"]
HOLDINGS_HEADERS = ["Ticker", "Shares", "Avg Cost", "Never Sell All", "Last Updated"]

# -------------------------------------------------------------------
# HTTP
# -------------------------------------------------------------------
_UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
]
cookie_jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))


def safe_get(url, retries=6):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": random.choice(_UA_POOL)})
            with opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, dict) and data.get("finance", {}).get("error"):
                    print("Yahoo error:", data["finance"]["error"])
                    return None
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = (2 ** attempt) * 10 + random.uniform(15, 40)
                print(f"  429 — waiting {wait:.0f}s...")
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code} for {url}")
            return None
        except Exception as e:
            if attempt == retries - 1:
                print(f"  Failed after {retries} attempts: {e}")
                return None
            wait = (2 ** attempt) * 4 + random.uniform(5, 12)
            print(f"  Retry {attempt+1}/{retries} in {wait:.1f}s")
            time.sleep(wait)
    return None

# -------------------------------------------------------------------
# INDICATORS
# -------------------------------------------------------------------

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        chg = closes[i] - closes[i - 1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        chg = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(chg,  0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-chg, 0)) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def calculate_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calculate_ema(values, period):
    if len(values) < period:
        return None
    k   = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calculate_macd(closes, fast=12, slow=26):
    if len(closes) < slow:
        return None
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return None
    return round(ema_fast - ema_slow, 6)


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    atr = sum(trs[-period:]) / period
    return (atr / closes[-1] * 100) if closes[-1] else None


def _market_open_dt_for(dt_utc):
    """UTC instant of 9:30 ET on the Eastern calendar date of dt_utc, DST-aware."""
    local_date = dt_utc.astimezone(EASTERN).date()
    open_local = datetime.datetime.combine(
        local_date, datetime.time(9, 30), tzinfo=EASTERN
    )
    return open_local.astimezone(datetime.timezone.utc)


def calculate_vwap(timestamps, highs, lows, closes, volumes):
    today = datetime.datetime.now(EASTERN).date()
    cum_tpv = cum_vol = 0.0
    for ts, h, l, c, v in zip(timestamps, highs, lows, closes, volumes):
        if any(x is None for x in (ts, h, l, c, v)):
            continue
        if datetime.datetime.fromtimestamp(ts, tz=EASTERN).date() != today:
            continue
        cum_tpv += ((h + l + c) / 3) * v
        cum_vol  += v
    return (cum_tpv / cum_vol) if cum_vol > 0 else None


def calculate_vol_ratio(timestamps, volumes):
    """
    Volume-so-far-today vs. average volume accumulated by THE SAME TIME OF DAY
    across prior sessions in the window. This fixes the original bug, which
    compared partial-day volume (today) against FULL prior-day totals — a
    comparison that's structurally biased low for most of the trading day.
    """
    if not timestamps:
        return 1.0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today   = now_utc.astimezone(EASTERN).date()
    market_open_utc = _market_open_dt_for(now_utc)
    seconds_elapsed_today = max((now_utc - market_open_utc).total_seconds(), 60)

    day_buckets = defaultdict(list)  # date -> list of (seconds_since_open, volume)
    for ts, v in zip(timestamps, volumes):
        if ts is None or v is None:
            continue
        dt_utc = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        d = dt_utc.astimezone(EASTERN).date()
        open_dt = _market_open_dt_for(dt_utc)
        secs_since_open = (dt_utc - open_dt).total_seconds()
        if secs_since_open < 0:
            continue
        day_buckets[d].append((secs_since_open, v))

    today_vol_so_far = sum(v for secs, v in day_buckets.get(today, []) if secs <= seconds_elapsed_today)

    prior_same_window_vols = []
    for d, points in day_buckets.items():
        if d == today:
            continue
        vol_in_window = sum(v for secs, v in points if secs <= seconds_elapsed_today)
        if vol_in_window > 0:
            prior_same_window_vols.append(vol_in_window)

    if not prior_same_window_vols:
        return 1.0
    avg_prior_same_window = sum(prior_same_window_vols) / len(prior_same_window_vols)
    return (today_vol_so_far / avg_prior_same_window) if avg_prior_same_window > 0 else 1.0

# -------------------------------------------------------------------
# DAILY INDICATOR CACHE
# -------------------------------------------------------------------
_daily_cache: dict = {}
DAILY_CACHE_TTL = 3600


def fetch_daily_indicators(ticker: str) -> dict | None:
    now = time.time()
    if ticker in _daily_cache:
        cached_ts, cached_data = _daily_cache[ticker]
        if now - cached_ts < DAILY_CACHE_TTL:
            return cached_data

    url  = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
    data = safe_get(url)
    if not data or not data.get("chart", {}).get("result"):
        return None

    q      = data["chart"]["result"][0]["indicators"]["quote"][0]
    closes = [x for x in q.get("close", []) if x is not None]
    highs  = [x for x in q.get("high",  []) if x is not None]
    lows   = [x for x in q.get("low",   []) if x is not None]

    if len(closes) < 50:
        return None

    result = {
        "rsi14":   calculate_rsi(closes, 14),
        "ma50":    calculate_sma(closes, 50),
        "sma200":  calculate_sma(closes, 200),
        "macd":    calculate_macd(closes),
        "atr_pct": calculate_atr(highs, lows, closes, 14),
    }
    _daily_cache[ticker] = (now, result)
    return result

# -------------------------------------------------------------------
# NOTIFICATIONS
# -------------------------------------------------------------------

def send_push(title, message):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        return
    try:
        conn = http.client.HTTPSConnection("api.pushover.net:443")
        conn.request(
            "POST", "/1/messages.json",
            urllib.parse.urlencode({
                "token":   PUSHOVER_TOKEN,
                "user":    PUSHOVER_USER,
                "title":   title,
                "message": message,
                "sound":   "echo",
            }),
            {"Content-type": "application/x-www-form-urlencoded"},
        )
        conn.getresponse()
    except Exception:
        pass

# -------------------------------------------------------------------
# GOOGLE SHEETS — connection helper
# -------------------------------------------------------------------
_gc = None
_spreadsheet = None


def _get_spreadsheet():
    global _gc, _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet
    if not GOOGLE_CREDENTIALS:
        return None
    import gspread
    _gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDENTIALS))
    _spreadsheet = _gc.open_by_key(GOOGLE_SHEET_ID)
    return _spreadsheet


def _get_or_create_worksheet(name, headers):
    ss = _get_spreadsheet()
    if ss is None:
        return None
    try:
        ws = ss.worksheet(name)
    except Exception:
        ws = ss.add_worksheet(title=name, rows=200, cols=len(headers) + 2)
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws
    if ws.cell(1, 1).value != headers[0]:
        ws.insert_row(headers, index=1)
    return ws

# -------------------------------------------------------------------
# HOLDINGS — derived from the Buy Log tab each scan
# -------------------------------------------------------------------

def load_holdings_from_buy_log():
    """
    Reads the "Buy Log" tab (Date, Ticker, Shares, Price, Notes — one row
    per purchase, entered by hand) and rolls it up into an average-cost
    position per ticker. Writes the result to the "Holdings" tab so you
    can see your current average cost at a glance, and returns it as a
    dict for the scan to use immediately.

    Add a new row to Buy Log any time you buy. Nothing else to maintain —
    the averaging math happens here automatically.
    """
    holdings = {}

    buy_log_ws = _get_or_create_worksheet("Buy Log", BUY_LOG_HEADERS)
    if buy_log_ws is None:
        print("  [HOLDINGS] No Google Sheets connection — holdings empty this run.")
        return holdings

    rows = buy_log_ws.get_all_records()  # list of dicts, keyed by header
    agg = defaultdict(lambda: {"shares": 0.0, "cost": 0.0})

    for r in rows:
        ticker = str(r.get("Ticker", "")).strip().upper()
        if not ticker:
            continue
        try:
            shares = float(r.get("Shares", 0) or 0)
            price  = float(r.get("Price", 0) or 0)
        except (TypeError, ValueError):
            print(f"  [HOLDINGS] Skipping bad row for {ticker}: {r}")
            continue
        if shares <= 0 or price <= 0:
            continue
        agg[ticker]["shares"] += shares
        agg[ticker]["cost"]   += shares * price

    for ticker, vals in agg.items():
        if vals["shares"] <= 0:
            continue
        holdings[ticker] = {
            "shares":          round(vals["shares"], 6),
            "avg":             round(vals["cost"] / vals["shares"], 4),
            "never_sell_all":  True,  # default safety: always keep a small stub position
        }

    # Write the computed Holdings tab (overwrite, since it's fully derived)
    holdings_ws = _get_or_create_worksheet("Holdings", HOLDINGS_HEADERS)
    if holdings_ws is not None:
        stamp = datetime.datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M %Z")
        try:
            holdings_ws.resize(rows=1)  # clear all rows except header
            holdings_ws.resize(rows=max(len(holdings) + 1, 2))
            body = [[t, h["shares"], h["avg"], "Y", stamp] for t, h in sorted(holdings.items())]
            if body:
                holdings_ws.update(f"A2:E{1 + len(body)}", body, value_input_option="USER_ENTERED")
        except Exception as exc:
            print(f"  [WARNING] Could not write Holdings tab: {exc}")

    print(f"  [HOLDINGS] {len(holdings)} position(s) loaded from Buy Log.")
    return holdings

# -------------------------------------------------------------------
# GOOGLE SHEETS — scan log
# -------------------------------------------------------------------

def log_to_sheet(ticker, name, price, vwap_diff, rsi5, vol_ratio, pl_pct,
                 quick_signal, quick_why, rsi14_daily, ma50, sma200,
                 macd_daily, atr_pct, long_signal, long_why):
    ws = _get_or_create_worksheet("Sheet1", SHEET_HEADERS)
    if ws is None:
        print("  [SHEETS SKIPPED] GOOGLE_CREDENTIALS not set.")
        return
    try:
        timestamp = datetime.datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M %Z")

        def fmt(v, d=2):
            return round(v, d) if v is not None else ""

        ws.append_row([
            timestamp, ticker, name,
            fmt(price, 4), fmt(vwap_diff, 2), fmt(rsi5, 1),
            fmt(vol_ratio, 2), fmt(pl_pct, 1),
            quick_signal, quick_why,
            fmt(rsi14_daily, 1), fmt(ma50, 2), fmt(sma200, 2),
            str(round(macd_daily, 6)) if macd_daily is not None else "",
            fmt(atr_pct, 2),
            long_signal, long_why,
        ], value_input_option="USER_ENTERED")

        print(f"  [SHEETS] {ticker} | Quick: {quick_signal} | Long: {long_signal}")
    except Exception as exc:
        import traceback
        print(f"  [WARNING] Sheets failed: {exc}")
        print(traceback.format_exc())

# -------------------------------------------------------------------
# MARKET TIMING
# -------------------------------------------------------------------

def _est_now():
    return datetime.datetime.now(EASTERN)


def is_market_hours():
    now = _est_now()
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=30, second=0, microsecond=0) <= now <= now.replace(hour=16, minute=0, second=0, microsecond=0)


def in_quick_window():
    now = _est_now()
    return now.replace(hour=9, minute=40, second=0, microsecond=0) <= now <= now.replace(hour=15, minute=55, second=0, microsecond=0)


def get_vwap_thresholds(ticker):
    if ticker in BROAD_ETFS: return -0.6
    if ticker in MEGA_CAPS:  return -0.8
    if ticker in HIGH_BETA:  return -1.2
    return -1.0

# -------------------------------------------------------------------
# SIGNALS
# -------------------------------------------------------------------
# BUY logic runs for every ticker in STOCKS — not just things you hold.
# SELL/trim logic only applies to tickers present in `holdings` (built
# fresh each scan from the Buy Log tab).
#
# Both BUY conditions were originally a strict 3-of-3 AND across narrow
# bands. Logged data showed all three rarely line up at once (<1% of
# scans for the Long signal), so this version requires 2-of-3 instead,
# with the bands kept the same. This alone should produce a meaningfully
# higher signal rate without abandoning the underlying logic.

def generate_quick_signal(ticker, price, vwap_diff_pct, rsi_5, vol_ratio, pl_pct, atr_pct, holdings):
    global daily_spent
    if not in_quick_window() or not is_market_hours():
        return "QUICK HOLD", "Outside quick window"

    cond_rsi  = rsi_5 < 30  # widened from <20: RSI5<20 is extreme and rare
    cond_vwap = vwap_diff_pct < get_vwap_thresholds(ticker)
    cond_vol  = vol_ratio > 1.3  # widened slightly from 1.5
    hits = sum([cond_rsi, cond_vwap, cond_vol])

    if hits >= 2:
        amount = 20 / (2 if atr_pct and atr_pct > 2 else 1)
        if daily_spent + amount > DAILY_RISK_BUDGET:
            return "QUICK HOLD", "Risk budget exceeded"
        daily_spent += amount
        reasons = []
        if cond_rsi:  reasons.append("RSI5 oversold")
        if cond_vwap: reasons.append("VWAP dip")
        if cond_vol:  reasons.append("vol spike")
        return f"QUICK BUY ${amount:.0f}", " + ".join(reasons) + " (2-of-3)"

    if ticker in holdings:
        if pl_pct > 20:
            pct, reason = 0.15, "20%+ quick profit → trim 15%"
        elif pl_pct > 10:
            pct, reason = 0.10, "10%+ quick profit → trim 10%"
        else:
            return "QUICK HOLD", "No quick edge"
        h       = holdings[ticker]
        keep    = 0.05 if h.get("never_sell_all") else 0
        shares  = min(h["shares"] * pct, h["shares"] - h["shares"] * keep)
        dollars = shares * price
        if dollars > 10:
            return f"QUICK SELL ${dollars:,.0f}", reason

    return "QUICK HOLD", "No quick edge"


def generate_long_signal(ticker, price, rsi_14_daily, ma50_pullback_pct,
                         vol_ratio, pl_pct, atr_pct, sma200, holdings):
    global daily_spent
    if not is_market_hours():
        return "LONG HOLD", "Outside long window"

    uptrend = (price > sma200) if sma200 else True
    if not (uptrend or (rsi_14_daily and rsi_14_daily < 25)):
        return "LONG HOLD", "Not in uptrend"

    cond_rsi      = 30 <= (rsi_14_daily or 50) <= 45
    cond_pullback = -6 <= (ma50_pullback_pct or 0) <= -3
    cond_vol      = vol_ratio > 1.2
    hits = sum([cond_rsi, cond_pullback, cond_vol])

    if hits >= 2:
        amount = 100 / (2 if atr_pct and atr_pct > 2 else 1)
        if daily_spent + amount > DAILY_RISK_BUDGET:
            return "LONG HOLD", "Risk budget exceeded"
        daily_spent += amount
        reasons = []
        if cond_rsi:      reasons.append("RSI14 30-45")
        if cond_pullback: reasons.append("MA50 pullback")
        if cond_vol:      reasons.append("vol confirm")
        return f"LONG BUY ${amount:.0f}", " + ".join(reasons) + " (2-of-3)"

    if ticker in holdings:
        if pl_pct > 200:
            pct, reason = 0.50, "200%+ profit → taking half"
        elif pl_pct > 120:
            pct, reason = 0.35, "120%+ profit → trimming 35%"
        elif pl_pct > 70:
            pct, reason = 0.25, "70%+ profit → trimming 25%"
        else:
            return "LONG HOLD", "No long edge"
        h       = holdings[ticker]
        keep    = 0.05 if h.get("never_sell_all") else 0
        shares  = min(h["shares"] * pct, h["shares"] - h["shares"] * keep)
        dollars = shares * price
        if dollars > 30:
            return f"LONG SELL ${dollars:,.0f}", reason

    return "LONG HOLD", "No long edge"

# -------------------------------------------------------------------
# SCAN
# -------------------------------------------------------------------

def run_scan():
    """One full scan pass. Invoked once per GitHub Actions run — the
    cron schedule provides the ~20-minute cadence, so there is no
    in-process loop or sleep-until-market-open here."""
    global daily_spent
    daily_spent = 0
    print(f"\n{'='*60}")
    print(f"  Scan — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    if not is_market_hours():
        print("  Market closed — skipping scan.")
        return

    holdings = load_holdings_from_buy_log()

    # Make sure every held ticker is also scanned, even if it's not on the
    # default watchlist (e.g. you bought something off-list).
    scan_list = list(dict.fromkeys(STOCKS + list(holdings.keys())))

    names: dict = {}
    batch_data = safe_get(
        "https://query2.finance.yahoo.com/v7/finance/quote?symbols=" + ",".join(scan_list)
    )
    if batch_data and batch_data.get("quoteResponse", {}).get("result"):
        for r in batch_data["quoteResponse"]["result"]:
            names[r["symbol"]] = r.get("shortName", r["symbol"])

    for symbol in scan_list:
        try:
            name = names.get(symbol, symbol)

            data = safe_get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=5d")
            if not data or not data.get("chart", {}).get("result"):
                time.sleep(8)
                continue

            res    = data["chart"]["result"][0]
            price  = res["meta"].get("regularMarketPrice") or res["meta"].get("previousClose")
            q      = res["indicators"]["quote"][0]
            ts     = res.get("timestamp", [])
            highs  = q.get("high",   [])
            lows   = q.get("low",    [])
            closes = q.get("close",  [])
            vols   = q.get("volume", [])

            c = [x for x in closes if x is not None]

            vwap          = calculate_vwap(ts, highs, lows, closes, vols)
            vwap_diff_pct = ((price - vwap) / vwap * 100) if vwap and price else 0.0
            rsi_5         = calculate_rsi(c[-30:], 5) if len(c) >= 6 else 50.0
            vol_ratio     = calculate_vol_ratio(ts, vols)

            avg_cost = holdings.get(symbol, {}).get("avg")
            pl_pct   = ((price / avg_cost) - 1) * 100 if (avg_cost and price) else 0

            daily        = fetch_daily_indicators(symbol)
            rsi14_daily  = daily["rsi14"]   if daily else None
            ma50         = daily["ma50"]    if daily else None
            sma200       = daily["sma200"]  if daily else None
            macd_daily   = daily["macd"]    if daily else None
            atr_pct      = daily["atr_pct"] if daily else None
            ma50_pullback = ((price / ma50) - 1) * 100 if ma50 and price else None

            quick_signal, quick_why = generate_quick_signal(
                symbol, price, vwap_diff_pct, rsi_5, vol_ratio, pl_pct, atr_pct, holdings)
            long_signal, long_why   = generate_long_signal(
                symbol, price, rsi14_daily, ma50_pullback, vol_ratio, pl_pct, atr_pct, sma200, holdings)

            rsi14_str = f"{rsi14_daily:.1f}" if rsi14_daily is not None else "N/A"
            print(f"  {symbol:<8} ${price:>10.2f}  VWAP diff: {vwap_diff_pct:+.2f}%"
                  f"  RSI5: {rsi_5:.1f}  RSI14: {rsi14_str}"
                  f"  | {quick_signal} / {long_signal}")

            log_to_sheet(symbol, name, price, vwap_diff_pct, rsi_5, vol_ratio, pl_pct,
                         quick_signal, quick_why, rsi14_daily, ma50, sma200,
                         macd_daily, atr_pct, long_signal, long_why)

            if "BUY" in quick_signal or "SELL" in quick_signal:
                send_push(f"{quick_signal} — {symbol}", f"{quick_why}\n${price:,.2f}")
            if "BUY" in long_signal or "SELL" in long_signal:
                send_push(f"{long_signal} — {symbol}", f"{long_why}\n${price:,.2f}")

            time.sleep(8 + random.uniform(4, 10))

        except Exception as e:
            print(f"  [ERROR] {symbol}: {e}")
            time.sleep(10)

    print("  Scan complete.\n")
