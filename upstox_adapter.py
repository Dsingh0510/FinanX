from __future__ import annotations

import logging
import json
import math
import os
import re
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



_INDEX_METRICS_CACHE = None
_INDEX_METRICS_AT = 0.0
_INDEX_METRICS_TTL = 6 * 60 * 60


def _nifty500_index_metrics() -> dict:
    """Load Nifty 500 index history, with labelled provider/index fallbacks."""
    global _INDEX_METRICS_CACHE, _INDEX_METRICS_AT
    now_ts = datetime.now(timezone.utc).timestamp()
    if _INDEX_METRICS_CACHE and now_ts - _INDEX_METRICS_AT < _INDEX_METRICS_TTL:
        return dict(_INDEX_METRICS_CACHE)

    # Prefer an exact index instrument key discovered from Upstox's instrument
    # master. Do not accidentally use a stock or ETF as the index itself.
    candidates = []
    try:
        from market_universe import instruments, _instrument_key
        rows = instruments()
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("instruments") or []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            label = " ".join(str(row.get(k) or "") for k in (
                "name", "trading_symbol", "short_name", "instrument_type",
                "segment", "exchange",
            )).upper()
            key = str(_instrument_key(row) or "")
            if "NIFTY 500" in label and key and (
                "INDEX" in str(row.get("segment") or "").upper()
                or "INDEX" in key.upper()
            ):
                candidates.append(key)
    except Exception:
        logger.exception("Unable to discover Nifty 500 in Upstox instrument master")

    for key in dict.fromkeys(candidates):
        try:
            series = _series(key, unit="months")
            metrics = _metrics(series)
            if metrics.get("return_1y") is not None:
                metrics.update({
                    "index_name": "Nifty 500",
                    "benchmark_source": "Upstox historical index candles",
                    "benchmark_symbol": key,
                    "benchmark_fallback": False,
                })
                _INDEX_METRICS_CACHE, _INDEX_METRICS_AT = metrics, now_ts
                return dict(metrics)
        except Exception:
            logger.exception("Upstox Nifty 500 history failed for %s", key)

    # Public-history fallback: Nifty 500 first, then Nifty 50 only if the
    # Nifty 500 series is unavailable. Keep the displayed benchmark name honest.
    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = end_ts - (365 * 5 + 220) * 24 * 60 * 60
    for ticker, label in (("^CRSLDX", "Nifty 500"), ("^NSEI", "Nifty 50")):
        try:
            response = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}",
                params={"period1": start_ts, "period2": end_ts, "interval": "1mo", "events": "history"},
                headers={"User-Agent": "Mozilla/5.0 FinanX/1.0"},
                timeout=_HISTORY_TIMEOUT,
            )
            response.raise_for_status()
            chart = (response.json().get("chart") or {})
            result_rows = chart.get("result") or []
            if not result_rows:
                continue
            item = result_rows[0]
            timestamps = item.get("timestamp") or []
            quote_rows = ((item.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote_rows.get("close") or []
            series = []
            for stamp, close in zip(timestamps, closes):
                if close is None:
                    continue
                try:
                    price = float(close)
                    dt = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    continue
                if price > 0:
                    series.append((dt, price))
            series.sort(key=lambda x: x[0])
            metrics = _metrics(series)
            if metrics.get("return_1y") is None:
                continue
            metrics.update({
                "index_name": label,
                "benchmark_source": "Yahoo Finance historical index series (public fallback)",
                "benchmark_symbol": ticker,
                "benchmark_fallback": True,
            })
            _INDEX_METRICS_CACHE, _INDEX_METRICS_AT = metrics, now_ts
            return dict(metrics)
        except Exception:
            logger.exception("Public index history failed for %s", ticker)

    return {
        "available": False,
        "return_1y": None,
        "return_3y": None,
        "return_5y": None,
        "index_name": "Nifty 500",
        "benchmark_source": "unavailable",
        "benchmark_fallback": True,
    }

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


def _external_market_history(symbol: str, label: str) -> dict | None:
    """Fetch a public historical price series as a last-resort, labelled proxy.
    This is only used when the primary broker/history sources have no usable
    history. Proxy returns are explicitly tagged so the UI can disclose them.
    """
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=365 * 6 + 10)
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='=.^-')}",
            params={
                "period1": int(start.timestamp()),
                "period2": int(end.timestamp()),
                "interval": "1mo",
                "events": "div,splits",
            },
            headers={"User-Agent": "Mozilla/5.0 FinanX-market-analysis/1.0"},
            timeout=8,
        )
        response.raise_for_status()
        result = (response.json().get("chart") or {}).get("result") or []
        if not result:
            return None
        item = result[0]
        stamps = item.get("timestamp") or []
        indicators = item.get("indicators") or {}
        quotes_data = indicators.get("quote") or [{}]
        closes = (quotes_data[0] or {}).get("close") or []
        adj = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
        series = []
        for index, stamp in enumerate(stamps):
            raw = adj[index] if index < len(adj) and adj[index] is not None else (
                closes[index] if index < len(closes) else None
            )
            try:
                value = float(raw)
                dt = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
            if value > 0:
                series.append((dt, value))
        if len(series) < 2:
            return None
        metrics = _metrics(series)
        if not metrics.get("available"):
            return None
        metrics["history_source"] = f"Yahoo Finance historical series ({label})"
        metrics["history_mode"] = "external-benchmark-proxy"
        metrics["history_instrument_key"] = symbol
        metrics["history_proxy"] = True
        return metrics
    except Exception:
        logger.debug("External historical fallback unavailable for %s", symbol, exc_info=True)
        return None



_AI_FALLBACK_TTL = 12 * 60 * 60
_AI_FALLBACK_CACHE: dict[str, tuple[float, dict | None]] = {}


def _ai_web_history_fallback(row: dict, category: str) -> dict | None:
    """Last-resort AI-assisted web research, with numeric validation and provenance.

    AI is used to locate cited source data, not to invent returns: this function
    requires dated price observations and source URLs, then calculates returns
    locally. It is disabled unless OPENAI_API_KEY is configured.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    category = str(category or "").strip().lower()
    name = " ".join(str(row.get(k) or "") for k in (
        "name", "short_name", "symbol", "trading_symbol", "underlying_symbol",
        "underlying_name", "scheme_name", "bank",
    )).strip()
    if not name:
        return None
    identity = str(row.get("instrument_key") or row.get("symbol") or name)
    cache_key = f"ai-web-history:{category}:{identity.casefold()}"
    now = datetime.now(timezone.utc).timestamp()
    cached = _AI_FALLBACK_CACHE.get(cache_key)
    if cached and now - cached[0] < _AI_FALLBACK_TTL:
        return dict(cached[1]) if cached[1] else None

    today = datetime.now(timezone.utc).date()
    targets = {
        "latest": today.isoformat(),
        "one_year": (today - timedelta(days=365)).isoformat(),
        "three_year": (today - timedelta(days=365 * 3)).isoformat(),
        "five_year": (today - timedelta(days=365 * 5)).isoformat(),
    }
    schema = {
        "type": "object",
        "properties": {
            "instrument_name": {"type": "string"},
            "currency": {"type": "string"},
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "horizon": {"type": "string", "enum": ["latest", "one_year", "three_year", "five_year"]},
                        "date": {"type": "string"},
                        "price": {"type": "number"},
                        "source_name": {"type": "string"},
                        "source_url": {"type": "string"},
                    },
                    "required": ["horizon", "date", "price", "source_name", "source_url"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["instrument_name", "currency", "observations", "notes"],
        "additionalProperties": False,
    }
    prompt = (
        "Research market history using web search. Do not guess or synthesize any numbers. "
        "Find the exact same instrument (not a similar company, index, ETF, or derivative proxy). "
        "Return observed prices and dates for these target dates (nearest available trading day): "
        + ", ".join(f"{k}={v}" for k, v in targets.items())
        + f". Instrument/category: {name} / {category}. "
        "Prefer exchange, issuer, AMFI, central-bank, or established market-data sources. "
        "Every observation must have a direct source URL that actually supports the price/date. "
        "For an individual bond, do not substitute a bond ETF; for F&O contracts, do not substitute "
        "underlying returns for option/futures contract returns; for MCX commodities do not substitute "
        "USD international futures unless no same-instrument source exists (if only a proxy is found, "
        "return no observation for that horizon). For FD, use the bank's official rate page and rate "
        "effective date, not a fabricated price series. If reliable source data is not found for a "
        "horizon, omit it. Do not make investment recommendations. "
        "Use positive raw prices/NAV/index levels in the instrument's stated currency."
    )
    result = None
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("FINANX_AI_FALLBACK_MODEL", "gpt-4.1-mini"),
                "tools": [{"type": "web_search_preview"}],
                "input": prompt,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "finanx_market_history",
                        "strict": True,
                        "schema": schema,
                    }
                },
            },
            timeout=18,
        )
        response.raise_for_status()
        payload = response.json()
        output_text = payload.get("output_text")
        if not output_text:
            chunks = []
            for output in payload.get("output") or []:
                for part in output.get("content") or []:
                    if part.get("type") == "output_text" and part.get("text"):
                        chunks.append(part["text"])
            output_text = "\n".join(chunks)
        parsed = json.loads(output_text) if output_text else {}
        observations = parsed.get("observations") or []
        by_horizon = {}
        for obs in observations:
            horizon = obs.get("horizon")
            if horizon not in targets:
                continue
            try:
                price = float(obs.get("price"))
                obs_date = date.fromisoformat(str(obs.get("date"))[:10])
                source_url = str(obs.get("source_url") or "")
                source_name = str(obs.get("source_name") or "").strip()
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0 or not source_name or not source_url.startswith("https://"):
                continue
            # Prevent unrelated dates from being passed off as the requested horizon.
            tolerance = {"latest": 45, "one_year": 45, "three_year": 120, "five_year": 180}[horizon]
            target_date = date.fromisoformat(targets[horizon])
            if abs((obs_date - target_date).days) > tolerance:
                continue
            by_horizon[horizon] = {
                "price": price, "date": obs_date.isoformat(),
                "source_name": source_name, "source_url": source_url,
            }

        latest = by_horizon.get("latest")
        if not latest:
            raise ValueError("AI web search did not return a validated latest price")
        metrics = {
            "available": False,
            "sample_size": len(by_horizon),
            "return_1y": None, "return_3y": None, "return_5y": None,
            "history_source": "AI-assisted web search; source-linked price observations (last resort)",
            "history_mode": "ai-web-search-source-linked",
            "history_proxy": False,
            "ai_web_fallback": True,
            "ai_fallback_instrument_name": str(parsed.get("instrument_name") or name),
            "ai_fallback_currency": str(parsed.get("currency") or ""),
            "ai_fallback_observations": by_horizon,
            "ai_fallback_notes": str(parsed.get("notes") or ""),
        }
        for horizon, key, years in (
            ("one_year", "return_1y", 1),
            ("three_year", "return_3y", 3),
            ("five_year", "return_5y", 5),
        ):
            point = by_horizon.get(horizon)
            if not point:
                continue
            if horizon == "one_year":
                value = (latest["price"] / point["price"] - 1.0) * 100.0
            else:
                value = ((latest["price"] / point["price"]) ** (1.0 / years) - 1.0) * 100.0
            if math.isfinite(value) and -99.99 <= value <= 1000:
                metrics[key] = round(value, 2)
        metrics["available"] = any(metrics.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))
        if not metrics["available"]:
            raise ValueError("AI web search returned no complete validated horizon")
        result = metrics
    except Exception:
        logger.info("AI web-search market-history fallback unavailable for %s", name, exc_info=True)
        result = None

    _AI_FALLBACK_CACHE[cache_key] = (now, result)
    return dict(result) if result else None

def _external_fallback_for_row(row: dict, category: str) -> dict | None:
    """Choose only category-compatible, named public benchmark proxies."""
    category = str(category or "").lower()
    name = " ".join(str(row.get(k) or "") for k in ("name", "symbol", "underlying_symbol", "trading_symbol")).upper()
    if category in {"gold"}:
        return _external_market_history("GOLDBEES.NS", "Indian gold ETF proxy in INR")
    if category in {"bonds", "bond"}:
        # Only use a gilt ETF proxy when the row itself is explicitly marked as
        # a debt ETF/G-Sec proxy. Never label ETF price performance as an
        # individual bond's return.
        if row.get("_bond_proxy") or str(row.get("asset_type") or "").lower() == "bond_etf_proxy":
            return _external_market_history("GILTBEES.NS", "Indian gilt ETF proxy (not an individual bond)")
    if category in {"fno", "futures", "derivatives"}:
        underlying = str(row.get("underlying_symbol") or row.get("underlying_name") or row.get("name") or "").upper()
        normalized_underlying = re.sub(r"[^A-Z0-9]", "", underlying)
        if "BANKNIFTY" in normalized_underlying or "NIFTYBANK" in normalized_underlying:
            return _external_market_history("^NSEBANK", "Bank Nifty index proxy for underlying history")
        if "NIFTY" in normalized_underlying and "500" not in normalized_underlying:
            return _external_market_history("^NSEI", "Nifty 50 index proxy for underlying history")
        equity_symbol = str(row.get("underlying_symbol") or "").strip().upper()
        if equity_symbol and re.fullmatch(r"[A-Z0-9&._-]+", equity_symbol):
            return _external_market_history(f"{equity_symbol}.NS", f"Public underlying equity history for {equity_symbol}")
    if category in {"currency"}:
        pair = next((p for p in ("USDINR", "EURINR", "GBPINR", "JPYINR") if p in name.replace("/", "").replace("-", "")), None)
        yahoo_pair = {"USDINR": "USDINR=X", "EURINR": "EURINR=X", "GBPINR": "GBPINR=X", "JPYINR": "JPYINR=X"}
        if pair:
            return _external_market_history(yahoo_pair[pair], f"{pair} currency pair")
    if category in {"commodities", "commodity"}:
        normalized = name.replace(" ", "")
        commodity_symbols = (
            (("GOLD",), "GC=F", "international gold futures in USD"),
            (("SILVER",), "SI=F", "international silver futures in USD"),
            (("CRUDEOIL", "CRUDE"), "CL=F", "WTI crude futures in USD"),
            (("NATURALGAS", "NATGAS"), "NG=F", "US natural-gas futures in USD"),
            (("COPPER",), "HG=F", "COMEX copper futures in USD"),
        )
        for tokens, symbol, label in commodity_symbols:
            if any(token in normalized for token in tokens):
                return _external_market_history(symbol, label)
    return None


def _external_stock_history(row: dict) -> dict | None:
    """Fetch split/dividend-adjusted history to validate anomalous Upstox stock returns.

    Upstox remains primary. The adjusted public series is used only when its
    same-symbol horizon return materially disagrees with Upstox, which can
    indicate a corporate-action adjustment problem in raw OHLC history.
    """
    symbol = str(row.get("trading_symbol") or row.get("symbol") or "").strip().upper()
    if not symbol or not re.fullmatch(r"[A-Z0-9&._-]+", symbol):
        return None
    return _external_market_history(f"{symbol}.NS", f"NSE adjusted history for {symbol}")


def _mfapi_history(row: dict) -> dict | None:
    """Resolve a fund by an explicit AMFI code or an exact scheme-name match."""
    scheme_code = row.get("scheme_code") or row.get("schemeCode")
    name = str(row.get("name") or row.get("scheme_name") or "").strip()

    try:
        if not scheme_code or not str(scheme_code).strip().isdigit():
            if not name:
                return None
            search = requests.get(
                "https://api.mfapi.in/mf/search",
                params={"q": name},
                timeout=8,
            )
            search.raise_for_status()
            matches = search.json()
            import re
            def norm(value):
                text = str(value or "").casefold().replace("&", "and")
                text = re.sub(r"[^a-z0-9]+", " ", text)
                # Upstox and AMFI sometimes format plan/option labels differently.
                text = re.sub(r"\b(direct|regular|growth|idcw|dividend|plan|option)\b", " ", text)
                return " ".join(text.split())
            normalized_name = norm(name)
            exact = [m for m in matches if norm(m.get("schemeName")) == normalized_name]
            if len(exact) == 1:
                scheme_code = exact[0].get("schemeCode")
            else:
                # Only accept a very close token match and require the plan/option
                # labels to agree where Upstox provides them; never guess among
                # similarly named funds.
                target_tokens = set(normalized_name.split())
                scored = []
                for candidate in matches:
                    candidate_name = norm(candidate.get("schemeName"))
                    tokens = set(candidate_name.split())
                    if not target_tokens or not tokens:
                        continue
                    score = len(target_tokens & tokens) / len(target_tokens | tokens)
                    original = str(candidate.get("schemeName") or "").casefold()
                    row_text = " ".join(str(row.get(k) or "") for k in ("name", "scheme_name", "plan", "dividend_type")).casefold()
                    plan_ok = not any(tag in row_text for tag in ("direct", "regular", "growth", "idcw", "dividend")) or any(
                        tag in row_text and tag in original for tag in ("direct", "regular", "growth", "idcw", "dividend")
                    )
                    if score >= 0.82 and plan_ok:
                        scored.append((score, candidate))
                scored.sort(key=lambda pair: pair[0], reverse=True)
                if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
                    return None
                scheme_code = scored[0][1].get("schemeCode")

        if not scheme_code or not str(scheme_code).strip().isdigit():
            return None
        code = str(scheme_code).strip()
        response = requests.get(f"https://api.mfapi.in/mf/{quote(code, safe='')}", timeout=8)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or []
        series = []
        for point in data:
            try:
                dt = datetime.strptime(str(point.get("date")), "%d-%m-%Y").replace(tzinfo=timezone.utc)
                nav = float(point.get("nav"))
            except (TypeError, ValueError):
                continue
            if nav > 0:
                series.append((dt, nav))
        series.sort(key=lambda item: item[0])
        if len(series) < 2:
            return None
        metrics = _metrics(series)
        if not metrics.get("available"):
            return None
        metrics["history_source"] = "MFapi.in historical NAV; exact scheme-name match"
        metrics["history_mode"] = "mutual-fund-nav"
        metrics["history_instrument_key"] = code
        return metrics
    except Exception:
        logger.debug("MF NAV fallback unavailable for %s", name or scheme_code, exc_info=True)
        return None


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

        if str(row.get("instrument_type") or "").upper() == "FUT":
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
                    if row.get("underlying_key") and any(
                        metrics.get(k) is None for k in ("return_1y", "return_3y", "return_5y")
                    ):
                        underlying_series = _series(str(row.get("underlying_key")), unit="months")
                        underlying_metrics = _metrics(underlying_series)
                    for metric_key in ("return_1y", "return_3y", "return_5y"):
                        if metrics.get(metric_key) is None and underlying_metrics.get(metric_key) is not None:
                            metrics[metric_key] = underlying_metrics[metric_key]

                    # Keep unavailable horizons null; do not label a short
                    # contract's annualized return as a true 1Y/3Y/5Y return.

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

                    # Keep Upstox as the primary stock-history source. If the
                    # adjusted same-symbol series disagrees materially on a
                    # comparable horizon, use it to avoid split/demerger
                    # distortions; the alternate source is explicitly labelled.
                    if str(category).lower() in {"stocks", "stock"}:
                        adjusted = _external_stock_history(row)
                        if adjusted:
                            comparable = [
                                k for k in ("return_1y", "return_3y", "return_5y")
                                if item.get(k) is not None and adjusted.get(k) is not None
                            ]
                            differences = {
                                k: round(float(adjusted[k]) - float(item[k]), 2)
                                for k in comparable
                            }
                            item["history_adjusted_crosscheck"] = "compared"
                            item["adjusted_history_differences_pp"] = differences
                            # A 10 percentage-point discrepancy is large enough
                            # to flag a likely corporate-action/raw-close mismatch.
                            if any(abs(delta) >= 10.0 for delta in differences.values()):
                                for k in ("return_1y", "return_3y", "return_5y", "volatility_annualized", "max_drawdown"):
                                    if adjusted.get(k) is not None:
                                        item[k] = adjusted[k]
                                item["history_source"] = adjusted.get("history_source")
                                item["history_mode"] = "adjusted-close-corporate-action-cross-check"
                                item["history_instrument_key"] = adjusted.get("history_instrument_key")
                                item["history_adjustment_note"] = "Used adjusted same-symbol history because Upstox raw-history returns differed by at least 10 percentage points."
                        else:
                            item["history_adjusted_crosscheck"] = "unavailable"
                    return item
            except Exception:
                continue

        # Mutual-fund fallback is isolated: only invoked when Upstox historical
        # candles cannot supply usable NAV returns for this scheme.
        if str(category).lower() in {"mutual-funds", "mutual_funds", "funds"}:
            metrics = _mfapi_history(row)
            if metrics:
                item.update(metrics)
                return item

        # If stock history is missing entirely, use same-symbol public adjusted
        # history as a labelled fallback. Upstox remains the preferred source.
        if str(category).lower() in {"stocks", "stock"}:
            metrics = _external_stock_history(row)
            if metrics:
                item.update(metrics)
                item["history_proxy"] = True
                item["history_mode"] = "public-adjusted-stock-history-fallback"
                item["history_adjustment_note"] = "Public adjusted history used because Upstox history was unavailable."
                return item

        # Last-resort category-compatible public series. Any ETF, index, or
        # international commodity proxy is explicitly labelled in its source.
        metrics = _external_fallback_for_row(row, category)
        if metrics:
            item.update(metrics)

        # AI can fill only horizons still missing after the normal public
        # fallback. It never overwrites a real Upstox/public-source horizon.
        missing_horizons = [k for k in ("return_1y", "return_3y", "return_5y") if item.get(k) is None]
        if missing_horizons:
            ai_metrics = _ai_web_history_fallback(row, category)
            if ai_metrics:
                for key in missing_horizons:
                    if ai_metrics.get(key) is not None:
                        item[key] = ai_metrics[key]
                item["ai_web_fallback"] = True
                item["ai_fallback_observations"] = ai_metrics.get("ai_fallback_observations", {})
                item["ai_fallback_currency"] = ai_metrics.get("ai_fallback_currency")
                item["ai_fallback_notes"] = ai_metrics.get("ai_fallback_notes")
                item["history_source"] = (
                    str(item.get("history_source") or "Primary history unavailable")
                    + "; missing horizons filled by AI-assisted web search with source-linked prices"
                )
                item["history_horizon_sources"] = dict(item.get("history_horizon_sources") or {})
                for key in missing_horizons:
                    if ai_metrics.get(key) is not None:
                        item["history_horizon_sources"][key] = "AI-assisted web search; source-linked price observations"
                item["available"] = any(item.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))
                if not item.get("history_instrument_key"):
                    item["history_instrument_key"] = row.get("instrument_key") or row.get("symbol")
                return item
        if metrics:
            return item
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


def _history_for_segment_entities(rows: list[dict], limit: int | None = None, category: str | None = None) -> dict:
    def backfill_missing_horizons(item: dict, row: dict) -> dict:
        """Fill only unavailable horizons from a labelled public benchmark."""
        category_name = str(category or "").lower()
        if category_name not in {"gold", "commodities", "currency"}:
            return item
        proxy = _external_fallback_for_row(row, category_name)
        if not proxy:
            return item
        horizon_sources = dict(item.get("history_horizon_sources") or {})
        horizon_instruments = dict(item.get("history_horizon_instruments") or {})
        used = False
        primary_source = str(item.get("history_source") or "Upstox historical candles")
        primary_instrument = str(item.get("history_instrument_key") or item.get("instrument_key") or "")
        for key in ("return_1y", "return_3y", "return_5y"):
            if item.get(key) is not None:
                horizon_sources.setdefault(key, primary_source)
                horizon_instruments.setdefault(key, primary_instrument)
            elif proxy.get(key) is not None:
                item[key] = proxy[key]
                horizon_sources[key] = proxy.get("history_source", "Public historical benchmark")
                horizon_instruments[key] = proxy.get("history_instrument_key")
                used = True
        if used:
            item["history_proxy"] = True
            item["history_horizon_sources"] = horizon_sources
            item["history_horizon_instruments"] = horizon_instruments
            item["history_source"] = (
                f"{primary_source}; missing horizons filled from "
                f"{proxy.get('history_source', 'public benchmark')} (labelled proxy)"
            )
            item["available"] = any(item.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))
        return item
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

        if str(representative.get("instrument_type") or "").upper() == "FUT":
            try:
                with _HISTORY_GATE:
                    series, mode = _continuous_derivative_series(representative, unit="months")
                    metrics = _metrics(series)

                    # Use the underlying only for horizons the contract-roll
                    # series genuinely cannot cover. This is especially
                    # important for NCD/currency and MCX futures, where the
                    # current contract is much shorter than 3Y/5Y history.
                    underlying_metrics = {}
                    if representative.get("underlying_key") and any(
                        metrics.get(k) is None for k in ("return_1y", "return_3y", "return_5y")
                    ):
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

                    # Missing horizons stay null unless backed by real
                    # underlying/global-indicator history above.

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
                    item = backfill_missing_horizons(item, representative)
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
                    item = backfill_missing_horizons(item, representative)
                    return entity_key, item
            except Exception:
                continue

        # Last-resort public history is category-aware and explicitly labelled.
        # Never apply this to F&O option contracts; this path is for the gold,
        # commodity and currency analysis segments only.
        proxy_metrics = _external_fallback_for_row(representative, str(category or ""))
        if proxy_metrics:
            item = dict(representative)
            item["instrument_key"] = entity_key
            item["entity_key"] = entity_key
            item.update(proxy_metrics)
            item["history_proxy"] = True
            item["history_proxy_only"] = True
            item["history_horizon_sources"] = {
                key: proxy_metrics.get("history_source")
                for key in ("return_1y", "return_3y", "return_5y")
                if proxy_metrics.get(key) is not None
            }
            item["history_horizon_instruments"] = {
                key: proxy_metrics.get("history_instrument_key")
                for key in ("return_1y", "return_3y", "return_5y")
                if proxy_metrics.get(key) is not None
            }
            missing_horizons = [k for k in ("return_1y", "return_3y", "return_5y") if item.get(k) is None]
            if missing_horizons:
                ai_metrics = _ai_web_history_fallback(representative, str(category or ""))
                if ai_metrics:
                    for key in missing_horizons:
                        if ai_metrics.get(key) is not None:
                            item[key] = ai_metrics[key]
                            item["history_horizon_sources"][key] = "AI-assisted web search; source-linked price observations"
                    item["ai_web_fallback"] = True
                    item["ai_fallback_observations"] = ai_metrics.get("ai_fallback_observations", {})
                    item["ai_fallback_currency"] = ai_metrics.get("ai_fallback_currency")
                    item["ai_fallback_notes"] = ai_metrics.get("ai_fallback_notes")
                    item["history_source"] = str(item.get("history_source") or "Public fallback") + "; missing horizons filled by AI-assisted web search"
                    item["available"] = any(item.get(k) is not None for k in ("return_1y", "return_3y", "return_5y"))
            return entity_key, item

        # Ultimate fallback after broker history and compatible public sources.
        ai_metrics = _ai_web_history_fallback(representative, str(category or ""))
        if ai_metrics:
            item = dict(representative)
            item["instrument_key"] = entity_key
            item["entity_key"] = entity_key
            item.update(ai_metrics)
            item["history_instrument_key"] = entity_key
            return entity_key, item

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
    # Coverage counts independent entities; a proxy-only series is counted once.
    valid = []
    seen_proxy_series = set()
    for row in rows:
        if not any(row.get(k) is not None for k in ("return_1y", "return_3y", "return_5y")):
            continue
        if row.get("history_proxy_only"):
            series_id = row.get("history_instrument_key") or row.get("history_source")
            if series_id in seen_proxy_series:
                continue
            seen_proxy_series.add(series_id)
        valid.append(row)
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
    for key in ("return_1y", "return_3y", "return_5y"):
        values = []
        seen_horizon_proxies = set()
        for row in valid:
            value = row.get(key)
            if value is None:
                continue
            source = str((row.get("history_horizon_sources") or {}).get(key) or row.get("history_source") or "")
            instrument = (row.get("history_horizon_instruments") or {}).get(key)
            if "Yahoo Finance historical series" in source:
                proxy_id = instrument or source
                if proxy_id in seen_horizon_proxies:
                    continue
                seen_horizon_proxies.add(proxy_id)
            values.append(float(value))
        if values:
            result[key] = round(sum(values) / len(values), 2)
    volatility_values = [float(x["volatility_annualized"]) for x in valid if x.get("volatility_annualized") is not None]
    if volatility_values:
        result["volatility_annualized"] = round(sum(volatility_values) / len(volatility_values), 2)
    if result["volatility_annualized"] is None:
        result["volatility_annualized"] = default_vol
    result["average_basis"] = (
        f"Average of {len(valid)}/{total} tracked entities" if total
        else "No tracked history available"
    )
    return result


def _crosscheck_stock_returns(rows: list[dict]) -> list[dict]:
    """Cross-check unusually negative Upstox stock returns against adjusted close.

    Only checks the already-tracked stock universe. It never adds symbols to
    the average, and keeps Upstox history unless adjusted same-symbol data
    materially disagrees.
    """
    candidates = [
        row for row in rows
        if any(
            row.get(key) is not None and float(row[key]) <= threshold
            for key, threshold in (
                ("return_1y", -25.0),
                ("return_3y", -15.0),
                ("return_5y", -15.0),
            )
        )
    ]

    def check(row):
        adjusted = _external_stock_history(row)
        item = dict(row)
        if not adjusted:
            item["history_adjusted_crosscheck"] = "unavailable"
            return item
        comparable = [
            key for key in ("return_1y", "return_3y", "return_5y")
            if item.get(key) is not None and adjusted.get(key) is not None
        ]
        differences = {
            key: round(float(adjusted[key]) - float(item[key]), 2)
            for key in comparable
        }
        item["history_adjusted_crosscheck"] = "compared"
        item["adjusted_history_differences_pp"] = differences
        if any(abs(delta) >= 10.0 for delta in differences.values()):
            for key in ("return_1y", "return_3y", "return_5y", "volatility_annualized", "max_drawdown"):
                if adjusted.get(key) is not None:
                    item[key] = adjusted[key]
            item["history_source"] = adjusted.get("history_source")
            item["history_mode"] = "adjusted-close-corporate-action-cross-check"
            item["history_instrument_key"] = adjusted.get("history_instrument_key")
            item["history_adjustment_note"] = "Adjusted same-symbol history used because Upstox raw-history returns differed by at least 10 percentage points."
        return item

    if not candidates:
        return rows
    by_symbol = {}
    with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as pool:
        futures = {pool.submit(check, row): str(row.get("symbol") or row.get("trading_symbol") or row.get("name")) for row in candidates}
        for future in as_completed(futures):
            try:
                by_symbol[futures[future]] = future.result()
            except Exception:
                pass
    return [
        by_symbol.get(str(row.get("symbol") or row.get("trading_symbol") or row.get("name")), row)
        for row in rows
    ]


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

    def load_history(rows, category, limit, segment_key):
        if category == "entities":
            return _history_for_segment_entities(rows, limit, segment_key)
        return _history_for_rows(rows, category, limit)

    history_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(load_history, rows, category, limit, key): key
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
            # Keep the live quote fields from the current snapshot and overlay
            # only fields actually present in the historical result. Replacing
            # the entire row used to erase today_change/volume and made breadth
            # appear as zero despite valid live stock quotes.
            item = dict(row)
            history_item = by_key.get(key)
            if history_item:
                item.update(history_item)
            output.append(item)
        return output

    stocks = merge(stocks, history_results.get("stocks", []))
    stocks = _crosscheck_stock_returns(stocks)
    fno_underlyings = history_results.get("fno_underlyings", {})
    enriched_fno = []
    for row in fno:
        item = dict(row)
        hist = fno_underlyings.get(str(row.get("underlying_key")))
        if hist:
            is_option = str(row.get("instrument_type") or row.get("type") or "").upper() in {"CE", "PE"}
            for key in ("return_1y", "return_3y", "return_5y", "volatility_annualized", "max_drawdown"):
                if hist.get(key) is not None:
                    if is_option and key.startswith("return_"):
                        item["underlying_" + key] = hist.get(key)
                    else:
                        item[key] = hist.get(key)
            if is_option:
                # An underlying index/future's performance is not the option's
                # premium return. Do not expose it under the option's return fields.
                for key in ("return_1y", "return_3y", "return_5y"):
                    item.pop(key, None)
                item["history_source"] = "Underlying benchmark history (Upstox); not option-premium return"
                item["return_basis"] = "underlying benchmark; option premium returns unavailable"
            else:
                item["history_source"] = "Underlying/futures market history (Upstox)"
                item["return_basis"] = "underlying/futures benchmark"
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

    tracked_stock_metrics = _category_metrics(
        stocks,
        20.0,
        tracked_count=len(hu.get("stocks", [])),
    )
    # Market Analysis presents the broad market benchmark as the main stock
    # return, while preserving the tracked-stock average as a separate metric.
    # If the index provider fails, use the tracked-stock average but label it
    # clearly instead of displaying a blank or misrepresenting it as Nifty 500.
    benchmark_metrics = _nifty500_index_metrics()
    if benchmark_metrics.get("return_1y") is not None:
        stock_metrics = dict(tracked_stock_metrics)
        horizon_sources = {}
        for horizon in ("return_1y", "return_3y", "return_5y"):
            index_value = benchmark_metrics.get(horizon)
            if index_value is not None:
                stock_metrics[horizon] = index_value
                horizon_sources[horizon] = (
                    f"{benchmark_metrics.get('index_name', 'Nifty 500')} index "
                    f"({benchmark_metrics.get('benchmark_source', 'historical index data')})"
                )
            else:
                # Keep each displayed horizon numeric where the tracked-stock
                # history has a valid value, while exposing its different basis.
                stock_metrics[horizon] = tracked_stock_metrics.get(horizon)
                horizon_sources[horizon] = "Average of tracked stocks (index horizon unavailable)"
        stock_metrics.update({
            "average_basis": f"{benchmark_metrics.get('index_name', 'Nifty 500')} index performance; per-horizon fallback labelled",
            "benchmark_name": benchmark_metrics.get("index_name", "Nifty 500"),
            "benchmark_source": benchmark_metrics.get("benchmark_source"),
            "benchmark_symbol": benchmark_metrics.get("benchmark_symbol"),
            "benchmark_fallback": benchmark_metrics.get("benchmark_fallback", False),
            "return_horizon_sources": horizon_sources,
            "tracked_stock_metrics": tracked_stock_metrics,
        })
    else:
        stock_metrics = dict(tracked_stock_metrics)
        stock_metrics.update({
            "average_basis": "Fallback: average of tracked stocks (index history unavailable)",
            "benchmark_name": "Tracked stocks average",
            "benchmark_source": "Upstox historical stock candles",
            "benchmark_fallback": True,
            "tracked_stock_metrics": tracked_stock_metrics,
        })
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
    fund_data_status = (
        "upstox+historical-nav-fallback"
        if any("MFapi.in" in str(row.get("history_source") or "") for row in history_results.get("mutual-funds", []))
        else "upstox"
    )

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
    bond_data_status = (
        "upstox+listed-debt-etf-proxy"
        if any(str(row.get("asset_type") or "").lower() == "bond_etf_proxy" or row.get("_bond_proxy") for row in bonds)
        else "upstox"
    )
    gold_metrics = _category_metrics(
        list(gold_history.values()),
        16.0,
        tracked_count=len(hu.get("gold", [])),
    )

    # Market-analysis return values are never substituted with a
    # representative symbol or model proxy. They remain the exact averages
    # calculated from the tracked entities above.
    stock_data_status = "upstox"
    gold_data_status = (
        "upstox+public-history-proxy"
        if any(row.get("history_proxy") for row in gold_history.values()) else "upstox"
    )
    commodity_data_status = (
        "upstox+public-history-proxy"
        if any(row.get("history_proxy") for row in commodity_history.values()) else "upstox"
    )
    currency_data_status = (
        "upstox+public-history-proxy"
        if any(row.get("history_proxy") for row in currency_history.values()) else "upstox"
    )

    def average_fd_rate(field):
        values = []
        for row in fds:
            try:
                value = float(row[field])
                if 0 < value < 20:
                    values.append(value)
            except (KeyError, TypeError, ValueError):
                continue
        return round(sum(values) / len(values), 2) if values else None

    fd_1y = average_fd_rate("rate_1y") if fds else None
    fd_3y = average_fd_rate("rate_3y") if fds else None
    fd_5y = average_fd_rate("rate_5y") if fds else None
    fd_senior_1y = average_fd_rate("senior_rate_1y") if fds else None
    fd_senior_3y = average_fd_rate("senior_rate_3y") if fds else None
    fd_senior_5y = average_fd_rate("senior_rate_5y") if fds else None
    fd_metrics = {
        "available": any(value is not None for value in (fd_1y, fd_3y, fd_5y)),
        "sample_size": len(fds),
        "tracked_count": len(fds),
        "history_count": len(fds),
        "history_coverage": 100.0 if fds else 0.0,
        "return_1y": fd_1y,
        "return_3y": fd_3y,
        "return_5y": fd_5y,
        "rate_1y_average": fd_1y,
        "rate_3y_average": fd_3y,
        "rate_5y_average": fd_5y,
        "senior_rate_1y_average": fd_senior_1y,
        "senior_rate_3y_average": fd_senior_3y,
        "senior_rate_5y_average": fd_senior_5y,
        "volatility_annualized": 1.0,
        "rate_average": fd_1y,
        "average_basis": f"Manual one-year/three-year/five-year FD rates across {len(fds)} banks",
        "source": "Manually maintained bank-published retail FD rate table",
    }

    result = {
        "fd": {
            "status": "manual-bank-rate-registry",
            "source": "Manually maintained bank-published retail FD rates; update market_universe.py when banks revise rates",
            "metrics": fd_metrics,
            "analyzed_options": fds,
            "updated_at": now,
        },
        "bonds": {
            "status": bond_data_status,
            "source": "Upstox listed debt quotes; listed Indian G-Sec/debt ETF prices used as clearly labelled proxies when individual bonds are absent",
            "metrics": bond_metrics,
            "analyzed_options": bonds,
            "updated_at": now,
        },
        "mutual-funds": {
            "status": fund_data_status,
            "source": "Upstox scheme metadata/history; MFapi.in NAV history fallback when needed",
            "metrics": fund_metrics,
            "analyzed_options": funds,
            "updated_at": now,
        },
        "gold": {
            "status": gold_data_status,
            "source": "Upstox MCX live quotes; labelled Indian gold ETF history proxy when Upstox history is missing",
            "metrics": gold_metrics,
            "analyzed_options": gold,
            "updated_at": now,
        },
        "commodities": {
            "status": commodity_data_status,
            "source": "Upstox MCX live quotes; labelled public benchmark history proxies where needed",
            "metrics": commodity_metrics,
            "analyzed_options": commodities,
            "updated_at": now,
        },
        "currency": {
            "status": currency_data_status,
            "source": "Upstox currency futures; labelled public FX history proxies for missing horizons",
            "metrics": currency_metrics,
            "analyzed_options": currency,
            "updated_at": now,
        },
        "fno": {
            "status": fno_data_status,
            "source": "Upstox F&O quotes; historical returns are underlying/futures benchmarks, not option-premium returns",
            "metrics": {
                **fno_metrics,
                "return_basis": "underlying/futures benchmark history; not option-premium performance",
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
        # Three or more real, quoted Indian G-Sec/debt ETF proxies are enough
        # to make the bond segment usable when the broker master omits direct bonds.
        and result["_tracking"]["configured_bonds"] >= 3
        and len(fds) >= 8
    )
    result["_tracking"]["message"] = (
        "Upstox is primary for live quotes and history; labelled public historical/FX "
        "fallbacks are used only where Upstox data is unavailable."
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
