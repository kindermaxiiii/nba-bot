def format_team_pick(pick, stake, bankroll, daily_budget, daily_spent):
    return f"""
🏀 NBA — TEAM PICK

Match: {pick.get("match")}
Marché: {pick.get("market")}
Ligne: {pick.get("line")}
Sélection: {pick.get("selection")}

Cote: {pick.get("odds")} ({pick.get("book")})
Médiane: {pick.get("median_odds")}
Books: {pick.get("books_used")}/{pick.get("total_books")}

P_real: {round(pick.get("fair_prob", 0)*100,2)}%
P_mkt: {round(pick.get("fair_prob_raw", 0)*100,2)}%

Edge: {round(pick.get("edge", 0)*100,2)}%
EV: {round(pick.get("ev", 0)*100,2)}%
Dev: {round(pick.get("dev", 0)*100,2)}%

Score: {round(pick.get("score_adj", 0),1)}/100

💰 Stake: {round(stake,2)}€
Budget utilisé: {round(daily_spent,2)}/{round(daily_budget,2)}€
"""


def format_prop_pick(pick, stake, bankroll, daily_budget, daily_spent):
    return f"""
🎯 NBA — PLAYER PROP

Match: {pick.get("match")}
Joueur: {pick.get("player")}
Marché: {pick.get("market")}
Ligne: {pick.get("line")}
Sélection: {pick.get("selection")}

Cote: {pick.get("odds")} ({pick.get("book")})
Médiane: {pick.get("median_odds")}
Books: {pick.get("books_used")}/{pick.get("total_books")}

P_real: {round(pick.get("fair_prob", 0)*100,2)}%
P_mkt: {round(pick.get("fair_prob_raw", 0)*100,2)}%

Edge: {round(pick.get("edge", 0)*100,2)}%
EV: {round(pick.get("ev", 0)*100,2)}%
Dev: {round(pick.get("dev", 0)*100,2)}%

Score: {round(pick.get("score_adj", 0),1)}/100

💰 Stake: {round(stake,2)}€
Budget utilisé: {round(daily_spent,2)}/{round(daily_budget,2)}€
"""


def format_no_bet(
    title,
    reason,
    regions_used,
    games_analyzed,
    markets_tested,
    top_rejects,
    near_miss_lines,
    daily_budget,
    daily_spent
):
    return f"""
❌ {title}

Raison: {reason}

Matchs analysés: {games_analyzed}
Marchés testés: {markets_tested}

Budget jour: {round(daily_spent,2)} / {round(daily_budget,2)} €
"""
