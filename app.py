# Deploy verification: current Market Now syntax fixed on main
# Production redeploy marker: Market Now syntax verified
# FinanX deployment sync: latest GitHub revision
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


@app.post('/api/analyze/fast')
def analyze_fast():
    """Return a near-instant allocation using cached market metrics when available."""
    try:
        p = request.get_json(force=True)
        amount = _parse_amount(str(p.get('amount', '0')))
        horizon = int(p.get('horizon', 5))
        risk = str(p.get('risk', 'moderate')).lower()
        liquidity = str(p.get('liquidity', 'medium')).lower()
        goal = str(p.get('goal', 'balanced_growth')).lower()
        emergency = str(p.get('emergency', 'yes')).lower()

        import upstox_adapter as _ua
        market = _ua._ANALYSIS if _ua._ANALYSIS is not None else None
        if market is None:
            from allocation_engine import build_portfolio
            quick = build_portfolio(amount, horizon, risk, liquidity, goal, emergency)
            # Project the quick allocation with conservative category planning rates.
            rates = {'fd':6.25,'bonds':7.0,'mutual-funds':10.0,'gold':7.0,'stocks':10.0,'commodities':6.0,'currency':3.0,'fno':0.0}
            projected = 0.0
            rows = []
            for item in quick['allocations']:
                rate = rates.get(item['slug'],6.0)
                value = item['amount'] * ((1 + rate/100) ** horizon)
                projected += value
                rows.append({
                    'slug': item['slug'], 'asset': item['asset'], 'percent': item['percent'],
                    'amount': item['amount'], 'annual_return_estimate': rate,
                    'projected_value': round(value,2), 'projected_gain': round(value-item['amount'],2),
                    'yoy_return': None, 'three_year_return': None, 'five_year_return': None,
                    'basis': 'Quick cached planning model',
                })
            result = dict(quick)
            result['allocations'] = rows
            result['projected_value'] = round(projected,2)
            result['projected_gain'] = round(projected-amount,2)
            result['annual_return_estimate'] = round((projected/amount)**(1/max(horizon,1))*100-100,2) if amount else 0
            result['projected_3y_value'] = None
            result['projected_5y_value'] = None
            result['ranked_categories'] = []
            result['selected_entities'] = []
            result['scenario_comparison'] = []
            result['explanation'] = 'Quick allocation shown from your profile while the cached full market analysis refreshes.'
            result['provisional'] = True
        else:
            result = build_market_adjusted_plan(amount, horizon, risk, liquidity, goal, emergency, market)
            result['provisional'] = True
            result['explanation'] = 'Quick result from the cached market analysis; the full tracked-universe result will replace it.'
        return jsonify({'generated_at': datetime.now(timezone.utc).isoformat(), **result})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'Fast analysis service error: {exc}'}), 500


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
        market = _engine().category_market_analysis(allow_stale=True)
        tracking = market.get('_tracking') or {}
        if _engine().__name__ == 'upstox_adapter' and tracking and not tracking.get('ready', False):
            return jsonify({
                'error': 'FinanX has not completed its configured tracking universe yet.',
                'message': tracking.get('message'),
                'tracking': tracking,
            }), 503
        result = build_market_adjusted_plan(amount, horizon, risk, liquidity, goal, emergency, market)
        return jsonify({'generated_at': datetime.now(timezone.utc).isoformat(), 'market': market, **result})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'Analysis service error: {exc}'}), 500


@app.post('/api/market/warm')
def warm_market():
    try:
        market = _engine().category_market_analysis(force=True)
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
        return jsonify({'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': 'fallback', 'segments': [], 'items': [], 'message': str(exc)})


@app.get('/api/market/highlights')
def market_highlights():
    try:
        return jsonify({'items': vercel_market.market_highlights()})
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

@app.get('/api/tracking')
def tracking():
    """Expose the backend tracking universe and live quote coverage."""
    try:
        import os as _os
        from market_universe import tracking_universe, compare_stocks, compare_fno, compare_bonds, compare_fds, compare_mutual_funds

        catalog = tracking_universe()
        fund_names = [x.get('name') for x in compare_mutual_funds(100) if x.get('name')]
        if not fund_names:
            try:
                from amfi_data import tracking_fund_universe
                fund_names = tracking_fund_universe(100)
            except Exception:
                fund_names = []
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
            'stocks': ('stocks', 30, 'Top tracked equity market quotes'),
            'fno': ('fno', 30, 'Top tracked derivatives market quotes'),
            'mutual-funds': ('mutual-funds', 30, 'Tracked mutual-fund scheme master'),
            'bonds': ('bonds', 20, 'Tracked listed bond/debt market quotes'),
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
    try:
        import upstox_adapter
        upstox = upstox_adapter.healthcheck()
    except Exception as exc:
        upstox = {'configured': False, 'reachable': False, 'error': str(exc)}
    return jsonify({
        'status': 'ok',
        'time': datetime.now(timezone.utc).isoformat(),
        'runtime': 'vercel-flask',
        'market_engine': 'upstox-live-universe-with-public-fallback',
        'upstox': upstox,
    })


if __name__ == '__main__':
    app.run(host=os.getenv('HOST', '0.0.0.0'), port=int(os.getenv('PORT', '5000')), debug=False, use_reloader=False)
