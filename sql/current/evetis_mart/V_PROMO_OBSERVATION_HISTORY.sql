-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_OBSERVATION_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Нейтральный слой: читает только wb_mart и ozon_mart (политика изоляции площадок).
-- Общего идентификатора акции между площадками нет и не создаётся: ключ всегда
-- (marketplace, source_promotion_id). Совпадение названий или дат НЕ означает одну акцию.
--
-- Грейн: marketplace × observation_id. Годные снимки обеих площадок.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`
OPTIONS (description = "PR-PROMO-2. Годные снимки состояния акций обеих площадок: одна строка на marketplace × observation_id. observed_at — время данных. Основа выбора текущего состояния: текущее = последний годный снимок площадки, а не последняя строка сущности.")
AS
SELECT marketplace, observation_id, observation_slot, observed_at, observed_at_basis, manifest_observed_at,
  history_class, observation_status, promotions_observed, catalog_products_observed
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`
UNION ALL
SELECT marketplace, observation_id, observation_slot, observed_at, observed_at_basis, manifest_observed_at,
  history_class, observation_status, promotions_observed, catalog_products_observed
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`
