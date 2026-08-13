from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from app.db import engine

app = FastAPI(title="Probastock", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SPARKLINE_DAYS = 30

ASSET_TYPE_ALIASES = {"action": "stock", "stock": "stock", "etf": "etf"}


class AssetIn(BaseModel):
    ticker: str
    name: str
    sector: str | None = None
    type: str  # "action" ou "ETF"

ASSETS_SQL = text("SELECT id, ticker, name, sector FROM assets WHERE is_active = true ORDER BY ticker")

# Une seule ligne par actif : le trigger unique (asset_id, trade_date, feature_version)
# sur features_daily garantit qu'il n'y a pas de doublon a dedupliquer ici.
LATEST_FEATURES_SQL = text(
    """
    SELECT DISTINCT ON (asset_id)
        asset_id, return_1d, rsi_14, sma_20
    FROM features_daily
    ORDER BY asset_id, trade_date DESC
    """
)

# Regle de dedup (cf. compute_features.py) : pour un (asset_id, trade_date) donne,
# price_snapshots peut contenir plusieurs lignes (plusieurs collectes le meme
# jour). On ne garde que la plus recente par fetched_at avant de prendre les
# N derniers jours.
SPARKLINE_SQL = text(
    """
    WITH latest_prices AS (
        SELECT DISTINCT ON (asset_id, trade_date)
            asset_id, trade_date, close
        FROM price_snapshots
        ORDER BY asset_id, trade_date, fetched_at DESC
    ),
    ranked_prices AS (
        SELECT asset_id, trade_date, close,
               ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY trade_date DESC) AS rn
        FROM latest_prices
    )
    SELECT asset_id, trade_date, close
    FROM ranked_prices
    WHERE rn <= :sparkline_days
    ORDER BY asset_id, trade_date ASC
    """
)


@app.get("/")
def root():
    return {"status": "ok", "project": "Probastock"}


@app.get("/dashboard")
def dashboard_page():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api/dashboard")
def dashboard():
    with engine.connect() as conn:
        assets = conn.execute(ASSETS_SQL).mappings().all()
        features_by_asset = {
            r["asset_id"]: r for r in conn.execute(LATEST_FEATURES_SQL).mappings().all()
        }
        sparkline_by_asset = defaultdict(list)
        for r in conn.execute(SPARKLINE_SQL, {"sparkline_days": SPARKLINE_DAYS}).mappings().all():
            sparkline_by_asset[r["asset_id"]].append({"trade_date": r["trade_date"], "close": r["close"]})

    result = defaultdict(list)
    for asset in assets:
        sparkline = sparkline_by_asset.get(asset["id"], [])
        if not sparkline:
            continue  # aucun cours collecte pour cet actif pour l'instant

        close = sparkline[-1]["close"]
        features = features_by_asset.get(asset["id"], {})
        sma_20 = features.get("sma_20")

        trend = None
        if close is not None and sma_20 is not None:
            trend = "above" if close > sma_20 else "below"

        result[asset["sector"]].append(
            {
                "ticker": asset["ticker"],
                "name": asset["name"],
                "close": close,
                "return_1d": features.get("return_1d"),
                "rsi_14": features.get("rsi_14"),
                "trend": trend,
                "sparkline": sparkline,
            }
        )

    return result


@app.get("/api/assets")
def list_assets():
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT ticker, name, sector, asset_type, notes, is_active, created_at
                FROM assets ORDER BY is_active DESC, ticker
                """
            )
        ).mappings().all()
    return list(rows)


@app.post("/api/assets", status_code=201)
def create_asset(payload: AssetIn):
    asset_type = ASSET_TYPE_ALIASES.get(payload.type.strip().lower())
    if asset_type is None:
        raise HTTPException(status_code=422, detail=f"type invalide : {payload.type!r} (attendu 'action' ou 'ETF')")

    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM assets WHERE ticker = :ticker"), {"ticker": payload.ticker}
        ).first()
        if exists:
            raise HTTPException(
                status_code=409,
                detail=f"Le ticker {payload.ticker} existe deja (actif ou archive).",
            )

        row = conn.execute(
            text(
                """
                INSERT INTO assets (ticker, name, sector, asset_type)
                VALUES (:ticker, :name, :sector, :asset_type)
                RETURNING ticker, name, sector, asset_type, is_active, created_at
                """
            ),
            {"ticker": payload.ticker, "name": payload.name, "sector": payload.sector, "asset_type": asset_type},
        ).mappings().first()

    return {
        **row,
        "message": (
            "Actif cree, mais aucun historique n'a ete recupere automatiquement "
            "(operation trop longue pour une requete HTTP synchrone). Il commencera "
            "a accumuler des cours a la prochaine execution de collect_prices.py."
        ),
    }


def _set_active(ticker: str, is_active: bool):
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE assets SET is_active = :is_active WHERE ticker = :ticker
                RETURNING ticker, name, sector, asset_type, is_active
                """
            ),
            {"is_active": is_active, "ticker": ticker},
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Ticker {ticker} introuvable.")
    return row


@app.patch("/api/assets/{ticker}/archive")
def archive_asset(ticker: str):
    return _set_active(ticker, False)


@app.patch("/api/assets/{ticker}/reactivate")
def reactivate_asset(ticker: str):
    return _set_active(ticker, True)


DETAIL_INDICATORS_DAYS = 90

# Historique de prix complet (pas de limite de jours), deduplique par la regle
# habituelle (derniere collecte du jour = fetched_at le plus recent).
DETAIL_PRICE_HISTORY_SQL = text(
    """
    SELECT DISTINCT ON (trade_date) trade_date, open, high, low, close, volume
    FROM price_snapshots
    WHERE asset_id = :asset_id
    ORDER BY trade_date, fetched_at DESC
    """
)

# Indicateurs limites aux 90 derniers jours (cf. etape 1) : sous-requete pour
# trier par date descendante avant de limiter, puis on re-trie en ascendant
# pour l'affichage chronologique du graphique.
DETAIL_INDICATORS_SQL = text(
    """
    SELECT * FROM (
        SELECT trade_date, return_1d, return_5d, return_20d, volatility_20d,
               sma_20, sma_50, rsi_14, volume_ratio
        FROM features_daily
        WHERE asset_id = :asset_id
        ORDER BY trade_date DESC
        LIMIT :days
    ) recent
    ORDER BY trade_date ASC
    """
)


@app.get("/api/assets/{ticker}/detail")
def asset_detail(ticker: str):
    with engine.connect() as conn:
        asset = conn.execute(
            text("SELECT id, ticker, name, sector, asset_type FROM assets WHERE ticker = :ticker"),
            {"ticker": ticker},
        ).mappings().first()
        if asset is None:
            raise HTTPException(status_code=404, detail=f"Ticker {ticker} introuvable.")

        price_history = conn.execute(
            DETAIL_PRICE_HISTORY_SQL, {"asset_id": asset["id"]}
        ).mappings().all()
        indicators_history = conn.execute(
            DETAIL_INDICATORS_SQL, {"asset_id": asset["id"], "days": DETAIL_INDICATORS_DAYS}
        ).mappings().all()

    return {
        "ticker": asset["ticker"],
        "name": asset["name"],
        "sector": asset["sector"],
        "asset_type": asset["asset_type"],
        "price_history": list(price_history),
        "indicators_history": list(indicators_history),
    }


NEWS_PLACEHOLDER = "(collecte de news pas encore implementee)"

# Derniere ligne de features_daily par actif, jointe a assets pour recuperer
# le secteur et le statut : sert a la fois a la fiche de l'actif demande et
# au calcul de la moyenne sectorielle (return_20d des autres actifs actifs
# du meme secteur) et au benchmark SPY, sans reinterroger la base 3 fois.
FICHE_LATEST_FEATURES_SQL = text(
    """
    SELECT DISTINCT ON (f.asset_id)
        f.asset_id, a.ticker, a.sector, a.is_active,
        f.trade_date, f.return_1d, f.return_5d, f.return_20d, f.volatility_20d,
        f.sma_20, f.sma_50, f.rsi_14, f.volume_ratio
    FROM features_daily f
    JOIN assets a ON a.id = f.asset_id
    ORDER BY f.asset_id, f.trade_date DESC
    """
)

# Regle de dedup habituelle : pour le dernier trade_date de l'actif, on prend
# la ligne au fetched_at le plus recent.
FICHE_LATEST_PRICE_SQL = text(
    """
    SELECT DISTINCT ON (asset_id) asset_id, trade_date, close, currency
    FROM price_snapshots
    WHERE asset_id = :asset_id
    ORDER BY asset_id, trade_date DESC, fetched_at DESC
    """
)


def _rsi_zone(rsi):
    if rsi is None:
        return "N/A"
    if rsi >= 70:
        return "surachat"
    if rsi <= 30:
        return "survente"
    return "normale"


def _vs_sma(close, sma):
    if close is None or sma is None:
        return "N/A"
    return "above" if close > sma else "below"


def _fmt_pct(x):
    if x is None:
        return "N/A"
    return f"{'+' if x > 0 else ''}{x * 100:.2f}%"


def _fmt_num(x, decimals=2):
    return "N/A" if x is None else f"{x:.{decimals}f}"


def _fmt_magnitude_pct(x):
    return "N/A" if x is None else f"{x * 100:.2f}%"


@app.get("/api/assets/{ticker}/fiche")
def asset_fiche(ticker: str):
    with engine.connect() as conn:
        asset = conn.execute(
            text("SELECT id, ticker, name, sector, asset_type FROM assets WHERE ticker = :ticker"),
            {"ticker": ticker},
        ).mappings().first()
        if asset is None:
            raise HTTPException(status_code=404, detail=f"Ticker {ticker} introuvable.")

        price = conn.execute(FICHE_LATEST_PRICE_SQL, {"asset_id": asset["id"]}).mappings().first()
        all_features = conn.execute(FICHE_LATEST_FEATURES_SQL).mappings().all()

    features_by_ticker = {r["ticker"]: r for r in all_features}
    own = features_by_ticker.get(asset["ticker"])
    spy = features_by_ticker.get("SPY")

    sector_returns = [
        r["return_20d"] for r in all_features
        if r["sector"] == asset["sector"] and r["is_active"] and r["return_20d"] is not None
    ]
    sector_avg_return_20d = sum(sector_returns) / len(sector_returns) if sector_returns else None
    spy_return_20d = spy["return_20d"] if spy else None

    close = price["close"] if price else None
    currency = price["currency"] if price else ""
    trade_date = price["trade_date"] if price else None
    rsi = own["rsi_14"] if own else None
    sma_20 = own["sma_20"] if own else None
    sma_50 = own["sma_50"] if own else None

    text_block = f"""ACTION : {asset['ticker']} — {asset['name']}
SECTEUR : {asset['sector'] or 'N/A'}
DATE D'ANALYSE : {trade_date if trade_date else 'N/A'}

COURS
Dernier close : {_fmt_num(close)} {currency}
Variation 1j / 5j / 20j : {_fmt_pct(own['return_1d'] if own else None)} / {_fmt_pct(own['return_5d'] if own else None)} / {_fmt_pct(own['return_20d'] if own else None)}
Volatilité 20j : {_fmt_magnitude_pct(own['volatility_20d'] if own else None)}

TECHNIQUE
RSI-14 : {_fmt_num(rsi, 1)} (zone : {_rsi_zone(rsi)})
Position vs SMA-20 : {_vs_sma(close, sma_20)}
Position vs SMA-50 : {_vs_sma(close, sma_50)}
Ratio volume du jour / volume moyen 20j : {_fmt_num(own['volume_ratio'] if own else None)}

NEWS DES 7 DERNIERS JOURS
{NEWS_PLACEHOLDER}

NEWS DES 30 DERNIERS JOURS
{NEWS_PLACEHOLDER}

CONTEXTE SECTORIEL
Performance moyenne du secteur {asset['sector'] or 'N/A'} sur 20j : {_fmt_pct(sector_avg_return_20d)}

BENCHMARK
Performance SPY sur la même période (20j) : {_fmt_pct(spy_return_20d)}
"""

    return {
        "ticker": asset["ticker"],
        "name": asset["name"],
        "sector": asset["sector"],
        "asset_type": asset["asset_type"],
        "trade_date": trade_date,
        "close": close,
        "currency": currency or None,
        "return_1d": own["return_1d"] if own else None,
        "return_5d": own["return_5d"] if own else None,
        "return_20d": own["return_20d"] if own else None,
        "volatility_20d": own["volatility_20d"] if own else None,
        "rsi_14": rsi,
        "rsi_zone": _rsi_zone(rsi),
        "vs_sma_20": _vs_sma(close, sma_20),
        "vs_sma_50": _vs_sma(close, sma_50),
        "volume_ratio": own["volume_ratio"] if own else None,
        "sector_avg_return_20d": sector_avg_return_20d,
        "benchmark_return_20d": spy_return_20d,
        "text": text_block,
    }
