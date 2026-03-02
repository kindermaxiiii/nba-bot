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
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    if regions_priority is None:
        regions_priority = ["fr", "eu", "uk", "us", "us2", "au"]

    attempted: List[str] = []
    errors: List[Dict[str, Any]] = []

    for region in regions_priority:
        attempted.append(region)

        params = {
            "apiKey": ODDS_API_KEY,
            "regions": region,
            "markets": markets,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }

        for attempt in range(1, retries + 2):
            try:
                r = requests.get(ODDS_ENDPOINT, params=params, timeout=timeout_s)

                if r.status_code == 422:
                    errors.append(
                        {"region": region, "status": 422, "body": (r.text or "")[:300], "markets": markets}
                    )
                    break

                r.raise_for_status()
                data = r.json()
                return data, {
                    "chosen_region": region,
                    "attempted_regions": attempted,
                    "errors": errors,
                    "markets": markets,
                    "notes": "success",
                }

            except Exception as e:
                if attempt <= retries:
                    time.sleep(sleep_base_s * attempt)
                else:
                    errors.append(
                        {
                            "region": region,
                            "status": getattr(getattr(e, "response", None), "status_code", None),
                            "body": str(e)[:300],
                            "markets": markets,
                        }
                    )

    return [], {
        "chosen_region": None,
        "attempted_regions": attempted,
        "errors": errors,
        "markets": markets,
        "notes": "all regions failed",
    }
