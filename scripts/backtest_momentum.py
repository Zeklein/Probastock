#!/usr/bin/env python3
"""
Backtest walk-forward du momentum relatif (vs SPY) de Probastock sur l'historique des cours.

Pour chaque date passée t, uniquement avec les cours <= t :
  - excès de rendement vs SPY : 12-1 mois, 3 mois, 1 mois, 5 jours
  - score 0-100 = moyenne des rangs percentiles (coupe transversale) des 4 mesures
  - état = biais (moyenne des excès 12-1m et 3m > 0 ?) x timing (excès 1 mois > 0 ?)
      biais+ timing+ : tendance confirmée      biais+ timing- : repli dans la tendance
      biais- timing+ : rebond contre-tendance  biais- timing- : tendance baissière
Puis on mesure ce qui s'est réellement passé à +5, +20, +60 séances, et on compare au taux de base
(toutes les observations). NB : définition volontairement simple et transparente ; si app/momentum.py
diffère, aligner les 5 lignes de `compute_features`.

Sources de cours :
  --database-url (ou variable DATABASE_URL) : lit price_snapshots (colonnes détectées, ou --sql)
  --csv fichier.csv : colonnes ticker,date,close
Exemples :
  python backtest_momentum.py
  python backtest_momentum.py --sql "SELECT ticker, date, close FROM price_snapshots"
  python backtest_momentum.py --csv cours.csv --benchmark SPY
"""
import argparse, os, sys
import numpy as np
import pandas as pd

HORIZONS = [5, 20, 60]
STATES = ["tendance confirmée", "repli dans la tendance", "rebond contre-tendance", "tendance baissière"]


# ---------- chargement ----------
def load_long(a):
    if a.csv:
        df = pd.read_csv(a.csv)
    else:
        import psycopg                                     # pip install "psycopg[binary]"
        url = a.database_url or os.environ.get("DATABASE_URL")
        if not url:
            sys.exit("Fournir --database-url, la variable DATABASE_URL ou --csv")
        with psycopg.connect(url) as conn:
            sql = a.sql or autodetect_sql(conn)
            df = pd.read_sql(sql, conn)
    df.columns = [c.lower() for c in df.columns]
    miss = {"ticker", "date", "close"} - set(df.columns)
    if miss:
        sys.exit(f"Colonnes manquantes {miss} : adapter --sql pour renvoyer ticker, date, close")
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    return df[["ticker", "date", "close"]].dropna()


def autodetect_sql(conn):
    cols = pd.read_sql("SELECT column_name FROM information_schema.columns "
                       "WHERE table_name='price_snapshots'", conn)["column_name"].str.lower().tolist()
    if not cols:
        sys.exit("Table price_snapshots introuvable : fournir --sql")
    pick = lambda opts: next((c for c in opts if c in cols), None)
    t = pick(["ticker", "symbol"]); d = pick(["date", "trade_date", "snapshot_date", "as_of", "day"])
    c = pick(["close", "close_price", "adj_close", "price"])
    if t and d and c:
        return f"SELECT {t} AS ticker, {d} AS date, {c} AS close FROM price_snapshots"
    if "asset_id" in cols and d and c:
        return (f"SELECT a.ticker AS ticker, p.{d} AS date, p.{c} AS close "
                f"FROM price_snapshots p JOIN assets a ON a.id = p.asset_id")
    sys.exit(f"Colonnes de price_snapshots non reconnues ({cols}) : fournir --sql")


# ---------- calcul ----------
def compute_features(close, bench):
    """close : DataFrame dates x tickers (sans le benchmark). Retourne dict de DataFrames."""
    def ex(n_from, n_to):                                  # rendement entre t-n_from et t-n_to, moins le benchmark
        r = close.shift(n_to) / close.shift(n_from) - 1
        rb = bench.shift(n_to) / bench.shift(n_from) - 1
        return r.sub(rb, axis=0)
    f = {"r12_1": ex(252, 21), "r3m": ex(63, 0), "r1m": ex(21, 0), "r5d": ex(5, 0)}
    ranks = [f[k].rank(axis=1, pct=True) for k in f]
    score = sum(ranks) / len(ranks) * 100
    bias_pos = (f["r12_1"] + f["r3m"]) / 2 > 0
    timing_pos = f["r1m"] > 0
    state = pd.DataFrame("", index=close.index, columns=close.columns)
    state = state.mask(bias_pos & timing_pos, STATES[0]).mask(bias_pos & ~timing_pos, STATES[1])
    state = state.mask(~bias_pos & timing_pos, STATES[2]).mask(~bias_pos & ~timing_pos, STATES[3])
    valid = f["r12_1"].notna() & f["r3m"].notna() & f["r1m"].notna() & f["r5d"].notna()
    return score.where(valid), state.where(valid, ""), f


def build_obs(close, bench, h, step):
    score, state, _ = compute_features(close, bench)
    fwd = close.shift(-h) / close - 1
    fwd_b = bench.shift(-h) / bench - 1
    fex = fwd.sub(fwd_b, axis=0)
    idx = np.arange(0, len(close), step)
    rows = []
    for i in idx:
        d = close.index[i]
        s, st, fr, fe = score.iloc[i], state.iloc[i], fwd.iloc[i], fex.iloc[i]
        m = s.notna() & fr.notna() & (st != "")
        if m.sum() == 0:
            continue
        rows.append(pd.DataFrame({"date": d, "ticker": s.index[m], "score": s[m].values,
                                  "state": st[m].values, "fwd": fr[m].values, "fex": fe[m].values}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def z_prop(k, n, p0):
    if n == 0 or p0 in (0, 1):
        return float("nan")
    return (k / n - p0) / np.sqrt(p0 * (1 - p0) / n)


def report(close, bench, step):
    out = []
    for h in HORIZONS:
        obs = build_obs(close, bench, h, step)
        if obs.empty:
            print(f"\n+{h} séances : historique insuffisant"); continue
        base_up, base_beat, base_ex = (obs.fwd > 0).mean(), (obs.fex > 0).mean(), obs.fex.mean()
        print(f"\n=== Horizon +{h} séances | {len(obs)} observations, {obs.date.nunique()} dates, "
              f"{obs.ticker.nunique()} titres ===")
        print(f"Taux de base : hausse {base_up*100:.1f}% | bat le SPY {base_beat*100:.1f}% | "
              f"excès moyen {base_ex*100:+.2f}%")
        print(f"{'État':<26}{'Cas':>6}{'Hausse':>9}{'Bat SPY':>9}{'Excès moy.':>12}{'z(bat SPY)':>12}")
        for s in STATES:
            g = obs[obs.state == s]
            if len(g) == 0:
                print(f"{s:<26}{0:>6}"); continue
            beat = (g.fex > 0)
            print(f"{s:<26}{len(g):>6}{(g.fwd>0).mean()*100:>8.1f}%{beat.mean()*100:>8.1f}%"
                  f"{g.fex.mean()*100:>+11.2f}%{z_prop(beat.sum(), len(g), base_beat):>12.2f}")
        # pouvoir de classement : IC de rang par date, sur dates non chevauchantes
        step_nov = max(step, h)
        sub = obs[obs.date.isin(obs.date.drop_duplicates().iloc[::max(1, step_nov // step)])]
        ics = sub.groupby("date").apply(lambda g: g.score.corr(g.fex, method="spearman") if len(g) > 4 else np.nan,
                                        include_groups=False).dropna()
        if len(ics) > 2:
            t = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))
            print(f"IC de rang (score -> excès futur), dates non chevauchantes : {ics.mean():+.3f} "
                  f"(t = {t:.2f}, {len(ics)} dates)")
        sub = sub.assign(q=sub.groupby("date").score.transform(lambda x: pd.qcut(x.rank(method='first'), 5, labels=False)))
        qm = sub.groupby("q").fex.mean() * 100
        if len(qm) == 5:
            print("Excès moyen par quintile de score (Q1 faible -> Q5 fort) : " +
                  "  ".join(f"Q{int(k)+1} {v:+.2f}%" for k, v in qm.items()) + f"  | Q5-Q1 {qm.iloc[4]-qm.iloc[0]:+.2f} pts")
        out.append(obs.assign(h=h))
    print("\nLecture : |z| < 2 et |t| < 2 => pas de différence démontrée avec le hasard. Les cas d'une même date "
          "sont corrélés (marché commun) et les titres ont été choisis AUJOURD'HUI : biais de sélection, "
          "résultats à lire comme descriptifs, pas comme une preuve.")
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv"); ap.add_argument("--database-url"); ap.add_argument("--sql")
    ap.add_argument("--benchmark", default="SPY"); ap.add_argument("--step", type=int, default=5,
                    help="pas d'échantillonnage en séances (5 = hebdomadaire)")
    ap.add_argument("--out", help="CSV de toutes les observations")
    a = ap.parse_args()
    df = load_long(a)
    wide = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()
    if a.benchmark not in wide.columns:
        sys.exit(f"Benchmark {a.benchmark} absent des données")
    wide = wide.ffill(limit=3)
    bench = wide[a.benchmark]
    close = wide.drop(columns=[a.benchmark])
    close = close.loc[:, close.notna().sum() > 300]
    print(f"{close.shape[1]} titres, {len(close)} séances ({close.index[0].date()} -> {close.index[-1].date()})")
    if len(close) < 252 + 60 + 20:
        print("ATTENTION : moins de ~14 mois d'historique ; la mesure 12-1 mois laisse très peu de dates testables.")
    res = report(close, bench, a.step)
    if a.out and not res.empty:
        res.to_csv(a.out, index=False); print(f"Observations écrites dans {a.out}")


if __name__ == "__main__":
    main()
