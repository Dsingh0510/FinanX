from __future__ import annotations

import gzip
import io
import json
import os
from datetime import date, datetime, timezone

import requests

BASE = "https://api.upstox.com/v3"
COMPLETE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
MF_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/mf-instruments.json.gz"

_INSTRUMENT_CACHE = {}
_QUOTE_CACHE = {}
TTL = 30 * 60
QUOTE_TTL = 60


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


def _eq_instruments():
    rows = [x for x in instruments() if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"]
    by_symbol = {str(x.get("trading_symbol", "")).upper(): x for x in rows}
    return [by_symbol[s] for s in LIQUID_SYMBOLS if s in by_symbol][:100]


def compare_stocks():
    rows = _eq_instruments()
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
    return output[:100]


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
    rows = futures[:120] + options[:380]

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
    return result[:100]


def compare_bonds():
    rows = [
        x for x in instruments()
        if x.get("segment") in ("NSE_EQ", "BSE_EQ")
        and x.get("instrument_type") == "EQ"
        and any(word in (
            str(x.get("name", "")).upper() + " " + str(x.get("trading_symbol", "")).upper()
        ) for word in ("BOND", "GILT", "SDL", "GSEC", "BHARAT"))
    ][:160]
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
    for i, row in enumerate(output[:50], 1):
        row["rank"] = i
    return output[:50]


def compare_commodities():
    rows = _active_rows({"MCX_FO"}, {"FUT"})
    quotes = _quotes([_instrument_key(r) for r in rows[:250]])
    output = []
    for row in rows[:250]:
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
    for i, row in enumerate(output[:50], 1):
        row["rank"] = i
    return output[:50]


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
    rows = _active_rows({"NCD_FO", "BCD_FO"}, {"FUT"})
    rows = [r for r in rows if r.get("underlying_type") == "CUR"]
    quotes = _quotes([_instrument_key(r) for r in rows[:150]])
    output = []
    for row in rows[:150]:
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
    for i, row in enumerate(output[:50], 1):
        row["rank"] = i
    return output[:50]


def compare_mutual_funds(limit=100):
    """Return a broad Upstox MF universe using the official MF instrument master."""
    raw = mutual_fund_instruments()
    if isinstance(raw, dict):
        rows = raw.get("data") or raw.get("instruments") or []
    else:
        rows = raw or []

    # Do not require purchase_allowed/plan/dividend fields: the Upstox MF
    # instrument master can omit those fields for some valid schemes.
    candidates = []
    for row in rows:
        key = _instrument_key(row)
        name = str(row.get("name") or row.get("scheme_name") or "").strip()
        nav = row.get("last_price")
        if not key or not name:
            continue
        try:
            nav_value = float(nav) if nav is not None else None
        except (TypeError, ValueError):
            nav_value = None
        candidates.append({
            "_row": row,
            "instrument_key": key,
            "name": name,
            "latest_nav": nav_value,
            "latest_date": row.get("last_price_date"),
            "scheme_type": row.get("scheme_type"),
            "plan": row.get("plan"),
            "dividend_type": row.get("dividend_type"),
        })

    def rank(item):
        row = item["_row"]
        text = " ".join(str(row.get(k, "")) for k in ("scheme_type", "name", "short_name")).upper()
        score = 0
        for term in ("EQUITY", "HYBRID", "DEBT", "ELSS", "INDEX"):
            if term in text:
                score += 3
        if "DIRECT" in text:
            score += 2
        if "GROWTH" in text:
            score += 2
        if item["latest_nav"] is not None:
            score += 1
        return score

    candidates.sort(key=lambda x: (rank(x), x["latest_nav"] is not None), reverse=True)

    output = []
    seen = set()
    for item in candidates:
        key = item["instrument_key"]
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "name": item["name"],
            "scheme_code": item["_row"].get("scheme_code") or item["_row"].get("schemeCode") or key,
            "symbol": key,
            "instrument_key": key,
            "latest_nav": item["latest_nav"],
            "latest_date": item["latest_date"],
            "scheme_type": item["scheme_type"],
            "plan": item["plan"],
            "dividend_type": item["dividend_type"],
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
    fno_rows = fno_rows[:100]

    bond_rows = [
        x for x in instruments()
        if x.get("segment") in ("NSE_EQ", "BSE_EQ")
        and x.get("instrument_type") == "EQ"
        and any(word in (
            str(x.get("name", "")).upper() + " " + str(x.get("trading_symbol", "")).upper()
        ) for word in ("BOND", "GILT", "SDL", "GSEC", "BHARAT"))
    ][:50]

    fund_rows = compare_mutual_funds(100)

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


def market_now():
    targets = [
        ("NIFTY 50", "NSE_INDEX|Nifty 50", "index"),
        ("Gold", None, "commodity"),
        ("USD/INR", None, "currency"),
        ("NIFTY Bank", "NSE_INDEX|Nifty Bank", "index"),
        ("NIFTY IT", "NSE_INDEX|Nifty IT", "index"),
        ("Reliance Industries", "NSE_EQ|INE002A01018", "equity"),
        ("HDFC Bank", "NSE_EQ|INE040A01034", "equity"),
        ("TCS", "NSE_EQ|INE467B01029", "equity"),
        ("India VIX", "NSE_INDEX|India VIX", "index"),
    ]

    gold = compare_gold()
    if gold:
        targets[1] = ("Gold", gold[0]["instrument_key"], "commodity")

    usd = [
        x for x in compare_currency()
        if "USDINR" in str(x.get("symbol", "")).upper()
        or "USDINR" in str(x.get("underlying", "")).upper()
    ]
    if usd:
        targets[2] = ("USD/INR", usd[0]["instrument_key"], "currency")

    index_fallbacks = {
        "NIFTY 50": ("NSE_INDEX|Nifty 50",),
        "NIFTY Bank": ("NSE_INDEX|Nifty Bank",),
        "NIFTY IT": ("NSE_INDEX|Nifty IT",),
        "India VIX": ("NSE_INDEX|India VIX",),
    }
    for i, (label, key, kind) in enumerate(targets):
        if not key and label in index_fallbacks:
            key = _find_index_key(label, *index_fallbacks[label])
            targets[i] = (label, key, kind)

    quotes = _quotes([key for _, key, _ in targets if key])
    output = []
    for label, key, kind in targets:
        if not key:
            continue
        q = quotes.get(key.replace("|", ":")) or quotes.get(key) or {}
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        output.append({
            "label": label,
            "value": round(ltp, 4 if kind in ("currency", "commodity") else 2),
            "today_change": round(change, 2) if change is not None else None,
            "kind": kind,
            "unit": "/10g" if label == "Gold" else "/$" if label == "USD/INR" else None,
            "freshness": "live",
            "instrument_key": key,
        })
    return output
