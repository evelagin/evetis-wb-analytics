-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_SKU_STATE_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Нейтральный слой: читает только wb_mart и ozon_mart (политика изоляции площадок).
-- Общего идентификатора акции между площадками нет и не создаётся: ключ всегда
-- (marketplace, source_promotion_id). Совпадение названий или дат НЕ означает одну акцию.
--
-- Грейн: marketplace × source_promotion_id × marketplace_product_id × observation_id.
-- Разрешённое состояние товара в акции. Правила разрешения — в представлениях площадок
-- (V_WB_PROMO_SKU_STATE_HISTORY, V_OZON_PROMO_SKU_STATE_HISTORY), здесь только общая форма.
-- NULL во флагах списков = не знаем / не применимо, НЕ «нет».
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY`
OPTIONS (description = "PR-PROMO-2. Разрешённое состояние товара в акции обеих площадок по годным снимкам: одна строка на marketplace × source_promotion_id × marketplace_product_id × observation_id. resolved_state с классом доказательности и правилом разрешения; оси participation_state и auto_add_state сохранены раздельно. NULL во флагах — не знаем или не применимо, не «нет». Строк WB-автоакций нет: их состав не наблюдаем.")
AS
SELECT
  marketplace,
  CAST(promotion_id AS STRING) AS source_promotion_id,
  observation_id,
  observation_slot,
  observed_at,
  history_class,
  promotion_type,
  promotion_lifecycle_status,
  CAST(nm_id AS STRING) AS marketplace_product_id,
  CAST(NULL AS STRING) AS offer_id,
  internal_sku,
  mapping_status,
  participation_state,
  participation_evidence_class,
  auto_add_state,
  auto_add_evidence_class,
  resolved_state,
  resolved_state_evidence_class,
  resolution_rule,
  in_participants_list,
  in_candidates_list,
  in_auto_add_scheduled_list,
  in_auto_add_eligible_list,
  evidence_conflict,
  CAST(NULL AS STRING) AS participation_add_mode,
  CAST(NULL AS TIMESTAMP) AS next_scheduled_auto_add_at,
  CAST(NULL AS BOOL) AS marketing_actions_corroborated
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY`
UNION ALL
SELECT
  marketplace,
  CAST(action_id AS STRING),
  observation_id,
  observation_slot,
  observed_at,
  history_class,
  promotion_type,
  promotion_lifecycle_status,
  CAST(product_id AS STRING),
  offer_id,
  internal_sku,
  mapping_status,
  participation_state,
  participation_evidence_class,
  auto_add_state,
  auto_add_evidence_class,
  resolved_state,
  resolved_state_evidence_class,
  resolution_rule,
  in_participants_list,
  in_candidates_list,
  in_auto_add_scheduled_list,
  in_auto_add_eligible_list,
  evidence_conflict,
  participation_add_mode,
  next_scheduled_auto_add_at,
  marketing_actions_corroborated
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`
