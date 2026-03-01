import os
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

EDGE_THRESHOLD = 0.03
DEV_THRESHOLD = 0.025
MIN_BOOKMAKERS = 3
MAX_BETS = 3


def post_discord(webhook, title, description):
    if not webhook:
        return
    data = {
        "embeds": [
            {
                "title": title,
                "description": description
            }
        ]
    }
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


def analyze_game(game):
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])

    picks = []

    for market_key in ["h2h", "totals", "spreads"]:
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

                    if len(all_odds) < MIN_BOOKMAKERS:
                        continue

                    med = median(all_odds)
                    edge = implied_prob(med) - implied_prob(odds)
                    dev = (odds - med) / med

                    if edge > EDGE_THRESHOLD and dev > DEV_THRESHOLD:
                        picks.append({
                            "selection": outcome["name"],
                            "odds": odds,
                            "edge": edge
                        })

    if picks:
        return sorted(picks, key=lambda x: x["edge"], reverse=True)[0]
    return None


def main():
    games = fetch_games()
    today = datetime.now(timezone.utc).date()
    bets_sent = 0

    for game in games:
        game_time = parser.isoparse(game["commence_time"]).date()
        if game_time != today:
            continue

        pick = analyze_game(game)

        match_name = f"{game['away_team']} @ {game['home_team']}"

        if pick and bets_sent < MAX_BETS:
            message = (
                f"Match: {match_name}\n"
                f"Selection: {pick['selection']}\n"
                f"Cote: {pick['odds']}\n"
                f"Edge proxy: {round(pick['edge']*100,2)}%"
            )
            post_discord(TEAM_WEBHOOK, "NBA VALUE BET", message)
            bets_sent += 1
        else:
            post_discord(LOG_WEBHOOK, "NO BET", f"{match_name} - No strong anomaly detected")


if __name__ == "__main__":
    main()
