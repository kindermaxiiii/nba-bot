# props_engine_v6.py
from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
import time
import statistics as stats

import numpy as np
from nba_api.stats.static import players as nba_players
from nba_api.stats.endpoints import playergamelog

from utils import dec_to_prob, implied_prob_no_vig_two_way, clamp, today_utc


SUPPORTED_PROP_MARKETS = {
    "player_points": ("PTS", ("PTS",)),
    "player_rebounds": ("REB", ("REB",)),
    "player_assists": ("AST", ("AST",)),
    "player_points_rebounds_assists": ("PRA", ("PTS", "REB", "AST")),
    "player_points_rebounds": ("PR", ("PTS", "REB")),
    "player_points_assists": ("PA", ("PTS", "AST")),
    "player_rebounds_assists": ("RA", ("REB", "AST")),
}


@dataclass
class Offer:
    match: str
    market_key: str
    player: str
    line: float
    side: str
    odds: float
    book: str
    p_mkt: float


def _safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _seed_for(player_id: int, market_key: str) -> int:
    s = f"{today_utc()}|{player_id}|{market_key}"
    h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    return int(h, 16)


def _player_id(full_name: str) -> Optional[int]:
    cand = nba_players.find_players_by_full_name(full_name)
    if not cand:
        parts = full_name.split()
        if parts:
            cand = nba_players.find_players_by_last_name(parts[-1])
    if not cand:
        return None
    # prefer active
    cand = sorted(cand, key=lambda x: (x.get("is_active", False), x.get("id", 0)), reverse=True)
    return int(cand[0]["id"])


def _cache_path(player_id: int) -> str:
    os.makedirs("data/cache", exist_ok=True)
    return os.path.join("data/cache", f"player_{player_id}.json")


def _load_cache(player_id: int) -> Optional[List[Dict[str, Any]]]:
    p = _cache_path(player_id)
    if not os.path.exists(p):
        return None
    try:
        obj = json.loads(open(p, "r", encoding="utf-8").read())
        if obj.get("date_utc") != today_utc():
            return None
        recs = obj.get("records")
        return recs if isinstance(recs, list) else None
    except Exception:
        return None


def _save_cache(player_id: int, records: List[Dict[str, Any]]) -> None:
    p = _cache_path(player_id)
    obj = {"date_utc": today_utc(), "records": records}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _fetch_gamelog(player_id: int, season: str) -> Optional[List[Dict[str, Any]]]:
    cached = _load_cache(player_id)
    if cached is not None:
        return cached
    try:
        df = playergamelog.PlayerGameLog(player_id=player_id, season=season, timeout=30).get_data_frames()[0]
        df = df.head(25)
        recs = df.to_dict("records")
        _save_cache(player_id, recs)
        return recs
    except Exception:
        return None


def _minutes_projection(records: List[Dict[str, Any]]) -> Tuple[float, float, float]:
    mins = []
    for r in records[:20]:
        m = _safe_float(r.get("MIN"))
        if m is not None and m > 0:
            mins.append(m)
    if len(mins) < 6:
        mu = float(stats.mean(mins)) if mins else 28.0
        sd = float(stats.pstdev(mins)) if len(mins) >= 2 else 6.0
    else:
        alpha = 0.35
        mu = mins[0]
        for m in mins[1:]:
            mu = alpha * m + (1.0 - alpha) * mu
        sd = float(stats.pstdev(mins))
    cv = (sd / mu) if mu > 0 else 0.35
    frag = clamp(2.0 + 18.0 * cv, 0.0, 10.0)
    return mu, max(3.0, sd), frag


def _fatigue_mult(records: List[Dict[str, Any]]) -> float:
    # use last 8 dates
    dates = []
    for r in records[:8]:
        s = r.get("GAME_DATE")
        if not s:
            continue
        try:
            dt = datetime.strptime(s, "%b %d, %Y").replace(tzinfo=timezone.utc)
            dates.append(dt)
        except Exception:
            continue
    if len(dates) < 2:
        return 1.0
    gaps = []
    for i in range(len(dates) - 1):
        gaps.append(abs((dates[i] - dates[i+1]).total_seconds()) / 86400.0)
    b2b = any(g <= 1.2 for g in gaps)
    three_in_four = sum(1 for g in gaps if g <= 2.2) >= 2
    mult = 1.0
    if b2b:
        mult *= 0.965
    if three_in_four:
        mult *= 0.97
    return mult


def _rate_per_min(records: List[Dict[str, Any]], stat_key: str) -> Tuple[float, float]:
    pairs = []
    for r in records[:20]:
        m = _safe_float(r.get("MIN"))
        x = _safe_float(r.get(stat_key))
        if m is None or x is None or m <= 0:
            continue
        pairs.append((m, x))
    if len(pairs) < 6:
        xs = [x for _, x in pairs]
        mu = float(stats.mean(xs)) if xs else 0.0
        sd = float(stats.pstdev(xs)) if len(xs) >= 2 else max(1.0, 0.35 * mu)
        return (mu / 30.0), max(0.01, sd / 30.0)
    rates = [x / m for m, x in pairs]
    mu = float(stats.mean(rates))
    sd = float(stats.pstdev(rates)) if len(rates) >= 2 else max(0.01, 0.12 * mu)
    return mu, max(0.01, sd)


def _blowout_mult(spread_abs: Optional[float]) -> float:
    if spread_abs is None:
        return 1.0
    x = abs(float(spread_abs))
    return 1.0 + 0.02 * min(18.0, x)


def _parse_offers(games: List[Dict[str, Any]]) -> List[Offer]:
    offers: List[Offer] = []
    for g in games:
        match = f"{g.get('away_team')} @ {g.get('home_team')}"
        for bm in (g.get("bookmakers") or []):
            book = bm.get("title") or bm.get("key") or "book"
            for mk in (bm.get("markets") or []):
                mkey = mk.get("key")
                if mkey not in SUPPORTED_PROP_MARKETS:
                    continue
                for o in (mk.get("outcomes") or []):
                    side = (o.get("name") or "").strip()
                    if side not in ("Over", "Under"):
                        continue
                    player = (o.get("description") or o.get("player") or "").strip()
                    if not player:
                        continue
                    line = _safe_float(o.get("point"))
                    odds = _safe_float(o.get("price"))
                    if line is None or odds is None:
                        continue
                    offers.append(Offer(match, mkey, player, float(line), side, float(odds), str(book), dec_to_prob(float(odds))))
    # no-vig per (match, market, player, line, book)
    groups: Dict[Tuple[str, str, str, float, str], List[Offer]] = {}
    for o in offers:
        k = (o.match, o.market_key, o.player, o.line, o.book)
        groups.setdefault(k, []).append(o)
    for k, lst in groups.items():
        over = next((x for x in lst if x.side == "Over"), None)
        under = next((x for x in lst if x.side == "Under"), None)
        if over and under:
            po, pu = implied_prob_no_vig_two_way(over.odds, under.odds)
            over.p_mkt = po
            under.p_mkt = pu
    return offers


def build_prop_candidates(
    games: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    team_spread_map: Dict[str, float],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns candidates + meta. If no supported prop markets in response -> candidates empty with reason.
    """
    offers = _parse_offers(games)
    if not offers:
        return [], {"ok": False, "reason": "No player prop markets in OddsAPI response."}

    min_odds = float(cfg.get("min_odds", 1.5))
    max_odds = float(cfg.get("max_odds", 2.2))
    offers = [o for o in offers if (min_odds <= o.odds <= max_odds)]
    if not offers:
        return [], {"ok": False, "reason": "Props present but none in odds range."}

    season = cfg.get("nba_season", "2025-26")
    clip = float(cfg.get("clip_vs_market", 0.08))
    model_w = float(cfg.get("props_model_weight", cfg.get("model_weight", 0.7)))
    min_edge = float(cfg.get("min_edge_props", cfg.get("min_edge", 0.02)))
    min_ev = float(cfg.get("min_ev", 0.0))
    edge_refuse = float(cfg.get("edge_refuse", 0.15))
    haircut_trigger = float(cfg.get("haircut_trigger", 0.06))
    haircut_rate = float(cfg.get("haircut_rate", 0.30))

    # Cache gamelogs per player
    gamelog_cache: Dict[int, List[Dict[str, Any]]] = {}

    # Group offers by player and market to reuse draws
    by_player_market: Dict[Tuple[str, str], List[Offer]] = {}
    for o in offers:
        by_player_market.setdefault((o.player, o.market_key), []).append(o)

    candidates: List[Dict[str, Any]] = []
    refusals = {
        "no_player_id": 0,
        "no_gamelog": 0,
        "insufficient_games": 0,
        "ev<=0": 0,
        "edge<th": 0,
        "edge_refuse": 0,
    }

    for (player, market_key), lst in by_player_market.items():
        pid = _player_id(player)
        if not pid:
            refusals["no_player_id"] += len(lst)
            continue

        recs = gamelog_cache.get(pid)
        if recs is None:
            recs = _fetch_gamelog(pid, season)
            if recs is None:
                refusals["no_gamelog"] += len(lst)
                continue
            gamelog_cache[pid] = recs
            time.sleep(0.10)

        if len(recs) < int(cfg.get("props_min_games", 8)):
            refusals["insufficient_games"] += len(lst)
            continue

        mu_min, sd_min, frag = _minutes_projection(recs)
        fat = _fatigue_mult(recs)

        spread_abs = team_spread_map.get(lst[0].match)
        blow = _blowout_mult(spread_abs)

        # Build minutes draws once (max 20000)
        seed = _seed_for(pid, market_key)
        rng = np.random.default_rng(seed)

        n_screen = int(cfg.get("props_n_screen", 5000))
        n_full = int(cfg.get("props_n_full", 20000))
        n_full = max(n_screen, n_full)

        mins = rng.normal(mu_min * fat, sd_min * blow, size=n_full)
        mins = np.clip(mins, 8.0, 44.0)

        label, comps = SUPPORTED_PROP_MARKETS[market_key]

        # Precompute component distributions using the same minutes
        comp_vals: Dict[str, np.ndarray] = {}
        for comp in comps:
            rmu, rsd = _rate_per_min(recs, comp)
            # rate draws (screen then full)
            rates = rng.normal(rmu, rsd, size=n_full)
            rates = np.clip(rates, 0.0, None)
            mean = rates * mins
            noise_sd = np.maximum(
                1.0,
                0.35 * np.sqrt(np.maximum(mean, 0.1)) + 0.25 * mean * (rsd / max(rmu, 1e-6))
            )
            vals = rng.normal(mean, noise_sd)
            comp_vals[comp] = vals

        total_vals = None
        if len(comps) == 1:
            total_vals = comp_vals[comps[0]]
        else:
            total_vals = np.zeros(n_full, dtype=float)
            for comp in comps:
                total_vals += comp_vals[comp]

        # Evaluate each unique line using screening then refine for best ones
        # Group by line
        by_line: Dict[float, Dict[str, Offer]] = {}
        for o in lst:
            by_line.setdefault(o.line, {})[o.side] = o

        # Build preliminary scores using screening slice
        prelim: List[Tuple[float, float, str]] = []  # (abs_edge, line, best_side)
        line_stats: Dict[Tuple[float, str], float] = {}

        for line, sides in by_line.items():
            screen_vals = total_vals[:n_screen]
            p_over = float(np.mean(screen_vals > float(line)))
            # store
            line_stats[(line, "Over")] = p_over
            line_stats[(line, "Under")] = 1.0 - p_over

            # find which side exists and has more edge
            best = None
            best_abs = 0.0
            best_side = None
            for side, o in sides.items():
                p_model = line_stats[(line, side)]
                p_mkt = float(o.p_mkt)
                # clip p_model vs p_mkt
                d = p_model - p_mkt
                if abs(d) > clip:
                    p_model = p_mkt + (clip if d > 0 else -clip)
                p_real = model_w * p_model + (1.0 - model_w) * p_mkt
                edge = p_real - p_mkt
                abs_edge = abs(edge)
                if abs_edge > best_abs:
                    best_abs = abs_edge
                    best_side = side
                    best = edge
            if best_side is not None:
                prelim.append((best_abs, line, best_side))

        # refine only top K lines
        prelim.sort(reverse=True, key=lambda x: x[0])
        refine_k = int(cfg.get("props_refine_k", 8))
        refine_set = set((line, side) for _, line, side in prelim[:refine_k])

        for line, sides in by_line.items():
            # compute full p_over only if needed
            if any((line, side) in refine_set for side in sides.keys()):
                p_over = float(np.mean(total_vals > float(line)))
                line_stats[(line, "Over")] = p_over
                line_stats[(line, "Under")] = 1.0 - p_over

            for side, o in sides.items():
                p_model = float(line_stats[(line, side)])
                p_mkt = float(o.p_mkt)

                d = p_model - p_mkt
                dev = abs(d)
                if dev > clip:
                    p_model = p_mkt + (clip if d > 0 else -clip)

                p_real = model_w * p_model + (1.0 - model_w) * p_mkt
                edge_raw = p_real - p_mkt

                # haircut on big edges
                edge = edge_raw
                if edge_raw > haircut_trigger:
                    p_real = p_mkt + (p_real - p_mkt) * (1.0 - haircut_rate)
                    edge = p_real - p_mkt

                if edge > edge_refuse:
                    refusals["edge_refuse"] += 1
                    continue

                ev = p_real * float(o.odds) - 1.0
                if ev <= min_ev:
                    refusals["ev<=0"] += 1
                    continue
                if edge < min_edge:
                    refusals["edge<th"] += 1
                    continue

                # dispersion penalty: per (player,market,line,side) use odds dispersion across books by scanning offers list
                odds_list = [x.odds for x in offers if (x.player == player and x.market_key == market_key and x.line == line and x.side == side)]
                if len(odds_list) >= 3:
                    med = float(np.median(odds_list))
                    sd = float(np.std(odds_list))
                    disp = (sd / med) if med > 0 else 0.0
                else:
                    disp = 0.0

                score = (100.0 * ev) + (60.0 * edge) - (2.0 * frag) - (30.0 * disp)

                selection = f"{player} {side} {line:g} ({label})"
                why = f"V6: minutes(EWMA)={mu_min:.1f}±{sd_min:.1f}, fatigue={fat:.3f}, blow={blow:.2f}, frag={frag:.1f}, disp={disp:.3f}"

                candidates.append({
                    "match": o.match,
                    "market": market_key,
                    "selection": selection,
                    "player": player,
                    "line": float(line),
                    "odds": float(o.odds),
                    "book": o.book,
                    "p_model": float(p_model),
                    "p_mkt": float(p_mkt),
                    "p_real": float(p_real),
                    "ev": float(ev),
                    "edge": float(edge),
                    "dev": float(dev),
                    "score": float(score),
                    "why": why,
                    "fragility": float(frag),
                })

    candidates.sort(key=lambda x: (x["score"], x["edge"], x["ev"]), reverse=True)
    return candidates, {"ok": True, "offers": len(offers), "candidates": len(candidates), "refusals": refusals}
