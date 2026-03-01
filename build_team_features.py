import json
import os
from datetime import datetime, timezone

from nba_api.stats.endpoints import leaguedashteamstats


OUT_PATH = "data/team_features.json"


def season_str_from_today() -> str:
    """
    NBA season format for nba_api: '2024-25'
    If month < 8 => season started previous year.
    """
    now = datetime.now(timezone.utc)
    start_year = now.year if now.month >= 8 else now.year - 1
    end_year_2 = (start_year + 1) % 100
    return f"{start_year}-{end_year_2:02d}"


def main():
    season = season_str_from_today()

    # Advanced team stats (includes PACE, OFF_RATING, DEF_RATING, NET_RATING)
    resp = leaguedashteamstats.LeagueDashTeamStats(
        season=season,
        season_type_all_star="Regular Season",
        per_mode_detailed="PerGame",
        measure_type_detailed_defense="Advanced",
    )

    df = resp.get_data_frames()[0]
    if df.empty:
        raise RuntimeError("NBA API returned empty dataframe (possibly blocked temporarily).")

    # Build mapping by team name
    out = {
        "season": season,
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "by_team_name": {},
    }

    # Typical columns: TEAM_NAME, GP, PACE, OFF_RATING, DEF_RATING, NET_RATING
    for _, row in df.iterrows():
        team_name = str(row.get("TEAM_NAME", "")).strip()
        if not team_name:
            continue

        out["by_team_name"][team_name] = {
            "team_name": team_name,
            "games": float(row.get("GP", 0)) if row.get("GP") is not None else None,
            "pace": float(row.get("PACE")) if row.get("PACE") is not None else None,
            "off_rtg": float(row.get("OFF_RATING")) if row.get("OFF_RATING") is not None else None,
            "def_rtg": float(row.get("DEF_RATING")) if row.get("DEF_RATING") is not None else None,
            "net_rtg": float(row.get("NET_RATING")) if row.get("NET_RATING") is not None else None,
        }

    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Saved team features to {OUT_PATH} for season {season} with {len(out['by_team_name'])} teams.")


if __name__ == "__main__":
    main()
