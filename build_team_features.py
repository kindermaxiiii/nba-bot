"""build_team_features.py

Generate a lightweight team features file used by model_team.py.

Output: data/team_features.json

Design:
- Primary keys are OddsAPI-compatible full names whenever possible (e.g. "Los Angeles Lakers").
- Also store a couple of aliases (nickname/abbr) to reduce name mismatch risk.

Free data only (nba_api).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from nba_api.stats.endpoints import leaguedashteamstats
from nba_api.stats.library.parameters import SeasonAll
from nba_api.stats.static import teams as static_teams


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUT_FILE = os.path.join(DATA_DIR, "team_features.json")


def build_team_features(season: str = SeasonAll.all) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)

    print("Fetching team advanced stats from NBA API...")
    df = leaguedashteamstats.LeagueDashTeamStats(
        season=season,
        measure_type_detailed_defense="Advanced",
        per_mode_detailed="PerGame",
    ).get_data_frames()[0]

    id_to_full = {int(t["id"]): (t.get("full_name") or "").strip() for t in static_teams.get_teams()}

    out: Dict[str, Dict[str, Any]] = {}

    for _, r in df.iterrows():
        team_id = r.get("TEAM_ID")
        if team_id is None:
            continue
        team_id = int(team_id)

        full_name = id_to_full.get(team_id, "").strip()
        if not full_name:
            city = str(r.get("TEAM_CITY") or "").strip()
            name = str(r.get("TEAM_NAME") or "").strip()
            full_name = (city + " " + name).strip()
        if not full_name:
            continue

        features = {
            "team_id": team_id,
            "net_rating": float(r.get("NET_RATING")),
            "ortg": float(r.get("OFF_RATING")),
            "drtg": float(r.get("DEF_RATING")),
            "pace": float(r.get("PACE")),
        }

        # Primary key
        out[full_name] = features

        # Aliases
        nickname = str(r.get("TEAM_NAME") or "").strip()
        if nickname and nickname not in out:
            out[nickname] = features

        abbr = str(r.get("TEAM_ABBREVIATION") or "").strip()
        if abbr and abbr not in out:
            out[abbr] = features

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(out)} team feature rows to {OUT_FILE}")


if __name__ == "__main__":
    build_team_features()
