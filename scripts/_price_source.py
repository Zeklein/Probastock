"""Logique partagee entre collect_prices.py et backfill_prices.py :
extraction des barres OHLCV depuis un DataFrame yfinance et insertion en base.
"""
from sqlalchemy import text

SOURCE = "yahoo_finance"

INSERT_SQL = text(
    """
    INSERT INTO price_snapshots (asset_id, trade_date, fetched_at, open, high, low, close, volume, source, currency)
    VALUES (:asset_id, :trade_date, :fetched_at, :open, :high, :low, :close, :volume, :source, :currency)
    """
)


def _clean(value):
    return float(value) if value == value else None  # NaN check


def extract_bars(hist):
    """Convertit un DataFrame yfinance (une ou plusieurs lignes) en liste de dicts
    OHLCV pretes a inserer. Les lignes sans close (NaN) sont ignorees."""
    bars = []
    for trade_date, row in hist.iterrows():
        if row["Close"] != row["Close"]:  # NaN check
            continue
        bars.append(
            {
                "trade_date": trade_date.date(),
                "open": _clean(row["Open"]),
                "high": _clean(row["High"]),
                "low": _clean(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else None,
            }
        )
    return bars
