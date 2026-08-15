"""Enrichissement de /api/assets/{ticker}/fiche via yfinance : valorisation
(PER), consensus analystes, targets, revisions d'estimations (eps_trend).

Contrairement au reste de /fiche (100% base de donnees), ce module fait un
appel reseau live a chaque requete. Il ne doit JAMAIS faire planter /fiche :
toute absence ou echec se traduit par un champ a None + un label explicite
("non couvert par les analystes", "n/a (societe deficitaire)"), jamais par
une exception qui remonte, ni par une valeur inventee.
"""
import math

from scripts._ca_bundle import ensure_ca_bundle

ensure_ca_bundle()

import yfinance as yf

NOT_COVERED = "non couvert par les analystes"
DEFICIT_LABEL = "n/a (société déficitaire)"

RECOMMENDATION_SCALE_NOTE = (
    "note = (5 - recommendationMean) * 2.5 -- echelle yfinance : "
    "1=Strong Buy, 3=Hold, 5=Strong Sell, convertie en note /10 (10=tres bullish)"
)


def _per(price, eps):
    """PER = prix / EPS. EPS negatif ou absent -> pas de valeur numerique,
    juste un label explicite plutot qu'un ratio negatif illisible."""
    if price is None or eps is None:
        return None, NOT_COVERED
    if eps <= 0:
        return None, DEFICIT_LABEL
    return round(price / eps, 2), None


def _recommendation_score(mean):
    if mean is None:
        return None
    return round((5 - mean) * 2.5, 2)


def _clean_eps_trend(df):
    """Convertit le DataFrame eps_trend de yfinance (lignes 0q/+1q/0y/+1y,
    colonnes current/7daysAgo/30daysAgo/60daysAgo/90daysAgo) en dict JSON-safe.
    Renvoie None si absent ou si tout est NaN (= pas de couverture analyste).

    yfinance renvoie parfois un 0.0 litteral (pas NaN) sur une colonne
    "XdaysAgo" quand l'historique ne remonte pas jusque-la (observe sur AMD,
    RTX...), alors que "current" est toujours renseigne correctement. Un vrai
    consensus EPS tombe pile sur 0 est quasi impossible ; on traite donc un 0
    sur une colonne *daysAgo comme une absence de donnee plutot que comme un
    vrai point, pour ne pas produire une fausse "revision" de +/-100%."""
    if df is None or df.empty:
        return None

    out = {}
    has_value = False
    for period, row in df.iterrows():
        cleaned = {}
        for col, v in row.items():
            is_missing = v is None or (isinstance(v, float) and math.isnan(v))
            is_suspect_zero = col != "current" and v == 0.0
            if is_missing or is_suspect_zero:
                cleaned[col] = None
            else:
                cleaned[col] = float(v)
                has_value = True
        out[period] = cleaned

    return out if has_value else None


def fetch_analyst_enrichment(ticker: str, last_close) -> dict:
    """last_close : dernier cours connu (depuis notre base), utilise pour
    calculer le PER Y+1 a partir de l'estimation d'EPS +1y de yfinance."""
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
    except Exception:
        info = {}

    trailing_pe, trailing_pe_label = _per(last_close, info.get("trailingEps"))
    # trailingPE de yfinance est deja ce ratio ; on prefere le lire directement
    # quand il existe (coherent avec le diagnostic), et ne retomber sur le
    # calcul maison que s'il est absent.
    if info.get("trailingPE") is not None:
        trailing_pe, trailing_pe_label = round(info["trailingPE"], 2), None

    eps_y1 = None
    try:
        est = tk.earnings_estimate
        if est is not None and "+1y" in est.index:
            eps_y1 = est.loc["+1y", "avg"]
    except Exception:
        pass
    per_y1, per_y1_label = _per(last_close, eps_y1)

    reco_mean = info.get("recommendationMean")
    reco_label = None if reco_mean is not None else NOT_COVERED
    target_mean = info.get("targetMeanPrice")
    targets_label = None if target_mean is not None else NOT_COVERED

    try:
        eps_trend = _clean_eps_trend(tk.eps_trend)
    except Exception:
        eps_trend = None

    return {
        "valorisation": {
            "per_annee_courante": trailing_pe,
            "per_annee_courante_label": trailing_pe_label,
            "per_y1": per_y1,
            "per_y1_label": per_y1_label,
        },
        "consensus_analystes": {
            "recommendation_key": info.get("recommendationKey"),
            "recommendation_mean": reco_mean,
            "note_sur_10": _recommendation_score(reco_mean),
            "note_formule": RECOMMENDATION_SCALE_NOTE,
            "nb_analystes": info.get("numberOfAnalystOpinions"),
            "label_absence": reco_label,
        },
        "targets_analystes": {
            "mean": target_mean,
            "high": info.get("targetHighPrice"),
            "low": info.get("targetLowPrice"),
            "label_absence": targets_label,
        },
        "eps_trend": eps_trend,
    }
