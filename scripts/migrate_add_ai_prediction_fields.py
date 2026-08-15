"""Migration ponctuelle : ajoute les champs de sortie qualitative du moteur
d'analyse IA (direction, score, risk_score, analysis_factors) a predictions,
et rend expected_return nullable (non produit par un pipeline qualitatif).

Usage: python scripts/migrate_add_ai_prediction_fields.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

SQL = """
ALTER TABLE predictions ALTER COLUMN expected_return DROP NOT NULL;

ALTER TABLE predictions ADD COLUMN IF NOT EXISTS direction TEXT
    CHECK (direction IN ('bullish', 'neutral', 'bearish'));
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS score NUMERIC(5, 2)
    CHECK (score BETWEEN 0 AND 100);
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS risk_score NUMERIC(4, 3)
    CHECK (risk_score BETWEEN 0 AND 1);
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS analysis_factors JSONB;
"""

if __name__ == "__main__":
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(SQL)
        raw.commit()
    finally:
        raw.close()
    print("Colonnes direction/score/risk_score/analysis_factors ajoutees, expected_return rendue nullable.")
