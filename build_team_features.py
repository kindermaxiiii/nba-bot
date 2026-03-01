import json
import os
import requests
from datetime import datetime, timezone

BASE_URL = "https://api.balldontlie.io/nba/v1"
OUT_PATH = "data/team_features.json"


def guess_season_year() -> int:
    now = datetime.now(timezone.utc)
    y = now.year
    if now.month < 8:
        return y - 1
    return y


def fetch_team_season_averages(season: int) -> dict:
    url = f"{BASE_URL}/team_season_averages/general"
    params = {"season": season, "season_type": "regular", "type": "advanced"}

    headers = {}
    api_key = os.environ.get("BALLDONTLIE_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-KEY"] = api_key

    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()


def normalize_team_name(name: str) -> str:
    return name.strip()


def main():
    season = guess_season_year()
    data = fetch_team_season_averages(season)

    teams = data.get("data", [])
    if not teams:
        raise RuntimeError("No team season averages returned. API may be down or requires a key.")

    out = {
        "season": season,
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "by_team_id": {},
        "by_team_name": {}
    }

    for t in teams:
        team = t.get("team", {})
        team_id = team.get("id")
        team_name = team.get("full_name") or team.get("name") or ""

        row = {
            "team_id": team_id,
            "team_name": team_name,
            "pace": t.get("pace"),
            "off_rtg": t.get("off_rtg") or t.get("offensive_rating") or t.get("ortg"),
            "def_rtg": t.get("def_rtg") or t.get("defensive_rating") or t.get("drtg"),
            "net_rtg": t.get("net_rtg") or t.get("net_rating") or t.get("netrtg"),
            "games": t.get("games") or t.get("gp")
        }

        if team_id is not None:
            out["by_team_id"][str(team_id)] = row
        if team_name:
            out["by_team_name"][normalize_team_name(team_name)] = row

    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Saved team features to {OUT_PATH} (season {season}) with {len(out['by_team_name'])} teams.")


if __name__ == "__main__":
    main()
