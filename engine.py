import math
from typing import Any, Dict, List, Optional, Tuple

FR_BOOK_KEYWORDS = [
    "betclic", "winamax", "parions", "pmu", "unibet (fr)", "unibet fr", "zebet",
    "bwin fr", "pokerstars", "vbet fr", "france"
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
    return max(entries, key=lambda x: float(x.get("price") or 0.0))


def best_fr_price(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    frs = [e for e in entries if e.get("is_fr")]
    return best_price(frs) if frs else None


def score_bet(edge_real: float, dev: float, books_used: int, market_type: str) -> float:
    edge_pts = max(0.0, min(1.0, edge_real / 0.08)) * 60.0
    dev_pts = max(0.0, min(1.0, dev / 0.15)) * 20.0
    book_pts = max(0.0, min(1.0, (books_used - 2) / 8.0)) * 15.0
    bonus = 5.0 if str(market_type).startswith("PROP") else 0.0
    return max(0.0, min(100.0, edge_pts + dev_pts + book_pts + bonus))


def collect_market_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    For h2h/spreads/totals:
      lines[line_key][outcome_key] = [ {price, book, is_fr, point} ... ]
    line_key:
      h2h -> "h2h"
      totals -> f"{point}"
      spreads -> f"{abs(point)}"
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
                    # fallback
                    line_key = f"{point}" if point is not None else "NA"
                    outcome_key = str(name)

                lines.setdefault(line_key, {}).setdefault(outcome_key, []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "lines": lines}


def collect_player_prop_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Props outcomes often:
      name: Over/Under
      description: player
      point: line
      price: odds
    Build:
      props[player][line_key][Over/Under] = [entry...]
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

                if side is None or player is None or price is None or point is None:
                    continue

                player = str(player).strip()
                if not player:
                    continue

                line_key = f"{point}"
                props.setdefault(player, {}).setdefault(line_key, {}).setdefault(str(side), []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "props": props}


def collect_team_totals_lines(bookmakers: List[Dict[str, Any]], market_key: str) -> Dict[str, Any]:
    """
    Team totals (if exposed) are like props, but description is team name.
    Build:
      teams[team][line_key][Over/Under] = [entry...]
    """
    teams: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []) or []:
            if m.get("key") != market_key:
                continue

            for o in m.get("outcomes", []) or []:
                side = o.get("name")  # Over/Under
                team = o.get("description") or o.get("participant") or o.get("team")
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))

                if side is None or team is None or price is None or point is None:
                    continue

                team = str(team).strip()
                if not team:
                    continue

                line_key = f"{point}"
                teams.setdefault(team, {}).setdefault(line_key, {}).setdefault(str(side), []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "teams": teams}


def pick_consensus_line(lines_dict: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
    if not lines_dict:
        return None
    best_key, best_count = None, -1
    for lk, outcomes in lines_dict.items():
        cnt = sum(len(v) for v in outcomes.values())
        if cnt > best_count:
            best_key, best_count = lk, cnt
    return best_key


def pick_consensus_prop_line(lines_dict: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Optional[str]:
    return pick_consensus_line(lines_dict)


def _apply_haircut(fair_prob_raw: float, implied_best: float, haircut_pct: float) -> float:
    """
    Shrink fair prob toward market implied to reduce overconfidence.
    haircut_pct=0.30 => 30% shrink toward implied.
    """
    h = max(0.0, min(1.0, haircut_pct))
    return (1.0 - h) * fair_prob_raw + h * implied_best


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
    tier: str = "STRICT",
    haircut_edge_threshold: float = 0.06,
    haircut_pct: float = 0.30,
    return_all: bool = False,
) -> Any:
    """
    Returns:
      if return_all=False -> passed list
      if return_all=True  -> {"passed":[...], "all":[...], "rejects":{...}}
    """
    rejects: Dict[str, int] = {}

    def rej(key: str, n: int = 1):
        rejects[key] = rejects.get(key, 0) + int(n)

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

    best_all_a = best_price(entries_a)
    best_all_b = best_price(entries_b)
    if best_all_a is None or best_all_b is None:
        rej("best_missing")
        return {"passed": [], "all": [], "rejects": rejects} if return_all else []

    best_fr_a = best_fr_price(entries_a)
    best_fr_b = best_fr_price(entries_b)

    chosen_a = best_fr_a if (prefer_fr and best_fr_a) else best_all_a
    chosen_b = best_fr_b if (prefer_fr and best_fr_b) else best_all_b

    imp_a = implied_prob(chosen_a.get("price"))
    imp_b = implied_prob(chosen_b.get("price"))

    edge_a_raw = fair_a_raw - imp_a
    edge_b_raw = fair_b_raw - imp_b

    # haircut if raw edge is big
    haircut_applied_a = edge_a_raw > haircut_edge_threshold
    haircut_applied_b = edge_b_raw > haircut_edge_threshold

    fair_a = _apply_haircut(fair_a_raw, imp_a, haircut_pct) if haircut_applied_a else fair_a_raw
    fair_b = _apply_haircut(fair_b_raw, imp_b, haircut_pct) if haircut_applied_b else fair_b_raw

    edge_a = fair_a - imp_a
    edge_b = fair_b - imp_b

    dev_a = (float(chosen_a["price"]) - float(med_a)) / float(med_a) if med_a > 0 else 0.0
    dev_b = (float(chosen_b["price"]) - float(med_b)) / float(med_b) if med_b > 0 else 0.0

    ev_a = fair_a * float(chosen_a["price"]) - 1.0
    ev_b = fair_b * float(chosen_b["price"]) - 1.0

    books_used = min(books_a, books_b)

    all_items: List[Dict[str, Any]] = []

    def make_item(outcome: str, chosen: Dict[str, Any], best_fr: Optional[Dict[str, Any]],
                  fair_raw: float, fair_adj: float, edge_raw: float, edge_adj: float,
                  dev: float, med: float, ev: float, haircut_applied: bool) -> Dict[str, Any]:
        it = {
            "match": match,
            "market": market_label,
            "line": line,
            "selection": outcome,
            "odds": float(chosen["price"]),
            "book": str(chosen.get("book", "Unknown")),
            "best_is_fr": bool(chosen.get("is_fr")),
            "fr_best": float(best_fr["price"]) if best_fr else None,
            "fr_best_book": str(best_fr.get("book")) if best_fr else None,
            "median_odds": float(med),
            "books_used": int(books_used),
            "total_books": int(total_books),
            "fair_prob_raw": float(fair_raw),
            "fair_prob": float(fair_adj),
            "edge_raw": float(edge_raw),
            "edge": float(edge_adj),
            "dev": float(dev),
            "ev": float(ev),
            "tier": tier,
            "haircut_applied": bool(haircut_applied),
        }
        it["score"] = score_bet(it["edge"], it["dev"], it["books_used"], market_label)
        it["passed"] = bool(it["edge"] >= edge_threshold and it["dev"] >= dev_threshold)
        return it

    all_items.append(make_item(outcome_a, chosen_a, best_fr_a, fair_a_raw, fair_a, edge_a_raw, edge_a, dev_a, med_a, ev_a, haircut_applied_a))
    all_items.append(make_item(outcome_b, chosen_b, best_fr_b, fair_b_raw, fair_b, edge_b_raw, edge_b, dev_b, med_b, ev_b, haircut_applied_b))

    for it in all_items:
        if it["edge"] < edge_threshold:
            rej("edge<th")
        if it["dev"] < dev_threshold:
            rej("dev<th")

    passed = [it for it in all_items if it["passed"]]

    return {"passed": passed, "all": all_items, "rejects": rejects} if return_all else passed


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
    Legacy: 1->100%, 2->60/40, 3->40/35/25
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


def allocate_stakes_capped(total_budget: float, n: int, max_single_share: float = 0.45) -> List[float]:
    """
    Prevents 1 pick from eating 100% of the day budget.
    If n==1 -> stake = min(max_single_share,1)*budget, rest unused.
    Else uses fixed splits then caps any stake > max_single_share*budget and redistributes if possible.
    """
    if n <= 0 or total_budget <= 0:
        return []
    cap = max(0.05, min(1.0, float(max_single_share)))

    if n == 1:
        return [round(total_budget * cap, 2)]

    stakes = allocate_stakes_fixed_splits(total_budget, n)
    cap_val = total_budget * cap

    # cap
    over = 0.0
    for i in range(len(stakes)):
        if stakes[i] > cap_val:
            over += stakes[i] - cap_val
            stakes[i] = round(cap_val, 2)

    # redistribute overflow to others under cap
    if over > 0:
        for i in range(len(stakes)):
            room = cap_val - stakes[i]
            if room <= 0:
                continue
            add = min(room, over)
            stakes[i] = round(stakes[i] + add, 2)
            over -= add
            if over <= 0:
                break

    # if still over, we just leave budget unused (safe)
    while sum(stakes) - total_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes
