from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from allocation_engine import ASSET_INFO, build_portfolio
from recommendation_engine import build_market_adjusted_plan
import upstox_adapter
import market_universe

app = Flask(__name__)
logger = logging.getLogger(__name__)

_ALLOWED_RISKS = {"low", "moderate", "high"}
_ALLOWED_LIQUIDITY = {"high", "medium", "low"}
_ALLOWED_GOALS = {
    "balanced_growth",
    "capital_preservation",
    "wealth",
    "education",
}
_ALLOWED_EMERGENCY = {"yes", "no"}
_MIN_HORIZON = 1
_MAX_HORIZON = 50


def _parse_amount(raw: str) -> float:
    try:
        amount = float(str(raw).replace(',', '').strip())
    except (AttributeError, ValueError):
        raise ValueError('Please enter a valid amount.')
    if not math.isfinite(amount):
        raise ValueError('Please enter a finite amount.')
    if amount < 1000:
        raise ValueError('Enter an amount of at least ₹1,000 for the simulation.')
    return amount


def _parse_plan_inputs(payload):
    if not isinstance(payload, dict):
        raise ValueError('Request body must be a JSON object.')

    try:
        horizon = int(payload.get('horizon', 5))
    except (TypeError, ValueError):
        raise ValueError('Time horizon must be a whole number of years.')

    risk = str(payload.get('risk', 'moderate')).strip().lower()
    liquidity = str(payload.get('liquidity', 'medium')).strip().lower()
    goal = str(payload.get('goal', 'balanced_growth')).strip().lower()
    emergency = str(payload.get('emergency', 'yes')).strip().lower()

    if risk not in _ALLOWED_RISKS:
        raise ValueError('Risk must be low, moderate, or high.')
    if liquidity not in _ALLOWED_LIQUIDITY:
        raise ValueError('Liquidity must be high, medium, or low.')
    if goal not in _ALLOWED_GOALS:
        raise ValueError('Invalid financial goal.')
    if emergency not in _ALLOWED_EMERGENCY:
        raise ValueError('Emergency fund must be yes or no.')
    if not _MIN_HORIZON <= horizon <= _MAX_HORIZON:
        raise ValueError(f'Time horizon must be between {_MIN_HORIZON} and {_MAX_HORIZON} years.')

    return {
        'amount': _parse_amount(payload.get('amount', '0')),
        'horizon': horizon,
        'risk': risk,
        'liquidity': liquidity,
        'goal': goal,
        'emergency': emergency,
    }


def _engine():
    """Return the single configured market-data engine used by FinanX."""
    if not upstox_adapter.configured():
        raise RuntimeError('UPSTOX_ANALYTICS_TOKEN is not configured.')
    return upstox_adapter


@app.get('/')
def home():
    return render_template('dashboard.html', assets=ASSET_INFO)


@app.post('/api/plan')
def plan():
    try:
        p = request.get_json(force=True)
        inputs = _parse_plan_inputs(p)
        result = build_portfolio(
            amount=inputs['amount'],
            horizon=inputs['horizon'],
            risk=inputs['risk'],
            liquidity=inputs['liquidity'],
            goal=inputs['goal'],
            emergency=inputs['emergency'],
        )
        result['generated_at'] = datetime.now(timezone.utc).isoformat()
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logger.exception("Planner endpoint failed")
        return jsonify({'error': f'Planner service error: {exc}'}), 500


@app.post('/api/analyze/fast')
def analyze_fast():
    """Return a fast plan, enriching it with cached Upstox analysis when available."""
    try:
        p = request.get_json(force=True)
        inputs = _parse_plan_inputs(p)
        amount = inputs['amount']
        horizon = inputs['horizon']
        risk = inputs['risk']
        liquidity = inputs['liquidity']
        goal = inputs['goal']
        emergency = inputs['emergency']

        base = build_portfolio(amount, horizon, risk, liquidity, goal, emergency)
        market = upstox_adapter.cached_market_analysis()

        if market is not None:
            try:
                result = build_market_adjusted_plan(
                    amount, horizon, risk, liquidity, goal, emergency, market
                )
                result["provisional"] = False
                result["explanation"] = (
                    "Plan generated using the available tracked Upstox market analysis "
                    "and your selected risk, horizon, liquidity and goal."
                )
                return jsonify({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    **result,
                })
            except Exception:
                logger.exception("Cached Upstox plan calculation failed")

        # Profile-based plan is the intentional graceful-degradation path.
        # It is clearly labelled and never presented as market-history output.
        for item in base["allocations"]:
            rate = {
                "fd": 6.25, "bonds": 7.0, "mutual-funds": 10.0,
                "gold": 7.0, "stocks": 10.0, "commodities": 6.0,
                "currency": 3.0, "fno": 0.0,
            }.get(item["slug"], 6.0)
            value = item["amount"] * ((1 + rate / 100) ** horizon)
            item.update({
                "annual_return_estimate": rate,
                "projected_value": round(value, 2),
                "projected_gain": round(value - item["amount"], 2),
                "yoy_return": None,
                "three_year_return": None,
                "five_year_return": None,
                "basis": "Profile allocation while Upstox history refreshes",
            })
        projected = sum(x["projected_value"] for x in base["allocations"])
        result = dict(base)
        result["projected_value"] = round(projected, 2)
        result["projected_gain"] = round(projected - amount, 2)
        result["annual_return_estimate"] = round(
            ((projected / amount) ** (1 / max(horizon, 1)) - 1) * 100, 2
        )
        result["projected_3y_value"] = None
        result["projected_5y_value"] = None
        result["ranked_categories"] = []
        result["selected_entities"] = []
        result["scenario_comparison"] = []
        result["provisional"] = True
        result["explanation"] = (
            "Profile-based allocation shown immediately. Full market analysis "
            "will use averages from the tracked Upstox universe when history is ready."
        )
        return jsonify({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **result,
        })
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logger.exception("Fast analysis endpoint failed")
        return jsonify({'error': f'Fast analysis service error: {exc}'}), 500


@app.post('/api/analyze')
def analyze():
    try:
        inputs = _parse_plan_inputs(request.get_json(force=True))
        amount = inputs['amount']
        horizon = inputs['horizon']
        risk = inputs['risk']
        liquidity = inputs['liquidity']
        goal = inputs['goal']
        emergency = inputs['emergency']
        engine = _engine()
        market = engine.category_market_analysis(allow_stale=False)
        tracking = market.get('_tracking') or {}
        result = build_market_adjusted_plan(amount, horizon, risk, liquidity, goal, emergency, market)
        return jsonify({
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'market': market,
            'tracking': tracking,
            **result,
        })
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logger.exception("Analysis endpoint failed")
        return jsonify({'error': f'Analysis service error: {exc}'}), 500


@app.post('/api/market/warm')
def warm_market():
    try:
        engine = _engine()
        market = engine.category_market_analysis(allow_stale=False, force=False)
        return jsonify({
            'success': True,
            'ready': True,
            'generated_at': market.get('stocks', {}).get('updated_at'),
            'tracking': market.get('_tracking') or {},
        })
    except Exception as exc:
        return jsonify({'success': False, 'ready': False, 'error': str(exc)}), 500


@app.get('/api/market')
def market():
    try:
        return jsonify(_engine().market_snapshot())
    except Exception as exc:
        return jsonify({'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': 'unavailable', 'segments': [], 'items': [], 'message': str(exc)}), 503


@app.get('/api/market/highlights')
def market_highlights():
    try:
        return jsonify({'items': _engine().market_highlights()})
    except Exception as exc:
        return jsonify({'items': [], 'error': str(exc)}), 503

@app.post('/api/market/refresh')
def refresh_market():
    try:
        _engine().clear_runtime_caches()
        return jsonify({
            'success': True,
            'message': 'Upstox market-data caches cleared. The next request will refresh from Upstox.',
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.exception("Market refresh failed")
        return jsonify({'success': False, 'error': str(exc)}), 503



@app.get('/api/market/compare/<segment>')
def market_compare(segment: str):
    """Return live comparison tables. Upstox quotes are exchange snapshots;
    Mutual-fund data comes from Upstox; FD rates remain separately labelled by source/date."""
    try:
        if segment == 'stocks':
            rows = market_universe.compare_stocks()
            return jsonify({'segment':'stocks','count':len(rows),'source':'Upstox Full Market Quotes V3','items':rows})
        if segment == 'fno':
            rows = market_universe.compare_fno()
            return jsonify({'segment':'fno','count':len(rows),'source':'Upstox Full Market Quotes V3','items':rows})
        if segment == 'funds':
            rows = market_universe.compare_mutual_funds(100)
            return jsonify({'segment':'funds','count':len(rows),'source':'Upstox mutual-fund instrument master','items':rows})
        if segment == 'bonds':
            rows = market_universe.compare_bonds()
            return jsonify({'segment':'bonds','count':len(rows),'source':'Upstox exchange quotes for listed bond/debt ETFs','items':rows})
        if segment == 'fd':
            rows = market_universe.compare_fds()
            return jsonify({'segment':'fd','count':len(rows),'source':'Official bank rate pages; general public, ~1-year tenor','items':rows})
        return jsonify({'error':'Unknown comparison segment'}), 404
    except Exception as exc:
        return jsonify({'segment':segment,'count':0,'items':[],'error':str(exc)}), 500

@app.get('/api/tracking')
def tracking():
    """Expose the backend tracking universe and live quote coverage."""
    try:
        from market_universe import tracking_universe, compare_stocks, compare_fno, compare_bonds, compare_fds, compare_mutual_funds

        catalog = tracking_universe()
        fund_names = [x.get('name') for x in compare_mutual_funds(100) if x.get('name')]
        fd_rows = compare_fds()
        try:
            import upstox_adapter as _market_adapter
            health = _market_adapter.healthcheck()
            live = bool(health.get('configured') and health.get('reachable'))
        except Exception:
            health = {'configured': False, 'reachable': False}
            live = False

        if live:
            try:
                rows = compare_stocks()
                if rows: catalog['stocks'] = [x.get('symbol') or x.get('name') for x in rows]
            except Exception:
                pass
            try:
                rows = compare_fno()
                if rows: catalog['fno'] = [x.get('symbol') or x.get('name') for x in rows]
            except Exception:
                pass
            try:
                rows = compare_bonds()
                if rows: catalog['bonds'] = [x.get('symbol') or x.get('name') for x in rows]
            except Exception:
                pass
            try:
                rows = compare_mutual_funds(100)
                if rows: catalog['mutual-funds'] = [x.get('name') for x in rows if x.get('name')]
            except Exception:
                pass

        live_segments = {
            'commodities': ('commodities', 50, 'Upstox MCX commodity market quotes'),
            'currency': ('currency', 50, 'Upstox currency market quotes'),
            'gold': ('gold', 5, 'Upstox MCX gold contracts'),
        }
        configs = {
            'stocks': ('stocks', 30, 'Upstox tracked equity quotes'),
            'fno': ('fno', 30, 'Upstox tracked derivatives quotes'),
            'mutual-funds': ('mutual-funds', 30, 'Upstox mutual-fund scheme master'),
            'bonds': ('bonds', 20, 'Upstox listed bond/debt quotes'),
            'fd': ('fd', len(fd_rows), 'Bank FD rate registry'),
            **live_segments,
        }

        categories = {}
        for slug, (key, target, source) in configs.items():
            if slug == 'mutual-funds':
                names = fund_names
            elif slug == 'fd':
                names = [f"{x.get('bank')} • {x.get('tenor')}" for x in fd_rows]
            else:
                try:
                    from market_universe import history_universe
                    hu = history_universe()
                    if slug in hu:
                        universe_rows = hu.get(slug) or []
                        names = [
                            x.get('trading_symbol') or x.get('symbol') or x.get('name')
                            for x in universe_rows
                            if isinstance(x, dict) and (x.get('trading_symbol') or x.get('symbol') or x.get('name'))
                        ]
                    else:
                        names = [str(x) for x in (catalog.get(key) or []) if x]
                except Exception:
                    names = [str(x) for x in (catalog.get(key) or []) if x]
            categories[slug] = {
                'tracked': min(len(names), target) if target else len(names),
                'target': target,
                'source': source,
                'names': names[:target] if target else names,
                'mode': 'live' if live and slug in {'stocks','fno','bonds','mutual-funds'} else 'configured',
            }

        category=request.args.get('category','').strip()
        if category:
            item=categories.get(category)
            if not item:
                return jsonify({'status':'error','message':'Unknown tracking category'}), 404
            return jsonify({
                'status': 'live' if live else 'configured',
                'live_quotes_configured': live,
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'category': item,
            })

        return jsonify({
            'status': 'live' if live else 'configured',
            'live_quotes_configured': live,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'message': 'Live market data is active.' if live else 'Configured tracking universe is available; live market data is not currently connected.',
            'categories': categories,
        })
    except Exception as exc:
        return jsonify({'status':'error','message':str(exc),'categories':{}}), 500

@app.get('/api/asset/<slug>')
def asset(slug: str):
    for item in ASSET_INFO:
        if item['slug'] == slug:
            return jsonify(item)
    return jsonify({'error': 'Asset category not found.'}), 404


@app.get('/api/health')
def health():
    """Cached liveness/readiness check for the configured Upstox connection."""

    try:
        upstox = _engine().healthcheck()
    except Exception as exc:
        upstox = {'configured': False, 'reachable': False, 'error': str(exc)}
    return jsonify({
        'status': 'ok',
        'time': datetime.now(timezone.utc).isoformat(),
        'runtime': 'vercel-flask',
        'market_engine': 'upstox-primary',
        'upstox': upstox,
    })


if __name__ == '__main__':
    app.run(host=os.getenv('HOST', '0.0.0.0'), port=int(os.getenv('PORT', '5000')), debug=False, use_reloader=False)
