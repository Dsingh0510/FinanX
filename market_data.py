from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import pstdev

import yfinance as yf

from allocation_engine import ASSET_INFO
from database import recent_closes, daily_closes, save_daily_history, save_metrics, save_tick, market_rows, mutual_fund_metrics, set_provider_status, connect, utc_now
from amfi_data import update_amfi_metrics, update_bond_proxy_metrics_fast, bond_proxy_metrics, category_metrics
from india_live import nse_indices, nifty_fno, current_currencies, mcx_watch, latest_mutual_funds
from bond_data import get_indian_bond_values

# Public-data watchlist.  Yahoo Finance labels NSE quotes as delayed; this is therefore
# an auto-refreshing public market-data layer, not an exchange-licensed real-time feed.
WATCHLIST = [
    {"symbol": "^NSEI", "name": "NIFTY 50", "category": "STOCKS", "currency": "INR"},
    {"symbol": "^NSEBANK", "name": "NIFTY Bank", "category": "STOCKS", "currency": "INR"},
    {"symbol": "RELIANCE.NS", "name": "Reliance Industries", "category": "STOCKS", "currency": "INR"},
    {"symbol": "HDFCBANK.NS", "name": "HDFC Bank", "category": "STOCKS", "currency": "INR"},
    {"symbol": "ICICIBANK.NS", "name": "ICICI Bank", "category": "STOCKS", "currency": "INR"},
    {"symbol": "TCS.NS", "name": "TCS", "category": "STOCKS", "currency": "INR"},
    {"symbol": "INFY.NS", "name": "Infosys", "category": "STOCKS", "currency": "INR"},
    {"symbol": "SBIN.NS", "name": "State Bank of India", "category": "STOCKS", "currency": "INR"},
    {"symbol": "ITC.NS", "name": "ITC", "category": "STOCKS", "currency": "INR"},
    {"symbol": "LT.NS", "name": "Larsen & Toubro", "category": "STOCKS", "currency": "INR"},
    {"symbol": "BHARTIARTL.NS", "name": "Bharti Airtel", "category": "STOCKS", "currency": "INR"},
    {"symbol": "HINDUNILVR.NS", "name": "Hindustan Unilever", "category": "STOCKS", "currency": "INR"},
    {"symbol": "AXISBANK.NS", "name": "Axis Bank", "category": "STOCKS", "currency": "INR"},
    {"symbol": "KOTAKBANK.NS", "name": "Kotak Mahindra Bank", "category": "STOCKS", "currency": "INR"},
    {"symbol": "GC=F", "name": "Gold Futures", "category": "GOLD", "currency": "USD"},
    {"symbol": "GOLDBEES.NS", "name": "Gold BeES", "category": "GOLD", "currency": "INR"},
    {"symbol": "GLD", "name": "Gold ETF Reference", "category": "GOLD", "currency": "USD"},
    {"symbol": "CL=F", "name": "Crude Oil Futures", "category": "COMMODITY", "currency": "USD"},
    {"symbol": "SI=F", "name": "Silver Futures", "category": "COMMODITY", "currency": "USD"},
    {"symbol": "HG=F", "name": "Copper Futures", "category": "COMMODITY", "currency": "USD"},
    {"symbol": "USDINR=X", "name": "USD/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "EURINR=X", "name": "EUR/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "GBPINR=X", "name": "GBP/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "^TNX", "name": "US 10Y Treasury Yield Proxy", "category": "BONDS", "currency": "USD"},
    {"symbol": "TLT", "name": "US 20+ Year Treasury ETF Proxy", "category": "BONDS", "currency": "USD"},
    {"symbol": "IEF", "name": "US 7-10 Year Treasury ETF Proxy", "category": "BONDS", "currency": "USD"},
    {"symbol": "NQ=F", "name": "Nasdaq-100 Futures (F&O reference)", "category": "FNO", "currency": "USD"},
    {"symbol": "ES=F", "name": "S&P 500 Futures (F&O reference)", "category": "FNO", "currency": "USD"},
    {"symbol": "YM=F", "name": "Dow Futures (F&O reference)", "category": "FNO", "currency": "USD"},
    # 10 INR currency crosses
    {"symbol": "USDINR=X", "name": "USD/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "EURINR=X", "name": "EUR/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "GBPINR=X", "name": "GBP/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "JPYINR=X", "name": "JPY/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "AUDINR=X", "name": "AUD/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "CADINR=X", "name": "CAD/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "CHFINR=X", "name": "CHF/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "CNYINR=X", "name": "CNY/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "SGDINR=X", "name": "SGD/INR", "category": "CURRENCY", "currency": "INR"},
    {"symbol": "NZDINR=X", "name": "NZD/INR", "category": "CURRENCY", "currency": "INR"},
]

_POLL_SECONDS = 180
_started = False
_lock = threading.Lock()
_amfi_started = False
_history_seed_started = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value):
    try:
        if value is None:
            return None
        x = float(value)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def _metrics_for(symbol: str, category: str):
    # Use stored DAILY history for multi-month/year metrics. Minute quote storage is
    # intentionally separate so the collector can remain lightweight.
    closes = daily_closes(symbol, 1600)
    if len(closes) < 5:
        return {
            "symbol": symbol, "category": category, "updated_at": _now(),
            "status": "insufficient_history", "data_points": len(closes)
        }

    def ret(n):
        if len(closes) <= n:
            return None
        base = closes[-(n + 1)]
        return ((closes[-1] / base) - 1) * 100 if base else None

    daily = []
    for prev, curr in zip(closes[:-1], closes[1:]):
        if prev > 0 and curr > 0:
            daily.append(math.log(curr / prev))
    vol = pstdev(daily) * math.sqrt(252) * 100 if len(daily) >= 5 else None

    peak = closes[0]
    max_dd = 0.0
    for px in closes:
        peak = max(peak, px)
        if peak:
            max_dd = min(max_dd, px / peak - 1)

    r90 = ret(90)
    r1y = ret(252)
    def cagr(days, years):
        if len(closes) <= days or closes[-(days+1)] in (None, 0):
            return None
        return ((closes[-1] / closes[-(days+1)]) ** (1 / years) - 1) * 100
    r3y = cagr(756, 3)
    r5y = cagr(1260, 5)
    trend = None
    if r1y is not None:
        trend = max(0, min(100, 50 + r1y * 1.2))

    return {
        "symbol": symbol,
        "category": category,
        "updated_at": _now(),
        "return_30d": round(ret(30), 2) if ret(30) is not None else None,
        "return_90d": round(r90, 2) if r90 is not None else None,
        "return_1y": round(r1y, 2) if r1y is not None else None,
        "return_3y": round(r3y, 2) if r3y is not None else None,
        "return_5y": round(r5y, 2) if r5y is not None else None,
        "volatility_annualized": round(vol, 2) if vol is not None else None,
        "max_drawdown": round(max_dd * 100, 2),
        "trend_score": round(trend, 1) if trend is not None else None,
        "data_points": len(closes),
        "status": "available",
    }


def _history_is_ready(symbol: str, minimum_points: int = 1260) -> bool:
    try:
        closes = daily_closes(symbol, minimum_points)
        return len(closes) >= minimum_points
    except Exception:
        return False


def _collect_one(item: dict) -> tuple[bool, str | None]:
    symbol = item["symbol"]
    try:
        # Daily quotes are considerably faster and more reliable than 1-minute history.
        t = yf.Ticker(symbol)
        hist = t.history(period="5d", interval="1d", auto_adjust=False, prepost=False)
        if hist is None or hist.empty:
            raise RuntimeError("No data returned")
        last = hist.iloc[-1]
        price = _safe_float(last.get("Close"))
        previous_close = None
        if len(hist) >= 2:
            previous_close = _safe_float(hist.iloc[-2].get("Close"))
        change_pct = None if price is None or previous_close in (None, 0) else (price / previous_close - 1) * 100
        row = {
            "symbol": symbol, "category": item["category"], "name": item["name"], "currency": item["currency"],
            "timestamp": _now(), "price": price, "previous_close": previous_close,
            "day_change_pct": round(change_pct, 3) if change_pct is not None else None,
            "volume": _safe_float(last.get("Volume")), "source": "Yahoo Finance / yfinance",
            "freshness": "public-auto-refresh",
        }
        save_tick(row)
        # Do not download multi-year history for every watchlist item during
        # the first collector pass. That was the main startup-speed bottleneck.
        # A small curated universe is seeded separately in the background.
        if symbol in _HISTORY_SEED_SET and not _history_is_ready(symbol):
            daily = t.history(period="7y", interval="1d", auto_adjust=False, prepost=False)
            if daily is not None and not daily.empty:
                daily_rows=[]
                for idx, rr in daily.iterrows():
                    try: trade_date=idx.date().isoformat()
                    except Exception: trade_date=str(idx)[:10]
                    daily_rows.append((trade_date, _safe_float(rr.get("Close")), _safe_float(rr.get("Volume"))))
                if daily_rows:
                    save_daily_history(symbol, daily_rows)
        # Always save metrics for seeded symbols; for other symbols current price
        # tracking remains fast and historical metrics can be added later.
        if symbol in _HISTORY_SEED_SET:
            save_metrics(_metrics_for(symbol, item["category"]))
        return True, None
    except Exception as exc:
        return False, f"{symbol}: {exc}"


def collect_once() -> dict:
    success = 0
    errors = []
    # Keep the worker small enough not to hammer public endpoints, while avoiding
    # the old serial 30+ request bottleneck.
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_collect_one, item) for item in WATCHLIST]
        for fut in as_completed(futures):
            ok, err = fut.result()
            if ok: success += 1
            elif err: errors.append(err)
    set_provider_status(
        "yahoo_public", "ok" if success else "error", success=success > 0,
        error="; ".join(errors[:8]) if errors else None, records=success,
    )
    return {"success": success, "errors": errors, "updated_at": utc_now()}


_HISTORY_SEED_SYMBOLS = [
    ("^NSEI", "STOCKS"),
    ("GC=F", "GOLD"),
    ("GOLDBEES.NS", "GOLD"),
    ("CL=F", "COMMODITY"),
    ("SI=F", "COMMODITY"),
    ("HG=F", "COMMODITY"),
    ("NG=F", "COMMODITY"),
    ("USDINR=X", "CURRENCY"),
    ("NQ=F", "FNO"),
]
_HISTORY_SEED_SET = {symbol for symbol, _ in _HISTORY_SEED_SYMBOLS}


def _seed_history_one(symbol: str, category: str) -> bool:
    try:
        if _history_is_ready(symbol):
            return True
        t=yf.Ticker(symbol)
        daily=t.history(period="7y", interval="1d", auto_adjust=False, prepost=False)
        if daily is None or daily.empty:
            return False
        rows=[]
        for idx, rr in daily.iterrows():
            try: trade_date=idx.date().isoformat()
            except Exception: trade_date=str(idx)[:10]
            rows.append((trade_date,_safe_float(rr.get("Close")),_safe_float(rr.get("Volume"))))
        if rows:
            save_daily_history(symbol,rows)
            save_metrics(_metrics_for(symbol,category))
            return True
    except Exception:
        pass
    return False


def start_fast_history_seed():
    global _history_seed_started
    with _lock:
        if _history_seed_started:
            return
        _history_seed_started=True
    def worker():
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures=[ex.submit(_seed_history_one,s,c) for s,c in _HISTORY_SEED_SYMBOLS]
            for fut in as_completed(futures):
                try: fut.result()
                except Exception: pass
    threading.Thread(target=worker,name="finanx-fast-history-seed",daemon=True).start()

def start_amfi_refresh():
    global _amfi_started
    with _lock:
        if _amfi_started:
            return
        _amfi_started = True
    def worker():
        try:
            from amfi_data import update_amfi_metrics_fast, update_bond_proxy_metrics_fast
            update_amfi_metrics_fast()
            update_bond_proxy_metrics_fast()
        except Exception:
            pass
        while True:
            try:
                update_amfi_metrics()
            except Exception as exc:
                set_provider_status("amfi", "error", error=str(exc), records=0)
            time.sleep(6 * 60 * 60)
    thread = threading.Thread(target=worker, name="finanx-amfi-data", daemon=True)
    thread.start()


def start_background_collector():
    global _started
    start_fast_history_seed()
    with _lock:
        if _started:
            return
        _started = True

    def worker():
        while True:
            try:
                collect_once()
            except Exception as exc:
                set_provider_status("yahoo_public", "error", error=str(exc), records=0)
            time.sleep(_POLL_SECONDS)

    thread = threading.Thread(target=worker, name="finanx-public-market-data", daemon=True)
    thread.start()


def _load_fd_rows():
    conn = connect()
    rows = conn.execute("SELECT bank,tenure,annual_rate,source_url,updated_at FROM fd_rates ORDER BY annual_rate DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _category_rows(category: str):
    return [r for r in market_rows() if r["category"] == category]


def _aggregate_metrics(rows):
    numeric_keys = ["return_30d", "return_90d", "return_1y", "return_3y", "return_5y", "volatility_annualized", "max_drawdown", "trend_score"]
    out = {k: None for k in numeric_keys}
    valid_rows = [r for r in rows if r.get("metric_status") == "available"]
    for key in numeric_keys:
        vals = [float(r[key]) for r in valid_rows if r.get(key) is not None]
        if vals:
            # Category performance is the arithmetic average across tracked instruments.
            out[key] = round(sum(vals) / len(vals), 2)
    out["available"] = any(out[k] is not None for k in numeric_keys)
    out["sample_size"] = len(valid_rows)
    out["data_points"] = max([int(r.get("data_points") or 0) for r in valid_rows], default=0)
    return out


def get_category_market_analysis():
    result = {}
    categories = ["fd", "bonds", "mutual-funds", "gold", "stocks", "commodities", "currency", "fno"]
    for category in categories:
        if category == "bonds":
            bp = bond_proxy_metrics()
            try:
                mf_rows = mutual_fund_metrics()
                bond_rows = [r for r in mf_rows if str(r.get('source','')).startswith('Bond proxy • AMFI NAV/history')]
            except Exception:
                bond_rows = []
            yield_rows = get_indian_bond_values()
            current_yield = next((x.get('value') for x in yield_rows if x.get('term') == '10Y G-Sec'), None)
            yield_label = next((x.get('label') for x in yield_rows if x.get('term') == '10Y G-Sec'), 'India 10Y Government Bond')
            options = bp.get('options', []) or [{
                'name': r.get('scheme_name'), 'symbol': r.get('scheme_code'),
                'yoy': r.get('return_1y'), 'three_year_return': r.get('return_3y'),
                'five_year_return': r.get('return_5y'), 'latest_nav': r.get('latest_nav'),
                'latest_date': r.get('latest_date')
            } for r in bond_rows[:3]]
            result[category] = {
                'status': 'proxy' if bp.get('available') else ('yield-only' if current_yield is not None else 'pending'),
                'source': 'Bond yield + representative corporate-bond fund proxy',
                'metrics': {
                    'available': bp.get('available', False), 'sample_size': bp.get('sample_size', 0),
                    'return_1y': bp.get('return_1y'), 'return_3y': bp.get('return_3y'), 'return_5y': bp.get('return_5y'),
                    'volatility_annualized': None, 'max_drawdown': None,
                    'current_yield': current_yield, 'yield_label': yield_label,
                },
                'analyzed_options': options + [
                    {'name': x.get('label'), 'symbol': 'YIELD:'+str(x.get('term','')), 'kind':'bond_yield', 'value':x.get('value'), 'term':x.get('term')}
                    for x in yield_rows if x.get('value') is not None
                ],
                'updated_at': _now(),
            }
            continue
        if category == "mutual-funds":
            mf = category_metrics()
            try:
                mf_rows = mutual_fund_metrics()
            except Exception:
                mf_rows = []
            mf_options = [{
                "name": r.get("scheme_name"),
                "symbol": r.get("scheme_code"),
                "yoy": r.get("return_1y"),
                "three_year_return": r.get("return_3y"),
                "five_year_return": r.get("return_5y"),
            } for r in mf_rows[:30] if r.get("scheme_name")]
            result[category] = {
                "status": "daily" if mf.get("available") else "pending",
                "source": mf.get("source", "AMFI official NAV/history"),
                "metrics": {
                    "available": mf.get("available", False),
                    "sample_size": mf.get("sample_size", 0),
                    "return_1y": mf.get("return_1y"),
                    "return_3y": mf.get("return_3y"),
                    "return_5y": mf.get("return_5y"),
                    "volatility_annualized": None,
                    "max_drawdown": None,
                },
                "analyzed_options": mf_options or [{"name":name,"symbol":"MUTUAL-FUND"} for name in mf.get("schemes",[])],
                "updated_at": _now(),
            }
            continue
        rows = _category_rows(category.upper())
        if rows:
            metrics = _aggregate_metrics(rows)
            result[category] = {
                "status": "auto" if metrics["available"] else "pending",
                "source": rows[0].get("source"),
                "metrics": metrics,
                "sample_size": metrics.get("sample_size", 0),
                "updated_at": max([r.get("timestamp") for r in rows if r.get("timestamp")], default=None),
                "analyzed_options": [
                    {"name": r.get("name"), "symbol": r.get("symbol")} for r in rows if r.get("name")
                ],
            }
        elif category == "fd":
            fd = _load_fd_rows()
            best = max([x["annual_rate"] for x in fd], default=6.5)
            result[category] = {
                "status": "rate_table" if fd else "estimate",
                "source": "Bank-rate registry",
                "metrics": {"available": True, "sample_size": len(fd), "return_1y": best, "return_3y": best, "return_5y": best},
                "updated_at": max([x["updated_at"] for x in fd], default=_now()),
                "analyzed_options": [{"name": x["bank"], "tenure": x["tenure"], "rate": x["annual_rate"]} for x in fd[:6]],
            }
        else:
            result[category] = {
                "status": "pending",
                "source": "No compatible public source configured",
                "metrics": {"available": False},
                "updated_at": None,
                "analyzed_options": [],
            }

    # User-facing info lists should show the full backend universe actually traced.
    stock_symbols=[x for x in WATCHLIST if x["category"]=="STOCKS"]
    currency_symbols=[x for x in WATCHLIST if x["category"]=="CURRENCY"]
    fno_symbols=[x for x in WATCHLIST if x["category"]=="FNO"]
    gold_symbols=[x for x in WATCHLIST if x["category"]=="GOLD"]
    commodity_symbols=[x for x in WATCHLIST if x["category"]=="COMMODITY"]

    # Add the requested Indian index universe.
    result.setdefault("stocks", {}).setdefault("analyzed_options", [])
    result["stocks"]["analyzed_options"] = (
        [{"name":x["name"],"symbol":x["symbol"]} for x in stock_symbols] +
        [{"name":"NIFTY 50","symbol":"INDEX:NIFTY 50"},
         {"name":"NIFTY 100","symbol":"INDEX:NIFTY 100"},
         {"name":"NIFTY Midcap 100","symbol":"INDEX:NIFTY MIDCAP 100"},
         {"name":"NIFTY LargeMidcap 250","symbol":"INDEX:NIFTY LARGE MIDCAP 250"},
         {"name":"NIFTY Bank","symbol":"INDEX:NIFTY BANK"}]
    )
    result.setdefault("currency", {}).setdefault("analyzed_options", [])
    result["currency"]["analyzed_options"] = [{"name":x["name"],"symbol":x["symbol"]} for x in currency_symbols]
    result.setdefault("fno", {}).setdefault("analyzed_options", [])
    result["fno"]["analyzed_options"] = (
        [{"name":x["name"],"symbol":x["symbol"]} for x in fno_symbols] +
        [{"name":"NIFTY Futures (nearest contract)","symbol":"FUTIDX:NIFTY"}]
    )
    result.setdefault("gold", {}).setdefault("analyzed_options", [])
    result["gold"]["analyzed_options"] = [{"name":x["name"],"symbol":x["symbol"]} for x in gold_symbols]
    result.setdefault("commodities", {}).setdefault("analyzed_options", [])
    result["commodities"]["analyzed_options"] = [{"name":x["name"],"symbol":x["symbol"]} for x in commodity_symbols]
    # Bond info tab: show the representative return proxies plus current yield references.
    bond_proxy_opts = result.get('bonds',{}).get('analyzed_options',[])
    result.setdefault("bonds", {}).setdefault("analyzed_options", [])
    if bond_proxy_opts:
        result["bonds"]["analyzed_options"] = bond_proxy_opts
    else:
        result["bonds"]["analyzed_options"] = [
            {"name":"India 10Y Government Bond","symbol":"GSEC:10Y","kind":"bond_yield","term":"10Y G-Sec"},
            {"name":"India 5Y AAA Corporate Bond","symbol":"CORP:AAA5Y","kind":"bond_yield","term":"5Y AAA"},
        ]
    return result



_LIVE_EXTRA = {"updated_at": None, "items": [], "index_items": [], "headline": [], "fno": None, "mcx": [], "currencies": [], "mfs": [], "bonds": []}

_LIVE_REFRESH_LOCK = threading.Lock()
_LIVE_REFRESH_STATE_LOCK = threading.Lock()
_LIVE_REFRESH_RUNNING = False
_LIVE_REFRESH_MIN_SECONDS = 45

def _usd_rate_fast():
    try:
        t=yf.Ticker("USDINR=X")
        hist=t.history(period="5d", interval="1d", auto_adjust=False, prepost=False)
        if hist is not None and not hist.empty:
            return _safe_float(hist.iloc[-1].get("Close"))
    except Exception:
        pass
    return None

def _convert_public_usd_commodity_to_inr(price, label):
    """Convert a Yahoo USD commodity quote into an INR-denominated value.

    MCX values are already INR and are not passed through this helper.
    """
    rate=_usd_rate_fast()
    if rate is None or price is None:
        return price, None
    p=float(price)
    name=label.lower()
    if "gold" in name:
        # Yahoo gold futures are USD/troy-ounce; express as INR/10g.
        return p*rate/31.1034768*10.0, "₹/10g"
    if "silver" in name:
        return p*rate/31.1034768*1000.0, "₹/kg"
    if "copper" in name:
        # HG=F is USD/lb; convert to INR/kg.
        return p*rate*2.2046226218, "₹/kg"
    if "crude" in name:
        return p*rate, "₹/barrel"
    if "natural gas" in name:
        return p*rate, "₹/MMBtu"
    return p*rate, "₹ equivalent"

def _yf_quote_fallback(symbol: str, label: str, kind: str):
    """Fallback current quote from the same free public Yahoo layer already used by FinanX."""
    try:
        t=yf.Ticker(symbol)
        # Fast path: daily public quote. Only fall back to minute history if needed.
        hist=t.history(period="5d", interval="1d", auto_adjust=False, prepost=False)
        if hist is None or hist.empty:
            hist=t.history(period="2d", interval="1m", auto_adjust=False, prepost=False)
        if hist is None or hist.empty:
            return None
        price=_safe_float(hist.iloc[-1].get("Close"))
        if price is None:
            return None
        daily=t.history(period="5d", interval="1d", auto_adjust=False)
        prev=None
        if daily is not None and len(daily)>=2:
            prev=_safe_float(daily.iloc[-2].get("Close"))
        pct=None if prev in (None,0) else (price/prev-1)*100
        unit=None
        if kind=="mcx" and symbol not in {"GOLDBEES.NS"}:
            converted, unit = _convert_public_usd_commodity_to_inr(price, label)
            if converted is not None:
                price = converted
        return {
            "label": label,
            "value": price,
            "today_change": pct,
            "kind": kind,
            "timestamp": str(hist.index[-1]),
            "unit": unit,
            "source_currency": "INR",
        }
    except Exception:
        return None

def _ensure_amfi_snapshot():
    """Populate the local AMFI table on-demand so the first page load is never empty."""
    try:
        existing=latest_mutual_funds()
        if existing:
            return existing
        result=update_amfi_metrics()
        if result.get("success"):
            return latest_mutual_funds()
    except Exception:
        pass
    return latest_mutual_funds()

def _headline_quotes():
    """Small fast quote set used by the hero/market-now cards. Runs before slower public endpoints."""
    picks = [
        ("^NSEI", "NIFTY 50", "index"),
        ("GC=F", "Gold", "mcx"),
        ("USDINR=X", "USD/INR", "currency"),
    ]
    out=[]
    for symbol, label, kind in picks:
        q=_yf_quote_fallback(symbol, label, kind)
        if q:
            q["yoy"]=None
            out.append(q)
    return out

def _live_cache_stale():
    stamp=_LIVE_EXTRA.get("updated_at")
    if not stamp:
        return True
    try:
        dt=datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
        return (datetime.now(timezone.utc)-dt).total_seconds() >= _LIVE_REFRESH_MIN_SECONDS
    except Exception:
        return True

def start_live_market_refresh(force=False):
    """Start a background live-value refresh without blocking Flask requests."""
    global _LIVE_REFRESH_RUNNING
    if not force and not _live_cache_stale():
        return
    with _LIVE_REFRESH_STATE_LOCK:
        if _LIVE_REFRESH_RUNNING:
            return
        _LIVE_REFRESH_RUNNING=True
    def worker():
        global _LIVE_REFRESH_RUNNING
        try:
            refresh_live_market_values()
        finally:
            _LIVE_REFRESH_RUNNING=False
    threading.Thread(target=worker, name="finanx-live-values", daemon=True).start()

def refresh_live_market_values():
    """Best-effort live/current market layer. It never invents a quote."""
    global _LIVE_EXTRA
    if not _LIVE_REFRESH_LOCK.acquire(blocking=False):
        return _LIVE_EXTRA
    try:
        items=[]

        # Fast hero values first. These are enough for the first screen even if a
        # slower specialist endpoint below is unavailable.
        try:
            headline=_headline_quotes()
            _LIVE_EXTRA["headline"]=headline
            items.extend(headline)
        except Exception:
            headline=_LIVE_EXTRA.get("headline",[])
            items.extend(headline)

        try:
            idx=nse_indices()
            _LIVE_EXTRA["index_items"]=idx
            items.extend(idx)
        except Exception:
            _LIVE_EXTRA["index_items"]=_LIVE_EXTRA.get("index_items",[])

        try:
            fno=nifty_fno()
            _LIVE_EXTRA["fno"]=fno
            if fno: items.append(fno)
        except Exception:
            _LIVE_EXTRA["fno"]=_LIVE_EXTRA.get("fno")

        try:
            mcx=mcx_watch()
            if not mcx:
                fallback_symbols=[
                    ("GC=F","Gold Futures","mcx"),
                    ("SI=F","Silver Futures","mcx"),
                    ("CL=F","Crude Oil Futures","mcx"),
                    ("NG=F","Natural Gas Futures","mcx"),
                    ("HG=F","Copper Futures","mcx"),
                ]
                mcx=[q for q in (_yf_quote_fallback(*x) for x in fallback_symbols) if q]
            _LIVE_EXTRA["mcx"]=mcx
            items.extend(mcx)
        except Exception:
            _LIVE_EXTRA["mcx"]=_LIVE_EXTRA.get("mcx",[])

        try:
            fx=current_currencies()
            _LIVE_EXTRA["currencies"]=fx
            items.extend(fx)
        except Exception:
            _LIVE_EXTRA["currencies"]=_LIVE_EXTRA.get("currencies",[])

        # Mutual-fund NAV refresh runs in its own worker so it cannot delay the fast
        # market-value layer. Existing cached NAVs are used here immediately.
        try:
            _LIVE_EXTRA["mfs"] = latest_mutual_funds()
        except Exception:
            _LIVE_EXTRA["mfs"] = _LIVE_EXTRA.get("mfs",[])

        try:
            bonds=get_indian_bond_values()
            _LIVE_EXTRA["bonds"]=bonds
            items.extend(bonds)
        except Exception:
            _LIVE_EXTRA["bonds"]=_LIVE_EXTRA.get("bonds",[])

        _LIVE_EXTRA["items"]=items
        _LIVE_EXTRA["updated_at"]=_now()
        return _LIVE_EXTRA
    finally:
        _LIVE_REFRESH_LOCK.release()

def _mfapi_fallback_highlights():
    """Direct free MFAPI fallback for a few representative direct-growth schemes."""
    try:
        import requests as _requests
        queries = [
            "HDFC Nifty 50 Index Fund Direct Growth",
            "Parag Parikh Flexi Cap Fund Direct Growth",
            "HDFC Balanced Advantage Fund Direct Growth",
        ]
        out=[]
        for q in queries:
            try:
                rr=_requests.get("https://api.mfapi.in/mf/search", params={"q":q}, timeout=12,
                                 headers={"User-Agent":"FinanX/1.0 educational project"})
                rr.raise_for_status()
                matches=rr.json() or []
                match=next((x for x in matches if "direct" in str(x.get("schemeName","")).lower()
                            and "growth" in str(x.get("schemeName","")).lower()), None)
                if not match and matches:
                    match=matches[0]
                if not match: continue
                code=match.get("schemeCode")
                if not code: continue
                latest=_requests.get(f"https://api.mfapi.in/mf/{code}/latest", timeout=12,
                                     headers={"User-Agent":"FinanX/1.0 educational project"})
                latest.raise_for_status()
                payload=latest.json()
                row=(payload.get("data") or [{}])[0]
                nav=_safe_float(row.get("nav"))
                if nav is not None:
                    out.append({
                        "label": match.get("schemeName") or payload.get("meta",{}).get("scheme_name","Mutual Fund"),
                        "value": nav,
                        "today_change": None,
                        "kind": "mutual_fund",
                        "timestamp": row.get("date"),
                        "yoy": None
                    })
            except Exception:
                continue
        return out
    except Exception:
        return []

def _offline_reference_highlights() -> list[dict]:
    """Fast non-network fallback so the first screen never stays in a loading state.

    These are last-verified public reference values, not exchange-real-time quotes.
    The background collectors replace them automatically when live/public data becomes available.
    """
    return [
        {
            "label": "NIFTY 50",
            "value": 23346.40,
            "today_change": None,
            "kind": "index",
            "timestamp": "2026-09-18",
            "yoy": None,
            "freshness": "reference",
            "source": "Public market reference — 18 Sep 2026",
        },
        {
            "label": "Gold",
            "value": 133633.13,
            "today_change": None,
            "kind": "mcx",
            "timestamp": "2026-09-15",
            "yoy": None,
            "freshness": "reference",
            "unit": "₹/10g",
            "source": "INR-equivalent gold reference — 15 Sep 2026",
        },
        {
            "label": "USD/INR",
            "value": 95.93,
            "today_change": None,
            "kind": "currency",
            "timestamp": "2026-09-17",
            "yoy": None,
            "freshness": "reference",
            "source": "Public INR/USD reference — 17 Sep 2026",
        },
    ]

def get_market_highlights() -> list[dict]:
    """User-facing current values. Uses the live layer first, then local stored data."""
    global _LIVE_EXTRA
    now = datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(_LIVE_EXTRA.get("updated_at").replace("Z","+00:00")) if _LIVE_EXTRA.get("updated_at") else None
    except Exception:
        last = None
    if last is None or (now-last).total_seconds() >= _LIVE_REFRESH_MIN_SECONDS:
        # The background worker refreshes independently; never make this HTTP request wait.
        start_live_market_refresh()

    out=[]
    # 1) Fast headline values first, followed by specialized live sources.
    out.extend(_LIVE_EXTRA.get("headline", []))
    for x in _LIVE_EXTRA.get("index_items", []):
        out.append({
            "label":x["label"], "value":x.get("value"), "today_change":x.get("today_change"),
            "kind":"index", "timestamp":x.get("timestamp")
        })

    if _LIVE_EXTRA.get("fno"):
        out.append(_LIVE_EXTRA["fno"])

    out.extend(_LIVE_EXTRA.get("mcx", [])[:6])
    out.extend(_LIVE_EXTRA.get("currencies", [])[:10])
    out.extend(_LIVE_EXTRA.get("mfs", [])[:4])
    out.extend(_LIVE_EXTRA.get("bonds", [])[:2])

    # 2) Critical fallback: the automatic yfinance collector already stores
    # current/most-recent values in SQLite. Use those values if a specialized
    # live endpoint (NSE/MCX/etc.) did not return anything.
    try:
        rows=market_rows()
        by_symbol={r.get("symbol"):r for r in rows}

        existing_labels={str(x.get("label")) for x in out}
        fallbacks=[
            ("NIFTY 50","^NSEI","index"),
            ("Gold","GC=F","mcx"),
            ("USD/INR","USDINR=X","currency"),
        ]
        for label,symbol,kind in fallbacks:
            if any(str(x.get("label"))==label for x in out):
                continue
            r=by_symbol.get(symbol)
            if r and r.get("price") is not None:
                value=r.get("price")
                unit=None
                if symbol=="GC=F":
                    value, unit = _convert_public_usd_commodity_to_inr(value, "Gold")
                out.append({
                    "label":label,
                    "value":value,
                    "today_change":r.get("day_change_pct"),
                    "kind":kind,
                    "timestamp":r.get("timestamp"),
                    "yoy":r.get("return_1y"),
                    "unit":unit,
                    "source_currency":"INR",
                })
    except Exception:
        pass

    # 3) Use the local AMFI cache immediately. The AMFI worker refreshes it in the
    # background; this request must stay fast and never perform a network lookup.
    if not any(x.get("kind")=="mutual_fund" for x in out):
        try:
            cached_mfs=latest_mutual_funds()
            out.extend(cached_mfs[:4])
        except Exception:
            pass

    # 4) Never leave the hero/Market Now cards empty while external feeds are cold/unavailable.
    # Fill only the missing critical cards with verified reference values; live/cache data wins.
    reference = _offline_reference_highlights()
    present_labels = {str(x.get("label")) for x in out}
    out.extend(x for x in reference if x.get("label") not in present_labels)

    # De-duplicate by display label while preserving the first successfully
    # obtained value. This prevents live + fallback copies from appearing twice.
    seen=set()
    clean=[]
    for item in out:
        key=(item.get("label"), item.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        clean.append(item)
    return clean

def get_market_snapshot():
    rows = market_rows()
    groups = {
        "stocks": {"label": "Stocks", "category": "STOCKS", "status": "auto", "source": "Yahoo Finance / yfinance", "refresh": "automatic public-data refresh"},
        "fno": {"label": "F&O", "category": "FNO", "status": "auto", "source": "Yahoo Finance derivatives reference", "refresh": "automatic public-data refresh"},
        "gold": {"label": "Gold", "category": "GOLD", "status": "auto", "source": "Yahoo Finance / gold futures", "refresh": "automatic public-data refresh"},
        "commodities": {"label": "Commodities", "category": "COMMODITY", "status": "auto", "source": "Yahoo Finance / commodity futures", "refresh": "automatic public-data refresh"},
        "mutual-funds": {"label": "Mutual Funds", "category": "MUTUAL_FUNDS", "status": "daily", "source": "AMFI daily NAV (next integration)", "refresh": "daily NAV"},
        "bonds": {"label": "Bonds", "category": "BONDS", "status": "auto", "source": "Bond proxy data (public market source)", "refresh": "automatic public-data refresh"},
        "currency": {"label": "Currency", "category": "CURRENCY", "status": "auto", "source": "Yahoo Finance / USD-INR", "refresh": "automatic public-data refresh"},
        "fd": {"label": "FD", "category": "FD", "status": "rate_table", "source": "Bank-rate registry", "refresh": "when bank rates change"},
    }
    out = []
    for slug, meta in groups.items():
        items = [r for r in rows if r["category"] == meta["category"]][:3]
        status = meta["status"] if items or slug in {"fd", "mutual-funds"} else "needs_api_key"
        lead = items[0] if items else None
        out.append({
            "slug": slug,
            "label": meta["label"],
            "status": status,
            "source": meta["source"],
            "refresh": meta["refresh"],
            "items": [{"symbol": r["symbol"], "price": r["price"], "change": r["day_change_pct"], "timestamp": r["timestamp"],
                       "return_30d": r.get("return_30d"), "return_90d": r.get("return_90d"), "return_1y": r.get("return_1y"), "return_3y": r.get("return_3y"), "return_5y": r.get("return_5y"),
                       "volatility_annualized": r.get("volatility_annualized"), "max_drawdown": r.get("max_drawdown"),
                       "trend_score": r.get("trend_score")} for r in items],
            "trend": ({
                "symbol": lead.get("symbol"),
                "price": lead.get("price"),
                "day_change": lead.get("day_change_pct"),
                "return_30d": lead.get("return_30d"),
                "return_90d": lead.get("return_90d"),
                "return_1y": lead.get("return_1y"),
                "trend_score": lead.get("trend_score"),
                "updated_at": lead.get("timestamp"),
            } if lead else None),
        })
    return {
        "generated_at": _now(),
        "mode": "auto-public-data",
        "segments": out,
        "message": "FinanX automatically collects publicly available market data. Exact freshness depends on the source; this is not an exchange-licensed real-time feed.",
    }
