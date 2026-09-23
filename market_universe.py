from __future__ import annotations

import gzip
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import requests

BASE = "https://api.upstox.com/v3"
COMPLETE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
MF_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/mf-instruments.json.gz"
GLOBAL_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/global.json.gz"

_INSTRUMENT_CACHE = {}
_QUOTE_CACHE = {}
_QUOTE_ITEM_CACHE = {}
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
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


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


def clear_runtime_caches():
    """Clear in-process Upstox instrument/quote caches."""
    _INSTRUMENT_CACHE.clear()
    _QUOTE_CACHE.clear()
    _QUOTE_ITEM_CACHE.clear()


def _load_gzip_json(url, timeout=30):
    r = requests.get(url, headers={"User-Agent": "FinanX/1.0"}, timeout=timeout)
    r.raise_for_status()
    raw = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read()
    return json.loads(raw.decode("utf-8"))


def instruments():
    return _cache_get(_INSTRUMENT_CACHE, "complete", lambda: _load_gzip_json(COMPLETE_INSTRUMENTS_URL), TTL)


def mutual_fund_instruments():
    return _cache_get(_INSTRUMENT_CACHE, "mutual-funds", lambda: _load_gzip_json(MF_INSTRUMENTS_URL), TTL)


def global_instruments():
    """Return Upstox global indices/indicators, including the USD INR indicator."""
    return _cache_get(
        _INSTRUMENT_CACHE,
        "global",
        lambda: _load_gzip_json(GLOBAL_INSTRUMENTS_URL),
        TTL,
    )


def _normalize_instrument_label(value):
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _global_currency_indicator_key(label, *terms):
    """Resolve a live GLOBAL_INDICATOR key without making Market Now depend
    on the global instrument-file parser.
    
    Upstox documents USD INR as a GLOBAL_INDICATOR and requires the exact
    instrument_key from the global instrument file. Keep the known USD/INR
    trading symbol as a deterministic fallback, then use the file for any
    other supported indicator.
    """
    normalized = _normalize_instrument_label(" ".join(
        [str(label or "")] + [str(term or "") for term in terms]
    ))
    if "USD INR" in normalized or "USDINR" in normalized:
        return "GLOBAL_INDICATOR|USDINR"
    return _find_global_indicator_key(*terms)

def _find_global_indicator_key(*terms):
    wanted = [
        _normalize_instrument_label(term)
        for term in terms
        if _normalize_instrument_label(term)
    ]
    if not wanted:
        return None
    try:
        rows = global_instruments()
    except Exception:
        return None
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("instruments") or rows
        if isinstance(rows, dict):
            rows = list(rows.values())
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("segment", "")).upper() != "GLOBAL_INDICATOR":
            continue
        label = _normalize_instrument_label(" ".join([
            str(row.get("name", "")),
            str(row.get("trading_symbol", "")),
            str(row.get("asset_symbol", "")),
        ]))
        if any(term == label or term in label for term in wanted):
            return _instrument_key(row)
    return None


def _quotes(keys):
    """Fetch Upstox quotes with per-instrument caching and bounded retries."""
    keys = [k for k in keys if k]
    if not keys:
        return {}
    unique = list(dict.fromkeys(keys))
    now = datetime.now(timezone.utc).timestamp()

    out = {}
    misses = []
    for key in unique:
        hit = None
        for variant in (
            key,
            key.replace("|", ":"),
            key.replace(":", "|"),
            key.replace("NSE_INDEX|", "NSE_INDEX:"),
        ):
            candidate = _QUOTE_ITEM_CACHE.get(variant)
            if candidate and now - candidate[0] < QUOTE_TTL:
                hit = candidate
                break
        if hit:
            out[key] = hit[1]
        else:
            misses.append(key)

    if not misses:
        return out

    def fetch_chunk(chunk, depth=0):
        try:
            payload = _get(
                f"{BASE}/market-quote/quotes",
                {"instrument_key": ",".join(chunk)},
                timeout=15,
            )
            return payload.get("data") or {}
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)

            # Authentication/authorization or server-side/transient failures
            # should not fan out into dozens of requests.
            if status in (401, 403) or (status is not None and status >= 500):
                raise

            # Split only for request-validation/instrument-key failures.
            if status not in (400, 404, 409, 422) or len(chunk) <= 1 or depth >= 6:
                return {}

            mid = len(chunk) // 2
            left = fetch_chunk(chunk[:mid], depth + 1)
            right = fetch_chunk(chunk[mid:], depth + 1)
            left.update(right)
            return left
        except (requests.Timeout, requests.ConnectionError):
            # A transient network failure affects the whole request, not one
            # instrument. Do not turn it into an O(n) request storm.
            return {}
        except Exception:
            return {}

    data = fetch_chunk(misses)

    # Global indicators were introduced into the existing V2 quote API as
    # well as V3. If the V3 batch does not return a GLOBAL_INDICATOR quote,
    # retry only those keys through V2. This is deliberately scoped to global
    # instruments so ordinary market quotes continue using V3.
    global_misses = [
        key for key in misses
        if str(key).upper().startswith("GLOBAL_INDICATOR|")
    ]
    unresolved_global = [
        key for key in global_misses
        if not _lookup_quote(data, key)
    ]
    if unresolved_global:
        try:
            payload = _get(
                "https://api.upstox.com/v2/market-quote/quotes",
                {"instrument_key": ",".join(unresolved_global)},
                timeout=10,
            )
            legacy_data = payload.get("data") or {}
            if legacy_data:
                data.update(legacy_data)
        except Exception:
            pass

    # Upstox returns exchange-keyed objects. Cache each successful item
    # separately so future requests with overlapping universes reuse them.
    stamp = datetime.now(timezone.utc).timestamp()
    for returned_key, value in (data or {}).items():
        if not isinstance(value, dict):
            continue

        out[returned_key] = value

        # Upstox Full Market Quotes V3 keys the response by
        # "<EXCHANGE>:<TRADING_SYMBOL>", while the request normally uses
        # "<EXCHANGE>|<INSTRUMENT_TOKEN>". The response also includes
        # data.instrument_token, which is the reliable way to map the quote
        # back to the requested instrument key.
        response_aliases = {
            str(returned_key),
            str(returned_key).replace(":", "|"),
        }
        instrument_token = (
            value.get("instrument_token")
            or value.get("instrument_key")
            or value.get("instrumentToken")
        )
        if instrument_token:
            response_aliases.add(str(instrument_token))
            response_aliases.add(str(instrument_token).replace(":", "|"))
            response_aliases.add(str(instrument_token).replace("|", ":"))

        for alias in response_aliases:
            _QUOTE_ITEM_CACHE[alias] = (stamp, value)

        for requested in misses:
            requested_aliases = {
                str(requested),
                str(requested).replace("|", ":"),
                str(requested).replace(":", "|"),
                str(requested).replace("NSE_INDEX|", "NSE_INDEX:"),
            }
            if response_aliases.intersection(requested_aliases):
                _QUOTE_ITEM_CACHE[requested] = (stamp, value)
                out[requested] = value
                break

    return out


def _quote_match_tokens(value):
    """Return stable exchange/symbol tokens for Upstox quote-key variants."""
    text = str(value or "").upper().replace("|", ":")
    parts = text.split(":", 1)
    if len(parts) != 2:
        return {re.sub(r"[^A-Z0-9]", "", text)}
    exchange, symbol = parts
    return {
        f"{exchange}:{re.sub(r'[^A-Z0-9]', '', symbol)}",
        re.sub(r"[^A-Z0-9]", "", symbol),
    }


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

    # Global indicators can be returned by symbol label (for example
    # GLOBAL_INDICATOR:USD INR) while the instrument file uses
    # GLOBAL_INDICATOR|USDINR. Match the exchange + normalized symbol as a
    # final fallback instead of treating a valid live quote as missing.
    wanted = _quote_match_tokens(key)
    for returned_key, row in quotes.items():
        if isinstance(row, dict) and wanted.intersection(_quote_match_tokens(returned_key)):
            return row
    return {}


def _ltp_quotes(keys):
    """Fetch a batched LTP fallback and map response keys back to requested keys."""
    keys = list(dict.fromkeys([key for key in keys if key]))
    if not keys:
        return {}

    try:
        payload = _get(
            f"{BASE}/market-quote/ltp",
            {"instrument_key": ",".join(keys)},
            timeout=10,
        )
        data = payload.get("data") or {}
    except Exception:
        data = {}

    missing_global = [
        key for key in keys
        if str(key).upper().startswith("GLOBAL_INDICATOR|")
        and not _lookup_quote(data, key)
    ]
    if missing_global:
        try:
            payload = _get(
                "https://api.upstox.com/v2/market-quote/ltp",
                {"instrument_key": ",".join(missing_global)},
                timeout=10,
            )
            legacy_data = payload.get("data") or {}
            if legacy_data:
                data.update(legacy_data)
        except Exception:
            pass

    output = {}
    for returned_key, value in data.items():
        if not isinstance(value, dict):
            continue

        aliases = {
            str(returned_key),
            str(returned_key).replace(":", "|"),
        }
        instrument_token = (
            value.get("instrument_token")
            or value.get("instrument_key")
            or value.get("instrumentToken")
        )
        if instrument_token:
            aliases.add(str(instrument_token))
            aliases.add(str(instrument_token).replace(":", "|"))
            aliases.add(str(instrument_token).replace("|", ":"))

        for requested in keys:
            requested_aliases = {
                str(requested),
                str(requested).replace("|", ":"),
                str(requested).replace(":", "|"),
                str(requested).replace("NSE_INDEX|", "NSE_INDEX:"),
            }
            if aliases.intersection(requested_aliases):
                output[requested] = value
                break

    return output


_SINGLE_QUOTE_TTL = 15


def _quote_one(key):
    """Fetch exactly one current Upstox quote for a display card."""
    if not key:
        return {}
    def load():
        try:
            payload = _get(
                f"{BASE}/market-quote/quotes",
                {"instrument_key": key},
                timeout=10,
            )
            data = payload.get("data") or {}
            if data:
                return _lookup_quote(data, key) or (next(iter(data.values())) if len(data) == 1 else {})
        except Exception:
            return {}
        return {}
    return _cache_get(_QUOTE_CACHE, "single:" + key, load, _SINGLE_QUOTE_TTL)


def _quote_value(row):
    # V3 Full Quote normally supplies last_price. Keep cp/previous-close
    # separate so a closed session can still display the latest known value
    # without pretending it is a live tick.
    ltp = row.get("last_price")
    prev = row.get("prev_close_price") or row.get("cp")
    try:
        ltp = float(ltp)
        if ltp <= 0:
            ltp = None
    except (TypeError, ValueError):
        ltp = None
    try:
        prev = float(prev)
        if prev <= 0:
            prev = None
    except (TypeError, ValueError):
        prev = None
    if ltp is None:
        # Some quote variants expose the current OHLC close but omit
        # last_price. It is the latest traded/closed value, not a fabricated
        # price, so use it as a display fallback.
        try:
            ohlc_close = float((row.get("ohlc") or {}).get("close"))
            if ohlc_close > 0:
                ltp = ohlc_close
        except (TypeError, ValueError):
            pass
    change = ((ltp / prev) - 1) * 100 if ltp is not None and prev is not None else None
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
        q = _lookup_quote(quotes, key)
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


def _is_bond_instrument(row):
    """Conservatively classify listed debt instruments from Upstox metadata."""
    instrument_type = str(row.get("instrument_type") or "").strip().upper()
    asset_type = str(row.get("asset_type") or "").strip().upper()
    if instrument_type in {"BOND", "GSEC", "SDL", "TBILL", "T-BILL", "NCD", "DEBENTURE"}:
        return True
    if asset_type in {"BOND", "GSEC", "SDL", "TBILL", "T-BILL", "NCD", "DEBENTURE"}:
        return True

    name = str(row.get("name") or "").upper()
    symbol = str(row.get("trading_symbol") or "").upper()
    text = f"{name} {symbol}"

    import re
    strong_patterns = (
        r"\bBOND\b",
        r"\bNCD\b",
        r"\bDEBENTURE\b",
        r"\bGSEC\b",
        r"\bSDL\b",
        r"\bSGB\b",
        r"\bTBILL\b",
        r"\bT[- ]BILL\b",
        r"\bTREASURY\b",
        r"\bSOVEREIGN\b",
        r"\bGOVERNMENT SECURITY\b",
        r"\bGOVT(?:ERNMENT)?\b.*\b(?:SEC|SECURITY)\b",
    )
    return any(re.search(pattern, text) for pattern in strong_patterns)


def _bond_instruments(limit=None):
    """Find listed debt-like instruments conservatively from Upstox metadata."""
    rows = []
    for row in instruments():
        if row.get("segment") not in ("NSE_EQ", "BSE_EQ"):
            continue
        if row.get("instrument_type") not in ("EQ", "BOND", "GSEC", "SDL", "TBILL", "T-BILL", "NCD", "DEBENTURE"):
            continue
        if _is_bond_instrument(row):
            rows.append(row)

    rows.sort(key=lambda x: (
        0 if str(x.get("instrument_type") or "").upper() in {"BOND", "GSEC", "SDL", "TBILL", "T-BILL", "NCD", "DEBENTURE"} else 1,
        0 if "GSEC" in str(x.get("name") or "").upper() else 1,
        str(x.get("trading_symbol") or "").upper(),
    ))
    limit = TRACKING_LIMITS["bonds"] if limit is None else limit
    return rows[:limit]


def _unique_underlying_rows(rows, limit):
    """Deduplicate derivative contracts to one tracked entity per underlying."""
    ordered = sorted(
        rows,
        key=lambda row: (
            -(row.get("volume") or row.get("oi") or 0),
            row.get("_expiry_ms") or 0,
        ),
    )
    output = []
    seen = set()
    for row in ordered:
        entity_key = row.get("underlying_key") or _instrument_key(row)
        if not entity_key:
            continue
        normalized = str(entity_key)
        if normalized in seen:
            continue
        seen.add(normalized)
        item = dict(row)
        item["_entity_key"] = normalized
        output.append(item)
        if limit is not None and len(output) >= limit:
            break
    return output


def history_universe():
    """Return the configured market universe used for aggregate history averages."""
    fno_rows = _active_rows({"NSE_FO", "BSE_FO"}, {"FUT", "CE", "PE"})
    if fno_rows:
        expiries = [r["_expiry_ms"] for r in fno_rows if r.get("_expiry_ms")]
        if expiries:
            nearest = min(expiries)
            fno_rows = [r for r in fno_rows if r.get("_expiry_ms") == nearest]

    gold_rows = _unique_underlying_rows([
        x for x in _active_rows({"MCX_FO"}, {"FUT"})
        if "GOLD" in (
            str(x.get("underlying_symbol", "")).upper()
            + " " + str(x.get("name", "")).upper()
            + " " + str(x.get("trading_symbol", "")).upper()
        )
    ], TRACKING_LIMITS["gold"])
    commodity_rows = _unique_underlying_rows(
        [
            row for row in _active_rows({"MCX_FO"}, {"FUT"})
            if "GOLD" not in (
                str(row.get("underlying_symbol", "")).upper()
                + " " + str(row.get("name", "")).upper()
                + " " + str(row.get("trading_symbol", "")).upper()
            )
        ],
        TRACKING_LIMITS["commodities"],
    )
    currency_rows = _unique_underlying_rows([
        r for r in _active_rows({"NCD_FO"}, {"FUT"})
        if r.get("underlying_type") == "CUR"
    ], TRACKING_LIMITS["currency"])

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
        q = _lookup_quote(quotes, key)
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
    rows = [
        row for row in _active_rows({"MCX_FO"}, {"FUT"})
        if "GOLD" not in (
            str(row.get("underlying_symbol", "")).upper()
            + " " + str(row.get("name", "")).upper()
            + " " + str(row.get("trading_symbol", "")).upper()
        )
    ]
    rows = _unique_underlying_rows(rows, TRACKING_LIMITS["commodities"])
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
            "name": row.get("trading_symbol") or row.get("name"),
            "symbol": row.get("trading_symbol"),
            "instrument_key": key,
            "price": round(ltp, 4),
            "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume") or q.get("ohlc", {}).get("volume"),
            "year_high": q.get("year_high"),
            "year_low": q.get("year_low"),
            "underlying": row.get("underlying_symbol"),
            "underlying_key": row.get("underlying_key"),
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
        q = _lookup_quote(quotes, key)
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
            "underlying": row.get("underlying_symbol"),
            "underlying_key": row.get("underlying_key"),
            "source": "Upstox MCX market quote",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    return output


def compare_currency():
    rows = _active_rows({"NCD_FO"}, {"FUT"})
    rows = [r for r in rows if r.get("underlying_type") == "CUR"]
    rows = _unique_underlying_rows(rows, TRACKING_LIMITS["currency"])
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
            "name": row.get("trading_symbol") or row.get("name"),
            "symbol": row.get("trading_symbol"),
            "instrument_key": key,
            "price": round(ltp, 4),
            "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume") or q.get("ohlc", {}).get("volume"),
            "year_high": q.get("year_high"),
            "year_low": q.get("year_low"),
            "underlying": row.get("underlying_symbol"),
            "underlying_key": row.get("underlying_key"),
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
    # Upstox does not expose bank-deposit rate tables. Keep this reference
    # registry separate from market quotes and explicitly mark source quality.
    rows = [
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

    for row in rows:
        effective = str(row.get("effective") or "")
        row["source_status"] = "dated_reference" if len(effective) == 10 and effective.count("-") == 2 else "month_reference"
        row["live_verification_required"] = row["source_status"] != "dated_reference"
    return rows


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


def _upstox_currency_converter_quote(label):
    """Last-resort latest FX value from Upstox's public currency converter."""
    pair = str(label or "").replace("/", "-").lower()
    if not pair:
        return None, None, None
    url = f"https://upstox.com/currency-converter/{pair}/"
    try:
        r = requests.get(url, headers={"User-Agent": "FinanX/1.0"}, timeout=8)
        r.raise_for_status()
        html = r.text
        match = re.search(r"Price\s*:\s*([0-9]+(?:\.[0-9]+)?)", html, re.I)
        if not match:
            match = re.search(
                r"1\s+[A-Z]{3}\s*=\s*₹?\s*([0-9]+(?:\.[0-9]+)?)",
                html,
                re.I,
            )
        if not match:
            return None, None, None
        value = float(match.group(1))
        if value <= 0 or not math.isfinite(value):
            return None, None, None
        return value, None, datetime.now(timezone.utc).isoformat()
    except Exception:
        return None, None, None


def market_now():
    """Build the homepage Market Now board from current Upstox V3 quotes only."""
    targets=[]; seen=set()

    def add(label,key,kind,unit=None):
        identity=key or f"__upstox_missing__:{kind}:{label}"
        if identity in seen:return
        seen.add(identity)
        targets.append((label,key,kind,unit))

    for label,terms in [
        ("NIFTY 50",("NIFTY 50",)),("NIFTY Bank",("NIFTY BANK","BANK NIFTY")),
        ("NIFTY IT",("NIFTY IT",)),("India VIX",("INDIA VIX",)),
        ("NIFTY Midcap 100",("NIFTY MIDCAP 100","NIFTY MIDCAP")),
        ("NIFTY Smallcap 100",("NIFTY SMALLCAP 100","NIFTY SMALLCAP")),
    ]:
        add(label,_index_key_from_terms(*terms),"index")

    eq_rows=[x for x in instruments() if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"]
    by_symbol={str(x.get("trading_symbol","")).upper():x for x in eq_rows}
    for symbol in ("RELIANCE","HDFCBANK","TCS","INFY","SBIN","ICICIBANK"):
        row=by_symbol.get(symbol)
        if row:
            add(row.get("short_name") or row.get("name") or symbol,_instrument_key(row),"equity")

    all_futures=[x for x in instruments() if str(x.get("instrument_type","")).upper()=="FUT"]

    for label,terms,unit in [
        ("Gold",("GOLD",),"per contract"),("Silver",("SILVER",),"per contract"),
        ("Crude Oil",("CRUDEOIL","CRUDE OIL","CRUDE"),"per contract"),
        ("Copper",("COPPER",),"per contract"),("Natural Gas",("NATURALGAS","NATURAL GAS","NATGAS"),"per contract"),
        ("Zinc",("ZINC",),"per contract"),("Aluminium",("ALUMINIUM","ALUMINI"),"per contract"),
    ]:
        rows=[x for x in all_futures if str(x.get("segment","")).upper()=="MCX_FO"
              and any(term in (str(x.get("underlying_symbol","")).upper()+" "+str(x.get("name","")).upper()+" "+str(x.get("trading_symbol","")).upper()) for term in terms)]
        item=_find_nearest_future(rows,lambda x:True)
        if item:
            add(label,_instrument_key(item),"commodity",unit)

    # Currency display uses Upstox GLOBAL_INDICATOR as the live spot-like
    # source. NCD_FO futures remain the analysis/derivatives source.
    # This avoids blank cards when an individual currency future is outside
    # its trading session or has no current quote.
    # Use the same active NCD_FO currency universe used by Market Analysis.
    # Choosing the nearest expiry alone can select an illiquid contract whose
    # quote is unavailable even when the active/liquid contract has a value.
    ncd_currency_rows = [
        x for x in _active_rows({"NCD_FO"}, {"FUT"})
        if str(x.get("underlying_type", "")).upper() == "CUR"
    ]
    currency_targets = [
        ("USD/INR", ("USDINR", "USD INR"), "₹/USD"),
        ("EUR/INR", ("EURINR", "EUR INR"), "₹/EUR"),
        ("GBP/INR", ("GBPINR", "GBP INR"), "₹/GBP"),
        ("JPY/INR", ("JPYINR", "JPY INR"), "₹/JPY"),
        ("AUD/INR", ("AUDINR", "AUD INR"), "₹/AUD"),
        ("CNY/INR", ("CNYINR", "CNY INR"), "₹/CNY"),
    ]
    for label, terms, unit in currency_targets:
        rows = [
            x for x in ncd_currency_rows
            if any(term in (
                str(x.get("underlying_symbol", "")).upper() + " "
                + str(x.get("name", "")).upper() + " "
                + str(x.get("trading_symbol", "")).upper()
            ) for term in terms)
        ]
        # Prefer the most liquid active contract so Market Now and Market
        # Analysis point at the same usable currency universe.
        rows.sort(
            key=lambda x: (
                -(x.get("volume") or x.get("oi") or 0),
                x.get("_expiry_ms") or 0,
            )
        )
        item = rows[0] if rows else None
        if item:
            add(label, _instrument_key(item), "currency", unit)
            continue

        # No active NCD contract for this pair: fall back to a supported
        # GLOBAL_INDICATOR (for example USD/INR).
        indicator_key = _global_currency_indicator_key(label, *terms)
        if indicator_key:
            add(label, indicator_key, "currency", unit)

    for row in _bond_instruments(25):
        symbol=str(row.get("trading_symbol") or "").strip()
        add(row.get("short_name") or row.get("name") or symbol or "Listed Bond",_instrument_key(row),"bond")
        if sum(1 for x in targets if x[2]=="bond")>=5: break

    quote_targets = [(label, key, kind, unit) for label, key, kind, unit in targets if key]
    all_keys = [key for _, key, _, _ in quote_targets]
    try:
        quotes = _quotes(all_keys)
    except Exception:
        quotes = {}

    output_by_key = {
        key: _lookup_quote(quotes, key)
        for key in all_keys
    }

    # Global indicators are supported by Upstox Full Market Quotes V3, but
    # keep a single batched LTP fallback for any unresolved target so one
    # missing global quote cannot blank the currency cards.
    missing_quote_keys = [
        key for key in all_keys
        if not output_by_key.get(key)
    ]
    if missing_quote_keys:
        ltp_quotes = _ltp_quotes(missing_quote_keys)
        for key in missing_quote_keys:
            row = _lookup_quote(ltp_quotes, key)
            if row:
                output_by_key[key] = row

    # Final per-instrument fallback for any quote that is still unresolved.
    # This keeps one bad global/derivative instrument from blanking the rest
    # of the Market Now board.
    unresolved = [key for key in all_keys if not output_by_key.get(key)]
    if unresolved:
        with ThreadPoolExecutor(max_workers=min(8, len(unresolved))) as pool:
            futures = {pool.submit(_quote_one, key): key for key in unresolved}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    row = future.result()
                except Exception:
                    row = {}
                if row:
                    output_by_key[key] = row

    # Currency Market Now: NCD_FO remains the primary source, but if an NCD
    # contract has no quote at the moment, use Upstox's GLOBAL_INDICATOR
    # currency benchmark as the live display fallback. This is necessary
    # because the global USD/INR indicator is explicitly supported by
    # Upstox's live quote APIs, while an individual currency future can be
    # unavailable outside its trading session. Market Analysis continues to
    # use NCD_FO entities.
    currency_global_keys = {}
    for label, terms, _unit in currency_targets:
        ncd_target = next(
            ((target_label, target_key) for target_label, target_key, target_kind, _u in targets
             if target_label == label and target_kind == "currency"),
            None,
        )
        if not ncd_target:
            continue
        target_label, target_key = ncd_target
        q = _lookup_quote(output_by_key, target_key)
        q_ltp, _q_change = _quote_value(q)
        if q_ltp is not None:
            continue
        global_key = _global_currency_indicator_key(target_label, *terms)
        if global_key:
            currency_global_keys[target_label] = global_key

    if currency_global_keys:
        global_quotes = _quotes(list(currency_global_keys.values()))
        missing_global = [
            key for key in currency_global_keys.values()
            if not _lookup_quote(global_quotes, key)
        ]
        if missing_global:
            global_quotes.update(_ltp_quotes(missing_global))
        for label, global_key in currency_global_keys.items():
            global_row = _lookup_quote(global_quotes, global_key)
            if global_row:
                # Replace only the unresolved NCD card's quote. The card
                # remains labelled as currency and carries the actual source
                # key for traceability.
                target = next(
                    ((target_label, target_key, target_kind, target_unit)
                     for target_label, target_key, target_kind, target_unit in targets
                     if target_label == label and target_kind == "currency"),
                    None,
                )
                if target:
                    output_by_key[target[1]] = global_row

    output=[]
    for label,key,kind,unit in targets:
        q=output_by_key.get(key, {}) if key else {}
        ltp,change=_quote_value(q)
        if ltp is None and kind == "currency":
            converter_value, converter_change, converter_at = _upstox_currency_converter_quote(label)
            if converter_value is not None:
                output.append({
                    "label": label,
                    "value": round(float(converter_value), 4),
                    "today_change": converter_change,
                    "kind": kind,
                    "unit": unit,
                    "freshness": "latest",
                    "instrument_key": key,
                    "source": "Upstox currency converter",
                    "date": converter_at,
                })
                continue
        if ltp is None:
            output.append({
                "label":label,"value":None,"today_change":None,
                "kind":kind,"unit":unit,"freshness":"unavailable",
                "instrument_key":key,
            })
            continue
        output.append({
            "label":label,
            "value":round(float(ltp),4 if kind=="currency" else 2),
            "today_change":round(change,2) if change is not None else None,
            "kind":kind,"unit":unit,"freshness":"live",
            "instrument_key":key,
            "date":None,
        })

    order={label:i for i,(label,*_) in enumerate(targets)}
    output.sort(key=lambda row:order.get(row.get("label"),9999))
    return output
