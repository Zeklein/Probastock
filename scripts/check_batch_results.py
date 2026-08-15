"""Verifie apres coup le fichier de resultats du batch IA (produit par
run_ai_engine_batch_all_providers.py) et fait echouer le job CI si le run est
significativement en echec -- ne duplique pas la gestion d'erreur par
(ticker, provider) deja geree dans analyze_ticker(), sert juste a rendre un
echec visible (exit code + annotation GitHub) plutot qu'un run "vert" qui a
en realite tourne a moitie a vide.

Usage: python scripts/check_batch_results.py
"""
import json
import sys
from pathlib import Path

RESULTS_PATH = Path(__file__).resolve().parent / "_last_batch_all_providers_results.json"
PROVIDERS = ["deepseek", "gemini", "claude"]

# Seuil "echec significatif" : au-dela de ce nombre d'appels en echec sur les
# 102 (34 tickers x 3 providers), ou si un provider entier est tombe a 0
# succes, le run merite une alerte plutot qu'un ok silencieux. ~10% choisi
# arbitrairement comme premier seuil raisonnable -- a ajuster avec l'usage.
FAILURE_COUNT_THRESHOLD = 10


def main():
    if not RESULTS_PATH.exists():
        print(f"::error::Fichier de resultats introuvable : {RESULTS_PATH}")
        sys.exit(1)

    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    total_failed = sum(1 for r in results if not r["ok"])

    provider_total = {p: sum(1 for r in results if r["provider"] == p) for p in PROVIDERS}
    provider_success = {p: sum(1 for r in results if r["provider"] == p and r["ok"]) for p in PROVIDERS}
    down_providers = [p for p in PROVIDERS if provider_total[p] > 0 and provider_success[p] == 0]

    print(f"Echecs totaux : {total_failed}/{len(results)}")
    for p in PROVIDERS:
        print(f"  {p}: {provider_success[p]}/{provider_total[p]} reussis")

    if total_failed > FAILURE_COUNT_THRESHOLD or down_providers:
        reasons = []
        if total_failed > FAILURE_COUNT_THRESHOLD:
            reasons.append(f"{total_failed} echecs (seuil : {FAILURE_COUNT_THRESHOLD})")
        if down_providers:
            reasons.append(f"provider(s) totalement en panne : {down_providers}")
        print(f"::error::Run IA significativement en echec -- {' ; '.join(reasons)}")
        sys.exit(1)

    print("Run OK -- pas d'echec significatif.")


if __name__ == "__main__":
    main()
