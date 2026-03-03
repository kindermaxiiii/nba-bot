from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from model_team import model_prob_for_team_market

# -------------------------
# Utils
# -------------------------

def _median(xs: List[float]) -> Optional[float]:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    return xs[mid] if (n % 2 == 1) else 0.5 * (xs[mid - 1] + xs[mid])

def _clamp(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))

def _imp_prob_from_decimal(odds: float) -> Optional[float]:
    try:
        o = float(odds)
        if o <= 1.00001:
            return None
        return 1.0 / o
    except Exception:
        return None

def _novig_two_way(p1: float, p2: float) -> Tuple[float, float]:
    s = p1 + p2
    if s <= 0:
        return 0.5, 0.5
    return p1 / s, p2 / s

def _ev(p_real: float, odds: float) -> float:
    # EV = p*(odds-1) - (1-p)
    return p_real * (odds - 1.0) - (1.0 - p_real)

def _dev_best_vs_median(best_odds: float, med_odds: float) -> float:
    if med_odds is None or med_odds <= 0:
        return 0.0
    return abs(best_odds - med_odds) / med_odds

# -------------------------
# Collect odds from OddsAPI JSON (déjà récupéré par odds_api.py)
# -------------------------

def collect_market_lines(
    games: List[Dict[str, Any]],
    market_key: str,
) -> List[Dict[str, Any]]:
    """
    Retourne une liste de 'candidats' standardisés:
    {
      match_id, match, commence_time,
      away_team, home_team,
      market, selection, line,
      book_prices: [(book, odds, line)]
    }
    """
    out: List[Dict[str, Any]] = []

    for g in games:
        gid = g.get("id")
        home = g.get("home_team")
        away = g.get("away_team")
        ct = g.get("commence_time")
        match = f"{away} @ {home}"

        for bm in (g.get("bookmakers") or []):
            book = bm.get("title") or bm.get("key")
            for m in (bm.get("markets") or []):
                if m.get("key") != market_key:
                    continue
                outcomes = m.get("outcomes") or []
                # h2h: 2 outcomes (home/away)
                # spreads: 2 outcomes avec point
                # totals: 2 outcomes Over/Under avec point
                for oc in outcomes:
                    name = oc.get("name")
                    price = oc.get("price")
                    point = oc.get("point")  # None pour h2h

                    if name is None or price is None:
                        continue

                    # Normalisation selection + market
                    if market_key == "h2h":
                        market = "MONEYLINE"
                        selection = str(name)
                        line = None
                    elif market_key == "spreads":
                        market = "SPREAD"
                        selection = str(name)
                        line = float(point) if point is not None else None
                    elif market_key == "totals":
                        market = "TOTAL"
                        selection = "Over" if str(name).lower() == "over" else "Under"
                        line = float(point) if point is not None else None
                    else:
                        continue

                    # Ajout dans out : on regroupe plus tard par (match, market, selection, line)
                    out.append({
                        "match_id": gid,
                        "match": match,
                        "commence_time": ct,
                        "away_team": away,
                        "home_team": home,
                        "market": market,
                        "selection": selection,
                        "line": line,
                        "book": book,
                        "odds": float(price),
                    })

    # Regroupement
    grouped: Dict[Tuple[str, str, str, Optional[float]], Dict[str, Any]] = {}
    for r in out:
        key = (r["match_id"], r["market"], r["selection"], r["line"])
        if key not in grouped:
            grouped[key] = {
                "match_id": r["match_id"],
                "match": r["match"],
                "commence_time": r["commence_time"],
                "away_team": r["away_team"],
                "home_team": r["home_team"],
                "market": r["market"],
                "selection": r["selection"],
                "line": r["line"],
                "book_prices": [],
            }
        grouped[key]["book_prices"].append((r["book"], r["odds"]))

    return list(grouped.values())

# Props (placeholder si ton OddsAPI plan n’inclut pas player props)
def collect_player_prop_lines(*args, **kwargs) -> List[Dict[str, Any]]:
    return []

# -------------------------
# Analyze 2-way
# -------------------------

def analyze_two_way_market(
    candidate: Dict[str, Any],
    team_features: Dict[str, Dict[str, Any]],
    *,
    w_model: float = 0.65,       # poids modèle
    clip_diff: float = 0.08,     # max |p_model - p_mkt| = 8%
    max_ml_odds: float = 4.50,   # anti “ML @6”
) -> Optional[Dict[str, Any]]:
    """
    Retourne un dict pick avec métriques calculées, ou None si impossible.
    """
    market = candidate["market"]
    selection = candidate["selection"]
    line = candidate["line"]
    away = candidate["away_team"]
    home = candidate["home_team"]

    prices = [o for (_, o) in candidate["book_prices"]]
    if len(prices) < 2:
        return None

    best_odds = max(prices)
    med_odds = _median(prices)

    # p_mkt no-vig (2-way) : on reconstruit à partir des 2 côtés au MEDIAN
    # -> On doit trouver l’autre côté correspondant (même match, même market, même line)
    # -> Cette fonction est appelée après regroupement ; donc p_mkt sera calculé dans main
    # Ici, on met un placeholder et main fournira p_mkt_pair.
    return {
        **candidate,
        "best_odds": float(best_odds),
        "median_odds": float(med_odds) if med_odds is not None else None,
        "books_count": len(prices),
        "dev": _dev_best_vs_median(float(best_odds), float(med_odds) if med_odds else float(best_odds)),
        "max_ml_odds": max_ml_odds,
        "w_model": w_model,
        "clip_diff": clip_diff,
        "p_mkt": None,
        "p_model": None,
        "p_real": None,
        "edge": None,
        "ev": None,
        "score": None,
    }

def finalize_two_way_pair(
    a: Dict[str, Any],
    b: Dict[str, Any],
    team_features: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Prend 2 côtés d’un même marché (a/b) et calcule p_mkt no-vig, p_model, p_real, EV, score.
    """
    # p_mkt no-vig via medians
    pa_raw = _imp_prob_from_decimal(a["median_odds"] or a["best_odds"])
    pb_raw = _imp_prob_from_decimal(b["median_odds"] or b["best_odds"])
    if pa_raw is None or pb_raw is None:
        return None, None
    pa_mkt, pb_mkt = _novig_two_way(pa_raw, pb_raw)

    def _compute(one: Dict[str, Any], p_mkt: float) -> Optional[Dict[str, Any]]:
        market = one["market"]
        selection = one["selection"]
        line = one["line"]
        away = one["away_team"]
        home = one["home_team"]

        # p_model
        p_model = model_prob_for_team_market(
            market=market,
            selection=selection,
            line=line,
            away_team=away,
            home_team=home,
            features=team_features,
        )
        if p_model is None:
            # fallback conservateur: on ne “devine” pas, on revient vers le marché
            p_model = p_mkt

        # clip différence modèle vs marché
        diff = p_model - p_mkt
        if abs(diff) > one["clip_diff"]:
            p_model = p_mkt + math.copysign(one["clip_diff"], diff)

        # blend
        p_real = _clamp(one["w_model"] * p_model + (1.0 - one["w_model"]) * p_mkt)

        # anti ML longshot
        if market == "MONEYLINE" and float(one["best_odds"]) > float(one["max_ml_odds"]):
            return None

        ev = _ev(p_real, float(one["best_odds"]))
        edge = p_real - p_mkt

        # score simple mais sain (évite les picks absurdes)
        # - priorité EV
        # - pénalité dev si trop dispersé
        # - pénalité ML
        score = 100.0 * max(0.0, ev)  # EV>0 requis (sinon score 0)
        score -= 40.0 * float(one["dev"])  # dispersion
        if market == "MONEYLINE":
            score -= 8.0

        return {
            **one,
            "p_mkt": float(p_mkt),
            "p_model": float(p_model),
            "p_real": float(p_real),
            "edge": float(edge),
            "ev": float(ev),
            "score": float(score),
        }

    return _compute(a, pa_mkt), _compute(b, pb_mkt)

# -------------------------
# Diversification / rules
# -------------------------

def diversify_team_picks(
    picks: List[Dict[str, Any]],
    *,
    max_picks: int = 3,
    one_pick_per_match: bool = True,
    max_ml: int = 2,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    for p in sorted(picks, key=lambda x: x.get("score", -1e9), reverse=True):
        if len(out) >= max_picks:
            break

        if one_pick_per_match and p["match_id"] in used_matches:
            continue

        if p["market"] == "MONEYLINE":
            if ml_count >= max_ml:
                continue
            ml_count += 1

        out.append(p)
        used_matches.add(p["match_id"])

    return out

def diversify_prop_picks(
    picks: List[Dict[str, Any]],
    *,
    max_picks: int = 3,
    one_pick_per_match: bool = True,
    one_pick_per_player: bool = True,
) -> List[Dict[str, Any]]:
    # placeholder : à activer quand tu as de vraies props
    return picks[:max_picks]

def allocate_stakes_capped(*args, **kwargs):
    # tu m’as dit que tu veux enlever la gestion des mises -> on n’utilise plus ça ici
    return []
