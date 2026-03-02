import os
import json
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List
from dateutil import parser

from odds_api import fetch_odds_with_fallback
from engine import (
    collect_market_lines,
    pick_consensus_line,
    analyze_two_way_market,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_fixed_splits,
    median,
)
from formatting import format_team_pick, format_prop_pick, format_no_bet


TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")


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


# ===============================
# DATE FILTER FIX (IMPORTANT)
# ===============================

def is_valid_game_date(commence_time: str) -> bool:
    """
    GitHub Actions tourne en UTC.
    Les matchs NBA commencent souvent après minuit UTC.
    On autorise:
      - aujourd'hui UTC
      - demain UTC
    """
    today_utc = datetime.now(timezone.utc).date()
    game_date = parser.isoparse(commence_time).date()
    delta = (game_date - today_utc).days

    # autorise today (0) et tomorrow (1)
    return 0 <= delta <= 1


# ===============================
# STATE RESET
# ===============================

def reset_state_if_new_day():
    today_utc = datetime.now(timezone.utc).date().isoformat()
    if STATE.get("date_utc") != today_utc:
        STATE.clear()
        STATE.update(
            {
                "date_utc": today_utc,
                "daily_spent_eur": 0.0,
                "team_bets_sent": 0,
                "prop_bets_sent": 0,
            }
        )


def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)


def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=15)
    r.raise_for_status()


# ===============================
# MAIN
# ===============================

def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE["prop_bets_sent"]))

    team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
    props_budget = remaining_budget_total * PROPS_BUDGET_SHARE

    # ===============================
    # FETCH TEAM ODDS
    # ===============================
    TEAM_MARKETS = "h2h,spreads,totals"
    team_games, team_meta = fetch_odds_with_fallback(markets=TEAM_MARKETS)

    team_candidates: List[Dict[str, Any]] = []
    games_analyzed = 0
    markets_tested = 0

    for g in team_games:
        if not is_valid_game_date(g["commence_time"]):
            continue

        games_analyzed += 1

        home = g["home_team"]
        away = g["away_team"]
        match = f"{away} @ {home}"
        bookmakers = g.get("bookmakers", [])

        if not bookmakers:
            continue

        # MONEYLINE
        h2h = collect_market_lines(bookmakers, "h2h")["lines"]
        lk = pick_consensus_line(h2h)
        if lk and lk in h2h:
            outcomes = list(h2h[lk].keys())
            if len(outcomes) >= 2:
                res = analyze_two_way_market(
                    match=match,
                    market_label="MONEYLINE",
                    line=None,
                    outcome_a=outcomes[0],
                    outcome_b=outcomes[1],
                    entries_a=h2h[lk][outcomes[0]],
                    entries_b=h2h[lk][outcomes[1]],
                    edge_threshold=EDGE_THRESHOLD,
                    dev_threshold=DEV_THRESHOLD,
                    min_books=MIN_BOOKMAKERS,
                    prefer_fr=PREFER_FR_BOOKS,
                )
                if res:
                    markets_tested += 1
                    team_candidates.extend(res)

    team_picks = diversify_team_picks(
        team_candidates,
        max_picks=min(remaining_team_slots, 3),
        max_ml=2,
        one_pick_per_match=True,
    )

    # ===============================
    # NO BET TEAM
    # ===============================
    if not team_picks:
        desc = format_no_bet(
            title="❌ NO BET (TEAM)",
            reason="aucune value détectée",
            regions_used=[team_meta.get("chosen_region", "n/a")],
            games_analyzed=games_analyzed,
            markets_tested=markets_tested,
            top_rejects=[],
            near_miss_lines=[],
            daily_budget=DAILY_BUDGET,
            daily_spent=float(STATE["daily_spent_eur"]),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_state()
        return

    # ===============================
    # SEND TEAM PICKS
    # ===============================
    stakes = allocate_stakes_fixed_splits(team_budget, len(team_picks))

    for pick, stake in zip(team_picks, stakes):
        spent_after = float(STATE["daily_spent_eur"]) + stake
        msg = format_team_pick(
            p=pick,
            stake=stake,
            bankroll=BANKROLL,
            daily_budget=DAILY_BUDGET,
            spent_after=spent_after,
        )
        post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)
        STATE["daily_spent_eur"] = spent_after
        STATE["team_bets_sent"] += 1

    save_state()


if __name__ == "__main__":
    main()
