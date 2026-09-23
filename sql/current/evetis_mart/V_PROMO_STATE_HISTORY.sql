-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PROMO_STATE_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Нейтральный слой: читает только wb_mart и ozon_mart (политика изоляции площадок).
-- Общего идентификатора акции между площадками нет и не создаётся: ключ всегда
-- (marketplace, source_promotion_id). Совпадение названий или дат НЕ означает одну акцию.
--
-- Грейн: marketplace × source_promotion_id × observation_id. Нормализованное состояние
-- акции в каждом годном снимке. Специфичные поля площадок (лестница бустинга WB, даты
-- автодобавления и скидка Ozon) остаются в V_WB_PROMO_* / V_OZON_PROMO_*: их смысл не
-- совпадает, и сводить их в общий столбец значило бы утверждать эквивалентность.
-- Счётчики источника переданы как есть, с явным source_counter_basis: это разные метрики
-- разных площадок, складывать их между площадками нельзя.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`
OPTIONS (description = "PR-PROMO-2. Каноническая история состояния акций обеих площадок: одна строка на marketplace × source_promotion_id × observation_id. Жизненный цикл выведен из дат источника относительно observed_at снимка. sku_membership_observability говорит, наблюдаем ли состав акции по SKU (WB-автоакции: NOT_OBSERVABLE). Счётчики — метрики источника с source_counter_basis, между площадками не сопоставимы. Без экономики и рекомендаций.")
AS
SELECT
  marketplace,
  CAST(promotion_id AS STRING) AS source_promotion_id,
  observation_id,
  observation_slot,
  observed_at,
  history_class,
  observation_status,
  promotion_name,
  promotion_type,
  starts_at,
  ends_at,
  starts_on_msk,
  ends_on_msk,
  lifecycle_status,
  lifecycle_evidence_class,
  seller_participation_state,
  seller_participation_evidence_class,
  aggregate_state,
  in_promo_total AS source_in_promo_count,
  not_in_promo_total AS source_not_in_promo_count,
  'WB /calendar/promotions/details: inPromoActionTotal, notInPromoActionTotal' AS source_counter_basis,
  sku_membership_observability,
  sku_membership_observability_reason
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`
UNION ALL
SELECT
  marketplace,
  CAST(action_id AS STRING),
  observation_id,
  observation_slot,
  observed_at,
  history_class,
  observation_status,
  promotion_name,
  promotion_type,
  starts_at,
  ends_at,
  starts_on_msk,
  ends_on_msk,
  lifecycle_status,
  lifecycle_evidence_class,
  seller_participation_state,
  seller_participation_evidence_class,
  aggregate_state,
  participating_products_count,
  potential_products_count,
  'OZON /v1/actions: participating_products_count, potential_products_count',
  sku_membership_observability,
  sku_membership_observability_reason
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`
