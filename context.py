import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests

BDL_API_KEY = os.environ.get("BALLDONTLIE_API_KEY")
BDL_BASE = "https://api.balldontlie.io/nba/v1"


class BdlError(RuntimeError):
    pass


def _bdl_headers() -> Dict[str, str]:
    if not BDL_API_KEY:
        return {}
    return {"Authorization": BDL_API_KEY}


def bdl_get(path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Any:
    url = f"{BDL_BASE}{path}"
    r = requests.get(url, headers=_bdl_headers(), params=params or {}, timeout=timeout)
    if r.status_code >= 400:
        raise BdlError(f"{r.status_code} {url}: {r.text[:200]}")
    return r.json()


def search_player_id(player_name: str) -> Optional[int]:
    q = (player_name or "").strip()
    if not q:
        return None
    try:
        js = bdl_get("/players", params={"search": q, "per_page": 5})
        items = js.get("data") or []
        if not items:
            return None
        return int(items[0].get("id"))
    except Exception:
        return None


def current_season_start_year_utc() -> int:
    now = datetime.now(timezone.utc)
    y = now.year
    return y if now.month >= 8 else y - 1


def fetch_player_season_minutes(player_id: int, season_start_year: Optional[int] = None) -> Optional[float]:
    if not player_id:
        return None

    season = season_start_year or current_season_start_year_utc()
    tries = [
        ("/season_averages", {"season": season, "player_ids[]": player_id}),
        ("/season_averages", {"season": season, "player_ids": [player_id]}),
    ]
    for path, params in tries:
        try:
            js = bdl_get(path, params=params)
            data = js.get("data") or []
            if not data:
                continue
            row = data[0]
            if "minutes" in row and row["minutes"] is not None:
                return float(row["minutes"])
            if "min" in row and row["min"]:
                m = str(row["min"])
                if ":" in m:
                    mm, ss = m.split(":", 1)
                    return float(mm) + float(ss) / 60.0
                return float(m)
        except Exception:
            continue
    return None


def fetch_injuries() -> List[Dict[str, Any]]:
    candidates = [
        ("/player_injuries", {"per_page": 100}),
        ("/injuries", {"per_page": 100}),
    ]
    for path, params in candidates:
        try:
            js = bdl_get(path, params=params)
            data = js.get("data") or []
            out: List[Dict[str, Any]] = []
            for it in data:
                player = it.get("player", {}) or {}
                team = it.get("team", {}) or {}
                out.append({
                    "player": player.get("full_name") or f"{player.get('first_name','')} {player.get('last_name','')}".strip(),
                    "team": team.get("abbreviation") or team.get("name") or team.get("full_name"),
                    "status": it.get("status") or it.get("injury_status") or it.get("report_status"),
                    "description": it.get("description") or it.get("injury") or it.get("note"),
                })
            if out:
                return out
        except Exception:
            continue
    return []


def build_injury_note(match: str, injuries: List[Dict[str, Any]], max_items: int = 4) -> Optional[str]:
    if not injuries:
        return None

    try:
        away, home = [x.strip() for x in match.split("@")]
    except Exception:
        away, home = "", ""

    def team_match(team_field: str) -> bool:
        t = (team_field or "").lower()
        # heuristique simple
        return (away.lower()[:3] in t) or (home.lower()[:3] in t)

    items = [i for i in injuries if team_match(i.get("team", ""))]
    if not items:
        return None

    parts = []
    for it in items[:max_items]:
        p = it.get("player") or "?"
        st = it.get("status") or "?"
        parts.append(f"{p} ({st})")

    more = "" if len(items) <= max_items else f" +{len(items) - max_items} autres"
    return ", ".join(parts) + more
