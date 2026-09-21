from __future__ import annotations

from typing import Dict, List, Optional

CATEGORY_RISK = {
    'fd': 1,
    'bonds': 1.5,
    'mutual-funds': 2,
    'gold': 2,
    'stocks': 3,
    'commodities': 3.5,
    'currency': 4,
    'fno': 5,
}

CATEGORY_LIQUIDITY = {
    'fd': 2,
    'bonds': 2.5,
    'mutual-funds': 3,
    'gold': 3.5,
    'stocks': 4,
    'commodities': 4,
    'currency': 4,
    'fno': 4,
}

USER_RISK = {'low': 1, 'moderate': 2, 'high': 3}
LIQUIDITY_TARGET = {'low': 2, 'medium': 3, 'high': 4}


def _clip(value: float, low=0.0, high=100.0):
    return max(low, min(high, value))


def _risk_fit(category, user_risk):
    return _clip(100 - abs(CATEGORY_RISK.get(category, 2) - USER_RISK.get(user_risk, 2)) * 28)


def _horizon_fit(category, horizon):
    if horizon <= 1:
        return {'fd':95,'bonds':92,'mutual-funds':55,'gold':68,'stocks':35,'commodities':22,'currency':20,'fno':5}.get(category,50)
    if horizon <= 3:
        return {'fd':92,'bonds':88,'mutual-funds':68,'gold':76,'stocks':55,'commodities':35,'currency':30,'fno':10}.get(category,50)
    if horizon <= 5:
        return {'fd':84,'bonds':82,'mutual-funds':84,'gold':82,'stocks':78,'commodities':48,'currency':38,'fno':15}.get(category,50)
    return {'fd':72,'bonds':74,'mutual-funds':94,'gold':86,'stocks':92,'commodities':58,'currency':42,'fno':18}.get(category,50)


def _liquidity_fit(category, liquidity):
    target = LIQUIDITY_TARGET.get(liquidity, 3)
    return _clip(100 - abs(CATEGORY_LIQUIDITY.get(category, 3) - target) * 28)


def _goal_fit(category, goal):
    if goal == 'capital_preservation':
        return {'fd':100,'bonds':95,'mutual-funds':68,'gold':72,'stocks':30,'commodities':20,'currency':10,'fno':0}.get(category,50)
    if goal == 'wealth':
        return {'fd':60,'bonds':65,'mutual-funds':95,'gold':82,'stocks':92,'commodities':66,'currency':30,'fno':8}.get(category,50)
    if goal == 'education':
        return {'fd':90,'bonds':86,'mutual-funds':90,'gold':75,'stocks':68,'commodities':30,'currency':12,'fno':0}.get(category,50)
    return {'fd':84,'bonds':82,'mutual-funds':90,'gold':82,'stocks':84,'commodities':52,'currency':25,'fno':0}.get(category,50)


def _historical_score(metrics: Optional[Dict], horizon: int) -> Optional[float]:
    if not metrics or not metrics.get('available'):
        return None
    values=[]
    weights=[]
    if metrics.get('return_1y') is not None:
        values.append(float(metrics['return_1y'])); weights.append(0.45 if horizon <= 2 else 0.20 if horizon <= 4 else 0.15)
    if metrics.get('return_3y') is not None:
        values.append(float(metrics['return_3y'])); weights.append(0.35 if horizon <= 2 else 0.50 if horizon <= 4 else 0.35)
    if metrics.get('return_5y') is not None:
        values.append(float(metrics['return_5y'])); weights.append(0.20 if horizon <= 2 else 0.30 if horizon <= 4 else 0.50)
    if not values:
        return None
    ws=sum(weights[:len(values)]) or 1
    blended=sum(v*w for v,w in zip(values,weights))/ws
    # Reward consistency across long-term horizons; penalize high volatility and drawdown.
    vol=metrics.get('volatility_annualized')
    dd=metrics.get('max_drawdown')
    risk_adjust=0
    if vol is not None:
        risk_adjust -= min(float(vol)/3, 18)
    if dd is not None:
        risk_adjust -= min(abs(float(dd))/4, 18)
    return round(_clip(50 + blended*2 + risk_adjust),1)


def score_categories(user: Dict, market_segments: Dict[str, Dict]) -> List[Dict]:
    risk=str(user.get('risk','moderate')).lower(); horizon=int(user.get('horizon',5)); liquidity=str(user.get('liquidity','medium')).lower(); goal=str(user.get('goal','balanced_growth')).lower()
    rows=[]
    for cat in CATEGORY_RISK:
        base=0.42*_risk_fit(cat,risk)+0.24*_horizon_fit(cat,horizon)+0.14*_liquidity_fit(cat,liquidity)+0.20*_goal_fit(cat,goal)
        metrics=market_segments.get(cat,{}).get('metrics') or {}
        hist=_historical_score(metrics,horizon)
        live_signal=metrics.get('live_breadth_score')
        if hist is None:
            score = base if live_signal is None else 0.90*base + 0.10*float(live_signal)
        elif live_signal is None:
            score = 0.70*base + 0.30*hist
        else:
            score = 0.60*base + 0.30*hist + 0.10*float(live_signal)
        cap=None
        if cat=='fno': cap=0 if risk!='high' else 0.02
        elif cat=='currency' and risk=='low': cap=0
        elif cat=='commodities' and risk=='low': cap=0
        rows.append({'category':cat,'score':round(_clip(score),1),'user_fit':round(base,1),'market_score':hist,'cap_percent':cap,'metrics':metrics,'data_status':market_segments.get(cat,{}).get('status','not_configured')})
    rows.sort(key=lambda x:x['score'],reverse=True)
    return rows


def build_dynamic_weights(scores: List[Dict], risk: str) -> Dict[str,float]:
    allowed=[x for x in scores if x['cap_percent']!=0]
    raw={}
    for row in allowed:
        raw[row['category']]=max(row['score']-45,0.0)**1.25
    total=sum(raw.values())
    if total<=0:return {}
    weights={k:v/total for k,v in raw.items()}
    caps={'fno':0.02 if risk=='high' else 0.0,'currency':0.05 if risk=='high' else 0.02,'commodities':0.10 if risk in {'moderate','high'} else 0.0}
    for cat,cap in caps.items():
        if cat in weights and cap==0:
            del weights[cat]
        elif cat in weights and weights[cat]>cap:
            excess=weights[cat]-cap; weights[cat]=cap
            rest=[k for k in weights if k!=cat]; rsum=sum(weights[k] for k in rest)
            for k in rest: weights[k]+=excess*(weights[k]/rsum if rsum else 1/len(rest))
    total=sum(weights.values())
    return {k:v/total for k,v in weights.items()}
