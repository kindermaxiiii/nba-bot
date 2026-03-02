import math
from typing import Any, Dict, List, Optional, Tuple


# Heuristic FR book detection from book title
FR_BOOK_KEYWORDS = [
    "betclic", "winamax", "parions", "pmu", "unibet fr", "unibet (fr)", "zebet",
    "bwin fr", "pokerstars", "vbet fr", "france", "fr"
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
    """
    Two-way market:
      p_raw = 1/odds
      fair = p_raw / (p_raw_a + p_raw_b)
    """
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


def score_bet(edge_real: float, dev: float, books_used: int, market_type: str) -> float:
    """
    Stable 0-100 scoring.
    - edge drives
    - dev helps
    - more books helps
    - props small bonus
    """
    edge_pts = max(0.0, min(1.0, edge_real / 0.08)) * 60.0        # 8% edge => 60 pts
    dev_pts = max(0.0, min(1.0, dev / 0.15)) * 20.0              # 15% dev => 20 pts
    book_pts = max(0.0, min(1.0, (books_used - 2) / 8.0)) * 15.0 # 10 books => 15 pts
    bonus = 5.0 if str(market_type).startswith("PROP") else 0.0
    return max(0.0, min(100.0, edge_pts + dev_pts + book_pts + bonus))


def kelly_fraction(p: float, odds: float) -> float:
    """
    Kelly for decimal odds:
      b = odds-1
      f = (p*b - (1-p)) / b
    clipped to [0, 1]
    """
    if odds is None:
        return 0.0
    o = float(odds)
    if o <= 1.0:
        return 0.0
    b = o - 1.0
    num = (p * b) - (1.0 - p)
    f = num / b
    return max(0.0, min(1.0, f))


def classify_fragility(market: str, edge: float, dev: float, books_used: int) -> Tuple[str, str]:
    """
    Simple, transparent fragility/robustness tags.
    (Ce n'est pas "institutionnel complet", mais c'est stable et utile.)
    """
    # fragility drivers
    frag = 0
    if books_used <= 2:
        frag += 2
    if dev >= 0.10:
        frag += 1
    if dev >= 0.20:
        frag += 1
    if market.startswith("PROP"):
        frag += 1  # minutes variance
    if market in ("TOTAL",):
        frag += 1  # pace/variance
    if edge >= 0.06:
        frag += 1  # possible misprice/outlier (needs haircut)

    if frag >= 5:
        frag_tag = "HIGH"
        rob_tag = "LOW"
    elif frag >= 3:
        frag_tag = "MED"
        rob_tag = "MED"
    else:
        frag_tag = "LOW"
        rob_tag = "HIGH"
    return rob_tag, frag_tag


def collect_market_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Returns:
      {"market": market_key, "lines": {line_key: {outcome_key: [entry,...]}}}

    entry = {"price": float, "book": str, "is_fr": bool, "point": float|None}
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


def collect_player_prop_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    The Odds API player prop outcomes typically:
      name: "Over" / "Under"
      description: player name
      point: line
      price: odds

    Output:
      {"market": market_key, "props": {player: {line_key: {"Over":[...],"Under":[...]}}}}
    """
    props: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}

    for b in bookmakers:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []):
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
                    {"price": price, "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "props": props}


def pick_consensus_line(lines_dict: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
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
    edge_threshold: float,
    dev_threshold: float,
    min_books: int,
    prefer_fr: bool = True,
    return_all: bool = False,
) -> Any:
    """
    Two-way market analysis using no-vig fair probs from MEDIAN odds (both sides).

    FIX 2:
      - return_all=True returns ALL outcomes with pass flag + reject counters
      - caller can build Near-Miss from positives even if not passing thresholds
    """
    rejects: Dict[str, int] = {}

    def rej(key: str, n: int = 1):
        rejects[key] = rejects.get(key, 0) + n

    # distinct book counts
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

    fair_a, fair_b = compute_no_vig_fair_probs(med_a, med_b)

    best_all_a = best_price(entries_a)
    best_all_b = best_price(entries_b)
    if best_all_a is None or best_all_b is None:
        rej("best_missing")
        return {"passed": [], "all": [], "rejects": rejects} if return_all else []

    best_fr_a = best_fr_price(entries_a)
    best_fr_b = best_fr_price(entries_b)

    chosen_a = best_fr_a if (prefer_fr and best_fr_a) else best_all_a
    chosen_b = best_fr_b if (prefer_fr and best_fr_b) else best_all_b

    edge_a = fair_a - implied_prob(chosen_a.get("price"))
    edge_b = fair_b - implied_prob(chosen_b.get("price"))

    dev_a = (float(chosen_a["price"]) - med_a) / med_a if med_a > 0 else 0.0
    dev_b = (float(chosen_b["price"]) - med_b) / med_b if med_b > 0 else 0.0

    # EV per 1u stake: p_fair*odds - 1
    ev_a = fair_a * float(chosen_a["price"]) - 1.0
    ev_b = fair_b * float(chosen_b["price"]) - 1.0

    books_used = min(books_a, books_b)

    all_items: List[Dict[str, Any]] = []
    for outcome, chosen, best_fr, fair, edge, dev, med, ev in [
        (outcome_a, chosen_a, best_fr_a, fair_a, edge_a, dev_a, med_a, ev_a),
        (outcome_b, chosen_b, best_fr_b, fair_b, edge_b, dev_b, med_b, ev_b),
    ]:
        odds = float(chosen["price"])
        k = kelly_fraction(fair, odds)
        rob, frag = classify_fragility(str(market_label), float(edge), float(dev), int(books_used))

        item = {
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
            "books_used": int(books_used),
            "total_books": int(total_books),

            "p_fair": float(fair),
            "p_imp_best": float(implied_prob(odds)),
            "edge": float(edge),
            "dev": float(dev),
            "ev": float(ev),

            "kelly_full": float(k),          # Kelly brut
            "robustness": rob,               # HIGH/MED/LOW
            "fragility": frag,               # LOW/MED/HIGH
        }
        item["score"] = score_bet(item["edge"], item["dev"], item["books_used"], market_label)
        item["passed"] = bool(item["edge"] >= edge_threshold and item["dev"] >= dev_threshold)
        all_items.append(item)

    # reject accounting
    for it in all_items:
        if it["edge"] < edge_threshold:
            rej("edge<th")
        if it["dev"] < dev_threshold:
            rej("dev<th")

    passed = [it for it in all_items if it["passed"]]
    return {"passed": passed, "all": all_items, "rejects": rejects} if return_all else passed


# Backward compat alias
def analyze_market_two_way(*args, **kwargs):
    return analyze_two_way_market(*args, **kwargs)


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

    while sum(stakes) - total_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes
