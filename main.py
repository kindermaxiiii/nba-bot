import os
import json
import math
import requests
from datetime import datetime, timezone
from dateutil import parser

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

REGIONS = "fr"
MARKETS = "h2h,spreads,totals"
ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

# --------------------------
# LOAD CONFIG
# --------------------------

with open("config.json", "r") as f:
    CONFIG = json.load(f)

with open("state.json", "r") as f:
    STATE = json.load(f)

BANKROLL = CONFIG["bankroll_eur"]
DAILY_BUDGET = BANKROLL * CONFIG["daily_budget_pct"]
MAX_TEAM = CONFIG["max_team_bets_per_day"]

today = datetime.now(timezone.utc).date().isoformat()

# Reset state if new day
if STATE["date_utc"] != today:
    STATE = {
        "date_utc": today,
        "daily_spent_eur": 0,
        "team_bets_sent": 0,
        "prop_bets_sent": 0
    }

# --------------------------
# UTILITIES
# --------------------------

def save_state():
    with open("state.json", "w") as f:
        json.dump(STATE, f)

def post_discord(webhook, title, description):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    requests.post(webhook, json=data)

def implied_prob(odds):
    return 1 / odds if odds else 0

def median(values):
    values = sorted(values)
    n = len(values)
    if n == 0:
        return None
    if n % 2 == 1:
        return values[n // 2]
    return (values[n//2 - 1] + values[n//2]) / 2

# --------------------------
# FETCH GAMES
# --------------------------

def fetch_games():
    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": DATE_FORMAT,
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()

# --------------------------
# ANALYZE
# --------------------------

def analyze_game(game):
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])

    picks = []

    for market_key in ["h2h"]:
        for book in bookmakers:
            for market in book.get("markets", []):
                if market["key"] != market_key:
                    continue
                for outcome in market["outcomes"]:
                    odds = outcome["price"]
                    if odds is None:
                        continue

                    all_odds = []
                    for b in bookmakers:
                        for m in b.get("markets", []):
                            if m["key"] == market_key:
                                for o in m["outcomes"]:
                                    if o["name"] == outcome["name"]:
                                        all_odds.append(o["price"])

                    if len(all_odds) < 3:
                        continue

                    med = median(all_odds)
                    edge = implied_prob(med) - implied_prob(odds)

                    if edge > 0.03:
                        picks.append({
                            "selection": outcome["name"],
                            "odds": odds,
                            "edge": edge
                        })

    if picks:
        return sorted(picks, key=lambda x: x["edge"], reverse=True)[0]
    return None

# --------------------------
# MAIN
# --------------------------

def main():
    games = fetch_games()
    bets_today = []

    for game in games:
        game_date = parser.isoparse(game["commence_time"]).date().isoformat()
        if game_date != today:
            continue

        if STATE["team_bets_sent"] >= MAX_TEAM:
            break

        pick = analyze_game(game)
        match_name = f"{game['away_team']} @ {game['home_team']}"

        if pick:
            bets_today.append({
                "match": match_name,
                "selection": pick["selection"],
                "odds": pick["odds"],
                "edge": pick["edge"]
            })

    # Limit to 3 best
    bets_today = sorted(bets_today, key=lambda x: x["edge"], reverse=True)[:MAX_TEAM]

    if not bets_today:
        post_discord(LOG_WEBHOOK, "NO BET", "Aucune value détectée aujourd'hui.")
        return

    # Allocation 40 / 35 / 25
    splits = [0.4, 0.35, 0.25]
    if len(bets_today) == 1:
        splits = [1.0]
    elif len(bets_today) == 2:
        splits = [0.6, 0.4]

    remaining_budget = DAILY_BUDGET - STATE["daily_spent_eur"]

    for i, bet in enumerate(bets_today):
        if remaining_budget <= 0:
            break

        stake = round(DAILY_BUDGET * splits[i], 2)

        if stake > remaining_budget:
            stake = round(remaining_budget, 2)

        message = (
            f"Match: {bet['match']}\n"
            f"Selection: {bet['selection']}\n"
            f"Cote: {bet['odds']}\n"
            f"Edge proxy: {round(bet['edge']*100,2)}%\n"
            f"Mise: {stake}€\n"
            f"Budget journalier total: {round(DAILY_BUDGET,2)}€"
        )

        post_discord(TEAM_WEBHOOK, "NBA TEAM BET", message)

        STATE["daily_spent_eur"] += stake
        STATE["team_bets_sent"] += 1
        remaining_budget -= stake

    save_state()

if __name__ == "__main__":
    main()
