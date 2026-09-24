-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Требуемая скорость распродажи ТОЛЬКО там, где у цели есть происхождение. Целей не
-- выдумываем: нет источника — строки цели нет или статус NO_TARGET.
--
-- Типы целей и происхождение:
--   EXPIRY_DATE     — самая ранняя партия на руках, остаток 0 к дате срока годности
--                     (CT_EXPIRY_BATCH). Математическая граница, НЕ операционная цель.
--   EXPIRY_SELL_BY  — то же к дате «срок − буфер», буфер — утверждённый владельцем
--                     c1_expiry_margin_days (C1 OWNER_CONVENTION 2026-09-12).
--   SEASON_END, MANUAL_DATE — цель владельца из evetis_ref.REF_SKU_INVENTORY_TARGET
--                     (последняя строка target_id, ACTIVE). Сейчас справочник пуст.
-- Цели по покрытию и по избытку запаса не создаются: утверждённого источника нет.
--
-- Формулы (горизонт — дни [inventory_as_of_date; target_date − 1]):
--   required_units_to_sell   = MAX(позиция − целевой остаток, 0)
--   required_units_per_day   = required_units_to_sell / (target_date − inventory_as_of_date)
--   required_inventory_uplift_pct = (required_units_per_day / скорость 30 дн − 1) × 100
-- Это НЕ required_sales_uplift_pct PR-PROMO-3 (экономический break-even) — оси не смешиваются.
--
-- Статусы (порядок проверки): SKU_NOT_IN_INVENTORY · NO_TARGET · INVALID_TARGET ·
--   EXPIRY_LOT_ALLOCATION_UNKNOWN (партий на руках > 1 или ждём партию: единицы по партиям
--   не известны) · TARGET_DATE_NOT_IN_FUTURE · INVENTORY_STALE (свежесть V_CT_FRESHNESS —
--   считать как текущее нельзя) · NO_ACCELERATION_REQUIRED (позиция ≤ цели, uplift = 0) ·
--   VELOCITY_UNAVAILABLE · CURRENT_VELOCITY_ZERO (единицы в день есть, процента нет) · COMPUTED.
-- Отрицательный uplift не обнуляется: текущей скорости хватает с запасом.
--
-- Грейн: internal_sku × target_type × target_key.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_INVENTORY_TARGET_CURRENT`
OPTIONS (description = "PR-PROMO-4. Требуемая скорость распродажи к цели с явным происхождением: срок годности (математическая граница), срок минус утверждённый буфер C1, цели владельца из REF_SKU_INVENTORY_TARGET. Требуемые единицы, единицы в день, разрыв и required_inventory_uplift_pct против скорости 30 дней; при устаревшем запасе не считается. Не смешивается с экономическим required_sales_uplift_pct.")
AS
WITH owner_targets AS (
  SELECT internal_sku, ARRAY_AGG(o) AS targets
  FROM (
    SELECT *
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_INVENTORY_TARGET`
    QUALIFY ROW_NUMBER() OVER (PARTITION BY target_id ORDER BY recorded_at DESC) = 1
  ) o
  WHERE o.target_status = 'ACTIVE'
  GROUP BY internal_sku
),
sx AS (
  -- Одно чтение V_SKU_SELL_THROUGH_CURRENT (представления раскрываются на каждую ссылку).
  -- FULL JOIN: цель владельца для SKU, которого нет в запасе, остаётся видна.
  SELECT
    COALESCE(s.internal_sku, o.internal_sku) AS internal_sku,
    s.internal_sku IS NOT NULL AS sku_in_inventory,
    s,
    o.targets AS owner
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT` s
  FULL JOIN owner_targets o ON o.internal_sku = s.internal_sku
),
tg AS (
  SELECT
    sx.internal_sku, sx.sku_in_inventory, sx.s,
    tt.target_type, tt.target_key, tt.target_date, tt.target_ending_units, tt.target_basis, tt.is_operational_target,
    tt.target_provenance
  FROM sx
  CROSS JOIN UNNEST(ARRAY_CONCAT(
    IF(sx.sku_in_inventory, [
      STRUCT('EXPIRY_DATE' AS target_type,
             CONCAT('EXPIRY_DATE|', IFNULL(sx.s.expiry_batch_id, '-')) AS target_key,
             sx.s.earliest_expiry_date AS target_date,
             0 AS target_ending_units,
             'MATHEMATICAL_TO_EXPIRY' AS target_basis,
             FALSE AS is_operational_target,
             IF(sx.s.expiry_batch_id IS NULL, NULL,
                FORMAT('evetis_ref.CT_EXPIRY_BATCH %s: %s, confidence %s', sx.s.expiry_batch_id, sx.s.expiry_source,
                       sx.s.expiry_confidence)) AS target_provenance),
      STRUCT('EXPIRY_SELL_BY', CONCAT('EXPIRY_SELL_BY|', IFNULL(sx.s.expiry_batch_id, '-')), sx.s.sell_by_date, 0,
             'OWNER_CONVENTION_C1_EXPIRY_MARGIN', TRUE,
             IF(sx.s.expiry_batch_id IS NULL OR sx.s.sell_by_margin_days IS NULL, NULL,
                FORMAT('evetis_ref.CT_EXPIRY_BATCH %s: %s, confidence %s − %d дн. (%s)', sx.s.expiry_batch_id,
                       sx.s.expiry_source, sx.s.expiry_confidence, sx.s.sell_by_margin_days, sx.s.sell_by_margin_source)))
    ], []),
    ARRAY(
      SELECT AS STRUCT
        o.target_type,
        CONCAT(o.target_type, '|', o.target_id),
        o.target_date,
        o.target_ending_units,
        'OWNER_TARGET',
        TRUE,
        FORMAT('evetis_ref.REF_SKU_INVENTORY_TARGET %s: approved_by %s at %t%s', o.target_id, o.approved_by,
               o.approved_at, IFNULL(CONCAT(', ref ', o.approval_reference), ''))
      FROM UNNEST(IFNULL(sx.owner, [])) o
    ))) AS tt
),
j AS (
  SELECT
    internal_sku, sku_in_inventory, target_type, target_key, target_date, target_ending_units, target_basis,
    is_operational_target, target_provenance,
    s.product_name, s.inventory_as_of_date, s.inventory_as_of, s.inventory_freshness_status, s.inventory_freshness_reason,
    s.inventory_position_units, s.expiry_evidence_status, s.expiry_lot_status, s.sales_as_of,
    s.reference_velocity_window_days, s.reference_units_per_day AS current_units_per_day,
    s.reference_velocity_quality AS current_velocity_quality,
    DATE_DIFF(target_date, s.inventory_as_of_date, DAY) AS days_until_target
  FROM tg
),
st AS (
  SELECT
    j.*,
    CASE
      WHEN NOT sku_in_inventory THEN 'SKU_NOT_IN_INVENTORY'
      WHEN target_date IS NULL THEN 'NO_TARGET'
      WHEN target_basis = 'OWNER_TARGET'
           AND (target_type NOT IN ('SEASON_END', 'MANUAL_DATE') OR target_ending_units IS NULL OR target_ending_units < 0)
        THEN 'INVALID_TARGET'
      WHEN target_type IN ('EXPIRY_DATE', 'EXPIRY_SELL_BY') AND expiry_lot_status != 'SINGLE_LOT'
        THEN 'EXPIRY_LOT_ALLOCATION_UNKNOWN'
      WHEN target_date <= inventory_as_of_date THEN 'TARGET_DATE_NOT_IN_FUTURE'
      WHEN inventory_freshness_status != 'FRESH' THEN 'INVENTORY_STALE'
      WHEN inventory_position_units <= target_ending_units THEN 'NO_ACCELERATION_REQUIRED'
      WHEN current_units_per_day IS NULL THEN 'VELOCITY_UNAVAILABLE'
      WHEN current_units_per_day = 0 THEN 'CURRENT_VELOCITY_ZERO'
      ELSE 'COMPUTED'
    END AS target_status
  FROM j
)
SELECT
  internal_sku,
  product_name,
  target_type,
  target_key,
  target_basis,
  is_operational_target,
  target_provenance,
  target_date,
  target_ending_units,
  target_status,
  inventory_as_of_date,
  inventory_as_of,
  inventory_freshness_status,
  inventory_freshness_reason,
  inventory_position_units,
  expiry_evidence_status,
  expiry_lot_status,
  IF(target_status IN ('SKU_NOT_IN_INVENTORY', 'NO_TARGET', 'INVALID_TARGET'), NULL, days_until_target) AS days_until_target,
  IF(target_status IN ('COMPUTED', 'CURRENT_VELOCITY_ZERO', 'VELOCITY_UNAVAILABLE', 'NO_ACCELERATION_REQUIRED'),
     GREATEST(inventory_position_units - target_ending_units, 0), NULL) AS required_units_to_sell,
  IF(target_status IN ('COMPUTED', 'CURRENT_VELOCITY_ZERO', 'VELOCITY_UNAVAILABLE', 'NO_ACCELERATION_REQUIRED'),
     GREATEST(inventory_position_units - target_ending_units, 0) / days_until_target, NULL) AS required_units_per_day,
  sales_as_of,
  reference_velocity_window_days AS current_velocity_window_days,
  current_units_per_day,
  current_velocity_quality,
  IF(target_status IN ('COMPUTED', 'CURRENT_VELOCITY_ZERO', 'NO_ACCELERATION_REQUIRED'),
     GREATEST(inventory_position_units - target_ending_units, 0) / days_until_target - current_units_per_day, NULL)
    AS velocity_gap_units_per_day,
  CASE target_status
    WHEN 'COMPUTED' THEN
      (GREATEST(inventory_position_units - target_ending_units, 0) / days_until_target / current_units_per_day - 1) * 100
    WHEN 'NO_ACCELERATION_REQUIRED' THEN 0
  END AS required_inventory_uplift_pct,
  CASE target_status
    WHEN 'COMPUTED' THEN 'required_units_per_day / current_units_per_day − 1; < 0 — текущей скорости хватает с запасом'
    WHEN 'NO_ACCELERATION_REQUIRED' THEN 'позиция не выше целевого остатка — ускорение распродажи не нужно'
    WHEN 'CURRENT_VELOCITY_ZERO' THEN 'текущая скорость 0 — процент не определён, см. required_units_per_day'
    ELSE NULL
  END AS uplift_interpretation,
  IF(target_status = 'COMPUTED', current_units_per_day * days_until_target, NULL) AS run_rate_units_by_target,
  IF(target_status = 'COMPUTED',
     GREATEST(inventory_position_units - current_units_per_day * days_until_target, 0), NULL) AS run_rate_ending_units,
  IF(target_status = 'COMPUTED', 'RUN_RATE_PROJECTION: позиция − скорость 30 дн × дни до цели; не прогноз', NULL)
    AS run_rate_method
FROM st
