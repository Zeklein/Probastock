"""Migration additive : ajoute predictions.horizons (JSONB, nullable -- compat
avec les 102 lignes deja stockees) et cree la table predictions_aggregated qui
stocke, par (asset, prediction_date), la moyenne des 3 providers.

Usage: python scripts/migrate_add_horizons_and_aggregation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

SQL = """
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS horizons JSONB;

CREATE TABLE IF NOT EXISTS predictions_aggregated (
    id                       BIGSERIAL PRIMARY KEY,
    asset_id                 INTEGER     NOT NULL REFERENCES assets(id),
    prediction_date          DATE        NOT NULL,

    score_agrege             NUMERIC(5, 2)  CHECK (score_agrege BETWEEN 0 AND 100),
    probability_20d_agregee  NUMERIC(5, 4)  CHECK (probability_20d_agregee BETWEEN 0 AND 1),
    confidence_agregee       NUMERIC(4, 3)  CHECK (confidence_agregee BETWEEN 0 AND 1),
    risk_agrege              NUMERIC(4, 3)  CHECK (risk_agrege BETWEEN 0 AND 1),
    horizons_agreges         JSONB,                  -- {probability_1d, probability_5d, probability_20d, probability_60d}, moyenne par horizon

    -- Conviction = mesure inverse de la dispersion entre les 3 scores providers,
    -- PAS le meme champ que predictions.conviction_score (qui est en realite la
    -- confidence auto-declaree par un seul provider). Formule documentee dans
    -- app/aggregation.py juste au-dessus de compute_conviction().
    conviction               NUMERIC(4, 3)  CHECK (conviction BETWEEN 0 AND 1),

    recommendation_finale    TEXT        CHECK (recommendation_finale IN ('BUY', 'HOLD', 'REDUCE', 'SELL')),
    provider_count           INTEGER     NOT NULL,     -- combien des 3 providers ont contribue (1 a 3)
    source_prediction_ids    BIGINT[]    NOT NULL,      -- predictions.id agregees, pour audit
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pred_agg_unique ON predictions_aggregated(asset_id, prediction_date);
"""

if __name__ == "__main__":
    with engine.begin() as conn:
        conn.execute(text(SQL))
    print("predictions.horizons ajoutee ; table predictions_aggregated creee (ou deja presentes).")
