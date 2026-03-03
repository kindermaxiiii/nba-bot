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


def score_bet(ev: float, dev: float, books_used: int, market_type: str) -> float:
    """
    Score orienté "top picks" :
      - EV (0..+20%) = poids principal
      - Dev (vs median) = liquidité / price quality
      - Books = profondeur
      - petit bonus props
    """
    ev_pts = max(0.0, min(1.0, ev / 0.20)) * 70.0
    dev_pts = max(0.0, min(1.0, dev / 0.10)) * 15.0
    book_pts = max(0.0, min(1.0, (books_used - 2) / 8.0)) * 10.0
    bonus = 5.0 if str(market_type).startswith("PROP") else 0.0
    return max(0.0, min(100.0, ev_pts + dev_pts + book_pts + bonus))


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
                elif market_key == "team_totals":
                    if point is None:
                        continue
                    team = o.get("description") or o.get("team") or o.get("participant")
                    if not team:
                        continue
                    line_key = f"{team}|{point}"
                    outcome_key = str(name)  # Over/Under
                else:
                    line_key = f"{point}" if point is not None else "NA"
                    outcome_key = str(name)

                lines.setdefault(line_key, {}).setdefault(outcome_key, []).append(
                    {"price": float(price), "book": book, "is_fr": fr, "point": point}
                )

    return {"market": market_key, "lines": lines}


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


def _apply_edge_haircut(
    fair_prob: float,
    best_odds: float,
    haircut: float = 0.25,
    edge_trigger: float = 0.08,
) -> Tuple[float, float, float]:
    """
    If edge_raw > edge_trigger:
      p_adj = p_imp + (p_fair - p_imp) * (1-haircut)
    Returns: (p_adj, edge_raw, edge_adj)
    """
    p_imp = implied_prob(best_odds)
    edge_raw = fair_prob - p_imp
    if edge_raw <= edge_trigger:
        return fair_prob, edge_raw, edge_raw

    p_adj = p_imp + (fair_prob - p_imp) * (1.0 - haircut)
    edge_adj = p_adj - p_imp
    return p_adj, edge_raw, edge_adj


def _blend_prob(p_model: Optional[float], p_mkt: float, blend: float) -> float:
    if p_model is None:
        return float(p_mkt)
    b = max(0.0, min(1.0, float(blend)))
    return (b * float(p_model)) + ((1.0 - b) * float(p_mkt))


def analyze_two_way_market(
    match: str,
    market_label: str,
    line: Optional[float],
    outcome_a: str,
    outcome_b: str,
    entries_a: List[Dict[str, Any]],
    entries_b: List[Dict[str, Any]],
    min_books: int,
    prefer_fr: bool = True,
    return_all: bool = False,
    p_model_a: Optional[float] = None,
    p_model_b: Optional[float] = None,
    model_blend: float = 0.65,
    haircut_edge_gt: float = 0.08,
    haircut_rate: float = 0.25,
) -> Any:
    """
    p_mkt = no-vig median odds (two-way)
    p_real = blend(p_model, p_mkt)
    EV = p_real * odds - 1
    """
    rejects: Dict[str, int] = {}

    def rej(k: str, n: int = 1):
        rejects[k] = rejects.get(k, 0) + int(n)

    total_books = len({e.get("book") for e in (entries_a + entries_b) if e.get("book")})
    books_a = len({e.get("book") for e in entries_a if e.get("book")})
    books_b = len({e.get("book") for e in entries_b if e.get("book")})

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

    # market no-vig fair
    p_mkt_a, p_mkt_b = compute_no_vig_fair_probs(med_a, med_b)

    best_all_a = best_price(entries_a)
    best_all_b = best_price(entries_b)
    if best_all_a is None or best_all_b is None:
        rej("best_missing")
        return {"passed": [], "all": [], "rejects": rejects} if return_all else []

    best_fr_a = best_fr_price(entries_a)
    best_fr_b = best_fr_price(entries_b)
    chosen_a = best_fr_a if (prefer_fr and best_fr_a) else best_all_a
    chosen_b = best_fr_b if (prefer_fr and best_fr_b) else best_all_b

    dev_a = (float(chosen_a["price"]) - float(med_a)) / float(med_a) if med_a > 0 else 0.0
    dev_b = (float(chosen_b["price"]) - float(med_b)) / float(med_b) if med_b > 0 else 0.0

    # BLEND model + market
    p_real_a_raw = _blend_prob(p_model_a, p_mkt_a, model_blend)
    p_real_b_raw = _blend_prob(p_model_b, p_mkt_b, model_blend)

    # Normalize (keep two-way coherent if both sides have model probs)
    s = p_real_a_raw + p_real_b_raw
    if s > 0:
        p_real_a_raw /= s
        p_real_b_raw /= s

    # haircut extreme edges
    p_adj_a, edge_raw_a, edge_adj_a = _apply_edge_haircut(
        fair_prob=float(p_real_a_raw),
        best_odds=float(chosen_a["price"]),
        haircut=haircut_rate,
        edge_trigger=haircut_edge_gt,
    )
    p_adj_b, edge_raw_b, edge_adj_b = _apply_edge_haircut(
        fair_prob=float(p_real_b_raw),
        best_odds=float(chosen_b["price"]),
        haircut=haircut_rate,
        edge_trigger=haircut_edge_gt,
    )

    ev_a = p_adj_a * float(chosen_a["price"]) - 1.0
    ev_b = p_adj_b * float(chosen_b["price"]) - 1.0

    books_used = min(books_a, books_b)

    all_items: List[Dict[str, Any]] = []
    for outcome, chosen, best_fr, p_mkt, p_raw, p_adj, edge_raw, edge_adj, dev, med, ev in [
        (outcome_a, chosen_a, best_fr_a, float(p_mkt_a), float(p_real_a_raw), float(p_adj_a), float(edge_raw_a), float(edge_adj_a), float(dev_a), float(med_a), float(ev_a)),
        (outcome_b, chosen_b, best_fr_b, float(p_mkt_b), float(p_real_b_raw), float(p_adj_b), float(edge_raw_b), float(edge_adj_b), float(dev_b), float(med_b), float(ev_b)),
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
            "books_used": int(books_used),
            "total_books": int(total_books),
            "p_mkt": float(p_mkt),
            "p_real_raw": float(p_raw),
            "p_real": float(p_adj),
            "edge_raw": float(edge_raw),
            "edge": float(edge_adj),
            "dev": float(dev),
            "ev": float(ev),
            "haircut_applied": bool(edge_raw > haircut_edge_gt),
        }
        item["score"] = score_bet(item["ev"], item["dev"], item["books_used"], market_label)
        all_items.append(item)

    # "passed" = EV>=0 (pour éviter NO BET artificiels tu prends TOP même si EV négatif plus tard)
    passed = [it for it in all_items if it.get("ev", -999) >= 0.0]
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

    for p in sorted(picks, key=lambda x: (x.get("score", 0), x.get("ev", 0), x.get("dev", 0)), reverse=True):
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

    for p in sorted(picks, key=lambda x: (x.get("score", 0), x.get("ev", 0), x.get("dev", 0)), reverse=True):
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


def allocate_stakes_equal(total_budget: float, n: int) -> List[float]:
    if n <= 0 or total_budget <= 0:
        return []
    stake = round(total_budget / n, 2)
    stakes = [stake for _ in range(n)]
    # fix rounding
    diff = round(total_budget - sum(stakes), 2)
    if stakes and abs(diff) >= 0.01:
        stakes[0] = round(stakes[0] + diff, 2)
    return stakes
