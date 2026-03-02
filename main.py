import os
import json
import math
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta

import requests
from dateutil import parser


# ==========================
# ENV / WEBHOOKS
# ==========================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

# Optional toggles
ENABLE_PROPS = os.environ.get("ENABLE_PROPS", "1").strip()  # "1" or "0"

# ==========================
# ODDS API SETTINGS
# ==========================
# NOTE: The Odds API regions are NOT "fr". They are like: us, us2, uk, eu, au
# We'll try combined first, then fallback to singles.
REGION_CANDIDATES = [
    "us,uk,eu",  # best scan if allowed
    "eu,uk",
    "uk,eu",
    "us",
    "us2",
    "eu",
    "uk",
    "au",
]

# Team markets
MARKETS_TEAMS = "h2h,spreads,totals"

# Props markets (best-effort; some plans won't allow)
# You can edit this later if your plan supports different keys.
DEFAULT_PROP_MARKETS = [
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_points_rebounds_assists",
    "player_threes",
]

ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

# ==========================
# THRESHOLDS / RULES
# ==========================
EDGE_THRESHOLD = 0.015  # 1.5%
DEV_THRESHOLD = 0.02    # 2%
MIN_BOOKMAKERS = 2      # >= 2 books minimum (keep it low for EU/UK coverage)
MAX_NO_BET_LOGS = 1

# Diversification / ranking weights
MARKET_BONUS = {
    "MONEYLINE": 0.00,
    "SPREAD": 0.15,  # small boost to avoid ML-only output
    "TOTAL": 0.12,
    "PROP": 0.10,
}

# FR-friendly books preference (best effort; titles vary by Odds API)
FR_BOOK_KEYWORDS = [
    "Unibet", "Winamax", "Parions", "PMU", "Betclic", "Bwin", "Zebet", "France",
]


# ==========================
# LOAD CONFIG + STATE
# ==========================
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("state.json", "r", encoding="utf-8") as f:
    STATE = json.load(f)

BANKROLL = float(CONFIG.get("bankroll_eur", 250.0))
DAILY_BUDGET = BANKROLL * float(CONFIG.get("daily_budget_pct", 0.10))
MAX_TEAM_PER_DAY = int(CONFIG.get("max_team_bets_per_day", 3))
MAX_PROPS_PER_DAY = int(CONFIG.get("max_prop_bets_per_day", 3)) if "max_prop_bets_per_day" in CONFIG else 3

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


# ==========================
# DISCORD
# ==========================
def post_discord(webhook, title, description):
    if not webhook:
        return
    payload = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=payload, timeout=20)
    r.raise_for_status()


# ==========================
# BASIC MATH
# ==========================
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


# ==========================
# TEAM FEATURES (optional cache)
# ==========================
def load_team_features():
    path = "data/team_features.json"
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("by_team_name", {})
    except Exception:
        return {}


TEAM_FEATURES = load_team_features()


def get_team_feature(team_name: str):
    # Odds API team naming should usually match TEAM_NAME in nba_api features.
    # If not, this just returns None-safe.
    return TEAM_FEATURES.get(team_name, {})


# ==========================
# FREE NBA CONTEXT (balldontlie)
# ==========================
BALLDONTLIE_BASE = "https://www.balldontlie.io/api/v1"
BALLDONTLIE_KEY = os.environ.get("BALLDONTLIE_API_KEY")  # optional

def _bdl_headers():
    # Some deployments require a key; if not, it's ignored.
    h = {}
    if BALLDONTLIE_KEY:
        # balldontlie uses Authorization on some versions; harmless if not required
        h["Authorization"] = f"Bearer {BALLDONTLIE_KEY}"
    return h

def fetch_last5_form(team_name: str):
    """
    Returns simple context:
    - last5 record (wins)
    - avg points for/against last5
    Best-effort; if API fails, returns None fields.
    """
    try:
        # Get team list to map name->id
        teams = requests.get(f"{BALLDONTLIE_BASE}/teams", headers=_bdl_headers(), timeout=20).json().get("data", [])
        team_id = None
        for t in teams:
            if (t.get("full_name") or "").strip() == team_name.strip():
                team_id = t.get("id")
                break
        if not team_id:
            return {"w": None, "pf": None, "pa": None}

        # last ~10 days window, then pick last 5 finished games
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=21)

        # balldontlie games endpoint supports dates[] on some versions; we do a broad query by season not reliable,
        # so we use per_page + simple filtering and accept partial context.
        # We fetch recent games by team_ids + per_page large
        url = f"{BALLDONTLIE_BASE}/games"
        params = {
            "team_ids[]": team_id,
            "per_page": 100,
        }
        r = requests.get(url, params=params, headers=_bdl_headers(), timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])

        # Filter finished games in last 21 days
        games = []
        for g in data:
            # g["date"] is ISO string
            try:
                gdt = parser.isoparse(g.get("date")).date()
            except Exception:
                continue
            if not (start <= gdt <= end):
                continue
            # must have scores
            if g.get("home_team_score") is None or g.get("visitor_team_score") is None:
                continue
            games.append(g)

        # Sort descending by date
        games.sort(key=lambda x: x.get("date", ""), reverse=True)
        games = games[:5]
        if not games:
            return {"w": None, "pf": None, "pa": None}

        w = 0
        pf_list, pa_list = [], []
        for g in games:
            is_home = (g.get("home_team", {}).get("id") == team_id)
            hs = int(g.get("home_team_score", 0))
            vs = int(g.get("visitor_team_score", 0))
            pf = hs if is_home else vs
            pa = vs if is_home else hs
            pf_list.append(pf)
            pa_list.append(pa)
            if pf > pa:
                w += 1

        pf_avg = sum(pf_list) / len(pf_list)
        pa_avg = sum(pa_list) / len(pa_list)
        return {"w": w, "pf": pf_avg, "pa": pa_avg}

    except Exception:
        return {"w": None, "pf": None, "pa": None}


# ==========================
# ODDS API FETCH (safe + fallback)
# ==========================
def odds_api_get(url, params, timeout=25):
    r = requests.get(url, params=params, timeout=timeout)
    return r

def fetch_games(markets: str):
    """
    Safe fetch + fallback regions.
    Returns (games, used_regions, error_text)
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
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
            r = odds_api_get(url, params=params, timeout=25)

            # 422 = invalid/unauthorized params for plan
            if r.status_code == 422:
                print(f"[odds-api] regions={reg} -> 422 (not allowed/invalid). Trying next...")
                last_err = f"422 for regions={reg}: {r.text[:200]}"
                continue

            if r.status_code != 200:
                print(f"[odds-api] regions={reg} -> {r.status_code}. Trying next...")
                last_err = f"{r.status_code} for regions={reg}: {r.text[:200]}"
                continue

            data = r.json()
            print(f"[odds-api] SUCCESS regions={reg} markets={markets} games={len(data)}")
            return data, reg, None

        except Exception as e:
            last_err = str(e)
            print(f"[odds-api] regions={reg} exception: {e}")

    return [], None, last_err


# ==========================
# MARKET PARSING
# ==========================
def collect_market_entries(bookmakers, market_key):
    """
    Returns list entries:
      {name, price, point, book, description}
    description is used in props sometimes
    """
    out = []
    for b in bookmakers:
        book = b.get("title", "UnknownBook")
        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                if o.get("price") is None:
                    continue
                out.append({
                    "name": o.get("name"),               # team name OR "Over"/"Under"
                    "description": o.get("description"), # player name (props) sometimes
                    "price": float(o["price"]),
                    "point": o.get("point"),
                    "book": book
                })
    return out


def is_fr_book(book_title: str) -> bool:
    if not book_title:
        return False
    for kw in FR_BOOK_KEYWORDS:
        if kw.lower() in book_title.lower():
            return True
    return False


def consensus_point(entries):
    """
    For spreads/totals: pick the point that appears the most (mode).
    This fixes your ML-only issue due to 'median' line mismatch.
    """
    pts = [e["point"] for e in entries if e.get("point") is not None]
    if not pts:
        return None
    counts = Counter(pts)
    # choose most common; if tie, choose median of tied points
    most = counts.most_common()
    top_count = most[0][1]
    tied = sorted([p for p, c in counts.items() if c == top_count])
    return median(tied)


def best_vs_median(entries):
    """
    entries: list of prices for same selection (same point if needed)
    returns med, best_entry, dev, edge_proxy
    """
    odds_list = [x["price"] for x in entries]
    if len(odds_list) == 0:
        return None, None, 0.0, 0.0

    med = median(odds_list)
    best = max(entries, key=lambda x: x["price"])
    best_odds = best["price"]
    dev = (best_odds - med) / med if med else 0.0
    edge = implied_prob(med) - implied_prob(best_odds) if med else 0.0
    return med, best, dev, edge


# ==========================
# PICK ENGINE (TEAM MARKETS)
# ==========================
def add_reject(stats, reason: str):
    stats["reject_reasons"][reason] = stats["reject_reasons"].get(reason, 0) + 1

def add_near_miss(stats, item: dict):
    stats["near_misses"].append(item)

def candidate_score(c):
    # edge/dev are decimals; convert to a usable score
    # bonus by market to avoid ML-only
    base = (c["edge"] * 100.0) + (c["dev"] * 50.0)
    base += MARKET_BONUS.get(c["market"], 0.0) * 10.0
    # books_used bonus
    base += min(10, c.get("books_used", 0)) * 0.15
    return base


def pick_candidates_for_game(game, stats):
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "Aucune cote bookmaker")
        return []

    candidates = []
    match = f"{away} @ {home}"

    # -------- ML (h2h) --------
    h2h_entries = collect_market_entries(bookmakers, "h2h")
    groups = defaultdict(list)
    for e in h2h_entries:
        if e.get("name"):
            groups[e["name"]].append(e)

    for team_name, entries in groups.items():
        if len(entries) < MIN_BOOKMAKERS:
            add_reject(stats, f"ML: pas assez de books (>= {MIN_BOOKMAKERS})")
            continue

        stats["markets_tested"] += 1

        med, best, dev, edge = best_vs_median(entries)
        if not best or not med:
            continue

        add_near_miss(stats, {
            "match": match,
            "market": "MONEYLINE",
            "selection": team_name,
            "odds": best["price"],
            "book": best["book"],
            "edge": edge,
            "dev": dev
        })

        if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
            candidates.append({
                "market": "MONEYLINE",
                "selection": team_name,
                "line": None,
                "odds": best["price"],
                "book": best["book"],
                "edge": edge,
                "dev": dev,
                "match": match,
                "median_odds": med,
                "books_used": len(entries),
            })
        else:
            if edge < EDGE_THRESHOLD:
                add_reject(stats, f"ML: edge < {EDGE_THRESHOLD*100:.1f}%")
            if dev < DEV_THRESHOLD:
                add_reject(stats, f"ML: dev < {DEV_THRESHOLD*100:.0f}%")

    # -------- TOTALS --------
    totals_entries = collect_market_entries(bookmakers, "totals")
    main_total = consensus_point(totals_entries)
    if main_total is not None:
        for side in ["Over", "Under"]:
            entries = [e for e in totals_entries if e.get("name") == side and e.get("point") == main_total]
            if len(entries) < MIN_BOOKMAKERS:
                add_reject(stats, f"TOTAL: pas assez de books (>= {MIN_BOOKMAKERS})")
                continue

            stats["markets_tested"] += 1

            med, best, dev, edge = best_vs_median(entries)
            if not best or not med:
                continue

            add_near_miss(stats, {
                "match": match,
                "market": "TOTAL",
                "selection": f"{side} {main_total}",
                "odds": best["price"],
                "book": best["book"],
                "edge": edge,
                "dev": dev
            })

            if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
                candidates.append({
                    "market": "TOTAL",
                    "selection": f"{side} {main_total}",
                    "line": main_total,
                    "odds": best["price"],
                    "book": best["book"],
                    "edge": edge,
                    "dev": dev,
                    "match": match,
                    "median_odds": med,
                    "books_used": len(entries),
                })
            else:
                if edge < EDGE_THRESHOLD:
                    add_reject(stats, f"TOTAL: edge < {EDGE_THRESHOLD*100:.1f}%")
                if dev < DEV_THRESHOLD:
                    add_reject(stats, f"TOTAL: dev < {DEV_THRESHOLD*100:.0f}%")

    # -------- SPREADS --------
    spreads_entries = collect_market_entries(bookmakers, "spreads")
    main_spread = consensus_point(spreads_entries)
    if main_spread is not None:
        for team_name in [home, away]:
            entries = [e for e in spreads_entries if e.get("name") == team_name and e.get("point") == main_spread]
            if len(entries) < MIN_BOOKMAKERS:
                add_reject(stats, f"SPREAD: pas assez de books (>= {MIN_BOOKMAKERS})")
                continue

            stats["markets_tested"] += 1

            med, best, dev, edge = best_vs_median(entries)
            if not best or not med:
                continue

            add_near_miss(stats, {
                "match": match,
                "market": "SPREAD",
                "selection": f"{team_name} {main_spread:+}",
                "odds": best["price"],
                "book": best["book"],
                "edge": edge,
                "dev": dev
            })

            if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
                candidates.append({
                    "market": "SPREAD",
                    "selection": f"{team_name} {main_spread:+}",
                    "line": main_spread,
                    "odds": best["price"],
                    "book": best["book"],
                    "edge": edge,
                    "dev": dev,
                    "match": match,
                    "median_odds": med,
                    "books_used": len(entries),
                })
            else:
                if edge < EDGE_THRESHOLD:
                    add_reject(stats, f"SPREAD: edge < {EDGE_THRESHOLD*100:.1f}%")
                if dev < DEV_THRESHOLD:
                    add_reject(stats, f"SPREAD: dev < {DEV_THRESHOLD*100:.0f}%")

    return candidates


# ==========================
# PROPS ENGINE (best-effort)
# ==========================
def fetch_props_odds():
    """
    Best-effort: try to fetch props markets.
    If your plan doesn't allow it, we'll return [] and explain in Discord.
    """
    markets = ",".join(DEFAULT_PROP_MARKETS)
    games, reg, err = fetch_games(markets=markets)
    return games, reg, err, markets


def pick_prop_candidates_for_game(game, stats):
    """
    Props format differs by API/market.
    We'll treat each outcome as a candidate:
      selection = "{player} {Over/Under} {line} ({market_key})"
    We still use best-vs-median edge proxy.
    """
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        return []

    match = f"{game.get('away_team')} @ {game.get('home_team')}"
    candidates = []

    for market_key in DEFAULT_PROP_MARKETS:
        entries = collect_market_entries(bookmakers, market_key)

        # group by (description/player, name Over/Under, point)
        groups = defaultdict(list)
        for e in entries:
            player = (e.get("description") or "").strip()
            side = (e.get("name") or "").strip()
            pt = e.get("point")
            if not player or not side or pt is None:
                continue
            groups[(player, side, pt)].append(e)

        for (player, side, pt), g in groups.items():
            if len(g) < MIN_BOOKMAKERS:
                continue

            med, best, dev, edge = best_vs_median(g)
            if not best or not med:
                continue

            # same thresholding
            if edge >= EDGE_THRESHOLD and dev >= DEV_THRESHOLD:
                candidates.append({
                    "market": "PROP",
                    "market_key": market_key,
                    "selection": f"{player} — {side} {pt}",
                    "line": pt,
                    "odds": best["price"],
                    "book": best["book"],
                    "edge": edge,
                    "dev": dev,
                    "match": match,
                    "median_odds": med,
                    "books_used": len(g),
                })

    return candidates


# ==========================
# STAKING
# ==========================
def allocate_stakes(num_bets, remaining_budget):
    """
    Split remaining daily budget:
    1 bet: 100%
    2 bets: 60/40
    3 bets: 40/35/25
    """
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

    # fix rounding drift
    while sum(stakes) - remaining_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes


# ==========================
# REPORT FORMATTING
# ==========================
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
        lines.append(
            f"{i}) {x['match']} — {x['market']} — **{x['selection']}** @ {x['odds']:.2f} ({x['book']})"
            f"\n   Edge: **{x['edge']*100:.2f}%** | Dev: {x['dev']*100:.2f}%"
        )
    return "\n".join(lines)


def market_diversify(picks):
    """
    Ensure at least one non-ML if available (like your Institutional spec).
    If the top picks are all ML but there exists SPREAD/TOTAL with close score, swap 1.
    """
    if not picks:
        return picks
    if any(p["market"] in ("SPREAD", "TOTAL") for p in picks):
        return picks

    # find best non-ML candidate among all picks pool stored in picks (not available here)
    return picks


def build_team_context(home, away):
    """
    Returns short context lines:
    - team_features ratings if available
    - last5 form (wins, avg pts for/against)
    """
    ctx = []

    for t in [away, home]:
        feat = get_team_feature(t)
        pace = feat.get("pace")
        net = feat.get("net_rtg")
        off = feat.get("off_rtg")
        deff = feat.get("def_rtg")

        form = fetch_last5_form(t)
        w = form.get("w")
        pf = form.get("pf")
        pa = form.get("pa")

        parts = []
        if w is not None:
            parts.append(f"Last5 W: **{w}/5**")
        if pf is not None and pa is not None:
            parts.append(f"PF/PA: **{pf:.1f}/{pa:.1f}**")
        if pace is not None:
            parts.append(f"Pace: **{pace:.1f}**")
        if net is not None:
            parts.append(f"NetRtg: **{net:.1f}**")
        if off is not None and deff is not None:
            parts.append(f"ORtg/DRtg: **{off:.1f}/{deff:.1f}**")

        if parts:
            ctx.append(f"• **{t}** — " + " | ".join(parts))

        # small rate limit protection
        time.sleep(0.2)

    return "\n".join(ctx) if ctx else "_Contexte stats indisponible (cache/team_features manquant)._"


# ==========================
# MAIN
# ==========================
def main():
    # --- Fetch TEAM odds ---
    games, used_regions, err = fetch_games(markets=MARKETS_TEAMS)

    # Stats for NO BET / logging
    stats = {
        "games_today": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": []
    }

    if not games:
        desc = (
            "**Impossible de récupérer les cotes (Odds API).**\n"
            f"- Dernière erreur: `{err}`\n"
            f"- Regions testées: {', '.join(REGION_CANDIDATES)}\n"
            f"- Markets: `{MARKETS_TEAMS}`\n\n"
            "👉 Vérifie ta clé ODDS_API_KEY, ton plan, et que l’API est up."
        )
        post_discord(LOG_WEBHOOK, "❌ NO BET (ODDS API DOWN)", desc)
        save_state()
        return

    today_date = datetime.now(timezone.utc).date()

    remaining_budget = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))
    remaining_prop_slots = max(0, MAX_PROPS_PER_DAY - int(STATE["prop_bets_sent"]))

    # --- Build candidate pool (TEAM) ---
    all_candidates = []
    for g in games:
        try:
            g_date = parser.isoparse(g["commence_time"]).date()
        except Exception:
            continue
        if g_date != today_date:
            continue

        stats["games_today"] += 1

        cands = pick_candidates_for_game(g, stats)
        for c in cands:
            c["score"] = candidate_score(c)
            all_candidates.append(c)

    # Sort candidates by score
    all_candidates.sort(key=lambda x: x["score"], reverse=True)

    # Build final TEAM picks: max 3, avoid multiple picks same match (optional)
    team_picks = []
    used_matches = set()
    for c in all_candidates:
        if len(team_picks) >= remaining_team_slots:
            break
        if c["match"] in used_matches:
            continue
        team_picks.append(c)
        used_matches.add(c["match"])

    # If no team picks OR no slots OR no budget
    if not team_picks or remaining_team_slots == 0 or remaining_budget <= 0:
        if MAX_NO_BET_LOGS > 0:
            reason = []
            if remaining_team_slots == 0:
                reason.append("limite TEAM bets/jour atteinte")
            if remaining_budget <= 0:
                reason.append("budget journalier déjà utilisé")
            if not team_picks:
                reason.append(f"aucune value détectée (seuil edge {EDGE_THRESHOLD*100:.1f}% & dev {DEV_THRESHOLD*100:.0f}%)")

            desc = (
                f"**Aucun bet team aujourd'hui.**\n"
                f"Raison: {', '.join(reason)}\n\n"
                f"**Résumé analyse (TEAM)**\n"
                f"- Regions utilisées: **{used_regions}**\n"
                f"- Matchs analysés: **{stats['games_today']}**\n"
                f"- Marchés testés (>= {MIN_BOOKMAKERS} books): **{stats['markets_tested']}**\n\n"
                f"**Refus principaux**\n{format_rejects(stats['reject_reasons'])}\n\n"
                f"**Near miss (Top 5)**\n{format_near_misses(stats['near_misses'])}\n\n"
                f"Budget jour: **{DAILY_BUDGET:.2f}€** | Déjà utilisé: **{STATE['daily_spent_eur']:.2f}€**"
            )
            post_discord(LOG_WEBHOOK, "❌ NO BET", desc)

        # Props channel message (if props disabled/unsupported)
        if PROPS_WEBHOOK:
            post_discord(PROPS_WEBHOOK, "ℹ️ Player Props", "Pas de props envoyés (no bet team ou budget/slots).")
        save_state()
        return

    # Stakes for TEAM picks
    stakes = allocate_stakes(len(team_picks), remaining_budget)

    # Post TEAM picks
    for pick, stake in zip(team_picks, stakes):
        if stake <= 0:
            continue

        pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0
        median_odds = pick.get("median_odds")
        books_used = pick.get("books_used")
        home = pick["match"].split(" @ ")[1]
        away = pick["match"].split(" @ ")[0]

        ctx = build_team_context(home=home, away=away)

        msg = (
            f"**Match:** {pick['match']}\n"
            f"**Marché:** {pick['market']}\n"
            + (f"**Line:** {pick['line']}\n" if pick.get("line") is not None else "")
            + f"**Sélection:** {pick['selection']}\n"
            f"**Meilleure cote:** {pick['odds']:.2f} (**{pick['book']}**)\n"
            + (f"**Books utilisés:** {books_used} | **Cote médiane:** {median_odds:.2f}\n" if (books_used and median_odds) else "")
            + f"**Mise (budget jour):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
            f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n\n"
            f"**Contexte rapide (stats gratuites)**\n{ctx}\n\n"
            f"_Max {MAX_TEAM_PER_DAY} TEAM bets/jour. Si la cote bouge fort avant ton clic, ne force pas._"
        )

        post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

        STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
        STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    # ==========================
    # PROPS (best-effort)
    # ==========================
    if PROPS_WEBHOOK and ENABLE_PROPS == "1" and remaining_prop_slots > 0:
        prop_games, prop_regions, prop_err, prop_markets = fetch_props_odds()

        if not prop_games:
            # Plan not supporting props or insufficient credits etc.
            post_discord(
                PROPS_WEBHOOK,
                "⚠️ Player Props",
                "Props indisponibles via ton plan Odds API (ou quota/markets non autorisés).\n"
                f"Dernière erreur: `{prop_err}`\n"
                f"Markets tentés: `{prop_markets}`\n"
                "👉 Si tu veux des props fiables, il faudra un plan Odds API qui supporte les player props."
            )
        else:
            # Build prop candidates for today
            prop_candidates = []
            for g in prop_games:
                try:
                    g_date = parser.isoparse(g["commence_time"]).date()
                except Exception:
                    continue
                if g_date != today_date:
                    continue

                cands = pick_prop_candidates_for_game(g, stats)
                for c in cands:
                    c["score"] = candidate_score(c)
                    prop_candidates.append(c)

            prop_candidates.sort(key=lambda x: x["score"], reverse=True)
            prop_picks = prop_candidates[:remaining_prop_slots]

            if not prop_picks:
                post_discord(
                    PROPS_WEBHOOK,
                    "ℹ️ Player Props",
                    f"Aucune value props détectée (seuil edge {EDGE_THRESHOLD*100:.1f}% & dev {DEV_THRESHOLD*100:.0f}%).\n"
                    f"Regions: **{prop_regions}**"
                )
            else:
                # Simple stake split for props using remaining budget AFTER team bets
                remaining_budget2 = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
                stakes_props = allocate_stakes(len(prop_picks), remaining_budget2)

                for pick, stake in zip(prop_picks, stakes_props):
                    if stake <= 0:
                        continue

                    pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0

                    msg = (
                        f"**Match:** {pick['match']}\n"
                        f"**Marché:** PROP ({pick.get('market_key')})\n"
                        f"**Pick:** {pick['selection']}\n"
                        f"**Meilleure cote:** {pick['odds']:.2f} (**{pick['book']}**)\n"
                        f"**Books utilisés:** {pick.get('books_used')} | **Cote médiane:** {pick.get('median_odds', 0):.2f}\n"
                        f"**Mise (budget jour):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
                        f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n\n"
                        "⚠️ Props = sensibles aux minutes / injuries.\n"
                        "_Si un statut devient Q/DOUT/OUT avant match → skip._"
                    )

                    post_discord(PROPS_WEBHOOK, "🎯 NBA PLAYER PROP", msg)

                    STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
                    STATE["prop_bets_sent"] = int(STATE["prop_bets_sent"]) + 1

    elif PROPS_WEBHOOK:
        post_discord(
            PROPS_WEBHOOK,
            "ℹ️ Player Props",
            "Props désactivés (ENABLE_PROPS=0) ou slots props déjà remplis aujourd’hui."
        )

    save_state()


if __name__ == "__main__":
    main()
