from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from allocation_engine import ASSET_INFO, build_portfolio
from database import init_database
from recommendation_engine import build_market_adjusted_plan
import vercel_market
import market_universe
from amfi_data import update_amfi_metrics
from database import mutual_fund_metrics

app = Flask(__name__)
init_database(app)


def _parse_amount(raw: str) -> float:
    try:
        amount = float(raw.replace(',', '').strip())
    except (AttributeError, ValueError):
        raise ValueError('Please enter a valid amount.')
    if amount < 1000:
        raise ValueError('Enter an amount of at least ₹1,000 for the simulation.')
    return amount


def _engine():
    # Upstox is optional at deploy time. When the token is present we use the
    # Phase-1 NSE/BSE adapter; otherwise the existing public-data fallback stays usable.
    try:
        import upstox_adapter
        if upstox_adapter.configured():
            return upstox_adapter
    except Exception:
        pass
    return vercel_market


@app.get('/')
def home():
    return render_template('dashboard.html', assets=ASSET_INFO)


@app.post('/api/plan')
def plan():
    try:
        p = request.get_json(force=True)
        result = build_portfolio(
            amount=_parse_amount(str(p.get('amount', '0'))),
            horizon=int(p.get('horizon', 5)),
            risk=str(p.get('risk', 'moderate')).lower(),
            liquidity=str(p.get('liquidity', 'medium')).lower(),
            goal=str(p.get('goal', 'balanced_growth')).lower(),
            emergency=str(p.get('emergency', 'yes')).lower(),
        )
        result['generated_at'] = datetime.now(timezone.utc).isoformat()
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'Planner service error: {exc}'}), 500


@app.post('/api/analyze')
def analyze():
    try:
        p = request.get_json(force=True)
        amount = _parse_amount(str(p.get('amount', '0')))
        horizon = int(p.get('horizon', 5))
        risk = str(p.get('risk', 'moderate')).lower()
        liquidity = str(p.get('liquidity', 'medium')).lower()
        goal = str(p.get('goal', 'balanced_growth')).lower()
        emergency = str(p.get('emergency', 'yes')).lower()
        market = _engine().category_market_analysis()
        result = build_market_adjusted_plan(amount, horizon, risk, liquidity, goal, emergency, market)
        return jsonify({'generated_at': datetime.now(timezone.utc).isoformat(), 'market': market, **result})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'Analysis service error: {exc}'}), 500


@app.get('/api/market')
def market():
    try:
        return jsonify(_engine().market_snapshot())
    except Exception as exc:
        return jsonify({'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': 'fallback', 'segments': [], 'items': [], 'message': str(exc)})


@app.get('/api/market/highlights')
def market_highlights():
    try:
        return jsonify({'items': _engine().market_highlights()})
    except Exception:
        return jsonify({'items': []})


@app.post('/api/market/refresh')
def refresh_market():
    return jsonify({'success': 1, 'message': 'Market data refreshes per request on Vercel.', 'updated_at': datetime.now(timezone.utc).isoformat()})



_COMPARE_CACHE = {}
def _compare_cached(segment, factory, ttl=900):
    now = datetime.now(timezone.utc).timestamp()
    hit = _COMPARE_CACHE.get(segment)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = factory()
    _COMPARE_CACHE[segment] = (now, value)
    return value

@app.get('/api/market/compare/<segment>')
def market_compare(segment: str):
    """Return live comparison tables. Upstox quotes are exchange snapshots;
    AMFI supplies mutual-fund NAV/history; FD rates are labelled by source/date."""
    try:
        if segment == 'stocks':
            rows = _compare_cached('stocks', market_universe.compare_stocks)
            return jsonify({'segment':'stocks','count':len(rows),'source':'Upstox Full Market Quotes V3','items':rows})
        if segment == 'fno':
            rows = _compare_cached('fno', market_universe.compare_fno)
            return jsonify({'segment':'fno','count':len(rows),'source':'Upstox Full Market Quotes V3','items':rows})
        if segment == 'funds':
            refreshed = _compare_cached('funds-refresh', update_amfi_metrics, ttl=1800)
            rows = mutual_fund_metrics()
            deduped = {}
            for row in rows:
                if str(row.get('source','')).startswith('Bond proxy'): continue
                code = str(row.get('scheme_code') or row.get('scheme_name') or '')
                deduped[code] = row
            rows = list(deduped.values())[:100]
            return jsonify({'segment':'funds','count':len(rows),'source':'AMFI official NAV/history','refresh':refreshed,'items':rows})
        if segment == 'bonds':
            rows = _compare_cached('bonds', market_universe.compare_bonds)
            return jsonify({'segment':'bonds','count':len(rows),'source':'Upstox exchange quotes for listed bond/debt ETFs','items':rows})
        if segment == 'fd':
            rows = _compare_cached('fd', market_universe.compare_fds, ttl=3600)
            return jsonify({'segment':'fd','count':len(rows),'source':'Official bank rate pages; general public, ~1-year tenor','items':rows})
        return jsonify({'error':'Unknown comparison segment'}), 404
    except Exception as exc:
        return jsonify({'segment':segment,'count':0,'items':[],'error':str(exc)}), 500

@app.get('/api/asset/<slug>')
def asset(slug: str):
    for item in ASSET_INFO:
        if item['slug'] == slug:
            return jsonify(item)
    return jsonify({'error': 'Asset category not found.'}), 404


@app.get('/api/health')
def health():
    try:
        import upstox_adapter
        upstox = upstox_adapter.healthcheck()
    except Exception as exc:
        upstox = {'configured': False, 'reachable': False, 'error': str(exc)}
    return jsonify({
        'status': 'ok',
        'time': datetime.now(timezone.utc).isoformat(),
        'runtime': 'vercel-flask',
        'market_engine': 'upstox-phase1-with-public-fallback',
        'upstox': upstox,
    })


if __name__ == '__main__':
    app.run(host=os.getenv('HOST', '0.0.0.0'), port=int(os.getenv('PORT', '5000')), debug=False, use_reloader=False)
