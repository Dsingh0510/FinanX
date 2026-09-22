from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import requests

from database import save_mf_metric, save_mf_scheme, mutual_fund_metrics

UA = {"User-Agent": "FinanX/1.0 educational project", "Accept": "application/json,text/plain,*/*"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
MFAPI = "https://api.mfapi.in/mf"

_REFERENCE = {
    "NIFTY 50": 23346.40,
    "Gold": 133633.13,
    "USD/INR": 95.93,
}

MF_QUERIES = [
    "HDFC Nifty 50 Index Fund Direct Growth",
    "Parag Parikh Flexi Cap Fund Direct Growth",
    "HDFC Balanced Advantage Fund Direct Growth",
    "SBI Nifty Index Fund Direct Growth",
]

TOP_STOCKS = [
    ("RELIANCE", "RELIANCE.NS", "Reliance Industries"), ("HDFCBANK", "HDFCBANK.NS", "HDFC Bank"),
    ("ICICIBANK", "ICICIBANK.NS", "ICICI Bank"), ("BHARTIARTL", "BHARTIARTL.NS", "Bharti Airtel"),
    ("INFY", "INFY.NS", "Infosys"), ("TCS", "TCS.NS", "TCS"), ("SBIN", "SBIN.NS", "SBI"),
    ("ITC", "ITC.NS", "ITC"), ("LT", "LT.NS", "Larsen & Toubro"), ("HINDUNILVR", "HINDUNILVR.NS", "Hindustan Unilever"),
    ("AXISBANK", "AXISBANK.NS", "Axis Bank"), ("KOTAKBANK", "KOTAKBANK.NS", "Kotak Mahindra Bank"),
    ("BAJFINANCE", "BAJFINANCE.NS", "Bajaj Finance"), ("MARUTI", "MARUTI.NS", "Maruti Suzuki"),
    ("SUNPHARMA", "SUNPHARMA.NS", "Sun Pharma"), ("HCLTECH", "HCLTECH.NS", "HCLTech"), ("NTPC", "NTPC.NS", "NTPC"),
    ("ONGC", "ONGC.NS", "ONGC"), ("TITAN", "TITAN.NS", "Titan Company"), ("ADANIENT", "ADANIENT.NS", "Adani Enterprises"),
    ("POWERGRID", "POWERGRID.NS", "Power Grid"), ("ULTRACEMCO", "ULTRACEMCO.NS", "UltraTech Cement"),
    ("ASIANPAINT", "ASIANPAINT.NS", "Asian Paints"), ("TATASTEEL", "TATASTEEL.NS", "Tata Steel"),
    ("JSWSTEEL", "JSWSTEEL.NS", "JSW Steel"), ("COALINDIA", "COALINDIA.NS", "Coal India"),
    ("TECHM", "TECHM.NS", "Tech Mahindra"), ("WIPRO", "WIPRO.NS", "Wipro"), ("TATAMOTORS", "TATAMOTORS.NS", "Tata Motors"),
    ("HINDALCO", "HINDALCO.NS", "Hindalco"), ("TRENT", "TRENT.NS", "Trent"),
]

PUBLIC_MARKET_TARGETS = [
    ("NIFTY 50", "^NSEI", "index", None), ("NIFTY Bank", "^NSEBANK", "index", None),
    ("NIFTY IT", "^CNXIT", "index", None), ("India VIX", "^INDIAVIX", "index", None),
    ("Reliance Industries", "RELIANCE.NS", "equity", None), ("HDFC Bank", "HDFCBANK.NS", "equity", None),
    ("TCS", "TCS.NS", "equity", None), ("Infosys", "INFY.NS", "equity", None),
    ("SBI", "SBIN.NS", "equity", None), ("ICICI Bank", "ICICIBANK.NS", "equity", None),
    ("Gold", "GC=F", "gold", "₹/10g"), ("Silver", "SI=F", "commodity", "₹/kg"),
    ("Crude Oil", "CL=F", "commodity", "₹/barrel"), ("Copper", "HG=F", "commodity", "₹/kg"),
    ("Natural Gas", "NG=F", "commodity", "₹/MMBtu"), ("Zinc", "ZNC=F", "commodity", "₹/tonne"),
    ("Aluminium", "ALI=F", "commodity", "₹/tonne"), ("USD/INR", "USDINR=X", "currency", None),
    ("EUR/INR", "EURINR=X", "currency", None), ("GBP/INR", "GBPINR=X", "currency", None),
    ("JPY/INR", "JPYINR=X", "currency", None), ("AUD/INR", "AUDINR=X", "currency", None),
    ("CNY/INR", "CNYINR=X", "currency", None),
]

PUBLIC_CACHE = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except Exception:
        return None


def _yf(symbol: str, period: str = "7y", interval: str = "1d") -> dict | None:
    key = ("yf", symbol, period, interval)
    now = datetime.now(timezone.utc).timestamp()
    hit = PUBLIC_CACHE.get(key)
    ttl = 30 if period in {"1d", "5d"} else 86400
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        r = requests.get(
            f"{YAHOO}/{symbol}",
            params={"range": period, "interval": interval, "events": "div,splits", "includePrePost": "true"},
            headers=UA,
            timeout=8,
        )
        r.raise_for_status()
        result = ((r.json().get("chart") or {}).get("result") or [None])[0]
    except Exception:
        result = None
    PUBLIC_CACHE[key] = (now, result)
    return result

def _series(symbol: str, period: str = "7y", interval: str = "1d") -> list[tuple[datetime, float]]:
    result = _yf(symbol, period=period, interval=interval)
    if not result:
        return []
    ts = result.get("timestamp") or []
    closes = ((((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or [])
    out = []
    for t, c in zip(ts, closes):
        price = _num(c)
        if price is not None:
            out.append((datetime.fromtimestamp(t, tz=timezone.utc), price))
    return out

def _metrics(series: list[tuple[datetime, float]], periods_per_year: int = 252) -> dict:
    if len(series) < 10:
        return {"available": False, "return_1y": None, "return_3y": None, "return_5y": None}
    latest_date, latest = series[-1]

    def price_at(days: int):
        target = latest_date - timedelta(days=days)
        for dt, px in reversed(series):
            if dt <= target:
                return px
        return None

    p1, p3, p5 = price_at(365), price_at(1095), price_at(1825)
    r1 = ((latest / p1) - 1) * 100 if p1 else None
    r3 = (((latest / p3) ** (1 / 3)) - 1) * 100 if p3 else None
    r5 = (((latest / p5) ** (1 / 5)) - 1) * 100 if p5 else None

    daily = [
        math.log(curr / prev)
        for (_, prev), (_, curr) in zip(series[:-1], series[1:])
        if prev > 0 and curr > 0
    ]
    vol = None
    if len(daily) >= 10:
        mean = sum(daily) / len(daily)
        variance = sum((x - mean) ** 2 for x in daily) / len(daily)
        vol = math.sqrt(variance) * math.sqrt(periods_per_year) * 100

    peak = series[0][1]
    max_dd = 0.0
    for _, px in series:
        peak = max(peak, px)
        if peak:
            max_dd = min(max_dd, px / peak - 1)

    return {
        "available": any(v is not None for v in (r1, r3, r5)),
        "return_1y": round(r1, 2) if r1 is not None else None,
        "return_3y": round(r3, 2) if r3 is not None else None,
        "return_5y": round(r5, 2) if r5 is not None else None,
        "volatility_annualized": round(vol, 2) if vol is not None else None,
        "max_drawdown": round(max_dd * 100, 2),
        "data_points": len(series),
    }


def _quote(symbol: str, label: str) -> dict | None:
    s = _series(symbol, period="5d", interval="5m")
    if not s:
        return None
    _, px = s[-1]
    prev = s[-2][1] if len(s) >= 2 else None
    pct = ((px / prev) - 1) * 100 if prev not in (None, 0) else None
    return {
        "label": label,
        "value": round(px, 4 if symbol.endswith("=X") else 2),
        "today_change": round(pct, 3) if pct is not None else None,
        "timestamp": s[-1][0].isoformat(),
        "freshness": "public_intraday",
    }

def _to_inr_quote(label: str, quote: dict, fx: dict | None) -> dict:
    if not fx:
        return quote
    conversions = {
        "Gold": (quote["value"] * fx["value"] / 31.1034768 * 10.0, "₹/10g"),
        "Silver": (quote["value"] * fx["value"] / 0.0311034768, "₹/kg"),
        "Crude Oil": (quote["value"] * fx["value"], "₹/barrel"),
        "Copper": (quote["value"] * fx["value"] / 0.45359237, "₹/kg"),
        "Natural Gas": (quote["value"] * fx["value"], "₹/MMBtu"),
        "Zinc": (quote["value"] * fx["value"], "₹/tonne"),
        "Aluminium": (quote["value"] * fx["value"], "₹/tonne"),
    }
    if label in conversions:
        value, unit = conversions[label]
        return {**quote, "value": round(value, 2), "unit": unit}
    return quote


def _public_market_cards() -> list[dict]:
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_quote, symbol, label): (label, kind, unit) for label, symbol, kind, unit in PUBLIC_MARKET_TARGETS}
        rows = []
        for fut in as_completed(futures):
            label, kind, unit = futures[fut]
            try:
                row = fut.result()
            except Exception:
                row = None
            if row:
                row["kind"] = kind
                if unit:
                    row["unit"] = unit
                rows.append(row)
    fx = next((x for x in rows if x.get("label") == "USD/INR"), None)
    out = [_to_inr_quote(row["label"], row, fx) if row.get("label") in {"Gold","Silver","Crude Oil","Copper","Natural Gas","Zinc","Aluminium"} else row for row in rows]
    order = {label: i for i, (label, *_rest) in enumerate(PUBLIC_MARKET_TARGETS)}
    out.sort(key=lambda x: order.get(x.get("label"), 999))
    return out


def _gold_inr_10g() -> dict | None:
    gold = _quote("GC=F", "Gold")
    fx = _quote("USDINR=X", "USD/INR")
    if not gold or not fx:
        return None
    value = gold["value"] * fx["value"] / 31.1034768 * 10.0
    return {
        **gold,
        "value": round(value, 2),
        "unit": "₹/10g",
        "source_currency": "INR",
    }


def _mfapi_search(q: str) -> dict | None:
    try:
        r = requests.get(f"{MFAPI}/search", params={"q": q}, headers=UA, timeout=10)
        r.raise_for_status()
        matches = r.json() or []
        direct = next(
            (
                x for x in matches
                if "direct" in str(x.get("schemeName", "")).lower()
                and "growth" in str(x.get("schemeName", "")).lower()
            ),
            None,
        )
        return direct or (matches[0] if matches else None)
    except Exception:
        return None


def _mf_history(code: str) -> dict | None:
    try:
        r = requests.get(f"{MFAPI}/{code}", headers=UA, timeout=20)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("data") or []
        if not rows:
            return None
        parsed = []
        for row in rows:
            try:
                dt = datetime.strptime(row["date"], "%d-%m-%Y").replace(tzinfo=timezone.utc)
                parsed.append((dt, float(row["nav"])))
            except Exception:
                continue
        parsed.sort()
        metrics = _metrics(parsed)
        latest = parsed[-1]
        return {
            "scheme_code": code,
            "scheme_name": payload.get("meta", {}).get("scheme_name", "Mutual Fund"),
            "latest_nav": latest[1],
            "latest_date": latest[0].date().isoformat(),
            **metrics,
            "source": "MFAPI / AMFI NAV history",
        }
    except Exception:
        return None


def _load_mutual_funds() -> list[dict]:
    cached = mutual_fund_metrics()
    if cached:
        return cached

    matches = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_mfapi_search, q) for q in MF_QUERIES]
        for future in as_completed(futures):
            row = future.result()
            if row:
                matches.append(row)

    codes = []
    for row in matches:
        code = row.get("schemeCode")
        if code and code not in codes:
            codes.append(code)

    selected = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_mf_history, code) for code in codes[:4]]
        for future in as_completed(futures):
            row = future.result()
            if row:
                selected.append(row)

    for row in selected:
        save_mf_scheme({
            "scheme_code": row["scheme_code"],
            "scheme_name": row["scheme_name"],
            "latest_nav": row["latest_nav"],
            "latest_date": row["latest_date"],
            "source": row["source"],
        })
        save_mf_metric(row)
    return selected


def _aggregate(category: str, symbol: str, name: str) -> dict:
    series = _series(symbol)
    metrics = _metrics(series)
    return {
        "status": "auto" if metrics.get("available") else "reference",
        "source": "Public market history",
        "metrics": {"sample_size": 1, **metrics},
        "analyzed_options": [{"name": name, "symbol": symbol}],
        "updated_at": _now(),
    }


def _average_metric_rows(rows: list[dict]) -> dict:
    valid = [x for x in rows if any(x.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))]
    out = {
        "available": bool(valid), "sample_size": len(valid), "tracked_count": len(rows),
        "history_count": len(valid), "history_coverage": round(len(valid)/len(rows)*100,1) if rows else 0.0,
        "return_1y": None, "return_3y": None, "return_5y": None, "volatility_annualized": None,
    }
    for key in ("return_1y","return_3y","return_5y","volatility_annualized"):
        vals=[_num(x.get(key)) for x in valid if _num(x.get(key)) is not None]
        if vals: out[key]=round(sum(vals)/len(vals),2)
    out["average_basis"]=f"Average of {len(valid)}/{len(rows)} tracked entities" if rows else "No tracked history"
    return out


def _stock_history_rows() -> list[dict]:
    def work(stock):
        symbol, yahoo, name = stock
        series = _series(yahoo, period="5y", interval="1mo")
        return {"name": name, "symbol": symbol, **_metrics(series, periods_per_year=12)}
    out=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures=[ex.submit(work,row) for row in TOP_STOCKS]
        for fut in as_completed(futures):
            try:
                row=fut.result()
                if row.get("available"): out.append(row)
            except Exception:
                pass
    return out


def _public_category_rows(symbols: list[tuple[str,str]]) -> list[dict]:
    def work(item):
        name,symbol=item
        series=_series(symbol, period="5y", interval="1mo")
        return {"name":name,"symbol":symbol,**_metrics(series, periods_per_year=12)}
    out=[]
    with ThreadPoolExecutor(max_workers=min(8,len(symbols) or 1)) as ex:
        futures=[ex.submit(work,item) for item in symbols]
        for fut in as_completed(futures):
            try: out.append(fut.result())
            except Exception: pass
    return out


def category_market_analysis() -> dict:
    now=_now()
    stocks=_stock_history_rows()
    commodity_symbols=[("Gold","GC=F"),("Silver","SI=F"),("Crude Oil","CL=F"),("Copper","HG=F"),("Natural Gas","NG=F"),("Zinc","ZNC=F"),("Aluminium","ALI=F")]
    currency_symbols=[("USD/INR","USDINR=X"),("EUR/INR","EURINR=X"),("GBP/INR","GBPINR=X"),("JPY/INR","JPYINR=X"),("AUD/INR","AUDINR=X"),("CNY/INR","CNYINR=X")]
    commodities=_public_category_rows(commodity_symbols)
    currencies=_public_category_rows(currency_symbols)

    funds_raw=[x for x in mutual_fund_metrics() if not str(x.get("source","")).startswith("Bond proxy")]
    funds=[{"name":x.get("scheme_name"),"symbol":x.get("scheme_code"),"return_1y":x.get("return_1y"),"return_3y":x.get("return_3y"),"return_5y":x.get("return_5y"),"latest_nav":x.get("latest_nav"),"available":True} for x in funds_raw[:30] if any(x.get(k) is not None for k in ("return_1y","return_3y","return_5y"))]
    bond_funds=[x for x in mutual_fund_metrics() if str(x.get("source","")).startswith("Bond proxy")]
    bonds=[{"name":x.get("scheme_name"),"symbol":x.get("scheme_code"),"return_1y":x.get("return_1y"),"return_3y":x.get("return_3y"),"return_5y":x.get("return_5y"),"available":True} for x in bond_funds if any(x.get(k) is not None for k in ("return_1y","return_3y","return_5y"))][:3]

    fd_rates=[6.25,6.25,6.40,6.25,6.40,6.25,6.25,6.25,6.25,6.25]
    fd_rows=[{"name":f"Official FD rate {i+1}","symbol":f"FD-{i+1}","return_1y":r,"return_3y":r,"return_5y":r,"available":True,"rate":r} for i,r in enumerate(fd_rates,1)]

    result={
        "stocks":{"status":"public-history","source":"Public market history","metrics":_average_metric_rows(stocks),"analyzed_options":stocks,"updated_at":now},
        "mutual-funds":{"status":"daily-nav","source":"AMFI/MFAPI cached NAV history","metrics":_average_metric_rows(funds),"analyzed_options":funds,"updated_at":now},
        "bonds":{"status":"bond-fund-history" if bonds else "history-unavailable","source":"AMFI bond-fund proxy NAV history","metrics":_average_metric_rows(bonds),"analyzed_options":bonds,"updated_at":now},
        "gold":{"status":"public-history","source":"Public gold futures history","metrics":_average_metric_rows([x for x in commodities if x["name"]=="Gold"]),"analyzed_options":[x for x in commodities if x["name"]=="Gold"],"updated_at":now},
        "commodities":{"status":"public-history","source":"Public commodity history","metrics":_average_metric_rows(commodities),"analyzed_options":commodities,"updated_at":now},
        "currency":{"status":"public-history","source":"Public currency history","metrics":_average_metric_rows(currencies),"analyzed_options":currencies,"updated_at":now},
        "fno":{"status":"underlying-reference","source":"NIFTY 50 underlying history reference","metrics":_average_metric_rows([stocks[0]] if stocks else []),"analyzed_options":[{"name":"NIFTY derivatives underlying reference","symbol":"^NSEI","return_1y":stocks[0].get("return_1y") if stocks else None,"return_3y":stocks[0].get("return_3y") if stocks else None,"return_5y":stocks[0].get("return_5y") if stocks else None}],"updated_at":now},
        "fd":{"status":"rate-reference","source":"Official 1-year FD rate entries","metrics":_average_metric_rows(fd_rows),"analyzed_options":fd_rows,"updated_at":now},
    }
    return result


def market_highlights() -> list[dict]:
    return _public_market_cards()

def market_snapshot() -> dict:
    analysis = category_market_analysis()
    segments = [
        {
            "slug": key,
            "label": key.replace("-", " ").title(),
            "status": value.get("status"),
            "source": value.get("source"),
            "items": [],
            "trend": None,
            "metrics": value.get("metrics"),
        }
        for key, value in analysis.items()
    ]
    return {
        "generated_at": _now(),
        "mode": "vercel-public-data",
        "segments": segments,
        "message": "FinanX uses public market data and historical proxies for this educational prototype. Exact freshness depends on the source and is not an exchange-licensed real-time feed.",
    }
