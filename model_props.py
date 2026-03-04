# model_props.py
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from nba_api.stats.static import players
from nba_api.stats.endpoints import playergamelog


def _clean_name(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace(".", "")
    s = re.sub(r"\b(JR|SR|II|III|IV|V)\b\.?", "", s, flags=re.IGNORECASE).strip()
    return s


_PLAYER_ID_CACHE: Dict[str, Optional[int]] = {}
_LOG_CACHE: Dict[Tuple[int, str], Optional[Any]] = {}  # (player_id, season) -> dataframe


def find_player_id(full_name: str) -> Optional[int]:
    key = _clean_name(full_name).lower()
    if key in _PLAYER_ID_CACHE:
        return _PLAYER_ID_CACHE[key]

    cand = players.find_players_by_full_name(_clean_name(full_name))
    if cand:
        pid = cand[0]["id"]
        _PLAYER_ID_CACHE[key] = pid
        return pid

    # fallback: try last name search
    parts = _clean_name(full_name).split(" ")
    if parts:
        last = parts[-1]
        cand2 = players.find_players_by_full_name(last)
        if cand2:
            pid = cand2[0]["id"]
            _PLAYER_ID_CACHE[key] = pid
            return pid

    _PLAYER_ID_CACHE[key] = None
    return None


def _season_string(year: int, month: int) -> str:
    # NBA season label like "2025-26"
    if month >= 10:
        y1 = year
        y2 = year + 1
    else:
        y1 = year - 1
        y2 = year
    return f"{y1}-{str(y2)[-2:]}"


def get_player_gamelog(player_id: int, season: str):
    k = (player_id, season)
    if k in _LOG_CACHE:
        return _LOG_CACHE[k]
    try:
        df = playergamelog.PlayerGameLog(player_id=player_id, season=season).get_data_frames()[0]
        _LOG_CACHE[k] = df
        return df
    except Exception:
        _LOG_CACHE[k] = None
        return None


def prob_over_from_logs(
    player_name: str,
    stat: str,
    line: float,
    season: str,
    last_n: int = 20,
    min_games: int = 8,
) -> Optional[Dict[str, Any]]:
    """
    stat:
      - "PTS", "REB", "AST", "PRA"
    Returns dict with p_over and metadata.
    """
    pid = find_player_id(player_name)
    if not pid:
        return None

    df = get_player_gamelog(pid, season)
    if df is None or df.empty:
        return None

    df = df.head(last_n).copy()
    if len(df) < min_games:
        return None

    if stat == "PTS":
        vals = df["PTS"].astype(float)
    elif stat == "REB":
        vals = df["REB"].astype(float)
    elif stat == "AST":
        vals = df["AST"].astype(float)
    elif stat == "PRA":
        vals = df["PTS"].astype(float) + df["REB"].astype(float) + df["AST"].astype(float)
    else:
        return None

    over = (vals > float(line)).sum()
    n = len(vals)
    p_over = over / n

    try:
        mins = df["MIN"].astype(float).mean()
    except Exception:
        mins = None

    return {
        "player_id": pid,
        "games_used": n,
        "p_over": float(p_over),
        "avg_min": mins,
    }
