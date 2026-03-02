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


def _line_str(v: Any) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
        # show sign for spreads
        if f == 0:
            return "0"
        if f > 0:
            return f"+{f:g}"
        return f"{f:g}"
    except Exception:
        return str(v)


def _best_price_block(p: Dict[str, Any]) -> str:
    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")
    best_is_fr = bool(p.get("best_is_fr", False))
    fr_best = p.get("fr_best")
    fr_best_book = p.get("fr_best_book")

    if best_is_fr:
        return f"**Best (FR):** {odds:.2f} (**{book}**) ✅"
    if fr_best is not None:
        return (
            f"**Best:** {odds:.2f} (**{book}**) — ⚠️ non-FR\n"
            f"**FR best:** {_maybe(fr_best)} ({fr_best_book})"
        )
    return f"**Best:** {odds:.2f} (**{book}**) — ⚠️ non-FR (FR indispo)"


def _quant_block(p: Dict[str, Any]) -> str:
    p_fair = float(p.get("p_fair", p.get("fair_prob", 0.0)))
    p_imp = float(p.get("p_imp_best", 0.0))
    ev = float(p.get("ev", 0.0))
    edge = float(p.get("edge", 0.0))
    dev = float(p.get("dev", 0.0))
    score = float(p.get("score", 0.0))
    kelly = float(p.get("kelly_full", 0.0))
    rob = p.get("robustness", "n/a")
    frag = p.get("fragility", "n/a")

    median_odds = p.get("median_odds")
    books_used = p.get("books_used")
    total_books = p.get("total_books")

    return (
        f"**p_fair (no-vig):** {_pct(p_fair)} | **p_imp(best):** {_pct(p_imp)}\n"
        f"**EV (p_fair):** {_pct(ev)} | **Edge:** {_pct(edge)} | **Dev vs médiane:** {_pct(dev)}\n"
        f"**Kelly (brut):** {_pct(kelly)} | **Score:** {score:.0f}/100 | **Robust:** {rob} | **Fragile:** {frag}\n"
        + (
            f"**Books (2-way):** {books_used} | **Total books:** {total_books} | **Cote médiane:** {_maybe(median_odds)}\n"
            if books_used and median_odds is not None else
            f"**Total books:** {total_books}\n" if total_books is not None else ""
        )
    )


def _context_block(p: Dict[str, Any]) -> str:
    lines: List[str] = []

    inj = p.get("injury_note")
    if inj:
        lines.append(f"**Injuries (best-effort):** {inj}")

    mins = p.get("minutes_note")
    if mins:
        lines.append(f"**Minutes (proj.):** {mins}")

    # optional: team metrics if you attach them
    # expected shapes:
    # p["team_metrics_home"] / p["team_metrics_away"] or nested dicts
    hm = p.get("team_metrics_home")
    aw = p.get("team_metrics_away")
    if isinstance(hm, dict) and isinstance(aw, dict):
        def pick(m: Dict[str, Any], key: str) -> Optional[float]:
            v = m.get(key)
            try:
                return float(v)
            except Exception:
                return None
        h_pace = pick(hm, "pace")
        a_pace = pick(aw, "pace")
        h_net = pick(hm, "net_rtg")
        a_net = pick(aw, "net_rtg")
        lines.append(
            f"**Team metrics:** "
            f"PACE(H/A) {_maybe(h_pace)}/{_maybe(a_pace)} | NET(H/A) {_maybe(h_net)}/{_maybe(a_net)}"
        )

    # optional: custom analysis strings
    extra = p.get("analysis_note")
    if extra:
        lines.append(f"**Analyse (résumé):** {extra}")

    if not lines:
        return ""
    return "\n" + "\n".join(lines)


def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "TEAM")
    selection = p.get("selection", "")
    line = p.get("line")

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0
    pct_day = (stake / daily_budget) if daily_budget > 0 else 0.0

    return (
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        + (f"**Line:** {_line_str(line)}\n" if line is not None else "")
        + f"**Sélection:** {selection}\n"
        + f"{_best_price_block(p)}\n"
        + _quant_block(p)
        + f"**Stake:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)}) | {_pct(pct_day)} du budget jour\n"
        + f"**Budget jour:** {_fmt_money(daily_budget)} | **Utilisé après bet:** {_fmt_money(spent_after)}\n"
        + _context_block(p)
        + "\n_Règles: 1 pick/match si possible, max ML/soir, ne pas forcer si gros move avant clic._"
    )


def format_prop_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "PROP")
    player = p.get("player", "")
    side = p.get("selection", "")
    line = p.get("line")

    odds = float(p.get("odds", 0.0))
    book = p.get("book", "Unknown")

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0

    sel = f"{player} — {side} {line}" if line is not None else f"{player} — {side}"

    # keep props best-block simple (books FR less relevant)
    p_local = dict(p)
    p_local["best_is_fr"] = True  # avoid FR warning spam for US books on props
    best_block = f"**Best:** {odds:.2f} (**{book}**)\n"

    return (
        f"**Match:** {match}\n"
        f"**Marché:** {market}\n"
        f"**Sélection:** {sel}\n"
        + best_block
        + _quant_block(p)
        + f"**Stake:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)})\n"
        + f"**Budget jour:** {_fmt_money(daily_budget)} | **Utilisé après bet:** {_fmt_money(spent_after)}"
        + _context_block(p)
        + "\n_Règles props: 1 pick/joueur + 1 pick/match si possible._"
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
