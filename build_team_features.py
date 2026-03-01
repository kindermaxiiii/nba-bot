import json
import os
from datetime import datetime, timezone
import requests

OUT_PATH = "data/team_features.json"
BASE_URL = "https://api.balldontlie.io/nba/v1/teams"


def fetch_all_teams():
    api_key = os.environ.get("BALLDONTLIE_API_KEY")

    if not api_key:
        raise RuntimeError("BALLDONTLIE_API_KEY is missing.")

    headers = {
        "Authorization": api_key,  # IMPORTANT: pas de Bearer
        "Accept": "application/json",
    }

    teams = []
    cursor = None

    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor

        r = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        teams.extend(data.get("data", []))

        meta = data.get("meta", {})
        cursor = meta.get("next_cursor")
        if not cursor:
            break

    return teams


def main():
    print("Fetching teams from balldontlie (NBA v1)...")

    teams = fetch_all_teams()

    out = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "by_team_name": {},
    }

    for t in teams:
        name = (t.get("full_name") or "").strip()
        if not name:
            continue

        out["by_team_name"][name] = {
            "team_name": name,
            "team_id": t.get("id"),
            "abbreviation": t.get("abbreviation"),
            "games": None,
            "pace": None,
            "off_rtg": None,
            "def_rtg": None,
            "net_rtg": None,
        }

    os.makedirs("data", exist_ok=True)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(out['by_team_name'])} NBA teams.")


if __name__ == "__main__":
    main()
