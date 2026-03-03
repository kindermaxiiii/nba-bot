from typing import Any, Dict, List


def _pct(x: float) -> str:
    try:
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "n/a"


def _money(x: float) -> str:
    try:
        return f"{float(x):.2f}€"
    except Exception:
        return "n/a"


def format_team_pick(p: Dict[str, Any], stake: float, daily_budget: float, spent_after: float, max_ml: int, one_pick_per_match: bool) -> str:
    line_txt = f"{p.get('line')}" if p.get("line") is not None else "None"
    return (
        f"✅ NBA TEAM BET\n"
        f"Match: {p.get('match')}\n"
        f"Marché: {p.get('market')}\n"
        f"Line: {line_txt}\n"
        f"Sélection: {p.get('selection')}\n"
        f"Best: {p.get('odds'):.2f} ({p.get('book')})\n"
        f"Books utilisés (médiane): {p.get('books_used')} | Cote médiane: {p.get('median_odds'):.2f}\n"
        f"p_real: {_pct(p.get('p_real', 0.0))} | p_mkt: {_pct(p.get('p_mkt', 0.0))}\n"
        f"EV: {_pct(p.get('ev', 0.0))} | Edge: {_pct(p.get('edge', 0.0))} | Dev vs médiane: {_pct(p.get('dev', 0.0))}\n"
        f"Mise (budget jour): {_pct(stake / daily_budget) if daily_budget > 0 else 'n/a'} BK ({_money(stake)})\n"
        f"Budget jour: {_money(daily_budget)} | Utilisé après bet: {_money(spent_after)}\n"
        f"Diversification: max {max_ml} ML si possible | 1 pick/match: {str(one_pick_per_match)}"
    )


def format_prop_pick(p: Dict[str, Any], stake: float, daily_budget: float, spent_after: float) -> str:
    line_txt = f"{p.get('line')}" if p.get("line") is not None else "None"
    sel = f"{p.get('player')} — {p.get('selection')} {line_txt}"
    return (
        f"✅ NBA PLAYER PROP\n"
        f"Match: {p.get('match')}\n"
        f"Marché: {p.get('market')}\n"
        f"Sélection: {sel}\n"
        f"Best: {p.get('odds'):.2f} ({p.get('book')})\n"
        f"Books utilisés (médiane): {p.get('books_used')} | Cote médiane: {p.get('median_odds'):.2f}\n"
        f"p_real: {_pct(p.get('p_real', 0.0))} | p_mkt: {_pct(p.get('p_mkt', 0.0))}\n"
        f"EV: {_pct(p.get('ev', 0.0))} | Edge: {_pct(p.get('edge', 0.0))} | Dev vs médiane: {_pct(p.get('dev', 0.0))}\n"
        f"Mise (budget jour): {_pct(stake / daily_budget) if daily_budget > 0 else 'n/a'} BK ({_money(stake)})\n"
        f"Budget jour: {_money(daily_budget)} | Utilisé après bet: {_money(spent_after)}\n"
        f"Props: 1 pick par joueur & 1 pick par match (si possible)"
    )


def format_no_bet(reason: str, daily_budget: float, matches_analyzed: int, markets_tested: int) -> str:
    return (
        f"❌ NBA NO BET LOG\n\n"
        f"Raison: {reason}\n\n"
        f"Matchs analysés: {matches_analyzed}\n"
        f"Marchés testés: {markets_tested}\n\n"
        f"Budget jour: {_money(daily_budget)}"
    )
