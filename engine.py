# engine.py (V8)
# - Correct OddsAPI parsing (books/markets/outcomes)
# - Model-first p_real (from model_team.team_model)
# - Clip vs market + haircut
# - Portfolio builder with Kelly sizing + caps

from __future__ import annotations

from typing import Any, Dict, List, Tuple
import math
import os
import json

from utils import clamp, norm_team, no_vig_2way, best_price_for_side, kelly_fraction
from model_team import team_model, p_real_ml, p_real_spread


def _iter_market_offers(game: Dict[str, Any], market_key: str) -> List[Dict[str, Any]]:
    offers: List[Dict[str, Any]] = []
    for bm in (game.get("bookmakers") or []):
        bkey = bm.get("key")
        btitle = bm.get("title") or bkey
        for m in (bm.get("markets") or []):
            if m.get("key") != market_key:
                continue
            outcomes = m.get("outcomes") or []
            offers.append({"book": btitle, "book_key": bkey, "outcomes": outcomes})
    return offers


def _median_implied_prob_2way(offers: List[Dict[str, Any]], side_a: str, side_b: str) -> Tuple[float, float]:
    """
    Returns median implied probabilities (not no-vig) for A and B.
    We use medians for robustness to outlier books.
    """
    pa: List[float] = []
    pb: List[float] = []
    for off in offers:
        for oc in off["outcomes"]:
            name = str(oc.get("name", ""))
            price = oc.get("price")
            if not price:
                continue
            try:
                p_imp = 1.0 / float(price)
            except Exception:
                continue
            if name == side_a:
                pa.append(p_imp)
            elif name == side_b:
                pb.append(p_imp)

    def _med(x: List[float]) -> float:
        if not x:
            return 0.0
        xs = sorted(x)
        n = len(xs)
        mid = n // 2
        return float(xs[mid] if n % 2 else 0.5 * (xs[mid - 1] + xs[mid]))

    return _med(pa), _med(pb)


def build_team_candidates(
    games: List[Dict[str, Any]],
    team_cfg: Dict[str, Any],
    features: Dict[str, Any],
    inj_adjust: Dict[str, Dict[str, float]] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, float]]:
    """
    Returns (candidates, meta, spread_map_abs)
    spread_map_abs used for props blowout proxy later.
    """
    inj_adjust = inj_adjust or {}
    clip = float(team_cfg.get("clip", 0.08))
    odds_min, odds_max = team_cfg.get("odds_range", [1.5, 2.2])

    candidates: List[Dict[str, Any]] = []
    clip_hits = 0
    total_evals = 0
    spread_map: Dict[str, float] = {}

    for g in games:
        home = str(g.get("home_team", ""))
        away = str(g.get("away_team", ""))
        if not home or not away:
            continue

        # We only build SPREAD picks for V8 baseline (most stable).
        # You can re-enable ML/TOTAL later.
        spread_offers = _iter_market_offers(g, "spreads")
        if not spread_offers:
            continue

        # Build a simple abs spread proxy from first available outcome point
        abs_sp = None
        for off in spread_offers:
            for oc in off["outcomes"]:
                if "point" in oc:
                    try:
                        abs_sp = abs(float(oc["point"]))
                        break
                    except Exception:
                        pass
            if abs_sp is not None:
                break
        if abs_sp is None:
            abs_sp = 0.0
        spread_map[f"{norm_team(away)}@{norm_team(home)}"] = float(abs_sp)

        # Model bundle
        m = team_model(home, away, features, inj_adjust=inj_adjust, rotation_fragility=0.0)
        mu = float(m["mu"])
        sigma_eff = float(m["sigma_eff"])

        # Determine best line/price per side across books (fallback books + best line)
        # Spread market outcomes: [{"name": team, "point": +/-x, "price": y}, ...]
        # We'll consider BOTH sides, compute p_real for that side line, compute edge vs market, etc.
        for side in [home, away]:
            total_evals += 1

            best = best_price_for_side(spread_offers, side, market="spreads")
            if not best:
                continue

            price = float(best["price"])
            line = float(best["point"])  # line attached to that side
            book = best["book"]

            if not (odds_min <= price <= odds_max):
                continue

            # Market implied/no-vig for this line using median across books at same line is hard;
            # Use median implied 2-way on the NAME only as a proxy + no-vig normalize.
            # (V8: stable; avoids heavy matching logic)
            pA_imp, pB_imp = _median_implied_prob_2way(spread_offers, home, away)
            p_home_nv, p_away_nv = no_vig_2way(pA_imp, pB_imp)

            p_mkt = p_home_nv if side == home else p_away_nv

            # Model p_real for "side covers" given its line:
            # If side is home, home covers at line_home (e.g., -4.5).
            # If side is away, away covers at line_away (e.g., +4.5) => home cover prob at -line_away.
            if side == home:
                p_model_raw = p_real_spread(line, mu, sigma_eff)
            else:
                # away cover probability = P(home margin < -line_away) = 1 - P(home covers -line_away)
                p_home_cover_at_neg = p_real_spread(-line, mu, sigma_eff)
                p_model_raw = float(clamp(1.0 - p_home_cover_at_neg, 0.01, 0.99))

            # Clip vs market
            p_model = float(clamp(p_model_raw, p_mkt - clip, p_mkt + clip))
            if abs(p_model - p_model_raw) > 1e-9:
                clip_hits += 1

            # Haircut if edge is “too clean”
            edge = float(p_model - p_mkt)
            haircut_trigger = float(team_cfg.get("haircut_trigger", 0.06))
            haircut_rate = float(team_cfg.get("haircut_rate", 0.30))
            if abs(edge) >= haircut_trigger:
                # pull p_model toward market
                p_model = float(p_mkt + (p_model - p_mkt) * (1.0 - haircut_rate))
                edge = float(p_model - p_mkt)

            # EV
            ev = float(p_model * price - 1.0)

            # Score (simple, stable): prioritize EV, edge, and lower uncertainty
            dev = abs(p_model - p_mkt)
            score = 100.0 * (0.55 * ev + 0.35 * dev + 0.10 * (1.0 / (1.0 + sigma_eff / 12.0)))
            score = float(clamp(score, -100.0, 100.0))

            candidates.append(
                {
                    "match": f"{away} @ {home}",
                    "home": home,
                    "away": away,
                    "market": "SPREAD",
                    "side": side,
                    "line": line,
                    "odds": price,
                    "book": book,
                    "p_mkt": p_mkt,
                    "p_model": p_model,
                    "p_real": p_model,  # for display; model-first after clip/haircut
                    "edge": edge,
                    "ev": ev,
                    "dev": dev,
                    "sigma_eff": sigma_eff,
                    "mu": mu,
                    "scripts": m.get("scripts"),
                    "why": "Model-first spread (margin prior + scripts + clip/haircut).",
                }
            )

    meta = {
        "clip_hits": clip_hits,
        "clip_hit_rate": float(clip_hits / max(1, total_evals)),
        "evaluated": total_evals,
    }
    return candidates, meta, spread_map


def _corr_mult_for_pick(pick: Dict[str, Any]) -> float:
    # V8 baseline: TEAM picks are 1 per match -> corr mostly 1.0
    return 1.0


def build_team_portfolio(
    candidates: List[Dict[str, Any]],
    team_cfg: Dict[str, Any],
    slate_kelly_mult: float = 1.0,
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    """
    Greedy selection under constraints + Kelly sizing.
    """
    one_per_match = bool(team_cfg.get("one_pick_per_match", True))
    max_ml_per_day = int(team_cfg.get("max_ml_per_day", 2))  # retained even if we do SPREAD only
    min_edge = float(team_cfg.get("min_edge", 0.02))
    edge_refuse = float(team_cfg.get("edge_refuse", 0.15))

    bankroll = float(team_cfg.get("bankroll", 1.0))
    bet_min_pct = float(team_cfg.get("bet_min_pct", 0.0025))  # 0.25%
    bet_max_pct = float(team_cfg.get("bet_max_pct", 0.02))    # 2.0%
    total_cap_pct = float(team_cfg.get("total_cap_pct", 0.10))  # 10%

    # Filter discipline
    filt = []
    for c in candidates:
        e = float(c.get("edge", 0.0))
        if e < min_edge:
            continue
        if e > edge_refuse:
            continue
        if float(c.get("ev", -999)) <= 0.0:
            continue
        filt.append(c)

    # Sort by score desc
    filt.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)

    picks: List[Dict[str, Any]] = []
    used_matches = set()
    total_alloc = 0.0

    for c in filt:
        if len(picks) >= top_n:
            break

        m = str(c.get("match", ""))
        if one_per_match and m in used_matches:
            continue

        p = float(c.get("p_real", 0.0))
        odds = float(c.get("odds", 0.0))
        if odds <= 1.01:
            continue

        # Kelly sizing
        k_raw = kelly_fraction(p, odds)
        corr_mult = _corr_mult_for_pick(c)

        # Haircut multiplier: if clip was heavy (dev small), be conservative
        dev = float(c.get("dev", 0.0))
        haircut_mult = float(clamp(1.0 - 1.5 * dev, 0.70, 1.0))

        k = float(clamp(k_raw * slate_kelly_mult * corr_mult * haircut_mult, 0.0, bet_max_pct))

        # Apply per-bet min if bet is non-zero
        if 0.0 < k < bet_min_pct:
            k = bet_min_pct

        if total_alloc + k > total_cap_pct:
            # stop allocating more
            continue

        stake = bankroll * k

        c2 = dict(c)
        c2["kelly_raw"] = k_raw
        c2["stake_pct"] = k
        c2["stake_units"] = stake
        c2["score"] = float(c.get("score", 0.0))

        picks.append(c2)
        used_matches.add(m)
        total_alloc += k

    return picks


def dump_artifacts(run_id: str, payload: Dict[str, Any], out_dir: str = "artifacts") -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_dir


def norm_team_alias(name: str) -> str:
    return norm_team(name)
