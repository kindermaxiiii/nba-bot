import os
import json
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from dateutil import parser

from odds_api import fetch_odds_with_fallback
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

# diversification
MAX_ML_PER_SLATE = int(CONFIG.get("max_ml_per_slate", 2))
ONE_PICK_PER_MATCH = bool(CONFIG.get("one_pick_per_match", True))


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
        })


def is_valid_game_date(commence_time: str) -> bool:
    """
    GitHub Actions tourne en UTC.
    NBA peut démarrer après minuit UTC.
    On autorise aujourd'hui UTC et demain UTC.
    """
    today_utc = datetime.now(timezone.utc).date()
    game_date = parser.isoparse(commence_time).date()
    delta = (game_date - today_utc).days
    return 0 <= delta <= 1


def build_near_miss_line(p: Dict[str, Any]) -> str:
    line_part = f" {p['line']}" if p.get("line") is not None else ""
    return (
        f"- {p['match']} — {p['market']} — **{p['selection']}{line_part}** @ {p['odds']:.2f} ({p['book']}) "
        f"| edge {p.get('edge', 0.0)*100:.2f}% | dev {p.get('dev', 0.0)*100:.2f}%"
    )


def analyze_team_game(g: Dict[str, Any], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Returns candidates across ML / Spread / Total.
    """
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", [])
    if not bookmakers:
        stats["rejects"]["no_bookmakers"] = stats["rejects"].get("no_bookmakers", 0) + 1
        return []

    candidates: List[Dict[str, Any]] = []

    # --- MONEYLINE ---
    h2h = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h)
    if lk and lk in h2h:
        outs = list(h2h[lk].keys())
        if len(outs) >= 2:
            stats["markets_attempted"] += 1
            res = analyze_two_way_market(
                match=match,
                market_label="MONEYLINE",
                line=None,
                outcome_a=outs[0],
                outcome_b=outs[1],
                entries_a=h2h[lk][outs[0]],
                entries_b=h2h[lk][outs[1]],
                edge_threshold=EDGE_THRESHOLD,
                dev_threshold=DEV_THRESHOLD,
                min_books=MIN_BOOKMAKERS,
                prefer_fr=PREFER_FR_BOOKS,
            )
            stats["markets_tested"] += 1
            candidates.extend(res)
            for p in res:
                stats["near_miss"].append(p)

    # --- TOTALS (Over/Under at consensus total) ---
    totals = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals)
    if tlk and tlk in totals:
        sides = totals[tlk]
        if "Over" in sides and "Under" in sides:
            stats["markets_attempted"] += 1
            line_val = float(tlk)
            res = analyze_two_way_market(
                match=match,
                market_label="TOTAL",
                line=line_val,
                outcome_a="Over",
                outcome_b="Under",
                entries_a=sides["Over"],
                entries_b=sides["Under"],
                edge_threshold=EDGE_THRESHOLD,
                dev_threshold=DEV_THRESHOLD,
                min_books=MIN_BOOKMAKERS,
                prefer_fr=PREFER_FR_BOOKS,
            )
            stats["markets_tested"] += 1
            candidates.extend(res)
            for p in res:
                stats["near_miss"].append(p)

    # --- SPREADS (team A / team B at consensus abs spread) ---
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads)
    if slk and slk in spreads:
        teams = spreads[slk]
        if home in teams and away in teams:
            stats["markets_attempted"] += 1
            # keep actual signed line from entries[0]["point"] for each side
            # We'll display the selected side's point as "line" in output.
            res = analyze_two_way_market(
                match=match,
                market_label="SPREAD",
                line=float(slk),  # informational; formatting shows p['line']
                outcome_a=home,
                outcome_b=away,
                entries_a=teams[home],
                entries_b=teams[away],
                edge_threshold=EDGE_THRESHOLD,
                dev_threshold=DEV_THRESHOLD,
                min_books=MIN_BOOKMAKERS,
                prefer_fr=PREFER_FR_BOOKS,
            )
            # For spreads we want to display the signed point of the chosen selection.
            # We'll patch the 'line' later when sending.
            stats["markets_tested"] += 1
            candidates.extend(res)
            for p in res:
                stats["near_miss"].append(p)

    return candidates


def patch_spread_signed_line(pick: Dict[str, Any], game: Dict[str, Any]) -> Dict[str, Any]:
    """
    For SPREAD picks, set p['line'] to the signed point (e.g. -3.5 / +11.5) for the selection.
    We locate the best book entry for that selection at the consensus abs(line).
    """
    if pick.get("market") != "SPREAD":
        return pick

    home = game["home_team"]
    away = game["away_team"]
    selection = pick.get("selection")
    bookmakers = game.get("bookmakers", [])

    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads)
    if not slk or slk not in spreads:
        return pick

    teams = spreads[slk]
    if selection not in teams:
        return pick

    # use the chosen book to get signed point
    chosen_book = pick.get("book")
    for e in teams[selection]:
        if e.get("book") == chosen_book and e.get("point") is not None:
            pick["line"] = float(e["point"])
            return pick

    # fallback: take first entry
    if teams[selection] and teams[selection][0].get("point") is not None:
        pick["line"] = float(teams[selection][0]["point"])

    return pick


def analyze_props(games: List[Dict[str, Any]], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Scan multiple prop markets, return candidate prop picks.
    """
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
        prop_games, prop_meta = fetch_odds_with_fallback(
            markets=market_key,
            regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
        )
        stats["regions_props"] = prop_meta.get("chosen_region") or stats.get("regions_props")

        for g in prop_games:
            if not is_valid_game_date(g["commence_time"]):
                continue

            home = g["home_team"]
            away = g["away_team"]
            match = f"{away} @ {home}"
            bookmakers = g.get("bookmakers", [])
            if not bookmakers:
                continue

            props_struct = collect_player_prop_lines(bookmakers, market_key)["props"]

            # Iterate players
            for player, player_lines in props_struct.items():
                lk = pick_consensus_prop_line(player_lines)
                if not lk or lk not in player_lines:
                    continue

                sides = player_lines[lk]
                if "Over" not in sides or "Under" not in sides:
                    continue

                stats["props_attempted"] += 1

                line_val = float(lk)
                res = analyze_two_way_market(
                    match=match,
                    market_label=label,
                    line=line_val,
                    outcome_a="Over",
                    outcome_b="Under",
                    entries_a=sides["Over"],
                    entries_b=sides["Under"],
                    edge_threshold=EDGE_THRESHOLD,
                    dev_threshold=DEV_THRESHOLD,
                    min_books=MIN_BOOKMAKERS,
                    prefer_fr=PREFER_FR_BOOKS,
                )

                stats["props_tested"] += 1

                # attach player name + selection wording
                for p in res:
                    p["player"] = player
                    # p['selection'] is Over/Under already
                    candidates.append(p)

    return candidates


def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
    props_budget = remaining_budget_total * PROPS_BUDGET_SHARE

    # -------------------------
    # TEAM ODDS
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
    team_games, team_meta = fetch_odds_with_fallback(markets=TEAM_MARKETS, regions_priority=["fr", "eu", "uk", "us", "us2", "au"])
    stats_team["region"] = team_meta.get("chosen_region")
    STATE["last_regions_team"] = stats_team["region"]

    team_candidates: List[Dict[str, Any]] = []

    # Keep a dict by match to patch spread signed lines later
    games_by_match: Dict[str, Dict[str, Any]] = {}

    for g in team_games:
        if not is_valid_game_date(g["commence_time"]):
            continue
        stats_team["games_analyzed"] += 1

        match = f"{g['away_team']} @ {g['home_team']}"
        games_by_match[match] = g

        team_candidates.extend(analyze_team_game(g, stats_team))

    # near miss = keep best 5 by (edge, dev)
    near_sorted = sorted(stats_team["near_miss"], key=lambda x: (x.get("edge", 0), x.get("dev", 0)), reverse=True)
    near_lines = [build_near_miss_line(p) for p in near_sorted[:5]]

    team_picks: List[Dict[str, Any]] = []
    if remaining_team_slots > 0 and team_budget > 0:
        team_picks = diversify_team_picks(
            team_candidates,
            max_picks=min(remaining_team_slots, 3),
            max_ml=MAX_ML_PER_SLATE,
            one_pick_per_match=ONE_PICK_PER_MATCH,
        )

        # patch spread signed line display
        patched = []
        for p in team_picks:
            g = games_by_match.get(p.get("match"))
            if g:
                p = patch_spread_signed_line(p, g)
            patched.append(p)
        team_picks = patched

    # -------------------------
    # PROPS ODDS (only if budget/slots)
    # -------------------------
    prop_picks: List[Dict[str, Any]] = []
    stats_props = {
        "props_attempted": 0,
        "props_tested": 0,
        "region": None,
    }

    if remaining_props_slots > 0 and props_budget > 0:
        prop_candidates = analyze_props(team_games, stats_props)  # games list not used directly inside
        STATE["last_regions_props"] = stats_props.get("regions_props") or stats_props.get("region")

        prop_picks = diversify_prop_picks(
            prop_candidates,
            max_picks=min(remaining_props_slots, 3),
            one_pick_per_match=True,
            one_pick_per_player=True,
        )

    # -------------------------
    # NO BET LOGIC
    # -------------------------
    if not team_picks and not prop_picks:
        desc = format_no_bet(
            title="❌ NO BET (TEAM+PROPS)",
            reason=f"aucune value détectée (edge>={EDGE_THRESHOLD*100:.1f}% & dev>={DEV_THRESHOLD*100:.0f}%)",
            regions_used=[stats_team.get("region") or "n/a"],
            games_analyzed=stats_team["games_analyzed"],
            markets_tested=stats_team["markets_tested"],
            top_rejects=[],
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
    if team_picks:
        stakes_team = allocate_stakes_fixed_splits(team_budget, len(team_picks))
        for pick, stake in zip(team_picks, stakes_team):
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
    # SEND PROPS
    # -------------------------
    if prop_picks:
        stakes_props = allocate_stakes_fixed_splits(props_budget, len(prop_picks))
        for pick, stake in zip(prop_picks, stakes_props):
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
