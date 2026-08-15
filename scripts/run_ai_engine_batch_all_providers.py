"""Lance le moteur d'analyse IA sur tous les actifs actifs, pour les 3
providers (DeepSeek, Gemini, Claude). Stocke chaque analyse independamment
en base (aucune agregation/scoring Probastock ici -- viendra dans une etape
suivante).

Gestion d'erreur : chaque (ticker, provider) est traite independamment.
analyze_ticker() gere deja en interne les echecs "attendus" (troncature,
JSON malforme, erreur API) sans lever d'exception, en les stockant dans
model_runs avec status='failed' + error_message. Le try/except ici couvre
en plus les cas vraiment inattendus (ticker introuvable, bug), pour ne
jamais interrompre la boucle -- un echec sur un couple ne doit jamais
bloquer les 101 autres appels.

Boucle provider-exterieur / ticker-interieur : isole un incident propre a
un provider (ex: la troncature DeepSeek deja observee sur CRWV) du
deroulement des deux autres providers.

Usage: python scripts/run_ai_engine_batch_all_providers.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.aggregation import aggregate_ticker
from app.ai_engine import analyze_ticker
from app.db import engine

PAUSE_BETWEEN_REQUESTS_SECONDS = 2  # meme pause "par courtoisie" que le batch DeepSeek existant, appliquee aux 3 providers a defaut de limite stricte documentee pour Gemini/Claude a ce volume

PROVIDERS = ["deepseek", "gemini", "claude"]

# Tarifs verifies (aout 2026), $/1M tokens (in, out) :
PRICING = {
    "deepseek": (0.14, 0.28),
    "gemini": (0.75, 3.75),   # gemini-3.7-flash, tarif intro jusqu'au 2026-12-31
    "claude": (2.00, 10.00),  # claude-sonnet-5, tarif intro jusqu'au 2026-08-31
}

RESULTS_JSON_PATH = Path(__file__).resolve().parent.parent / "scripts" / "_last_batch_all_providers_results.json"


def main():
    with engine.connect() as conn:
        tickers = [
            r[0] for r in conn.execute(
                text("SELECT ticker FROM assets WHERE is_active = true ORDER BY ticker")
            ).fetchall()
        ]

    all_results = []  # liste de dicts : ticker, provider, ok, analysis, error, tokens_input, tokens_output

    for provider in PROVIDERS:
        print(f"\n{'='*70}\nPROVIDER : {provider}\n{'='*70}")
        for i, ticker in enumerate(tickers):
            try:
                r = analyze_ticker(ticker, provider=provider)
            except Exception as e:
                r = {
                    "ticker": ticker, "provider": provider, "ok": False,
                    "error": f"exception non geree : {e}",
                    "analysis": None, "tokens_input": None, "tokens_output": None,
                }

            all_results.append(r)
            status = "OK" if r["ok"] else f"ECHEC ({r['error']})"
            print(f"[{provider}][{i + 1}/{len(tickers)}] {ticker}: {status}")

            if i < len(tickers) - 1:
                time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    RESULTS_JSON_PATH.write_text(
        json.dumps(all_results, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(f"\nResultats bruts sauvegardes dans {RESULTS_JSON_PATH}")

    # --- Accord / desaccord entre providers, par ticker -----------------
    by_ticker = {}
    for r in all_results:
        by_ticker.setdefault(r["ticker"], {})[r["provider"]] = r

    # --- Agregation Probastock (moyenne des providers ayant reussi CE run) --
    # Seuls les resultats ok=True obtenus dans cette execution entrent dans
    # l'agregation -- jamais une prediction plus ancienne repechee en base,
    # meme si elle existe pour le meme ticker/date (cf. app/aggregation.py).
    # Un provider en echec compte pour 0, jamais pour une valeur perimee.
    print(f"\n{'='*70}\nAGREGATION\n{'='*70}")
    aggregated = []
    for ticker in tickers:
        entry = by_ticker.get(ticker, {})
        successful = [entry[p] for p in PROVIDERS if entry.get(p) and entry[p]["ok"]]
        agg = aggregate_ticker(ticker, successful)
        if agg is None:
            failed_providers = [p for p in PROVIDERS if not (entry.get(p) and entry[p]["ok"])]
            print(f"[agregation] {ticker}: ignore -- seulement {len(successful)}/3 providers ok (echec : {failed_providers})")
            continue
        aggregated.append(agg)
        print(
            f"[agregation] {ticker}: score={agg['score_agrege']:.1f} "
            f"reco={agg['recommendation_finale']} conviction={agg['conviction']:.2f} "
            f"(n={agg['provider_count']})"
        )

    # --- Resume par provider --------------------------------------------
    print(f"\n\n{'#'*70}\nRESUME\n{'#'*70}")

    total_cost = 0.0
    for provider in PROVIDERS:
        p_results = [r for r in all_results if r["provider"] == provider]
        succeeded = [r for r in p_results if r["ok"]]
        failed = [r for r in p_results if not r["ok"]]

        total_in = sum(r["tokens_input"] or 0 for r in p_results)
        total_out = sum(r["tokens_output"] or 0 for r in p_results)
        price_in, price_out = PRICING[provider]
        cost = (total_in / 1_000_000) * price_in + (total_out / 1_000_000) * price_out
        total_cost += cost

        recos = {}
        for r in succeeded:
            reco = r["analysis"]["recommendation"]
            recos[reco] = recos.get(reco, 0) + 1

        print(f"\n--- {provider} ---")
        print(f"Reussis : {len(succeeded)}/{len(p_results)}")
        if failed:
            print("Echecs :")
            for r in failed:
                print(f"  - {r['ticker']}: {r['error']}")
        print(f"Tokens in={total_in} out={total_out}  Cout=${cost:.4f}")
        print(f"Distribution recommendation : {recos}")

    print(f"\nCout total (3 providers) : ${total_cost:.4f}")

    # --- Accord / desaccord entre providers, par ticker (by_ticker construit plus haut) ---
    unanimous = []
    disagree = []
    for ticker in tickers:
        entry = by_ticker.get(ticker, {})
        recos = {}
        for provider in PROVIDERS:
            r = entry.get(provider)
            if r and r["ok"]:
                recos[provider] = r["analysis"]["recommendation"]
        if len(recos) < 2:
            continue  # pas assez de reponses valides pour juger d'un accord
        distinct = set(recos.values())
        if len(distinct) == 1 and len(recos) == len(PROVIDERS):
            unanimous.append((ticker, recos))
        elif len(distinct) > 1:
            disagree.append((ticker, recos))

    print(f"\nTickers unanimes (3/3 providers reussis, meme recommendation) : {len(unanimous)}")
    print(f"Tickers en desaccord (au moins un providers different)         : {len(disagree)}")

    SEVERITY = {"BUY": 0, "HOLD": 1, "REDUCE": 2, "SELL": 3}
    scored_disagree = []
    for ticker, recos in disagree:
        spread = max(SEVERITY[v] for v in recos.values()) - min(SEVERITY[v] for v in recos.values())
        scored_disagree.append((spread, ticker, recos))
    scored_disagree.sort(key=lambda x: -x[0])

    print("\nDesaccords les plus marques :")
    for spread, ticker, recos in scored_disagree:
        print(f"  {ticker}: {recos}  (ecart={spread})")


if __name__ == "__main__":
    main()
