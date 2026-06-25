"""
Congressional Trade Monitor
---------------------------
Tracks new stock trades disclosed by members of the U.S. Senate and House
under the STOCK Act — free, no account/API key required.

House: pulled from a community-maintained JSON mirror of House Clerk PTR
filings (disclosures-clerk.house.gov), refreshed daily.
Senate: scraped directly from efdsearch.senate.gov (the official EFD
search site), since no current free structured feed exists for it.

New disclosures are appended to the "insider trading" tab. A "Trends" tab
is recomputed on every run with per-ticker and per-politician aggregates,
so patterns build up over time instead of just showing one-off lookups.

Same Google Sheet / service-account auth approach as stocks.py.

Env vars required:
  GOOGLE_CREDENTIALS  — Google service account JSON (string)
  GOOGLE_SHEET_ID     — same spreadsheet used by stocks.py

Env vars optional:
  PUSHOVER_USER / PUSHOVER_TOKEN  — one push alert per politician per run,
                                    summarizing all their newly logged trades
  HOUSE_FEED_URL / SENATE_BASE_URL — override the data source if it moves
  CONGRESS_LOOKBACK_DAYS — how far back (by disclosure date) to consider
                           trades "current"; bounds the first-run backfill
                           and how far back the Senate scraper searches.
                           Default 120.
"""

import datetime
import http.client
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
HOUSE_FEED_URL = os.environ.get(
    "HOUSE_FEED_URL",
    "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/main/data/all_transactions.json",
)
SENATE_BASE_URL = os.environ.get("SENATE_BASE_URL", "https://efdsearch.senate.gov")

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

SENATE_HOME_URL   = f"{SENATE_BASE_URL}/search/home/"
SENATE_SEARCH_URL = f"{SENATE_BASE_URL}/search/"
SENATE_DATA_URL   = f"{SENATE_BASE_URL}/search/report/data/"
SENATE_PTR_TYPE   = 11   # "report_types" code for Periodic Transaction Reports
SENATE_PAGE_SIZE  = 100
SENATE_REQUEST_DELAY = 0.3

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

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

def _clean(value):
    if not isinstance(value, str):
        return value
    return _CONTROL_CHARS_RE.sub("", value).strip()


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


def normalize_house(rec):
    return {
        "chamber": "House",
        "politician": _clean(_first(rec, "representative", "name")),
        "owner": _clean(_first(rec, "owner")),
        "ticker": _clean(_first(rec, "ticker")),
        "asset": _clean(_first(rec, "asset_description", "asset")),
        "type": _clean(_first(rec, "type", "transaction_type")),
        "amount": _clean(_first(rec, "amount")),
        "comment": _clean(_first(rec, "comment", "notes")),
        "transaction_date": _first(rec, "transaction_date"),
        "disclosure_date": _first(rec, "disclosure_date"),
        "link": _first(rec, "ptr_link", "link", "source_url"),
    }


def _coerce_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "transactions", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def fetch_house_trades(cutoff):
    raw = safe_get_json(HOUSE_FEED_URL)
    if raw is None:
        print(f"  [WARNING] No data from House feed ({HOUSE_FEED_URL}); skipping.")
        return []

    records = _coerce_list(raw)
    if not records:
        print("  [WARNING] House feed returned no usable records; skipping.")
        return []

    trades, kept, skipped = [], 0, 0
    for rec in records:
        norm = normalize_house(rec)
        if not norm["ticker"] or not norm["type"]:
            skipped += 1
            continue
        disclosure_date = _parse_date(norm["disclosure_date"])
        if disclosure_date and disclosure_date < cutoff:
            continue
        trades.append(norm)
        kept += 1
    print(f"  House: {kept} kept, {skipped} skipped (missing ticker/type), {len(records)} total")
    return trades

# -------------------------------------------------------------------
# SENATE — scraped directly from efdsearch.senate.gov
# -------------------------------------------------------------------

def _senate_session():
    import requests
    from bs4 import BeautifulSoup

    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept": "application/json, text/javascript, */*"})

    r = s.get(SENATE_HOME_URL, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not csrf_input:
        raise RuntimeError("Could not find CSRF token on Senate disclosure search page")

    r = s.post(
        SENATE_HOME_URL,
        data={"csrfmiddlewaretoken": csrf_input["value"], "prohibition_agreement": "1"},
        headers={"Referer": SENATE_HOME_URL},
        timeout=20,
    )
    r.raise_for_status()
    return s


def _senate_filing_index(session, cutoff):
    """Paginate PTR filings submitted since cutoff (newest first)."""
    filings = []
    start = 0
    csrf = session.cookies.get("csrftoken", "")
    submitted_start = cutoff.strftime("%m/%d/%Y 00:00:00")

    while True:
        payload = {
            "start": start,
            "length": SENATE_PAGE_SIZE,
            "report_types": f"[{SENATE_PTR_TYPE}]",
            "filer_types": "[]",
            "submitted_start_date": submitted_start,
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": csrf,
        }
        r = session.post(SENATE_DATA_URL, data=payload, headers={"Referer": SENATE_SEARCH_URL}, timeout=30)
        r.raise_for_status()
        resp = r.json()
        rows = resp.get("data", [])
        if not rows:
            break

        for row in rows:
            first, last, filing_date, link_html = row[0], row[1], row[4], row[3]
            href_match = re.search(r'href="([^"]+)"', link_html)
            if not href_match:
                continue
            href = href_match.group(1)
            filings.append({
                "politician": f"{first} {last}".strip(),
                "filing_date": filing_date,
                "is_pdf": "/view/paper/" in href,
                "url": f"{SENATE_BASE_URL}{href}",
            })

        total = resp.get("recordsFiltered", 0)
        start += SENATE_PAGE_SIZE
        if start >= total:
            break
        time.sleep(SENATE_REQUEST_DELAY)

    return filings


def _parse_senate_report(html, filing):
    from bs4 import BeautifulSoup

    trades = []
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "ptr-table"}) or soup.find("table")
    if not table:
        return trades

    for row in table.find_all("tr")[1:]:
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 8:
            continue
        tx_date, owner, ticker, asset_desc, _asset_type, tx_type, amount = cols[1:8]
        comment = cols[8] if len(cols) > 8 else ""
        if not re.match(r"\d{2}/\d{2}/\d{4}", tx_date):
            continue

        trades.append({
            "chamber": "Senate",
            "politician": filing["politician"],
            "owner": _clean(owner),
            "ticker": "" if ticker == "--" else _clean(ticker),
            "asset": _clean(asset_desc),
            "type": _clean(tx_type.title()),
            "amount": _clean(amount),
            "comment": _clean(comment),
            "transaction_date": tx_date,
            "disclosure_date": filing["filing_date"],
            "link": filing["url"],
        })

    return trades


def fetch_senate_trades(cutoff):
    """Scrape recent Senate PTR filings directly from efdsearch.senate.gov."""
    try:
        session = _senate_session()
        filings = _senate_filing_index(session, cutoff)
    except Exception as e:
        print(f"  [WARNING] Senate filing index failed: {e}")
        return []

    html_filings = [f for f in filings if not f["is_pdf"]]
    skipped_pdf = len(filings) - len(html_filings)

    trades, kept = [], 0
    for filing in html_filings:
        try:
            r = session.get(filing["url"], timeout=20)
            if r.status_code != 200:
                continue
            rows = [t for t in _parse_senate_report(r.text, filing) if t["ticker"] and t["type"]]
            trades.extend(rows)
            kept += len(rows)
            time.sleep(SENATE_REQUEST_DELAY)
        except Exception as e:
            print(f"  [WARNING] Senate filing parse failed ({filing['url']}): {e}")
            continue

    print(
        f"  Senate: {kept} transaction(s) from {len(html_filings)} filing(s) "
        f"({skipped_pdf} PDF filing(s) skipped), since {cutoff.isoformat()}"
    )
    return trades


def fetch_trades():
    """Fetch + normalize trades from House + Senate, bounded by LOOKBACK_DAYS."""
    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    trades = []
    trades.extend(fetch_house_trades(cutoff))
    trades.extend(fetch_senate_trades(cutoff))
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


_PUSH_MAX_LINES = 15  # Pushover messages are capped at 1024 chars; keep it skimmable


def send_trade_notifications(new_trades):
    """One push per politician per run, listing all their newly logged trades."""
    if not new_trades:
        return

    groups = {}
    for t in new_trades:
        groups.setdefault((t["chamber"], t["politician"]), []).append(t)

    for (chamber, politician), trades in groups.items():
        lines = [
            f"{t['type']} {t['ticker'] or t['asset']} | {t['amount']} (filed {t['disclosure_date']})"
            for t in trades
        ]
        shown = lines[:_PUSH_MAX_LINES]
        if len(lines) > _PUSH_MAX_LINES:
            shown.append(f"...and {len(lines) - _PUSH_MAX_LINES} more")

        title = f"{politician} ({chamber}) — {len(trades)} new trade(s)"
        send_push(title, "\n".join(shown))

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

    send_trade_notifications(new_trades)

    print(f"  {len(new_trades)} new trade(s) logged.\n")


if __name__ == "__main__":
    run()
