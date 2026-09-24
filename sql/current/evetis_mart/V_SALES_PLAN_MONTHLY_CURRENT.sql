-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Месячный план продаж и его исполнение. ВТОРОГО ХРАНИЛИЩА ПЛАНА НЕТ: план живёт в
-- Control Tower (evetis_ref.CT_PLAN_VERSION + CT_SEASON_PLAN_MONTHLY, версионирован, есть
-- статус, источник, автор). Здесь добавлено одно — утверждение владельца
-- (evetis_ref.REF_SALES_PLAN_APPROVAL). Без строки APPROVED версия плана — модельный
-- сценарий: плановые числа и метрики исполнения НЕ выводятся (NULL + planning_metrics_status).
--
-- Единицы плана — карточки заказа без отмен (контракт Control Tower: факт = cards_ordered);
-- физические единицы = карточки × число компонентов BOM. Грейн плана — родной:
-- версия × месяц × канал × карточка. Это же ключ будущего моста к вкладу
-- (marketplace, internal_sku) PR-PROMO-3 — формулы экономики здесь нет.
--
-- Календарь — настоящий: плановый период месяца = [max(1-е число, начало горизонта);
-- min(последний день месяца, конец горизонта)], дни считаются датами (28/29/30/31).
-- Факт — до sales_as_of (последний полный день обоих каналов, как в V_SKU_SELL_THROUGH_CURRENT).
-- Прогноз на конец месяца — RUN_RATE_PROJECTION: факт + скорость 30 дн × оставшиеся дни;
-- это не модель спроса. Качество скорости видно: для одиночной карточки — качество окна
-- 30 дней её физического SKU, для набора — не оценивается.
--
-- Грейн: plan_version × month × marketplace × internal_sku. Только ACTIVE-версии Control Tower.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_SALES_PLAN_MONTHLY_CURRENT`
OPTIONS (description = "PR-PROMO-4. Месячный план продаж Control Tower (карточки × канал × SKU) и его исполнение: факт с начала планового периода, остаток, оставшиеся календарные дни, требуемый темп, % исполнения, RUN_RATE_PROJECTION. Плановые числа и метрики выводятся только для версии, утверждённой владельцем в REF_SALES_PLAN_APPROVAL; иначе — NULL и статус PLAN_APPROVAL_NOT_RECORDED.")
AS
WITH v AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION`
  WHERE plan_status = 'ACTIVE'
),
approval AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY plan_version ORDER BY recorded_at DESC) = 1
),
bom AS (
  SELECT card_sku, MAX(component_count) AS component_count
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`
  GROUP BY card_sku
),
plan AS (
  SELECT
    m.plan_version, m.month, UPPER(m.marketplace) AS marketplace, m.internal_sku,
    ANY_VALUE(m.sales_mode) AS sales_mode,
    SUM(m.target_cards) AS target_cards
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN_MONTHLY` m
  JOIN v USING (plan_version)
  GROUP BY m.plan_version, m.month, UPPER(m.marketplace), m.internal_sku
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
    p.*,
    v.plan_name, v.scenario_code, v.plan_status, v.horizon_from, v.horizon_to, v.source_artifact AS plan_source_artifact,
    v.created_by AS plan_created_by, v.created_at AS plan_created_at,
    IFNULL(a.approval_status, 'APPROVAL_NOT_RECORDED') AS plan_approval_status,
    a.approved_by, a.approved_at, a.approval_reference, a.approved_scope,
    IFNULL(b.component_count, 1) AS component_count,
    LAST_DAY(p.month) AS month_end,
    EXTRACT(DAY FROM LAST_DAY(p.month)) AS days_in_month,
    GREATEST(p.month, v.horizon_from) AS plan_period_start,
    LEAST(LAST_DAY(p.month), v.horizon_to) AS plan_period_end,
    s.sales_as_of,
    s.q
  FROM plan p
  JOIN v USING (plan_version)
  LEFT JOIN approval a USING (plan_version)
  LEFT JOIN bom b ON b.card_sku = p.internal_sku
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
    b.plan_approval_status = 'APPROVED' AS approved
  FROM base b
  LEFT JOIN act x USING (plan_version, month, marketplace, internal_sku)
)
SELECT
  m.plan_version,
  m.plan_name,
  m.scenario_code,
  m.plan_status AS ct_plan_status,
  m.plan_source_artifact,
  m.plan_created_by,
  m.plan_created_at,
  m.plan_approval_status,
  m.approved_by,
  m.approved_at,
  m.approval_reference,
  m.approved_scope,
  CASE
    WHEN m.plan_approval_status = 'REVOKED' THEN 'PLAN_APPROVAL_REVOKED'
    WHEN NOT m.approved THEN 'PLAN_APPROVAL_NOT_RECORDED'
    WHEN m.sales_as_of IS NULL THEN 'SALES_FACTS_UNAVAILABLE'
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
