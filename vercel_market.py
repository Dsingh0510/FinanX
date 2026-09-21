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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except Exception:
        return None


def _yf(symbol: str, period: str = "7y") -> dict | None:
    try:
        r = requests.get(
            f"{YAHOO}/{symbol}",
            params={"range": period, "interval": "1d", "events": "div,splits"},
            headers=UA,
            timeout=12,
        )
        r.raise_for_status()
        result = ((r.json().get("chart") or {}).get("result") or [None])[0]
        return result
    except Exception:
        return None


def _series(symbol: str, period: str = "7y") -> list[tuple[datetime, float]]:
    result = _yf(symbol, period=period)
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


def _metrics(series: list[tuple[datetime, float]]) -> dict:
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
        vol = math.sqrt(variance) * math.sqrt(252) * 100

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
    s = _series(symbol, period="5d")
    if not s:
        return None
    _, px = s[-1]
    prev = s[-2][1] if len(s) >= 2 else None
    pct = ((px / prev) - 1) * 100 if prev not in (None, 0) else None
    return {
        "label": label,
        "value": round(px, 2),
        "today_change": round(pct, 3) if pct is not None else None,
        "timestamp": s[-1][0].isoformat(),
    }


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


def category_market_analysis() -> dict:
    result = {
        "fd": {
            "status": "estimate",
            "source": "Educational bank-rate assumption",
            "metrics": {
                "available": True,
                "sample_size": 1,
                "return_1y": 6.5,
                "return_3y": 6.5,
                "return_5y": 6.5,
            },
            "analyzed_options": [{"name": "Representative bank FD rate", "rate": 6.5}],
            "updated_at": _now(),
        },
        "bonds": {
            "status": "estimate",
            "source": "Educational bond return proxy",
            "metrics": {
                "available": True,
                "sample_size": 1,
                "return_1y": 7.0,
                "return_3y": 7.0,
                "return_5y": 7.0,
            },
            "analyzed_options": [{"name": "Representative bond proxy", "symbol": "BOND-PROXY"}],
            "updated_at": _now(),
        },
    }

    for category, symbol, name in [
        ("stocks", "^NSEI", "NIFTY 50"),
        ("gold", "GOLDBEES.NS", "Gold BeES"),
        ("commodities", "CL=F", "Crude Oil"),
        ("currency", "USDINR=X", "USD/INR"),
        ("fno", "^NSEI", "NIFTY 50 reference"),
    ]:
        result[category] = _aggregate(category, symbol, name)

    result["stocks"]["analyzed_options"] += [
        {"name": "NIFTY Bank", "symbol": "^NSEBANK"},
        {"name": "Reliance Industries", "symbol": "RELIANCE.NS"},
        {"name": "HDFC Bank", "symbol": "HDFCBANK.NS"},
        {"name": "TCS", "symbol": "TCS.NS"},
        {"name": "Infosys", "symbol": "INFY.NS"},
    ]
    result["gold"]["analyzed_options"] += [{"name": "Gold Futures", "symbol": "GC=F"}]
    result["commodities"]["analyzed_options"] += [
        {"name": "Silver", "symbol": "SI=F"},
        {"name": "Gold", "symbol": "GC=F"},
    ]
    result["currency"]["analyzed_options"] = [
        {"name": "USD/INR", "symbol": "USDINR=X"},
        {"name": "EUR/INR", "symbol": "EURINR=X"},
        {"name": "GBP/INR", "symbol": "GBPINR=X"},
        {"name": "JPY/INR", "symbol": "JPYINR=X"},
    ]
    result["fno"]["analyzed_options"] = [{"name": "NIFTY Futures (reference)", "symbol": "FUTIDX:NIFTY"}]

    mf_result = {
        "status": "daily",
        "source": "MFAPI / AMFI NAV history",
        "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
        "analyzed_options": [],
        "updated_at": _now(),
    }
    try:
        mf = _load_mutual_funds()
        if mf:
            metrics = {"available": True, "sample_size": len(mf)}
            for key in ("return_1y", "return_3y", "return_5y"):
                vals = [x.get(key) for x in mf if x.get(key) is not None]
                metrics[key] = round(sum(vals) / len(vals), 2) if vals else None
            mf_result["metrics"] = metrics
            mf_result["analyzed_options"] = [
                {
                    "name": x["scheme_name"],
                    "symbol": x["scheme_code"],
                    "yoy": x.get("return_1y"),
                    "three_year_return": x.get("return_3y"),
                    "five_year_return": x.get("return_5y"),
                }
                for x in mf[:30]
            ]
    except Exception:
        pass
    result["mutual-funds"] = mf_result
    return result


def market_highlights() -> list[dict]:
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [
            ex.submit(_quote, "^NSEI", "NIFTY 50"),
            ex.submit(_gold_inr_10g),
            ex.submit(_quote, "USDINR=X", "USD/INR"),
        ]
        rows = [future.result() for future in futures]

    out = [row for row in rows if row]
    if not any(x.get("label") == "NIFTY 50" for x in out):
        out.append({"label": "NIFTY 50", "value": _REFERENCE["NIFTY 50"], "today_change": None, "kind": "index", "timestamp": "2026-09-18", "freshness": "reference"})
    if not any(x.get("label") == "Gold" for x in out):
        out.append({"label": "Gold", "value": _REFERENCE["Gold"], "today_change": None, "kind": "gold", "timestamp": "2026-09-15", "unit": "₹/10g", "freshness": "reference"})
    if not any(x.get("label") == "USD/INR" for x in out):
        out.append({"label": "USD/INR", "value": _REFERENCE["USD/INR"], "today_change": None, "kind": "currency", "timestamp": "2026-09-17", "freshness": "reference"})
    return out


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
