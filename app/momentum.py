"""Momentum multi-horizon, en relatif vs un benchmark. Calcul pur : aucun acces base,
aucun appel reseau (l'I/O est dans scripts/compute_momentum.py).

Quatre mesures, chacune = rendement du titre - rendement du benchmark sur la meme
fenetre (l'"exces de rendement") :

  r12_1  12 mois en sautant le dernier mois   -> biais de fond
  r3m    3 mois                               -> biais intermediaire
  r1m    1 mois                               -> timing
  r5d    5 jours                              -> timing

Les fenetres sont comptees en lignes (seances) du calendrier commun a tous les
titres : un jour ferme sur une place mais ouvert sur une autre reprend le dernier
cours connu (ffill limite a 5 jours, cf. scripts/compute_momentum.py).

Score (0-100) : moyenne ponderee des rangs percentiles de chaque mesure entre les
titres suivis. Si une mesure manque (historique trop court : SPCX, IPO recentes),
les poids restent normalises sur les mesures disponibles ; n_measures dit combien
ont servi.

Etat : combine le biais (exces moyen 12-1 et 3 mois) et le timing (exces moyen
1 mois et 5 jours), tous deux par leur signe vs le benchmark :

  biais > 0, timing > 0   tendance confirmee
  biais > 0, timing <= 0  repli dans la tendance
  biais <= 0, timing > 0  rebond contre-tendance
  biais <= 0, timing <= 0 tendance baissiere

Point de depart a valider par mesure, pas une verite : les poids et les signes
ci-dessous sont des hypotheses. A 1 mois et 5 jours, l'effet est souvent inverse
(retour a la moyenne) -- c'est le ledger (momentum_outcomes) qui dira s'il faut
mettre SIGNS["r1m"] / SIGNS["r5d"] a -1 ou leur poids a 0. Tout changement de
parametres doit s'accompagner d'un nouveau CONFIG_VERSION.
"""
import numpy as np
import pandas as pd

CONFIG_VERSION = "momentum-v0.1"

# (retard de depart, retard de fin) en seances.
# Rendement = P[-1 - fin] / P[-1 - depart] - 1
WINDOWS = {
    "r12_1": (252, 21),
    "r3m": (63, 0),
    "r1m": (21, 0),
    "r5d": (5, 0),
}
WEIGHTS = {"r12_1": 0.40, "r3m": 0.30, "r1m": 0.15, "r5d": 0.15}
# +1 : un exces eleve augmente le score. -1 : inverse (retour a la moyenne a court terme).
SIGNS = {"r12_1": 1, "r3m": 1, "r1m": 1, "r5d": 1}
OUTCOME_HORIZONS = (5, 20, 60)  # seances

PARAMS = {
    "windows": WINDOWS,
    "weights": WEIGHTS,
    "signs": SIGNS,
    "outcome_horizons": OUTCOME_HORIZONS,
}

STATE_UNDETERMINED = "indéterminé"
STATE_CONFIRMED = "tendance confirmée"
STATE_PULLBACK = "repli dans la tendance"
STATE_REBOUND = "rebond contre-tendance"
STATE_BEARISH = "tendance baissière"


def window_return(px: pd.DataFrame, start_lag: int, end_lag: int) -> pd.Series:
    """Rendement par titre sur la fenetre, depuis la fin de px. NaN si historique insuffisant."""
    if len(px) <= start_lag:
        return pd.Series(np.nan, index=px.columns)
    return px.iloc[-1 - end_lag] / px.iloc[-1 - start_lag] - 1


def classify(bias: float, timing: float) -> str:
    if pd.isna(bias) or pd.isna(timing):
        return STATE_UNDETERMINED
    if bias > 0 and timing > 0:
        return STATE_CONFIRMED
    if bias > 0:
        return STATE_PULLBACK
    if timing > 0:
        return STATE_REBOUND
    return STATE_BEARISH


def compute_snapshot(px: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    """
    px   : cours de cloture, dates (triees) x tickers, benchmarks inclus.
    meta : colonnes ticker, name, benchmark.

    Un titre dont le benchmark est lui-meme (ex: SPY) n'est pas classe : son exces
    vaut 0 par construction et fausserait les rangs des autres. Un titre absent de px
    (aucun cours) est ignore.
    """
    rows = meta.set_index("ticker")
    rows = rows[
        rows.index.isin(px.columns)
        & rows["benchmark"].isin(px.columns)
        & (rows.index != rows["benchmark"])
    ]
    as_of = px.index[-1]

    raw = pd.DataFrame({k: window_return(px, s, e) for k, (s, e) in WINDOWS.items()})
    stock = raw.loc[rows.index]
    bench = raw.loc[rows["benchmark"].values].copy()
    bench.index = rows.index
    ex = stock - bench  # exces de rendement

    pct = ex.rank(pct=True) * 100
    for k, sign in SIGNS.items():
        if sign < 0:
            pct[k] = 100 - pct[k]

    w = pd.Series(WEIGHTS)
    valid = pct.notna()
    score = (pct.fillna(0) * w).sum(axis=1) / (valid * w).sum(axis=1).replace(0, np.nan)

    bias = ex[["r12_1", "r3m"]].mean(axis=1, skipna=True)
    timing = ex[["r1m", "r5d"]].mean(axis=1, skipna=True)

    out = pd.DataFrame(index=rows.index)
    out["as_of"] = as_of.date()
    out["name"] = rows["name"]
    out["benchmark"] = rows["benchmark"]
    out["close"] = px.iloc[-1].reindex(rows.index)
    for k in WINDOWS:
        out[f"ex_{k}"] = ex[k]
    for k in WINDOWS:
        out[f"pct_{k}"] = pct[k]
    out["score"] = score
    out["bias"] = bias
    out["timing"] = timing
    out["state"] = [classify(b, t) for b, t in zip(bias, timing)]
    out["alignment"] = (ex > 0).sum(axis=1)
    out["n_measures"] = ex.notna().sum(axis=1)
    out.index.name = "ticker"
    return out.reset_index()


def settle_outcomes(px: pd.DataFrame, pending: pd.DataFrame) -> pd.DataFrame:
    """
    pending : colonnes snapshot_id, as_of (date), ticker, benchmark, horizon_days.
    Retourne une ligne par echeance atteinte ; les autres restent en attente.
    Rendement reel du titre et du benchmark entre as_of et as_of + horizon_days seances.
    """
    res = []
    idx = px.index
    for r in pending.itertuples(index=False):
        if r.ticker not in px.columns or r.benchmark not in px.columns:
            continue
        pos = idx.searchsorted(pd.Timestamp(r.as_of), side="right") - 1
        if pos < 0 or pos + r.horizon_days >= len(idx):
            continue
        end = pos + r.horizon_days
        p0, p1 = px[r.ticker].iloc[pos], px[r.ticker].iloc[end]
        b0, b1 = px[r.benchmark].iloc[pos], px[r.benchmark].iloc[end]
        if any(pd.isna(v) for v in (p0, p1, b0, b1)):
            continue
        ret, bret = p1 / p0 - 1, b1 / b0 - 1
        res.append(
            {
                "snapshot_id": int(r.snapshot_id),
                "horizon_days": int(r.horizon_days),
                "end_date": idx[end].date(),
                "ret": float(ret),
                "bench_ret": float(bret),
                "excess": float(ret - bret),
                "outperformed": bool(ret > bret),
            }
        )
    return pd.DataFrame(
        res,
        columns=["snapshot_id", "horizon_days", "end_date", "ret", "bench_ret", "excess", "outperformed"],
    )
