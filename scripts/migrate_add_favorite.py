"""Migration ponctuelle : ajoute la colonne is_favorite a assets (affichage/tri
dashboard uniquement, aucun impact sur le pipeline d'analyse automatise).

Usage: python scripts/migrate_add_favorite.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

if __name__ == "__main__":
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE assets ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN NOT NULL DEFAULT false"))
    print("Colonne is_favorite ajoutee (ou deja presente) sur assets.")
