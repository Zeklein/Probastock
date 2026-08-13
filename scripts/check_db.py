"""Verifie que la connexion a la base Supabase fonctionne.

Usage: python scripts/check_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

if __name__ == "__main__":
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
        print("Connexion OK")
        print(version)
    except Exception as e:
        print(f"Connexion echouee: {e}")
        sys.exit(1)
