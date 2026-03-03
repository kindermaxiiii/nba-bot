import math
from typing import Any, Dict, List, Optional, Tuple

FR_BOOK_KEYWORDS = [
    "betclic", "winamax", "parions", "pmu", "unibet (fr)", "unibet fr",
    "zebet", "bwin fr", "pokerstars", "vbet fr", "france"
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
                    line_key = f"{abs(point)}"
                    outcome_key = str(name)  # team
                else:
                    # generic
                    line_key = f"{point}" if point is not None else "NA"
                    outcome_key = str(name)

                lines.setdefault(line_key, {}).setdefault(outcome_key, []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "lines": lines}


def collect_team_totals_lines(bookmakers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    OddsAPI team_totals:
      name: Over/Under
      description: team name
      point: line
    Output:
      {"market":"team_totals","lines": { "TEAM|POINT": {"Over":[...], "Under":[...] } } }
    """
    lines: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for b in bookmakers or []:
        book = b.get("title", "UnknownBook")
        fr = is_fr_book(book)

        for m in b.get("markets", []) or []:
            if m.get("key") != "team_totals":
                continue

            for o in m.get("outcomes", []) or []:
                side = o.get("name")  # Over/Under
                price = safe_float(o.get("price"))
                point = safe_float(o.get("point"))
                team = o.get("description") or o.get("team") or o.get("participant")

                if not side or price is None or point is None or not team:
                    continue

                lk = f"{team}|{float(point)}"
                lines.setdefault(lk, {}).setdefault(str(side), []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": float(point)}
                )

    return {"market": "team_totals", "lines": lines}


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

                lk = f"{float(point)}"
                props.setdefault(player, {}).setdefault(lk, {}).setdefault(str(side), []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": float(point)}
                )

    return {"market": market_key, "props": props}


def analyze_two_way_market(
    match: str,
    market_label: str,
    line: Optional[float],
    outcome_a: str,
    outcome_b: str,
    entries_a: List[Dict[str, Any]],
    entries_b: List[Dict[str, Any]],
    min_books: int = 2,
    prefer_fr: bool = True,
    # NEW: pass p_real per side (already blended). If None -> fallback to p_mkt (no-vig)
    p_real_a: Optional[float] = None,
    p_real_b: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Returns 2 dicts (one per side) with:
      - fair_prob_raw = p_mkt (no-vig median)
      - fair_prob = p_real (model blend)
      - edge_raw = p_mkt - p_imp
      - edge = p_real - p_imp
      - ev = p_real*odds - 1
    """
    books_a = len({e.get("book") for e in entries_a if e.get("book")})
    books_b = len({e.get("book") for e in entries_b if e.get("book")})
    total_books = len({e.get("book") for e in (entries_a + entries_b) if e.get("book")})

    if total_books < min_books or books_a < 1 or books_b < 1:
        return []

    med_a = median([e.get("price") for e in entries_a])
    med_b = median([e.get("price") for e in entries_b])
    if med_a is None or med_b is None:
        return []

    p_mkt_a, p_mkt_b = compute_no_vig_fair_probs(med_a, med_b)

    best_all_a = best_price(entries_a)
    best_all_b = best_price(entries_b)
    if best_all_a is None or best_all_b is None:
        return []

    best_fr_a = best_fr_price(entries_a)
    best_fr_b = best_fr_price(entries_b)
    chosen_a = best_fr_a if (prefer_fr and best_fr_a) else best_all_a
    chosen_b = best_fr_b if (prefer_fr and best_fr_b) else best_all_b

    dev_a = (float(chosen_a["price"]) - float(med_a)) / float(med_a) if med_a > 0 else 0.0
    dev_b = (float(chosen_b["price"]) - float(med_b)) / float(med_b) if med_b > 0 else 0.0

    # fallback: p_real = p_mkt if not provided
    p_real_a = float(p_real_a) if p_real_a is not None else float(p_mkt_a)
    p_real_b = float(p_real_b) if p_real_b is not None else float(p_mkt_b)

    # implied based on best odds
    p_imp_a = implied_prob(float(chosen_a["price"]))
    p_imp_b = implied_prob(float(chosen_b["price"]))

    edge_raw_a = float(p_mkt_a) - p_imp_a
    edge_raw_b = float(p_mkt_b) - p_imp_b
    edge_a = float(p_real_a) - p_imp_a
    edge_b = float(p_real_b) - p_imp_b

    ev_a = float(p_real_a) * float(chosen_a["price"]) - 1.0
    ev_b = float(p_real_b) * float(chosen_b["price"]) - 1.0

    books_used = int(min(books_a, books_b))

    out: List[Dict[str, Any]] = []
    for outcome, chosen, best_fr, p_mkt, p_real, edge_raw, edge, dev, med, ev in [
        (outcome_a, chosen_a, best_fr_a, float(p_mkt_a), float(p_real_a), float(edge_raw_a), float(edge_a), float(dev_a), float(med_a), float(ev_a)),
        (outcome_b, chosen_b, best_fr_b, float(p_mkt_b), float(p_real_b), float(edge_raw_b), float(edge_b), float(dev_b), float(med_b), float(ev_b)),
    ]:
        item = {
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
            "books_used": books_used,
            "total_books": total_books,
            "fair_prob_raw": p_mkt,   # marché no-vig
            "fair_prob": p_real,      # p_real = blend
            "edge_raw": edge_raw,
            "edge": edge,
            "dev": dev,
            "ev": ev,
            "haircut_applied": False,
        }
        item["score"] = score_bet(item["edge"], item["dev"], item["books_used"], market_label)
        item["score_adj"] = item["score"]  # compat formatting
        out.append(item)

    return out


def diversify_team_picks(
    picks: List[Dict[str, Any]],
    max_picks: int,
    max_ml: int = 2,
    one_pick_per_match: bool = True,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used_matches = set()
    ml_count = 0

    for p in sorted(picks, key=lambda x: (x.get("ev", 0), x.get("edge", 0), x.get("dev", 0), x.get("score_adj", 0)), reverse=True):
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
    one_pick_per_match: bool = False,
    one_pick_per_player: bool = True,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used_matches = set()
    used_players = set()

    for p in sorted(picks, key=lambda x: (x.get("ev", 0), x.get("edge", 0), x.get("dev", 0), x.get("score_adj", 0)), reverse=True):
        if len(out) >= max_picks:
            break
        if one_pick_per_match and p.get("match") in used_matches:
            continue

        player = p.get("player")
        if one_pick_per_player and player:
            if player in used_players:
                continue

        out.append(p)
        if one_pick_per_match:
            used_matches.add(p.get("match"))
        if player:
            used_players.add(player)

    return out


def allocate_stakes_capped(total_budget: float, n: int, max_single_share: float = 0.30) -> List[float]:
    """
    Gardé pour compat avec ton formatting.
    Si tu ne veux plus afficher les mises : tu pourras juste ignorer stake côté formatting.
    """
    if n <= 0 or total_budget <= 0:
        return []
    cap = max(0.05, min(1.0, float(max_single_share)))

    if n == 1:
        return [round(total_budget * cap, 2)]

    # splits simples (top1 > top2 > top3)
    if n == 2:
        splits = [0.60, 0.40]
    else:
        splits = [0.40, 0.35, 0.25]

    stakes = [round(total_budget * s, 2) for s in splits[:n]]
    cap_val = total_budget * cap

    over = 0.0
    for i in range(len(stakes)):
        if stakes[i] > cap_val:
            over += stakes[i] - cap_val
            stakes[i] = round(cap_val, 2)

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

    while sum(stakes) - total_budget > 0.001:
        i = max(range(len(stakes)), key=lambda k: stakes[k])
        stakes[i] = round(max(0.0, stakes[i] - 0.01), 2)

    return stakes
