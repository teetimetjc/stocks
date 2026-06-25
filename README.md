# stocks
Looking at the stock market and finding things to invest in.

Also tracks congressional stock trade disclosures (STOCK Act) — see
`congress_trades.py`. House trades come from a free, community-maintained
JSON mirror of House Clerk PTR filings; Senate trades are scraped
directly from efdsearch.senate.gov (no account or API key needed for
either). New trades are logged to the "insider trading" tab, and a
"Trends" tab tracks per-ticker and per-politician patterns over time.
Runs daily via `.github/workflows/congress_trades.yml`, reusing the same
Google Sheet and service-account credentials as `stocks.py`.
