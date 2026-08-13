"""Collecte les news + sentiment (Alpha Vantage NEWS_SENTIMENT) pour les actifs actifs.

Quota gratuit Alpha Vantage (verifie sur macroption.com et alphavantage.co, aout
2026) : 25 requetes/jour, 5 requetes/minute. Avec 34 tickers actifs, UN SEUL RUN
NE PEUT PAS tous les couvrir : ce script s'arrete proprement au quota (25 tickers
par defaut, ou plus tot si l'API renvoie elle-meme un message de rate-limit) et
liste les tickers non traites, a relancer un jour suivant.

Dedup : sur la contrainte UNIQUE(url) deja definie dans le schema (news_items.url),
via INSERT ... ON CONFLICT (url) DO NOTHING, plutot que sur (titre + date). Une
URL d'article est un identifiant plus stable qu'un titre, qui peut legerement
varier de formulation d'un appel a l'autre pour le meme article.

Limite heritee du schema existant : url est UNIQUE globalement (pas par actif).
Si un meme article couvre deux de nos tickers (ex: AMD et MU dans le meme
papier), il ne sera rattache qu'au premier actif qui l'a rapporte -- ce script
ne corrige pas cette contrainte, deja presente avant cette tache.

Usage: python scripts/collect_news.py
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._ca_bundle import ensure_ca_bundle

ensure_ca_bundle()

import requests
from sqlalchemy import text

from app.db import engine

API_KEY = os.environ["ALPHAVANTAGE_API_KEY"]
API_URL = "https://www.alphavantage.co/query"

DAYS_BACK = 7
MAX_REQUESTS_PER_RUN = 25  # quota gratuit Alpha Vantage : 25 requetes/jour
PAUSE_BETWEEN_REQUESTS_SECONDS = 13  # limite 5/minute -> >=12s entre requetes, marge de securite

INSERT_SQL = text(
    """
    INSERT INTO news_items (asset_id, published_at, collected_at, source, url, title, summary, sentiment_score)
    VALUES (:asset_id, :published_at, :collected_at, :source, :url, :title, :summary, :sentiment_score)
    ON CONFLICT (url) DO NOTHING
    """
)


def parse_published_at(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def sentiment_for_ticker(item: dict, ticker: str) -> float:
    for ts in item.get("ticker_sentiment", []):
        if ts.get("ticker") == ticker:
            return float(ts["ticker_sentiment_score"])
    return float(item["overall_sentiment_score"])


def fetch_news(ticker: str, time_from: str) -> list:
    resp = requests.get(
        API_URL,
        params={
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "time_from": time_from,
            "sort": "LATEST",
            "apikey": API_KEY,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    rate_limit_msg = data.get("Information") or data.get("Note")
    if rate_limit_msg:
        raise RuntimeError(rate_limit_msg)
    if "Error Message" in data:
        raise RuntimeError(data["Error Message"])

    return data.get("feed", [])


def main():
    run_collected_at = datetime.now(timezone.utc)
    time_from = (run_collected_at - timedelta(days=DAYS_BACK)).strftime("%Y%m%dT%H%M")

    with engine.connect() as conn:
        assets = conn.execute(
            text("SELECT id, ticker FROM assets WHERE is_active = true ORDER BY ticker")
        ).fetchall()

    to_process = assets[:MAX_REQUESTS_PER_RUN]
    skipped_quota = list(assets[MAX_REQUESTS_PER_RUN:])

    results = []  # (ticker, nb_inserted, nb_vus, erreur|None)

    for i, (asset_id, ticker) in enumerate(to_process):
        try:
            feed = fetch_news(ticker, time_from)
        except Exception as e:
            msg = str(e)
            if "rate limit" in msg.lower() or "call frequency" in msg.lower() or "requests per day" in msg.lower():
                skipped_quota = list(to_process[i:]) + skipped_quota
                break
            results.append((ticker, 0, 0, msg))
            if i < len(to_process) - 1:
                time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
            continue

        inserted = 0
        with engine.begin() as conn:
            for item in feed:
                try:
                    result = conn.execute(
                        INSERT_SQL,
                        {
                            "asset_id": asset_id,
                            "published_at": parse_published_at(item["time_published"]),
                            "collected_at": run_collected_at,
                            "source": item.get("source") or "alphavantage",
                            "url": item["url"],
                            "title": item["title"],
                            "summary": item.get("summary"),
                            "sentiment_score": sentiment_for_ticker(item, ticker),
                        },
                    )
                    if result.rowcount:
                        inserted += 1
                except Exception:
                    continue  # item malforme : on l'ignore, on continue les autres

        results.append((ticker, inserted, len(feed), None))
        if i < len(to_process) - 1:
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    total_inserted = sum(r[1] for r in results)
    no_news = [t for t, ins, seen, err in results if err is None and seen == 0]
    failed = [(t, err) for t, ins, seen, err in results if err is not None]

    print(f"Tickers traites        : {len(results)}/{len(assets)}")
    print(f"News inserees au total : {total_inserted}")

    if skipped_quota:
        print(f"\nATTENTION : quota gratuit (25 requetes/jour) insuffisant pour couvrir les {len(assets)} tickers actifs en un seul run.")
        print(f"Tickers NON traites ce run (a relancer un jour suivant) : {', '.join(t for _, t in skipped_quota)}")

    if no_news:
        print(f"\nTickers traites mais sans aucune news trouvee sur {DAYS_BACK}j : {', '.join(no_news)}")

    if failed:
        print("\nTickers en echec :")
        for ticker, err in failed:
            print(f"  {ticker}: {err}")


if __name__ == "__main__":
    main()
