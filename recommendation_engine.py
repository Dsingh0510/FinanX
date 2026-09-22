from __future__ import annotations

from typing import Dict, List
import math

from allocation_engine import ASSET_INFO
from analysis_engine import build_dynamic_weights, score_categories

BASELINE_RETURN = {
    'fd': 6.5,
    'bonds': 7.0,
    'mutual-funds': 10.0,
    'gold': 7.0,
    'stocks': 10.0,
    'commodities': 6.0,
    'currency': 3.0,
    'fno': 0.0,
}
BASELINE_VOL = {
    'fd': 1.0,
    'bonds': 5.0,
    'mutual-funds': 14.0,
    'gold': 16.0,
    'stocks': 20.0,
    'commodities': 25.0,
    'currency': 12.0,
    'fno': 45.0,
}

# How many tracked entities FinanX exposes as the final candidates inside
# each category. The full tracked universe is screened first; only the
# strongest risk-adjusted candidates receive portfolio money.
ENTITY_DISPLAY_LIMIT = {
    'fd': 3,
    'bonds': 3,
    'mutual-funds': 5,
    'stocks': 5,
    'fno': 3,
    'gold': 1,
    'commodities': 3,
    'currency': 3,
}


def _clip(v, lo, hi):
    return max(lo, min(hi, float(v)))


def _blend_long_term(metrics: Dict) -> tuple[float | None, str]:
    """Blend 1Y/3Y/5Y CAGR with more weight on longer histories."""
    pairs = []
    for key, weight in (('return_1y', 0.20), ('return_3y', 0.35), ('return_5y', 0.45)):
        value = metrics.get(key)
        if value is not None and math.isfinite(float(value)):
            pairs.append((float(value), weight))
    if not pairs:
        return None, 'model assumption'
    total_w = sum(w for _, w in pairs)
    raw = sum(v * w for v, w in pairs) / total_w
    return raw, 'historical blend'


def _planning_rate(category: str, metrics: Dict) -> Dict:
    hist, basis = _blend_long_term(metrics or {})
    baseline = BASELINE_RETURN[category]
    if hist is None:
        rate = baseline
        basis = 'model assumption'
    else:
        # Pull extreme historical values toward a conservative planning baseline.
        rate = 0.70 * _clip(hist, -25, 35) + 0.30 * baseline
    rate = _clip(rate, -10, 8) if category == 'fno' else _clip(rate, -10, 22)

    vol = metrics.get('volatility_annualized')
    if vol is None or not math.isfinite(float(vol)):
        vol = BASELINE_VOL[category]
    else:
        vol = _clip(vol, 0.5, 60)

    return {
        'annual_return_estimate': round(rate, 2),
        'volatility_estimate': round(vol, 2),
        'basis': basis,
        'yoy_return': metrics.get('return_1y'),
        'three_year_return': metrics.get('return_3y'),
        'five_year_return': metrics.get('return_5y'),
    }


def _project(amount: float, rate_pct: float, years: int) -> float:
    rate = max(rate_pct, -99.0) / 100.0
    return amount * ((1.0 + rate) ** years)


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _entity_metrics(category: str, row: Dict, category_metrics: Dict) -> Dict:
    """Convert a tracked entity row into comparable planning metrics.

    Real historical returns are used when the backend provides them. For
    entities that currently only expose quotes/rates, FinanX keeps the
    category-level planning return but derives a risk/liquidity score from
    the live fields available for that entity.
    """
    metrics = {
        'return_1y': _number(row.get('return_1y', row.get('yoy'))),
        'return_3y': _number(row.get('return_3y', row.get('three_year_return'))),
        'return_5y': _number(row.get('return_5y', row.get('five_year_return'))),
        'volatility_annualized': _number(row.get('volatility_annualized')),
        'max_drawdown': _number(row.get('max_drawdown')),
    }

    # FD: the quoted deposit rate is the planning return.
    if category == 'fd':
        rate = _number(row.get('rate'))
        if rate is not None:
            metrics.update({'return_1y': rate, 'return_3y': rate, 'return_5y': rate, 'volatility_annualized': 1.0})
        return metrics

    # For live listed instruments without full history, use the 52-week range
    # and current-day move as a conservative stability proxy. This is not a
    # historical volatility calculation.
    price = _number(row.get('price'))
    year_high = _number(row.get('year_high'))
    year_low = _number(row.get('year_low'))
    today_change = abs(_number(row.get('today_change')) or 0.0)

    if metrics['volatility_annualized'] is None:
        if price and year_high and year_low and year_high > year_low > 0:
            range_pct = (year_high - year_low) / year_high * 100
            distance_from_high = max(0.0, (year_high - price) / year_high * 100)
            proxy = BASELINE_VOL.get(category, 18.0) + 0.20 * range_pct + 0.35 * today_change + 0.15 * distance_from_high
            metrics['volatility_annualized'] = round(_clip(proxy, 1.0, 60.0), 2)
        else:
            metrics['volatility_annualized'] = BASELINE_VOL.get(category, 18.0)

    # Funds have long-term return data but usually no weekly volatility in the
    # current AMFI snapshot. Penalize large differences between return horizons
    # as a consistency proxy rather than pretending it is true volatility.
    if category == 'mutual-funds' and metrics['volatility_annualized'] == BASELINE_VOL['mutual-funds']:
        vals = [x for x in (metrics['return_1y'], metrics['return_3y'], metrics['return_5y']) if x is not None]
        if len(vals) >= 2:
            spread = max(vals) - min(vals)
            metrics['volatility_annualized'] = round(_clip(8.0 + spread * 0.75, 4.0, 30.0), 2)

    return metrics


def _entity_score(category: str, row: Dict, user_risk: str, horizon: int, goal: str, category_metrics: Dict) -> Dict:
    metrics = _entity_metrics(category, row, category_metrics)
    p = _planning_rate(category, metrics)

    # Start from the entity's own return data when available, otherwise use
    # the category planning rate as the return prior.
    entity_return = p['annual_return_estimate']
    if not any(metrics.get(k) is not None for k in ('return_1y', 'return_3y', 'return_5y')):
        cat_plan = _planning_rate(category, category_metrics or {})
        entity_return = cat_plan['annual_return_estimate']

    vol = p['volatility_estimate']

    risk_penalty = {'low': 1.45, 'moderate': 0.90, 'high': 0.55}.get(user_risk, 0.90)
    horizon_bonus = 0.0
    if horizon >= 7 and category in {'mutual-funds', 'stocks'}:
        horizon_bonus = 0.45
    elif horizon <= 2 and category in {'fd', 'bonds'}:
        horizon_bonus = 0.75

    goal_bonus = 0.0
    if goal == 'capital_preservation' and category in {'fd', 'bonds'}:
        goal_bonus = 2.0
    elif goal == 'wealth' and category in {'stocks', 'mutual-funds'}:
        goal_bonus = 1.0

    # Liquidity proxy: listed/high-volume rows get a small preference when the
    # user's liquidity requirement is high.
    liquidity_bonus = 0.0
    if user_risk in {'low', 'moderate'} and category in {'stocks', 'mutual-funds', 'fd'}:
        liquidity_bonus = 0.25

    score = entity_return - risk_penalty * (vol / 10.0) + horizon_bonus + goal_bonus + liquidity_bonus

    # Current-day gains should not drive a recommendation, so momentum is
    # deliberately capped and only used as a small tie-breaker.
    momentum = _number(row.get('today_change'))
    if momentum is not None:
        score += _clip(momentum, -3.0, 3.0) * 0.08

    # Penalize listed instruments sitting materially below their 52-week high,
    # which is a stability proxy rather than a prediction.
    drawdown_proxy = 0.0
    price = _number(row.get('price'))
    year_high = _number(row.get('year_high'))
    if price and year_high and year_high > 0:
        drawdown_proxy = max(0.0, (year_high - price) / year_high * 100)
        score -= min(drawdown_proxy * 0.04, 2.5)

    return {
        'score': round(score, 4),
        'metrics': metrics,
        'annual_return_estimate': round(entity_return, 2),
        'volatility_estimate': round(vol, 2),
        'basis': p['basis'] if any(metrics.get(k) is not None for k in ('return_1y', 'return_3y', 'return_5y')) else 'category planning rate + live risk proxy',
    }


def _rank_entity_options(category: str, market_row: Dict, risk: str, horizon: int, goal: str) -> List[Dict]:
    rows = market_row.get('analyzed_options') or []
    category_metrics = market_row.get('metrics') or {}
    ranked = []

    for row in rows:
        if not isinstance(row, dict):
            row = {'name': str(row)}
        # Skip non-investment placeholder rows.
        if row.get('name') and 'tracked in Market Now' in str(row.get('name')):
            continue
        scored = _entity_score(category, row, risk, horizon, goal, category_metrics)
        name = row.get('name') or row.get('bank') or row.get('symbol') or row.get('scheme_name') or category
        ranked.append({
            'name': str(name),
            'symbol': row.get('symbol') or row.get('scheme_code') or row.get('instrument_key'),
            'category': category,
            'score': scored['score'],
            'annual_return_estimate': scored['annual_return_estimate'],
            'volatility_estimate': scored['volatility_estimate'],
            'basis': scored['basis'],
            'yoy_return': scored['metrics'].get('return_1y'),
            'three_year_return': scored['metrics'].get('return_3y'),
            'five_year_return': scored['metrics'].get('return_5y'),
            'latest_value': row.get('latest_nav', row.get('price', row.get('rate'))),
            'raw': row,
        })

    ranked.sort(key=lambda x: (x['score'], x['annual_return_estimate']), reverse=True)
    return ranked


def _select_entity_allocations(amount: float, category_weights: Dict[str, float], market_segments: Dict, risk: str, horizon: int, goal: str) -> Dict[str, List[Dict]]:
    """Split each category amount across the strongest tracked entities."""
    selected = {}

    for category, category_pct in category_weights.items():
        if category_pct <= 0:
            continue
        ranked = _rank_entity_options(category, market_segments.get(category, {}), risk, horizon, goal)

        # Keep the whole tracked universe in the screening step, but place
        # portfolio money only into the top candidates.
        limit = ENTITY_DISPLAY_LIMIT.get(category, 3)
        chosen = ranked[:limit]

        if not chosen:
            # A category without entity records keeps its category-level model.
            selected[category] = [{
                'name': next((x['name'] for x in ASSET_INFO if x['slug'] == category), category),
                'category': category,
                'score': 0.0,
                'weight_within_category': 1.0,
                'annual_return_estimate': _planning_rate(category, market_segments.get(category, {}).get('metrics') or {})['annual_return_estimate'],
                'volatility_estimate': _planning_rate(category, market_segments.get(category, {}).get('metrics') or {})['volatility_estimate'],
                'basis': 'category planning rate',
            }]
            continue

        positive = [max(x['score'] + 10.0, 0.1) for x in chosen]
        total = sum(positive)
        cat_amount = amount * category_pct

        bucket = []
        for x, raw_score in zip(chosen, positive):
            within = raw_score / total
            item = dict(x)
            item['weight_within_category'] = round(within, 6)
            item['invested'] = round(cat_amount * within, 2)
            bucket.append(item)
        selected[category] = bucket

    return selected


def _portfolio_projection(amount: float, allocations: Dict[str, float], market_segments: Dict, years: int, risk: str = 'moderate', horizon: int | None = None, goal: str = 'balanced_growth'):
    """Project using entity-level returns, not category averages alone."""
    horizon = years if horizon is None else horizon
    entity_allocations = _select_entity_allocations(amount, allocations, market_segments, risk, horizon, goal)

    total_return_numerator = 0.0
    total_vol_numerator = 0.0
    projected_total = 0.0
    projected_gain = 0.0
    per_asset = []
    entity_rows = []

    for slug, pct in allocations.items():
        category_bucket = entity_allocations.get(slug) or []
        if not category_bucket:
            continue

        category_invested = amount * pct
        cat_projected = 0.0
        cat_return = 0.0
        cat_vol = 0.0

        for entity in category_bucket:
            invested = entity['invested']
            rate = entity['annual_return_estimate']
            vol = entity['volatility_estimate']
            v3_rate = entity.get('three_year_return')
            v5_rate = entity.get('five_year_return')
            if v3_rate is None:
                v3_rate = rate
            if v5_rate is None:
                v5_rate = rate

            projected = _project(invested, rate, years)
            gain = projected - invested
            projected_3 = _project(invested, v3_rate, 3)
            projected_5 = _project(invested, v5_rate, 5)

            cat_projected += projected
            cat_return += (invested / amount) * rate
            cat_vol += (invested / amount) * vol
            projected_gain += gain
            entity_rows.append({
                'category': slug,
                'name': entity['name'],
                'symbol': entity.get('symbol'),
                'invested': round(invested, 2),
                'percent': round(invested / amount * 100, 2),
                'annual_return_estimate': round(rate, 2),
                'volatility_estimate': round(vol, 2),
                'yoy_return': entity.get('yoy_return'),
                'three_year_return': entity.get('three_year_return'),
                'five_year_return': entity.get('five_year_return'),
                'projected_value': round(projected, 2),
                'projected_gain': round(gain, 2),
                'projected_3y_value': round(projected_3, 2),
                'projected_5y_value': round(projected_5, 2),
                'basis': entity.get('basis'),
            })

        total_return_numerator += cat_return
        total_vol_numerator += cat_vol
        projected_total += cat_projected

        cat_info = next((x for x in ASSET_INFO if x['slug'] == slug), None)
        selected_names = [x['name'] for x in category_bucket]
        category_rate = sum(x['invested'] * x['annual_return_estimate'] for x in category_bucket) / category_invested
        category_vol = sum(x['invested'] * x['volatility_estimate'] for x in category_bucket) / category_invested
        v3_cat = sum(x['invested'] * (x.get('three_year_return') if x.get('three_year_return') is not None else x['annual_return_estimate']) for x in category_bucket) / category_invested
        v5_cat = sum(x['invested'] * (x.get('five_year_return') if x.get('five_year_return') is not None else x['annual_return_estimate']) for x in category_bucket) / category_invested
        cat_projected_3 = _project(category_invested, v3_cat, 3)
        cat_projected_5 = _project(category_invested, v5_cat, 5)

        per_asset.append({
            'slug': slug,
            'asset': cat_info['name'] if cat_info else slug,
            'invested': round(category_invested, 2),
            'percent': round(pct * 100, 1),
            'annual_return_estimate': round(category_rate, 2),
            'yoy_return': round(sum(x['invested'] * (x.get('yoy_return') or x['annual_return_estimate']) for x in category_bucket) / category_invested, 2),
            'three_year_return': round(v3_cat, 2),
            'five_year_return': round(v5_cat, 2),
            'projected_3y_value': round(cat_projected_3, 2),
            'projected_5y_value': round(cat_projected_5, 2),
            'projected_value': round(cat_projected, 2),
            'projected_gain': round(cat_projected - category_invested, 2),
            'basis': (
                f"Screened across {len(_rank_entity_options(slug, market_segments.get(slug, {}), risk, horizon, goal))} tracked options"
                + (f" • {len(selected_names)} selected" if selected_names else "")
            ),
            'selected_options': selected_names,
        })

    annual = total_return_numerator
    return {
        'annual_return_estimate': round(annual, 2),
        'portfolio_volatility_estimate': round(total_vol_numerator, 2),
        'projected_value': round(projected_total, 2),
        'projected_gain': round(projected_gain, 2),
        'projected_3y_value': round(sum(
            _project(x['invested'], x['three_year_return'] or x['annual_return_estimate'], 3)
            for x in per_asset
        ), 2),
        'projected_5y_value': round(sum(
            _project(x['invested'], x['five_year_return'] or x['annual_return_estimate'], 5)
            for x in per_asset
        ), 2),
        'per_asset': per_asset,
        'entity_rows': entity_rows,
    }


def _renorm(w):
    w = {k: max(float(v), 0.0) for k, v in w.items() if float(v) > 0}
    total = sum(w.values())
    return {k: v / total for k, v in w.items()} if total else {}


def _fit_to_risk_caps(weights, risk):
    caps = {
        'low': {'fd': 0.60, 'bonds': 0.40, 'mutual-funds': 0.25, 'gold': 0.18, 'stocks': 0.10, 'commodities': 0.00, 'currency': 0.00, 'fno': 0.00},
        'moderate': {'fd': 0.50, 'bonds': 0.35, 'mutual-funds': 0.40, 'gold': 0.20, 'stocks': 0.25, 'commodities': 0.08, 'currency': 0.03, 'fno': 0.00},
        'high': {'fd': 0.35, 'bonds': 0.30, 'mutual-funds': 0.45, 'gold': 0.22, 'stocks': 0.45, 'commodities': 0.15, 'currency': 0.07, 'fno': 0.03},
    }[risk]
    w = {k: max(float(v), 0.0) for k, v in weights.items() if float(v) > 0}
    for k, cap in caps.items():
        if cap <= 0:
            w.pop(k, None)
    changed = True
    while changed:
        changed = False
        for k, cap in caps.items():
            if k in w and w[k] > cap + 1e-9:
                excess = w[k] - cap
                w[k] = cap
                room = sum(max(caps.get(x, 1.0) - w[x], 0) for x in w if x != k)
                if room > 0:
                    for x in list(w):
                        if x == k:
                            continue
                        add = excess * max(caps.get(x, 1.0) - w[x], 0) / room
                        w[x] += add
    return _renorm(w)


def _shift_mix(base, risk, direction):
    w = dict(base)
    if direction == 'stable':
        source = ['mutual-funds', 'stocks', 'gold', 'commodities', 'currency']
        available = [x for x in source if w.get(x, 0) > 0]
        total = sum(w.get(x, 0) for x in available) or 1
        shift = min(0.08, sum(w.get(x, 0) for x in available) * 0.18)
        for x in available:
            take = min(w[x], shift * w[x] / total)
            w[x] -= take
        w['fd'] = w.get('fd', 0) + shift * 0.65
        w['bonds'] = w.get('bonds', 0) + shift * 0.35
    elif direction == 'growth':
        source = ['fd', 'bonds']
        total = sum(w.get(x, 0) for x in source) or 1
        shift = min(0.08, total * 0.18)
        for x in source:
            take = min(w.get(x, 0), shift * w.get(x, 0) / total)
            w[x] -= take
        w['mutual-funds'] = w.get('mutual-funds', 0) + shift * 0.65
        w['stocks'] = w.get('stocks', 0) + shift * 0.35
    return _fit_to_risk_caps(w, risk)


def _candidate_plans(scores, risk, horizon):
    base = build_dynamic_weights(scores, risk)
    if not base:
        base = {'fd': 0.35, 'bonds': 0.20, 'mutual-funds': 0.25, 'gold': 0.10, 'stocks': 0.10}
    base = _fit_to_risk_caps(base, risk)
    return [
        ('Option 1', _shift_mix(base, risk, 'stable'), 'More weight to lower-volatility categories while staying within your selected risk level.'),
        ('Option 2', _fit_to_risk_caps(base, risk), 'Data-driven balanced allocation using the tracked universe.'),
        ('Option 3', _shift_mix(base, risk, 'growth'), 'More weight to growth categories while staying within your selected risk level.'),
    ]


def build_market_adjusted_plan(amount, horizon, risk, liquidity, goal, emergency, market_analysis):
    user = {'risk': risk, 'horizon': horizon, 'liquidity': liquidity, 'goal': goal, 'emergency': emergency}
    scores = score_categories(user, market_analysis)
    candidates = _candidate_plans(scores, risk, horizon)

    evaluated = []
    risk_penalty = {'low': 1.20, 'moderate': 0.85, 'high': 0.45}.get(risk, 0.85)
    for name, weights, description in candidates:
        projection = _portfolio_projection(amount, weights, market_analysis, horizon, risk=risk, horizon=horizon, goal=goal)
        utility = projection['annual_return_estimate'] - risk_penalty * (projection['portfolio_volatility_estimate'] / 10)
        if emergency == 'no':
            utility -= 2.0
        evaluated.append({
            'name': name,
            'weights': weights,
            'description': description,
            'projection': projection,
            'utility': round(utility, 2),
        })

    evaluated.sort(key=lambda x: x['utility'], reverse=True)
    selected = evaluated[0]

    allocations = []
    for detail in selected['projection']['per_asset']:
        allocations.append({
            'slug': detail['slug'],
            'asset': detail['asset'],
            'percent': detail['percent'],
            'amount': detail['invested'],
            'annual_return_estimate': detail['annual_return_estimate'],
            'yoy_return': detail['yoy_return'],
            'three_year_return': detail['three_year_return'],
            'five_year_return': detail['five_year_return'],
            'projected_3y_value': detail['projected_3y_value'],
            'projected_5y_value': detail['projected_5y_value'],
            'projected_value': detail['projected_value'],
            'projected_gain': detail['projected_gain'],
            'basis': detail['basis'],
            'selected_options': detail.get('selected_options', []),
        })

    ranked = []
    for row in scores:
        info = next((x for x in ASSET_INFO if x['slug'] == row['category']), None)
        p = _planning_rate(row['category'], row.get('metrics') or {})
        sample_size = int((row.get('metrics') or {}).get('sample_size') or 0)

        if row['category'] == 'mutual-funds' and sample_size:
            basis = f'AMFI history • {sample_size} funds'
        elif row['category'] == 'bonds' and sample_size:
            cy = (row.get('metrics') or {}).get('current_yield')
            basis = f'Bond data • {sample_size} tracked' + (f' • 10Y yield {float(cy):.2f}%' if cy is not None else '')
        elif row['category'] == 'fd' and sample_size:
            basis = f'Official rate table • {sample_size} entries'
        elif row.get('data_status') == 'fallback':
            basis = 'Fallback market history'
        elif (row.get('metrics') or {}).get('available'):
            basis = 'Upstox market history'
        else:
            basis = 'History unavailable • planning rate used for projection'

        ranked_options = _rank_entity_options(row['category'], market_analysis.get(row['category'], {}), risk, horizon, goal)
        ranked.append({
            'slug': row['category'],
            'asset': info['name'] if info else row['category'],
            'metrics': row['metrics'],
            'data_status': row['data_status'],
            'current_yield': (row.get('metrics') or {}).get('current_yield') if row['category'] == 'bonds' else None,
            'yield_label': (row.get('metrics') or {}).get('yield_label') if row['category'] == 'bonds' else None,
            'annual_return_estimate': p['annual_return_estimate'],
            'basis': basis,
            'yoy_return': p['yoy_return'],
            'three_year_return': p['three_year_return'],
            'five_year_return': p['five_year_return'],
            'tracked_options': market_analysis.get(row['category'], {}).get('analyzed_options', []),
            'selected_options': [x['name'] for x in ranked_options[:ENTITY_DISPLAY_LIMIT.get(row['category'], 3)]],
        })

    scenario_comparison = []
    for e in evaluated:
        scenario_comparison.append({
            'name': e['name'],
            'description': e['description'],
            'annual_return_estimate': e['projection']['annual_return_estimate'],
            'projected_3y_value': e['projection']['projected_3y_value'],
            'projected_5y_value': e['projection']['projected_5y_value'],
            'projected_value': e['projection']['projected_value'],
            'projected_gain': e['projection']['projected_gain'],
            'risk_estimate': e['projection']['portfolio_volatility_estimate'],
            'is_selected': e['name'] == selected['name'],
            'allocation_breakup': [
                {'slug': slug, 'percent': round(pct * 100, 1), 'amount': round(amount * pct, 2)}
                for slug, pct in sorted(e['weights'].items(), key=lambda kv: kv[1], reverse=True)
                if pct >= 0.01
            ],
        })

    selected_entity_rows = selected['projection'].get('entity_rows', [])
    return {
        'amount': round(amount, 2),
        'horizon': horizon,
        'risk': risk.title(),
        'liquidity': liquidity.title(),
        'goal': goal.replace('_', ' ').title(),
        'emergency_buffer': emergency == 'yes',
        'allocations': allocations,
        'ranked_categories': ranked,
        'selected_entities': selected_entity_rows,
        'chosen_strategy': selected['name'],
        'chosen_description': selected['description'],
        'scenario_comparison': scenario_comparison,
        'projected_value': selected['projection']['projected_value'],
        'projected_gain': selected['projection']['projected_gain'],
        'projected_3y_value': selected['projection']['projected_3y_value'],
        'projected_5y_value': selected['projection']['projected_5y_value'],
        'annual_return_estimate': selected['projection']['annual_return_estimate'],
        'portfolio_volatility_estimate': selected['projection']['portfolio_volatility_estimate'],
        'explanation': (
            'FinanX now screens the full configured tracking universe for each segment, '
            'combines your risk, horizon, liquidity and goal with the backend market data, '
            'then splits each category across the strongest risk-adjusted tracked entities. '
            'Where entity history is available it is used directly; where the backend only '
            'has live quote/rate fields, FinanX uses a clearly labelled planning-rate and '
            'risk proxy instead of pretending it has historical performance. Projected values '
            'are scenarios, not guarantees.'
        ),
        'notes': [
            'The allocation is data-driven across the configured tracked universe, not a fixed percentage-only template.',
            'Historical returns are used at entity level where available; quote-only entities use a category planning rate plus live risk proxies.',
            'Projected values are illustrative scenarios. Actual returns, prices, rates, taxes and liquidity can differ materially.',
            'F&O remains tightly limited because derivatives can magnify losses and are not treated as a normal core diversification bucket.',
        ],
    }
