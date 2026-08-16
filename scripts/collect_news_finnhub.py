"""Collecte les news Finnhub (company-news) en complement d'Alpha Vantage, sur
tous les actifs actifs sauf FTC.L (confirme non couvert par aucun des 5
providers testes lors des diagnostics). Pas de sentiment fourni par cet
endpoint -> sentiment_score reste NULL (deja gere proprement par le code
existant : _fmt_news_line() dans main.py n'affiche le score que s'il n'est
pas None).

Dedup : contrainte UNIQUE(url) du schema (doublons exacts du meme provider)
+ TitleDeduper (scripts/_news_dedup.py) pour les doublons inter-providers
sous des URLs differentes (l'URL Finnhub est une redirection interne,
jamais identique a l'URL originale stockee par Alpha Vantage/Marketaux).

Quota : free tier Finnhub ~60 requetes/minute, tres large pour 33 tickers --
pas de gestion de quota complexe necessaire, juste une pause de courtoisie.

Usage:
  python scripts/collect_news_finnhub.py                    # tous les actifs actifs (sauf FTC.L)
  python scripts/collect_news_finnhub.py TICKER1 TICKER2 ... # seulement ces tickers-la
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

API_KEY = os.environ["FINNHUB_API_KEY"]
API_URL = "https://finnhub.io/api/v1/company-news"

DAYS_BACK = 7
PAUSE_BETWEEN_REQUESTS_SECONDS = 1  # tres large marge sous le quota (60/min), simple courtoisie

# Confirme non couvert par Alpha Vantage NI Finnhub NI Marketaux NI APITube
# lors des diagnostics precedents (FTC resout vers un ETF NASDAQ sans rapport).
EXCLUDED_TICKERS = {"FTC.L"}

INSERT_SQL = text(
    """
    INSERT INTO news_items (asset_id, published_at, collected_at, source, provider, url, title, summary, sentiment_score)
    VALUES (:asset_id, :published_at, :collected_at, :source, 'finnhub', :url, :title, :summary, NULL)
    ON CONFLICT (url) DO NOTHING
    """
)


def fetch_news(ticker: str, date_from: str, date_to: str) -> list:
    resp = requests.get(
        API_URL,
        params={"symbol": ticker, "from": date_from, "to": date_to, "token": API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


def main():
    run_collected_at = datetime.now(timezone.utc)
    date_from = (run_collected_at - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    date_to = run_collected_at.strftime("%Y-%m-%d")

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

    for i, (asset_id, ticker) in enumerate(to_process):
        try:
            feed = fetch_news(ticker, date_from, date_to)
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
                title = item.get("headline")
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
                            "published_at": datetime.fromtimestamp(item["datetime"], tz=timezone.utc),
                            "collected_at": run_collected_at,
                            "source": item.get("source") or "finnhub",
                            "url": item["url"],
                            "title": title,
                            "summary": item.get("summary") or None,
                        },
                    )
                    if result.rowcount:
                        inserted += 1
                        deduper.add(title)
                except Exception:
                    continue  # item malforme : on l'ignore, on continue les autres

        results.append((ticker, inserted, duplicates, len(feed), None))
        if i < len(to_process) - 1:
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    total_inserted = sum(r[1] for r in results)
    total_duplicates = sum(r[2] for r in results)
    no_news = [t for t, ins, dup, seen, err in results if err is None and seen == 0]
    failed = [(t, err) for t, ins, dup, seen, err in results if err is not None]

    print(f"Tickers traites          : {len(results)}/{len(to_process)}")
    print(f"News inserees au total   : {total_inserted}")
    print(f"Doublons detectes/rejetes: {total_duplicates}")

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
