"""Lance le moteur d'analyse IA (DeepSeek) sur tous les actifs actifs.

Rate limit : DeepSeek n'impose pas de quota strict de requetes/minute (limite
exprimee en concurrence, ~2500 pour deepseek-v4-flash au niveau du compte,
verifie sur la doc/les sources tierces en aout 2026) -- une execution
sequentielle sur 34 tickers est tres largement dans les clous. Une pause
courte est quand meme ajoutee par courtoisie, pas par necessite documentee.

Gestion d'erreur : chaque ticker est traite independamment. Un echec (timeout,
erreur API, JSON malforme) est deja gere sans lever d'exception par
analyze_ticker() lui-meme (stocke en model_runs avec status='failed') ; le
try/except ici couvre en plus les cas vraiment inattendus (ticker introuvable,
bug), pour ne jamais interrompre la boucle.

Usage: python scripts/run_ai_engine_batch.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.ai_engine import analyze_ticker
from app.db import engine

PAUSE_BETWEEN_REQUESTS_SECONDS = 2

# Tarifs deepseek-v4-flash verifies aout 2026 (cache miss) : $0.14 / 1M tokens
# input, $0.28 / 1M tokens output.
PRICE_PER_M_INPUT_USD = 0.14
PRICE_PER_M_OUTPUT_USD = 0.28


def main():
    with engine.connect() as conn:
        tickers = [
            r[0] for r in conn.execute(
                text("SELECT ticker FROM assets WHERE is_active = true ORDER BY ticker")
            ).fetchall()
        ]

    results = []
    for i, ticker in enumerate(tickers):
        try:
            r = analyze_ticker(ticker, provider="deepseek")
        except Exception as e:
            r = {
                "ticker": ticker, "ok": False, "error": f"exception non geree : {e}",
                "analysis": None, "tokens_input": None, "tokens_output": None,
            }

        results.append(r)
        status = "OK" if r["ok"] else f"ECHEC ({r['error']})"
        print(f"[{i + 1}/{len(tickers)}] {ticker}: {status}")

        if i < len(tickers) - 1:
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    # --- Resume --------------------------------------------------------
    succeeded = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    total_in = sum(r["tokens_input"] or 0 for r in results)
    total_out = sum(r["tokens_output"] or 0 for r in results)
    cost = (total_in / 1_000_000) * PRICE_PER_M_INPUT_USD + (total_out / 1_000_000) * PRICE_PER_M_OUTPUT_USD

    directions = {}
    scores = []
    confidences = []
    for r in succeeded:
        a = r["analysis"]
        directions[a["direction"]] = directions.get(a["direction"], 0) + 1
        scores.append(a["score"])
        confidences.append(a["confidence"])

    print()
    print(f"Reussis : {len(succeeded)}/{len(results)}")
    print(f"Echoues : {len(failed)}/{len(results)}")
    if failed:
        for r in failed:
            print(f"  - {r['ticker']}: {r['error']}")

    print()
    print(f"Tokens input total  : {total_in}")
    print(f"Tokens output total : {total_out}")
    print(f"Cout estime         : ${cost:.4f}")

    print()
    print(f"Distribution direction : {directions}")
    if scores:
        print(f"Score   : min={min(scores):.0f} max={max(scores):.0f} moyenne={sum(scores) / len(scores):.1f}")
    if confidences:
        print(f"Confiance : min={min(confidences):.2f} max={max(confidences):.2f} moyenne={sum(confidences) / len(confidences):.2f}")


if __name__ == "__main__":
    main()
