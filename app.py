from __future__ import annotations
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from allocation_engine import ASSET_INFO, build_portfolio
from database import init_database

load_dotenv()
app=Flask(__name__)
init_database(app)

# Vercel uses request-driven serverless functions. Keep heavyweight market-data
# integrations lazy so an optional provider/library failure cannot crash the
# entire Flask function before the homepage can render.
IS_VERCEL = os.getenv("VERCEL") == "1"

if not IS_VERCEL:
    try:
        from market_data import start_background_collector, start_amfi_refresh, start_live_market_refresh
        start_background_collector()
        start_amfi_refresh()
        start_live_market_refresh()
    except Exception:
        # A normal long-running host can still serve the planner if an optional
        # market-data provider is temporarily unavailable at startup.
        pass


def _parse_amount(raw: str) -> float:
    try:
        amount=float(raw.replace(',','').strip())
    except (AttributeError,ValueError):
        raise ValueError('Please enter a valid amount.')
    if amount<1000:
        raise ValueError('Enter an amount of at least ₹1,000 for the simulation.')
    return amount


def _live_reference_items() -> list[dict]:
    """Fast current-value layer that does not import yfinance/mcxlib."""
    picks = [
        ("^NSEI", "NIFTY 50", "index", None),
        ("GC=F", "Gold", "gold", "gold_10g"),
        ("USDINR=X", "USD/INR", "currency", None),
    ]
    out=[]
    for symbol,label,kind,conversion in picks:
        try:
            url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            payload=requests.get(
                url,
                params={"range":"5d","interval":"1d"},
                timeout=8,
                headers={"User-Agent":"FinanX/1.0 educational project"},
            ).json()
            result=(payload.get("chart") or {}).get("result") or []
            if not result:
                continue
            r=result[0]
            closes=((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            timestamps=r.get("timestamp") or []
            valid=[(ts,px) for ts,px in zip(timestamps,closes) if px is not None]
            if not valid:
                continue
            _,price=valid[-1]
            previous=valid[-2][1] if len(valid)>=2 else None
            change=None if previous in (None,0) else (float(price)/float(previous)-1)*100
            value=float(price)
            unit=None
            if conversion=="gold_10g":
                # Yahoo GC=F is USD per troy ounce. Convert using USD/INR.
                fx_price=None
                try:
                    fx_payload=requests.get(
                        "https://query1.finance.yahoo.com/v8/finance/chart/USDINR=X",
                        params={"range":"5d","interval":"1d"},
                        timeout=8,
                        headers={"User-Agent":"FinanX/1.0 educational project"},
                    ).json()
                    fx_result=((fx_payload.get("chart") or {}).get("result") or [])
                    fx_closes=((((fx_result or [{}])[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
                    fx_valid=[x for x in fx_closes if x is not None]
                    if fx_valid:
                        fx_price=float(fx_valid[-1])
                except Exception:
                    pass
                if fx_price is not None:
                    value=value*fx_price/31.1034768*10.0
                unit="₹/10g"
            out.append({
                "label":label,
                "value":round(value,2),
                "today_change":round(change,3) if change is not None else None,
                "kind":kind,
                "timestamp":datetime.fromtimestamp(valid[-1][0], tz=timezone.utc).isoformat(),
                "unit":unit,
                "source_currency":"INR",
                "freshness":"public-auto-refresh",
            })
        except Exception:
            continue
    return out


def _offline_reference_items() -> list[dict]:
    # Last-verified public references used only when external quote providers are
    # temporarily unavailable. The UI already labels such values as reference.
    return [
        {"label":"NIFTY 50","value":23346.40,"today_change":None,"kind":"index","timestamp":"2026-09-18","freshness":"reference","source":"Public market reference"},
        {"label":"Gold","value":133633.13,"today_change":None,"kind":"gold","timestamp":"2026-09-15","freshness":"reference","unit":"₹/10g","source":"INR-equivalent public reference"},
        {"label":"USD/INR","value":95.93,"today_change":None,"kind":"currency","timestamp":"2026-09-17","freshness":"reference","source":"Public INR/USD reference"},
    ]


def _market_module():
    try:
        import market_data
        return market_data
    except Exception:
        return None


@app.get('/')
def home():
    return render_template('dashboard.html', assets=ASSET_INFO)


@app.post('/api/plan')
def plan():
    try:
        p=request.get_json(force=True)
        amount=_parse_amount(str(p.get('amount','0')))
        horizon=int(p.get('horizon',5))
        risk=str(p.get('risk','moderate')).lower()
        liquidity=str(p.get('liquidity','medium')).lower()
        goal=str(p.get('goal','balanced_growth')).lower()
        emergency=str(p.get('emergency','yes')).lower()
        result=build_portfolio(
            amount=amount,horizon=horizon,risk=risk,liquidity=liquidity,
            goal=goal,emergency=emergency
        )
        result['generated_at']=datetime.now(timezone.utc).isoformat()
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400


@app.post('/api/analyze')
def analyze():
    try:
        p=request.get_json(force=True)
        amount=_parse_amount(str(p.get('amount','0')))
        horizon=int(p.get('horizon',5))
        risk=str(p.get('risk','moderate')).lower()
        liquidity=str(p.get('liquidity','medium')).lower()
        goal=str(p.get('goal','balanced_growth')).lower()
        emergency=str(p.get('emergency','yes')).lower()

        market_module=_market_module()
        market = market_module.get_category_market_analysis() if market_module else {}

        from recommendation_engine import build_market_adjusted_plan
        result=build_market_adjusted_plan(
            amount,horizon,risk,liquidity,goal,emergency,market
        )
        return jsonify({
            'generated_at':datetime.now(timezone.utc).isoformat(),
            'market':market,
            **result
        })
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400
    except Exception as exc:
        # Keep the planner usable even if an optional market provider is down.
        try:
            from recommendation_engine import build_market_adjusted_plan
            p=request.get_json(force=True)
            amount=_parse_amount(str(p.get('amount','0')))
            horizon=int(p.get('horizon',5))
            risk=str(p.get('risk','moderate')).lower()
            liquidity=str(p.get('liquidity','medium')).lower()
            goal=str(p.get('goal','balanced_growth')).lower()
            emergency=str(p.get('emergency','yes')).lower()
            result=build_market_adjusted_plan(
                amount,horizon,risk,liquidity,goal,emergency,{}
            )
            result['market_error']="Some live market data was unavailable; this scenario used FinanX model assumptions."
            return jsonify({
                'generated_at':datetime.now(timezone.utc).isoformat(),
                'market':{},
                **result
            })
        except Exception as inner_exc:
            return jsonify({'error':f'Planner service error: {inner_exc}'}),500


@app.get('/api/market')
def market():
    module=_market_module()
    if module:
        try:
            return jsonify(module.get_market_snapshot())
        except Exception:
            pass
    items=_live_reference_items() or _offline_reference_items()
    return jsonify({
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "mode":"public-reference-fallback",
        "segments":[],
        "items":items,
        "message":"Market feeds are temporarily unavailable; showing public reference values where needed."
    })


@app.get('/api/market/highlights')
def market_highlights():
    # Use the light request-driven layer first on Vercel. If the full market
    # module is healthy it can provide the richer MCX/AMFI/NSE universe.
    items=_live_reference_items()
    module=_market_module()
    if module:
        try:
            rich=module.get_market_highlights()
            if rich:
                items=rich
        except Exception:
            pass
    if not items:
        items=_offline_reference_items()
    return jsonify({"items":items})


@app.post('/api/market/refresh')
def refresh_market():
    module=_market_module()
    if not module:
        return jsonify({'error':'Market collector is unavailable in this serverless runtime.'}),503
    try:
        return jsonify(module.collect_once())
    except Exception as exc:
        return jsonify({'error':str(exc)}),500


@app.get('/api/asset/<slug>')
def asset(slug:str):
    for item in ASSET_INFO:
        if item['slug']==slug:
            return jsonify(item)
    return jsonify({'error':'Asset category not found.'}),404


@app.get('/api/health')
def health():
    return jsonify({
        'status':'ok',
        'time':datetime.now(timezone.utc).isoformat(),
        'data_engine':'public-auto-refresh',
        'upstox_required':False
    })


if __name__=='__main__':
    app.run(
        host=os.getenv('HOST','0.0.0.0'),
        port=int(os.getenv('PORT','5000')),
        debug=False,
        use_reloader=False
    )
