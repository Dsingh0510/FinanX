from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

from market_universe import (
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
_SNAPSHOT_TTL = 8 * 60
_ANALYSIS_TTL = 5 * 60
_CACHE = {}
_SNAPSHOT = None
_SNAPSHOT_AT = 0.0
_ANALYSIS = None
_ANALYSIS_AT = 0.0


def configured() -> bool:
    return bool(os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip())


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


def _series(instrument_key: str) -> list[tuple[datetime, float]]:
    def load():
        end = date.today()
        start = end - timedelta(days=365 * 5 + 45)
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

    return _cached("history:" + instrument_key, load, _ANALYSIS_TTL)


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


def _mfapi_metrics(scheme_key: str) -> dict:
    # Fallback only: Upstox is attempted first for MF history.
    code = str(scheme_key).split("|")[-1]
    try:
        r = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=8)
        r.raise_for_status()
        payload = r.json()
        rows = []
        for item in payload.get("data", []) or []:
            try:
                dt = datetime.strptime(item["date"], "%d-%m-%Y")
                nav = float(item["nav"])
            except Exception:
                continue
            if nav > 0:
                rows.append((dt, nav))
        rows.sort(key=lambda x: x[0])
        if not rows:
            return {}
        latest_dt, latest = rows[-1]

        def nearest(days):
            target = latest_dt - timedelta(days=days)
            return min(rows, key=lambda x: abs((x[0] - target).days))[1]

        p1, p3, p5 = nearest(365), nearest(365 * 3), nearest(365 * 5)
        return {
            "available": True,
            "sample_size": len(rows),
            "return_1y": round(((latest / p1) - 1) * 100, 2) if p1 else None,
            "return_3y": round(((latest / p3) ** (1 / 3) - 1) * 100, 2) if p3 else None,
            "return_5y": round(((latest / p5) ** (1 / 5) - 1) * 100, 2) if p5 else None,
            "source": "MFAPI fallback",
        }
    except Exception:
        return {}


def _history_for_rows(rows: list[dict], category: str, limit: int) -> list[dict]:
    if not rows:
        return []

    # Screen the complete live universe first, then spend historical requests
    # only on the shortlist so the analysis stays fast.
    candidates = list(rows)
    if category in {"stocks", "bonds", "commodities", "currency", "fno"}:
        candidates.sort(key=lambda x: x.get("volume") or x.get("oi") or 0, reverse=True)
    candidates = candidates[:limit]

    def work(row):
        item = dict(row)
        key = row.get("instrument_key") or row.get("symbol")
        if not key:
            return item
        try:
            metrics = _metrics(_series(key))
            if metrics.get("available"):
                item.update(metrics)
                item["history_source"] = "Upstox historical candles"
                return item
        except Exception:
            pass

        if category == "mutual-funds":
            # Upstox provides the full MF scheme master and latest NAV. For
            # historical returns, use already-cached AMFI metrics first; only
            # then fall back to MFAPI for a scheme we can resolve.
            try:
                from database import mutual_fund_metrics
                target_name = str(row.get("name", "")).strip().lower()
                cached = next(
                    (
                        x for x in mutual_fund_metrics()
                        if str(x.get("scheme_name", "")).strip().lower() == target_name
                        and any(x.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))
                    ),
                    None,
                )
                if cached:
                    item.update({
                        "return_1y": cached.get("return_1y"),
                        "return_3y": cached.get("return_3y"),
                        "return_5y": cached.get("return_5y"),
                    })
                    item["history_source"] = cached.get("source") or "AMFI cached history"
                    return item
            except Exception:
                pass
            fallback = _mfapi_metrics(key)
            if fallback:
                item.update(fallback)
                item["history_source"] = "MFAPI fallback"
        return item

    out = []
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        futures = [pool.submit(work, row) for row in candidates]
        for future in as_completed(futures):
            try:
                out.append(future.result())
            except Exception:
                pass

    return out


def _history_for_fno_underlyings(rows: list[dict], limit: int = 6) -> dict:
    """Get historical performance for the underlying assets of the leading F&O contracts."""
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
    shortlisted = sorted(unique.values(), key=lambda x: x.get("volume") or 0, reverse=True)[:limit]
    history = _history_for_rows(shortlisted, "stocks", limit)
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

    # Enrich Upstox mutual-fund rows with cached historical NAV returns.
    # Upstox remains primary for the live fund universe/latest NAV; AMFI is used
    # only for historical-return fields that Upstox's MF instrument master does
    # not expose.
    if snapshot.get("mutual-funds"):
        try:
            from database import mutual_fund_metrics
            cached = mutual_fund_metrics()
            if not cached:
                from amfi_data import update_amfi_metrics_fast
                update_amfi_metrics_fast()
                cached = mutual_fund_metrics()
            by_name = {
                str(x.get("scheme_name", "")).strip().lower(): x
                for x in cached
                if any(x.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))
            }
            for row in snapshot["mutual-funds"]:
                key = str(row.get("name", "")).strip().lower()
                hist = by_name.get(key)
                if hist:
                    row["return_1y"] = hist.get("return_1y")
                    row["return_3y"] = hist.get("return_3y")
                    row["return_5y"] = hist.get("return_5y")
                    row["history_source"] = hist.get("source") or "AMFI cached history"
        except Exception:
            pass

    # Fallback sources are touched only when the primary Upstox segment call
    # fails or returns no usable records.
    snapshot["_fallback"] = {}
    if any(not snapshot.get(key) for key in ("stocks", "fno", "bonds", "mutual-funds", "gold", "commodities", "currency")):
        try:
            import vercel_market
            fallback = vercel_market.category_market_analysis()
            for key in ("stocks", "fno", "bonds", "mutual-funds", "gold", "commodities", "currency"):
                if snapshot.get(key):
                    continue
                item = fallback.get(key) or {}
                rows = item.get("analyzed_options") or []
                if rows:
                    snapshot[key] = rows
                    snapshot["_fallback"][key] = item.get("source") or "fallback source"
        except Exception:
            pass

    # AMFI is used only as a second-stage fallback for mutual-fund history.
    if not snapshot.get("mutual-funds"):
        try:
            from amfi_data import tracking_fund_universe
            snapshot["mutual-funds"] = [
                {"name": name, "source": "AMFI fallback"}
                for name in tracking_fund_universe(100)
            ]
            snapshot["_fallback"]["mutual-funds"] = "AMFI fallback"
        except Exception:
            pass

    _SNAPSHOT = snapshot
    _SNAPSHOT_AT = now
    return snapshot


def category_market_analysis() -> dict:
    global _ANALYSIS, _ANALYSIS_AT
    now_ts = datetime.now(timezone.utc).timestamp()
    if _ANALYSIS is not None and now_ts - _ANALYSIS_AT < _ANALYSIS_TTL:
        return _ANALYSIS

    now = datetime.now(timezone.utc).isoformat()
    snapshot = _load_live_universe()

    stocks = snapshot.get("stocks", [])[:100]
    fno = snapshot.get("fno", [])[:100]
    bonds = snapshot.get("bonds", [])[:50]
    funds = snapshot.get("mutual-funds", [])[:100]
    fds = snapshot.get("fds", [])
    gold = snapshot.get("gold", [])
    commodities = snapshot.get("commodities", [])[:50]
    currency = snapshot.get("currency", [])[:50]

    # Historical requests are parallel and limited to the shortlist. Live
    # quotes already screened the complete universe.
    history_jobs = {
        "stocks": (stocks, "stocks", 12),
        "bonds": (bonds, "bonds", 8),
        "mutual-funds": (funds, "mutual-funds", 12),
        "gold": (gold, "gold", 2),
        "commodities": (commodities, "commodities", 3),
        "currency": (currency, "currency", 3),
    }
    history_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_history_for_rows, rows, category, limit): category
            for category, (rows, _, limit) in history_jobs.items()
        }
        fno_future = pool.submit(_history_for_fno_underlyings, fno, 6)
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

    stock_metrics = _category_metrics(stocks, 20.0)
    stock_metrics.update({
        "live_sample_size": len(snapshot.get("stocks", [])),
        "advancers": adv,
        "decliners": dec,
        "live_breadth_score": breadth,
    })

    fund_metrics = _category_metrics(funds, 14.0)
    fund_data_status = "upstox"
    if not fund_metrics.get("available"):
        try:
            from amfi_data import category_metrics, update_amfi_metrics_fast
            amfi_metrics = category_metrics()
            if not amfi_metrics.get("available"):
                update_amfi_metrics_fast()
                amfi_metrics = category_metrics()
            if amfi_metrics.get("available"):
                fund_metrics.update({
                    "available": True,
                    "sample_size": amfi_metrics.get("sample_size", 0),
                    "return_1y": amfi_metrics.get("return_1y"),
                    "return_3y": amfi_metrics.get("return_3y"),
                    "return_5y": amfi_metrics.get("return_5y"),
                })
                fund_data_status = "upstox+amfi-history"
        except Exception:
            pass

    bond_metrics = _category_metrics(bonds, 7.0)
    fno_metrics = _category_metrics(fno, 45.0)
    if fno_metrics.get("available"):
        fno_data_status = "upstox-underlying-history"
    else:
        fno_data_status = "upstox"
    bond_data_status = "upstox"
    if not bond_metrics.get("available"):
        try:
            from amfi_data import bond_proxy_metrics, update_bond_proxy_metrics_fast
            proxy = bond_proxy_metrics()
            if not proxy.get("available"):
                update_bond_proxy_metrics_fast()
                proxy = bond_proxy_metrics()
            if proxy.get("available"):
                for key in ("return_1y", "return_3y", "return_5y"):
                    if proxy.get(key) is not None:
                        bond_metrics[key] = proxy.get(key)
                bond_metrics["available"] = True
                bond_metrics["sample_size"] = proxy.get("sample_size") or bond_metrics.get("sample_size", 0)
                bond_metrics["volatility_annualized"] = bond_metrics.get("volatility_annualized") or 7.0
                bond_data_status = "fallback"
        except Exception:
            pass
    gold_metrics = _category_metrics(gold, 16.0)
    commodity_metrics = _category_metrics(commodities, 25.0)
    currency_metrics = _category_metrics(currency, 12.0)

    # If Upstox historical candles are unavailable for a segment, use a
    # targeted public-data fallback for that segment only. This keeps Upstox
    # primary while avoiding fake baseline return numbers in the UI.
    fallback_analysis = {}
    try:
        import vercel_market
        fallback_analysis = vercel_market.category_market_analysis()
    except Exception:
        fallback_analysis = {}

    def enrich_with_fallback(category, metrics):
        if metrics.get("available"):
            return metrics, "upstox"
        fb = fallback_analysis.get(category) or {}
        fbm = fb.get("metrics") or {}
        if fbm.get("available") and any(fbm.get(k) is not None for k in ("return_1y", "return_3y", "return_5y")):
            merged = dict(metrics)
            for key in ("return_1y", "return_3y", "return_5y", "volatility_annualized"):
                if fbm.get(key) is not None:
                    merged[key] = fbm.get(key)
            merged["available"] = True
            merged["sample_size"] = fbm.get("sample_size") or metrics.get("sample_size", 0)
            return merged, "fallback"
        return metrics, "upstox"

    stock_metrics, stock_data_status = enrich_with_fallback("stocks", stock_metrics)
    gold_metrics, gold_data_status = enrich_with_fallback("gold", gold_metrics)
    commodity_metrics, commodity_data_status = enrich_with_fallback("commodities", commodity_metrics)
    currency_metrics, currency_data_status = enrich_with_fallback("currency", currency_metrics)

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
            "source": snapshot.get("_fallback", {}).get("bonds") or ("AMFI corporate-bond proxy history + Upstox listed bond/debt quotes" if bond_data_status == "fallback" else "Upstox listed bond/debt quotes"),
            "metrics": bond_metrics,
            "analyzed_options": bonds,
            "updated_at": now,
        },
        "mutual-funds": {
            "status": "fallback" if "mutual-funds" in snapshot.get("_fallback", {}) else fund_data_status,
            "source": snapshot.get("_fallback", {}).get("mutual-funds") or "Upstox mutual-fund instrument master; historical fallback only where needed",
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
        "stocks_requested": 100,
        "stocks_tracked": len(stocks),
        "fno_requested": 100,
        "fno_tracked": len(fno),
        "funds_requested": 100,
        "funds_tracked": len(funds),
        "bonds_requested": 50,
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
        result["_tracking"]["configured_stocks"] >= 80
        and result["_tracking"]["configured_fno"] >= 80
        and result["_tracking"]["configured_funds"] >= 80
        and result["_tracking"]["configured_bonds"] >= 10
        and len(fds) >= 8
    )
    result["_tracking"]["message"] = (
        "The full configured market universe is screened with Upstox first. "
        "Fallback data is used only where Upstox does not expose the required field."
    )

    _ANALYSIS = result
    _ANALYSIS_AT = now_ts
    return result


def _fallback_market_cards(missing_labels):
    """Use a public reference source only for labels Upstox could not supply."""
    out = {}
    try:
        import vercel_market
        if any(x in missing_labels for x in ("NIFTY 50", "Gold", "USD/INR")):
            for row in vercel_market.market_highlights():
                if row.get("label") in missing_labels:
                    row = dict(row)
                    row["freshness"] = "fallback"
                    out[row["label"]] = row
    except Exception:
        pass
    return out


def market_highlights() -> list[dict]:
    wanted = [
        "NIFTY 50",
        "Gold",
        "USD/INR",
        "NIFTY Bank",
        "NIFTY IT",
        "Reliance Industries",
        "HDFC Bank",
        "TCS",
        "India VIX",
    ]

    out = []
    try:
        live = {x.get("label"): x for x in market_now() if x.get("label")}
        missing = [label for label in wanted if label not in live]
        live.update(_fallback_market_cards(missing))
        out = [live[label] for label in wanted if label in live]
    except Exception:
        out = []

    return out[:9]

def market_snapshot() -> dict:
    analysis = category_market_analysis()
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
        "message": "Upstox is the primary market-data source; other sources are used only where the required data is not exposed by Upstox.",
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
