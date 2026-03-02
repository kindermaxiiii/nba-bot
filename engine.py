import math
from typing import Any, Dict, List, Optional, Tuple


FR_BOOK_KEYWORDS = [
    "betclic", "winamax", "parions", "pmu", "unibet (fr)", "zebet", "bwin fr", "pokerstars", "vbet fr"
]


def is_fr_book(book_title: str) -> bool:
    t = (book_title or "").strip().lower()
    return any(k in t for k in FR_BOOK_KEYWORDS)


def implied_prob(odds: float) -> float:
    return 1.0 / odds if odds and odds > 0 else 0.0


def median(values: List[float]) -> Optional[float]:
    vals = sorted([v for v in values if v is not None])
    n = len(vals)
    if n == 0:
        return None
    if n % 2 == 1:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def stdev(values: List[float]) -> float:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    var = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(var)


def safe_float(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def collect_market_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Build a structure to analyze a market.

    Returns dict:
      lines[line_key][outcome_key] = list of entries
        entry = {price, book, is_fr, point}
    line_key:
      - h2h: "h2h"
      - totals: f"{point}"
      - spreads: f"{abs(point)}"  (canonical)
    outcome_key:
      - h2h: team name
      - totals: "Over" / "Under"
      - spreads: team name (point stored in entry)
    """
    lines: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for b in bookmakers:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []):
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
                    # props etc: use point as line_key if present, else "NA"
                    line_key = f"{point}" if point is not None else "NA"
                    outcome_key = str(name)

                lines.setdefault(line_key, {}).setdefault(outcome_key, []).append(
                    {"price": price, "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "lines": lines}


def pick_consensus_line(lines_dict: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
    """
    Choose the most 'supported' line_key by number of entries across outcomes.
    """
    if not lines_dict:
        return None

    best_key = None
    best_count = -1
    for lk, outcomes in lines_dict.items():
        cnt = sum(len(v) for v in outcomes.values())
        if cnt > best_count:
            best_count = cnt
            best_key = lk
    return best_key


def compute_no_vig_fair_probs(
    median_odds_a: float, median_odds_b: float
) -> Tuple[float, float]:
    """
    Two-way market:
      p_raw = 1/odds
      fair = p_raw / (p_raw_a + p_raw_b)
    """
    pa = implied_prob(median_odds_a)
    pb = implied_prob(median_odds_b)
    s = pa + pb
    if s <= 0:
        return 0.0, 0.0
    return pa / s, pb / s


def best_price(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entries:
        return None
    return max(entries, key=lambda x: x["price"])


def best_fr_price(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    frs = [e for e in entries if e.get("is_fr")]
    return best_price(frs) if frs else None


def score_bet(edge_real: float, dev: float, books_used: int, market_type: str) -> float:
    """
    Simple, stable scoring (0-100):
    - edge_real drives
    - dev helps
    - more books better
    - props slightly boosted (more inefficiencies)
    """
    edge_pts = max(0.0, min(1.0, edge_real / 0.08)) * 60.0   # 8% edge => 60 pts
    dev_pts = max(0.0, min(1.0, dev / 0.15)) * 20.0         # 15% dev => 20 pts
    book_pts = max(0.0, min(1.0, (books_used - 2) / 8.0)) * 15.0  # 10 books => 15 pts

    bonus = 0.0
    if market_type.startswith("PROP"):
        bonus = 5.0

    return max(0.0, min(100.0, edge_pts + dev_pts + book_pts + bonus))


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
    prefer_fr: bool = True,
) -> List[Dict[str, Any]]:
    """
    Analyze a 2-way market using no-vig fair prob from MEDIAN odds of both sides.
    Produce candidates for side A and side B.

    Returns list of candidate dicts.
    """
    odds_a = [e["price"] for e in entries_a]
    odds_b = [e["price"] for e in entries_b]

    # distinct books counts matter
    books_a = len({e["book"] for e in entries_a})
    books_b = len({e["book"] for e in entries_b})

    if books_a < min_books or books_b < min_books:
        return []

    med_a = median(odds_a)
    med_b = median(odds_b)
    if med_a is None or med_b is None:
        return []

    fair_a, fair_b = compute_no_vig_fair_probs(med_a, med_b)

    best_all_a = best_price(entries_a)
    best_all_b = best_price(entries_b)
    if best_all_a is None or best_all_b is None:
        return []

    best_fr_a = best_fr_price(entries_a)
    best_fr_b = best_fr_price(entries_b)

    # choose displayed best (FR pref)
    chosen_a = best_fr_a if (prefer_fr and best_fr_a) else best_all_a
    chosen_b = best_fr_b if (prefer_fr and best_fr_b) else best_all_b

    # edge_real = fair - implied(best)
    edge_a = fair_a - implied_prob(chosen_a["price"])
    edge_b = fair_b - implied_prob(chosen_b["price"])

    # dev vs median = (best - median)/median
    dev_a = (chosen_a["price"] - med_a) / med_a if med_a > 0 else 0.0
    dev_b = (chosen_b["price"] - med_b) / med_b if med_b > 0 else 0.0

    candidates = []
    # candidate A
    if edge_a >= edge_threshold and dev_a >= dev_threshold:
        candidates.append(
            {
                "match": match,
                "market": market_label,
                "line": line,
                "selection": outcome_a,
                "odds": chosen_a["price"],
                "book": chosen_a["book"],
                "best_is_fr": bool(chosen_a.get("is_fr")),
                "fr_best": best_fr_a["price"] if best_fr_a else None,
                "fr_best_book": best_fr_a["book"] if best_fr_a else None,
                "fair_prob": fair_a,
                "edge": edge_a,
                "dev": dev_a,
                "median_odds": med_a,
                "books_used": min(books_a, books_b),
                "score": None,  # filled later
            }
        )
    # candidate B
    if edge_b >= edge_threshold and dev_b >= dev_threshold:
        candidates.append(
            {
                "match": match,
                "market": market_label,
                "line": line,
                "selection": outcome_b,
                "odds": chosen_b["price"],
                "book": chosen_b["book"],
                "best_is_fr": bool(chosen_b.get("is_fr")),
                "fr_best": best_fr_b["price"] if best_fr_b else None,
                "fr_best_book": best_fr_b["book"] if best_fr_b else None,
                "fair_prob": fair_b,
                "edge": edge_b,
                "dev": dev_b,
                "median_odds": med_b,
                "books_used": min(books_a, books_b),
                "score": None,  # filled later
            }
        )

    # fill score
    for c in candidates:
        c["score"] = score_bet(c["edge"], c["dev"], int(c["books_used"]), market_label)

    return candidates


def diversify_team_picks(
    picks: List[Dict[str, Any]],
    max_picks: int,
    max_ml: int = 2,
    one_pick_per_match: bool = True,
) -> List[Dict[str, Any]]:
    out = []
    used_matches = set()
    ml_count = 0

    for p in sorted(picks, key=lambda x: (x["score"], x["edge"], x["dev"]), reverse=True):
        if len(out) >= max_picks:
            break
        if one_pick_per_match and p["match"] in used_matches:
            continue
        if p["market"] == "MONEYLINE" and ml_count >= max_ml:
            continue

        out.append(p)
        used_matches.add(p["match"])
        if p["market"] == "MONEYLINE":
            ml_count += 1

    return out


def diversify_prop_picks(
    picks: List[Dict[str, Any]],
    max_picks: int,
    one_pick_per_match: bool = True,
    one_pick_per_player: bool = True,
) -> List[Dict[str, Any]]:
    out = []
    used_matches = set()
    used_players = set()

    for p in sorted(picks, key=lambda x: (x["score"], x["edge"], x["dev"]), reverse=True):
        if len(out) >= max_picks:
            break

        if one_pick_per_match and p["match"] in used_matches:
            continue

        player = p.get("player")
        if one_pick_per_player and player:
            if player in used_players:
                continue

        out.append(p)
        used_matches.add(p["match"])
        if player:
            used_players.add(player)

    return out


def allocate_stakes_fixed_splits(total_budget: float, n: int) -> List[float]:
    """
    Stable stake splits:
      1 -> 100%
      2 -> 60/40
      3 -> 40/35/25
    Rounded cents, sum <= budget.
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

    # fix rounding drift
    while sum(stakes) - total_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes
