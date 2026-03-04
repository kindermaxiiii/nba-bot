# engine.py
import json
from types import SimpleNamespace


def load_config(path):
    with open(path, "r") as f:
        cfg = json.load(f)
    return SimpleNamespace(**cfg)


def _prob_from_odds(odds):
    try:
        return 1 / float(odds)
    except:
        return None


def run_engine(games, cfg):

    team_picks = []
    prop_picks = []
    markets_tested = 0

    for g in games:

        home = g.get("home_team")
        away = g.get("away_team")

        bookmakers = g.get("bookmakers", [])

        for book in bookmakers:

            book_name = book.get("title")

            markets = book.get("markets", [])

            for m in markets:

                key = m.get("key")

                if key not in cfg.markets:
                    continue

                outcomes = m.get("outcomes", [])

                markets_tested += 1

                for o in outcomes:

                    name = o.get("name")
                    odds = o.get("price")

                    if odds is None:
                        continue

                    p_mkt = _prob_from_odds(odds)

                    if p_mkt is None:
                        continue

                    # modèle simple
                    p_model = p_mkt * 1.05

                    p_real = (p_model * cfg.model_weight) + (p_mkt * (1 - cfg.model_weight))

                    ev = (p_real * odds) - 1

                    edge = p_real - p_mkt
                    dev = abs(p_model - p_mkt)

                    score = (ev * 100) + (edge * 100)

                    if ev < cfg.min_ev:
                        continue

                    pick = {
                        "match": f"{away} @ {home}",
                        "market": key.upper(),
                        "selection": name,
                        "odds": odds,
                        "book": book_name,
                        "p_model": p_model,
                        "p_mkt": p_mkt,
                        "fair_prob": p_real,
                        "ev": ev,
                        "edge": edge,
                        "dev": dev,
                        "score": score,
                    }

                    team_picks.append(pick)

    # tri
    team_picks = sorted(team_picks, key=lambda x: x["score"], reverse=True)

    # limiter ML
    ml_count = 0
    filtered = []

    for p in team_picks:

        if p["market"] == "H2H":

            if p["odds"] > cfg.max_odds_ml:
                continue

            if ml_count >= cfg.max_ml_per_day:
                continue

            ml_count += 1

        filtered.append(p)

    team_picks = filtered[: cfg.max_picks_team]

    return {
        "team_picks": team_picks,
        "prop_picks": prop_picks,
        "meta": {
            "games": len(games),
            "markets_tested": markets_tested,
            "model_weight": cfg.model_weight,
            "clip_vs_market": cfg.clip_vs_market,
            "max_ml_per_day": cfg.max_ml_per_day,
            "max_odds_ml": cfg.max_odds_ml,
        },
    }
