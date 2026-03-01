import json
import os
import time
from datetime import datetime, timezone

import requests
from nba_api.stats.endpoints import leaguedashteamstats


OUT_PATH = "data/team_features.json"


def season_str_from_today() -> str:
    now = datetime.now(timezone.utc)
    start_year = now.year if now.month >= 8 else now.year - 1
    end_year_2 = (start_year + 1) % 100
    return f"{start_year}-{end_year_2:02d}"


def make_session() -> requests.Session:
    """
    stats.nba.com is picky: we mimic a browser.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
        "Connection": "keep-alive",
    })
    return s


def fetch_team_advanced(season: str, retries: int = 3, sleep_s: float = 2.0):
    """
    Retry wrapper for stats.nba.com calls.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            sess = make_session()
            resp = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                season_type_all_star="Regular Season",
                per_mode_detailed="PerGame",
                measure_type_detailed_defense="Advanced",
                timeout=60,               # important (seconds)
                headers=sess.headers      # inject browser-like headers
            )
            df = resp.get_data_frames()[0]
            return df
        except Exception as e:
            last_err = e
            print(f"[attempt {attempt}/{retries}] NBA stats fetch failed: {e}")
            if attempt < retries:
                time.sleep(sleep_s * attempt)
    raise last_err


def main():
    season = season_str_from_today()

    df = fetch_team_advanced(season)
    if df is None or df.empty:
        raise RuntimeError("NBA API returned empty dataframe (possibly blocked temporarily).")

    out = {
        "season": season,
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "by_team_name": {},
    }

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
