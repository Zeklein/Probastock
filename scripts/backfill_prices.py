"""Peuple l'historique (jusqu'a 1 an) de price_snapshots pour tous les actifs actifs,
pour que les indicateurs techniques (RSI, moyennes mobiles, etc.) aient assez de
donnees des le depart.

fetched_at = timestamp de l'execution de ce script, pas la date historique de chaque
barre. C'est un backfill pragmatique : pour des cours OHLCV bruts, quasiment jamais
revises a posteriori, ce n'est pas un vrai risque de look-ahead bias (contrairement
aux fondamentaux, qui eux sont parfois retraites). Mais fetched_at ne represente donc
pas rigoureusement "ce qui etait connu ce jour-la" pour ces lignes historiques.

Un actif peut avoir moins d'un an d'historique disponible (IPO recente) : c'est gere
normalement, on insere ce qui existe et on le signale dans le resume.

Idempotent : ne duplique pas une ligne (asset_id, trade_date) deja presente
(notamment le jour deja collecte par collect_prices.py).

Usage: python scripts/backfill_prices.py
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

PAUSE_BETWEEN_REQUESTS_SECONDS = 1.0
HISTORY_PERIOD = "1y"


def main():
    run_fetched_at = datetime.now(timezone.utc)

    with engine.connect() as conn:
        assets = conn.execute(
            text("SELECT id, ticker FROM assets WHERE is_active = true ORDER BY id")
        ).fetchall()
        existing = set(
            conn.execute(text("SELECT asset_id, trade_date FROM price_snapshots")).fetchall()
        )

    results = []  # (ticker, jours_inseres, jours_deja_presents, erreur|None)

    with engine.begin() as conn:
        for i, (asset_id, ticker) in enumerate(assets):
            try:
                tk = yf.Ticker(ticker)
                hist = tk.history(period=HISTORY_PERIOD)
                if hist.empty:
                    raise ValueError("aucune donnee retournee par yfinance")
                currency = tk.fast_info.get("currency")

                inserted = 0
                skipped = 0
                for bar in extract_bars(hist):
                    if (asset_id, bar["trade_date"]) in existing:
                        skipped += 1
                        continue
                    conn.execute(
                        INSERT_SQL,
                        {
                            "asset_id": asset_id,
                            "fetched_at": run_fetched_at,
                            "source": SOURCE,
                            "currency": currency,
                            **bar,
                        },
                    )
                    existing.add((asset_id, bar["trade_date"]))
                    inserted += 1

                results.append((ticker, inserted, skipped, None))
            except Exception as e:
                results.append((ticker, 0, 0, str(e)))

            if i < len(assets) - 1:
                time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    total_inserted = sum(r[1] for r in results)
    failed = [r for r in results if r[3] is not None]

    print(f"Total lignes inserees : {total_inserted}")
    print(f"Actifs en echec       : {len(failed)}/{len(results)}")
    print()
    print(f"{'ticker':<12}{'inseres':<10}{'deja presents':<16}erreur")
    for ticker, inserted, skipped, err in results:
        print(f"{ticker:<12}{inserted:<10}{skipped:<16}{err or ''}")


if __name__ == "__main__":
    main()
