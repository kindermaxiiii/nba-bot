import os
import json
import math
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser

# --------------------------
# ENV
# --------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

# --------------------------
# ODDS API PARAMS
# --------------------------
# On élargit volontairement les régions (US/UK/EU) pour trouver + d'opportunités.
# On garde "fr" si ton plan l'autorise, mais c'est souvent bloquant -> fallback automatique.
MARKETS = "h2h,spreads,totals"
ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

# Fenêtre d'analyse : au lieu de "même date UTC", on prend les matchs qui démarrent
# dans les prochaines heures. Ça évite le "0 match analysé" selon fuseau et horaire.
LOOKAHEAD_HOURS = 18

# --------------------------
# THRESHOLDS (Phase 1)
# --------------------------
EDGE_THRESHOLD = 0.015  # 1.5%
DEV_THRESHOLD = 0.02    # 2%
MIN_BOOKMAKERS = 2      # >=2 books

# 1 seul NO_BET si aucun bet
MAX_NO_BET_LOGS = 1

# --------------------------
# BOOK PREFERENCE (FR)
# --------------------------
# Heuristique simple : on préfère ces books si disponibles, sinon best price global.
FR_BOOK_KEYWORDS = [
    "unibet", "winamax", "betclic", "parions", "pmu", "zebet",
    "bwin", "betsson", "pokerstars", "fr"
]

def is_fr_book(book_title: str) -> bool:
    if not book_title:
        return False
    s = book_title.strip().lower()
    return any(k in s for k in FR_BOOK_KEYWORDS)

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
    r = requests.post(webhook, json=data, timeout=20)
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

def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

# --------------------------
# GAME TIME FILTER
# --------------------------
def game_in_window(commence_time_iso: str, lookahead_hours: int) -> bool:
    try:
        t = parser.isoparse(commence_time_iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now <= t <= (now + timedelta(hours=lookahead_hours))
    except Exception:
        return False

# --------------------------
# ODDS API FETCH (robust)
# --------------------------
def fetch_games():
    """
    Fix 422 + plans limités:
    - regions accepte une liste séparée par virgules, MAIS certains plans refusent certaines régions.
    - On essaye des combos du + large au + safe.
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

    # Combos (du + large au + petit) :
    region_candidates = [
        "fr,eu,uk,us,us2",  # si autorisé, idéal
        "eu,uk,us,us2",
        "eu,uk,us",
        "uk,us",
        "eu",
        "uk",
        "us",
        "us2",
    ]

    last_err = None
    used_regions = None

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

            if r.status_code == 422:
                # paramètres invalides pour ton plan / régions non autorisées
                last_err = RuntimeError(f"422 for regions={reg}: {r.text[:300]}")
                print(f"[odds-api] regions={reg} -> 422. Trying next...")
                continue

            r.raise_for_status()
            data = r.json()
            used_regions = reg
            print(f"[odds-api] SUCCESS regions={reg} | games={len(data)}")
            return data, used_regions

        except Exception as e:
            print(f"[odds-api] regions={reg} failed: {e}")
            last_err = e

    raise RuntimeError(f"All regions failed. Last error: {last_err}")

# --------------------------
# MARKET PARSING
# --------------------------
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
                nm = o.get("name")
                pr = o.get("price")
                if nm is None or pr is None:
                    continue
                out.append({
                    "name": nm,
                    "price": float(pr),
                    "point": o.get("point"),
                    "book": book
                })
    return out

def choose_best_and_fr(entries):
    """
    Return:
      best_overall_entry, best_fr_entry (can be None)
    """
    if not entries:
        return None, None
    best = max(entries, key=lambda x: x["price"])
    fr_entries = [e for e in entries if is_fr_book(e.get("book", ""))]
    best_fr = max(fr_entries, key=lambda x: x["price"]) if fr_entries else None
    return best, best_fr

# --------------------------
# LOG HELPERS
# --------------------------
def add_reject(stats, reason: str):
    stats["reject_reasons"][reason] = stats["reject_reasons"].get(reason, 0) + 1

def add_near_miss(stats, item: dict):
    stats["near_misses"].append(item)

def format_rejects(reject_reasons: dict, top_n: int = 6) -> str:
    if not reject_reasons:
        return "- (aucune donnée)"
    items = sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return "\n".join([f"- {k}: {v}" for k, v in items])

def format_near_misses(near_misses: list, top_n: int = 5) -> str:
    if not near_misses:
        return "_Aucun near-miss._"
    near = [x for x in near_misses if x.get("edge") is not None and x["edge"] > 0]
    near.sort(key=lambda x: (x["edge"], x.get("dev", 0.0)), reverse=True)
    near = near[:top_n]
    if not near:
        return "_Aucun near-miss._"

    lines = []
    for i, x in enumerate(near, start=1):
        line = (
            f"{i}) {x['match']} — {x['market']} — **{x['selection']}** @ {x['odds']:.2f} ({x['book']})"
            f"\n   Edge: **{x['edge']*100:.2f}%** | Dev: {x.get('dev', 0.0)*100:.2f}%"
        )
        if x.get("fr_odds") is not None:
            line += f"\n   FR best: {x['fr_odds']:.2f} ({x.get('fr_book','?')})"
        lines.append(line)
    return "\n".join(lines)

# --------------------------
# CORE PICK ENGINE (Phase 1)
# --------------------------
def evaluate_candidate(odds_list, best_entry, best_fr, match, market, selection, line, stats):
    """
    best-vs-median proxy:
      dev = (best - median)/median
      edge = implied(median) - implied(best)
    """
    if len(odds_list) < MIN_BOOKMAKERS:
        add_reject(stats, f"{market}: pas assez de books (>= {MIN_BOOKMAKERS})")
        return None

    med = median(odds_list)
    if not med or med <= 0:
        add_reject(stats, f"{market}: médiane invalide")
        return None

    best_odds = best_entry["price"]
    dev = (best_odds - med) / med
    edge = implied_prob(med) - implied_prob(best_odds)

    fr_odds = best_fr["price"] if best_fr else None
    fr_book = best_fr["book"] if best_fr else None

    stats["markets_tested"] += 1

    # log near miss always (use best overall)
    add_near_miss(stats, {
        "match": match,
        "market": market,
        "selection": selection,
        "odds": best_odds,
        "book": best_entry["book"],
        "edge": edge,
        "dev": dev,
        "fr_odds": fr_odds,
        "fr_book": fr_book
    })

    if edge < EDGE_THRESHOLD:
        add_reject(stats, f"Edge < {EDGE_THRESHOLD*100:.1f}%")
        return None
    if dev < DEV_THRESHOLD:
        add_reject(stats, f"Dev vs médiane < {DEV_THRESHOLD*100:.1f}%")
        return None

    # pick selection to POST:
    # - prefer FR if exists and not too far from best (sinon on poste best global + warning)
    chosen = best_entry
    fr_used = False
    if best_fr:
        # si FR >= 98% du best price, on prend FR
        if best_fr["price"] >= 0.98 * best_entry["price"]:
            chosen = best_fr
            fr_used = True

    return {
        "match": match,
        "market": market,
        "selection": selection,
        "line": line,
        "odds": float(chosen["price"]),
        "book": chosen["book"],
        "edge": float(edge),
        "dev": float(dev),
        "books_used": len(odds_list),
        "median_odds": float(med),
        "best_global_odds": float(best_entry["price"]),
        "best_global_book": best_entry["book"],
        "best_fr_odds": float(fr_odds) if fr_odds is not None else None,
        "best_fr_book": fr_book,
        "fr_used": fr_used
    }

def pick_best_value_for_game(game, stats):
    home = game["home_team"]
    away = game["away_team"]
    match = f"{away} @ {home}"

    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "Aucune cote bookmaker")
        return None

    candidates = []

    # ---------- MONEYLINE ----------
    h2h_entries = collect_market_entries(bookmakers, "h2h")
    groups = {}
    for e in h2h_entries:
        groups.setdefault(e["name"], []).append(e)

    for outcome, entries in groups.items():
        odds_list = [x["price"] for x in entries]
        best, best_fr = choose_best_and_fr(entries)
        if not best:
            continue
        cand = evaluate_candidate(
            odds_list=odds_list,
            best_entry=best,
            best_fr=best_fr,
            match=match,
            market="MONEYLINE",
            selection=outcome,
            line=None,
            stats=stats
        )
        if cand:
            candidates.append(cand)

    # ---------- TOTALS ----------
    totals_entries = collect_market_entries(bookmakers, "totals")
    total_points = [e["point"] for e in totals_entries if e["point"] is not None]
    if total_points:
        main_total = median(sorted(total_points))
        for side in ["Over", "Under"]:
            entries = [e for e in totals_entries if e["name"] == side and e.get("point") == main_total]
            if not entries:
                continue
            odds_list = [x["price"] for x in entries]
            best, best_fr = choose_best_and_fr(entries)
            if not best:
                continue
            cand = evaluate_candidate(
                odds_list=odds_list,
                best_entry=best,
                best_fr=best_fr,
                match=match,
                market="TOTAL",
                selection=f"{side} {main_total}",
                line=main_total,
                stats=stats
            )
            if cand:
                candidates.append(cand)

    # ---------- SPREADS ----------
    spreads_entries = collect_market_entries(bookmakers, "spreads")
    spread_points = [e["point"] for e in spreads_entries if e["point"] is not None]
    if spread_points:
        main_spread = median(sorted(spread_points))
        for team in [home, away]:
            entries = [e for e in spreads_entries if e["name"] == team and e.get("point") == main_spread]
            if not entries:
                continue
            odds_list = [x["price"] for x in entries]
            best, best_fr = choose_best_and_fr(entries)
            if not best:
                continue
            cand = evaluate_candidate(
                odds_list=odds_list,
                best_entry=best,
                best_fr=best_fr,
                match=match,
                market="SPREAD",
                selection=f"{team} {main_spread:+}",
                line=main_spread,
                stats=stats
            )
            if cand:
                candidates.append(cand)

    if not candidates:
        return None

    # Rank: edge then dev, but favor non-ML a bit to avoid “only ML”
    def market_bonus(mkt: str) -> float:
        if mkt == "TOTAL":
            return 0.0005
        if mkt == "SPREAD":
            return 0.0005
        return 0.0

    candidates.sort(key=lambda x: (x["edge"] + market_bonus(x["market"]), x["dev"]), reverse=True)
    return candidates[0]

# --------------------------
# STAKES
# --------------------------
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

# --------------------------
# MAIN
# --------------------------
def main():
    games, used_regions = fetch_games()

    remaining_budget = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))

    stats = {
        "regions_used": used_regions,
        "games_window": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": []
    }

    picks = []
    for g in games:
        if not game_in_window(g.get("commence_time", ""), LOOKAHEAD_HOURS):
            continue

        stats["games_window"] += 1
        pick = pick_best_value_for_game(g, stats)
        if pick:
            picks.append(pick)

    # NO BET
    if not picks or remaining_team_slots == 0 or remaining_budget <= 0:
        if MAX_NO_BET_LOGS > 0:
            reason = []
            if remaining_team_slots == 0:
                reason.append("limite TEAM bets/jour atteinte")
            if remaining_budget <= 0:
                reason.append("budget journalier déjà utilisé")
            if not picks:
                reason.append(f"aucune value détectée (edge {EDGE_THRESHOLD*100:.1f}% & dev {DEV_THRESHOLD*100:.1f}%)")

            desc = (
                f"**Aucun bet team aujourd'hui.**\n"
                f"Raison: {', '.join(reason)}\n\n"
                f"**Résumé analyse (TEAM)**\n"
                f"- Regions utilisées: **{stats['regions_used']}**\n"
                f"- Matchs (fenêtre {LOOKAHEAD_HOURS}h): **{stats['games_window']}**\n"
                f"- Marchés testés (>= {MIN_BOOKMAKERS} books): **{stats['markets_tested']}**\n\n"
                f"**Refus principaux**\n{format_rejects(stats['reject_reasons'])}\n\n"
                f"**Near miss (Top 5)**\n{format_near_misses(stats['near_misses'])}\n\n"
                f"Budget jour: **{DAILY_BUDGET:.2f}€** | Déjà utilisé: **{STATE['daily_spent_eur']:.2f}€**"
            )
            post_discord(LOG_WEBHOOK, "❌ NO BET", desc)

        # Props message (Phase 1 = info only)
        if PROPS_WEBHOOK:
            post_discord(
                PROPS_WEBHOOK,
                "ℹ️ Player Props",
                "Pas de props envoyées (Phase 1). Phase 2 = props auto (nécessite marchés props/plan + injuries/minutes)."
            )

        save_state()
        return

    # Picks
    picks.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)
    picks = picks[:remaining_team_slots]
    stakes = allocate_stakes(len(picks), remaining_budget)

    for pick, stake in zip(picks, stakes):
        if stake <= 0:
            continue

        pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0

        warn = ""
        if not pick.get("fr_used", False) and pick.get("best_fr_odds") is not None:
            warn = " — ⚠️ best non-FR (FR dispo mais moins bon)"

        msg = (
            f"**Match:** {pick['match']}\n"
            f"**Marché:** {pick['market']}\n"
            + (f"**Line:** {pick['line']}\n" if pick.get("line") is not None else "")
            + f"**Sélection:** {pick['selection']}\n"
            f"**Meilleure cote (préférence FR):** {pick['odds']:.2f} (**{pick['book']}**){warn}\n"
            + (f"**FR best:** {pick['best_fr_odds']:.2f} ({pick['best_fr_book']})\n" if pick.get("best_fr_odds") is not None else "")
            + f"**Books utilisés (médiane):** {pick['books_used']} | **Cote médiane:** {pick['median_odds']:.2f}\n"
            f"**Mise (budget jour):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
            f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
            f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
            f"_Max {MAX_TEAM_PER_DAY} TEAM bets/jour. Si la cote bouge fortement avant ton clic, ne force pas._"
        )

        post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

        STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
        STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    # Props message (Phase 1 = info only)
    if PROPS_WEBHOOK:
        post_discord(
            PROPS_WEBHOOK,
            "ℹ️ Player Props",
            "Pas de props envoyées (Phase 1). Phase 2 = props auto (nécessite marchés props/plan + injuries/minutes)."
        )

    save_state()

if __name__ == "__main__":
    main()
