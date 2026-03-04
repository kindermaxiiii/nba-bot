# odds_api.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://api.the-odds-api.com/v4"


def _api_key() -> str:
    k = (os.getenv("ODDS_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("ODDS_API_KEY missing")
    return k


def _as_list(x: Any) -> List[Dict[str, Any]]:
    # Normalize common shapes (list, dict{data}, tuple nesting)
    if x is None:
        return []
    if isinstance(x, tuple) and len(x) >= 1:
        return _as_list(x[0])
    if isinstance(x, dict) and "data" in x and isinstance(x["data"], list):
        return x["data"]
    if isinstance(x, list):
        # accidental [[...]]
        if len(x) == 1 and isinstance(x[0], list):
            return x[0]
        # only keep game-like dicts
        return [g for g in x if isinstance(g, dict)]
    return []


def _request_json(url: str, params: Dict[str, Any], timeout: int = 30, max_retries: int = 4) -> Tuple[int, Any, Dict[str, str]]:
    backoff = 0.8
    last_status = 0
    last_body: Any = None
    last_headers: Dict[str, str] = {}
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={"Accept": "application/json"})
            last_status = r.status_code
            last_headers = {k.lower(): v for k, v in r.headers.items()}
            if r.headers.get("content-type", "").startswith("application/json"):
                try:
                    last_body = r.json()
                except Exception:
                    last_body = None
            else:
                last_body = r.text

            if last_status == 429:
                ra = last_headers.get("retry-after")
                sleep_s = float(ra) if (ra and ra.isdigit()) else backoff
                time.sleep(min(8.0, sleep_s))
                backoff *= 1.6
                continue

            if 500 <= last_status <= 599:
                time.sleep(min(8.0, backoff))
                backoff *= 1.6
                continue

            return last_status, last_body, last_headers
        except requests.RequestException:
            time.sleep(min(8.0, backoff))
            backoff *= 1.6

    return last_status, last_body, last_headers


def fetch_odds(sport_key: str, region: str, markets: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    url = f"{BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": _api_key(),
        "regions": region,
        "markets": ",".join(markets),
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    status, body, headers = _request_json(url, params=params)
    meta = {
        "status": status,
        "region": region,
        "markets": markets,
        "error": None,
        "headers": {k: headers.get(k) for k in ["x-requests-remaining", "x-requests-used", "x-requests-last", "x-requests-reset"] if k in headers},
        "raw_error": None,
    }
    if status == 200:
        return _as_list(body), meta

    if isinstance(body, dict):
        meta["error"] = body.get("message") or body.get("error") or f"HTTP {status}"
        meta["raw_error"] = body
    else:
        meta["error"] = f"HTTP {status}"
        meta["raw_error"] = str(body)[:200]
    return [], meta


def fetch_odds_with_fallback(
    sport_key: str,
    markets: List[str],
    regions_priority: List[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    regions = [r for r in (regions_priority or []) if r]
    if not regions:
        regions = ["us"]

    core = ["h2h", "spreads", "totals"]
    errors: List[str] = []
    tried: List[str] = []
    last_meta: Dict[str, Any] = {}

    for region in regions:
        tried.append(region)
        games, meta = fetch_odds(sport_key, region, markets)
        last_meta = meta

        if games:
            return games, {"region_used": region, "markets_used": markets, "regions_tried": tried, "note": None, "error": None}

        # 422 -> invalid markets. Retry core markets.
        if meta.get("status") == 422:
            games2, meta2 = fetch_odds(sport_key, region, core)
            if games2:
                return games2, {"region_used": region, "markets_used": core, "regions_tried": tried,
                                "note": "422 invalid markets -> fallback to core h2h/spreads/totals", "error": None}
            errors.append(f"{region}:422 core_failed:{meta2.get('error')}")
        else:
            errors.append(f"{region}:{meta.get('status')}:{meta.get('error')}")

    return [], {"region_used": None, "markets_used": None, "regions_tried": tried, "note": None, "error": errors[-1] if errors else last_meta.get("error")}
