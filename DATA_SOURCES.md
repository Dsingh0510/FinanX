# FinanX Data Sources

## Primary market-data source

**Upstox API V3 and Upstox instrument data**

FinanX uses Upstox for equity/index quotes, F&O, MCX commodities, currency futures, listed bonds/debt, mutual-fund instrument metadata and historical market candles. Upstox V3 provides full market quotes and historical candle data for supported instruments. citeturn494833search5turn494833search1

## FD reference

Bank FD rates remain a separate **official-bank rate registry** because deposit rates are not exchange-market quotes exposed by the Upstox market-data APIs used by FinanX.

## Market Performance

For each tracked segment FinanX loads Upstox historical monthly candles, calculates 1Y/3Y/5Y metrics where possible, and averages the valid tracked entities. History coverage is reported instead of inventing missing values.

## Data integrity

Yahoo Finance, MFAPI and AMFI are not used as hidden market-data fallbacks.
