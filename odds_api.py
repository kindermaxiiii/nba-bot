import os
import time
import requests

BASE = "https://api.the-odds-api.com/v4"


def _api_key():
    k = os.getenv("ODDS_API_KEY", "").strip()
    if not k:
        raise RuntimeError("ODDS_API_KEY missing")
    return k


def _normalize(x):
    if x is None:
        return []
    if isinstance(x, tuple):
        return x[0]
    if isinstance(x, dict) and "data" in x:
        return x["data"]
    return x


def _flatten(x):
    x = _normalize(x)
    out = []

    def rec(v):
        if isinstance(v, dict):
            if "home_team" in v and "away_team" in v:
                out.append(v)
                return
            for vv in v.values():
                rec(vv)
        elif isinstance(v, list):
            for it in v:
                rec(it)

    rec(x)
    return out


def fetch_events():
    url = f"{BASE}/sports/basketball_nba/events"
    r = requests.get(url, params={"apiKey": _api_key()}, timeout=20)
    if r.status_code != 200:
        return []
    j = r.json()
    if isinstance(j, dict) and "data" in j:
        return j["data"]
    if isinstance(j, list):
        return j
    return []


def fetch_odds(regions, markets):
    url = f"{BASE}/sports/basketball_nba/odds"
    params = {
        "apiKey": _api_key(),
        "regions": regions,
        "markets": ",".join(markets),
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"OddsAPI HTTP {r.status_code}")
    return r.json()


def filter_books(games, books):
    if not books:
        return games

    books = {b.lower() for b in books}
    out = []

    for g in games:
        bms = g.get("bookmakers", [])
        good = []

        for bm in bms:
            key = str(bm.get("key", "")).lower()
            title = str(bm.get("title", "")).lower()
            if key in books or title in books:
                good.append(bm)

        if good:
            g = dict(g)
            g["bookmakers"] = good
        out.append(g)

    return out


def fetch_odds_with_fallback(markets, regions=("us", "eu", "uk", "au"), books=None):
    fetch_events()  # auto slate check

    for r in regions:
        try:
            raw = fetch_odds(r, markets)
            games = _flatten(raw)

            if games:
                return filter_books(games, books)
        except Exception:
            time.sleep(0.3)

    return []
