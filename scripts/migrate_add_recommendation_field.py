"""Migration ponctuelle : ajoute predictions.recommendation (BUY/HOLD/REDUCE/SELL),
nullable pour rester compatible avec les lignes deja stockees.

Usage: python scripts/migrate_add_recommendation_field.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

SQL = """
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS recommendation TEXT
    CHECK (recommendation IN ('BUY', 'HOLD', 'REDUCE', 'SELL'));
"""

if __name__ == "__main__":
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(SQL)
        raw.commit()
    finally:
        raw.close()
    print("Colonne recommendation ajoutee (nullable, compatible avec l'existant).")
