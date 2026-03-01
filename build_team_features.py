import json
import os
from datetime import datetime, timezone

import requests


OUT_PATH = "data/team_features.json"
BASE_URL = "https://www.balldontlie.io/api/v1/teams"


def main():
    print("Fetching teams from balldontlie (stable endpoint)...")

    r = requests.get(BASE_URL, timeout=30)
    r.raise_for_status()
    data = r.json()

    teams = data.get("data", [])
    if not teams:
        raise RuntimeError("No teams returned from balldontlie.")

    out = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "by_team_name": {}
    }

    for t in teams:
        name = t.get("full_name")
        if not name:
            continue

        # On initialise avec valeurs neutres
        out["by_team_name"][name] = {
            "team_name": name,
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
