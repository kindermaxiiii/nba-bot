import json
import os

from odds_api import fetch_odds_with_fallback
from context import post_log, post_team, post_props


def main():

    post_log("NBA BOT started")

    markets = ["h2h", "spreads", "totals"]

    games = fetch_odds_with_fallback(
        markets,
        regions=("us", "eu", "uk", "au"),
        books=["fanduel", "draftkings", "betmgm"],
    )

    if not games:
        post_log("❌ No games received from OddsAPI")
        return

    picks = []

    for g in games:

        home = g["home_team"]
        away = g["away_team"]

        for bm in g.get("bookmakers", []):
            for m in bm.get("markets", []):

                if m["key"] == "h2h":

                    for o in m["outcomes"]:

                        picks.append(
                            {
                                "match": f"{away} @ {home}",
                                "team": o["name"],
                                "odds": o["price"],
                                "book": bm["title"],
                            }
                        )

    if not picks:
        post_log("❌ No picks parsed")
        return

    msg = "NBA GAMES\n\n"

    for p in picks[:5]:
        msg += f"{p['match']} → {p['team']} @ {p['odds']} ({p['book']})\n"

    post_team(msg)

    print(json.dumps(picks[:5], indent=2))


if __name__ == "__main__":
    main()
