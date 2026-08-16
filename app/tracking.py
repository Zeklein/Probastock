"""Suivi "prediction vs realite" : pour la derniere prediction de chaque
provider et pour le dernier score agrege d'un actif, calcule le rendement reel
constate a J+1/J+7/J+30 (jours calendaires depuis prediction_date) et un statut
correct/incorrect/neutre par rapport a la recommandation.

Volontairement independant de predictions.horizons (probabilites 1j/5j/60j
demandees au modele au moment de l'analyse) : ce module mesure ce qui s'est
reellement passe sur le marche apres coup, pas ce que le modele anticipait.

Une seule ligne par (ticker, provider) et par (ticker, agregat) : la plus
recente. Si le pipeline tourne plusieurs jours de suite, ce module suit
toujours "la position actuelle" du modele, pas un historique complet de
chaque prediction passee -- plus simple a lire pour une vue de comparaison.
"""
from bisect import bisect_left
from datetime import date, timedelta

from sqlalchemy import text

from app.ai_engine import CLAUDE_MODEL, DEEPSEEK_MODEL, GEMINI_MODEL, NEMOTRON_MODEL
from app.db import engine

TRACKING_HORIZONS = {"J+1": 1, "J+7": 7, "J+30": 30}  # jours calendaires depuis prediction_date

PROVIDER_LABEL_BY_MODEL = {
    DEEPSEEK_MODEL: "deepseek",
    GEMINI_MODEL: "gemini",
    CLAUDE_MODEL: "claude",
    NEMOTRON_MODEL: "nemotron",
}

# Seuil de mouvement significatif (rendement reel, ex: 0.02 = 2%). En-dessous,
# pour une reco directionnelle (BUY/REDUCE/SELL), le mouvement est trop faible
# pour juger la prediction -> statut "neutral". Pour HOLD, ce meme seuil sert
# de zone de tolerance : rester dedans valide le HOLD, en sortir l'invalide
# (HOLD n'a pas de zone "neutre" : "le cours ne bouge pas beaucoup" EST la
# prediction, donc soit ca tient soit ca ne tient pas).
CORRECTNESS_THRESHOLD = 0.02

# Fenetre de tolerance (jours calendaires) pour trouver le jour de bourse le
# plus proche d'une date cible qui tombe un week-end/jour ferie. Au-dela, on
# considere qu'il n'y a pas de donnee de prix exploitable pour cet horizon.
NEAREST_TRADING_DAY_WINDOW_DAYS = 5


def evaluate_status(recommendation: str, actual_return: float) -> str:
    """correct / incorrect / neutral, cf. CORRECTNESS_THRESHOLD ci-dessus."""
    if recommendation == "BUY":
        if actual_return > CORRECTNESS_THRESHOLD:
            return "correct"
        if actual_return < -CORRECTNESS_THRESHOLD:
            return "incorrect"
        return "neutral"
    if recommendation in ("REDUCE", "SELL"):
        if actual_return < -CORRECTNESS_THRESHOLD:
            return "correct"
        if actual_return > CORRECTNESS_THRESHOLD:
            return "incorrect"
        return "neutral"
    if recommendation == "HOLD":
        return "correct" if abs(actual_return) <= CORRECTNESS_THRESHOLD else "incorrect"
    raise ValueError(f"recommendation inconnue : {recommendation!r}")


def _nearest_price(sorted_dates: list[date], prices_by_date: dict, target_date: date):
    """sorted_dates : dates de bourse disponibles pour l'actif, triees. Renvoie
    (date_trouvee, close) le plus proche de target_date dans la fenetre de
    tolerance, ou (None, None) si rien d'exploitable."""
    if not sorted_dates:
        return None, None
    idx = bisect_left(sorted_dates, target_date)
    candidates = [d for d in (sorted_dates[idx - 1] if idx > 0 else None,
                               sorted_dates[idx] if idx < len(sorted_dates) else None) if d is not None]
    if not candidates:
        return None, None
    best = min(candidates, key=lambda d: abs((d - target_date).days))
    if abs((best - target_date).days) > NEAREST_TRADING_DAY_WINDOW_DAYS:
        return None, None
    return best, prices_by_date[best]


def _horizon_result(prediction_date, price_at_prediction, sorted_dates, prices_by_date, offset_days, recommendation, today):
    target_date = prediction_date + timedelta(days=offset_days)

    if today < target_date:
        return {"target_date": target_date, "status": "pending", "price": None, "actual_return": None}

    found_date, price = _nearest_price(sorted_dates, prices_by_date, target_date)
    if found_date is None or price_at_prediction is None:
        return {"target_date": target_date, "status": "no_data", "price": None, "actual_return": None}

    actual_return = float(price) / float(price_at_prediction) - 1
    return {
        "target_date": target_date,
        "price_date": found_date,
        "price": float(price),
        "actual_return": actual_return,
        "status": evaluate_status(recommendation, actual_return),
    }


def compute_tracking(ticker: str | None = None) -> list[dict]:
    today = date.today()

    with engine.connect() as conn:
        assets = conn.execute(
            text("SELECT id, ticker FROM assets" + (" WHERE ticker = :t" if ticker else "") + " ORDER BY ticker"),
            {"t": ticker} if ticker else {},
        ).mappings().all()
        if not assets:
            return []
        asset_ids = [a["id"] for a in assets]
        ticker_by_id = {a["id"]: a["ticker"] for a in assets}

        price_rows = conn.execute(
            text(
                """
                SELECT DISTINCT ON (asset_id, trade_date) asset_id, trade_date, close
                FROM price_snapshots
                WHERE asset_id = ANY(:ids)
                ORDER BY asset_id, trade_date, fetched_at DESC
                """
            ),
            {"ids": asset_ids},
        ).mappings().all()

        # La derniere prediction par (actif, provider) : si le pipeline tourne
        # plusieurs jours de suite, on ne suit que la position la plus recente.
        pred_rows = conn.execute(
            text(
                """
                SELECT DISTINCT ON (asset_id, model_version)
                    asset_id, model_version, prediction_date, recommendation, score
                FROM predictions
                WHERE asset_id = ANY(:ids) AND recommendation IS NOT NULL
                ORDER BY asset_id, model_version, prediction_date DESC, predicted_at DESC
                """
            ),
            {"ids": asset_ids},
        ).mappings().all()

        agg_rows = conn.execute(
            text(
                """
                SELECT DISTINCT ON (asset_id)
                    asset_id, prediction_date, score_agrege, recommendation_finale, conviction, provider_count
                FROM predictions_aggregated
                WHERE asset_id = ANY(:ids)
                ORDER BY asset_id, prediction_date DESC
                """
            ),
            {"ids": asset_ids},
        ).mappings().all()

    prices_by_asset: dict[int, dict[date, float]] = {}
    for r in price_rows:
        prices_by_asset.setdefault(r["asset_id"], {})[r["trade_date"]] = r["close"]
    sorted_dates_by_asset = {aid: sorted(d.keys()) for aid, d in prices_by_asset.items()}

    def price_at(asset_id, target_date):
        return _nearest_price(sorted_dates_by_asset.get(asset_id, []), prices_by_asset.get(asset_id, {}), target_date)

    def horizons_for(asset_id, prediction_date, price_at_prediction, recommendation):
        return {
            label: _horizon_result(
                prediction_date, price_at_prediction,
                sorted_dates_by_asset.get(asset_id, []), prices_by_asset.get(asset_id, {}),
                offset, recommendation, today,
            )
            for label, offset in TRACKING_HORIZONS.items()
        }

    results_by_ticker: dict[str, dict] = {}

    for row in pred_rows:
        asset_id = row["asset_id"]
        tkr = ticker_by_id[asset_id]
        _, price_at_prediction = price_at(asset_id, row["prediction_date"])
        entry = results_by_ticker.setdefault(tkr, {"ticker": tkr, "providers": [], "aggregate": None})
        entry["providers"].append({
            "provider": PROVIDER_LABEL_BY_MODEL.get(row["model_version"], row["model_version"]),
            "prediction_date": row["prediction_date"],
            "recommendation": row["recommendation"],
            "score": float(row["score"]) if row["score"] is not None else None,
            "price_at_prediction": float(price_at_prediction) if price_at_prediction is not None else None,
            "horizons": horizons_for(asset_id, row["prediction_date"], price_at_prediction, row["recommendation"]),
        })

    for row in agg_rows:
        asset_id = row["asset_id"]
        tkr = ticker_by_id[asset_id]
        _, price_at_prediction = price_at(asset_id, row["prediction_date"])
        entry = results_by_ticker.setdefault(tkr, {"ticker": tkr, "providers": [], "aggregate": None})
        entry["aggregate"] = {
            "prediction_date": row["prediction_date"],
            "recommendation": row["recommendation_finale"],
            "score": float(row["score_agrege"]) if row["score_agrege"] is not None else None,
            "conviction": float(row["conviction"]) if row["conviction"] is not None else None,
            "provider_count": row["provider_count"],
            "price_at_prediction": float(price_at_prediction) if price_at_prediction is not None else None,
            "horizons": horizons_for(asset_id, row["prediction_date"], price_at_prediction, row["recommendation_finale"]),
        }

    order = {tkr: i for i, tkr in enumerate(ticker_by_id[a["id"]] for a in assets)}
    return sorted(results_by_ticker.values(), key=lambda e: order.get(e["ticker"], 999))


def _demo():
    # --- evaluate_status ---------------------------------------------------
    assert evaluate_status("BUY", 0.05) == "correct"
    assert evaluate_status("BUY", -0.05) == "incorrect"
    assert evaluate_status("BUY", 0.01) == "neutral"
    assert evaluate_status("REDUCE", -0.05) == "correct"
    assert evaluate_status("SELL", -0.05) == "correct"
    assert evaluate_status("REDUCE", 0.05) == "incorrect"
    assert evaluate_status("HOLD", 0.01) == "correct"
    assert evaluate_status("HOLD", 0.05) == "incorrect"

    # --- _nearest_price : week-end / jour ferie ----------------------------
    dates = [date(2026, 8, 14), date(2026, 8, 17)]  # vendredi -> lundi (trou du week-end)
    prices = {date(2026, 8, 14): 100.0, date(2026, 8, 17): 103.0}
    found, price = _nearest_price(dates, prices, date(2026, 8, 15))  # samedi, non cote
    assert found == date(2026, 8, 14) and price == 100.0
    found, price = _nearest_price(dates, prices, date(2026, 8, 16))  # dimanche, plus proche du lundi
    assert found == date(2026, 8, 17) and price == 103.0
    found, price = _nearest_price(dates, prices, date(2026, 9, 1))  # trop loin -> pas de donnee
    assert found is None

    # --- _horizon_result : simule un horizon pas encore ecoule (pending) ---
    pred_date = date(2026, 8, 15)
    today_early = date(2026, 8, 16)  # J+1 pas encore atteint pour J+7/J+30
    r = _horizon_result(pred_date, 100.0, dates, prices, 7, "BUY", today_early)
    assert r["status"] == "pending"

    # --- _horizon_result : simule un horizon ecoule, sans attendre le vrai calendrier ---
    today_later = date(2026, 8, 25)
    all_dates = [date(2026, 8, 15), date(2026, 8, 22)]
    all_prices = {date(2026, 8, 15): 100.0, date(2026, 8, 22): 108.0}  # +8%
    r = _horizon_result(pred_date, 100.0, all_dates, all_prices, 7, "BUY", today_later)
    assert r["status"] == "correct" and abs(r["actual_return"] - 0.08) < 1e-9

    r = _horizon_result(pred_date, 100.0, all_dates, all_prices, 7, "REDUCE", today_later)
    assert r["status"] == "incorrect"  # le cours a monte, REDUCE avait tort

    print("app/tracking.py: self-check OK")


if __name__ == "__main__":
    _demo()
