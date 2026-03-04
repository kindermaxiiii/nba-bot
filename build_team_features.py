# build_team_features.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from nba_api.stats.endpoints import leaguedashteamstats


def _season_string(dt: datetime) -> str:
    y = dt.year
    m = dt.month
    if m >= 10:
        y1, y2 = y, y + 1
    else:
        y1, y2 = y - 1, y
    return f"{y1}-{str(y2)[-2:]}"


def main() -> None:
    os.makedirs("data", exist_ok=True)
    season = _season_string(datetime.now(timezone.utc))

    out: Dict[str, Dict[str, Any]] = {}

    try:
        df = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            per_mode_detailed="Per100Possessions"
        ).get_data_frames()[0]

        # Columns typically include: TEAM_NAME, OFF_RATING, DEF_RATING, NET_RATING, PACE
        for _, r in df.iterrows():
            name = str(r.get("TEAM_NAME"))
            out[name] = {
                "ortg": float(r.get("OFF_RATING")) if r.get("OFF_RATING") is not None else None,
                "drtg": float(r.get("DEF_RATING")) if r.get("DEF_RATING") is not None else None,
                "net_rating": float(r.get("NET_RATING")) if r.get("NET_RATING") is not None else None,
                "pace": float(r.get("PACE")) if r.get("PACE") is not None else None,
            }

        with open("data/team_features.json", "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

        print(f"Saved team_features.json for season {season} with {len(out)} teams.")
    except Exception as e:
        # fallback: still write an empty file so the bot doesn't crash
        with open("data/team_features.json", "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2, ensure_ascii=False)
        print("Failed to build team features:", repr(e))


if __name__ == "__main__":
    main()
