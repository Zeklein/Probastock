"""Migration ponctuelle : ajoute news_items.provider (quelle API a fourni la
ligne : 'alphavantage' / 'finnhub' / 'marketaux'), distinct de la colonne
'source' existante qui est le MEDIA/editeur (Reuters, Benzinga...), pas l'API.
Backfill des lignes existantes (toutes issues d'Alpha Vantage a ce jour) pour
que la colonne soit exploitable immediatement sur tout l'historique.

Usage: python scripts/migrate_add_news_provider.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

SQL = """
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS provider TEXT;
UPDATE news_items SET provider = 'alphavantage' WHERE provider IS NULL;
ALTER TABLE news_items ALTER COLUMN provider SET NOT NULL;
"""

if __name__ == "__main__":
    with engine.begin() as conn:
        conn.execute(text(SQL))
    print("Colonne news_items.provider ajoutee, backfill 'alphavantage' applique.")
