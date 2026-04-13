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
# HOLDINGS — your cost basis for P/L calculation
# -------------------------------------------------------------------
HOLDINGS = {
    "VOO":   {"shares": 4.33,    "avg": 604.54},
    "QQQ":   {"shares": 0.9284,  "avg": 592.39},
    "VTI":   {"shares": 1.55,    "avg": 323.13},
    "SPY":   {"shares": 0.7624,  "avg": 655.81},
    "VOOG":  {"shares": 1.16,    "avg": 429.40},
    "SCHD":  {"shares": 2.85,    "avg": 27.02},
    "DIA":   {"shares": 0.1718,  "avg": 464.47},
    "GLD":   {"shares": 0.1394,  "avg": 365.43},
    "SMH":   {"shares": 0.1500,  "avg": 333.31},
    "JPM":   {"shares": 0.0862,  "avg": 304.51},
    "XLK":   {"shares": 0.3495,  "avg": 143.03},
    "F":     {"shares": 3.81,    "avg": 13.23},
    "XLV":   {"shares": 0.3469,  "avg": 145.54},
    "NVDA":  {"shares": 0.2455,  "avg": 205.65},
    "META":  {"shares": 0.0791,  "avg": 638.41},
    "VOOV":  {"shares": 0.2473,  "avg": 202.18},
}

CRYPTO_HOLDINGS = {
    "bitcoin":  {"symbol": "BTC", "units": 0.0, "avg": 0.0},
    "ethereum": {"symbol": "ETH", "units": 0.0, "avg": 0.0},
    "solana":   {"symbol": "SOL", "units": 0.0, "avg": 0.0},
}

# -------------------------------------------------------------------
# Tickers to scan
# -------------------------------------------------------------------
SCAN_TICKERS = [
    "VOO", "QQQ", "VTI", "SPY", "VOOG", "SCHD", "DIA",
    "GLD", "SMH", "JPM", "XLK", "F", "XLV", "NVDA", "META", "VOOV",
    # Popular stocks
    "AAPL", "TSLA", "AMZN", "MSFT", "GOOGL", "AMD", "AVGO", "BRK-B",
    "COST", "DIS", "HD", "KO", "LLY", "MA", "MRK", "NFLX", "ORCL",
    "PFE", "PG", "UNH", "V", "WMT", "XOM",
    # Popular ETFs
    "ARKK", "IWF", "IVV", "IVW", "MGK", "OEF", "SCHG", "SPYG",
    "TMFC", "VEA", "VUG", "VWO", "VYM", "XLG",
    # 2025 additions
    "TQQQ", "SOXL", "IJR", "VGT", "VT", "VTV", "VB", "BNDX",
    "TSLL", "NVDL", "QQQM", "BKLC", "FSTA", "SGOL",
    "XLE", "XLF", "XLI", "XLP", "XLU", "XLY",
]

CRYPTO_IDS = [
    "bitcoin",        # BTC
    "ethereum",       # ETH
    "ripple",         # XRP
    "solana",         # SOL
    "binancecoin",    # BNB
    "dogecoin",       # DOGE
    "cardano",        # ADA
    "tron",           # TRX
    "avalanche-2",    # AVAX
    "chainlink",      # LINK
    "shiba-inu",      # SHIB
    "sui",            # SUI
    "stellar",        # XLM
    "polkadot",       # DOT
    "hyperliquid",    # HYPE
    "litecoin",       # LTC
    "uniswap",        # UNI
    "bitcoin-cash",   # BCH
    "pepe",           # PEPE
    "near",           # NEAR
    "aptos",          # APT
    "internet-computer", # ICP
    "official-trump",          # TRUMP
    "world-liberty-financial", # WLFI
]

# -------------------------------------------------------------------
# Pushover
# -------------------------------------------------------------------
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")

def send_push(title, message, priority=0):
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        print(f"⚠️  Pushover not configured: {title}")
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
        print(f"📲 Pushover {'sent' if resp.status == 200 else f'failed {resp.status}'}: {title}")
    except Exception as e:
        print(f"⚠️  Pushover error: {e}")

# -------------------------------------------------------------------
# Google Sheets logger
# Columns: Timestamp | Type | Ticker | Name | Price | Change% |
#          AfterHours% | Volume | VolRatio | 52wkHigh | 52wkLow |
#          PctFrom52wkHigh | PctFrom52wkLow | MarketCap |
#          PE | EPS | HoldingValue | PL% | Signal | SignalReason
# -------------------------------------------------------------------
SHEET_URL = "https://script.google.com/macros/s/AKfycbzGjU2QIiOlEtIHxMnFks1DYXDwuDRwPzun_BXZnLVp2iW8AeV4Up1jT7QMkiJJARHvUA/exec"

def log_row(row: dict):
    params = urllib.parse.urlencode({k: (v if v is not None else "") for k, v in row.items()})
    # Debug: print first row fully to verify all fields are being sent
    if not hasattr(log_row, '_printed_sample'):
        print(f"🔍 Sample row being sent: {json.dumps(row, indent=2)}")
        print(f"🔍 URL params: {params[:500]}")
        log_row._printed_sample = True
    try:
        req = urllib.request.Request(
            SHEET_URL + "?" + params,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"📊 Sheet {resp.status} → {row.get('ticker','?')}: {body[:80]}")
    except urllib.error.HTTPError as e:
        print(f"⚠️  Sheet HTTP {e.code} for {row.get('ticker','?')}: {e.read().decode()[:100]}")
    except Exception as e:
        print(f"⚠️  Sheet error for {row.get('ticker','?')}: {e}")

# -------------------------------------------------------------------
# HTTP helper
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
                return json.loads(r.read().decode())
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
            time.sleep((2 ** attempt) * 2 + random.uniform(1, 3))
    return None

# -------------------------------------------------------------------
# Signal logic (quote-data only, no charts needed)
# -------------------------------------------------------------------
def generate_signal(ticker, price, change_pct, vol_ratio,
                    pct_from_52wk_high, pct_from_52wk_low,
                    afterhours_pct, pl_pct):
    signals = []

    # Near 52-week low — potential buy zone
    if pct_from_52wk_low is not None and pct_from_52wk_low <= 5:
        signals.append(f"⚠️ Near 52wk low (+{pct_from_52wk_low:.1f}%)")

    # Near 52-week high — momentum
    if pct_from_52wk_high is not None and pct_from_52wk_high >= -2:
        signals.append(f"🚀 Near 52wk high ({pct_from_52wk_high:.1f}%)")

    # Volume spike
    if vol_ratio is not None and vol_ratio >= 2.0:
        signals.append(f"📊 Volume spike ({vol_ratio:.1f}x avg)")

    # Big intraday move
    if change_pct is not None:
        if change_pct <= -3:
            signals.append(f"🔴 Big drop ({change_pct:.1f}%)")
        elif change_pct >= 3:
            signals.append(f"🟢 Big gain ({change_pct:.1f}%)")

    # After hours move
    if afterhours_pct is not None:
        if afterhours_pct <= -2:
            signals.append(f"🌙 AH drop ({afterhours_pct:.1f}%)")
        elif afterhours_pct >= 2:
            signals.append(f"🌙 AH spike ({afterhours_pct:.1f}%)")

    # P/L on holdings
    if pl_pct is not None and ticker in HOLDINGS:
        if pl_pct >= 20:
            signals.append(f"💰 Consider trim (up {pl_pct:.1f}%)")
        elif pl_pct <= -10:
            signals.append(f"🔻 Down {pl_pct:.1f}% from avg")

    return " | ".join(signals) if signals else "—"

# -------------------------------------------------------------------
# Stock / ETF scan — single batch call
# -------------------------------------------------------------------
def run_stock_scan():
    print(f"\n📈 Fetching {len(SCAN_TICKERS)} tickers in one batch call...")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    action_lines = []

    symbols = ",".join(SCAN_TICKERS)
    data = safe_get(f"https://query2.finance.yahoo.com/v7/finance/quote?symbols={symbols}")
    if not data:
        print("❌ Batch quote fetch failed")
        return

    results = data.get("quoteResponse", {}).get("result", [])
    print(f"✅ Got {len(results)} quotes\n")

    for q in results:
        try:
            ticker = q.get("symbol", "")
            name   = q.get("shortName", ticker)
            price  = (q.get("regularMarketPrice") or
                      q.get("postMarketPrice") or
                      q.get("preMarketPrice") or
                      q.get("previousClose") or 0)

            change_pct       = q.get("regularMarketChangePercent")
            volume           = q.get("regularMarketVolume")
            avg_volume       = q.get("averageDailyVolume10Day")
            vol_ratio        = round(volume / avg_volume, 2) if volume and avg_volume else None
            week52_high      = q.get("fiftyTwoWeekHigh")
            week52_low       = q.get("fiftyTwoWeekLow")
            pct_from_high    = round((price / week52_high - 1) * 100, 1) if week52_high else None
            pct_from_low     = round((price / week52_low  - 1) * 100, 1) if week52_low  else None
            market_cap       = q.get("marketCap")
            pe_ratio         = q.get("trailingPE")
            eps              = q.get("epsTrailingTwelveMonths")
            post_price       = q.get("postMarketPrice")
            afterhours_pct   = round((post_price / price - 1) * 100, 2) if post_price and price else None

            holding    = HOLDINGS.get(ticker)
            pl_pct     = round((price / holding["avg"] - 1) * 100, 1) if holding else None
            hold_value = round(holding["shares"] * price, 2)           if holding else None

            signal = generate_signal(ticker, price, change_pct, vol_ratio,
                                     pct_from_high, pct_from_low,
                                     afterhours_pct, pl_pct)

            row = {
                "timestamp":        now_str,
                "type":             "STOCK/ETF",
                "ticker":           ticker,
                "name":             name,
                "price":            round(price, 2),
                "change_pct":       round(change_pct, 2)  if change_pct  is not None else "",
                "afterhours_pct":   afterhours_pct         if afterhours_pct is not None else "",
                "volume":           volume                  if volume      is not None else "",
                "vol_ratio":        vol_ratio               if vol_ratio   is not None else "",
                "week52_high":      week52_high             if week52_high is not None else "",
                "week52_low":       week52_low              if week52_low  is not None else "",
                "pct_from_52wk_high": pct_from_high        if pct_from_high is not None else "",
                "pct_from_52wk_low":  pct_from_low         if pct_from_low  is not None else "",
                "market_cap":       market_cap              if market_cap  is not None else "",
                "pe_ratio":         round(pe_ratio, 1)      if pe_ratio    is not None else "",
                "eps":              round(eps, 2)            if eps         is not None else "",
                "holding_value":    hold_value              if hold_value  is not None else "",
                "pl_pct":           pl_pct                  if pl_pct      is not None else "",
                "signal":           signal,
            }

            log_row(row)

            if signal != "—":
                action_lines.append(f"{ticker} @ ${price:,.2f}\n{signal}")
                print(f"🚨 {ticker}: {signal}")

        except Exception as e:
            print(f"Error on {ticker}: {e}")

    if action_lines:
        send_push("📈 Stock Signals", "\n\n".join(action_lines), priority=1)
    else:
        print("✅ No actionable stock signals this scan")

    print("\n📈 Stock scan complete.\n")

# -------------------------------------------------------------------
# Crypto scan — two batch calls (prices + market data)
# -------------------------------------------------------------------
def run_crypto_scan():
    print("\n₿ Fetching crypto in batch...\n")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    action_lines = []

    ids = ",".join(CRYPTO_IDS)
    url = (f"https://api.coingecko.com/api/v3/coins/markets"
           f"?vs_currency=usd&ids={ids}"
           f"&order=market_cap_desc"
           f"&price_change_percentage=24h,7d")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            coins = json.loads(r.read().decode())
        print(f"✅ Got {len(coins)} crypto quotes")
    except Exception as e:
        print(f"⚠️  CoinGecko fetch failed: {e}")
        return

    for coin in coins:
        try:
            coin_id    = coin.get("id", "")
            symbol     = coin.get("symbol", "").upper()
            name       = coin.get("name", symbol)
            price      = coin.get("current_price", 0)
            change_24h = coin.get("price_change_percentage_24h")
            change_7d  = coin.get("price_change_percentage_7d_in_currency")
            market_cap = coin.get("market_cap")
            volume_24h = coin.get("total_volume")
            high_24h   = coin.get("high_24h")
            low_24h    = coin.get("low_24h")
            ath        = coin.get("ath")
            pct_from_ath = coin.get("ath_change_percentage")

            holding  = CRYPTO_HOLDINGS.get(coin_id, {})
            avg_cost = holding.get("avg", 0)
            units    = holding.get("units", 0)
            pl_pct   = round((price / avg_cost - 1) * 100, 1) if avg_cost > 0 else None
            hold_val = round(units * price, 2) if units > 0 else None

            # Crypto signals
            signals = []
            if change_24h is not None and change_24h <= -5:
                signals.append(f"🔴 24h drop ({change_24h:.1f}%)")
            if change_24h is not None and change_24h >= 5:
                signals.append(f"🟢 24h surge ({change_24h:.1f}%)")
            if change_7d is not None and change_7d <= -15:
                signals.append(f"📉 7d down ({change_7d:.1f}%)")
            if pct_from_ath is not None and pct_from_ath >= -5:
                signals.append(f"🚀 Near ATH ({pct_from_ath:.1f}%)")
            if pl_pct is not None and pl_pct >= 50:
                signals.append(f"💰 Consider trim (up {pl_pct:.1f}%)")
            signal = " | ".join(signals) if signals else "—"

            row = {
                "timestamp":      now_str,
                "type":           "CRYPTO",
                "ticker":         symbol,
                "name":           name,
                "price":          round(price, 2),
                "change_pct":     round(change_24h, 2) if change_24h is not None else "",
                "change_7d_pct":  round(change_7d, 2)  if change_7d  is not None else "",
                "volume":         volume_24h             if volume_24h is not None else "",
                "market_cap":     market_cap             if market_cap is not None else "",
                "high_24h":       high_24h               if high_24h   is not None else "",
                "low_24h":        low_24h                if low_24h    is not None else "",
                "ath":            ath                    if ath        is not None else "",
                "pct_from_ath":   round(pct_from_ath, 1) if pct_from_ath is not None else "",
                "holding_value":  hold_val               if hold_val   is not None else "",
                "pl_pct":         pl_pct                 if pl_pct     is not None else "",
                "signal":         signal,
            }

            log_row(row)

            if signal != "—":
                action_lines.append(f"{symbol} @ ${price:,.2f}\n{signal}")
                print(f"🚨 {symbol}: {signal}")

        except Exception as e:
            print(f"Error on {coin_id}: {e}")

    if action_lines:
        send_push("₿ Crypto Signals", "\n\n".join(action_lines), priority=1)
    else:
        print("✅ No actionable crypto signals this scan")

    print("\n₿ Crypto scan complete.\n")

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    print(f"🚀 Scan started at {datetime.datetime.now()}")
    run_stock_scan()
    run_crypto_scan()
    print(f"✅ All scans complete at {datetime.datetime.now()}")
