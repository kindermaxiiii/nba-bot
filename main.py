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

# Thresholds (validé par toi)
EDGE_THRESHOLD = 0.02  # 2%
DEV_THRESHOLD = 0.02   # 2%
MIN_BOOKMAKERS = 3

# 1 seul NO_BET si aucun bet
MAX_NO_BET_LOGS = 1

# --------------------------
# LOAD CONFIG + STATE
# --------------------------
with open("config.json", "r") as f:
    CONFIG = json.load(f)

with open("state.json", "r") as f:
    STATE = json.load(f)

BANKROLL = float(CONFIG["bankroll_eur"])
DAILY_BUDGET = BANKROLL * float(CONFIG["daily_budget_pct"])
MAX_TEAM_PER_DAY = int(CONFIG["max_team_bets_per_day"])

today_utc = datetime.now(timezone.utc).date().isoformat()

# reset state each new UTC day
if STATE.get("date_utc") != today_utc:
    STATE = {
        "date_utc": today_utc,
        "daily_spent_eur": 0.0,
        "team_bets_sent": 0,
        "prop_bets_sent": 0
    }

def save_state():
    with open("state.json", "w") as f:
        json.dump(STATE, f, indent=2)

def post_discord(webhook, title, description):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=15)
    r.raise_for_status()

def implied_prob(odds: float) -> float:
    return 1.0 / odds if odds and odds > 0 else 0.0

def median(values):
    values = sorted(values)
    n = len(values)
    if n == 0:
        return None
    if n % 2 == 1:
        return values[n // 2]
    return (values[n//2 - 1] + values[n//2]) / 2

def mean(values):
    return sum(values) / len(values) if values else None

def stdev(values):
    if not values or len(values) < 2:
        return 0.0
    m = mean(values)
    var = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(var)

def fetch_games():
    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": DATE_FORMAT,
    }
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def collect_market_entries(bookmakers, market_key):
    """
    Returns list of dict entries:
      {name, price, point, book}
    """
    out = []
    for b in bookmakers:
        book = b.get("title", "UnknownBook")
        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                if o.get("name") is None or o.get("price") is None:
                    continue
                out.append({
                    "name": o["name"],
                    "price": float(o["price"]),
                    "point": o.get("point"),
                    "book": book
                })
    return out

def pick_best_value_for_game(game):
    """
    Analyse ML + spreads + totals via 'best vs median' proxy.
    Return best pick dict or None.
    """
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        return None

    candidates = []

    # -------- Moneyline (h2h) --------
    h2h_entries = collect_market_entries(bookmakers, "h2h")
    # group by outcome name (team)
    groups = {}
    for e in h2h_entries:
        groups.setdefault(e["name"], []).append(e)

    for outcome, entries in groups.items():
        odds_list = [x["price"] for x in entries]
        if len(odds_list) < MIN_BOOKMAKERS:
            continue
        med = median(odds_list)
        best = max(entries, key=lambda x: x["price"])
        best_odds = best["price"]
        dev = (best_odds - med) / med
        edge = implied_prob(med) - implied_prob(best_odds)

        if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
            candidates.append({
                "market": "MONEYLINE",
                "selection": outcome,
                "line": None,
                "odds": best_odds,
                "book": best["book"],
                "edge": edge,
                "dev": dev,
                "match": f"{away} @ {home}"
            })

    # -------- Totals --------
    totals_entries = collect_market_entries(bookmakers, "totals")
    # Determine a "main line" by median of points available
    total_points = [e["point"] for e in totals_entries if e["point"] is not None]
    if total_points:
        main_total = median(sorted(total_points))
        for side in ["Over", "Under"]:
            entries = [e for e in totals_entries if e["name"] == side and e["point"] == main_total]
            odds_list = [x["price"] for x in entries]
            if len(odds_list) < MIN_BOOKMAKERS:
                continue
            med = median(odds_list)
            best = max(entries, key=lambda x: x["price"])
            best_odds = best["price"]
            dev = (best_odds - med) / med
            edge = implied_prob(med) - implied_prob(best_odds)

            if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
                candidates.append({
                    "market": "TOTAL",
                    "selection": f"{side} {main_total}",
                    "line": main_total,
                    "odds": best_odds,
                    "book": best["book"],
                    "edge": edge,
                    "dev": dev,
                    "match": f"{away} @ {home}"
                })

    # -------- Spreads --------
    spreads_entries = collect_market_entries(bookmakers, "spreads")
    spread_points = [e["point"] for e in spreads_entries if e["point"] is not None]
    if spread_points:
        main_spread = median(sorted(spread_points))
        # outcomes named by team
        for team in [home, away]:
            entries = [e for e in spreads_entries if e["name"] == team and e["point"] == main_spread]
            odds_list = [x["price"] for x in entries]
            if len(odds_list) < MIN_BOOKMAKERS:
                continue
            med = median(odds_list)
            best = max(entries, key=lambda x: x["price"])
            best_odds = best["price"]
            dev = (best_odds - med) / med
            edge = implied_prob(med) - implied_prob(best_odds)

            if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
                candidates.append({
                    "market": "SPREAD",
                    "selection": f"{team} {main_spread:+}",
                    "line": main_spread,
                    "odds": best_odds,
                    "book": best["book"],
                    "edge": edge,
                    "dev": dev,
                    "match": f"{away} @ {home}"
                })

    if not candidates:
        return None

    # Rank by edge then dev (simple & lisible)
    candidates.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)
    return candidates[0]

def allocate_stakes(num_bets, remaining_budget):
    """
    Split remaining daily budget into 40/35/25, or 60/40, or 100.
    Returns list of stakes length num_bets, rounded to 2 decimals,
    and never exceeding remaining_budget total.
    """
    if num_bets <= 0:
        return []
    if num_bets == 1:
        splits = [1.0]
    elif num_bets == 2:
        splits = [0.6, 0.4]
    else:
        splits = [0.4, 0.35, 0.25]

    stakes = []
    # base on DAILY_BUDGET (not remaining) but cap to remaining
    planned = [DAILY_BUDGET * s for s in splits[:num_bets]]
    total_planned = sum(planned)
    if total_planned <= 0:
        return [0.0] * num_bets

    # scale down if remaining_budget is smaller than planned sum
    scale = min(1.0, remaining_budget / total_planned)

    for x in planned:
        stakes.append(round(x * scale, 2))

    # fix rounding drift (ensure sum <= remaining_budget)
    while sum(stakes) - remaining_budget > 0.001:
        # subtract 0.01 from the largest stake until ok
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes

def main():
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    games = fetch_games()
    today_date = datetime.now(timezone.utc).date()

    remaining_budget = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))

    # Collect best pick per game
    picks = []
    for g in games:
        g_date = parser.isoparse(g["commence_time"]).date()
        if g_date != today_date:
            continue
        pick = pick_best_value_for_game(g)
        if pick:
            picks.append(pick)

    # If no picks OR no slots OR no budget => single NO_BET
    if not picks or remaining_team_slots == 0 or remaining_budget <= 0:
        if MAX_NO_BET_LOGS > 0:
            reason = []
            if remaining_team_slots == 0:
                reason.append("limite 3 TEAM bets/jour atteinte")
            if remaining_budget <= 0:
                reason.append("budget journalier 10% déjà utilisé")
            if not picks:
                reason.append("aucune value détectée (seuil 2%)")

            post_discord(
                LOG_WEBHOOK,
                "❌ NO BET",
                "Aucun bet team.\nRaison: " + ", ".join(reason) + f"\nBudget jour: {DAILY_BUDGET:.2f}€ | Déjà utilisé: {STATE['daily_spent_eur']:.2f}€"
            )
        # Props channel message (clean)
        if PROPS_WEBHOOK:
            post_discord(
                PROPS_WEBHOOK,
                "ℹ️ Player Props",
                "Mode 100% gratuit: props automatiques désactivés si les cotes props ne sont pas disponibles via l'API."
            )
        save_state()
        return

    # Take top picks overall, max 3/day remaining
    picks.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)
    picks = picks[:remaining_team_slots]

    stakes = allocate_stakes(len(picks), remaining_budget)

    for pick, stake in zip(picks, stakes):
        if stake <= 0:
            continue

        msg = (
            f"**Match:** {pick['match']}\n"
            f"**Marché:** {pick['market']}\n"
            f"**Sélection:** {pick['selection']}\n"
            f"**Meilleure cote FR:** {pick['odds']:.2f} (**{pick['book']}**)\n"
            f"**Mise (budget jour 10% BK):** {stake:.2f}€\n"
            f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
            f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
            f"_Max 3 TEAM bets/jour. Si la cote bouge fortement avant ton clic, ne force pas._"
        )
        post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

        STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + stake
        STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    # Clean props channel message (free mode)
    if PROPS_WEBHOOK:
        post_discord(
            PROPS_WEBHOOK,
            "ℹ️ Player Props",
            "Mode 100% gratuit: props automatiques désactivés si les cotes props ne sont pas disponibles via l'API."
        )

    save_state()

if __name__ == "__main__":
    main()
