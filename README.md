# stocks
Looking at the stock market and finding things to invest in.

Also tracks congressional stock trade disclosures (STOCK Act) — see
`congress_trades.py`. New Senate/House trades are logged to the
"insider trading" tab, and a "Trends" tab tracks per-ticker and
per-politician patterns over time. Runs daily via
`.github/workflows/congress_trades.yml`, reusing the same Google Sheet
and service-account credentials as `stocks.py`.
