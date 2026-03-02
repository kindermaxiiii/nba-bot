import os
import json
import math
import requests
from collections import Counter, defaultdict
from datetime import datetime, timezone
from dateutil import parser

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

REGIONS = "eu"
MARKETS = "h2h,spreads,totals,Q1"
ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

# Thresholds
EDGE_THRESHOLD = 0.015  # 1.5%
DEV_THRESHOLD = 0.02    # 2%
MIN_BOOKMAKERS = 2      # >=2 books

# Team bet limits
MAX_NO_BET_LOGS = 1

# Props (Phase 2 scaffold)
ENABLE_PROPS = os.environ.get("ENABLE_PROPS", "0") == "1"
MAX_PROPS_PER_DAY = int(os.environ.get("MAX_PROPS_PER_DAY", "3"))
PROPS_MARKETS = os.environ.get("PROPS_MARKETS", "player_points,player_rebounds,player_assists")

# --------------------------
# LOAD CONFIG + STATE
# --------------------------
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("state.json", "r", encoding="utf-8") as f:
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

    # On essaie plusieurs régions, dans un ordre "utile" pour toi.
    # Tu peux changer l'ordre ensuite, mais là on veut juste que ça RUN.
    region_candidates = [
        "fr",      # si ça marche chez toi (tu l'avais avant)
        "eu",
        "uk",
        "us",
        "us2",
        "au",
    ]

    last_err = None

    for reg in region_candidates:
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": reg,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }

        try:
            r = requests.get(url, params=params, timeout=25)

            # Si l'API renvoie 422 sur une region: on tente la suivante
            if r.status_code == 422:
                print(f"[odds-api] region={reg} -> 422 (not allowed / invalid). Trying next...")
                last_err = RuntimeError(f"422 for regions={reg}: {r.text[:300]}")
                continue

            r.raise_for_status()

            data = r.json()
            print(f"[odds-api] SUCCESS with regions={reg} | games={len(data)}")
            return data

        except Exception as e:
            print(f"[odds-api] region={reg} failed: {e}")
            last_err = e
            continue

    raise RuntimeError(f"All regions failed. Last error: {last_err}")


def fetch_events_today():
    """Needed for props (Phase 2)."""
    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
    params = {"apiKey": ODDS_API_KEY, "dateFormat": DATE_FORMAT}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_event_props(event_id: str):
    """
    Props are generally accessed via /events/{eventId}/odds (Phase 2).
    We'll keep this optional to avoid burning credits / failing when coverage is missing.
    """
    url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": PROPS_MARKETS,
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


def pick_main_line(entries):
    """
    Robust main line selection for spreads/totals:
    choose the most frequent 'point' (mode). Fallback to median.
    """
    pts = [e["point"] for e in entries if e.get("point") is not None]
    if not pts:
        return None
    counts = Counter(pts)
    best_pt, best_ct = None, -1
    for pt, ct in counts.items():
        if ct > best_ct:
            best_pt, best_ct = pt, ct
        elif ct == best_ct:
            # tie-break: closer to median
            if abs(pt - median(pts)) < abs(best_pt - median(pts)):
                best_pt = pt
    return best_pt


def add_reject(stats, reason: str):
    stats["reject_reasons"][reason] = stats["reject_reasons"].get(reason, 0) + 1


def add_near_miss(stats, item: dict):
    stats["near_misses"].append(item)


def compute_value(odds_list, best_odds):
    med = median(odds_list)
    if not med or med <= 0:
        return None
    dev = (best_odds - med) / med
    edge = implied_prob(med) - implied_prob(best_odds)
    return med, edge, dev


def analyze_group(entries, min_books, stats, match, market_label, selection_label):
    """
    entries: list of {price, book, point}
    returns candidate dict or None
    """
    odds_list = [x["price"] for x in entries]
    if len(odds_list) < min_books:
        add_reject(stats, f"{market_label}: pas assez de books (>= {min_books})")
        return None

    med_edge = compute_value(odds_list, max(odds_list))
    if not med_edge:
        add_reject(stats, f"{market_label}: données invalides")
        return None

    stats["markets_tested"] += 1

    best = max(entries, key=lambda x: x["price"])
    best_odds = best["price"]
    med, edge, dev = compute_value(odds_list, best_odds)

    # near miss log
    add_near_miss(stats, {
        "match": match,
        "market": market_label,
        "selection": selection_label,
        "odds": best_odds,
        "book": best["book"],
        "edge": edge,
        "dev": dev
    })

    if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
        return {
            "market": market_label,
            "selection": selection_label,
            "odds": best_odds,
            "book": best["book"],
            "edge": edge,
            "dev": dev,
            "match": match,
            "median_odds": med,
            "books_used": sorted(list({x["book"] for x in entries})),
            "books_count": len(odds_list),
        }

    if edge < EDGE_THRESHOLD:
        add_reject(stats, f"Edge < {EDGE_THRESHOLD*100:.1f}%")
    if dev < DEV_THRESHOLD:
        add_reject(stats, f"Dev vs médiane < {DEV_THRESHOLD*100:.1f}%")
    return None


def pick_best_value_for_game(game, stats):
    home = game["home_team"]
    away = game["away_team"]
    match = f"{away} @ {home}"
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "Aucune cote bookmaker")
        return []

    candidates = []

    # -------- Moneyline (h2h) --------
    h2h_entries = collect_market_entries(bookmakers, "h2h")
    by_team = defaultdict(list)
    for e in h2h_entries:
        by_team[e["name"]].append(e)

    for team_name, entries in by_team.items():
        cand = analyze_group(
            entries=entries,
            min_books=MIN_BOOKMAKERS,
            stats=stats,
            match=match,
            market_label="MONEYLINE",
            selection_label=team_name
        )
        if cand:
            candidates.append(cand)

    # -------- Totals --------
    totals_entries = collect_market_entries(bookmakers, "totals")
    main_total = pick_main_line(totals_entries)
    if main_total is not None:
        for side in ["Over", "Under"]:
            entries = [e for e in totals_entries if e["name"] == side and e.get("point") == main_total]
            if not entries:
                continue
            cand = analyze_group(
                entries=entries,
                min_books=MIN_BOOKMAKERS,
                stats=stats,
                match=match,
                market_label="TOTAL",
                selection_label=f"{side} {main_total}"
            )
            if cand:
                cand["line"] = float(main_total)
                candidates.append(cand)

    # -------- Spreads --------
    spreads_entries = collect_market_entries(bookmakers, "spreads")
    main_spread = pick_main_line(spreads_entries)
    if main_spread is not None:
        for team in [home, away]:
            entries = [e for e in spreads_entries if e["name"] == team and e.get("point") == main_spread]
            if not entries:
                continue
            cand = analyze_group(
                entries=entries,
                min_books=MIN_BOOKMAKERS,
                stats=stats,
                match=match,
                market_label="SPREAD",
                selection_label=f"{team} {float(main_spread):+}"
            )
            if cand:
                cand["line"] = float(main_spread)
                candidates.append(cand)

    return candidates


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

    # rounding drift
    while sum(stakes) - remaining_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes


def format_rejects(reject_reasons: dict, top_n: int = 6) -> str:
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


def diversify_picks(picks, k):
    """
    Try to avoid 3 ML if spreads/totals are close.
    Simple heuristic:
      - sort by edge/dev
      - pick best overall
      - then try to include at least 1 non-ML if possible
    """
    picks = sorted(picks, key=lambda x: (x["edge"], x["dev"]), reverse=True)
    if len(picks) <= k:
        return picks

    chosen = []
    chosen_markets = Counter()

    for p in picks:
        if len(chosen) >= k:
            break
        # If we already have 2 ML and still none of SPREAD/TOTAL, prefer non-ML when close
        if k >= 3 and chosen_markets["MONEYLINE"] >= 2 and chosen_markets["SPREAD"] == 0 and chosen_markets["TOTAL"] == 0:
            if p["market"] == "MONEYLINE":
                continue
        chosen.append(p)
        chosen_markets[p["market"]] += 1

    # if we skipped too much, fill remaining with best leftovers
    if len(chosen) < k:
        for p in picks:
            if len(chosen) >= k:
                break
            if p in chosen:
                continue
            chosen.append(p)

    return chosen[:k]


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

    all_candidates = []
    for g in games:
        g_date = parser.isoparse(g["commence_time"]).date()
        if g_date != today_date:
            continue
        stats["games_today"] += 1
        cands = pick_best_value_for_game(g, stats)
        all_candidates.extend(cands)

    # Keep top per match to avoid 3 bets same game unless truly needed
    best_by_match = {}
    for c in all_candidates:
        m = c["match"]
        if m not in best_by_match:
            best_by_match[m] = c
        else:
            if (c["edge"], c["dev"]) > (best_by_match[m]["edge"], best_by_match[m]["dev"]):
                best_by_match[m] = c

    picks = list(best_by_match.values())

    # NO BET
    if not picks or remaining_team_slots == 0 or remaining_budget <= 0:
        if MAX_NO_BET_LOGS > 0 and LOG_WEBHOOK:
            reason = []
            if remaining_team_slots == 0:
                reason.append("limite TEAM bets/jour atteinte")
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

        # Props info message (kept simple)
        if PROPS_WEBHOOK:
            post_discord(
                PROPS_WEBHOOK,
                "ℹ️ Player Props",
                "Props: Phase 2 en cours. (Données props dépendent de l’API et du plan/coverage.)"
            )
        save_state()
        return

    # Select & diversify
    picks = diversify_picks(picks, remaining_team_slots)
    picks = sorted(picks, key=lambda x: (x["edge"], x["dev"]), reverse=True)

    stakes = allocate_stakes(len(picks), remaining_budget)

    for pick, stake in zip(picks, stakes):
        if stake <= 0:
            continue

        pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0
        median_odds = pick.get("median_odds")
        books_used = pick.get("books_used", [])
        books_count = pick.get("books_count", len(books_used))

        msg = (
            f"**Match:** {pick['match']}\n"
            f"**Marché:** {pick['market']}\n"
            + (f"**Line:** {pick.get('line')}\n" if pick.get("line") is not None else "")
            + f"**Sélection:** {pick['selection']}\n"
            f"**Meilleure cote:** {pick['odds']:.2f} (**{pick['book']}**)\n"
            f"**Books utilisés (médiane):** {books_count} | Cote médiane: {median_odds:.2f}\n"
            f"**Mise (budget jour):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
            f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
            f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
            f"_Max {MAX_TEAM_PER_DAY} TEAM bets/jour. Si la cote bouge fortement avant ton clic, ne force pas._"
        )

        post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

        STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
        STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    # Props scaffold
    if PROPS_WEBHOOK:
        if ENABLE_PROPS:
            post_discord(
                PROPS_WEBHOOK,
                "🧪 Player Props (Phase 2)",
                "Props activées côté bot, mais l’algorithme complet arrive à l’étape suivante."
            )
        else:
            post_discord(
                PROPS_WEBHOOK,
                "ℹ️ Player Props",
                "Props désactivées (ENABLE_PROPS=0). Phase 2: on va les activer proprement avec contrôle crédits + sélection top 3."
            )

    save_state()


if __name__ == "__main__":
    main()
