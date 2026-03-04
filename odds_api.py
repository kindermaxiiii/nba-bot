# odds_api.py
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import requests


ODDS_API_BASE = os.getenv("ODDS_API_BASE", "https://api.the-odds-api.com")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"_non_json": resp.text[:5000]}


def _normalize_response_shape(x: Any) -> Any:
    """
    Support typical shapes seen in the wild:
    - list[dict] (ideal)
    - dict with "data" (some wrappers)
    - tuple(list, meta) or tuple(meta, list)
    - {"data": [...], "meta": {...}}
    """
    if x is None:
        return []
    if isinstance(x, tuple) and len(x) == 2:
        a, b = x
        if isinstance(a, list) and isinstance(b, dict):
            return a
        if isinstance(b, list) and isinstance(a, dict):
            return b
        if isinstance(a, list) and isinstance(b, list):
            # weird but possible
            return a
        # unknown tuple, just return first
        return a
    if isinstance(x, dict):
        if "data" in x:
            return x["data"]
    return x


def _flatten_games(obj: Any) -> List[Dict[str, Any]]:
    """
    Convert any nested response to a clean list of game dicts.
    A "game dict" is recognized by having home_team & away_team.
    """
    obj = _normalize_response_shape(obj)

    out: List[Dict[str, Any]] = []

    def rec(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, dict):
            # wrapper
            if "data" in v:
                rec(v["data"])
                return
            # game object
            if "home_team" in v and "away_team" in v:
                out.append(v)
                return
            # walk
            for vv in v.values():
                rec(vv)
            return
        if isinstance(v, list):
            for it in v:
                rec(it)
            return
        # ignore scalars

    rec(obj)
    return out


def _request_json(url: str, params: Dict[str, Any], timeout: int = 25) -> Any:
    headers = {
        "User-Agent": "nba-bot/1.0 (+github-actions)",
        "Accept": "application/json",
    }
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    if r.status_code != 200:
        payload = _safe_json(r)
        raise RuntimeError(f"OddsAPI HTTP {r.status_code}: {payload}")
    return r.json()


def fetch_slate_events(sport_key: str = "basketball_nba") -> List[Dict[str, Any]]:
    """
    Optional: try to fetch today's slate from /events if available.
    Not all plans/endpoints always enabled; fails gracefully.
    """
    api_key = _env("ODDS_API_KEY")
    if not api_key:
        raise RuntimeError("ODDS_API_KEY missing")

    # v4 events endpoint (commonly supported)
    url = f"{ODDS_API_BASE}/v4/sports/{sport_key}/events"
    params = {"apiKey": api_key}

    try:
        data = _request_json(url, params=params, timeout=25)
        data = _normalize_response_shape(data)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return data["data"]
        return []
    except Exception:
        # events endpoint not available or blocked; return empty
        return []


def fetch_odds(
    sport_key: str,
    regions: str,
    markets: List[str],
    odds_format: str = "decimal",
    date_format: str = "iso",
) -> Any:
    api_key = _env("ODDS_API_KEY")
    if not api_key:
        raise RuntimeError("ODDS_API_KEY missing")

    markets_csv = ",".join([m.strip() for m in markets if m and str(m).strip() != ""])
    if not markets_csv:
        raise RuntimeError("No markets provided")

    # v4 odds endpoint
    url = f"{ODDS_API_BASE}/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets_csv,
        "oddsFormat": odds_format,
        "dateFormat": date_format,
    }
    return _request_json(url, params=params, timeout=35)


def filter_books_in_games(
    games: List[Dict[str, Any]],
    preferred_books: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Fallback books:
    - If preferred_books provided, keep only those bookmaker keys/titles when present.
    - If after filtering a game has zero bookmakers, keep original bookmakers (fallback).
    """
    if not preferred_books:
        return games

    prefs = set([b.lower().strip() for b in preferred_books if b and str(b).strip() != ""])
    if not prefs:
        return games

    out: List[Dict[str, Any]] = []
    for g in games:
        bms = g.get("bookmakers") or []
        if not isinstance(bms, list):
            out.append(g)
            continue

        filtered = []
        for bm in bms:
            if not isinstance(bm, dict):
                continue
            key = str(bm.get("key", "")).lower()
            title = str(bm.get("title", "")).lower()
            if key in prefs or title in prefs:
                filtered.append(bm)

        if filtered:
            gg = dict(g)
            gg["bookmakers"] = filtered
            out.append(gg)
        else:
            # fallback: keep all books if none matched
            out.append(g)

    return out


def fetch_odds_with_fallback(
    markets: List[str],
    regions_priority: Optional[List[str]] = None,
    sport_key: str = "basketball_nba",
    preferred_books: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    1) Optional slate auto (events) -> only used for logging / sanity (we still pull odds directly)
    2) Try regions in order until we get non-empty games
    3) Parse any response shape into List[game dict]
    4) Apply preferred_books filter with fallback-to-any-book
    """
    if not regions_priority:
        regions_priority = ["us", "eu", "uk", "au"]

    # Try to fetch events (doesn't block if unavailable)
    _ = fetch_slate_events(sport_key=sport_key)

    last_err: Optional[Exception] = None
    for reg in regions_priority:
        try:
            raw = fetch_odds(sport_key=sport_key, regions=reg, markets=markets)
            games = _flatten_games(raw)
            if games:
                games = filter_books_in_games(games, preferred_books=preferred_books)
                return games
        except Exception as e:
            last_err = e
            time.sleep(0.25)
            continue

    if last_err is not None:
        raise last_err
    return []
