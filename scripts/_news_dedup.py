"""Dedup partagee entre providers de news (Alpha Vantage/Finnhub/Marketaux/...).

La contrainte UNIQUE(url) du schema n'attrape que les doublons EXACTS d'un meme
provider qui republie deux fois la meme URL. Elle ne peut pas detecter qu'un
meme evenement est rapporte par deux providers differents sous deux URLs
differentes (constate a plusieurs reprises pendant les diagnostics Finnhub/
Marketaux/APITube : aucune des URLs ne coincide jamais entre deux providers
pour un article pourtant identique). On complete donc par une similarite de
titre -- seuil 55%, la meme mesure (difflib.SequenceMatcher) utilisee pour
chiffrer le chevauchement entre providers lors de ces diagnostics.

Usage : un TitleDeduper par (asset_id, run), reutilise pour tous les articles
candidats de ce ticker sur ce run -- charge d'abord les titres deja en base
sur la fenetre de dedup, puis accumule au fil des insertions du run pour
attraper aussi les doublons entre deux articles candidats du meme run.
"""
import difflib
from datetime import timedelta

from sqlalchemy import text

TITLE_SIMILARITY_THRESHOLD = 0.55

# Fenetre de recherche des doublons potentiels en base : doit couvrir la
# fenetre de collecte (7j) avec de la marge, au cas ou un meme evenement
# serait rapporte plusieurs jours plus tard par un autre provider.
DEDUP_WINDOW_DAYS = 10


def titles_are_similar(title_a: str, title_b: str, threshold: float = TITLE_SIMILARITY_THRESHOLD) -> bool:
    return difflib.SequenceMatcher(None, title_a.lower(), title_b.lower()).ratio() > threshold


class TitleDeduper:
    """Suit les titres deja vus (en base + inseres pendant ce run) pour un
    asset_id donne, et decide si un nouveau titre candidat est un doublon."""

    def __init__(self, conn, asset_id: int, now):
        since = now - timedelta(days=DEDUP_WINDOW_DAYS)
        self._titles = list(
            conn.execute(
                text("SELECT title FROM news_items WHERE asset_id = :asset_id AND published_at >= :since"),
                {"asset_id": asset_id, "since": since},
            ).scalars()
        )

    def is_duplicate(self, candidate_title: str) -> bool:
        return any(titles_are_similar(candidate_title, t) for t in self._titles)

    def add(self, title: str) -> None:
        self._titles.append(title)


def _demo():
    assert titles_are_similar("AMD Stock Surges 6.5%", "AMD Stock Surges 6.5%") is True
    assert titles_are_similar(
        "MP Materials Jumps 7.4% Amid Sector-Wide Rally",
        "MP Materials Jumps 7. 4% Amid Sector-Wide Rally - Alphastreet",
    ) is True
    assert titles_are_similar("AMD Stock Surges 6.5%", "Sidus Space Reports Q2 2026 Results") is False
    print("scripts/_news_dedup.py: self-check OK")


if __name__ == "__main__":
    _demo()
