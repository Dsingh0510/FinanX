# FinanX data-source plan

## Live / near-live market segments
- **Stocks / indices:** Upstox V3 LTP or WebSocket feed; NSE also provides authorized real-time market-data products.
- **F&O:** Upstox V3 LTP / WebSocket; option-chain and Greeks can be added later.
- **Gold / commodities:** use a configured MCX-enabled instrument key through an authorized provider.
- **Currency:** use a configured NSE/BSE currency-derivative instrument key through an authorized provider.
- **Bonds:** use a specific supported bond-data feed; do not label a static bond yield table as real-time.

## Daily / rate data
- **Mutual Funds:** AMFI daily NAV file.
- **FD:** bank-published rate tables; refresh when banks publish a change.
- **Government bond yields:** RBI/FBIL publications can provide yield/reference information at their published cadence.

## Analysis metrics
For live market-traded categories FinanX calculates:
- Current LTP
- Previous close and 1-day change
- 30-day and 90-day returns
- 1-year return when sufficient history is available
- Annualized volatility from daily log returns
- Maximum drawdown

## Recommendation logic
1. Build user-fit score from risk comfort, horizon, liquidity and goal.
2. Blend in current market-condition score only when reliable market data exists.
3. Apply hard caps to derivatives and other high-risk categories.
4. Produce a ranked **category scenario**, not a guaranteed-return forecast.
5. Show data freshness and source next to the analysis.

## Important rule
Never display a demo/cached value as real-time. If a feed is missing, the UI must show `NOT CONNECTED` or the appropriate freshness label.
