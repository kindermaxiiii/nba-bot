# engine.py
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from model_team import model_prob_for_team_market


# -------------------------
# Config
# -------------------------

@dataclass
class Config:
    regions_priority: List[str]
    markets: List[str]

    # calibration / discipline
    model_weight: float = 0.7          # blend after clipping for stability
    clip_vs_market: float = 0.08       # max deviation allowed vs market

    # user constraints
    min_odds: float = 1.5
    max_odds: float = 2.2

    # portfolio constraints
    max_ml_per_day: int = 1
    max_odds_ml: float = 2.2

    min_edge: float = 0.02
    min_ev: float = 0.0

    max_picks_team: int = 3
    max_picks_props: int = 3

    # robustness controls
    max_overround: float = 0.15
    kelly_penalty_overround: float = 0.25  # applied when overround > 10%


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    return Config(
        regions_priority=list(raw.get("regions_priority") or []),
        markets=list(raw.get("markets") or []),
        model_weight=float(raw.get("model_weight", 0.7)),
        clip_vs_market=float(raw.get("clip_vs_market", 0.08)),
        min_odds=float(raw.get("min_odds", 1.5)),
        max_odds=float(raw.get("max_odds", 2.2)),
        max_ml_per_day=int(raw.get("max_ml_per_day", 1)),
        max_odds_ml=float(raw.get("max_odds_ml", 2.2)),
        min_edge=float(raw.get("min_edge", 0.02)),
        min_ev=float(raw.get("min_ev", 0.0)),
        max_picks_team=int(raw.get("max_picks_team", 3)),
        max_picks_props=int(raw.get("max_picks_props", 3)),
        max_overround=float(raw.get("max_overround", 0.15)),
        kelly_penalty_overround=float(raw.get("kelly_penalty_overround", 0.25)),
    )


# -------------------------
# Helpers
# -------------------------


def _clamp(x: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, float(x)))


def _implied_prob(odds_decimal: float) -> Optional[float]:
    try:
        o = float(odds_decimal)
        if o <= 1.0:
            return None
        return 1.0 / o
    except Exception:
        return None


def _median_or_none(xs: List[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    return float(statistics.median(xs))


def _mode_or_median(xs: List[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    # mode with tolerance (book lines can differ by 0.5)
    buckets: Dict[float, int] = {}
    for v in xs:
        k = round(v * 2) / 2.0
        buckets[k] = buckets.get(k, 0) + 1
    best = max(buckets.items(), key=lambda kv: kv[1])[0]
    return float(best)


def _market_key_to_internal(market_key: str) -> Optional[str]:
    k = (market_key or "").lower()
    if k in ("h2h", "moneyline"):
        return "MONEYLINE"
    if k in ("spreads", "spread"):
        return "SPREAD"
    if k in ("totals", "total"):
        return "TOTAL"
    # 1H (OddsAPI naming varies)
    if k in ("h2h_h1", "h2h_1h", "moneyline_h1"):
        return "MONEYLINE 1H"
    if k in ("spreads_h1", "spreads_1h"):
        return "SPREAD 1H"
    if k in ("totals_h1", "totals_1h"):
        return "TOTAL 1H"
    return None


def _no_vig_two_way(p1: float, p2: float) -> Tuple[float, float, float]:
    """Return (p1_nv, p2_nv, overround)."""
    s = p1 + p2
    if s <= 0:
        return 0.5, 0.5, 0.0
    return p1 / s, p2 / s, (s - 1.0)


def _haircut_edge_ev(edge: float, ev: float) -> Tuple[float, float, List[str]]:
    """Institutional haircuts on suspiciously large edges."""
    reasons: List[str] = []
    e = float(edge)
    v = float(ev)

    if e > 0.06:
        reasons.append("haircut_edge>6%: -30%")
        e *= 0.70
        v *= 0.70
    # if still very large, flag or refuse
    if e > 0.10:
        reasons.append("flag_edge>10%")
    if e > 0.15:
        reasons.append("refuse_edge>15%")
    return e, v, reasons


def _score_candidate(ev: float, edge: float, dev: float, overround: float, market: str) -> float:
    # Prefer spreads/totals over ML by default ("logical" / less longshot)
    market_bonus = 2.0 if market in ("SPREAD", "TOTAL", "SPREAD 1H", "TOTAL 1H") else 0.0
    # Penalize dev and high overround
    return (100.0 * ev) + (50.0 * edge) - (40.0 * dev) - (30.0 * max(0.0, overround)) + market_bonus


# -------------------------
# Loading team features
# -------------------------


def load_team_features(path: str = "data/team_features.json") -> Dict[str, Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): (v if isinstance(v, dict) else {}) for k, v in data.items()}
        return {}
    except Exception:
        return {}


# -------------------------
# Main engine
# -------------------------


def analyze_team_slate(games: List[Dict[str, Any]], cfg: Config) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    features = load_team_features("data/team_features.json")

    candidates: List[Dict[str, Any]] = []
    markets_tested = 0

    for g in games:
        home = g.get("home_team")
        away = g.get("away_team")
        if not home or not away:
            continue

        bookmakers = g.get("bookmakers") or []
        if not isinstance(bookmakers, list) or not bookmakers:
            continue

        # Collect quotes per market
        for bm in bookmakers:
            bm_title = bm.get("title") or bm.get("key") or "?"
            for m in (bm.get("markets") or []):
                mk = m.get("key")
                internal = _market_key_to_internal(mk)
                if internal is None:
                    continue
                if cfg.markets and mk not in cfg.markets:
                    # only use markets requested in config
                    continue

                outcomes = m.get("outcomes") or []
                if not isinstance(outcomes, list) or len(outcomes) < 2:
                    continue

                markets_tested += 1

                # Parse two-way market probs (no-vig per book)
                # OddsAPI gives decimal odds in "price".
                # For spreads, each outcome has "point".
                o1, o2 = outcomes[0], outcomes[1]
                p1 = _implied_prob(o1.get("price"))
                p2 = _implied_prob(o2.get("price"))
                if p1 is None or p2 is None:
                    continue
                p1_nv, p2_nv, overround = _no_vig_two_way(p1, p2)

                # Skip absurd overround markets
                if overround > cfg.max_overround:
                    continue

                # Add each side as a quote
                for o, p_nv in ((o1, p1_nv), (o2, p2_nv)):
                    sel = o.get("name")
                    odds = o.get("price")
                    line = o.get("point") if "point" in o else None
                    if sel is None or odds is None:
                        continue

                    # Store quotes
                    candidates.append(
                        {
                            "match": f"{away} @ {home}",
                            "home_team": home,
                            "away_team": away,
                            "market_key": mk,
                            "market": internal,
                            "selection": sel,
                            "line": line,
                            "odds": float(odds),
                            "book": str(bm_title),
                            "p_mkt": float(p_nv),
                            "overround": float(overround),
                        }
                    )

    # If no candidates at all, return
    if not candidates:
        return [], {
            "games": len(games),
            "markets_tested": markets_tested,
            "model_weight": cfg.model_weight,
            "clip_vs_market": cfg.clip_vs_market,
            "max_ml_per_day": cfg.max_ml_per_day,
            "max_odds_ml": cfg.max_odds_ml,
            "reason": "no_market_candidates",
        }

    # Consensus line selection (avoid picking 1.01 mislines)
    # For each (match, market, selection), pick most common line, then best odds for that line.
    by_key: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for c in candidates:
        k = (c["match"], c["market"], c["selection"])
        by_key.setdefault(k, []).append(c)

    best_quotes: List[Dict[str, Any]] = []
    for (match, market, sel), qs in by_key.items():
        lines = [q.get("line") for q in qs if q.get("line") is not None]
        target_line = _mode_or_median(lines) if lines else None

        # Filter to target line when applicable
        if target_line is not None:
            qs2 = [q for q in qs if q.get("line") is not None and abs(float(q.get("line")) - float(target_line)) < 1e-9]
            if qs2:
                qs = qs2

        # Best odds within allowed range
        qs = sorted(qs, key=lambda x: float(x.get("odds", 0.0)), reverse=True)
        picked = None
        for q in qs:
            o = float(q["odds"])
            if o < cfg.min_odds or o > cfg.max_odds:
                continue
            # Additional ML guard
            if q["market"] == "MONEYLINE" and o > cfg.max_odds_ml:
                continue
            picked = q
            break
        if picked:
            best_quotes.append(picked)

    # Compute model probs and run discipline filters
    final: List[Dict[str, Any]] = []
    for q in best_quotes:
        market = q["market"]
        sel = q["selection"]
        line = q.get("line")
        home = q["home_team"]
        away = q["away_team"]

        p_mkt = float(q["p_mkt"])

        p_model_raw = model_prob_for_team_market(
            market=market,
            selection=sel,
            line=float(line) if line is not None else None,
            away_team=away,
            home_team=home,
            features=features,
        )
        if p_model_raw is None:
            continue
        p_model_raw = _clamp(float(p_model_raw))

        # Clip vs market
        delta = p_model_raw - p_mkt
        delta_clipped = max(-cfg.clip_vs_market, min(cfg.clip_vs_market, delta))
        p_model = _clamp(p_mkt + delta_clipped)

        # Final p_real (stabilized)
        p_real = _clamp(cfg.model_weight * p_model + (1.0 - cfg.model_weight) * p_mkt)

        odds = float(q["odds"])
        ev = p_real * odds - 1.0
        edge = p_real - p_mkt
        dev = abs(p_model_raw - p_mkt)

        # Haircuts
        edge2, ev2, haircut_reasons = _haircut_edge_ev(edge, ev)
        if "refuse_edge>15%" in haircut_reasons:
            continue

        # Base discipline filters
        if ev2 < cfg.min_ev:
            continue
        if edge2 < cfg.min_edge:
            continue

        # Overround penalty on score
        overround = float(q.get("overround", 0.0))

        score = _score_candidate(ev2, edge2, dev, overround, market)

        final.append(
            {
                "match": q["match"],
                "market": market if market != "MONEYLINE" else "H2H",  # display friendly
                "selection": sel,
                "line": float(line) if line is not None else None,
                "odds": odds,
                "book": q["book"],
                "p_model": p_model,
                "p_mkt": p_mkt,
                "fair_prob": p_real,
                "ev": ev2,
                "edge": edge2,
                "dev": dev,
                "overround": overround,
                "haircuts": haircut_reasons,
                "score": score,
            }
        )

    # Deduplicate by match+market+selection; keep best score
    uniq: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for p in final:
        k = (p["match"], p["market"], p["selection"])
        if k not in uniq or float(p["score"]) > float(uniq[k]["score"]):
            uniq[k] = p
    final = list(uniq.values())

    # Sort by score descending
    final.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)

    # Portfolio construction: 1 pick per match, prefer non-ML, cap ML count
    picks: List[Dict[str, Any]] = []
    used_matches: set[str] = set()
    ml_count = 0

    # First pass: non-ML
    for p in final:
        if len(picks) >= cfg.max_picks_team:
            break
        if p["match"] in used_matches:
            continue
        if p["market"] == "H2H":
            continue
        picks.append(p)
        used_matches.add(p["match"])

    # Second pass: ML (only if still short)
    if len(picks) < cfg.max_picks_team:
        for p in final:
            if len(picks) >= cfg.max_picks_team:
                break
            if p["match"] in used_matches:
                continue
            if p["market"] != "H2H":
                continue
            if ml_count >= cfg.max_ml_per_day:
                continue
            if float(p["odds"]) > cfg.max_odds_ml:
                continue
            picks.append(p)
            used_matches.add(p["match"])
            ml_count += 1

    meta = {
        "games": len(games),
        "markets_tested": markets_tested,
        "model_weight": cfg.model_weight,
        "clip_vs_market": cfg.clip_vs_market,
        "max_ml_per_day": cfg.max_ml_per_day,
        "max_odds_ml": cfg.max_odds_ml,
        "min_odds": cfg.min_odds,
        "max_odds": cfg.max_odds,
        "min_edge": cfg.min_edge,
        "min_ev": cfg.min_ev,
    }

    return picks, meta


def run_engine(games: List[Dict[str, Any]], cfg: Config) -> Dict[str, Any]:
    team_picks, meta = analyze_team_slate(games, cfg)

    # Props not implemented yet (depends on available props markets / player model ingest)
    prop_picks: List[Dict[str, Any]] = []

    return {"team_picks": team_picks, "prop_picks": prop_picks, "meta": meta}
