from __future__ import annotations

from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request

from allocation_engine import ASSET_INFO, build_portfolio
from database import init_database
from recommendation_engine import build_market_adjusted_plan
import vercel_market

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
        market = vercel_market.category_market_analysis()
        result = build_market_adjusted_plan(amount, horizon, risk, liquidity, goal, emergency, market)
        return jsonify({'generated_at': datetime.now(timezone.utc).isoformat(), 'market': market, **result})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'Analysis service error: {exc}'}), 500


@app.get('/api/market')
def market():
    return jsonify(vercel_market.market_snapshot())


@app.get('/api/market/highlights')
def market_highlights():
    return jsonify({'items': vercel_market.market_highlights()})


@app.post('/api/market/refresh')
def refresh_market():
    return jsonify({'success': 0, 'errors': ['Manual background collection is disabled on Vercel; market data refreshes on request.'], 'updated_at': datetime.now(timezone.utc).isoformat()})


@app.get('/api/asset/<slug>')
def asset(slug: str):
    for item in ASSET_INFO:
        if item['slug'] == slug:
            return jsonify(item)
    return jsonify({'error': 'Asset category not found.'}), 404


@app.get('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.now(timezone.utc).isoformat(), 'data_engine': 'vercel-request-refresh', 'upstox_required': False})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
