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


def category_market_analysis() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    try:
        stock_metrics, tracked = _stock_analysis()
        stocks_status = "upstox" if tracked else "unavailable"
    except Exception:
        stock_metrics = {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None}
        tracked = []
        stocks_status = "unavailable"

    result = {
        "fd": {
            "status": "estimate", "source": "Educational fixed-rate assumption",
            "metrics": {"available": True, "sample_size": 1, "return_1y": 6.5, "return_3y": 6.5, "return_5y": 6.5},
            "analyzed_options": [{"name": "Representative bank FD rate"}], "updated_at": now,
        },
        "bonds": {
            "status": "estimate", "source": "Educational bond-return assumption",
            "metrics": {"available": True, "sample_size": 1, "return_1y": 7.0, "return_3y": 7.0, "return_5y": 7.0},
            "analyzed_options": [{"name": "Representative bond proxy"}], "updated_at": now,
        },
        "mutual-funds": {
            "status": "not_configured", "source": "AMFI module can be connected later.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "Mutual funds — integration pending"}], "updated_at": now,
        },
        "gold": {
            "status": "not_configured", "source": "Commodity segment is not enabled in Phase 1.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "Gold — integration pending"}], "updated_at": now,
        },
        "commodities": {
            "status": "not_configured", "source": "Commodity segment is not enabled in Phase 1.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "Commodities — integration pending"}], "updated_at": now,
        },
        "currency": {
            "status": "not_configured", "source": "Currency segment is not enabled in Phase 1.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "Currency — integration pending"}], "updated_at": now,
        },
        "fno": {
            "status": "not_configured", "source": "F&O segment is not enabled in Phase 1.",
            "metrics": {"available": False, "sample_size": 0, "return_1y": None, "return_3y": None, "return_5y": None},
            "analyzed_options": [{"name": "F&O — integration pending"}], "updated_at": now,
        },
        "stocks": {
            "status": stocks_status, "source": "Upstox Historical Candle V3",
            "metrics": stock_metrics, "analyzed_options": tracked, "updated_at": now,
        },
    }
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
    out = []
    if configured():
        try:
            out.extend(_upstox_rows([(k, n, "NSE") for k, n in NSE_EQ] + [(k, n, "BSE") for k, n in BSE_EQ]))
        except Exception:
            pass

    # These keep the existing hero cards populated without claiming they are
    # Upstox-enabled Phase-1 segments.
    nifty = _yahoo_quote("^NSEI", "NIFTY 50", "index")
    usd = _yahoo_quote("USDINR=X", "USD/INR", "currency")
    gold = _yahoo_quote("GC=F", "Gold", "gold", "₹/10g")
    if nifty:
        out.insert(0, nifty)
    if gold:
        out.insert(1 if out else 0, gold)
    if usd:
        out.insert(2 if len(out) >= 2 else len(out), usd)

    if not out:
        out = [
            {"label": "NIFTY 50", "value": 23346.40, "today_change": None, "kind": "index", "freshness": "reference"},
            {"label": "Gold", "value": 133633.13, "today_change": None, "kind": "gold", "unit": "₹/10g", "freshness": "reference"},
            {"label": "USD/INR", "value": 95.93, "today_change": None, "kind": "currency", "freshness": "reference"},
        ]
    return out


def market_snapshot() -> dict:
    analysis = category_market_analysis()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "vercel-upstox-phase1",
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
        "message": "Phase 1 uses Upstox for NSE/BSE equities. Other Upstox segments remain disabled.",
    )


def healthcheck() -> dict:
    if not configured():
        return {"configured": False, "reachable": False, "error": "UPSTOX_ANALYTICS_TOKEN is missing."}
    try:
        rows = _upstox_rows([(NSE_EQ[0][0], NSE_EQ[0][1], "NSE")])
        return {
            "configured": True,
            "reachable": bool(rows),
            "sample": rows[0] if rows else None,
            "enabled_segments": ["NSE_EQ", "BSE_EQ"],
        }
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": str(exc), "enabled_segments": ["NSE_EQ", "BSE_EQ"]}
