-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_PLANNING_EXCEPTIONS (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Исключения экрана планирования. Каждое — факт с числом и происхождением, без решения:
--   INVENTORY_STALE        запас не FRESH: утверждённая траектория не считается (BLOCKER)
--   NO_APPROVED_PLAN       текущий месяц не покрыт утверждённой версией (BLOCKER)
--   PLAN_INTEGRITY         пересчёт хеша ≠ шапке или есть недействующие события (BLOCKER)
--   BOM_BASIS_MISSING      набор в плане без снимка BOM версии — не раскладывается (BLOCKER)
--   COMPONENT_SHORTFALL    остаток компонента уходит ниже нуля; первый месяц, максимум, разбивка
--                          одиночные / в наборах и список наборов (BLOCKER для утверждённого
--                          плана, WARNING для сценария). План не урезается.
--   EXPIRY_PRESSURE        к дате «срок − буфер C1» по плану версии остаётся запас (WARNING)
--   INBOUND_ETA_UNKNOWN    партия заказана/произведена, но даты нет или она оценочная (WARNING)
--   INBOUND_BLOCKED        у партии блокер, например AWAITING_PAYMENT (WARNING)
--   INBOUND_ETA_OVERDUE    подтверждённая дата прошла, приёмки нет (WARNING)
--   INBOUND_HYPOTHETICAL   партия только планируется — в траекторию не входит (INFO)
--
-- Грейн: exception_code × trajectory_basis × plan_version × internal_sku (или inbound_id).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_EXCEPTIONS`
OPTIONS (description = "PR-PLAN-1. Исключения планирования с числами и происхождением: несвежий запас, нет утверждённого плана, целостность версий, наборы без BOM, нехватка компонента (разбивка по наборам), давление срока годности, поступления без подтверждённой даты, с блокером, просроченные и гипотетические. Решений не принимает.")
AS
WITH t AS (
  SELECT trajectory_basis, plan_version, plan_kind, version_lifecycle_status, internal_sku, product_name,
    ANY_VALUE(inventory_freshness_status) AS inventory_freshness_status,
    ANY_VALUE(inventory_freshness_reason) AS inventory_freshness_reason,
    ANY_VALUE(inventory_as_of_date) AS inventory_as_of_date,
    ARRAY_AGG(STRUCT(month, trajectory_status, plan_source_version, closing_units, shortfall_units, planned_standalone_units, planned_via_bundle_units,
                     planned_bundle_breakdown, projected_units_at_sell_by, sell_by_date) ORDER BY month) AS m
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_TRAJECTORY_MONTHLY`
  GROUP BY trajectory_basis, plan_version, plan_kind, version_lifecycle_status, internal_sku, product_name
),
tx AS (
  SELECT t.*,
    (SELECT AS STRUCT x.* FROM UNNEST(t.m) x WHERE x.shortfall_units > 0 ORDER BY x.month LIMIT 1) AS first_short,
    (SELECT MAX(x.shortfall_units) FROM UNNEST(t.m) x) AS max_short,
    (SELECT AS STRUCT x.* FROM UNNEST(t.m) x WHERE x.projected_units_at_sell_by IS NOT NULL LIMIT 1) AS at_sell_by,
    t.m[OFFSET(0)].plan_source_version IS NULL AS no_plan_now,
    ROW_NUMBER() OVER (PARTITION BY t.trajectory_basis ORDER BY t.internal_sku) = 1 AS first_sku_of_basis
  FROM t
),
tx_ex AS (
  -- Все исключения траектории — из ОДНОГО чтения (представление раскрывается на каждую ссылку).
  SELECT e.*
  FROM tx,
  UNNEST(ARRAY_CONCAT(
    IF(tx.trajectory_basis = 'APPROVED_PLAN' AND tx.inventory_freshness_status != 'FRESH',
       [STRUCT('INVENTORY_STALE' AS exception_code,
               'BLOCKER' AS severity,
               tx.trajectory_basis AS trajectory_basis,
               CAST(NULL AS STRING) AS plan_version,
               CAST(NULL AS STRING) AS plan_kind,
               tx.internal_sku AS internal_sku,
               tx.product_name AS product_name,
               CAST(NULL AS STRING) AS inbound_id,
               DATE_TRUNC(tx.inventory_as_of_date, MONTH) AS month,
               CAST(NULL AS FLOAT64) AS units,
               tx.inventory_freshness_reason AS detail,
               'V_SKU_SELL_THROUGH_CURRENT (свежесть по срокам Control Tower)' AS evidence)], []),
    IF(tx.trajectory_basis = 'APPROVED_PLAN' AND tx.no_plan_now AND tx.first_sku_of_basis,
       [STRUCT('NO_APPROVED_PLAN' AS exception_code,
               'BLOCKER' AS severity,
               'APPROVED_PLAN' AS trajectory_basis,
               CAST(NULL AS STRING) AS plan_version,
               CAST(NULL AS STRING) AS plan_kind,
               CAST(NULL AS STRING) AS internal_sku,
               CAST(NULL AS STRING) AS product_name,
               CAST(NULL AS STRING) AS inbound_id,
               tx.m[OFFSET(0)].month AS month,
               CAST(NULL AS FLOAT64) AS units,
               'Текущий месяц не покрыт утверждённой версией: бизнес-траектория не строится, видны только сценарии' AS detail,
               'V_PLAN_VERSION_STATUS (окна действия утверждений)' AS evidence)], []),
    IF(tx.first_short IS NOT NULL,
       [STRUCT('COMPONENT_SHORTFALL' AS exception_code,
               IF(tx.trajectory_basis = 'APPROVED_PLAN', 'BLOCKER', 'WARNING') AS severity,
               tx.trajectory_basis AS trajectory_basis,
               tx.plan_version AS plan_version,
               tx.plan_kind AS plan_kind,
               tx.internal_sku AS internal_sku,
               tx.product_name AS product_name,
               CAST(NULL AS STRING) AS inbound_id,
               tx.first_short.month AS month,
               CAST(tx.max_short AS FLOAT64) AS units,
               FORMAT('Первый месяц нехватки %s: остаток %s; максимум нехватки %s. В месяце: одиночные %s, в наборах %s [%s].%s',
                      CAST(tx.first_short.month AS STRING), FORMAT('%.1f', tx.first_short.closing_units), FORMAT('%.1f', tx.max_short),
                      FORMAT('%.1f', IFNULL(tx.first_short.planned_standalone_units, 0)),
                      FORMAT('%.1f', IFNULL(tx.first_short.planned_via_bundle_units, 0)),
                      IFNULL(tx.first_short.planned_bundle_breakdown, '—'),
                      IF(tx.first_short.trajectory_status = 'SCENARIO_ON_STALE_INVENTORY', ' Сценарий на несвежем запасе.', '')) AS detail,
               'V_PLAN_TRAJECTORY_MONTHLY: начало + подтверждённые поступления − план (BOM версии)' AS evidence)], []),
    IF(tx.at_sell_by.projected_units_at_sell_by > 0,
       [STRUCT('EXPIRY_PRESSURE' AS exception_code,
               'WARNING' AS severity,
               tx.trajectory_basis AS trajectory_basis,
               tx.plan_version AS plan_version,
               tx.plan_kind AS plan_kind,
               tx.internal_sku AS internal_sku,
               tx.product_name AS product_name,
               CAST(NULL AS STRING) AS inbound_id,
               DATE_TRUNC(tx.at_sell_by.sell_by_date, MONTH) AS month,
               CAST(tx.at_sell_by.projected_units_at_sell_by AS FLOAT64) AS units,
               FORMAT('К дате sell-by %s по плану остаётся %s ед.%s', CAST(tx.at_sell_by.sell_by_date AS STRING),
                      FORMAT('%.1f', tx.at_sell_by.projected_units_at_sell_by),
                      IF(tx.at_sell_by.trajectory_status = 'SCENARIO_ON_STALE_INVENTORY', ' Сценарий на несвежем запасе.', '')) AS detail,
               'V_PLAN_TRAJECTORY_MONTHLY; sell-by = срок годности − буфер C1 (OPS_CONFIG.c1_expiry_margin_days)' AS evidence)], [])
  )) e
),
st AS (
  SELECT plan_version, plan_kind, lifecycle_status, integrity_ok, events_ignored, ignored_events, content_sha256, recomputed_sha256
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
),
bom_missing AS (
  SELECT plan_version, ANY_VALUE(plan_kind) AS plan_kind, MIN(month) AS first_month,
    STRING_AGG(DISTINCT bundles_without_bom_basis, ', ') AS bundles
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_PHYSICAL_MONTHLY`
  WHERE expansion_status = 'BOM_BASIS_MISSING'
  GROUP BY plan_version
),
lots AS (
  -- Партия может дать несколько исключений сразу: блокер И неподтверждённая дата.
  SELECT e.*
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT` l,
  UNNEST(ARRAY_CONCAT(
    IF(l.blocker IS NOT NULL AND l.lot_state NOT IN ('CANCELLED', 'RECEIVED'), [STRUCT('INBOUND_BLOCKED' AS code, 'WARNING' AS severity)], []),
    IF(l.lot_state IN ('ORDER_CONFIRMED', 'IN_PRODUCTION', 'PRODUCED', 'READY_FOR_SHIPMENT', 'IN_TRANSIT')
       AND (l.eta_status != 'CONFIRMED' OR l.eta_date IS NULL), [STRUCT('INBOUND_ETA_UNKNOWN', 'WARNING')], []),
    IF(l.inclusion_status = 'ETA_OVERDUE', [STRUCT('INBOUND_ETA_OVERDUE', 'WARNING')], []),
    IF(l.inclusion_status = 'EXCLUDED_HYPOTHETICAL', [STRUCT('INBOUND_HYPOTHETICAL', 'INFO')], []),
    IF(l.inclusion_status = 'RECEIVED_POSITION_UNKNOWN', [STRUCT('INBOUND_RECEIVED_POSITION_UNKNOWN', 'WARNING')], [])
  )) x,
  UNNEST([STRUCT(
    x.code AS exception_code, x.severity AS severity, CAST(NULL AS STRING) AS trajectory_basis, CAST(NULL AS STRING) AS plan_version,
    CAST(NULL AS STRING) AS plan_kind, l.internal_sku AS internal_sku, CAST(NULL AS STRING) AS product_name, l.inbound_id AS inbound_id,
    DATE_TRUNC(l.eta_date, MONTH) AS month, CAST(l.quantity AS FLOAT64) AS units,
    FORMAT('%s: %d ед., состояние %s%s, дата %s (%s)%s. В траекторию не входит (%s).', l.inbound_id, l.quantity, l.lot_state,
           IFNULL(CONCAT(', блокер ', l.blocker), ''), IFNULL(CAST(l.eta_date AS STRING), 'нет'), l.eta_status,
           IFNULL(CONCAT('; дата модели Control Tower ', CAST(l.ct_model_inbound_eta AS STRING), ' — не подтверждение'), ''),
           l.inclusion_status) AS detail,
    CONCAT(l.evidence_class, ': ', l.evidence_ref) AS evidence)]) e
),
u AS (
  SELECT * FROM tx_ex
  UNION ALL
  SELECT 'PLAN_INTEGRITY', 'BLOCKER', NULL, plan_version, plan_kind, NULL, NULL, NULL, NULL, CAST(events_ignored AS FLOAT64),
    CONCAT(IF(integrity_ok, '', CONCAT('Хеш шапки ', content_sha256, ' ≠ пересчёт ', recomputed_sha256, '. ')),
           IFNULL(CONCAT('Недействующие события: ', ignored_events), '')),
    'V_PLAN_VERSION_STATUS'
  FROM st
  WHERE NOT integrity_ok OR events_ignored > 0
  UNION ALL
  SELECT 'BOM_BASIS_MISSING', 'BLOCKER', NULL, plan_version, plan_kind, NULL, NULL, NULL, first_month, NULL,
    CONCAT('Наборы без снимка BOM версии: ', bundles, ' — физическая потребность не раскладывается'),
    'V_PLAN_PHYSICAL_MONTHLY / PLAN_BOM_BASIS'
  FROM bom_missing
  UNION ALL
  SELECT * FROM lots
)
SELECT
  exception_code,
  severity,
  trajectory_basis,
  plan_version,
  plan_kind,
  internal_sku,
  product_name,
  inbound_id,
  month,
  units,
  detail,
  evidence,
  CASE severity WHEN 'BLOCKER' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END AS severity_rank
FROM u
