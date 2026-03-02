from typing import Any, Dict, List, Optional


def _pct(x: float) -> str:
    return f"{x*100:.2f}%"


def _fmt_money(x: float) -> str:
    return f"{x:.2f}€"


def _maybe(v: Optional[float], fmt: str = "{:.2f}") -> str:
    if v is None:
        return "n/a"
    try:
        return fmt.format(float(v))
    except Exception:
        return "n/a"


def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    market = p.get("market", "TEAM")
    selection = p.get("selection", "")
    match = p.get("match", "")
    line = p.get("line")

    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")
    best_is_fr = bool(p.get("best_is_fr", False))

    fr_best = p.get("fr_best")
    fr_best_book = p.get("fr_best_book")
    median_odds = p.get("median_odds")
    books_used = p.get("books_used")

    fair_prob = float(p.get("fair_prob", 0.0))
    edge = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair_prob * odds - 1.0))
    score = float(p.get("score", 0.0))

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0

    if best_is_fr:
        best_line = f"**Best (FR):** {odds:.2f} (**{book}**)"
    else:
        if fr_best is not None:
            best_line = (
                f"**Best (pref FR):** {odds:.2f} (**{book}**) — ⚠️ best non-FR\n"
                f"**FR best:** {_maybe(fr_best)} ({fr_best_book})"
            )
        else:
            best_line = f"**Best:** {odds:.2f} (**{book}**) — ⚠️ FR indispo"

    ctx_lines: List[str] = []
    if p.get("injury_note"):
        ctx_lines.append(f"**Injuries:** {p['injury_note']}")
    if p.get("minutes_note"):
        ctx_lines.append(f"**Minutes proj.:** {p['minutes_note']}")
    ctx = ("\n" + "\n".join(ctx_lines)) if ctx_lines else ""

    return (
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        + (f"**Line:** {line}\n" if line is not None else "")
        + f"**Sélection:** {selection}\n"
        f"{best_line}\n"
        + (f"**Books utilisés (médiane):** {books_used} | **Cote médiane:** {_maybe(median_odds)}\n"
           if (books_used and median_odds is not None) else "")
        + f"**p_fair (no-vig):** {_pct(fair_prob)} | **EV:** {_pct(ev)}\n"
        + f"**Edge:** {_pct(edge)} | **Dev:** {_pct(dev)} | **Score:** {score:.0f}/100\n"
        + f"**Mise:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)})\n"
        + f"**Budget jour:** {_fmt_money(daily_budget)} | **Utilisé après bet:** {_fmt_money(spent_after)}"
        + ctx
    )


def format_prop_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "PROP")
    player = p.get("player", "")
    side = p.get("selection", "")
    line = p.get("line")

    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")

    fair_prob = float(p.get("fair_prob", 0.0))
    edge = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair_prob * odds - 1.0))
    score = float(p.get("score", 0.0))

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0

    ctx_lines: List[str] = []
    if p.get("injury_note"):
        ctx_lines.append(f"**Injuries:** {p['injury_note']}")
    if p.get("minutes_note"):
        ctx_lines.append(f"**Minutes proj.:** {p['minutes_note']}")
    ctx = ("\n" + "\n".join(ctx_lines)) if ctx_lines else ""

    sel = f"{player} — {side} {line}" if line is not None else f"{player} — {side}"

    return (
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        f"**Sélection:** {sel}\n"
        f"**Best:** {odds:.2f} (**{book}**)\n"
        f"**p_fair (no-vig):** {_pct(fair_prob)} | **EV:** {_pct(ev)}\n"
        f"**Edge:** {_pct(edge)} | **Dev:** {_pct(dev)} | **Score:** {score:.0f}/100\n"
        f"**Mise:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)})\n"
        f"**Budget jour:** {_fmt_money(daily_budget)} | **Utilisé après bet:** {_fmt_money(spent_after)}"
        + ctx
    )


def format_no_bet(
    title: str,
    reason: str,
    regions_used: List[str],
    games_analyzed: int,
    markets_tested: int,
    top_rejects: List[str],
    near_miss_lines: List[str],
    daily_budget: float,
    daily_spent: float,
) -> str:
    regions_txt = ", ".join([r for r in regions_used if r]) if regions_used else "n/a"
    rejects_block = "\n".join([f"• {x}" for x in top_rejects]) if top_rejects else "• (aucune donnée)"
    near_block = "\n".join(near_miss_lines) if near_miss_lines else "Aucun near-miss."

    return (
        f"**{title}**\n"
        f"Raison: {reason}\n\n"
        f"**Résumé analyse**\n"
        f"• Regions utilisées: {regions_txt}\n"
        f"• Matchs analysés: {games_analyzed}\n"
        f"• Marchés testés (2-way & >=2 books): {markets_tested}\n\n"
        f"**Refus principaux**\n{rejects_block}\n\n"
        f"**Near miss (Top 5)**\n{near_block}\n\n"
        f"Budget jour: **{_fmt_money(daily_budget)}** | Déjà utilisé: **{_fmt_money(daily_spent)}**"
    )
