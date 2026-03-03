import os
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from dateutil import parser

from odds_api import fetch_odds_with_fallback, OddsApiError
from engine import (
    collect_market_lines,
    collect_player_prop_lines,
    collect_team_totals_lines,
    pick_consensus_line,
    pick_consensus_prop_line,
    analyze_two_way_market,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_capped,
)
from formatting import format_team_pick, format_prop_pick, format_no_bet

from context import (
    fetch_injuries,
    build_injury_note,
    search_player_id,
    fetch_player_season_minutes,
)

TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

CLV_REFRESH_MINUTES = 30
CLV_MAX_SNAPSHOTS = 4
CLV_HORIZON_HOURS = 6

# -------------------------
# LOAD CONFIG + STATE
# -------------------------
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("state.json", "r", encoding="utf-8") as f:
    STATE = json.load(f)

BANKROLL = float(CONFIG["bankroll_eur"])
DAILY_BUDGET = BANKROLL * float(CONFIG["daily_budget_pct"])

MAX_TEAM_PER_DAY = int(CONFIG.get("max_team_bets_per_day", 3))
MAX_PROPS_PER_DAY = int(CONFIG.get("max_prop_bets_per_day", 3))

EDGE_THRESHOLD_STRICT = float(CONFIG.get("edge_threshold", 0.015))
DEV_THRESHOLD_STRICT = float(CONFIG.get("dev_threshold", 0.02))
MIN_BOOKMAKERS = int(CONFIG.get("min_bookmakers", 2))

TEAM_BUDGET_SHARE = float(CONFIG.get("team_budget_share", 0.60))
PROPS_BUDGET_SHARE = float(CONFIG.get("props_budget_share", 0.40))

MAX_ML_PER_SLATE = int(CONFIG.get("max_ml_per_slate", 2))
ONE_PICK_PER_MATCH = bool(CONFIG.get("one_pick_per_match", True))

# ---- Confirmed by you ----
EV_MUST_BE_POSITIVE_EVEN_FILL = True   # oui
CAP_PER_PICK_DAY_SHARE = 0.25          # 25%
WATCHLIST_MULT = 0.30                  # 0.30x

TARGET_TEAM = 3
TARGET_PROPS = 3

# Ladder = only for candidate generation (still needs EV>=0)
LADDER = [
    {"tier": "STRICT",  "edge": EDGE_THRESHOLD_STRICT, "dev": DEV_THRESHOLD_STRICT},
    {"tier": "RELAXED", "edge": max(0.010, EDGE_THRESHOLD_STRICT * 0.67), "dev": max(0.015, DEV_THRESHOLD_STRICT * 0.75)},
    {"tier": "RELAXED", "edge": 0.005, "dev": 0.010},
]

TEAM_MARKETS_BASE = "h2h,spreads,totals"
TEAM_MARKETS_OPTIONAL = [
    "team_totals",
    "h2h_h1",
    "spreads_h1",
    "totals_h1",
    "team_totals_h1",
]

# -------------------------
# Discord + state helpers
# -------------------------
def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=20)
    r.raise_for_status()

def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)

def reset_state_if_new_day():
    today_utc = datetime.now(timezone.utc).date().isoformat()
    if STATE.get("date_utc") != today_utc:
        STATE.clear()
        STATE.update({
            "date_utc": today_utc,
            "daily_spent_eur": 0.0,
            "team_bets_sent": 0,
            "prop_bets_sent": 0,
            "team_spent_eur": 0.0,
            "props_spent_eur": 0.0,
            "last_regions_team": None,
            "last_regions_props": None,
            "clv": {},
        })

def is_game_soon(commence_time: str, horizon_hours: int = 36) -> bool:
    try:
        start_dt = parser.isoparse(commence_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1) <= start_dt <= now + timedelta(hours=horizon_hours)
    except Exception:
        return False

# -------------------------
# CLV
# -------------------------
def pick_id(p: Dict[str, Any]) -> str:
    return "|".join([
        p.get("match", ""),
        p.get("market", ""),
        p.get("player", ""),
        p.get("selection", ""),
        str(p.get("line", "")),
    ])

def clv_get_store() -> Dict[str, Any]:
    if "clv" not in STATE or not isinstance(STATE.get("clv"), dict):
        STATE["clv"] = {}
    return STATE["clv"]

def clv_attach(p: Dict[str, Any]) -> Dict[str, Any]:
    store = clv_get_store()
    rec = store.get(pick_id(p)) or {}
    p["clv_snapshots"] = rec.get("snapshots") or []
    return p

def clv_add_snapshot(p: Dict[str, Any], tag: str, odds: float, book: str):
    store = clv_get_store()
    pid = pick_id(p)
    now = datetime.now(timezone.utc).isoformat()
    rec = store.get(pid)
    if not rec:
        rec = {"created_ts": now, "snapshots": []}
        store[pid] = rec
    rec["snapshots"].append({"tag": tag, "odds": float(odds), "book": str(book), "ts_utc": now})
    rec["snapshots"] = rec["snapshots"][-CLV_MAX_SNAPSHOTS:]

def clv_should_refresh(rec: Dict[str, Any]) -> bool:
    snaps = rec.get("snapshots") or []
    if len(snaps) >= CLV_MAX_SNAPSHOTS:
        return False

    created = rec.get("created_ts")
    if created:
        try:
            created_dt = parser.isoparse(created)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created_dt > timedelta(hours=CLV_HORIZON_HOURS):
                return False
        except Exception:
            pass

    if not snaps:
        return False

    last_ts = snaps[-1].get("ts_utc")
    try:
        last_dt = parser.isoparse(last_ts)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_dt >= timedelta(minutes=CLV_REFRESH_MINUTES)
    except Exception:
        return True

def refresh_clv_from_current_games(team_games: List[Dict[str, Any]]):
    store = clv_get_store()
    if not store:
        return

    games_by_match: Dict[str, Dict[str, Any]] = {}
    for g in team_games or []:
        match = f"{g.get('away_team')} @ {g.get('home_team')}"
        games_by_match[match] = g

    now = datetime.now(timezone.utc)
    for pid, rec in list(store.items()):
        if not isinstance(rec, dict) or not clv_should_refresh(rec):
            continue

        try:
            match, market, player, selection, line_str = pid.split("|", 4)
        except Exception:
            continue

        g = games_by_match.get(match)
        if not g:
            continue

        bookmakers = g.get("bookmakers", []) or []
        best = None

        if market == "MONEYLINE":
            h2h = collect_market_lines(bookmakers, "h2h")["lines"]
            lk = pick_consensus_line(h2h)
            if lk and lk in h2h and selection in h2h[lk]:
                best = max(h2h[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)
        elif market == "TOTAL":
            totals = collect_market_lines(bookmakers, "totals")["lines"]
            lk = pick_consensus_line(totals)
            if lk and lk in totals and selection in totals[lk]:
                best = max(totals[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)
        elif "SPREAD" in market:
            spreads = collect_market_lines(bookmakers, "spreads")["lines"]
            lk = pick_consensus_line(spreads)
            if lk and lk in spreads and selection in spreads[lk]:
                best = max(spreads[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)

        snaps = rec.get("snapshots") or []
        tag = "T+30" if len(snaps) <= 1 else ("T+60" if len(snaps) == 2 else "T+90")

        if best and best.get("price"):
            rec["snapshots"].append({"tag": tag, "odds": float(best["price"]), "book": str(best.get("book", "")), "ts_utc": now.isoformat()})
            rec["snapshots"] = rec["snapshots"][-CLV_MAX_SNAPSHOTS:]
            store[pid] = rec

    STATE["clv"] = store

# -------------------------
# Utils
# -------------------------
def merge_rejects(dst: Dict[str, int], src: Dict[str, int]):
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + int(v)

def build_near_miss_line(p: Dict[str, Any]) -> str:
    line_part = f" {p.get('line')}" if p.get("line") is not None else ""
    who = p.get("player") or p.get("selection")
    return (
        f"• {p.get('match')} — {p.get('market')} — **{who}{line_part}** @ {float(p.get('odds',0)):.2f} ({p.get('book')}) | "
        f"edge {float(p.get('edge',0))*100:.2f}% | dev {float(p.get('dev',0))*100:.2f}% | score_adj {float(p.get('score',0)):.0f}"
    )

def send_recap(webhook: str, title: str, picks: List[Dict[str, Any]], target_n: int):
    if not webhook:
        return
    lines = []
    for i in range(1, target_n + 1):
        if i <= len(picks):
            p = picks[i-1]
            who = p.get("player") or p.get("selection")
            line = p.get("line")
            line_part = f" {line}" if line is not None else ""
            lines.append(
                f"- Bet {i}: **{p.get('match')}** | {p.get('market')} | **{who}{line_part}** "
                f"@ {float(p.get('odds',0)):.2f} | score {float(p.get('score',0)):.0f}/100"
            )
        else:
            lines.append(f"- Bet {i}: _Aucun pick disponible (EV>=0 introuvable)._")
    post_discord(webhook, title, "\n".join(lines))

# -------------------------
# ONLY US fetch (base + optional merged)
# -------------------------
def fetch_team_games_all_markets_only_us() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    games, meta = fetch_odds_with_fallback(markets=TEAM_MARKETS_BASE, regions_priority=["us", "us2"])
    games_by_id: Dict[str, Dict[str, Any]] = {g.get("id"): g for g in games if g.get("id")}

    for mk in TEAM_MARKETS_OPTIONAL:
        try:
            g2, _ = fetch_odds_with_fallback(markets=mk, regions_priority=["us", "us2"])
        except OddsApiError:
            continue

        for gg in g2:
            gid = gg.get("id")
            if not gid:
                continue
            if gid not in games_by_id:
                games_by_id[gid] = gg
                continue

            base = games_by_id[gid]
            base_books = base.get("bookmakers", []) or []
            add_books = gg.get("bookmakers", []) or []
            by_title = {b.get("title"): b for b in base_books if b.get("title")}

            for b in add_books:
                t = b.get("title")
                if not t:
                    continue
                if t not in by_title:
                    base_books.append(b)
                    by_title[t] = b
                else:
                    bm = by_title[t].get("markets", []) or []
                    am = b.get("markets", []) or []
                    existing = {m.get("key") for m in bm}
                    for m in am:
                        if m.get("key") not in existing:
                            bm.append(m)
                    by_title[t]["markets"] = bm

            base["bookmakers"] = base_books
            games_by_id[gid] = base

    return list(games_by_id.values()), meta

# -------------------------
# Score adjustment (anti-ML / anti-longshot + prefer spreads/totals/team totals)
# -------------------------
def adjust_score_inplace(p: Dict[str, Any]):
    base = float(p.get("score", 0.0))
    market = str(p.get("market", "")).upper()
    odds = float(p.get("odds", 0.0))

    bonus = 0.0

    # Prefer non-ML markets
    if "TEAM TOTAL" in market:
        bonus += 10.0
    elif "SPREAD" in market:
        bonus += 8.0
    elif "TOTAL" in market:
        bonus += 8.0
    elif "MONEYLINE" in market:
        bonus -= 10.0

    # 1H slight bonus (inefficiencies)
    if "1H" in market:
        bonus += 3.0

    # Kill longshot ML
    if "MONEYLINE" in market and odds >= 3.0:
        # penalty grows fast after 3.0
        bonus -= min(35.0, (odds - 3.0) * 8.0)

    score_adj = max(0.0, min(100.0, base + bonus))
    p["score_base"] = base
    p["score"] = score_adj

def mark_watchlist(p: Dict[str, Any]):
    p["tier"] = "WATCHLIST"
    flags = p.get("flags") or []
    if "WATCHLIST (below SAFE)" not in flags:
        flags.append("WATCHLIST (below SAFE)")
    p["flags"] = flags

# -------------------------
# Team analysis (collect STRICT/RELAXED passed candidates)
# -------------------------
def analyze_team_game(g: Dict[str, Any], injuries: List[Dict[str, Any]], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        stats["rejects"]["no_bookmakers"] = stats["rejects"].get("no_bookmakers", 0) + 1
        return []

    injury_note = build_injury_note(match, injuries)
    candidates: List[Dict[str, Any]] = []

    def run_two_way(market_label: str, line_val: Optional[float], a_key: str, b_key: str, a_entries, b_entries, tier: str, edge_th: float, dev_th: float):
        out = analyze_two_way_market(
            match=match,
            market_label=market_label,
            line=line_val,
            outcome_a=a_key,
            outcome_b=b_key,
            entries_a=a_entries,
            entries_b=b_entries,
            edge_threshold=edge_th,
            dev_threshold=dev_th,
            min_books=MIN_BOOKMAKERS,
            prefer_fr=False,
            tier=tier,
            return_all=True,
        )
        merge_rejects(stats["rejects"], out.get("rejects", {}))
        all_items = out.get("all", [])
        if all_items:
            stats["markets_tested"] += 1
            stats["near_miss"].extend(all_items)

        for p in out.get("passed", []):
            if EV_MUST_BE_POSITIVE_EVEN_FILL and float(p.get("ev", 0.0)) < 0:
                continue
            p["injury_note"] = injury_note
            candidates.append(p)

    for rung in LADDER:
        tier = rung["tier"]
        edge_th = float(rung["edge"])
        dev_th = float(rung["dev"])

        # ML
        h2h = collect_market_lines(bookmakers, "h2h")["lines"]
        lk = pick_consensus_line(h2h)
        if lk and lk in h2h:
            outs = list(h2h[lk].keys())
            if len(outs) >= 2:
                stats["markets_attempted"] += 1
                run_two_way("MONEYLINE", None, outs[0], outs[1], h2h[lk][outs[0]], h2h[lk][outs[1]], tier, edge_th, dev_th)

        # TOTAL
        totals = collect_market_lines(bookmakers, "totals")["lines"]
        tlk = pick_consensus_line(totals)
        if tlk and tlk in totals:
            sides = totals[tlk]
            if "Over" in sides and "Under" in sides:
                stats["markets_attempted"] += 1
                run_two_way("TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"], tier, edge_th, dev_th)

        # SPREAD
        spreads = collect_market_lines(bookmakers, "spreads")["lines"]
        slk = pick_consensus_line(spreads)
        if slk and slk in spreads:
            teams = spreads[slk]
            if home in teams and away in teams:
                stats["markets_attempted"] += 1
                run_two_way("SPREAD", float(slk), home, away, teams[home], teams[away], tier, edge_th, dev_th)

        # TEAM TOTALS
        tt = collect_team_totals_lines(bookmakers, "team_totals").get("teams", {})
        for team, team_lines in (tt or {}).items():
            lk2 = pick_consensus_prop_line(team_lines)
            if not lk2 or lk2 not in team_lines:
                continue
            sides = team_lines[lk2]
            if "Over" in sides and "Under" in sides:
                stats["markets_attempted"] += 1
                run_two_way(f"TEAM TOTAL ({team})", float(lk2), "Over", "Under", sides["Over"], sides["Under"], tier, edge_th, dev_th)

        # 1H
        h1_h2h = collect_market_lines(bookmakers, "h2h_h1")["lines"]
        lk = pick_consensus_line(h1_h2h)
        if lk and lk in h1_h2h:
            outs = list(h1_h2h[lk].keys())
            if len(outs) >= 2:
                stats["markets_attempted"] += 1
                run_two_way("MONEYLINE 1H", None, outs[0], outs[1], h1_h2h[lk][outs[0]], h1_h2h[lk][outs[1]], tier, edge_th, dev_th)

        h1_totals = collect_market_lines(bookmakers, "totals_h1")["lines"]
        tlk = pick_consensus_line(h1_totals)
        if tlk and tlk in h1_totals:
            sides = h1_totals[tlk]
            if "Over" in sides and "Under" in sides:
                stats["markets_attempted"] += 1
                run_two_way("TOTAL 1H", float(tlk), "Over", "Under", sides["Over"], sides["Under"], tier, edge_th, dev_th)

        h1_spreads = collect_market_lines(bookmakers, "spreads_h1")["lines"]
        slk = pick_consensus_line(h1_spreads)
        if slk and slk in h1_spreads:
            teams = h1_spreads[slk]
            if home in teams and away in teams:
                stats["markets_attempted"] += 1
                run_two_way("SPREAD 1H", float(slk), home, away, teams[home], teams[away], tier, edge_th, dev_th)

        tt1 = collect_team_totals_lines(bookmakers, "team_totals_h1").get("teams", {})
        for team, team_lines in (tt1 or {}).items():
            lk2 = pick_consensus_prop_line(team_lines)
            if not lk2 or lk2 not in team_lines:
                continue
            sides = team_lines[lk2]
            if "Over" in sides and "Under" in sides:
                stats["markets_attempted"] += 1
                run_two_way(f"TEAM TOTAL 1H ({team})", float(lk2), "Over", "Under", sides["Over"], sides["Under"], tier, edge_th, dev_th)

    return candidates

def patch_spread_signed_line(pick: Dict[str, Any], game: Dict[str, Any], market_key: str = "spreads") -> Dict[str, Any]:
    if "SPREAD" not in str(pick.get("market", "")):
        return pick
    selection = pick.get("selection")
    bookmakers = game.get("bookmakers", []) or []
    spreads = collect_market_lines(bookmakers, market_key)["lines"]
    slk = pick_consensus_line(spreads)
    if not slk or slk not in spreads:
        return pick
    teams = spreads[slk]
    if selection not in teams:
        return pick
    chosen_book = pick.get("book")
    for e in teams[selection]:
        if e.get("book") == chosen_book and e.get("point") is not None:
            pick["line"] = float(e["point"])
            return pick
    if teams[selection] and teams[selection][0].get("point") is not None:
        pick["line"] = float(teams[selection][0]["point"])
    return pick

# -------------------------
# Props analysis
# -------------------------
def analyze_props(injuries: List[Dict[str, Any]], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
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

    candidates: List[Dict[str, Any]] = []

    for label, market_key in prop_market_map.items():
        try:
            games, meta = fetch_odds_with_fallback(markets=market_key, regions_priority=["us", "us2"])
        except OddsApiError:
            continue
        stats["regions_props"] = meta.get("chosen_region") or stats.get("regions_props")

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

                for rung in LADDER:
                    out = analyze_two_way_market(
                        match=match,
                        market_label=label,
                        line=float(lk),
                        outcome_a="Over",
                        outcome_b="Under",
                        entries_a=sides["Over"],
                        entries_b=sides["Under"],
                        edge_threshold=float(rung["edge"]),
                        dev_threshold=float(rung["dev"]),
                        min_books=MIN_BOOKMAKERS,
                        prefer_fr=False,
                        tier=rung["tier"],
                        return_all=True,
                    )
                    stats["props_tested"] += 1
                    merge_rejects(stats["rejects"], out.get("rejects", {}))
                    stats["near_miss"].extend(out.get("all", []))

                    for p in out.get("passed", []):
                        if EV_MUST_BE_POSITIVE_EVEN_FILL and float(p.get("ev", 0.0)) < 0:
                            continue
                        p["player"] = player
                        p["injury_note"] = injury_note

                        pid = search_player_id(player)
                        mpg = fetch_player_season_minutes(pid) if pid else None
                        if mpg is not None:
                            p["minutes_note"] = f"{mpg:.1f} min (saison)"

                        candidates.append(p)

    return candidates

# -------------------------
# Pick construction: SAFE >=80, else WATCHLIST to reach 3
# -------------------------
def build_3_picks(pool: List[Dict[str, Any]], is_team: bool) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    # adjust scores
    for p in pool:
        adjust_score_inplace(p)

    # SAFE first
    safe = [p for p in pool if float(p.get("score", 0.0)) >= 80.0]
    # sort by score_adj then EV then edge
    safe.sort(key=lambda x: (float(x.get("score",0)), float(x.get("ev",0)), float(x.get("edge",0))), reverse=True)

    # WATCHLIST candidates (score<80 but EV>=0)
    watch = [p for p in pool if float(p.get("score",0)) < 80.0]
    watch.sort(key=lambda x: (float(x.get("score",0)), float(x.get("ev",0)), float(x.get("edge",0))), reverse=True)

    picks: List[Dict[str, Any]] = []
    picks.extend(safe[:3])

    # fill to 3 with watchlist
    for p in watch:
        if len(picks) >= 3:
            break
        mark_watchlist(p)
        picks.append(p)

    # diversification is still needed (avoid 3 same match/player)
    if is_team:
        picks = diversify_team_picks(picks, max_picks=3, max_ml=MAX_ML_PER_SLATE, one_pick_per_match=ONE_PICK_PER_MATCH)
    else:
        picks = diversify_prop_picks(picks, max_picks=3, one_pick_per_match=True, one_pick_per_player=True)

    # If diversification removed some, refill with watchlist until 3
    if len(picks) < 3:
        used = set()
        for p in picks:
            used.add(pick_id(p))
        for p in watch:
            if len(picks) >= 3:
                break
            if pick_id(p) in used:
                continue
            mark_watchlist(p)
            picks.append(p)

    return picks, safe

def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    # -------------------------
    # TEAM (ONLY US)
    # -------------------------
    stats_team = {"games_analyzed": 0, "markets_attempted": 0, "markets_tested": 0, "rejects": {}, "near_miss": [], "region": None}

    try:
        team_games, team_meta = fetch_team_games_all_markets_only_us()
        stats_team["region"] = team_meta.get("chosen_region")
        STATE["last_regions_team"] = stats_team["region"]
    except OddsApiError:
        team_games, team_meta = [], {}
        stats_team["region"] = None

    refresh_clv_from_current_games(team_games)

    team_candidates: List[Dict[str, Any]] = []
    games_by_match: Dict[str, Dict[str, Any]] = {}

    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        stats_team["games_analyzed"] += 1
        match = f"{g['away_team']} @ {g['home_team']}"
        games_by_match[match] = g
        team_candidates.extend(analyze_team_game(g, injuries, stats_team))

    # -------------------------
    # PROPS (ONLY US)
    # -------------------------
    stats_props = {"props_tested": 0, "rejects": {}, "near_miss": [], "regions_props": None}

    prop_candidates: List[Dict[str, Any]] = []
    if remaining_props_slots > 0 and remaining_budget_total > 0:
        prop_candidates = analyze_props(injuries, stats_props)
        STATE["last_regions_props"] = stats_props.get("regions_props")

    # -------------------------
    # Build picks (SAFE>=80 else WATCHLIST)
    # -------------------------
    team_picks, team_safe = build_3_picks(team_candidates, is_team=True)
    prop_picks, prop_safe = build_3_picks(prop_candidates, is_team=False)

    # patch signed spread line for team picks (only full game spreads)
    patched = []
    for p in team_picks:
        g = games_by_match.get(p.get("match"))
        if g:
            p = patch_spread_signed_line(p, g, market_key="spreads")
        patched.append(p)
    team_picks = patched

    # budgets (60/40, no “one side eats 100%”)
    team_budget = remaining_budget_total * TEAM_BUDGET_SHARE if team_picks else 0.0
    props_budget = remaining_budget_total * PROPS_BUDGET_SHARE if prop_picks else 0.0

    # -------------------------
    # If nothing at all (rare): log
    # -------------------------
    if not team_picks and not prop_picks:
        near_sorted = sorted(stats_team["near_miss"], key=lambda x: (float(x.get("edge",0)), float(x.get("dev",0))), reverse=True)[:5]
        near_lines = [build_near_miss_line(p) for p in near_sorted]
        rejects_items = sorted(stats_team["rejects"].items(), key=lambda kv: kv[1], reverse=True)[:6]
        top_rejects = [f"{k}: {v}" for k, v in rejects_items]

        desc = format_no_bet(
            title="NO BET (TEAM+PROPS)",
            reason="aucun candidat EV>=0 exploitable (même RELAXED).",
            regions_used=[stats_team.get("region") or "n/a", stats_props.get("regions_props") or ""],
            games_analyzed=stats_team["games_analyzed"],
            markets_tested=stats_team["markets_tested"],
            top_rejects=top_rejects,
            near_miss_lines=near_lines,
            daily_budget=DAILY_BUDGET,
            daily_spent=float(STATE.get("daily_spent_eur", 0.0)),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_state()
        return

    # -------------------------
    # SEND TEAM
    # -------------------------
    sent_team: List[Dict[str, Any]] = []
    if team_picks and team_budget > 0 and remaining_team_slots > 0:
        stakes_team = allocate_stakes_capped(
            total_budget=team_budget,
            n=min(len(team_picks), remaining_team_slots),
            daily_budget=DAILY_BUDGET,
            cap_day_share=CAP_PER_PICK_DAY_SHARE,
        )

        for pick, stake in zip(team_picks[:remaining_team_slots], stakes_team):
            if stake <= 0:
                continue

            # WATCHLIST sizing (0.30x)
            if str(pick.get("tier", "")) == "WATCHLIST":
                stake = round(float(stake) * WATCHLIST_MULT, 2)

            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + float(stake)
            if spent_after > DAILY_BUDGET + 0.01:
                continue

            clv_add_snapshot(pick, "T0", float(pick["odds"]), str(pick.get("book", "")))
            pick = clv_attach(pick)

            msg = format_team_pick(p=pick, stake=stake, bankroll=BANKROLL, daily_budget=DAILY_BUDGET, spent_after=spent_after)
            post_discord(TEAM_WEBHOOK, "NBA TEAM BET", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["team_bets_sent"] = int(STATE.get("team_bets_sent", 0)) + 1
            STATE["team_spent_eur"] = float(STATE.get("team_spent_eur", 0.0)) + float(stake)
            sent_team.append(pick)

    # -------------------------
    # SEND PROPS
    # -------------------------
    sent_props: List[Dict[str, Any]] = []
    if prop_picks and props_budget > 0 and remaining_props_slots > 0:
        stakes_props = allocate_stakes_capped(
            total_budget=props_budget,
            n=min(len(prop_picks), remaining_props_slots),
            daily_budget=DAILY_BUDGET,
            cap_day_share=CAP_PER_PICK_DAY_SHARE,
        )

        for pick, stake in zip(prop_picks[:remaining_props_slots], stakes_props):
            if stake <= 0:
                continue

            if str(pick.get("tier", "")) == "WATCHLIST":
                stake = round(float(stake) * WATCHLIST_MULT, 2)

            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + float(stake)
            if spent_after > DAILY_BUDGET + 0.01:
                continue

            clv_add_snapshot(pick, "T0", float(pick["odds"]), str(pick.get("book", "")))
            pick = clv_attach(pick)

            msg = format_prop_pick(p=pick, stake=stake, bankroll=BANKROLL, daily_budget=DAILY_BUDGET, spent_after=spent_after)
            post_discord(PROPS_WEBHOOK, "NBA PLAYER PROP", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["prop_bets_sent"] = int(STATE.get("prop_bets_sent", 0)) + 1
            STATE["props_spent_eur"] = float(STATE.get("props_spent_eur", 0.0)) + float(stake)
            sent_props.append(pick)

    # Recaps (always 3 lines)
    send_recap(TEAM_WEBHOOK, "VOICI LES 3 MEILLEURS PICKS EQUIPES DU JOUR !", sent_team, TARGET_TEAM)
    send_recap(PROPS_WEBHOOK, "VOICI LES 3 MEILLEURS PICKS JOUEURS DU JOUR !", sent_props, TARGET_PROPS)

    # If not enough SAFE, log it explicitly
    if LOG_WEBHOOK:
        safe_note = (
            f"SAFE TEAM: {len(team_safe)}/3 | SAFE PROPS: {len(prop_safe)}/3\n"
            f"Règle: score_adj>=80 = SAFE. Le reste = WATCHLIST (stake×{WATCHLIST_MULT})."
        )
        post_discord(LOG_WEBHOOK, "INFO SAFE vs WATCHLIST", safe_note)

    save_state()

if __name__ == "__main__":
    main()
