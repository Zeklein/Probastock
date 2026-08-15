from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from app.db import engine
from app.fiche_enrichment import NOT_COVERED, RECOMMENDATION_SCALE_NOTE, fetch_analyst_enrichment

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


NEWS_PLACEHOLDER = "(aucune news collectee sur cette periode)"
NEWS_7D_LIMIT = 8    # plafond d'items dans le prompt : certains tickers ont 40-50 news/7j, il faut borner le cout/latence
NEWS_30D_LIMIT = 10  # items 8-30j, hors ceux deja lists dans la section 7j

FICHE_NEWS_SQL = text(
    """
    SELECT title, summary, published_at, sentiment_score
    FROM news_items
    WHERE asset_id = :asset_id AND published_at >= :since
    ORDER BY published_at DESC
    """
)


def _fmt_news_line(item, with_summary=False):
    date_str = item["published_at"].strftime("%Y-%m-%d")
    sentiment = f" (sentiment {item['sentiment_score']:+.2f})" if item["sentiment_score"] is not None else ""
    line = f"- [{date_str}] {item['title']}{sentiment}"
    if with_summary and item["summary"]:
        line += f"\n  {item['summary'][:200]}"
    return line

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

# Historique complet (high/low), le plus recent d'abord, meme regle de dedup.
# Sert aux plages de prix (1j/1sem/1mois/1an) et au target technique maison.
FICHE_PRICE_RANGE_HISTORY_SQL = text(
    """
    SELECT DISTINCT ON (trade_date) trade_date, high, low
    FROM price_snapshots
    WHERE asset_id = :asset_id
    ORDER BY trade_date DESC, fetched_at DESC
    """
)

PRICE_RANGE_WINDOWS = {"1j": 1, "1sem": 5, "1mois": 21, "1an": 252}
TECHNICAL_TARGET_SESSIONS = 60  # ~1 trimestre boursier ; distinct des fenetres 20j deja couvertes par sma_20/volatility_20d


def _price_ranges(prices_desc):
    """prices_desc : lignes {trade_date, high, low} triees du plus recent au
    plus ancien. Renvoie None par fenetre si aucun historique disponible."""
    ranges = {}
    for label, n in PRICE_RANGE_WINDOWS.items():
        window = [p for p in prices_desc[:n] if p["high"] is not None and p["low"] is not None]
        if not window:
            ranges[label] = None
            continue
        ranges[label] = {
            "high": max(p["high"] for p in window),
            "low": min(p["low"] for p in window),
            "n_seances": len(window),
        }
    return ranges


def _technical_target(prices_desc, volatility_20d):
    """Target technique maison (pas un modele predictif) : plus haut/bas sur
    les N dernieres seances, elargi de +/- volatility_20d pour donner une
    fourchette proportionnee a la volatilite recente du titre plutot qu'un
    chiffre fixe. A comparer avec targets_analystes, pas a la place."""
    window = [
        p for p in prices_desc[:TECHNICAL_TARGET_SESSIONS]
        if p["high"] is not None and p["low"] is not None
    ]
    if not window:
        return None

    high_n = max(float(p["high"]) for p in window)
    low_n = min(float(p["low"]) for p in window)
    vol = float(volatility_20d) if volatility_20d is not None else 0.0

    return {
        "high": round(high_n * (1 + vol), 4),
        "low": round(low_n * (1 - vol), 4),
        "n_seances_visees": TECHNICAL_TARGET_SESSIONS,
        "n_seances_disponibles": len(window),
        "methode": "plus haut/bas glissant, elargi de +/- volatility_20d",
    }


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
        price_history_desc = conn.execute(
            FICHE_PRICE_RANGE_HISTORY_SQL, {"asset_id": asset["id"]}
        ).mappings().all()
        news_30d = conn.execute(
            FICHE_NEWS_SQL,
            {"asset_id": asset["id"], "since": datetime.now(timezone.utc) - timedelta(days=30)},
        ).mappings().all()

    since_7d = datetime.now(timezone.utc) - timedelta(days=7)
    news_7d = [n for n in news_30d if n["published_at"] >= since_7d][:NEWS_7D_LIMIT]
    news_older = [n for n in news_30d if n["published_at"] < since_7d][:NEWS_30D_LIMIT]

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
    volatility_20d = own["volatility_20d"] if own else None

    price_ranges = _price_ranges(price_history_desc)
    technical_target = _technical_target(price_history_desc, volatility_20d)

    try:
        enrichment = fetch_analyst_enrichment(asset["ticker"], float(close) if close is not None else None)
    except Exception:
        # /fiche reste utilisable meme si yfinance est indisponible : degrade
        # en "non couvert" plutot que de casser l'endpoint.
        enrichment = {
            "valorisation": {"per_annee_courante": None, "per_annee_courante_label": NOT_COVERED, "per_y1": None, "per_y1_label": NOT_COVERED},
            "consensus_analystes": {"recommendation_key": None, "recommendation_mean": None, "note_sur_10": None, "note_formule": RECOMMENDATION_SCALE_NOTE, "nb_analystes": None, "label_absence": NOT_COVERED},
            "targets_analystes": {"mean": None, "high": None, "low": None, "label_absence": NOT_COVERED},
            "eps_trend": None,
        }

    def _fmt_range(r):
        return f"{_fmt_num(r['high'])} / {_fmt_num(r['low'])} ({r['n_seances']} séances)" if r else "N/A"

    def _fmt_per(value, label):
        return label if label else _fmt_num(value)

    def _fmt_technical_target(t):
        if t is None:
            return "N/A"
        return f"{_fmt_num(t['high'])} / {_fmt_num(t['low'])} ({t['n_seances_disponibles']}/{t['n_seances_visees']} séances, {t['methode']})"

    def _fmt_analyst_target(t):
        if t["label_absence"]:
            return t["label_absence"]
        return f"moyenne {_fmt_num(t['mean'])} / haut {_fmt_num(t['high'])} / bas {_fmt_num(t['low'])}"

    news_7d_text = "\n".join(_fmt_news_line(n, with_summary=True) for n in news_7d) if news_7d else NEWS_PLACEHOLDER
    news_older_text = "\n".join(_fmt_news_line(n) for n in news_older) if news_older else NEWS_PLACEHOLDER

    eps_trend = enrichment["eps_trend"]
    if eps_trend:
        eps_trend_lines = "\n".join(
            f"  {period} : actuel {_fmt_num(v.get('current'), 3)} (il y a 90j : {_fmt_num(v.get('90daysAgo'), 3)})"
            for period, v in eps_trend.items()
        )
    else:
        eps_trend_lines = f"  {NOT_COVERED}"

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

PLAGES DE PRIX (haut / bas)
1 jour : {_fmt_range(price_ranges['1j'])}
1 semaine : {_fmt_range(price_ranges['1sem'])}
1 mois : {_fmt_range(price_ranges['1mois'])}
1 an : {_fmt_range(price_ranges['1an'])}

VALORISATION
PER année courante : {_fmt_per(enrichment['valorisation']['per_annee_courante'], enrichment['valorisation']['per_annee_courante_label'])}
PER Y+1 (estimation) : {_fmt_per(enrichment['valorisation']['per_y1'], enrichment['valorisation']['per_y1_label'])}

CONSENSUS ANALYSTES
{enrichment['consensus_analystes']['label_absence'] or f"{enrichment['consensus_analystes']['recommendation_key']} -- note {enrichment['consensus_analystes']['note_sur_10']}/10 ({enrichment['consensus_analystes']['nb_analystes']} analystes)"}

TARGETS
Target analystes : {_fmt_analyst_target(enrichment['targets_analystes'])}
Target technique Probastock : {_fmt_technical_target(technical_target)}

RÉVISIONS D'ESTIMATIONS EPS (vs il y a 90 jours)
{eps_trend_lines}

NEWS DES 7 DERNIERS JOURS
{news_7d_text}

NEWS DES 8-30 DERNIERS JOURS (titres seulement)
{news_older_text}

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
        "price_ranges": price_ranges,
        "valorisation": enrichment["valorisation"],
        "consensus_analystes": enrichment["consensus_analystes"],
        "targets": {
            "analystes": enrichment["targets_analystes"],
            "technique_probastock": technical_target,
        },
        "eps_trend": enrichment["eps_trend"],
        "news": {
            "last_7d": [
                {"title": n["title"], "summary": n["summary"], "published_at": n["published_at"], "sentiment_score": n["sentiment_score"]}
                for n in news_7d
            ],
            "last_8_30d": [
                {"title": n["title"], "published_at": n["published_at"], "sentiment_score": n["sentiment_score"]}
                for n in news_older
            ],
        },
        "text": text_block,
    }


@app.post("/api/assets/{ticker}/analyze")
def analyze_asset(ticker: str):
    # Import differe : app.ai_engine importe asset_fiche depuis ce module,
    # un import en tete de fichier creerait un cycle au chargement.
    from app.ai_engine import analyze_ticker

    try:
        return analyze_ticker(ticker, provider="deepseek")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/predictions/tracking")
def predictions_tracking(ticker: str | None = None):
    from app.tracking import compute_tracking

    return compute_tracking(ticker)
