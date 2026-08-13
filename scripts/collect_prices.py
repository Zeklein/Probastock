"""Collecte les cours du jour pour tous les actifs actifs et les insere dans price_snapshots.

Append-only : jamais d'UPDATE, uniquement des INSERT (une ligne par actif par execution).

Usage: python scripts/collect_prices.py
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._ca_bundle import ensure_ca_bundle

ensure_ca_bundle()

import yfinance as yf
from sqlalchemy import text

from app.db import engine
from scripts._price_source import INSERT_SQL, SOURCE, extract_bars

PAUSE_BETWEEN_REQUESTS_SECONDS = 1.0  # ponytail: pause fixe, passer a un backoff si le rate-limit persiste


def fetch_last_bar(ticker: str):
    tk = yf.Ticker(ticker)
    hist = tk.history(period="1d")
    if hist.empty:
        raise ValueError("aucune donnee retournee par yfinance")
    bars = extract_bars(hist)
    if not bars:
        raise ValueError("close manquant dans la donnee retournee")
    return {**bars[-1], "currency": tk.fast_info.get("currency")}


def main():
    run_fetched_at = datetime.now(timezone.utc)

    with engine.connect() as conn:
        assets = conn.execute(
            text("SELECT id, ticker FROM assets WHERE is_active = true ORDER BY id")
        ).fetchall()

    succeeded = []
    failed = []

    with engine.begin() as conn:
        for i, (asset_id, ticker) in enumerate(assets):
            try:
                bar = fetch_last_bar(ticker)
                conn.execute(
                    INSERT_SQL,
                    {
                        "asset_id": asset_id,
                        "fetched_at": run_fetched_at,
                        "source": SOURCE,
                        **bar,
                    },
                )
                succeeded.append((ticker, bar))
            except Exception as e:
                failed.append((ticker, str(e)))

            if i < len(assets) - 1:
                time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    print(f"Reussis : {len(succeeded)}/{len(assets)}")
    print(f"Echecs  : {len(failed)}/{len(assets)}")
    if failed:
        print("\nDetail des echecs :")
        for ticker, err in failed:
            print(f"  {ticker}: {err}")


if __name__ == "__main__":
    main()
