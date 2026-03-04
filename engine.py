# engine.py
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Model(s)
from model_team import model_prob_for_team_market

# -----------------------------
# Config
# -----------------------------

@dataclass
class Config:
    # OddsAPI
    markets: List[str]
    regions_priority: List[str]

    # Engine behavior
    top_n_team: int = 3
    top_n_props: int = 3

    # Model/market blend
    model_weight: float = 0.70  # p_real = w*p_model + (1-w)*p_mkt
    clip_vs_market: float = 0.08  # clamp p_real within +/-8% abs around p_mkt

    # Discipline
    require_positive_ev: bool = True  # keep True for "institutional"
    max_ml_per_day: int = 2
    max_odds_ml: float = 2.40  # anti longshot ML
    one_pick_per_match: bool = True

    # Preference bias (non-ML should win ties)
    prefer_non_ml: bool = True
    non_ml_bonus: float = 2.5  # score bonus for spread/total/teamtotal vs ML

    # Filtering / stability
    min_books_required: int = 2  # for dev/median sanity (can be 1 if you want)
    min_dev_percent: float = 0.0  # if you want dev floor; keep 0 for now

    # Features
    team_features_path: str = "data/team_features.json"


def load_config(path: str = "config.json") -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # sensible defaults if missing
    return Config(
        markets=raw.get("markets", ["h2h", "spreads", "totals"]),
        regions_priority=raw.get("regions_priority", ["us"]),
        top_n_team=int(raw.get("top_n_team", 3)),
        top_n_props=int(raw.get("top_n_props", 3)),
        model_weight=float(raw.get("model_weight", 0.70)),
        clip_vs_market=float(raw.get("clip_vs_market", 0.08)),
        require_positive_ev=bool(raw.get("require_positive_ev", True)),
        max_ml_per_day=int(raw.get("max_ml_per_day", 2)),
        max_odds_ml=float(raw.get("max_odds_ml", 2.40)),
        one_pick_per_match=bool(raw.get("one_pick_per_match", True)),
        prefer_non_ml=bool(raw.get("prefer_non_ml", True)),
        non_ml_bonus=float(raw.get("non_ml_bonus", 2.5)),
        min_books_required=int(raw.get("min_books_required", 2)),
        min_dev_percent=float(raw.get("min_dev_percent", 0.0)),
        team_features_path=str(raw.get("team_features_path", "data/team_features.json")),
    )


# -----------------------------
# Utilities
# -----------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _implied_prob_from_decimal_odds(odds: float) -> float:
    odds = float(odds)
    if odds <= 1e-9:
        return 1.0
    return 1.0 / odds


def _no_vig_two_way(p_a: float, p_b: float) -> Tuple[float, float, float]:
    """
    Return (p_a_nv, p_b_nv, overround)
    """
    s = p_a + p_b
    if s <= 1e-9:
        return 0.5, 0.5, 0.0
    return p_a / s, p_b / s, (s - 1.0)


def _median(vals: List[float]) -> Optional[float]:
    xs = sorted([v for v in vals if v is not None])
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return xs[mid]
    return 0.5 * (xs[mid - 1] + xs[mid])


def _normalize_team_name(name: str) -> str:
    # keep it minimal: just strip; your data/team_features.json uses official names
    return (name or "").strip()


def _market_key_to_internal(mkey: str) -> Optional[str]:
    mkey = (mkey or "").lower().strip()
    if mkey == "h2h":
        return "MONEYLINE"
    if mkey == "spreads":
        return "SPREAD"
    if mkey == "totals":
        return "TOTAL"
    # ignore other markets for now
    return None


# -----------------------------
# Load features
# -----------------------------

def load_team_features(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # ensure key normalization
            out: Dict[str, Dict[str, Any]] = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    out[_normalize_team_name(k)] = v
            return out
    except Exception:
        return {}
    return {}


# -----------------------------
# Collect lines from OddsAPI response
# -----------------------------

def collect_market_lines(games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Output: list of offers
    offer = {
      game_id, match, home_team, away_team,
      market, selection, line, odds, book, books_count,
      odds_list (for selection), median_odds, best_odds
      plus: for two-way nv: paired_median_other_side
    }
    """
    offers: List[Dict[str, Any]] = []

    for g in games:
        home = _normalize_team_name(g.get("home_team", ""))
        away = _normalize_team_name(g.get("away_team", ""))
        if not home or not away:
            continue

        match = f"{away} @ {home}"
        game_id = g.get("id") or match

        bookmakers = g.get("bookmakers") or []
        if not isinstance(bookmakers, list) or not bookmakers:
            continue

        # Gather by (market_internal, selection, line) -> list of prices, also keep per-book
        bucket: Dict[Tuple[str, str, Optional[float]], List[Tuple[str, float]]] = {}

        for bk in bookmakers:
            bname = bk.get("title") or bk.get("key") or "?"
            markets = bk.get("markets") or []
            for mk in markets:
                internal = _market_key_to_internal(mk.get("key"))
                if internal is None:
                    continue

                outcomes = mk.get("outcomes") or []
                if not isinstance(outcomes, list):
                    continue

                if internal == "MONEYLINE":
                    # outcomes: two teams with price
                    for oc in outcomes:
                        sel = _normalize_team_name(oc.get("name", ""))
                        price = _safe_float(oc.get("price"))
                        if not sel or price is None:
                            continue
                        key = (internal, sel, None)
                        bucket.setdefault(key, []).append((bname, float(price)))

                elif internal == "SPREAD":
                    # outcomes: each team with point + price
                    for oc in outcomes:
                        sel = _normalize_team_name(oc.get("name", ""))
                        price = _safe_float(oc.get("price"))
                        line = _safe_float(oc.get("point"))
                        if not sel or price is None or line is None:
                            continue
                        key = (internal, sel, float(line))
                        bucket.setdefault(key, []).append((bname, float(price)))

                elif internal == "TOTAL":
                    # outcomes: Over/Under with point + price
                    for oc in outcomes:
                        sel = (oc.get("name") or "").strip().title()  # "Over"/"Under"
                        price = _safe_float(oc.get("price"))
                        line = _safe_float(oc.get("point"))
                        if sel not in ("Over", "Under") or price is None or line is None:
                            continue
                        key = (internal, sel, float(line))
                        bucket.setdefault(key, []).append((bname, float(price)))

        # Turn buckets into offers (best + median)
        for (market, selection, line), book_prices in bucket.items():
            prices = [p for _, p in book_prices]
            med = _median(prices)
            if med is None:
                continue
            best_idx = max(range(len(prices)), key=lambda i: prices[i])
            best_odds = prices[best_idx]
            best_book = book_prices[best_idx][0]

            offers.append(
                {
                    "game_id": game_id,
                    "match": match,
                    "home_team": home,
                    "away_team": away,
                    "market": market,
                    "selection": selection,
                    "line": line,
                    "odds": float(best_odds),
                    "book": best_book,
                    "books_count": len(book_prices),
                    "odds_list": prices,
                    "median_odds": float(med),
                    "best_odds": float(best_odds),
                }
            )

    return offers


# -----------------------------
# Market probability (no-vig approx using medians)
# -----------------------------

def _market_prob_no_vig_for_offer(offer: Dict[str, Any], offers_by_game: Dict[str, List[Dict[str, Any]]]) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (p_mkt_no_vig, overround_est)
    Uses median odds for both sides (same line when relevant).
    """
    market = offer["market"]
    game_id = offer["game_id"]
    selection = offer["selection"]
    line = offer.get("line")

    game_offers = offers_by_game.get(game_id, [])

    if market == "MONEYLINE":
        # find both teams median odds
        home = offer["home_team"]
        away = offer["away_team"]
        med_home = None
        med_away = None
        for o in game_offers:
            if o["market"] != "MONEYLINE":
                continue
            if o["selection"] == home:
                med_home = o.get("median_odds")
            elif o["selection"] == away:
                med_away = o.get("median_odds")

        if med_home is None or med_away is None:
            return None, None

        p_h = _implied_prob_from_decimal_odds(float(med_home))
        p_a = _implied_prob_from_decimal_odds(float(med_away))
        p_h_nv, p_a_nv, ov = _no_vig_two_way(p_h, p_a)
        if selection == home:
            return p_h_nv, ov
        if selection == away:
            return p_a_nv, ov
        return None, ov

    if market == "SPREAD":
        # same line, both sides
        if line is None:
            return None, None

        home = offer["home_team"]
        away = offer["away_team"]

        med_home = None
        med_away = None
        for o in game_offers:
            if o["market"] != "SPREAD":
                continue
            if o.get("line") is None:
                continue
            if abs(float(o["line"]) - float(line)) > 1e-9:
                continue

            if o["selection"] == home:
                med_home = o.get("median_odds")
            elif o["selection"] == away:
                med_away = o.get("median_odds")

        if med_home is None or med_away is None:
            return None, None

        p_h = _implied_prob_from_decimal_odds(float(med_home))
        p_a = _implied_prob_from_decimal_odds(float(med_away))
        p_h_nv, p_a_nv, ov = _no_vig_two_way(p_h, p_a)

        if selection == home:
            return p_h_nv, ov
        if selection == away:
            return p_a_nv, ov
        return None, ov

    if market == "TOTAL":
        # same total line, over/under
        if line is None:
            return None, None

        med_over = None
        med_under = None
        for o in game_offers:
            if o["market"] != "TOTAL":
                continue
            if o.get("line") is None:
                continue
            if abs(float(o["line"]) - float(line)) > 1e-9:
                continue
            if o["selection"] == "Over":
                med_over = o.get("median_odds")
            elif o["selection"] == "Under":
                med_under = o.get("median_odds")

        if med_over is None or med_under is None:
            return None, None

        p_o = _implied_prob_from_decimal_odds(float(med_over))
        p_u = _implied_prob_from_decimal_odds(float(med_under))
        p_o_nv, p_u_nv, ov = _no_vig_two_way(p_o, p_u)

        if selection == "Over":
            return p_o_nv, ov
        if selection == "Under":
            return p_u_nv, ov
        return None, ov

    return None, None


# -----------------------------
# Score / EV
# -----------------------------

def _ev_decimal(p: float, odds: float) -> float:
    """
    EV in ROI terms (e.g. 0.05 = +5%)
    """
    odds = float(odds)
    p = float(p)
    return p * odds - 1.0


def _dev_best_vs_median(best_odds: float, median_odds: float) -> float:
    """
    Dev = relative improvement vs median price.
    Example: best 2.00 vs median 1.90 => (2/1.9 - 1) = 5.26%
    """
    if median_odds <= 1e-9:
        return 0.0
    return best_odds / median_odds - 1.0


def _score_offer(
    market: str,
    ev: float,
    edge: float,
    dev: float,
    overround_est: Optional[float],
    cfg: Config,
) -> float:
    """
    Score in [0..100] approx.
    Institutional: EV/edge matters, dev helps (good price), high overround penalized.
    """
    # base on EV and edge
    # map EV ~ 0..0.15 to 0..60
    ev_part = _clamp((ev / 0.15) * 60.0, 0.0, 60.0)
    # map edge ~ 0..0.08 to 0..30
    edge_part = _clamp((edge / 0.08) * 30.0, 0.0, 30.0)
    # dev ~ 0..0.06 to 0..10
    dev_part = _clamp((dev / 0.06) * 10.0, 0.0, 10.0)

    score = ev_part + edge_part + dev_part

    # overround penalty if available
    if overround_est is not None:
        # penalize above 6%
        pen = max(0.0, float(overround_est) - 0.06)
        score -= _clamp(pen / 0.08 * 8.0, 0.0, 8.0)

    # prefer non-ML (spread/total) to avoid "ML longshot nonsense"
    if cfg.prefer_non_ml and market != "MONEYLINE":
        score += cfg.non_ml_bonus

    return _clamp(score, 0.0, 100.0)


# -----------------------------
# Core analysis
# -----------------------------

def analyze_team_slate(games: List[Dict[str, Any]], cfg: Config) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    features = load_team_features(cfg.team_features_path)

    offers = collect_market_lines(games)
    offers_by_game: Dict[str, List[Dict[str, Any]]] = {}
    for o in offers:
        offers_by_game.setdefault(o["game_id"], []).append(o)

    candidates: List[Dict[str, Any]] = []
    markets_tested = 0

    for o in offers:
        market = o["market"]
        selection = o["selection"]
        line = o.get("line")
        odds = float(o["odds"])
        median_odds = float(o.get("median_odds") or odds)
        books_count = int(o.get("books_count") or 0)

        # basic books sanity
        if books_count < cfg.min_books_required:
            continue

        # anti longshot ML
        if market == "MONEYLINE" and odds > cfg.max_odds_ml:
            continue

        # compute model prob
        away = o["away_team"]
        home = o["home_team"]

        # HARD STOP: refuse if team features missing (prevents the 50% bug)
        if away not in features or home not in features:
            continue

        p_model = model_prob_for_team_market(
            market=market,
            selection=selection,
            line=line,
            away_team=away,
            home_team=home,
            features=features,
        )
        if p_model is None:
            continue

        # market prob no-vig (from medians)
        p_mkt, ov = _market_prob_no_vig_for_offer(o, offers_by_game)
        if p_mkt is None:
            # if cannot compute no-vig, fallback to implied from median (still better than nothing)
            p_mkt = _implied_prob_from_decimal_odds(median_odds)
            ov = None

        # blend + clip (institutional discipline)
        w = cfg.model_weight
        p_real_raw = w * float(p_model) + (1.0 - w) * float(p_mkt)
        p_real = float(p_real_raw)

        # clip p_real around market
        clip = float(cfg.clip_vs_market)
        p_real = _clamp(p_real, float(p_mkt) - clip, float(p_mkt) + clip)
        p_real = _clamp(p_real, 0.01, 0.99)

        # EV and edge
        ev = _ev_decimal(p_real, odds)
        edge = p_real - float(p_mkt)

        # dev
        dev = _dev_best_vs_median(odds, median_odds)
        if dev < cfg.min_dev_percent:
            pass  # keep (0 by default)

        # discipline: EV >= 0
        if cfg.require_positive_ev and ev < 0.0:
            continue

        score = _score_offer(market=market, ev=ev, edge=edge, dev=dev, overround_est=ov, cfg=cfg)

        markets_tested += 1
        candidates.append(
            {
                "game_id": o["game_id"],
                "match": o["match"],
                "home_team": home,
                "away_team": away,
                "market": market,
                "selection": selection,
                "line": line,
                "odds": odds,
                "book": o.get("book") or "?",
                "books": books_count,
                "median_odds": median_odds,
                "p_model": float(p_model),
                "p_mkt": float(p_mkt),
                "fair_prob": float(p_real),  # p_real final
                "ev": float(ev),
                "edge": float(edge),
                "dev": float(dev),
                "score": float(score),
            }
        )

    # sort by score descending
    candidates.sort(key=lambda x: (x["score"], x["ev"], x["dev"]), reverse=True)

    # selection with constraints: max ML, 1 pick/match if possible
    picks: List[Dict[str, Any]] = []
    used_games: set[str] = set()
    ml_count = 0

    for c in candidates:
        if len(picks) >= cfg.top_n_team:
            break

        if cfg.one_pick_per_match and c["game_id"] in used_games:
            continue

        if c["market"] == "MONEYLINE":
            if ml_count >= cfg.max_ml_per_day:
                continue
            ml_count += 1

        picks.append(c)
        used_games.add(c["game_id"])

    meta = {
        "games": len(games),
        "offers": len(offers),
        "candidates": len(candidates),
        "markets_tested": markets_tested,
        "model_weight": cfg.model_weight,
        "clip_vs_market": cfg.clip_vs_market,
        "max_ml_per_day": cfg.max_ml_per_day,
        "max_odds_ml": cfg.max_odds_ml,
        "features_loaded": len(load_team_features(cfg.team_features_path)),
        "features_path": cfg.team_features_path,
    }

    return picks, meta


def analyze_props_slate(_: List[Dict[str, Any]], __: Config) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    # Props not wired yet (OddsAPI props require specific plan / endpoints).
    return [], {"props_status": "not_wired"}


# -----------------------------
# Public entrypoint
# -----------------------------

def run_engine(games: List[Dict[str, Any]], cfg: Config) -> Dict[str, Any]:
    team_picks, meta_team = analyze_team_slate(games, cfg)
    prop_picks, meta_props = analyze_props_slate(games, cfg)

    meta: Dict[str, Any] = {}
    meta.update(meta_team)
    meta.update(meta_props)

    return {
        "team_picks": team_picks,
        "prop_picks": prop_picks,
        "meta": meta,
    }
