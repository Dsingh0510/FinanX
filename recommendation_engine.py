from __future__ import annotations

from typing import Dict
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


def _clip(v, lo, hi):
    return max(lo, min(hi, float(v)))


def _blend_long_term(metrics: Dict) -> tuple[float | None, str]:
    """Blend 1Y/3Y/5Y CAGR. Longer histories are given more weight."""
    pairs = []
    for key, weight in (('return_1y', 0.20), ('return_3y', 0.35), ('return_5y', 0.45)):
        value = metrics.get(key)
        if value is not None and math.isfinite(float(value)):
            pairs.append((float(value), weight))
    if not pairs:
        return None, 'model assumption'
    total_w = sum(w for _, w in pairs)
    raw = sum(v*w for v,w in pairs) / total_w
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
    if category == 'fno':
        rate = _clip(rate, -10, 8)
    else:
        rate = _clip(rate, -10, 22)
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


def _portfolio_projection(amount: float, allocations: Dict[str, float], market_segments: Dict, years: int):
    weighted_rate = 0.0
    weighted_vol = 0.0
    per_asset = []
    for slug, pct in allocations.items():
        metrics = market_segments.get(slug, {}).get('metrics') or {}
        p = _planning_rate(slug, metrics)
        weighted_rate += pct * p['annual_return_estimate']
        weighted_vol += pct * p['volatility_estimate']
        invested = amount * pct
        v3_rate = p['three_year_return'] if p['three_year_return'] is not None else p['annual_return_estimate']
        v5_rate = p['five_year_return'] if p['five_year_return'] is not None else p['annual_return_estimate']
        per_asset.append({
            'slug': slug,
            'invested': round(invested, 2),
            'percent': round(pct * 100, 1),
            'annual_return_estimate': p['annual_return_estimate'],
            'yoy_return': p['yoy_return'],
            'three_year_return': v3_rate,
            'five_year_return': v5_rate,
            'projected_3y_value': round(_project(invested, v3_rate, 3), 2),
            'projected_5y_value': round(_project(invested, v5_rate, 5), 2),
            'projected_value': round(_project(invested, p['annual_return_estimate'], years), 2),
            'projected_gain': round(_project(invested, p['annual_return_estimate'], years) - invested, 2),
            'basis': p['basis'],
        })
    annual = weighted_rate
    return {
        'annual_return_estimate': round(annual, 2),
        'portfolio_volatility_estimate': round(weighted_vol, 2),
        'projected_value': round(_project(amount, annual, years), 2),
        'projected_gain': round(_project(amount, annual, years) - amount, 2),
        'projected_3y_value': round(_project(amount, annual, 3), 2),
        'projected_5y_value': round(_project(amount, annual, 5), 2),
        'per_asset': per_asset,
    }


def _renorm(w):
    w = {k: max(float(v), 0.0) for k,v in w.items() if v > 0}
    total = sum(w.values())
    return {k:v/total for k,v in w.items()} if total else {}


def _fit_to_risk_caps(weights, risk):
    caps = {
        'low': {'fd':0.60,'bonds':0.40,'mutual-funds':0.25,'gold':0.18,'stocks':0.10,'commodities':0.00,'currency':0.00,'fno':0.00},
        'moderate': {'fd':0.50,'bonds':0.35,'mutual-funds':0.40,'gold':0.20,'stocks':0.25,'commodities':0.08,'currency':0.03,'fno':0.00},
        'high': {'fd':0.35,'bonds':0.30,'mutual-funds':0.45,'gold':0.22,'stocks':0.45,'commodities':0.15,'currency':0.07,'fno':0.03},
    }[risk]
    w={k:max(float(v),0.0) for k,v in weights.items() if float(v)>0}
    for k,cap in caps.items():
        if cap<=0: w.pop(k,None)
    # Cap overweight buckets and redistribute the excess into available buckets.
    changed=True
    while changed:
        changed=False
        for k,cap in caps.items():
            if k in w and w[k] > cap + 1e-9:
                excess=w[k]-cap; w[k]=cap; changed=True
                room=sum(max(caps.get(x,1.0)-w[x],0) for x in w if x!=k)
                if room>0:
                    for x in list(w):
                        if x==k: continue
                        add=excess*max(caps.get(x,1.0)-w[x],0)/room
                        w[x]+=add
    return _renorm(w)


def _shift_mix(base, risk, direction):
    w=dict(base)
    if direction == 'stable':
        source=['mutual-funds','stocks','gold','commodities','currency']
        available=[x for x in source if w.get(x,0)>0]
        total=sum(w.get(x,0) for x in available) or 1
        shift=min(0.08, sum(w.get(x,0) for x in available)*0.18)
        for x in available:
            take=min(w[x], shift*w[x]/total)
            w[x]-=take
        w['fd']=w.get('fd',0)+shift*0.65
        w['bonds']=w.get('bonds',0)+shift*0.35
    elif direction == 'growth':
        source=['fd','bonds']
        total=sum(w.get(x,0) for x in source) or 1
        shift=min(0.08, total*0.18)
        for x in source:
            take=min(w.get(x,0), shift*w.get(x,0)/total)
            w[x]-=take
        w['mutual-funds']=w.get('mutual-funds',0)+shift*0.65
        w['stocks']=w.get('stocks',0)+shift*0.35
    return _fit_to_risk_caps(w,risk)


def _candidate_plans(scores, risk, horizon):
    base = build_dynamic_weights(scores, risk)
    if not base:
        base = {'fd':0.35,'bonds':0.20,'mutual-funds':0.25,'gold':0.10,'stocks':0.10}
    base = _fit_to_risk_caps(base, risk)
    return [
        ('Option 1', _shift_mix(base, risk, 'stable'), 'Different distribution within the same risk level, with a little more stability.'),
        ('Option 2', _fit_to_risk_caps(base, risk), 'Different distribution within the same risk level, keeping a balanced mix.'),
        ('Option 3', _shift_mix(base, risk, 'growth'), 'Different distribution within the same risk level, with a little more growth exposure.'),
    ]


def build_market_adjusted_plan(amount, horizon, risk, liquidity, goal, emergency, market_analysis):
    user={'risk':risk,'horizon':horizon,'liquidity':liquidity,'goal':goal,'emergency':emergency}
    scores=score_categories(user, market_analysis)
    candidates=_candidate_plans(scores,risk,horizon)

    evaluated=[]
    risk_penalty={'low':1.20,'moderate':0.85,'high':0.45}.get(risk,0.85)
    for idx,(name,weights,description) in enumerate(candidates):
        projection=_portfolio_projection(amount,weights,market_analysis,horizon)
        utility=projection['annual_return_estimate'] - risk_penalty*(projection['portfolio_volatility_estimate']/10)
        if emergency=='no': utility -= 2.0
        evaluated.append({'name':name,'weights':weights,'description':description,'projection':projection,'utility':round(utility,2)})
    evaluated.sort(key=lambda x:x['utility'], reverse=True)
    selected=evaluated[0]

    allocations=[]
    for slug,pct in sorted(selected['weights'].items(), key=lambda kv:kv[1], reverse=True):
        info=next((x for x in ASSET_INFO if x['slug']==slug),None)
        if not info or pct<0.005: continue
        detail=next(x for x in selected['projection']['per_asset'] if x['slug']==slug)
        allocations.append({
            'slug':slug,'asset':info['name'],'percent':detail['percent'],'amount':detail['invested'],
            'annual_return_estimate':detail['annual_return_estimate'],
            'yoy_return':detail['yoy_return'],'three_year_return':detail['three_year_return'],'five_year_return':detail['five_year_return'],
            'projected_3y_value':detail['projected_3y_value'],'projected_5y_value':detail['projected_5y_value'],
            'projected_value':detail['projected_value'],'projected_gain':detail['projected_gain'],'basis':detail['basis'],
        })

    ranked=[]
    for row in scores:
        info=next((x for x in ASSET_INFO if x['slug']==row['category']),None)
        p=_planning_rate(row['category'], row.get('metrics') or {})
        sample_size = int((row.get('metrics') or {}).get('sample_size') or 0)
        if row['category'] == 'mutual-funds' and sample_size:
            basis = f'AMFI average • {sample_size} funds'
        elif row['category'] == 'bonds' and sample_size:
            cy=(row.get('metrics') or {}).get('current_yield')
            basis = f'Bond fund proxy • {sample_size} funds' + (f' • 10Y yield {float(cy):.2f}%' if cy is not None else '')
        elif row['category'] == 'fd' and sample_size:
            basis = f'Rate average • {sample_size} entries'
        elif sample_size > 1:
            basis = f'Average • {sample_size} tracked'
        else:
            cy=(row.get('metrics') or {}).get('current_yield') if row['category']=='bonds' else None
            basis = (f'10Y G-Sec yield {float(cy):.2f}%' if cy is not None else p['basis'])
        ranked.append({
            'slug':row['category'],'asset':info['name'] if info else row['category'],
            'metrics':row['metrics'],'data_status':row['data_status'],
            'current_yield':(row.get('metrics') or {}).get('current_yield') if row['category']=='bonds' else None,
            'yield_label':(row.get('metrics') or {}).get('yield_label') if row['category']=='bonds' else None,
            'annual_return_estimate':p['annual_return_estimate'],'basis':basis,
            'yoy_return':p['yoy_return'],
            'three_year_return':p['three_year_return'] if p['three_year_return'] is not None else p['annual_return_estimate'],
            'five_year_return':p['five_year_return'] if p['five_year_return'] is not None else p['annual_return_estimate'],
            'tracked_options': market_analysis.get(row['category'],{}).get('analyzed_options',[]),
        })

    scenario_comparison=[]
    for e in evaluated:
        scenario_comparison.append({
            'name':e['name'],'description':e['description'],
            'annual_return_estimate':e['projection']['annual_return_estimate'],
            'projected_3y_value':e['projection']['projected_3y_value'],
            'projected_5y_value':e['projection']['projected_5y_value'],
            'projected_value':e['projection']['projected_value'],
            'projected_gain':e['projection']['projected_gain'],
            'risk_estimate':e['projection']['portfolio_volatility_estimate'],
            'is_selected':e['name']==selected['name'],
            'allocation_breakup':[{'slug':slug,'percent':round(pct*100,1),'amount':round(amount*pct,2)} for slug,pct in sorted(e['weights'].items(), key=lambda kv:kv[1], reverse=True) if pct >= 0.01],
        })

    return {
        'amount':round(amount,2),'horizon':horizon,'risk':risk.title(),'liquidity':liquidity.title(),
        'goal':goal.replace('_',' ').title(),'emergency_buffer':emergency=='yes',
        'allocations':allocations,
        'ranked_categories':ranked,
        'chosen_strategy':selected['name'],
        'chosen_description':selected['description'],
        'scenario_comparison':scenario_comparison,
        'projected_value':selected['projection']['projected_value'],
        'projected_gain':selected['projection']['projected_gain'],
        'projected_3y_value':selected['projection']['projected_3y_value'],
        'projected_5y_value':selected['projection']['projected_5y_value'],
        'annual_return_estimate':selected['projection']['annual_return_estimate'],
        'portfolio_volatility_estimate':selected['projection']['portfolio_volatility_estimate'],
        'explanation':(
            'FinanX uses your risk comfort, investment horizon, liquidity and goal first. It then compares 1-year, 3-year and 5-year historical performance where available, blends those signals into a planning estimate, and tests three different allocation distributions within the same user-selected risk level. The displayed values are scenarios, not guaranteed returns.'
        ),
        'notes':[
            'Projected values are illustrative scenarios, not guaranteed returns. Actual returns can be materially higher or lower.',
            'The 3-year and 5-year figures are annualized historical returns where available; missing segments use clearly labelled planning assumptions.',
            'F&O remains tightly limited because derivatives can magnify losses and are not treated like a normal core diversification bucket.',
        ],
    }
