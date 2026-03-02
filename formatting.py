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


def _clv_block(p: Dict[str, Any]) -> str:
    snaps = p.get("clv_snapshots") or []
    if not snaps:
        return ""
    lines = []
    for s in snaps[-4:]:
        tag = s.get("tag", "?")
        odds = s.get("odds")
        book = s.get("book", "")
        dt = s.get("ts_utc", "")
        lines.append(f"• {tag}: {odds:.2f} ({book}) @ {dt}" if odds else f"• {tag}: n/a @ {dt}")
    return "\n**CLV snapshots**\n" + "\n".join(lines)


def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "TEAM")
    selection = p.get("selection", "")
    line = p.get("line")
    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")

    best_is_fr = bool(p.get("best_is_fr", False))
    fr_best = p.get("fr_best")
    fr_best_book = p.get("fr_best_book")
    median_odds = p.get("median_odds")
    books_used = p.get("books_used")
    total_books = p.get("total_books")

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)))
    fair_adj = float(p.get("fair_prob", 0.0))
    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)))
    edge_adj = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair_adj * odds - 1.0))
    score = float(p.get("score", 0.0))
    tier = p.get("tier", "STRICT")
    haircut = bool(p.get("haircut_applied", False))

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0
    pct_day = (stake / daily_budget) if daily_budget > 0 else 0.0

    if best_is_fr:
        best_line = f"**Meilleure cote (FR):** {odds:.2f} (**{book}**) ✅"
    else:
        if fr_best is not None:
            best_line = (
                f"**Meilleure cote:** {odds:.2f} (**{book}**) — ⚠️ best non-FR\n"
                f"**FR best:** {_maybe(fr_best)} ({fr_best_book})"
            )
        else:
            best_line = f"**Meilleure cote:** {odds:.2f} (**{book}**) — ⚠️ best non-FR (FR indispo)"

    injury_note = p.get("injury_note")
    minutes_note = p.get("minutes_note")

    ctx = []
    if injury_note:
        ctx.append(f"**Injuries:** {injury_note}")
    if minutes_note:
        ctx.append(f"**Minutes proj.:** {minutes_note}")

    ctx_block = ("\n" + "\n".join(ctx)) if ctx else ""
    clv = _clv_block(p)

    return (
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        + (f"**Line:** {line}\n" if line is not None else "")
        + f"**Sélection:** {selection}\n"
        f"{best_line}\n"
        + (f"**Books:** {books_used} (médiane) | **Total books:** {total_books} | **Cote médiane:** {_maybe(median_odds)}\n"
           if books_used and total_books and median_odds else "")
        + f"**Tier:** {tier} {'(haircut)' if haircut else ''}\n"
        + f"**p_fair:** {_pct(fair_adj)} (raw {_pct(fair_raw)})\n"
        + f"**EV:** {_pct(ev)} | **Edge:** {_pct(edge_adj)} (raw {_pct(edge_raw)}) | **Dev:** {_pct(dev)} | **Score:** {score:.0f}/100\n"
        + f"**Mise:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)}) — {_pct(pct_day)} du budget jour\n"
        + f"**Budget jour:** {_fmt_money(daily_budget)} | **Utilisé après bet:** {_fmt_money(spent_after)}"
        + ctx_block
        + clv
        + "\n_Max 3 TEAM bets/jour. Si la cote bouge fortement avant ton clic, ne force pas._"
    )


def format_prop_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "PROP")
    player = p.get("player", "")
    side = p.get("selection", "")
    line = p.get("line")
    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)))
    fair_adj = float(p.get("fair_prob", 0.0))
    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)))
    edge_adj = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    ev = float(p.get("ev", fair_adj * odds - 1.0))
    score = float(p.get("score", 0.0))
    tier = p.get("tier", "STRICT")
    haircut = bool(p.get("haircut_applied", False))

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0

    injury_note = p.get("injury_note")
    minutes_note = p.get("minutes_note")

    ctx = []
    if injury_note:
        ctx.append(f"**Injuries:** {injury_note}")
    if minutes_note:
        ctx.append(f"**Minutes proj.:** {minutes_note}")

    ctx_block = ("\n" + "\n".join(ctx)) if ctx else ""
    clv = _clv_block(p)

    sel = f"{player} — {side} {line}" if line is not None else f"{player} — {side}"

    return (
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        f"**Sélection:** {sel}\n"
        f"**Best:** {odds:.2f} (**{book}**)\n"
        f"**Tier:** {tier} {'(haircut)' if haircut else ''}\n"
        f"**p_fair:** {_pct(fair_adj)} (raw {_pct(fair_raw)})\n"
        f"**EV:** {_pct(ev)} | **Edge:** {_pct(edge_adj)} (raw {_pct(edge_raw)}) | **Dev:** {_pct(dev)} | **Score:** {score:.0f}/100\n"
        f"**Mise:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)})\n"
        f"**Budget jour:** {_fmt_money(daily_budget)} | **Utilisé après bet:** {_fmt_money(spent_after)}"
        + ctx_block
        + clv
        + "\n_Props: 1 pick par joueur & 1 pick par match (si possible)._"
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
    rejects_block = "\n".join([f"• {x}" for x in top_rejects]) if top_rejects else "• (aucune donnée)"
    near_block = "\n".join(near_miss_lines) if near_miss_lines else "Aucun near-miss."
    regions_txt = ", ".join([r for r in regions_used if r]) if regions_used else "n/a"

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
