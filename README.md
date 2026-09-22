# FinanX — Upstox Market Data Architecture

FinanX uses **Upstox as its market-data source** for the tracked investment universe.

## Upstox coverage

- Live quotes for stocks, indices, F&O, listed bonds/debt, MCX commodities and currencies.
- Mutual-fund instrument metadata and latest NAV fields.
- Historical candles for 1Y/3Y/5Y market-performance calculations where supported.
- Instrument discovery from Upstox instrument data.

## Tracked universe

- Stocks: 30
- F&O: 30
- Mutual funds: 30
- Bonds: 20
- Gold: 3
- Commodities: 20
- Currency: 10

Market Performance averages are calculated from configured tracked Upstox entities with usable historical data. Missing history is not fabricated.

## FD reference

Bank FD rates remain a separate official-bank rate registry because FD deposit rates are not exchange market data exposed by Upstox.

## Environment

```text
UPSTOX_ANALYTICS_TOKEN=your_token
HOST=0.0.0.0
PORT=5000
```

## Run

```bat
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open http://127.0.0.1:5000

## Data rules

1. Upstox is the only market-data provider used by the application.
2. No Yahoo Finance, MFAPI or AMFI market-data fallback is used.
3. When an Upstox quote is unavailable, FinanX shows an unavailable value instead of fabricating one.
4. Historical averages use actual Upstox historical candles for the configured tracking universe.
5. Projected values are illustrative scenarios and are not guarantees.

FinanX is an educational decision-support project, not personalized investment advice.
