import json
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; probe/1.0)"

CANDIDATES = [
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions_qol.json",
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/raw/transaction_report_data.json",
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/raw_data/all_transactions.json",
    "https://bff.capitoltrades.com/trades?page=1&pageSize=5",
    "https://www.capitoltrades.com/api/trades?page=1&pageSize=5",
    "https://api.capitoltrades.com/trades?page=1&pageSize=5",
]

for url in CANDIDATES:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(500)
            print(f"OK {resp.status} {url}\n  {body[:300]!r}\n")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {url}")
    except Exception as e:
        print(f"ERR {type(e).__name__} {url} ({e})")

print("\n--- github search for current mirrors ---")
for q in ["senate-stock-watcher-data", "house-stock-watcher-data", "congress trading json api"]:
    url = "https://api.github.com/search/repositories?q=" + urllib.parse.quote(q) + "&per_page=5"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            names = [item["full_name"] for item in data.get("items", [])]
            print(f"query={q!r} -> {names}")
    except Exception as e:
        print(f"query={q!r} ERR {e}")
