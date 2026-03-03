import os
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple
from dateutil import parser

from odds_api import fetch_odds_with_fallback, OddsApiError
from engine import (
    collect_market_lines,
    collect_player_prop_lines,
    pick_consensus_line,
    analyze_two_way_market,
    diversify_team_picks,
    diversify_prop_picks,
    allocate_stakes_fixed_splits,
    apply_stake_rules,
)
from formatting import format_team_pick, format_prop_pick, format_no_bet


TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")


def post_discord(webhook: str, title: str, description: str):
    if not webhook:
        return
    data = {"embeds": [{"title": title, "description": description}]}
    r = requests.post(webhook, json=data, timeout=20)
    r.raise_for_status()


def is_game_soon(commence_time: str, horizon_hours: int = 36) -> bool:
    try:
        start_dt = parser.isoparse(commence_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1) <= start_dt <= now + timedelta(hours=horizon_hours)
    except Exception:
        return False


def main():
    # -------------------------
    # CONFIG
    # -------------------------
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    bankroll = float(cfg.get("bankroll_eur", 100.0))
    daily_budget_pct = float(cfg.get("daily_budget_pct", 0.10))
    daily_budget = bankroll * daily_budget_pct

    edge_th = float(cfg.get("edge_threshold", 0.015))
    dev_th = float(cfg.get("dev_threshold", 0.02))
    min_books = int(cfg.get("min_bookmakers", 2))

    max_ml = int(cfg.get("max_ml_per_slate", 2))
    one_pick_per_match = bool(cfg.get("one_pick_per_match", True))

    # Stake rules (comme tu voulais figer)
    cap_pct = float(cfg.get("cap_per_pick_pct", 0.30))      # 30% budget jour
    fill_mult = float(cfg.get("fill_stake_multiplier", 0.30))  # FILL = 0.30x

    # -------------------------
    # TEAM FETCH (ONLY SUPPORTED)
    # -------------------------
    stats_matches = 0
    stats_markets_tested = 0

    try:
        team_games, meta = fetch_odds_with_fallback(
            markets="h2h,spreads,totals",
            regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
        )
    except OddsApiError as e:
        msg = format_no_bet(
            reason=f"OddsAPI error: {str(e)}",
            daily_budget=daily_budget,
            matches_analyzed=0,
            markets_tested=0,
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", msg)
        return

    if not team_games:
        msg = format_no_bet(
            reason="Aucun match reçu depuis OddsAPI (team_games vide). Vérifie ODDS_API_KEY / quota.",
            daily_budget=daily_budget,
            matches_analyzed=0,
            markets_tested=0,
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", msg)
        return

    team_candidates: List[Dict[str, Any]] = []

    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue

        stats_matches += 1
        match = f"{g['away_team']} @ {g['home_team']}"
        bookmakers = g.get("bookmakers", []) or []
        if not bookmakers:
            continue

        # MONEYLINE
        h2h = collect_market_lines(bookmakers, "h2h")["lines"]
        lk = pick_consensus_line(h2h)
        if lk and lk in h2h:
            outs = list(h2h[lk].keys())
            if len(outs) >= 2:
                out = analyze_two_way_market(
                    match=match,
                    market_label="MONEYLINE",
                    line=None,
                    outcome_a=outs[0],
                    outcome_b=outs[1],
                    entries_a=h2h[lk][outs[0]],
                    entries_b=h2h[lk][outs[1]],
                    edge_threshold=edge_th,
                    dev_threshold=dev_th,
                    min_books=min_books,
                    prefer_fr=False,
                    require_ev_nonneg=True,
                )
                stats_markets_tested += 1
                team_candidates.extend(out["passed"])

        # TOTAL
        totals = collect_market_lines(bookmakers, "totals")["lines"]
        tlk = pick_consensus_line(totals)
        if tlk and tlk in totals:
            sides = totals[tlk]
            if "Over" in sides and "Under" in sides:
                out = analyze_two_way_market(
                    match=match,
                    market_label="TOTAL",
                    line=float(tlk),
                    outcome_a="Over",
                    outcome_b="Under",
                    entries_a=sides["Over"],
                    entries_b=sides["Under"],
                    edge_threshold=edge_th,
                    dev_threshold=dev_th,
                    min_books=min_books,
                    prefer_fr=False,
                    require_ev_nonneg=True,
                )
                stats_markets_tested += 1
                team_candidates.extend(out["passed"])

        # SPREAD
        spreads = collect_market_lines(bookmakers, "spreads")["lines"]
        slk = pick_consensus_line(spreads)
        if slk and slk in spreads:
            teams = spreads[slk]
            home = g["home_team"]
            away = g["away_team"]
            if home in teams and away in teams:
                out = analyze_two_way_market(
                    match=match,
                    market_label="SPREAD",
                    line=float(slk),
                    outcome_a=home,
                    outcome_b=away,
                    entries_a=teams[home],
                    entries_b=teams[away],
                    edge_threshold=edge_th,
                    dev_threshold=dev_th,
                    min_books=min_books,
                    prefer_fr=False,
                    require_ev_nonneg=True,
                )
                stats_markets_tested += 1
                team_candidates.extend(out["passed"])

    # Diversify and select top 3
    team_picks = diversify_team_picks(team_candidates, max_picks=3, max_ml=max_ml, one_pick_per_match=one_pick_per_match)

    # -------------------------
    # PROPS FETCH (BASE)
    # -------------------------
    prop_market_map = {
        "PROP PTS": "player_points",
        "PROP REB": "player_rebounds",
        "PROP AST": "player_assists",
    }

    prop_candidates: List[Dict[str, Any]] = []

    for label, mk in prop_market_map.items():
        try:
            games, _ = fetch_odds_with_fallback(
                markets=mk,
                regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
            )
        except OddsApiError:
            continue

        for g in games:
            if not is_game_soon(g.get("commence_time", "")):
                continue

            match = f"{g['away_team']} @ {g['home_team']}"
            bookmakers = g.get("bookmakers", []) or []
            if not bookmakers:
                continue

            props_struct = collect_player_prop_lines(bookmakers, mk)["props"]

            for player, player_lines in props_struct.items():
                lk = pick_consensus_line(player_lines)
                if not lk or lk not in player_lines:
                    continue
                sides = player_lines[lk]
                if "Over" not in sides or "Under" not in sides:
                    continue

                out = analyze_two_way_market(
                    match=match,
                    market_label=label,
                    line=float(lk),
                    outcome_a="Over",
                    outcome_b="Under",
                    entries_a=sides["Over"],
                    entries_b=sides["Under"],
                    edge_threshold=edge_th,
                    dev_threshold=dev_th,
                    min_books=min_books,
                    prefer_fr=False,
                    require_ev_nonneg=True,
                )

                for it in out["passed"]:
                    it["player"] = player
                prop_candidates.extend(out["passed"])

    prop_picks = diversify_prop_picks(prop_candidates, max_picks=3, one_pick_per_match=True, one_pick_per_player=True)

    # -------------------------
    # NO BET
    # -------------------------
    if not team_picks and not prop_picks:
        msg = format_no_bet(
            reason=f"EV>=0 introuvable sur TEAM + PROPS (edge>={edge_th*100:.1f}% & dev>={dev_th*100:.1f}%).",
            daily_budget=daily_budget,
            matches_analyzed=stats_matches,
            markets_tested=stats_markets_tested,
        )
        post_discord(LOG_WEBHOOK, "NBA NO BET LOG", msg)
        return

    # -------------------------
    # BUDGET SPLIT + STAKES
    # -------------------------
    spent = 0.0
    # 60/40 par défaut si on a les deux
    if team_picks and prop_picks:
        team_budget = daily_budget * 0.60
        prop_budget = daily_budget * 0.40
    elif team_picks:
        team_budget = daily_budget
        prop_budget = 0.0
    else:
        team_budget = 0.0
        prop_budget = daily_budget

    # TEAM SEND
    if team_picks:
        stakes = allocate_stakes_fixed_splits(team_budget, len(team_picks))
        stakes = apply_stake_rules(stakes, daily_budget=daily_budget, cap_pct=cap_pct)
        for pick, stake in zip(team_picks, stakes):
            spent_after = spent + stake
            msg = format_team_pick(
                p=pick,
                stake=stake,
                daily_budget=daily_budget,
                spent_after=spent_after,
                max_ml=max_ml,
                one_pick_per_match=one_pick_per_match,
            )
            post_discord(TEAM_WEBHOOK, "NBA TEAM BET", msg)
            spent = spent_after

    # PROPS SEND
    if prop_picks:
        stakes = allocate_stakes_fixed_splits(prop_budget, len(prop_picks))
        stakes = apply_stake_rules(stakes, daily_budget=daily_budget, cap_pct=cap_pct)
        for pick, stake in zip(prop_picks, stakes):
            # (si tu veux un “fill” un jour, tu pourras multiplier par fill_mult sur les picks taggés RELAXED)
            spent_after = spent + stake
            msg = format_prop_pick(p=pick, stake=stake, daily_budget=daily_budget, spent_after=spent_after)
            post_discord(PROPS_WEBHOOK, "NBA PLAYER PROPS", msg)
            spent = spent_after


if __name__ == "__main__":
    main()
