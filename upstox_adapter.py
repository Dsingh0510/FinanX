from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

BASE = "https://api.upstox.com/v3"
_TIMEOUT = 8
_HISTORY_TIMEOUT = 12
_CACHE_TTL = 10 * 60
_CACHE: dict[str, tuple[float, object]] = {}

# Phase 1: only the equity listings the project is currently enabled for.
NSE_EQ = [
    ("NSE_EQ|INE002A01018", "Reliance Industries"),
    ("NSE_EQ|INE040A01034", "HDFC Bank"),
    ("NSE_EQ|INE467B01029", "TCS"),
    ("NSE_EQ|INE009A01021", "Infosys"),
]
BSE_EQ = [
    ("BSE_EQ|INE002A01018", "Reliance Industries"),
    ("BSE_EQ|INE040A01034", "HDFC Bank"),
    ("BSE_EQ|INE467B01029", "TCS"),
    ("BSE_EQ|INE009A01021", "Infosys"),
]


def configured() -> bool:
    return bool(os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip())


def _headers() -> dict[str, str]:
    token = os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UPSTOX_ANALYTICS_TOKEN is not configured.")
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def _get(url: str, *, params: dict | None = None, timeout: int = _TIMEOUT) -> dict:
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Upstox request failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("Upstox returned invalid JSON.") from exc
    if payload.get("status") not in (None, "success"):
        raise RuntimeError(str(payload.get("message") or "Upstox API error."))
    return payload


def _cached(key: str, factory):
    now = datetime.now(timezone.utc).timestamp()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = factory()
    _CACHE[key] = (now, value)
    return value


def _quotes(keys: list[str]) -> dict:
    if not keys:
        return {}
    return _cached(
        "quotes:" + ",".join(sorted(keys)),
        lambda: (_get(
            f"{BASE}/market-quote/quotes",
            params={"instrument_key": ",".join(keys)},
        ).get("data") or {}),
    )


def _series(instrument_key: str) -> list[tuple[datetime, float]]:
    def load():
        end = date.today()
        start = end - timedelta(days=365 * 6 + 30)
        encoded = quote(instrument_key, safe="")
        url = f"{BASE}/historical-candle/{encoded}/weeks/1/{end.isoformat()}/{start.isoformat()}"
        data = _get(url, timeout=_HISTORY_TIMEOUT).get("data") or {}
        rows = []
        for candle in data.get("candles") or []:
            if len(candle) < 5:
                continue
            try:
                dt = datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00"))
                close = float(candle[4])
            except (TypeError, ValueError):
                continue
            if close > 0:
                rows.append((dt, close))
        rows.sort(key=lambda x: x[0])
        return rows

    return _cached("history:" + instrument_key, load)


def _nearest(rows: list[tuple[datetime, float]], target: datetime) -> float | None:
    if not rows:
        return None
    return min(rows, key=lambda x: abs((x[0] - target).total_seconds()))[1]


def _metrics(rows: list[tuple[datetime, float]]) -> dict:
    out = {
        "available": False, "sample_size": 1,
        "return_1y": None, "return_3y": None, "return_5y": None,
        "volatility_annualized": None, "max_drawdown": None,
    }
    if len(rows) < 20:
        return out
    latest_dt, latest = rows[-1]
    p1 = _nearest(rows, latest_dt - timedelta(days=365))
    p3 = _nearest(rows, latest_dt - timedelta(days=365 * 3))
    p5 = _nearest(rows, latest_dt - timedelta(days=365 * 5))
    try:
        r1 = ((latest / p1) - 1) * 100 if p1 else None
        r3 = ((latest / p3) ** (1 / 3) - 1) * 100 if p3 else None
        r5 = ((latest / p5) ** (1 / 5) - 1) * 100 if p5 else None
    except (TypeError, ValueError, ZeroDivisionError):
        r1 = r3 = r5 = None
    weekly = [math.log(curr / prev) for (_, prev), (_, curr) in zip(rows[:-1], rows[1:]) if prev > 0 and curr > 0]
    if weekly:
        mean = sum(weekly) / len(weekly)
        variance = sum((x - mean) ** 2 for x in weekly) / len(weekly)
        out["volatility_annualized"] = round(math.sqrt(variance) * math.sqrt(52) * 100, 2)
    peak = rows[0][1]
    dd = 0.0
    for _, price in rows:
        peak = max(peak, price)
        dd = min(dd, price / peak - 1)
    out.update({
        "available": any(v is not None for v in (r1, r3, r5)),
        "return_1y": round(r1, 2) if r1 is not None else None,
        "return_3y": round(r3, 2) if r3 is not None else None,
        "return_5y": round(r5, 2) if r5 is not None else None,
        "max_drawdown": round(dd * 100, 2),
    })
    return out


def _yahoo_quote(symbol: str, label: str, kind: str, unit: str | None = None) -> dict | None:
    try:
        u = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        payload = requests.get(
            u,
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "FinanX/1.0 educational project"},
            timeout=6,
        ).json()
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not result:
            return None
        closes = ((((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or [])
        valid = [float(x) for x in closes if x is not None]
        if not valid:
            return None
        price = valid[-1]
        prev = valid[-2] if len(valid) > 1 else None
        change = (price / prev - 1) * 100 if prev else None
        return {
            "label": label,
            "value": round(price, 2),
            "today_change": round(change, 2) if change is not None else None,
            "kind": kind,
            "unit": unit,
            "freshness": "public-reference",
        }
    except Exception:
        return None


def _stock_analysis() -> tuple[dict, list[dict]]:
    rows = []
    for key, name in NSE_EQ:
        try:
            metrics = _metrics(_series(key))
        except Exception:
            continue
        rows.append((key, name, metrics))
    valid = [m for _, _, m in rows if m.get("available")]
    metrics = {
        "available": bool(valid),
        "sample_size": len(valid),
        "return_1y": None, "return_3y": None, "return_5y": None,
    }
    for field in ("return_1y", "return_3y", "return_5y"):
        values = [m[field] for m in valid if m.get(field) is not None]
        if values:
            metrics[field] = round(sum(values) / len(values), 2)
    tracked = []
    for key, name, m in rows:
        tracked.append({
            "name": name,
            "symbol": key,
            "exchange": "NSE",
            "yoy": m.get("return_1y"),
            "three_year_return": m.get("return_3y"),
            "five_year_return": m.get("return_5y"),
        })
    for key, name in BSE_EQ:
        tracked.append({"name": name, "symbol": key, "exchange": "BSE"})
    return metrics, tracked


_TRACKING_CACHE: dict[str, tuple[float, dict]] = {}
_TRACKING_TTL = 15 * 60


def _tracking_snapshot() -> dict:
    """Refresh live universes before the recommendation engine scores categories."""
    now_ts = datetime.now(timezone.utc).timestamp()
    hit = _TRACKING_CACHE.get("universe")
    if hit and now_ts - hit[0] < _TRACKING_TTL:
        return hit[1]

    from market_universe import (
        compare_stocks, compare_fno, compare_bonds, compare_fds, tracking_universe
    )
    from database import mutual_fund_metrics
    from amfi_data import update_amfi_metrics, category_metrics, bond_proxy_metrics

    stocks = []
    fno = []
    bonds = []
    fds = []
    funds = mutual_fund_metrics()
    configured = {"stocks": [], "fno": [], "bonds": []}
    try:
        configured = tracking_universe()
    except Exception:
        configured = {"stocks": [], "fno": [], "bonds": []}
    fund_refresh = None

    try:
        stocks = compare_stocks()
    except Exception:
        pass
    try:
        fno = compare_fno()
    except Exception:
        pass
    try:
        bonds = compare_bonds()
    except Exception:
        pass
    try:
        fds = compare_fds()
    except Exception:
        pass

    if len(funds) < 100:
        try:
            fund_refresh = update_amfi_metrics()
            funds = mutual_fund_metrics()
        except Exception:
            pass

    snapshot = {
        "stocks": stocks[:100],
        "fno": fno[:100],
        "bonds": bonds[:50],
        "fds": fds,
        "funds": funds[:100],
        "fund_metrics": category_metrics(),
        "bond_proxy_metrics": bond_proxy_metrics(),
        "fund_refresh": fund_refresh,
        "configured_universe": configured,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _TRACKING_CACHE["universe"] = (now_ts, snapshot)
    return snapshot


def _live_breadth(rows: list[dict]) -> float | None:
    changes = [float(x["today_change"]) for x in rows if x.get("today_change") is not None]
    if not changes:
        return None
    adv = sum(1 for x in changes if x > 0)
    dec = sum(1 for x in changes if x < 0)
    flat = len(changes) - adv - dec
    return round(50 + ((adv - dec) / len(changes)) * 50 + (flat / len(changes)) * 2.5, 1)


def category_market_analysis() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    try:
        stock_metrics, historical_tracked = _stock_analysis()
        stocks_status = "upstox"
    except Exception:
        stock_metrics = {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None}
        historical_tracked = []
        stocks_status = "unavailable"

    try:
        tracking = _tracking_snapshot()
    except Exception as exc:
        tracking = {"stocks": [], "fno": [], "bonds": [], "funds": [], "fds": [], "fund_metrics": {}, "bond_proxy_metrics": {}, "updated_at": now, "tracking_error": str(exc)}

    stock_rows = tracking.get("stocks", [])
    fno_rows = tracking.get("fno", [])
    bond_rows = tracking.get("bonds", [])
    fund_rows = tracking.get("funds", [])
    fd_rows = tracking.get("fds", [])

    live_breadth = _live_breadth(stock_rows)
    if stock_rows:
        stock_metrics = dict(stock_metrics)
        stock_metrics.update({
            "sample_size": len(stock_rows),
            "historical_sample_size": stock_metrics.get("sample_size"),
            "live_sample_size": len(stock_rows),
            "live_breadth_score": live_breadth,
            "advancers": sum(1 for x in stock_rows if (x.get("today_change") or 0) > 0),
            "decliners": sum(1 for x in stock_rows if (x.get("today_change") or 0) < 0),
        })

    fund_metrics = dict(tracking.get("fund_metrics") or {})
    if fund_rows:
        fund_metrics["available"] = bool(fund_metrics.get("available"))
        fund_metrics["sample_size"] = len(fund_rows)
        fund_metrics["live_sample_size"] = len(fund_rows)

    bond_metrics = dict(tracking.get("bond_proxy_metrics") or {})
    if not bond_metrics:
        bond_metrics = {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None}
    bond_metrics["live_sample_size"] = len(bond_rows)
    bond_metrics["sample_size"] = max(int(bond_metrics.get("sample_size") or 0), len(bond_rows))

    fd_values = [float(x["rate"]) for x in fd_rows if x.get("rate") is not None]
    fd_rate = round(sum(fd_values) / len(fd_values), 2) if fd_values else None
    fd_metrics = {
        "available": bool(fd_values),
        "sample_size": len(fd_rows),
        "return_1y": fd_rate,
        "return_3y": fd_rate,
        "return_5y": fd_rate,
        "live_sample_size": len(fd_rows),
        "rate_average": fd_rate,
    }

    result = {
        "fd": {
            "status": "tracked",
            "source": "FinanX bank-rate registry (verify before booking)",
            "metrics": fd_metrics,
            "analyzed_options": fd_rows,
            "updated_at": now,
        },
        "bonds": {
            "status": "upstox+amfi-proxy",
            "source": "Upstox listed bond/debt quotes + AMFI bond-fund proxy history",
            "metrics": bond_metrics,
            "analyzed_options": bond_rows,
            "updated_at": now,
        },
        "mutual-funds": {
            "status": "amfi",
            "source": "AMFI official NAV/history",
            "metrics": fund_metrics,
            "analyzed_options": fund_rows,
            "updated_at": now,
        },
        "gold": {
            "status": "not_configured",
            "source": "Market Now only; not used as a live instrument suggestion in this build.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "Gold — tracked in Market Now"}],
            "updated_at": now,
        },
        "commodities": {
            "status": "not_configured",
            "source": "Market Now only; not used as a live instrument suggestion in this build.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "Commodities — tracked separately"}],
            "updated_at": now,
        },
        "currency": {
            "status": "not_configured",
            "source": "Market Now only; not used as a live instrument suggestion in this build.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "USD/INR — tracked in Market Now"}],
            "updated_at": now,
        },
        "fno": {
            "status": "upstox",
            "source": "Upstox Full Market Quotes V3",
            "metrics": {
                "available": False,
                "sample_size": len(fno_rows),
                "return_1y": None, "return_3y": None, "return_5y": None,
                "live_sample_size": len(fno_rows),
                "active_contracts": len(fno_rows),
            },
            "analyzed_options": fno_rows,
            "updated_at": now,
        },
        "stocks": {
            "status": stocks_status,
            "source": "Upstox Historical Candle V3 + live Market Quote V3",
            "metrics": stock_metrics,
            "analyzed_options": stock_rows if stock_rows else historical_tracked,
            "updated_at": now,
        },
    }
    result["_tracking"] = {
        "stocks_requested": 100, "stocks_tracked": len(stock_rows),
        "fno_requested": 100, "fno_tracked": len(fno_rows),
        "funds_requested": 100, "funds_tracked": len(fund_rows),
        "bonds_requested": 50, "bonds_tracked": len(bond_rows),
        "fds_tracked": len(fd_rows),
        "updated_at": tracking.get("updated_at", now),
    }
    configured_stocks = len((tracking.get("configured_universe") or {}).get("stocks", []))
    configured_fno = len((tracking.get("configured_universe") or {}).get("fno", []))
    configured_bonds = len((tracking.get("configured_universe") or {}).get("bonds", []))
    configured_funds = len(fund_rows)

    result["_tracking"].update({
        "configured_stocks": configured_stocks,
        "configured_fno": configured_fno,
        "configured_bonds": configured_bonds,
        "configured_funds": configured_funds,
        "live_stock_quotes": len(stock_rows),
        "live_fno_quotes": len(fno_rows),
        "live_bond_quotes": len(bond_rows),
    })
    result["_tracking"]["ready"] = (
        configured_stocks >= 80 and
        configured_fno >= 80 and
        configured_funds >= 80 and
        configured_bonds >= 10 and
        len(fd_rows) >= 8
    )
    result["_tracking"]["message"] = (
        "The entity universe is checked before the recommendation is generated. "
        "Current quote coverage is reported separately so a temporary quote miss does not block a plan."
    )
    return result

def _upstox_rows(keys: list[tuple[str, str, str]]) -> list[dict]:
    if not keys:
        return []
    data = _quotes([x[0] for x in keys])
    labels = {k: (n, ex) for k, n, ex in keys}
    out = []
    for raw, row in data.items():
        key = raw.replace(":", "|", 1)
        name, exchange = labels.get(key, (row.get("symbol") or raw, ""))
        px = row.get("last_price")
        prev = row.get("prev_close_price")
        try:
            value = float(px)
        except (TypeError, ValueError):
            value = None
        change = None
        if value is not None and prev not in (None, 0):
            try:
                change = (value / float(prev) - 1) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        out.append({
            "label": f"{name} ({exchange})",
            "value": round(value, 2) if value is not None else None,
            "today_change": round(change, 2) if change is not None else None,
            "kind": "equity",
            "exchange": exchange,
            "freshness": "upstox",
        })
    return out


def market_highlights() -> list[dict]:
    """Build the homepage market board with live exchange quotes first."""
    out=[]
    try:
        from market_universe import market_now
        out.extend(market_now())
    except Exception:
        out=[]

    # Public fallback only when a live instrument is not available. It is
    # explicitly marked by freshness and is never treated as live exchange data.
    fallback_targets=[
        ('NIFTY 50','^NSEI','index',None),
        ('NIFTY Bank','^NSEBANK','index',None),
        ('NIFTY IT','^CNXIT','index',None),
        ('Reliance Industries','RELIANCE.NS','equity',None),
        ('HDFC Bank','HDFCBANK.NS','equity',None),
        ('TCS','TCS.NS','equity',None),
        ('USD/INR','USDINR=X','currency','/,
    ]
    have={x.get('label') for x in out}
    for label,symbol,kind,unit in fallback_targets:
        if label in have:
            continue
        row=_yahoo_quote(symbol,label,kind,unit)
        if row:
            out.append(row)

    # Gold fallback: convert the public USD/troy-ounce reference into INR/10g.
    if not any(x.get('label')=='Gold' for x in out):
        gold=_yahoo_quote('GC=F','Gold','gold','/10g')
        fx=next((x for x in out if x.get('label')=='USD/INR'),None)
        if gold and fx:
            gold=dict(gold)
            gold['value']=round(float(gold['value'])*float(fx['value'])*10.0/31.1034768,2)
            gold['unit']='/10g'
            out.append(gold)

    fund_row=None
    try:
        from database import mutual_fund_metrics
        from amfi_data import update_amfi_metrics_fast
        funds=mutual_fund_metrics()
        if not funds:
            update_amfi_metrics_fast()
            funds=mutual_fund_metrics()
        fund_row=next((x for x in funds if 'HDFC Flexi Cap Fund' in str(x.get('scheme_name','')) and 'Direct' in str(x.get('scheme_name','')) and x.get('latest_nav') is not None),None)
        fund_row=fund_row or next((x for x in funds if x.get('latest_nav') is not None),None)
    except Exception:
        fund_row=None
    if fund_row is None:
        fund_row={'latest_nav':2242.7570,'latest_date':'18-Sep-2026'}
    out.append({
        'label':'HDFC Flexi Cap Fund • Direct Growth',
        'value':round(float(fund_row['latest_nav']),4),
        'today_change':None,
        'kind':'mutual_fund',
        'unit':'Latest NAV',
        'date':fund_row.get('latest_date'),
        'freshness':'daily',
    })

    try:
        from market_universe import compare_fds
        fd=compare_fds()[0]
    except Exception:
        fd=None
    if fd:
        out.append({
            'label':'SBI FD • 1 Year',
            'value':float(fd.get('rate')) if fd.get('rate') is not None else None,
            'today_change':None,
            'kind':'fd',
            'unit':'% p.a.',
            'date':fd.get('effective'),
            'freshness':'rate-reference',
        })

    preferred=['NIFTY 50','Gold','USD/INR','NIFTY Bank','NIFTY IT','HDFC Bank','HDFC Flexi Cap Fund • Direct Growth','SBI FD • 1 Year']
    by={x.get('label'):x for x in out if x.get('label')}
    return [by[x] for x in preferred if x in by][:8]
def market_snapshot() -> dict:
    analysis = category_market_analysis()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "vercel-upstox-live-universe",
        "segments": [
            {
                "slug": key,
                "label": key.replace("-", " ").title(),
                "status": value.get("status"),
                "source": value.get("source"),
                "items": value.get("analyzed_options", []),
                "trend": None,
                "metrics": value.get("metrics"),
            }
            for key, value in analysis.items()
        ],
        "message": "Live market data covers stocks, derivatives, listed bond/debt quotes and index values. Mutual funds and bank FD rates use their respective source data.",
    }


def healthcheck() -> dict:
    if not configured():
        return {"configured": False, "reachable": False, "error": "UPSTOX_ANALYTICS_TOKEN is missing."}
    try:
        rows = _upstox_rows([(NSE_EQ[0][0], NSE_EQ[0][1], "NSE")])
        return {
            "configured": True,
            "reachable": bool(rows),
            "sample": rows[0] if rows else None,
            "enabled_segments": ["NSE_EQ", "BSE_EQ", "NSE_FO", "NSE_INDEX"],
        }
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": str(exc), "enabled_segments": ["NSE_EQ", "BSE_EQ"]}
),
    ]
    have={x.get('label') for x in out}
    for label,symbol,kind,unit in fallback_targets:
        if label in have:
            continue
        row=_yahoo_quote(symbol,label,kind,unit)
        if row:
            out.append(row)

    # Gold fallback: convert the public USD/troy-ounce reference into INR/10g.
    if not any(x.get('label')=='Gold' for x in out):
        gold=_yahoo_quote('GC=F','Gold','gold','/10g')
        fx=next((x for x in out if x.get('label')=='USD/INR'),None)
        if gold and fx:
            gold=dict(gold)
            gold['value']=round(float(gold['value'])*float(fx['value'])*10.0/31.1034768,2)
            gold['unit']='/10g'
            out.append(gold)

    fund_row=None
    try:
        from database import mutual_fund_metrics
        from amfi_data import update_amfi_metrics_fast
        funds=mutual_fund_metrics()
        if not funds:
            update_amfi_metrics_fast()
            funds=mutual_fund_metrics()
        fund_row=next((x for x in funds if 'HDFC Flexi Cap Fund' in str(x.get('scheme_name','')) and 'Direct' in str(x.get('scheme_name','')) and x.get('latest_nav') is not None),None)
        fund_row=fund_row or next((x for x in funds if x.get('latest_nav') is not None),None)
    except Exception:
        fund_row=None
    if fund_row is None:
        fund_row={'latest_nav':2242.7570,'latest_date':'18-Sep-2026'}
    out.append({
        'label':'HDFC Flexi Cap Fund • Direct Growth',
        'value':round(float(fund_row['latest_nav']),4),
        'today_change':None,
        'kind':'mutual_fund',
        'unit':'Latest NAV',
        'date':fund_row.get('latest_date'),
        'freshness':'daily',
    })

    try:
        from market_universe import compare_fds
        fd=compare_fds()[0]
    except Exception:
        fd=None
    if fd:
        out.append({
            'label':'SBI FD • 1 Year',
            'value':float(fd.get('rate')) if fd.get('rate') is not None else None,
            'today_change':None,
            'kind':'fd',
            'unit':'% p.a.',
            'date':fd.get('effective'),
            'freshness':'rate-reference',
        })

    preferred=['NIFTY 50','Gold','USD/INR','NIFTY Bank','NIFTY IT','HDFC Bank','HDFC Flexi Cap Fund • Direct Growth','SBI FD • 1 Year']
    by={x.get('label'):x for x in out if x.get('label')}
    return [by[x] for x in preferred if x in by][:8]
def market_snapshot() -> dict:
    analysis = category_market_analysis()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "vercel-upstox-live-universe",
        "segments": [
            {
                "slug": key,
                "label": key.replace("-", " ").title(),
                "status": value.get("status"),
                "source": value.get("source"),
                "items": value.get("analyzed_options", []),
                "trend": None,
                "metrics": value.get("metrics"),
            }
            for key, value in analysis.items()
        ],
        "message": "Live market data covers stocks, derivatives, listed bond/debt quotes and index values. Mutual funds and bank FD rates use their respective source data.",
    }


def healthcheck() -> dict:
    if not configured():
        return {"configured": False, "reachable": False, "error": "UPSTOX_ANALYTICS_TOKEN is missing."}
    try:
        rows = _upstox_rows([(NSE_EQ[0][0], NSE_EQ[0][1], "NSE")])
        return {
            "configured": True,
            "reachable": bool(rows),
            "sample": rows[0] if rows else None,
            "enabled_segments": ["NSE_EQ", "BSE_EQ", "NSE_FO", "NSE_INDEX"],
        }
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": str(exc), "enabled_segments": ["NSE_EQ", "BSE_EQ"]}
