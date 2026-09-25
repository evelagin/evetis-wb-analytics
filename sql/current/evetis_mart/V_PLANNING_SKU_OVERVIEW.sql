-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PLANNING_SKU_OVERVIEW (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Одна строка на физический SKU для экрана владельца. Четыре смысла разведены по колонкам:
--   OBSERVED  — запас, свежесть, наблюдаемый темп 30/90 дней (V_SKU_SELL_THROUGH_CURRENT);
--   REQUIRED  — дата sell-by и требуемый темп к ней (цель с происхождением, PR-PROMO-4);
--   PROPOSED  — последняя предложенная/владельческая версия (не отозванная): итог горизонта,
--               остаток в конце, первая нехватка, остаток к sell-by;
--   APPROVED  — бизнес-траектория по утверждённому плану (или статус, почему её нет).
-- Рядом — модельный сценарий Control Tower (MODEL_SCENARIO) и поступления: сколько входит в
-- траекторию, сколько видно, но исключено, и дата модели Control Tower (только для сравнения).
--
-- Грейн: internal_sku.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_SKU_OVERVIEW`
OPTIONS (description = "PR-PLAN-1. Экран планирования по физическому SKU: наблюдаемое (запас, свежесть, темп 30/90), требуемое (sell-by, требуемый темп), предложенное (последняя версия: итог, остаток в конце, первая нехватка) и утверждённое (статус бизнес-траектории), модельный сценарий CT и поступления с причиной исключения.")
AS
WITH s AS (
  SELECT internal_sku, product_name, product_line, inventory_as_of_date, inventory_freshness_status, inventory_freshness_reason,
    inventory_position_units, warehouse_ff_units, marketplace_available_units, in_transit_units, ff_anchor_date,
    units_per_day_30d, units_per_day_90d, velocity_quality_30d, velocity_quality_90d, sales_as_of
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
t AS (
  SELECT internal_sku,
    ANY_VALUE(sell_by_date) AS sell_by_date,
    ANY_VALUE(expiry_date) AS expiry_date,
    ANY_VALUE(required_units_per_day_to_sell_by) AS required_units_per_day_to_sell_by,
    ARRAY_AGG(STRUCT(trajectory_basis, plan_version, plan_kind, version_lifecycle_status, plan_created_at, month,
                     trajectory_status, planned_physical_units, eligible_inbound_units, closing_units, shortfall_units,
                     projected_units_at_sell_by)) AS r
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_TRAJECTORY_MONTHLY`
  GROUP BY internal_sku
),
t1 AS (
  SELECT t.*,
    (SELECT x.plan_version FROM UNNEST(t.r) x
     WHERE x.trajectory_basis = 'VERSION_SCENARIO' AND x.plan_kind IN ('SYSTEM_PROPOSED', 'OWNER_AUTHORED')
       AND x.version_lifecycle_status NOT IN ('WITHDRAWN', 'INTEGRITY_BROKEN')
     ORDER BY x.plan_created_at DESC, x.plan_version DESC LIMIT 1) AS proposal_version,
    (SELECT x.plan_version FROM UNNEST(t.r) x
     WHERE x.trajectory_basis = 'VERSION_SCENARIO' AND x.plan_kind = 'MODEL_SCENARIO'
     ORDER BY x.plan_created_at DESC, x.plan_version DESC LIMIT 1) AS model_version
  FROM t
),
t2 AS (
  SELECT t1.internal_sku, t1.sell_by_date, t1.expiry_date, t1.required_units_per_day_to_sell_by,
    ARRAY(
      SELECT AS STRUCT
        k.role,
        ANY_VALUE(x.plan_version) AS plan_version,
        ANY_VALUE(x.version_lifecycle_status) AS version_lifecycle_status,
        ARRAY_AGG(x.trajectory_status ORDER BY x.month LIMIT 1)[OFFSET(0)] AS status_now,
        ARRAY_AGG(x.planned_physical_units ORDER BY x.month LIMIT 1)[OFFSET(0)] AS planned_current_month,
        SUM(x.planned_physical_units) AS planned_horizon,
        SUM(x.eligible_inbound_units) AS inbound_horizon,
        ARRAY_AGG(STRUCT(x.month, x.closing_units) ORDER BY x.month DESC LIMIT 1)[OFFSET(0)] AS last,
        MIN(IF(x.shortfall_units > 0, x.month, NULL)) AS first_shortfall_month,
        MAX(x.shortfall_units) AS max_shortfall_units,
        MAX(x.projected_units_at_sell_by) AS projected_units_at_sell_by
      FROM UNNEST(t1.r) x
      JOIN UNNEST([STRUCT('APPROVED' AS role, 'APPROVED_PLAN' AS basis, 'APPROVED' AS v),
                   STRUCT('PROPOSED', 'VERSION_SCENARIO', t1.proposal_version),
                   STRUCT('MODEL', 'VERSION_SCENARIO', t1.model_version)]) k
        ON x.trajectory_basis = k.basis AND x.plan_version = k.v
      GROUP BY k.role
    ) AS sm
  FROM t1
),
l AS (
  SELECT internal_sku,
    SUM(IF(in_base_trajectory, trajectory_units, 0)) AS inbound_in_trajectory_units,
    SUM(committed_units_not_in_trajectory) AS inbound_committed_not_in_trajectory_units,
    SUM(IF(inclusion_status = 'EXCLUDED_HYPOTHETICAL', quantity, 0)) AS inbound_hypothetical_units,
    STRING_AGG(CONCAT(inbound_id, ' ', CAST(quantity AS STRING), ' ', lot_state, IFNULL(CONCAT('/', blocker), ''),
                      ' → ', inclusion_status), '; ' ORDER BY inbound_id) AS inbound_lots,
    ANY_VALUE(ct_model_inbound_units) AS ct_model_inbound_units,
    ANY_VALUE(ct_model_inbound_eta) AS ct_model_inbound_eta
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT`
  GROUP BY internal_sku
),
j AS (
  SELECT s.*, t2.sell_by_date, t2.expiry_date, t2.required_units_per_day_to_sell_by,
    (SELECT AS STRUCT x.* FROM UNNEST(t2.sm) x WHERE x.role = 'APPROVED') AS a,
    (SELECT AS STRUCT x.* FROM UNNEST(t2.sm) x WHERE x.role = 'PROPOSED') AS p,
    (SELECT AS STRUCT x.* FROM UNNEST(t2.sm) x WHERE x.role = 'MODEL') AS m,
    l.* EXCEPT (internal_sku)
  FROM s
  LEFT JOIN t2 USING (internal_sku)
  LEFT JOIN l USING (internal_sku)
)
SELECT
  internal_sku,
  product_name,
  product_line,
  -- OBSERVED
  inventory_as_of_date,
  inventory_freshness_status,
  inventory_freshness_reason,
  ff_anchor_date,
  inventory_position_units,
  warehouse_ff_units,
  marketplace_available_units,
  in_transit_units,
  sales_as_of,
  units_per_day_30d AS observed_units_per_day_30d,
  velocity_quality_30d AS observed_velocity_quality_30d,
  units_per_day_90d AS observed_units_per_day_90d,
  velocity_quality_90d AS observed_velocity_quality_90d,
  -- REQUIRED
  sell_by_date,
  expiry_date,
  required_units_per_day_to_sell_by,
  -- PROPOSED
  p.plan_version AS proposed_plan_version,
  p.version_lifecycle_status AS proposed_lifecycle_status,
  p.status_now AS proposed_trajectory_status,
  p.planned_current_month AS proposed_units_current_month_remaining,
  p.planned_horizon AS proposed_units_horizon,
  p.last.month AS proposed_horizon_last_month,
  p.last.closing_units AS proposed_closing_units_at_horizon_end,
  p.first_shortfall_month AS proposed_first_shortfall_month,
  p.max_shortfall_units AS proposed_max_shortfall_units,
  p.projected_units_at_sell_by AS proposed_units_left_at_sell_by,
  -- APPROVED
  a.status_now AS approved_trajectory_status,
  a.planned_horizon AS approved_units_horizon,
  a.last.closing_units AS approved_closing_units_at_horizon_end,
  a.first_shortfall_month AS approved_first_shortfall_month,
  a.max_shortfall_units AS approved_max_shortfall_units,
  -- MODEL (Control Tower, не план)
  m.plan_version AS model_plan_version,
  m.planned_horizon AS model_units_horizon,
  m.first_shortfall_month AS model_first_shortfall_month,
  m.max_shortfall_units AS model_max_shortfall_units,
  -- ПОСТУПЛЕНИЯ
  IFNULL(inbound_in_trajectory_units, 0) AS inbound_in_trajectory_units,
  IFNULL(inbound_committed_not_in_trajectory_units, 0) AS inbound_committed_not_in_trajectory_units,
  IFNULL(inbound_hypothetical_units, 0) AS inbound_hypothetical_units,
  inbound_lots,
  ct_model_inbound_units,
  ct_model_inbound_eta
FROM j
