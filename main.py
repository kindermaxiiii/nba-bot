# main.py
import os
import json
import time
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from dateutil import parser

from odds_api import fetch_odds_with_fallback, OddsApiError
from engine import (
    collect_market_lines,
    collect_player_prop_lines,
    pick_consensus_line,
    pick_consensus_prop_line,
    analyze_two_way_market,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_fixed_splits,
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

EDGE_THRESHOLD = float(CONFIG.get("edge_threshold", 0.015))
DEV_THRESHOLD = float(CONFIG.get("dev_threshold", 0.02))
MIN_BOOKMAKERS = int(CONFIG.get("min_bookmakers", 2))

TEAM_BUDGET_SHARE = float(CONFIG.get("team_budget_share", 0.60))
PROPS_BUDGET_SHARE = float(CONFIG.get("props_budget_share", 0.40))
PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", True))

MAX_ML_PER_SLATE = int(CONFIG.get("max_ml_per_slate", 2))
ONE_PICK_PER_MATCH = bool(CONFIG.get("one_pick_per_match", True))

# CLV
CLV_REFRESH_MINUTES = [30, 60]  # T+30/T+60
CLV_MAX_TRACKED = 30           # safety cap


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
            "clv": {},  # pick_id -> dict
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


def build_near_miss_line(p: Dict[str, Any]) -> str:
    line_part = f" {p['line']}" if p.get("line") is not None else ""
    who = p.get("player") or p.get("selection")
    return (
        f"• {p['match']} — {p['market']} — **{who}{line_part}** @ {p['odds']:.2f} ({p['book']}) "
        f"| edge {_pct(p.get('edge', 0.0))} (raw {_pct(p.get('edge_raw', p.get('edge',0.0)))}) "
        f"| dev {_pct(p.get('dev', 0.0))}"
    )


def _pct(x: float) -> str:
    try:
        return f"{float(x)*100:.2f}%"
    except Exception:
        return "n/a"


def merge_rejects(dst: Dict[str, int], src: Dict[str, int]):
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + int(v)


def safe_fetch(markets: str, regions_priority: List[str]) -> List[Dict[str, Any]]:
    try:
        games, meta = fetch_odds_with_fallback(markets=markets, regions_priority=regions_priority)
        return games
    except Exception:
        return []


# -------------------------
# LADDER SELECTION (STRICT -> RELAXED)
# -------------------------
def ladder_select(
    pool_all: List[Dict[str, Any]],
    max_picks: int,
    diversify_fn,
    diversify_kwargs: Dict[str, Any],
    base_edge: float,
    base_dev: float,
    base_min_books: int,
) -> List[Dict[str, Any]]:
    """
    pool_all: candidates (even if not passing strict) BUT must include edge/dev/total_books.
    Ladder tiers:
      STRICT: edge>=base_edge & dev>=base_dev & books>=base_min_books
      RELAXED_1: 80%
      RELAXED_2: 60%
      RELAXED_3: edge>=0 & dev>=0 & books>=max(1, base_min_books-1)
    Always tags p["tier"].
    """
    tiers = [
        ("STRICT", base_edge, base_dev, base_min_books),
        ("RELAXED_1", base_edge * 0.8, base_dev * 0.8, base_min_books),
        ("RELAXED_2", base_edge * 0.6, base_dev * 0.6, base_min_books),
        ("RELAXED_3", 0.0, 0.0, max(1, base_min_books - 1)),
    ]

    chosen: List[Dict[str, Any]] = []
    used_ids = set()

    def pick_from(filtered: List[Dict[str, Any]], tier_name: str) -> List[Dict[str, Any]]:
        # tag tier before diversify; keep only not already used
        cand = []
        for p in filtered:
            pid = p.get("_pid")
            if pid and pid in used_ids:
                continue
            q = dict(p)
            q["tier"] = tier_name
            cand.append(q)

        picks = diversify_fn(cand, max_picks=max_picks, **diversify_kwargs)
        return picks

    for tier_name, e_th, d_th, b_th in tiers:
        if len(chosen) >= max_picks:
            break
        filtered = []
        for p in pool_all:
            if float(p.get("total_books", 0)) < b_th:
                continue
            if float(p.get("edge", 0)) < e_th:
                continue
            if float(p.get("dev", 0)) < d_th:
                continue
            filtered.append(p)

        if not filtered:
            continue

        need = max_picks - len(chosen)
        picks = pick_from(filtered, tier_name)
        for pk in picks:
            if len(chosen) >= max_picks:
                break
            pid = pk.get("_pid")
            if pid:
                used_ids.add(pid)
            chosen.append(pk)

    # If still empty, we return empty (true no data).
    return chosen[:max_picks]


def make_pick_id(p: Dict[str, Any]) -> str:
    """
    Stable id across runs for CLV tracking.
    """
    key = f"{p.get('match')}|{p.get('market')}|{p.get('selection')}|{p.get('line')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def attach_pid(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for p in pool:
        q = dict(p)
        q["_pid"] = make_pick_id(q)
        out.append(q)
    return out


# -------------------------
# TEAM ANALYSIS (including optional markets)
# -------------------------
def analyze_team_game(
    g: Dict[str, Any],
    injuries: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> List[Dict[str, Any]]:
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        stats["rejects"]["no_bookmakers"] = stats["rejects"].get("no_bookmakers", 0) + 1
        return []

    pool_all: List[Dict[str, Any]] = []  # IMPORTANT: store "all" outcomes (not just passed)

    def run_two_way(market_label: str, line_val, a_key, b_key, a_entries, b_entries):
        stats["markets_attempted"] += 1
        out = analyze_two_way_market(
            match=match,
            market_label=market_label,
            line=line_val,
            outcome_a=a_key,
            outcome_b=b_key,
            entries_a=a_entries,
            entries_b=b_entries,
            edge_threshold=EDGE_THRESHOLD,
            dev_threshold=DEV_THRESHOLD,
            min_books=MIN_BOOKMAKERS,
            prefer_fr=PREFER_FR_BOOKS,
            return_all=True,
        )
        merge_rejects(stats["rejects"], out.get("rejects", {}))
        all_items = out.get("all", [])
        if all_items:
            stats["markets_tested"] += 1
            for it in all_items:
                it["injury_note"] = build_injury_note(match, injuries)
            pool_all.extend(all_items)

    # Moneyline
    h2h = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h)
    if lk and lk in h2h:
        outs = list(h2h[lk].keys())
        if len(outs) >= 2:
            run_two_way("MONEYLINE", None, outs[0], outs[1], h2h[lk][outs[0]], h2h[lk][outs[1]])

    # Totals
    totals = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals)
    if tlk and tlk in totals:
        sides = totals[tlk]
        if "Over" in sides and "Under" in sides:
            run_two_way("TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    # Spreads
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads)
    if slk and slk in spreads:
        teams = spreads[slk]
        if home in teams and away in teams:
            run_two_way("SPREAD", float(slk), home, away, teams[home], teams[away])

    return pool_all


def analyze_team_totals_and_1h(
    markets_key: str,
    region_priority: List[str],
    injuries: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Optional markets: team_totals / 1H markets.
    If plan doesn't expose them -> odds_api raises, we return [].
    """
    try:
        games, meta = fetch_odds_with_fallback(markets=markets_key, regions_priority=region_priority)
    except Exception:
        return []

    pool_all: List[Dict[str, Any]] = []
    for g in games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        home = g["home_team"]
        away = g["away_team"]
        match = f"{away} @ {home}"
        bookmakers = g.get("bookmakers", []) or []
        if not bookmakers:
            continue

        # TEAM TOTALS
        if "team_totals" in markets_key:
            tt = collect_market_lines(bookmakers, "team_totals")["lines"]
            lk = pick_consensus_line(tt)
            if lk and lk in tt:
                # lk format: "TEAM|POINT"
                # outcomes are "TEAM|Over" / "TEAM|Under"
                outcomes = list(tt[lk].keys())
                # must find Over and Under for the SAME TEAM
                # easiest: parse from key
                try:
                    team, point = lk.split("|", 1)
                    over_k = f"{team}|Over"
                    under_k = f"{team}|Under"
                except Exception:
                    over_k, under_k = None, None

                if over_k in tt[lk] and under_k in tt[lk]:
                    out = analyze_two_way_market(
                        match=match,
                        market_label="TEAM TOTAL",
                        line=float(point) if 'point' in locals() else None,
                        outcome_a=f"{team} Over",
                        outcome_b=f"{team} Under",
                        entries_a=tt[lk][over_k],
                        entries_b=tt[lk][under_k],
                        edge_threshold=EDGE_THRESHOLD,
                        dev_threshold=DEV_THRESHOLD,
                        min_books=MIN_BOOKMAKERS,
                        prefer_fr=PREFER_FR_BOOKS,
                        return_all=True,
                    )
                    merge_rejects(stats["rejects"], out.get("rejects", {}))
                    all_items = out.get("all", [])
                    if all_items:
                        stats["markets_tested"] += 1
                        for it in all_items:
                            it["injury_note"] = build_injury_note(match, injuries)
                        pool_all.extend(all_items)

        # 1H TOTALS/SPREADS (best-effort keys; if OddsAPI doesn't return, nothing happens)
        for mk, label in [("totals_h1", "1H TOTAL"), ("spreads_h1", "1H SPREAD"), ("h2h_h1", "1H ML")]:
            lines = collect_market_lines(bookmakers, mk)["lines"]
            lk = pick_consensus_line(lines)
            if not lk or lk not in lines:
                continue
            if mk == "totals_h1":
                sides = lines[lk]
                if "Over" in sides and "Under" in sides:
                    out = analyze_two_way_market(
                        match=match, market_label=label, line=float(lk),
                        outcome_a="Over", outcome_b="Under",
                        entries_a=sides["Over"], entries_b=sides["Under"],
                        edge_threshold=EDGE_THRESHOLD, dev_threshold=DEV_THRESHOLD,
                        min_books=MIN_BOOKMAKERS, prefer_fr=PREFER_FR_BOOKS, return_all=True
                    )
                    merge_rejects(stats["rejects"], out.get("rejects", {}))
                    all_items = out.get("all", [])
                    if all_items:
                        stats["markets_tested"] += 1
                        for it in all_items:
                            it["injury_note"] = build_injury_note(match, injuries)
                        pool_all.extend(all_items)

            if mk == "spreads_h1":
                teams = lines[lk]
                if home in teams and away in teams:
                    out = analyze_two_way_market(
                        match=match, market_label=label, line=float(lk),
                        outcome_a=home, outcome_b=away,
                        entries_a=teams[home], entries_b=teams[away],
                        edge_threshold=EDGE_THRESHOLD, dev_threshold=DEV_THRESHOLD,
                        min_books=MIN_BOOKMAKERS, prefer_fr=PREFER_FR_BOOKS, return_all=True
                    )
                    merge_rejects(stats["rejects"], out.get("rejects", {}))
                    all_items = out.get("all", [])
                    if all_items:
                        stats["markets_tested"] += 1
                        for it in all_items:
                            it["injury_note"] = build_injury_note(match, injuries)
                        pool_all.extend(all_items)

            if mk == "h2h_h1":
                outs = list(lines[lk].keys())
                if len(outs) >= 2:
                    out = analyze_two_way_market(
                        match=match, market_label=label, line=None,
                        outcome_a=outs[0], outcome_b=outs[1],
                        entries_a=lines[lk][outs[0]], entries_b=lines[lk][outs[1]],
                        edge_threshold=EDGE_THRESHOLD, dev_threshold=DEV_THRESHOLD,
                        min_books=MIN_BOOKMAKERS, prefer_fr=PREFER_FR_BOOKS, return_all=True
                    )
                    merge_rejects(stats["rejects"], out.get("rejects", {}))
                    all_items = out.get("all", [])
                    if all_items:
                        stats["markets_tested"] += 1
                        for it in all_items:
                            it["injury_note"] = build_injury_note(match, injuries)
                        pool_all.extend(all_items)

    return pool_all


def patch_spread_signed_line(pick: Dict[str, Any], game: Dict[str, Any]) -> Dict[str, Any]:
    # Only for full game SPREAD; 1H spreads could also use this but optional
    if pick.get("market") != "SPREAD":
        return pick

    selection = pick.get("selection")
    bookmakers = game.get("bookmakers", []) or []
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
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
# PROPS ANALYSIS (pool_all + ladder)
# -------------------------
def analyze_props_pool_all(stats: Dict[str, Any], injuries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prop_market_map = {
        "PROP PTS": "player_points",
        "PROP REB": "player_rebounds",
        "PROP AST": "player_assists",
        "PROP 3PT": "player_threes",
        "PROP PRA": "player_points_rebounds_assists",
    }

    pool_all: List[Dict[str, Any]] = []
    stats.setdefault("rejects", {})
    stats.setdefault("near_miss", [])

    for label, market_key in prop_market_map.items():
        try:
            games, meta = fetch_odds_with_fallback(
                markets=market_key,
                regions_priority=["fr", "eu", "uk", "us", "us2", "au"],
            )
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

            props_struct = collect_player_prop_lines(bookmakers, market_key)["props"]
            for player, player_lines in props_struct.items():
                lk = pick_consensus_prop_line(player_lines)
                if not lk or lk not in player_lines:
                    continue
                sides = player_lines[lk]
                if "Over" not in sides or "Under" not in sides:
                    continue

                stats["props_attempted"] = stats.get("props_attempted", 0) + 1

                out = analyze_two_way_market(
                    match=match,
                    market_label=label,
                    line=float(lk),
                    outcome_a="Over",
                    outcome_b="Under",
                    entries_a=sides["Over"],
                    entries_b=sides["Under"],
                    edge_threshold=EDGE_THRESHOLD,
                    dev_threshold=DEV_THRESHOLD,
                    min_books=MIN_BOOKMAKERS,
                    prefer_fr=PREFER_FR_BOOKS,
                    return_all=True,
                )
                merge_rejects(stats["rejects"], out.get("rejects", {}))
                all_items = out.get("all", [])
                for it in all_items:
                    it["player"] = player
                    it["injury_note"] = build_injury_note(match, injuries)

                    # minutes projection (best effort)
                    pid = search_player_id(player)
                    mpg = fetch_player_season_minutes(pid) if pid else None
                    if mpg is not None:
                        it["minutes_note"] = f"{mpg:.1f} min (saison)"

                if all_items:
                    stats["props_tested"] = stats.get("props_tested", 0) + 1
                    pool_all.extend(all_items)

    return pool_all


# -------------------------
# CLV TRACKING
# -------------------------
def clv_state() -> Dict[str, Any]:
    STATE.setdefault("clv", {})
    if not isinstance(STATE["clv"], dict):
        STATE["clv"] = {}
    # cap
    if len(STATE["clv"]) > CLV_MAX_TRACKED:
        # remove oldest
        items = list(STATE["clv"].items())
        items.sort(key=lambda kv: (kv[1].get("created_ts", "")))
        for k, _ in items[: max(0, len(items) - CLV_MAX_TRACKED)]:
            STATE["clv"].pop(k, None)
    return STATE["clv"]


def add_clv_snapshot(pick: Dict[str, Any], tag: str):
    clv = clv_state()
    pid = pick.get("_pid") or make_pick_id(pick)
    now = datetime.now(timezone.utc).isoformat()

    rec = clv.get(pid) or {
        "created_ts": now,
        "match": pick.get("match"),
        "market": pick.get("market"),
        "selection": pick.get("selection"),
        "line": pick.get("line"),
        "snapshots": [],
    }
    rec["snapshots"].append({
        "tag": tag,
        "odds": pick.get("odds"),
        "book": pick.get("book"),
        "ts_utc": now,
    })
    # store also on pick for formatting
    pick["clv_snapshots"] = rec["snapshots"]
    clv[pid] = rec


def refresh_clv_snapshots(team_games: List[Dict[str, Any]]):
    """
    Minimal refresh:
    - for each tracked pick, if last snapshot older than 30/60 minutes, try to find current best odds
      from current team_games markets.
    - Only for TEAM markets (ML/Spread/Total). Props refresh is heavier; you can add later.
    """
    clv = clv_state()
    if not clv:
        return

    # build quick lookup from latest team_games
    by_match = {}
    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        m = f"{g['away_team']} @ {g['home_team']}"
        by_match[m] = g

    now_dt = datetime.now(timezone.utc)

    for pid, rec in list(clv.items()):
        snaps = rec.get("snapshots") or []
        if not snaps:
            continue
        last_ts = snaps[-1].get("ts_utc")
        try:
            last_dt = parser.isoparse(last_ts)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        mins = (now_dt - last_dt).total_seconds() / 60.0
        need_tag = None
        if mins >= 60 and all(s.get("tag") != "T+60" for s in snaps):
            need_tag = "T+60"
        elif mins >= 30 and all(s.get("tag") != "T+30" for s in snaps):
            need_tag = "T+30"

        if not need_tag:
            continue

        match = rec.get("match")
        market = rec.get("market")
        selection = rec.get("selection")
        line = rec.get("line")

        g = by_match.get(match)
        if not g:
            continue
        books = g.get("bookmakers", []) or []
        if not books:
            continue

        # find current best odds for the same market/selection/line
        cur_odds = None
        cur_book = None

        if market == "MONEYLINE":
            lines = collect_market_lines(books, "h2h")["lines"]
            lk = "h2h"
            if lk in lines and selection in lines[lk]:
                best = max(lines[lk][selection], key=lambda x: x.get("price", 0.0))
                cur_odds, cur_book = best.get("price"), best.get("book")

        if market == "TOTAL":
            lines = collect_market_lines(books, "totals")["lines"]
            lk = f"{line}" if line is not None else None
            if lk and lk in lines and selection in lines[lk]:
                best = max(lines[lk][selection], key=lambda x: x.get("price", 0.0))
                cur_odds, cur_book = best.get("price"), best.get("book")

        if market == "SPREAD":
            lines = collect_market_lines(books, "spreads")["lines"]
            # in your engine, spreads use abs(line) as key; here line stored may be signed,
            # so search by abs
            if line is not None:
                lk = f"{abs(float(line))}"
                if lk in lines and selection in lines[lk]:
                    best = max(lines[lk][selection], key=lambda x: x.get("price", 0.0))
                    cur_odds, cur_book = best.get("price"), best.get("book")

        if cur_odds is None:
            continue

        rec["snapshots"].append({
            "tag": need_tag,
            "odds": float(cur_odds),
            "book": str(cur_book or ""),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        })
        clv[pid] = rec


# -------------------------
# MAIN
# -------------------------
def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    # injuries (best effort)
    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    # -------------------------
    # TEAM ODDS (core markets)
    # -------------------------
    stats_team = {
        "games_analyzed": 0,
        "markets_attempted": 0,
        "markets_tested": 0,
        "rejects": {},
        "near_miss": [],
        "region": None,
    }

    TEAM_MARKETS = "h2h,spreads,totals"
    try:
        team_games, team_meta = fetch_odds_with_fallback(
            markets=TEAM_MARKETS,
            regions_priority=["fr", "eu", "uk", "us", "us2", "au"],
        )
        stats_team["region"] = team_meta.get("chosen_region")
        STATE["last_regions_team"] = stats_team["region"]
    except OddsApiError:
        team_games = []
        stats_team["region"] = None

    # Refresh CLV (before doing anything else)
    try:
        refresh_clv_snapshots(team_games)
    except Exception:
        pass

    team_pool_all: List[Dict[str, Any]] = []
    games_by_match: Dict[str, Dict[str, Any]] = {}

    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        stats_team["games_analyzed"] += 1
        match = f"{g['away_team']} @ {g['home_team']}"
        games_by_match[match] = g

        pool = analyze_team_game(g, injuries, stats_team)
        team_pool_all.extend(pool)
        stats_team["near_miss"].extend(pool)

    # -------------------------
    # OPTIONAL: Team Totals + 1H markets
    # -------------------------
    # Each is "best effort" and won't break the bot if your plan doesn't expose them.
    extra_pool: List[Dict[str, Any]] = []
    extra_stats = {"rejects": {}, "markets_tested": 0}

    for mk in ["team_totals", "totals_h1,spreads_h1,h2h_h1"]:
        pool = analyze_team_totals_and_1h(
            markets_key=mk,
            region_priority=["fr", "eu", "uk", "us", "us2", "au"],
            injuries=injuries,
            stats=extra_stats,
        )
        extra_pool.extend(pool)

    if extra_pool:
        team_pool_all.extend(extra_pool)
        stats_team["near_miss"].extend(extra_pool)
        merge_rejects(stats_team["rejects"], extra_stats.get("rejects", {}))
        stats_team["markets_tested"] += int(extra_stats.get("markets_tested", 0))

    # attach stable ids for ladder + CLV
    team_pool_all = attach_pid(team_pool_all)

    # Near miss top 5
    near_sorted = [x for x in stats_team["near_miss"] if float(x.get("edge", 0)) > 0]
    near_sorted.sort(key=lambda x: (x.get("edge", 0), x.get("dev", 0), x.get("score", 0)), reverse=True)
    near_lines = [build_near_miss_line(p) for p in near_sorted[:5]]

    rejects_items = sorted(stats_team["rejects"].items(), key=lambda kv: kv[1], reverse=True)[:6]
    top_rejects = [f"{k}: {v}" for k, v in rejects_items]

    # -------------------------
    # PROPS POOL (all) + LADDER
    # -------------------------
    stats_props = {
        "props_attempted": 0,
        "props_tested": 0,
        "rejects": {},
        "near_miss": [],
        "regions_props": None,
    }
    prop_pool_all: List[Dict[str, Any]] = []
    if remaining_props_slots > 0:
        prop_pool_all = analyze_props_pool_all(stats_props, injuries)
        prop_pool_all = attach_pid(prop_pool_all)
        STATE["last_regions_props"] = stats_props.get("regions_props")

    # -------------------------
    # LADDER PICKS (this is what fixes "always 3+3")
    # -------------------------
    team_picks: List[Dict[str, Any]] = []
    prop_picks: List[Dict[str, Any]] = []

    if remaining_team_slots > 0:
        team_picks = ladder_select(
            pool_all=team_pool_all,
            max_picks=min(3, remaining_team_slots),
            diversify_fn=diversify_team_picks,
            diversify_kwargs={"max_ml": MAX_ML_PER_SLATE, "one_pick_per_match": ONE_PICK_PER_MATCH},
            base_edge=EDGE_THRESHOLD,
            base_dev=DEV_THRESHOLD,
            base_min_books=MIN_BOOKMAKERS,
        )

        # patch signed spread for full game spreads
        patched = []
        for p in team_picks:
            g = games_by_match.get(p.get("match"))
            if g:
                p = patch_spread_signed_line(p, g)
            patched.append(p)
        team_picks = patched

    if remaining_props_slots > 0:
        # diversify props requires player field -> already in pool
        prop_picks = ladder_select(
            pool_all=prop_pool_all,
            max_picks=min(3, remaining_props_slots),
            diversify_fn=diversify_prop_picks,
            diversify_kwargs={"one_pick_per_match": True, "one_pick_per_player": True},
            base_edge=EDGE_THRESHOLD,
            base_dev=DEV_THRESHOLD,
            base_min_books=MIN_BOOKMAKERS,
        )

    # -------------------------
    # Budget allocation
    # If only one bucket has picks, allocate 100% to it.
    # -------------------------
    if team_picks and prop_picks:
        team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
        props_budget = remaining_budget_total * PROPS_BUDGET_SHARE
    elif team_picks and not prop_picks:
        team_budget = remaining_budget_total
        props_budget = 0.0
    elif prop_picks and not team_picks:
        team_budget = 0.0
        props_budget = remaining_budget_total
    else:
        team_budget = 0.0
        props_budget = 0.0

    # -------------------------
    # NO BET (true no-data) -> only if literally nothing to select from
    # -------------------------
    if not team_picks and not prop_picks:
        desc = format_no_bet(
            title="NO BET (TEAM+PROPS)",
            reason=f"aucune value détectée (edge>={EDGE_THRESHOLD*100:.1f}% & dev>={DEV_THRESHOLD*100:.0f}%)",
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
    # SEND TEAM (and CLV T0 snapshot)
    # -------------------------
    if team_picks and team_budget > 0:
        stakes_team = allocate_stakes_fixed_splits(team_budget, len(team_picks))
        for pick, stake in zip(team_picks, stakes_team):
            # CLV snapshot at send time
            add_clv_snapshot(pick, tag="T0")

            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake
            msg = format_team_pick(
                p=pick,
                stake=stake,
                bankroll=BANKROLL,
                daily_budget=DAILY_BUDGET,
                spent_after=spent_after,
            )
            post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["team_bets_sent"] = int(STATE.get("team_bets_sent", 0)) + 1
            STATE["team_spent_eur"] = float(STATE.get("team_spent_eur", 0.0)) + stake

    # -------------------------
    # SEND PROPS (and CLV T0 snapshot)
    # -------------------------
    if prop_picks and props_budget > 0:
        stakes_props = allocate_stakes_fixed_splits(props_budget, len(prop_picks))
        for pick, stake in zip(prop_picks, stakes_props):
            add_clv_snapshot(pick, tag="T0")

            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake
            msg = format_prop_pick(
                p=pick,
                stake=stake,
                bankroll=BANKROLL,
                daily_budget=DAILY_BUDGET,
                spent_after=spent_after,
            )
            post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROP", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["prop_bets_sent"] = int(STATE.get("prop_bets_sent", 0)) + 1
            STATE["props_spent_eur"] = float(STATE.get("props_spent_eur", 0.0)) + stake

    save_state()


if __name__ == "__main__":
    main()
