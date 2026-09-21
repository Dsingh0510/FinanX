
from __future__ import annotations
import re
from datetime import datetime, timezone
import requests

UA = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36"}

def _get(url):
    r=requests.get(url,headers=UA,timeout=12)
    r.raise_for_status()
    return r.text

def _first_float(patterns, text):
    for p in patterns:
        m=re.search(p,text,re.I|re.S)
        if m:
            try:
                return float(m.group(1).replace(",",""))
            except Exception:
                pass
    return None

def get_indian_bond_values():
    out=[]
    # Government 10Y yield: public India bond page.
    try:
        html=_get("https://countryeconomy.com/bonds/india")
        v=_first_float([
            r'Last\s*</?[^>]*>\s*([0-9]+\.[0-9]+)',
            r'India\s*-\s*10-Year Government Bond Yield.*?Last.*?([0-9]+\.[0-9]+)',
            r'10-Year Government Bond Yield.*?([0-9]+\.[0-9]+)\s*</'
        ],html)
        if v is not None:
            out.append({"label":"India 10Y Government Bond","value":v,"kind":"bond_yield","timestamp":datetime.now(timezone.utc).isoformat(),"term":"10Y G-Sec"})
    except Exception:
        pass

    # Indian AAA corporate bond reference from Canara Robeco daily rates.
    try:
        html=_get("https://www.canararobeco.com/daily-market-updates/")
        text=re.sub(r'\s+',' ',re.sub(r'<[^>]+>',' ',html))
        patterns=[
            r'5\s*Yr\s*AAA\s*Corp\s*Bond.*?([0-9]+\.[0-9]+)',
            r'5\s*Year\s*AAA\s*Corp\s*Bond.*?([0-9]+\.[0-9]+)',
        ]
        v=_first_float(patterns,text)
        if v is not None:
            out.append({"label":"India 5Y AAA Corporate Bond","value":v,"kind":"bond_yield","timestamp":datetime.now(timezone.utc).isoformat(),"term":"5Y AAA"})
    except Exception:
        pass
    return out
