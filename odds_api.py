import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"
ODDS_ENDPOINT = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/odds"

ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"


def fetch_odds_with_fallback(
    markets: str,
    regions_priority: Optional[List[str]] = None,
    timeout_s: int = 25,
    retries: int = 2,
    sleep_base_s: float = 1.25,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY missing (GitHub Secret).")

    if regions_priority is None:
        regions_priority = ["fr", "eu", "uk", "us", "us2", "au"]

    attempted = []
    errors = []

    for region in regions_priority:
        attempted.append(region)

        params = {
            "apiKey": ODDS_API_KEY,
            "regions": region,
            "markets": markets,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }

        last_exc: Optional[Exception] = None

        for attempt in range(1, retries + 2):
            try:
                r = requests.get(ODDS_ENDPOINT, params=params, timeout=timeout_s)

                if r.status_code == 422:
                    errors.append({"region": region, "status": 422, "body": (r.text or "")[:300], "markets": markets})
                    last_exc = RuntimeError(f"422 for regions={region}")
                    break

                r.raise_for_status()
                data = r.json()
                return data, {
                    "chosen_region": region,
                    "attempted_regions": attempted,
                    "errors": errors,
                    "markets": markets,
                    "notes": "success",
                }

            except Exception as e:
                last_exc = e
                if attempt <= retries:
                    time.sleep(sleep_base_s * attempt)
                else:
                    errors.append(
                        {
                            "region": region,
                            "status": getattr(getattr(e, "response", None), "status_code", None),
                            "body": str(e)[:300],
                            "markets": markets,
                        }
                    )

        _ = last_exc

    return [], {
        "chosen_region": None,
        "attempted_regions": attempted,
        "errors": errors,
        "markets": markets,
        "notes": "all regions failed",
    }
3) formatting.py (COLLE TOUT ET REMPLACE TOUT)
from typing import Any, Dict, List


def pct(x: float) -> str:
    return f"{x*100:.2f}%"


def eur(x: float) -> str:
    return f"{x:.2f}€"


def tier(score: float) -> str:
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B+"
    if score >= 60:
        return "B"
    return "C"


def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    best_flag = "✅ FR book" if p.get("best_is_fr") else "⚠️ best non-FR (FR dispo moins bon)"
    fr_best_line = ""
    if p.get("fr_best") is not None and p.get("fr_best_book") is not None:
        fr_best_line = f"\nFR best: **{p['fr_best']:.2f}** ({p['fr_best_book']})"

    line_line = f"\nLine: **{p['line']}**" if p.get("line") is not None else ""
    bk_pct = (stake / bankroll) * 100.0 if bankroll > 0 else 0.0

    return (
        f"**Match:** {p['match']}\n"
        f"**Marché:** {p['market']}{line_line}\n"
        f"**Sélection:** {p['selection']}\n"
        f"**Best:** **{p['odds']:.2f}** ({p['book']}) — {best_flag}{fr_best_line}\n"
        f"**Books utilisés (total):** {p.get('books_used','?')} | **Cote médiane (sélection):** {p.get('median_odds',0):.2f}\n"
        f"**Fair p (no-vig):** {pct(p.get('fair_prob',0))} | **Implied(best):** {pct(1.0/p['odds'])}\n"
        f"**Edge réel:** **{pct(p.get('edge',0))}** | **Dev vs médiane:** {pct(p.get('dev',0))}\n"
        f"**Bet Quality:** **{p.get('score',0):.0f}/100 ({tier(p.get('score',0))})**\n"
        f"**Mise (budget jour):** {bk_pct:.2f}% BK ({eur(stake)})\n"
        f"**Budget jour:** {eur(daily_budget)} | **Utilisé après bet:** {eur(spent_after)}\n"
        f"_Diversification: max 2 ML si possible · 1 pick/match._"
    )


def format_prop_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    best_flag = "✅ FR book" if p.get("best_is_fr") else "⚠️ best non-FR (FR dispo moins bon)"
    fr_best_line = ""
    if p.get("fr_best") is not None and p.get("fr_best_book") is not None:
        fr_best_line = f"\nFR best: **{p['fr_best']:.2f}** ({p['fr_best_book']})"

    bk_pct = (stake / bankroll) * 100.0 if bankroll > 0 else 0.0

    return (
        f"**Match:** {p['match']}\n"
        f"**Marché:** {p['market']}\n"
        f"**Sélection:** {p['player']} — {p['selection']}\n"
        f"**Best:** **{p['odds']:.2f}** ({p['book']}) — {best_flag}{fr_best_line}\n"
        f"**Books utilisés (total):** {p.get('books_used','?')} | **Cote médiane (sélection):** {p.get('median_odds',0):.2f}\n"
        f"**Fair p (no-vig):** {pct(p.get('fair_prob',0))} | **Implied(best):** {pct(1.0/p['odds'])}\n"
        f"**Edge réel:** **{pct(p.get('edge',0))}** | **Dev vs médiane:** {pct(p.get('dev',0))}\n"
        f"**Bet Quality:** **{p.get('score',0):.0f}/100 ({tier(p.get('score',0))})**\n"
        f"**Mise (budget jour):** {bk_pct:.2f}% BK ({eur(stake)})\n"
        f"**Budget jour:** {eur(daily_budget)} | **Utilisé après bet:** {eur(spent_after)}\n"
        f"_Props: 1 pick/joueur · 1 pick/match (si possible)._"
    )


def format_no_bet(
    title: str,
    reason: str,
    regions_used: List[str],
    games_analyzed: int,
    markets_tested: int,
    daily_budget: float,
    daily_spent: float,
) -> str:
    return (
        f"**{title}**\n"
        f"Raison: {reason}\n\n"
        f"**Résumé analyse**\n"
        f"- Regions utilisées: **{','.join(regions_used) if regions_used else 'n/a'}**\n"
        f"- Matchs analysés: **{games_analyzed}**\n"
        f"- Marchés testés (2-way & >=2 books total): **{markets_tested}**\n\n"
        f"Budget jour: **{daily_budget:.2f}€** | Déjà utilisé: **{daily_spent:.2f}€**"
    )
4) main.py (COLLE TOUT ET REMPLACE TOUT)

👉 Cette version :

analyse TEAM (ML/Spread/Total),

analyse PROPS (PTS/REB/AST/3PT/PRA/PR/PA/RA) si dispo,

corrige le filtre date (UTC today+tomorrow),

budget dynamique : si pas de team → props prennent tout, et inversement.

import os
import json
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from dateutil import parser

from odds_api import fetch_odds_with_fallback
from engine import (
    is_fr_book,
    collect_market_lines,
    pick_consensus_line,
    two_way_metrics,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_fixed_splits,
)
from formatting import format_team_pick, format_prop_pick, format_no_bet


ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")


with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("state.json", "r", encoding="utf-8") as f:
    STATE = json.load(f)

BANKROLL = float(CONFIG["bankroll_eur"])
DAILY_BUDGET = BANKROLL * float(CONFIG["daily_budget_pct"])

MAX_TEAM_PER_DAY = int(CONFIG.get("max_team_bets_per_day", 3))
MAX_PROPS_PER_DAY = int(CONFIG.get("max_prop_bets_per_day", 3))

EDGE_THRESHOLD = float(CONFIG.get("edge_threshold", 0.015))
DEV_THRESHOLD = float(CONFIG.get("dev_threshold", 0.02))
MIN_BOOKS_TOTAL = int(CONFIG.get("min_bookmakers", 2))

PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", True))


TEAM_MARKETS = "h2h,spreads,totals"
PROPS_MARKETS = [
    ("PROP PTS", "player_points", "PTS"),
    ("PROP REB", "player_rebounds", "REB"),
    ("PROP AST", "player_assists", "AST"),
    ("PROP 3PT", "player_threes", "3PT"),
    ("PROP PRA", "player_points_rebounds_assists", "PRA"),
    ("PROP PR", "player_points_rebounds", "PR"),
    ("PROP PA", "player_points_assists", "PA"),
    ("PROP RA", "player_rebounds_assists", "RA"),
]


def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=15)
    r.raise_for_status()


def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)


def reset_state_if_new_day():
    today_utc = datetime.now(timezone.utc).date().isoformat()
    if STATE.get("date_utc") != today_utc:
        STATE.clear()
        STATE.update(
            {
                "date_utc": today_utc,
                "daily_spent_eur": 0.0,
                "team_bets_sent": 0,
                "prop_bets_sent": 0,
            }
        )


def is_valid_game_date(commence_time: str) -> bool:
    today = datetime.now(timezone.utc).date()
    g_date = parser.isoparse(commence_time).date()
    delta = (g_date - today).days
    return 0 <= delta <= 1  # today or tomorrow UTC


def collect_prop_player_lines(game: Dict[str, Any], market_key: str) -> Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]]:
    """
    Returns mapping:
      players[player][line_key]["Over"/"Under"] = list(entries)
    """
    players: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}
    bookmakers = game.get("bookmakers", [])

    for b in bookmakers:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []):
                name = o.get("name")
                desc = o.get("description") or o.get("player") or o.get("participant")
                price = o.get("price")
                point = o.get("point")

                if name is None or desc is None or price is None:
                    continue

                try:
                    price = float(price)
                except Exception:
                    continue

                try:
                    point_f = float(point) if point is not None else None
                except Exception:
                    point_f = None

                # detect Over/Under and player name
                ou = None
                player = None

                if str(name).lower() in ["over", "under"]:
                    ou = str(name).title()
                    player = str(desc)
                elif str(desc).lower() in ["over", "under"]:
                    ou = str(desc).title()
                    player = str(name)
                else:
                    continue

                line_key = f"{point_f}" if point_f is not None else "NA"
                players.setdefault(player, {}).setdefault(line_key, {}).setdefault(ou, []).append(
                    {"price": price, "book": book, "is_fr": fr, "point": point_f}
                )

    return players


def main():
    reset_state_if_new_day()

    remaining_total = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))
    prop_slots = max(0, MAX_PROPS_PER_DAY - int(STATE["prop_bets_sent"]))

    # ---------------- TEAM ----------------
    team_games, team_meta = fetch_odds_with_fallback(markets=TEAM_MARKETS)
    team_region = team_meta.get("chosen_region") or "n/a"

    team_candidates: List[Dict[str, Any]] = []
    team_games_analyzed = 0
    team_markets_tested = 0

    for g in team_games:
        if not is_valid_game_date(g["commence_time"]):
            continue

        team_games_analyzed += 1
        home = g["home_team"]
        away = g["away_team"]
        match = f"{away} @ {home}"
        bookmakers = g.get("bookmakers", [])
        if not bookmakers:
            continue

        # MONEYLINE
        h2h = collect_market_lines(bookmakers, "h2h")["lines"]
        lk = pick_consensus_line(h2h)
        if lk and lk in h2h:
            outs = list(h2h[lk].keys())
            if len(outs) >= 2:
                met = two_way_metrics(
                    match=match,
                    market_label="MONEYLINE",
                    line=None,
                    selection_a=outs[0],
                    selection_b=outs[1],
                    entries_a=h2h[lk][outs[0]],
                    entries_b=h2h[lk][outs[1]],
                    prefer_fr=PREFER_FR_BOOKS,
                    min_books_total=MIN_BOOKS_TOTAL,
                )
                if met:
                    team_markets_tested += 1
                    a, b = met
                    if a["edge"] >= EDGE_THRESHOLD and a["dev"] >= DEV_THRESHOLD:
                        team_candidates.append(a)
                    if b["edge"] >= EDGE_THRESHOLD and b["dev"] >= DEV_THRESHOLD:
                        team_candidates.append(b)

        # TOTAL
        tots = collect_market_lines(bookmakers, "totals")["lines"]
        lk = pick_consensus_line(tots)
        if lk and lk in tots and "Over" in tots[lk] and "Under" in tots[lk]:
            line = float(lk)
            met = two_way_metrics(
                match=match,
                market_label="TOTAL",
                line=line,
                selection_a=f"Over {line}",
                selection_b=f"Under {line}",
                entries_a=tots[lk]["Over"],
                entries_b=tots[lk]["Under"],
                prefer_fr=PREFER_FR_BOOKS,
                min_books_total=MIN_BOOKS_TOTAL,
            )
            if met:
                team_markets_tested += 1
                a, b = met
                if a["edge"] >= EDGE_THRESHOLD and a["dev"] >= DEV_THRESHOLD:
                    team_candidates.append(a)
                if b["edge"] >= EDGE_THRESHOLD and b["dev"] >= DEV_THRESHOLD:
                    team_candidates.append(b)

        # SPREAD
        spr = collect_market_lines(bookmakers, "spreads")["lines"]
        lk = pick_consensus_line(spr)
        if lk and lk in spr and home in spr[lk] and away in spr[lk]:
            # choose signed points via first entry (ok for display)
            home_pt = spr[lk][home][0].get("point")
            away_pt = spr[lk][away][0].get("point")
            line_abs = float(lk)

            met = two_way_metrics(
                match=match,
                market_label="SPREAD",
                line=line_abs,
                selection_a=f"{home} {home_pt:+}" if home_pt is not None else f"{home} {-line_abs:+}",
                selection_b=f"{away} {away_pt:+}" if away_pt is not None else f"{away} {line_abs:+}",
                entries_a=spr[lk][home],
                entries_b=spr[lk][away],
                prefer_fr=PREFER_FR_BOOKS,
                min_books_total=MIN_BOOKS_TOTAL,
            )
            if met:
                team_markets_tested += 1
                a, b = met
                if a["edge"] >= EDGE_THRESHOLD and a["dev"] >= DEV_THRESHOLD:
                    team_candidates.append(a)
                if b["edge"] >= EDGE_THRESHOLD and b["dev"] >= DEV_THRESHOLD:
                    team_candidates.append(b)

    team_picks = diversify_team_picks(team_candidates, max_picks=min(3, team_slots), max_ml=2, one_pick_per_match=True)

    # ---------------- PROPS ----------------
    prop_candidates: List[Dict[str, Any]] = []
    props_games_analyzed = 0
    props_markets_tested = 0

    if prop_slots > 0 and remaining_total > 0:
        props_keys_joined = ",".join([k for _, k, _ in PROPS_MARKETS])
        prop_games, prop_meta = fetch_odds_with_fallback(markets=props_keys_joined)
        # fallback market-by-market if empty
        if not prop_games:
            for label, key, short in PROPS_MARKETS:
                g2, _ = fetch_odds_with_fallback(markets=key)
                prop_games.extend(g2)

        for g in prop_games:
            if not is_valid_game_date(g["commence_time"]):
                continue

            props_games_analyzed += 1
            home = g["home_team"]
            away = g["away_team"]
            match = f"{away} @ {home}"

            for label, key, short in PROPS_MARKETS:
                players = collect_prop_player_lines(g, key)

                # limit computation: keep top 12 combos by support
                combos: List[Tuple[str, str, int]] = []
                for pl, lines in players.items():
                    for lk, ous in lines.items():
                        cnt = sum(len(v) for v in ous.values())
                        combos.append((pl, lk, cnt))
                combos.sort(key=lambda x: x[2], reverse=True)
                combos = combos[:12]

                for pl, lk, _ in combos:
                    ous = players[pl][lk]
                    if "Over" not in ous or "Under" not in ous:
                        continue
                    line = float(lk) if lk != "NA" else None

                    met = two_way_metrics(
                        match=match,
                        market_label=label,
                        line=line,
                        selection_a=f"{short} Over {line}",
                        selection_b=f"{short} Under {line}",
                        entries_a=ous["Over"],
                        entries_b=ous["Under"],
                        prefer_fr=PREFER_FR_BOOKS,
                        min_books_total=MIN_BOOKS_TOTAL,
                    )
                    if not met:
                        continue

                    props_markets_tested += 1
                    a, b = met
                    a["player"] = pl
                    b["player"] = pl

                    if a["edge"] >= EDGE_THRESHOLD and a["dev"] >= DEV_THRESHOLD:
                        prop_candidates.append(a)
                    if b["edge"] >= EDGE_THRESHOLD and b["dev"] >= DEV_THRESHOLD:
                        prop_candidates.append(b)

    prop_picks = diversify_prop_picks(prop_candidates, max_picks=min(3, prop_slots), one_pick_per_match=True, one_pick_per_player=True)

    # ---------------- BUDGET SPLIT DYNAMIC ----------------
    # If only team picks exist -> 100% team
    # If only props picks exist -> 100% props
    if team_picks and not prop_picks:
        team_budget = remaining_total
        props_budget = 0.0
    elif prop_picks and not team_picks:
        team_budget = 0.0
        props_budget = remaining_total
    else:
        team_budget = remaining_total * 0.60
        props_budget = remaining_total * 0.40

    # ---------------- SEND / LOG ----------------
    if not team_picks:
        desc = format_no_bet(
            title="❌ NO BET (TEAM)",
            reason=f"aucune value TEAM (edge>={EDGE_THRESHOLD*100:.1f}% & dev>={DEV_THRESHOLD*100:.0f}%)",
            regions_used=[team_region],
            games_analyzed=team_games_analyzed,
            markets_tested=team_markets_tested,
            daily_budget=DAILY_BUDGET,
            daily_spent=float(STATE["daily_spent_eur"]),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)

    if not prop_picks and PROPS_WEBHOOK:
        post_discord(PROPS_WEBHOOK, "ℹ️ Player Props", "Pas de props envoyés (pas de value ou budget/slots).")

    # TEAM posts
    if team_picks and TEAM_WEBHOOK and team_budget > 0 and team_slots > 0:
        stakes = allocate_stakes_fixed_splits(team_budget, len(team_picks))
        for p, stake in zip(team_picks, stakes):
            spent_after = float(STATE["daily_spent_eur"]) + float(stake)
            msg = format_team_pick(p, stake, BANKROLL, DAILY_BUDGET, spent_after)
            post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)
            STATE["daily_spent_eur"] = spent_after
            STATE["team_bets_sent"] += 1

    # PROPS posts
    if prop_picks and PROPS_WEBHOOK and props_budget > 0 and prop_slots > 0:
        stakes = allocate_stakes_fixed_splits(props_budget, len(prop_picks))
        for p, stake in zip(prop_picks, stakes):
            spent_after = float(STATE["daily_spent_eur"]) + float(stake)
            msg = format_prop_pick(p, stake, BANKROLL, DAILY_BUDGET, spent_after)
            post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROP", msg)
            STATE["daily_spent_eur"] = spent_after
            STATE["prop_bets_sent"] += 1

    save_state()


if __name__ == "__main__":
    main()
