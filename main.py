import os
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from dateutil import parser

from odds_api import fetch_odds_with_fallback, OddsApiError

from engine import (
    collect_market_lines,
    collect_player_prop_lines,
    collect_team_totals_lines,
    pick_consensus_line,
    pick_consensus_prop_line,
    analyze_two_way_market,
    allocate_stakes_capped,
)

from formatting import format_team_pick, format_prop_pick, format_no_bet

from context import (
    fetch_injuries,
    build_injury_note,
    search_player_id,
    fetch_player_season_minutes,
)

TEAM_WEBHOOK = os.environ.get("DISCORD_TEAM_WEBHOOK")
PROPS_WEBHOOK = os.environ.get("DISCORD_PROPS_WEBHOOK")
LOG_WEBHOOK = os.environ.get("DISCORD_LOG_WEBHOOK")

# CLV
CLV_REFRESH_MINUTES = 30
CLV_MAX_SNAPSHOTS = 4  # T0 + 3 refresh
CLV_HORIZON_HOURS = 6  # stop tracking after 6h

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

EDGE_THRESHOLD_STRICT = float(CONFIG.get("edge_threshold", 0.015))
DEV_THRESHOLD_STRICT = float(CONFIG.get("dev_threshold", 0.02))
MIN_BOOKMAKERS = int(CONFIG.get("min_bookmakers", 2))

TEAM_BUDGET_SHARE = float(CONFIG.get("team_budget_share", 0.60))
PROPS_BUDGET_SHARE = float(CONFIG.get("props_budget_share", 0.40))
PREFER_FR_BOOKS = bool(CONFIG.get("prefer_fr_books", True))

MAX_ML_PER_SLATE = int(CONFIG.get("max_ml_per_slate", 2))
ONE_PICK_PER_MATCH = bool(CONFIG.get("one_pick_per_match", True))

# cap stake so 1 pick never eats the whole day
MAX_SINGLE_STAKE_SHARE = float(CONFIG.get("max_single_stake_share", 0.45))  # 45% of bucket

# ladder (slate-level selection)
LADDER = [
    {"tier": "STRICT",  "edge": EDGE_THRESHOLD_STRICT, "dev": DEV_THRESHOLD_STRICT},
    {"tier": "RELAXED", "edge": max(0.010, EDGE_THRESHOLD_STRICT * 0.67), "dev": max(0.015, DEV_THRESHOLD_STRICT * 0.75)},
    {"tier": "RELAXED", "edge": 0.005, "dev": 0.010},
]

TEAM_MARKETS_BASE = "h2h,spreads,totals"
TEAM_MARKETS_OPTIONAL = [
    "team_totals",
    "h2h_h1",
    "spreads_h1",
    "totals_h1",
    "team_totals_h1",
]


# -------------------------
# HELPERS
# -------------------------
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
            "clv": {},
        })


def is_game_soon(commence_time: str, horizon_hours: int = 36) -> bool:
    try:
        start_dt = parser.isoparse(commence_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1) <= start_dt <= now + timedelta(hours=horizon_hours)
    except Exception:
        return False


def pick_id(p: Dict[str, Any]) -> str:
    parts = [
        p.get("match", ""),
        p.get("market", ""),
        p.get("player", ""),
        p.get("selection", ""),
        str(p.get("line", "")),
    ]
    return "|".join(parts)


def clv_get_store() -> Dict[str, Any]:
    if "clv" not in STATE or not isinstance(STATE.get("clv"), dict):
        STATE["clv"] = {}
    return STATE["clv"]


def clv_attach(p: Dict[str, Any]) -> Dict[str, Any]:
    store = clv_get_store()
    pid = pick_id(p)
    rec = store.get(pid) or {}
    p["clv_snapshots"] = rec.get("snapshots") or []
    return p


def clv_add_snapshot(p: Dict[str, Any], tag: str, odds: float, book: str):
    store = clv_get_store()
    pid = pick_id(p)
    now = datetime.now(timezone.utc).isoformat()

    rec = store.get(pid)
    if not rec:
        rec = {"created_ts": now, "snapshots": []}
        store[pid] = rec

    rec["snapshots"].append({
        "tag": tag,
        "odds": float(odds),
        "book": str(book),
        "ts_utc": now,
    })
    rec["snapshots"] = rec["snapshots"][-CLV_MAX_SNAPSHOTS:]


def clv_should_refresh(rec: Dict[str, Any]) -> bool:
    snaps = rec.get("snapshots") or []
    if len(snaps) >= CLV_MAX_SNAPSHOTS:
        return False

    created = rec.get("created_ts")
    if created:
        try:
            created_dt = parser.isoparse(created)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created_dt > timedelta(hours=CLV_HORIZON_HOURS):
                return False
        except Exception:
            pass

    if not snaps:
        return False

    last_ts = snaps[-1].get("ts_utc")
    try:
        last_dt = parser.isoparse(last_ts)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_dt >= timedelta(minutes=CLV_REFRESH_MINUTES)
    except Exception:
        return True


def merge_rejects(dst: Dict[str, int], src: Dict[str, int]):
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + int(v)


def build_near_miss_line(p: Dict[str, Any]) -> str:
    line_part = f" {p['line']}" if p.get("line") is not None else ""
    who = p.get("player") or p.get("selection")
    return (
        f"• {p.get('match','')} — {p.get('market','')} — **{who}{line_part}** @ {float(p.get('odds',0.0)):.2f} "
        f"({p.get('book','')}) | edge {p.get('edge',0.0)*100:.2f}% | dev {p.get('dev',0.0)*100:.2f}%"
    )


def _books_stats(games: List[Dict[str, Any]]) -> Tuple[float, int]:
    if not games:
        return 0.0, 0
    per_game = [len(g.get("bookmakers", []) or []) for g in games]
    avg_books = (sum(per_game) / len(per_game)) if per_game else 0.0

    uniq = set()
    for g in games:
        for b in (g.get("bookmakers", []) or []):
            t = (b.get("title") or "").strip()
            if t:
                uniq.add(t.lower())
    return avg_books, len(uniq)


# -------------------------
# FETCH TEAM GAMES (ALL MARKETS) + FR->US FORCE
# -------------------------
def fetch_team_games_all_markets() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    1) Base markets (FR first)
    2) If FR too poor => force US fallback
    3) Optional markets merged by game id
    """
    games, meta = fetch_odds_with_fallback(
        markets=TEAM_MARKETS_BASE,
        regions_priority=["fr", "us", "us2", "uk", "eu", "au"],
    )

    chosen = meta.get("chosen_region")
    avg_books, uniq_books = _books_stats(games)

    # 🔥 Strong rule: if chosen FR but <3 books/match avg OR too few unique books => force US
    if chosen == "fr" and (avg_books < 3.0 or uniq_books < 6):
        games, meta = fetch_odds_with_fallback(
            markets=TEAM_MARKETS_BASE,
            regions_priority=["us", "us2", "uk", "eu", "au"],
        )
        meta["forced_us"] = True

    games_by_id: Dict[str, Dict[str, Any]] = {g.get("id"): g for g in games if g.get("id")}

    def merge_books(base_game: Dict[str, Any], add_game: Dict[str, Any]):
        base_books = base_game.get("bookmakers", []) or []
        add_books = add_game.get("bookmakers", []) or []
        by_title = {b.get("title"): b for b in base_books if b.get("title")}

        for b in add_books:
            t = b.get("title")
            if not t:
                continue
            if t not in by_title:
                base_books.append(b)
                by_title[t] = b
            else:
                bm = by_title[t].get("markets", []) or []
                am = b.get("markets", []) or []
                existing = {m.get("key") for m in bm}
                for m in am:
                    k = m.get("key")
                    if k and k not in existing:
                        bm.append(m)
                by_title[t]["markets"] = bm

        base_game["bookmakers"] = base_books

    # Optional markets
    for mk in TEAM_MARKETS_OPTIONAL:
        try:
            g2, _m2 = fetch_odds_with_fallback(
                markets=mk,
                regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
            )
        except OddsApiError:
            continue

        for gg in g2:
            gid = gg.get("id")
            if not gid:
                continue
            if gid not in games_by_id:
                games_by_id[gid] = gg
            else:
                merge_books(games_by_id[gid], gg)

    return list(games_by_id.values()), meta


# -------------------------
# TEAM ANALYSIS (RAW candidates once)
# -------------------------
def analyze_team_game_raw(
    g: Dict[str, Any],
    injuries: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> List[Dict[str, Any]]:
    home = g["home_team"]
    away = g["away_team"]
    match = f"{away} @ {home}"
    bookmakers = g.get("bookmakers", []) or []
    if not bookmakers:
        stats["rejects"]["no_bookmakers"] = stats["rejects"].get("no_bookmakers", 0) + 1
        return []

    injury_note = build_injury_note(match, injuries)
    raw: List[Dict[str, Any]] = []

    def add_market_two_way(market_label: str, line_val: Optional[float], a_key: str, b_key: str, a_entries, b_entries):
        stats["markets_attempted"] += 1
        out = analyze_two_way_market(
            match=match,
            market_label=market_label,
            line=line_val,
            outcome_a=a_key,
            outcome_b=b_key,
            entries_a=a_entries,
            entries_b=b_entries,
            edge_threshold=EDGE_THRESHOLD_STRICT,
            dev_threshold=DEV_THRESHOLD_STRICT,
            min_books=MIN_BOOKMAKERS,
            prefer_fr=PREFER_FR_BOOKS,
            tier="STRICT",
            return_all=True,
        )
        merge_rejects(stats["rejects"], out.get("rejects", {}))
        all_items = out.get("all", []) or []
        if all_items:
            stats["markets_tested"] += 1
            for it in all_items:
                it["injury_note"] = injury_note
                raw.append(it)

    # ML
    h2h_lines = collect_market_lines(bookmakers, "h2h")["lines"]
    lk = pick_consensus_line(h2h_lines)
    if lk and lk in h2h_lines:
        outs = list(h2h_lines[lk].keys())
        if len(outs) >= 2:
            add_market_two_way("MONEYLINE", None, outs[0], outs[1], h2h_lines[lk][outs[0]], h2h_lines[lk][outs[1]])

    # TOTAL
    totals_lines = collect_market_lines(bookmakers, "totals")["lines"]
    tlk = pick_consensus_line(totals_lines)
    if tlk and tlk in totals_lines:
        sides = totals_lines[tlk]
        if "Over" in sides and "Under" in sides:
            add_market_two_way("TOTAL", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    # SPREAD
    spreads_lines = collect_market_lines(bookmakers, "spreads")["lines"]
    slk = pick_consensus_line(spreads_lines)
    if slk and slk in spreads_lines:
        teams = spreads_lines[slk]
        if home in teams and away in teams:
            add_market_two_way("SPREAD", float(slk), home, away, teams[home], teams[away])

    # TEAM TOTALS
    tt = collect_team_totals_lines(bookmakers, "team_totals").get("teams", {}) or {}
    for team, team_lines in tt.items():
        lk2 = pick_consensus_prop_line(team_lines)
        if not lk2 or lk2 not in team_lines:
            continue
        sides = team_lines[lk2]
        if "Over" in sides and "Under" in sides:
            add_market_two_way(f"TEAM TOTAL ({team})", float(lk2), "Over", "Under", sides["Over"], sides["Under"])

    # 1H ML/TOTAL/SPREAD + 1H team totals
    h1_h2h = collect_market_lines(bookmakers, "h2h_h1")["lines"]
    lk = pick_consensus_line(h1_h2h)
    if lk and lk in h1_h2h:
        outs = list(h1_h2h[lk].keys())
        if len(outs) >= 2:
            add_market_two_way("MONEYLINE 1H", None, outs[0], outs[1], h1_h2h[lk][outs[0]], h1_h2h[lk][outs[1]])

    h1_tot = collect_market_lines(bookmakers, "totals_h1")["lines"]
    tlk = pick_consensus_line(h1_tot)
    if tlk and tlk in h1_tot:
        sides = h1_tot[tlk]
        if "Over" in sides and "Under" in sides:
            add_market_two_way("TOTAL 1H", float(tlk), "Over", "Under", sides["Over"], sides["Under"])

    h1_sp = collect_market_lines(bookmakers, "spreads_h1")["lines"]
    slk = pick_consensus_line(h1_sp)
    if slk and slk in h1_sp:
        teams = h1_sp[slk]
        if home in teams and away in teams:
            add_market_two_way("SPREAD 1H", float(slk), home, away, teams[home], teams[away])

    tt1 = collect_team_totals_lines(bookmakers, "team_totals_h1").get("teams", {}) or {}
    for team, team_lines in tt1.items():
        lk2 = pick_consensus_prop_line(team_lines)
        if not lk2 or lk2 not in team_lines:
            continue
        sides = team_lines[lk2]
        if "Over" in sides and "Under" in sides:
            add_market_two_way(f"TEAM TOTAL 1H ({team})", float(lk2), "Over", "Under", sides["Over"], sides["Under"])

    return raw


def patch_spread_signed_line(pick: Dict[str, Any], game: Dict[str, Any]) -> Dict[str, Any]:
    market = str(pick.get("market", ""))
    if "SPREAD" not in market:
        return pick

    selection = pick.get("selection")
    bookmakers = game.get("bookmakers", []) or []

    market_key = "spreads_h1" if "1H" in market else "spreads"
    spreads_lines = collect_market_lines(bookmakers, market_key)["lines"]
    slk = pick_consensus_line(spreads_lines)
    if not slk or slk not in spreads_lines:
        return pick

    teams = spreads_lines[slk]
    if selection not in teams:
        return pick

    chosen_book = pick.get("book")
    for e in teams[selection]:
        if e.get("book") == chosen_book and e.get("point") is not None:
            pick["line"] = float(e["point"])
            return pick

    if teams[selection] and teams[selection][0].get("point") is not None:
        pick["line"] = float(teams[selection][0]["point"])
    return pick


# -------------------------
# PROPS (RAW candidates once)
# -------------------------
def analyze_props_raw(injuries: List[Dict[str, Any]], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    prop_market_map = {
        "PROP PTS": "player_points",
        "PROP REB": "player_rebounds",
        "PROP AST": "player_assists",
        "PROP 3PT": "player_threes",
        "PROP PRA": "player_points_rebounds_assists",
        "PROP PR": "player_points_rebounds",
        "PROP PA": "player_points_assists",
        "PROP RA": "player_rebounds_assists",
    }

    raw: List[Dict[str, Any]] = []

    for label, market_key in prop_market_map.items():
        try:
            games, meta = fetch_odds_with_fallback(
                markets=market_key,
                regions_priority=["us", "us2", "uk", "eu", "au", "fr"],
            )
        except OddsApiError:
            continue

        stats["regions_props"] = meta.get("chosen_region") or stats.get("regions_props")

        for g in games:
            if not is_game_soon(g.get("commence_time", "")):
                continue

            match = f"{g['away_team']} @ {g['home_team']}"
            bookmakers = g.get("bookmakers", []) or []
            if not bookmakers:
                continue

            injury_note = build_injury_note(match, injuries)
            props_struct = collect_player_prop_lines(bookmakers, market_key)["props"]

            for player, player_lines in props_struct.items():
                lk = pick_consensus_prop_line(player_lines)
                if not lk or lk not in player_lines:
                    continue
                sides = player_lines[lk]
                if "Over" not in sides or "Under" not in sides:
                    continue

                stats["props_attempted"] += 1

                out = analyze_two_way_market(
                    match=match,
                    market_label=label,
                    line=float(lk),
                    outcome_a="Over",
                    outcome_b="Under",
                    entries_a=sides["Over"],
                    entries_b=sides["Under"],
                    edge_threshold=EDGE_THRESHOLD_STRICT,
                    dev_threshold=DEV_THRESHOLD_STRICT,
                    min_books=MIN_BOOKMAKERS,
                    prefer_fr=PREFER_FR_BOOKS,
                    tier="STRICT",
                    return_all=True,
                )

                merge_rejects(stats["rejects"], out.get("rejects", {}))
                items = out.get("all", []) or []
                if items:
                    stats["props_tested"] += 1
                    for it in items:
                        it["player"] = player
                        it["injury_note"] = injury_note

                        pid = search_player_id(player)
                        mpg = fetch_player_season_minutes(pid) if pid else None
                        if mpg is not None:
                            it["minutes_note"] = f"{mpg:.1f} min (saison)"

                        raw.append(it)

    return raw


# -------------------------
# SLATE-LEVEL LADDER SELECTION
# -------------------------
def select_team_picks_with_ladder(
    raw: List[Dict[str, Any]],
    max_picks: int,
) -> List[Dict[str, Any]]:
    """
    Priorité STRICT, puis RELAXED pour remplir jusqu'à max_picks.
    Diversification: max ML, one pick per match.
    """
    selected: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    def can_take(p: Dict[str, Any]) -> bool:
        nonlocal ml_count
        m = p.get("match")
        if ONE_PICK_PER_MATCH and m in used_matches:
            return False
        if p.get("market") == "MONEYLINE" and ml_count >= MAX_ML_PER_SLATE:
            return False
        return True

    def take(p: Dict[str, Any]):
        nonlocal ml_count
        selected.append(p)
        used_matches.add(p.get("match"))
        if p.get("market") == "MONEYLINE":
            ml_count += 1

    # sort once by score/edge/dev
    raw_sorted = sorted(raw, key=lambda x: (x.get("score", 0.0), x.get("edge", 0.0), x.get("dev", 0.0)), reverse=True)

    for rung in LADDER:
        if len(selected) >= max_picks:
            break
        edge_th = float(rung["edge"])
        dev_th = float(rung["dev"])
        tier = rung["tier"]

        for p in raw_sorted:
            if len(selected) >= max_picks:
                break
            if p.get("edge", 0.0) < edge_th:
                continue
            if p.get("dev", 0.0) < dev_th:
                continue
            if not can_take(p):
                continue

            pp = dict(p)
            pp["tier"] = tier
            take(pp)

    return selected


def select_prop_picks_with_ladder(
    raw: List[Dict[str, Any]],
    max_picks: int,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    used_matches = set()
    used_players = set()

    raw_sorted = sorted(raw, key=lambda x: (x.get("score", 0.0), x.get("edge", 0.0), x.get("dev", 0.0)), reverse=True)

    for rung in LADDER:
        if len(selected) >= max_picks:
            break
        edge_th = float(rung["edge"])
        dev_th = float(rung["dev"])
        tier = rung["tier"]

        for p in raw_sorted:
            if len(selected) >= max_picks:
                break
            if p.get("edge", 0.0) < edge_th:
                continue
            if p.get("dev", 0.0) < dev_th:
                continue

            m = p.get("match")
            pl = p.get("player")

            if m in used_matches:
                continue
            if pl and pl in used_players:
                continue

            pp = dict(p)
            pp["tier"] = tier
            selected.append(pp)
            used_matches.add(m)
            if pl:
                used_players.add(pl)

    return selected


# -------------------------
# CLV REFRESH
# -------------------------
def refresh_clv_from_current_games(team_games: List[Dict[str, Any]]):
    store = clv_get_store()
    if not store:
        return

    games_by_match: Dict[str, Dict[str, Any]] = {}
    for g in team_games or []:
        match = f"{g.get('away_team')} @ {g.get('home_team')}"
        games_by_match[match] = g

    now = datetime.now(timezone.utc)
    for pid, rec in list(store.items()):
        if not isinstance(rec, dict) or not clv_should_refresh(rec):
            continue

        try:
            match, market, player, selection, line_str = pid.split("|", 4)
        except Exception:
            continue

        g = games_by_match.get(match)
        if not g:
            continue

        bookmakers = g.get("bookmakers", []) or []
        best = None

        # ML
        if market == "MONEYLINE" or market == "MONEYLINE 1H":
            key = "h2h_h1" if "1H" in market else "h2h"
            lines = collect_market_lines(bookmakers, key)["lines"]
            lk = pick_consensus_line(lines)
            if lk and lk in lines and selection in lines[lk]:
                best = max(lines[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)

        # TOTAL
        elif market == "TOTAL" or market == "TOTAL 1H":
            key = "totals_h1" if "1H" in market else "totals"
            lines = collect_market_lines(bookmakers, key)["lines"]
            lk = pick_consensus_line(lines)
            if lk and lk in lines and selection in lines[lk]:
                best = max(lines[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)

        # SPREAD
        elif "SPREAD" in market:
            key = "spreads_h1" if "1H" in market else "spreads"
            lines = collect_market_lines(bookmakers, key)["lines"]
            lk = pick_consensus_line(lines)
            if lk and lk in lines and selection in lines[lk]:
                best = max(lines[lk][selection], key=lambda e: float(e.get("price") or 0.0), default=None)

        snaps = rec.get("snapshots") or []
        tag = "T+30" if len(snaps) <= 1 else ("T+60" if len(snaps) == 2 else "T+90")

        if best and best.get("price"):
            rec["snapshots"].append({
                "tag": tag,
                "odds": float(best["price"]),
                "book": str(best.get("book", "")),
                "ts_utc": now.isoformat(),
            })
            rec["snapshots"] = rec["snapshots"][-CLV_MAX_SNAPSHOTS:]
            store[pid] = rec

    STATE["clv"] = store


# -------------------------
# MAIN
# -------------------------
def main():
    reset_state_if_new_day()

    remaining_budget_total = max(0.0, DAILY_BUDGET - float(STATE.get("daily_spent_eur", 0.0)))
    remaining_team_slots = max(0, MAX_TEAM_PER_DAY - int(STATE.get("team_bets_sent", 0)))
    remaining_props_slots = max(0, MAX_PROPS_PER_DAY - int(STATE.get("prop_bets_sent", 0)))

    # injuries (best effort)
    try:
        injuries = fetch_injuries()
    except Exception:
        injuries = []

    # TEAM fetch + optional markets
    stats_team = {
        "games_analyzed": 0,
        "markets_attempted": 0,
        "markets_tested": 0,
        "rejects": {},
        "near_miss": [],
        "region": None,
    }

    try:
        team_games, team_meta = fetch_team_games_all_markets()
        stats_team["region"] = team_meta.get("chosen_region")
        STATE["last_regions_team"] = stats_team["region"]
    except OddsApiError:
        team_games, team_meta = [], {}
        stats_team["region"] = None

    refresh_clv_from_current_games(team_games)

    team_raw: List[Dict[str, Any]] = []
    games_by_match: Dict[str, Dict[str, Any]] = {}

    for g in team_games:
        if not is_game_soon(g.get("commence_time", "")):
            continue
        stats_team["games_analyzed"] += 1
        match = f"{g['away_team']} @ {g['home_team']}"
        games_by_match[match] = g

        raw_items = analyze_team_game_raw(g, injuries, stats_team)
        team_raw.extend(raw_items)
        stats_team["near_miss"].extend(raw_items)

    # PROPS
    stats_props = {
        "props_attempted": 0,
        "props_tested": 0,
        "rejects": {},
        "near_miss": [],
        "regions_props": None,
    }

    prop_raw: List[Dict[str, Any]] = []
    if remaining_props_slots > 0 and remaining_budget_total > 0:
        prop_raw = analyze_props_raw(injuries, stats_props)
        stats_props["near_miss"].extend(prop_raw)
        STATE["last_regions_props"] = stats_props.get("regions_props")

    # Build NO BET near miss + rejects
    near_sorted = sorted(
        [x for x in stats_team["near_miss"] if isinstance(x, dict)],
        key=lambda x: (x.get("edge", 0.0), x.get("dev", 0.0), x.get("score", 0.0)),
        reverse=True,
    )
    near_lines = [build_near_miss_line(p) for p in near_sorted[:5]]

    rejects_items = sorted(stats_team["rejects"].items(), key=lambda kv: kv[1], reverse=True)[:6]
    top_rejects = [f"{k}: {v}" for k, v in rejects_items]

    # Select picks (slate-level ladder)
    team_picks: List[Dict[str, Any]] = []
    prop_picks: List[Dict[str, Any]] = []

    if remaining_team_slots > 0:
        team_picks = select_team_picks_with_ladder(team_raw, max_picks=min(remaining_team_slots, 3))
        patched = []
        for p in team_picks:
            g = games_by_match.get(p.get("match"))
            if g:
                p = patch_spread_signed_line(p, g)
            patched.append(p)
        team_picks = patched

    if remaining_props_slots > 0:
        prop_picks = select_prop_picks_with_ladder(prop_raw, max_picks=min(remaining_props_slots, 3))

    # Budget allocation
    if team_picks and prop_picks:
        team_budget = remaining_budget_total * TEAM_BUDGET_SHARE
        props_budget = remaining_budget_total * PROPS_BUDGET_SHARE
    elif team_picks and not prop_picks:
        team_budget = remaining_budget_total
        props_budget = 0.0
    elif prop_picks and not team_picks:
        team_budget = 0.0
        props_budget = remaining_budget_total
    else:
        team_budget = 0.0
        props_budget = 0.0

    # NO BET
    if not team_picks and not prop_picks:
        desc = format_no_bet(
            title="❌ NO BET (TEAM+PROPS)",
            reason=f"aucune value détectée (edge>={EDGE_THRESHOLD_STRICT*100:.1f}% & dev>={DEV_THRESHOLD_STRICT*100:.0f}%)",
            regions_used=[stats_team.get("region") or "n/a", stats_props.get("regions_props") or ""],
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

    # SEND TEAM
    if team_picks and team_budget > 0:
        stakes_team = allocate_stakes_capped(team_budget, len(team_picks), max_single_share=MAX_SINGLE_STAKE_SHARE)
        for pick, stake in zip(team_picks, stakes_team):
            if stake <= 0:
                continue
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake

            clv_add_snapshot(pick, "T0", float(pick["odds"]), str(pick.get("book", "")))
            pick = clv_attach(pick)

            msg = format_team_pick(
                p=pick,
                stake=stake,
                bankroll=BANKROLL,
                daily_budget=DAILY_BUDGET,
                spent_after=spent_after,
            )
            post_discord(TEAM_WEBHOOK, "✅ NBA TEAM BET", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["team_bets_sent"] = int(STATE.get("team_bets_sent", 0)) + 1
            STATE["team_spent_eur"] = float(STATE.get("team_spent_eur", 0.0)) + stake

    # SEND PROPS
    if prop_picks and props_budget > 0:
        stakes_props = allocate_stakes_capped(props_budget, len(prop_picks), max_single_share=MAX_SINGLE_STAKE_SHARE)
        for pick, stake in zip(prop_picks, stakes_props):
            if stake <= 0:
                continue
            spent_after = float(STATE.get("daily_spent_eur", 0.0)) + stake

            clv_add_snapshot(pick, "T0", float(pick["odds"]), str(pick.get("book", "")))
            pick = clv_attach(pick)

            msg = format_prop_pick(
                p=pick,
                stake=stake,
                bankroll=BANKROLL,
                daily_budget=DAILY_BUDGET,
                spent_after=spent_after,
            )
            post_discord(PROPS_WEBHOOK, "✅ NBA PLAYER PROP", msg)

            STATE["daily_spent_eur"] = spent_after
            STATE["prop_bets_sent"] = int(STATE.get("prop_bets_sent", 0)) + 1
            STATE["props_spent_eur"] = float(STATE.get("props_spent_eur", 0.0)) + stake

    save_state()


if __name__ == "__main__":
    main()
