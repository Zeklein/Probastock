-- =============================================================================
-- Probastock — Schéma PostgreSQL initial
-- =============================================================================
-- Principe d'immutabilité : on n'UPDATE jamais les données brutes.
-- Chaque collecte crée une nouvelle ligne avec un fetched_at précis.
-- Pour savoir "ce que le modèle savait le jour J", on filtre WHERE fetched_at <= J.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Table : assets
-- Les 34 valeurs suivies. Référencée par toutes les autres tables.
-- -----------------------------------------------------------------------------
CREATE TABLE assets (
    id          SERIAL PRIMARY KEY,
    ticker      TEXT    NOT NULL UNIQUE,         -- ex: "AAPL", "CW8.PA"
    name        TEXT    NOT NULL,                -- ex: "Apple Inc."
    asset_type  TEXT    NOT NULL                 -- "stock" ou "etf"
                CHECK (asset_type IN ('stock', 'etf')),
    sector      TEXT,                            -- ex: "Technology", NULL ok pour les ETF
    notes       TEXT,                            -- particularité: ETF, ADR, place boursière étrangère, etc.
    is_active   BOOLEAN NOT NULL DEFAULT true,   -- false = on arrête de suivre cet actif
    is_favorite BOOLEAN NOT NULL DEFAULT false,  -- affichage/tri dashboard uniquement, aucun impact sur le pipeline automatisé
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE assets IS 'Référentiel des actifs suivis. Jamais supprimés, désactivés via is_active.';


-- -----------------------------------------------------------------------------
-- Table : price_snapshots
-- Cours et volumes quotidiens. Chaque collecte est une ligne indépendante.
-- Pourquoi ne pas avoir une contrainte UNIQUE(asset_id, trade_date) ?
-- Parce qu'on veut pouvoir stocker plusieurs collectes pour la même date
-- (collecte du soir, correction du lendemain, etc.). Pour obtenir le prix
-- "officiel" d'une date, on prend MAX(fetched_at) pour ce trade_date.
-- -----------------------------------------------------------------------------
CREATE TABLE price_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    asset_id    INTEGER     NOT NULL REFERENCES assets(id),
    trade_date  DATE        NOT NULL,            -- date de bourse à laquelle le prix s'applique
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(), -- moment exact de la collecte
    open        NUMERIC(12, 4),
    high        NUMERIC(12, 4),
    low         NUMERIC(12, 4),
    close       NUMERIC(12, 4) NOT NULL,         -- seul champ obligatoire
    volume      BIGINT,
    source      TEXT        NOT NULL,            -- ex: "yahoo_finance", "alpha_vantage"
    currency    TEXT                             -- devise native du prix, ex: "USD", "GBp", "KRW" (telle que renvoyée par la source)
);

COMMENT ON TABLE price_snapshots IS 'Cours quotidiens en mode append-only. Plusieurs lignes peuvent exister pour un même (asset, trade_date) si la donnée a été re-collectée.';
COMMENT ON COLUMN price_snapshots.fetched_at IS 'Horodatage de collecte. Permet de reconstituer ce que le modèle savait à tout instant passé.';

CREATE INDEX idx_price_asset_date     ON price_snapshots(asset_id, trade_date DESC);
CREATE INDEX idx_price_asset_fetched  ON price_snapshots(asset_id, fetched_at DESC);


-- -----------------------------------------------------------------------------
-- Table : fundamentals_snapshots
-- Données fondamentales (PER, market cap, croissance, etc.).
-- On utilise JSONB pour le champ "data" car les métriques disponibles varient
-- selon la source et l'actif (un ETF n'a pas de PER). Cela évite d'avoir
-- 30 colonnes dont la moitié sont NULL. On peut toujours indexer des clés JSONB.
-- -----------------------------------------------------------------------------
CREATE TABLE fundamentals_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    asset_id        INTEGER     NOT NULL REFERENCES assets(id),
    snapshot_date   DATE        NOT NULL,        -- date à laquelle ces fondamentaux s'appliquent
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT        NOT NULL,
    data            JSONB       NOT NULL          -- ex: {"pe_ratio": 28.5, "market_cap_usd": 3e12}
);

COMMENT ON TABLE fundamentals_snapshots IS 'Données fondamentales en mode append-only. JSONB pour flexibilité selon actif et source.';

CREATE INDEX idx_fund_asset_date     ON fundamentals_snapshots(asset_id, snapshot_date DESC);
CREATE INDEX idx_fund_data_gin       ON fundamentals_snapshots USING GIN (data);  -- permet de requêter les clés JSONB


-- -----------------------------------------------------------------------------
-- Table : news_items
-- Articles collectés par actif. Deux timestamps distincts :
--   published_at : quand l'article a été publié (fourni par la source)
--   collected_at : quand notre pipeline l'a récupéré
-- L'URL est unique pour éviter les doublons entre collectes.
-- -----------------------------------------------------------------------------
CREATE TABLE news_items (
    id              BIGSERIAL PRIMARY KEY,
    asset_id        INTEGER     NOT NULL REFERENCES assets(id),
    published_at    TIMESTAMPTZ NOT NULL,         -- date de publication réelle
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(), -- date de notre collecte
    source          TEXT        NOT NULL,         -- media/éditeur, ex: "reuters", "seeking_alpha" (PAS l'API utilisée)
    provider        TEXT        NOT NULL,         -- API qui a fourni la ligne : 'alphavantage' / 'finnhub' / 'marketaux'
    url             TEXT        NOT NULL UNIQUE,  -- clé naturelle anti-doublon (par provider : une même actu peut réapparaître sous 2 URLs différentes, cf. dédup applicative par similarité de titre)
    title           TEXT        NOT NULL,
    summary         TEXT,                         -- extrait ou résumé généré
    sentiment_score NUMERIC(4, 3)                 -- optionnel, entre -1.000 et 1.000 ; NULL si le provider n'en fournit pas (ex: Finnhub)
                    CHECK (sentiment_score BETWEEN -1 AND 1)
);

COMMENT ON TABLE news_items IS 'News collectées par actif. published_at = date réelle de l article, collected_at = date de notre collecte.';

CREATE INDEX idx_news_asset_published ON news_items(asset_id, published_at DESC);
CREATE INDEX idx_news_collected       ON news_items(collected_at DESC);


-- -----------------------------------------------------------------------------
-- Table : model_runs
-- Trace chaque exécution du pipeline de prédiction.
-- Créée AVANT predictions car les prédictions y font référence.
-- -----------------------------------------------------------------------------
CREATE TABLE model_runs (
    id                  SERIAL PRIMARY KEY,
    run_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_version       TEXT        NOT NULL,     -- ex: "v1.0", "v1.2-sentiment"
    status              TEXT        NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed', 'failed')),
    assets_processed    INTEGER,                  -- combien d'actifs traités
    data_sources        TEXT[]      NOT NULL,     -- ex: ARRAY['yahoo_finance','reuters']
    error_message       TEXT,                     -- NULL si succès
    metadata            JSONB                     -- tout autre info utile (durée, nb prédictions, etc.)
);

COMMENT ON TABLE model_runs IS 'Journal de chaque exécution du pipeline. Chaque run produit N prédictions (une par actif × horizon).';


-- -----------------------------------------------------------------------------
-- Table : predictions
-- Le "Prediction Ledger" : immuable une fois écrit.
-- Une ligne = une prédiction pour un actif, à un horizon donné, à une date donnée.
-- La première moitié des colonnes est remplie à la création.
-- La seconde moitié (actual_*, outperformed, resolved_at) est remplie plus tard
-- quand la date cible est atteinte.
-- -----------------------------------------------------------------------------
CREATE TABLE predictions (
    id                      BIGSERIAL PRIMARY KEY,
    asset_id                INTEGER     NOT NULL REFERENCES assets(id),
    model_run_id            INTEGER     NOT NULL REFERENCES model_runs(id),

    -- Ce que le modèle prédit
    predicted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),  -- moment exact de la prédiction
    prediction_date         DATE        NOT NULL,                -- "prédit le..."
    horizon_days            INTEGER     NOT NULL
                            CHECK (horizon_days IN (1, 5, 20, 60)),
    target_date             DATE        NOT NULL,                -- prediction_date + horizon_days (stocké pour faciliter les requêtes)
    benchmark               TEXT        NOT NULL,                -- ex: "SP500", "CAC40" — référence de surperformance
    outperform_probability  NUMERIC(5, 4) NOT NULL               -- probabilité entre 0.0000 et 1.0000
                            CHECK (outperform_probability BETWEEN 0 AND 1),
    expected_return         NUMERIC(8, 5),                       -- rendement prédit sur l'horizon (ex: 0.03500 = +3.5%) ; NULL pour un pipeline qualitatif qui ne produit pas ce chiffre
    conviction_score        NUMERIC(4, 3) NOT NULL               -- score interne de confiance, entre 0 et 1
                            CHECK (conviction_score BETWEEN 0 AND 1),
    model_version           TEXT        NOT NULL,                -- dupliqué depuis model_runs pour faciliter les requêtes
    features_snapshot       JSONB,                               -- optionnel : valeurs des features utilisées (pour audit)

    -- Sortie qualitative des moteurs d'analyse IA (DeepSeek, puis Gemini/Claude)
    direction               TEXT
                            CHECK (direction IN ('bullish', 'neutral', 'bearish')),
    recommendation           TEXT                                 -- champ decisionnel principal, plus actionnable que direction
                            CHECK (recommendation IN ('BUY', 'HOLD', 'REDUCE', 'SELL')),
    score                   NUMERIC(5, 2)                        -- score global 0-100, distinct de conviction_score (0-1)
                            CHECK (score BETWEEN 0 AND 100),
    risk_score              NUMERIC(4, 3)                        -- niveau de risque perçu, entre 0 et 1
                            CHECK (risk_score BETWEEN 0 AND 1),
    analysis_factors        JSONB,                               -- {key_positive_factors, key_negative_factors, catalysts, red_flags}
    horizons                JSONB,                               -- {probability_1d, probability_5d, probability_60d} ; probability_20d reste au niveau racine

    -- Résultat réel, rempli après target_date
    actual_close_at_target  NUMERIC(12, 4),                      -- prix de clôture constaté à target_date
    actual_return           NUMERIC(8, 5),                       -- rendement réel constaté
    benchmark_return        NUMERIC(8, 5),                       -- rendement du benchmark sur la même période
    outperformed            BOOLEAN,                             -- a-t-il surperformé ? NULL tant que non résolu
    prediction_error        NUMERIC(8, 5),                       -- actual_return - expected_return
    resolved_at             TIMESTAMPTZ                          -- quand on a rempli les champs actual_*
);

COMMENT ON TABLE predictions IS 'Prediction Ledger immuable. Les colonnes actual_* et resolved_at sont NULL à la création, remplies après target_date.';
COMMENT ON COLUMN predictions.target_date IS 'Stocké explicitement (redondant avec prediction_date + horizon_days) pour simplifier les requêtes de résolution.';
COMMENT ON COLUMN predictions.features_snapshot IS 'Snapshot optionnel des features d entrée du modèle au moment de la prédiction, pour audit et débogage.';

CREATE INDEX idx_pred_asset_date      ON predictions(asset_id, prediction_date DESC);
CREATE INDEX idx_pred_target_resolve  ON predictions(target_date, resolved_at)
    WHERE resolved_at IS NULL;   -- index partiel : seulement les prédictions non encore résolues
CREATE INDEX idx_pred_model_run       ON predictions(model_run_id);
CREATE UNIQUE INDEX idx_pred_unique   ON predictions(asset_id, prediction_date, horizon_days, model_run_id);
-- L'index unique garantit qu'un run ne produit pas deux prédictions identiques pour le même actif+horizon.


-- -----------------------------------------------------------------------------
-- Table : features_daily
-- Indicateurs techniques dérivés de price_snapshots (un calcul déterministe,
-- pas une observation brute) : contrairement aux tables précédentes, cette
-- table n'est PAS append-only. Un recalcul écrase la ligne existante via
-- UPSERT sur (asset_id, trade_date, feature_version). feature_version permet
-- de faire cohabiter plusieurs versions de la logique de calcul si elle évolue
-- (ex: passer d'un RSI à lissage de Wilder à un autre lissage) sans perdre
-- l'historique des anciennes valeurs.
-- -----------------------------------------------------------------------------
CREATE TABLE features_daily (
    id                  BIGSERIAL PRIMARY KEY,
    asset_id            INTEGER     NOT NULL REFERENCES assets(id),
    trade_date          DATE        NOT NULL,

    return_1d           NUMERIC(8, 5),               -- rendement 1 jour (ex: 0.03500 = +3.5%)
    return_5d           NUMERIC(8, 5),
    return_20d          NUMERIC(8, 5),
    volatility_20d      NUMERIC(8, 5),                -- écart-type des rendements journaliers sur 20j

    sma_20              NUMERIC(12, 4),
    sma_50              NUMERIC(12, 4),
    rsi_14              NUMERIC(6, 3)
                        CHECK (rsi_14 BETWEEN 0 AND 100),

    volume_avg_20d      NUMERIC(16, 4),
    volume_ratio        NUMERIC(10, 4),               -- volume du jour / volume_avg_20d, ex: 1.3500 = +35% vs moyenne

    feature_version     TEXT        NOT NULL DEFAULT 'v1',
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE features_daily IS 'Indicateurs techniques dérivés de price_snapshots. Recalculable : UPSERT sur (asset_id, trade_date, feature_version), à la différence des tables append-only en amont.';
COMMENT ON COLUMN features_daily.computed_at IS 'Horodatage du (re)calcul, mis à jour à chaque UPSERT.';

CREATE UNIQUE INDEX idx_features_unique     ON features_daily(asset_id, trade_date, feature_version);
CREATE INDEX idx_features_asset_date        ON features_daily(asset_id, trade_date DESC);


-- -----------------------------------------------------------------------------
-- Table : predictions_aggregated
-- Score Probastock : moyenne des 3 providers (DeepSeek/Gemini/Claude) pour un
-- meme (asset, prediction_date). Une ligne par jour et par actif, recalculee
-- (UPSERT) si les 3 providers sont relances le meme jour -- a la difference de
-- predictions qui reste un ledger immuable par provider.
-- -----------------------------------------------------------------------------
CREATE TABLE predictions_aggregated (
    id                       BIGSERIAL PRIMARY KEY,
    asset_id                 INTEGER     NOT NULL REFERENCES assets(id),
    prediction_date          DATE        NOT NULL,

    score_agrege             NUMERIC(5, 2)  CHECK (score_agrege BETWEEN 0 AND 100),
    probability_20d_agregee  NUMERIC(5, 4)  CHECK (probability_20d_agregee BETWEEN 0 AND 1),
    confidence_agregee       NUMERIC(4, 3)  CHECK (confidence_agregee BETWEEN 0 AND 1),
    risk_agrege              NUMERIC(4, 3)  CHECK (risk_agrege BETWEEN 0 AND 1),
    horizons_agreges         JSONB,                  -- {probability_1d, probability_5d, probability_20d, probability_60d}, moyenne par horizon

    -- Conviction = mesure inverse de la dispersion entre les 3 scores providers,
    -- distinct de predictions.conviction_score (qui est la confidence auto-declaree
    -- d'un seul provider). Formule dans app/aggregation.py.
    conviction               NUMERIC(4, 3)  CHECK (conviction BETWEEN 0 AND 1),

    recommendation_finale    TEXT        CHECK (recommendation_finale IN ('BUY', 'HOLD', 'REDUCE', 'SELL')),
    provider_count           INTEGER     NOT NULL,     -- combien des 3 providers ont contribue (1 a 3)
    source_prediction_ids    BIGINT[]    NOT NULL,      -- predictions.id agregees, pour audit
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE predictions_aggregated IS 'Score Probastock agrege (moyenne des 3 providers) par actif et par jour.';

CREATE UNIQUE INDEX idx_pred_agg_unique ON predictions_aggregated(asset_id, prediction_date);
