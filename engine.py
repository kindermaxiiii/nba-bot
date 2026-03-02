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


def safe_float(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def compute_no_vig_fair_probs(median_odds_a: float, median_odds_b: float) -> Tuple[float, float]:
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
    # score 0-100 stable
    edge_pts = max(0.0, min(1.0, edge_real / 0.08)) * 60.0   # 8% edge -> 60pts
    dev_pts = max(0.0, min(1.0, dev / 0.15)) * 20.0         # 15% dev -> 20pts
    book_pts = max(0.0, min(1.0, (books_used - 2) / 8.0)) * 15.0  # 10 books -> 15pts
    bonus = 5.0 if market_type.startswith("PROP") else 0.0
    return max(0.0, min(100.0, edge_pts + dev_pts + book_pts + bonus))


def collect_market_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Returns:
      lines[line_key][outcome_key] = list(entries)
      entry = {price, book, is_fr, point}
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
                    line_key = f"{point}" if point is not None else "NA"
                    outcome_key = str(name)

                lines.setdefault(line_key, {}).setdefault(outcome_key, []).append(
                    {"price": price, "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "lines": lines}


def pick_consensus_line(lines_dict: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
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


def two_way_metrics(
    match: str,
    market_label: str,
    line: Optional[float],
    selection_a: str,
    selection_b: str,
    entries_a: List[Dict[str, Any]],
    entries_b: List[Dict[str, Any]],
    prefer_fr: bool,
    min_books_total: int,
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    Compute metrics for both sides (A/B) with no-vig fair prob from MEDIAN odds.
    min_books_total = min distinct books across BOTH sides (>=2 recommended).
    Requires at least 1 book per side.
    """
    if not entries_a or not entries_b:
        return None

    books_a = len({e["book"] for e in entries_a if e.get("book")})
    books_b = len({e["book"] for e in entries_b if e.get("book")})
    total_books = len({e["book"] for e in (entries_a + entries_b) if e.get("book")})

    if total_books < min_books_total:
        return None
    if books_a < 1 or books_b < 1:
        return None

    odds_a = [e["price"] for e in entries_a if e.get("price") is not None]
    odds_b = [e["price"] for e in entries_b if e.get("price") is not None]

    med_a = median(odds_a)
    med_b = median(odds_b)
    if med_a is None or med_b is None or med_a <= 0 or med_b <= 0:
        return None

    fair_a, fair_b = compute_no_vig_fair_probs(med_a, med_b)

    best_all_a = best_price(entries_a)
    best_all_b = best_price(entries_b)
    if best_all_a is None or best_all_b is None:
        return None

    best_fr_a = best_fr_price(entries_a)
    best_fr_b = best_fr_price(entries_b)

    chosen_a = best_fr_a if (prefer_fr and best_fr_a) else best_all_a
    chosen_b = best_fr_b if (prefer_fr and best_fr_b) else best_all_b

    edge_a = fair_a - implied_prob(chosen_a["price"])
    edge_b = fair_b - implied_prob(chosen_b["price"])

    dev_a = (chosen_a["price"] - med_a) / med_a
    dev_b = (chosen_b["price"] - med_b) / med_b

    base_a = {
        "match": match,
        "market": market_label,
        "line": line,
        "selection": selection_a,
        "odds": chosen_a["price"],
        "book": chosen_a["book"],
        "best_is_fr": bool(chosen_a.get("is_fr")),
        "fr_best": best_fr_a["price"] if best_fr_a else None,
        "fr_best_book": best_fr_a["book"] if best_fr_a else None,
        "median_odds": med_a,
        "books_used": int(total_books),
        "fair_prob": float(fair_a),
        "edge": float(edge_a),
        "dev": float(dev_a),
        "score": score_bet(float(edge_a), float(dev_a), int(total_books), market_label),
    }

    base_b = {
        "match": match,
        "market": market_label,
        "line": line,
        "selection": selection_b,
        "odds": chosen_b["price"],
        "book": chosen_b["book"],
        "best_is_fr": bool(chosen_b.get("is_fr")),
        "fr_best": best_fr_b["price"] if best_fr_b else None,
        "fr_best_book": best_fr_b["book"] if best_fr_b else None,
        "median_odds": med_b,
        "books_used": int(total_books),
        "fair_prob": float(fair_b),
        "edge": float(edge_b),
        "dev": float(dev_b),
        "score": score_bet(float(edge_b), float(dev_b), int(total_books), market_label),
    }

    return base_a, base_b


def diversify_team_picks(
    picks: List[Dict[str, Any]],
    max_picks: int,
    max_ml: int = 2,
    one_pick_per_match: bool = True,
) -> List[Dict[str, Any]]:
    # ✅ force 1 non-ML si possible
    sorted_picks = sorted(picks, key=lambda x: (x.get("score", 0), x.get("edge", 0), x.get("dev", 0)), reverse=True)

    out: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    # pick best non-ML first if exists
    best_non_ml = next((p for p in sorted_picks if p.get("market") in ["SPREAD", "TOTAL"]), None)
    if best_non_ml and max_picks > 0:
        out.append(best_non_ml)
        used_matches.add(best_non_ml["match"])
        if best_non_ml["market"] == "MONEYLINE":
            ml_count += 1

    for p in sorted_picks:
        if len(out) >= max_picks:
            break
        if one_pick_per_match and p["match"] in used_matches:
            continue
        if p.get("market") == "MONEYLINE" and ml_count >= max_ml:
            continue
        out.append(p)
        used_matches.add(p["match"])
        if p.get("market") == "MONEYLINE":
            ml_count += 1

    return out[:max_picks]


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
        if one_pick_per_match and p["match"] in used_matches:
            continue
        player = p.get("player")
        if one_pick_per_player and player and player in used_players:
            continue
        out.append(p)
        used_matches.add(p["match"])
        if player:
            used_players.add(player)
    return out


def allocate_stakes_fixed_splits(total_budget: float, n: int) -> List[float]:
    if n <= 0 or total_budget <= 0:
        return []
    if n == 1:
        splits = [1.0]
    elif n == 2:
        splits = [0.6, 0.4]
    else:
        splits = [0.4, 0.35, 0.25]

    stakes = [round(total_budget * s, 2) for s in splits[:n]]

    # rounding drift fix
    while sum(stakes) - total_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)
    return stakes
