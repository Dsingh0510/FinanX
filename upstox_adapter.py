from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
import threading

import requests

from market_universe import (
    TRACKING_LIMITS,
    compare_bonds,
    compare_commodities,
    compare_currency,
    compare_fds,
    compare_fno,
    compare_gold,
    compare_mutual_funds,
    compare_stocks,
    market_now,
    tracking_universe,
)

BASE = "https://api.upstox.com/v3"
_TIMEOUT = 8
_HISTORY_TIMEOUT = 10
_SNAPSHOT_TTL = 30 * 60
_ANALYSIS_TTL = 30 * 60
_HISTORY_TTL = 24 * 60 * 60
_CACHE = {}
_SNAPSHOT = None
_SNAPSHOT_AT = 0.0
_ANALYSIS = None
_ANALYSIS_AT = 0.0
_HISTORY_GATE = threading.BoundedSemaphore(8)


def configured() -> bool:
    return bool(os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip())


def cached_market_analysis() -> dict | None:
    """Return the most recent in-process analysis without reaching the API."""
    return _ANALYSIS


def clear_runtime_caches() -> None:
    """Clear in-process analysis, snapshot, history, and Market Now caches."""
    global _SNAPSHOT, _SNAPSHOT_AT, _ANALYSIS, _ANALYSIS_AT
    global _MARKET_HIGHLIGHTS, _MARKET_HIGHLIGHTS_AT
    _CACHE.clear()
    _SNAPSHOT = None
    _SNAPSHOT_AT = 0.0
    _ANALYSIS = None
    _ANALYSIS_AT = 0.0
    _MARKET_HIGHLIGHTS = []
    _MARKET_HIGHLIGHTS_AT = 0.0
    try:
        from market_universe import clear_runtime_caches as clear_universe_caches
        clear_universe_caches()
    except Exception:
        pass


def _headers() -> dict[str, str]:
    token = os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UPSTOX_ANALYTICS_TOKEN is not configured.")
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def _get(url: str, *, params: dict | None = None, timeout: int = _TIMEOUT) -> dict:
    r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") not in (None, "success"):
        raise RuntimeError(str(payload.get("message") or "Upstox API error"))
    return payload


def _cached(key: str, factory, ttl: float):
    now = datetime.now(timezone.utc).timestamp()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = factory()
    _CACHE[key] = (now, value)
    return value


def _series(instrument_key: str, unit: str = "months") -> list[tuple[datetime, float]]:
    def load():
        end = date.today()
        start = end - timedelta(days=365 * 5 + 45)
        encoded = quote(instrument_key, safe="")
        url = f"{BASE}/historical-candle/{encoded}/{unit}/1/{end.isoformat()}/{start.isoformat()}"
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

    return _cached(f"history:{unit}:" + instrument_key, load, _HISTORY_TTL)


def _metrics(rows: list[tuple[datetime, float]]) -> dict:
    out = {
        "available": False,
        "sample_size": len(rows),
        "return_1y": None,
        "return_3y": None,
        "return_5y": None,
        "volatility_annualized": None,
        "max_drawdown": None,
    }
    if len(rows) < 20:
        return out

    latest_dt, latest = rows[-1]

    def nearest(days: int):
        target = latest_dt - timedelta(days=days)
        if not rows:
            return None
        return min(rows, key=lambda x: abs((x[0] - target).total_seconds()))[1]

    p1 = nearest(365)
    p3 = nearest(365 * 3)
    p5 = nearest(365 * 5)

    try:
        r1 = ((latest / p1) - 1) * 100 if p1 else None
        r3 = ((latest / p3) ** (1 / 3) - 1) * 100 if p3 else None
        r5 = ((latest / p5) ** (1 / 5) - 1) * 100 if p5 else None
    except (TypeError, ValueError, ZeroDivisionError):
        r1 = r3 = r5 = None

    weekly = [
        math.log(curr / prev)
        for (_, prev), (_, curr) in zip(rows[:-1], rows[1:])
        if prev > 0 and curr > 0
    ]
    if weekly:
        mean = sum(weekly) / len(weekly)
        variance = sum((x - mean) ** 2 for x in weekly) / len(weekly)
        out["volatility_annualized"] = round(math.sqrt(variance) * math.sqrt(52) * 100, 2)

    peak = rows[0][1]
    drawdown = 0.0
    for _, price in rows:
        peak = max(peak, price)
        drawdown = min(drawdown, price / peak - 1)

    out.update({
        "available": any(v is not None for v in (r1, r3, r5)),
        "return_1y": round(r1, 2) if r1 is not None else None,
        "return_3y": round(r3, 2) if r3 is not None else None,
        "return_5y": round(r5, 2) if r5 is not None else None,
        "max_drawdown": round(drawdown * 100, 2),
    })
    return out


def _history_for_rows(rows: list[dict], category: str, limit: int | None = None, mf_cache: dict | None = None) -> list[dict]:
    """Enrich tracked rows only with Upstox historical candles."""
    if not rows:
        return []
    candidates = list(rows[:limit] if limit is not None else rows)
    def work(row):
        item = dict(row)
        key = row.get("instrument_key") or row.get("symbol")
        if not key:
            return item
        try:
            with _HISTORY_GATE:
                metrics = _metrics(_series(key, unit="months"))
            if metrics.get("available"):
                item.update(metrics)
                item["history_source"] = "Upstox historical candles"
        except Exception:
            pass
        return item
    out = []
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        futures=[pool.submit(work,row) for row in candidates]
        for future in as_completed(futures):
            try: out.append(future.result())
            except Exception: pass
    return out


def _history_for_fno_underlyings(rows: list[dict], limit: int | None = None) -> dict:
    """Average history of the unique underlyings represented by tracked F&O contracts."""
    unique = {}
    for row in rows:
        key = row.get("underlying_key")
        if key and key not in unique:
            unique[key] = {
                "instrument_key": key,
                "symbol": row.get("underlying"),
                "name": row.get("underlying") or row.get("name") or key,
                "volume": row.get("volume") or row.get("oi") or 0,
            }
    shortlisted = sorted(unique.values(), key=lambda x: x.get("volume") or 0, reverse=True)
    if limit is not None:
        shortlisted = shortlisted[:limit]
    history = _history_for_rows(shortlisted, "stocks", None)
    return {
        str(row.get("instrument_key")): row
        for row in history
        if row.get("instrument_key")
    }


def _category_metrics(rows: list[dict], default_vol: float) -> dict:
    valid = [x for x in rows if any(x.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))]
    result = {
        "available": bool(valid),
        "sample_size": len(valid),
        "tracked_count": len(rows),
        "history_count": len(valid),
        "history_coverage": round((len(valid) / len(rows)) * 100, 1) if rows else 0.0,
        "return_1y": None,
        "return_3y": None,
        "return_5y": None,
        "volatility_annualized": None,
    }
    for key in ("return_1y", "return_3y", "return_5y", "volatility_annualized"):
        values = [float(x[key]) for x in valid if x.get(key) is not None]
        if values:
            result[key] = round(sum(values) / len(values), 2)
    if result["volatility_annualized"] is None:
        result["volatility_annualized"] = default_vol
    result["average_basis"] = (
        f"Average of {len(valid)}/{len(rows)} tracked entities" if rows
        else "No tracked history available"
    )
    return result


def _load_live_universe() -> dict:
    global _SNAPSHOT, _SNAPSHOT_AT
    now = datetime.now(timezone.utc).timestamp()
    if _SNAPSHOT is not None and now - _SNAPSHOT_AT < _SNAPSHOT_TTL:
        return _SNAPSHOT

    loaders = {
        "stocks": compare_stocks,
        "fno": compare_fno,
        "bonds": compare_bonds,
        "mutual-funds": compare_mutual_funds,
        "fds": compare_fds,
        "gold": compare_gold,
        "commodities": compare_commodities,
        "currency": compare_currency,
    }

    snapshot = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(fn): key for key, fn in loaders.items()}
        for future in as_completed(jobs):
            key = jobs[future]
            try:
                snapshot[key] = future.result()
            except Exception:
                snapshot[key] = []

    # Upstox is the only market-data provider used by the application.
    snapshot["_fallback"] = {}

    _SNAPSHOT = snapshot
    _SNAPSHOT_AT = now
    return snapshot


def category_market_analysis(*, allow_stale: bool = False, force: bool = False) -> dict:
    global _ANALYSIS, _ANALYSIS_AT
    now_ts = datetime.now(timezone.utc).timestamp()
    if _ANALYSIS is not None and not force and (allow_stale or now_ts - _ANALYSIS_AT < _ANALYSIS_TTL):
        return _ANALYSIS

    now = datetime.now(timezone.utc).isoformat()
    snapshot = _load_live_universe()

    stocks = snapshot.get("stocks", [])[:TRACKING_LIMITS["stocks"]]
    fno = snapshot.get("fno", [])[:TRACKING_LIMITS["fno"]]
    bonds = snapshot.get("bonds", [])[:TRACKING_LIMITS["bonds"]]
    funds = snapshot.get("mutual-funds", [])[:TRACKING_LIMITS["mutual-funds"]]
    fds = snapshot.get("fds", [])
    gold = snapshot.get("gold", [])
    commodities = snapshot.get("commodities", [])[:TRACKING_LIMITS["commodities"]]
    currency = snapshot.get("currency", [])[:TRACKING_LIMITS["currency"]]

    # Calculate segment averages across the complete tracked universe.
    from market_universe import history_universe
    hu = history_universe()

    history_jobs = {
        "stocks": (hu.get("stocks", [])[:TRACKING_LIMITS["stocks"]], "stocks", None),
        "bonds": (hu.get("bonds", [])[:TRACKING_LIMITS["bonds"]], "bonds", None),
        "mutual-funds": (hu.get("mutual-funds", [])[:TRACKING_LIMITS["mutual-funds"]], "mutual-funds", None),
        "gold": (hu.get("gold", [])[:TRACKING_LIMITS["gold"]], "gold", None),
        "commodities": (hu.get("commodities", [])[:TRACKING_LIMITS["commodities"]], "commodities", None),
        "currency": (hu.get("currency", [])[:TRACKING_LIMITS["currency"]], "currency", None),
    }
    history_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_history_for_rows, rows, category, limit): category
            for category, (rows, _, limit) in history_jobs.items()
        }
        fno_future = pool.submit(
            _history_for_fno_underlyings,
            hu.get("fno", [])[:TRACKING_LIMITS["fno"]],
            None,
        )
        for future in as_completed([*futures.keys(), fno_future]):
            if future is fno_future:
                try:
                    history_results["fno_underlyings"] = future.result()
                except Exception:
                    history_results["fno_underlyings"] = {}
                continue
            category = futures[future]
            try:
                history_results[category] = future.result()
            except Exception:
                history_results[category] = []

    def merge(rows, history):
        by_key = {}
        for row in history:
            by_key[str(row.get("instrument_key") or row.get("symbol") or row.get("name"))] = row
        output = []
        for row in rows:
            key = str(row.get("instrument_key") or row.get("symbol") or row.get("name"))
            item = dict(by_key.get(key) or row)
            output.append(item)
        return output

    stocks = merge(stocks, history_results.get("stocks", []))
    fno_underlyings = history_results.get("fno_underlyings", {})
    enriched_fno = []
    for row in fno:
        item = dict(row)
        hist = fno_underlyings.get(str(row.get("underlying_key")))
        if hist:
            for key in ("return_1y", "return_3y", "return_5y", "volatility_annualized", "max_drawdown"):
                if hist.get(key) is not None:
                    item[key] = hist.get(key)
            item["history_source"] = "Underlying market history (Upstox)"
            item["underlying_history_available"] = True
        enriched_fno.append(item)
    fno = enriched_fno
    bonds = merge(bonds, history_results.get("bonds", []))
    funds = merge(funds, history_results.get("mutual-funds", []))
    gold = merge(gold, history_results.get("gold", []))
    commodities = merge(commodities, history_results.get("commodities", []))
    currency = merge(currency, history_results.get("currency", []))

    live_changes = [float(x["today_change"]) for x in stocks if x.get("today_change") is not None]
    adv = sum(1 for x in live_changes if x > 0)
    dec = sum(1 for x in live_changes if x < 0)
    breadth = round(50 + ((adv - dec) / len(live_changes)) * 50, 1) if live_changes else None

    stock_metrics = _category_metrics(history_results.get("stocks", []), 20.0)
    stock_metrics.update({
        "live_sample_size": len(snapshot.get("stocks", [])),
        "advancers": adv,
        "decliners": dec,
        "live_breadth_score": breadth,
    })

    fund_metrics = _category_metrics(history_results.get("mutual-funds", []), 14.0)
    fund_data_status = "upstox"

    bond_metrics = _category_metrics(history_results.get("bonds", []), 7.0)
    commodity_metrics = _category_metrics(history_results.get("commodities", []), 25.0)
    currency_metrics = _category_metrics(history_results.get("currency", []), 12.0)
    fno_metrics = _category_metrics(list(fno_underlyings.values()), 45.0)
    if fno_metrics.get("available"):
        fno_data_status = "upstox-underlying-history"
    else:
        fno_data_status = "upstox"
    bond_data_status = "upstox"
    gold_metrics = _category_metrics(history_results.get("gold", []), 16.0)

    # Market-analysis return values are never substituted with a
    # representative symbol or model proxy. They remain the exact averages
    # calculated from the tracked entities above.
    stock_data_status = "upstox"
    gold_data_status = "upstox"
    commodity_data_status = "upstox"
    currency_data_status = "upstox"

    fd_values = [float(x["rate"]) for x in fds if x.get("rate") is not None]
    fd_rate = round(sum(fd_values) / len(fd_values), 2) if fd_values else None
    fd_metrics = {
        "available": bool(fd_values),
        "sample_size": len(fds),
        "return_1y": fd_rate,
        "return_3y": fd_rate,
        "return_5y": fd_rate,
        "volatility_annualized": 1.0,
        "rate_average": fd_rate,
    }

    result = {
        "fd": {
            "status": "fallback-official-bank-rates",
            "source": "Official bank FD rate tables",
            "metrics": fd_metrics,
            "analyzed_options": fds,
            "updated_at": now,
        },
        "bonds": {
            "status": "fallback" if "bonds" in snapshot.get("_fallback", {}) else bond_data_status,
            "source": snapshot.get("_fallback", {}).get("bonds") or "Upstox listed bond/debt quotes",
            "metrics": bond_metrics,
            "analyzed_options": bonds,
            "updated_at": now,
        },
        "mutual-funds": {
            "status": "upstox",
            "source": "Upstox mutual-fund instrument master + historical candles",
            "metrics": fund_metrics,
            "analyzed_options": funds,
            "updated_at": now,
        },
        "gold": {
            "status": gold_data_status if "gold" not in snapshot.get("_fallback", {}) else "fallback",
            "source": snapshot.get("_fallback", {}).get("gold") or "Upstox MCX gold contracts",
            "metrics": gold_metrics,
            "analyzed_options": gold,
            "updated_at": now,
        },
        "commodities": {
            "status": commodity_data_status if "commodities" not in snapshot.get("_fallback", {}) else "fallback",
            "source": snapshot.get("_fallback", {}).get("commodities") or "Upstox MCX commodity contracts",
            "metrics": commodity_metrics,
            "analyzed_options": commodities,
            "updated_at": now,
        },
        "currency": {
            "status": currency_data_status if "currency" not in snapshot.get("_fallback", {}) else "fallback",
            "source": snapshot.get("_fallback", {}).get("currency") or "Upstox currency futures",
            "metrics": currency_metrics,
            "analyzed_options": currency,
            "updated_at": now,
        },
        "fno": {
            "status": "fallback" if "fno" in snapshot.get("_fallback", {}) else "upstox",
            "source": snapshot.get("_fallback", {}).get("fno") or "Upstox F&O market quotes + historical futures/options candles",
            "metrics": {
                **fno_metrics,
                "live_sample_size": len(fno),
                "active_contracts": len(fno),
            },
            "analyzed_options": fno,
            "updated_at": now,
        },
        "stocks": {
            "status": stock_data_status if "stocks" not in snapshot.get("_fallback", {}) else "fallback",
            "source": snapshot.get("_fallback", {}).get("stocks") or "Upstox full market quotes + historical candles",
            "metrics": stock_metrics,
            "analyzed_options": stocks,
            "updated_at": now,
        },
    }

    try:
        configured = tracking_universe()
    except Exception:
        configured = {"stocks": [], "fno": [], "bonds": []}

    result["_tracking"] = {
        "stocks_requested": TRACKING_LIMITS["stocks"],
        "stocks_tracked": len(stocks),
        "fno_requested": TRACKING_LIMITS["fno"],
        "fno_tracked": len(fno),
        "funds_requested": TRACKING_LIMITS["mutual-funds"],
        "funds_tracked": len(funds),
        "bonds_requested": TRACKING_LIMITS["bonds"],
        "bonds_tracked": len(bonds),
        "fds_tracked": len(fds),
        "configured_stocks": len(configured.get("stocks", [])),
        "configured_fno": len(configured.get("fno", [])),
        "configured_bonds": len(configured.get("bonds", [])),
        "configured_funds": len(funds),
        "live_stock_quotes": len(snapshot.get("stocks", [])),
        "live_fno_quotes": len(snapshot.get("fno", [])),
        "live_bond_quotes": len(snapshot.get("bonds", [])),
        "updated_at": now,
    }
    result["_tracking"]["ready"] = (
        result["_tracking"]["configured_stocks"] >= 20
        and result["_tracking"]["configured_fno"] >= 10
        and result["_tracking"]["configured_funds"] >= 20
        and result["_tracking"]["configured_bonds"] >= 5
        and len(fds) >= 8
    )
    result["_tracking"]["message"] = (
        "The configured market universe is screened with Upstox market quotes "
        "and Upstox historical candles. No public market-data provider is used."
    )

    _ANALYSIS = result
    _ANALYSIS_AT = now_ts
    return result


_MARKET_HIGHLIGHTS = []
_MARKET_HIGHLIGHTS_AT = 0.0
_MARKET_HIGHLIGHTS_TTL = 45


def _market_row(label, value, change, kind, unit=None, freshness="live", **extra):
    return {
        "label": label,
        "value": value,
        "today_change": change,
        "kind": kind,
        "unit": unit,
        "freshness": freshness,
        **extra,
    }


def market_highlights() -> list[dict]:
    """Return the homepage Market Now board using Upstox only."""
    global _MARKET_HIGHLIGHTS, _MARKET_HIGHLIGHTS_AT
    now = datetime.now(timezone.utc).timestamp()
    if _MARKET_HIGHLIGHTS and now - _MARKET_HIGHLIGHTS_AT < _MARKET_HIGHLIGHTS_TTL:
        return _MARKET_HIGHLIGHTS
    try:
        items = market_now()
    except Exception:
        items = []
    _MARKET_HIGHLIGHTS = items
    _MARKET_HIGHLIGHTS_AT = now
    return items


def market_snapshot() -> dict:
    analysis = category_market_analysis(allow_stale=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "upstox-primary",
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
            if key != "_tracking"
        ],
        "message": "Upstox is the sole market-data source for FinanX market quotes and historical market analysis.",
    }


def healthcheck() -> dict:
    if not configured():
        return {"configured": False, "reachable": False, "error": "UPSTOX_ANALYTICS_TOKEN is missing."}
    try:
        from market_universe import compare_stocks
        rows = compare_stocks()[:1]
        return {
            "configured": True,
            "reachable": bool(rows),
            "sample": rows[0] if rows else None,
            "enabled_segments": ["NSE_EQ", "BSE_EQ", "NSE_FO", "BSE_FO", "MCX_FO", "NCD_FO", "BCD_FO"],
        }
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "error": str(exc),
            "enabled_segments": ["NSE_EQ", "BSE_EQ", "NSE_FO", "BSE_FO", "MCX_FO", "NCD_FO", "BCD_FO"],
        }
