"""engine.py (V7)

Core responsibilities:
- TEAM: build candidates from OddsAPI (H2H / spreads / totals)
- TEAM: model-first p_real via model_team.py, using market ONLY for clipping + haircuts
- TEAM: best line selection across books, consensus line, median odds
- TEAM: portfolio builder (top 3) with constraints (odds range, max ML/day, 1 pick/match)
- ARTIFACTS: write audit JSON to artifacts/<run_id>/

PROPS are handled in props_engine_v6.py, but if OddsAPI plan/endpoint does not provide props,
main.py will send a detailed "NO BET PROPS" message.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

from model_team import team_p_model


# ----------------------------
# Helpers
# ----------------------------

def clamp_prob(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    try:
        p = float(p)
    except Exception:
        return 0.5
    return max(lo, min(hi, p))


def norm_team(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace(".", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("la "):
        s = "los angeles " + s[3:]
    if s.startswith("ny "):
        s = "new york " + s[3:]
    return s


def match_key(g: Dict[str, Any]) -> str:
    return f"{g.get('away_team','?')} @ {g.get('home_team','?')}"


def implied_prob(odds: float) -> float:
    odds = float(odds)
    if odds <= 1.0:
        return 1.0
    return 1.0 / odds


def no_vig_2way(p_a: float, p_b: float) -> Tuple[float, float, float]:
    """Return (p_a_no_vig, p_b_no_vig, overround)."""
    s = p_a + p_b
    if s <= 0:
        return 0.5, 0.5, 0.0
    return p_a / s, p_b / s, s - 1.0


def median(xs: List[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    return float(statistics.median(xs))


def consensus_line(lines: List[float]) -> Optional[float]:
    """Return most frequent line; tie-breaker by median."""
    if not lines:
        return None
    # bucket by exact value (OddsAPI uses .5 increments normally)
    counts: Dict[float, int] = {}
    for x in lines:
        x = float(x)
        counts[x] = counts.get(x, 0) + 1
    best_count = max(counts.values())
    best = [k for k, v in counts.items() if v == best_count]
    return float(statistics.median(best))


def clip(p_model: float, p_mkt: float, clip_abs: float) -> float:
    p_model = clamp_prob(p_model)
    p_mkt = clamp_prob(p_mkt)
    d = p_model - p_mkt
    if abs(d) <= clip_abs:
        return p_model
    return clamp_prob(p_mkt + clip_abs * (1 if d > 0 else -1))


def haircut(p: float, p_mkt: float, trigger: float, rate: float) -> float:
    p = clamp_prob(p)
    p_mkt = clamp_prob(p_mkt)
    edge = p - p_mkt
    if edge <= trigger:
        return p
    return clamp_prob(p_mkt + edge * (1.0 - rate))


def best_line_and_books(
    books: List[Dict[str, Any]],
    market_key: str,
    selection: str,
    line: Optional[float],
) -> Tuple[Optional[float], Optional[str], int, Optional[float]]:
    """Return (best_odds, best_book, books_count, median_odds).

    For spreads/totals, match also on line.
    """
    odds_list: List[float] = []
    best_odds = None
    best_book = None
    books_used = 0

    for b in books or []:
        mkts = b.get("markets") or []
        for m in mkts:
            if str(m.get("key")) != market_key:
                continue
            for oc in (m.get("outcomes") or []):
                if str(oc.get("name")) != selection:
                    continue
                if line is not None:
                    # spreads/totals include "point"
                    pt = oc.get("point")
                    if pt is None:
                        continue
                    if float(pt) != float(line):
                        continue
                price = oc.get("price")
                if price is None:
                    continue
                price = float(price)
                odds_list.append(price)
                if best_odds is None or price > best_odds:
                    best_odds = price
                    best_book = b.get("title") or b.get("key")

    if odds_list:
        books_used = len(set(odds_list))  # proxy; we also show count separately
        med = float(statistics.median(odds_list))
    else:
        med = None

    # Better books count: count distinct bookmakers that had a matching outcome
    # (approximate with the same traversal but using set of book titles)
    book_set = set()
    for b in books or []:
        mkts = b.get("markets") or []
        for m in mkts:
            if str(m.get("key")) != market_key:
                continue
            for oc in (m.get("outcomes") or []):
                if str(oc.get("name")) != selection:
                    continue
                if line is not None:
                    pt = oc.get("point")
                    if pt is None or float(pt) != float(line):
                        continue
                if oc.get("price") is None:
                    continue
                book_set.add(b.get("title") or b.get("key"))
    books_count = len(book_set)

    return best_odds, best_book, books_count, med


# ----------------------------
# TEAM candidates
# ----------------------------


def build_team_candidates(
    games: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    features: Dict[str, Dict[str, Any]],
    inj_adjust: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, float]]:
    """Return (candidates, meta, team_spread_map).

    team_spread_map maps match_key -> abs consensus spread (for props blowout proxy).
    """

    odds_lo, odds_hi = cfg.get("odds_range", [1.5, 2.2])
    clip_abs = float(cfg.get("clip", 0.08))
    haircut_trigger = float(cfg.get("haircut_trigger", 0.06))
    haircut_rate = float(cfg.get("haircut_rate", 0.30))

    # Normalized feature map for robust lookup
    feat_norm = {norm_team(k): v for k, v in (features or {}).items()}

    def feat(team: str) -> Dict[str, Any]:
        return (features or {}).get(team) or feat_norm.get(norm_team(team), {}) or {}

    inj_norm = {norm_team(k): v for k, v in (inj_adjust or {}).items()}

    def inj(team: str) -> Dict[str, Any]:
        return inj_norm.get(norm_team(team), {}) or {}

    candidates: List[Dict[str, Any]] = []
    spread_map: Dict[str, float] = {}

    markets_tested = 0
    clip_hits = 0

    for g in games:
        home = str(g.get("home_team") or "")
        away = str(g.get("away_team") or "")
        if not home or not away:
            continue

        mk = match_key(g)
        books = g.get("bookmakers") or []

        # -----------------
        # SPREADS
        # -----------------
        # Determine consensus spread line for home side
        spread_lines: List[float] = []
        for b in books:
            for m in (b.get("markets") or []):
                if m.get("key") != "spreads":
                    continue
                for oc in (m.get("outcomes") or []):
                    if oc.get("name") == home and oc.get("point") is not None:
                        spread_lines.append(float(oc.get("point")))

        cons_spread = consensus_line(spread_lines)
        if cons_spread is not None:
            spread_map[mk] = abs(float(cons_spread))

            # Evaluate both sides at that line
            for selection, line in ((home, cons_spread), (away, -float(cons_spread))):
                best_odds, best_book, books_cnt, med_odds = best_line_and_books(books, "spreads", selection, float(line))
                if best_odds is None or med_odds is None:
                    continue
                if not (float(odds_lo) <= float(best_odds) <= float(odds_hi)):
                    continue

                # Market implied / no-vig from median odds for both sides
                # Need opponent median at the same line
                opp = away if selection == home else home
                opp_line = -float(line)
                opp_best, _, _, opp_med = best_line_and_books(books, "spreads", opp, float(opp_line))
                if opp_med is None:
                    continue

                p_a = implied_prob(float(med_odds))
                p_b = implied_prob(float(opp_med))
                p_no_vig, _, overround = no_vig_2way(p_a, p_b)
                p_mkt = clamp_prob(p_no_vig)

                # Model (raw) and calibrated p_real
                inj_mu_home = float(inj(home).get("mu", 0.0))
                inj_mu_away = float(inj(away).get("mu", 0.0))
                inj_sigma_mult = float(max(inj(home).get("sigma_mult", 1.0), inj(away).get("sigma_mult", 1.0)))

                local_features = {home: feat(home), away: feat(away)}
                p_model_raw = team_p_model(
                    "SPREAD",
                    selection,
                    float(line),
                    away,
                    home,
                    local_features,
                    inj_mu_home=inj_mu_home,
                    inj_mu_away=inj_mu_away,
                    inj_sigma_mult=inj_sigma_mult,
                )
                if p_model_raw is None:
                    continue

                p_model = clip(float(p_model_raw), p_mkt, clip_abs)
                if abs(p_model - float(p_model_raw)) > 1e-9:
                    clip_hits += 1
                p_real = haircut(p_model, p_mkt, haircut_trigger, haircut_rate)

                markets_tested += 1

                ev = float(p_real) * float(best_odds) - 1.0
                if ev <= 0:
                    continue

                edge = float(p_real) - float(p_mkt)
                dev = float(best_odds / float(med_odds) - 1.0) if med_odds else 0.0
                score = min(100.0, 1000.0 * max(ev, 0.0) + 600.0 * max(edge, 0.0) + 150.0 * max(dev, 0.0))

                candidates.append(
                    {
                        "match": mk,
                        "market": "SPREAD",
                        "line": float(line),
                        "selection": selection,
                        "odds": float(best_odds),
                        "book": best_book or "?",
                        "books": int(books_cnt),
                        "odds_median": float(med_odds),
                        "p_mkt": float(p_mkt),
                        "p_model": float(p_model),
                        "p_model_raw": float(p_model_raw),
                        "p_real": float(p_real),
                        "edge": float(edge),
                        "ev": float(ev),
                        "dev": float(dev),
                        "overround": float(overround),
                        "score": float(score),
                        "why": "Model-first spread (margin prior + scripts + calibration).",
                    }
                )

        # -----------------
        # TOTALS
        # -----------------
        total_lines: List[float] = []
        for b in books:
            for m in (b.get("markets") or []):
                if m.get("key") != "totals":
                    continue
                for oc in (m.get("outcomes") or []):
                    if oc.get("name") == "Over" and oc.get("point") is not None:
                        total_lines.append(float(oc.get("point")))

        cons_total = consensus_line(total_lines)
        if cons_total is not None:
            for selection in ("Over", "Under"):
                best_odds, best_book, books_cnt, med_odds = best_line_and_books(books, "totals", selection, float(cons_total))
                if best_odds is None or med_odds is None:
                    continue
                if not (float(odds_lo) <= float(best_odds) <= float(odds_hi)):
                    continue

                opp = "Under" if selection == "Over" else "Over"
                _, _, _, opp_med = best_line_and_books(books, "totals", opp, float(cons_total))
                if opp_med is None:
                    continue

                p_a = implied_prob(float(med_odds))
                p_b = implied_prob(float(opp_med))
                p_no_vig, _, overround = no_vig_2way(p_a, p_b)
                p_mkt = clamp_prob(p_no_vig)

                inj_mu_home = float(inj(home).get("mu", 0.0))
                inj_mu_away = float(inj(away).get("mu", 0.0))
                inj_sigma_mult = float(max(inj(home).get("sigma_mult", 1.0), inj(away).get("sigma_mult", 1.0)))

                local_features = {home: feat(home), away: feat(away)}
                p_model_raw = team_p_model(
                    "TOTAL",
                    selection,
                    float(cons_total),
                    away,
                    home,
                    local_features,
                    inj_mu_home=inj_mu_home,
                    inj_mu_away=inj_mu_away,
                    inj_sigma_mult=inj_sigma_mult,
                )
                if p_model_raw is None:
                    continue

                p_model = clip(float(p_model_raw), p_mkt, clip_abs)
                if abs(p_model - float(p_model_raw)) > 1e-9:
                    clip_hits += 1
                p_real = haircut(p_model, p_mkt, haircut_trigger, haircut_rate)

                markets_tested += 1

                ev = float(p_real) * float(best_odds) - 1.0
                if ev <= 0:
                    continue

                edge = float(p_real) - float(p_mkt)
                dev = float(best_odds / float(med_odds) - 1.0) if med_odds else 0.0
                score = min(100.0, 1000.0 * max(ev, 0.0) + 600.0 * max(edge, 0.0) + 150.0 * max(dev, 0.0))

                candidates.append(
                    {
                        "match": mk,
                        "market": "TOTAL",
                        "line": float(cons_total),
                        "selection": selection,
                        "odds": float(best_odds),
                        "book": best_book or "?",
                        "books": int(books_cnt),
                        "odds_median": float(med_odds),
                        "p_mkt": float(p_mkt),
                        "p_model": float(p_model),
                        "p_model_raw": float(p_model_raw),
                        "p_real": float(p_real),
                        "edge": float(edge),
                        "ev": float(ev),
                        "dev": float(dev),
                        "overround": float(overround),
                        "score": float(score),
                        "why": "Model-first total (pace+efficiency) calibrated (clip+haircut).",
                    }
                )

        # -----------------
        # H2H / ML
        # -----------------
        # Only consider ML if within odds range (avoid 1.01 and longshots)
        for selection in (home, away):
            best_odds, best_book, books_cnt, med_odds = best_line_and_books(books, "h2h", selection, None)
            if best_odds is None or med_odds is None:
                continue
            if not (float(odds_lo) <= float(best_odds) <= float(odds_hi)):
                continue

            opp = away if selection == home else home
            _, _, _, opp_med = best_line_and_books(books, "h2h", opp, None)
            if opp_med is None:
                continue

            p_a = implied_prob(float(med_odds))
            p_b = implied_prob(float(opp_med))
            p_no_vig, _, overround = no_vig_2way(p_a, p_b)
            p_mkt = clamp_prob(p_no_vig)

            inj_mu_home = float(inj(home).get("mu", 0.0))
            inj_mu_away = float(inj(away).get("mu", 0.0))
            inj_sigma_mult = float(max(inj(home).get("sigma_mult", 1.0), inj(away).get("sigma_mult", 1.0)))

            local_features = {home: feat(home), away: feat(away)}
            p_model_raw = team_p_model(
                "H2H",
                selection,
                None,
                away,
                home,
                local_features,
                inj_mu_home=inj_mu_home,
                inj_mu_away=inj_mu_away,
                inj_sigma_mult=inj_sigma_mult,
            )
            if p_model_raw is None:
                continue

            p_model = clip(float(p_model_raw), p_mkt, clip_abs)
            if abs(p_model - float(p_model_raw)) > 1e-9:
                clip_hits += 1
            p_real = haircut(p_model, p_mkt, haircut_trigger, haircut_rate)

            markets_tested += 1

            ev = float(p_real) * float(best_odds) - 1.0
            if ev <= 0:
                continue

            edge = float(p_real) - float(p_mkt)
            dev = float(best_odds / float(med_odds) - 1.0) if med_odds else 0.0
            score = min(100.0, 900.0 * max(ev, 0.0) + 500.0 * max(edge, 0.0) + 120.0 * max(dev, 0.0) - 5.0)

            candidates.append(
                {
                    "match": mk,
                    "market": "MONEYLINE",
                    "line": None,
                    "selection": selection,
                    "odds": float(best_odds),
                    "book": best_book or "?",
                    "books": int(books_cnt),
                    "odds_median": float(med_odds),
                    "p_mkt": float(p_mkt),
                    "p_model": float(p_model),
                    "p_model_raw": float(p_model_raw),
                    "p_real": float(p_real),
                    "edge": float(edge),
                    "ev": float(ev),
                    "dev": float(dev),
                    "overround": float(overround),
                    "score": float(score),
                    "why": "Model-first ML (margin prior) calibrated (clip+haircut).",
                }
            )

    # Sort by score then EV
    candidates.sort(key=lambda x: (x.get("score", 0.0), x.get("ev", 0.0)), reverse=True)

    meta = {
        "games": len(games),
        "markets_tested": int(markets_tested),
        "clip": float(clip_abs),
        "clip_hits": int(clip_hits),
        "clip_hit_rate": round(float(clip_hits) / float(max(1, markets_tested)), 3),
        "odds_range": [float(odds_lo), float(odds_hi)],
        "haircut_trigger": haircut_trigger,
        "haircut_rate": haircut_rate,
    }

    return candidates, meta, spread_map


# ----------------------------
# TEAM portfolio
# ----------------------------


def build_team_portfolio(candidates: List[Dict[str, Any]], cfg: Dict[str, Any], top_n: int = 3) -> List[Dict[str, Any]]:
    max_ml = int(cfg.get("max_ml_per_day", 2))
    one_per_match = bool(cfg.get("one_pick_per_match", True))

    picks: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    for c in candidates:
        if len(picks) >= top_n:
            break
        m = c.get("match")
        if one_per_match and m in used_matches:
            continue
        if c.get("market") == "MONEYLINE":
            if ml_count >= max_ml:
                continue
            ml_count += 1
        picks.append(c)
        used_matches.add(m)

    return picks


# ----------------------------
# Artifacts
# ----------------------------


def dump_artifacts(run_id: str, payloads: Dict[str, Any]) -> str:
    """Write json artifacts under artifacts/<run_id>/ and return directory path."""
    out_dir = os.path.join(os.path.dirname(__file__), "artifacts", run_id)
    os.makedirs(out_dir, exist_ok=True)

    for name, obj in payloads.items():
        path = os.path.join(out_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    return out_dir
