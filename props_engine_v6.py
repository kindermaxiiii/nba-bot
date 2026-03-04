# props_engine_v6.py
"""
Institutional V6 (lightweight) player-props engine.

Design constraints:
- Must not exceed GitHub Actions runtime; only fetch NBA API data for players that appear in OddsAPI prop markets.
- If OddsAPI props markets are unavailable (common on some plans), caller should skip.

Approach:
- Parse OddsAPI player markets into (player, stat, line, over_odds, under_odds, book).
- Build p_model with a small Monte Carlo using NBA API game logs:
  * minutes projection (EWMA + variance)
  * usage proxy for points (possessions proxy/min)
  * fatigue proxy (back-to-back / 3-in-4 from recent game dates)
  * blowout fragility proxy from spread magnitude
- Convert to p_real with market clipping (cfg.clip_vs_market) and discipline filters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math
import time
import statistics as stats
from datetime import datetime, timezone

import numpy as np

from nba_api.stats.static import players as nba_players
from nba_api.stats.endpoints import playergamelog

from utils import (
    dec_to_prob,
    implied_prob_no_vig_two_way,
    now_iso,
)

SUPPORTED_PROP_MARKETS = {
    "player_points": "PTS",
    "player_rebounds": "REB",
    "player_assists": "AST",
    "player_points_rebounds_assists": "PRA",
    "player_points_assists": "PA",
    "player_points_rebounds": "PR",
    "player_rebounds_assists": "RA",
}


@dataclass
class PropOffer:
    match: str
    market: str
    player: str
    line: float
    side: str  # "Over" or "Under"
    odds: float
    book: str
    p_mkt: float  # no-vig (two-way) if possible, else implied


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def parse_prop_offers(games: List[Dict[str, Any]]) -> List[PropOffer]:
    offers: List[PropOffer] = []
    for g in games:
        match = f"{g.get('away_team')} @ {g.get('home_team')}"
        for bm in (g.get("bookmakers") or []):
            book = bm.get("title") or bm.get("key") or "book"
            for mk in (bm.get("markets") or []):
                key = mk.get("key")
                if key not in SUPPORTED_PROP_MARKETS:
                    continue
                # OddsAPI props outcomes often look like:
                # {"name":"Over","price":1.87,"point":24.5,"description":"LeBron James"}
                # {"name":"Under",...}
                for out in (mk.get("outcomes") or []):
                    side = (out.get("name") or "").strip()
                    if side not in ("Over", "Under"):
                        continue
                    player = (out.get("description") or out.get("player") or "").strip()
                    if not player:
                        # some feeds place player in "name" and side in "description" (rare); ignore safely
                        continue
                    line = _safe_float(out.get("point"))
                    odds = _safe_float(out.get("price"))
                    if line is None or odds is None:
                        continue
                    offers.append(
                        PropOffer(
                            match=match,
                            market=key,
                            player=player,
                            line=float(line),
                            side=side,
                            odds=float(odds),
                            book=book,
                            p_mkt=dec_to_prob(float(odds)),  # temporary; will no-vig later per (player, market, line, book)
                        )
                    )

    # compute no-vig p_mkt per two-way pair (Over/Under) if both exist at same book/line
    by_pair: Dict[Tuple[str, str, float, str, str], List[PropOffer]] = {}
    # key: (match, market, line, book, player)
    for o in offers:
        k = (o.match, o.market, o.line, o.book, o.player)
        by_pair.setdefault(k, []).append(o)

    for k, lst in by_pair.items():
        if len(lst) < 2:
            continue
        over = next((x for x in lst if x.side == "Over"), None)
        under = next((x for x in lst if x.side == "Under"), None)
        if over and under:
            p_over, p_under = implied_prob_no_vig_two_way(over.odds, under.odds)
            over.p_mkt = p_over
            under.p_mkt = p_under

    return offers


def _player_id(full_name: str) -> Optional[int]:
    # Try exact / close match
    found = nba_players.find_players_by_full_name(full_name)
    if not found:
        # Try fallback by last name only (take best)
        parts = full_name.split()
        if len(parts) >= 2:
            found = nba_players.find_players_by_last_name(parts[-1])
    if not found:
        return None
    # choose most recent active if possible
    found_sorted = sorted(found, key=lambda x: (x.get("is_active", False), x.get("id", 0)), reverse=True)
    return int(found_sorted[0]["id"])


def _fetch_gamelog(player_id: int, season: str = "2025-26") -> List[Dict[str, Any]]:
    # NBA API can rate-limit; keep it minimal.
    gl = playergamelog.PlayerGameLog(player_id=player_id, season=season, timeout=30)
    df = gl.get_data_frames()[0]
    # Keep last 20 games
    df = df.head(20)
    out = df.to_dict("records")
    return out


def _minutes_projection(records: List[Dict[str, Any]]) -> Tuple[float, float, float]:
    # EWMA mean + std; plus a fragility score 0-10
    mins = [float(r.get("MIN", 0) or 0) for r in records if (r.get("MIN") is not None)]
    mins = [m for m in mins if m > 0]
    if len(mins) < 5:
        mu = float(stats.mean(mins)) if mins else 28.0
        sd = float(stats.pstdev(mins)) if len(mins) >= 2 else 6.0
    else:
        alpha = 0.35
        mu = mins[-1]
        for m in reversed(mins[:-1]):
            mu = alpha * m + (1 - alpha) * mu
        sd = float(stats.pstdev(mins))
    # fragility: higher sd / mean and low sample -> higher
    cv = (sd / mu) if mu > 0 else 0.3
    frag = min(10.0, 2.0 + 18.0 * cv)
    return mu, max(3.0, sd), frag


def _fatigue_penalty(records: List[Dict[str, Any]]) -> float:
    # Use dates to approximate B2B/3in4. Returns multiplier on minutes (<=1).
    # GAME_DATE in format 'MAR 03, 2026'
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
    # compute gaps between consecutive games
    gaps = []
    for i in range(len(dates) - 1):
        gaps.append(abs((dates[i] - dates[i+1]).total_seconds()) / 86400.0)
    b2b = any(g <= 1.2 for g in gaps)  # within ~1 day
    three_in_four = sum(1 for g in gaps if g <= 2.2) >= 2
    penalty = 1.0
    if b2b:
        penalty *= 0.965
    if three_in_four:
        penalty *= 0.97
    return penalty


def _stat_series(records: List[Dict[str, Any]], stat_key: str) -> List[float]:
    vals = []
    for r in records:
        v = r.get(stat_key)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except Exception:
            continue
    return vals


def _rate_model(records: List[Dict[str, Any]], stat_key: str) -> Tuple[float, float]:
    # rate per minute mean and std
    mins = _stat_series(records, "MIN")
    xs = _stat_series(records, stat_key)
    pairs = [(m, x) for m, x in zip(mins, xs) if m and m > 0]
    if len(pairs) < 5:
        # fallback to per-game mean, assume sd moderate
        xvals = [x for _, x in pairs] or xs
        mu = float(stats.mean(xvals)) if xvals else 0.0
        sd = float(stats.pstdev(xvals)) if len(xvals) >= 2 else max(1.0, 0.35 * mu)
        # convert to rate using typical 30 minutes
        return mu / 30.0, max(0.05, sd / 30.0)
    rates = [x / m for m, x in pairs]
    mu = float(stats.mean(rates))
    sd = float(stats.pstdev(rates)) if len(rates) >= 2 else 0.12 * mu
    return mu, max(0.01, sd)


def _blowout_fragility(spread_abs: Optional[float]) -> float:
    # 0..1 multiplier on minutes variance; higher abs spread => more fragility
    if spread_abs is None:
        return 1.0
    x = float(abs(spread_abs))
    # logistic-ish from ~0 at 0 to ~0.35 at 15+
    return 1.0 + 0.02 * min(18.0, x)


def _simulate_prob_over(line: float, mu_min: float, sd_min: float, rate_mu: float, rate_sd: float,
                        fatigue_mult: float, blowout_mult: float, n: int = 25000) -> float:
    rng = np.random.default_rng(12345)
    # minutes: truncated normal
    mins = rng.normal(mu_min * fatigue_mult, sd_min * blowout_mult, size=n)
    mins = np.clip(mins, 8.0, 44.0)
    # rate: truncated normal around rate_mu
    rates = rng.normal(rate_mu, rate_sd, size=n)
    rates = np.clip(rates, 0.0, None)
    # stat: normal with sd proportional to sqrt(minutes) and rate variance
    mean = rates * mins
    # heteroskedastic noise: combine residual variance and Poisson-like
    noise_sd = np.maximum(1.0, 0.35 * np.sqrt(np.maximum(mean, 0.1)) + 0.25 * mean * (rate_sd / max(rate_mu, 1e-6)))
    stat_vals = rng.normal(mean, noise_sd)
    return float(np.mean(stat_vals > line))


def build_prop_candidates_v6(
    games: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    team_spread_map: Dict[str, float],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (prop_candidates, meta).
    prop_candidates items contain:
      - match, market, selection, odds, book, p_model, p_mkt, p_real, ev, edge, dev, score, why, fragility
    """
    offers = parse_prop_offers(games)
    if not offers:
        return [], {"props_available": False, "reason": "No player prop markets present in OddsAPI response."}

    # Keep only offers within odds range
    min_odds = float(cfg.get("min_odds", 1.5))
    max_odds = float(cfg.get("max_odds", 2.2))
    offers = [o for o in offers if (min_odds <= o.odds <= max_odds)]
    if not offers:
        return [], {"props_available": True, "reason": "Prop markets present, but none in configured odds range."}

    # For each prop offer, compute model probability
    season = cfg.get("nba_season", "2025-26")

    cache: Dict[str, Any] = {}
    candidates: List[Dict[str, Any]] = []

    clip = float(cfg.get("clip_vs_market", 0.08))
    model_w = float(cfg.get("model_weight", 0.7))
    min_edge = float(cfg.get("min_edge", 0.02))
    min_ev = float(cfg.get("min_ev", 0.0))

    for o in offers:
        pid = cache.get(("pid", o.player))
        if pid is None:
            pid = _player_id(o.player)
            cache[("pid", o.player)] = pid
        if not pid:
            continue

        recs = cache.get(("gamelog", pid))
        if recs is None:
            try:
                recs = _fetch_gamelog(pid, season=season)
                cache[("gamelog", pid)] = recs
                time.sleep(0.12)  # small pacing
            except Exception:
                continue

        mu_min, sd_min, frag = _minutes_projection(recs)
        fatigue = _fatigue_penalty(recs)

        spread_abs = None
        if o.match in team_spread_map:
            spread_abs = abs(team_spread_map[o.match])
        blow_mult = _blowout_fragility(spread_abs)

        # Map market to stat_key(s)
        stat = SUPPORTED_PROP_MARKETS.get(o.market)
        if not stat:
            continue

        # Composite markets approximated with sum of component sims via shared minutes + independent noise.
        if stat in ("PTS", "REB", "AST"):
            rate_mu, rate_sd = _rate_model(recs, stat)
            p_over = _simulate_prob_over(o.line, mu_min, sd_min, rate_mu, rate_sd, fatigue, blow_mult)
        else:
            # PRA / PR / PA / RA: simulate components and sum (shared minutes)
            comps = []
            for comp in ("PTS", "REB", "AST"):
                if comp == "PTS" and stat not in ("PRA", "PR", "PA"):
                    continue
                if comp == "REB" and stat not in ("PRA", "PR", "RA"):
                    continue
                if comp == "AST" and stat not in ("PRA", "PA", "RA"):
                    continue
                comps.append(comp)
            rng = np.random.default_rng(12345)
            n = 25000
            mins = rng.normal(mu_min * fatigue, sd_min * blow_mult, size=n)
            mins = np.clip(mins, 8.0, 44.0)
            total = np.zeros(n, dtype=float)
            for comp in comps:
                rmu, rsd = _rate_model(recs, comp)
                rates = rng.normal(rmu, rsd, size=n)
                rates = np.clip(rates, 0.0, None)
                mean = rates * mins
                noise_sd = np.maximum(1.0, 0.35 * np.sqrt(np.maximum(mean, 0.1)) + 0.25 * mean * (rsd / max(rmu, 1e-6)))
                vals = rng.normal(mean, noise_sd)
                total += vals
            p_over = float(np.mean(total > o.line))

        p_model = p_over if o.side == "Over" else (1.0 - p_over)
        p_mkt = float(o.p_mkt)

        # Clip to market if too far
        dev = abs(p_model - p_mkt)
        if dev > clip:
            # pull towards market
            direction = -1 if p_model > p_mkt else 1
            p_model = p_mkt + direction * clip

        # blend model with market (stability)
        p_real = model_w * p_model + (1.0 - model_w) * p_mkt

        edge = p_real - p_mkt
        ev = p_real * o.odds - 1.0

        if edge < min_edge or ev <= min_ev:
            continue

        # Score: EV + edge, penalize fragility
        score = (100.0 * ev) + (50.0 * edge) - (2.0 * frag)
        why = f"V6: minutes(EWMA)+usage proxy+fatigue+blowout fragility | frag={frag:.1f} fatigue={fatigue:.3f}"
        selection = f"{o.player} {o.side} {o.line:g} ({stat})"

        candidates.append({
            "match": o.match,
            "market": o.market,
            "selection": selection,
            "odds": o.odds,
            "book": o.book,
            "p_model": p_model,
            "p_mkt": p_mkt,
            "p_real": p_real,
            "ev": ev,
            "edge": edge,
            "dev": dev,
            "score": score,
            "why": why,
            "fragility": frag,
        })

    # Rank and keep top N
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates, {"props_available": True, "offers_parsed": len(offers), "candidates": len(candidates)}
