-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Исполнение УТВЕРЖДЁННОГО владельцем плана продаж по месяцам.
-- С PR-PLAN-1 источник плана — единственный контракт evetis_mart.V_SALES_PLAN_APPROVED
-- (версии evetis_ref.PLAN_VERSION, утверждение с точным content_sha256 в
-- evetis_ref.REF_SALES_PLAN_APPROVAL, окна действия). Строки есть только для месяцев, в которых
-- утверждённая версия действует; модельный сценарий Control Tower (SET_2026-09-09 и подобные),
-- черновики и предложенные версии сюда не попадают. Пока утверждения нет — представление пусто.
-- Control Tower (V_CT_PLAN_ACTIVE, C1) продолжает читать свой план и не переключается.
--
-- Единицы плана — карточки заказа без отмен (контракт Control Tower: факт = cards_ordered);
-- физические единицы = карточки × сумма количеств в снимке BOM версии (PLAN_BOM_BASIS);
-- набор без снимка BOM — физические единицы NULL (BOM_BASIS_MISSING), не выдумываются.
--
-- Календарь — настоящий: плановый период месяца = [max(1-е число, начало горизонта);
-- min(последний день месяца, конец горизонта)], дни считаются датами (28/29/30/31).
-- Факт — до sales_as_of (последний полный день обоих каналов, как в V_SKU_SELL_THROUGH_CURRENT).
-- Прогноз на конец месяца — RUN_RATE_PROJECTION: факт + скорость 30 дн × оставшиеся дни;
-- это не модель спроса. Качество скорости видно: для одиночной карточки — качество окна
-- 30 дней её физического SKU, для набора — не оценивается.
--
-- Грейн: plan_version × month × marketplace × internal_sku (строки утверждённых окон).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT`
OPTIONS (description = "PR-PROMO-4, источник плана с PR-PLAN-1 — V_SALES_PLAN_APPROVED. Исполнение утверждённого владельцем плана продаж (карточки × канал × SKU): факт с начала планового периода, остаток, оставшиеся календарные дни, требуемый темп, % исполнения, RUN_RATE_PROJECTION. Только месяцы окон действия утверждённых версий; модельные сценарии Control Tower сюда не попадают.")
AS
WITH p AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_APPROVED`
),
bom AS (
  SELECT plan_version, bundle_sku, SUM(component_qty) AS component_count
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_BOM_BASIS`
  GROUP BY plan_version, bundle_sku
),
quality AS (
  -- Одно чтение V_SKU_SELL_THROUGH_CURRENT: и качество окна, и sales_as_of (одинаков во всех строках).
  SELECT internal_sku, velocity_quality_30d, sales_as_of
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
),
sell AS (
  SELECT ANY_VALUE(sales_as_of) AS sales_as_of, ARRAY_AGG(STRUCT(internal_sku, velocity_quality_30d)) AS q
  FROM quality
),
base AS (
  SELECT
    p.plan_version, p.month, p.marketplace, p.internal_sku, p.sales_mode,
    CAST(p.planned_cards AS FLOAT64) AS target_cards,  -- тип колонок витрины PR-PROMO-4 сохраняется (FLOAT64)
    p.plan_name, p.plan_kind, p.method_id, p.horizon_from, p.horizon_to, p.plan_created_by, p.plan_created_at,
    p.version_lifecycle_status, p.effective_from_month, p.effective_to_month_exclusive,
    p.approved_by, p.approved_at, p.approval_reference, p.approved_content_sha256,
    IF(p.sales_mode = 'BUNDLE', b.component_count, 1) AS component_count,
    LAST_DAY(p.month) AS month_end,
    EXTRACT(DAY FROM LAST_DAY(p.month)) AS days_in_month,
    GREATEST(p.month, p.horizon_from) AS plan_period_start,
    LEAST(LAST_DAY(p.month), p.horizon_to) AS plan_period_end,
    s.sales_as_of,
    s.q
  FROM p
  LEFT JOIN bom b ON b.plan_version = p.plan_version AND b.bundle_sku = p.internal_sku
  CROSS JOIN sell s
),
act AS (
  SELECT
    b.plan_version, b.month, b.marketplace, b.internal_sku,
    SUM(IF(d.d BETWEEN b.plan_period_start AND LEAST(b.sales_as_of, b.plan_period_end), d.cards_ordered, 0)) AS actual_cards,
    SUM(IF(d.d > DATE_SUB(b.sales_as_of, INTERVAL 30 DAY) AND d.d <= b.sales_as_of, d.cards_ordered, 0)) / 30 AS cards_per_day_30d
  FROM base b
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` d
    ON UPPER(d.marketplace) = b.marketplace AND d.internal_sku = b.internal_sku
   AND d.d <= b.sales_as_of AND d.d > DATE_SUB(LEAST(b.plan_period_start, DATE_SUB(b.sales_as_of, INTERVAL 29 DAY)), INTERVAL 1 DAY)
  GROUP BY b.plan_version, b.month, b.marketplace, b.internal_sku
),
m AS (
  SELECT
    b.*,
    DATE_DIFF(b.plan_period_end, b.plan_period_start, DAY) + 1 AS plan_days,
    CASE
      WHEN b.sales_as_of IS NULL THEN NULL
      WHEN b.plan_period_end <= b.sales_as_of THEN 'PAST'
      WHEN b.plan_period_start > b.sales_as_of THEN 'FUTURE'
      ELSE 'CURRENT'
    END AS month_position,
    GREATEST(LEAST(DATE_DIFF(b.sales_as_of, b.plan_period_start, DAY) + 1, DATE_DIFF(b.plan_period_end, b.plan_period_start, DAY) + 1), 0)
      AS elapsed_plan_days,
    IF(b.sales_as_of IS NULL, NULL, IFNULL(x.actual_cards, 0)) AS actual_cards_to_date,
    IF(b.sales_as_of IS NULL, NULL, IFNULL(x.cards_per_day_30d, 0)) AS current_cards_per_day_30d,
    TRUE AS approved
  FROM base b
  LEFT JOIN act x USING (plan_version, month, marketplace, internal_sku)
)
SELECT
  m.plan_version,
  m.plan_name,
  m.plan_kind,
  m.method_id,
  m.plan_created_by,
  m.plan_created_at,
  'APPROVED' AS plan_approval_status,
  m.version_lifecycle_status,
  m.approved_by,
  m.approved_at,
  m.approval_reference,
  m.approved_content_sha256,
  m.effective_from_month,
  m.effective_to_month_exclusive,
  CASE
    WHEN m.sales_as_of IS NULL THEN 'SALES_FACTS_UNAVAILABLE'
    WHEN m.component_count IS NULL THEN 'BOM_BASIS_MISSING'
    ELSE 'COMPUTED'
  END AS planning_metrics_status,
  m.month,
  m.month_end,
  m.days_in_month,
  m.plan_period_start,
  m.plan_period_end,
  m.plan_days,
  m.plan_days < m.days_in_month AS is_partial_plan_month,
  m.marketplace,
  m.internal_sku,
  m.sales_mode,
  m.component_count,
  'CARDS_ORDERED_NET_OF_CANCELLATIONS' AS plan_unit_contract,
  IF(m.approved, m.target_cards, NULL) AS planned_cards,
  IF(m.approved, m.target_cards * m.component_count, NULL) AS planned_physical_units,
  m.sales_as_of,
  m.month_position,
  m.elapsed_plan_days,
  m.plan_days - m.elapsed_plan_days AS remaining_calendar_days,
  m.actual_cards_to_date,
  IF(m.approved AND m.sales_as_of IS NOT NULL, GREATEST(m.target_cards - m.actual_cards_to_date, 0), NULL) AS remaining_planned_cards,
  IF(m.approved AND m.sales_as_of IS NOT NULL AND m.plan_days - m.elapsed_plan_days > 0,
     GREATEST(m.target_cards - m.actual_cards_to_date, 0) / (m.plan_days - m.elapsed_plan_days), NULL) AS required_cards_per_day_remaining,
  IF(m.approved AND m.sales_as_of IS NOT NULL AND m.target_cards > 0, m.actual_cards_to_date / m.target_cards * 100, NULL)
    AS plan_attainment_pct,
  m.current_cards_per_day_30d,
  IF(m.approved AND m.sales_as_of IS NOT NULL,
     m.actual_cards_to_date + m.current_cards_per_day_30d * (m.plan_days - m.elapsed_plan_days), NULL) AS run_rate_projection_cards,
  IF(m.approved AND m.sales_as_of IS NOT NULL,
     'RUN_RATE_PROJECTION: факт + скорость 30 дн × оставшиеся дни планового периода; не прогноз', NULL) AS projection_method,
  IF(m.sales_mode = 'BUNDLE', 'NOT_ASSESSED_BUNDLE_CARD',
     (SELECT x.velocity_quality_30d FROM UNNEST(m.q) x WHERE x.internal_sku = m.internal_sku)) AS projection_velocity_quality
FROM m
