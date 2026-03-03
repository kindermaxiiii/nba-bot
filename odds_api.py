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


def _request_json(url: str, params: Dict[str, Any], timeout: int, retries: int = 2) -> Tuple[int, Any, str, Dict[str, str]]:
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            txt = r.text or ""
            headers = dict(r.headers or {})
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


def _count_books(games: List[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Returns: (unique_books, total_books)
    unique_books: distinct bookmaker titles across the slate
    total_books: sum of bookmakers across all games
    """
    titles = set()
    total = 0
    for g in games or []:
        bms = g.get("bookmakers", []) or []
        total += len(bms)
        for b in bms:
            t = b.get("title")
            if t:
                titles.add(t)
    return len(titles), total


def fetch_odds_with_fallback(
    markets: str,
    regions_priority: List[str],
    odds_format: str = DEFAULT_ODDS_FORMAT,
    date_format: str = DEFAULT_DATE_FORMAT,
    timeout: int = 25,
    retries: int = 2,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not ODDS_API_KEY:
        raise OddsApiError("ODDS_API_KEY missing (env var / GitHub Secret).")

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
        last_headers = headers or {}

        # 422 = plan/market not supported, try next region
        if status == 422:
            errors.append(f"422 region={region} markets={markets}: {raw[:200]}")
            continue

        if status >= 400:
            errors.append(f"{status} region={region} markets={markets}: {raw[:200]}")
            continue

        games = js if isinstance(js, list) else []
        unique_books, total_books = _count_books(games)

        return games, {
            "chosen_region": region,
            "regions_tried": tried,
            "errors": errors,
            "markets": markets,
            "unique_books": unique_books,
            "total_books": total_books,
            # info quota (useful debug)
            "x_requests_remaining": last_headers.get("x-requests-remaining"),
            "x_requests_used": last_headers.get("x-requests-used"),
        }

    raise OddsApiError(f"All regions failed. tried={tried} errors={errors[-3:]}")
