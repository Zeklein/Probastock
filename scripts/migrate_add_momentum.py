"""Migration ponctuelle : cree les tables momentum_snapshots et momentum_outcomes.

Idempotente (IF NOT EXISTS / OR REPLACE) : rejouee a chaque run par le workflow
momentum.yml, donc aucune etape manuelle avant le premier calcul.

momentum_snapshots est immuable (trigger bloquant UPDATE/DELETE), comme le ledger
predictions : une valeur enregistree ne se reecrit jamais. Les resultats reels
arrivent plus tard dans momentum_outcomes.

Usage: python scripts/migrate_add_momentum.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine

SQL = """
CREATE TABLE IF NOT EXISTS momentum_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    asset_id        INTEGER     NOT NULL REFERENCES assets(id),
    as_of           DATE        NOT NULL,          -- derniere seance incluse dans le calcul
    benchmark       TEXT        NOT NULL,          -- ticker du benchmark, ex: "SPY"
    close           NUMERIC(14, 4),

    -- exces de rendement vs benchmark (rendement titre - rendement benchmark)
    ex_r12_1        NUMERIC,
    ex_r3m          NUMERIC,
    ex_r1m          NUMERIC,
    ex_r5d          NUMERIC,
    -- rang percentile entre les titres suivis (0-100, 100 = meilleur)
    pct_r12_1       NUMERIC,
    pct_r3m         NUMERIC,
    pct_r1m         NUMERIC,
    pct_r5d         NUMERIC,

    score           NUMERIC(5, 2) CHECK (score BETWEEN 0 AND 100),
    bias            NUMERIC,                       -- exces moyen 12-1 et 3 mois
    timing          NUMERIC,                       -- exces moyen 1 mois et 5 jours
    state           TEXT        NOT NULL,
    alignment       SMALLINT,                      -- mesures avec exces > 0 (0 a 4)
    n_measures      SMALLINT,                      -- mesures calculables (historique court)

    config_version  TEXT        NOT NULL,
    params          JSONB       NOT NULL,          -- fenetres, poids, signes utilises
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_momentum_snap_unique
    ON momentum_snapshots(asset_id, as_of, config_version);
CREATE INDEX IF NOT EXISTS idx_momentum_snap_asset_date
    ON momentum_snapshots(asset_id, as_of DESC);

CREATE TABLE IF NOT EXISTS momentum_outcomes (
    snapshot_id     BIGINT      NOT NULL REFERENCES momentum_snapshots(id),
    horizon_days    SMALLINT    NOT NULL,          -- 5, 20, 60 seances
    end_date        DATE        NOT NULL,
    ret             NUMERIC,
    bench_ret       NUMERIC,
    excess          NUMERIC,
    outperformed    BOOLEAN,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id, horizon_days)
);

CREATE OR REPLACE FUNCTION momentum_forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'momentum_snapshots est immuable';
END;
$fn$;

DROP TRIGGER IF EXISTS momentum_snapshots_immutable ON momentum_snapshots;
CREATE TRIGGER momentum_snapshots_immutable
    BEFORE UPDATE OR DELETE ON momentum_snapshots
    FOR EACH ROW EXECUTE FUNCTION momentum_forbid_change();

-- Pas d'acces via l'API publique Supabase : l'application se connecte en direct.
ALTER TABLE momentum_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE momentum_outcomes  ENABLE ROW LEVEL SECURITY;
"""

if __name__ == "__main__":
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(SQL)
        raw.commit()
    finally:
        raw.close()
    print("Tables momentum_snapshots et momentum_outcomes creees (ou deja presentes).")
