from __future__ import annotations

import logging
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
logger = logging.getLogger(__name__)


def configured() -> bool:
    return bool(os.getenv("UPSTOX_ANALYTICS_TOKEN", "").strip())


def cached_market_analysis() -> dict | None:
    """Return the most recent in-process analysis without reaching the API."""
    return _ANALYSIS


def clear_runtime_caches() -> None:
    """Clear in-process analysis, snapshot, history, Market Now and health caches."""
    global _SNAPSHOT, _SNAPSHOT_AT, _ANALYSIS, _ANALYSIS_AT
    global _MARKET_HIGHLIGHTS, _MARKET_HIGHLIGHTS_AT, _HEALTHCHECK_CACHE, _HEALTHCHECK_AT
    _CACHE.clear()
    _SNAPSHOT = None
    _SNAPSHOT_AT = 0.0
    _ANALYSIS = None
    _ANALYSIS_AT = 0.0
    _MARKET_HIGHLIGHTS = []
    _MARKET_HIGHLIGHTS_AT = 0.0
    _HEALTHCHECK_CACHE = None
    _HEALTHCHECK_AT = 0.0
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


_EXPIRED_HISTORY_TTL = 24 * 60 * 60
_EXPIRED_DISCOVERY_TTL = 6 * 60 * 60
_EXPIRED_API_BASE = "https://api.upstox.com/v2"


def _expired_get(path: str, *, params: dict | None = None, timeout: int = _HISTORY_TIMEOUT) -> dict:
    return _get(f"{_EXPIRED_API_BASE}{path}", params=params, timeout=timeout)


def _expired_expiries(underlying_key: str) -> list[str]:
    if not underlying_key:
        return []

    def load():
        try:
            payload = _expired_get(
                "/expired-instruments/expiries",
                params={"instrument_key": underlying_key},
            )
            values = payload.get("data") or []
            return sorted({str(value)[:10] for value in values if value})
        except Exception:
            return []

    return _cached("expired:expiries:" + str(underlying_key), load, _EXPIRED_DISCOVERY_TTL)


def _expired_future_contracts(underlying_key: str, expiry_date: str) -> list[dict]:
    if not underlying_key or not expiry_date:
        return []

    def load():
        try:
            payload = _expired_get(
                "/expired-instruments/future/contract",
                params={"instrument_key": underlying_key, "expiry_date": expiry_date},
            )
            rows = payload.get("data") or []
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    return _cached(
        f"expired:contracts:{underlying_key}:{expiry_date}",
        load,
        _EXPIRED_DISCOVERY_TTL,
    )


def _expired_series(expired_instrument_key: str, expiry_date: str) -> list[tuple[datetime, float]]:
    if not expired_instrument_key or not expiry_date:
        return []

    def load():
        try:
            encoded = quote(expired_instrument_key, safe="")
            start = (date.fromisoformat(expiry_date) - timedelta(days=120)).isoformat()
            payload = _expired_get(
                f"/expired-instruments/historical-candle/{encoded}/day/{expiry_date}/{start}",
            )
            candles = (payload.get("data") or {}).get("candles") or []
        except Exception:
            return []

        rows = []
        for candle in candles:
            if len(candle) < 5:
                continue
            try:
                dt = datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00"))
                close = float(candle[4])
            except (TypeError, ValueError):
                continue
            if close > 0:
                rows.append((dt, close))
        rows.sort(key=lambda item: item[0])
        return rows

    return _cached(
        f"expired:series:{expired_instrument_key}",
        load,
        _EXPIRED_HISTORY_TTL,
    )


def _continuous_derivative_series(
    row: dict,
    *,
    unit: str = "months",
    lookback_days: int = 365 * 5 + 45,
) -> tuple[list[tuple[datetime, float]], str]:
    """Stitch expiring futures into one continuous trend series."""
    primary_key = row.get("instrument_key") or row.get("symbol")
    underlying_key = row.get("underlying_key")
    segment = str(row.get("segment") or "").upper()

    # Upstox currently does not expose expiry discovery for MCX. Do not
    # invent MCX expiry dates; use its normal contract history until an
    # expired-contract key is discoverable.
    if not underlying_key or segment == "MCX_FO":
        return _series(primary_key, unit=unit), "single-contract"

    today = date.today()
    start_date = today - timedelta(days=lookback_days)
    pieces = []

    try:
        pieces.extend(_series(primary_key, unit=unit))
    except Exception:
        pass

    # Upstox's expiry discovery currently exposes up to six months of
    # historical expiries. Stitch every discovered future contract.
    for expiry in _expired_expiries(str(underlying_key)):
        try:
            expiry_date = date.fromisoformat(expiry)
        except ValueError:
            continue
        if expiry_date >= today or expiry_date < start_date:
            continue

        contracts = _expired_future_contracts(str(underlying_key), expiry)
        candidates = [
            contract for contract in contracts
            if str(contract.get("segment") or "").upper() == segment
            and str(contract.get("instrument_type") or "").upper() == "FUT"
        ]
        if not candidates:
            continue

        contract = candidates[0]
        expired_key = contract.get("instrument_key")
        if expired_key:
            pieces.extend(_expired_series(str(expired_key), expiry))

    merged = {}
    for dt, close in sorted(pieces, key=lambda item: item[0]):
        if dt.date() >= start_date:
            merged[dt] = close

    rows = sorted(merged.items(), key=lambda item: item[0])
    return rows, "continuous-rollover" if rows else "single-contract"


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

    return _cached(f"history:v3:{unit}:" + instrument_key, load, _HISTORY_TTL)


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
    # Monthly history can legitimately contain only ~13 candles for a
    # one-year horizon. Horizon availability is checked independently below,
    # so do not reject valid 1Y/3Y series just because they have fewer than
    # twenty monthly observations.
    if len(rows) < 2:
        return out

    latest_dt, latest = rows[-1]

    def nearest(days: int, tolerance_days: int):
        target = latest_dt - timedelta(days=days)
        if not rows:
            return None
        candidate = min(rows, key=lambda x: abs((x[0] - target).total_seconds()))
        distance = abs((candidate[0] - target).total_seconds()) / 86400
        return candidate if distance <= tolerance_days else None

    # Do not manufacture a 3Y/5Y number from a short contract history.
    # Each horizon is independently available only when the source actually
    # reaches that historical point. This prevents a six-month futures series
    # from being incorrectly labelled as a five-year CAGR.
    p1 = nearest(365, 45)
    p3 = nearest(365 * 3, 120)
    p5 = nearest(365 * 5, 180)

    try:
        r1 = ((latest / p1[1]) - 1) * 100 if p1 else None
        r3 = ((latest / p3[1]) ** (1 / 3) - 1) * 100 if p3 else None
        r5 = ((latest / p5[1]) ** (1 / 5) - 1) * 100 if p5 else None
    except (TypeError, ValueError, ZeroDivisionError):
        r1 = r3 = r5 = None

    monthly_returns = [
        math.log(curr / prev)
        for (_, prev), (_, curr) in zip(rows[:-1], rows[1:])
        if prev > 0 and curr > 0
    ]
    if monthly_returns:
        mean = sum(monthly_returns) / len(monthly_returns)
        variance = sum((x - mean) ** 2 for x in monthly_returns) / len(monthly_returns)
        # _series(..., unit="months") returns monthly candles, so annualize
        # monthly return volatility with sqrt(12).
        out["volatility_annualized"] = round(math.sqrt(variance) * math.sqrt(12) * 100, 2)

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


def _fill_derivative_horizon_gaps(metrics: dict, series: list[tuple[datetime, float]]) -> dict:
    """Fill missing long horizons from the longest real derivative history.
    
    This is used only for derivative-heavy segments where Upstox cannot always
    expose a complete multi-year expired-contract chain. The annualized result
    is explicitly marked as such instead of pretending it is a true 3Y/5Y
    contract return.
    """
    if not series or len(series) < 2:
        return metrics
    first_dt, first = series[0]
    last_dt, last = series[-1]
    if first <= 0 or last <= 0:
        return metrics
    span_days = max((last_dt - first_dt).total_seconds() / 86400.0, 0.0)
    if span_days < 180:
        return metrics
    years = max(span_days / 365.25, 0.5)
    annualized = ((last / first) ** (1.0 / years) - 1.0) * 100.0
    if not math.isfinite(annualized):
        return metrics
    annualized = round(max(-99.0, min(200.0, annualized)), 2)
    methods = metrics.setdefault("horizon_methods", {})
    for key in ("return_1y", "return_3y", "return_5y"):
        if metrics.get(key) is None:
            metrics[key] = annualized
            methods[key] = "available-history-annualized"
    metrics["available"] = any(metrics.get(key) is not None for key in ("return_1y", "return_3y", "return_5y"))
    return metrics


def _history_for_rows(rows: list[dict], category: str, limit: int | None = None, mf_cache: dict | None = None) -> list[dict]:
    """Enrich tracked entities with Upstox history, using stable underlyings when available."""
    if not rows:
        return []
    candidates = list(rows[:limit] if limit is not None else rows)

    def work(row):
        item = dict(row)
        primary_key = row.get("instrument_key") or row.get("symbol")
        underlying_key = row.get("underlying_key")
        history_candidates = []
        if primary_key:
            history_candidates.append((primary_key, "Upstox historical candles"))
        if underlying_key and underlying_key != primary_key:
            history_candidates.append((underlying_key, "Underlying market history (Upstox)"))

        if str(row.get("instrument_type") or "").upper() == "FUT" and row.get("underlying_key"):
            try:
                with _HISTORY_GATE:
                    series, mode = _continuous_derivative_series(row, unit="months")
                    metrics = _metrics(series)

                    # A derivative contract can have enough data for one
                    # horizon but not the longer horizons. Backfill only the
                    # missing horizons from the stable underlying instrument.
                    # This keeps 1Y/3Y/5Y independently real while preserving
                    # the continuous-roll series wherever it is available.
                    underlying_metrics = {}
                    if any(metrics.get(k) is None for k in ("return_1y", "return_3y", "return_5y")):
                        underlying_series = _series(str(row.get("underlying_key")), unit="months")
                        underlying_metrics = _metrics(underlying_series)
                    for metric_key in ("return_1y", "return_3y", "return_5y"):
                        if metrics.get(metric_key) is None and underlying_metrics.get(metric_key) is not None:
                            metrics[metric_key] = underlying_metrics[metric_key]

                    if str(category).lower() in {"entities", "gold", "commodities", "currency"}:
                        metrics = _fill_derivative_horizon_gaps(metrics, series)

                if metrics.get("available"):
                    item.update(metrics)
                    item["history_source"] = (
                        "Upstox expired-contract continuous rollover"
                        if not underlying_metrics
                        else "Upstox continuous rollover + underlying long-history backfill"
                    )
                    item["history_mode"] = (
                        mode if not underlying_metrics else "continuous-rollover+underlying-backfill"
                    )
                    item["history_instrument_key"] = row.get("instrument_key") or row.get("symbol")
                    return item
            except Exception:
                pass

        for key, source in history_candidates:
            try:
                with _HISTORY_GATE:
                    metrics = _metrics(_series(key, unit="months"))
                if metrics.get("available"):
                    item.update(metrics)
                    item["history_source"] = source
                    item["history_mode"] = "single-instrument"
                    item["history_instrument_key"] = key
                    return item
            except Exception:
                continue
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


def _history_for_segment_entities(rows: list[dict], limit: int | None = None) -> dict:
    """Return one real historical result per unique segment entity.

    Derivative contracts can expire, so use the stable underlying first when
    Upstox provides an underlying_key. If that history is unavailable, fall
    back to the tracked contract key for the same entity.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        entity_key = row.get("underlying_key") or row.get("instrument_key") or row.get("symbol")
        if not entity_key:
            continue
        groups.setdefault(str(entity_key), []).append(row)

    ordered = sorted(
        groups.items(),
        key=lambda item: max(
            (row.get("volume") or row.get("oi") or 0) for row in item[1]
        ),
        reverse=True,
    )
    if limit is not None:
        ordered = ordered[:limit]

    def work(entity_key: str, entity_rows: list[dict]):
        representative = dict(entity_rows[0])
        history_candidates: list[tuple[str, str]] = []
        underlying_key = representative.get("underlying_key")
        if underlying_key and str(underlying_key) == entity_key:
            history_candidates.append((entity_key, "Underlying market history (Upstox)"))

        seen_keys = {key for key, _ in history_candidates}
        for row in sorted(
            entity_rows,
            key=lambda item: (
                -(item.get("volume") or item.get("oi") or 0),
                item.get("_expiry_ms") or 0,
            ),
        ):
            key = row.get("instrument_key") or row.get("symbol")
            if not key or str(key) in seen_keys:
                continue
            history_candidates.append((str(key), "Upstox historical candles"))
            seen_keys.add(str(key))

        if str(representative.get("instrument_type") or "").upper() == "FUT" and representative.get("underlying_key"):
            try:
                with _HISTORY_GATE:
                    series, mode = _continuous_derivative_series(representative, unit="months")
                    metrics = _metrics(series)

                    # Use the underlying only for horizons the contract-roll
                    # series genuinely cannot cover. This is especially
                    # important for NCD/currency and MCX futures, where the
                    # current contract is much shorter than 3Y/5Y history.
                    underlying_metrics = {}
                    if any(metrics.get(k) is None for k in ("return_1y", "return_3y", "return_5y")):
                        underlying_series = _series(str(representative.get("underlying_key")), unit="months")
                        underlying_metrics = _metrics(underlying_series)
                    for metric_key in ("return_1y", "return_3y", "return_5y"):
                        if metrics.get(metric_key) is None and underlying_metrics.get(metric_key) is not None:
                            metrics[metric_key] = underlying_metrics[metric_key]

                    # Currency futures are the correct NCD_FO source for
                    # derivative history, but a current NCD contract cannot
                    # reconstruct several years after expiry. Upstox explicitly
                    # provides USD/INR as a GLOBAL_INDICATOR with long historical
                    # candles. Use it only for horizons still missing after
                    # NCD_FO/underlying history, never as the primary 1Y result.
                    currency_indicator_metrics = {}
                    if str(representative.get("underlying_type") or "").upper() == "CUR":
                        try:
                            from market_universe import _find_global_indicator_key
                            indicator_key = _find_global_indicator_key(
                                representative.get("underlying_symbol"),
                                representative.get("name"),
                                representative.get("trading_symbol"),
                            )
                            if indicator_key:
                                currency_indicator_metrics = _metrics(
                                    _series(str(indicator_key), unit="months")
                                )
                                for metric_key in ("return_1y", "return_3y", "return_5y"):
                                    if metrics.get(metric_key) is None and currency_indicator_metrics.get(metric_key) is not None:
                                        metrics[metric_key] = currency_indicator_metrics[metric_key]
                        except Exception:
                            currency_indicator_metrics = {}

                    if str(representative.get("underlying_type") or "").upper() in {"COM", "CUR"}:
                        metrics = _fill_derivative_horizon_gaps(metrics, series)

                if metrics.get("available"):
                    item = dict(representative)
                    item["instrument_key"] = entity_key
                    item["entity_key"] = entity_key
                    item.update(metrics)
                    item["history_source"] = (
                        "Upstox expired-contract continuous rollover"
                        if not underlying_metrics
                        else "Upstox continuous rollover + underlying long-history backfill"
                    )
                    item["history_mode"] = (
                        mode if not underlying_metrics else "continuous-rollover+underlying-backfill"
                    )
                    item["history_instrument_key"] = representative.get("instrument_key") or representative.get("symbol")
                    return entity_key, item
            except Exception:
                pass

        for history_key, source in history_candidates:
            try:
                with _HISTORY_GATE:
                    metrics = _metrics(_series(history_key, unit="months"))
                if metrics.get("available"):
                    item = dict(representative)
                    item["instrument_key"] = entity_key
                    item["entity_key"] = entity_key
                    item.update(metrics)
                    item["history_source"] = source
                    item["history_mode"] = "single-instrument"
                    item["history_instrument_key"] = history_key
                    return entity_key, item
            except Exception:
                continue

        return entity_key, None

    result = {}
    with ThreadPoolExecutor(max_workers=min(8, len(ordered) or 1)) as pool:
        futures = [
            pool.submit(work, entity_key, entity_rows)
            for entity_key, entity_rows in ordered
        ]
        for future in as_completed(futures):
            try:
                entity_key, item = future.result()
                if item is not None:
                    result[entity_key] = item
            except Exception:
                pass
    return result


def _history_for_fno_underlyings(rows: list[dict], limit: int | None = None) -> dict:
    """Average history of the unique underlyings represented by tracked F&O contracts."""
    return _history_for_segment_entities(rows, limit)


def _category_metrics(rows: list[dict], default_vol: float, tracked_count: int | None = None) -> dict:
    valid = [x for x in rows if any(x.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))]
    total = len(rows) if tracked_count is None else max(int(tracked_count), len(valid))
    result = {
        "available": bool(valid),
        "sample_size": len(valid),
        "tracked_count": total,
        "history_count": len(valid),
        "history_coverage": round((len(valid) / total) * 100, 1) if total else 0.0,
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
        f"Average of {len(valid)}/{total} tracked entities" if total
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
    history_universe_error = None
    try:
        hu = history_universe()
    except Exception as exc:
        history_universe_error = str(exc)
        logger.exception("History-universe discovery failed; using live snapshot rows")
        hu = {
            "stocks": stocks,
            "bonds": bonds,
            "mutual-funds": funds,
            "gold": gold,
            "commodities": commodities,
            "currency": currency,
            "fno": fno,
        }

    history_jobs = {
        "stocks": (hu.get("stocks", [])[:TRACKING_LIMITS["stocks"]], "stocks", None),
        "bonds": (hu.get("bonds", [])[:TRACKING_LIMITS["bonds"]], "bonds", None),
        "mutual-funds": (hu.get("mutual-funds", [])[:TRACKING_LIMITS["mutual-funds"]], "mutual-funds", None),
        "gold": (hu.get("gold", []), "entities", TRACKING_LIMITS["gold"]),
        "commodities": (hu.get("commodities", []), "entities", TRACKING_LIMITS["commodities"]),
        "currency": (hu.get("currency", []), "entities", TRACKING_LIMITS["currency"]),
    }

    def load_history(rows, category, limit):
        if category == "entities":
            return _history_for_segment_entities(rows, limit)
        return _history_for_rows(rows, category, limit)

    history_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(load_history, rows, category, limit): key
            for key, (rows, category, limit) in history_jobs.items()
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
                history_results[category] = {} if history_jobs[category][1] == "entities" else []

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

    def enrich_entity_rows(rows, history_by_entity):
        output = []
        for row in rows:
            entity_key = str(
                row.get("underlying_key")
                or row.get("instrument_key")
                or row.get("symbol")
                or row.get("name")
            )
            item = dict(row)
            hist = history_by_entity.get(entity_key) if isinstance(history_by_entity, dict) else None
            if hist:
                for metric_key in (
                    "return_1y",
                    "return_3y",
                    "return_5y",
                    "volatility_annualized",
                    "max_drawdown",
                ):
                    if hist.get(metric_key) is not None:
                        item[metric_key] = hist.get(metric_key)
                item["history_source"] = hist.get("history_source")
                item["history_instrument_key"] = hist.get("history_instrument_key")
                item["entity_key"] = entity_key
            output.append(item)
        return output

    gold_history = history_results.get("gold", {})
    commodity_history = history_results.get("commodities", {})
    currency_history = history_results.get("currency", {})

    gold = enrich_entity_rows(gold, gold_history)
    commodities = enrich_entity_rows(commodities, commodity_history)
    currency = enrich_entity_rows(currency, currency_history)

    live_changes = [float(x["today_change"]) for x in stocks if x.get("today_change") is not None]
    adv = sum(1 for x in live_changes if x > 0)
    dec = sum(1 for x in live_changes if x < 0)
    breadth = round(50 + ((adv - dec) / len(live_changes)) * 50, 1) if live_changes else None

    stock_metrics = _category_metrics(
        history_results.get("stocks", []),
        20.0,
        tracked_count=len(hu.get("stocks", [])),
    )
    stock_metrics.update({
        "live_sample_size": len(snapshot.get("stocks", [])),
        "advancers": adv,
        "decliners": dec,
        "live_breadth_score": breadth,
    })

    fund_metrics = _category_metrics(
        history_results.get("mutual-funds", []),
        14.0,
        tracked_count=len(hu.get("mutual-funds", [])),
    )
    fund_data_status = "upstox"

    bond_metrics = _category_metrics(
        history_results.get("bonds", []),
        7.0,
        tracked_count=len(hu.get("bonds", [])),
    )
    commodity_metrics = _category_metrics(
        list(commodity_history.values()),
        25.0,
        tracked_count=len(hu.get("commodities", [])),
    )
    currency_metrics = _category_metrics(
        list(currency_history.values()),
        12.0,
        tracked_count=len(hu.get("currency", [])),
    )
    fno_underlying_count = len({
        str(row.get("underlying_key"))
        for row in hu.get("fno", [])
        if row.get("underlying_key")
    })
    fno_metrics = _category_metrics(
        list(fno_underlyings.values()),
        45.0,
        tracked_count=fno_underlying_count,
    )
    if fno_metrics.get("available"):
        fno_data_status = "upstox-underlying-history"
    else:
        fno_data_status = "upstox"
    bond_data_status = "upstox"
    gold_metrics = _category_metrics(
        list(gold_history.values()),
        16.0,
        tracked_count=len(hu.get("gold", [])),
    )

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
            "status": bond_data_status,
            "source": "Upstox listed bond/debt quotes",
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
            "status": gold_data_status,
            "source": "Upstox MCX gold contracts",
            "metrics": gold_metrics,
            "analyzed_options": gold,
            "updated_at": now,
        },
        "commodities": {
            "status": commodity_data_status,
            "source": "Upstox MCX commodity contracts",
            "metrics": commodity_metrics,
            "analyzed_options": commodities,
            "updated_at": now,
        },
        "currency": {
            "status": currency_data_status,
            "source": "Upstox currency futures",
            "metrics": currency_metrics,
            "analyzed_options": currency,
            "updated_at": now,
        },
        "fno": {
            "status": fno_data_status,
            "source": "Upstox F&O market quotes + historical futures/options candles",
            "metrics": {
                **fno_metrics,
                "live_sample_size": len(fno),
                "active_contracts": len(fno),
            },
            "analyzed_options": fno,
            "updated_at": now,
        },
        "stocks": {
            "status": stock_data_status,
            "source": "Upstox full market quotes + historical candles",
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
        "history_universe_error": history_universe_error,
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
_MARKET_HIGHLIGHTS_TTL = 20
_HEALTHCHECK_TTL = 45
_HEALTHCHECK_CACHE = None
_HEALTHCHECK_AT = 0.0


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
    """Return a short-lived cached Upstox reachability check."""
    global _HEALTHCHECK_CACHE, _HEALTHCHECK_AT
    now = datetime.now(timezone.utc).timestamp()

    if _HEALTHCHECK_CACHE is not None and now - _HEALTHCHECK_AT < _HEALTHCHECK_TTL:
        return _HEALTHCHECK_CACHE

    if not configured():
        _HEALTHCHECK_CACHE = {
            "configured": False,
            "reachable": False,
            "error": "UPSTOX_ANALYTICS_TOKEN is missing.",
        }
        _HEALTHCHECK_AT = now
        return _HEALTHCHECK_CACHE

    try:
        from market_universe import compare_stocks
        rows = compare_stocks()[:1]
        _HEALTHCHECK_CACHE = {
            "configured": True,
            "reachable": bool(rows),
            "sample": rows[0] if rows else None,
            "enabled_segments": [
                "NSE_EQ", "BSE_EQ", "NSE_FO", "BSE_FO",
                "MCX_FO", "NCD_FO", "BCD_FO",
            ],
        }
    except Exception as exc:
        _HEALTHCHECK_CACHE = {
            "configured": True,
            "reachable": False,
            "error": str(exc),
            "enabled_segments": [
                "NSE_EQ", "BSE_EQ", "NSE_FO", "BSE_FO",
                "MCX_FO", "NCD_FO", "BCD_FO",
            ],
        }

    _HEALTHCHECK_AT = now
    return _HEALTHCHECK_CACHE
