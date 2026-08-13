"""Applique app/db/schema.sql a la base Supabase.

Usage: python scripts/apply_schema.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"

if __name__ == "__main__":
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql)
        raw.commit()
    finally:
        raw.close()
    print(f"Schema applique depuis {SCHEMA_PATH}")
