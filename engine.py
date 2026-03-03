import os
import math
from typing import Any, Dict, List, Optional, Tuple

# -------------------------
# BOOK FILTERING / QUALITY
# -------------------------

DEFAULT_EXCLUDE = ["mybookie", "lowvig"]
EXCLUDE_BOOKS = [x.strip().lower() for x in os.environ.get("EXCLUDE_BOOKS", "").split(",") if x.strip()]
EXCLUDE_BOOK_KEYWORDS = DEFAULT_EXCLUDE + EXCLUDE_BOOKS

TIER1_US_BOOKS = {
    "fanduel", "draftkings", "betmgm", "caesars", "pointsbet", "betrivers",
    "bet365", "circa", "superbook", "barstool", "hard rock", "espn bet",
}

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def is_excluded_book(book_title: str) -> bool:
    t = _norm(book_title)
    return any(k in t for k in EXCLUDE_BOOK_KEYWORDS)

def book_quality_points(book_title: str) -> float:
    t = _norm(book_title)
    return 7.0 if any(k in t for k in TIER1_US_BOOKS) else 0.0


# -------------------------
# BASIC HELPERS
# -------------------------

def safe_float(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def implied_prob(odds: Optional[float]) -> float:
    o = safe_float(odds)
    if not o or o <= 0:
        return 0.0
    return 1.0 / o

def median(values: List[Optional[float]]) -> Optional[float]:
    vals = sorted([v for v in values if v is not None])
    n = len(vals)
    if n == 0:
        return None
    if n % 2 == 1:
        return float(vals[n // 2])
    return (float(vals[n // 2 - 1]) + float(vals[n // 2])) / 2.0

def stdev(values: List[Optional[float]]) -> float:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    var = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(var)

def compute_no_vig_fair_probs(med_a: float, med_b: float) -> Tuple[float, float]:
    pa = implied_prob(med_a)
    pb = implied_prob(med_b)
    s = pa + pb
    if s <= 0:
        return 0.0, 0.0
    return pa / s, pb / s

def best_price(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entries:
        return None
    return max(entries, key=lambda x: float(x.get("price") or 0.0))

def best_tier1_price(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    tier1 = [e for e in entries if book_quality_points(e.get("book", "")) >= 7.0]
    return best_price(tier1) if tier1 else None


# -------------------------
# LINE COLLECTORS
# -------------------------

def collect_market_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Returns:
      {"market": market_key, "lines": {line_key: {outcome_key: [entry,...]}}}

    entry = {"price": float, "book": str, "point": float|None}
    """
    lines: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        if is_excluded_book(book):
            continue

        for m in b.get("markets", []) or []:
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []) or []:
                name = o.get("name")
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))
                if name is None or price is None:
                    continue

                if market_key == "h2h":
                    line_key = "h2h"
                    outcome_key = str(name)
                elif market_key == "totals":
                    if point is None:
                        continue
                    line_key = f"{point}"
                    outcome_key = str(name)  # Over/Under
                elif market_key == "spreads":
                    if point is None:
                        continue
                    line_key = f"{abs(point)}"
                    outcome_key = str(name)  # team
                else:
                    line_key = f"{point}" if point is not None else "NA"
                    outcome_key = str(name)

                lines.setdefault(line_key, {}).setdefault(outcome_key, []).append(
                    {"price": price, "book": book, "point": point}
                )

    return {"market": market_key, "lines": lines}


def collect_player_prop_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Build:
      {"market": market_key, "props": {player: {line_key: {"Over":[...],"Under":[...]}}}}
    """
    props: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        if is_excluded_book(book):
            continue

        for m in b.get("markets", []) or []:
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []) or []:
                side = o.get("name")  # Over/Under
                player = o.get("description") or o.get("participant") or o.get("player")
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))

                if side is None or player is None or price is None or point is None:
                    continue
                player = str(player).strip()
                if not player:
                    continue

                line_key = f"{point}"
                props.setdefault(player, {}).setdefault(line_key, {}).setdefault(str(side), []).append(
                    {"price": price, "book": book, "point": point}
                )

    return {"market": market_key, "props": props}


def collect_team_totals_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Team totals outcomes often:
      name: Over/Under
      description: team name
      point: line
    Build:
      {"market": market_key, "teams": {team: {line_key: {"Over":[...],"Under":[...]}}}}
    """
    teams: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        if is_excluded_book(book):
            continue

        for m in b.get("markets", []) or []:
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []) or []:
                side = o.get("name")
                team = o.get("description") or o.get("team") or o.get("participant")
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))

                if side is None or team is None or price is None or point is None:
                    continue
                team = str(team).strip()
                if not team:
                    continue

                line_key = f"{point}"
                teams.setdefault(team, {}).setdefault(line_key, {}).setdefault(str(side), []).append(
                    {"price": price, "book": book, "point": point}
                )

    return {"market": market_key, "teams": teams}


# -------------------------
# CONSENSUS LINE (FIX ML-ONLY ISSUE)
# -------------------------

def pick_consensus_line(lines_dict: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
    """
    FIX: choose the line_key that is the most 'balanced' (both sides present).
    Score by (min_count_across_outcomes, total_count).
    """
    if not lines_dict:
        return None

    best_key = None
    best_tuple = (-1, -1)

    for lk, outcomes in lines_dict.items():
        counts = [len(v) for v in outcomes.values()]
        if len(counts) < 2:
            continue
        minc = min(counts)
        tot = sum(counts)
        if (minc, tot) > best_tuple:
            best_tuple = (minc, tot)
            best_key = lk

    return best_key


def pick_consensus_prop_line(player_lines: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
    return pick_consensus_line(player_lines)


# -------------------------
# SCORING (RECALIBRATED)
# -------------------------

def robust_score(
    market_label: str,
    tier: str,
    edge: float,
    dev: float,
    books_used: int,
    chosen_book: str,
    haircut_applied: bool,
    edge_raw: float,
    odds: float,
) -> float:
    """
    0-100 score that matches your expectation:
    - rewards edge/dev/books/quality book
    - penalizes ML, crazy odds, haircut, relaxed
    Goal: good spreads/totals/props can reach 80+.
    """
    edge = max(0.0, float(edge))
    dev = max(0.0, float(dev))
    books_used = int(books_used)

    # edge: 5% => 55 pts
    edge_pts = min(55.0, (edge / 0.05) * 55.0)

    # dev: 10% => 20 pts
    dev_pts = min(20.0, (dev / 0.10) * 20.0)

    # books: 10+ books => 15 pts
    books_pts = min(15.0, max(0.0, (books_used - 2) / 8.0) * 15.0)

    # market preference
    m = (market_label or "").upper()
    market_pts = 0.0
    if "PROP" in m:
        market_pts += 10.0
    elif "TEAM TOTAL" in m:
        market_pts += 9.0
    elif "SPREAD" in m:
        market_pts += 8.0
    elif "TOTAL" in m:
        market_pts += 8.0
    elif "MONEYLINE" in m:
        market_pts -= 5.0

    if "1H" in m:
        market_pts += 4.0

    # book quality
    bq = book_quality_points(chosen_book)

    # tier bonus/penalty
    tier_bonus = 15.0 if (tier == "STRICT") else -10.0

    # penalties
    pen = 0.0
    if haircut_applied:
        pen += 5.0
    if edge_raw > 0.10:
        pen += 15.0
    if "MONEYLINE" in m and odds > 3.0:
        pen += 15.0

    score = edge_pts + dev_pts + books_pts + market_pts + bq + tier_bonus - pen
    return float(max(0.0, min(100.0, score)))


# -------------------------
# ANALYZE 2-WAY MARKET + HAIRCUT
# -------------------------

def analyze_two_way_market(
    match: str,
    market_label: str,
    line: Optional[float],
    outcome_a: str,
    outcome_b: str,
    entries_a: List[Dict[str, Any]],
    entries_b: List[Dict[str, Any]],
    edge_threshold: float,
    dev_threshold: float,
    min_books: int,
    prefer_fr: bool = False,  # ignored in US-only mode; kept for compatibility
    tier: str = "STRICT",
    return_all: bool = False,
) -> Any:
    rejects: Dict[str, int] = {}

    def rej(k: str):
        rejects[k] = rejects.get(k, 0) + 1

    # distinct books
    books_a = len({e.get("book") for e in entries_a if e.get("book")})
    books_b = len({e.get("book") for e in entries_b if e.get("book")})
    total_books = len({e.get("book") for e in (entries_a + entries_b) if e.get("book")})

    if total_books < min_books:
        rej("books<th")
        return {"passed": [], "all": [], "rejects": rejects} if return_all else []

    if books_a < 1 or books_b < 1:
        rej("missing_side")
        return {"passed": [], "all": [], "rejects": rejects} if return_all else []

    med_a = median([e.get("price") for e in entries_a])
    med_b = median([e.get("price") for e in entries_b])
    if med_a is None or med_b is None:
        rej("median_missing")
        return {"passed": [], "all": [], "rejects": rejects} if return_all else []

    fair_a_raw, fair_b_raw = compute_no_vig_fair_probs(med_a, med_b)

    # prefer tier1 book price if available, else best
    best_all_a = best_price(entries_a)
    best_all_b = best_price(entries_b)
    if not best_all_a or not best_all_b:
        rej("best_missing")
        return {"passed": [], "all": [], "rejects": rejects} if return_all else []

    best_t1_a = best_tier1_price(entries_a)
    best_t1_b = best_tier1_price(entries_b)

    chosen_a = best_t1_a if best_t1_a else best_all_a
    chosen_b = best_t1_b if best_t1_b else best_all_b

    # raw edge vs implied(best)
    edge_a_raw = fair_a_raw - implied_prob(chosen_a.get("price"))
    edge_b_raw = fair_b_raw - implied_prob(chosen_b.get("price"))

    # dev vs median
    dev_a = (float(chosen_a["price"]) - float(med_a)) / float(med_a) if med_a > 0 else 0.0
    dev_b = (float(chosen_b["price"]) - float(med_b)) / float(med_b) if med_b > 0 else 0.0

    # median vig proxy (2-way)
    vig_median = (implied_prob(med_a) + implied_prob(med_b)) - 1.0

    # odds dispersion
    odds_sd_a = stdev([e.get("price") for e in entries_a])
    odds_sd_b = stdev([e.get("price") for e in entries_b])

    def apply_haircut(fair_raw: float, edge_raw: float, best_odds: float) -> Tuple[float, float, bool, List[str]]:
        """
        Haircut if edge_raw > 6% => -30% on edge (=> edge_adj = 0.7 * edge_raw)
        Flags if >10%, refuse if >15%
        """
        flags: List[str] = []
        haircut = False

        if edge_raw > 0.15:
            flags.append("edge_raw>15% REFUSE")
            return fair_raw, edge_raw, haircut, flags

        if edge_raw > 0.10:
            flags.append("edge_raw>10% (suspect)")

        edge_adj = edge_raw
        if edge_raw > 0.06:
            haircut = True
            edge_adj = edge_raw * 0.70
            flags.append("haircut -30%")

        # fair_adj reconstructed around implied(best): p = imp(best) + edge_adj
        fair_adj = implied_prob(best_odds) + edge_adj
        fair_adj = max(0.0, min(1.0, fair_adj))
        return fair_adj, edge_adj, haircut, flags

    def build_item(outcome: str, chosen: Dict[str, Any], med: float, fair_raw: float, edge_raw: float, dev: float, odds_sd: float) -> Dict[str, Any]:
        fair_adj, edge_adj, haircut, flags = apply_haircut(fair_raw, edge_raw, float(chosen["price"]))

        # if refused by haircut logic
        if any("REFUSE" in f for f in flags):
            passed = False
            ev = fair_adj * float(chosen["price"]) - 1.0
            item = {
                "match": match, "market": market_label, "line": line,
                "selection": outcome, "odds": float(chosen["price"]), "book": str(chosen.get("book", "Unknown")),
                "median_odds": float(med), "books_used": int(min(books_a, books_b)), "total_books": int(total_books),
                "fair_prob_raw": float(fair_raw), "fair_prob": float(fair_adj),
                "edge_raw": float(edge_raw), "edge": float(edge_adj),
                "dev": float(dev),
                "ev": float(ev),
                "vig_median": float(vig_median),
                "odds_stdev": float(odds_sd),
                "tier": tier,
                "haircut_applied": bool(haircut),
                "flags": flags,
                "passed": False,
            }
            item["score"] = robust_score(
                market_label, tier, item["edge"], item["dev"], item["books_used"], item["book"],
                item["haircut_applied"], item["edge_raw"], item["odds"]
            )
            return item

        # EV
        ev = fair_adj * float(chosen["price"]) - 1.0

        # threshold pass
        passed = (edge_adj >= edge_threshold) and (dev >= dev_threshold)

        item = {
            "match": match, "market": market_label, "line": line,
            "selection": outcome, "odds": float(chosen["price"]), "book": str(chosen.get("book", "Unknown")),
            "median_odds": float(med), "books_used": int(min(books_a, books_b)), "total_books": int(total_books),
            "fair_prob_raw": float(fair_raw), "fair_prob": float(fair_adj),
            "edge_raw": float(edge_raw), "edge": float(edge_adj),
            "dev": float(dev),
            "ev": float(ev),
            "vig_median": float(vig_median),
            "odds_stdev": float(odds_sd),
            "tier": tier,
            "haircut_applied": bool(haircut),
            "flags": flags,
            "passed": bool(passed),
        }
        item["score"] = robust_score(
            market_label, tier, item["edge"], item["dev"], item["books_used"], item["book"],
            item["haircut_applied"], item["edge_raw"], item["odds"]
        )
        return item

    a_item = build_item(outcome_a, chosen_a, float(med_a), fair_a_raw, edge_a_raw, dev_a, odds_sd_a)
    b_item = build_item(outcome_b, chosen_b, float(med_b), fair_b_raw, edge_b_raw, dev_b, odds_sd_b)

    all_items = [a_item, b_item]

    # reject counters
    for it in all_items:
        if it["books_used"] < min_books:
            rej("books<th")
        if it["edge"] < edge_threshold:
            rej("edge<th")
        if it["dev"] < dev_threshold:
            rej("dev<th")

    passed = [it for it in all_items if it.get("passed")]

    if return_all:
        return {"passed": passed, "all": all_items, "rejects": rejects}
    return passed


# Backward compat alias
def analyze_market_two_way(*args, **kwargs):
    return analyze_two_way_market(*args, **kwargs)


# -------------------------
# DIVERSIFICATION
# -------------------------

def diversify_team_picks(
    picks: List[Dict[str, Any]],
    max_picks: int,
    max_ml: int = 2,
    one_pick_per_match: bool = True,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    for p in sorted(picks, key=lambda x: (x.get("score", 0), x.get("edge", 0), x.get("dev", 0)), reverse=True):
        if len(out) >= max_picks:
            break
        if one_pick_per_match and p.get("match") in used_matches:
            continue
        if p.get("market") == "MONEYLINE" and ml_count >= max_ml:
            continue

        out.append(p)
        used_matches.add(p.get("match"))
        if p.get("market") == "MONEYLINE":
            ml_count += 1

    return out


def diversify_prop_picks(
    picks: List[Dict[str, Any]],
    max_picks: int,
    one_pick_per_match: bool = True,
    one_pick_per_player: bool = True,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used_matches = set()
    used_players = set()

    for p in sorted(picks, key=lambda x: (x.get("score", 0), x.get("edge", 0), x.get("dev", 0)), reverse=True):
        if len(out) >= max_picks:
            break

        if one_pick_per_match and p.get("match") in used_matches:
            continue

        player = p.get("player")
        if one_pick_per_player and player:
            if player in used_players:
                continue

        out.append(p)
        used_matches.add(p.get("match"))
        if player:
            used_players.add(player)

    return out


# -------------------------
# STAKES
# -------------------------

def allocate_stakes_capped(total_budget: float, n: int, daily_budget: float, cap_day_share: float = 0.25) -> List[float]:
    """
    Stable split 40/35/25 then:
    - never exceed total_budget
    - never exceed cap_day_share of DAILY budget per pick
    """
    if n <= 0 or total_budget <= 0:
        return []

    if n == 1:
        splits = [1.0]
    elif n == 2:
        splits = [0.6, 0.4]
    else:
        splits = [0.4, 0.35, 0.25]

    planned = [total_budget * s for s in splits[:n]]
    stakes = [round(x, 2) for x in planned]

    # cap per pick (25% day budget)
    cap_abs = max(0.0, float(daily_budget) * float(cap_day_share))
    stakes = [min(s, cap_abs) for s in stakes]

    # ensure sum <= total_budget (after cap can only go down)
    while sum(stakes) - total_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes
