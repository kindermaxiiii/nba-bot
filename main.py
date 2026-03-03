import os
import json
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from dateutil import parser

from odds_api import fetch_odds_with_fallback, OddsApiError
from engine import (
    collect_market_lines,
    collect_team_totals_lines,
    collect_player_prop_lines,
    pick_consensus_line,
    pick_consensus_prop_line,
    analyze_two_way_market,
)
from formatting import format_team_pick, format_prop_pick, format_no_bet
from context import fetch_injuries, build_injury_note, search_player_id, fetch_player_season_minutes

TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

# -------------------------
# CONFIG (NO BANKROLL / NO STAKES)
# -------------------------
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

MIN_BOOKMAKERS = int(CONFIG.get("min_bookmakers", 2))
PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", False))  # for odds depth, default False in ranking mode
MAX_ML_TOTAL = int(CONFIG.get("max_ml_total", 2))
TARGET_TEAM = int(CONFIG.get("target_team", 3))
TARGET_PROPS = int(CONFIG.get("target_props", 3))

# Region strategy (depth first)
REGIONS_TEAM = CONFIG.get("regions_team", ["us", "us2", "uk", "eu", "au", "fr"])
REGIONS_PROPS = CONFIG.get("regions_props", ["us", "us2", "uk", "eu", "au", "fr"])

# Load team features (built by build_team_features.py). Optional.
TEAM_FEATURES_PATH = CONFIG.get("team_features_path", "data/team_features.json")
try:
    with open(TEAM_FEATURES_PATH, "r", encoding="utf-8") as f:
        TEAM_FEATURES = json.load(f)
except Exception:
    TEAM_FEATURES = {}

def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=20)
    r.raise_for_status()

def is_game_soon(commence_time: str, horizon_hours: int = 36) -> bool:
    try:
        start_dt = parser.isoparse(commence_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1) <= start_dt <= now + timedelta(hours=horizon_hours)
    except Exception:
        return False

def pick_id(p: Dict[str, Any]) -> str:
    base = f"{p.get('match','')}|{p.get('market','')}|{p.get('selection','')}|{p.get('line','')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:14]

# -------------------------
# Injury + minutes engine (robust)
# -------------------------
STATUS_PPLAY = {
    "out": 0.00,
    "doubtful": 0.15,
    "questionable": 0.55,
    "gtd": 0.60,
    "probable": 0.85,
    "available": 0.98,
}

def _infer_status_from_note(note: str) -> str:
    t = (note or "").lower()
    for k in ["out", "doubtful", "questionable", "probable"]:
        if k in t:
            return k
    if "gtd" in t or "game time" in t:
        return "gtd"
    return "available"

def attach_minutes_and_injury(p: Dict[str, Any], injuries: List[Dict[str, Any]]):
    """
    Adds:
      minutes_note, minutes_confidence, minutes_fragility, injury_note
      score_adj penalty if uncertainty high
    """
    match = p.get("match", "")
    injury_note = p.get("injury_note") or build_injury_note(match, injuries)
    p["injury_note"] = injury_note

    # Minutes only for props (player known)
    player = p.get("player")
    if player:
        pid = search_player_id(player)
        mpg = fetch_player_season_minutes(pid) if pid else None
        if mpg is not None:
            status = _infer_status_from_note(injury_note)
            pplay = STATUS_PPLAY.get(status, 0.98)
            minutes_proj = float(mpg) * float(pplay)
            # Confidence: high when available/probable, low when Q/GTD
            conf = 0.90 if status in ("available", "probable") else 0.65 if status in ("questionable", "gtd") else 0.25 if status == "doubtful" else 0.10
            # Fragility: higher when uncertain
            frag = 2.0 if status in ("available", "probable") else 6.5 if status in ("questionable", "gtd") else 8.5 if status == "doubtful" else 9.5
            p["minutes_note"] = f"{minutes_proj:.1f} min proj. (base {mpg:.1f}, P(play)={pplay:.2f})"
            p["minutes_confidence"] = conf
            p["minutes_fragility"] = frag

            # Apply penalty on score for low confidence (institutional)
            sc = float(p.get("score", 0.0))
            penalty = 0.0
            if conf < 0.75:
                penalty += 8.0
            if conf < 0.50:
                penalty += 8.0
            p["score_adj"] = max(0.0, sc - penalty)
        else:
            p["score_adj"] = float(p.get("score", 0.0))
    else:
        p["score_adj"] = float(p.get("score", 0.0))

def _team_stats_lines(match: str) -> List[str]:
    """
    Uses team_features.json if available.
    Expected schema (best effort): TEAM_FEATURES[team] = {...}
    """
    try:
        away, home = match.split(" @ ")
    except Exception:
        return []
    a = TEAM_FEATURES.get(away) or {}
    h = TEAM_FEATURES.get(home) or {}

    def g(d, k):
        v = d.get(k)
        return None if v is None else float(v)

    lines = []
    # Use common keys if present
    for label, k in [("NetRtg", "net_rating"), ("ORtg", "ortg"), ("DRtg", "drtg"), ("Pace", "pace")]:
        av = g(a, k); hv = g(h, k)
        if av is not None and hv is not None:
            diff = av - hv
            lines.append(f"{label} diff (away-home): {diff:+.2f}")
    return lines

def explain_pick(p: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Produces:
      why bullets (ranking reasons)
      stats bullets (statistical justification)
    """
    why = []
    stats = []

    why.append(f"EV positif ({p['ev']*100:.2f}%) et edge vs implicite ({p['edge']*100:.2f}%).")
    why.append(f"Meilleure cote vs médiane: dev {_fmt_pct(p.get('dev',0.0))} avec {p.get('books_used')}/{p.get('total_books')} books.")
    if p.get("market") in ("MONEYLINE", "MONEYLINE 1H"):
        why.append("ML retenue car c’est la meilleure option du match (comparée à Spread/Total/1H/TT).")

    # Statistical justif
    if p.get("player"):
        if p.get("minutes_note"):
            stats.append(p["minutes_note"])
        if p.get("injury_note"):
            stats.append("Injury context intégré (pénalité score si incertitude).")
    else:
        stats.extend(_team_stats_lines(p.get("match","")))
        if p.get("injury_note"):
            stats.append("Injury context équipe pris en compte (notes).")

    return why, stats

def _fmt_pct(x: float) -> str:
    return f"{float(x)*100:.2f}%"

# -------------------------
# Fetch helpers
# -------------------------
def fetch_team_games_all_markets() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    # Single call with many markets can 422 depending on plan; we do staged fetch and merge.
    base = "h2h,spreads,totals"
    games, meta = fetch_odds_with_fallback(markets=base, regions_priority=REGIONS_TEAM)
    # Try extras best effort
    for mk in ["team_totals", "h2h_h1", "spreads_h1", "totals_h1", "team_totals_h1"]:
        try:
            g2, _ = fetch_odds_with_fallback(markets=mk, regions_priority=REGIONS_TEAM)
        except OddsApiError:
            continue
        # merge by id
        by_id = {g.get("id"): g for g in games if g.get("id")}
        for g in g2:
            gid = g.get("id")
            if gid and gid in by_id:
                # merge bookmakers (append)
                by_id[gid]["bookmakers"] = (by_id[gid].get("bookmakers") or []) + (g.get("bookmakers") or [])
            else:
                games.append(g)
    return games, meta

# -------------------------
# Universe builders
# -------------------------
def build_team_candidates(game: Dict[str, Any], injuries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    home = game.get("home_team"); away = game.get("away_team")
    match = f"{away} @ {home}"
    bookmakers = game.get("bookmakers") or []
    if not bookmakers:
        return []

    injury_note = build_injury_note(match, injuries)
    out: List[Dict[str, Any]] = []

    def add_two_way(market_label: str, line_val: Optional[float], a_key: str, b_key: str, a_entries, b_entries):
        res = analyze_two_way_market(
            match=match,
            market_label=market_label,
            line=line_val,
            outcome_a=a_key,
            outcome_b=b_key,
            entries_a=a_entries,
            entries_b=b_entries,
            min_books=MIN_BOOKMAKERS,
            prefer_fr=PREFER_FR_BOOKS,
        )
        if not res:
            return
        for side in ("A","B"):
            c = res[side]
            c["injury_note"] = injury_note
            # Eligible only if EV>=0
            if float(c.get("ev", -999.0)) >= 0.0:
                out.append(c)

    # ML
    h2h = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h)
    if lk and lk in h2h:
        outs = list(h2h[lk].keys())
        if len(outs) >= 2:
            add_two_way("MONEYLINE", None, outs[0], outs[1], h2h[lk][outs[0]], h2h[lk][outs[1]])

    # TOTAL
    totals = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals)
    if tlk and tlk in totals:
        sides = totals[tlk]
        if "Over" in sides and "Under" in sides:
            add_two_way("TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    # SPREAD
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads)
    if slk and slk in spreads:
        teams = spreads[slk]
        if home in teams and away in teams:
            add_two_way("SPREAD", float(slk), home, away, teams[home], teams[away])

    # TEAM TOTALS (full)
    tt = collect_team_totals_lines(bookmakers, "team_totals").get("teams", {})
    for team, team_lines in (tt or {}).items():
        lk2 = pick_consensus_prop_line(team_lines)
        if not lk2 or lk2 not in team_lines:
            continue
        sides = team_lines[lk2]
        if "Over" in sides and "Under" in sides:
            add_two_way(f"TEAM TOTAL ({team})", float(lk2), "Over", "Under", sides["Over"], sides["Under"])

    # 1H
    h2h1 = collect_market_lines(bookmakers, "h2h_h1")["lines"]
    lk1 = pick_consensus_line(h2h1)
    if lk1 and lk1 in h2h1:
        outs = list(h2h1[lk1].keys())
        if len(outs) >= 2:
            add_two_way("MONEYLINE 1H", None, outs[0], outs[1], h2h1[lk1][outs[0]], h2h1[lk1][outs[1]])

    totals1 = collect_market_lines(bookmakers, "totals_h1")["lines"]
    tlk1 = pick_consensus_line(totals1)
    if tlk1 and tlk1 in totals1:
        sides = totals1[tlk1]
        if "Over" in sides and "Under" in sides:
            add_two_way("TOTAL 1H", float(tlk1), "Over", "Under", sides["Over"], sides["Under"])

    spreads1 = collect_market_lines(bookmakers, "spreads_h1")["lines"]
    slk1 = pick_consensus_line(spreads1)
    if slk1 and slk1 in spreads1:
        teams = spreads1[slk1]
        if home in teams and away in teams:
            add_two_way("SPREAD 1H", float(slk1), home, away, teams[home], teams[away])

    tt1 = collect_team_totals_lines(bookmakers, "team_totals_h1").get("teams", {})
    for team, team_lines in (tt1 or {}).items():
        lk2 = pick_consensus_prop_line(team_lines)
        if not lk2 or lk2 not in team_lines:
            continue
        sides = team_lines[lk2]
        if "Over" in sides and "Under" in sides:
            add_two_way(f"TEAM TOTAL 1H ({team})", float(lk2), "Over", "Under", sides["Over"], sides["Under"])

    return out

def build_prop_candidates(injuries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    prop_market_map = {
        "PROP PTS": "player_points",
        "PROP REB": "player_rebounds",
        "PROP AST": "player_assists",
        "PROP 3PT": "player_threes",
        "PROP PRA": "player_points_rebounds_assists",
        "PROP PR": "player_points_rebounds",
        "PROP PA": "player_points_assists",
        "PROP RA": "player_rebounds_assists",
    }

    out: List[Dict[str, Any]] = []
    chosen_region = None

    for label, market_key in prop_market_map.items():
        try:
            games, meta = fetch_odds_with_fallback(markets=market_key, regions_priority=REGIONS_PROPS)
            chosen_region = chosen_region or meta.get("chosen_region")
        except OddsApiError:
            continue

        for g in games:
            if not is_game_soon(g.get("commence_time", "")):
                continue

            match = f"{g['away_team']} @ {g['home_team']}"
            bookmakers = g.get("bookmakers", []) or []
            if not bookmakers:
                continue

            injury_note = build_injury_note(match, injuries)
            props_struct = collect_player_prop_lines(bookmakers, market_key)["props"]

            for player, player_lines in props_struct.items():
                lk = pick_consensus_prop_line(player_lines)
                if not lk or lk not in player_lines:
                    continue
                sides = player_lines[lk]
                if "Over" not in sides or "Under" not in sides:
                    continue

                res = analyze_two_way_market(
                    match=match,
                    market_label=label,
                    line=float(lk),
                    outcome_a="Over",
                    outcome_b="Under",
                    entries_a=sides["Over"],
                    entries_b=sides["Under"],
                    min_books=MIN_BOOKMAKERS,
                    prefer_fr=PREFER_FR_BOOKS,
                )
                if not res:
                    continue
                for side in ("A","B"):
                    c = res[side]
                    c["player"] = player
                    c["injury_note"] = injury_note
                    if float(c.get("ev", -999.0)) >= 0.0:
                        out.append(c)

    return out, chosen_region

# -------------------------
# Selection rules
# -------------------------
def best_of_match(team_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    For each match, keep only the best candidate (highest score_adj after penalties).
    Ensures ML is only taken if it's the best option in that match.
    """
    by_match: Dict[str, Dict[str, Any]] = {}
    for c in team_candidates:
        m = c.get("match")
        if not m:
            continue
        cur = by_match.get(m)
        if cur is None or float(c.get("score_adj", c.get("score",0))) > float(cur.get("score_adj", cur.get("score",0))):
            by_match[m] = c
    return list(by_match.values())

def select_top_team(team_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    - prefer distinct matches if possible
    - allow up to 2 picks same match only if needed
    - max 2 ML total
    - correlation guard: avoid ML+Spread same side, Total+TeamTotal same side when picking 2 from same match
    """
    # rank by score_adj then EV then books
    ranked = sorted(team_candidates, key=lambda x: (x.get("score_adj", x.get("score",0.0)), x.get("ev",0.0), x.get("books_used",0)), reverse=True)

    out: List[Dict[str, Any]] = []
    used_match: Dict[str, int] = {}
    ml_count = 0

    def is_ml(p):
        return p.get("market") in ("MONEYLINE", "MONEYLINE 1H")

    for c in ranked:
        if len(out) >= TARGET_TEAM:
            break
        m = c.get("match")
        if not m:
            continue

        # match diversity: try unique first
        if used_match.get(m, 0) >= 1:
            # allow second pick same match only if we still can't reach 3 otherwise (handled by later pass)
            continue

        if is_ml(c) and ml_count >= MAX_ML_TOTAL:
            continue

        out.append(c)
        used_match[m] = used_match.get(m, 0) + 1
        if is_ml(c):
            ml_count += 1

    # Second pass: allow up to 2 picks same match if still short
    if len(out) < TARGET_TEAM:
        for c in ranked:
            if len(out) >= TARGET_TEAM:
                break
            m = c.get("match")
            if not m:
                continue
            if used_match.get(m, 0) >= 2:
                continue
            if pick_id(c) in {pick_id(x) for x in out}:
                continue
            if is_ml(c) and ml_count >= MAX_ML_TOTAL:
                continue

            # correlation guard if this becomes the 2nd pick of the match
            if used_match.get(m, 0) == 1:
                first = next((x for x in out if x.get("match")==m), None)
                if first:
                    if (first.get("market") in ("MONEYLINE","MONEYLINE 1H") and c.get("market") in ("SPREAD","SPREAD 1H")):
                        continue
                    if (first.get("market") in ("SPREAD","SPREAD 1H") and c.get("market") in ("MONEYLINE","MONEYLINE 1H")):
                        continue
                    if (first.get("market") == "TOTAL" and str(c.get("market","")).startswith("TEAM TOTAL")):
                        continue
                    if (str(first.get("market","")).startswith("TEAM TOTAL") and c.get("market") == "TOTAL"):
                        continue

            out.append(c)
            used_match[m] = used_match.get(m, 0) + 1
            if is_ml(c):
                ml_count += 1

    return out[:TARGET_TEAM]

def select_top_props(prop_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked = sorted(prop_candidates, key=lambda x: (x.get("score_adj", x.get("score",0.0)), x.get("ev",0.0), x.get("books_used",0)), reverse=True)
    out: List[Dict[str, Any]] = []
    used_player = set()
    for c in ranked:
        if len(out) >= TARGET_PROPS:
            break
        pl = c.get("player")
        if pl and pl in used_player:
            continue
        out.append(c)
        if pl:
            used_player.add(pl)
    return out[:TARGET_PROPS]

# -------------------------
# Main
# -------------------------
def main():
    # Injuries
    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    # TEAM
    stats_team = {"games_analyzed": 0, "markets_tested": 0, "rejects": {}, "region": None}
    try:
        team_games, meta = fetch_team_games_all_markets()
        stats_team["region"] = meta.get("chosen_region")
    except OddsApiError:
        team_games, meta = [], {}

    team_candidates: List[Dict[str, Any]] = []
    for g in team_games:
        if not is_game_soon(g.get("commence_time","")):
            continue
        stats_team["games_analyzed"] += 1
        team_candidates.extend(build_team_candidates(g, injuries))

    # Attach injury notes, score_adj etc
    for c in team_candidates:
        attach_minutes_and_injury(c, injuries)

    # Best-of-match filter (ensures ML only if best in match)
    bom = best_of_match(team_candidates)
    # Select top 3 with constraints
    team_picks = select_top_team(bom)

    # PROPS
    prop_candidates, props_region = build_prop_candidates(injuries)
    for c in prop_candidates:
        attach_minutes_and_injury(c, injuries)
    prop_picks = select_top_props(prop_candidates)

    if not team_picks and not prop_picks:
        msg = format_no_bet(
            title="❌ NO BET (TEAM+PROPS)",
            reason="aucun bet EV>=0 avec min books",
            regions_used=[stats_team.get("region") or "n/a", props_region or ""],
            games_analyzed=stats_team["games_analyzed"],
            markets_tested=stats_team["markets_tested"],
            top_rejects=[],
            near_miss_lines=[],
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", msg)
        return

    # Explain + post TEAM
    if team_picks:
        for i, p in enumerate(team_picks, start=1):
            why, stats = explain_pick(p)
            p["why"] = why
            p["stats_justif"] = stats
            msg = format_team_pick(p, i)
            post_discord(TEAM_WEBHOOK, "🏀 NBA — TOP 3 TEAM", msg)

    # Explain + post PROPS
    if prop_picks:
        for i, p in enumerate(prop_picks, start=1):
            why, stats = explain_pick(p)
            p["why"] = why
            p["stats_justif"] = stats
            msg = format_prop_pick(p, i)
            post_discord(PROPS_WEBHOOK, "🎯 NBA — TOP 3 PROPS", msg)

if __name__ == "__main__":
    main()
