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
CLV_MAX_SNAPSHOTS = 4  # T0 + 3 refresh
CLV_HORIZON_HOURS = 6  # stop tracking after 6h


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

PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", True))

MAX_ML_PER_SLATE = int(CONFIG.get("max_ml_per_slate", 2))
ONE_PICK_PER_MATCH = bool(CONFIG.get("one_pick_per_match", True))

# cap ABSOLU par bet (en % du budget jour)
MAX_SINGLE_STAKE_SHARE_DAY = float(CONFIG.get("max_single_stake_share_day", 0.20))  # 20% day budget par bet

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


def pick_id(p: Dict[str, Any]) -> str:
    parts = [
        p.get("match", ""),
        p.get("market", ""),
        p.get("player", ""),
        p.get("selection", ""),
        str(p.get("line", "")),
    ]
    return "|".join(parts)


def clv_get_store() -> Dict[str, Any]:
    if "clv" not in STATE or not isinstance(STATE.get("clv"), dict):
        STATE["clv"] = {}
    return STATE["clv"]


def clv_attach(p: Dict[str, Any]) -> Dict[str, Any]:
    store = clv_get_store()
    pid = pick_id(p)
    rec = store.get(pid) or {}
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

    rec["snapshots"].append({
        "tag": tag,
        "odds": float(odds),
        "book": str(book),
        "ts_utc": now,
    })
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


def build_near_miss_line(p: Dict[str, Any]) -> str:
    line_part = f" {p['line']}" if p.get("line") is not None else ""
    who = p.get("player") or p.get("selection")
    return (
        f"• {p['match']} — {p['market']} — **{who}{line_part}** @ {p['odds']:.2f} ({p['book']}) | "
        f"edge {p.get('edge', 0.0)*100:.2f}% (raw {p.get('edge_raw', p.get('edge', 0.0))*100:.2f}%) | "
        f"dev {p.get('dev', 0.0)*100:.2f}%"
    )


def merge_rejects(dst: Dict[str, int], src: Dict[str, int]):
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + int(v)


def _should_force_us(meta: Dict[str, Any]) -> bool:
    if (meta or {}).get("chosen_region") != "fr":
        return False
    unique_books = int(meta.get("unique_books") or 0)
    total_books = int(meta.get("total_books") or 0)
    return (unique_books < 3) or (total_books < 15)


def fetch_team_games_all_markets() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    games, meta = fetch_odds_with_fallback(
        markets=TEAM_MARKETS_BASE,
        regions_priority=["fr", "us", "us2", "uk", "eu", "au"],
    )

    # 🔥 Force US fallback si FR pauvre
    if _should_force_us(meta):
        games, meta = fetch_odds_with_fallback(
            markets=TEAM_MARKETS_BASE,
            regions_priority=["us", "us2", "uk", "eu", "au"],
        )

    games_by_id: Dict[str, Dict[str, Any]] = {g.get("id"): g for g in games if g.get("id")}

    for mk in TEAM_MARKETS_OPTIONAL:
        try:
            g2, meta2 = fetch_odds_with_fallback(
                markets=mk,
                regions_priority=["fr", "us", "us2", "uk", "eu", "au"],
            )
            if _should_force_us(meta2):
                g2, _meta_us = fetch_odds_with_fallback(
                    markets=mk,
                    regions_priority=["us", "us2", "uk", "eu", "au"],
                )
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
                    existing_keys = {m.get("key") for m in bm}
                    for m in am:
                        if m.get("key") not in existing_keys:
                            bm.append(m)
                    by_title[t]["markets"] = bm

            base["bookmakers"] = base_books
            games_by_id[gid] = base

    return list(games_by_id.values()), meta


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

    def run_two_way(market_label: str, line_val: Optional[float], a_key: str, b_key: str, a_entries, b_entries,
                    tier: str, edge_th: float, dev_th: float):
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
            prefer_fr=PREFER_FR_BOOKS,
            tier=tier,
            return_all=True,
        )
        merge_rejects(stats["rejects"], out.get("rejects", {}))
        all_items = out.get("all", [])
        if all_items:
            stats["markets_tested"] += 1
            stats["near_miss"].extend(all_items)

        for p in out.get("passed", []):
            p["injury_note"] = injury_note
            candidates.append(p)

    for rung in LADDER:
        tier = rung["tier"]
        edge_th = float(rung["edge"])
        dev_th = float(rung["dev"])

        # MONEYLINE
        h2h_lines = collect_market_lines(bookmakers, "h2h")["lines"]
        lk = pick_consensus_line(h2h_lines)
        if lk and lk in h2h_lines:
            outs = list(h2h_lines[lk].keys())
            if len(outs) >= 2:
                run_two_way("MONEYLINE", None, outs[0], outs[1],
                            h2h_lines[lk][outs[0]], h2h_lines[lk][outs[1]], tier, edge_th, dev_th)

        # TOTAL
        totals_lines = collect_market_lines(bookmakers, "totals")["lines"]
        tlk = pick_consensus_line(totals_lines)
        if tlk and tlk in totals_lines:
            sides = totals_lines[tlk]
            if "Over" in sides and "Under" in sides:
                run_two_way("TOTAL", float(tlk), "Over", "Under",
                            sides["Over"], sides["Under"], tier, edge_th, dev_th)

        # SPREAD
        spreads_lines = collect_market_lines(bookmakers, "spreads")["lines"]
        slk = pick_consensus_line(spreads_lines)
        if slk and slk in spreads_lines:
            teams = spreads_lines[slk]
            if home in teams and away in teams:
                run_two_way("SPREAD", float(slk), home, away,
                            teams[home], teams[away], tier, edge_th, dev_th)

        # TEAM TOTALS
        tt = collect_team_totals_lines(bookmakers, "team_totals").get("teams", {})
        for team, team_lines in (tt or {}).items():
            lk2 = pick_consensus_prop_line(team_lines)
            if not lk2 or lk2 not in team_lines:
                continue
            sides = team_lines[lk2]
            if "Over" in sides and "Under" in sides:
                run_two_way(f"TEAM TOTAL ({team})", float(lk2), "Over", "Under",
                            sides["Over"], sides["Under"], tier, edge_th, dev_th)

        # 1H markets
        h1_h2h = collect_market_lines(bookmakers, "h2h_h1")["lines"]
        lk = pick_consensus_line(h1_h2h)
        if lk and lk in h1_h2h:
            outs = list(h1_h2h[lk].keys())
            if len(outs) >= 2:
                run_two_way("MONEYLINE 1H", None, outs[0], outs[1],
                            h1_h2h[lk][outs[0]], h1_h2h[lk][outs[1]], tier, edge_th, dev_th)

        h1_totals = collect_market_lines(bookmakers, "totals_h1")["lines"]
        tlk = pick_consensus_line(h1_totals)
        if tlk and tlk in h1_totals:
            sides = h1_totals[tlk]
            if "Over" in sides and "Under" in sides:
                run_two_way("TOTAL 1H", float(tlk), "Over", "Under",
                            sides["Over"], sides["Under"], tier, edge_th, dev_th)

        h1_spreads = collect_market_lines(bookmakers, "spreads_h1")["lines"]
        slk = pick_consensus_line(h1_spreads)
        if slk and slk in h1_spreads:
            teams = h1_spreads[slk]
            if home in teams and away in teams:
                run_two_way("SPREAD 1H", float(slk), home, away,
                            teams[home], teams[away], tier, edge_th, dev_th)

        tt1 = collect_team_totals_lines(bookmakers, "team_totals_h1").get("teams", {})
        for team, team_lines in (tt1 or {}).items():
            lk2 = pick_consensus_prop_line(team_lines)
            if not lk2 or lk2 not in team_lines:
                continue
            sides = team_lines[lk2]
            if "Over" in sides and "Under" in sides:
                run_two_way(f"TEAM TOTAL 1H ({team})", float(lk2), "Over", "Under",
                            sides["Over"], sides["Under"], tier, edge_th, dev_th)

    return candidates


def patch_spread_signed_line(pick: Dict[str, Any], game: Dict[str, Any], market_key: str = "spreads") -> Dict[str, Any]:
    if "SPREAD" not in str(pick.get("market", "")):
        return pick

    selection = pick.get("selection")
    bookmakers = game.get("bookmakers", []) or []
    spreads_lines = collect_market_lines(bookmakers, market_key)["lines"]
    slk = pick_consensus_line(spreads_lines)
    if not slk or slk not in spreads_lines:
        return pick

    teams = spreads_lines[slk]
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
            games, meta = fetch_odds_with_fallback(
                markets=market_key,
                regions_priority=["fr", "us", "us2", "uk", "eu", "au"],
            )
            if _should_force_us(meta):
                games, meta = fetch_odds_with_fallback(
                    markets=market_key,
                    regions_priority=["us", "us2", "uk", "eu", "au"],
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
                        prefer_fr=PREFER_FR_BOOKS,
                        tier=rung["tier"],
                        return_all=True,
                    )

                    merge_rejects(stats["rejects"], out.get("rejects", {}))
                    stats["near_miss"].extend(out.get("all", []))

                    for p in out.get("passed", []):
                        p["player"] = player
                        p["injury_note"] = injury_note

                        pid = search_player_id(player)
                        mpg = fetch_player_season_minutes(pid) if pid else None
                        if mpg is not None:
                            p["minutes_note"] = f"{mpg:.1f} min (saison)"

                        candidates.append(p)

    return candidates


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
            match, market, _player, selection, _line_str = pid.split("|", 4)
        except Exception:
            continue

        g = games_by_match.get(match)
        if not g:
            continue

        bookmakers = g.get("bookmakers", []) or []
        best = None

        if market == "MONEYLINE":
            h2h_lines = collect_market_lines(bookmakers, "h2h")["lines"]
            lk = pick_consensus_line(h2h_lines)
            if lk and lk in h2h_lines and selection in h2h_lines[lk]:
                best = max(h2h_lines[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)

        elif market == "TOTAL":
            totals_lines = collect_market_lines(bookmakers, "totals")["lines"]
            lk = pick_consensus_line(totals_lines)
            if lk and lk in totals_lines and selection in totals_lines[lk]:
                best = max(totals_lines[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)

        elif "SPREAD" in market:
            spreads_lines = collect_market_lines(bookmakers, "spreads")["lines"]
            lk = pick_consensus_line(spreads_lines)
            if lk and lk in spreads_lines and selection in spreads_lines[lk]:
                best = max(spreads_lines[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)

        snaps = rec.get("snapshots") or []
        tag = "T+30"
        if len(snaps) == 2:
            tag = "T+60"
        elif len(snaps) >= 3:
            tag = "T+90"

        if best and best.get("price"):
            rec["snapshots"].append({
                "tag": tag,
                "odds": float(best["price"]),
                "book": str(best.get("book", "")),
                "ts_utc": now.isoformat(),
            })
            rec["snapshots"] = rec["snapshots"][-CLV_MAX_SNAPSHOTS:]
            store[pid] = rec

    STATE["clv"] = store


def _allocate_stakes_safe(total_budget: float, n: int, max_single_abs: float) -> List[float]:
    """
    Distribute total_budget across n picks with a hard absolute cap per pick (max_single_abs).
    """
    if n <= 0 or total_budget <= 0:
        return []
    cap = max(0.0, float(max_single_abs))
    if cap <= 0:
        # no cap => equal split
        x = total_budget / n
        return [x] * n

    # start equal split
    base = total_budget / n
    stakes = [min(base, cap)] * n
    used = sum(stakes)
    leftover = total_budget - used

    # redistribute leftover while respecting cap
    i = 0
    guard = 0
    while leftover > 1e-9 and guard < 10000:
        guard += 1
        if stakes[i] + 1e-9 < cap:
            add = min(cap - stakes[i], leftover)
            stakes[i] += add
            leftover -= add
        i = (i + 1) % n
        # if everyone capped, stop
        if all(s + 1e-9 >= cap for s in stakes):
            break

    # round cents
    stakes = [round(s, 2) for s in stakes]
    # ensure not exceeding budget due to rounding
    diff = round(sum(stakes) - total_budget, 2)
    if diff > 0:
        # subtract diff from last stake(s)
        for j in range(n - 1, -1, -1):
            take = min(diff, stakes[j])
            stakes[j] = round(stakes[j] - take, 2)
            diff = round(diff - take, 2)
            if diff <= 0:
                break
    return stakes


def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    stats_team = {
        "games_analyzed": 0,
        "markets_tested": 0,
        "rejects": {},
        "near_miss": [],
        "region": None,
    }

    try:
        team_games, team_meta = fetch_team_games_all_markets()
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

    near_sorted = sorted(
        stats_team["near_miss"],
        key=lambda x: (x.get("edge", 0.0), x.get("dev", 0.0), x.get("score", 0.0)),
        reverse=True,
    )
    near_lines = [build_near_miss_line(p) for p in near_sorted[:5]]

    rejects_items = sorted(stats_team["rejects"].items(), key=lambda kv: kv[1], reverse=True)[:6]
    top_rejects = [f"{k}: {v}" for k, v in rejects_items]

    stats_props = {
        "rejects": {},
        "near_miss": [],
        "regions_props": None,
    }

    prop_candidates: List[Dict[str, Any]] = []
    if remaining_props_slots > 0 and remaining_budget_total > 0:
        prop_candidates = analyze_props(injuries, stats_props)
        STATE["last_regions_props"] = stats_props.get("regions_props")

    team_picks: List[Dict[str, Any]] = []
    prop_picks: List[Dict[str, Any]] = []

    if remaining_team_slots > 0:
        team_picks = diversify_team_picks(
            team_candidates,
            max_picks=min(remaining_team_slots, 3),
            max_ml=MAX_ML_PER_SLATE,
            one_pick_per_match=ONE_PICK_PER_MATCH,
        )
        patched = []
        for p in team_picks:
            g = games_by_match.get(p.get("match"))
            if g:
                p = patch_spread_signed_line(p, g, market_key="spreads")
            patched.append(p)
        team_picks = patched

    if remaining_props_slots > 0:
        prop_picks = diversify_prop_picks(
            prop_candidates,
            max_picks=min(remaining_props_slots, 3),
            one_pick_per_match=True,
            one_pick_per_player=True,
        )

    # Budgets plafonnés (ne pas donner 100% aux TEAM si props vides)
    team_budget = remaining_budget_total * TEAM_BUDGET_SHARE if team_picks else 0.0
    props_budget = remaining_budget_total * PROPS_BUDGET_SHARE if prop_picks else 0.0

    if not team_picks and not prop_picks:
        desc = format_no_bet(
            title="❌ NO BET (TEAM+PROPS)",
            reason=f"aucune value détectée (edge>={EDGE_THRESHOLD_STRICT*100:.1f}% & dev>={DEV_THRESHOLD_STRICT*100:.0f}%)",
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

    max_single_abs = DAILY_BUDGET * MAX_SINGLE_STAKE_SHARE_DAY

    # SEND TEAM
    if team_picks and team_budget > 0:
        stakes_team = _allocate_stakes_safe(team_budget, len(team_picks), max_single_abs=max_single_abs)
        for pick, stake in zip(team_picks, stakes_team):
            if stake <= 0:
                continue
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake

            clv_add_snapshot(pick, "T0", float(pick["odds"]), str(pick.get("book", "")))
            pick = clv_attach(pick)

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

    # SEND PROPS
    if prop_picks and props_budget > 0:
        stakes_props = _allocate_stakes_safe(props_budget, len(prop_picks), max_single_abs=max_single_abs)
        for pick, stake in zip(prop_picks, stakes_props):
            if stake <= 0:
                continue
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake

            clv_add_snapshot(pick, "T0", float(pick["odds"]), str(pick.get("book", "")))
            pick = clv_attach(pick)

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
