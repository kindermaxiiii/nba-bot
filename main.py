import os
import json
import math
import requests
from datetime import datetime, timezone
from dateutil import parser as dtparser
from zoneinfo import ZoneInfo

# =========================
# ENV
# =========================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

# =========================
# SETTINGS
# =========================
TZ_SLATE = ZoneInfo("Europe/Paris")

# Odds API
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

# Regions fallback (422 fix)
REGION_CANDIDATES = ["fr", "eu", "uk", "us", "us2", "au"]

# Markets (TEAM)
TEAM_MARKETS = "h2h,spreads,totals"

# Markets (PROPS)
# The Odds API naming generally follows these keys.
# If your plan doesn't include props, the request may return empty markets.
PROPS_MARKETS = ",".join([
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_points_rebounds_assists",
    "player_points_rebounds",
    "player_points_assists",
    "player_rebounds_assists",
])

# Thresholds (TEAM)
TEAM_EDGE_THRESHOLD = 0.015  # 1.5%
TEAM_DEV_THRESHOLD = 0.02    # 2%
TEAM_MIN_BOOKMAKERS = 2      # >=2

# Thresholds (PROPS) – un peu plus strict par défaut
PROPS_EDGE_THRESHOLD = 0.018  # 1.8%
PROPS_DEV_THRESHOLD = 0.02    # 2%
PROPS_MIN_BOOKMAKERS = 2      # >=2

# Portfolio rules
MAX_NO_BET_LOGS = 1
MAX_NO_BET_PROPS_LOGS = 1
MAX_PROPS_PER_DAY_DEFAULT = 3

# Budget split (daily budget = bankroll * daily_budget_pct)
TEAM_BUDGET_SHARE = 0.70  # 70% pour team
PROPS_BUDGET_SHARE = 0.30 # 30% pour props

# =========================
# LOAD CONFIG + STATE
# =========================
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("state.json", "r", encoding="utf-8") as f:
    STATE = json.load(f)

BANKROLL = float(CONFIG["bankroll_eur"])
DAILY_BUDGET = BANKROLL * float(CONFIG["daily_budget_pct"])
MAX_TEAM_PER_DAY = int(CONFIG["max_team_bets_per_day"])
MAX_PROPS_PER_DAY = int(CONFIG.get("max_prop_bets_per_day", MAX_PROPS_PER_DAY_DEFAULT))

today_utc = datetime.now(timezone.utc).date().isoformat()
if STATE.get("date_utc") != today_utc:
    STATE = {
        "date_utc": today_utc,
        "daily_spent_eur": 0.0,
        "team_bets_sent": 0,
        "prop_bets_sent": 0
    }

# =========================
# UTILS
# =========================
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

def to_slate_date(iso_dt: str):
    # Convert commence_time to Europe/Paris date for "today slate"
    dt = dtparser.isoparse(iso_dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_SLATE).date()

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

# =========================
# ODDS API FETCH
# =========================
def fetch_odds(markets: str):
    """
    Try multiple regions to avoid 422 depending on plan.
    Returns: (region_used, data_list)
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    last_err = None
    for reg in REGION_CANDIDATES:
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": reg,
            "markets": markets,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }
        try:
            r = requests.get(ODDS_API_URL, params=params, timeout=25)

            if r.status_code == 422:
                print(f"[odds-api] region={reg} -> 422 (not allowed/invalid). next...")
                last_err = RuntimeError(f"422 for regions={reg}: {r.text[:300]}")
                continue

            r.raise_for_status()
            data = r.json()
            print(f"[odds-api] SUCCESS region={reg} markets={markets} games={len(data)}")
            return reg, data

        except Exception as e:
            print(f"[odds-api] region={reg} failed: {e}")
            last_err = e

    raise RuntimeError(f"All regions failed for markets={markets}. Last error: {last_err}")

# =========================
# PARSING HELPERS
# =========================
def collect_market_entries(bookmakers, market_key):
    """
    Returns list of entries:
      TEAM:
        {name, price, point, book}
      PROPS:
        The Odds API often returns outcomes with:
          - name: "Over"/"Under"
          - point: numeric line
          - description: player name (common)
      We'll include 'description' when present.
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
                    "name": o.get("name"),
                    "price": safe_float(o.get("price")),
                    "point": o.get("point"),
                    "description": o.get("description"),  # often player for props
                    "book": book
                })
    return [x for x in out if x["price"] is not None]

def add_reject(stats, reason: str):
    stats["reject_reasons"][reason] = stats["reject_reasons"].get(reason, 0) + 1

def add_near_miss(stats, item: dict):
    stats["near_misses"].append(item)

# =========================
# TEAM CANDIDATES (ML / SPREAD / TOTAL)
# =========================
def best_candidate_h2h(game, stats):
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "Aucune cote bookmaker")
        return None

    entries = collect_market_entries(bookmakers, "h2h")
    groups = {}
    for e in entries:
        groups.setdefault(e["name"], []).append(e)

    best_out = None
    for outcome, ents in groups.items():
        odds_list = [x["price"] for x in ents if x["price"]]
        if len(odds_list) < TEAM_MIN_BOOKMAKERS:
            add_reject(stats, f"ML: pas assez de books (>= {TEAM_MIN_BOOKMAKERS})")
            continue

        stats["markets_tested"] += 1
        med = median(odds_list)
        best = max(ents, key=lambda x: x["price"])
        best_odds = best["price"]
        dev = (best_odds - med) / med if med else 0.0
        edge = implied_prob(med) - implied_prob(best_odds) if med else 0.0

        add_near_miss(stats, {
            "match": f"{away} @ {home}",
            "market": "MONEYLINE",
            "selection": outcome,
            "line": None,
            "odds": best_odds,
            "book": best["book"],
            "edge": edge,
            "dev": dev
        })

        cand = {
            "match": f"{away} @ {home}",
            "market": "MONEYLINE",
            "selection": outcome,
            "line": None,
            "odds": best_odds,
            "book": best["book"],
            "books_used": len(odds_list),
            "median_odds": med,
            "edge": edge,
            "dev": dev,
        }

        if best_out is None or (cand["edge"], cand["dev"]) > (best_out["edge"], best_out["dev"]):
            best_out = cand

    if not best_out:
        return None

    if best_out["edge"] < TEAM_EDGE_THRESHOLD:
        add_reject(stats, f"ML: Edge < {TEAM_EDGE_THRESHOLD*100:.1f}%")
        return None
    if best_out["dev"] < TEAM_DEV_THRESHOLD:
        add_reject(stats, f"ML: Dev < {TEAM_DEV_THRESHOLD*100:.0f}%")
        return None

    return best_out

def best_candidate_by_line(game, stats, market_key, market_label):
    """
    For spreads/totals:
    - group by 'point' (line)
    - for each line, compute best vs median
    - return the best line candidate that meets thresholds
    """
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "Aucune cote bookmaker")
        return None

    entries = collect_market_entries(bookmakers, market_key)
    if not entries:
        add_reject(stats, f"{market_label}: aucune donnée")
        return None

    # group by (name, point) so Over/Under or team line are separate
    groups = {}
    for e in entries:
        key = (e.get("name"), e.get("point"))
        groups.setdefault(key, []).append(e)

    best_out = None

    for (name, point), ents in groups.items():
        odds_list = [x["price"] for x in ents if x["price"]]
        if len(odds_list) < TEAM_MIN_BOOKMAKERS:
            add_reject(stats, f"{market_label}: pas assez de books (>= {TEAM_MIN_BOOKMAKERS})")
            continue

        stats["markets_tested"] += 1
        med = median(odds_list)
        best = max(ents, key=lambda x: x["price"])
        best_odds = best["price"]
        dev = (best_odds - med) / med if med else 0.0
        edge = implied_prob(med) - implied_prob(best_odds) if med else 0.0

        # nice selection text
        if market_key == "totals":
            selection = f"{name} {point}"
        else:  # spreads -> name is team, point is spread
            # point already has sign from API (usually negative for favorite)
            try:
                selection = f"{name} {float(point):+g}"
            except Exception:
                selection = f"{name} {point}"

        add_near_miss(stats, {
            "match": f"{away} @ {home}",
            "market": market_label,
            "selection": selection,
            "line": point,
            "odds": best_odds,
            "book": best["book"],
            "edge": edge,
            "dev": dev
        })

        cand = {
            "match": f"{away} @ {home}",
            "market": market_label,
            "selection": selection,
            "line": point,
            "odds": best_odds,
            "book": best["book"],
            "books_used": len(odds_list),
            "median_odds": med,
            "edge": edge,
            "dev": dev,
        }

        if best_out is None or (cand["edge"], cand["dev"]) > (best_out["edge"], best_out["dev"]):
            best_out = cand

    if not best_out:
        return None

    if best_out["edge"] < TEAM_EDGE_THRESHOLD:
        add_reject(stats, f"{market_label}: Edge < {TEAM_EDGE_THRESHOLD*100:.1f}%")
        return None
    if best_out["dev"] < TEAM_DEV_THRESHOLD:
        add_reject(stats, f"{market_label}: Dev < {TEAM_DEV_THRESHOLD*100:.0f}%")
        return None

    return best_out

def build_team_candidates_for_game(game, stats):
    cands = []
    ml = best_candidate_h2h(game, stats)
    if ml:
        cands.append(ml)

    tot = best_candidate_by_line(game, stats, "totals", "TOTAL")
    if tot:
        cands.append(tot)

    sp = best_candidate_by_line(game, stats, "spreads", "SPREAD")
    if sp:
        cands.append(sp)

    return cands

# =========================
# PROPS CANDIDATES
# =========================
def props_market_label(key: str) -> str:
    return {
        "player_points": "PTS",
        "player_rebounds": "REB",
        "player_assists": "AST",
        "player_threes": "3PM",
        "player_points_rebounds_assists": "PRA",
        "player_points_rebounds": "PR",
        "player_points_assists": "PA",
        "player_rebounds_assists": "RA",
    }.get(key, key)

def build_props_candidates_for_game(game, stats):
    """
    Build candidates for:
      points, rebounds, assists, threes, PRA, PR, PA, RA
    Each prop outcome is typically Over/Under at a point line.
    We select best vs median per (player, market, point, side).
    """
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "PROPS: aucune cote bookmaker")
        return []

    all_cands = []

    for mkey in PROPS_MARKETS.split(","):
        entries = collect_market_entries(bookmakers, mkey)
        if not entries:
            continue

        # group by (player, side, point)
        groups = {}
        for e in entries:
            side = e.get("name")  # Over/Under
            point = e.get("point")
            player = e.get("description") or "Unknown Player"
            if side not in ("Over", "Under"):
                # sometimes APIs may use team name etc; ignore
                continue
            if point is None:
                continue
            groups.setdefault((player, side, point), []).append(e)

        for (player, side, point), ents in groups.items():
            odds_list = [x["price"] for x in ents if x["price"]]
            if len(odds_list) < PROPS_MIN_BOOKMAKERS:
                add_reject(stats, f"PROPS: pas assez de books (>= {PROPS_MIN_BOOKMAKERS})")
                continue

            stats["markets_tested"] += 1
            med = median(odds_list)
            best = max(ents, key=lambda x: x["price"])
            best_odds = best["price"]
            dev = (best_odds - med) / med if med else 0.0
            edge = implied_prob(med) - implied_prob(best_odds) if med else 0.0

            label = props_market_label(mkey)
            selection = f"{player} — {label} {side} {point}"

            add_near_miss(stats, {
                "match": f"{away} @ {home}",
                "market": f"PROP {label}",
                "selection": selection,
                "line": point,
                "odds": best_odds,
                "book": best["book"],
                "edge": edge,
                "dev": dev
            })

            if edge < PROPS_EDGE_THRESHOLD or dev < PROPS_DEV_THRESHOLD:
                if edge < PROPS_EDGE_THRESHOLD:
                    add_reject(stats, f"PROPS: Edge < {PROPS_EDGE_THRESHOLD*100:.1f}%")
                if dev < PROPS_DEV_THRESHOLD:
                    add_reject(stats, f"PROPS: Dev < {PROPS_DEV_THRESHOLD*100:.0f}%")
                continue

            all_cands.append({
                "match": f"{away} @ {home}",
                "market": f"PROP {label}",
                "selection": selection,
                "player": player,
                "line": point,
                "side": side,
                "odds": best_odds,
                "book": best["book"],
                "books_used": len(odds_list),
                "median_odds": med,
                "edge": edge,
                "dev": dev,
                "prop_key": mkey,
            })

    return all_cands

# =========================
# STAKES
# =========================
def allocate_stakes(num_bets, budget_amount):
    """
    Split budget_amount into 60/40 or 40/35/25.
    """
    if num_bets <= 0:
        return []
    if num_bets == 1:
        splits = [1.0]
    elif num_bets == 2:
        splits = [0.6, 0.4]
    else:
        splits = [0.4, 0.35, 0.25]

    planned = [budget_amount * s for s in splits[:num_bets]]
    stakes = [round(x, 2) for x in planned]

    # ensure rounding doesn't exceed budget
    while sum(stakes) - budget_amount > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes

# =========================
# FORMATTING
# =========================
def format_rejects(reject_reasons: dict, top_n: int = 6) -> str:
    if not reject_reasons:
        return "- (aucune donnée)"
    items = sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return "\n".join([f"- {k}: {v}" for k, v in items])

def format_near_misses(near_misses: list, top_n: int = 5) -> str:
    if not near_misses:
        return "_Aucun near-miss._"
    near = [x for x in near_misses if x.get("edge") is not None and x["edge"] > 0]
    near.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)
    near = near[:top_n]
    if not near:
        return "_Aucun near-miss._"
    lines = []
    for i, x in enumerate(near, start=1):
        odds = x.get("odds")
        lines.append(
            f"{i}) {x['match']} — {x['market']} — **{x['selection']}** @ {odds:.2f} ({x['book']})"
            f"\n   Edge: **{x['edge']*100:.2f}%** | Dev: {x['dev']*100:.2f}%"
        )
    return "\n".join(lines)

def pick_is_ml(pick: dict) -> bool:
    return pick.get("market") == "MONEYLINE"

# =========================
# MAIN
# =========================
def main():
    # ---------- Fetch TEAM odds ----------
    region_team, games_team = fetch_odds(TEAM_MARKETS)

    # ---------- Fetch PROPS odds (non bloquant) ----------
    region_props = None
    games_props = []
    try:
        region_props, games_props = fetch_odds(PROPS_MARKETS)
    except Exception as e:
        print(f"[props] fetch failed (ok if plan doesn't support): {e}")

    # Determine slate date in Europe/Paris
    today_slate = datetime.now(TZ_SLATE).date()

    # Remaining budgets/slots
    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    # Split remaining budget between TEAM/PROPS
    team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
    props_budget = remaining_budget_total * PROPS_BUDGET_SHARE

    # ---------- TEAM analysis ----------
    stats_team = {
        "games_today": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": [],
        "regions_used": region_team
    }

    team_candidates_all = []
    for g in games_team:
        if to_slate_date(g["commence_time"]) != today_slate:
            continue
        stats_team["games_today"] += 1
        team_candidates_all.extend(build_team_candidates_for_game(g, stats_team))

    # Keep only best candidate per match+market? (avoid duplicates explosion)
    # We'll keep top candidate per game per market already; that's fine.

    # Sort all candidates by (edge, dev)
    team_candidates_all.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)

    # Portfolio selection with diversification:
    # - target up to remaining_team_slots
    # - try to include at least 1 non-ML if available among top 20
    team_picks = []
    if remaining_team_slots > 0 and team_budget > 0 and team_candidates_all:
        top_pool = team_candidates_all[:20]  # pool for diversification

        non_ml = [x for x in top_pool if not pick_is_ml(x)]
        ml = [x for x in top_pool if pick_is_ml(x)]

        if remaining_team_slots >= 2 and non_ml:
            # pick best non-ML + best overall (then fill)
            best_non_ml = non_ml[0]
            team_picks.append(best_non_ml)

            for cand in top_pool:
                if cand is best_non_ml:
                    continue
                team_picks.append(cand)
                if len(team_picks) >= remaining_team_slots:
                    break
        else:
            team_picks = top_pool[:remaining_team_slots]

        # final trim
        team_picks = team_picks[:remaining_team_slots]

    # ---------- PROPS analysis ----------
    stats_props = {
        "games_today": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": [],
        "regions_used": region_props or ""
    }

    props_candidates_all = []
    if games_props:
        for g in games_props:
            if to_slate_date(g["commence_time"]) != today_slate:
                continue
            stats_props["games_today"] += 1
            props_candidates_all.extend(build_props_candidates_for_game(g, stats_props))

    props_candidates_all.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)

    # pick up to 3 props, but avoid duplicates same player
    prop_picks = []
    used_players = set()
    if remaining_props_slots > 0 and props_budget > 0 and props_candidates_all:
        for cand in props_candidates_all:
            player = (cand.get("player") or "").strip().lower()
            if not player:
                continue
            if player in used_players:
                continue
            prop_picks.append(cand)
            used_players.add(player)
            if len(prop_picks) >= remaining_props_slots:
                break

    # ---------- If NO TEAM picks ----------
    if (not team_picks) or remaining_team_slots == 0 or team_budget <= 0:
        if MAX_NO_BET_LOGS > 0:
            reason = []
            if remaining_team_slots == 0:
                reason.append("limite TEAM bets/jour atteinte")
            if team_budget <= 0:
                reason.append("budget TEAM épuisé")
            if not team_picks:
                reason.append(f"aucune value TEAM (edge≥{TEAM_EDGE_THRESHOLD*100:.1f}% & dev≥{TEAM_DEV_THRESHOLD*100:.0f}%)")

            desc = (
                f"**Aucun bet TEAM aujourd'hui.**\n"
                f"Raison: {', '.join(reason)}\n\n"
                f"**Résumé analyse (TEAM)**\n"
                f"- Regions utilisées: **{stats_team['regions_used']}**\n"
                f"- Matchs analysés: **{stats_team['games_today']}**\n"
                f"- Marchés testés (>= {TEAM_MIN_BOOKMAKERS} books): **{stats_team['markets_tested']}**\n\n"
                f"**Refus principaux**\n{format_rejects(stats_team['reject_reasons'])}\n\n"
                f"**Near miss (Top 5)**\n{format_near_misses(stats_team['near_misses'], top_n=5)}\n\n"
                f"Budget jour: **{DAILY_BUDGET:.2f}€** | Déjà utilisé: **{STATE['daily_spent_eur']:.2f}€**"
            )
            post_discord(LOG_WEBHOOK, "❌ NO BET (TEAM)", desc)

    # ---------- If NO PROPS picks ----------
    if PROPS_WEBHOOK:
        if (not prop_picks) or remaining_props_slots == 0 or props_budget <= 0:
            if MAX_NO_BET_PROPS_LOGS > 0:
                reason = []
                if not games_props:
                    reason.append("plan OddsAPI sans props / props indisponibles")
                if remaining_props_slots == 0:
                    reason.append("limite PROPS/jour atteinte")
                if props_budget <= 0:
                    reason.append("budget PROPS épuisé")
                if not prop_picks:
                    reason.append(f"aucune value PROPS (edge≥{PROPS_EDGE_THRESHOLD*100:.1f}% & dev≥{PROPS_DEV_THRESHOLD*100:.0f}%)")

                desc = (
                    f"**Aucun bet PROPS envoyé.**\n"
                    f"Raison: {', '.join(reason)}\n\n"
                    f"**Résumé analyse (PROPS)**\n"
                    f"- Regions utilisées: **{stats_props['regions_used'] or '-'}**\n"
                    f"- Matchs analysés: **{stats_props['games_today']}**\n"
                    f"- Marchés testés (>= {PROPS_MIN_BOOKMAKERS} books): **{stats_props['markets_tested']}**\n\n"
                    f"**Refus principaux**\n{format_rejects(stats_props['reject_reasons'])}\n\n"
                    f"**Near miss (Top 5)**\n{format_near_misses(stats_props['near_misses'], top_n=5)}\n"
                )
                post_discord(PROPS_WEBHOOK, "ℹ️ NBA PLAYER PROPS", desc)
        else:
            # we'll send actual props below
            pass

    # ---------- Send TEAM picks ----------
    if team_picks and remaining_team_slots > 0 and team_budget > 0:
        stakes_team = allocate_stakes(len(team_picks), min(team_budget, remaining_budget_total))

        for pick, stake in zip(team_picks, stakes_team):
            if stake <= 0:
                continue

            pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0
            median_odds = pick.get("median_odds")
            books_used = pick.get("books_used")

            msg = (
                f"**Match:** {pick['match']}\n"
                f"**Marché:** {pick['market']}\n"
                + (f"**Line:** {pick['line']}\n" if pick.get("line") is not None else "")
                + f"**Sélection:** {pick['selection']}\n"
                f"**Meilleure cote:** {pick['odds']:.2f} (**{pick['book']}**)\n"
                + (f"**Books utilisés:** {books_used} | **Cote médiane:** {median_odds:.2f}\n" if (books_used and median_odds) else "")
                + f"**Mise (budget jour):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
                f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
                f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
                f"_Diversification activée: on essaye d'inclure spreads/totals si edge validé. Max {MAX_TEAM_PER_DAY} TEAM bets/jour._"
            )

            post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)
            STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
            STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    # ---------- Send PROPS picks ----------
    if PROPS_WEBHOOK and prop_picks and remaining_props_slots > 0 and props_budget > 0:
        stakes_props = allocate_stakes(len(prop_picks), min(props_budget, max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))))

        for pick, stake in zip(prop_picks, stakes_props):
            if stake <= 0:
                continue

            pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0
            median_odds = pick.get("median_odds")
            books_used = pick.get("books_used")

            msg = (
                f"**Match:** {pick['match']}\n"
                f"**Marché:** {pick['market']}\n"
                f"**Sélection:** {pick['selection']}\n"
                f"**Meilleure cote:** {pick['odds']:.2f} (**{pick['book']}**)\n"
                + (f"**Books utilisés:** {books_used} | **Cote médiane:** {median_odds:.2f}\n" if (books_used and median_odds) else "")
                + f"**Mise (budget jour):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
                f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
                f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
                f"_Props: 1 pick max par joueur. Max {MAX_PROPS_PER_DAY} props/jour._"
            )

            post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROP", msg)
            STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
            STATE["prop_bets_sent"] = int(STATE.get("prop_bets_sent", 0)) + 1

    save_state()

if __name__ == "__main__":
    main()
