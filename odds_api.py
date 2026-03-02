import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"
ODDS_ENDPOINT = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/odds"

ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"


def fetch_odds_with_fallback(
    markets: str,
    regions_priority: Optional[List[str]] = None,
    timeout_s: int = 25,
    retries: int = 2,
    sleep_base_s: float = 1.25,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Fetch odds from The Odds API with:
    - fallback regions (some plans do NOT allow certain regions -> 422)
    - retries for transient errors

    Returns: (games_json, meta)
      meta includes chosen region, attempted regions, errors, and notes.
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    if regions_priority is None:
        regions_priority = ["fr", "eu", "uk", "us", "us2", "au"]

    attempted = []
    errors = []

    for region in regions_priority:
        attempted.append(region)

        params = {
            "apiKey": ODDS_API_KEY,
            "regions": region,
            "markets": markets,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }

        last_exc: Optional[Exception] = None

        for attempt in range(1, retries + 2):  # retries means extra tries
            try:
                r = requests.get(ODDS_ENDPOINT, params=params, timeout=timeout_s)

                # 422 = invalid params/region/market for plan
                if r.status_code == 422:
                    errors.append(
                        {
                            "region": region,
                            "status": 422,
                            "body": (r.text or "")[:400],
                            "markets": markets,
                        }
                    )
                    last_exc = RuntimeError(f"422 Unprocessable Entity for regions={region}")
                    break  # move to next region

                # other hard errors
                r.raise_for_status()

                data = r.json()
                meta = {
                    "chosen_region": region,
                    "attempted_regions": attempted,
                    "errors": errors,
                    "markets": markets,
                    "notes": "success",
                }
                return data, meta

            except Exception as e:
                last_exc = e
                if attempt <= retries:
                    time.sleep(sleep_base_s * attempt)
                else:
                    errors.append(
                        {
                            "region": region,
                            "status": getattr(getattr(e, "response", None), "status_code", None),
                            "body": str(e)[:400],
                            "markets": markets,
                        }
                    )

        # region failed, try next
        _ = last_exc

    # all regions failed
    return [], {
        "chosen_region": None,
        "attempted_regions": attempted,
        "errors": errors,
        "markets": markets,
        "notes": "all regions failed",
    }
