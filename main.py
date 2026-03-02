import os
import json
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List
from dateutil import parser

from odds_api import fetch_odds_with_fallback
from engine import (
    collect_market_lines,
    collect_player_prop_lines,
    pick_consensus_line,
    pick_consensus_prop_line,
    analyze_two_way_market,
    analyze_market_two_way_with_diagnostics,  # FIX 2
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_fixed_splits,
)

from formatting import format_team_pick, format_prop_pick, format_no_bet


TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")


# -------------------------
# LOAD CONFIG + STATE
# -------------------------
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
MIN_BOOKMAKERS = int(CONFIG.get("min_bookmakers", 2))

TEAM_BUDGET_SHARE = float(CONFIG.get("team_budget_share", 0.60))
PROPS_BUDGET_SHARE = float(CONFIG.get("props_budget_share", 0.40))

PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", True))

MAX_ML_PER_SLATE = int(CONFIG.get("max_ml_per_slate", 2))
ONE_PICK_PER_MATCH = bool(CONFIG.get("one_pick_per_match", True))


def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=20)
    r.raise_for_status()


def save_state():
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)


def reset_state_if_new_day():
    today_utc = datetime.now(timezone.utc).date().isoformat()
    if STATE.get("date_utc") != today_utc:
        STATE.clear()
        STATE.update({
            "date_utc": today_utc,
            "daily_spent_eur": 0.0,
            "team_bets_sent": 0,
            "prop_bets_sent": 0,
            "team_spent_eur": 0.0,
            "props_spent_eur": 0.0,
            "last_regions_team": None,
            "last_regions_props": None,
        })


def is_valid_game_date(commence_time: str) -> bool:
    today_utc = datetime.now(timezone.utc).date()
    game_date = parser.isoparse(commence_time).date()
    delta = (game_date - today_utc).days
    return 0 <= delta <= 1


def build_near_miss_line(p: Dict[str, Any]) -> str:
    line_part = f" {p['line']}" if p.get("line") is not None else ""
    return (
        f"- {p['match']} — {p['market']} — **{p['selection']}{line_part}** @ {p['odds']:.2f} "
        f"| edge {p.get('edge', 0.0)*100:.2f}% | dev {p.get('dev', 0.0)*100:.2f}%"
    )


# ============================================================
# TEAM ANALYSIS (FIX 2 intégré)
# ============================================================

def analyze_team_game(g: Dict[str, Any], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", [])

    if not bookmakers:
        stats["rejects"]["no_bookmakers"] = stats["rejects"].get("no_bookmakers", 0) + 1
        return []

    candidates: List[Dict[str, Any]] = []

    def process_market(market_key, label, line_val=None):
        lines = collect_market_lines(bookmakers, market_key)["lines"]
        lk = pick_consensus_line(lines)
        if not lk or lk not in lines:
            return

        outs = list(lines[lk].keys())
        if len(outs) < 2:
            return

        stats["markets_tested"] += 1

        diags = analyze_market_two_way_with_diagnostics(
            match=match,
            market_label=label,
            line=line_val if line_val is not None else (float(lk) if lk != "h2h" else None),
            outcome_a=outs[0],
            outcome_b=outs[1],
            entries_a=lines[lk][outs[0]],
            entries_b=lines[lk][outs[1]],
            prefer_fr=PREFER_FR_BOOKS,
        )

        stats["near_miss"].extend(diags)

        for d in diags:
            if d["books_used"] < MIN_BOOKMAKERS:
                stats["rejects"]["books<min"] = stats["rejects"].get("books<min", 0) + 1
                continue

            ok_edge = d["edge"] >= EDGE_THRESHOLD
            ok_dev = d["dev"] >= DEV_THRESHOLD

            if ok_edge and ok_dev:
                candidates.append(d)
            else:
                if not ok_edge:
                    stats["rejects"]["edge<th"] = stats["rejects"].get("edge<th", 0) + 1
                if not ok_dev:
                    stats["rejects"]["dev<th"] = stats["rejects"].get("dev<th", 0) + 1

    # ML
    process_market("h2h", "MONEYLINE")

    # TOTAL
    process_market("totals", "TOTAL")

    # SPREAD
    process_market("spreads", "SPREAD")

    return candidates


# ============================================================
# MAIN
# ============================================================

def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
    props_budget = remaining_budget_total * PROPS_BUDGET_SHARE

    stats_team = {
        "games_analyzed": 0,
        "markets_tested": 0,
        "rejects": {},
        "near_miss": [],
        "region": None,
    }

    TEAM_MARKETS = "h2h,spreads,totals"

    # FIX 3 — élargissement couverture
    team_games, team_meta = fetch_odds_with_fallback(
        markets=TEAM_MARKETS,
        regions_priority=["us", "uk", "eu", "fr", "us2", "au"]
    )

    stats_team["region"] = team_meta.get("chosen_region")
    STATE["last_regions_team"] = stats_team["region"]

    team_candidates: List[Dict[str, Any]] = []

    for g in team_games:
        if not is_valid_game_date(g["commence_time"]):
            continue
        stats_team["games_analyzed"] += 1
        team_candidates.extend(analyze_team_game(g, stats_team))

    # Near miss top 5
    near_sorted = sorted(
        stats_team["near_miss"],
        key=lambda x: (x.get("edge", -999), x.get("dev", -999)),
        reverse=True
    )
    near_lines = [build_near_miss_line(p) for p in near_sorted[:5]]

    team_picks: List[Dict[str, Any]] = []
    if remaining_team_slots > 0 and team_budget > 0:
        team_picks = diversify_team_picks(
            team_candidates,
            max_picks=min(remaining_team_slots, 3),
            max_ml=MAX_ML_PER_SLATE,
            one_pick_per_match=ONE_PICK_PER_MATCH,
        )

    # =========================
    # NO BET
    # =========================
    if not team_picks:
        rejects_sorted = sorted(stats_team["rejects"].items(), key=lambda x: x[1], reverse=True)[:5]
        top_rejects = [f"{k}: {v}" for k, v in rejects_sorted]

        desc = format_no_bet(
            title="❌ NO BET (TEAM)",
            reason=f"aucune value détectée (edge>={EDGE_THRESHOLD*100:.1f}% & dev>={DEV_THRESHOLD*100:.0f}%)",
            regions_used=[stats_team.get("region") or "n/a"],
            games_analyzed=stats_team["games_analyzed"],
            markets_tested=stats_team["markets_tested"],
            top_rejects=top_rejects,
            near_miss_lines=near_lines,
            daily_budget=DAILY_BUDGET,
            daily_spent=float(STATE.get("daily_spent_eur", 0.0)),
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", desc)
        save_state()
        return

    # =========================
    # SEND TEAM
    # =========================
    stakes_team = allocate_stakes_fixed_splits(team_budget, len(team_picks))

    for pick, stake in zip(team_picks, stakes_team):
        spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake
        msg = format_team_pick(
            p=pick,
            stake=stake,
            bankroll=BANKROLL,
            daily_budget=DAILY_BUDGET,
            spent_after=spent_after,
        )
        post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)
        STATE["daily_spent_eur"] = spent_after
        STATE["team_bets_sent"] += 1
        STATE["team_spent_eur"] += stake

    save_state()


if __name__ == "__main__":
    main()
