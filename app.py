from __future__ import annotations
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from allocation_engine import ASSET_INFO, build_portfolio
from database import init_database
from market_data import get_market_snapshot, get_market_highlights, get_category_market_analysis, start_background_collector, start_amfi_refresh, start_live_market_refresh, collect_once
from recommendation_engine import build_market_adjusted_plan

load_dotenv()
app=Flask(__name__)
init_database(app)

# Vercel runs Flask as a request-driven serverless application. Do not start
# perpetual daemon threads there: they are not a reliable scheduling mechanism
# in a serverless runtime and would waste compute. Vercel requests use the
# synchronous/live fallback layer in market_data.py instead.
IS_VERCEL = os.getenv("VERCEL") == "1"

if not IS_VERCEL:
    # Start FinanX's automatic public-data collectors on a normal long-running host.
    start_background_collector()
    start_amfi_refresh()
    # Current market values refresh independently so page load never waits on external market sites.
    start_live_market_refresh()


def _parse_amount(raw: str) -> float:
    try: amount=float(raw.replace(',','').strip())
    except (AttributeError,ValueError): raise ValueError('Please enter a valid amount.')
    if amount<1000: raise ValueError('Enter an amount of at least ₹1,000 for the simulation.')
    return amount

@app.get('/')
def home():
    return render_template('dashboard.html', assets=ASSET_INFO)

@app.post('/api/plan')
def plan():
    try:
        p=request.get_json(force=True)
        amount=_parse_amount(str(p.get('amount','0')))
        horizon=int(p.get('horizon',5)); risk=str(p.get('risk','moderate')).lower(); liquidity=str(p.get('liquidity','medium')).lower(); goal=str(p.get('goal','balanced_growth')).lower(); emergency=str(p.get('emergency','yes')).lower()
        result=build_portfolio(amount=amount,horizon=horizon,risk=risk,liquidity=liquidity,goal=goal,emergency=emergency)
        result['generated_at']=datetime.now(timezone.utc).isoformat()
        return jsonify(result)
    except ValueError as exc: return jsonify({'error':str(exc)}),400

@app.post('/api/analyze')
def analyze():
    try:
        p=request.get_json(force=True)
        amount=_parse_amount(str(p.get('amount','0')))
        horizon=int(p.get('horizon',5)); risk=str(p.get('risk','moderate')).lower(); liquidity=str(p.get('liquidity','medium')).lower(); goal=str(p.get('goal','balanced_growth')).lower(); emergency=str(p.get('emergency','yes')).lower()
        market=get_category_market_analysis()
        result=build_market_adjusted_plan(amount,horizon,risk,liquidity,goal,emergency,market)
        return jsonify({'generated_at':datetime.now(timezone.utc).isoformat(),'market':market,**result})
    except ValueError as exc: return jsonify({'error':str(exc)}),400

@app.get('/api/market')
def market():
    return jsonify(get_market_snapshot())

@app.get('/api/market/highlights')
def market_highlights():
    # On a long-running host the background worker owns refreshes. On Vercel,
    # get_market_highlights performs a synchronous best-effort refresh when its
    # in-memory live cache is stale.
    return jsonify({"items": get_market_highlights()})

@app.post('/api/market/refresh')
def refresh_market():
    # This endpoint remains useful for manual/admin refreshes. It is deliberately
    # not called automatically by the browser.
    return jsonify(collect_once())

@app.get('/api/asset/<slug>')
def asset(slug:str):
    for item in ASSET_INFO:
        if item['slug']==slug: return jsonify(item)
    return jsonify({'error':'Asset category not found.'}),404

@app.get('/api/health')
def health():
    return jsonify({'status':'ok','time':datetime.now(timezone.utc).isoformat(),'data_engine':'public-auto-refresh','upstox_required':False})

if __name__=='__main__':
    app.run(host=os.getenv('HOST','0.0.0.0'),port=int(os.getenv('PORT','5000')),debug=False,use_reloader=False)
