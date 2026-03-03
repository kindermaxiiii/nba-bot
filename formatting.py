from typing import Any, Dict, List, Optional

def _pct(x: float) -> str:
    return f"{x*100:.2f}%"

def _maybe(v: Optional[float], fmt: str = "{:.2f}") -> str:
    if v is None:
        return "n/a"
    try:
        return fmt.format(float(v))
    except Exception:
        return "n/a"

def _bullet(lines: List[str]) -> str:
    return "\n".join([f"• {x}" for x in lines if x])

def _injury_minutes_block(p: Dict[str, Any]) -> str:
    parts = []
    if p.get("injury_note"):
        parts.append(f"**Injuries:** {p['injury_note']}")
    if p.get("minutes_note"):
        parts.append(f"**Minutes:** {p['minutes_note']}")
    if p.get("minutes_confidence") is not None:
        parts.append(f"**Minutes confidence:** {_pct(float(p['minutes_confidence']))}")
    if p.get("minutes_fragility") is not None:
        parts.append(f"**Minutes fragility:** {float(p['minutes_fragility']):.1f}/10")
    return "\n".join(parts)

def format_team_pick(p: Dict[str, Any], rank: int) -> str:
    match = p.get("match", "")
    market = p.get("market", "TEAM")
    selection = p.get("selection", "")
    line = p.get("line")

    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")
    median_odds = p.get("median_odds")
    books_used = p.get("books_used")
    total_books = p.get("total_books")

    fair = float(p.get("fair_prob", 0.0))
    edge = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair * odds - 1.0))
    score = float(p.get("score", 0.0))
    score_adj = float(p.get("score_adj", score))

    why = p.get("why", [])
    stats = p.get("stats_justif", [])

    return (
        f"**#{rank} — TEAM PICK**\n"
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        + (f"**Line:** {line}\n" if line is not None else "")
        + f"**Sélection:** {selection}\n"
        f"**Best:** {odds:.2f} ({book}) | **Médiane:** {_maybe(median_odds)} | **Books:** {books_used}/{total_books}\n"
        f"**p_fair:** {_pct(fair)} | **EV:** {_pct(ev)} | **Edge:** {_pct(edge)} | **Dev:** {_pct(dev)}\n"
        f"**Score_adj:** {score_adj:.1f}/100\n\n"
        f"**Pourquoi ce pick**\n{_bullet(list(why))}\n\n"
        f"**Justification statistique**\n{_bullet(list(stats))}\n\n"
        f"{_injury_minutes_block(p)}"
    ).strip()

def format_prop_pick(p: Dict[str, Any], rank: int) -> str:
    match = p.get("match", "")
    market = p.get("market", "PROP")
    player = p.get("player", "")
    side = p.get("selection", "")
    line = p.get("line")

    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")
    median_odds = p.get("median_odds")
    books_used = p.get("books_used")
    total_books = p.get("total_books")

    fair = float(p.get("fair_prob", 0.0))
    edge = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair * odds - 1.0))
    score = float(p.get("score", 0.0))
    score_adj = float(p.get("score_adj", score))

    why = p.get("why", [])
    stats = p.get("stats_justif", [])

    sel = f"{player} — {side} {line}" if line is not None else f"{player} — {side}"

    return (
        f"**#{rank} — PLAYER PROP**\n"
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        f"**Sélection:** {sel}\n"
        f"**Best:** {odds:.2f} ({book}) | **Médiane:** {_maybe(median_odds)} | **Books:** {books_used}/{total_books}\n"
        f"**p_fair:** {_pct(fair)} | **EV:** {_pct(ev)} | **Edge:** {_pct(edge)} | **Dev:** {_pct(dev)}\n"
        f"**Score_adj:** {score_adj:.1f}/100\n\n"
        f"**Pourquoi ce pick**\n{_bullet(list(why))}\n\n"
        f"**Justification statistique**\n{_bullet(list(stats))}\n\n"
        f"{_injury_minutes_block(p)}"
    ).strip()

def format_no_bet(title: str, reason: str, regions_used: List[str], games_analyzed: int, markets_tested: int, top_rejects: List[str], near_miss_lines: List[str]) -> str:
    regions_txt = ", ".join([r for r in regions_used if r]) if regions_used else "n/a"
    rejects_block = "\n".join([f"• {x}" for x in top_rejects]) if top_rejects else "• (aucune donnée)"
    near_block = "\n".join(near_miss_lines) if near_miss_lines else "Aucun near-miss."

    return (
        f"**{title}**\n"
        f"Raison: {reason}\n\n"
        f"**Résumé analyse**\n"
        f"• Regions utilisées: {regions_txt}\n"
        f"• Matchs analysés: {games_analyzed}\n"
        f"• Marchés testés: {markets_tested}\n\n"
        f"**Refus principaux**\n{rejects_block}\n\n"
        f"**Near miss (Top 5)**\n{near_block}\n"
    )
