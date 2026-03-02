import os
import json
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple, Optional
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
from context import fetch_injuries, build_injury_note, search_player_id, fetch_player_season_minutes

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

# Ladder: on veut toujours 3/3 si possible -> on relâche progressivement
TEAM_LADDER = [
    ("STRICT", EDGE_THRESHOLD, DEV_THRESHOLD),
    ("RELAXED-1", 0.010, 0.015),
    ("RELAXED-2", 0.007, 0.010),
    ("RELAXED-3", 0.005, 0.008),
]
PROPS_LADDER = [
    ("STRICT", EDGE_THRESHOLD, DEV_THRESHOLD),
    ("RELAXED-1", 0.010, 0.015),
    ("RELAXED-2", 0.007, 0.010),
    ("RELAXED-3", 0.005, 0.008),
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
            # CLV
            "clv": {
                "picks": {}  # pick_id -> {"sent_ts_utc":..., "snapshots":[...], "meta":{...}}
            }
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


def _pct(x: float) -> str:
    return f"{x*100:.2f}%"


def pick_id_from(p: Dict[str, Any]) -> str:
    base = f"{p.get('match','')}|{p.get('market','')}|{p.get('selection','')}|{p.get('line','')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:14]


def clv_record_snapshot(p: Dict[str, Any], tag: str):
    """
    Stocke des snapshots dans state.json.
    tag: "T0", "T+30", "T+60", etc.
    """
    sid = pick_id_from(p)
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    clv = STATE.setdefault("clv", {}).setdefault("picks", {})
    slot = clv.setdefault(sid, {
        "sent_ts_utc": None,
        "snapshots": [],
        "meta": {}
    })

    slot["snapshots"].append({
        "tag": tag,
        "ts_utc": now_iso,
        "odds": float(p.get("odds", 0.0)) if p.get("odds") else None,
        "book": p.get("book"),
    })

    # keep short history
    slot["snapshots"] = slot["snapshots"][-10:]
    return sid


def clv_attach_snapshots(p: Dict[str, Any]):
    sid = pick_id_from(p)
    clv = STATE.get("clv", {}).get("picks", {})
    if sid in clv:
        p["clv_snapshots"] = clv[sid].get("snapshots", [])


def clv_refresh_open_picks(team_games: List[Dict[str, Any]]):
    """
    À chaque run, on refresh les odds des picks déjà envoyés aujourd’hui.
    On refait une lecture sur les matchs du slate (team_games) et on update si on retrouve le marché.
    """
    clv = STATE.get("clv", {}).get("picks", {})
    if not clv:
        return

    # build quick lookup: match -> game bookmakers
    games_by_match = {}
    for g in team_games or []:
        m = f"{g.get('away_team')} @ {g.get('home_team')}"
        games_by_match[m] = g

    now = datetime.now(timezone.utc)
    for sid, rec in clv.items():
        # skip if no meta
        meta = rec.get("meta") or {}
        match = meta.get("match")
        market = meta.get("market")
        selection = meta.get("selection")
        line = meta.get("line")

        if not match or match not in games_by_match:
            continue

        sent_ts = rec.get("sent_ts_utc")
        if not sent_ts:
            continue

        try:
            sent_dt = parser.isoparse(sent_ts)
        except Exception:
            continue

        mins = int((now - sent_dt).total_seconds() // 60)
        if mins < 25:
            continue
        tag = "T+30" if mins < 55 else "T+60" if mins < 85 else f"T+{(mins//30)*30}"

        # avoid duplicate tag
        if any(s.get("tag") == tag for s in rec.get("snapshots", [])):
            continue

        g = games_by_match[match]
        bookmakers = g.get("bookmakers", []) or []
        if not bookmakers:
            continue

        # Refresh only classic team markets (ML/Spread/Total). For other markets, skip (stable).
        if market == "MONEYLINE":
            lines = collect_market_lines(bookmakers, "h2h")["lines"]
            lk = pick_consensus_line(lines)
            if not lk or lk not in lines or selection not in lines[lk]:
                continue
            best = max(lines[lk][selection], key=lambda x: x.get("price", 0.0))
            rec["snapshots"].append({"tag": tag, "ts_utc": now.replace(microsecond=0).isoformat(), "odds": best["price"], "book": best["book"]})
        elif market == "TOTAL":
            lines = collect_market_lines(bookmakers, "totals")["lines"]
            lk = pick_consensus_line(lines)
            if not lk or lk not in lines or selection not in lines[lk]:
                continue
            # line check (optional)
            best = max(lines[lk][selection], key=lambda x: x.get("price", 0.0))
            rec["snapshots"].append({"tag": tag, "ts_utc": now.replace(microsecond=0).isoformat(), "odds": best["price"], "book": best["book"]})
        elif market == "SPREAD":
            lines = collect_market_lines(bookmakers, "spreads")["lines"]
            lk = pick_consensus_line(lines)
            if not lk or lk not in lines or selection not in lines[lk]:
                continue
            best = max(lines[lk][selection], key=lambda x: x.get("price", 0.0))
            rec["snapshots"].append({"tag": tag, "ts_utc": now.replace(microsecond=0).isoformat(), "odds": best["price"], "book": best["book"]})
        else:
            continue

        rec["snapshots"] = rec["snapshots"][-10:]


def merge_rejects(dst: Dict[str, int], src: Dict[str, int]):
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + int(v)


def build_near_miss_line(p: Dict[str, Any]) -> str:
    line_part = f" {p['line']}" if p.get("line") is not None else ""
    label = p.get("player") or p.get("selection")
    return (
        f"• {p['match']} — {p['market']} — **{label}{line_part}** @ {p['odds']:.2f} ({p['book']}) "
        f"| edge {_pct(p.get('edge', 0.0))} (raw {_pct(p.get('edge_raw', p.get('edge', 0.0)))}) | dev {_pct(p.get('dev', 0.0))}"
    )


def patch_spread_signed_line(pick: Dict[str, Any], game: Dict[str, Any]) -> Dict[str, Any]:
    if pick.get("market") != "SPREAD":
        return pick

    selection = pick.get("selection")
    bookmakers = game.get("bookmakers", []) or []
    spreads_lines = collect_market_lines(bookmakers, "spreads")["lines"]
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


def analyze_team_game(
    g: Dict[str, Any],
    injuries: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Retourne:
      passed_candidates, all_candidates (pour ladder)
    """
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        stats["rejects"]["no_bookmakers"] = stats["rejects"].get("no_bookmakers", 0) + 1
        return [], []

    passed: List[Dict[str, Any]] = []
    allc: List[Dict[str, Any]] = []

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

        items = out.get("all", [])
        if items:
            stats["markets_tested"] += 1
            stats["near_miss"].extend(items)
            for it in items:
                it["injury_note"] = build_injury_note(match, injuries)
        allc.extend(items)

        for p in out.get("passed", []):
            p["injury_note"] = build_injury_note(match, injuries)
        passed.extend(out.get("passed", []))

    # MONEYLINE
    h2h = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h)
    if lk and lk in h2h:
        outs = list(h2h[lk].keys())
        if len(outs) >= 2:
            run_two_way("MONEYLINE", None, outs[0], outs[1], h2h[lk][outs[0]], h2h[lk][outs[1]])

    # TOTAL
    totals = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals)
    if tlk and tlk in totals:
        sides = totals[tlk]
        if "Over" in sides and "Under" in sides:
            run_two_way("TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    # SPREAD
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads)
    if slk and slk in spreads:
        teams = spreads[slk]
        if home in teams and away in teams:
            run_two_way("SPREAD", float(slk), home, away, teams[home], teams[away])

    # TEAM TOTALS (si dispo)
    team_totals = collect_market_lines(bookmakers, "team_totals")["lines"]
    ttk = pick_consensus_line(team_totals)
    if ttk and ttk in team_totals:
        # ttk = "TEAM|POINT"
        try:
            team_name, pt = ttk.split("|", 1)
            ptf = float(pt)
        except Exception:
            team_name, ptf = None, None

        if team_name and ptf is not None:
            sides = team_totals[ttk]
            if "Over" in sides and "Under" in sides:
                # Market label includes team name
                run_two_way(f"TEAM TOTAL ({team_name})", ptf, "Over", "Under", sides["Over"], sides["Under"])

    # 1H markets (si dispo)
    # h2h_h1
    h2h1 = collect_market_lines(bookmakers, "h2h_h1")["lines"]
    lk1 = pick_consensus_line(h2h1)
    if lk1 and lk1 in h2h1:
        outs = list(h2h1[lk1].keys())
        if len(outs) >= 2:
            run_two_way("MONEYLINE 1H", None, outs[0], outs[1], h2h1[lk1][outs[0]], h2h1[lk1][outs[1]])

    totals1 = collect_market_lines(bookmakers, "totals_h1")["lines"]
    tlk1 = pick_consensus_line(totals1)
    if tlk1 and tlk1 in totals1:
        sides = totals1[tlk1]
        if "Over" in sides and "Under" in sides:
            run_two_way("TOTAL 1H", float(tlk1), "Over", "Under", sides["Over"], sides["Under"])

    spreads1 = collect_market_lines(bookmakers, "spreads_h1")["lines"]
    slk1 = pick_consensus_line(spreads1)
    if slk1 and slk1 in spreads1:
        teams = spreads1[slk1]
        if home in teams and away in teams:
            run_two_way("SPREAD 1H", float(slk1), home, away, teams[home], teams[away])

    return passed, allc


def ladder_select_team(
    all_candidates: List[Dict[str, Any]],
    slots: int,
) -> List[Dict[str, Any]]:
    picked: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    for tier, e_th, d_th in TEAM_LADDER:
        if len(picked) >= slots:
            break

        # filter by tier thresholds
        pool = [c for c in all_candidates if c.get("edge", 0.0) >= e_th and c.get("dev", 0.0) >= d_th]
        pool.sort(key=lambda x: (x.get("score", 0), x.get("edge", 0), x.get("dev", 0)), reverse=True)

        for p in pool:
            if len(picked) >= slots:
                break
            m = p.get("match")
            if ONE_PICK_PER_MATCH and m in used_matches:
                continue
            if p.get("market") == "MONEYLINE" and ml_count >= MAX_ML_PER_SLATE:
                continue
            p = dict(p)
            p["tier"] = tier
            picked.append(p)
            used_matches.add(m)
            if p.get("market") == "MONEYLINE":
                ml_count += 1

    return picked[:slots]


def analyze_props(injuries: List[Dict[str, Any]], stats: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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

    passed: List[Dict[str, Any]] = []
    allc: List[Dict[str, Any]] = []

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

                stats["props_attempted"] += 1

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
                stats["props_tested"] += 1
                merge_rejects(stats["rejects"], out.get("rejects", {}))

                items = out.get("all", [])
                for it in items:
                    it["player"] = player
                    it["injury_note"] = build_injury_note(match, injuries)
                allc.extend(items)
                stats["near_miss"].extend(items)

                for p in out.get("passed", []):
                    p["player"] = player
                    p["injury_note"] = build_injury_note(match, injuries)

                    pid = search_player_id(player)
                    mpg = fetch_player_season_minutes(pid) if pid else None
                    if mpg is not None:
                        p["minutes_note"] = f"{mpg:.1f} min (saison)"
                    passed.append(p)

    return passed, allc


def ladder_select_props(all_candidates: List[Dict[str, Any]], slots: int) -> List[Dict[str, Any]]:
    picked: List[Dict[str, Any]] = []
    used_matches = set()
    used_players = set()

    for tier, e_th, d_th in PROPS_LADDER:
        if len(picked) >= slots:
            break

        pool = [c for c in all_candidates if c.get("edge", 0.0) >= e_th and c.get("dev", 0.0) >= d_th]
        pool.sort(key=lambda x: (x.get("score", 0), x.get("edge", 0), x.get("dev", 0)), reverse=True)

        for p in pool:
            if len(picked) >= slots:
                break
            m = p.get("match")
            pl = p.get("player")
            if m in used_matches:
                continue
            if pl and pl in used_players:
                continue
            p = dict(p)
            p["tier"] = tier
            picked.append(p)
            used_matches.add(m)
            if pl:
                used_players.add(pl)

    return picked[:slots]


def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    # Injuries (best-effort)
    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    # -------------------------
    # TEAM ODDS (base markets fetch)
    # -------------------------
    stats_team = {
        "games_analyzed": 0,
        "markets_attempted": 0,
        "markets_tested": 0,
        "rejects": {},
        "near_miss": [],
        "region": None,
    }

    team_games: List[Dict[str, Any]] = []
    try:
        # Base markets only here (stable)
        base_markets = "h2h,spreads,totals"
        team_games, team_meta = fetch_odds_with_fallback(
            markets=base_markets,
            regions_priority=["fr", "eu", "uk", "us", "us2", "au"],
        )
        stats_team["region"] = team_meta.get("chosen_region")
        STATE["last_regions_team"] = stats_team["region"]
    except OddsApiError:
        team_games = []

    # Refresh CLV from previous sent picks (using current team_games)
    clv_refresh_open_picks(team_games)

    games_by_match: Dict[str, Dict[str, Any]] = {}
    team_passed: List[Dict[str, Any]] = []
    team_all: List[Dict[str, Any]] = []

    # analyze base markets
    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        stats_team["games_analyzed"] += 1
        match = f"{g['away_team']} @ {g['home_team']}"
        games_by_match[match] = g

        passed, allc = analyze_team_game(g, injuries, stats_team)
        team_passed.extend(passed)
        team_all.extend(allc)

    # Try to enrich with extra markets (Team Totals + 1H) if plan exposes them:
    # We re-fetch games with extra markets but we don't fail the run if 422.
    extra_markets = ["team_totals", "h2h_h1", "spreads_h1", "totals_h1"]
    for mk in extra_markets:
        try:
            games_extra, _ = fetch_odds_with_fallback(
                markets=mk,
                regions_priority=["fr", "eu", "uk", "us", "us2", "au"],
            )
        except OddsApiError:
            continue

        for g in games_extra:
            if not is_game_soon(g.get("commence_time", "")):
                continue
            match = f"{g['away_team']} @ {g['home_team']}"
            # merge bookmakers into existing if same match
            if match in games_by_match:
                # safest: use extra bookmakers for that match only in a temporary pass
                tmp = dict(g)
                passed, allc = analyze_team_game(tmp, injuries, stats_team)
                team_passed.extend(passed)
                team_all.extend(allc)
            else:
                games_by_match[match] = g
                passed, allc = analyze_team_game(g, injuries, stats_team)
                team_passed.extend(passed)
                team_all.extend(allc)

    # Near miss top5 (edge>0 only)
    near_sorted = [x for x in stats_team["near_miss"] if x.get("edge", 0) > 0]
    near_sorted.sort(key=lambda x: (x.get("edge", 0), x.get("dev", 0), x.get("score", 0)), reverse=True)
    near_lines = [build_near_miss_line(p) for p in near_sorted[:5]]

    rejects_items = sorted(stats_team["rejects"].items(), key=lambda kv: kv[1], reverse=True)[:6]
    top_rejects = [f"{k}: {v}" for k, v in rejects_items]

    # -------------------------
    # PROPS
    # -------------------------
    stats_props = {
        "props_attempted": 0,
        "props_tested": 0,
        "rejects": {},
        "near_miss": [],
        "regions_props": None,
    }

    prop_passed: List[Dict[str, Any]] = []
    prop_all: List[Dict[str, Any]] = []
    if remaining_props_slots > 0 and remaining_budget_total > 0:
        prop_passed, prop_all = analyze_props(injuries, stats_props)
        STATE["last_regions_props"] = stats_props.get("regions_props")

    # -------------------------
    # SELECTION: WANT 3/3 (ladder fill)
    # -------------------------
    team_picks: List[Dict[str, Any]] = []
    prop_picks: List[Dict[str, Any]] = []

    team_target = min(3, remaining_team_slots)
    props_target = min(3, remaining_props_slots)

    if team_target > 0:
        team_picks = ladder_select_team(team_all, team_target)
        # patch signed spread line
        patched = []
        for p in team_picks:
            g = games_by_match.get(p.get("match"))
            if g:
                p = patch_spread_signed_line(p, g)
            patched.append(p)
        team_picks = patched

    if props_target > 0:
        prop_picks = ladder_select_props(prop_all, props_target)

    # -------------------------
    # Budget allocation:
    # If one side has 0 picks, allocate 100% to the other.
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
    # NO BET LOGIC
    # -------------------------
    if not team_picks and not prop_picks:
        desc = format_no_bet(
            title="❌ NO BET (TEAM+PROPS)",
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
    # SEND TEAM (with CLV T0)
    # -------------------------
    if team_picks and team_budget > 0:
        stakes_team = allocate_stakes_fixed_splits(team_budget, len(team_picks))
        for pick, stake in zip(team_picks, stakes_team):
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake

            # CLV: mark meta & T0 snapshot
            sid = pick_id_from(pick)
            clv = STATE.setdefault("clv", {}).setdefault("picks", {})
            clv.setdefault(sid, {"sent_ts_utc": None, "snapshots": [], "meta": {}})
            clv[sid]["meta"] = {
                "match": pick.get("match"),
                "market": pick.get("market"),
                "selection": pick.get("selection"),
                "line": pick.get("line"),
            }
            if not clv[sid].get("sent_ts_utc"):
                clv[sid]["sent_ts_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

            clv_record_snapshot(pick, "T0")
            clv_attach_snapshots(pick)

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
    # SEND PROPS (with CLV T0)
    # -------------------------
    if prop_picks and props_budget > 0:
        stakes_props = allocate_stakes_fixed_splits(props_budget, len(prop_picks))
        for pick, stake in zip(prop_picks, stakes_props):
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake

            sid = pick_id_from(pick)
            clv = STATE.setdefault("clv", {}).setdefault("picks", {})
            clv.setdefault(sid, {"sent_ts_utc": None, "snapshots": [], "meta": {}})
            clv[sid]["meta"] = {
                "match": pick.get("match"),
                "market": pick.get("market"),
                "selection": f"{pick.get('player','')}|{pick.get('selection','')}",
                "line": pick.get("line"),
            }
            if not clv[sid].get("sent_ts_utc"):
                clv[sid]["sent_ts_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

            clv_record_snapshot(pick, "T0")
            clv_attach_snapshots(pick)

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
