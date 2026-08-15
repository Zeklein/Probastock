"""Moteur d'analyse IA Probastock.

Pipeline : ticker -> dossier standardise (reutilise /fiche telle quelle) ->
prompt -> provider LLM -> JSON structure valide -> stockage model_runs +
predictions.

Interface generique multi-provider : chaque provider est une fonction
`(user_prompt: str, api_key: str) -> ProviderCallResult` enregistree dans
PROVIDERS (deepseek, gemini, claude) -- meme prompt, meme schema JSON, meme
validation/stockage pour les trois.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

import anthropic
import requests
from sqlalchemy import text

from app.db import engine
from app.main import asset_fiche
from scripts._ca_bundle import ensure_ca_bundle

ensure_ca_bundle()

ANALYSIS_HORIZON_DAYS = 20  # correspond a probability_20d ; CHECK predictions.horizon_days IN (1,5,20,60)
BENCHMARK = "SPY"

REQUIRED_LIST_FIELDS = ["key_positive_factors", "key_negative_factors", "catalysts", "red_flags"]
REQUIRED_RANGE_FIELDS = {"score": (0, 100), "probability_20d": (0, 1), "confidence": (0, 1), "risk": (0, 1)}
# Horizons secondaires -- probability_20d reste au niveau racine (compat avec l'existant),
# ces trois-la vivent sous "horizons" pour juger si un titre est plutot un coup court terme
# ou une conviction longue.
HORIZON_FIELDS = {"probability_1d": (0, 1), "probability_5d": (0, 1), "probability_60d": (0, 1)}
VALID_DIRECTIONS = {"bullish", "neutral", "bearish"}
VALID_RECOMMENDATIONS = {"BUY", "HOLD", "REDUCE", "SELL"}

# Garde-fou post-traitement (pas dans le prompt) : score bas mais recommendation
# BUY = incoherence potentielle a surveiller, pas a corriger automatiquement pour
# l'instant -- on veut d'abord voir si ca arrive en pratique.
LOW_SCORE_THRESHOLD = 50

# Schema JSON partage entre les providers qui savent forcer une sortie structuree
# (Claude output_config.format, Gemini responseSchema) -- construit a partir des
# memes constantes que _validate_analysis pour ne jamais diverger du contrat reel.
HORIZONS_SCHEMA = {
    "type": "object",
    "properties": {f: {"type": "number"} for f in HORIZON_FIELDS},
    "required": list(HORIZON_FIELDS),
}

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string", "enum": sorted(VALID_RECOMMENDATIONS)},
        "direction": {"type": "string", "enum": sorted(VALID_DIRECTIONS)},
        **{f: {"type": "number"} for f in REQUIRED_RANGE_FIELDS},
        **{f: {"type": "array", "items": {"type": "string"}} for f in REQUIRED_LIST_FIELDS},
        "horizons": HORIZONS_SCHEMA,
    },
    "required": ["recommendation", "direction"] + list(REQUIRED_RANGE_FIELDS) + REQUIRED_LIST_FIELDS + ["horizons"],
}

# Claude exige "additionalProperties: false" explicite sur les schemas object,
# alors que ce champ n'est pas dans le sous-ensemble OpenAPI supporte par le
# responseSchema de Gemini -- variante dediee plutot que de risquer de casser
# Gemini avec un champ qu'il ne reconnait pas. Le "false" doit aussi etre repete
# sur le sous-objet "horizons" -- Claude le verifie a chaque niveau imbrique.
CLAUDE_RESPONSE_JSON_SCHEMA = {
    **RESPONSE_JSON_SCHEMA,
    "properties": {**RESPONSE_JSON_SCHEMA["properties"], "horizons": {**HORIZONS_SCHEMA, "additionalProperties": False}},
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "Tu es un analyste financier senior. Un client te demande, sur le titre suivant : "
    "\"Dois-je acheter, garder, alleger ou vendre cette position maintenant ?\" Il attend "
    "une vraie recommandation, engagee, pas une reponse prudente qui evite de trancher. "
    "Base-toi strictement sur le dossier standardise fourni (cours, indicateurs techniques, "
    "valorisation, consensus analystes). Reponds UNIQUEMENT avec un objet JSON valide, sans "
    "texte autour, avec exactement ces champs :\n"
    "{\n"
    '  "recommendation": "BUY" | "HOLD" | "REDUCE" | "SELL",\n'
    "    -- ta recommandation actionnable, le champ le plus important de cette reponse.\n"
    "    -- BUY : le titre merite d'etre achete/renforce maintenant.\n"
    "    -- HOLD : conserver une position existante, mais pas d'argument fort pour en ouvrir une nouvelle.\n"
    "    -- REDUCE : alleger une position existante, signaux inquietants mais pas au point de tout liquider.\n"
    "    -- SELL : sortir completement / eviter le titre.\n"
    "    -- Prends reellement position comme un analyste qui engage sa reputation : n'utilise PAS HOLD par\n"
    "    -- defaut pour eviter de trancher quand les donnees pointent clairement dans un sens.\n"
    '  "direction": "bullish" | "neutral" | "bearish" (lecture de tendance, distincte de recommendation),\n'
    '  "score": nombre entre 0 et 100 (score global d\'opportunite, 0=tres negatif, 100=tres positif),\n'
    '  "probability_20d": nombre entre 0 et 1 (probabilite de surperformer le benchmark SPY sur 20 jours de bourse),\n'
    '  "confidence": nombre entre 0 et 1 (ta confiance dans cette analyse, compte tenu de la qualite/quantite des donnees disponibles),\n'
    '  "risk": nombre entre 0 et 1 (niveau de risque percu, 0=faible, 1=eleve),\n'
    '  "key_positive_factors": ["...", ...],\n'
    '  "key_negative_factors": ["...", ...],\n'
    '  "catalysts": ["...", ...] (evenements a venir pouvant faire bouger le cours),\n'
    '  "red_flags": ["...", ...] (signaux d\'alerte specifiques, peut etre vide),\n'
    '  "horizons": {\n'
    '    "probability_1d": nombre entre 0 et 1,\n'
    '    "probability_5d": nombre entre 0 et 1,\n'
    '    "probability_60d": nombre entre 0 et 1\n'
    "  } -- meme lecture que probability_20d (probabilite de surperformer SPY) mais a 1 jour,\n"
    "    -- 5 jours et 60 jours de bourse. Cette action est-elle plus interessante a court terme\n"
    "    -- ou plus long terme ? Un catalyseur imminent peut faire monter probability_1d/5d sans\n"
    "    -- soutenir probability_60d, et inversement une conviction structurelle peut ne rien\n"
    "    -- donner a court terme. Ces 4 probabilites (1j/5j/20j/60j) n'ont pas a converger.\n"
    "}\n"
    "Si des donnees sont marquees absentes ou 'non couvert par les analystes' dans le dossier, "
    "baisse ta confidence en consequence plutot que d'inventer des chiffres -- mais ca ne "
    "t'empeche pas de trancher sur recommendation."
)


@dataclass
class ProviderCallResult:
    provider: str
    model: str
    ok: bool
    raw_content: str | None = None
    parsed: dict | None = None
    error: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    warnings: list[str] = field(default_factory=list)


def _validate_analysis(parsed) -> tuple[dict | None, str | None]:
    if not isinstance(parsed, dict):
        return None, "la reponse n'est pas un objet JSON"

    missing = [
        f for f in list(REQUIRED_RANGE_FIELDS) + REQUIRED_LIST_FIELDS + ["direction", "recommendation"]
        if f not in parsed
    ]
    if missing:
        return None, f"champs manquants dans la reponse : {', '.join(missing)}"

    if parsed["direction"] not in VALID_DIRECTIONS:
        return None, f"direction invalide : {parsed['direction']!r}"

    if parsed["recommendation"] not in VALID_RECOMMENDATIONS:
        return None, f"recommendation invalide : {parsed['recommendation']!r}"

    for fld, (lo, hi) in REQUIRED_RANGE_FIELDS.items():
        val = parsed[fld]
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not (lo <= val <= hi):
            return None, f"champ {fld!r} invalide (attendu nombre entre {lo} et {hi}, reçu {val!r})"

    for fld in REQUIRED_LIST_FIELDS:
        if not isinstance(parsed[fld], list):
            return None, f"champ {fld!r} doit etre une liste"

    horizons = parsed.get("horizons")
    if not isinstance(horizons, dict):
        return None, "champ 'horizons' manquant ou n'est pas un objet"
    missing_h = [f for f in HORIZON_FIELDS if f not in horizons]
    if missing_h:
        return None, f"champs manquants dans 'horizons' : {', '.join(missing_h)}"
    for fld, (lo, hi) in HORIZON_FIELDS.items():
        val = horizons[fld]
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not (lo <= val <= hi):
            return None, f"champ horizons.{fld!r} invalide (attendu nombre entre {lo} et {hi}, reçu {val!r})"

    return parsed, None


def _consistency_warnings(parsed: dict) -> list[str]:
    """Garde-fou en post-traitement : signale les incoherences entre le score
    continu et la recommendation categorique, sans jamais bloquer le stockage.
    Volontairement limite au cas demande (score bas + BUY) pour l'instant --
    on observe avant de durcir."""
    warnings = []
    if parsed["score"] < LOW_SCORE_THRESHOLD and parsed["recommendation"] == "BUY":
        warnings.append(
            f"incoherence potentielle : score={parsed['score']} (< {LOW_SCORE_THRESHOLD}) "
            f"mais recommendation=BUY"
        )
    return warnings


# --- Providers -----------------------------------------------------------

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"


def _call_deepseek(user_prompt: str, api_key: str) -> ProviderCallResult:
    try:
        resp = requests.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 8000,  # laisser de la marge : une reponse tronquee produit un JSON invalide (deja observe a 1500, 4000, puis 6000 sur CRWV/FTC.L)
                "temperature": 0.3,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return ProviderCallResult(provider="deepseek", model=DEEPSEEK_MODEL, ok=False, error=f"appel API DeepSeek echoue : {e}")

    usage = data.get("usage", {})
    tokens_input = usage.get("prompt_tokens")
    tokens_output = usage.get("completion_tokens")

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        return ProviderCallResult(
            provider="deepseek", model=DEEPSEEK_MODEL, ok=False,
            error=f"reponse DeepSeek sans contenu exploitable : {e}",
            tokens_input=tokens_input, tokens_output=tokens_output,
        )

    try:
        parsed_raw = json.loads(content)
    except json.JSONDecodeError as e:
        return ProviderCallResult(
            provider="deepseek", model=DEEPSEEK_MODEL, ok=False, raw_content=content,
            error=f"JSON malforme : {e}", tokens_input=tokens_input, tokens_output=tokens_output,
        )

    parsed, validation_error = _validate_analysis(parsed_raw)
    warnings = _consistency_warnings(parsed) if parsed is not None else []
    for w in warnings:
        print(f"AVERTISSEMENT [ai_engine/deepseek] : {w}")

    return ProviderCallResult(
        provider="deepseek", model=DEEPSEEK_MODEL, ok=parsed is not None,
        raw_content=content, parsed=parsed, error=validation_error,
        tokens_input=tokens_input, tokens_output=tokens_output, warnings=warnings,
    )


GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-3.7-flash"


def _call_gemini(user_prompt: str, api_key: str) -> ProviderCallResult:
    try:
        resp = requests.post(
            GEMINI_API_URL.format(model=GEMINI_MODEL),
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": user_prompt}]}],
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": RESPONSE_JSON_SCHEMA,
                    "maxOutputTokens": 8000,  # marge pour le raisonnement interne du modele avant le JSON final
                    "temperature": 0.3,
                },
                # pas de "tools" : aucune recherche web / grounding, pour comparer les 3 providers a donnees egales (le dossier /fiche uniquement)
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return ProviderCallResult(provider="gemini", model=GEMINI_MODEL, ok=False, error=f"appel API Gemini echoue : {e}")

    usage = data.get("usageMetadata", {})
    tokens_input = usage.get("promptTokenCount")
    tokens_output = usage.get("candidatesTokenCount")

    try:
        content = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        finish_reason = data.get("candidates", [{}])[0].get("finishReason")
        return ProviderCallResult(
            provider="gemini", model=GEMINI_MODEL, ok=False,
            error=f"reponse Gemini sans contenu exploitable (finishReason={finish_reason}) : {e}",
            tokens_input=tokens_input, tokens_output=tokens_output,
        )

    try:
        parsed_raw = json.loads(content)
    except json.JSONDecodeError as e:
        return ProviderCallResult(
            provider="gemini", model=GEMINI_MODEL, ok=False, raw_content=content,
            error=f"JSON malforme : {e}", tokens_input=tokens_input, tokens_output=tokens_output,
        )

    parsed, validation_error = _validate_analysis(parsed_raw)
    warnings = _consistency_warnings(parsed) if parsed is not None else []
    for w in warnings:
        print(f"AVERTISSEMENT [ai_engine/gemini] : {w}")

    return ProviderCallResult(
        provider="gemini", model=GEMINI_MODEL, ok=parsed is not None,
        raw_content=content, parsed=parsed, error=validation_error,
        tokens_input=tokens_input, tokens_output=tokens_output, warnings=warnings,
    )


CLAUDE_MODEL = "claude-sonnet-5"


def _call_claude(user_prompt: str, api_key: str) -> ProviderCallResult:
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,  # marge pour le raisonnement adaptatif avant le JSON final (meme logique que Gemini)
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"format": {"type": "json_schema", "schema": CLAUDE_RESPONSE_JSON_SCHEMA}},
        )
    except Exception as e:
        return ProviderCallResult(provider="claude", model=CLAUDE_MODEL, ok=False, error=f"appel API Claude echoue : {e}")

    tokens_input = response.usage.input_tokens
    tokens_output = response.usage.output_tokens

    if response.stop_reason == "refusal":
        return ProviderCallResult(
            provider="claude", model=CLAUDE_MODEL, ok=False,
            error="reponse refusee par les garde-fous de securite Claude",
            tokens_input=tokens_input, tokens_output=tokens_output,
        )
    if response.stop_reason == "max_tokens":
        return ProviderCallResult(
            provider="claude", model=CLAUDE_MODEL, ok=False,
            error="reponse Claude tronquee (max_tokens atteint)",
            tokens_input=tokens_input, tokens_output=tokens_output,
        )

    content = next((b.text for b in response.content if b.type == "text"), None)
    if content is None:
        return ProviderCallResult(
            provider="claude", model=CLAUDE_MODEL, ok=False,
            error="reponse Claude sans contenu texte exploitable",
            tokens_input=tokens_input, tokens_output=tokens_output,
        )

    try:
        parsed_raw = json.loads(content)
    except json.JSONDecodeError as e:
        return ProviderCallResult(
            provider="claude", model=CLAUDE_MODEL, ok=False, raw_content=content,
            error=f"JSON malforme : {e}", tokens_input=tokens_input, tokens_output=tokens_output,
        )

    parsed, validation_error = _validate_analysis(parsed_raw)
    warnings = _consistency_warnings(parsed) if parsed is not None else []
    for w in warnings:
        print(f"AVERTISSEMENT [ai_engine/claude] : {w}")

    return ProviderCallResult(
        provider="claude", model=CLAUDE_MODEL, ok=parsed is not None,
        raw_content=content, parsed=parsed, error=validation_error,
        tokens_input=tokens_input, tokens_output=tokens_output, warnings=warnings,
    )


PROVIDERS = {
    "deepseek": _call_deepseek,
    "gemini": _call_gemini,
    "claude": _call_claude,
}

PROVIDER_API_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}


# --- Stockage --------------------------------------------------------------

def _store_result(ticker: str, fiche: dict, result: ProviderCallResult) -> dict:
    with engine.begin() as conn:
        asset_id = conn.execute(text("SELECT id FROM assets WHERE ticker = :t"), {"t": ticker}).scalar_one()

        run_row = conn.execute(
            text(
                """
                INSERT INTO model_runs (model_version, status, assets_processed, data_sources, error_message, metadata)
                VALUES (:model_version, :status, 1, ARRAY['probastock_fiche'], :error_message, CAST(:metadata AS jsonb))
                RETURNING id
                """
            ),
            {
                "model_version": result.model,
                "status": "completed" if result.ok else "failed",
                "error_message": result.error,
                "metadata": json.dumps(
                    {
                        "provider": result.provider,
                        "ticker": ticker,
                        "tokens_input": result.tokens_input,
                        "tokens_output": result.tokens_output,
                    }
                ),
            },
        ).mappings().first()
        model_run_id = run_row["id"]

        prediction_id = None
        if result.ok:
            p = result.parsed
            prediction_date = date.today()
            row = conn.execute(
                text(
                    """
                    INSERT INTO predictions (
                        asset_id, model_run_id, prediction_date, horizon_days, target_date,
                        benchmark, outperform_probability, conviction_score, model_version,
                        features_snapshot, direction, recommendation, score, risk_score, analysis_factors, horizons
                    ) VALUES (
                        :asset_id, :model_run_id, :prediction_date, :horizon_days, :target_date,
                        :benchmark, :outperform_probability, :conviction_score, :model_version,
                        CAST(:features_snapshot AS jsonb), :direction, :recommendation, :score, :risk_score, CAST(:analysis_factors AS jsonb), CAST(:horizons AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "asset_id": asset_id,
                    "model_run_id": model_run_id,
                    "prediction_date": prediction_date,
                    "horizon_days": ANALYSIS_HORIZON_DAYS,
                    "target_date": prediction_date + timedelta(days=ANALYSIS_HORIZON_DAYS),
                    "benchmark": BENCHMARK,
                    "outperform_probability": p["probability_20d"],
                    "conviction_score": p["confidence"],
                    "model_version": result.model,
                    "features_snapshot": json.dumps(fiche, default=str),
                    "direction": p["direction"],
                    "recommendation": p["recommendation"],
                    "score": p["score"],
                    "risk_score": p["risk"],
                    "analysis_factors": json.dumps({k: p[k] for k in REQUIRED_LIST_FIELDS}),
                    "horizons": json.dumps(p["horizons"]),
                },
            ).mappings().first()
            prediction_id = row["id"]

    return {"model_run_id": model_run_id, "prediction_id": prediction_id}


# --- Orchestration -----------------------------------------------------------

def analyze_ticker(ticker: str, provider: str = "deepseek") -> dict:
    """Construit le dossier /fiche pour ticker, l'envoie au provider demande,
    stocke le resultat (succes ou echec) en base, et renvoie un recapitulatif
    complet -- utile pour les tests manuels et pour un futur appelant HTTP."""
    if provider not in PROVIDERS:
        raise ValueError(f"provider inconnu : {provider!r} (disponibles : {list(PROVIDERS)})")

    api_key = os.environ.get(PROVIDER_API_KEY_ENV[provider])
    if not api_key:
        raise RuntimeError(
            f"{PROVIDER_API_KEY_ENV[provider]} absente de l'environnement -- "
            f"impossible d'appeler le provider {provider!r}."
        )

    fiche = asset_fiche(ticker)
    result = PROVIDERS[provider](fiche["text"], api_key)
    storage = _store_result(ticker, fiche, result)

    return {
        "ticker": ticker,
        "provider": result.provider,
        "model": result.model,
        "ok": result.ok,
        "analysis": result.parsed,
        "raw_content": result.raw_content,
        "error": result.error,
        "warnings": result.warnings,
        "tokens_input": result.tokens_input,
        "tokens_output": result.tokens_output,
        "model_run_id": storage["model_run_id"],
        "prediction_id": storage["prediction_id"],
    }
