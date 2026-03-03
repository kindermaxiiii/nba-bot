import os
import time
from typing import Any, Dict, List, Tuple, Optional

import requests

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

DEFAULT_ODDS_FORMAT = "decimal"
DEFAULT_DATE_FORMAT = "iso"


class OddsApiError(RuntimeError):
    pass


def _request_json(
    url: str,
    params: Dict[str, Any],
    timeout: int,
    retries: int = 2,
) -> Tuple[int, Any, str]:
    """
    Returns (status_code, json_or_none, raw_text).
    Retries on network + 429 with backoff.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            txt = r.text or ""

            # 429: backoff and retry
            if r.status_code == 429 and attempt < retries:
                time.sleep(1.2 * attempt)
                continue

            if r.headers.get("content-type", "").startswith("application/json"):
                try:
                    return r.status_code, r.json(), txt
                except Exception:
                    return r.status_code, None, txt

            return r.status_code, None, txt

        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.7 * attempt)

    raise OddsApiError(f"Request failed after {retries} retries: {last_exc}")


def fetch_odds_with_fallback(
    markets: str,
    regions_priority: List[str],
    odds_format: str = DEFAULT_ODDS_FORMAT,
    date_format: str = DEFAULT_DATE_FORMAT,
    timeout: int = 25,
    retries: int = 2,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Tries regions in order until an accepted response is returned.
    Handles 422 (invalid region/market for plan).
    Returns (games, meta).
    """
    if not ODDS_API_KEY:
        raise OddsApiError("ODDS_API_KEY missing (GitHub Secret).")

    tried: List[str] = []
    errors: List[str] = []

    for region in regions_priority:
        tried.append(region)
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": region,
            "markets": markets,
            "oddsFormat": odds_format,
            "dateFormat": date_format,
        }

        status, js, raw = _request_json(BASE_URL, params=params, timeout=timeout, retries=retries)

        # Plan / param errors
        if status == 422:
            errors.append(f"422 region={region} markets={markets}: {raw[:200]}")
            continue

        if status >= 400:
            errors.append(f"{status} region={region} markets={markets}: {raw[:200]}")
            continue

        games = js if isinstance(js, list) else []

        # Some plans sometimes return [] for unsupported market/region combos without 422
        if not games:
            errors.append(f"empty region={region} markets={markets}")
            continue

        return games, {
            "chosen_region": region,
            "regions_tried": tried,
            "errors": errors,
            "markets": markets,
            "games": len(games),
        }

    raise OddsApiError(f"All regions failed. tried={tried} errors={errors[-3:]}")
