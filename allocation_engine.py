from __future__ import annotations

from typing import Dict, List

ASSET_INFO = [
    {
        'slug': 'fd', 'name': 'Fixed Deposit', 'short': 'FD', 'group': 'Stable',
        'risk': 'Low', 'liquidity': 'Medium', 'refresh': 'Rate table',
        'what': 'A bank deposit kept for a fixed period in return for a stated interest rate.',
        'how': 'You deposit a fixed amount for a chosen period. The bank pays interest according to the selected FD terms.',
        'example': '₹50,000 kept for 2 years at the stated FD rate earns interest during that period.',
        'use': 'Often used for stability and capital protection. Premature withdrawal rules and tax treatment vary by bank and product.',
        'watch': 'Interest rate, tenure, bank terms, tax and deposit-insurance limits.'
    },
    {
        'slug': 'bonds', 'name': 'Bonds', 'short': 'Bonds', 'group': 'Income',
        'risk': 'Low–Medium', 'liquidity': 'Medium', 'refresh': 'Market feed / yield',
        'what': 'A bond is a way of lending money to a government or company for a fixed or defined period.',
        'how': 'You buy a bond and receive interest as per its terms. Its market price can change before maturity.',
        'example': 'A bond with a 7% coupon on ₹10,000 may pay about ₹700 interest per year before taxes and other terms.',
        'use': 'Can add income and diversification. Bond prices can move when market interest rates change.',
        'watch': 'Credit quality, maturity, yield, duration, liquidity and interest-rate risk.'
    },
    {
        'slug': 'mutual-funds', 'name': 'Mutual Funds', 'short': 'MF', 'group': 'Growth',
        'risk': 'Varies', 'liquidity': 'Medium–High', 'refresh': 'Daily NAV',
        'what': 'Money from many investors is pooled and managed in a fund according to its stated objective.',
        'how': 'You buy units of a scheme. The fund manager invests the pool in securities such as stocks or bonds.',
        'example': 'A mutual fund may hold many companies, so one investment can be spread across several securities.',
        'use': 'Can provide diversification across securities. Risk depends heavily on the scheme and portfolio.',
        'watch': 'Scheme objective, risk level, fees, holdings, benchmark and time horizon.'
    },
    {
        'slug': 'gold', 'name': 'Gold', 'short': 'Gold', 'group': 'Diversifier',
        'risk': 'Medium', 'liquidity': 'High', 'refresh': 'Market feed',
        'what': 'Gold is a precious metal that people buy as an investment and as a way to diversify savings.',
        'how': 'You gain or lose as the market price of gold changes. Gold can be held through different products.',
        'example': 'If gold rises 8% after you invest ₹20,000, the investment value would be about ₹21,600 before costs.',
        'use': 'Its price can move independently of some financial assets, but it can also be volatile.',
        'watch': 'Price volatility, product form, spreads, storage or fund costs and taxes.'
    },
    {
        'slug': 'stocks', 'name': 'Stocks', 'short': 'Stocks', 'group': 'Growth',
        'risk': 'High', 'liquidity': 'High', 'refresh': 'Real-time feed',
        'what': 'A stock represents a small ownership share in a company.',
        'how': 'You buy shares through a market. Their price changes with company performance, market conditions and investor demand.',
        'example': 'Buying ₹20,000 of shares does not guarantee a return; the value can rise or fall over time.',
        'use': 'Used for long-term growth potential but carries substantial market risk.',
        'watch': 'Valuation, business performance, sector risk, volatility and diversification.'
    },
    {
        'slug': 'commodities', 'name': 'Commodities', 'short': 'Commodities', 'group': 'Diversifier',
        'risk': 'High', 'liquidity': 'Varies', 'refresh': 'Real-time feed',
        'what': 'Commodities are tradable raw materials such as crude oil, metals and agricultural products.',
        'how': 'Their prices move mainly with supply, demand, global events and market expectations.',
        'example': 'Gold, crude oil and copper are common examples of commodities tracked by investors and traders.',
        'use': 'Can diversify a portfolio but prices may be strongly affected by global supply, demand and geopolitics.',
        'watch': 'Volatility, contract specifications, roll costs and leverage.'
    },
    {
        'slug': 'currency', 'name': 'Currency', 'short': 'FX', 'group': 'Diversifier',
        'risk': 'High', 'liquidity': 'High', 'refresh': 'Real-time / delayed by provider',
        'what': 'Currency instruments reflect the changing value of one currency against another.',
        'how': 'A currency pair such as USD/INR moves as the relative value of the two currencies changes.',
        'example': 'If USD/INR moves from 84 to 85, one US dollar becomes more expensive in rupees.',
        'use': 'Can be useful for learning about exchange-rate risk, but derivatives can magnify gains and losses.',
        'watch': 'Leverage, volatility, central-bank actions, spreads and contract rules.'
    },
    {
        'slug': 'fno', 'name': 'Futures & Options', 'short': 'F&O', 'group': 'High Risk',
        'risk': 'Very High', 'liquidity': 'High', 'refresh': 'Real-time feed',
        'what': 'Futures and options are contracts whose value depends on an underlying asset or index.',
        'how': 'A future creates an obligation at an agreed price; an option gives a right, subject to its contract terms.',
        'example': 'A small price movement in the underlying can create a much larger percentage gain or loss because of leverage.',
        'use': 'Useful for risk-management and advanced strategies, but losses can be rapid, especially with leverage.',
        'watch': 'Margin, leverage, expiry, option Greeks, volatility and maximum-loss scenarios.'
    },
]

RISK_PROFILES = {
    'low': {'fd': 0.45, 'bonds': 0.25, 'mutual-funds': 0.15, 'gold': 0.10, 'stocks': 0.05, 'commodities': 0.00, 'currency': 0.00, 'fno': 0.00},
    'moderate': {'fd': 0.30, 'bonds': 0.18, 'mutual-funds': 0.27, 'gold': 0.10, 'stocks': 0.12, 'commodities': 0.03, 'currency': 0.00, 'fno': 0.00},
    'high': {'fd': 0.15, 'bonds': 0.10, 'mutual-funds': 0.25, 'gold': 0.10, 'stocks': 0.28, 'commodities': 0.07, 'currency': 0.03, 'fno': 0.02},
}


def _normalize(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return {k: 0.0 for k in weights}
    return {k: max(v, 0.0) / total for k, v in weights.items()}


def build_portfolio(amount: float, horizon: int, risk: str, liquidity: str, goal: str, emergency: str) -> Dict:
    if risk not in RISK_PROFILES:
        raise ValueError('Select a valid risk profile.')

    weights = dict(RISK_PROFILES[risk])

    # Short horizon: reduce higher-volatility buckets.
    if horizon <= 2:
        for slug in ('stocks', 'commodities', 'currency', 'fno'):
            shift = weights[slug] * 0.50
            weights[slug] -= shift
            weights['fd'] += shift * 0.70
            weights['bonds'] += shift * 0.30

    # Longer horizon: modestly increase growth buckets.
    if horizon >= 7 and risk != 'low':
        shift = min(weights['fd'] * 0.12, 0.05)
        weights['fd'] -= shift
        weights['mutual-funds'] += shift * 0.65
        weights['stocks'] += shift * 0.35

    if liquidity == 'high':
        shift = min(weights['stocks'] * 0.20 + weights['commodities'] * 0.15, 0.05)
        weights['stocks'] -= min(weights['stocks'], shift * 0.65)
        weights['commodities'] -= min(weights['commodities'], shift * 0.15)
        weights['fd'] += shift * 0.80
        weights['bonds'] += shift * 0.20
    elif liquidity == 'low' and horizon >= 5 and risk != 'low':
        shift = min(weights['fd'] * 0.10, 0.03)
        weights['fd'] -= shift
        weights['mutual-funds'] += shift * 0.60
        weights['stocks'] += shift * 0.40

    if emergency == 'yes':
        # Keep a small learning-model liquidity buffer inside stable categories.
        shift = min(max(weights['fd'] * 0.10, 0.0), 0.05)
        weights['fd'] += shift
        weights['stocks'] *= (1 - shift)
        weights['mutual-funds'] *= (1 - shift)

    # This MVP intentionally does not use individual securities or forecast guaranteed returns.
    weights = _normalize(weights)

    allocations = []
    for slug, pct in weights.items():
        if pct <= 0.001:
            continue
        info = next(x for x in ASSET_INFO if x['slug'] == slug)
        allocations.append({
            'slug': slug,
            'asset': info['name'],
            'percent': round(pct * 100, 1),
            'amount': round(amount * pct, 2),
        })

    return {
        'amount': round(amount, 2),
        'horizon': horizon,
        'risk': risk.title(),
        'liquidity': liquidity.title(),
        'goal': goal.replace('_', ' ').title(),
        'emergency_buffer': emergency == 'yes',
        'allocations': allocations,
        'notes': [
            'Educational scenario only — not a guarantee of return and not a substitute for advice from a SEBI-registered investment adviser.',
            'Market values, taxes, fees, inflation, liquidity and product terms can change the result.',
            'F&O is shown as an optional high-risk learning bucket, not as a default recommendation.',
        ],
    }
