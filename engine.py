import math
from typing import Any, Dict, List, Optional, Tuple

FR_BOOK_KEYWORDS = [
    "betclic", "winamax", "parions", "pmu", "unibet (fr)", "unibet fr",
    "zebet", "bwin fr", "pokerstars", "vbet fr"
]

def is_fr_book(book_title: str) -> bool:
    t = (book_title or "").strip().lower()
    return any(k in t for k in FR_BOOK_KEYWORDS)

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
    return max(entries, key=lambda x: x.get("price", 0.0))

def best_fr_price(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    frs = [e for e in entries if e.get("is_fr")]
    return best_price(frs) if frs else None

def score_pick(edge: float, dev: float, books_used: int, market_label: str, is_ml: bool) -> float:
    """
    Score 0-100 (ranking only, no thresholds).
    - edge dominates
    - dev confirms mispricing vs consensus
    - books_used adds robustness
    - small props bonus
    - ML gets a mild penalty (we prefer spread/total/1H if similar)
    """
    edge_pts = max(0.0, min(1.0, edge / 0.08)) * 60.0
    dev_pts  = max(0.0, min(1.0, dev  / 0.15)) * 20.0
    book_pts = max(0.0, min(1.0, (books_used - 2) / 8.0)) * 15.0
    bonus = 5.0 if str(market_label).startswith("PROP") else 0.0
    ml_penalty = 6.0 if is_ml else 0.0
    return max(0.0, min(100.0, edge_pts + dev_pts + book_pts + bonus - ml_penalty))

def collect_market_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Output:
      {"market": market_key, "lines": {line_key: {outcome_key: [entry...]}}}
    entry = {"price": float, "book": str, "is_fr": bool, "point": float|None}
    """
    lines: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []) or []:
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []) or []:
                name = o.get("name")
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))

                if name is None or price is None:
                    continue

                if market_key in ("h2h", "h2h_h1"):
                    line_key = "h2h"
                    outcome_key = str(name)
                elif market_key in ("totals", "totals_h1"):
                    if point is None:
                        continue
                    line_key = f"{point}"
                    outcome_key = str(name)  # Over/Under
                elif market_key in ("spreads", "spreads_h1"):
                    if point is None:
                        continue
                    # KEEP SIGNED LINE to avoid collisions (+11.5 vs -11.5)
                    line_key = f"{point}"
                    outcome_key = str(name)  # team
                else:
                    # unknown key: keep a stable bucket
                    line_key = f"{point}" if point is not None else "NA"
                    outcome_key = str(name)

                lines.setdefault(line_key, {}).setdefault(outcome_key, []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "lines": lines}

def collect_team_totals_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    OddsAPI team_totals / team_totals_h1:
      name: Over/Under
      description: team
      point: line
    Output:
      {"market": market_key, "teams": {team: {line_key: {"Over":[...],"Under":[...]}}}}
    """
    teams: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []) or []:
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []) or []:
                side = o.get("name")
                team = o.get("description") or o.get("team") or o.get("participant")
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))

                if not side or not team or price is None or point is None:
                    continue

                team = str(team).strip()
                if not team:
                    continue

                line_key = f"{point}"
                teams.setdefault(team, {}).setdefault(line_key, {}).setdefault(str(side), []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": float(point)}
                )

    return {"market": market_key, "teams": teams}

def collect_player_prop_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    OddsAPI player props:
      name: "Over"/"Under"
      description: player name
      point: line
      price: odds
    Output:
      {"market": market_key, "props": {player: {line_key: {"Over":[...],"Under":[...]}}}}
    """
    props: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []) or []:
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []) or []:
                side = o.get("name")
                player = o.get("description") or o.get("participant") or o.get("player")
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))

                if not side or not player or price is None or point is None:
                    continue

                player = str(player).strip()
                if not player:
                    continue

                line_key = f"{point}"
                props.setdefault(player, {}).setdefault(line_key, {}).setdefault(str(side), []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": float(point)}
                )

    return {"market": market_key, "props": props}

def pick_consensus_line(lines_dict: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
    """
    Pick the line with the most total quotes.
    """
    if not lines_dict:
        return None
    best_key, best_count = None, -1
    for lk, outcomes in lines_dict.items():
        cnt = sum(len(v) for v in outcomes.values())
        if cnt > best_count:
            best_key, best_count = lk, cnt
    return best_key

def pick_consensus_prop_line(player_lines: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
    return pick_consensus_line(player_lines)

def analyze_two_way_market(
    match: str,
    market_label: str,
    line: Optional[float],
    outcome_a: str,
    outcome_b: str,
    entries_a: List[Dict[str, Any]],
    entries_b: List[Dict[str, Any]],
    *,
    min_books: int,
    prefer_fr: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Returns a single best candidate for side A and side B (we will rank globally later),
    but ONLY if the market is well-formed (2-way, min books, medians exist).
    Candidate includes:
      fair_prob (no-vig median), edge, dev, EV, score
    """
    total_books = len({e.get("book") for e in (entries_a + entries_b) if e.get("book")})
    if total_books < min_books:
        return None

    med_a = median([e.get("price") for e in entries_a])
    med_b = median([e.get("price") for e in entries_b])
    if med_a is None or med_b is None:
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

    def build(outcome: str, chosen: Dict[str, Any], fair_prob: float, med: float, best_fr: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        odds = float(chosen["price"])
        p_imp = implied_prob(odds)
        edge = float(fair_prob) - float(p_imp)
        dev = (odds - float(med)) / float(med) if med > 0 else 0.0
        ev = float(fair_prob) * odds - 1.0
        books_used = min(len({e.get("book") for e in entries_a}), len({e.get("book") for e in entries_b}))
        is_ml = (market_label == "MONEYLINE" or market_label == "MONEYLINE 1H")
        sc = score_pick(edge=edge, dev=dev, books_used=int(books_used), market_label=market_label, is_ml=is_ml)
        return {
            "match": match,
            "market": market_label,
            "line": line,
            "selection": outcome,
            "odds": odds,
            "book": str(chosen.get("book", "Unknown")),
            "best_is_fr": bool(chosen.get("is_fr")),
            "fr_best": float(best_fr["price"]) if best_fr else None,
            "fr_best_book": str(best_fr.get("book")) if best_fr else None,
            "median_odds": float(med),
            "total_books": int(total_books),
            "books_used": int(books_used),
            "fair_prob": float(fair_prob),
            "edge": float(edge),
            "dev": float(dev),
            "ev": float(ev),
            "score": float(sc),
        }

    # Return both sides as separate candidates (caller will add them)
    return {
        "A": build(outcome_a, chosen_a, fair_a, float(med_a), best_fr_a),
        "B": build(outcome_b, chosen_b, fair_b, float(med_b), best_fr_b),
    }
