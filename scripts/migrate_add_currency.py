"""Migration ponctuelle : ajoute la colonne currency a price_snapshots.

Usage: python scripts/migrate_add_currency.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

if __name__ == "__main__":
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE price_snapshots ADD COLUMN IF NOT EXISTS currency TEXT"))
    print("Colonne currency ajoutee (ou deja presente) sur price_snapshots.")
