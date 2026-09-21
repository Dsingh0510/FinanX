# FinanX Version 1 — Own Backend

This version keeps the existing FinanX UI/UX and replaces the Upstox dependency with an automatic public-data market layer.

## What it does
- Keeps the existing planner and learning UI.
- Automatically collects publicly available market data in the background.
- Stores market ticks/snapshots and calculated metrics in `finanx.db`.
- Uses user inputs + current available market metrics to rank asset categories and create an illustrative diversified plan.
- No Upstox account or token is required.

## Data-source note
The current collector uses `yfinance`/Yahoo Finance public data for a prototype. Yahoo currently labels many NSE quotes as delayed, so the project must not call this an exchange-licensed real-time feed. The backend is *auto-updating* and source/freshness is shown in the UI.

## Run
```bat
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```
Open http://127.0.0.1:5000

The collector starts automatically when the app starts. To force an immediate refresh:
POST /api/market/refresh

## Next data integrations
- AMFI daily mutual-fund NAV import
- Official bank FD-rate registry/update job
- India-specific bond market source
- A permitted Indian derivatives source for NSE/BSE F&O

These sources are separate because each product has different publication/data rules.

## Important
FinanX is an educational decision-support prototype. Scores and allocations are illustrative and not guaranteed returns or personalized investment advice.

## UI/analysis update
The plan result now shows an explicit investment breakup with invested amount, weight, estimated annual return, projected value and projected gain. The analysis list shows 1-year (YoY-style) return where available, data/estimate basis, and a visible list of the instruments/categories scanned by the backend.


## Latest UI refinement
- Market Board section removed from the homepage.
- Backend scan score is no longer displayed to users.
- Ranked analysis now highlights YoY return instead.
- Homepage hero market trends remain available as a compact summary.

## Long-term analysis update
- 1-year (YoY), 3-year CAGR and 5-year CAGR are now calculated where sufficient daily history exists.
- The allocation engine uses 1Y/3Y/5Y signals when building portfolio weights; longer horizons receive more weight from 3Y/5Y history.
- The result shows three allocation options for comparison; one is highlighted as FinanX's suggested fit.
- Per-segment 3Y/5Y projected values are shown for the selected option.
- Mutual-fund category data uses an AMFI-based integration with representative direct-growth schemes and AMFI historical NAV snapshots. AMFI publishes NAV daily and exposes historical NAV downloads; the official site notes a maximum 90-day range per history download.
- No numeric suitability score is shown to the user. Internal ranking remains available to the engine.


## UX and analysis update
- Result summary shows amount invested, annual estimate and projected value after the selected horizon.
- Investment breakup shows invested amount and projected value after the selected horizon.
- Market Analysis shows average category YoY, 3Y CAGR and 5Y CAGR.
- Multiple underlying instruments are averaged by category; Mutual Funds use a representative AMFI sample (up to 30 schemes).
- Three allocation options are presented within the same user-selected risk level.
- Learn is simplified to two beginner sections: What it is / How it works.


## Latest market UI refinement
- Added a compact Market Now strip with selected market values and representative AMFI mutual-fund NAVs.
- Removed provider/source details from user-facing market cards.
- Added an info icon beside each Market Analysis segment to show all tracked instruments/schemes.
- Category analysis still aggregates across the backend-tracked universe.


## Market coverage update
- Added current NSE index coverage: NIFTY 50, NIFTY 100, NIFTY Midcap 100, NIFTY LargeMidcap 250 and NIFTY Bank.
- Added a NIFTY futures reference from NSE derivatives data when available.
- Added MCX market-watch integration for Gold, Silver, Crude Oil, Natural Gas, Copper and other available commodities.
- Added 10 INR currency pairs.
- Mutual-fund cards use the existing AMFI-backed NAV data and are correctly labelled as Latest NAV, not intraday prices.
- Backend tracked-option info now exposes the full tracked universe through the information icon.


## Priority market coverage
- Live/current NSE index layer: NIFTY 50, NIFTY 100, NIFTY Midcap 100, NIFTY LargeMidcap 250, NIFTY Bank.
- NIFTY nearest futures reference from NSE derivatives.
- MCX market-watch: Gold, Silver, Crude Oil, Natural Gas, Copper and other available contracts through mcxlib.
- Ten INR currency pairs: USD, EUR, GBP, JPY, AUD, CAD, CHF, CNY, SGD, NZD.
- Representative AMFI mutual-fund NAVs remain daily NAVs, not intraday quotes.
- Indian bond cards: India 10Y government bond and 5Y AAA corporate-bond yield references.
- Full tracked universe remains available through the info icon beside each analysis segment.


## Homepage market-data reliability fix
- Gold/commodity values now have a public-data fallback through the same Yahoo layer if MCX market-watch is unavailable.
- Mutual-fund cards populate AMFI data on demand on first load instead of waiting for the background refresh.
- Homepage values refresh on a short cache interval so a temporary provider failure does not leave the cards permanently empty.


## Data error fix
The homepage market highlight endpoint now falls back to the local SQLite market cache and direct MFAPI lookup when the specialized live/NAV endpoint is temporarily empty. No UI changes were made in this fix.
