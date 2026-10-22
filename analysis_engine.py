from __future__ import annotations

from typing import Dict, List, Optional

from allocation_engine import RISK_PROFILES

# Score model constants. Keep these named so changes to the scoring model are
# deliberate and easy to audit.
RISK_FIT_WEIGHT = 0.42
HORIZON_FIT_WEIGHT = 0.24
LIQUIDITY_FIT_WEIGHT = 0.14
GOAL_FIT_WEIGHT = 0.20
HISTORICAL_BASE_SCORE = 50.0
HISTORICAL_RETURN_MULTIPLIER = 2.0
VOLATILITY_PENALTY_DIVISOR = 3.0
DRAWDOWN_PENALTY_DIVISOR = 4.0
MAX_VOLATILITY_PENALTY = 18.0
MAX_DRAWDOWN_PENALTY = 18.0
WEIGHT_SCORE_FLOOR = 45.0
WEIGHT_SCORE_EXPONENT = 1.25

# RISK_PROFILES in allocation_engine is the canonical baseline target table.
# This separate table is the single source of truth for category ceilings used
# by both category scoring and the final recommendation layer.
RISK_CAPS = {
    'low': {
        'fd': 0.60, 'bonds': 0.40, 'mutual-funds': 0.25, 'gold': 0.18,
        'stocks': 0.10, 'commodities': 0.00, 'currency': 0.00, 'fno': 0.00,
    },
    'moderate': {
        'fd': 0.50, 'bonds': 0.35, 'mutual-funds': 0.40, 'gold': 0.20,
        'stocks': 0.25, 'commodities': 0.10, 'currency': 0.02, 'fno': 0.00,
    },
    'high': {
        # High risk means growth-oriented, not risk-blind: retain a modest
        # stability sleeve while preventing FD/bonds from dominating the mix.
        'fd': 0.25, 'bonds': 0.25, 'mutual-funds': 0.45, 'gold': 0.20,
        'stocks': 0.45, 'commodities': 0.15, 'currency': 0.05, 'fno': 0.02,
    },
}


def apply_risk_caps(weights: Dict[str, float], risk: str) -> Dict[str, float]:
    """Normalize weights while strictly respecting every category ceiling.

    Uses iterative capped proportional allocation: when a category reaches its
    ceiling, it is fixed there and the remaining budget is redistributed among
    categories that still have room. Excluded categories remain exactly zero.
    """
    if risk not in RISK_CAPS:
        return {}
    caps = RISK_CAPS[risk]
    keys = list(caps)
    clean = {k: max(float(weights.get(k, 0.0) or 0.0), 0.0) for k in keys}
    eligible = [k for k in keys if caps[k] > 0.0]
    if not eligible or sum(caps[k] for k in eligible) < 1.0 - 1e-9:
        return {}

    # If a score vector is all zero, use equal weights among allowed categories.
    if sum(clean[k] for k in eligible) <= 1e-12:
        clean = {k: (1.0 if k in eligible else 0.0) for k in keys}

    result = {k: 0.0 for k in keys}
    remaining = 1.0
    open_keys = list(eligible)

    for _ in range(len(keys) + 2):
        if remaining <= 1e-10 or not open_keys:
            break
        weight_sum = sum(clean[k] for k in open_keys)
        if weight_sum <= 1e-12:
            shares = {k: remaining / len(open_keys) for k in open_keys}
        else:
            shares = {k: remaining * clean[k] / weight_sum for k in open_keys}

        capped_now = [k for k in open_keys if shares[k] > caps[k] + 1e-10]
        if not capped_now:
            for k in open_keys:
                result[k] = shares[k]
            remaining = 0.0
            break

        for k in capped_now:
            result[k] = caps[k]
            remaining -= caps[k]
            open_keys.remove(k)

    if remaining > 1e-7:
        # Caps must permit a full portfolio; fill remaining capacity safely.
        for k in eligible:
            room = max(caps[k] - result[k], 0.0)
            add = min(room, remaining)
            result[k] += add
            remaining -= add
            if remaining <= 1e-9:
                break

    total = sum(result.values())
    if total <= 0 or abs(total - 1.0) > 1e-6:
        return {}
    if any(result[k] > caps[k] + 1e-7 for k in keys):
        return {}
    return {k: result[k] for k in keys if result[k] > 1e-12}


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
        risk_adjust -= min(float(vol) / VOLATILITY_PENALTY_DIVISOR, MAX_VOLATILITY_PENALTY)
    if dd is not None:
        risk_adjust -= min(abs(float(dd)) / DRAWDOWN_PENALTY_DIVISOR, MAX_DRAWDOWN_PENALTY)
    return round(_clip(
        HISTORICAL_BASE_SCORE + blended * HISTORICAL_RETURN_MULTIPLIER + risk_adjust
    ), 1)


def score_categories(user: Dict, market_segments: Dict[str, Dict]) -> List[Dict]:
    risk=str(user.get('risk','moderate')).lower(); horizon=int(user.get('horizon',5)); liquidity=str(user.get('liquidity','medium')).lower(); goal=str(user.get('goal','balanced_growth')).lower()
    rows=[]
    for cat in CATEGORY_RISK:
        base=(
            RISK_FIT_WEIGHT * _risk_fit(cat, risk)
            + HORIZON_FIT_WEIGHT * _horizon_fit(cat, horizon)
            + LIQUIDITY_FIT_WEIGHT * _liquidity_fit(cat, liquidity)
            + GOAL_FIT_WEIGHT * _goal_fit(cat, goal)
        )
        metrics=market_segments.get(cat,{}).get('metrics') or {}
        hist=_historical_score(metrics,horizon)
        live_signal=metrics.get('live_breadth_score')
        if hist is None:
            score = base if live_signal is None else 0.90*base + 0.10*float(live_signal)
        elif live_signal is None:
            score = 0.70*base + 0.30*hist
        else:
            score = 0.60*base + 0.30*hist + 0.10*float(live_signal)
        cap = RISK_CAPS[risk].get(cat)
        rows.append({
            'category': cat,
            'score': round(_clip(score), 1),
            'user_fit': round(base, 1),
            'market_score': hist,
            'cap_percent': cap,
            'metrics': metrics,
            'data_status': market_segments.get(cat, {}).get('status', 'not_configured'),
        })
    rows.sort(key=lambda x:x['score'],reverse=True)
    return rows


def build_dynamic_weights(scores: List[Dict], risk: str) -> Dict[str, float]:
    allowed = [row for row in scores if (row.get('cap_percent') or 0.0) > 0.0]
    raw = {
        row['category']: max(row['score'] - WEIGHT_SCORE_FLOOR, 0.0) ** WEIGHT_SCORE_EXPONENT
        for row in allowed
    }
    total = sum(raw.values())

    if total <= 0:
        # Preserve the user's risk profile instead of returning no allocation.
        return apply_risk_caps(dict(RISK_PROFILES[risk]), risk)

    weights = {k: v / total for k, v in raw.items()}
    capped = apply_risk_caps(weights, risk)

    # This should only be reached for malformed inputs; keep a safe profile
    # rather than returning an empty portfolio.
    return capped or apply_risk_caps(dict(RISK_PROFILES[risk]), risk)
