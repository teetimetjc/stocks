"""
Congressional Trade Monitor
---------------------------
Tracks new stock trades disclosed by members of the U.S. Senate and House
under the STOCK Act, plus periodic transaction reports filed by the
President and Vice President — free, no account/API key required.

House: pulled from a community-maintained JSON mirror of House Clerk PTR
filings (disclosures-clerk.house.gov), refreshed daily.
Senate: scraped directly from efdsearch.senate.gov (the official EFD
search site), since no current free structured feed exists for it.
Executive (President/VP): scraped from OGE's public Domino filing index
(extapps2.oge.gov), filtered to just the President and VP, with their
OGE Form 278-T PDFs parsed for transaction tables. This source is more
brittle than House/Senate — it's an unofficial scrape of a legacy system
with PDF-only filings — so failures here are logged and skipped rather
than breaking the rest of the run.

New disclosures are appended to the "insider trading" tab. For every
ticker with a disclosed trade, the closing price near the disclosure
date is recorded, then refreshed daily (current price + running peak +
days from trade to peak) for PRICE_TRACKING_DAYS after disclosure, using
Yahoo Finance's free chart endpoint (same approach as stocks.py).

A "Trends" tab is recomputed on every run with per-ticker and
per-politician aggregates, so patterns build up over time instead of
just showing one-off lookups.

Same Google Sheet / service-account auth approach as stocks.py.

Env vars required:
  GOOGLE_CREDENTIALS  — Google service account JSON (string)
  GOOGLE_SHEET_ID     — same spreadsheet used by stocks.py

Env vars optional:
  PUSHOVER_USER / PUSHOVER_TOKEN  — one push alert per politician per run,
                                    summarizing all their newly logged trades
  HOUSE_FEED_URL / SENATE_BASE_URL / OGE_INDEX_URL — override a data
                                    source if it moves
  CONGRESS_LOOKBACK_DAYS — how far back (by disclosure date) to consider
                           trades "current"; bounds the first-run backfill
                           and how far back the Senate/OGE scrapers search.
                           Default 120.
  PRICE_TRACKING_DAYS   — how many days after disclosure to keep refreshing
                          current/peak price for a trade. Default 90.
"""

import datetime
import http.client
import io
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
OGE_INDEX_URL = os.environ.get(
    "OGE_INDEX_URL",
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView&Count=10000",
)

PUSHOVER_USER      = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_TOKEN     = os.environ.get("PUSHOVER_TOKEN", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GOOGLE_SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "")
LOOKBACK_DAYS      = int(os.environ.get("CONGRESS_LOOKBACK_DAYS", "120"))
PRICE_TRACKING_DAYS = int(os.environ.get("PRICE_TRACKING_DAYS", "90"))

TRADES_TAB = "insider trading"
TRENDS_TAB = "Trends"

TRADES_HEADERS = [
    "Logged At", "Chamber", "Politician", "Owner", "Ticker", "Asset",
    "Type", "Amount Range", "Transaction Date", "Disclosure Date",
    "Days To Disclose", "Comment", "Source Link", "Key",
    "Price At Disclosure", "Current Price", "Peak Price", "Peak Date",
    "Days To Peak", "Price Last Updated",
]

_UA = "Mozilla/5.0 (compatible; congress-trade-monitor/1.0)"
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

SENATE_HOME_URL   = f"{SENATE_BASE_URL}/search/home/"
SENATE_SEARCH_URL = f"{SENATE_BASE_URL}/search/"
SENATE_DATA_URL   = f"{SENATE_BASE_URL}/search/report/data/"
SENATE_PTR_TYPE   = 11   # "report_types" code for Periodic Transaction Reports
SENATE_PAGE_SIZE  = 100
SENATE_REQUEST_DELAY = 0.3

# Only the President and VP — OGE's filer index covers ~1,600 appointees,
# but we deliberately scope the scrape to these two to keep the daily
# PDF-parsing workload small and reliable.
TRACKED_EXEC_NAMES = ("trump", "vance")

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


def fetch_yahoo_json(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": random.choice(_UA_POOL), "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            wait = (2 ** attempt) * 10 + random.uniform(5, 15) if e.code == 429 else (2 ** attempt) * 3 + random.uniform(1, 3)
            if attempt == retries - 1:
                print(f"  [WARNING] Yahoo Finance fetch failed ({e.code}) for {url}")
                return None
            time.sleep(wait)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [WARNING] Yahoo Finance fetch failed for {url}: {e}")
                return None
            time.sleep((2 ** attempt) * 3 + random.uniform(1, 3))
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


def _col_letter(idx):
    """1-based column index -> spreadsheet column letter(s)."""
    letters = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


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

# -------------------------------------------------------------------
# EXECUTIVE BRANCH (President / VP) — scraped from OGE's public filing
# index + their OGE Form 278-T PDFs. Best-effort: this is an unofficial
# scrape of a legacy system, so any failure here is logged and skipped
# rather than breaking House/Senate processing.
# -------------------------------------------------------------------

def _is_tracked_filer(name):
    n = (name or "").lower()
    return any(key in n for key in TRACKED_EXEC_NAMES)


def _parse_oge_index(html):
    """Pull (filer, filing_date, pdf_url) out of OGE's Domino filing index.

    Domino exposes attachments as .../$FILE/<filename>.pdf, and OGE's PTR
    filenames consistently follow "Last, First-MM.DD.YYYY-278T...pdf", so
    we derive filer + date from the filename rather than relying on the
    surrounding table markup (which Domino views render inconsistently).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    filings = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/$FILE/" not in href or not href.lower().endswith(".pdf"):
            continue
        filename = urllib.parse.unquote(href.rsplit("/$FILE/", 1)[-1])
        m = re.match(r"^([^,]+),\s*([^\-]+)-(\d{2}\.\d{2}\.\d{4})-", filename)
        if not m:
            continue
        last, first, date_str = m.groups()
        try:
            filing_date = datetime.datetime.strptime(date_str, "%m.%d.%Y").date()
        except ValueError:
            continue
        filings.append({
            "filer": f"{first.strip()} {last.strip()}",
            "filing_date": filing_date,
            "url": urllib.parse.urljoin(OGE_INDEX_URL, href),
        })
    return filings


def _parse_278t_pdf(pdf_bytes, filer, filing_date, source_url):
    import pdfplumber

    trades = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                if not table or len(table) < 2:
                    continue
                header = [(_clean(c) or "").lower() for c in table[0]]
                if not any("asset" in h for h in header) or not any("type" in h for h in header):
                    continue

                col = {}
                for i, h in enumerate(header):
                    if "owner" in h:
                        col["owner"] = i
                    elif "asset" in h:
                        col["asset"] = i
                    elif "type" in h:
                        col["type"] = i
                    elif "date" in h and "date" not in col:
                        col["date"] = i
                    elif "amount" in h:
                        col["amount"] = i
                if "asset" not in col or "type" not in col:
                    continue

                for row in table[1:]:
                    if not row or len(row) <= max(col.values()):
                        continue
                    asset = _clean(row[col["asset"]] or "")
                    ttype = _clean(row[col["type"]] or "")
                    if not asset or not ttype:
                        continue

                    ticker_match = re.search(r"\(([A-Z]{1,5})\)", asset)
                    trades.append({
                        "chamber": "Executive",
                        "politician": filer,
                        "owner": _clean(row[col["owner"]]) if "owner" in col else "Self",
                        "ticker": ticker_match.group(1) if ticker_match else "",
                        "asset": asset,
                        "type": ttype.title(),
                        "amount": _clean(row[col["amount"]]) if "amount" in col else "",
                        "comment": "",
                        "transaction_date": _clean(row[col["date"]]) if "date" in col else "",
                        "disclosure_date": filing_date.strftime("%m/%d/%Y"),
                        "link": source_url,
                    })
    return trades


def fetch_executive_trades(cutoff):
    """Scrape new President/VP periodic transaction reports from OGE."""
    try:
        import requests
    except ImportError:
        print("  [WARNING] 'requests' not available; skipping executive branch trades.")
        return []

    try:
        resp = requests.get(OGE_INDEX_URL, headers={"User-Agent": _UA}, timeout=30)
        resp.raise_for_status()
        filings = _parse_oge_index(resp.text)
    except Exception as e:
        print(f"  [WARNING] Executive branch (OGE) index fetch/parse failed: {e}")
        return []

    tracked = [f for f in filings if _is_tracked_filer(f["filer"]) and f["filing_date"] >= cutoff]

    trades = []
    for filing in tracked:
        try:
            pdf_resp = requests.get(filing["url"], headers={"User-Agent": _UA}, timeout=30)
            pdf_resp.raise_for_status()
            trades.extend(_parse_278t_pdf(pdf_resp.content, filing["filer"], filing["filing_date"], filing["url"]))
            time.sleep(SENATE_REQUEST_DELAY)
        except Exception as e:
            print(f"  [WARNING] Executive branch filing parse failed ({filing['url']}): {e}")
            continue

    kept = [t for t in trades if t["ticker"] and t["type"]]
    print(
        f"  Executive branch: {len(kept)} transaction(s) from {len(tracked)} filing(s) "
        f"(President/VP), since {cutoff.isoformat()}"
    )
    return kept


def fetch_trades():
    """Fetch + normalize trades from House + Senate + Executive, bounded by LOOKBACK_DAYS."""
    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    trades = []
    trades.extend(fetch_house_trades(cutoff))
    trades.extend(fetch_senate_trades(cutoff))
    trades.extend(fetch_executive_trades(cutoff))
    return trades


def make_key(t):
    return "|".join([
        t["chamber"], t["politician"], t["owner"], t["ticker"],
        t["type"], t["amount"], t["transaction_date"], t["asset"],
    ])

# -------------------------------------------------------------------
# PRICE TRACKING — Yahoo Finance's free chart endpoint (same source as
# stocks.py). Price history per ticker is cached for the life of one run
# since multiple trades/rows often share a ticker.
# -------------------------------------------------------------------

_price_history_cache = {}


def get_price_history(ticker):
    if ticker in _price_history_cache:
        return _price_history_cache[ticker]

    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?interval=1d&range=2y"
    data = fetch_yahoo_json(url)
    result = None
    try:
        chart_result = data["chart"]["result"][0]
        timestamps = chart_result["timestamp"]
        closes = chart_result["indicators"]["quote"][0]["close"]
        result = (timestamps, closes)
    except (TypeError, KeyError, IndexError):
        result = None

    _price_history_cache[ticker] = result
    return result


def price_on_or_before(ticker, target_date):
    history = get_price_history(ticker)
    if not history:
        return None
    timestamps, closes = history
    target_ts = time.mktime(target_date.timetuple())

    best = None
    for ts, close in zip(timestamps, closes):
        if close is None or ts > target_ts:
            continue
        if best is None or ts > best[0]:
            best = (ts, close)
    return round(best[1], 2) if best else None


def current_price(ticker):
    history = get_price_history(ticker)
    if not history:
        return None
    _timestamps, closes = history
    for close in reversed(closes):
        if close is not None:
            return round(close, 2)
    return None

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
        ws.insert_row(headers, index=1)
        return ws

    existing_header = ws.row_values(1)
    if not existing_header:
        ws.insert_row(headers, index=1)
    elif len(existing_header) < len(headers):
        # Sheet was created before some headers (e.g. price tracking) existed —
        # extend the header row in place without touching existing data.
        new_cols = headers[len(existing_header):]
        start_col = _col_letter(len(existing_header) + 1)
        end_col = _col_letter(len(headers))
        ws.update(f"{start_col}1:{end_col}1", [new_cols], value_input_option="USER_ENTERED")
    return ws


def log_new_trades(trades):
    if not GOOGLE_CREDENTIALS:
        print("  [SHEETS SKIPPED] GOOGLE_CREDENTIALS not set.")
        return []
    try:
        spreadsheet = _open_sheet()
        ws = _get_or_create_worksheet(spreadsheet, TRADES_TAB, TRADES_HEADERS)

        existing_keys = set(ws.col_values(TRADES_HEADERS.index("Key") + 1))
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
                "", "", "", "", "", "",  # price tracking columns, filled in by update_price_tracking()
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


def update_price_tracking():
    """Initialize price-at-disclosure for new rows, and refresh current/peak
    price for any row still within PRICE_TRACKING_DAYS of its disclosure date.
    Uses a single batch_update so cost stays flat regardless of sheet size.
    """
    if not GOOGLE_CREDENTIALS:
        return
    try:
        spreadsheet = _open_sheet()
        ws = _get_or_create_worksheet(spreadsheet, TRADES_TAB, TRADES_HEADERS)
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            return

        header = all_values[0]
        col = {name: header.index(name) for name in TRADES_HEADERS}

        today = datetime.date.today()
        cutoff = today - datetime.timedelta(days=PRICE_TRACKING_DAYS)

        updates = []
        touched_tickers = set()
        for row_idx, raw_row in enumerate(all_values[1:], start=2):
            row = raw_row + [""] * (len(header) - len(raw_row))

            ticker = row[col["Ticker"]].strip()
            if not ticker:
                continue
            disclosure_date = _parse_date(row[col["Disclosure Date"]])
            if not disclosure_date or disclosure_date < cutoff:
                continue

            price_at_disclosure = row[col["Price At Disclosure"]]
            peak_price_str = row[col["Peak Price"]]
            peak_date_str = row[col["Peak Date"]]

            touched_tickers.add(ticker)

            if not price_at_disclosure:
                baseline = price_on_or_before(ticker, disclosure_date)
                if baseline is None:
                    continue
                cur = current_price(ticker)
                cur = cur if cur is not None else baseline
                if cur > baseline:
                    peak, peak_date = cur, today
                else:
                    peak, peak_date = baseline, disclosure_date
                days_to_peak = (peak_date - disclosure_date).days
                values = [baseline, cur, peak, peak_date.isoformat(), days_to_peak, today.isoformat()]
            else:
                cur = current_price(ticker)
                if cur is None:
                    continue
                try:
                    peak = float(peak_price_str) if peak_price_str else float(price_at_disclosure)
                except ValueError:
                    peak = cur
                peak_date = _parse_date(peak_date_str) or disclosure_date
                if cur > peak:
                    peak, peak_date = cur, today
                days_to_peak = (peak_date - disclosure_date).days
                values = [price_at_disclosure, cur, round(peak, 2), peak_date.isoformat(), days_to_peak, today.isoformat()]

            start_col = _col_letter(col["Price At Disclosure"] + 1)
            end_col = _col_letter(col["Price Last Updated"] + 1)
            updates.append({"range": f"{start_col}{row_idx}:{end_col}{row_idx}", "values": [values]})

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            print(f"  [SHEETS] Price tracking updated for {len(updates)} row(s) across {len(touched_tickers)} ticker(s)")
        else:
            print("  [SHEETS] No rows due for price tracking update.")
    except Exception as exc:
        import traceback
        print(f"  [WARNING] Price tracking update failed: {exc}")
        print(traceback.format_exc())


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
    update_price_tracking()
    update_trends()

    send_trade_notifications(new_trades)

    print(f"  {len(new_trades)} new trade(s) logged.\n")


if __name__ == "__main__":
    run()
