from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

from database import save_mf_scheme, save_mf_metric, mutual_fund_metrics

# Official AMFI public files/endpoints.
# NAVAll is the daily snapshot for all schemes. Historical NAV is available
# through AMFI's portal and is requested in short date windows.
AMFI_LATEST = "https://portal.amfiindia.com/spages/NAVAll.txt"
AMFI_HISTORY = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
# Free fallback API whose documentation exposes AMFI scheme-code/NAV history.
MFAPI_BASE = "https://api.mfapi.in/mf"
HEADERS = {
    "User-Agent": "FinanX/1.0 educational project",
    "Accept": "text/plain,text/html,application/json,*/*",
}

# A representative universe: direct + growth schemes spanning large-cap,
# flexi-cap, mid-cap, small-cap, index and hybrid categories.
TARGET_TERMS = [
    "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
    "HDFC Flexi Cap Fund - Direct Plan - Growth",
    "Quant Flexi Cap Fund - Direct Plan - Growth",
    "Kotak Flexicap Fund - Direct Growth",
    "UTI Flexi Cap Fund - Direct Growth",
    "SBI Flexicap Fund - Direct Plan - Growth",
    "Canara Robeco Flexi Cap Fund - Direct Growth",
    "JM Flexicap Fund - Direct Plan - Growth",
    "Mirae Asset Large & Midcap Fund - Direct Growth",
    "Canara Robeco Emerging Equities - Direct Plan - Growth",
    "HDFC Large and Mid Cap Fund - Direct Plan - Growth",
    "Motilal Oswal Large and Midcap Fund - Direct Growth",
    "SBI Large & Midcap Fund - Direct Plan - Growth",
    "ICICI Prudential Large & Mid Cap Fund - Direct Plan - Growth",
    "HDFC Mid-Cap Opportunities Fund - Direct Plan - Growth",
    "Kotak Midcap Fund - Direct Growth",
    "Nippon India Growth Mid Cap Fund - Direct Growth",
    "Motilal Oswal Midcap Fund - Direct Growth",
    "SBI Magnum Midcap Fund - Direct Plan - Growth",
    "Edelweiss Mid Cap Fund - Direct Plan - Growth",
    "Nippon India Small Cap Fund - Direct Plan - Growth",
    "SBI Small Cap Fund - Direct Plan - Growth",
    "HDFC Small Cap Fund - Direct Plan - Growth",
    "Tata Small Cap Fund - Direct Growth",
    "Bandhan Small Cap Fund - Direct Growth",
    "ICICI Prudential Bluechip Fund - Direct Plan - Growth",
    "HDFC Nifty 50 Index Fund - Direct Plan - Growth",
    "UTI Nifty 50 Index Fund - Direct Growth",
    "SBI Nifty Index Fund - Direct Plan - Growth",
    "HDFC Balanced Advantage Fund - Direct Plan - Growth",
    "ICICI Prudential Balanced Advantage Fund - Direct Plan - Growth",
    "SBI Balanced Advantage Fund - Direct Plan - Growth",
    "Kotak Equity Savings Fund - Direct Growth",
    "Parag Parikh Conservative Hybrid Fund - Direct Growth",
]


def _get(url: str, *, params: dict | None = None, timeout: int = 35, attempts: int = 3):
    last_exc = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:
            last_exc = exc
            if i + 1 < attempts:
                time.sleep(1.5 * (i + 1))
    raise last_exc


def _parse_latest(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        parts = [p.strip() for p in line.split(';')]
        if len(parts) < 6:
            continue
        code, isin_growth, isin_reinv, name, nav, nav_date = parts[:6]
        try:
            navf = float(nav)
        except (ValueError, TypeError):
            continue
        rows.append({
            "scheme_code": code,
            "scheme_name": name,
            "isin": isin_growth or isin_reinv,
            "latest_nav": navf,
            "latest_date": nav_date,
            "source": "AMFI official NAVAll"
        })
    return rows


def _mfapi_resolve_targets(terms, limit=6):
    """Resolve a small representative set directly from MFAPI when AMFI's
    NAVAll endpoint is temporarily unavailable."""
    selected=[]
    seen=set()
    for term in terms[:limit]:
        try:
            r=_get(f"{MFAPI_BASE}/search", params={"q":term}, timeout=12, attempts=1)
            matches=r.json() or []
            match=next((x for x in matches if "direct" in str(x.get("schemeName","")).lower()
                        and "growth" in str(x.get("schemeName","")).lower()), None)
            match=match or (matches[0] if matches else None)
            if not match: continue
            code=str(match.get("schemeCode") or "").strip()
            name=str(match.get("schemeName") or term).strip()
            if not code or code in seen: continue
            latest=None; latest_date=None
            rr=_get(f"{MFAPI_BASE}/{code}/latest", timeout=12, attempts=1)
            payload=rr.json() or {}
            row=(payload.get("data") or [{}])[0]
            try:
                latest=float(row.get("nav"))
                latest_date=row.get("date")
            except Exception:
                latest=None
            if latest is None: continue
            selected.append({"scheme_code":code,"scheme_name":name,"isin":"","latest_nav":latest,"latest_date":latest_date,"source":"MFAPI/AMFI"})
            seen.add(code)
        except Exception:
            continue
    return selected

def _select_targets(rows, target_count: int = 100):
    names = [(r['scheme_name'].lower(), r) for r in rows]
    selected = []
    for term in TARGET_TERMS:
        t = term.lower()
        exact = next((r for n, r in names if n == t), None)
        if exact:
            selected.append(exact)
            continue
        partial = next((r for n, r in names if t in n), None)
        if partial:
            selected.append(partial)
        if len(selected) >= target_count:
            break

    # Fill remaining slots from actual AMFI latest data, preferring direct-growth
    # schemes across multiple common equity/hybrid categories.
    if len(selected) < target_count:
        for r in rows:
            n = r['scheme_name'].lower()
            if 'direct' in n and 'growth' in n and any(k in n for k in (
                'large cap', 'large & mid', 'flexi', 'mid cap', 'small cap',
                'index', 'balanced advantage', 'hybrid', 'value', 'focused'
            )):
                selected.append(r)
                if len(selected) >= target_count:
                    break

    out, seen = [], set()
    for r in selected:
        if r['scheme_code'] not in seen:
            out.append(r)
            seen.add(r['scheme_code'])
        if len(out) >= target_count:
            break
    return out


def _parse_history(text: str, wanted_codes: set[str]) -> dict[str, list[tuple[date, float]]]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        parts = [p.strip() for p in line.split(';')]
        if len(parts) < 8:
            continue
        # AMFI history: Scheme Code;Scheme Name;ISIN Growth;ISIN Reinvest;NAV;Repurchase;Sale;Date
        code, name, isin1, isin2, nav, repurchase, sale, dstr = parts[:8]
        if code not in wanted_codes:
            continue
        try:
            navf = float(nav)
            d = datetime.strptime(dstr, '%d-%b-%Y').date()
        except (ValueError, TypeError):
            continue
        out.setdefault(code, []).append((d, navf))
    return out


def _nearest_from_rows(rows: list[tuple[date, float]], target: date):
    if not rows:
        return None
    d, nav = min(rows, key=lambda x: abs((x[0] - target).days))
    return d, nav


def _fetch_history_amfi(codes: set[str]):
    """Fetch all target scheme history through short AMFI portal windows.

    AMFI documents a maximum 90-day historical download interval. In practice,
    short 7-day windows are much more reliable, so we query around the exact
    1Y/3Y/5Y anchor dates.
    """
    today = date.today()
    targets = {
        '1y': today - timedelta(days=365),
        '3y': today - timedelta(days=365 * 3),
        '5y': today - timedelta(days=365 * 5),
    }
    collected = {code: {} for code in codes}
    for label, target in targets.items():
        start = target - timedelta(days=3)
        end = target + timedelta(days=3)
        params = {
            'tp': '1',
            'frmdt': start.strftime('%d-%b-%Y'),
            'todt': end.strftime('%d-%b-%Y'),
        }
        r = _get(AMFI_HISTORY, params=params)
        parsed = _parse_history(r.text, codes)
        for code, rows in parsed.items():
            nearest = _nearest_from_rows(rows, target)
            if nearest:
                collected[code][label] = nearest
    return collected


def _fetch_history_mfapi(code: str):
    """Fallback: MFapi exposes scheme-code NAV history derived from AMFI data."""
    url = f"{MFAPI_BASE}/{code}"
    r = _get(url, timeout=25, attempts=2)
    payload = r.json()
    rows = []
    for item in payload.get('data', []) or []:
        try:
            rows.append((datetime.strptime(item['date'], '%d-%m-%Y').date(), float(item['nav'])))
        except Exception:
            continue
    if not rows:
        return {}
    today = date.today()
    targets = {
        '1y': today - timedelta(days=365),
        '3y': today - timedelta(days=365 * 3),
        '5y': today - timedelta(days=365 * 5),
    }
    out = {}
    for label, target in targets.items():
        nearest = _nearest_from_rows(rows, target)
        if nearest:
            out[label] = nearest
    return out


def _fallback_history_all(codes: set[str], existing: dict[str, dict]):
    missing_codes = [c for c in codes if len(existing.get(c, {})) < 3]
    if not missing_codes:
        return existing
    # Keep fallback concurrency modest; the normal path is still direct AMFI.
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_history_mfapi, code): code for code in missing_codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                alt = fut.result()
                if alt:
                    existing.setdefault(code, {}).update({k: v for k, v in alt.items() if k not in existing.get(code, {})})
            except Exception:
                continue
    return existing


def update_amfi_metrics() -> dict:
    """Refresh a 100-fund comparison universe using official AMFI data.

    The primary path is AMFI NAVAll + AMFI historical NAV. If the AMFI history
    portal temporarily returns an error/stub, the history calculation falls
    back to MFapi's free scheme-code history, which is derived from AMFI NAV data.
    """
    try:
        try:
            r = _get(AMFI_LATEST, timeout=15, attempts=2)
            latest = _parse_latest(r.text)
            selected = _select_targets(latest, 100)
        except Exception:
            latest = []
            selected = _mfapi_resolve_targets(TARGET_TERMS, limit=8)
        if not selected:
            raise RuntimeError('No representative mutual-fund schemes could be loaded')

        # Save latest NAV immediately, even if history is temporarily unavailable.
        for row in selected:
            save_mf_scheme(row)

        codes = {x['scheme_code'] for x in selected}
        history = {code: {} for code in codes}
        history_error = None
        try:
            history = _fetch_history_amfi(codes)
        except Exception as exc:
            history_error = str(exc)

        history = _fallback_history_all(codes, history)

        count = 0
        history_complete = 0
        for row in selected:
            h = history.get(row['scheme_code'], {})
            if len(h) == 3:
                history_complete += 1
            vals = {k: (v[1] if v else None) for k, v in h.items()}
            latest_nav = row['latest_nav']

            def cagr(label, years):
                base = vals.get(label)
                if base in (None, 0) or latest_nav in (None, 0):
                    return None
                return round(((latest_nav / base) ** (1 / years) - 1) * 100, 2)

            save_mf_metric({
                'scheme_code': row['scheme_code'],
                'scheme_name': row['scheme_name'],
                'latest_nav': latest_nav,
                'latest_date': row['latest_date'],
                'return_1y': cagr('1y', 1),
                'return_3y': cagr('3y', 3),
                'return_5y': cagr('5y', 5),
                'source': 'AMFI official NAV/history' if len(h) else 'AMFI NAV only',
            })
            count += 1

        source_note = 'AMFI official NAV + history'
        if history_error:
            source_note += ' (history fallback used where needed)'
        return {
            'success': count,
            'history_complete': history_complete,
            'source': source_note,
            'history_error': history_error,
        }
    except Exception as exc:
        return {'success': 0, 'error': str(exc)}


def update_amfi_metrics_fast() -> dict:
    """Quick first-pass AMFI refresh for a small representative sample."""
    try:
        try:
            r=_get(AMFI_LATEST, timeout=12, attempts=2)
            latest=_parse_latest(r.text)
            selected=_select_targets(latest, 6)
        except Exception:
            selected=_mfapi_resolve_targets(TARGET_TERMS, limit=6)
        if not selected:
            return {"success":0,"error":"No representative mutual-fund schemes found"}
        for row in selected:
            save_mf_scheme(row)
        codes={x["scheme_code"] for x in selected}
        hist={c:{} for c in codes}
        try:
            hist=_fallback_history_all(codes,hist)
        except Exception:
            pass
        saved=0
        for row in selected:
            h=hist.get(row["scheme_code"],{})
            vals={k:(v[1] if v else None) for k,v in h.items()}
            latest_nav=row["latest_nav"]
            def cagr(label,years):
                base=vals.get(label)
                if base in (None,0) or latest_nav in (None,0): return None
                return round(((latest_nav/base)**(1/years)-1)*100,2)
            save_mf_metric({
                "scheme_code":row["scheme_code"],"scheme_name":row["scheme_name"],
                "latest_nav":latest_nav,"latest_date":row["latest_date"],
                "return_1y":cagr("1y",1),"return_3y":cagr("3y",3),"return_5y":cagr("5y",5),
                "source":"AMFI/MFAPI quick history",
            })
            saved+=1
        return {"success":saved,"quick":True}
    except Exception as exc:
        return {"success":0,"error":str(exc)}


BOND_PROXY_TERMS = [
    "Nippon India Corporate Bond Fund - Direct Plan - Growth",
    "HDFC Corporate Bond Fund - Direct Plan - Growth",
    "ICICI Prudential Corporate Bond Fund - Direct Plan - Growth",
]
BOND_PROXY_SOURCE_PREFIX = 'Bond proxy • AMFI NAV/history'


def _select_named_targets(rows, terms):
    names = [(r['scheme_name'].lower(), r) for r in rows]
    selected=[]
    seen=set()
    for term in terms:
        t=term.lower()
        exact=next((r for n,r in names if n==t),None)
        partial=next((r for n,r in names if t in n),None)
        row=exact or partial
        if row and row['scheme_code'] not in seen:
            selected.append(row); seen.add(row['scheme_code'])
    return selected


def _save_scheme_history_metrics(selected, history, source_prefix):
    saved=0
    complete=0
    for row in selected:
        h=history.get(row['scheme_code'],{})
        if len(h)==3: complete += 1
        vals={k:(v[1] if v else None) for k,v in h.items()}
        latest_nav=row.get('latest_nav')
        def cagr(label, years):
            base=vals.get(label)
            if base in (None,0) or latest_nav in (None,0): return None
            return round(((latest_nav/base)**(1/years)-1)*100,2)
        save_mf_metric({
            'scheme_code':row['scheme_code'],
            'scheme_name':row['scheme_name'],
            'latest_nav':latest_nav,
            'latest_date':row.get('latest_date'),
            'return_1y':cagr('1y',1),
            'return_3y':cagr('3y',3),
            'return_5y':cagr('5y',5),
            'source':source_prefix,
        })
        saved += 1
    return saved, complete


def update_bond_proxy_metrics_fast() -> dict:
    """Fast background return series for Indian bond funds.

    Bond yields are displayed separately. For 1Y/3Y/5Y return columns, use
    a small representative corporate-bond mutual-fund proxy whose daily NAV
    history is available from AMFI/MFAPI.
    """
    try:
        try:
            r=_get(AMFI_LATEST, timeout=12, attempts=1)
            latest=_parse_latest(r.text)
            selected=_select_named_targets(latest, BOND_PROXY_TERMS)
        except Exception:
            selected=_mfapi_resolve_targets(BOND_PROXY_TERMS, limit=3)
        if len(selected) < 1:
            return {'success':0,'error':'No bond proxy schemes found'}
        for row in selected:
            save_mf_scheme(row)
        codes={x['scheme_code'] for x in selected}
        hist={c:{} for c in codes}
        try:
            hist=_fetch_history_amfi(codes)
        except Exception:
            pass
        hist=_fallback_history_all(codes,hist)
        saved, complete=_save_scheme_history_metrics(selected,hist,BOND_PROXY_SOURCE_PREFIX)
        return {'success':saved,'history_complete':complete,'source':'AMFI/MFAPI bond-fund proxy'}
    except Exception as exc:
        return {'success':0,'error':str(exc)}


def bond_proxy_metrics() -> dict:
    rows = [r for r in mutual_fund_metrics() if str(r.get('source','')).startswith(BOND_PROXY_SOURCE_PREFIX)]
    valid=[r for r in rows if any(r.get(k) is not None for k in ('return_1y','return_3y','return_5y'))]
    out={
        'available': bool(valid),
        'source':'AMFI NAV/history • representative corporate bond fund proxy',
        'sample_size':len(valid),
        'return_1y':None,'return_3y':None,'return_5y':None,
        'schemes':[r.get('scheme_name') for r in valid if r.get('scheme_name')],
        'options':[{
            'name':r.get('scheme_name'),'symbol':r.get('scheme_code'),
            'yoy':r.get('return_1y'),'three_year_return':r.get('return_3y'),
            'five_year_return':r.get('return_5y'),
            'latest_nav':r.get('latest_nav'),'latest_date':r.get('latest_date'),
        } for r in valid],
    }
    for k in ('return_1y','return_3y','return_5y'):
        vals=[float(r[k]) for r in valid if r.get(k) is not None]
        if vals: out[k]=round(sum(vals)/len(vals),2)
    return out


def category_metrics() -> dict:
    rows = [r for r in mutual_fund_metrics() if not str(r.get('source','')).startswith(BOND_PROXY_SOURCE_PREFIX)]
    valid = [r for r in rows if any(r.get(k) is not None for k in ('return_1y', 'return_3y', 'return_5y'))]
    if not valid:
        return {
            'available': False,
            'source': 'AMFI official NAV/history',
            'schemes': [],
            'sample_size': 0,
        }

    out = {
        'available': True,
        'source': 'AMFI official NAV/history',
        'schemes': [],
        'sample_size': len(valid),
        'return_1y': None,
        'return_3y': None,
        'return_5y': None,
    }
    for k in ('return_1y', 'return_3y', 'return_5y'):
        vals = [float(r[k]) for r in valid if r.get(k) is not None]
        if vals:
            out[k] = round(sum(vals) / len(vals), 2)
    out['schemes'] = [r['scheme_name'] for r in valid[:30]]
    return out
