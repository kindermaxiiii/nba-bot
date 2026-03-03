# engine.py
from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Your model files
from model_team import model_prob_for_team_market

try:
    from model_props import model_prob_for_prop_market  # optional
except Exception:
    model_prob_for_prop_market = None


# -------------------------
# Helpers
# -------------------------
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def implied_prob_from_decimal(odds: float) -> Optional[float]:
    try:
        o = float(odds)
        if o <= 1.0:
            return None
        return 1.0 / o
    except Exception:
        return None


def no_vig_two_way(p_a: float, p_b: float) -> Tuple[float, float]:
    s = p_a + p_b
    if s <= 0:
        return 0.5, 0.5
    return p_a / s, p_b / s


def median(xs: List[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    try:
        return float(statistics.median(xs))
    except Exception:
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2]


def stdev(xs: List[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    if len(xs) < 2:
        return None
    try:
        return float(statistics.pstdev(xs))
    except Exception:
        return None


def dev_vs_median(best_odds: float, med_odds: float) -> float:
    if med_odds <= 0:
        return 0.0
    return max(0.0, float(best_odds) / float(med_odds) - 1.0)


def score_pick(
    ev: float,
    edge: float,
    dev: float,
    odds: float,
    market: str,
    is_ml: bool,
) -> float:
    """
    Score designed to:
    - favor EV/edge
    - reward some dev (line shopping) but not too much
    - strongly penalize huge ML odds (the "ML at 6" problem)
    """
    # Base from EV and edge (cap to avoid crazy numbers)
    ev_c = _clamp(ev, -0.10, 0.25)
    edge_c = _clamp(edge, -0.05, 0.10)

    # dev reward but capped
    dev_c = _clamp(dev, 0.0, 0.08)

    s = 0.0
    s += 520.0 * max(0.0, ev_c)     # EV is king
    s += 220.0 * max(0.0, edge_c)   # edge secondary
    s += 80.0 * dev_c               # small bonus for best vs median

    # Market preference: spreads/totals around ~1.90 are "logical"
    # Penalize odds far from 1.90
    try:
        o = float(odds)
        s -= 70.0 * min(1.5, abs(o - 1.90))
    except Exception:
        pass

    # Hard penalty for ML longshots
    if is_ml:
        try:
            o = float(odds)
            if o >= 3.0:
                s -= 220.0
            if o >= 4.0:
                s -= 320.0
            if o >= 5.0:
                s -= 420.0
        except Exception:
            pass

    # Small preference for non-ML
    if not is_ml:
        s += 25.0

    return float(_clamp(s, 0.0, 100.0))


# -------------------------
# Config (safe defaults)
# -------------------------
@dataclass
class EngineConfig:
    # Markets
    markets: List[str]
    regions_priority: List[str]

    # Model/market blending & calibration
    model_weight: float = 0.75          # p_model weight in blend
    clip_vs_market: float = 0.08        # |p_real - p_mkt| <= 8%

    # Selection rules
    ev_min: float = 0.0                 # EV must be >= 0
    max_ml_per_day: int = 2
    max_odds_ml: float = 3.25           # refuse ML if odds > this (unless you change)
    prefer_not_same_match_team: bool = True

    # Outputs
    top_n_team: int = 3
    top_n_props: int = 3


def load_config(path: str = "config.json") -> EngineConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Existing config.json might not have these keys; safe defaults
    return EngineConfig(
        markets=raw.get("MARKETS", ["h2h", "spreads", "totals"]),
        regions_priority=raw.get("REGIONS_PRIORITY", ["us", "us2"]),
        model_weight=float(raw.get("MODEL_WEIGHT", 0.75)),
        clip_vs_market=float(raw.get("CLIP_VS_MARKET", 0.08)),
        ev_min=float(raw.get("EV_MIN", 0.0)),
        max_ml_per_day=int(raw.get("MAX_ML_PER_DAY", 2)),
        max_odds_ml=float(raw.get("MAX_ODDS_ML", 3.25)),
        prefer_not_same_match_team=bool(raw.get("PREFER_NOT_SAME_MATCH_TEAM", True)),
        top_n_team=int(raw.get("TOP_N_TEAM", 3)),
        top_n_props=int(raw.get("TOP_N_PROPS", 3)),
    )


def load_team_features(path: str = "data/team_features.json") -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or {}


# -------------------------
# Odds collection (same structure as your odds_api output)
# -------------------------
def collect_market_lines(game: Dict[str, Any], market_key: str) -> List[Dict[str, Any]]:
    """
    Returns list of entries:
    {book, odds, selection, line(optional)}
    """
    out: List[Dict[str, Any]] = []
    for bm in game.get("bookmakers", []) or []:
        book = bm.get("title") or bm.get("key") or "Unknown"
        for m in bm.get("markets", []) or []:
            if m.get("key") != market_key:
                continue
            for oc in m.get("outcomes", []) or []:
                entry = {
                    "book": book,
                    "selection": oc.get("name"),
                    "odds": oc.get("price"),
                }
                if "point" in oc:
                    entry["line"] = oc.get("point")
                out.append(entry)
    return out


def analyze_two_way_market(
    match: str,
    market_label: str,
    away_team: str,
    home_team: str,
    lines: List[Dict[str, Any]],
    features: Dict[str, Dict[str, Any]],
    cfg: EngineConfig,
) -> List[Dict[str, Any]]:
    """
    Build candidates for a two-way market (ML/spread/total).
    Returns list of candidate pick dicts (one per side).
    """
    # group by selection (+ line where relevant)
    buckets: Dict[Tuple[str, Optional[float]], List[Dict[str, Any]]] = {}
    for r in lines:
        sel = r.get("selection")
        if sel is None:
            continue
        line = r.get("line", None)
        key = (str(sel), float(line) if line is not None else None)
        buckets.setdefault(key, []).append(r)

    candidates: List[Dict[str, Any]] = []

    for (sel, line), rows in buckets.items():
        odds_list = []
        for rr in rows:
            o = rr.get("odds")
            if o is None:
                continue
            try:
                odds_list.append(float(o))
            except Exception:
                pass

        if not odds_list:
            continue

        best_odds = max(odds_list)
        med_odds = median(odds_list) or best_odds
        dev = dev_vs_median(best_odds, med_odds)

        # market fair prob from median odds (no-vig computed via pairing when possible)
        p_imp_sel = implied_prob_from_decimal(med_odds)
        if p_imp_sel is None:
            continue

        # For proper no-vig we need both sides at same "line".
        # We'll attempt: find the opposite side entry at same line.
        opp_key = None
        if market_label.startswith("TOTAL"):
            opp_key = ("Under" if sel == "Over" else "Over", line)
        elif market_label.startswith("SPREAD"):
            # Opp side is the other team with negated line
            if sel == home_team:
                opp_key = (away_team, -float(line) if line is not None else None)
            elif sel == away_team:
                opp_key = (home_team, -float(line) if line is not None else None)
        else:
            # ML: opposite is other team
            if sel == home_team:
                opp_key = (away_team, None)
            elif sel == away_team:
                opp_key = (home_team, None)

        p_mkt = p_imp_sel
        if opp_key and opp_key in buckets:
            opp_odds_list = []
            for rr in buckets[opp_key]:
                o = rr.get("odds")
                if o is None:
                    continue
                try:
                    opp_odds_list.append(float(o))
                except Exception:
                    pass
            opp_med = median(opp_odds_list)
            if opp_med:
                p_imp_opp = implied_prob_from_decimal(opp_med)
                if p_imp_opp is not None:
                    p_sel_nv, _ = no_vig_two_way(p_imp_sel, p_imp_opp)
                    p_mkt = p_sel_nv

        # MODEL probability
        p_model = model_prob_for_team_market(
            market=market_label,
            selection=sel,
            line=line,
            away_team=away_team,
            home_team=home_team,
            features=features,
        )

        if p_model is None:
            # If model can't compute, do NOT invent alpha: fallback to market fair
            p_model = p_mkt

        # Blend + calibration clip
        w = _clamp(cfg.model_weight, 0.0, 1.0)
        p_blend = w * float(p_model) + (1.0 - w) * float(p_mkt)

        # Clip vs market (institutional discipline)
        d = float(p_blend) - float(p_mkt)
        d = _clamp(d, -cfg.clip_vs_market, cfg.clip_vs_market)
        p_real = _clamp(float(p_mkt) + d, 0.01, 0.99)

        ev = p_real * float(best_odds) - 1.0
        edge = p_real - float(p_mkt)

        is_ml = market_label.startswith("MONEYLINE")

        # Hard anti-ML longshot
        if is_ml and float(best_odds) > float(cfg.max_odds_ml):
            continue

        if ev < cfg.ev_min:
            continue

        s = score_pick(ev=ev, edge=edge, dev=dev, odds=best_odds, market=market_label, is_ml=is_ml)

        # choose the best book for best_odds
        best_book = "Unknown"
        for rr in rows:
            try:
                if float(rr.get("odds")) == float(best_odds):
                    best_book = rr.get("book") or "Unknown"
                    break
            except Exception:
                pass

        candidates.append(
            {
                "match": match,
                "market": market_label,
                "selection": sel,
                "line": line,
                "odds": float(best_odds),
                "book": best_book,
                "median_odds": float(med_odds),
                "dev": float(dev),
                "p_model": float(p_model),
                "p_mkt": float(p_mkt),
                "fair_prob": float(p_real),
                "edge": float(edge),
                "ev": float(ev),
                "score": float(s),
                "is_ml": bool(is_ml),
            }
        )

    # return sorted by score desc
    candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return candidates


def analyze_team_slate(games: List[Dict[str, Any]], features: Dict[str, Dict[str, Any]], cfg: EngineConfig) -> List[Dict[str, Any]]:
    all_candidates: List[Dict[str, Any]] = []

    for g in games:
        away = g.get("away_team")
        home = g.get("home_team")
        if not away or not home:
            continue
        match = f"{away} @ {home}"

        # Moneyline
        ml_lines = collect_market_lines(g, "h2h")
        if ml_lines:
            all_candidates += analyze_two_way_market(
                match=match,
                market_label="MONEYLINE",
                away_team=away,
                home_team=home,
                lines=ml_lines,
                features=features,
                cfg=cfg,
            )

        # Spread
        sp_lines = collect_market_lines(g, "spreads")
        if sp_lines:
            all_candidates += analyze_two_way_market(
                match=match,
                market_label="SPREAD",
                away_team=away,
                home_team=home,
                lines=sp_lines,
                features=features,
                cfg=cfg,
            )

        # Total
        tot_lines = collect_market_lines(g, "totals")
        if tot_lines:
            # outcomes are Over/Under names
            all_candidates += analyze_two_way_market(
                match=match,
                market_label="TOTAL",
                away_team=away,
                home_team=home,
                lines=tot_lines,
                features=features,
                cfg=cfg,
            )

    # Build TOP3 with rules:
    # - max 2 ML
    # - avoid same match if possible
    picks: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    for c in sorted(all_candidates, key=lambda x: x["score"], reverse=True):
        if len(picks) >= cfg.top_n_team:
            break
        if c.get("is_ml") and ml_count >= cfg.max_ml_per_day:
            continue

        if cfg.prefer_not_same_match_team and c["match"] in used_matches:
            continue

        picks.append(c)
        used_matches.add(c["match"])
        if c.get("is_ml"):
            ml_count += 1

    # If we couldn't fill because of "no same match", relax that rule to fill remaining
    if len(picks) < cfg.top_n_team:
        for c in sorted(all_candidates, key=lambda x: x["score"], reverse=True):
            if len(picks) >= cfg.top_n_team:
                break
            if c in picks:
                continue
            if c.get("is_ml") and ml_count >= cfg.max_ml_per_day:
                continue
            picks.append(c)
            if c.get("is_ml"):
                ml_count += 1

    return picks


def analyze_prop_slate(games: List[Dict[str, Any]], cfg: EngineConfig) -> List[Dict[str, Any]]:
    """
    Props require your model_props.py.
    If absent, returns [].
    """
    if model_prob_for_prop_market is None:
        return []

    # If your odds_api does not fetch props yet, keep empty.
    # (You can later wire player props collection here.)
    return []


def run_engine(games: List[Dict[str, Any]], cfg: EngineConfig) -> Dict[str, Any]:
    features = load_team_features("data/team_features.json")

    team_picks = analyze_team_slate(games, features, cfg)
    prop_picks = analyze_prop_slate(games, cfg)

    return {
        "team_picks": team_picks,
        "prop_picks": prop_picks,
        "meta": {
            "games": len(games),
            "model_weight": cfg.model_weight,
            "clip_vs_market": cfg.clip_vs_market,
            "max_ml_per_day": cfg.max_ml_per_day,
            "max_odds_ml": cfg.max_odds_ml,
        },
    }
