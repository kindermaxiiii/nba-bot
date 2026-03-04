# engine.py
from __future__ import annotations

from typing import Any, Dict, List, Tuple, Optional
import math

from utils import dec_to_prob, implied_prob_no_vig_two_way


CORE_MARKETS = {"h2h", "spreads", "totals"}


def _best_two_way_prices(outcomes: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    if not outcomes or len(outcomes) < 2:
        return None
    a, b = outcomes[0], outcomes[1]
    if "price" not in a or "price" not in b:
        return None
    return a, b


def _overround_two_way(p1: float, p2: float) -> float:
    return (p1 + p2) - 1.0


def build_team_candidates(games: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, float]]:
    """
    Returns:
      candidates: list of evaluated selections
      meta: diagnostics
      spread_map: match -> best_abs_spread (used by props engine to estimate blowout fragility)
    """
    candidates: List[Dict[str, Any]] = []
    spread_map: Dict[str, float] = {}

    min_odds = float(cfg.get("min_odds", 1.5))
    max_odds = float(cfg.get("max_odds", 2.2))
    max_overround = float(cfg.get("max_overround", 0.15))
    kelly_penalty_overround = float(cfg.get("kelly_penalty_overround", 0.25))

    model_w = float(cfg.get("model_weight", 0.7))
    clip = float(cfg.get("clip_vs_market", 0.08))
    min_edge = float(cfg.get("min_edge", 0.02))
    min_ev = float(cfg.get("min_ev", 0.0))

    # Very simple team model placeholder: p_model = 0.5 for all.
    # (You can replace with a real team model later; the portfolio discipline stays valid.)
    def p_model_stub() -> float:
        return 0.50

    for g in games:
        match = f"{g.get('away_team')} @ {g.get('home_team')}"
        for bm in (g.get("bookmakers") or []):
            book = bm.get("title") or bm.get("key") or "book"
            for mk in (bm.get("markets") or []):
                mkey = mk.get("key")
                if mkey not in CORE_MARKETS:
                    continue

                outcomes = mk.get("outcomes") or []
                pair = _best_two_way_prices(outcomes)
                if not pair:
                    continue
                o1, o2 = pair
                odds1 = float(o1["price"])
                odds2 = float(o2["price"])

                if not (min_odds <= odds1 <= max_odds and min_odds <= odds2 <= max_odds):
                    # allow e.g. spreads/totals typically ~1.91, ok
                    pass

                p1_imp = dec_to_prob(odds1)
                p2_imp = dec_to_prob(odds2)
                overround = _overround_two_way(p1_imp, p2_imp)

                if overround > max_overround:
                    continue

                # no-vig
                p1_nv, p2_nv = implied_prob_no_vig_two_way(odds1, odds2)

                # record spread magnitude for blowout proxy
                if mkey == "spreads":
                    try:
                        # outcomes include "point" per side
                        pt1 = float(o1.get("point"))
                        pt2 = float(o2.get("point"))
                        spread_map[match] = min(abs(pt1), abs(pt2))
                    except Exception:
                        pass

                for outcome, p_mkt in [(o1, p1_nv), (o2, p2_nv)]:
                    selection = str(outcome.get("name"))
                    odds = float(outcome.get("price"))

                    if not (min_odds <= odds <= max_odds):
                        continue

                    p_model = p_model_stub()

                    # Clip to market if too far (discipline)
                    dev = abs(p_model - p_mkt)
                    if dev > clip:
                        p_model = p_mkt + (clip if p_model > p_mkt else -clip)

                    p_real = model_w * p_model + (1.0 - model_w) * p_mkt
                    edge = p_real - p_mkt
                    ev = p_real * odds - 1.0

                    if edge < min_edge or ev <= min_ev:
                        continue

                    # Simple score: EV + edge, penalize overround
                    score = (100.0 * ev) + (50.0 * edge) - (50.0 * max(0.0, overround) * kelly_penalty_overround)

                    candidates.append({
                        "match": match,
                        "market": mkey.upper(),
                        "selection": selection,
                        "odds": odds,
                        "book": book,
                        "p_model": p_model,
                        "p_mkt": p_mkt,
                        "p_real": p_real,
                        "ev": ev,
                        "edge": edge,
                        "dev": dev,
                        "score": score,
                        "why": "Team stub (0.50) + market clipping + discipline filters",
                    })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    meta = {
        "games": len(games),
        "team_candidates": len(candidates),
        "markets_seen": sorted({c["market"] for c in candidates}),
    }
    return candidates, meta, spread_map
