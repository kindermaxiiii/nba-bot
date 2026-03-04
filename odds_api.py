# odds_api.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests


ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
NBA_SPORT_KEY = "basketball_nba"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return v


def _as_csv(x: Union[str, List[str], None]) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    return ",".join([str(s).strip() for s in x if str(s).strip()])


def fetch_odds_with_fallback(
    markets: Union[str, List[str]],
    regions_priority: Optional[List[str]] = None,
    odds_format: str = "decimal",
    date_format: str = "iso",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (games, meta).

    games is a List[Dict] in OddsAPI format.
    meta includes regions_used, markets_used, remaining_requests if present.
    """

    api_key = _env("ODDS_API_KEY")
    if not api_key:
        raise RuntimeError("ODDS_API_KEY missing")

    markets_csv = _as_csv(markets)
    if not markets_csv:
        raise RuntimeError("No markets specified")

    regions_priority = regions_priority or ["us"]

    last_err: Optional[str] = None
    for region in regions_priority:
        params = {
            "apiKey": api_key,
            "regions": region,
            "markets": markets_csv,
            "oddsFormat": odds_format,
            "dateFormat": date_format,
        }
        url = f"{ODDS_API_BASE}/{NBA_SPORT_KEY}/odds"
        try:
            r = requests.get(url, params=params, timeout=20)
            meta = {
                "regions_used": region,
                "markets_used": markets_csv,
                "status_code": r.status_code,
            }
            if "x-requests-remaining" in r.headers:
                meta["requests_remaining"] = r.headers.get("x-requests-remaining")
            if "x-requests-used" in r.headers:
                meta["requests_used"] = r.headers.get("x-requests-used")

            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                continue

            data = r.json()
            if isinstance(data, list) and data:
                return data, meta
            # empty list -> try next region
            last_err = "empty_list"
        except Exception as e:
            last_err = repr(e)
            continue

        time.sleep(0.2)

    raise RuntimeError(f"OddsAPI fetch failed: {last_err}")
