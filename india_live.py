
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    import yfinance as yf
except Exception:
    yf = None

try:
    import mcxlib
except Exception:
    mcxlib = None

NSE_BASE = "https://www.nseindia.com"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

INDEX_ALIASES = {
    "NIFTY 50": ["NIFTY 50"],
    "NIFTY 100": ["NIFTY 100"],
    "NIFTY MIDCAP 100": ["NIFTY MIDCAP 100", "NIFTY MIDCAP 100"],
    "NIFTY LARGE MIDCAP 250": ["NIFTY LARGE MIDCAP 250", "NIFTY LARGE MIDCAP 250"],
    "NIFTY BANK": ["NIFTY BANK"],
}

CURRENCY_TICKERS = {
    "USD/INR": "USDINR=X",
    "EUR/INR": "EURINR=X",
    "GBP/INR": "GBPINR=X",
    "JPY/INR": "JPYINR=X",
    "AUD/INR": "AUDINR=X",
    "CAD/INR": "CADINR=X",
    "CHF/INR": "CHFINR=X",
    "CNY/INR": "CNYINR=X",
    "SGD/INR": "SGDINR=X",
    "NZD/INR": "NZDINR=X",
}

def _num(v):
    try:
        if v is None:
            return None
        x=float(v)
        return None if x != x else x
    except Exception:
        return None

def _session():
    s=requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get(NSE_BASE, timeout=8)
    except Exception:
        pass
    return s

_NSE_SESSION = None

def _nse_get(path, params=None):
    global _NSE_SESSION
    if _NSE_SESSION is None:
        _NSE_SESSION = _session()
    url=NSE_BASE + path
    r=_NSE_SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def nse_indices():
    """Best-effort current NSE index values from the public index endpoint."""
    payload=_nse_get("/api/allIndices")
    rows=payload.get("data", payload if isinstance(payload,list) else [])
    out=[]
    wanted=set()
    for aliases in INDEX_ALIASES.values():
        wanted.update(a.upper() for a in aliases)
    for row in rows:
        name=str(row.get("index") or row.get("indexSymbol") or row.get("name") or "").strip()
        if name.upper() in wanted:
            out.append({
                "label": name,
                "value": _num(row.get("last") if row.get("last") is not None else row.get("ltp")),
                "today_change": _num(row.get("percentChange") if row.get("percentChange") is not None else row.get("percentchange")),
                "kind": "nse_index",
                "timestamp": row.get("timeVal") or row.get("lastUpdateTime") or datetime.now(timezone.utc).isoformat(),
            })
    # Normalize names so the UI remains stable.
    normalized=[]
    for display, aliases in INDEX_ALIASES.items():
        match=next((x for x in out if x["label"].upper() in {a.upper() for a in aliases}), None)
        if match:
            match["label"]=display
            normalized.append(match)
    return normalized

def nifty_fno():
    """Best-effort nearest NIFTY index-futures quote from NSE derivative data."""
    try:
        payload=_nse_get("/api/quote-derivative", params={"symbol":"NIFTY"})
    except Exception:
        return None
    data=payload.get("stocks") or payload.get("data") or []
    if isinstance(data, dict):
        data=list(data.values())
    futures=[]
    for row in data:
        d=row.get("metadata", row)
        inst=str(d.get("instrumentType") or d.get("instrument") or "")
        if "FUTIDX" not in inst.upper() and "FUTURE" not in inst.upper():
            # Try nested identifier as fallback.
            ident=str(d.get("identifier") or row.get("identifier") or "")
            if "FUTIDX" not in ident.upper():
                continue
        expiry=d.get("expiryDate") or row.get("expiryDate") or ""
        ltp=_num(d.get("lastPrice") if d.get("lastPrice") is not None else row.get("lastPrice"))
        if ltp is not None:
            futures.append((str(expiry), ltp, d))
    if not futures:
        return None
    # Sort ISO-like expiry strings or leave API order if parsing fails.
    def key(x):
        s=x[0]
        return s
    futures.sort(key=key)
    expiry, price, row=futures[0]
    prev=_num(row.get("prevClose") if row.get("prevClose") is not None else row.get("closePrice"))
    pct=None if prev in (None,0) else (price/prev-1)*100
    return {"label":"NIFTY Futures","value":price,"today_change":pct,"kind":"nse_fno","timestamp":row.get("lastUpdateTime") or datetime.now(timezone.utc).isoformat(),"expiry":expiry}

def _one_currency(label, ticker):
    try:
        t=yf.Ticker(ticker)
        hist=t.history(period="5d", interval="1d", auto_adjust=False, prepost=False)
        if hist is None or hist.empty:
            return None
        price=_num(hist.iloc[-1].get("Close"))
        prev=_num(hist.iloc[-2].get("Close")) if len(hist)>=2 else None
        pct=None if price is None or prev in (None,0) else (price/prev-1)*100
        if price is None:
            return None
        return {"label":label,"value":price,"today_change":pct,"kind":"currency","timestamp":str(hist.index[-1]),"source_currency":"INR"}
    except Exception:
        return None

def current_currencies():
    if yf is None:
        return []
    # Parallel requests make the ten INR crosses much faster than the old
    # sequential loop, while keeping concurrency modest.
    out=[]
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures=[ex.submit(_one_currency,label,ticker) for label,ticker in CURRENCY_TICKERS.items()]
        for fut in as_completed(futures):
            try:
                row=fut.result()
                if row: out.append(row)
            except Exception:
                pass
    return out

def mcx_watch():
    """Current MCX market-watch values using mcxlib. Returns selected contracts."""
    if mcxlib is None:
        return []
    try:
        df=mcxlib.get_market_watch()
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    records=[]
    cols={str(c).strip().lower():c for c in getattr(df,"columns",[])}
    # Flexible column matching because MCX can vary names slightly.
    def col(*names):
        for n in names:
            if n.lower() in cols:
                return cols[n.lower()]
        for k,v in cols.items():
            if any(n.lower() in k for n in names):
                return v
        return None
    symbol_c=col("symbol","contract","instrument","commodity")
    price_c=col("ltp","last","last price","ltp price","close","close price")
    change_c=col("%change","percent change","change %","pchange")
    time_c=col("time","timestamp","updated at","date")
    expiry_c=col("expiry","expiry date")
    if price_c is None:
        return []
    wanted=["GOLD","GOLDM","SILVER","CRUDEOIL","NATURALGAS","COPPER","ALUMINIUM","ZINC"]
    for _,row in df.iterrows():
        symbol=str(row.get(symbol_c,"")).upper() if symbol_c else ""
        if not any(w in symbol for w in wanted):
            continue
        price=_num(row.get(price_c))
        if price is None:
            continue
        pct=_num(row.get(change_c)) if change_c else None
        records.append({
            "label":symbol,
            "value":price,
            "today_change":pct,
            "kind":"mcx",
            "timestamp":str(row.get(time_c,"") if time_c else ""),
            "expiry":str(row.get(expiry_c,"") if expiry_c else ""),
        })
    # Keep one representative/latest contract per wanted commodity.
    selected=[]
    seen=set()
    for r in records:
        key=re.sub(r"[^A-Z]","",r["label"])
        if "GOLD" in key: g="GOLD"
        elif "SILVER" in key: g="SILVER"
        elif "CRUDEOIL" in key: g="CRUDEOIL"
        elif "NATURALGAS" in key: g="NATURALGAS"
        elif "COPPER" in key: g="COPPER"
        elif "ALUMINIUM" in key: g="ALUMINIUM"
        elif "ZINC" in key: g="ZINC"
        else: g=key
        if g in seen: continue
        seen.add(g)
        r["label"]=g
        selected.append(r)
    return selected

def latest_mutual_funds():
    """Latest NAV and YoY for representative AMFI-backed schemes."""
    # Imported lazily to keep startup resilient.
    try:
        from database import mutual_fund_metrics
        rows=mutual_fund_metrics()
    except Exception:
        return []
    preferred=[
        "HDFC Nifty 50 Index Fund - Direct Plan - Growth",
        "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        "HDFC Balanced Advantage Fund - Direct Plan - Growth",
        "SBI Nifty Index Fund - Direct Plan - Growth",
    ]
    picked=[]
    for name in preferred:
        row=next((x for x in rows if x.get("scheme_name","").lower()==name.lower()),None)
        if row: picked.append(row)
    if len(picked)<4:
        for row in rows:
            if row not in picked:
                picked.append(row)
            if len(picked)>=4: break
    out=[]
    for row in picked[:4]:
        out.append({
            "label":row.get("scheme_name","Mutual Fund"),
            "value":_num(row.get("latest_nav")),
            "today_change":None,
            "yoy":_num(row.get("return_1y")),
            "kind":"mutual_fund",
            "timestamp":row.get("latest_date"),
        })
    return out
