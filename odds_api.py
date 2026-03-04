# odds_api.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = os.getenv("ODDS_API_BASE", "https://api.the-odds-api.com/v4")


def _api_key() -> str:
    k = (os.getenv("ODDS_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("ODDS_API_KEY missing")
    return k


def _normalize_shape(x: Any) -> Any:
    # common: (games, meta) or (meta, games)
    if isinstance(x, tuple) and len(x) == 2:
        a, b = x
        if isinstance(a, list):
            return a
        if isinstance(b, list):
            return b
        return a
    # wrapper dict
    if isinstance(x, dict) and "data" in x:
        return x["data"]
    return x


def _flatten_games(x: Any) -> List[Dict[str, Any]]:
    x = _normalize_shape(x)
    out: List[Dict[str, Any]] = []

    def rec(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, dict):
            if "data" in v:
                rec(v["data"])
                return
            if "home_team" in v and "away_team" in v:
                out.append(v)
                return
            for vv in v.values():
                rec(vv)
            return
        if isinstance(v, list):
            for it in v:
                rec(it)
            return

    rec(x)
    return out


def fetch_events(sport_key: str = "basketball_nba") -> List[Dict[str, Any]]:
    """
    Auto-slate (non bloquant). Si l’endpoint est indispo selon ton plan, on ignore.
    """
    url = f"{BASE}/sports/{sport_key}/events"
    try:
        r = requests.get(url, params={"apiKey": _api_key()}, timeout=20)
        if r.status_code != 200:
            return []
        j = r.json()
        j = _normalize_shape(j)
        return j if isinstance(j, list) else []
    except Exception:
        return []


def fetch_odds(
    sport_key: str,
    regions: str,
    markets: List[str],
    odds_format: str = "decimal",
    date_format: str = "iso",
) -> Any:
    url = f"{BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": _api_key(),
        "regions": regions,
        "markets": ",".join([m.strip() for m in markets if m]),
        "oddsFormat": odds_format,
        "dateFormat": date_format,
    }
    r = requests.get(url, params=params, timeout=35)
    if r.status_code != 200:
        # useful debug without dumping secrets
        try:
            msg = r.json()
        except Exception:
            msg = (r.text or "")[:500]
        raise RuntimeError(f"OddsAPI HTTP {r.status_code}: {msg}")
    return r.json()


def filter_books(games: List[Dict[str, Any]], preferred_books: Optional[List[str]]) -> List[Dict[str, Any]]:
    """
    preferred_books = ["fanduel","draftkings","betmgm",...]
    - Si après filtrage un match n’a plus de bookmakers → on garde l’original (fallback).
    """
    if not preferred_books:
        return games
    prefs = {b.lower().strip() for b in preferred_books if b and str(b).strip()}
    if not prefs:
        return games

    out: List[Dict[str, Any]] = []
    for g in games:
        bms = g.get("bookmakers") or []
        if not isinstance(bms, list):
            out.append(g)
            continue

        keep = []
        for bm in bms:
            if not isinstance(bm, dict):
                continue
            key = str(bm.get("key", "")).lower()
            title = str(bm.get("title", "")).lower()
            if key in prefs or title in prefs:
                keep.append(bm)

        if keep:
            gg = dict(g)
            gg["bookmakers"] = keep
            out.append(gg)
        else:
            out.append(g)  # fallback: keep all books

    return out


def fetch_odds_with_fallback(
    markets: List[str],
    regions_priority: Optional[List[str]] = None,
    sport_key: str = "basketball_nba",
    preferred_books: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Retourne: (games, meta)
    - auto-slate events (best effort)
    - fallback régions
    - parsing robuste (tuple/list/dict)
    - fallback books
    """
    if not regions_priority:
        regions_priority = ["us", "eu", "uk", "au"]

    _ = fetch_events(sport_key=sport_key)

    last_err: Optional[str] = None
    tried: List[str] = []

    for reg in regions_priority:
        tried.append(reg)
        try:
            raw = fetch_odds(sport_key=sport_key, regions=reg, markets=markets)
            games = _flatten_games(raw)
            if games:
                games = filter_books(games, preferred_books)
                return games, {"region": reg, "regions_tried": tried, "markets": markets}
        except Exception as e:
            last_err = repr(e)
            time.sleep(0.25)

    return [], {"region": None, "regions_tried": tried, "markets": markets, "error": last_err}
