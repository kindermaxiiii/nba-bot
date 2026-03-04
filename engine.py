# engine.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

from utils import dec_to_prob, implied_prob_no_vig_two_way, clamp, now_iso
from model_team import team_p_model


def _median(xs: List[float]) -> Optional[float]:
    xs = sorted([x for x in xs if x is not None])
    if not xs:
        return None
    n = len(xs)
    if n % 2 == 1:
        return float(xs[n // 2])
    return (float(xs[n//2 - 1]) + float(xs[n//2])) / 2.0


def _collect_market(game: Dict[str, Any], market_key: str) -> Dict[str, Any]:
    """
    Build consensus line structure from bookmakers.
    For spreads: consensus abs(point).
    For totals: consensus point.
    For h2h: no line.
    """
    bms = game.get("bookmakers") or []
    lines_count: Dict[str, int] = {}
    by_line: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for bm in bms:
        book = bm.get("title") or bm.get("key") or "book"
        for mk in (bm.get("markets") or []):
            if mk.get("key") != market_key:
                continue
            for o in (mk.get("outcomes") or []):
                name = o.get("name")
                price = o.get("price")
                point = o.get("point")
                if name is None or price is None:
                    continue
                if market_key == "h2h":
                    lk = "h2h"
                elif market_key == "totals":
                    if point is None:
                        continue
                    lk = str(float(point))
                elif market_key == "spreads":
                    if point is None:
                        continue
                    lk = str(abs(float(point)))
                else:
                    continue

                by_line.setdefault(lk, {}).setdefault(str(name), []).append({
                    "price": float(price),
                    "point": float(point) if point is not None else None,
                    "book": str(book),
                })
                lines_count[lk] = lines_count.get(lk, 0) + 1

    if not lines_count:
        return {}
    consensus = max(lines_count.items(), key=lambda kv: kv[1])[0]
    return {"line_key": consensus, "sides": by_line.get(consensus, {})}


def _best_odds(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entries:
        return None
    return max(entries, key=lambda e: e.get("price", 0.0))


def _signed_spread_for_team(sides: Dict[str, List[Dict[str, Any]]], team: str) -> Optional[float]:
    # return a signed point from any entry
    lst = sides.get(team) or []
    for e in lst:
        if e.get("point") is not None:
            return float(e["point"])
    return None


def _clip(p_model: float, p_mkt: float, clip: float) -> float:
    d = p_model - p_mkt
    if d > clip:
        return p_mkt + clip
    if d < -clip:
        return p_mkt - clip
    return p_model


def _haircut(p_real: float, p_mkt: float, trigger: float, rate: float) -> Tuple[float, float]:
    edge_raw = p_real - p_mkt
    if edge_raw <= trigger:
        return p_real, edge_raw
    p_adj = p_mkt + (p_real - p_mkt) * (1.0 - rate)
    return p_adj, (p_adj - p_mkt)


def _score(edge: float, ev: float, dev: float) -> float:
    # 0..100-ish
    return max(0.0, min(100.0, 100.0*ev + 60.0*edge + 10.0*dev))


def build_team_candidates(games: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, float]]:
    candidates: List[Dict[str, Any]] = []
    spread_map: Dict[str, float] = {}

    min_odds = float(cfg.get("min_odds", 1.5))
    max_odds = float(cfg.get("max_odds", 2.2))
    clip = float(cfg.get("clip_vs_market", 0.08))
    model_w = float(cfg.get("model_weight", 0.75))
    min_edge = float(cfg.get("min_edge", 0.02))
    min_ev = float(cfg.get("min_ev", 0.0))
    edge_refuse = float(cfg.get("edge_refuse", 0.15))
    haircut_trigger = float(cfg.get("haircut_trigger", 0.06))
    haircut_rate = float(cfg.get("haircut_rate", 0.30))

    for g in games:
        home = g.get("home_team"); away = g.get("away_team")
        if not home or not away:
            continue
        match = f"{away} @ {home}"

        # H2H
        h2h = _collect_market(g, "h2h")
        if h2h:
            sides = h2h["sides"]
            if home in sides and away in sides:
                med_home = _median([e["price"] for e in sides[home]])
                med_away = _median([e["price"] for e in sides[away]])
                if med_home and med_away:
                    pH, pA = implied_prob_no_vig_two_way(med_home, med_away)
                    for team, med, p_mkt in [(home, med_home, pH), (away, med_away, pA)]:
                        best = _best_odds(sides[team])
                        if not best:
                            continue
                        odds = float(best["price"])
                        if not (min_odds <= odds <= max_odds):
                            continue
                        p_model = team_p_model("H2H", team, None, away, home)
                        if p_model is None:
                            continue
                        p_model = _clip(float(p_model), float(p_mkt), clip)
                        p_real = model_w * p_model + (1.0 - model_w) * float(p_mkt)
                        p_real, edge = _haircut(p_real, float(p_mkt), haircut_trigger, haircut_rate)
                        if edge > edge_refuse:
                            continue
                        ev = p_real * odds - 1.0
                        if ev <= min_ev or edge < min_edge:
                            continue
                        dev = (odds - float(med)) / float(med) if med > 0 else 0.0
                        score = _score(edge, ev, dev)
                        candidates.append({
                            "match": match,
                            "market": "H2H",
                            "selection": team,
                            "line": None,
                            "odds": odds,
                            "book": best["book"],
                            "p_model": float(p_model),
                            "p_mkt": float(p_mkt),
                            "p_real": float(p_real),
                            "edge": float(edge),
                            "ev": float(ev),
                            "dev": float(dev),
                            "score": float(score),
                            "why": "Model-first ML (margin prior) calibrated (clip+haircut).",
                        })

        # TOTALS
        tot = _collect_market(g, "totals")
        if tot:
            line = float(tot["line_key"])
            sides = tot["sides"]
            if "Over" in sides and "Under" in sides:
                med_o = _median([e["price"] for e in sides["Over"]])
                med_u = _median([e["price"] for e in sides["Under"]])
                if med_o and med_u:
                    pO, pU = implied_prob_no_vig_two_way(med_o, med_u)
                    for sel, med, p_mkt in [("Over", med_o, pO), ("Under", med_u, pU)]:
                        best = _best_odds(sides[sel])
                        if not best:
                            continue
                        odds = float(best["price"])
                        if not (min_odds <= odds <= max_odds):
                            continue
                        p_model = team_p_model("TOTAL", sel, line, away, home)
                        if p_model is None:
                            continue
                        p_model = _clip(float(p_model), float(p_mkt), clip)
                        p_real = model_w * p_model + (1.0 - model_w) * float(p_mkt)
                        p_real, edge = _haircut(p_real, float(p_mkt), haircut_trigger, haircut_rate)
                        if edge > edge_refuse:
                            continue
                        ev = p_real * odds - 1.0
                        if ev <= min_ev or edge < min_edge:
                            continue
                        dev = (odds - float(med)) / float(med) if med > 0 else 0.0
                        score = _score(edge, ev, dev)
                        candidates.append({
                            "match": match,
                            "market": "TOTAL",
                            "selection": sel,
                            "line": line,
                            "odds": odds,
                            "book": best["book"],
                            "p_model": float(p_model),
                            "p_mkt": float(p_mkt),
                            "p_real": float(p_real),
                            "edge": float(edge),
                            "ev": float(ev),
                            "dev": float(dev),
                            "score": float(score),
                            "why": "Model-first total (pace+ratings) calibrated (clip+haircut).",
                        })

        # SPREADS
        spr = _collect_market(g, "spreads")
        if spr:
            line_abs = float(spr["line_key"])
            sides = spr["sides"]
            if home in sides and away in sides:
                med_home = _median([e["price"] for e in sides[home]])
                med_away = _median([e["price"] for e in sides[away]])
                if med_home and med_away:
                    pH, pA = implied_prob_no_vig_two_way(med_home, med_away)

                    signed_home = _signed_spread_for_team(sides, home)
                    signed_away = _signed_spread_for_team(sides, away)
                    if signed_home is not None:
                        spread_map[match] = abs(float(signed_home))

                    for team, med, p_mkt in [(home, med_home, pH), (away, med_away, pA)]:
                        best = _best_odds(sides[team])
                        if not best:
                            continue
                        odds = float(best["price"])
                        if not (min_odds <= odds <= max_odds):
                            continue
                        signed = _signed_spread_for_team(sides, team)
                        if signed is None:
                            continue
                        p_model = team_p_model("SPREAD", team, float(signed), away, home)
                        if p_model is None:
                            continue
                        p_model = _clip(float(p_model), float(p_mkt), clip)
                        p_real = model_w * p_model + (1.0 - model_w) * float(p_mkt)
                        p_real, edge = _haircut(p_real, float(p_mkt), haircut_trigger, haircut_rate)
                        if edge > edge_refuse:
                            continue
                        ev = p_real * odds - 1.0
                        if ev <= min_ev or edge < min_edge:
                            continue
                        dev = (odds - float(med)) / float(med) if med > 0 else 0.0
                        score = _score(edge, ev, dev)
                        candidates.append({
                            "match": match,
                            "market": "SPREAD",
                            "selection": team,
                            "line": float(signed),
                            "odds": odds,
                            "book": best["book"],
                            "p_model": float(p_model),
                            "p_mkt": float(p_mkt),
                            "p_real": float(p_real),
                            "edge": float(edge),
                            "ev": float(ev),
                            "dev": float(dev),
                            "score": float(score),
                            "why": "Model-first spread (margin prior + scripts) calibrated (clip+haircut).",
                        })

    candidates.sort(key=lambda x: (x["score"], x["edge"], x["ev"]), reverse=True)
    meta = {"games": len(games), "team_candidates": len(candidates)}
    return candidates, meta, spread_map


def build_portfolio_team(candidates: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    max_picks = int(cfg.get("max_picks_team", 3))
    max_ml = int(cfg.get("max_ml_per_day", 2))
    one_per_match = bool(cfg.get("one_pick_per_match", True))

    # prefer spreads/totals over ML unless ML has better score for that match
    best_by_match: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        m = c["match"]
        cur = best_by_match.get(m)
        if cur is None or c["score"] > cur["score"]:
            best_by_match[m] = c

    shortlist = sorted(best_by_match.values(), key=lambda x: (x["score"], x["edge"], x["ev"]), reverse=True)

    picks: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    for c in shortlist:
        if len(picks) >= max_picks:
            break
        if one_per_match and c["match"] in used_matches:
            continue
        if c["market"] == "H2H" and ml_count >= max_ml:
            continue
        picks.append(c)
        used_matches.add(c["match"])
        if c["market"] == "H2H":
            ml_count += 1

    # fill if still missing (allow second-best markets from different match first, else same match)
    if len(picks) < max_picks:
        for c in candidates:
            if len(picks) >= max_picks:
                break
            if c in picks:
                continue
            if c["market"] == "H2H" and ml_count >= max_ml:
                continue
            if one_per_match and c["match"] in used_matches:
                continue
            picks.append(c)
            used_matches.add(c["match"])
            if c["market"] == "H2H":
                ml_count += 1

    return picks[:max_picks]


def dump_artifacts(run_id: str, meta: Dict[str, Any], team_candidates: List[Dict[str, Any]],
                   team_picks: List[Dict[str, Any]], prop_candidates: List[Dict[str, Any]], prop_picks: List[Dict[str, Any]]) -> None:
    os.makedirs("artifacts", exist_ok=True)
    def dump(name: str, obj: Any):
        with open(os.path.join("artifacts", f"{run_id}_{name}.json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    dump("meta", meta)
    dump("team_candidates", team_candidates[:500])
    dump("team_picks", team_picks)
    dump("prop_candidates", prop_candidates[:500])
    dump("prop_picks", prop_picks)
