"""Calcule les indicateurs techniques (features_daily) a partir de l'historique
de price_snapshots, pour chaque actif et chaque jour disponible.

Regle de deduplication : pour un (asset_id, trade_date) donne, price_snapshots
peut contenir plusieurs lignes (plusieurs collectes le meme jour, comportement
normal de l'architecture append-only). On ne garde QUE la ligne au fetched_at
le plus recent pour chaque (asset_id, trade_date) -- c'est le "SELECT DISTINCT
ON (asset_id, trade_date) ... ORDER BY fetched_at DESC" ci-dessous. Ignorer
cette regle ferait halluciner des points de donnees fantomes.

Indicateurs calcules avec les rolling/ewm de pandas (pas de reimplementation
manuelle de SMA/RSI). Quand l'historique est insuffisant (ex: SPCX recemment
introduit en bourse), pandas produit naturellement des NaN, convertis en NULL
en base plutot que plantage ou valeur inventee.

Usage: python scripts/compute_features.py
"""
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from sqlalchemy import text

from app.db import engine

FEATURE_VERSION = "v1"

# DISTINCT ON (Postgres) : ne garde que la ligne au fetched_at le plus recent
# par (asset_id, trade_date). Voir docstring du module.
PRICES_SQL = text(
    """
    SELECT DISTINCT ON (asset_id, trade_date)
        asset_id, trade_date, close, volume
    FROM price_snapshots
    ORDER BY asset_id, trade_date, fetched_at DESC
    """
)

UPSERT_SQL = text(
    """
    INSERT INTO features_daily (
        asset_id, trade_date, return_1d, return_5d, return_20d, volatility_20d,
        sma_20, sma_50, rsi_14, volume_avg_20d, volume_ratio, feature_version, computed_at
    ) VALUES (
        :asset_id, :trade_date, :return_1d, :return_5d, :return_20d, :volatility_20d,
        :sma_20, :sma_50, :rsi_14, :volume_avg_20d, :volume_ratio, :feature_version, :computed_at
    )
    ON CONFLICT (asset_id, trade_date, feature_version) DO UPDATE SET
        return_1d = EXCLUDED.return_1d,
        return_5d = EXCLUDED.return_5d,
        return_20d = EXCLUDED.return_20d,
        volatility_20d = EXCLUDED.volatility_20d,
        sma_20 = EXCLUDED.sma_20,
        sma_50 = EXCLUDED.sma_50,
        rsi_14 = EXCLUDED.rsi_14,
        volume_avg_20d = EXCLUDED.volume_avg_20d,
        volume_ratio = EXCLUDED.volume_ratio,
        computed_at = EXCLUDED.computed_at
    """
)


def rsi_14(close: pd.Series) -> pd.Series:
    """RSI 14 avec lissage de Wilder (= EMA de facteur alpha=1/14), formule
    standard. min_periods=14 garantit du NaN tant qu'il n'y a pas assez
    d'historique, plutot qu'une valeur peu fiable."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_features_for_asset(df: pd.DataFrame) -> pd.DataFrame:
    """df : colonnes trade_date, close, volume, triees par trade_date croissant."""
    close = df["close"]
    volume = df["volume"]
    daily_return = close.pct_change()

    out = pd.DataFrame({"trade_date": df["trade_date"]})
    out["return_1d"] = daily_return
    out["return_5d"] = close.pct_change(5)
    out["return_20d"] = close.pct_change(20)
    out["volatility_20d"] = daily_return.rolling(20).std()
    out["sma_20"] = close.rolling(20).mean()
    out["sma_50"] = close.rolling(50).mean()
    out["rsi_14"] = rsi_14(close)
    out["volume_avg_20d"] = volume.rolling(20).mean()
    out["volume_ratio"] = volume / out["volume_avg_20d"]
    return out


def to_param_rows(asset_id: int, features: pd.DataFrame, computed_at) -> list[dict]:
    rows = []
    for record in features.to_dict("records"):
        row = {
            k: (None if isinstance(v, float) and not math.isfinite(v) else v)
            for k, v in record.items()
        }
        row["asset_id"] = asset_id
        row["feature_version"] = FEATURE_VERSION
        row["computed_at"] = computed_at
        rows.append(row)
    return rows


def main():
    computed_at = datetime.now(timezone.utc)

    with engine.connect() as conn:
        prices = pd.DataFrame(
            conn.execute(PRICES_SQL).fetchall(),
            columns=["asset_id", "trade_date", "close", "volume"],
        )
        assets = conn.execute(text("SELECT id, ticker FROM assets ORDER BY id")).fetchall()

    prices["close"] = prices["close"].astype(float)
    prices["volume"] = prices["volume"].astype(float)

    total_rows = 0
    summary = []

    with engine.begin() as conn:
        for asset_id, ticker in assets:
            asset_prices = prices[prices["asset_id"] == asset_id].sort_values("trade_date")
            if asset_prices.empty:
                summary.append((ticker, 0, 0))
                continue

            features = compute_features_for_asset(asset_prices)
            rows = to_param_rows(asset_id, features, computed_at)
            conn.execute(UPSERT_SQL, rows)

            total_rows += len(rows)
            summary.append((ticker, len(rows), len(asset_prices)))

    print(f"Total lignes upsertees dans features_daily : {total_rows}")
    print()
    print(f"{'ticker':<12}{'features':<10}{'jours de prix'}")
    for ticker, nb_features, nb_days in summary:
        print(f"{ticker:<12}{nb_features:<10}{nb_days}")


if __name__ == "__main__":
    main()
