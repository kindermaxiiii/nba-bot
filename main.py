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

# IMPORTANT:
# - "fr" est souvent trop pauvre en spreads/totals (pas assez de books).
# - On élargit à EU pour avoir des lignes + books.
REGIONS = "eu"
MARKETS = "h2h,spreads,totals"
ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

EDGE_THRESHOLD = 0.015  # 1.5%
DEV_THRESHOLD = 0.02    # 2%
MIN_BOOKMAKERS = 2      # >=2 books

MAX_NO_BET_LOGS = 1

# Books FR (approx) : on essaye de prioriser ces books dans le "best price"
FR_BOOK_HINTS = [
    "Betclic", "Parions", "Unibet", "Winamax", "PMU", "ZEbet", "Bwin"
]

def is_fr_book(book_title: str) -> bool:
    t = (book_title or "").lower()
    return any(h.lower() in t for h in FR_BOOK_HINTS)

with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("state.json", "r", encoding="utf-8") as f:
    STATE = json.load(f)

BANKROLL = float(CONFIG["bankroll_eur"])
DAILY_BUDGET = BANKROLL * float(CONFIG["daily_budget_pct"])
MAX_TEAM_PER_DAY = int(CONFIG["max_team_bets_per_day"])

today_utc = datetime.now(timezone.utc).date().isoformat()
if STATE.get("date_utc") != today_utc:
    STATE = {
        "date_utc": today_utc,
        "daily_spent_eur": 0.0,
        "team_bets_sent": 0,
        "prop_bets_sent": 0
    }

def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)

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
    return (values[n // 2 - 1] + values[n // 2]) / 2

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

def add_reject(stats, reason: str):
    stats["reject_reasons"][reason] = stats["reject_reasons"].get(reason, 0) + 1

def add_near_miss(stats, item: dict):
    stats["near_misses"].append(item)

def compute_candidate(entries, market, selection, match):
    # median/edge basée sur TOUS les books dispo (EU)
    odds_list = [x["price"] for x in entries if x.get("price")]
    if len(odds_list) < MIN_BOOKMAKERS:
        return None

    med = median(odds_list)
    if not med:
        return None

    # Best FR si possible, sinon best global
    fr_entries = [e for e in entries if is_fr_book(e.get("book", ""))]
    chosen_pool = fr_entries if fr_entries else entries

    best = max(chosen_pool, key=lambda x: x["price"])
    best_odds = float(best["price"])

    dev = (best_odds - med) / med
    edge = implied_prob(med) - implied_prob(best_odds)

    return {
        "market": market,
        "selection": selection,
        "line": best.get("point"),
        "odds": best_odds,
        "book": best.get("book", "UnknownBook"),
        "edge": edge,
        "dev": dev,
        "match": match,
        "books": len(odds_list),
        "median_odds": med,
        "has_fr": bool(fr_entries),
    }

def best_candidate_across_lines(entries, market, match, make_selection_fn):
    groups = {}
    for e in entries:
        name = e.get("name")
        point = e.get("point")
        if name is None:
            continue
        key = (name, point)
        groups.setdefault(key, []).append(e)

    cands = []
    for (name, point), group in groups.items():
        sel = make_selection_fn(point, name)
        if not sel:
            continue
        cand = compute_candidate(group, market, sel, match)
        if cand:
            cands.append(cand)

    if not cands:
        return None

    cands.sort(key=lambda x: (x["edge"], x["dev"], x["books"]), reverse=True)
    return cands[0]

def pick_best_value_for_game(game, stats):
    home = game["home_team"]
    away = game["away_team"]
    match_str = f"{away} @ {home}"

    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "Aucune cote bookmaker")
        return None

    candidates = []

    # ---- MONEYLINE ----
    h2h_entries = collect_market_entries(bookmakers, "h2h")
    groups = {}
    for e in h2h_entries:
        groups.setdefault(e["name"], []).append(e)

    for outcome, entries in groups.items():
        cand = compute_candidate(entries, "MONEYLINE", outcome, match_str)
        if not cand:
            add_reject(stats, f"MONEYLINE: pas assez de books (>= {MIN_BOOKMAKERS})")
            continue

        stats["markets_tested"] += 1

        add_near_miss(stats, {
            "match": match_str,
            "market": "MONEYLINE",
            "selection": outcome,
            "odds": cand["odds"],
            "book": cand["book"],
            "edge": cand["edge"],
            "dev": cand["dev"]
        })

        if cand["edge"] >= EDGE_THRESHOLD and cand["dev"] >= DEV_THRESHOLD:
            candidates.append(cand)
        else:
            if cand["edge"] < EDGE_THRESHOLD:
                add_reject(stats, f"Edge < {EDGE_THRESHOLD*100:.1f}%")
            if cand["dev"] < DEV_THRESHOLD:
                add_reject(stats, f"Dev vs médiane < {DEV_THRESHOLD*100:.1f}%")

    # ---- TOTALS (all lines) ----
    totals_entries = collect_market_entries(bookmakers, "totals")
    best_total = best_candidate_across_lines(
        totals_entries,
        market="TOTAL",
        match=match_str,
        make_selection_fn=lambda point, name: f"{name} {point}" if name in ("Over", "Under") and point is not None else None
    )

    if best_total:
        stats["markets_tested"] += 1
        add_near_miss(stats, {
            "match": match_str,
            "market": "TOTAL",
            "selection": best_total["selection"],
            "odds": best_total["odds"],
            "book": best_total["book"],
            "edge": best_total["edge"],
            "dev": best_total["dev"]
        })

        if best_total["edge"] >= EDGE_THRESHOLD and best_total["dev"] >= DEV_THRESHOLD:
            candidates.append(best_total)
        else:
            if best_total["edge"] < EDGE_THRESHOLD:
                add_reject(stats, f"Edge < {EDGE_THRESHOLD*100:.1f}%")
            if best_total["dev"] < DEV_THRESHOLD:
                add_reject(stats, f"Dev vs médiane < {DEV_THRESHOLD*100:.1f}%")
    else:
        add_reject(stats, f"TOTAL: pas assez de books (>= {MIN_BOOKMAKERS})")

    # ---- SPREADS (all lines) ----
    spreads_entries = collect_market_entries(bookmakers, "spreads")
    best_spread = best_candidate_across_lines(
        spreads_entries,
        market="SPREAD",
        match=match_str,
        make_selection_fn=lambda point, name: f"{name} {point:+}" if point is not None and name in (home, away) else None
    )

    if best_spread:
        stats["markets_tested"] += 1
        add_near_miss(stats, {
            "match": match_str,
            "market": "SPREAD",
            "selection": best_spread["selection"],
            "odds": best_spread["odds"],
            "book": best_spread["book"],
            "edge": best_spread["edge"],
            "dev": best_spread["dev"]
        })

        if best_spread["edge"] >= EDGE_THRESHOLD and best_spread["dev"] >= DEV_THRESHOLD:
            candidates.append(best_spread)
        else:
            if best_spread["edge"] < EDGE_THRESHOLD:
                add_reject(stats, f"Edge < {EDGE_THRESHOLD*100:.1f}%")
            if best_spread["dev"] < DEV_THRESHOLD:
                add_reject(stats, f"Dev vs médiane < {DEV_THRESHOLD*100:.1f}%")
    else:
        add_reject(stats, f"SPREAD: pas assez de books (>= {MIN_BOOKMAKERS})")

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x["edge"], x["dev"], x["books"]), reverse=True)
    return candidates[0]

def allocate_stakes(num_bets, remaining_budget):
    if num_bets <= 0:
        return []
    if num_bets == 1:
        splits = [1.0]
    elif num_bets == 2:
        splits = [0.6, 0.4]
    else:
        splits = [0.4, 0.35, 0.25]

    planned = [DAILY_BUDGET * s for s in splits[:num_bets]]
    total_planned = sum(planned)
    if total_planned <= 0:
        return [0.0] * num_bets

    scale = min(1.0, remaining_budget / total_planned)
    stakes = [round(x * scale, 2) for x in planned]

    while sum(stakes) - remaining_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes

def format_rejects(reject_reasons: dict, top_n: int = 4) -> str:
    if not reject_reasons:
        return "- (aucune donnée)"
    items = sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return "\n".join([f"- {k}: {v}" for k, v in items])

def format_near_misses(near_misses: list, top_n: int = 3) -> str:
    if not near_misses:
        return "_Aucun near-miss._"
    near = [x for x in near_misses if x.get("edge") is not None and x["edge"] > 0]
    near.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)
    near = near[:top_n]
    if not near:
        return "_Aucun near-miss._"

    lines = []
    for i, x in enumerate(near, start=1):
        lines.append(
            f"{i}) {x['match']} — {x['market']} — **{x['selection']}** @ {x['odds']:.2f} ({x['book']})"
            f"\n   Edge: **{x['edge']*100:.2f}%** | Dev: {x['dev']*100:.2f}%"
        )
    return "\n".join(lines)

def main():
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    games = fetch_games()
    today_date = datetime.now(timezone.utc).date()

    remaining_budget = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))

    stats = {
        "games_today": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": []
    }

    picks = []
    for g in games:
        g_date = parser.isoparse(g["commence_time"]).date()
        if g_date != today_date:
            continue
        stats["games_today"] += 1

        pick = pick_best_value_for_game(g, stats)
        if pick:
            picks.append(pick)

    if not picks or remaining_team_slots == 0 or remaining_budget <= 0:
        if MAX_NO_BET_LOGS > 0:
            reason = []
            if remaining_team_slots == 0:
                reason.append("limite 3 TEAM bets/jour atteinte")
            if remaining_budget <= 0:
                reason.append("budget journalier 10% déjà utilisé")
            if not picks:
                reason.append(f"aucune value détectée (seuil {EDGE_THRESHOLD*100:.1f}%)")

            desc = (
                f"**Aucun bet team aujourd'hui.**\n"
                f"Raison: {', '.join(reason)}\n\n"
                f"**Résumé analyse**\n"
                f"- Matchs analysés: **{stats['games_today']}**\n"
                f"- Marchés testés (>= {MIN_BOOKMAKERS} books): **{stats['markets_tested']}**\n\n"
                f"**Refus principaux**\n{format_rejects(stats['reject_reasons'])}\n\n"
                f"**Near miss (Top 3)**\n{format_near_misses(stats['near_misses'])}\n\n"
                f"Budget jour: **{DAILY_BUDGET:.2f}€** | Déjà utilisé: **{STATE['daily_spent_eur']:.2f}€**"
            )
            post_discord(LOG_WEBHOOK, "❌ NO BET", desc)

        if PROPS_WEBHOOK:
            post_discord(
                PROPS_WEBHOOK,
                "ℹ️ Player Props",
                "Mode 100% gratuit: props automatiques désactivés si les cotes props ne sont pas disponibles via l'API."
            )
        save_state()
        return

    picks.sort(key=lambda x: (x["edge"], x["dev"], x["books"]), reverse=True)
    picks = picks[:remaining_team_slots]

    stakes = allocate_stakes(len(picks), remaining_budget)

    for pick, stake in zip(picks, stakes):
        if stake <= 0:
            continue

        pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0
        line_txt = f"\n**Line:** {pick['line']}" if pick.get("line") is not None else ""
        fr_tag = "✅ FR book" if pick.get("has_fr") else "⚠️ best non-FR"

        msg = (
            f"**Match:** {pick['match']}\n"
            f"**Marché:** {pick['market']}{line_txt}\n"
            f"**Sélection:** {pick['selection']}\n"
            f"**Meilleure cote (préférence FR):** {pick['odds']:.2f} (**{pick['book']}**) — {fr_tag}\n"
            f"**Books utilisés (médiane):** {pick['books']} | **Cote médiane:** {pick['median_odds']:.2f}\n"
            f"**Mise (budget jour 10% BK):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
            f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
            f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
            f"_Max 3 TEAM bets/jour. Si la cote bouge fortement avant ton clic, ne force pas._"
        )
        post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

        STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
        STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    if PROPS_WEBHOOK:
        post_discord(
            PROPS_WEBHOOK,
            "ℹ️ Player Props",
            "Mode 100% gratuit: props automatiques désactivés si les cotes props ne sont pas disponibles via l'API."
        )

    save_state()

if __name__ == "__main__":
    main()
