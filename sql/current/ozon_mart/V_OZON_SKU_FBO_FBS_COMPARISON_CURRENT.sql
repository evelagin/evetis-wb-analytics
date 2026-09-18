-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4d2_ozon_mart_forward.sql (parity: COMMENTS_WHITESPACE_ONLY).
-- Internal dependencies: V_OZON_SKU_CURRENT_TARIFF, V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT`
OPTIONS (description = "Сравнение FBO и FBS по действующему тарифу на уровне одного заказа. Две модели отгрузки FBS: СЦ (обработка 0 руб) и ПВЗ/ППЗ (10 руб); курьерская отгрузка NULL - тариф партнёра виден только в кабинете. fbs_status = PARTIAL: реализованных FBS-заказов нет, склад не настроен. Рекомендация схемы PROVISIONAL.")
AS
WITH t AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`),
f AS (SELECT internal_sku, contribution_expected, contribution_worst_case
      FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`),
b AS (
  SELECT t.*, f.contribution_expected AS fbo_contribution_expected,
         f.contribution_worst_case AS fbo_contribution_worst,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_expected_rub - t.last_mile_rub - t.fbs_first_mile_sc_rub, 2) AS fbs_sc_expected,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_max_rub - t.last_mile_rub - t.fbs_first_mile_sc_rub, 2) AS fbs_sc_worst,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_expected_rub - t.last_mile_rub - t.fbs_first_mile_pvz_rub, 2) AS fbs_pvz_expected,
         ROUND(t.seller_base_price - t.current_management_cogs_rub
               - t.seller_base_price * t.commission_fbs_pct / 100 - t.acquiring_max_rub
               - t.fbo_logistics_max_rub - t.last_mile_rub - t.fbs_first_mile_pvz_rub, 2) AS fbs_pvz_worst
  FROM t JOIN f USING (internal_sku))
SELECT
  b.internal_sku, b.product_name, b.product_type, b.seller_base_price,
  b.commission_fbo_pct, b.commission_fbs_pct,
  b.fbo_contribution_expected, b.fbo_contribution_worst,
  b.fbs_first_mile_sc_rub, b.fbs_first_mile_pvz_rub, b.fbs_first_mile_courier_rub,
  b.fbs_sc_expected AS fbs_contribution_sc_expected,
  b.fbs_sc_worst AS fbs_contribution_sc_worst,
  b.fbs_pvz_expected AS fbs_contribution_pvz_expected,
  b.fbs_pvz_worst AS fbs_contribution_pvz_worst,
  ROUND(b.fbs_sc_expected  - b.fbo_contribution_expected, 2) AS fbs_max_external_ff_to_match_fbo_sc,
  ROUND(b.fbs_pvz_expected - b.fbo_contribution_expected, 2) AS fbs_max_external_ff_to_match_fbo_pvz,
  ROUND(b.seller_base_price * NUMERIC '0.03', 2) AS fbs_early_shipment_discount_max_rub,
  ROUND(b.fbs_sc_expected - b.fbo_contribution_expected + b.seller_base_price * NUMERIC '0.03', 2) AS fbs_max_external_ff_to_match_fbo_sc_with_early_discount,
  CAST(NULL AS NUMERIC) AS external_fbs_fulfillment_cost_rub,
  ROUND(b.fbs_pvz_expected - 50,  2) AS fbs_contribution_ff_50,
  ROUND(b.fbs_pvz_expected - 75,  2) AS fbs_contribution_ff_75,
  ROUND(b.fbs_pvz_expected - 100, 2) AS fbs_contribution_ff_100,
  ROUND(b.fbs_pvz_expected - 125, 2) AS fbs_contribution_ff_125,
  ROUND(b.fbs_pvz_expected - 150, 2) AS fbs_contribution_ff_150,
  IF(b.fbs_pvz_expected - 50  > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_50,
  IF(b.fbs_pvz_expected - 75  > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_75,
  IF(b.fbs_pvz_expected - 100 > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_100,
  IF(b.fbs_pvz_expected - 125 > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_125,
  IF(b.fbs_pvz_expected - 150 > b.fbo_contribution_expected, 'FBS', 'FBO') AS preferred_scheme_at_ff_150,
  'PARTIAL' AS fbs_forward_economics_ready,
  'PROVISIONAL' AS fbs_scheme_recommendation,
  'NO_REALIZED_FBS_ORDERS_EVER; FBS_WAREHOUSE_NOT_CONFIGURED; COURIER_HANDOVER_TARIFF_NOT_PROVEN' AS fbs_blockers,
  b.proof_status_fbs_first_mile,
  'ANALYTICAL_ONLY_NO_SCHEME_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM b ORDER BY b.internal_sku;
