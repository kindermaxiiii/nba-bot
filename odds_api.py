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
    retries: int = 3,
) -> Tuple[int, Any, str, Dict[str, str]]:
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            txt = r.text or ""
            headers = {k.lower(): v for k, v in (r.headers or {}).items()}

            # Rate limit handling (OddsAPI sometimes returns 429)
            if r.status_code == 429:
                ra = headers.get("retry-after")
                sleep_s = float(ra) if ra and ra.replace(".", "", 1).isdigit() else (0.8 * attempt)
                time.sleep(sleep_s)
                continue

            if headers.get("content-type", "").startswith("application/json"):
                try:
                    return r.status_code, r.json(), txt, headers
                except Exception:
                    return r.status_code, None, txt, headers
            return r.status_code, None, txt, headers

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
    retries: int = 3,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (games, meta)
    meta includes chosen_region, regions_tried, errors, markets, and rate-limit headers if present.
    """
    if not ODDS_API_KEY:
        raise OddsApiError("ODDS_API_KEY missing (GitHub Secret).")

    tried: List[str] = []
    errors: List[str] = []
    last_headers: Dict[str, str] = {}

    for region in regions_priority:
        tried.append(region)
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": region,
            "markets": markets,
            "oddsFormat": odds_format,
            "dateFormat": date_format,
        }

        status, js, raw, headers = _request_json(BASE_URL, params=params, timeout=timeout, retries=retries)
        last_headers = headers or last_headers

        if status == 422:
            errors.append(f"422 region={region} markets={markets}: {raw[:200]}")
            continue
        if status >= 400:
            errors.append(f"{status} region={region} markets={markets}: {raw[:200]}")
            continue

        games = js if isinstance(js, list) else []
        return games, {
            "chosen_region": region,
            "regions_tried": tried,
            "errors": errors,
            "markets": markets,
            "rate_limit_remaining": headers.get("x-requests-remaining"),
            "rate_limit_used": headers.get("x-requests-used"),
            "rate_limit_reset": headers.get("x-requests-reset"),
        }

    raise OddsApiError(f"All regions failed. tried={tried} errors={errors[-3:]}")
