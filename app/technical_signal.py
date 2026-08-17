"""Signal technique RSI, totalement independant du score Probastock
(predictions_aggregated) et du moteur d'analyse IA (app/ai_engine.py) -- ce
module ne lit ni n'ecrit rien dans ces tables. Base uniquement sur
features_daily (rsi_14, volume_ratio) et price_snapshots (close), deja
calcules par compute_features.py.

Trois composantes combinees en un signal BUY/NEUTRAL/SELL :
1. Franchissement de zone RSI -- sortie de survente/surachat (le RSI vient de
   changer de regime entre hier et aujourd'hui), pas juste "RSI est bas".
2. Divergence prix/RSI -- le prix et le RSI ne racontent pas la meme histoire
   sur une fenetre recente (essoufflement ou creux qui se forme).
3. Confirmation par le volume -- un signal sur volume anormalement haut est
   plus credible qu'un signal sur volume normal.
"""
from dataclasses import dataclass

from sqlalchemy import text

from app.db import engine

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# Fenetre de divergence : alignee sur la periode du RSI-14 lui-meme (14 jours)
# plutot qu'un chiffre arbitraire choisi dans la fourchette 10-15j suggeree --
# la divergence doit se mesurer sur le meme horizon que celui que l'oscillateur
# encode deja, sinon on compare des tendances de duree differente.
DIVERGENCE_WINDOW_DAYS = 14

# En dessous de ces seuils, un ecart prix/RSI est trop faible pour distinguer
# une vraie divergence du bruit normal (le RSI ondule facilement +/-2-3 points
# d'un jour a l'autre sans tendance de fond). Choisis pour filtrer ce bruit
# typique -- a ajuster si le signal se revele trop/pas assez sensible en usage.
DIVERGENCE_MIN_PRICE_CHANGE = 0.01   # 1% minimum de variation de prix sur la fenetre
DIVERGENCE_MIN_RSI_CHANGE = 3.0      # 3 points RSI minimum, dans le sens oppose au prix

# Volume "significativement au-dessus de la normale" : au moins 50% de plus
# que la moyenne 20j -- volume_ratio est deja ce rapport (compute_features.py).
VOLUME_CONFIRMATION_RATIO = 1.5

# Historique necessaire : fenetre de divergence + marge (jours non-boursiers,
# donnees manquantes ponctuelles) sans aller chercher les 90j utilises ailleurs.
HISTORY_DAYS = 30

HISTORY_SQL = text(
    """
    SELECT * FROM (
        SELECT f.trade_date, lp.close, f.rsi_14, f.volume_ratio
        FROM features_daily f
        JOIN (
            SELECT DISTINCT ON (asset_id, trade_date) asset_id, trade_date, close
            FROM price_snapshots
            WHERE asset_id = :asset_id
            ORDER BY asset_id, trade_date, fetched_at DESC
        ) lp ON lp.asset_id = f.asset_id AND lp.trade_date = f.trade_date
        WHERE f.asset_id = :asset_id
        ORDER BY f.trade_date DESC
        LIMIT :days
    ) recent
    ORDER BY trade_date ASC
    """
)


@dataclass
class TechnicalSignal:
    signal: str  # "BUY" | "NEUTRAL" | "SELL"
    justification: str
    crossover: str | None = None    # "bullish" | "bearish" | None
    divergence: str | None = None   # "bullish" | "bearish" | None
    volume_confirmed: bool = False
    rsi_today: float | None = None
    trade_date: object = None


def _detect_crossover(rsi_yesterday, rsi_today) -> str | None:
    """Sortie de zone, pas juste 'RSI est actuellement < 30' : il faut que la
    valeur d'hier soit encore dans la zone et celle d'aujourd'hui l'ait quittee."""
    if rsi_yesterday is None or rsi_today is None:
        return None
    if rsi_yesterday < RSI_OVERSOLD <= rsi_today:
        return "bullish"
    if rsi_yesterday > RSI_OVERBOUGHT >= rsi_today:
        return "bearish"
    return None


def _detect_divergence(price_then, price_now, rsi_then, rsi_now) -> str | None:
    if None in (price_then, price_now, rsi_then, rsi_now) or price_then == 0:
        return None
    price_change = (float(price_now) - float(price_then)) / float(price_then)
    rsi_change = float(rsi_now) - float(rsi_then)
    if abs(price_change) < DIVERGENCE_MIN_PRICE_CHANGE or abs(rsi_change) < DIVERGENCE_MIN_RSI_CHANGE:
        return None
    if price_change > 0 and rsi_change < 0:
        return "bearish"  # prix monte, RSI faiblit -> essoufflement de la hausse
    if price_change < 0 and rsi_change > 0:
        return "bullish"  # prix baisse, RSI se redresse -> creux qui se forme
    return None


def compute_signal(history: list[dict]) -> TechnicalSignal | None:
    """history : lignes {trade_date, close, rsi_14, volume_ratio} triees par
    trade_date ASCENDANT (le plus ancien en premier), meme format que
    HISTORY_SQL. Renvoie None si l'historique est insuffisant pour au moins
    detecter un franchissement (2 jours)."""
    if len(history) < 2:
        return None

    today = history[-1]
    yesterday = history[-2]

    crossover = _detect_crossover(yesterday.get("rsi_14"), today.get("rsi_14"))

    divergence = None
    if len(history) > DIVERGENCE_WINDOW_DAYS:
        past = history[-1 - DIVERGENCE_WINDOW_DAYS]
        divergence = _detect_divergence(past.get("close"), today.get("close"), past.get("rsi_14"), today.get("rsi_14"))

    volume_ratio = today.get("volume_ratio")
    volume_confirmed = volume_ratio is not None and float(volume_ratio) >= VOLUME_CONFIRMATION_RATIO

    # Le franchissement de zone est prioritaire sur la divergence : c'est un
    # signal plus concret/immediat (le RSI vient reellement de changer de
    # regime), alors que la divergence reste un signal avance/plus speculatif.
    if crossover == "bullish":
        base, signal = "Sortie de survente", "BUY"
    elif crossover == "bearish":
        base, signal = "Sortie de surachat", "SELL"
    elif divergence == "bullish":
        base, signal = "Divergence haussière détectée", "BUY"
    elif divergence == "bearish":
        base, signal = "Divergence baissière détectée", "SELL"
    else:
        base, signal = "Pas de signal technique significatif", "NEUTRAL"

    if signal == "NEUTRAL":
        justification = base
    elif volume_confirmed:
        justification = f"{base} confirmée par un volume élevé"
    else:
        justification = f"{base}, signal à surveiller"

    return TechnicalSignal(
        signal=signal,
        justification=justification,
        crossover=crossover,
        divergence=divergence,
        volume_confirmed=volume_confirmed,
        rsi_today=today.get("rsi_14"),
        trade_date=today.get("trade_date"),
    )


def get_signal_for_asset(asset_id: int) -> TechnicalSignal | None:
    with engine.connect() as conn:
        rows = conn.execute(HISTORY_SQL, {"asset_id": asset_id, "days": HISTORY_DAYS}).mappings().all()
    return compute_signal([dict(r) for r in rows])


# Version groupee (1 requete pour tous les actifs) plutot que N appels a
# get_signal_for_asset() -- meme logique que les autres jointures bulk de
# /api/dashboard (features, agregation, sparkline), pour ne pas refaire une
# requete par ticker a chaque chargement du dashboard.
BULK_HISTORY_SQL = text(
    """
    SELECT * FROM (
        SELECT f.asset_id, f.trade_date, lp.close, f.rsi_14, f.volume_ratio,
               ROW_NUMBER() OVER (PARTITION BY f.asset_id ORDER BY f.trade_date DESC) AS rn
        FROM features_daily f
        JOIN (
            SELECT DISTINCT ON (asset_id, trade_date) asset_id, trade_date, close
            FROM price_snapshots
            WHERE asset_id = ANY(:asset_ids)
            ORDER BY asset_id, trade_date, fetched_at DESC
        ) lp ON lp.asset_id = f.asset_id AND lp.trade_date = f.trade_date
        WHERE f.asset_id = ANY(:asset_ids)
    ) recent
    WHERE rn <= :days
    ORDER BY asset_id, trade_date ASC
    """
)


def get_signals_for_assets(asset_ids: list[int]) -> dict[int, TechnicalSignal]:
    if not asset_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(BULK_HISTORY_SQL, {"asset_ids": asset_ids, "days": HISTORY_DAYS}).mappings().all()
    by_asset: dict[int, list[dict]] = {}
    for r in rows:
        by_asset.setdefault(r["asset_id"], []).append(dict(r))
    signals = {aid: compute_signal(history) for aid, history in by_asset.items()}
    return {aid: sig for aid, sig in signals.items() if sig is not None}


def _demo():
    def day(trade_date, close, rsi, vol_ratio=1.0):
        return {"trade_date": trade_date, "close": close, "rsi_14": rsi, "volume_ratio": vol_ratio}

    # Franchissement haussier (sortie de survente), volume eleve -> BUY confirme
    history = [day(i, 100, 50) for i in range(13)] + [day(13, 95, 28), day(14, 96, 32, vol_ratio=2.0)]
    r = compute_signal(history)
    assert r.signal == "BUY" and r.crossover == "bullish" and r.volume_confirmed
    assert r.justification == "Sortie de survente confirmée par un volume élevé"

    # Franchissement baissier, volume normal -> SELL non confirme
    history = [day(i, 100, 50) for i in range(13)] + [day(13, 105, 72), day(14, 104, 68, vol_ratio=1.0)]
    r = compute_signal(history)
    assert r.signal == "SELL" and r.crossover == "bearish" and not r.volume_confirmed
    assert r.justification == "Sortie de surachat, signal à surveiller"

    # Divergence baissiere : prix +5% sur 14j, RSI en repli -> SELL (watch)
    history = [day(i, 100, 50) for i in range(1)] + [day(i, 100 + i, 55 - i) for i in range(1, 15)]
    r = compute_signal(history)
    assert r.divergence == "bearish" and r.crossover is None
    assert r.signal == "SELL"

    # Rien de significatif -> NEUTRAL
    history = [day(i, 100, 50, vol_ratio=1.0) for i in range(20)]
    r = compute_signal(history)
    assert r.signal == "NEUTRAL"

    # Historique insuffisant -> None, pas de plantage
    assert compute_signal([day(1, 100, 50)]) is None
    assert compute_signal([]) is None

    print("app/technical_signal.py: self-check OK")


if __name__ == "__main__":
    _demo()
