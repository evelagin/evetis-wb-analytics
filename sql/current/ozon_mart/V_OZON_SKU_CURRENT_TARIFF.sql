-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_SKU_CURRENT_TARIFF (VIEW)
-- Authoritative Git definition of the CURRENT production object. Not a migration,
-- not a rollback. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Captured verbatim from production INFORMATION_SCHEMA.VIEWS at 2026-09-18T14:14:32Z
-- (main eecde14936d1). Historical source: sql/ozon/stage3_4d3_ozon_mart_agent_contract.sql (parity: TEXT_DIFFERS_SCHEMA_DATA_EQUIVALENT).
-- Internal dependencies: none.
-- Older definition in sql/ozon/stage3_4d2_ozon_mart_forward.sql is SUPERSEDED
-- and must not be applied (it would break dependants; proven in R2 forensic preflight).
-- Live text differs from the historical file but is schema- and data-equivalent
-- (R2A preflight); per owner decision the validated live definition is canonical.
-- The view body below is byte-for-byte the production body: do not reformat it.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`
OPTIONS (description = "Действующий тарифный контракт Ozon на уровне internal_sku из последнего снимка /v5/product/info/prices. Без trailing-подстановок: отсутствующее значение = NULL. Статус подписки Premium приходит из RAW_OZON_SELLER_INFO и имеет возраст; в формулу вклада SKU он не входит по политике. Ставка 52% сверена с официальным XLSX от 28.08.2026, границы логистики - с официальной матрицей маршрутов 20/20. ТОЛЬКО АНАЛИТИКА.")
AS
WITH snap AS (SELECT MAX(snapshot_date) AS d FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`),
csnap AS (SELECT MAX(snapshot_date) AS d FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_CATALOG`),
cat AS (
  SELECT c.offer_id, c.sku AS ozon_sku, c.status_name, c.is_archived,
         c.stock_present, c.description_category_id, c.type_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_CATALOG` c, csnap
  WHERE c.snapshot_date = csnap.d),
p AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`, snap WHERE snapshot_date = snap.d),
m AS (SELECT marketplace_sku, internal_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
      WHERE marketplace = 'OZON' AND is_current),
cogs AS (SELECT internal_sku, product_cogs_rub, cost_basis, cogs_provenance_status
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
         WHERE CURRENT_DATE() BETWEEN effective_from AND COALESCE(effective_to, DATE '9999-12-31')),
obs AS (
  SELECT mp.internal_sku, APPROX_QUANTILES(-f.amount_rub, 2)[OFFSET(1)] AS median_logistics_rub, COUNT(*) AS n_obs
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` po
    ON po.posting_number = f.posting_number AND po.sku = f.sku
  JOIN m mp ON mp.marketplace_sku = f.sku
  WHERE f.type_id = 32 AND po.created_at >= TIMESTAMP('2026-08-28 00:00:00', 'Europe/Moscow')
  GROUP BY 1),
si AS (
  SELECT TRUE AS si_exists, is_premium, premium, premium_plus, subscription_type, tax_system, retrieved_at,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), retrieved_at, HOUR) AS age_hours
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SELLER_INFO`
  QUALIFY ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) = 1),
prem AS (SELECT MAX(event_date) AS last_premium_charge_date
         FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` WHERE type_id = 52)
SELECT
  mp.internal_sku, cat.offer_id, cat.ozon_sku,
  pm.canonical_product_name AS product_name,
  IF(pm.is_bundle, 'BUNDLE', 'SINGLE') AS product_type,
  cat.status_name AS catalog_status, cat.stock_present AS fbo_stock,
  cat.description_category_id, cat.type_id,
  p.snapshot_ts AS snapshot_at, p.snapshot_date AS source_snapshot_at,
  DATE '2026-08-28' AS tariff_effective_at,
  p.marketing_seller_price_rub AS seller_base_price,
  p.price_rub AS list_price_rub, p.min_price_rub, p.old_price_rub, p.vat_rate,
  cg.product_cogs_rub AS current_management_cogs_rub,
  cg.cost_basis AS cogs_basis, cg.cogs_provenance_status AS cogs_provenance,
  p.sales_percent_fbo AS commission_fbo_pct,
  ROUND(p.marketing_seller_price_rub * p.sales_percent_fbo / 100, 2) AS commission_fbo_rub_at_current_price,
  p.sales_percent_fbs AS commission_fbs_pct,
  ROUND(p.marketing_seller_price_rub * p.sales_percent_fbs / 100, 2) AS commission_fbs_rub_at_current_price,
  p.sales_percent_rfbs AS commission_rfbs_pct, p.sales_percent_fbp AS commission_fbp_pct,
  'SELLER_BASE_PRICE' AS commission_base,
  p.acquiring_rub AS acquiring_max_rub,
  ROUND(SAFE_DIVIDE(p.acquiring_rub, p.marketing_seller_price_rub) * 100, 4) AS acquiring_pct,
  p.volume_weight_l,
  p.fbo_direct_flow_trans_min_rub AS fbo_logistics_min_rub,
  p.fbo_direct_flow_trans_max_rub AS fbo_logistics_max_rub,
  ROUND(LEAST(GREATEST(
      IF(ob.n_obs >= 2, ob.median_logistics_rub, p.fbo_direct_flow_trans_min_rub * NUMERIC '1.20'),
      p.fbo_direct_flow_trans_min_rub), p.fbo_direct_flow_trans_max_rub), 2) AS fbo_logistics_expected_rub,
  IF(ob.n_obs >= 2, 'MODELLED_EXPECTED_OBSERVED_MEDIAN_POST_20260828',
                    'MODELLED_EXPECTED_API_MIN_x1.20_INFERRED') AS fbo_logistics_expected_basis,
  IFNULL(ob.n_obs, 0) AS fbo_logistics_observations,
  p.fbs_direct_flow_trans_min_rub AS fbs_logistics_min_rub,
  p.fbs_direct_flow_trans_max_rub AS fbs_logistics_max_rub,
  p.fbs_first_mile_min_rub AS fbs_first_mile_sc_rub,
  p.fbs_first_mile_max_rub AS fbs_first_mile_pvz_rub,
  CAST(NULL AS NUMERIC) AS fbs_first_mile_courier_rub,
  p.fbo_deliv_to_customer_rub AS last_mile_rub,
  NUMERIC '15' AS return_processing_rub,
  p.fbo_return_flow_rub AS fbo_return_logistics_rub,
  p.fbs_return_flow_rub AS fbs_return_logistics_rub,
  'TARIFF_PROVEN_PROBABILITY_NOT_PROVEN' AS return_tariff_status,
  NUMERIC '2.50' AS forced_storage_rub_per_liter_per_day,
  99 AS storage_free_days,
  'EXCLUDED_FROM_ORDER_ECONOMICS_INVENTORY_HOLDING' AS storage_applicability,
  si.is_premium AS premium_active,
  si.premium_plus AS premium_plus_active,
  si.subscription_type AS premium_subscription_type,
  si.retrieved_at AS premium_status_source_at,
  si.age_hours AS premium_status_age_hours,
  CASE
    WHEN si.si_exists IS NOT TRUE THEN 'UNKNOWN_NEVER_INGESTED'
    WHEN si.age_hours > 30 THEN 'UNKNOWN_STALE'
    WHEN si.premium_plus THEN 'PREMIUM_PLUS_ACTIVE'
    WHEN si.is_premium OR si.premium THEN 'ACTIVE'
    ELSE 'INACTIVE' END AS premium_status,
  'PROVEN_CURRENT_API_INGESTED_DAILY' AS premium_status_basis,
  NUMERIC '9990' AS premium_tariff_rub_per_month,
  FALSE AS premium_affects_sku_contribution,
  'STORE_LEVEL_FIXED_COST_NOT_ALLOCATED_TO_SKU (OZON_PNL_POLICY_V1 запрет 5)' AS premium_allocation_rule,
  prem.last_premium_charge_date,
  si.tax_system AS seller_tax_system,
  'PROVEN_OFFICIAL_DOCUMENTATION_XLSX_28082026 + PROVEN_CURRENT_API + REALIZED' AS proof_status_commission,
  'PROVEN_CURRENT_API_BOUNDS_MATCH_OFFICIAL_ROUTE_MATRIX_20_OF_20; EXPECTED_IS_MODELLED' AS proof_status_logistics,
  'PROVEN_CURRENT_API_MAX; OFFICIAL_2.20_RATE_SET_BY_BANK' AS proof_status_acquiring,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.3.2_25082026_MATCHES_API; COURIER_HANDOVER_NOT_PROVEN' AS proof_status_fbs_first_mile,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.7' AS proof_status_last_mile,
  'PROVEN_OFFICIAL_DOCUMENTATION_2.1.3_RATE_AND_TERMS' AS proof_status_storage,
  'PROVEN_CURRENT_API_DAILY_INGESTION' AS proof_status_premium,
  p.commissions_field_count, p.commissions_unknown_fields,
  'ANALYTICAL_ONLY_NO_PRICE_WRITES' AS usage_note,
  CURRENT_TIMESTAMP() AS mart_computed_at
FROM p
JOIN cat USING (offer_id)
JOIN m mp ON mp.marketplace_sku = cat.ozon_sku
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm ON pm.internal_sku = mp.internal_sku
LEFT JOIN cogs cg ON cg.internal_sku = mp.internal_sku
LEFT JOIN obs ob ON ob.internal_sku = mp.internal_sku
LEFT JOIN si ON TRUE
LEFT JOIN prem ON TRUE
WHERE NOT cat.is_archived;
