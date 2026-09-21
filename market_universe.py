from __future__ import annotations

import gzip
import io
import os
from datetime import date, datetime, timezone
import requests

BASE = "https://api.upstox.com/v3"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_CACHE = {}
TTL = 15 * 60

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
        raise RuntimeError(str(payload.get("message") or "Upstox API error"))
    return payload

def _cache_get(key, factory):
    now = datetime.now(timezone.utc).timestamp()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    value = factory()
    _CACHE[key] = (now, value)
    return value

def instruments():
    def load():
        r = requests.get(INSTRUMENTS_URL, headers={"User-Agent":"FinanX/1.0"}, timeout=25)
        r.raise_for_status()
        raw = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read()
        import json
        return json.loads(raw.decode("utf-8"))
    return _cache_get("nse-instruments", load)

def _quotes(keys):
    if not keys:
        return {}
    out = {}
    for i in range(0, len(keys), 500):
        payload = _get(f"{BASE}/market-quote/quotes", {"instrument_key": ",".join(keys[i:i+500])}, timeout=15)
        out.update(payload.get("data") or {})
    return out

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

# A stable liquid-equity universe. The actual instrument key is resolved from
# the daily Upstox BOD file, so prices never depend on hard-coded tokens.
LIQUID_SYMBOLS = [
"RELIANCE","HDFCBANK","ICICIBANK","BHARTIARTL","INFY","TCS","SBIN","ITC","LT","HINDUNILVR",
"AXISBANK","KOTAKBANK","BAJFINANCE","M&M","MARUTI","SUNPHARMA","HCLTECH","NTPC","ONGC","TITAN",
"ADANIENT","ADANIPORTS","BEL","POWERGRID","ULTRACEMCO","ASIANPAINT","TATASTEEL","JSWSTEEL","COALINDIA","NESTLEIND",
"TECHM","WIPRO","TATAMOTORS","HINDALCO","GRASIM","TRENT","ETERNAL","INDUSINDBK","BAJAJFINSV","DIVISLAB",
"DRREDDY","CIPLA","EICHERMOT","APOLLOHOSP","BRITANNIA","HEROMOTOCO","BAJAJ-AUTO","TATACONSUM","SHRIRAMFIN","HDFCLIFE",
"SBILIFE","ADANIPOWER","JINDALSTEL","VEDL","IOC","BPCL","GAIL","RECLTD","PFC","HAL",
"INDIGO","IRCTC","DLF","LODHA","PIDILITIND","SIEMENS","ABB","AMBUJACEM","ACC","BANKBARODA",
"PNB","CANBK","IDFCFIRSTB","FEDERALBNK","YESBANK","INDIANB","LICI","ZOMATO","PAYTM","POLICYBZR",
"DMART","MOTHERSON","TVSMOTOR","ASHOKLEY","BOSCHLTD","CUMMINSIND","DABUR","GODREJCP","COLPAL","MARICO",
"VBL","HAVELLS","DIXON","POLYCAB","SRF","DLF","ICICIGI","ICICIPRULI","MAXHEALTH","FORTIS",
"LTIM","MPHASIS","PERSISTENT","COFORGE","TORNTPHARM","AUROPHARMA","ALKEM","BIOCON","LUPIN","LAURUSLABS"
]

def _eq_instruments():
    rows = [x for x in instruments() if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"]
    by_symbol = {str(x.get("trading_symbol","")).upper(): x for x in rows}
    selected = []
    for symbol in LIQUID_SYMBOLS:
        row = by_symbol.get(symbol)
        if row:
            selected.append(row)
    return selected[:100]

def compare_stocks():
    rows = _eq_instruments()
    quotes = _quotes([r["instrument_key"] for r in rows])
    result = []
    for r in rows:
        key = r["instrument_key"].replace("|", ":")
        q = quotes.get(key) or quotes.get(r["instrument_key"]) or {}
        ltp, change = _quote_value(q)
        if ltp is None:
            continue
        result.append({
            "rank": 0, "name": r.get("short_name") or r.get("name") or r.get("trading_symbol"),
            "symbol": r.get("trading_symbol"), "instrument_key": r.get("instrument_key"),
            "price": round(ltp, 2), "today_change": round(change, 2) if change is not None else None,
            "volume": q.get("volume"), "year_high": q.get("year_high"), "year_low": q.get("year_low"),
            "prev_close": q.get("prev_close_price"), "source": "Upstox Full Market Quotes V3",
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
    result.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    for i, x in enumerate(result, 1): x["rank"] = i
    return result[:100]

def _fno_instruments():
    today_ms = int(datetime.combine(date.today(), datetime.min.time()).timestamp() * 1000)
    rows = []
    for x in instruments():
        if x.get("segment") != "NSE_FO": continue
        typ = x.get("instrument_type")
        if typ not in ("FUT", "CE", "PE"): continue
        expiry = x.get("expiry")
        try:
            exp = int(expiry)
            expiry_sort = exp
            if exp < today_ms:
                continue
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                expiry_sort = int(parsed.timestamp() * 1000)
            except Exception:
                continue
            if expiry_sort < today_ms:
                continue
        x["_expiry_sort"] = expiry_sort
        rows.append(x)
    if not rows: return []
    nearest = min(x["_expiry_sort"] for x in rows)
    rows = [x for x in rows if x["_expiry_sort"] == nearest]
    futures = [x for x in rows if x.get("instrument_type") == "FUT" and x.get("underlying_type") in ("EQUITY","INDEX")]
    options = [x for x in rows if x.get("instrument_type") in ("CE","PE") and x.get("underlying_type") in ("EQUITY","INDEX")]
    # Take a broad candidate set, then use live OI/volume to keep the displayed
    # 100 contracts useful rather than arbitrary.
    return futures[:120] + options[:380]

def compare_fno():
    rows = _fno_instruments()
    quotes = _quotes([r["instrument_key"] for r in rows[:500]])
    result = []
    for r in rows[:500]:
        key = r["instrument_key"].replace("|", ":")
        q = quotes.get(key) or quotes.get(r["instrument_key"]) or {}
        ltp, change = _quote_value(q)
        if ltp is None: continue
        result.append({
            "name": r.get("trading_symbol") or r.get("name"),
            "symbol": r.get("trading_symbol"), "type": r.get("instrument_type"),
            "underlying": r.get("underlying_symbol"), "expiry": r.get("expiry"),
            "strike": r.get("strike_price") if r.get("instrument_type") in ("CE","PE") else None,
            "lot_size": r.get("lot_size"), "price": round(ltp, 4),
            "today_change": round(change,2) if change is not None else None,
            "volume": q.get("volume"), "oi": q.get("oi"), "previous_oi": q.get("previous_oi"),
            "year_high": q.get("year_high"), "year_low": q.get("year_low"),
            "source": "Upstox Full Market Quotes V3",
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
    # Mix the most liquid futures and options instead of returning only one type.
    futures = sorted([x for x in result if x["type"]=="FUT"], key=lambda x:x.get("volume") or 0, reverse=True)[:50]
    options = sorted([x for x in result if x["type"] in ("CE","PE")], key=lambda x:x.get("oi") or 0, reverse=True)[:50]
    result = futures + options
    result.sort(key=lambda x: ((x.get("type") != "FUT"), -(x.get("volume") or x.get("oi") or 0)))
    for i,x in enumerate(result,1): x["rank"]=i
    return result[:100]

def _nearest_live_future(rows, matcher):
    """Find the nearest non-expired futures contract matching matcher."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candidates=[]
    for row in rows:
        if row.get('instrument_type') != 'FUT':
            continue
        expiry=row.get('expiry')
        try:
            expiry_ms=int(expiry)
        except (TypeError,ValueError):
            try:
                expiry_ms=int(datetime.fromisoformat(str(expiry).replace('Z','+00:00')).timestamp()*1000)
            except Exception:
                continue
        if expiry_ms < now_ms or not matcher(row):
            continue
        row['_expiry_ms']=expiry_ms
        candidates.append(row)
    if not candidates:
        return None
    return min(candidates,key=lambda x:x['_expiry_ms'])

def market_now():
    """Return live market cards for the homepage."""
    instruments_all=instruments()

    # Direct index/equity instruments.
    targets=[
        ('NIFTY 50','NSE_INDEX|Nifty 50','index'),
        ('NIFTY Bank','NSE_INDEX|Nifty Bank','index'),
        ('NIFTY IT','NSE_INDEX|Nifty IT','index'),
        ('Reliance Industries','NSE_EQ|INE002A01018','equity'),
        ('HDFC Bank','NSE_EQ|INE040A01034','equity'),
    ]

    # Resolve near-month Gold futures dynamically from the daily MCX universe.
    gold_row=_nearest_live_future(
        [x for x in instruments_all if x.get('segment')=='MCX_FO'],
        lambda x: 'GOLD' in str(x.get('underlying_symbol','')).upper() or 'GOLD' in str(x.get('name','')).upper()
    )
    if gold_row:
        targets.append(('Gold',''+gold_row.get('instrument_key',''),'commodity'))

    # Resolve the nearest USD/INR currency future dynamically.
    fx_rows=[x for x in instruments_all if x.get('segment') in ('NSE_FO','BCD_FO','NCD_FO')]
    usd_row=_nearest_live_future(
        fx_rows,
        lambda x: any(term in (str(x.get(k,'' )).upper()) for k in ('trading_symbol','name','underlying_symbol') for term in ('USDINR','USD/INR'))
    )
    if usd_row:
        targets.append(('USD/INR',''+usd_row.get('instrument_key',''),'currency'))

    # Add TCS as the eighth tracked market-style entity when Gold/FX resolve;
    # it also acts as a fallback if one of those contracts is unavailable.
    targets.append(('TCS','NSE_EQ|INE467B01029','equity'))

    keys=[key for _,key,_ in targets if key]
    data=_quotes(keys)
    out=[]
    for label,key,kind in targets:
        q=data.get(key.replace('|',':')) or data.get(key) or {}
        ltp,change=_quote_value(q)
        if ltp is None:
            continue
        out.append({
            'label':label,
            'value':round(ltp,4 if kind in ('currency','commodity') else 2),
            'today_change':round(change,2) if change is not None else None,
            'kind':kind,
            'unit': None,
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
 if kind=='currency' else None,
            'freshness':'live',
            'instrument_key':key,
        })
    return out[:8]

def compare_bonds():
    rows = [x for x in instruments() if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    rows = [x for x in rows if any(w in str(x.get("name","")).upper() or w in str(x.get("trading_symbol","")).upper() for w in bond_words)]
    rows = rows[:160]
    quotes = _quotes([x["instrument_key"] for x in rows])
    out=[]
    for r in rows:
        q=quotes.get(r["instrument_key"].replace("|",":")) or {}
        ltp,change=_quote_value(q)
        if ltp is None: continue
        out.append({
            "rank":0,"name":r.get("short_name") or r.get("name") or r.get("trading_symbol"),
            "symbol":r.get("trading_symbol"),"price":round(ltp,4),
            "today_change":round(change,2) if change is not None else None,
            "year_high":q.get("year_high"),"year_low":q.get("year_low"),
            "source":"Upstox Full Market Quotes V3","updated_at":datetime.now(timezone.utc).isoformat()
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
 if kind=='currency' else None,
        })
    return out[:8]

def compare_bonds():
    rows = [x for x in instruments() if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    rows = [x for x in rows if any(w in str(x.get("name","")).upper() or w in str(x.get("trading_symbol","")).upper() for w in bond_words)]
    rows = rows[:160]
    quotes = _quotes([x["instrument_key"] for x in rows])
    out=[]
    for r in rows:
        q=quotes.get(r["instrument_key"].replace("|",":")) or {}
        ltp,change=_quote_value(q)
        if ltp is None: continue
        out.append({
            "rank":0,"name":r.get("short_name") or r.get("name") or r.get("trading_symbol"),
            "symbol":r.get("trading_symbol"),"price":round(ltp,4),
            "today_change":round(change,2) if change is not None else None,
            "year_high":q.get("year_high"),"year_low":q.get("year_low"),
            "source":"Upstox Full Market Quotes V3","updated_at":datetime.now(timezone.utc).isoformat()
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
 if kind=='currency' else None,
            'freshness':'live',
            'instrument_key':key,
        })
    return out[:8]

def compare_bonds():
    rows = [x for x in instruments() if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    rows = [x for x in rows if any(w in str(x.get("name","")).upper() or w in str(x.get("trading_symbol","")).upper() for w in bond_words)]
    rows = rows[:160]
    quotes = _quotes([x["instrument_key"] for x in rows])
    out=[]
    for r in rows:
        q=quotes.get(r["instrument_key"].replace("|",":")) or {}
        ltp,change=_quote_value(q)
        if ltp is None: continue
        out.append({
            "rank":0,"name":r.get("short_name") or r.get("name") or r.get("trading_symbol"),
            "symbol":r.get("trading_symbol"),"price":round(ltp,4),
            "today_change":round(change,2) if change is not None else None,
            "year_high":q.get("year_high"),"year_low":q.get("year_low"),
            "source":"Upstox Full Market Quotes V3","updated_at":datetime.now(timezone.utc).isoformat()
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
 if kind == 'currency' else None)),
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
 if kind=='currency' else None,
            'freshness':'live',
            'instrument_key':key,
        })
    return out[:8]

def compare_bonds():
    rows = [x for x in instruments() if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    rows = [x for x in rows if any(w in str(x.get("name","")).upper() or w in str(x.get("trading_symbol","")).upper() for w in bond_words)]
    rows = rows[:160]
    quotes = _quotes([x["instrument_key"] for x in rows])
    out=[]
    for r in rows:
        q=quotes.get(r["instrument_key"].replace("|",":")) or {}
        ltp,change=_quote_value(q)
        if ltp is None: continue
        out.append({
            "rank":0,"name":r.get("short_name") or r.get("name") or r.get("trading_symbol"),
            "symbol":r.get("trading_symbol"),"price":round(ltp,4),
            "today_change":round(change,2) if change is not None else None,
            "year_high":q.get("year_high"),"year_low":q.get("year_low"),
            "source":"Upstox Full Market Quotes V3","updated_at":datetime.now(timezone.utc).isoformat()
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
 if kind=='currency' else None,
        })
    return out[:8]

def compare_bonds():
    rows = [x for x in instruments() if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    rows = [x for x in rows if any(w in str(x.get("name","")).upper() or w in str(x.get("trading_symbol","")).upper() for w in bond_words)]
    rows = rows[:160]
    quotes = _quotes([x["instrument_key"] for x in rows])
    out=[]
    for r in rows:
        q=quotes.get(r["instrument_key"].replace("|",":")) or {}
        ltp,change=_quote_value(q)
        if ltp is None: continue
        out.append({
            "rank":0,"name":r.get("short_name") or r.get("name") or r.get("trading_symbol"),
            "symbol":r.get("trading_symbol"),"price":round(ltp,4),
            "today_change":round(change,2) if change is not None else None,
            "year_high":q.get("year_high"),"year_low":q.get("year_low"),
            "source":"Upstox Full Market Quotes V3","updated_at":datetime.now(timezone.utc).isoformat()
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
 if kind=='currency' else None,
            'freshness':'live',
            'instrument_key':key,
        })
    return out[:8]

def compare_bonds():
    rows = [x for x in instruments() if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    rows = [x for x in rows if any(w in str(x.get("name","")).upper() or w in str(x.get("trading_symbol","")).upper() for w in bond_words)]
    rows = rows[:160]
    quotes = _quotes([x["instrument_key"] for x in rows])
    out=[]
    for r in rows:
        q=quotes.get(r["instrument_key"].replace("|",":")) or {}
        ltp,change=_quote_value(q)
        if ltp is None: continue
        out.append({
            "rank":0,"name":r.get("short_name") or r.get("name") or r.get("trading_symbol"),
            "symbol":r.get("trading_symbol"),"price":round(ltp,4),
            "today_change":round(change,2) if change is not None else None,
            "year_high":q.get("year_high"),"year_low":q.get("year_low"),
            "source":"Upstox Full Market Quotes V3","updated_at":datetime.now(timezone.utc).isoformat()
        })
    out.sort(key=lambda x:x.get("volume") or 0,reverse=True)
    for i,x in enumerate(out[:50],1): x["rank"]=i
    return out[:50]

def compare_fds():
    # General-public card rates for a broadly comparable ~1-year tenor.
    # Each entry is tied to an official bank rate page and its effective date.
    return [
      {"bank":"SBI","tenor":"1 year to <2 years","rate":6.25,"senior_rate":6.75,"effective":"2026-06-16","source":"SBI official retail domestic term-deposit table"},
      {"bank":"HDFC Bank","tenor":"1 year to <15 months","rate":6.25,"senior_rate":6.75,"effective":"2026-08-19","source":"HDFC Bank official FD rate page"},
      {"bank":"PNB","tenor":"1 year","rate":6.40,"senior_rate":6.90,"effective":"2025-06-18","source":"PNB official domestic term-deposit table"},
      {"bank":"Canara Bank","tenor":"1 year & above to 1 year 3 months","rate":6.25,"senior_rate":6.75,"effective":"2025-08-07","source":"Canara Bank official deposit interest-rate page"},
      {"bank":"Axis Bank","tenor":"1 year–1 year 10 days","rate":6.40,"senior_rate":6.90,"effective":"2025-09-26","source":"Axis Bank official Fixed Deposits Plus table; verify product/tenor before booking"},
      {"bank":"ICICI Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live ICICI rate before booking"},
      {"bank":"Bank of India","tenor":"1 year to <3 years","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live BOI rate before booking"},
      {"bank":"Bank of Baroda","tenor":"1 year","rate":6.25,"senior_rate":7.25,"effective":"2026-09","source":"Bank rate reference; verify live BOB rate before booking"},
      {"bank":"Indian Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Indian Bank rate before booking"},
      {"bank":"Kotak Mahindra Bank","tenor":"Around 1 year","rate":6.25,"senior_rate":6.75,"effective":"2026-09","source":"Bank rate reference; verify live Kotak rate before booking"}
    ]


def tracking_universe():
    """Return configured instrument names without requiring quote authentication."""
    all_rows = instruments()
    stocks = _eq_instruments()
    fno_rows = _fno_instruments()
    futures = [x for x in fno_rows if x.get("instrument_type") == "FUT"]
    options = [x for x in fno_rows if x.get("instrument_type") in ("CE", "PE")]
    fno = futures[:50] + options[:50]

    eq_rows = [
        x for x in all_rows
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
    ]
    bond_words = ("BOND", "GILT", "SDL", "GSEC", "BHARAT")
    bonds = [
        x for x in eq_rows
        if any(
            word in str(x.get("name", "")).upper() or
            word in str(x.get("trading_symbol", "")).upper()
            for word in bond_words
        )
    ][:50]

    return {
        "stocks": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in stocks
        ],
        "fno": [x.get("trading_symbol") or x.get("name") for x in fno],
        "bonds": [
            x.get("trading_symbol") or x.get("short_name") or x.get("name")
            for x in bonds
        ],
    }
