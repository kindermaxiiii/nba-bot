import os
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
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

TEAM_FEATURES_PATH = "data/team_features.json"


def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=20)
    r.raise_for_status()


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
        })


def is_game_soon(commence_time: str, horizon_hours: int = 36) -> bool:
    """
    FIX 3:
    Filtre stable UTC : garde les matchs entre now-1h et now+36h.
    """
    try:
        start_dt = parser.isoparse(commence_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1) <= start_dt <= now + timedelta(hours=horizon_hours)
    except Exception:
        return False


def load_team_features() -> Dict[str, Any]:
    try:
        with open(TEAM_FEATURES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _pct(x: float) -> str:
    return f"{x*100:.2f}%"


def build_near_miss_line(p: Dict[str, Any]) -> str:
    line = p.get("line")
    line_part = f" {line}" if line is not None else ""
    # props: player, team: selection
    who = p.get("player") or p.get("selection")
    return (
        f"• {p.get('match','')} — {p.get('market','')} — **{who}{line_part}** @ {float(p.get('odds',0)):.2f} "
        f"({p.get('book','')}) | edge {_pct(float(p.get('edge',0)))} | dev {_pct(float(p.get('dev',0)))}"
    )


def merge_rejects(dst: Dict[str, int], src: Dict[str, int]):
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + int(v)


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


def analyze_team_game(g: Dict[str, Any], injuries: List[Dict[str, Any]], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        stats["rejects"]["no_bookmakers"] = stats["rejects"].get("no_bookmakers", 0) + 1
        return []

    candidates: List[Dict[str, Any]] = []

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

        # FIX 2: near-miss inclut tous les outcomes analysés
        all_items = out.get("all", [])
        if all_items:
            stats["markets_tested"] += 1
            stats["near_miss"].extend(all_items)

        passed = out.get("passed", [])
        for p in passed:
            p["injury_note"] = build_injury_note(match, injuries)
        candidates.extend(passed)

    # MONEYLINE
    h2h_lines = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h_lines)
    if lk and lk in h2h_lines:
        outs = list(h2h_lines[lk].keys())
        if len(outs) >= 2:
            run_two_way("MONEYLINE", None, outs[0], outs[1], h2h_lines[lk][outs[0]], h2h_lines[lk][outs[1]])

    # TOTAL
    totals_lines = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals_lines)
    if tlk and tlk in totals_lines:
        sides = totals_lines[tlk]
        if "Over" in sides and "Under" in sides:
            run_two_way("TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    # SPREAD
    spreads_lines = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads_lines)
    if slk and slk in spreads_lines:
        teams = spreads_lines[slk]
        if home in teams and away in teams:
            run_two_way("SPREAD", float(slk), home, away, teams[home], teams[away])

    return candidates


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
                regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
            )
        except OddsApiError:
            continue

        stats["region"] = stats.get("region") or meta.get("chosen_region")

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
                stats["near_miss"].extend(out.get("all", []))

                for p in out.get("passed", []):
                    p["player"] = player
                    p["injury_note"] = build_injury_note(match, injuries)

                    pid = search_player_id(player)
                    mpg = fetch_player_season_minutes(pid) if pid else None
                    if mpg is not None:
                        p["minutes_note"] = f"{mpg:.1f} min (saison)"

                    candidates.append(p)

    return candidates


def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    # injuries best-effort
    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    # TEAM
    stats_team = {
        "games_analyzed": 0,
        "markets_attempted": 0,
        "markets_tested": 0,
        "rejects": {},
        "near_miss": [],
        "region": None,
    }

    team_candidates: List[Dict[str, Any]] = []
    games_by_match: Dict[str, Dict[str, Any]] = {}

    try:
        team_games, team_meta = fetch_odds_with_fallback(
            markets="h2h,spreads,totals",
            regions_priority=["fr", "eu", "uk", "us", "us2", "au"],
        )
        stats_team["region"] = team_meta.get("chosen_region")
        STATE["last_regions_team"] = stats_team["region"]
    except OddsApiError:
        team_games = []

    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        stats_team["games_analyzed"] += 1
        match = f"{g['away_team']} @ {g['home_team']}"
        games_by_match[match] = g
        team_candidates.extend(analyze_team_game(g, injuries, stats_team))

    # picks team
    team_picks: List[Dict[str, Any]] = []
    if remaining_team_slots > 0 and remaining_budget_total > 0:
        team_picks = diversify_team_picks(
            team_candidates,
            max_picks=min(remaining_team_slots, 3),
            max_ml=MAX_ML_PER_SLATE,
            one_pick_per_match=ONE_PICK_PER_MATCH,
        )
        team_picks = [patch_spread_signed_line(p, games_by_match.get(p.get("match"), {})) for p in team_picks]

    # PROPS
    stats_props = {
        "props_attempted": 0,
        "props_tested": 0,
        "rejects": {},
        "near_miss": [],
        "region": None,
    }

    prop_candidates: List[Dict[str, Any]] = []
    prop_picks: List[Dict[str, Any]] = []
    if remaining_props_slots > 0 and remaining_budget_total > 0:
        prop_candidates = analyze_props(injuries, stats_props)
        STATE["last_regions_props"] = stats_props.get("region")

        prop_picks = diversify_prop_picks(
            prop_candidates,
            max_picks=min(remaining_props_slots, 3),
            one_pick_per_match=True,
            one_pick_per_player=True,
        )

    # Budget allocation: si un seul bucket a des picks -> 100% dessus
    if team_picks and prop_picks:
        team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
        props_budget = remaining_budget_total * PROPS_BUDGET_SHARE
    elif team_picks:
        team_budget = remaining_budget_total
        props_budget = 0.0
    elif prop_picks:
        team_budget = 0.0
        props_budget = remaining_budget_total
    else:
        team_budget = 0.0
        props_budget = 0.0

    # NO BET
    if not team_picks and not prop_picks:
        # near-miss top5 edge>0
        near_sorted = [x for x in stats_team["near_miss"] if float(x.get("edge", 0)) > 0]
        near_sorted.sort(key=lambda x: (float(x.get("edge", 0)), float(x.get("dev", 0)), float(x.get("score", 0))), reverse=True)
        near_lines = [build_near_miss_line(p) for p in near_sorted[:5]]

        rejects_items = sorted(stats_team["rejects"].items(), key=lambda kv: kv[1], reverse=True)[:6]
        top_rejects = [f"{k}: {v}" for k, v in rejects_items]

        desc = format_no_bet(
            title="❌ NO BET (TEAM+PROPS)",
            reason=f"aucune value détectée (edge>={EDGE_THRESHOLD*100:.1f}% & dev>={DEV_THRESHOLD*100:.0f}%)",
            regions_used=[stats_team.get("region") or "n/a", stats_props.get("region") or ""],
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

    # SEND TEAM
    if team_picks and team_budget > 0:
        stakes_team = allocate_stakes_fixed_splits(team_budget, len(team_picks))
        for pick, stake in zip(team_picks, stakes_team):
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake
            msg = format_team_pick(pick, stake, BANKROLL, DAILY_BUDGET, spent_after)
            post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)
            STATE["daily_spent_eur"] = spent_after
            STATE["team_bets_sent"] = int(STATE.get("team_bets_sent", 0)) + 1
            STATE["team_spent_eur"] = float(STATE.get("team_spent_eur", 0.0)) + stake

    # SEND PROPS
    if prop_picks and props_budget > 0:
        stakes_props = allocate_stakes_fixed_splits(props_budget, len(prop_picks))
        for pick, stake in zip(prop_picks, stakes_props):
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake
            msg = format_prop_pick(pick, stake, BANKROLL, DAILY_BUDGET, spent_after)
            post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROP", msg)
            STATE["daily_spent_eur"] = spent_after
            STATE["prop_bets_sent"] = int(STATE.get("prop_bets_sent", 0)) + 1
            STATE["props_spent_eur"] = float(STATE.get("props_spent_eur", 0.0)) + stake

    save_state()


if __name__ == "__main__":
    main()
