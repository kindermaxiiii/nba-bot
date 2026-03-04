# odds_api.py
"""
OddsAPI client with robust fallbacks.

Key goals:
- Always return (games_list, meta_dict) where games_list is a list of dict games.
- Support regions fallback (cfg.regions_priority)
- Support markets fallback:
  * Try cfg.markets
  * If OddsAPI returns 422 invalid_market, retry with core markets: h2h, spreads, totals
  * Props markets (player_*) are attempted only if they are in cfg.markets.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Tuple


ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def _get(url: str, timeout: int = 25) -> Tuple[int, Any, Dict[str, str]]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            data = json.loads(r.read().decode("utf-8"))
            headers = {k.lower(): v for k, v in dict(r.headers).items()}
            return status, data, headers
    except urllib.error.HTTPError as e:
        # OddsAPI returns JSON for errors as well
        try:
            body = e.read().decode("utf-8")
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        headers = {k.lower(): v for k, v in dict(e.headers).items()}
        return e.code, data, headers


def _as_list_games(data: Any) -> List[Dict[str, Any]]:
    # OddsAPI returns list[game]; sometimes wrappers happen due to accidental tuple nesting.
    if data is None:
        return []
    if isinstance(data, list):
        # could be [[...]] accidental nesting
        if len(data) == 1 and isinstance(data[0], list):
            inner = data[0]
            return inner if isinstance(inner, list) else []
        return data
    # If someone passed (data, meta)
    if isinstance(data, tuple) and len(data) >= 1:
        return _as_list_games(data[0])
    # If dict wrapper
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        return data["data"]
    return []


def _normalize_markets(markets: List[str]) -> List[str]:
    # de-dup, keep order
    seen = set()
    out = []
    for m in markets or []:
        m = (m or "").strip()
        if not m or m in seen:
            continue
        out.append(m)
        seen.add(m)
    return out


def _core_markets() -> List[str]:
    return ["h2h", "spreads", "totals"]


def fetch_odds(
    api_key: str,
    region: str,
    markets: List[str],
    odds_format: str = "decimal",
    date_format: str = "iso",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    markets = _normalize_markets(markets)
    url = (
        f"{ODDS_API_BASE}/sports/basketball_nba/odds/"
        f"?apiKey={api_key}"
        f"&regions={region}"
        f"&markets={','.join(markets)}"
        f"&oddsFormat={odds_format}"
        f"&dateFormat={date_format}"
    )
    status, data, headers = _get(url)

    meta = {
        "region": region,
        "markets": markets,
        "status": status,
        "headers": {k: headers.get(k) for k in ["x-requests-remaining", "x-requests-used", "x-requests-last"] if k in headers},
        "error": None,
        "note": None,
        "raw_error": None,
    }

    if status == 200:
        return _as_list_games(data), meta

    # Normalize OddsAPI error shapes
    err_msg = None
    err_code = None
    if isinstance(data, dict):
        err_msg = data.get("message") or data.get("error") or None
        err_code = data.get("error_code") or data.get("code") or None
    meta["error"] = err_msg or f"HTTP {status}"
    meta["raw_error"] = data

    return [], meta


def fetch_odds_with_fallback(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    api_key = os.getenv("ODDS_API_KEY", "").strip()
    if not api_key:
        return [], {"error": "Missing ODDS_API_KEY secret/env", "region_used": None, "markets_used": None}

    regions = cfg.get("regions_priority") or ["us"]
    regions = [r for r in regions if r]
    markets = _normalize_markets(cfg.get("markets") or _core_markets())

    meta: Dict[str, Any] = {
        "regions_tried": [],
        "region_used": None,
        "markets_requested": markets,
        "markets_used": None,
        "status": None,
        "error": None,
        "note": None,
        "headers": None,
    }

    # Try each region; within each region, handle 422 invalid_market by retrying core markets only.
    for region in regions:
        meta["regions_tried"].append(region)

        games, m1 = fetch_odds(api_key=api_key, region=region, markets=markets)
        meta["status"] = m1.get("status")
        meta["headers"] = m1.get("headers")
        meta["error"] = m1.get("error")
        meta["region_used"] = region
        meta["markets_used"] = markets

        if games:
            return games, meta

        # If invalid_market (422), retry with core markets
        if m1.get("status") == 422:
            core = _core_markets()
            games2, m2 = fetch_odds(api_key=api_key, region=region, markets=core)
            meta["status"] = m2.get("status")
            meta["headers"] = m2.get("headers")
            meta["error"] = m2.get("error")
            meta["region_used"] = region
            meta["markets_used"] = core
            meta["note"] = "Props/extra markets not supported by this OddsAPI endpoint/plan; retried with core markets only."
            if games2:
                return games2, meta

        # Small backoff to be gentle
        time.sleep(0.25)

    meta["error"] = meta["error"] or "No data returned from OddsAPI after fallbacks."
    return [], meta
