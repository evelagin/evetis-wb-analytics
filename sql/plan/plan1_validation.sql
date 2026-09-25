-- ============================================================================
-- PR-PLAN-1 · DQ-набор sales_plan (живые объекты). Контракт:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md
-- Каждый блок возвращает строки с status PASS/FAIL. Только SELECT. Пороги нулевые (NUMERIC точен),
-- FLOAT64 — 1e-6. Регрессия на фикстурах — tools/plan1_render.py run fixtures.
-- ============================================================================

-- @check P01_VERSION_INTEGRITY
-- Пересчёт content_sha256 = шапке, строки и базис BOM = заявленным; шапка версии одна.
SELECT plan_version, plan_kind, lifecycle_status, line_rows_actual, bom_rows_actual,
  IF(integrity_ok AND header_rows = 1, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`;

-- @check P02_MODEL_NEVER_PROPOSED_OR_APPROVED
-- Модельный сценарий (в т.ч. SET_2026-09-09) не имеет ни действующего, ни записанного APPROVED/PROPOSED.
SELECT v.plan_version, v.lifecycle_status,
  COUNTIF(e.approval_status IN ('PROPOSED', 'APPROVED')) AS recorded_propose_or_approve,
  IF(v.lifecycle_status = 'MODEL_SCENARIO' AND v.approved_at IS NULL AND v.proposed_at IS NULL
     AND COUNTIF(e.approval_status IN ('PROPOSED', 'APPROVED')) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS` v
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL` e USING (plan_version)
WHERE v.plan_kind = 'MODEL_SCENARIO'
GROUP BY v.plan_version, v.lifecycle_status, v.approved_at, v.proposed_at;

-- @check P03_APPROVAL_EXACT_HASH
-- Действующее утверждение относится ровно к тому содержимому, которое сейчас в версии.
SELECT COUNT(*) AS approved_versions,
  COUNTIF(approved_content_sha256 IS DISTINCT FROM content_sha256 OR content_sha256 != recomputed_sha256) AS mismatched,
  IF(COUNTIF(approved_content_sha256 IS DISTINCT FROM content_sha256 OR content_sha256 != recomputed_sha256) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
WHERE approved_at IS NOT NULL;

-- @check P04_EVENT_LEDGER_VALID
-- Реестр событий: закрытый список, версия существует, хеш записан, месяц действия только у APPROVED.
SELECT COUNT(*) AS n_rows,
  COUNTIF(e.approval_status NOT IN ('PROPOSED', 'APPROVED', 'REVOKED', 'WITHDRAWN') OR v.plan_version IS NULL
          OR e.content_sha256 IS NULL OR (e.effective_from_month IS NOT NULL) != (e.approval_status = 'APPROVED')) AS invalid_rows,
  IF(COUNTIF(e.approval_status NOT IN ('PROPOSED', 'APPROVED', 'REVOKED', 'WITHDRAWN') OR v.plan_version IS NULL
             OR e.content_sha256 IS NULL OR (e.effective_from_month IS NOT NULL) != (e.approval_status = 'APPROVED')) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL` e
LEFT JOIN (SELECT DISTINCT plan_version FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`) v USING (plan_version);

-- @check P05_APPROVED_PLAN_GRAIN
-- Окна утверждений не пересекаются: на месяц × площадку × карточку не больше одной строки.
SELECT COUNT(*) AS n_rows, COUNT(DISTINCT FORMAT('%t|%s|%s', month, marketplace, internal_sku)) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t|%s|%s', month, marketplace, internal_sku)), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_APPROVED`;

-- @check P06_LEGACY_ADAPTER_PARITY
-- Legacy-версия читает CT_SEASON_PLAN_MONTHLY без копирования: итоги совпадают, в PLAN_LINE_MONTHLY строк нет.
WITH h AS (
  SELECT plan_version, source_ref FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`
  WHERE source_kind = 'LEGACY_CT_SEASON_PLAN_MONTHLY'
),
a AS (
  SELECT plan_version, SUM(planned_cards) AS adapter_cards
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_LINE_MONTHLY_ALL` GROUP BY plan_version
),
c AS (
  SELECT plan_version AS source_ref, SUM(target_cards) AS ct_cards
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN_MONTHLY` GROUP BY plan_version
),
n AS (
  SELECT plan_version, COUNT(*) AS copied_rows
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_LINE_MONTHLY` GROUP BY plan_version
)
SELECT h.plan_version, a.adapter_cards, ROUND(c.ct_cards, 4) AS ct_cards, IFNULL(n.copied_rows, 0) AS copied_rows,
  IF(ABS(a.adapter_cards - c.ct_cards) < 0.01 AND IFNULL(n.copied_rows, 0) = 0, 'PASS', 'FAIL') AS status
FROM h
LEFT JOIN a USING (plan_version)
LEFT JOIN c USING (source_ref)
LEFT JOIN n USING (plan_version);

-- @check P07_PHYSICAL_RECONCILIATION
-- Физическая потребность = независимый пересчёт «строки × базис BOM версии» (целый месяц).
WITH lines AS (
  SELECT l.plan_version, l.month, l.internal_sku, l.planned_cards, p.is_bundle
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_LINE_MONTHLY_ALL` l
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p USING (internal_sku)
),
indep AS (
  SELECT x.plan_version, x.month, IF(x.is_bundle, b.component_sku, x.internal_sku) AS component_sku,
    SUM(x.planned_cards * IF(x.is_bundle, b.component_qty, 1)) AS units
  FROM lines x
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_BOM_BASIS` b
    ON x.is_bundle AND b.plan_version = x.plan_version AND b.bundle_sku = x.internal_sku
  GROUP BY 1, 2, 3
)
SELECT COUNT(*) AS keys, COUNTIF(ABS(IFNULL(i.units, 0) - IFNULL(v.planned_units_full_month, 0)) > 1e-6) AS mismatched,
  IF(COUNTIF(ABS(IFNULL(i.units, 0) - IFNULL(v.planned_units_full_month, 0)) > 1e-6) = 0, 'PASS', 'FAIL') AS status
FROM indep i
FULL JOIN `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_PHYSICAL_MONTHLY` v
  ON v.plan_version = i.plan_version AND v.month = i.month AND v.component_sku IS NOT DISTINCT FROM i.component_sku;

-- @check P08_CHANNEL_AND_BUNDLE_SPLIT
-- WB + Ozon = остаток; одиночные + в наборах = остаток (по каждой строке физической потребности).
SELECT COUNT(*) AS n_rows,
  COUNTIF(ABS(wb_units_remaining + ozon_units_remaining - planned_units_remaining) > 1e-6
          OR ABS(standalone_units_remaining + via_bundle_units_remaining - planned_units_remaining) > 1e-6
          OR planned_units_remaining > planned_units_full_month + 1e-6 OR planned_units_remaining < 0) AS bad,
  IF(COUNTIF(ABS(wb_units_remaining + ozon_units_remaining - planned_units_remaining) > 1e-6
             OR ABS(standalone_units_remaining + via_bundle_units_remaining - planned_units_remaining) > 1e-6
             OR planned_units_remaining > planned_units_full_month + 1e-6 OR planned_units_remaining < 0) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_PHYSICAL_MONTHLY`
WHERE component_sku IS NOT NULL;

-- @check P09_AS_OF_PARITY
-- Даты запаса и продаж шапки (дешёвая формула) = V_SKU_SELL_THROUGH_CURRENT.
SELECT h.inventory_as_of_date, s.inventory_as_of_date AS layer_inventory_as_of, h.sales_as_of, s.sales_as_of AS layer_sales_as_of,
  IF(h.inventory_as_of_date = s.inventory_as_of_date AND h.sales_as_of = s.sales_as_of, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_HEADER` h
CROSS JOIN (SELECT ANY_VALUE(inventory_as_of_date) AS inventory_as_of_date, ANY_VALUE(sales_as_of) AS sales_as_of
            FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`) s;

-- @check P10_TRAJECTORY_ARITHMETIC
-- Конец = начало + поступления − план; начало месяца = конец предыдущего; нехватка = MAX(−конец, 0), без обрезки.
WITH t AS (
  SELECT trajectory_basis, plan_version, internal_sku, month, opening_units, eligible_inbound_units, planned_physical_units,
    closing_units, shortfall_units,
    LAG(closing_units) OVER (PARTITION BY trajectory_basis, plan_version, internal_sku ORDER BY month) AS prev_closing
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_TRAJECTORY_MONTHLY`
)
SELECT COUNT(*) AS n_rows, COUNTIF(closing_units IS NOT NULL) AS with_numbers,
  COUNTIF(closing_units IS NOT NULL AND ABS(closing_units - (opening_units + eligible_inbound_units - planned_physical_units)) > 1e-6) AS bad_closing,
  COUNTIF(closing_units IS NOT NULL AND prev_closing IS NOT NULL AND ABS(opening_units - prev_closing) > 1e-6) AS bad_chain,
  COUNTIF(closing_units IS NOT NULL AND ABS(shortfall_units - GREATEST(-closing_units, 0)) > 1e-6) AS bad_shortfall,
  IF(COUNTIF(closing_units IS NOT NULL AND ABS(closing_units - (opening_units + eligible_inbound_units - planned_physical_units)) > 1e-6)
     + COUNTIF(closing_units IS NOT NULL AND prev_closing IS NOT NULL AND ABS(opening_units - prev_closing) > 1e-6)
     + COUNTIF(closing_units IS NOT NULL AND ABS(shortfall_units - GREATEST(-closing_units, 0)) > 1e-6) = 0, 'PASS', 'FAIL') AS status
FROM t;

-- @check P11_STALE_AND_PLAN_GATING
-- Утверждённая траектория: числа только при FRESH запасе и покрытом утверждением месяце; сценарий помечен.
SELECT trajectory_basis, trajectory_status, inventory_freshness_status, COUNT(*) AS n_rows,
  COUNTIF(closing_units IS NOT NULL) AS with_numbers,
  IF(CASE
       WHEN trajectory_basis = 'APPROVED_PLAN' AND trajectory_status != 'COMPUTED' THEN COUNTIF(closing_units IS NOT NULL) = 0
       WHEN trajectory_basis = 'APPROVED_PLAN' THEN inventory_freshness_status = 'FRESH'
       WHEN inventory_freshness_status != 'FRESH' THEN trajectory_status IN ('SCENARIO_ON_STALE_INVENTORY', 'SKU_NOT_IN_INVENTORY', 'INTEGRITY_BROKEN')
       ELSE trajectory_status IN ('SCENARIO_COMPUTED', 'SKU_NOT_IN_INVENTORY', 'INTEGRITY_BROKEN')
     END, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_TRAJECTORY_MONTHLY`
GROUP BY 1, 2, 3;

-- @check P12_INBOUND_ELIGIBILITY
-- Независимый пересчёт правила: в траекторию — только подтверждённая дата без блокера или приёмка после среза ФФ.
WITH last_ev AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INBOUND_LOT_EVENT`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY inbound_id ORDER BY recorded_at DESC) = 1
)
SELECT v.inbound_id, v.internal_sku, v.lot_state, v.blocker, v.eta_status, v.inclusion_status, v.in_base_trajectory,
  IF(v.in_base_trajectory = (
       (e.lot_state IN ('ORDER_CONFIRMED', 'IN_PRODUCTION', 'PRODUCED', 'READY_FOR_SHIPMENT', 'IN_TRANSIT')
        AND e.blocker IS NULL AND e.eta_status = 'CONFIRMED' AND e.eta_date >= v.inventory_as_of_date)
       OR (e.lot_state = 'RECEIVED' AND e.received_date > v.ff_anchor_date))
     AND v.lot_state = e.lot_state, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT` v
JOIN last_ev e USING (inbound_id);

-- @check P13_TRAJECTORY_INBOUND_ONLY_ELIGIBLE
-- Поступления траектории (утверждённая основа, по SKU) = сумме допущенных партий в месяцах сетки.
WITH t AS (
  SELECT internal_sku, SUM(eligible_inbound_units) AS traj_units, MIN(month) AS m0, MAX(month) AS m1
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_TRAJECTORY_MONTHLY`
  WHERE trajectory_basis = 'APPROVED_PLAN'
  GROUP BY internal_sku
),
l AS (
  SELECT internal_sku, trajectory_month, SUM(IF(in_base_trajectory, trajectory_units, 0)) AS units
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT`
  GROUP BY 1, 2
),
j AS (
  SELECT t.internal_sku, ANY_VALUE(t.traj_units) AS traj_units,
    IFNULL(SUM(IF(l.trajectory_month BETWEEN t.m0 AND t.m1, l.units, 0)), 0) AS lot_units
  FROM t LEFT JOIN l USING (internal_sku)
  GROUP BY t.internal_sku
)
SELECT internal_sku, traj_units, lot_units, IF(traj_units = lot_units, 'PASS', 'FAIL') AS status
FROM j;

-- @check P14_PROPOSAL_METHOD_CALENDAR
-- OBSERVED_RUN_RATE_30D_V1: строка = ROUND(темп из допущения R-* × календарные дни месяца в горизонте, 1).
WITH v AS (
  SELECT plan_version, horizon_to FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`
  WHERE method_id = 'OBSERVED_RUN_RATE_30D_V1'
),
r AS (
  SELECT plan_version, scope_marketplace AS marketplace, scope_sku AS internal_sku, value_numeric AS rate
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_ASSUMPTION` WHERE assumption_type = 'OBSERVED_RATE'
)
SELECT l.plan_version, COUNT(*) AS lines,
  COUNTIF(l.planned_cards != ROUND(r.rate * (DATE_DIFF(LEAST(LAST_DAY(l.month), v.horizon_to), l.month, DAY) + 1), 1)) AS mismatched,
  COUNT(DISTINCT DATE_DIFF(LAST_DAY(l.month), l.month, DAY) + 1) AS distinct_month_lengths,
  IF(COUNTIF(r.rate IS NULL OR l.planned_cards != ROUND(r.rate * (DATE_DIFF(LEAST(LAST_DAY(l.month), v.horizon_to), l.month, DAY) + 1), 1)) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_LINE_MONTHLY` l
JOIN v USING (plan_version)
LEFT JOIN r USING (plan_version, marketplace, internal_sku)
GROUP BY l.plan_version;

-- @check P15_PROPOSAL_INHERITS_NO_MODEL_POLICY
-- Предложение системы не наследует множители, сезонность, программы и поставки модели.
SELECT v.plan_version,
  COUNTIF(a.assumption_type IN ('LEGACY_MULTIPLIER', 'PROGRAM') OR a.evidence_class = 'LEGACY_MODEL'
          OR (a.assumption_type = 'SEASONALITY' AND a.value_numeric != 1)) AS inherited,
  IF(COUNTIF(a.assumption_type IN ('LEGACY_MULTIPLIER', 'PROGRAM') OR a.evidence_class = 'LEGACY_MODEL'
             OR (a.assumption_type = 'SEASONALITY' AND a.value_numeric != 1)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION` v
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_ASSUMPTION` a USING (plan_version)
WHERE v.plan_kind = 'SYSTEM_PROPOSED'
GROUP BY v.plan_version;

-- @check P16_CONTROL_TOWER_NOT_SWITCHED
-- Control Tower и C1 читают свой план как раньше: ACTIVE-версия CT на месте и не утверждена в новом реестре.
WITH ct AS (SELECT COUNT(*) AS ct_active_rows FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE`),
ap AS (
  SELECT plan_version, COUNTIF(approval_status = 'APPROVED') AS approvals
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL` GROUP BY plan_version
)
SELECT p.plan_version, p.plan_status, ct.ct_active_rows, IFNULL(ap.approvals, 0) AS approvals,
  IF(ct.ct_active_rows > 0 AND IFNULL(ap.approvals, 0) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` p
CROSS JOIN ct
LEFT JOIN ap USING (plan_version)
WHERE p.plan_status = 'ACTIVE';

-- @check P17_HEADER_EXCEPTION_CONSISTENCY
-- Шапка и исключения говорят одно: нет утверждения ⇔ исключение NO_APPROVED_PLAN; несвежий запас ⇔ INVENTORY_STALE.
SELECT h.approved_plan_status, h.inventory_freshness_status, h.exception_codes,
  IF((h.approved_plan_status = 'NO_APPROVED_PLAN') = IFNULL(h.exception_codes LIKE '%NO_APPROVED_PLAN%', FALSE)
     AND (h.inventory_freshness_status = 'STALE') = IFNULL(h.exception_codes LIKE '%INVENTORY_STALE%', FALSE), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_HEADER` h;

-- @check P18_OVERVIEW_GRAIN
-- Обзор: одна строка на физический SKU запаса, наборов среди строк нет.
SELECT COUNT(*) AS n_rows, COUNT(DISTINCT o.internal_sku) AS n_skus, COUNTIF(IFNULL(p.is_bundle, FALSE)) AS bundles,
  IF(COUNT(*) = COUNT(DISTINCT o.internal_sku) AND COUNTIF(IFNULL(p.is_bundle, FALSE)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLANNING_SKU_OVERVIEW` o
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p USING (internal_sku);
