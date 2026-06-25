"""
Congressional Trade Monitor
---------------------------
Tracks new stock trades disclosed by members of the U.S. Senate and House
under the STOCK Act, using the free Senate/House Stock Watcher JSON feeds.

New disclosures are appended to the "insider trading" tab. A "Trends" tab
is recomputed on every run with per-ticker and per-politician aggregates,
so patterns build up over time instead of just showing one-off lookups.

Same Google Sheet / service-account auth approach as stocks.py.

Env vars required:
  GOOGLE_CREDENTIALS  — Google service account JSON (string)
  GOOGLE_SHEET_ID     — same spreadsheet used by stocks.py

Env vars optional:
  PUSHOVER_USER / PUSHOVER_TOKEN  — push alert for each newly logged trade
  SENATE_FEED_URL / HOUSE_FEED_URL — override the data source if it moves
  CONGRESS_LOOKBACK_DAYS — how far back (by disclosure date) to consider
                           trades "current"; bounds the first-run backfill.
                           Default 120.
"""

import datetime
import http.client
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
SENATE_FEED_URL = os.environ.get(
    "SENATE_FEED_URL",
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
)
HOUSE_FEED_URL = os.environ.get(
    "HOUSE_FEED_URL",
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions_qol.json",
)

PUSHOVER_USER      = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_TOKEN     = os.environ.get("PUSHOVER_TOKEN", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GOOGLE_SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "")
LOOKBACK_DAYS      = int(os.environ.get("CONGRESS_LOOKBACK_DAYS", "120"))

TRADES_TAB = "insider trading"
TRENDS_TAB = "Trends"

TRADES_HEADERS = [
    "Logged At", "Chamber", "Politician", "Owner", "Ticker", "Asset",
    "Type", "Amount Range", "Transaction Date", "Disclosure Date",
    "Days To Disclose", "Comment", "Source Link", "Key",
]

_UA = "Mozilla/5.0 (compatible; congress-trade-monitor/1.0)"

# -------------------------------------------------------------------
# HTTP
# -------------------------------------------------------------------

def safe_get_json(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [ERROR] Failed to fetch {url}: {e}")
                return None
            wait = (2 ** attempt) * 5 + random.uniform(1, 5)
            print(f"  Retry {attempt + 1}/{retries} for {url} in {wait:.1f}s ({e})")
            time.sleep(wait)
    return None

# -------------------------------------------------------------------
# NORMALIZATION
# -------------------------------------------------------------------

def _parse_date(value):
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(value.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _first(rec, *keys):
    for k in keys:
        v = rec.get(k)
        if v:
            return v
    return ""


def normalize_senate(rec):
    return {
        "chamber": "Senate",
        "politician": _first(rec, "senator", "name"),
        "owner": _first(rec, "owner"),
        "ticker": _first(rec, "ticker"),
        "asset": _first(rec, "asset_description", "asset"),
        "type": _first(rec, "type", "transaction_type"),
        "amount": _first(rec, "amount"),
        "comment": _first(rec, "comment"),
        "transaction_date": _first(rec, "transaction_date"),
        "disclosure_date": _first(rec, "disclosure_date"),
        "link": _first(rec, "ptr_link", "link"),
    }


def normalize_house(rec):
    return {
        "chamber": "House",
        "politician": _first(rec, "representative", "name"),
        "owner": _first(rec, "owner"),
        "ticker": _first(rec, "ticker"),
        "asset": _first(rec, "asset_description", "asset"),
        "type": _first(rec, "type", "transaction_type"),
        "amount": _first(rec, "amount"),
        "comment": _first(rec, "comment", "notes"),
        "transaction_date": _first(rec, "transaction_date"),
        "disclosure_date": _first(rec, "disclosure_date"),
        "link": _first(rec, "ptr_link", "link"),
    }


def _coerce_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "transactions", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def fetch_trades():
    """Fetch + normalize trades from both feeds, bounded by LOOKBACK_DAYS."""
    trades = []
    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)

    for url, normalizer, label in (
        (SENATE_FEED_URL, normalize_senate, "Senate"),
        (HOUSE_FEED_URL, normalize_house, "House"),
    ):
        raw = safe_get_json(url)
        if raw is None:
            print(f"  [WARNING] No data from {label} feed ({url}); skipping.")
            continue

        records = _coerce_list(raw)
        if not records:
            print(f"  [WARNING] {label} feed returned no usable records; skipping.")
            continue

        kept = skipped = 0
        for rec in records:
            norm = normalizer(rec)
            if not norm["ticker"] or not norm["type"]:
                skipped += 1
                continue
            disclosure_date = _parse_date(norm["disclosure_date"])
            if disclosure_date and disclosure_date < cutoff:
                continue
            trades.append(norm)
            kept += 1
        print(f"  {label}: {kept} kept, {skipped} skipped (missing ticker/type), {len(records)} total")

    return trades


def make_key(t):
    return "|".join([
        t["chamber"], t["politician"], t["owner"], t["ticker"],
        t["type"], t["amount"], t["transaction_date"], t["asset"],
    ])

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
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "title": title,
                "message": message,
                "sound": "cashregister",
            }),
            {"Content-type": "application/x-www-form-urlencoded"},
        )
        conn.getresponse()
    except Exception:
        pass

# -------------------------------------------------------------------
# GOOGLE SHEETS
# -------------------------------------------------------------------

def _open_sheet():
    import gspread
    gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDENTIALS))
    return gc.open_by_key(GOOGLE_SHEET_ID)


def _get_or_create_worksheet(spreadsheet, title, headers):
    import gspread
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(headers) + 2)
    if ws.cell(1, 1).value != headers[0]:
        ws.insert_row(headers, index=1)
    return ws


def log_new_trades(trades):
    if not GOOGLE_CREDENTIALS:
        print("  [SHEETS SKIPPED] GOOGLE_CREDENTIALS not set.")
        return []
    try:
        spreadsheet = _open_sheet()
        ws = _get_or_create_worksheet(spreadsheet, TRADES_TAB, TRADES_HEADERS)

        existing_keys = set(ws.col_values(len(TRADES_HEADERS)))  # "Key" column
        now_est = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=-5)))
        timestamp = now_est.strftime("%Y-%m-%d %H:%M EST")

        rows, new_trades = [], []
        for t in trades:
            key = make_key(t)
            if key in existing_keys:
                continue
            existing_keys.add(key)

            tx_date = _parse_date(t["transaction_date"])
            disc_date = _parse_date(t["disclosure_date"])
            days_to_disclose = (disc_date - tx_date).days if tx_date and disc_date else ""

            rows.append([
                timestamp, t["chamber"], t["politician"], t["owner"], t["ticker"],
                t["asset"], t["type"], t["amount"], t["transaction_date"],
                t["disclosure_date"], days_to_disclose, t["comment"], t["link"], key,
            ])
            new_trades.append(t)

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            print(f"  [SHEETS] Logged {len(rows)} new trade(s) to '{TRADES_TAB}'")
        else:
            print("  [SHEETS] No new trades.")

        return new_trades
    except Exception as exc:
        import traceback
        print(f"  [WARNING] Sheets failed: {exc}")
        print(traceback.format_exc())
        return []


def update_trends():
    if not GOOGLE_CREDENTIALS:
        return
    try:
        spreadsheet = _open_sheet()
        trades_ws = spreadsheet.worksheet(TRADES_TAB)
        records = trades_ws.get_all_records()

        by_ticker, by_politician = {}, {}
        for r in records:
            ticker = r.get("Ticker") or "?"
            pol = r.get("Politician") or "?"
            ttype = (r.get("Type") or "").lower()
            is_buy = "purchase" in ttype or "buy" in ttype
            is_sell = "sale" in ttype or "sell" in ttype

            bt = by_ticker.setdefault(ticker, {"total": 0, "buys": 0, "sells": 0, "politicians": set()})
            bt["total"] += 1
            bt["buys"] += int(is_buy)
            bt["sells"] += int(is_sell)
            bt["politicians"].add(pol)

            bp = by_politician.setdefault(pol, {"total": 0, "buys": 0, "sells": 0, "tickers": set()})
            bp["total"] += 1
            bp["buys"] += int(is_buy)
            bp["sells"] += int(is_sell)
            bp["tickers"].add(ticker)

        top_tickers = sorted(by_ticker.items(), key=lambda kv: -kv[1]["total"])[:25]
        top_politicians = sorted(by_politician.items(), key=lambda kv: -kv[1]["total"])[:25]

        now_est = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=-5)))
        timestamp = now_est.strftime("%Y-%m-%d %H:%M EST")

        values = [[f"Updated {timestamp} — {len(records)} trades analyzed"], []]
        values.append(["Top Tickers", "Total Trades", "Buys", "Sells", "Unique Politicians"])
        for ticker, d in top_tickers:
            values.append([ticker, d["total"], d["buys"], d["sells"], len(d["politicians"])])
        values.append([])
        values.append(["Top Politicians", "Total Trades", "Buys", "Sells", "Unique Tickers"])
        for pol, d in top_politicians:
            values.append([pol, d["total"], d["buys"], d["sells"], len(d["tickers"])])

        try:
            ws = spreadsheet.worksheet(TRENDS_TAB)
            ws.clear()
        except Exception:
            ws = spreadsheet.add_worksheet(title=TRENDS_TAB, rows=max(len(values) + 10, 100), cols=6)

        ws.update(values, value_input_option="USER_ENTERED")
        print(f"  [SHEETS] '{TRENDS_TAB}' updated ({len(records)} total trades analyzed)")
    except Exception as exc:
        import traceback
        print(f"  [WARNING] Trends update failed: {exc}")
        print(traceback.format_exc())

# -------------------------------------------------------------------
# ENTRY POINT
# -------------------------------------------------------------------

def run():
    print(f"\n{'=' * 60}")
    print(f"  Congressional trade scan — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    trades = fetch_trades()
    print(f"  {len(trades)} trade(s) within {LOOKBACK_DAYS}-day disclosure lookback window")

    new_trades = log_new_trades(trades)
    update_trends()

    for t in new_trades:
        send_push(
            f"{t['type']} — {t['ticker'] or t['asset']}",
            f"{t['politician']} ({t['chamber']}) | {t['amount']}\nFiled {t['disclosure_date']}",
        )

    print(f"  {len(new_trades)} new trade(s) logged.\n")


if __name__ == "__main__":
    run()
