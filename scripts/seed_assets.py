"""Remplit la table assets avec les 34 valeurs suivies.

Usage: python scripts/seed_assets.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db import engine

# (ticker, name, asset_type, notes)
ASSETS = [
    ("AVAV", "AeroVironment", "stock", None),
    ("AMD", "AMD", "stock", None),
    ("ASTS", "AST SpaceMobile", "stock", None),
    ("BKSY", "BlackSky Technology", "stock", None),
    ("COHR", "Coherent", "stock", None),
    ("CRWV", "CoreWeave", "stock", None),
    ("ECHO", "EchoStar", "stock", None),
    ("ESLT", "Elbit Systems", "stock", None),
    ("FTC.L", "Filtronic", "stock", "LSE AIM"),
    ("SHLD", "Global X Defense Tech", "etf", None),
    ("ORBX", "Global X Space Tech", "etf", None),
    ("GFS", "GlobalFoundries", "stock", None),
    ("GSAT", "Globalstar", "stock", None),
    ("ITA", "iShares US Aerospace & Defense", "etf", None),
    ("LDOS", "Leidos", "stock", None),
    ("LITE", "Lumentum Holdings", "stock", None),
    ("LYSDY", "Lynas Rare Earths", "stock", "ADR OTC"),
    ("MRVL", "Marvell", "stock", None),
    ("META", "Meta Platforms", "stock", None),
    ("MU", "Micron", "stock", None),
    ("MP", "MP Materials", "stock", None),
    ("PL", "Planet Labs PBC", "stock", None),
    ("UFO", "Procure Space", "etf", None),
    ("RKLB", "Rocket Lab", "stock", None),
    ("RTX", "RTX Corp", "stock", None),
    ("SIDU", "Sidus Space", "stock", None),
    ("000660.KS", "SK Hynix", "stock", "Korea Exchange"),
    ("SPCX", "SpaceX", "stock", "IPO juin 2026, historique limité"),
    ("SPY", "SPDR S&P 500", "etf", "benchmark"),
    ("STM", "STMicroelectronics", "stock", "ADR NYSE, alternative à STMPA.PA"),
    ("NASA", "Tema Space Innovators", "etf", None),
    ("USAR", "USA Rare Earth", "stock", None),
    ("NLR", "VanEck Uranium+Nuclear Energy", "etf", None),
    ("GOOG", "Alphabet Inc Class C", "stock", None),
]

INSERT_SQL = text(
    """
    INSERT INTO assets (ticker, name, asset_type, notes)
    VALUES (:ticker, :name, :asset_type, :notes)
    ON CONFLICT (ticker) DO UPDATE
        SET name = EXCLUDED.name,
            asset_type = EXCLUDED.asset_type,
            notes = EXCLUDED.notes
    """
)

if __name__ == "__main__":
    assert len(ASSETS) == 34, f"attendu 34 actifs, trouve {len(ASSETS)}"

    with engine.begin() as conn:
        for ticker, name, asset_type, notes in ASSETS:
            conn.execute(
                INSERT_SQL,
                {"ticker": ticker, "name": name, "asset_type": asset_type, "notes": notes},
            )

    print(f"{len(ASSETS)} actifs inseres/mis a jour.")
