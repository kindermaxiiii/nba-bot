from typing import Any, Dict, List, Optional


# -------------------------
# Helpers
# -------------------------
def _pct(x: float) -> str:
    try:
        return f"{float(x)*100:.2f}%"
    except Exception:
        return "n/a"


def _fmt_money(x: float) -> str:
    try:
        return f"{float(x):.2f}€"
    except Exception:
        return "n/a"


def _maybe(v: Optional[float], fmt: str = "{:.2f}") -> str:
    if v is None:
        return "n/a"
    try:
        return fmt.format(float(v))
    except Exception:
        return "n/a"


def _bar(x: float, lo: float, hi: float, width: int = 12) -> str:
    """
    Simple text bar for visual signal. Clamps x between lo and hi.
    """
    try:
        x = float(x)
    except Exception:
        x = lo
    if hi <= lo:
        hi = lo + 1e-9
    x = max(lo, min(hi, x))
    k = int(round((x - lo) / (hi - lo) * width))
    k = max(0, min(width, k))
    return "█" * k + "░" * (width - k)


def _flag_line(edge_raw: float, edge_adj: float, dev: float, haircut: bool, tier: str) -> str:
    flags = []
    if tier and str(tier).startswith("RELAXED"):
        flags.append("🟠 RELAXED")
    else:
        flags.append("🟢 STRICT")

    if haircut:
        flags.append("✂️ haircut")

    # lightweight warnings
    if dev < 0.02:
        flags.append("⚠️ dev faible")
    if edge_adj < 0.015:
        flags.append("⚠️ edge faible")
    if edge_raw >= 0.10:
        flags.append("🚩 edge_raw>10% (suspect)")

    return " | ".join(flags)


def _clv_block(p: Dict[str, Any]) -> str:
    """
    FIX: odds=0.0 should still display, so test odds is not None (not truthy).
    """
    snaps = p.get("clv_snapshots") or []
    if not snaps:
        return ""
    lines = []
    for s in snaps[-4:]:
        tag = s.get("tag", "?")
        odds = s.get("odds", None)
        book = s.get("book", "") or ""
        dt = s.get("ts_utc", "") or ""
        if odds is None:
            lines.append(f"• {tag}: n/a @ {dt}")
        else:
            try:
                lines.append(f"• {tag}: {float(odds):.2f} ({book}) @ {dt}")
            except Exception:
                lines.append(f"• {tag}: n/a @ {dt}")
    return "\n\n**📈 CLV snapshots**\n" + "\n".join(lines)


def _section(title: str) -> str:
    return f"\n\n━━━━━━━━━━━━━━━━━━━━\n**{title}**\n━━━━━━━━━━━━━━━━━━━━"


# -------------------------
# TEAM PICK
# -------------------------
def format_team_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "TEAM")
    selection = p.get("selection", "")
    line = p.get("line", None)

    odds = float(p.get("odds", 0.0) or 0.0)
    book = p.get("book", "Unknown")

    best_is_fr = bool(p.get("best_is_fr", False))
    fr_best = p.get("fr_best", None)
    fr_best_book = p.get("fr_best_book", None)

    median_odds = p.get("median_odds", None)
    books_used = p.get("books_used", None)
    total_books = p.get("total_books", None)

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)) or 0.0)
    fair_adj = float(p.get("fair_prob", 0.0) or 0.0)

    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)) or 0.0)
    edge_adj = float(p.get("edge", 0.0) or 0.0)

    dev = float(p.get("dev", 0.0) or 0.0)
    ev = float(p.get("ev", fair_adj * odds - 1.0) or 0.0)

    score = float(p.get("score", 0.0) or 0.0)
    tier = p.get("tier", "STRICT") or "STRICT"
    haircut = bool(p.get("haircut_applied", False))

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0
    pct_day = (stake / daily_budget) if daily_budget > 0 else 0.0

    # Best line visual
    if best_is_fr:
        best_line = f"**Best:** {odds:.2f} (**{book}**) ✅ FR"
    else:
        if fr_best is not None:
            best_line = f"**Best:** {odds:.2f} (**{book}**) ⚠️ non-FR | **FR best:** {_maybe(fr_best)} ({fr_best_book})"
        else:
            best_line = f"**Best:** {odds:.2f} (**{book}**) ⚠️ non-FR (FR indispo)"

    # Context
    injury_note = p.get("injury_note", None)
    minutes_note = p.get("minutes_note", None)

    ctx_lines = []
    if injury_note:
        ctx_lines.append(f"🩺 **Injuries:** {injury_note}")
    if minutes_note:
        ctx_lines.append(f"⏱️ **Minutes:** {minutes_note}")
    ctx_block = ("\n" + "\n".join(ctx_lines)) if ctx_lines else ""

    # Visual blocks
    flags = _flag_line(edge_raw=edge_raw, edge_adj=edge_adj, dev=dev, haircut=haircut, tier=tier)

    edge_bar = _bar(edge_adj, 0.0, 0.05)
    dev_bar = _bar(dev, 0.0, 0.12)
    score_bar = _bar(score, 0.0, 100.0)

    books_line = ""
    if books_used is not None or total_books is not None or median_odds is not None:
        books_line = (
            f"📚 **Books (median calc):** {_maybe(books_used, '{:.0f}')} | **Total books:** {_maybe(total_books, '{:.0f}')} | "
            f"**Median odds:** {_maybe(median_odds)}"
        )

    clv = _clv_block(p)

    return (
        f"✅ **NBA TEAM BET**\n"
        f"**Match:** {match}\n"
        f"**Market:** {market}\n"
        + (f"**Line:** {line}\n" if line is not None else "")
        + f"**Pick:** **{selection}**\n"
        f"{best_line}"
        + (_section("📊 VALUE METRICS"))
        + (f"\n{books_line}" if books_line else "")
        + f"\n🎯 **p_fair:** {_pct(fair_adj)} (raw {_pct(fair_raw)})"
        + f"\n📈 **EV:** {_pct(ev)}"
        + f"\n🧠 **Edge:** {_pct(edge_adj)} (raw {_pct(edge_raw)})  {edge_bar}"
        + f"\n🧾 **Dev vs median:** {_pct(dev)}  {dev_bar}"
        + f"\n⭐ **Score:** {score:.0f}/100  {score_bar}"
        + (_section("🧷 FLAGS"))
        + f"\n{flags}"
        + (_section("💰 STAKE"))
        + f"\n**Stake:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)}) — {_pct(pct_day)} day budget"
        + f"\n**Day budget:** {_fmt_money(daily_budget)} | **Spent after:** {_fmt_money(spent_after)}"
        + (ctx_block if ctx_block else "")
        + (clv if clv else "")
        + "\n\n_If odds moved a lot before clicking: skip._"
    )


# -------------------------
# PROP PICK
# -------------------------
def format_prop_pick(p: Dict[str, Any], stake: float, bankroll: float, daily_budget: float, spent_after: float) -> str:
    match = p.get("match", "")
    market = p.get("market", "PROP")
    player = p.get("player", "")
    side = p.get("selection", "")
    line = p.get("line", None)

    odds = float(p.get("odds", 0.0) or 0.0)
    book = p.get("book", "Unknown")

    fair_raw = float(p.get("fair_prob_raw", p.get("fair_prob", 0.0)) or 0.0)
    fair_adj = float(p.get("fair_prob", 0.0) or 0.0)

    edge_raw = float(p.get("edge_raw", p.get("edge", 0.0)) or 0.0)
    edge_adj = float(p.get("edge", 0.0) or 0.0)

    dev = float(p.get("dev", 0.0) or 0.0)
    ev = float(p.get("ev", fair_adj * odds - 1.0) or 0.0)

    score = float(p.get("score", 0.0) or 0.0)
    tier = p.get("tier", "STRICT") or "STRICT"
    haircut = bool(p.get("haircut_applied", False))

    pct_bk = (stake / bankroll) if bankroll > 0 else 0.0

    injury_note = p.get("injury_note", None)
    minutes_note = p.get("minutes_note", None)

    ctx_lines = []
    if injury_note:
        ctx_lines.append(f"🩺 **Injuries:** {injury_note}")
    if minutes_note:
        ctx_lines.append(f"⏱️ **Minutes:** {minutes_note}")
    ctx_block = ("\n" + "\n".join(ctx_lines)) if ctx_lines else ""

    sel = f"{player} — {side} {line}" if line is not None else f"{player} — {side}"
    flags = _flag_line(edge_raw=edge_raw, edge_adj=edge_adj, dev=dev, haircut=haircut, tier=tier)

    edge_bar = _bar(edge_adj, 0.0, 0.05)
    dev_bar = _bar(dev, 0.0, 0.12)
    score_bar = _bar(score, 0.0, 100.0)

    clv = _clv_block(p)

    return (
        f"✅ **NBA PLAYER PROP**\n"
        f"**Match:** {match}\n"
        f"**Market:** {market}\n"
        f"**Pick:** **{sel}**\n"
        f"**Best:** {odds:.2f} (**{book}**)"
        + (_section("📊 VALUE METRICS"))
        + f"\n🎯 **p_fair:** {_pct(fair_adj)} (raw {_pct(fair_raw)})"
        + f"\n📈 **EV:** {_pct(ev)}"
        + f"\n🧠 **Edge:** {_pct(edge_adj)} (raw {_pct(edge_raw)})  {edge_bar}"
        + f"\n🧾 **Dev vs median:** {_pct(dev)}  {dev_bar}"
        + f"\n⭐ **Score:** {score:.0f}/100  {score_bar}"
        + (_section("🧷 FLAGS"))
        + f"\n{flags}"
        + (_section("💰 STAKE"))
        + f"\n**Stake:** {pct_bk*100:.2f}% BK ({_fmt_money(stake)})"
        + f"\n**Day budget:** {_fmt_money(daily_budget)} | **Spent after:** {_fmt_money(spent_after)}"
        + (ctx_block if ctx_block else "")
        + (clv if clv else "")
        + "\n\n_Props: 1 per player & 1 per match (when possible)._"
    )


# -------------------------
# NO BET LOG (more visual)
# -------------------------
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
    near_block = "\n".join(near_miss_lines) if near_miss_lines else "• Aucun near-miss."

    regions_txt = ", ".join([r for r in regions_used if r]) if regions_used else "n/a"

    # small visual summary
    used_pct = (daily_spent / daily_budget) if daily_budget > 0 else 0.0
    budget_bar = _bar(used_pct, 0.0, 1.0)

    return (
        f"❌ **{title}**\n"
        f"**Reason:** {reason}"
        + (_section("🧾 RUN SUMMARY"))
        + f"\n🌍 **Regions:** {regions_txt}"
        + f"\n🎮 **Games analyzed:** {games_analyzed}"
        + f"\n🧪 **Markets tested:** {markets_tested}"
        + (_section("🧱 MAIN REJECTS"))
        + f"\n{rejects_block}"
        + (_section("🎯 NEAR MISSES (TOP 5)"))
        + f"\n{near_block}"
        + (_section("💰 BUDGET"))
        + f"\n**Day budget:** {_fmt_money(daily_budget)} | **Spent:** {_fmt_money(daily_spent)} ({_pct(used_pct)})  {budget_bar}"
    )
