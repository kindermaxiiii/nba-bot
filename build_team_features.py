import json
import os
from datetime import datetime, timezone

import requests


OUT_PATH = "data/team_features.json"

# ✅ Nouveau endpoint balldontlie (NBA v1)
BASE_URL = "https://api.balldontlie.io/nba/v1/teams"


def fetch_all_teams() -> list:
    """
    Récupère toutes les équipes via l'API balldontlie.
    (On gère la pagination au cas où.)
    """
    teams = []
    cursor = None

    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor

        r = requests.get(BASE_URL, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        teams.extend(payload.get("data", []))

        meta = payload.get("meta") or {}
        cursor = meta.get("next_cursor")
        if not cursor:
            break

    return teams


def main():
    print("Fetching teams from balldontlie (stable endpoint)...")

    teams = fetch_all_teams()
    if not teams:
        raise RuntimeError("No teams returned from balldontlie.")

    out = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "by_team_name": {}
    }

    for t in teams:
        name = t.get("full_name") or t.get("name")
        if not name:
            continue

        out["by_team_name"][name.strip()] = {
            "team_name": name.strip(),
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
