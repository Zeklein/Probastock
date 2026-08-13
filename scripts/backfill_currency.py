"""Backfill ponctuel : renseigne currency sur les lignes price_snapshots existantes
qui ne l'ont pas encore (pas d'insertion, uniquement UPDATE des lignes deja presentes).

Usage: python scripts/backfill_currency.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._ca_bundle import ensure_ca_bundle

ensure_ca_bundle()

import yfinance as yf
from sqlalchemy import text

from app.db import engine

PAUSE_BETWEEN_REQUESTS_SECONDS = 1.0

if __name__ == "__main__":
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT a.ticker
                FROM price_snapshots p
                JOIN assets a ON a.id = p.asset_id
                WHERE p.currency IS NULL
                """
            )
        ).fetchall()

    tickers = [r[0] for r in rows]
    print(f"{len(tickers)} ticker(s) a completer.")

    with engine.begin() as conn:
        for i, ticker in enumerate(tickers):
            currency = yf.Ticker(ticker).fast_info.get("currency")
            conn.execute(
                text(
                    """
                    UPDATE price_snapshots p
                    SET currency = :currency
                    FROM assets a
                    WHERE p.asset_id = a.id AND a.ticker = :ticker AND p.currency IS NULL
                    """
                ),
                {"currency": currency, "ticker": ticker},
            )
            print(f"  {ticker}: {currency}")
            if i < len(tickers) - 1:
                time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    print("Backfill termine.")
