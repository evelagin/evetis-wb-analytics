-- ============================================================================
-- PR-PROMO-4 · проверки слоя запасов и распродажи против production. ТОЛЬКО ЧТЕНИЕ.
-- Контракт: docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md §24.
-- Исполнитель: python tools/run_data_checks.py --suite inventory_context …
-- До развёртывания: python tools/promo_inventory_render.py run predeploy <этот файл> …
-- (вью слоя — из Git, справочники владельца — пустые типизированные CTE). I30 до
-- развёртывания FAIL по построению: INFORMATION_SCHEMA ещё не знает новых вью.
-- Пороги только нулевые и арифметика FLOAT64 (1e-9). Бизнес-порогов нет.
-- Каждое тяжёлое представление — в своём операторе (предел планировщика BigQuery).
-- ============================================================================

-- @check I01A_GRAIN_POSITION
-- Зерно истории запаса и текущих фактов SKU.
SELECT 'V_INVENTORY_POSITION_HISTORY' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (snapshot_date, internal_sku))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (snapshot_date, internal_sku))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`
UNION ALL
SELECT 'V_SKU_SELL_THROUGH_CURRENT', COUNT(*), COUNT(DISTINCT internal_sku),
  IF(COUNT(*) = COUNT(DISTINCT internal_sku) AND COUNT(*) > 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`;

-- @check I01B_GRAIN_TARGETS
SELECT 'V_SKU_INVENTORY_TARGET_CURRENT' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (internal_sku, target_key))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (internal_sku, target_key))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`;

-- @check I01C_GRAIN_PLAN
SELECT 'V_SALES_PLAN_MONTHLY_CURRENT' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (plan_version, month, marketplace, internal_sku))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (plan_version, month, marketplace, internal_sku))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT`;

-- @check I01D_GRAIN_TRAJECTORY
SELECT 'V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (internal_sku, trajectory_basis, basis_key, month))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (internal_sku, trajectory_basis, basis_key, month))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT`;

-- @check I01E_GRAIN_BUNDLES
SELECT 'V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (bundle_sku, component_sku))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (bundle_sku, component_sku))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT`;

-- @check I02_NO_DOUBLE_COUNT
-- Позиция = ФФ + площадки + в пути; площадки = WB + Ozon + FBS; в пути = WB + Ozon. Резерв у
-- покупателя, претензия и неполученная партия в позицию не входят — по всей истории.
SELECT COUNT(*) AS n_rows,
  COUNTIF(inventory_position_units != warehouse_ff_units + marketplace_available_units + in_transit_units) AS bad_position,
  COUNTIF(marketplace_available_units != wb_available_units + ozon_available_units + fbs_units) AS bad_marketplace,
  COUNTIF(in_transit_units != wb_in_transit_units + ozon_in_transit_units) AS bad_transit,
  COUNTIF(marketplace_units_inside_bundle_cards > marketplace_available_units) AS bundle_units_not_subset,
  IF(COUNTIF(inventory_position_units != warehouse_ff_units + marketplace_available_units + in_transit_units)
     + COUNTIF(marketplace_available_units != wb_available_units + ozon_available_units + fbs_units)
     + COUNTIF(in_transit_units != wb_in_transit_units + ozon_in_transit_units)
     + COUNTIF(marketplace_units_inside_bundle_cards > marketplace_available_units) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`;

-- @check I03_NON_NEGATIVE
-- Ни одна составляющая запаса не отрицательна (источник не допускает отрицательного остатка).
SELECT COUNT(*) AS n_rows,
  COUNTIF(LEAST(warehouse_ff_units, wb_available_units, ozon_available_units, fbs_units, wb_in_transit_units,
                ozon_in_transit_units, inventory_position_units, reserved_in_delivery_units,
                unavailable_claimed_units, inbound_not_received_units) < 0) AS negative_rows,
  IF(COUNTIF(LEAST(warehouse_ff_units, wb_available_units, ozon_available_units, fbs_units, wb_in_transit_units,
                   ozon_in_transit_units, inventory_position_units, reserved_in_delivery_units,
                   unavailable_claimed_units, inbound_not_received_units) < 0) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`;

-- @check I04_FRESHNESS_EXPOSED
-- У каждого факта есть время: запас, дата продаж, статус свежести; причина — ровно при STALE.
SELECT COUNT(*) AS n_rows,
  COUNTIF(inventory_as_of IS NULL OR inventory_as_of_date IS NULL) AS no_inventory_time,
  COUNTIF(sales_as_of IS NULL) AS no_sales_time,
  COUNTIF(inventory_freshness_status NOT IN ('FRESH', 'STALE') OR inventory_freshness_status IS NULL) AS bad_status,
  COUNTIF((inventory_freshness_status = 'STALE') != (inventory_freshness_reason IS NOT NULL)) AS reason_mismatch,
  IF(COUNTIF(inventory_as_of IS NULL OR inventory_as_of_date IS NULL) + COUNTIF(sales_as_of IS NULL)
     + COUNTIF(inventory_freshness_status NOT IN ('FRESH', 'STALE') OR inventory_freshness_status IS NULL)
     + COUNTIF((inventory_freshness_status = 'STALE') != (inventory_freshness_reason IS NOT NULL)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`;

-- @check I05_FRESHNESS_PARITY_WITH_CONTROL_TOWER
-- Свежесть слоя = V_CT_FRESHNESS по доменам остатков и среза; сроки слоя = sla_days Control Tower.
WITH mine AS (
  SELECT ANY_VALUE(ct_inventory_freshness) AS ct_inventory, ANY_VALUE(wb_stock_freshness) AS wb_stock,
    ANY_VALUE(ozon_stock_freshness) AS ozon_stock, ANY_VALUE(ff_stock_freshness) AS ff_stock
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
ct AS (
  SELECT domain, status, sla_days
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_FRESHNESS`
  WHERE domain IN ('CT_INVENTORY', 'WB_STOCK', 'OZON_STOCK', 'FF_STOCK')
)
SELECT ct.domain, ct.status AS ct_status, ct.sla_days AS ct_sla_days,
  CASE ct.domain WHEN 'CT_INVENTORY' THEN mine.ct_inventory WHEN 'WB_STOCK' THEN mine.wb_stock
                 WHEN 'OZON_STOCK' THEN mine.ozon_stock WHEN 'FF_STOCK' THEN mine.ff_stock END AS layer_status,
  CASE ct.domain WHEN 'WB_STOCK' THEN 1 WHEN 'OZON_STOCK' THEN 1 WHEN 'FF_STOCK' THEN 7 END AS layer_sla_days,
  IF(ct.status = CASE ct.domain WHEN 'CT_INVENTORY' THEN mine.ct_inventory WHEN 'WB_STOCK' THEN mine.wb_stock
                                WHEN 'OZON_STOCK' THEN mine.ozon_stock WHEN 'FF_STOCK' THEN mine.ff_stock END
     AND (ct.domain = 'CT_INVENTORY'
          OR ct.sla_days = CASE ct.domain WHEN 'WB_STOCK' THEN 1 WHEN 'OZON_STOCK' THEN 1 WHEN 'FF_STOCK' THEN 7 END),
     'PASS', 'FAIL') AS status
FROM ct CROSS JOIN mine;

-- @check I06_UNMAPPED_INVENTORY_SURFACED
-- Остаток площадки без канонического SKU не пропадает молча: 0 единиц — PASS, иначе FAIL со счётом.
SELECT 'WB' AS marketplace, COUNT(*) AS unmapped_rows, IFNULL(SUM(quantity), 0) AS unmapped_units,
  IF(COUNT(*) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT`
WHERE internal_sku IS NULL AND row_type = 'AGGREGATE' AND quantity != 0
UNION ALL
SELECT 'OZON', COUNT(*), IFNULL(SUM(s.available_stock_count), 0), IF(COUNT(*) = 0, 'PASS', 'FAIL')
FROM (
  SELECT sku, available_stock_count
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`
  WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY sku, warehouse_id ORDER BY extracted_at DESC) = 1
) s
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m ON m.marketplace = 'OZON' AND m.marketplace_sku = s.sku
WHERE m.internal_sku IS NULL AND s.available_stock_count != 0
UNION ALL
SELECT 'POSITION_HISTORY', COUNTIF(mapping_status != 'CANONICAL_SKU'), SUM(IF(mapping_status != 'CANONICAL_SKU', inventory_position_units, 0)),
  IF(COUNTIF(mapping_status != 'CANONICAL_SKU') = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`;

-- @check I07_VELOCITY_WINDOW_ARITHMETIC
-- Скорость окна = единицы / N; единицы окна пересчитаны из V_CT_PHYSICAL_DAILY за буквальные
-- N суток [sales_as_of − N + 1; sales_as_of].
WITH s AS (
  SELECT internal_sku, sales_as_of,
    [units_ordered_7d, units_ordered_14d, units_ordered_30d, units_ordered_60d, units_ordered_90d] AS u,
    [units_per_day_7d, units_per_day_14d, units_per_day_30d, units_per_day_60d, units_per_day_90d] AS v
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
p AS (
  SELECT internal_sku, d, units_ordered FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY`
),
w AS (
  SELECT s.internal_sku, n, s.u[OFFSET(i)] AS units, s.v[OFFSET(i)] AS per_day,
    (SELECT IFNULL(SUM(p.units_ordered), 0) FROM p WHERE p.internal_sku = s.internal_sku
       AND p.d BETWEEN DATE_SUB(s.sales_as_of, INTERVAL n - 1 DAY) AND s.sales_as_of) AS recomputed
  FROM s, UNNEST([7, 14, 30, 60, 90]) AS n WITH OFFSET i
)
SELECT n AS window_days, COUNT(*) AS skus,
  COUNTIF(units != recomputed) AS units_mismatch,
  COUNTIF(ABS(per_day - units / n) > 1e-9) AS rate_mismatch,
  IF(COUNTIF(units != recomputed) + COUNTIF(ABS(per_day - units / n) > 1e-9) = 0, 'PASS', 'FAIL') AS status
FROM w GROUP BY n ORDER BY n;

-- @check I08_CALENDAR_CORRECT
-- Месяцы плана и траектории — настоящими датами: дни месяца = LAST_DAY, февраль 28/29, период = даты.
SELECT 'PLAN' AS object_name, COUNT(*) AS n_rows,
  COUNTIF(days_in_month != EXTRACT(DAY FROM LAST_DAY(month))) AS bad_days_in_month,
  COUNTIF(plan_days != DATE_DIFF(plan_period_end, plan_period_start, DAY) + 1) AS bad_period_days,
  COUNTIF(EXTRACT(MONTH FROM month) = 2 AND days_in_month NOT IN (28, 29)) AS bad_february,
  IF(COUNTIF(days_in_month != EXTRACT(DAY FROM LAST_DAY(month)))
     + COUNTIF(plan_days != DATE_DIFF(plan_period_end, plan_period_start, DAY) + 1)
     + COUNTIF(EXTRACT(MONTH FROM month) = 2 AND days_in_month NOT IN (28, 29)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT`;

-- @check I09_ZERO_VELOCITY_HANDLING
-- Скорость 0 никогда не даёт 0 дней покрытия; 0 дней — только при нулевой позиции и скорости > 0.
SELECT COUNT(*) AS skus,
  COUNTIF(units_per_day_30d = 0 AND cover_days_30d IS NOT NULL) AS zero_velocity_with_cover,
  COUNTIF(units_per_day_30d = 0 AND cover_status_30d NOT IN ('NO_VELOCITY', 'NO_INVENTORY_NO_VELOCITY')) AS bad_status,
  COUNTIF(cover_days_30d = 0 AND NOT (inventory_position_units = 0 AND units_per_day_30d > 0)) AS zero_days_misused,
  IF(COUNTIF(units_per_day_30d = 0 AND cover_days_30d IS NOT NULL)
     + COUNTIF(units_per_day_30d = 0 AND cover_status_30d NOT IN ('NO_VELOCITY', 'NO_INVENTORY_NO_VELOCITY'))
     + COUNTIF(cover_days_30d = 0 AND NOT (inventory_position_units = 0 AND units_per_day_30d > 0)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`;

-- @check I10_STOCKOUT_CONSTRAINED_STATUS
-- Дни нуля на полке пересчитаны из истории запаса; окно с таким днём не может быть NORMAL.
WITH s AS (
  SELECT internal_sku, sales_as_of, stockout_days_14d, velocity_quality_14d, stockout_days_30d, velocity_quality_30d
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
h AS (
  SELECT internal_sku, snapshot_date,
    (listed_on_wb AND wb_available_units = 0) OR (listed_on_ozon AND ozon_available_units = 0) AS zero_day
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`
),
r AS (
  SELECT s.*,
    (SELECT COUNTIF(h.zero_day) FROM h WHERE h.internal_sku = s.internal_sku
       AND h.snapshot_date BETWEEN DATE_SUB(s.sales_as_of, INTERVAL 13 DAY) AND s.sales_as_of) AS recomputed_14d
  FROM s
)
SELECT COUNT(*) AS skus,
  COUNTIF(stockout_days_14d != recomputed_14d) AS days_mismatch,
  COUNTIF(stockout_days_14d > 0 AND velocity_quality_14d IN ('NORMAL', 'NO_SALES_WITH_STOCK', 'AVAILABILITY_NOT_FULLY_OBSERVED')) AS normal_despite_stockout,
  COUNTIF(stockout_days_30d > 0 AND velocity_quality_30d IN ('NORMAL', 'NO_SALES_WITH_STOCK', 'AVAILABILITY_NOT_FULLY_OBSERVED')) AS normal_despite_stockout_30d,
  IF(COUNTIF(stockout_days_14d != recomputed_14d)
     + COUNTIF(stockout_days_14d > 0 AND velocity_quality_14d IN ('NORMAL', 'NO_SALES_WITH_STOCK', 'AVAILABILITY_NOT_FULLY_OBSERVED'))
     + COUNTIF(stockout_days_30d > 0 AND velocity_quality_30d IN ('NORMAL', 'NO_SALES_WITH_STOCK', 'AVAILABILITY_NOT_FULLY_OBSERVED')) = 0,
     'PASS', 'FAIL') AS status
FROM r;

-- @check I11_COVER_ARITHMETIC
-- Покрытие = позиция / скорость окна для каждого окна со скоростью > 0.
WITH s AS (
  SELECT internal_sku, inventory_position_units AS pos,
    [units_per_day_7d, units_per_day_14d, units_per_day_30d, units_per_day_60d, units_per_day_90d] AS v,
    [cover_days_7d, cover_days_14d, cover_days_30d, cover_days_60d, cover_days_90d] AS c
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
)
SELECT COUNT(*) AS sku_windows,
  COUNTIF(v[OFFSET(i)] > 0 AND ABS(c[OFFSET(i)] - pos / v[OFFSET(i)]) > 1e-6) AS bad_cover,
  COUNTIF(v[OFFSET(i)] > 0 AND c[OFFSET(i)] IS NULL) AS missing_cover,
  IF(COUNTIF(v[OFFSET(i)] > 0 AND ABS(c[OFFSET(i)] - pos / v[OFFSET(i)]) > 1e-6)
     + COUNTIF(v[OFFSET(i)] > 0 AND c[OFFSET(i)] IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM s, UNNEST([0, 1, 2, 3, 4]) AS i;

-- @check I13_EXPIRY_EVIDENCE
-- Статус доказательности срока следует из источника партии; SKU без партий — EXPIRY_UNAVAILABLE;
-- самая ранняя партия на руках = срок, который Control Tower положил в срез того же дня.
WITH b AS (
  SELECT internal_sku,
    ARRAY_AGG(IF(NOT is_inbound, STRUCT(expiry_date, expiry_source), NULL) IGNORE NULLS ORDER BY expiry_date, batch_id LIMIT 1)[SAFE_OFFSET(0)] AS e
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_EXPIRY_BATCH` GROUP BY internal_sku
),
s AS (
  SELECT internal_sku, expiry_evidence_status, earliest_expiry_date, inventory_as_of_date
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
ct AS (
  SELECT internal_sku, snapshot_date, expiry_date
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`
)
SELECT COUNT(*) AS skus,
  COUNTIF(expiry_evidence_status != CASE WHEN b.e IS NULL THEN 'EXPIRY_UNAVAILABLE'
    WHEN b.e.expiry_source = 'OWNER_FACT_HARD' THEN 'EXPLICIT_EXPIRY_OWNER_FACT'
    WHEN b.e.expiry_source = 'OWNER_FACT_SHELF_LIFE' THEN 'MFG_DATE_PLUS_SHELF_LIFE'
    WHEN b.e.expiry_source = 'INFERENCE_IMPORT_MINUS_70D' THEN 'MFG_DATE_INFERRED_PLUS_SHELF_LIFE'
    ELSE 'UNRECOGNISED_EXPIRY_SOURCE' END) AS bad_evidence,
  COUNTIF(s.earliest_expiry_date IS DISTINCT FROM ct.expiry_date) AS differs_from_control_tower,
  COUNTIF(expiry_evidence_status = 'UNRECOGNISED_EXPIRY_SOURCE') AS unrecognised,
  IF(COUNTIF(expiry_evidence_status != CASE WHEN b.e IS NULL THEN 'EXPIRY_UNAVAILABLE'
    WHEN b.e.expiry_source = 'OWNER_FACT_HARD' THEN 'EXPLICIT_EXPIRY_OWNER_FACT'
    WHEN b.e.expiry_source = 'OWNER_FACT_SHELF_LIFE' THEN 'MFG_DATE_PLUS_SHELF_LIFE'
    WHEN b.e.expiry_source = 'INFERENCE_IMPORT_MINUS_70D' THEN 'MFG_DATE_INFERRED_PLUS_SHELF_LIFE'
    ELSE 'UNRECOGNISED_EXPIRY_SOURCE' END)
     + COUNTIF(s.earliest_expiry_date IS DISTINCT FROM ct.expiry_date)
     + COUNTIF(expiry_evidence_status = 'UNRECOGNISED_EXPIRY_SOURCE') = 0, 'PASS', 'FAIL') AS status
FROM s
LEFT JOIN b USING (internal_sku)
LEFT JOIN ct ON ct.internal_sku = s.internal_sku AND ct.snapshot_date = s.inventory_as_of_date;

-- @check I14_DAYS_TO_EXPIRY
-- Дни до срока = дата срока − дата запаса; то же число, что у Control Tower в срезе того же дня.
SELECT COUNT(*) AS skus,
  COUNTIF(s.days_to_expiry IS DISTINCT FROM DATE_DIFF(s.earliest_expiry_date, s.inventory_as_of_date, DAY)) AS bad_arith,
  COUNTIF(s.days_to_expiry IS DISTINCT FROM ct.days_to_expiry) AS differs_from_control_tower,
  IF(COUNTIF(s.days_to_expiry IS DISTINCT FROM DATE_DIFF(s.earliest_expiry_date, s.inventory_as_of_date, DAY))
     + COUNTIF(s.days_to_expiry IS DISTINCT FROM ct.days_to_expiry) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT` s
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY` ct
  ON ct.internal_sku = s.internal_sku AND ct.snapshot_date = s.inventory_as_of_date;

-- @check I15_NO_INVENTED_BUFFER
-- Буфер продажи до срока — ровно OPS_CONFIG.c1_expiry_margin_days; другого нет.
WITH cfg AS (
  SELECT SAFE_CAST(MAX(config_value) AS INT64) AS margin
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` WHERE config_key = 'c1_expiry_margin_days'
)
SELECT COUNT(*) AS skus, ANY_VALUE(cfg.margin) AS owner_margin_days,
  COUNTIF(s.sell_by_margin_days IS DISTINCT FROM cfg.margin) AS other_margin,
  COUNTIF(s.sell_by_date IS DISTINCT FROM DATE_SUB(s.earliest_expiry_date, INTERVAL cfg.margin DAY)) AS bad_sell_by,
  IF(COUNTIF(s.sell_by_margin_days IS DISTINCT FROM cfg.margin)
     + COUNTIF(s.sell_by_date IS DISTINCT FROM DATE_SUB(s.earliest_expiry_date, INTERVAL cfg.margin DAY)) = 0
     AND ANY_VALUE(cfg.margin) IS NOT NULL, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT` s CROSS JOIN cfg;

-- @check I16_TARGET_PROVENANCE
-- Цель с датой обязана иметь происхождение; типы и основания — из закрытого списка.
SELECT COUNT(*) AS targets,
  COUNTIF(target_date IS NOT NULL AND target_provenance IS NULL) AS no_provenance,
  COUNTIF(target_basis NOT IN ('MATHEMATICAL_TO_EXPIRY', 'OWNER_CONVENTION_C1_EXPIRY_MARGIN', 'OWNER_TARGET')) AS bad_basis,
  COUNTIF(target_basis != 'OWNER_TARGET' AND target_type NOT IN ('EXPIRY_DATE', 'EXPIRY_SELL_BY')) AS invented_type,
  COUNTIF(target_basis = 'MATHEMATICAL_TO_EXPIRY' AND is_operational_target) AS math_called_operational,
  IF(COUNTIF(target_date IS NOT NULL AND target_provenance IS NULL)
     + COUNTIF(target_basis NOT IN ('MATHEMATICAL_TO_EXPIRY', 'OWNER_CONVENTION_C1_EXPIRY_MARGIN', 'OWNER_TARGET'))
     + COUNTIF(target_basis != 'OWNER_TARGET' AND target_type NOT IN ('EXPIRY_DATE', 'EXPIRY_SELL_BY'))
     + COUNTIF(target_basis = 'MATHEMATICAL_TO_EXPIRY' AND is_operational_target) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`;

-- @check I17_I20_REQUIRED_SELL_THROUGH_ARITHMETIC
-- Требуемые единицы, единицы в день, uplift и нулевая скорость — по формулам контракта.
SELECT COUNT(*) AS targets,
  COUNTIF(required_units_to_sell IS NOT NULL
          AND required_units_to_sell != GREATEST(inventory_position_units - target_ending_units, 0)) AS bad_units,
  COUNTIF(days_until_target IS NOT NULL AND days_until_target != DATE_DIFF(target_date, inventory_as_of_date, DAY)) AS bad_days,
  COUNTIF(required_units_per_day IS NOT NULL AND ABS(required_units_per_day - required_units_to_sell / days_until_target) > 1e-9) AS bad_rate,
  COUNTIF(target_status = 'COMPUTED'
          AND ABS(required_inventory_uplift_pct - (required_units_per_day / current_units_per_day - 1) * 100) > 1e-6) AS bad_uplift,
  COUNTIF(target_status = 'NO_ACCELERATION_REQUIRED' AND required_inventory_uplift_pct != 0) AS bad_no_accel,
  COUNTIF(target_status = 'CURRENT_VELOCITY_ZERO' AND (required_inventory_uplift_pct IS NOT NULL OR required_units_per_day IS NULL)) AS bad_zero_velocity,
  COUNTIF(target_status NOT IN ('COMPUTED', 'NO_ACCELERATION_REQUIRED') AND required_inventory_uplift_pct IS NOT NULL) AS uplift_without_basis,
  IF(COUNTIF(required_units_to_sell IS NOT NULL
             AND required_units_to_sell != GREATEST(inventory_position_units - target_ending_units, 0))
     + COUNTIF(days_until_target IS NOT NULL AND days_until_target != DATE_DIFF(target_date, inventory_as_of_date, DAY))
     + COUNTIF(required_units_per_day IS NOT NULL AND ABS(required_units_per_day - required_units_to_sell / days_until_target) > 1e-9)
     + COUNTIF(target_status = 'COMPUTED'
               AND ABS(required_inventory_uplift_pct - (required_units_per_day / current_units_per_day - 1) * 100) > 1e-6)
     + COUNTIF(target_status = 'NO_ACCELERATION_REQUIRED' AND required_inventory_uplift_pct != 0)
     + COUNTIF(target_status = 'CURRENT_VELOCITY_ZERO' AND (required_inventory_uplift_pct IS NOT NULL OR required_units_per_day IS NULL))
     + COUNTIF(target_status NOT IN ('COMPUTED', 'NO_ACCELERATION_REQUIRED') AND required_inventory_uplift_pct IS NOT NULL) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`;

-- @check I21_STALE_INVENTORY_NOT_CURRENT
-- Устаревший запас не даёт требуемых чисел (ни целям, ни траектории по плану).
SELECT 'TARGETS' AS object_name, COUNTIF(inventory_freshness_status = 'STALE') AS stale_rows,
  COUNTIF(inventory_freshness_status = 'STALE' AND (required_units_per_day IS NOT NULL OR required_inventory_uplift_pct IS NOT NULL)) AS computed_on_stale,
  IF(COUNTIF(inventory_freshness_status = 'STALE' AND (required_units_per_day IS NOT NULL OR required_inventory_uplift_pct IS NOT NULL)) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`;

-- @check I21B_STALE_TRAJECTORY
SELECT 'TRAJECTORY' AS object_name, COUNTIF(trajectory_status = 'INVENTORY_STALE') AS stale_rows,
  COUNTIF(trajectory_status != 'COMPUTED' AND (opening_units IS NOT NULL OR sales_units IS NOT NULL)) AS computed_on_stale,
  IF(COUNTIF(trajectory_status != 'COMPUTED' AND (opening_units IS NOT NULL OR sales_units IS NOT NULL)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT`;

-- @check I22_I23_BUNDLE_INVENTORY_ONLY_FROM_BOM
-- Мощность — только у набора с BOM; qty > 0; наборов нет в позиции физических SKU.
SELECT COUNT(DISTINCT bundle_sku) AS bundles,
  COUNTIF(capacity_status = 'BUNDLE_BOM_UNAVAILABLE' AND technical_assembly_capacity_units IS NOT NULL) AS capacity_without_bom,
  COUNTIF(component_qty <= 0) AS bad_qty,
  COUNTIF(capacity_status = 'BOM_QTY_INVALID') AS invalid_bom,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY` WHERE STARTS_WITH(internal_sku, 'EVT-SET-')) AS bundles_in_position,
  IF(COUNTIF(capacity_status = 'BUNDLE_BOM_UNAVAILABLE' AND technical_assembly_capacity_units IS NOT NULL)
     + COUNTIF(component_qty <= 0) + COUNTIF(capacity_status = 'BOM_QTY_INVALID')
     + (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY` WHERE STARTS_WITH(internal_sku, 'EVT-SET-')) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT`;

-- @check I24_I26_BUNDLE_CAPACITY_ARITHMETIC
-- Мощность = MIN(FLOOR(ФФ / qty)) = assemblable_now_ff Control Tower; ограничивающие — все с
-- минимумом, по алфавиту; независимость ⇔ нет общих компонентов; семантика не распределена.
WITH b AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT`
  WHERE capacity_status = 'COMPUTED'
)
SELECT COUNT(*) AS rows_computed,
  COUNTIF(technical_assembly_capacity_units != DIV(component_ff_units, component_qty) AND is_constraining_component) AS constraining_not_min,
  COUNTIF(technical_assembly_capacity_units != ct_assemblable_now_ff) AS differs_from_control_tower,
  COUNTIF(component_capacity_units < technical_assembly_capacity_units) AS capacity_above_component,
  COUNTIF(capacity_is_independent != (shares_components_with IS NULL)) AS bad_independence,
  COUNTIF(capacity_semantics != 'TECHNICAL_ASSEMBLY_CAPACITY_FROM_FF_UNITS_NOT_ALLOCATED') AS bad_semantics,
  COUNTIF(STRPOS(constraining_components, first_constraining_component) != 1) AS nondeterministic_first,
  IF(COUNTIF(technical_assembly_capacity_units != DIV(component_ff_units, component_qty) AND is_constraining_component)
     + COUNTIF(technical_assembly_capacity_units != ct_assemblable_now_ff)
     + COUNTIF(component_capacity_units < technical_assembly_capacity_units)
     + COUNTIF(capacity_is_independent != (shares_components_with IS NULL))
     + COUNTIF(capacity_semantics != 'TECHNICAL_ASSEMBLY_CAPACITY_FROM_FF_UNITS_NOT_ALLOCATED')
     + COUNTIF(STRPOS(constraining_components, first_constraining_component) != 1) = 0, 'PASS', 'FAIL') AS status
FROM b;

-- @check I27_SCOPE_EXPLICIT
-- Область доступности каждой составляющей названа явно и одинаково во всей истории.
SELECT COUNT(*) AS n_rows,
  COUNTIF(warehouse_ff_scope != 'WAREHOUSE_SHARED_POOL' OR marketplace_available_scope != 'CHANNEL_SPECIFIC'
          OR in_transit_scope != 'CHANNEL_COMMITTED' OR inventory_position_scope != 'GLOBAL_PHYSICAL') AS bad_scope,
  IF(COUNTIF(warehouse_ff_scope != 'WAREHOUSE_SHARED_POOL' OR marketplace_available_scope != 'CHANNEL_SPECIFIC'
             OR in_transit_scope != 'CHANNEL_COMMITTED' OR inventory_position_scope != 'GLOBAL_PHYSICAL') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`;

-- @check I28_PROMO_JOIN_GRAIN
-- Контекст запаса не размножает сценарии: строк столько же, ключи уникальны.
SELECT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT`) AS source_rows,
  COUNT(*) AS context_rows, COUNT(DISTINCT FORMAT('%t', (marketplace, scenario_entity_key))) AS context_keys,
  IF(COUNT(*) = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT`)
     AND COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, scenario_entity_key))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_INVENTORY_CONTEXT_CURRENT`;

-- @check I29_ECONOMICS_UNCHANGED_AFTER_JOIN
-- Каждое экономическое поле контекста совпадает с PR-PROMO-3 по ключу (включая NULL).
SELECT COUNT(*) AS scenarios,
  COUNTIF(c.scenario_entity_key IS NULL) AS missing_in_context,
  COUNTIF(FORMAT('%t', (c.scenario_price_rub, c.baseline_price_rub, c.baseline_contribution_expected_rub,
                        c.promo_contribution_expected_rub, c.delta_contribution_expected_rub, c.baseline_margin_expected_pct,
                        c.promo_margin_expected_pct, c.break_even_price_expected_rub, c.promo_contribution_downside_rub,
                        c.promo_downside_negative, c.uplift_status, c.required_sales_uplift_pct, c.economics_status))
          != FORMAT('%t', (e.scenario_price_rub, e.baseline_price_rub, e.baseline_contribution_expected_rub,
                           e.promo_contribution_expected_rub, e.delta_contribution_expected_rub, e.baseline_margin_expected_pct,
                           e.promo_margin_expected_pct, e.break_even_price_expected_rub, e.promo_contribution_downside_rub,
                           e.promo_downside_negative, e.uplift_status, e.required_sales_uplift_pct, e.economics_status))) AS changed,
  IF(COUNTIF(c.scenario_entity_key IS NULL) = 0
     AND COUNTIF(FORMAT('%t', (c.scenario_price_rub, c.baseline_price_rub, c.baseline_contribution_expected_rub,
                        c.promo_contribution_expected_rub, c.delta_contribution_expected_rub, c.baseline_margin_expected_pct,
                        c.promo_margin_expected_pct, c.break_even_price_expected_rub, c.promo_contribution_downside_rub,
                        c.promo_downside_negative, c.uplift_status, c.required_sales_uplift_pct, c.economics_status))
          != FORMAT('%t', (e.scenario_price_rub, e.baseline_price_rub, e.baseline_contribution_expected_rub,
                           e.promo_contribution_expected_rub, e.delta_contribution_expected_rub, e.baseline_margin_expected_pct,
                           e.promo_margin_expected_pct, e.break_even_price_expected_rub, e.promo_contribution_downside_rub,
                           e.promo_downside_negative, e.uplift_status, e.required_sales_uplift_pct, e.economics_status))) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT` e
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_INVENTORY_CONTEXT_CURRENT` c
  USING (marketplace, scenario_entity_key);

-- @check I30_I32_NO_DECISION_NO_FLOOR_NO_PII
-- В колонках семи вью нет решений, баллов, ранжирования, ослабления порога маржи и персональных данных.
SELECT COUNT(*) AS columns_checked,
  COUNT(DISTINCT table_name) AS views_found,
  STRING_AGG(IF(REGEXP_CONTAINS(LOWER(column_name),
    r'score|rank|recommend|decision|verdict|approve_promo|enter_|exit_|stay_|watch_|floor|relax|buyer|customer|phone|email|address|surname|patronymic|fio'),
    CONCAT(table_name, '.', column_name), NULL)) AS forbidden_columns,
  IF(COUNT(DISTINCT table_name) = 7 AND COUNTIF(REGEXP_CONTAINS(LOWER(column_name),
    r'score|rank|recommend|decision|verdict|approve_promo|enter_|exit_|stay_|watch_|floor|relax|buyer|customer|phone|email|address|surname|patronymic|fio')) = 0,
    'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('V_INVENTORY_POSITION_HISTORY', 'V_SKU_SELL_THROUGH_CURRENT', 'V_SKU_INVENTORY_TARGET_CURRENT',
                     'V_SALES_PLAN_MONTHLY_CURRENT', 'V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT',
                     'V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT', 'V_PROMO_INVENTORY_CONTEXT_CURRENT');

-- @check I33_CONTROL_TOWER_CONTRACT_UNCHANGED
-- Потребляемые и соседние объекты Control Tower / evetis_ops не изменены: хеш тела = закреплённый
-- при PR-PROMO-4 (2026-09-24). Осознанное изменение Control Tower требует обновить закрепление.
WITH pins AS (
  SELECT * FROM UNNEST([
    STRUCT('evetis_ops.V_CT_BUNDLE_CAPACITY' AS obj, 'a9998fd37fb7593dce839f83de007b955e94c4d73330dcc04381e0a2bafb23f1' AS h),
    ('evetis_ops.V_CT_STOCK_STATE', '9a1631dabe41a79e374c02bb6f319e1907a80fa90dc2b16d20899b3ab5e53a76'),
    ('evetis_ops.V_OPS_BOM', '54d0e6f18f2220d66b78795cff422076a6a58090e32ded08a3902493f5b8f0c9'),
    ('wb_mart.V_CT_ACTION_CANDIDATES', 'd6bfb4af2b2f2378dacac3b89f856f707cd46cb52d8ccf95def4adc9541557a9'),
    ('wb_mart.V_CT_ACTUAL_DAILY', '07432c8fe597bb3ac92248404a3a9d8d90398a0e56e188bb96bf440525dab6f3'),
    ('wb_mart.V_CT_ACTUAL_DAILY_LIVE', '862c338244762dc3a4da75fd09581256763e67ceea82719cde71a78dbaff7dbe'),
    ('wb_mart.V_CT_ATTENTION', '0981f4011e972a09a2c8d88708084f2e099519a73110150d2100815a911c40a8'),
    ('wb_mart.V_CT_BOM_CURRENT', 'f4544b5bc7b1b8c663d03e85a117e983027629a07fb85891e86ca61651239bf7'),
    ('wb_mart.V_CT_BUNDLE_STATUS', '0616fb11099a0bf423b4958ca9fd7023e05f622c892dd59e45ec5e0a2d1f791c'),
    ('wb_mart.V_CT_CASH_CONVERSION', '81df431f0ebb575da881a0fb683ccdb0f4963ca0c756efb3ecc9777abaffc400'),
    ('wb_mart.V_CT_FRESHNESS', '6b72ad35986a3516c16b42eff683816ae09bbd0d7dc78971aa32c724a1ac90f6'),
    ('wb_mart.V_CT_HAND_CREAM_CONTROL', '5d94a8edc832b924933800c7c39a62c06d91e572a19bfaaf95055026974ea1af'),
    ('wb_mart.V_CT_INVENTORY_TRUTH', '1fa64a385578e0a027efad06a2449ab8038f44f92347728a687a5cc6ff23cc90'),
    ('wb_mart.V_CT_INVENTORY_TRUTH_LIVE', '0029e643a7be2947db4aff2c0a87eab9ae8a07845ea3ed00fa39ebd367ada754'),
    ('wb_mart.V_CT_PHYSICAL_DAILY', 'fce607e52bd18d273998c793cb51b77b896e70178ebda94a2b210fc1ec387547'),
    ('wb_mart.V_CT_PLAN_ACTIVE', '86b17141e20564f62986a93001a73360a8bf9b610be90dba13f058ef53c58c50'),
    ('wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY', '2a3461673c670a1c5e52c7cbd420bbb497e14ec06e5aaa3f468da645b593a07f'),
    ('wb_mart.V_CT_SKU_CONTROL', 'ce4b467633ca24e8e1461fa1c5df116c149bf640751f67915ced88279a59d9da'),
    ('wb_mart.V_CT_SUPPLY_FLOW', 'db80522fcfb8dbed049edd0ce5250ec65df23e4a478e39524f46de45a48d986e'),
    ('wb_mart.V_CT_SUPPLY_NEED', '1582847269ff939fc0222918d371cb9ce36528a2d335c6c64e61d5dcc5f544e6')
  ])
),
live AS (
  SELECT CONCAT('wb_mart.', table_name) AS obj, TO_HEX(SHA256(view_definition)) AS h
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.VIEWS` WHERE STARTS_WITH(table_name, 'V_CT_')
  UNION ALL
  SELECT CONCAT('evetis_ops.', table_name), TO_HEX(SHA256(view_definition))
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.INFORMATION_SCHEMA.VIEWS`
)
SELECT p.obj, p.h = l.h AS unchanged, IF(p.h = l.h, 'PASS', 'FAIL') AS status
FROM pins p LEFT JOIN live l USING (obj)
ORDER BY p.obj;

-- @check I36_RECONCILIATION_WITH_CONTROL_TOWER
-- Позиция и её части = V_CT_INVENTORY_TRUTH того же среза. Скорость 30 дней отличается только
-- концом окна: Control Tower — (срез − 30; срез], слой — [sales_as_of − 29; sales_as_of]. Тождество
-- проверяется точно: 30 × (слой − CT) = Σ дней только слоя − Σ дней только CT.
WITH s AS (
  SELECT internal_sku, inventory_as_of_date, sales_as_of, inventory_position_units, warehouse_ff_units,
    marketplace_available_units, in_transit_units, units_per_day_30d
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
ct AS (
  SELECT internal_sku, ct_snapshot_date, sellable_units, ff_total_units, marketplace_units,
    ozon_transit_units + wb_in_transit_units AS transit_units, units_per_day_30d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`
),
p AS (
  SELECT internal_sku, d, units_ordered FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY`
),
r AS (
  SELECT s.*, ct.sellable_units, ct.ff_total_units, ct.marketplace_units, ct.transit_units, ct.ct_snapshot_date,
    ct.units_per_day_30d AS ct_units_per_day_30d,
    (SELECT IFNULL(SUM(p.units_ordered), 0) FROM p WHERE p.internal_sku = s.internal_sku
       AND p.d BETWEEN DATE_SUB(s.sales_as_of, INTERVAL 29 DAY) AND s.sales_as_of
       AND NOT (p.d > DATE_SUB(ct.ct_snapshot_date, INTERVAL 30 DAY) AND p.d <= ct.ct_snapshot_date)) AS only_layer,
    (SELECT IFNULL(SUM(p.units_ordered), 0) FROM p WHERE p.internal_sku = s.internal_sku
       AND p.d > DATE_SUB(ct.ct_snapshot_date, INTERVAL 30 DAY) AND p.d <= ct.ct_snapshot_date
       AND NOT (p.d BETWEEN DATE_SUB(s.sales_as_of, INTERVAL 29 DAY) AND s.sales_as_of)) AS only_ct
  FROM s JOIN ct USING (internal_sku)
)
SELECT internal_sku, inventory_position_units, sellable_units AS ct_sellable_units,
  ROUND(units_per_day_30d, 4) AS layer_v30, ROUND(ct_units_per_day_30d, 4) AS ct_v30, only_layer, only_ct,
  IF(inventory_as_of_date = ct_snapshot_date
     AND inventory_position_units = sellable_units AND warehouse_ff_units = ff_total_units
     AND marketplace_available_units = marketplace_units AND in_transit_units = transit_units
     AND ABS(30 * (units_per_day_30d - ct_units_per_day_30d) - (only_layer - only_ct)) < 1e-6, 'PASS', 'FAIL') AS status
FROM r ORDER BY internal_sku;

-- @check I37_NO_FABRICATED_PLAN
-- С PR-PLAN-1: строки исполнения плана есть только для месяцев окон утверждённых версий
-- (V_SALES_PLAN_APPROVED); модельные, черновые и предложенные версии сюда не попадают.
SELECT COUNT(*) AS n_rows, COUNTIF(a.plan_version IS NULL) AS outside_approved_contract,
  COUNTIF(p.plan_approval_status != 'APPROVED') AS not_approved_rows,
  IF(COUNTIF(a.plan_version IS NULL) + COUNTIF(p.plan_approval_status != 'APPROVED') = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT` p
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_APPROVED` a USING (plan_version, month, marketplace, internal_sku);

-- @check I38_TRAJECTORY_ARITHMETIC
-- Остаток на конец = начало − продажи; пополнение не моделируется; дни месяца — календарные.
SELECT COUNT(*) AS n_rows,
  COUNTIF(ABS(closing_units_unconstrained - (opening_units - sales_units)) > 1e-6) AS bad_closing,
  COUNTIF(replenishment_status != 'REPLENISHMENT_NOT_MODELLED' OR replenishment_units IS NOT NULL) AS replenishment_invented,
  COUNTIF(days_in_month != EXTRACT(DAY FROM LAST_DAY(month)) OR period_days != DATE_DIFF(period_end, period_start, DAY) + 1) AS bad_calendar,
  IF(COUNTIF(ABS(closing_units_unconstrained - (opening_units - sales_units)) > 1e-6)
     + COUNTIF(replenishment_status != 'REPLENISHMENT_NOT_MODELLED' OR replenishment_units IS NOT NULL)
     + COUNTIF(days_in_month != EXTRACT(DAY FROM LAST_DAY(month)) OR period_days != DATE_DIFF(period_end, period_start, DAY) + 1) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT`;

-- @check I39_OWNER_INPUT_VALID
-- Справочники владельца: события плана — из закрытого списка PR-PLAN-1 и ссылаются на версию
-- evetis_ref.PLAN_VERSION (с PR-PLAN-1 утверждается версия, а не план Control Tower); цели из
-- закрытого списка, целевой остаток ≥ 0.
SELECT 'REF_SALES_PLAN_APPROVAL' AS object_name, COUNT(*) AS n_rows,
  COUNTIF(a.approval_status NOT IN ('PROPOSED', 'APPROVED', 'REVOKED', 'WITHDRAWN') OR v.plan_version IS NULL) AS invalid_rows,
  IF(COUNTIF(a.approval_status NOT IN ('PROPOSED', 'APPROVED', 'REVOKED', 'WITHDRAWN') OR v.plan_version IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL` a
LEFT JOIN (SELECT DISTINCT plan_version FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`) v USING (plan_version)
UNION ALL
SELECT 'REF_SKU_INVENTORY_TARGET', COUNT(*),
  COUNTIF(target_type NOT IN ('SEASON_END', 'MANUAL_DATE') OR target_status NOT IN ('ACTIVE', 'CANCELLED') OR target_ending_units < 0),
  IF(COUNTIF(target_type NOT IN ('SEASON_END', 'MANUAL_DATE') OR target_status NOT IN ('ACTIVE', 'CANCELLED') OR target_ending_units < 0) = 0,
     'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_INVENTORY_TARGET`;

-- @check I40_EXTERNAL_SCHEMA_CONTRACT
-- Схемы 15 внешних объектов = sql/promotions/pr_promo4_external_schemas.json (контракт фикстур).
-- Отпечаток: SHA-256 строк «объект.колонка:тип» по объекту и порядку колонок.
WITH c AS (
  SELECT CONCAT('evetis_ref.', table_name) AS obj, column_name, data_type, ordinal_position
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name IN ('CT_ACTUAL_DAILY', 'CT_CONFIG', 'CT_EXPIRY_BATCH', 'CT_INVENTORY_SNAPSHOT_DAILY', 'CT_PLAN_VERSION',
                       'CT_SEASON_PLAN_MONTHLY', 'REF_PRODUCT_MASTER', 'REF_SKU_CHANNEL_MAP')
  UNION ALL
  SELECT CONCAT('evetis_ops.', table_name), column_name, data_type, ordinal_position
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.INFORMATION_SCHEMA.COLUMNS` WHERE table_name = 'OPS_CONFIG'
  UNION ALL
  SELECT CONCAT('wb_mart.', table_name), column_name, data_type, ordinal_position
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name IN ('V_CT_ACTUAL_DAILY', 'V_CT_BOM_CURRENT', 'V_CT_BUNDLE_STATUS', 'V_CT_PHYSICAL_DAILY')
  UNION ALL
  SELECT CONCAT('evetis_mart.', table_name), column_name, data_type, ordinal_position
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name IN ('V_PROMO_ECONOMICS_SCENARIO_CURRENT', 'V_PROMO_OBSERVATION_HISTORY')
)
SELECT COUNT(*) AS columns_live, COUNT(DISTINCT obj) AS objects_live,
  TO_HEX(SHA256(STRING_AGG(CONCAT(obj, '.', column_name, ':', data_type), '\n' ORDER BY obj, ordinal_position))) AS fingerprint,
  IF(TO_HEX(SHA256(STRING_AGG(CONCAT(obj, '.', column_name, ':', data_type), '\n' ORDER BY obj, ordinal_position)))
     = '2c5b708861e11a7fef51a057a4e8cd06e30d2e7c163fdd8eda009c39f2c7b1f4', 'PASS', 'FAIL') AS status
FROM c;

-- @check I41_CONTEXT_TEMPORAL_ALIGNMENT
-- ALIGNED — только при |разрыв| ≤ refresh_sla_hours; вне согласования значения запаса пусты.
SELECT inventory_context_status, COUNT(*) AS scenarios,
  MIN(inventory_temporal_gap_minutes) AS min_gap_min, MAX(inventory_temporal_gap_minutes) AS max_gap_min,
  COUNTIF(inventory_context_status = 'ALIGNED' AND ABS(inventory_temporal_gap_minutes) > alignment_limit_hours * 60) AS aligned_too_far,
  COUNTIF(inventory_context_status != 'ALIGNED'
          AND (inventory_position_units IS NOT NULL OR bundle_technical_assembly_capacity_units IS NOT NULL
               OR sell_by_required_units_per_day IS NOT NULL)) AS values_when_not_aligned,
  IF(COUNTIF(inventory_context_status = 'ALIGNED' AND ABS(inventory_temporal_gap_minutes) > alignment_limit_hours * 60)
     + COUNTIF(inventory_context_status != 'ALIGNED'
               AND (inventory_position_units IS NOT NULL OR bundle_technical_assembly_capacity_units IS NOT NULL
                    OR sell_by_required_units_per_day IS NOT NULL)) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_INVENTORY_CONTEXT_CURRENT`
GROUP BY inventory_context_status;

-- @check I42_AXES_SEPARATE
-- Ось запаса в контексте = цели SKU; ось экономики — PR-PROMO-3; одна не выводится из другой.
WITH t AS (
  SELECT internal_sku, MAX(IF(target_type = 'EXPIRY_SELL_BY', required_inventory_uplift_pct, NULL)) AS sell_by_uplift
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT` GROUP BY internal_sku
)
SELECT COUNT(*) AS aligned_physical_scenarios,
  COUNTIF(c.sell_by_required_inventory_uplift_pct IS DISTINCT FROM t.sell_by_uplift) AS inventory_axis_mismatch,
  IF(COUNTIF(c.sell_by_required_inventory_uplift_pct IS DISTINCT FROM t.sell_by_uplift) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_INVENTORY_CONTEXT_CURRENT` c
LEFT JOIN t USING (internal_sku)
WHERE c.inventory_context_status = 'ALIGNED' AND c.inventory_entity_type = 'PHYSICAL_SKU';
