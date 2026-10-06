"""Momentum multi-horizon (relatif vs SPY) de chaque actif actif.

Lit l'historique de price_snapshots (aucun appel reseau), calcule le momentum
(cf. app/momentum.py), enregistre un snapshot immuable dans momentum_snapshots,
puis --settle renseigne les resultats reels (momentum_outcomes) des snapshots
arrives a echeance (5, 20, 60 seances).

Regle de deduplication : comme compute_features.py, on ne garde que la ligne au
fetched_at le plus recent pour chaque (asset_id, trade_date).

Idempotent : rejouer le meme jour n'ajoute rien (unique sur asset_id, as_of,
config_version). A lancer apres collect_prices.py.

Usage:
  python scripts/compute_momentum.py --dry-run   # calcule et affiche, n'ecrit rien
  python scripts/compute_momentum.py             # calcule et enregistre le snapshot
  python scripts/compute_momentum.py --settle    # renseigne les resultats arrives a echeance
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.momentum import (
    CONFIG_VERSION,
    OUTCOME_HORIZONS,
    PARAMS,
    compute_snapshot,
    settle_outcomes,
)

BENCHMARK_TICKER = "SPY"
FFILL_LIMIT_DAYS = 5  # calendriers de places differentes : un jour ferme reprend le dernier cours

PRICES_SQL = text(
    """
    SELECT DISTINCT ON (p.asset_id, p.trade_date)
        a.ticker, p.trade_date, p.close
    FROM price_snapshots p
    JOIN assets a ON a.id = p.asset_id
    ORDER BY p.asset_id, p.trade_date, p.fetched_at DESC
    """
)

ACTIVE_ASSETS_SQL = text("SELECT id, ticker, name FROM assets WHERE is_active = true ORDER BY ticker")

COUNT_SNAPSHOTS_SQL = text(
    "SELECT count(*) FROM momentum_snapshots WHERE as_of = :as_of AND config_version = :config_version"
)

INSERT_SNAPSHOT_SQL = text(
    """
    INSERT INTO momentum_snapshots (
        asset_id, as_of, benchmark, close,
        ex_r12_1, ex_r3m, ex_r1m, ex_r5d,
        pct_r12_1, pct_r3m, pct_r1m, pct_r5d,
        score, bias, timing, state, alignment, n_measures,
        config_version, params
    ) VALUES (
        :asset_id, :as_of, :benchmark, :close,
        :ex_r12_1, :ex_r3m, :ex_r1m, :ex_r5d,
        :pct_r12_1, :pct_r3m, :pct_r1m, :pct_r5d,
        :score, :bias, :timing, :state, :alignment, :n_measures,
        :config_version, CAST(:params AS jsonb)
    )
    ON CONFLICT (asset_id, as_of, config_version) DO NOTHING
    """
)

PENDING_SQL = text(
    """
    SELECT s.id AS snapshot_id, s.as_of, a.ticker, s.benchmark, v.h AS horizon_days
    FROM momentum_snapshots s
    JOIN assets a ON a.id = s.asset_id
    CROSS JOIN (SELECT unnest(CAST(:horizons AS integer[])) AS h) v
    LEFT JOIN momentum_outcomes o ON o.snapshot_id = s.id AND o.horizon_days = v.h
    WHERE o.snapshot_id IS NULL
    ORDER BY s.as_of, a.ticker, v.h
    """
)

COUNT_OUTCOMES_SQL = text("SELECT count(*) FROM momentum_outcomes")

INSERT_OUTCOME_SQL = text(
    """
    INSERT INTO momentum_outcomes (snapshot_id, horizon_days, end_date, ret, bench_ret, excess, outperformed)
    VALUES (:snapshot_id, :horizon_days, :end_date, :ret, :bench_ret, :excess, :outperformed)
    ON CONFLICT (snapshot_id, horizon_days) DO NOTHING
    """
)


def _py(value):
    """Valeur Python native pour psycopg2 : NaN/NA -> None, numpy -> int/float."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def to_param_rows(df: pd.DataFrame) -> list[dict]:
    return [{k: _py(v) for k, v in record.items()} for record in df.to_dict("records")]


def load_prices(conn) -> pd.DataFrame:
    """Cours de cloture, dates x tickers, calendrier commun, ffill limite."""
    long = pd.DataFrame(conn.execute(PRICES_SQL).fetchall(), columns=["ticker", "trade_date", "close"])
    if long.empty:
        raise SystemExit("Aucun cours dans price_snapshots.")
    long["close"] = long["close"].astype(float)
    long["trade_date"] = pd.to_datetime(long["trade_date"])
    px = long.pivot(index="trade_date", columns="ticker", values="close").sort_index()
    return px.ffill(limit=FFILL_LIMIT_DAYS)


def run_snapshot(dry_run: bool) -> None:
    with engine.connect() as conn:
        px = load_prices(conn)
        assets = pd.DataFrame(conn.execute(ACTIVE_ASSETS_SQL).fetchall(), columns=["id", "ticker", "name"])

    if BENCHMARK_TICKER not in px.columns:
        raise SystemExit(f"Benchmark {BENCHMARK_TICKER} sans cours : calcul impossible.")

    meta = assets[["ticker", "name"]].assign(benchmark=BENCHMARK_TICKER)
    snap = compute_snapshot(px, meta)

    missing = sorted(set(assets["ticker"]) - set(snap["ticker"]) - {BENCHMARK_TICKER})
    if missing:
        print(f"Sans cours, ignores : {', '.join(missing)}")

    show = snap[
        ["ticker", "score", "state", "alignment", "n_measures", "ex_r12_1", "ex_r3m", "ex_r1m", "ex_r5d"]
    ].sort_values("score", ascending=False)
    with pd.option_context("display.width", 200, "display.float_format", "{:.3f}".format):
        print(f"Momentum au {snap['as_of'].iloc[0]} ({len(snap)} titres, benchmark {BENCHMARK_TICKER})")
        print(show.to_string(index=False))

    short = snap.loc[snap["n_measures"] < 4, "ticker"].tolist()
    if short:
        print(f"\nHistorique trop court pour toutes les mesures : {', '.join(short)}")

    if dry_run:
        return

    snap = snap.merge(assets[["id", "ticker"]].rename(columns={"id": "asset_id"}), on="ticker")
    snap["config_version"] = CONFIG_VERSION
    snap["params"] = json.dumps(PARAMS)
    as_of = snap["as_of"].iloc[0]
    count_args = {"as_of": as_of, "config_version": CONFIG_VERSION}

    with engine.begin() as conn:
        before = conn.execute(COUNT_SNAPSHOTS_SQL, count_args).scalar()
        conn.execute(INSERT_SNAPSHOT_SQL, to_param_rows(snap))
        after = conn.execute(COUNT_SNAPSHOTS_SQL, count_args).scalar()

    print(f"\n{after - before} nouvelles lignes enregistrees pour le {as_of} ({after} au total ce jour-la).")
    if after == before:
        print("Rien de nouveau : snapshot de cette date deja present (cours du jour non collectes ?).")


def run_settle() -> None:
    with engine.connect() as conn:
        pending = pd.DataFrame(
            conn.execute(PENDING_SQL, {"horizons": list(OUTCOME_HORIZONS)}).fetchall(),
            columns=["snapshot_id", "as_of", "ticker", "benchmark", "horizon_days"],
        )
        if pending.empty:
            print("Rien a regler.")
            return
        px = load_prices(conn)

    done = settle_outcomes(px, pending)
    print(f"{len(pending)} echeances en attente, {len(done)} arrivees a terme.")
    if done.empty:
        return

    with engine.begin() as conn:
        before = conn.execute(COUNT_OUTCOMES_SQL).scalar()
        conn.execute(INSERT_OUTCOME_SQL, to_param_rows(done))
        after = conn.execute(COUNT_OUTCOMES_SQL).scalar()
    print(f"{after - before} resultats enregistres.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="calcule et affiche, n'ecrit rien")
    ap.add_argument("--settle", action="store_true", help="renseigne les resultats arrives a echeance")
    args = ap.parse_args()

    if args.settle:
        run_settle()
    else:
        run_snapshot(args.dry_run)


if __name__ == "__main__":
    main()
