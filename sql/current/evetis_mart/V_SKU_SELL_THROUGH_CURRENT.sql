-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_SKU_SELL_THROUGH_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- Текущие факты по физическому SKU: запас, наблюдаемая скорость продаж, покрытие, срок
-- годности. Решений, целей и порогов здесь нет — только факты и их качество.
--
-- ЗАПАС — последний день V_INVENTORY_POSITION_HISTORY (текущее выводится из истории).
-- Свежесть — по контракту Control Tower: сроки V_CT_FRESHNESS (WB/Ozon остатки 1 день, срез
-- ФФ владельца 7 дней) и CT_CONFIG.refresh_sla_hours для самого среза, применённые к датам
-- ВНУТРИ среза (что реально легло в позицию). FRESH = все четыре в OK. Паритет с
-- V_CT_FRESHNESS — DQ I05.
--
-- СОБЫТИЕ ПРОДАЖ — контракт Control Tower: единицы заказов без отмен, по дате заказа,
-- разложенные по BOM в физические единицы, WB + Ozon (wb_mart.V_CT_PHYSICAL_DAILY).
-- «Заказ — сигнал спроса сегодня»: он же списывает единицу с полки площадки, и в тех же
-- единицах записан план (карточки без отмен). Выкупы — справочно, окно 30 дней.
-- Это наблюдаемая скорость продаж, а не спрос: при нулевом наличии спрос неизвестен.
--
-- ОКНА — буквально N суток, заканчиваются последним ПОЛНЫМ днём обоих каналов:
--   sales_as_of = LEAST(последний день WB, последний день Ozon в CT_ACTUAL_DAILY,
--                       дата пересборки CT_ACTUAL_DAILY − 1).
--   Окно N = [sales_as_of − N + 1; sales_as_of], скорость = единицы / N. Ни одно окно не
--   объявлено «плановым»: опорное окно для покрытия и целей — 30 дней, как в Control Tower.
--
-- КАЧЕСТВО СКОРОСТИ (порядок проверки):
--   UNKNOWN                          — факты продаж недоступны
--   INSUFFICIENT_HISTORY             — первого заказа нет или он позже начала окна
--   STOCKOUT_CONSTRAINED             — в окне был наблюдаемый день с нулём на полке канала,
--                                      где у SKU есть текущая карточка
--   AVAILABILITY_NOT_FULLY_OBSERVED  — наличие видно не за все дни окна (история с 10.09.2026)
--   NO_SALES_WITH_STOCK              — все дни наблюдены, полка не пуста, продаж ноль
--   NORMAL
-- Спрос статистически не восстанавливается.
--
-- ПОКРЫТИЕ = позиция запаса / скорость окна. Ноль скорости не превращается в ноль дней:
--   позиция 0, скорость > 0 → 0 (ZERO_INVENTORY); позиция > 0, скорость 0 → NULL (NO_VELOCITY);
--   обе 0 → NULL (NO_INVENTORY_NO_VELOCITY); скорость неизвестна → NULL (VELOCITY_UNAVAILABLE).
--
-- СРОК ГОДНОСТИ — evetis_ref.CT_EXPIRY_BATCH по партиям: самая ранняя партия на руках.
-- Иерархия доказательности: явный срок владельца → дата производства + срок хранения →
-- дата производства выведена (импорт − 70 дней) + срок хранения → нет данных.
-- Буфер продажи до срока — только утверждённый: C1 OWNER_CONVENTION 2026-09-12,
-- evetis_ops.OPS_CONFIG.c1_expiry_margin_days. Своего буфера нет.
--
-- План запроса: каждое звено цепочки читается ОДИН раз (BigQuery раскрывает CTE и представления
-- на каждую ссылку, иначе составные запросы упираются в предел планировщика). История запаса
-- сворачивается по SKU в одном проходе: последняя строка + массив дней наличия.
--
-- Грейн: internal_sku (физические SKU последнего дня истории запаса).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`
OPTIONS (description = "PR-PROMO-4. Текущие факты по физическому SKU: позиция запаса из истории Control Tower, свежесть по срокам Control Tower, скорость продаж (заказы без отмен, физ. единицы, WB+Ozon) за 7/14/30/60/90 полных суток с качеством окна, покрытие с явными статусами нуля, срок годности по партиям и утверждённый буфер C1. Без целей, решений и порогов.")
AS
WITH hs AS (
  -- Один проход по истории: последняя строка SKU и дни наличия по каналам.
  SELECT
    internal_sku,
    ARRAY_AGG(x ORDER BY snapshot_date DESC LIMIT 1)[OFFSET(0)] AS l,
    ARRAY_AGG(STRUCT(
      snapshot_date AS day,
      (listed_on_wb AND wb_available_units = 0) AS wb_zero,
      (listed_on_ozon AND ozon_available_units = 0) AS ozon_zero)) AS days
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY` x
  GROUP BY internal_sku
),
sales AS (
  -- Один проход по физическим продажам: первая дата заказа и массив дней с продажами.
  SELECT
    internal_sku,
    MIN(IF(units_ordered > 0, d, NULL)) AS first_order_date,
    ARRAY_AGG(STRUCT(d, units_ordered, units_sold)) AS days
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PHYSICAL_DAILY`
  GROUP BY internal_sku
),
asof AS (
  -- Сроки годности данных — те же, что в wb_mart.V_CT_FRESHNESS.sla_days (WB_STOCK 1,
  -- OZON_STOCK 1, FF_STOCK 7); окно пересборки среза — CT_CONFIG.refresh_sla_hours. Само
  -- V_CT_FRESHNESS не читается: оно тяжёлое и раскрывается при каждом обращении. Совпадение
  -- статусов с ним проверяет DQ I05 — расхождение порогов провалит проверку, а не уйдёт в дрейф.
  SELECT
    f.*,
    LEAST(f.wb_sales_as_of, f.ozon_sales_as_of, DATE_SUB(DATE(f.ct_actuals_refreshed_at), INTERVAL 1 DAY)) AS sales_as_of,
    (SELECT SAFE_CAST(MAX(config_value) AS INT64) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG`
      WHERE config_key = 'refresh_sla_hours') AS ct_refresh_sla_hours,
    1 AS wb_stock_sla_days,
    1 AS ozon_stock_sla_days,
    7 AS ff_stock_sla_days,
    (SELECT SAFE_CAST(MAX(config_value) AS INT64) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
      WHERE config_key = 'c1_expiry_margin_days') AS sell_by_margin_days
  FROM (
    SELECT
      MAX(IF(marketplace = 'WB', d, NULL)) AS wb_sales_as_of,
      MAX(IF(marketplace = 'OZON', d, NULL)) AS ozon_sales_as_of,
      MAX(refreshed_at) AS ct_actuals_refreshed_at
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`
  ) f
),
batches AS (
  SELECT internal_sku,
    COUNTIF(NOT is_inbound) AS lots_on_hand,
    COUNTIF(is_inbound) AS lots_inbound,
    ARRAY_AGG(IF(NOT is_inbound, STRUCT(expiry_date, expiry_source, expiry_confidence, batch_id, manufacture_date, shelf_life_months), NULL)
              IGNORE NULLS ORDER BY expiry_date, batch_id LIMIT 1)[SAFE_OFFSET(0)] AS e
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_EXPIRY_BATCH`
  GROUP BY internal_sku
),
j AS (
  -- Только SKU последнего дня истории (как V_CT_INVENTORY_TRUTH: последняя партиция среза).
  SELECT
    h.l, h.days AS avail_days, s.first_order_date, s.days AS sale_days, a, b.lots_on_hand, b.lots_inbound, b.e
  FROM hs h
  CROSS JOIN asof a
  LEFT JOIN sales s ON s.internal_sku = h.internal_sku
  LEFT JOIN batches b ON b.internal_sku = h.internal_sku
  WHERE TRUE
  QUALIFY h.l.snapshot_date = MAX(h.l.snapshot_date) OVER ()
),
w AS (
  -- Окна N суток, заканчивающиеся sales_as_of: продажи, дни наблюдения наличия, дни нуля на полке.
  SELECT
    j.*,
    ARRAY(
      SELECT AS STRUCT
        n,
        DATE_SUB(j.a.sales_as_of, INTERVAL n - 1 DAY) AS window_start,
        IF(j.a.sales_as_of IS NULL, NULL,
           (SELECT IFNULL(SUM(x.units_ordered), 0) FROM UNNEST(j.sale_days) x
             WHERE x.d <= j.a.sales_as_of AND x.d > DATE_SUB(j.a.sales_as_of, INTERVAL n DAY))) AS units_ordered,
        IF(j.a.sales_as_of IS NULL, NULL,
           (SELECT IFNULL(SUM(x.units_sold), 0) FROM UNNEST(j.sale_days) x
             WHERE x.d <= j.a.sales_as_of AND x.d > DATE_SUB(j.a.sales_as_of, INTERVAL n DAY))) AS units_sold,
        (SELECT COUNT(*) FROM UNNEST(j.avail_days) y
          WHERE y.day <= j.a.sales_as_of AND y.day > DATE_SUB(j.a.sales_as_of, INTERVAL n DAY)) AS observed_days,
        (SELECT COUNTIF(y.wb_zero OR y.ozon_zero) FROM UNNEST(j.avail_days) y
          WHERE y.day <= j.a.sales_as_of AND y.day > DATE_SUB(j.a.sales_as_of, INTERVAL n DAY)) AS stockout_days,
        (SELECT COUNTIF(y.wb_zero) FROM UNNEST(j.avail_days) y
          WHERE y.day <= j.a.sales_as_of AND y.day > DATE_SUB(j.a.sales_as_of, INTERVAL n DAY)) AS wb_stockout_days,
        (SELECT COUNTIF(y.ozon_zero) FROM UNNEST(j.avail_days) y
          WHERE y.day <= j.a.sales_as_of AND y.day > DATE_SUB(j.a.sales_as_of, INTERVAL n DAY)) AS ozon_stockout_days
      FROM UNNEST([7, 14, 30, 60, 90]) AS n
      ORDER BY n
    ) AS wins
  FROM j
),
q AS (
  -- Качество окна и покрытие. Позиция 0 при скорости > 0 → 0 дней; скорость 0 → NULL, не ноль.
  SELECT
    w.* EXCEPT (wins),
    ARRAY(
      SELECT AS STRUCT
        v.*,
        IF(v.units_ordered IS NULL, NULL, v.units_ordered / v.n) AS units_per_day,
        CASE
          WHEN v.units_ordered IS NULL THEN 'UNKNOWN'
          WHEN w.first_order_date IS NULL OR w.first_order_date > v.window_start THEN 'INSUFFICIENT_HISTORY'
          WHEN v.stockout_days > 0 THEN 'STOCKOUT_CONSTRAINED'
          WHEN v.observed_days < v.n THEN 'AVAILABILITY_NOT_FULLY_OBSERVED'
          WHEN v.units_ordered = 0 THEN 'NO_SALES_WITH_STOCK'
          ELSE 'NORMAL'
        END AS velocity_quality,
        CASE
          WHEN w.l.inventory_position_units IS NULL OR v.units_ordered IS NULL THEN NULL
          WHEN v.units_ordered > 0 THEN w.l.inventory_position_units / (v.units_ordered / v.n)
          ELSE NULL
        END AS cover_days,
        CASE
          WHEN w.l.inventory_position_units IS NULL THEN 'INVENTORY_UNAVAILABLE'
          WHEN v.units_ordered IS NULL THEN 'VELOCITY_UNAVAILABLE'
          WHEN v.units_ordered > 0 AND w.l.inventory_position_units = 0 THEN 'ZERO_INVENTORY'
          WHEN v.units_ordered > 0 THEN 'COMPUTED'
          WHEN w.l.inventory_position_units > 0 THEN 'NO_VELOCITY'
          ELSE 'NO_INVENTORY_NO_VELOCITY'
        END AS cover_status
      FROM UNNEST(w.wins) v
      ORDER BY v.n
    ) AS wq,
    -- Свежесть на уровне источника, как в V_CT_FRESHNESS: отсутствие SKU в выгрузке канала —
    -- ноль на полке, а не устаревшие данные.
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(w.l.inventory_as_of) OVER (), HOUR) AS ct_snapshot_age_hours,
    DATE_DIFF(CURRENT_DATE(), MAX(w.l.wb_stock_as_of) OVER (), DAY) AS wb_stock_age_days,
    DATE_DIFF(CURRENT_DATE(), MAX(w.l.ozon_stock_as_of) OVER (), DAY) AS ozon_stock_age_days,
    DATE_DIFF(CURRENT_DATE(), MAX(w.l.ff_anchor_date) OVER (), DAY) AS ff_anchor_age_days_now
  FROM w
),
f AS (
  SELECT
    q.*,
    IF(q.ct_snapshot_age_hours <= q.a.ct_refresh_sla_hours, 'OK', 'STALE') AS ct_inventory_freshness,
    IF(q.wb_stock_age_days <= q.a.wb_stock_sla_days, 'OK', 'STALE') AS wb_stock_freshness,
    IF(q.ozon_stock_age_days <= q.a.ozon_stock_sla_days, 'OK', 'STALE') AS ozon_stock_freshness,
    IF(q.ff_anchor_age_days_now <= q.a.ff_stock_sla_days, 'OK', 'STALE') AS ff_stock_freshness
  FROM q
)
SELECT
  f.l.internal_sku,
  f.l.product_name,
  f.l.product_line,
  f.l.mapping_status,
  f.l.wb_nm_ids,
  f.l.wb_vendor_codes,
  f.l.ozon_skus,
  f.l.ozon_offer_ids,
  f.l.listed_on_wb,
  f.l.listed_on_ozon,
  -- Запас
  f.l.snapshot_date AS inventory_as_of_date,
  f.l.inventory_as_of,
  f.l.warehouse_ff_units,
  f.l.wb_available_units,
  f.l.ozon_available_units,
  f.l.fbs_units,
  f.l.marketplace_available_units,
  f.l.wb_in_transit_units,
  f.l.ozon_in_transit_units,
  f.l.in_transit_units,
  f.l.inventory_position_units,
  f.l.marketplace_units_inside_bundle_cards,
  f.l.reserved_in_delivery_units,
  f.l.unavailable_claimed_units,
  f.l.inbound_not_received_units,
  f.l.inbound_eta,
  f.l.warehouse_ff_scope,
  f.l.marketplace_available_scope,
  f.l.in_transit_scope,
  f.l.inventory_position_scope,
  -- Свежесть
  f.l.ff_anchor_date,
  f.l.ff_anchor_age_days,
  f.l.wb_stock_as_of,
  f.l.ozon_stock_as_of,
  f.ct_snapshot_age_hours,
  f.wb_stock_age_days,
  f.ozon_stock_age_days,
  f.ff_anchor_age_days_now,
  IFNULL(f.ct_inventory_freshness, 'STALE') AS ct_inventory_freshness,
  IFNULL(f.wb_stock_freshness, 'STALE') AS wb_stock_freshness,
  IFNULL(f.ozon_stock_freshness, 'STALE') AS ozon_stock_freshness,
  IFNULL(f.ff_stock_freshness, 'STALE') AS ff_stock_freshness,
  IF(f.ct_inventory_freshness = 'OK' AND f.wb_stock_freshness = 'OK' AND f.ozon_stock_freshness = 'OK'
     AND f.ff_stock_freshness = 'OK', 'FRESH', 'STALE') AS inventory_freshness_status,
  NULLIF(ARRAY_TO_STRING([
    IF(IFNULL(f.ct_inventory_freshness, 'STALE') != 'OK',
       FORMAT('CT_INVENTORY=STALE(age %sh, SLA %sh)', IFNULL(CAST(f.ct_snapshot_age_hours AS STRING), '?'),
              IFNULL(CAST(f.a.ct_refresh_sla_hours AS STRING), '?')), NULL),
    IF(IFNULL(f.wb_stock_freshness, 'STALE') != 'OK',
       FORMAT('WB_STOCK=STALE(age %sd, SLA %dd)', IFNULL(CAST(f.wb_stock_age_days AS STRING), '?'), f.a.wb_stock_sla_days), NULL),
    IF(IFNULL(f.ozon_stock_freshness, 'STALE') != 'OK',
       FORMAT('OZON_STOCK=STALE(age %sd, SLA %dd)', IFNULL(CAST(f.ozon_stock_age_days AS STRING), '?'), f.a.ozon_stock_sla_days), NULL),
    IF(IFNULL(f.ff_stock_freshness, 'STALE') != 'OK',
       FORMAT('FF_STOCK=STALE(age %sd, SLA %dd)', IFNULL(CAST(f.ff_anchor_age_days_now AS STRING), '?'), f.a.ff_stock_sla_days), NULL)
  ], '; '), '') AS inventory_freshness_reason,
  'CT snapshot fields; SLA = wb_mart.V_CT_FRESHNESS.sla_days, CT_CONFIG.refresh_sla_hours' AS freshness_contract,
  -- Продажи
  'UNITS_ORDERED_NET_OF_CANCELLATIONS_PHYSICAL_BOM_WB_PLUS_OZON' AS sales_event_contract,
  f.a.sales_as_of,
  f.a.wb_sales_as_of,
  f.a.ozon_sales_as_of,
  f.a.ct_actuals_refreshed_at,
  f.first_order_date,
  f.wq[OFFSET(0)].units_ordered AS units_ordered_7d,
  f.wq[OFFSET(1)].units_ordered AS units_ordered_14d,
  f.wq[OFFSET(2)].units_ordered AS units_ordered_30d,
  f.wq[OFFSET(3)].units_ordered AS units_ordered_60d,
  f.wq[OFFSET(4)].units_ordered AS units_ordered_90d,
  f.wq[OFFSET(0)].units_per_day AS units_per_day_7d,
  f.wq[OFFSET(1)].units_per_day AS units_per_day_14d,
  f.wq[OFFSET(2)].units_per_day AS units_per_day_30d,
  f.wq[OFFSET(3)].units_per_day AS units_per_day_60d,
  f.wq[OFFSET(4)].units_per_day AS units_per_day_90d,
  f.wq[OFFSET(0)].velocity_quality AS velocity_quality_7d,
  f.wq[OFFSET(1)].velocity_quality AS velocity_quality_14d,
  f.wq[OFFSET(2)].velocity_quality AS velocity_quality_30d,
  f.wq[OFFSET(3)].velocity_quality AS velocity_quality_60d,
  f.wq[OFFSET(4)].velocity_quality AS velocity_quality_90d,
  f.wq[OFFSET(0)].observed_days AS availability_observed_days_7d,
  f.wq[OFFSET(1)].observed_days AS availability_observed_days_14d,
  f.wq[OFFSET(2)].observed_days AS availability_observed_days_30d,
  f.wq[OFFSET(3)].observed_days AS availability_observed_days_60d,
  f.wq[OFFSET(4)].observed_days AS availability_observed_days_90d,
  f.wq[OFFSET(0)].stockout_days AS stockout_days_7d,
  f.wq[OFFSET(1)].stockout_days AS stockout_days_14d,
  f.wq[OFFSET(2)].stockout_days AS stockout_days_30d,
  f.wq[OFFSET(3)].stockout_days AS stockout_days_60d,
  f.wq[OFFSET(4)].stockout_days AS stockout_days_90d,
  f.wq[OFFSET(2)].wb_stockout_days AS wb_stockout_days_30d,
  f.wq[OFFSET(2)].ozon_stockout_days AS ozon_stockout_days_30d,
  SAFE_DIVIDE(f.wq[OFFSET(0)].units_per_day, f.wq[OFFSET(4)].units_per_day) AS velocity_ratio_7d_to_90d,
  SAFE_DIVIDE(f.wq[OFFSET(2)].units_per_day, f.wq[OFFSET(4)].units_per_day) AS velocity_ratio_30d_to_90d,
  f.wq[OFFSET(2)].units_sold AS units_sold_30d,
  IF(f.wq[OFFSET(2)].units_sold IS NULL, NULL, f.wq[OFFSET(2)].units_sold / 30) AS units_sold_per_day_30d,
  30 AS reference_velocity_window_days,
  f.wq[OFFSET(2)].units_per_day AS reference_units_per_day,
  f.wq[OFFSET(2)].velocity_quality AS reference_velocity_quality,
  -- Покрытие (числитель — позиция запаса)
  'INVENTORY_POSITION_UNITS' AS cover_numerator,
  f.wq[OFFSET(0)].cover_days AS cover_days_7d,
  f.wq[OFFSET(1)].cover_days AS cover_days_14d,
  f.wq[OFFSET(2)].cover_days AS cover_days_30d,
  f.wq[OFFSET(3)].cover_days AS cover_days_60d,
  f.wq[OFFSET(4)].cover_days AS cover_days_90d,
  f.wq[OFFSET(0)].cover_status AS cover_status_7d,
  f.wq[OFFSET(1)].cover_status AS cover_status_14d,
  f.wq[OFFSET(2)].cover_status AS cover_status_30d,
  f.wq[OFFSET(3)].cover_status AS cover_status_60d,
  f.wq[OFFSET(4)].cover_status AS cover_status_90d,
  CASE
    WHEN f.wq[OFFSET(2)].units_per_day IS NULL THEN NULL
    WHEN f.wq[OFFSET(2)].units_per_day > 0 THEN f.l.marketplace_available_units / f.wq[OFFSET(2)].units_per_day
    ELSE NULL
  END AS marketplace_cover_days_30d,
  CASE
    WHEN f.wq[OFFSET(2)].units_per_day IS NULL THEN 'VELOCITY_UNAVAILABLE'
    WHEN f.wq[OFFSET(2)].units_per_day > 0 AND f.l.marketplace_available_units = 0 THEN 'ZERO_INVENTORY'
    WHEN f.wq[OFFSET(2)].units_per_day > 0 THEN 'COMPUTED'
    WHEN f.l.marketplace_available_units > 0 THEN 'NO_VELOCITY'
    ELSE 'NO_INVENTORY_NO_VELOCITY'
  END AS marketplace_cover_status_30d,
  -- Срок годности
  CASE
    WHEN f.e IS NULL THEN 'EXPIRY_UNAVAILABLE'
    WHEN f.e.expiry_source = 'OWNER_FACT_HARD' THEN 'EXPLICIT_EXPIRY_OWNER_FACT'
    WHEN f.e.expiry_source = 'OWNER_FACT_SHELF_LIFE' THEN 'MFG_DATE_PLUS_SHELF_LIFE'
    WHEN f.e.expiry_source = 'INFERENCE_IMPORT_MINUS_70D' THEN 'MFG_DATE_INFERRED_PLUS_SHELF_LIFE'
    ELSE 'UNRECOGNISED_EXPIRY_SOURCE'
  END AS expiry_evidence_status,
  f.e.expiry_source,
  f.e.expiry_confidence,
  f.e.batch_id AS expiry_batch_id,
  f.e.manufacture_date AS expiry_batch_manufacture_date,
  f.e.shelf_life_months AS expiry_batch_shelf_life_months,
  IFNULL(f.lots_on_hand, 0) AS expiry_lots_on_hand,
  IFNULL(f.lots_inbound, 0) AS expiry_lots_inbound,
  CASE
    WHEN f.e IS NULL THEN 'NO_LOT_DATA'
    WHEN f.lots_on_hand = 1 AND f.lots_inbound = 0 THEN 'SINGLE_LOT'
    WHEN f.lots_on_hand = 1 THEN 'SINGLE_LOT_PLUS_INBOUND_LOT'
    ELSE 'MULTIPLE_LOTS_UNITS_NOT_ALLOCATED'
  END AS expiry_lot_status,
  f.e.expiry_date AS earliest_expiry_date,
  DATE_DIFF(f.e.expiry_date, f.l.snapshot_date, DAY) AS days_to_expiry,
  f.e.expiry_date <= f.l.snapshot_date AS expiry_passed,
  f.a.sell_by_margin_days,
  IF(f.a.sell_by_margin_days IS NULL, NULL, 'evetis_ops.OPS_CONFIG.c1_expiry_margin_days (C1 OWNER_CONVENTION 2026-09-12)')
    AS sell_by_margin_source,
  DATE_SUB(f.e.expiry_date, INTERVAL f.a.sell_by_margin_days DAY) AS sell_by_date,
  DATE_DIFF(DATE_SUB(f.e.expiry_date, INTERVAL f.a.sell_by_margin_days DAY), f.l.snapshot_date, DAY) AS days_to_sell_by,
  -- Существующая классификация Control Tower — политика, не факт
  f.l.ct_policy_status_code,
  f.l.ct_policy_status_reason,
  f.l.ct_policy_status_provenance
FROM f
