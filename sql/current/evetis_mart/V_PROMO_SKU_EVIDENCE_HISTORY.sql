-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Нейтральный слой: читает только wb_mart и ozon_mart (политика изоляции площадок).
-- Общего идентификатора акции между площадками нет и не создаётся: ключ всегда
-- (marketplace, source_promotion_id). Совпадение названий или дат НЕ означает одну акцию.
--
-- Грейн: marketplace × observation_id × evidence_key. Все свидетельства уровня товара обеих
-- площадок без разрешения. marketplace_product_id — идентификатор товара в источнике
-- акций: WB nm_id, Ozon product_id. offer_id есть только у Ozon (у WB источник его не отдаёт).
-- Строк WB для автоакций нет: состав источник не отдаёт (NOT_OBSERVABLE на уровне акции).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`
OPTIONS (description = "PR-PROMO-2. Все свидетельства участия товаров в акциях обеих площадок по годным снимкам: одна строка на marketplace × observation_id × evidence_key, без разрешения противоречий. marketplace_product_id — WB nm_id или Ozon product_id. promotion_link_method показывает, как установлена связь с акцией (DIRECT_ID, DETERMINISTIC_EXACT_MATCH, AMBIGUOUS, UNRESOLVED). Несопоставленные товары не отбрасываются.")
AS
SELECT
  marketplace,
  observation_id,
  observation_slot,
  observed_at,
  history_class,
  evidence_source,
  source_endpoint,
  source_state,
  evidence_class,
  CAST(promotion_id AS STRING) AS source_promotion_id,
  promotion_link_method,
  CAST(NULL AS STRING) AS link_candidate_promotion_ids,
  CAST(nm_id AS STRING) AS marketplace_product_id,
  CAST(NULL AS STRING) AS offer_id,
  internal_sku,
  mapping_status,
  CAST(NULL AS TIMESTAMP) AS auto_add_at,
  CAST(NULL AS STRING) AS add_mode,
  CAST(NULL AS STRING) AS source_action_title,
  evidence_key
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`
UNION ALL
SELECT
  marketplace,
  observation_id,
  observation_slot,
  observed_at,
  history_class,
  evidence_source,
  source_endpoint,
  source_state,
  evidence_class,
  CAST(action_id AS STRING),
  promotion_link_method,
  link_candidate_action_ids,
  CAST(product_id AS STRING),
  offer_id,
  internal_sku,
  mapping_status,
  auto_add_at,
  add_mode,
  source_action_title,
  evidence_key
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`
