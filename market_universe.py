from __future__ import annotations

import gzip
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import requests

BASE = "https://api.upstox.com/v3"
COMPLETE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
MF_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/mf-instruments.json.gz"

_INSTRUMENT_CACHE = {}
_QUOTE_CACHE = {}
TTL = 30 * 60
QUOTE_TTL = 60

TRACKING_LIMITS = {
    "stocks": 30,
    "fno": 30,
    "mutual-funds": 30,
    "bonds": 20,
    "gold": 3,
    "commodities": 20,
    "currency": 10,
}

def _headers():
    token = os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UPSTOX_ANALYTICS_TOKEN is not configured.")
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def _get(url, params=None, timeout=15):
    r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") not in (None, "success"):
        raise RuntimeError(str(payload.get("message") or "Market API error"))
    return payload


def _cache_get(cache, key, factory, ttl):
    now = datetime.now(timezone.utc).timestamp()
    hit = cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = factory()
    cache[key] = (now, value)
    return value


def _load_gzip_json(url, timeout=30):
    r = requests.get(url, headers={"User-Agent": "FinanX/1.0"}, timeout=timeout)
    r.raise_for_status()
    raw = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read()
    return json.loads(raw.decode("utf-8"))


def instruments():
    return _cache_get(_INSTRUMENT_CACHE, "complete", lambda: _load_gzip_json(COMPLETE_INSTRUMENTS_URL), TTL)


def mutual_fund_instruments():
    return _cache_get(_INSTRUMENT_CACHE, "mutual-funds", lambda: _load_gzip_json(MF_INSTRUMENTS_URL), TTL)


def _quotes(keys):
    keys = [k for k in keys if k]
    if not keys:
        return {}
    unique = list(dict.fromkeys(keys))

    def load():
        out = {}
        for i in range(0, len(unique), 500):
            payload = _get(
                f"{BASE}/market-quote/quotes",
                {"instrument_key": ",".join(unique[i:i + 500])},
                timeout=15,
            )
            out.update(payload.get("data") or {})
        return out

    cache_key = "quotes:" + ",".join(sorted(unique))
    return _cache_get(_QUOTE_CACHE, cache_key, load, QUOTE_TTL)


def _lookup_quote(quotes, key):
    if not key:
        return {}
    variants = (
        key,
        key.replace("|", ":"),
        key.replace(":", "|"),
        key.replace("NSE_INDEX|", "NSE_INDEX:"),
    )
    for variant in variants:
        row = quotes.get(variant)
        if isinstance(row, dict):
            return row
    return {}


def _quote_value(row):
    ltp = row.get("last_price")
    prev = row.get("prev_close_price")
    try:
        ltp = float(ltp)
    except (TypeError, ValueError):
        ltp = None
    try:
        prev = float(prev)
    except (TypeError, ValueError):
        prev = None
    change = ((ltp / prev) - 1) * 100 if ltp is not None and prev else None
    return ltp, change


_PUBLIC_QUOTE_CACHE = {}
PUBLIC_QUOTE_TTL = 30
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

_PUBLIC_SYMBOLS = {
    "NIFTY 50": "^NSEI",
    "NIFTY Bank": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "India VIX": "^INDIAVIX",
    "NIFTY Midcap 100": "NIFTY_MIDCAP_100.NS",
    "NIFTY Smallcap 100": "NIFTY_SMLCAP_100.NS",
    "Reliance Industries": "RELIANCE.NS",
    "HDFC Bank": "HDFCBANK.NS",
    "TCS": "TCS.NS",
    "Infosys": "INFY.NS",
    "SBI": "SBIN.NS",
    "ICICI Bank": "ICICIBANK.NS",
    "Gold": "GC=F",
    "Silver": "SI=F",
    "Crude Oil": "CL=F",
    "Copper": "HG=F",
    "Natural Gas": "NG=F",
    "Zinc": "ZNC=F",
    "Aluminium": "ALI=F",
    "USD/INR": "USDINR=X",
    "EUR/INR": "EURINR=X",
    "GBP/INR": "GBPINR=X",
    "JPY/INR": "JPYINR=X",
    "AUD/INR": "AUDINR=X",
    "CNY/INR": "CNYINR=X",
}

_PUBLIC_UNITS = {
    "Gold": "₹/10g",
    "Silver": "₹/kg",
    "Crude Oil": "₹/barrel",
    "Copper": "₹/kg",
    "Natural Gas": "₹/MMBtu",
    "Zinc": "₹/tonne",
    "Aluminium": "₹/tonne",
}

def _yahoo_raw(symbol: str) -> dict:
    def load():
        try:
            r = requests.get(
                YAHOO_CHART_URL,
                params={"symbol": symbol, "range": "1d", "interval": "1m", "includePrePost": "true"},
                headers={"User-Agent": "FinanX/1.0", "Accept": "application/json"},
                timeout=8,
            )
            r.raise_for_status()
            result = ((r.json().get("chart") or {}).get("result") or [None])[0]
            if not result:
                return {}
            timestamps = result.get("timestamp") or []
            closes = ((((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or [])
            pairs = []
            for ts, close in zip(timestamps, closes):
                try:
                    value = float(close) if close is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is not None:
                    pairs.append((int(ts), value))
            if not pairs:
                return {}
            latest_ts, latest = pairs[-1]
            meta = result.get("meta") or {}
            previous = meta.get("chartPreviousClose") or meta.get("previousClose")
            try:
                previous = float(previous) if previous is not None else None
            except (TypeError, ValueError):
                previous = None
            if previous is None and len(pairs) >= 2:
                previous = pairs[-2][1]
            change = ((latest / previous) - 1) * 100 if previous not in (None, 0) else None
            return {
                "value": latest,
                "today_change": change,
                "timestamp": datetime.fromtimestamp(latest_ts, tz=timezone.utc).isoformat(),
            }
        except Exception:
            return {}

    return _cache_get(_PUBLIC_QUOTE_CACHE, symbol, load, PUBLIC_QUOTE_TTL)


def _public_market_fallback(label: str, explicit_symbol: str | None = None):
    symbol = explicit_symbol or _PUBLIC_SYMBOLS.get(label)
    if not symbol:
        return None

    raw = _yahoo_raw(symbol)
    if not raw:
        return None

    value = float(raw["value"])
    unit = None
    # Commodities are quoted in USD on Yahoo; convert to INR and make the unit explicit.
    if label in {"Gold", "Silver", "Crude Oil", "Copper", "Natural Gas", "Zinc", "Aluminium"}:
        fx = _yahoo_raw("USDINR=X")
        if not fx:
            return None
        usd_inr = float(fx["value"])
        conversions = {
            "Gold": value * usd_inr / 31.1034768 * 10.0,       # troy oz -> 10g
            "Silver": value * usd_inr / 0.0311034768,          # troy oz -> kg
            "Crude Oil": value * usd_inr,                      # USD/barrel
            "Copper": value * usd_inr / 0.45359237,            # USD/lb -> INR/kg
            "Natural Gas": value * usd_inr,                    # USD/MMBtu
            "Zinc": value * usd_inr,                           # USD/metric tonne
            "Aluminium": value * usd_inr,                     # USD/metric tonne
        }
        value = conversions[label]
        unit = _PUBLIC_UNITS[label]
    return {
        "value": round(value, 4 if label in {"USD/INR", "EUR/INR", "GBP/INR", "JPY/INR", "AUD/INR", "CNY/INR"} else 2),
        "today_change": round(float(raw["today_change"]), 2) if raw.get("today_change") is not None else None,
        "unit": unit,
        "freshness": "public_intraday",
        "date": raw.get("timestamp"),
    }


def _instrument_key(row):
    return row.get("instrument_key") or row.get("instrument_key_name")


LIQUID_SYMBOLS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "BHARTIARTL", "INFY", "TCS",
    "SBIN", "ITC", "LT", "HINDUNILVR", "AXISBANK", "KOTAKBANK",
    "BAJFINANCE", "M&M", "MARUTI", "SUNPHARMA", "HCLTECH", "NTPC",
    "ONGC", "TITAN", "ADANIENT", "ADANIPORTS", "BEL", "POWERGRID",
    "ULTRACEMCO", "ASIANPAINT", "TATASTEEL", "JSWSTEEL", "COALINDIA",
    "NESTLEIND", "TECHM", "WIPRO", "TATAMOTORS", "HINDALCO", "GRASIM",
    "TRENT", "ETERNAL", "INDUSINDBK", "BAJAJFINSV", "DIVISLAB",
    "DRREDDY", "CIPLA", "EICHERMOT", "APOLLOHOSP", "BRITANNIA",
    "HEROMOTOCO", "BAJAJ-AUTO", "TATACONSUM", "SHRIRAMFIN", "HDFCLIFE",
    "SBILIFE", "ADANIPOWER", "JINDALSTEL", "VEDL", "IOC", "BPCL",
    "GAIL", "RECLTD", "PFC", "HAL", "INDIGO", "IRCTC", "DLF", "LODHA",
    "PIDILITIND", "SIEMENS", "ABB", "AMBUJACEM", "ACC", "BANKBARODA",
    "PNB", "CANBK", "IDFCFIRSTB", "FEDERALBNK", "YESBANK", "INDIANB",
    "LICI", "ZOMATO", "PAYTM", "POLICYBZR", "DMART", "MOTHERSON",
    "TVSMOTOR", "ASHOKLEY", "BOSCHLTD", "CUMMINSIND", "DABUR",
    "GODREJCP", "COLPAL", "MARICO", "VBL", "HAVELLS", "DIXON",
    "POLYCAB", "SRF", "ICICIGI", "ICICIPRULI", "MAXHEALTH", "FORTIS",
    "LTIM", "MPHASIS", "PERSISTENT", "COFORGE", "TORNTPHARM",
    "AUROPHARMA", "ALKEM", "BIOCON", "LUPIN", "LAURUSLABS",
]


def _eq_instruments(limit=None):
    rows = [x for x in instruments() if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"]
    by_symbol = {str(x.get("trading_symbol", "")).upper(): x for x in rows}
    limit = TRACKING_LIMITS["stocks"] if limit is None else limit
    return [by_symbol[s] for s in LIQUID_SYMBOLS if s in by_symbol][:limit]



def compare_stocks():
    rows = _eq_instruments(TRACKING_LIMITS["stocks"])
    quotes = _quotes([_instrument_key(r) for r in rows])
    output = []
    for row in rows:
        key = _instrument_key(row)
        q = _lookup_quote(quotes, key)
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        output.append({
            "rank": 0,
            "name": row.get("short_name") or row.get("name") or row.get("trading_symbol"),
            "symbol": row.get("trading_symbol"),
            "instrument_key": key,
            "price": round(ltp, 2),
            "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume") or q.get("ohlc", {}).get("volume"),
            "year_high": q.get("year_high"),
            "year_low": q.get("year_low"),
            "prev_close": q.get("prev_close_price"),
            "source": "Upstox market quote",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    output.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    for i, row in enumerate(output, 1):
        row["rank"] = i
    return output[:TRACKING_LIMITS["stocks"]]


def _parse_expiry(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return None


def _active_rows(segments, types=None):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    result = []
    for row in instruments():
        if row.get("segment") not in segments:
            continue
        if types and row.get("instrument_type") not in types:
            continue
        expiry = row.get("expiry")
        if expiry is not None:
            expiry_ms = _parse_expiry(expiry)
            if expiry_ms is not None and expiry_ms < now_ms:
                continue
        item = dict(row)
        item["_expiry_ms"] = _parse_expiry(expiry) if expiry is not None else None
        result.append(item)
    return result


def compare_fno():
    rows = _active_rows({"NSE_FO", "BSE_FO"}, {"FUT", "CE", "PE"})
    expiries = [r["_expiry_ms"] for r in rows if r.get("_expiry_ms")]
    if expiries:
        nearest = min(expiries)
        rows = [r for r in rows if r.get("_expiry_ms") == nearest]

    futures = [r for r in rows if r.get("instrument_type") == "FUT" and r.get("underlying_type") in ("EQUITY", "INDEX")]
    options = [r for r in rows if r.get("instrument_type") in ("CE", "PE") and r.get("underlying_type") in ("EQUITY", "INDEX")]
    rows = futures[:15] + options[:15]

    quotes = _quotes([_instrument_key(r) for r in rows])
    output = []
    for row in rows:
        key = _instrument_key(row)
        q = quotes.get(key.replace("|", ":")) or quotes.get(key) or {}
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        output.append({
            "rank": 0,
            "name": row.get("trading_symbol") or row.get("name"),
            "symbol": row.get("trading_symbol"),
            "type": row.get("instrument_type"),
            "underlying": row.get("underlying_symbol"),
            "underlying_key": row.get("underlying_key"),
            "expiry": row.get("expiry"),
            "strike": row.get("strike_price") if row.get("instrument_type") in ("CE", "PE") else None,
            "lot_size": row.get("lot_size"),
            "price": round(ltp, 4),
            "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume") or q.get("ohlc", {}).get("volume"),
            "oi": q.get("oi"),
            "previous_oi": q.get("previous_oi"),
            "year_high": q.get("year_high"),
            "year_low": q.get("year_low"),
            "source": "Upstox market quote",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    futures = sorted([x for x in output if x["type"] == "FUT"], key=lambda x: x.get("volume") or 0, reverse=True)[:50]
    options = sorted([x for x in output if x["type"] in ("CE", "PE")], key=lambda x: x.get("oi") or x.get("volume") or 0, reverse=True)[:50]
    result = futures + options
    for i, row in enumerate(result, 1):
        row["rank"] = i
    return result[:TRACKING_LIMITS["fno"]]


def _bond_instruments(limit=None):
    """Find listed debt-like instruments from the Upstox equity universe."""
    words = (
        "BOND", "GILT", "SDL", "GSEC", "BHARAT", "NCD", "DEBENTURE",
        "SECURITY", "TBILL", "T-BILL", "SGB", "SOVEREIGN", "TREASURY",
    )
    rows = []
    for x in instruments():
        if x.get("segment") not in ("NSE_EQ", "BSE_EQ"):
            continue
        if x.get("instrument_type") not in ("EQ", "BOND"):
            continue
        text = (
            str(x.get("name", "")).upper()
            + " "
            + str(x.get("trading_symbol", "")).upper()
        )
        if any(word in text for word in words):
            rows.append(x)
    rows.sort(key=lambda x: (
        0 if "GSEC" in str(x.get("name", "")).upper() else 1,
        str(x.get("trading_symbol", "")).upper(),
    ))
    limit = TRACKING_LIMITS["bonds"] if limit is None else limit
    return rows[:limit]



def history_universe():
    """Return the configured market universe used for aggregate history averages."""
    fno_rows = _active_rows({"NSE_FO", "BSE_FO"}, {"FUT", "CE", "PE"})
    if fno_rows:
        expiries = [r["_expiry_ms"] for r in fno_rows if r.get("_expiry_ms")]
        if expiries:
            nearest = min(expiries)
            fno_rows = [r for r in fno_rows if r.get("_expiry_ms") == nearest]

    gold_rows = [
        x for x in _active_rows({"MCX_FO"}, {"FUT"})
        if "GOLD" in (
            str(x.get("underlying_symbol", "")).upper()
            + " " + str(x.get("name", "")).upper()
            + " " + str(x.get("trading_symbol", "")).upper()
        )
    ][:TRACKING_LIMITS["gold"]]
    commodity_rows = _active_rows({"MCX_FO"}, {"FUT"})[:TRACKING_LIMITS["commodities"]]
    currency_rows = [
        r for r in _active_rows({"NSE_FO", "NCD_FO", "BCD_FO"}, {"FUT"})
        if r.get("underlying_type") == "CUR"
    ][:TRACKING_LIMITS["currency"]]

    return {
        "stocks": _eq_instruments(TRACKING_LIMITS["stocks"]),
        "bonds": _bond_instruments(TRACKING_LIMITS["bonds"]),
        "mutual-funds": compare_mutual_funds(TRACKING_LIMITS["mutual-funds"]),
        "gold": gold_rows,
        "commodities": commodity_rows,
        "currency": currency_rows,
        "fno": fno_rows[:TRACKING_LIMITS["fno"]],
    }


def compare_bonds():
    rows = _bond_instruments(TRACKING_LIMITS["bonds"])
    quotes = _quotes([_instrument_key(r) for r in rows])
    output = []
    for row in rows:
        key = _instrument_key(row)
        q = quotes.get(key.replace("|", ":")) or quotes.get(key) or {}
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        output.append({
            "rank": 0,
            "name": row.get("short_name") or row.get("name") or row.get("trading_symbol"),
            "symbol": row.get("trading_symbol"),
            "instrument_key": key,
            "price": round(ltp, 4),
            "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume") or q.get("ohlc", {}).get("volume"),
            "year_high": q.get("year_high"),
            "year_low": q.get("year_low"),
            "source": "Upstox market quote",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    output.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    for i, row in enumerate(output[:TRACKING_LIMITS["bonds"]], 1):
        row["rank"] = i
    return output[:TRACKING_LIMITS["bonds"]]


def compare_commodities():
    rows = _active_rows({"MCX_FO"}, {"FUT"})
    quotes = _quotes([_instrument_key(r) for r in rows[:TRACKING_LIMITS["commodities"]]])
    output = []
    for row in rows[:TRACKING_LIMITS["commodities"]]:
        key = _instrument_key(row)
        q = quotes.get(key.replace("|", ":")) or quotes.get(key) or {}
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        output.append({
            "rank": 0,
            "name": row.get("trading_symbol") or row.get("name"),
            "symbol": row.get("trading_symbol"),
            "instrument_key": key,
            "price": round(ltp, 4),
            "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume") or q.get("ohlc", {}).get("volume"),
            "year_high": q.get("year_high"),
            "year_low": q.get("year_low"),
            "underlying": row.get("underlying_symbol"),
            "source": "Upstox market quote",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    output.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    for i, row in enumerate(output[:TRACKING_LIMITS["commodities"]], 1):
        row["rank"] = i
    return output[:TRACKING_LIMITS["commodities"]]


def compare_gold():
    rows = [x for x in _active_rows({"MCX_FO"}, {"FUT"}) if "GOLD" in (
        str(x.get("underlying_symbol", "")).upper() + " " + str(x.get("name", "")).upper()
    )]
    rows.sort(key=lambda x: x.get("_expiry_ms") or 0)
    rows = rows[:5]
    quotes = _quotes([_instrument_key(r) for r in rows])
    output = []
    for row in rows:
        key = _instrument_key(row)
        q = quotes.get(key.replace("|", ":")) or quotes.get(key) or {}
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        output.append({
            "rank": len(output) + 1,
            "name": "Gold",
            "symbol": row.get("trading_symbol"),
            "instrument_key": key,
            "price": round(ltp, 2),
            "today_change": round(change, 2) if change is not None else None,
            "unit": "per exchange contract",
            "expiry": row.get("expiry"),
            "source": "Upstox MCX market quote",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    return output


def compare_currency():
    rows = _active_rows({"NSE_FO", "NCD_FO", "BCD_FO"}, {"FUT"})
    rows = [r for r in rows if r.get("underlying_type") == "CUR"]
    quotes = _quotes([_instrument_key(r) for r in rows[:TRACKING_LIMITS["currency"]]])
    output = []
    for row in rows[:TRACKING_LIMITS["currency"]]:
        key = _instrument_key(row)
        q = quotes.get(key.replace("|", ":")) or quotes.get(key) or {}
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        output.append({
            "rank": 0,
            "name": row.get("trading_symbol") or row.get("name"),
            "symbol": row.get("trading_symbol"),
            "instrument_key": key,
            "price": round(ltp, 4),
            "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume") or q.get("ohlc", {}).get("volume"),
            "year_high": q.get("year_high"),
            "year_low": q.get("year_low"),
            "underlying": row.get("underlying_symbol"),
            "source": "Upstox currency market quote",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    output.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    for i, row in enumerate(output[:TRACKING_LIMITS["currency"]], 1):
        row["rank"] = i
    return output[:TRACKING_LIMITS["currency"]]


def compare_mutual_funds(limit=None):
    """Return the configured Upstox mutual-fund universe."""
    if limit is None:
        limit = TRACKING_LIMITS["mutual-funds"]
    raw = mutual_fund_instruments()

    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("data") or raw.get("instruments") or raw.get("mutual_funds") or []
        if not isinstance(rows, list):
            # Some instrument files may wrap records one level deeper.
            rows = [item for value in raw.values() if isinstance(value, list) for item in value]
    else:
        rows = []

    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _instrument_key(row)
        name = str(row.get("name") or row.get("scheme_name") or "").strip()
        if not key or not name:
            continue
        if row.get("purchase_allowed") is False:
            continue

        text = " ".join(
            str(row.get(k, "")) for k in ("name", "scheme_name", "plan", "dividend_type", "scheme_type")
        ).upper()
        direct = "DIRECT" in text or str(row.get("plan", "")).upper() == "DIRECT"
        growth = "GROWTH" in text or "GROWTH" in str(row.get("dividend_type", "")).upper()

        try:
            nav = float(row.get("last_price")) if row.get("last_price") is not None else None
        except (TypeError, ValueError):
            nav = None

        quality = (
            100
            if direct and growth
            else 80 if direct
            else 60 if growth
            else 40
        )
        quality += 5 if nav is not None else 0
        candidates.append((quality, row, key, name, nav))

    # Direct-growth first; then other valid Upstox MF instruments as a safety net.
    candidates.sort(key=lambda x: (-x[0], x[3].upper()))

    output = []
    seen = set()
    for _, row, key, name, nav in candidates:
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "name": name,
            "scheme_code": row.get("scheme_code") or row.get("schemeCode") or key,
            "symbol": key,
            "instrument_key": key,
            "latest_nav": nav,
            "latest_date": row.get("last_price_date"),
            "scheme_type": row.get("scheme_type"),
            "plan": row.get("plan"),
            "dividend_type": row.get("dividend_type"),
            "minimum_purchase_amount": row.get("minimum_purchase_amount"),
            "source": "Upstox mutual-fund instrument master",
        })
        if len(output) >= limit:
            break
    return output


def compare_fds():
    # Upstox's documented market/instrument APIs do not expose bank FD-rate
    # tables, so FD rates intentionally use a small official-bank fallback.
    return [
        {"bank": "SBI", "tenor": "1 year to <2 years", "rate": 6.25, "senior_rate": 6.75, "effective": "2026-06-16", "source": "Official SBI retail term-deposit table"},
        {"bank": "HDFC Bank", "tenor": "1 year to <15 months", "rate": 6.25, "senior_rate": 6.75, "effective": "2026-08-19", "source": "Official HDFC Bank FD rate page"},
        {"bank": "PNB", "tenor": "1 year", "rate": 6.40, "senior_rate": 6.90, "effective": "2025-06-18", "source": "Official PNB domestic term-deposit table"},
        {"bank": "Canara Bank", "tenor": "1 year & above to 1 year 3 months", "rate": 6.25, "senior_rate": 6.75, "effective": "2025-08-07", "source": "Official Canara Bank deposit-rate page"},
        {"bank": "Axis Bank", "tenor": "1 year–1 year 10 days", "rate": 6.40, "senior_rate": 6.90, "effective": "2025-09-26", "source": "Official Axis Bank FD table; verify before booking"},
        {"bank": "ICICI Bank", "tenor": "Around 1 year", "rate": 6.25, "senior_rate": 6.75, "effective": "2026-09", "source": "Official-rate fallback; verify live ICICI rate"},
        {"bank": "Bank of India", "tenor": "1 year to <3 years", "rate": 6.25, "senior_rate": 6.75, "effective": "2026-09", "source": "Official-rate fallback; verify live BOI rate"},
        {"bank": "Bank of Baroda", "tenor": "1 year", "rate": 6.25, "senior_rate": 7.25, "effective": "2026-09", "source": "Official-rate fallback; verify live BOB rate"},
        {"bank": "Indian Bank", "tenor": "Around 1 year", "rate": 6.25, "senior_rate": 6.75, "effective": "2026-09", "source": "Official-rate fallback; verify live Indian Bank rate"},
        {"bank": "Kotak Mahindra Bank", "tenor": "Around 1 year", "rate": 6.25, "senior_rate": 6.75, "effective": "2026-09", "source": "Official-rate fallback; verify live Kotak rate"},
    ]


def tracking_universe():
    stock_rows = _eq_instruments()
    fno_rows = _active_rows({"NSE_FO", "BSE_FO"}, {"FUT", "CE", "PE"})
    if fno_rows:
        expiries = [r["_expiry_ms"] for r in fno_rows if r.get("_expiry_ms")]
        if expiries:
            nearest = min(expiries)
            fno_rows = [r for r in fno_rows if r.get("_expiry_ms") == nearest]
    fno_rows = fno_rows[:TRACKING_LIMITS["fno"]]

    bond_rows = _bond_instruments(TRACKING_LIMITS["bonds"])

    fund_rows = compare_mutual_funds(TRACKING_LIMITS["mutual-funds"])

    return {
        "stocks": [x.get("trading_symbol") or x.get("short_name") or x.get("name") for x in stock_rows[:100]],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno_rows],
        "bonds": [x.get("trading_symbol") or x.get("short_name") or x.get("name") for x in bond_rows],
        "mutual-funds": [x.get("name") for x in fund_rows],
    }


def _find_index_key(*names):
    wanted = {str(x).strip().upper() for x in names}
    for row in instruments():
        if row.get("segment") != "NSE_INDEX":
            continue
        text = {
            str(row.get("name", "")).strip().upper(),
            str(row.get("trading_symbol", "")).strip().upper(),
        }
        if wanted & text:
            return row.get("instrument_key")
    return None


def _find_nearest_future(rows, matcher):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candidates = []
    for row in rows:
        if row.get("instrument_type") != "FUT" or not matcher(row):
            continue
        expiry_ms = _parse_expiry(row.get("expiry"))
        if expiry_ms is None or expiry_ms < now_ms:
            continue
        item = dict(row)
        item["_expiry_ms"] = expiry_ms
        candidates.append(item)
    return min(candidates, key=lambda x: x["_expiry_ms"]) if candidates else None


def _index_key_from_terms(*terms):
    wanted = [str(t).upper() for t in terms]
    for row in instruments():
        if row.get("segment") != "NSE_INDEX":
            continue
        label = (str(row.get("name", "")) + " " + str(row.get("trading_symbol", ""))).upper()
        if any(term in label for term in wanted):
            return _instrument_key(row)
    return None


def _nearest_future_by_terms(rows, terms):
    wanted = tuple(str(t).upper() for t in terms)
    matches = [
        x for x in rows
        if any(term in (
            str(x.get("underlying_symbol", "")).upper()
            + " " + str(x.get("name", "")).upper()
            + " " + str(x.get("trading_symbol", "")).upper()
        ) for term in wanted)
    ]
    return _find_nearest_future(matches, lambda x: True)


def market_now():
    """Build a broad Market Now board; Upstox quote first, instrument price fallback."""
    targets = []
    seen = set()
    last_prices = {}

    def add(label, key, kind, unit=None, fallback_price=None, public_symbol=None):
        identity = key or f"__public__:{kind}:{label}"
        if identity in seen:
            return
        seen.add(identity)
        if fallback_price is not None:
            last_prices[identity] = fallback_price
        targets.append((label, key, kind, unit, public_symbol))

    # Indices
    for label, terms in [
        ("NIFTY 50", ("NIFTY 50",)),
        ("NIFTY Bank", ("NIFTY BANK", "BANK NIFTY")),
        ("NIFTY IT", ("NIFTY IT",)),
        ("India VIX", ("INDIA VIX",)),
        ("NIFTY Midcap 100", ("NIFTY MIDCAP 100", "NIFTY MIDCAP")),
        ("NIFTY Smallcap 100", ("NIFTY SMALLCAP 100", "NIFTY SMALLCAP")),
    ]:
        add(label, _index_key_from_terms(*terms), "index", None, None, _PUBLIC_SYMBOLS.get(label))

    # Equities
    eq_rows = [
        x for x in instruments()
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    by_symbol = {str(x.get("trading_symbol", "")).upper(): x for x in eq_rows}
    for symbol in ("RELIANCE", "HDFCBANK", "TCS", "INFY", "SBIN", "ICICIBANK"):
        row = by_symbol.get(symbol)
        if row:
            try:
                master_price = float(row.get("last_price")) if row.get("last_price") is not None else None
            except (TypeError, ValueError):
                master_price = None
            add(
                row.get("short_name") or row.get("name") or symbol,
                _instrument_key(row),
                "equity",
                None,
                master_price,
                f"{symbol}.NS",
            )

    all_futures = [
        x for x in instruments()
        if str(x.get("instrument_type", "")).upper() == "FUT"
    ]

    # Commodities
    for label, terms, unit in [
        ("Gold", ("GOLD",), "/10g"),
        ("Silver", ("SILVER",), None),
        ("Crude Oil", ("CRUDEOIL", "CRUDE OIL", "CRUDE"), None),
        ("Copper", ("COPPER",), None),
        ("Natural Gas", ("NATURALGAS", "NATURAL GAS", "NATGAS"), None),
        ("Zinc", ("ZINC",), None),
        ("Aluminium", ("ALUMINIUM", "ALUMINI"), None),
    ]:
        rows = [
            x for x in all_futures
            if str(x.get("segment", "")).upper() == "MCX_FO"
            and any(term in (
                str(x.get("underlying_symbol", "")).upper()
                + " "
                + str(x.get("name", "")).upper()
                + " "
                + str(x.get("trading_symbol", "")).upper()
            ) for term in terms)
        ]
        item = _find_nearest_future(rows, lambda x: True)
        if item:
            try:
                master_price = float(item.get("last_price")) if item.get("last_price") is not None else None
            except (TypeError, ValueError):
                master_price = None
            add(label, _instrument_key(item), "commodity", unit, master_price, _PUBLIC_SYMBOLS.get(label))

    # Currencies
    for label, terms in [
        ("USD/INR", ("USDINR",)),
        ("EUR/INR", ("EURINR",)),
        ("GBP/INR", ("GBPINR",)),
        ("JPY/INR", ("JPYINR",)),
        ("AUD/INR", ("AUDINR",)),
        ("CNY/INR", ("CNYINR",)),
    ]:
        rows = [
            x for x in all_futures
            if any(term in (
                str(x.get("underlying_symbol", "")).upper()
                + " "
                + str(x.get("name", "")).upper()
                + " "
                + str(x.get("trading_symbol", "")).upper()
            ) for term in terms)
            and str(x.get("instrument_type", "")).upper() == "FUT"
        ]
        item = _find_nearest_future(rows, lambda x: True)
        if item:
            try:
                master_price = float(item.get("last_price")) if item.get("last_price") is not None else None
            except (TypeError, ValueError):
                master_price = None
            add(label, _instrument_key(item), "currency", None, master_price, _PUBLIC_SYMBOLS.get(label))

    # Listed bonds/debt. Keep five distinct names.
    bond_rows = _bond_instruments(25)
    for row in bond_rows:
        try:
            master_price = float(row.get("last_price")) if row.get("last_price") is not None else None
        except (TypeError, ValueError):
            master_price = None
        trading_symbol = str(row.get("trading_symbol") or "").strip()
        public_symbol = f"{trading_symbol}.NS" if trading_symbol else None
        add(
            row.get("short_name") or row.get("name") or trading_symbol or "Listed Bond",
            _instrument_key(row),
            "bond",
            None,
            master_price,
            public_symbol,
        )
        if sum(1 for x in targets if x[2] == "bond") >= 5:
            break

    quote_keys = [key for _, key, _, _, _ in targets if key]
    try:
        quotes = _quotes(quote_keys)
    except Exception:
        quotes = {}

    # When the exchange quote endpoint is unavailable for a card, use a public
    # intraday quote instead of leaving the user with an empty dash. The UI
    # distinguishes this from an exchange-live quote.
    missing = []
    output = []
    for label, key, kind, unit, public_symbol in targets:
        q = _lookup_quote(quotes, key)
        ltp, change = _quote_value(q)
        identity = key or f"__public__:{kind}:{label}"
        freshness = "live"
        used_unit = unit
        used_date = None
        if ltp is None:
            ltp = last_prices.get(identity)
            change = None
            freshness = "latest"
        if ltp is None:
            missing.append((label, key, kind, unit, public_symbol, identity))
            continue

        output.append({
            "label": label,
            "value": round(float(ltp), 4 if kind in ("currency", "commodity") else 2),
            "today_change": round(change, 2) if change is not None else None,
            "kind": kind,
            "unit": used_unit,
            "freshness": freshness,
            "instrument_key": key,
            "date": used_date,
        })

    if missing:
        with ThreadPoolExecutor(max_workers=min(12, len(missing))) as pool:
            futures = {
                pool.submit(_public_market_fallback, label, public_symbol): (label, key, kind, unit, identity)
                for label, key, kind, unit, public_symbol, identity in missing
            }
            for future in as_completed(futures):
                label, key, kind, unit, identity = futures[future]
                try:
                    fallback = future.result()
                except Exception:
                    fallback = None
                if fallback:
                    output.append({
                        "label": label,
                        "value": fallback["value"],
                        "today_change": fallback.get("today_change"),
                        "kind": kind,
                        "unit": fallback.get("unit") or unit,
                        "freshness": fallback.get("freshness", "public_intraday"),
                        "instrument_key": key,
                        "date": fallback.get("date"),
                    })
                else:
                    # Keep the card visible, but never invent a number.
                    output.append({
                        "label": label,
                        "value": None,
                        "today_change": None,
                        "kind": kind,
                        "unit": unit,
                        "freshness": "unavailable",
                        "instrument_key": key,
                    })

    # Restore the configured card order after parallel fallback calls.
    order = {label: i for i, (label, *_rest) in enumerate(targets)}
    output.sort(key=lambda row: order.get(row.get("label"), 9999))
    return output
