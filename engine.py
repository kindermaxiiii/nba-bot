# engine.py
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from odds_api import fetch_odds_with_fallback
from model_team import model_prob_for_team_market
from model_props import prob_over_from_logs


def _median(xs: List[float]) -> Optional[float]:
    xs = sorted([x for x in xs if x is not None])
    if not xs:
        return None
    n = len(xs)
    if n % 2 == 1:
        return float(xs[n // 2])
    return (float(xs[n // 2 - 1]) + float(xs[n // 2])) / 2.0


def _imp(odds: float) -> float:
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def _no_vig(pa: float, pb: float) -> Tuple[float, float]:
    s = pa + pb
    if s <= 0:
        return 0.0, 0.0
    return pa / s, pb / s


def _clip(p_model: float, p_mkt: float, clip: float) -> float:
    # calibration constraint: |p_real - p_mkt| <= clip
    d = p_model - p_mkt
    if d > clip:
        return p_mkt + clip
    if d < -clip:
        return p_mkt - clip
    return p_model


def _haircut(p_real: float, p_mkt: float, edge_trigger: float, haircut_rate: float) -> Tuple[float, float]:
    edge_raw = p_real - p_mkt
    if edge_raw <= edge_trigger:
        return p_real, edge_raw
    # shrink towards market by haircut_rate
    p_adj = p_mkt + (p_real - p_mkt) * (1.0 - haircut_rate)
    return p_adj, (p_adj - p_mkt)


def _dev(best: float, med: float) -> float:
    if med <= 0:
        return 0.0
    return (best - med) / med


def _score(edge: float, ev: float, dev: float) -> float:
    # simple institutional score, capped
    # edge up to 8% -> 60 pts, EV up to 6% -> 30 pts, dev up to 5% -> 10 pts
    e = max(0.0, min(1.0, edge / 0.08)) * 60.0
    v = max(0.0, min(1.0, ev / 0.06)) * 30.0
    d = max(0.0, min(1.0, dev / 0.05)) * 10.0
    return max(0.0, min(100.0, e + v + d))


def _season_string(dt: datetime) -> str:
    y, m = dt.year, dt.month
    if m >= 10:
        y1, y2 = y, y + 1
    else:
        y1, y2 = y - 1, y
    return f"{y1}-{str(y2)[-2:]}"


@dataclass
class Config:
    sport_key: str
    regions_priority: List[str]
    team_markets: List[str]
    prop_markets: List[str]
    preferred_books: List[str]

    max_team_picks: int
    max_prop_picks: int
    max_ml_per_day: int

    odds_min: float
    odds_max: float

    clip_vs_market: float
    edge_min_strict: float
    edge_min_fill: float

    haircut_trigger: float
    haircut_rate: float
    edge_refuse: float

    model_weight: float

    props_last_n: int
    props_min_games: int
    props_blowout_penalty_spread: float


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        j = json.load(f)

    return Config(
        sport_key=j.get("sport_key", "basketball_nba"),
        regions_priority=j.get("regions_priority", ["us", "eu", "uk"]),
        team_markets=j.get("team_markets", ["spreads", "totals", "h2h"]),
        prop_markets=j.get("prop_markets", []),
        preferred_books=j.get("preferred_books", []),

        max_team_picks=int(j.get("max_team_picks", 3)),
        max_prop_picks=int(j.get("max_prop_picks", 3)),
        max_ml_per_day=int(j.get("max_ml_per_day", 2)),

        odds_min=float(j.get("odds_min", 1.5)),
        odds_max=float(j.get("odds_max", 2.2)),

        clip_vs_market=float(j.get("clip_vs_market", 0.08)),
        edge_min_strict=float(j.get("edge_min_strict", 0.02)),
        edge_min_fill=float(j.get("edge_min_fill", 0.0)),

        haircut_trigger=float(j.get("haircut_trigger", 0.06)),
        haircut_rate=float(j.get("haircut_rate", 0.30)),
        edge_refuse=float(j.get("edge_refuse", 0.15)),

        model_weight=float(j.get("model_weight", 1.0)),

        props_last_n=int(j.get("props_last_n", 20)),
        props_min_games=int(j.get("props_min_games", 8)),
        props_blowout_penalty_spread=float(j.get("props_blowout_penalty_spread", 10.0)),
    )


def _load_team_features() -> Dict[str, Dict[str, Any]]:
    try:
        with open("data/team_features.json", "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _collect_two_way_market(game: Dict[str, Any], market_key: str) -> Dict[str, Any]:
    """
    Returns structure:
      - for h2h: {"type":"h2h", "sides": {team:[odds...]}}
      - for totals: {"type":"totals", "line": point, "sides": {"Over":[...], "Under":[...]}}
      - for spreads: {"type":"spreads", "line": point_abs, "sides": {team:[(odds, point_signed)...]}}
    """
    home = game.get("home_team")
    away = game.get("away_team")
    bms = game.get("bookmakers") or []

    if not isinstance(bms, list):
        return {}

    # count occurrences per line
    line_counts: Dict[str, int] = {}
    raw_by_line: Dict[str, Dict[str, List[Any]]] = {}

    for bm in bms:
        for m in (bm.get("markets") or []):
            if m.get("key") != market_key:
                continue
            for o in (m.get("outcomes") or []):
                name = o.get("name")
                price = o.get("price")
                point = o.get("point")
                if name is None or price is None:
                    continue

                if market_key == "h2h":
                    lk = "h2h"
                    raw_by_line.setdefault(lk, {}).setdefault(str(name), []).append(float(price))
                    line_counts[lk] = line_counts.get(lk, 0) + 1

                elif market_key == "totals":
                    if point is None:
                        continue
                    lk = str(float(point))
                    raw_by_line.setdefault(lk, {}).setdefault(str(name), []).append(float(price))
                    line_counts[lk] = line_counts.get(lk, 0) + 1

                elif market_key == "spreads":
                    if point is None:
                        continue
                    # normalize line by absolute value (consensus)
                    lk = str(abs(float(point)))
                    raw_by_line.setdefault(lk, {}).setdefault(str(name), []).append((float(price), float(point)))
                    line_counts[lk] = line_counts.get(lk, 0) + 1

    if not line_counts:
        return {}

    # consensus line = most occurrences
    consensus = max(line_counts.items(), key=lambda kv: kv[1])[0]
    sides = raw_by_line.get(consensus, {})

    if market_key == "h2h":
        return {"type": "h2h", "line": None, "home": home, "away": away, "sides": sides}

    if market_key == "totals":
        return {"type": "totals", "line": float(consensus), "home": home, "away": away, "sides": sides}

    if market_key == "spreads":
        return {"type": "spreads", "line": float(consensus), "home": home, "away": away, "sides": sides}

    return {}


def _best_odds(game: Dict[str, Any], market_key: str, selection: str, line: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
    best = None
    best_book = None
    for bm in (game.get("bookmakers") or []):
        title = bm.get("title") or bm.get("key") or "Unknown"
        for m in (bm.get("markets") or []):
            if m.get("key") != market_key:
                continue
            for o in (m.get("outcomes") or []):
                if str(o.get("name")) != str(selection):
                    continue
                if market_key in ("totals", "spreads"):
                    if o.get("point") is None:
                        continue
                    if line is None:
                        continue
                    # totals: exact point match; spreads: abs(point) match consensus
                    if market_key == "totals" and float(o.get("point")) != float(line):
                        continue
                    if market_key == "spreads" and abs(float(o.get("point"))) != float(line):
                        continue

                price = o.get("price")
                if price is None:
                    continue
                price = float(price)
                if best is None or price > best:
                    best = price
                    best_book = str(title)
    return best, best_book


def _spread_signed_point(game: Dict[str, Any], team: str, line_abs: float) -> Optional[float]:
    # return the signed point for `team` at abs(point)==line_abs
    for bm in (game.get("bookmakers") or []):
        for m in (bm.get("markets") or []):
            if m.get("key") != "spreads":
                continue
            for o in (m.get("outcomes") or []):
                if str(o.get("name")) != str(team):
                    continue
                if o.get("point") is None:
                    continue
                if abs(float(o.get("point"))) != float(line_abs):
                    continue
                return float(o.get("point"))
    return None


def _make_team_candidates(games: List[Dict[str, Any]], cfg: Config, features: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []

    for g in games:
        home = g.get("home_team")
        away = g.get("away_team")
        if not home or not away:
            continue

        match = f"{away} @ {home}"

        # PRIORITY: spreads -> totals -> h2h
        for mk in ("spreads", "totals", "h2h"):
            if mk not in cfg.team_markets:
                continue

            struct = _collect_two_way_market(g, mk)
            if not struct:
                continue

            if mk == "h2h":
                sides = struct["sides"]
                if home not in sides or away not in sides:
                    continue

                med_home = _median([float(x) for x in sides[home]])
                med_away = _median([float(x) for x in sides[away]])
                if med_home is None or med_away is None:
                    continue

                pA, pB = _no_vig(_imp(med_home), _imp(med_away))
                # p_mkt for each side
                for team, med, p_mkt in [(home, med_home, pA), (away, med_away, pB)]:
                    best, book = _best_odds(g, "h2h", team, None)
                    if best is None or book is None:
                        continue
                    if not (cfg.odds_min <= best <= cfg.odds_max):
                        continue

                    p_model = model_prob_for_team_market("H2H", team, None, away, home, features)
                    if p_model is None:
                        continue

                    p_real = _clip(float(p_model), float(p_mkt), cfg.clip_vs_market)
                    p_real, edge = _haircut(p_real, float(p_mkt), cfg.haircut_trigger, cfg.haircut_rate)

                    if edge > cfg.edge_refuse:
                        continue

                    ev = p_real * best - 1.0
                    if ev <= 0:
                        continue

                    dev = _dev(best, float(med))
                    sc = _score(edge, ev, dev)

                    cands.append({
                        "match": match,
                        "market": "H2H",
                        "selection": team,
                        "line": None,
                        "odds": float(best),
                        "book": book,
                        "p_model": float(p_model),
                        "p_mkt": float(p_mkt),
                        "fair_prob": float(p_real),
                        "edge": float(edge),
                        "ev": float(ev),
                        "dev": float(dev),
                        "score": float(sc),
                        "why": f"Model vs no-vig market (clip±{cfg.clip_vs_market:.2f}, haircut>{cfg.haircut_trigger:.2f})."
                    })

            if mk == "totals":
                sides = struct["sides"]
                line = struct["line"]
                if "Over" not in sides or "Under" not in sides:
                    continue

                med_o = _median([float(x) for x in sides["Over"]])
                med_u = _median([float(x) for x in sides["Under"]])
                if med_o is None or med_u is None:
                    continue

                pO, pU = _no_vig(_imp(med_o), _imp(med_u))

                for sel, med, p_mkt in [("Over", med_o, pO), ("Under", med_u, pU)]:
                    best, book = _best_odds(g, "totals", sel, line)
                    if best is None or book is None:
                        continue
                    if not (cfg.odds_min <= best <= cfg.odds_max):
                        continue

                    p_model = model_prob_for_team_market("TOTAL", sel, float(line), away, home, features)
                    if p_model is None:
                        continue

                    p_real = _clip(float(p_model), float(p_mkt), cfg.clip_vs_market)
                    p_real, edge = _haircut(p_real, float(p_mkt), cfg.haircut_trigger, cfg.haircut_rate)
                    if edge > cfg.edge_refuse:
                        continue

                    ev = p_real * best - 1.0
                    if ev <= 0:
                        continue

                    dev = _dev(best, float(med))
                    sc = _score(edge, ev, dev)

                    cands.append({
                        "match": match,
                        "market": "TOTAL",
                        "selection": sel,
                        "line": float(line),
                        "odds": float(best),
                        "book": book,
                        "p_model": float(p_model),
                        "p_mkt": float(p_mkt),
                        "fair_prob": float(p_real),
                        "edge": float(edge),
                        "ev": float(ev),
                        "dev": float(dev),
                        "score": float(sc),
                        "why": f"Expected total vs line (calibrated to market no-vig)."
                    })

            if mk == "spreads":
                sides = struct["sides"]
                line_abs = struct["line"]
                if home not in sides or away not in sides:
                    continue

                # med odds per team
                med_home = _median([float(x[0]) for x in sides[home]])
                med_away = _median([float(x[0]) for x in sides[away]])
                if med_home is None or med_away is None:
                    continue

                pH, pA = _no_vig(_imp(med_home), _imp(med_away))

                for team, med, p_mkt in [(home, med_home, pH), (away, med_away, pA)]:
                    best, book = _best_odds(g, "spreads", team, line_abs)
                    if best is None or book is None:
                        continue
                    if not (cfg.odds_min <= best <= cfg.odds_max):
                        continue

                    signed = _spread_signed_point(g, team, line_abs)
                    if signed is None:
                        continue

                    p_model = model_prob_for_team_market("SPREAD", team, float(signed), away, home, features)
                    if p_model is None:
                        continue

                    p_real = _clip(float(p_model), float(p_mkt), cfg.clip_vs_market)
                    p_real, edge = _haircut(p_real, float(p_mkt), cfg.haircut_trigger, cfg.haircut_rate)
                    if edge > cfg.edge_refuse:
                        continue

                    ev = p_real * best - 1.0
                    if ev <= 0:
                        continue

                    dev = _dev(best, float(med))
                    sc = _score(edge, ev, dev)

                    cands.append({
                        "match": match,
                        "market": "SPREAD",
                        "selection": team,
                        "line": float(signed),
                        "odds": float(best),
                        "book": book,
                        "p_model": float(p_model),
                        "p_mkt": float(p_mkt),
                        "fair_prob": float(p_real),
                        "edge": float(edge),
                        "ev": float(ev),
                        "dev": float(dev),
                        "score": float(sc),
                        "why": "Margin model (net rating + home adv) vs spread line."
                    })

    return cands


def _diversify_team(cands: List[Dict[str, Any]], cfg: Config) -> List[Dict[str, Any]]:
    # rank by score desc
    cands = sorted(cands, key=lambda x: (x["score"], x["edge"], x["ev"]), reverse=True)

    picked: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    # strict pass first
    for tier_edge in (cfg.edge_min_strict, cfg.edge_min_fill):
        for c in cands:
            if len(picked) >= cfg.max_team_picks:
                break
            if c["match"] in used_matches:
                continue
            if c["market"] == "H2H":
                if ml_count >= cfg.max_ml_per_day:
                    continue
            if c["edge"] < tier_edge:
                continue

            picked.append(c)
            used_matches.add(c["match"])
            if c["market"] == "H2H":
                ml_count += 1

        if len(picked) >= cfg.max_team_picks:
            break

    return picked


def _fetch_props(cfg: Config) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not cfg.prop_markets:
        return [], {"note": "no prop_markets in config"}

    games, meta = fetch_odds_with_fallback(
        sport_key=cfg.sport_key,
        markets=cfg.prop_markets,
        regions_priority=cfg.regions_priority,
        preferred_books=cfg.preferred_books,
    )
    return games, meta


def _make_prop_candidates(cfg: Config, props_games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Evaluate player props using nba_api game logs (stat-only), then clip/haircut vs market.
    """
    cands: List[Dict[str, Any]] = []
    season = _season_string(datetime.now(timezone.utc))

    # Helper to compute consensus spread for blowout penalty per match (if present in same game object)
    def consensus_spread_abs(g: Dict[str, Any]) -> Optional[float]:
        struct = _collect_two_way_market(g, "spreads")
        if not struct:
            return None
        return float(struct["line"])

    for g in props_games:
        home = g.get("home_team")
        away = g.get("away_team")
        if not home or not away:
            continue
        match = f"{away} @ {home}"

        blowout_abs = consensus_spread_abs(g)

        for bm in (g.get("bookmakers") or []):
            for m in (bm.get("markets") or []):
                mk = m.get("key")
                if mk not in cfg.prop_markets:
                    continue

                # OddsAPI props format: name=Over/Under, description=player, point=line
                # group by (player,line)
                groups: Dict[Tuple[str, float], Dict[str, List[float]]] = {}
                best_by_side: Dict[Tuple[str, float, str], Tuple[float, str]] = {}

                for o in (m.get("outcomes") or []):
                    side = o.get("name")
                    player = o.get("description") or o.get("participant") or o.get("player")
                    price = o.get("price")
                    line = o.get("point")

                    if not side or not player or price is None or line is None:
                        continue
                    side = str(side)
                    player = str(player).strip()
                    line = float(line)
                    price = float(price)

                    if side not in ("Over", "Under"):
                        continue

                    groups.setdefault((player, line), {}).setdefault(side, []).append(price)

                    key = (player, line, side)
                    cur = best_by_side.get(key)
                    if cur is None or price > cur[0]:
                        best_by_side[key] = (price, bm.get("title") or bm.get("key") or "Unknown")

                # evaluate groups
                for (player, line), sides in groups.items():
                    if "Over" not in sides or "Under" not in sides:
                        continue

                    med_o = _median(sides["Over"])
                    med_u = _median(sides["Under"])
                    if med_o is None or med_u is None:
                        continue

                    pO, pU = _no_vig(_imp(med_o), _imp(med_u))

                    # map prop market to stat field
                    if mk == "player_points":
                        stat = "PTS"
                    elif mk == "player_rebounds":
                        stat = "REB"
                    elif mk == "player_assists":
                        stat = "AST"
                    elif mk == "player_points_rebounds_assists":
                        stat = "PRA"
                    else:
                        continue

                    # model from nba_api logs (stat-only)
                    info = prob_over_from_logs(
                        player_name=player,
                        stat=stat,
                        line=line,
                        season=season,
                        last_n=cfg.props_last_n,
                        min_games=cfg.props_min_games,
                    )
                    if not info:
                        continue

                    p_model_over = float(info["p_over"])
                    # blowout penalty for props if spread huge
                    if blowout_abs is not None and blowout_abs >= cfg.props_blowout_penalty_spread:
                        # penalize towards 0.5 a bit
                        p_model_over = 0.5 + (p_model_over - 0.5) * 0.85

                    for side, med, p_mkt, p_model in [
                        ("Over", med_o, pO, p_model_over),
                        ("Under", med_u, pU, 1.0 - p_model_over),
                    ]:
                        best_tuple = best_by_side.get((player, line, side))
                        if not best_tuple:
                            continue
                        best, book = best_tuple

                        if not (cfg.odds_min <= best <= cfg.odds_max):
                            continue

                        p_real = _clip(float(p_model), float(p_mkt), cfg.clip_vs_market)
                        p_real, edge = _haircut(p_real, float(p_mkt), cfg.haircut_trigger, cfg.haircut_rate)
                        if edge > cfg.edge_refuse:
                            continue

                        ev = p_real * best - 1.0
                        if ev <= 0:
                            continue

                        dev = _dev(best, float(med))
                        sc = _score(edge, ev, dev)

                        cands.append({
                            "match": match,
                            "market": mk,
                            "selection": f"{player} {side}",
                            "player": player,
                            "line": float(line),
                            "odds": float(best),
                            "book": str(book),
                            "p_model": float(p_model),
                            "p_mkt": float(p_mkt),
                            "fair_prob": float(p_real),
                            "edge": float(edge),
                            "ev": float(ev),
                            "dev": float(dev),
                            "score": float(sc),
                            "why": f"Stat-only ({stat}) last{info['games_used']} games. avg_min={info.get('avg_min')}"
                        })

    return cands


def _diversify_props(cands: List[Dict[str, Any]], cfg: Config) -> List[Dict[str, Any]]:
    cands = sorted(cands, key=lambda x: (x["score"], x["edge"], x["ev"]), reverse=True)

    picked: List[Dict[str, Any]] = []
    used_players = set()

    for tier_edge in (cfg.edge_min_strict, cfg.edge_min_fill):
        for c in cands:
            if len(picked) >= cfg.max_prop_picks:
                break
            pl = c.get("player")
            if pl and pl in used_players:
                continue
            if c["edge"] < tier_edge:
                continue

            picked.append(c)
            if pl:
                used_players.add(pl)

        if len(picked) >= cfg.max_prop_picks:
            break

    return picked


def run_engine(team_games: List[Dict[str, Any]], cfg: Config) -> Dict[str, Any]:
    features = _load_team_features()

    team_cands = _make_team_candidates(team_games, cfg, features)
    team_picks = _diversify_team(team_cands, cfg)

    # props fetch separately
    props_games, meta_props = _fetch_props(cfg)
    prop_picks: List[Dict[str, Any]] = []
    props_note = None
    if props_games:
        prop_cands = _make_prop_candidates(cfg, props_games)
        prop_picks = _diversify_props(prop_cands, cfg)
    else:
        props_note = meta_props.get("error") or "props unavailable (plan/markets/region)"

    meta = {
        "games": len(team_games),
        "markets_tested": len(team_cands),
        "model_weight": cfg.model_weight,
        "clip_vs_market": cfg.clip_vs_market,
        "max_ml_per_day": cfg.max_ml_per_day,
        "max_odds_ml": cfg.odds_max,
        "props_games": len(props_games) if props_games else 0,
        "props_note": props_note,
    }

    return {"meta": meta, "team_picks": team_picks, "prop_picks": prop_picks}
