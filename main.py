import os
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparser
from zoneinfo import ZoneInfo

# =========================
# ENV
# =========================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
BALLDONTLIE_API_KEY = os.environ.get("BALLDONTLIE_API_KEY")  # optionnel (injuries)
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

# =========================
# TIME / SLATE WINDOW
# =========================
TZ_PARIS = ZoneInfo("Europe/Paris")
LOOKAHEAD_HOURS = 36          # fenêtre future
PAST_GRACE_HOURS = 3          # inclure matchs qui viennent de démarrer

# =========================
# ODDS API
# =========================
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"
ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

# Regions valides (The Odds API): us, us2, uk, au, eu
REGION_CANDIDATES_TEAM = ["eu", "uk", "us", "us2", "au"]
REGION_CANDIDATES_PROPS = ["us", "us2", "eu", "uk", "au"]

TEAM_MARKETS = "h2h,spreads,totals"

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

# =========================
# THRESHOLDS
# =========================
TEAM_EDGE_THRESHOLD = 0.015   # 1.5%
TEAM_DEV_THRESHOLD = 0.02     # 2%
TEAM_MIN_BOOKS = 2

PROPS_EDGE_THRESHOLD = 0.018  # 1.8%
PROPS_DEV_THRESHOLD = 0.02    # 2%
PROPS_MIN_BOOKS = 2

MAX_NO_BET_LOGS = 1

# Diversification
MAX_ML_IN_PORTFOLIO = 2  # sur 3 picks team, on tente de limiter à 2 ML si possible
MAX_1_PICK_PER_MATCH = True

# Budget split (sur le budget journalier 10%)
TEAM_BUDGET_SHARE = 0.70
PROPS_BUDGET_SHARE = 0.30

# FR books detection (pour afficher "FR best" si possible)
FR_BOOK_KEYWORDS = [
    "Winamax", "Betclic", "Unibet", "Parions Sport", "PMU", "ZEbet", "Bwin",
    "PokerStars", "Vbet", "NetBet", "France"
]

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
MAX_PROPS_PER_DAY = int(CONFIG.get("max_prop_bets_per_day", 3))

today_utc = datetime.now(timezone.utc).date().isoformat()

def reset_state_for_day():
    return {
        "date_utc": today_utc,
        "daily_spent_eur": 0.0,
        "team_spent_eur": 0.0,
        "props_spent_eur": 0.0,
        "team_bets_sent": 0,
        "prop_bets_sent": 0,
        "props_scan_done": False,   # pour éviter de cramer des crédits 2x/jour
        "last_regions_team": "",
        "last_regions_props": ""
    }

if STATE.get("date_utc") != today_utc:
    STATE = reset_state_for_day()
else:
    # compat si state.json ancien
    STATE.setdefault("daily_spent_eur", 0.0)
    STATE.setdefault("team_spent_eur", 0.0)
    STATE.setdefault("props_spent_eur", 0.0)
    STATE.setdefault("team_bets_sent", 0)
    STATE.setdefault("prop_bets_sent", 0)
    STATE.setdefault("props_scan_done", False)
    STATE.setdefault("last_regions_team", "")
    STATE.setdefault("last_regions_props", "")

def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)

# =========================
# HELPERS
# =========================
def post_discord(webhook, title, description):
    if not webhook:
        return
    payload = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=payload, timeout=15)
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

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def is_fr_book(book_title: str) -> bool:
    if not book_title:
        return False
    return any(k.lower() in book_title.lower() for k in FR_BOOK_KEYWORDS)

def now_window_utc():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=PAST_GRACE_HOURS)
    end = now + timedelta(hours=LOOKAHEAD_HOURS)
    return start, end

def in_window(iso_dt: str, start_utc: datetime, end_utc: datetime) -> bool:
    dt = dtparser.isoparse(iso_dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start_utc <= dt <= end_utc

# =========================
# ODDS API CALLS
# =========================
def fetch_odds_for_markets(markets: str, region_candidates):
    """
    /sports/{sport_key}/odds
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/odds"
    last_err = None

    for reg in region_candidates:
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": reg,
            "markets": markets,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 422:
                last_err = RuntimeError(f"422 regions={reg}: {r.text[:200]}")
                continue
            r.raise_for_status()
            data = r.json()
            return reg, data
        except Exception as e:
            last_err = e

    raise RuntimeError(f"All regions failed for markets={markets}. Last={last_err}")

def fetch_events(region_hint="us"):
    """
    /sports/{sport_key}/events
    (pas besoin de regions ici)
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events"
    params = {"apiKey": ODDS_API_KEY, "dateFormat": DATE_FORMAT}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_event_odds(event_id: str, markets: str, region_candidates):
    """
    /sports/{sport_key}/events/{eventId}/odds  (required for props)
    """
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{event_id}/odds"
    last_err = None

    for reg in region_candidates:
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": reg,
            "markets": markets,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 422:
                last_err = RuntimeError(f"422 regions={reg}: {r.text[:200]}")
                continue
            r.raise_for_status()
            return reg, r.json()
        except Exception as e:
            last_err = e

    raise RuntimeError(f"All regions failed on event odds. Last={last_err}")

# =========================
# BALLDONTLIE INJURIES (OPTIONAL)
# =========================
def fetch_injuries_balldontlie():
    """
    GET https://api.balldontlie.io/v1/player_injuries
    Header: Authorization: YOUR_API_KEY  (pas Bearer)
    """
    if not BALLDONTLIE_API_KEY:
        return {}

    url = "https://api.balldontlie.io/v1/player_injuries"
    headers = {"Authorization": BALLDONTLIE_API_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=25)
        if r.status_code in (401, 403):
            return {"_error": f"injuries unauthorized ({r.status_code})"}
        r.raise_for_status()
        data = r.json()
        injuries = data.get("data", [])
        # count by team abbreviation if present
        by_team = {}
        for it in injuries:
            team = it.get("team") or {}
            abbr = team.get("abbreviation")
            status = (it.get("status") or "").lower()
            if not abbr:
                continue
            # count only meaningful statuses
            if status in ("out", "doubtful", "questionable", "probable") or status:
                by_team[abbr] = by_team.get(abbr, 0) + 1
        return {"by_team": by_team, "count": len(injuries)}
    except Exception as e:
        return {"_error": str(e)}

# =========================
# MARKET PARSING
# =========================
def collect_market_entries(bookmakers, market_key):
    out = []
    for b in bookmakers:
        book = b.get("title", "UnknownBook")
        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                name = o.get("name")
                price = safe_float(o.get("price"))
                point = o.get("point")
                desc = o.get("description")  # props: player
                if name is None or price is None:
                    continue
                out.append({
                    "name": name,
                    "price": price,
                    "point": point,
                    "description": desc,
                    "book": book,
                    "is_fr": is_fr_book(book),
                })
    return out

def add_reject(stats, reason: str):
    stats["reject_reasons"][reason] = stats["reject_reasons"].get(reason, 0) + 1

def add_near_miss(stats, item: dict):
    stats["near_misses"].append(item)

# =========================
# TEAM CANDIDATES
# =========================
def best_vs_median(entries):
    odds_list = [e["price"] for e in entries]
    if not odds_list:
        return None
    med = median(odds_list)
    best_all = max(entries, key=lambda x: x["price"])
    fr_entries = [e for e in entries if e.get("is_fr")]
    best_fr = max(fr_entries, key=lambda x: x["price"]) if fr_entries else None
    return med, best_all, best_fr, len(odds_list)

def candidate_moneyline(game, stats):
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "ML: aucune cote bookmaker")
        return None

    entries = collect_market_entries(bookmakers, "h2h")
    groups = {}
    for e in entries:
        groups.setdefault(e["name"], []).append(e)

    best_cand = None
    for team_name, ents in groups.items():
        if len(ents) < TEAM_MIN_BOOKS:
            add_reject(stats, f"ML: pas assez de books (>= {TEAM_MIN_BOOKS})")
            continue

        stats["markets_tested"] += 1
        res = best_vs_median(ents)
        if not res:
            continue
        med, best_all, best_fr, nbooks = res
        dev = (best_all["price"] - med) / med if med else 0.0
        edge = implied_prob(med) - implied_prob(best_all["price"]) if med else 0.0

        add_near_miss(stats, {
            "match": f"{away} @ {home}",
            "market": "MONEYLINE",
            "selection": team_name,
            "odds": best_all["price"],
            "book": best_all["book"],
            "edge": edge,
            "dev": dev
        })

        if edge < TEAM_EDGE_THRESHOLD or dev < TEAM_DEV_THRESHOLD:
            continue

        cand = {
            "match": f"{away} @ {home}",
            "market": "MONEYLINE",
            "selection": team_name,
            "line": None,
            "odds_best": best_all["price"],
            "book_best": best_all["book"],
            "odds_fr": (best_fr["price"] if best_fr else None),
            "book_fr": (best_fr["book"] if best_fr else None),
            "median_odds": med,
            "books_used": nbooks,
            "edge": edge,
            "dev": dev,
        }
        if best_cand is None or (cand["edge"], cand["dev"]) > (best_cand["edge"], best_cand["dev"]):
            best_cand = cand

    return best_cand

def candidate_spreads_or_totals(game, stats, market_key, market_label):
    home = game["home_team"]
    away = game["away_team"]
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, f"{market_label}: aucune cote bookmaker")
        return None

    entries = collect_market_entries(bookmakers, market_key)
    if not entries:
        add_reject(stats, f"{market_label}: pas de données")
        return None

    groups = {}
    for e in entries:
        groups.setdefault((e["name"], e["point"]), []).append(e)

    best_cand = None
    for (name, point), ents in groups.items():
        if len(ents) < TEAM_MIN_BOOKS:
            add_reject(stats, f"{market_label}: pas assez de books (>= {TEAM_MIN_BOOKS})")
            continue

        stats["markets_tested"] += 1
        res = best_vs_median(ents)
        if not res:
            continue
        med, best_all, best_fr, nbooks = res
        dev = (best_all["price"] - med) / med if med else 0.0
        edge = implied_prob(med) - implied_prob(best_all["price"]) if med else 0.0

        if market_key == "totals":
            selection = f"{name} {point}"
        else:
            try:
                selection = f"{name} {float(point):+g}"
            except Exception:
                selection = f"{name} {point}"

        add_near_miss(stats, {
            "match": f"{away} @ {home}",
            "market": market_label,
            "selection": selection,
            "odds": best_all["price"],
            "book": best_all["book"],
            "edge": edge,
            "dev": dev
        })

        if edge < TEAM_EDGE_THRESHOLD or dev < TEAM_DEV_THRESHOLD:
            continue

        cand = {
            "match": f"{away} @ {home}",
            "market": market_label,
            "selection": selection,
            "line": point,
            "odds_best": best_all["price"],
            "book_best": best_all["book"],
            "odds_fr": (best_fr["price"] if best_fr else None),
            "book_fr": (best_fr["book"] if best_fr else None),
            "median_odds": med,
            "books_used": nbooks,
            "edge": edge,
            "dev": dev,
        }
        if best_cand is None or (cand["edge"], cand["dev"]) > (best_cand["edge"], best_cand["dev"]):
            best_cand = cand

    return best_cand

def build_team_candidates_for_game(game, stats):
    cands = []
    ml = candidate_moneyline(game, stats)
    if ml:
        cands.append(ml)
    tot = candidate_spreads_or_totals(game, stats, "totals", "TOTAL")
    if tot:
        cands.append(tot)
    sp = candidate_spreads_or_totals(game, stats, "spreads", "SPREAD")
    if sp:
        cands.append(sp)
    return cands

# =========================
# PROPS CANDIDATES (EVENT ODDS)
# =========================
def prop_label(key: str) -> str:
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

def build_prop_candidates_from_event_odds(event_odds_json, match_str, stats):
    """
    event_odds_json usually contains: bookmakers -> markets -> outcomes
    outcomes: name=Over/Under, point=line, description=player, price=odds
    """
    bookmakers = event_odds_json.get("bookmakers", [])
    if not bookmakers:
        add_reject(stats, "PROPS: aucune donnée bookmaker")
        return []

    all_cands = []

    for mkey in PROPS_MARKETS.split(","):
        entries = collect_market_entries(bookmakers, mkey)
        if not entries:
            continue

        groups = {}
        for e in entries:
            side = e.get("name")
            point = e.get("point")
            player = e.get("description") or ""
            if side not in ("Over", "Under"):
                continue
            if point is None or not player:
                continue
            groups.setdefault((player, mkey, side, point), []).append(e)

        for (player, mk, side, point), ents in groups.items():
            if len(ents) < PROPS_MIN_BOOKS:
                add_reject(stats, f"PROPS: pas assez de books (>= {PROPS_MIN_BOOKS})")
                continue

            stats["markets_tested"] += 1
            res = best_vs_median(ents)
            if not res:
                continue
            med, best_all, best_fr, nbooks = res
            dev = (best_all["price"] - med) / med if med else 0.0
            edge = implied_prob(med) - implied_prob(best_all["price"]) if med else 0.0

            label = prop_label(mk)
            selection = f"{player} — {label} {side} {point}"

            add_near_miss(stats, {
                "match": match_str,
                "market": f"PROP {label}",
                "selection": selection,
                "odds": best_all["price"],
                "book": best_all["book"],
                "edge": edge,
                "dev": dev
            })

            if edge < PROPS_EDGE_THRESHOLD or dev < PROPS_DEV_THRESHOLD:
                continue

            all_cands.append({
                "match": match_str,
                "market": f"PROP {label}",
                "selection": selection,
                "player": player,
                "line": point,
                "side": side,
                "odds_best": best_all["price"],
                "book_best": best_all["book"],
                "odds_fr": (best_fr["price"] if best_fr else None),
                "book_fr": (best_fr["book"] if best_fr else None),
                "median_odds": med,
                "books_used": nbooks,
                "edge": edge,
                "dev": dev,
            })

    return all_cands

# =========================
# STAKES
# =========================
def allocate_stakes(num_bets, budget_amount):
    if num_bets <= 0:
        return []
    if num_bets == 1:
        splits = [1.0]
    elif num_bets == 2:
        splits = [0.6, 0.4]
    else:
        splits = [0.4, 0.35, 0.25]

    stakes = [round(budget_amount * s, 2) for s in splits[:num_bets]]
    while sum(stakes) - budget_amount > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)
    return stakes

def fmt_best_odds_line(pick: dict) -> str:
    # show best overall + FR best if different
    best = f"{pick['odds_best']:.2f} ({pick['book_best']})"
    fr = pick.get("odds_fr")
    frb = pick.get("book_fr")
    if fr is not None and frb and (fr != pick["odds_best"] or frb != pick["book_best"]):
        best += f"\n**FR best:** {fr:.2f} ({frb})"
    return best

# =========================
# MAIN
# =========================
def main():
    start_utc, end_utc = now_window_utc()

    # ---- Injuries optional ----
    injuries = fetch_injuries_balldontlie()
    injuries_note = ""
    if injuries.get("_error"):
        injuries_note = f"Injuries: non dispo ({injuries['_error']})"
    elif injuries.get("by_team"):
        injuries_note = f"Injuries: OK ({injuries.get('count', 0)} entrées)"

    # ---- TEAM odds ----
    region_team, games = fetch_odds_for_markets(TEAM_MARKETS, REGION_CANDIDATES_TEAM)
    STATE["last_regions_team"] = region_team

    stats_team = {
        "games_fetched": len(games),
        "games_in_window": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": [],
        "regions_used": region_team,
    }

    team_candidates = []
    for g in games:
        ct = g.get("commence_time")
        if not ct:
            continue
        if not in_window(ct, start_utc, end_utc):
            continue
        stats_team["games_in_window"] += 1
        team_candidates.extend(build_team_candidates_for_game(g, stats_team))

    # ---- Select TEAM picks ----
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))
    daily_team_budget = DAILY_BUDGET * TEAM_BUDGET_SHARE
    remaining_team_budget = max(0.0, daily_team_budget - float(STATE["team_spent_eur"]))

    team_picks = []
    if remaining_team_slots > 0 and remaining_team_budget > 0 and team_candidates:
        team_candidates.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)

        used_matches = set()
        ml_count = 0

        # Pass 1: respect ML cap + 1 pick per match
        for cand in team_candidates:
            if len(team_picks) >= remaining_team_slots:
                break
            if MAX_1_PICK_PER_MATCH and cand["match"] in used_matches:
                continue
            is_ml = (cand["market"] == "MONEYLINE")
            if is_ml and ml_count >= MAX_ML_IN_PORTFOLIO and remaining_team_slots >= 3:
                continue

            team_picks.append(cand)
            used_matches.add(cand["match"])
            if is_ml:
                ml_count += 1

        # Pass 2: if not enough, allow ML
        if len(team_picks) < remaining_team_slots:
            for cand in team_candidates:
                if len(team_picks) >= remaining_team_slots:
                    break
                if MAX_1_PICK_PER_MATCH and cand["match"] in used_matches:
                    continue
                if cand in team_picks:
                    continue
                team_picks.append(cand)
                used_matches.add(cand["match"])

    # ---- PROPS ----
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))
    daily_props_budget = DAILY_BUDGET * PROPS_BUDGET_SHARE
    remaining_props_budget = max(0.0, daily_props_budget - float(STATE.get("props_spent_eur", 0.0)))

    stats_props = {
        "events_in_window": 0,
        "events_scanned": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": [],
        "regions_used": "",
    }

    prop_picks = []
    props_possible = True

    # IMPORTANT: scan props only once per day to save credits
    if remaining_props_slots > 0 and remaining_props_budget > 0 and (not STATE.get("props_scan_done", False)):
        try:
            events = fetch_events()
            # filter events in window
            events_in_window = []
            for ev in events:
                ct = ev.get("commence_time")
                if not ct:
                    continue
                if in_window(ct, start_utc, end_utc):
                    events_in_window.append(ev)

            stats_props["events_in_window"] = len(events_in_window)

            # scan at most N events to control credits
            MAX_EVENTS_SCAN = 8
            events_in_window = events_in_window[:MAX_EVENTS_SCAN]

            used_players = set()
            used_matches_props = set()

            all_prop_candidates = []

            for ev in events_in_window:
                ev_id = ev.get("id")
                home = ev.get("home_team")
                away = ev.get("away_team")
                match_str = f"{away} @ {home}"

                if not ev_id:
                    continue

                # fetch event odds (props)
                region_props, ev_odds = fetch_event_odds(ev_id, PROPS_MARKETS, REGION_CANDIDATES_PROPS)
                stats_props["regions_used"] = region_props
                STATE["last_regions_props"] = region_props
                stats_props["events_scanned"] += 1

                cands = build_prop_candidates_from_event_odds(ev_odds, match_str, stats_props)
                all_prop_candidates.extend(cands)

                # tiny sleep to be polite + reduce burst
                time.sleep(0.2)

            # rank candidates
            all_prop_candidates.sort(key=lambda x: (x["edge"], x["dev"]), reverse=True)

            for cand in all_prop_candidates:
                if len(prop_picks) >= remaining_props_slots:
                    break
                player_key = (cand.get("player") or "").strip().lower()
                if not player_key:
                    continue
                if player_key in used_players:
                    continue
                if MAX_1_PICK_PER_MATCH and cand["match"] in used_matches_props:
                    continue

                prop_picks.append(cand)
                used_players.add(player_key)
                used_matches_props.add(cand["match"])

            STATE["props_scan_done"] = True

        except Exception as e:
            props_possible = False
            add_reject(stats_props, f"PROPS fetch failed: {e}")
            STATE["props_scan_done"] = True  # avoid retrying forever

    # ---- NO BET / LOGS ----
    if not team_picks:
        if MAX_NO_BET_LOGS > 0:
            reason = []
            if remaining_team_slots == 0:
                reason.append("limite TEAM bets/jour atteinte")
            if remaining_team_budget <= 0:
                reason.append("budget TEAM épuisé")
            if stats_team["games_in_window"] == 0:
                reason.append(f"0 match dans fenêtre {LOOKAHEAD_HOURS}h (filtre temps)")
            if not team_candidates:
                reason.append(f"aucune value TEAM (edge≥{TEAM_EDGE_THRESHOLD*100:.1f}% & dev≥{TEAM_DEV_THRESHOLD*100:.0f}%)")

            desc = (
                f"**Aucun bet TEAM.**\n"
                f"Raison: {', '.join(reason)}\n\n"
                f"**Résumé (TEAM)**\n"
                f"- Regions: **{stats_team['regions_used']}**\n"
                f"- Games fetched: **{stats_team['games_fetched']}**\n"
                f"- Games window: **{stats_team['games_in_window']}**\n"
                f"- Marchés testés (>= {TEAM_MIN_BOOKS} books): **{stats_team['markets_tested']}**\n"
                f"- {injuries_note}\n\n"
                f"Budget jour: **{DAILY_BUDGET:.2f}€** | Déjà utilisé: **{STATE['daily_spent_eur']:.2f}€**"
            )
            post_discord(LOG_WEBHOOK, "❌ NO BET (TEAM)", desc)

    # ---- SEND TEAM ----
    if team_picks:
        stakes = allocate_stakes(len(team_picks), min(remaining_team_budget, max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))))

        for pick, stake in zip(team_picks, stakes):
            if stake <= 0:
                continue

            pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0

            msg = (
                f"**Match:** {pick['match']}\n"
                f"**Marché:** {pick['market']}\n"
                + (f"**Line:** {pick['line']}\n" if pick.get("line") is not None else "")
                + f"**Sélection:** {pick['selection']}\n"
                f"**Best:** {fmt_best_odds_line(pick)}\n"
                f"**Books utilisés (médiane):** {pick['books_used']} | **Cote médiane:** {pick['median_odds']:.2f}\n"
                f"**Mise (budget jour 10%):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
                f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
                f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
                f"_Diversification: max {MAX_ML_IN_PORTFOLIO} ML si possible. 1 pick/match: {MAX_1_PICK_PER_MATCH}._"
            )
            post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

            STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
            STATE["team_spent_eur"] = float(STATE["team_spent_eur"]) + float(stake)
            STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    # ---- SEND PROPS ----
    if PROPS_WEBHOOK:
        if not prop_picks:
            why = []
            if remaining_props_slots == 0:
                why.append("limite props/jour atteinte")
            if remaining_props_budget <= 0:
                why.append("budget props épuisé")
            if not props_possible:
                why.append("props indisponibles (plan/endpoint)")
            if STATE.get("props_scan_done", False) and (not prop_picks):
                why.append(f"aucune value props (edge≥{PROPS_EDGE_THRESHOLD*100:.1f}% & dev≥{PROPS_DEV_THRESHOLD*100:.0f}%)")

            post_discord(
                PROPS_WEBHOOK,
                "ℹ️ NBA PLAYER PROPS",
                f"Pas de props envoyés.\nRaison: {', '.join(why) if why else '—'}\n"
                f"Events window: {stats_props.get('events_in_window', 0)} | scanned: {stats_props.get('events_scanned', 0)} | regions: {stats_props.get('regions_used','-')}"
            )
        else:
            stakes = allocate_stakes(len(prop_picks), min(remaining_props_budget, max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))))

            for pick, stake in zip(prop_picks, stakes):
                if stake <= 0:
                    continue
                pct_bk = (stake / BANKROLL) * 100 if BANKROLL > 0 else 0.0

                msg = (
                    f"**Match:** {pick['match']}\n"
                    f"**Marché:** {pick['market']}\n"
                    f"**Sélection:** {pick['selection']}\n"
                    f"**Best:** {fmt_best_odds_line(pick)}\n"
                    f"**Books utilisés (médiane):** {pick['books_used']} | **Cote médiane:** {pick['median_odds']:.2f}\n"
                    f"**Mise (budget jour 10%):** {pct_bk:.2f}% BK ({stake:.2f}€)\n"
                    f"**Edge proxy:** {pick['edge']*100:.2f}% | **Dev vs médiane:** {pick['dev']*100:.2f}%\n"
                    f"**Budget jour:** {DAILY_BUDGET:.2f}€ | **Utilisé après bet:** {(STATE['daily_spent_eur'] + stake):.2f}€\n"
                    f"_Props: 1 pick par joueur & 1 pick par match (si possible)._"
                )
                post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROP", msg)

                STATE["daily_spent_eur"] = float(STATE["daily_spent_eur"]) + float(stake)
                STATE["props_spent_eur"] = float(STATE["props_spent_eur"]) + float(stake)
                STATE["prop_bets_sent"] = int(STATE.get("prop_bets_sent", 0)) + 1

    save_state()

if __name__ == "__main__":
    main()
