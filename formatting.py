from typing import Any, Dict, List


def pct(x: float) -> str:
    return f"{x*100:.2f}%"


def fmt_money(x: float) -> str:
    return f"{x:.2f}€"


def tier(score: float) -> str:
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B+"
    if score >= 60:
        return "B"
    return "C"


def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    best_flag = "✅ FR book" if p.get("best_is_fr") else "⚠️ best non-FR (FR dispo moins bon)"
    fr_best_line = ""
    if p.get("fr_best") is not None and p.get("fr_best_book") is not None:
        fr_best_line = f"\nFR best: **{p['fr_best']:.2f}** ({p['fr_best_book']})"

    line_line = f"\nLine: **{p['line']}**" if p.get("line") is not None else ""
    bk_pct = (stake / bankroll) * 100.0 if bankroll > 0 else 0.0

    return (
        f"**Match:** {p['match']}\n"
        f"**Marché:** {p['market']}{line_line}\n"
        f"**Sélection:** {p['selection']}\n"
        f"**Best:** **{p['odds']:.2f}** ({p['book']}) — {best_flag}"
        f"{fr_best_line}\n"
        f"**Books utilisés (2-way):** {p.get('books_used', '?')} | **Cote médiane (sélection):** {p.get('median_odds', 0.0):.2f}\n"
        f"**Fair p (no-vig):** {pct(p.get('fair_prob', 0.0))} | **Implied(best):** {pct(1.0 / p['odds'])}\n"
        f"**Edge réel:** **{pct(p.get('edge', 0.0))}** | **Dev vs médiane:** {pct(p.get('dev', 0.0))}\n"
        f"**Bet Quality:** **{p.get('score', 0):.0f}/100 ({tier(p.get('score', 0))})**\n"
        f"**Mise (budget jour):** {bk_pct:.2f}% BK ({fmt_money(stake)})\n"
        f"**Budget jour:** {fmt_money(daily_budget)} | **Utilisé après bet:** {fmt_money(spent_after)}\n"
        f"_Diversification: max 2 ML si possible · 1 pick/match._"
    )


def format_prop_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    best_flag = "✅ FR book" if p.get("best_is_fr") else "⚠️ best non-FR (FR dispo moins bon)"
    fr_best_line = ""
    if p.get("fr_best") is not None and p.get("fr_best_book") is not None:
        fr_best_line = f"\nFR best: **{p['fr_best']:.2f}** ({p['fr_best_book']})"

    bk_pct = (stake / bankroll) * 100.0 if bankroll > 0 else 0.0
    line_line = f" {p['line']}" if p.get("line") is not None else ""

    return (
        f"**Match:** {p['match']}\n"
        f"**Marché:** {p['market']}\n"
        f"**Sélection:** {p['player']} — {p['selection']}{line_line}\n"
        f"**Best:** **{p['odds']:.2f}** ({p['book']}) — {best_flag}"
        f"{fr_best_line}\n"
        f"**Books utilisés (2-way):** {p.get('books_used', '?')} | **Cote médiane (sélection):** {p.get('median_odds', 0.0):.2f}\n"
        f"**Fair p (no-vig):** {pct(p.get('fair_prob', 0.0))} | **Implied(best):** {pct(1.0 / p['odds'])}\n"
        f"**Edge réel:** **{pct(p.get('edge', 0.0))}** | **Dev vs médiane:** {pct(p.get('dev', 0.0))}\n"
        f"**Bet Quality:** **{p.get('score', 0):.0f}/100 ({tier(p.get('score', 0))})**\n"
        f"**Mise (budget jour):** {bk_pct:.2f}% BK ({fmt_money(stake)})\n"
        f"**Budget jour:** {fmt_money(daily_budget)} | **Utilisé après bet:** {fmt_money(spent_after)}\n"
        f"_Props: 1 pick/joueur · 1 pick/match (si possible)._"
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
    rejects_block = "\n".join([f"- {x}" for x in top_rejects]) if top_rejects else "- (aucune donnée)"
    near_block = "\n".join(near_miss_lines) if near_miss_lines else "_Aucun near-miss._"

    return (
        f"**{title}**\n"
        f"Raison: {reason}\n\n"
        f"**Résumé analyse**\n"
        f"- Regions utilisées: **{','.join(regions_used) if regions_used else 'n/a'}**\n"
        f"- Matchs analysés: **{games_analyzed}**\n"
        f"- Marchés testés (2-way & >=2 books): **{markets_tested}**\n\n"
        f"**Refus principaux**\n{rejects_block}\n\n"
        f"**Near miss (Top 5)**\n{near_block}\n\n"
        f"Budget jour: **{daily_budget:.2f}€** | Déjà utilisé: **{daily_spent:.2f}€**"
    )
