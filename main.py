import os
import json
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from dateutil import parser

from odds_api import fetch_odds_with_fallback
from engine import (
    collect_market_lines,
    pick_consensus_line,
    analyze_two_way_market,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_fixed_splits,
    median,
)
from formatting import format_team_pick, format_prop_pick, format_no_bet


# ----------------------------
# ENV
# ----------------------------
TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

# ----------------------------
# CONFIG / STATE
# ----------------------------
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("state.json", "r", encoding="utf-8") as f:
    STATE = json.load(f)

BANKROLL = float(CONFIG["bankroll_eur"])
DAILY_BUDGET = BANKROLL * float(CONFIG["daily_budget_pct"])

MAX_TEAM_PER_DAY = int(CONFIG.get("max_team_bets_per_day", 3))
MAX_PROPS_PER_DAY = int(CONFIG.get("max_prop_bets_per_day", 3))

EDGE_THRESHOLD = float(CONFIG.get("edge_threshold", 0.015))  # 1.5% default
DEV_THRESHOLD = float(CONFIG.get("dev_threshold", 0.02))     # 2% default
MIN_BOOKMAKERS = int(CONFIG.get("min_bookmakers", 2))        # >=2 default

TEAM_BUDGET_SHARE = float(CONFIG.get("team_budget_share", 0.60))
PROPS_BUDGET_SHARE = float(CONFIG.get("props_budget_share", 0.40))

PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", True))
MAX_NO_BET_LOGS = 1


def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)


def post_discord(webhook: Optional[str], title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=15)
    r.raise_for_status()


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


# ----------------------------
# TEAM MARKETS
# ----------------------------
TEAM_MARKETS = "h2h,spreads,totals"

# ----------------------------
# PROPS MARKETS (Odds API keys)
# ----------------------------
# IMPORTANT:
# Selon ton plan OddsAPI, certains markets props peuvent être refusés (422).
# On gère ça proprement: si refus -> on log et props = off.
PROPS_MARKETS_LIST = [
    ("PROP PTS", "player_points"),
    ("PROP REB", "player_rebounds"),
    ("PROP AST", "player_assists"),
    ("PROP 3PT", "player_threes"),
    ("PROP PRA", "player_points_rebounds_assists"),
    ("PROP PR", "player_points_rebounds"),
    ("PROP PA", "player_points_assists"),
    ("PROP RA", "player_rebounds_assists"),
]


def top_rejects_from_counter(counter: Dict[str, int], top_n: int = 6) -> List[str]:
    if not counter:
        return []
    items = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [f"{k}: {v}" for k, v in items]


def make_near_miss_lines(near: List[Dict[str, Any]], top_n: int = 5) -> List[str]:
    if not near:
        return []
    # keep positive edge only, sort by edge then score
    near2 = [x for x in near if x.get("edge") is not None and x["edge"] > 0]
    near2.sort(key=lambda x: (x.get("edge", 0), x.get("score", 0)), reverse=True)
    near2 = near2[:top_n]
    lines = []
    for i, x in enumerate(near2, start=1):
        sel = x.get("selection", "")
        if x.get("player"):
            sel = f"{x['player']} — {sel}"
        lines.append(
            f"{i}) {x['match']} — {x['market']} — **{sel}** @ {x['odds']:.2f} ({x['book']})\n"
            f"   Edge: **{x['edge']*100:.2f}%** | Dev: {x['dev']*100:.2f}% | Score: {x.get('score',0):.0f}/100"
        )
    return lines


def analyze_team_game(game: Dict[str, Any], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    home = game["home_team"]
    away = game["away_team"]
    match = f"{away} @ {home}"
    bookmakers = game.get("bookmakers", [])

    if not bookmakers:
        stats["reject_reasons"]["Aucune cote bookmaker"] = stats["reject_reasons"].get("Aucune cote bookmaker", 0) + 1
        return []

    candidates: List[Dict[str, Any]] = []

    # h2h
    h2h = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h)
    if lk and lk in h2h:
        outcomes = list(h2h[lk].keys())
        if len(outcomes) >= 2:
            a, b = outcomes[0], outcomes[1]
            res = analyze_two_way_market(
                match=match,
                market_label="MONEYLINE",
                line=None,
                outcome_a=a,
                outcome_b=b,
                entries_a=h2h[lk].get(a, []),
                entries_b=h2h[lk].get(b, []),
                edge_threshold=EDGE_THRESHOLD,
                dev_threshold=DEV_THRESHOLD,
                min_books=MIN_BOOKMAKERS,
                prefer_fr=PREFER_FR_BOOKS,
            )
            if res:
                stats["markets_tested"] += 1
                candidates.extend(res)
            else:
                stats["reject_reasons"]["ML: insuffisant (2-way/min books/edge/dev)"] = stats["reject_reasons"].get(
                    "ML: insuffisant (2-way/min books/edge/dev)", 0
                ) + 1

    # totals
    totals = collect_market_lines(bookmakers, "totals")["lines"]
    lk = pick_consensus_line(totals)
    if lk and lk in totals:
        outcomes = list(totals[lk].keys())
        if "Over" in outcomes and "Under" in outcomes:
            line = float(lk)
            res = analyze_two_way_market(
                match=match,
                market_label="TOTAL",
                line=line,
                outcome_a=f"Over {line}",
                outcome_b=f"Under {line}",
                entries_a=totals[lk].get("Over", []),
                entries_b=totals[lk].get("Under", []),
                edge_threshold=EDGE_THRESHOLD,
                dev_threshold=DEV_THRESHOLD,
                min_books=MIN_BOOKMAKERS,
                prefer_fr=PREFER_FR_BOOKS,
            )
            if res:
                stats["markets_tested"] += 1
                candidates.extend(res)
            else:
                stats["reject_reasons"]["TOTAL: insuffisant (2-way/min books/edge/dev)"] = stats["reject_reasons"].get(
                    "TOTAL: insuffisant (2-way/min books/edge/dev)", 0
                ) + 1

    # spreads (canonical abs line)
    spreads = collect_market_lines(bookmakers, "spreads")["lines"]
    lk = pick_consensus_line(spreads)
    if lk and lk in spreads:
        line_abs = float(lk)
        # need both teams present at that abs line (one +, one -) across books
        if home in spreads[lk] and away in spreads[lk]:
            # Note: entries store the point sign per team, we keep it in selection label
            # We'll pick canonical selection strings using the median point sign seen.
            def pick_point(entries):
                pts = [e.get("point") for e in entries if e.get("point") is not None]
                return median(pts) if pts else None

            home_pt = pick_point(spreads[lk][home])
            away_pt = pick_point(spreads[lk][away])

            # fallback if None
            home_pt = home_pt if home_pt is not None else (-line_abs)
            away_pt = away_pt if away_pt is not None else (+line_abs)

            res = analyze_two_way_market(
                match=match,
                market_label="SPREAD",
                line=line_abs,
                outcome_a=f"{home} {home_pt:+}",
                outcome_b=f"{away} {away_pt:+}",
                entries_a=spreads[lk].get(home, []),
                entries_b=spreads[lk].get(away, []),
                edge_threshold=EDGE_THRESHOLD,
                dev_threshold=DEV_THRESHOLD,
                min_books=MIN_BOOKMAKERS,
                prefer_fr=PREFER_FR_BOOKS,
            )
            if res:
                stats["markets_tested"] += 1
                # keep "line" field as signed for readability:
                for c in res:
                    # c["selection"] already includes sign
                    pass
                candidates.extend(res)
            else:
                stats["reject_reasons"]["SPREAD: insuffisant (2-way/min books/edge/dev)"] = stats["reject_reasons"].get(
                    "SPREAD: insuffisant (2-way/min books/edge/dev)", 0
                ) + 1
        else:
            stats["reject_reasons"]["SPREAD: pas 2 côtés (home+away)"] = stats["reject_reasons"].get(
                "SPREAD: pas 2 côtés (home+away)", 0
            ) + 1

    # add near-miss candidates (already filtered by thresholds in analyze_two_way_market)
    # For near-miss tracking, we keep top by score later: we can store the best per match.
    return candidates


def analyze_props_game(game: Dict[str, Any], stats: Dict[str, Any], market_label: str, market_key: str) -> List[Dict[str, Any]]:
    home = game["home_team"]
    away = game["away_team"]
    match = f"{away} @ {home}"
    bookmakers = game.get("bookmakers", [])

    if not bookmakers:
        stats["reject_reasons"]["Aucune cote bookmaker"] = stats["reject_reasons"].get("Aucune cote bookmaker", 0) + 1
        return []

    # For props, outcomes names are typically:
    # - name: "Over"/"Under"
    # - description: player name (varies by API, sometimes under "description")
    # We'll normalize: outcome_key = "Over"/"Under", and player = description or "player"
    lines_struct = collect_market_lines(bookmakers, market_key)
    lines = lines_struct["lines"]
    lk = pick_consensus_line(lines)
    if not lk or lk not in lines:
        stats["reject_reasons"][f"{market_label}: aucune line exploitable"] = stats["reject_reasons"].get(
            f"{market_label}: aucune line exploitable", 0
        ) + 1
        return []

    outcomes_map = lines[lk]

    # We need to rebuild per player: in many OddsAPI props, the "name" is player and "description" is Over/Under
    # But in other feeds it's reversed. We'll inspect entries by trying to see if keys are Over/Under.
    keys = list(outcomes_map.keys())

    # If keys contain Over/Under, it's standard
    is_over_under_keys = ("Over" in keys and "Under" in keys)

    candidates: List[Dict[str, Any]] = []

    if is_over_under_keys:
        # Here we do NOT know player name from keys; need to pull it from entry "description"
        # Our collector doesn't store description; so we must fallback to direct parsing from bookmakers.
        # => We'll do a prop-specific parse below (robust).
        return candidates

    # Robust prop parsing (directly from raw bookmakers):
    # Build: players[player][line_key][Over/Under] -> entries
    players: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}

    for b in bookmakers:
        book = b.get("title", "UnknownBook")
        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                name = o.get("name")
                price = o.get("price")
                point = o.get("point")
                desc = o.get("description") or o.get("player") or o.get("participant")

                if name is None or price is None or desc is None:
                    continue

                try:
                    price = float(price)
                except Exception:
                    continue

                try:
                    point_f = float(point) if point is not None else None
                except Exception:
                    point_f = None

                # determine OU
                ou = None
                if str(name).lower() in ["over", "under"]:
                    ou = str(name)
                else:
                    # sometimes name is player, and description is Over/Under
                    if str(desc).lower() in ["over", "under"]:
                        ou = str(desc)
                        desc = str(name)  # player
                    else:
                        continue

                line_key = f"{point_f}" if point_f is not None else "NA"
                players.setdefault(str(desc), {}).setdefault(line_key, {}).setdefault(ou, []).append(
                    {"price": price, "book": book, "is_fr": False, "point": point_f}
                )

    # choose a “best supported” (player, line) combo by entry count
    best_player = None
    best_line = None
    best_cnt = -1
    for pl, lines2 in players.items():
        for lk2, ous in lines2.items():
            cnt = sum(len(v) for v in ous.values())
            if cnt > best_cnt:
                best_cnt = cnt
                best_player = pl
                best_line = lk2

    if not best_player or not best_line:
        stats["reject_reasons"][f"{market_label}: parsing vide"] = stats["reject_reasons"].get(f"{market_label}: parsing vide", 0) + 1
        return []

    ous = players[best_player][best_line]
    if "Over" not in ous or "Under" not in ous:
        stats["reject_reasons"][f"{market_label}: pas 2 côtés"] = stats["reject_reasons"].get(f"{market_label}: pas 2 côtés", 0) + 1
        return []

    line = float(best_line) if best_line != "NA" else None

    res = analyze_two_way_market(
        match=match,
        market_label=market_label,
        line=line,
        outcome_a=f"{market_label.split()[-1]} Over {line}",
        outcome_b=f"{market_label.split()[-1]} Under {line}",
        entries_a=ous["Over"],
        entries_b=ous["Under"],
        edge_threshold=EDGE_THRESHOLD,
        dev_threshold=DEV_THRESHOLD,
        min_books=MIN_BOOKMAKERS,
        prefer_fr=PREFER_FR_BOOKS,
    )

    # attach player + improve selection strings
    for c in res:
        c["player"] = best_player
        # make selection readable:
        if "Over" in c["selection"]:
            c["selection"] = f"{market_label.split()[-1]} Over {line}"
        elif "Under" in c["selection"]:
            c["selection"] = f"{market_label.split()[-1]} Under {line}"

    if res:
        stats["markets_tested"] += 1
        candidates.extend(res)
    else:
        stats["reject_reasons"][f"{market_label}: insuffisant (2-way/min books/edge/dev)"] = stats["reject_reasons"].get(
            f"{market_label}: insuffisant (2-way/min books/edge/dev)", 0
        ) + 1

    return candidates


def main():
    reset_state_if_new_day()

    today_date = datetime.now(timezone.utc).date()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE["daily_spent_eur"]))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE["team_bets_sent"]))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE["prop_bets_sent"]))

    # Budget split
    team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
    props_budget = remaining_budget_total * PROPS_BUDGET_SHARE

    # ----------------------------
    # FETCH TEAM ODDS
    # ----------------------------
    team_games, team_meta = fetch_odds_with_fallback(markets=TEAM_MARKETS)
    regions_used = []
    if team_meta.get("chosen_region"):
        regions_used.append(team_meta["chosen_region"])

    team_stats = {
        "games_today": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": [],
    }

    team_candidates: List[Dict[str, Any]] = []

    for g in team_games:
        g_date = parser.isoparse(g["commence_time"]).date()
        if g_date != today_date:
            continue
        team_stats["games_today"] += 1
        team_candidates.extend(analyze_team_game(g, team_stats))

    # Keep near-miss from all candidates (even though they passed thresholds already, still useful)
    team_stats["near_misses"] = sorted(team_candidates, key=lambda x: (x["score"], x["edge"]), reverse=True)[:25]

    # Diversify + limit
    team_picks = diversify_team_picks(team_candidates, max_picks=min(remaining_team_slots, 3), max_ml=2, one_pick_per_match=True)

    # ----------------------------
    # FETCH PROPS ODDS
    # ----------------------------
    props_stats = {
        "games_today": 0,
        "markets_tested": 0,
        "reject_reasons": {},
        "near_misses": [],
        "notes": [],
    }

    prop_candidates: List[Dict[str, Any]] = []
    props_regions_used = []

    if remaining_props_slots > 0 and props_budget > 0:
        # Try props markets in a single request first (faster).
        # If plan rejects, we degrade gracefully per-market.
        props_markets_joined = ",".join([k for _, k in PROPS_MARKETS_LIST])

        props_games, props_meta = fetch_odds_with_fallback(markets=props_markets_joined)
        if props_meta.get("chosen_region"):
            props_regions_used.append(props_meta["chosen_region"])

        # If empty, try per market (some plans allow some props but not all)
        if not props_games:
            props_stats["notes"].append("Props: request globale vide/refusée -> fallback par market.")
            for label, key in PROPS_MARKETS_LIST:
                g2, m2 = fetch_odds_with_fallback(markets=key)
                if m2.get("chosen_region"):
                    props_regions_used.append(m2["chosen_region"])
                for gg in g2:
                    g_date = parser.isoparse(gg["commence_time"]).date()
                    if g_date != today_date:
                        continue
                    props_stats["games_today"] += 1
                    prop_candidates.extend(analyze_props_game(gg, props_stats, label, key))
        else:
            # Parse the returned games for each prop key
            for gg in props_games:
                g_date = parser.isoparse(gg["commence_time"]).date()
                if g_date != today_date:
                    continue
                props_stats["games_today"] += 1
                for label, key in PROPS_MARKETS_LIST:
                    prop_candidates.extend(analyze_props_game(gg, props_stats, label, key))

    props_stats["near_misses"] = sorted(prop_candidates, key=lambda x: (x["score"], x["edge"]), reverse=True)[:25]

    prop_picks = diversify_prop_picks(
        prop_candidates,
        max_picks=min(remaining_props_slots, 3),
        one_pick_per_match=True,
        one_pick_per_player=True,
    )

    # ----------------------------
    # NO BET LOGIC (TEAM + PROPS)
    # ----------------------------
    # Note: you asked "3 TEAM + 3 PROPS", but if no edges => NO BET in that channel.
    # Also, if budget or slots already consumed => log reason.

    # TEAM no-bet
    if (not team_picks) or remaining_team_slots <= 0 or team_budget <= 0:
        if MAX_NO_BET_LOGS > 0 and LOG_WEBHOOK:
            reason_parts = []
            if remaining_team_slots <= 0:
                reason_parts.append("limite TEAM bets/jour atteinte")
            if team_budget <= 0:
                reason_parts.append("budget TEAM (part) déjà utilisé")
            if not team_picks:
                reason_parts.append(f"aucune value TEAM (edge>={EDGE_THRESHOLD*100:.1f}% & dev>={DEV_THRESHOLD*100:.0f}%)")

            desc = format_no_bet(
                title="❌ NO BET (TEAM)",
                reason=", ".join(reason_parts) if reason_parts else "n/a",
                regions_used=regions_used,
                games_analyzed=team_stats["games_today"],
                markets_tested=team_stats["markets_tested"],
                top_rejects=top_rejects_from_counter(team_stats["reject_reasons"]),
                near_miss_lines=make_near_miss_lines(team_stats["near_misses"], top_n=5),
                daily_budget=DAILY_BUDGET,
                daily_spent=float(STATE["daily_spent_eur"]),
            )
            post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)

    # PROPS no-bet message (clean)
    if PROPS_WEBHOOK:
        if (not prop_picks) or remaining_props_slots <= 0 or props_budget <= 0:
            msg = "Pas de props envoyés (pas de value ou budget/slots)."
            post_discord(PROPS_WEBHOOK, "ℹ️ Player Props", msg)

    # If nothing to bet at all, save state and exit
    if not team_picks and not prop_picks:
        save_state()
        return

    # ----------------------------
    # SEND TEAM BETS
    # ----------------------------
    if team_picks and TEAM_WEBHOOK and remaining_team_slots > 0 and team_budget > 0:
        team_stakes = allocate_stakes_fixed_splits(team_budget, len(team_picks))
        for pick, stake in zip(team_picks, team_stakes):
            if stake <= 0:
                continue
            spent_after = float(STATE["daily_spent_eur"]) + float(stake)
            msg = format_team_pick(
                p=pick,
                stake=stake,
                bankroll=BANKROLL,
                daily_budget=DAILY_BUDGET,
                spent_after=spent_after,
            )
            post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)
            STATE["daily_spent_eur"] = spent_after
            STATE["team_bets_sent"] = int(STATE["team_bets_sent"]) + 1

    # ----------------------------
    # SEND PROPS BETS
    # ----------------------------
    if prop_picks and PROPS_WEBHOOK and remaining_props_slots > 0 and props_budget > 0:
        props_stakes = allocate_stakes_fixed_splits(props_budget, len(prop_picks))
        for pick, stake in zip(prop_picks, props_stakes):
            if stake <= 0:
                continue
            spent_after = float(STATE["daily_spent_eur"]) + float(stake)
            msg = format_prop_pick(
                p=pick,
                stake=stake,
                bankroll=BANKROLL,
                daily_budget=DAILY_BUDGET,
                spent_after=spent_after,
            )
            post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROP", msg)
            STATE["daily_spent_eur"] = spent_after
            STATE["prop_bets_sent"] = int(STATE["prop_bets_sent"]) + 1

    save_state()


if __name__ == "__main__":
    main()
