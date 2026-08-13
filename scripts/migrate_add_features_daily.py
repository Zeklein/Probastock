"""Migration ponctuelle : cree la table features_daily.

Usage: python scripts/migrate_add_features_daily.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

SQL = """
CREATE TABLE IF NOT EXISTS features_daily (
    id                  BIGSERIAL PRIMARY KEY,
    asset_id            INTEGER     NOT NULL REFERENCES assets(id),
    trade_date          DATE        NOT NULL,

    return_1d           NUMERIC(8, 5),
    return_5d           NUMERIC(8, 5),
    return_20d          NUMERIC(8, 5),
    volatility_20d      NUMERIC(8, 5),

    sma_20              NUMERIC(12, 4),
    sma_50              NUMERIC(12, 4),
    rsi_14              NUMERIC(6, 3)
                        CHECK (rsi_14 BETWEEN 0 AND 100),

    volume_avg_20d      NUMERIC(16, 4),
    volume_ratio        NUMERIC(10, 4),

    feature_version     TEXT        NOT NULL DEFAULT 'v1',
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_features_unique ON features_daily(asset_id, trade_date, feature_version);
CREATE INDEX IF NOT EXISTS idx_features_asset_date ON features_daily(asset_id, trade_date DESC);
"""

if __name__ == "__main__":
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(SQL)
        raw.commit()
    finally:
        raw.close()
    print("Table features_daily creee (ou deja presente).")
