"""Collecte les news Marketaux (avec sentiment par entite) en complement
d'Alpha Vantage/Finnhub, sur tous les actifs actifs sauf FTC.L (confirme non
couvert par aucun des 5 providers testes lors des diagnostics).

Sentiment : Marketaux fournit entities[].sentiment_score deja sur l'echelle
-1/+1, la meme que la colonne news_items.sentiment_score (CHECK BETWEEN -1
AND 1) -- stocke tel quel, aucune renormalisation necessaire (contrairement
a APITube, ecarte lors du diagnostic pour cette raison).

Quota : ~100 requetes/jour sur le tier gratuit, deja partiellement consomme
par les diagnostics precedents. Ce script suit x-usagelimit-remaining sur
chaque reponse et s'arrete proprement (sans lever d'exception) des que le
quota semble insuffisant pour continuer -- les tickers non traites sont
listes pour un relance ulterieure, jamais un plantage en cours de route.

Dedup : contrainte UNIQUE(url) du schema + TitleDeduper (scripts/_news_dedup.py)
pour les doublons inter-providers sous des URLs differentes.

Usage:
  python scripts/collect_news_marketaux.py                    # tous les actifs actifs (sauf FTC.L)
  python scripts/collect_news_marketaux.py TICKER1 TICKER2 ... # seulement ces tickers-la
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
from scripts._news_dedup import TitleDeduper

API_KEY = os.environ["MARKETAUX_API_KEY"]
API_URL = "https://api.marketaux.com/v1/news/all"

DAYS_BACK = 7
PAUSE_BETWEEN_REQUESTS_SECONDS = 2  # tres large marge sous le rate-limit (30/min)

# En dessous de ce nombre de requetes visiblement restantes sur le compte,
# on arrete proprement plutot que de risquer une erreur de quota en pleine
# boucle -- mieux vaut traiter moins de tickers proprement que planter.
MIN_QUOTA_REMAINING_TO_CONTINUE = 1

EXCLUDED_TICKERS = {"FTC.L"}

INSERT_SQL = text(
    """
    INSERT INTO news_items (asset_id, published_at, collected_at, source, provider, url, title, summary, sentiment_score)
    VALUES (:asset_id, :published_at, :collected_at, :source, 'marketaux', :url, :title, :summary, :sentiment_score)
    ON CONFLICT (url) DO NOTHING
    """
)


class QuotaExhausted(Exception):
    pass


def fetch_news(ticker: str, published_after: str) -> tuple[list, int | None]:
    resp = requests.get(
        API_URL,
        params={
            "symbols": ticker,
            "filter_entities": "true",
            "published_after": published_after,
            "sort": "published_desc",
            "api_token": API_KEY,
        },
        timeout=30,
    )
    remaining = resp.headers.get("x-usagelimit-remaining")
    remaining = int(remaining) if remaining is not None else None

    if resp.status_code in (402, 429):
        raise QuotaExhausted(f"HTTP {resp.status_code} : quota Marketaux atteint")

    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise QuotaExhausted(str(data["error"]))

    return data.get("data", []), remaining


def sentiment_for_ticker(item: dict, ticker: str):
    for entity in item.get("entities", []):
        if entity.get("symbol") == ticker:
            score = entity.get("sentiment_score")
            return float(score) if score is not None else None
    return None


def main():
    run_collected_at = datetime.now(timezone.utc)
    published_after = (run_collected_at - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")

    requested_tickers = sys.argv[1:]

    with engine.connect() as conn:
        if requested_tickers:
            assets = conn.execute(
                text("SELECT id, ticker FROM assets WHERE is_active = true AND ticker = ANY(:tickers) ORDER BY ticker"),
                {"tickers": requested_tickers},
            ).fetchall()
        else:
            assets = conn.execute(
                text("SELECT id, ticker FROM assets WHERE is_active = true ORDER BY ticker")
            ).fetchall()

    to_process = [(aid, t) for aid, t in assets if t not in EXCLUDED_TICKERS]

    results = []  # (ticker, nb_inserted, nb_doublons, nb_vus, erreur|None)
    skipped_quota = []
    last_remaining = None

    for i, (asset_id, ticker) in enumerate(to_process):
        try:
            feed, remaining = fetch_news(ticker, published_after)
            last_remaining = remaining if remaining is not None else last_remaining
        except QuotaExhausted as e:
            skipped_quota = [t for _, t in to_process[i:]]
            print(f"ARRET PROPRE : {e} -- {len(skipped_quota)} tickers restants non traites : {', '.join(skipped_quota)}")
            break
        except Exception as e:
            results.append((ticker, 0, 0, 0, str(e)))
            if i < len(to_process) - 1:
                time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
            continue

        inserted = 0
        duplicates = 0
        with engine.begin() as conn:
            deduper = TitleDeduper(conn, asset_id, run_collected_at)
            for item in feed:
                title = item.get("title")
                if not title:
                    continue
                if deduper.is_duplicate(title):
                    duplicates += 1
                    continue
                try:
                    result = conn.execute(
                        INSERT_SQL,
                        {
                            "asset_id": asset_id,
                            "published_at": item["published_at"],
                            "collected_at": run_collected_at,
                            "source": item.get("source") or "marketaux",
                            "url": item["url"],
                            "title": title,
                            "summary": item.get("description") or item.get("snippet") or None,
                            "sentiment_score": sentiment_for_ticker(item, ticker),
                        },
                    )
                    if result.rowcount:
                        inserted += 1
                        deduper.add(title)
                except Exception:
                    continue  # item malforme : on l'ignore, on continue les autres

        results.append((ticker, inserted, duplicates, len(feed), None))

        # Arret preventif si le quota semble sur le point de s'epuiser, avant
        # meme que l'API ne renvoie une erreur explicite.
        if last_remaining is not None and last_remaining < MIN_QUOTA_REMAINING_TO_CONTINUE:
            skipped_quota = [t for _, t in to_process[i + 1:]]
            if skipped_quota:
                print(f"ARRET PROPRE : quota Marketaux quasi epuise (restant={last_remaining}) -- {len(skipped_quota)} tickers restants non traites : {', '.join(skipped_quota)}")
            break

        if i < len(to_process) - 1:
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    total_inserted = sum(r[1] for r in results)
    total_duplicates = sum(r[2] for r in results)
    no_news = [t for t, ins, dup, seen, err in results if err is None and seen == 0]
    failed = [(t, err) for t, ins, dup, seen, err in results if err is not None]

    print(f"Tickers traites          : {len(results)}/{len(to_process)}")
    print(f"News inserees au total   : {total_inserted}")
    print(f"Doublons detectes/rejetes: {total_duplicates}")
    print(f"Quota restant (dernier releve) : {last_remaining}")

    if skipped_quota:
        print(f"\nTickers non traites (quota) : {', '.join(skipped_quota)}")

    if no_news:
        print(f"\nTickers traites mais sans aucune news trouvee sur {DAYS_BACK}j : {', '.join(no_news)}")

    if failed:
        print("\nTickers en echec :")
        for ticker, err in failed:
            print(f"  {ticker}: {err}")

    print("\nDetail par ticker :")
    for ticker, ins, dup, seen, err in results:
        status = f"ECHEC ({err})" if err else f"{ins} inserees, {dup} doublons rejetes, {seen} vues"
        print(f"  {ticker}: {status}")


if __name__ == "__main__":
    main()
