"""Score Probastock agrege : moyenne simple des providers ayant reussi
(DeepSeek/Gemini/Claude/Nemotron) pour un meme (asset, prediction_date),
stockee dans predictions_aggregated. Le nombre de providers n'est fige nulle
part (ni dans les moyennes, ni dans la conviction) -- ajouter/retirer un
provider n'exige aucun changement ici, juste dans PROVIDERS (ai_engine.py) et
le batch qui appelle aggregate_ticker().

Seuils recommendation_finale et formule de conviction : voir commentaires
ci-dessous, derives de l'analyse empirique des 102 predictions individuelles
du 2026-08-15 (34 tickers x 3 providers, avant l'ajout de Nemotron) -- a
recalibrer une fois assez de donnees accumulees avec 4 providers.
"""
import json
from datetime import date as date_cls

from sqlalchemy import text

from app.db import engine

HORIZON_KEYS = ["probability_1d", "probability_5d", "probability_20d", "probability_60d"]

# Seuils empiriques sur score_agrege (0-100), derives des 102 predictions individuelles
# du 2026-08-15 : on a cherche le point de coupure qui minimise les erreurs de
# classification entre recommendation observee et score, pour chaque paire de
# classes adjacentes (BUY/HOLD : erreur minimale a 61-62 -> 62 ; HOLD/REDUCE :
# erreur minimale a 49-52 -> 50). Aucun SELL n'est apparu dans l'echantillon
# (score individuel min observe = 38, toujours classe REDUCE) : le seuil SELL
# est donc extrapole (meme largeur de bande que REDUCE/HOLD, 12 points, sous le
# plancher observe) plutot qu'empirique -- a recalibrer des que des SELL reels
# apparaissent en base.
RECOMMENDATION_THRESHOLDS = [
    (62, "BUY"),
    (50, "HOLD"),
    (35, "REDUCE"),
    (float("-inf"), "SELL"),
]


def score_to_recommendation(score: float) -> str:
    for threshold, reco in RECOMMENDATION_THRESHOLDS:
        if score >= threshold:
            return reco
    return "SELL"  # inatteignable (dernier seuil est -inf) mais garde le type honnete


def compute_conviction(scores: list[float]) -> float:
    """Conviction = 1 - dispersion normalisee entre les scores providers, sur le
    meme principe qu'un consensus d'analystes resserre (conviction haute) ou
    disperse (conviction basse) : on prend l'ecart max-min (0-100) plutot que
    l'ecart-type -- avec un petit nombre de points (2 a 4 providers), l'ecart-type
    est peu lisible et l'ecart max-min dit directement "les providers etaient
    d'accord a X points pres", quel que soit le nombre de providers. Normalise
    sur l'etendue totale possible (100) puis borne a [0, 1]."""
    if len(scores) < 2:
        return 1.0  # un seul provider : aucune dispersion a mesurer
    spread = max(scores) - min(scores)
    return max(0.0, min(1.0, 1 - spread / 100))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


# En-dessous de ce nombre de providers reussis, on n'agrege pas : agreger un
# seul score n'est pas un consensus (conviction serait trivialement 1.0, ce qui
# laisserait croire a un accord jamais mesure), et l'objectif meme du score
# Probastock est de comparer plusieurs avis independants.
MIN_PROVIDERS_FOR_AGGREGATION = 2


def aggregate_ticker(ticker: str, successful_results: list[dict], prediction_date: date_cls | None = None) -> dict | None:
    """Agrege successful_results (sortie de analyze_ticker(), une entree par
    provider ayant reussi CE run) et upsert le resultat dans
    predictions_aggregated. N'interroge jamais la base pour "completer" avec
    une prediction plus ancienne : un provider qui echoue ce run compte pour 0,
    jamais pour une valeur perimee reutilisee silencieusement (bug corrige :
    l'ancienne version repechait par erreur la derniere prediction DeepSeek
    valide d'un run precedent le meme jour quand le nouvel appel DeepSeek
    echouait, ex. SPY). Renvoie None si moins de MIN_PROVIDERS_FOR_AGGREGATION
    providers ont reussi -- l'appelant doit alors logger l'echec explicitement."""
    if len(successful_results) < MIN_PROVIDERS_FOR_AGGREGATION:
        return None

    prediction_date = prediction_date or date_cls.today()

    with engine.begin() as conn:
        asset_id = conn.execute(text("SELECT id FROM assets WHERE ticker = :t"), {"t": ticker}).scalar_one()

        scores = [float(r["analysis"]["score"]) for r in successful_results]
        score_agrege = _mean(scores)
        probability_20d_agregee = _mean([float(r["analysis"]["probability_20d"]) for r in successful_results])
        confidence_agregee = _mean([float(r["analysis"]["confidence"]) for r in successful_results])
        risk_agrege = _mean([float(r["analysis"]["risk"]) for r in successful_results])

        horizons_agreges = {}
        for key in HORIZON_KEYS:
            if key == "probability_20d":
                horizons_agreges[key] = probability_20d_agregee
                continue
            vals = [float(r["analysis"]["horizons"][key]) for r in successful_results]
            horizons_agreges[key] = _mean(vals)

        conviction = compute_conviction(scores)
        recommendation_finale = score_to_recommendation(score_agrege)
        source_prediction_ids = [r["prediction_id"] for r in successful_results]

        result = conn.execute(
            text(
                """
                INSERT INTO predictions_aggregated (
                    asset_id, prediction_date, score_agrege, probability_20d_agregee,
                    confidence_agregee, risk_agrege, horizons_agreges, conviction,
                    recommendation_finale, provider_count, source_prediction_ids
                ) VALUES (
                    :asset_id, :prediction_date, :score_agrege, :probability_20d_agregee,
                    :confidence_agregee, :risk_agrege, CAST(:horizons_agreges AS jsonb), :conviction,
                    :recommendation_finale, :provider_count, :source_prediction_ids
                )
                ON CONFLICT (asset_id, prediction_date) DO UPDATE SET
                    score_agrege = EXCLUDED.score_agrege,
                    probability_20d_agregee = EXCLUDED.probability_20d_agregee,
                    confidence_agregee = EXCLUDED.confidence_agregee,
                    risk_agrege = EXCLUDED.risk_agrege,
                    horizons_agreges = EXCLUDED.horizons_agreges,
                    conviction = EXCLUDED.conviction,
                    recommendation_finale = EXCLUDED.recommendation_finale,
                    provider_count = EXCLUDED.provider_count,
                    source_prediction_ids = EXCLUDED.source_prediction_ids,
                    computed_at = now()
                RETURNING id
                """
            ),
            {
                "asset_id": asset_id,
                "prediction_date": prediction_date,
                "score_agrege": score_agrege,
                "probability_20d_agregee": probability_20d_agregee,
                "confidence_agregee": confidence_agregee,
                "risk_agrege": risk_agrege,
                "horizons_agreges": json.dumps(horizons_agreges),
                "conviction": conviction,
                "recommendation_finale": recommendation_finale,
                "provider_count": len(successful_results),
                "source_prediction_ids": source_prediction_ids,
            },
        ).mappings().first()

        return {
            "id": result["id"],
            "ticker": ticker,
            "score_agrege": score_agrege,
            "recommendation_finale": recommendation_finale,
            "conviction": conviction,
            "horizons_agreges": horizons_agreges,
            "provider_count": len(successful_results),
        }


def _demo():
    assert score_to_recommendation(88) == "BUY"
    assert score_to_recommendation(62) == "BUY"
    assert score_to_recommendation(61.9) == "HOLD"
    assert score_to_recommendation(50) == "HOLD"
    assert score_to_recommendation(49.9) == "REDUCE"
    assert score_to_recommendation(35) == "REDUCE"
    assert score_to_recommendation(34.9) == "SELL"

    assert compute_conviction([70, 70, 70]) == 1.0          # accord parfait
    assert compute_conviction([70]) == 1.0                  # un seul provider
    assert compute_conviction([20, 70]) == 0.5               # ecart de 50 pts -> conviction 0.5
    assert compute_conviction([0, 100]) == 0.0               # desaccord total
    print("app/aggregation.py: self-check OK")


if __name__ == "__main__":
    _demo()
