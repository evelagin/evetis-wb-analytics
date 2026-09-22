-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.FACT_SKU_DAILY (VIEW)
-- Git-first object (SCALE 1, 2026-09-20): not in production until deployed. Rules:
-- sql/current/README.md. Metadata: MANIFEST.json.
--
-- Нейтральный к площадке суточный факт SKU: зерно = fact_date x marketplace x internal_sku,
-- ровно одна строка на ключ. Объект НЕ считает экономику площадок заново — он только приводит
-- два авторитетных источника к одному контракту:
--   WB   ← wb_mart.SKU_PERFORMANCE_V2_DAILY   (материализованный слой SKU Performance V2)
--   OZON ← ozon_mart.FCT_OZON_SKU_PNL_DAILY   (суточное выражение месячного P&L Ozon)
-- Чтение wb_raw / ozon_raw отсюда запрещено политикой валидатора (EXTERNAL_DATASET_POLICY).
--
-- NULL и 0: 0 — наблюдаемый ноль; NULL — метрика недоступна, не покрыта или не атрибутируется.
--   WB   — NULL вне покрытия источника (заказы до 13.04.2026, финансы вне покрытия, хранение SKU
--          до 01.09.2026); acquiring_rub = NULL: эквайринг входит в marketplace_commission_rub
--          и за всю историю отдельно не выделяется; other_marketplace_costs_rub = NULL: штрафы и
--          удержания — уровня кабинета, на SKU не атрибутируются.
--   OZON — return_qty = NULL: возвраты не загружаются; cogs_rub и contribution_after_cogs_rub =
--          NULL, если себестоимость хотя бы одной реализованной единицы не разрешилась.
--
-- Тождества (обе площадки), контракт V2:
--   marketplace_costs_total_rub = commission + logistics + storage + acquiring + other (NULL → 0)
--   contribution_after_ads_rub  = seller_revenue_rub - marketplace_costs_total_rub
--                                 - advertising_attributed_rub - promotion_billed_rub (NULL → 0)
--   contribution_after_cogs_rub = contribution_after_ads_rub - cogs_rub
--
-- 🔴 promotion_billed_rub (контракт V2, UBR-010, решение владельца 2026-09-22) —
--   фактически НАЧИСЛЕННЫЙ площадкой расход на продвижение с привязкой к SKU, слой L3.
--   Он НЕ уменьшает seller_revenue_rub, НЕ входит в marketplace_costs_total_rub и НЕ
--   смешивается с advertising_attributed_rub: там атрибуция, здесь факт начисления.
--   OZON — ozon_mart.FCT_OZON_SKU_PNL_DAILY.sku_promotion_rub (типы начислений 116, 74, 48).
--   WB   — NULL: на уровне SKU у WB нет начисленного продвижения, только атрибуция; величина
--          «биллинг» существует лишь на уровне кабинета (FIN CONTRACT V2) и на SKU не
--          раскладывается. NULL здесь означает «не покрыто», а не ноль.
--   Дефект, который закрывает V2: в V1 срез OZON вычитал продвижение в
--   contribution_after_cogs_rub, но не в contribution_after_ads_rub — тождество ломалось на
--   71 строке ровно на −10 428,67 ₽. Разбор: docs/ozon/OZON_SKU_PROMOTION_CLASSIFICATION_2026-09-22.md.
-- Всё — до налога, до расходов уровня кабинета/магазина, до фулфилмента и OPEX. Это вклад, не прибыль.
--
-- marketplace_commission_rub — «удержание площадки с цены продажи»:
--   WB   = fin_seller_price_rub - credited_for_goods_rub (нетто возвратов; равно ценовой цепочке:
--          вознаграждение WB + НДС + эквайринг + ПВЗ + округление). НЕ wb_fee_as_reported_rub:
--          тот прибавляет комиссию возвратов с плюсом и тождество вклада не замыкает.
--   OZON = комиссия за продажу (эквайринг — отдельно, в acquiring_rub).
--
-- Базис даты различается по площадкам и метрикам; он назван в fact_date_semantics, а не скрыт.
-- Реклама — АТРИБУЦИЯ на обеих площадках (не биллинг). Налог не моделируется.
-- Порядок развёртывания: ozon_mart.V_OZON_CIS_BUYOUT → ozon_mart.FCT_OZON_SKU_PNL_DAILY →
-- датасет evetis_mart → этот объект.
-- Internal dependencies: none.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY`
OPTIONS (description = "Нейтральный суточный факт SKU (контракт V2), зерно = fact_date x marketplace x internal_sku. VIEW: приводит wb_mart.SKU_PERFORMANCE_V2_DAILY и ozon_mart.FCT_OZON_SKU_PNL_DAILY к одному контракту, экономику площадок заново не считает. 0 = наблюдаемый ноль, NULL = метрика недоступна или не атрибутируется. contribution_after_ads_rub = выручка продавца - расходы площадки - атрибутированная реклама - начисленное продвижение (promotion_billed_rub, слой L3: не уменьшает выручку, не входит в расходы площадки, не смешивается с атрибуцией); contribution_after_cogs_rub = он же - себестоимость. До налога, расходов уровня кабинета, фулфилмента и OPEX: вклад, не прибыль. Реклама - атрибуция, не биллинг. Базис даты различается по площадкам - см. fact_date_semantics.")
AS
WITH pm AS (
  SELECT internal_sku, canonical_product_name, is_bundle
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`),
cur_map AS (
  -- Идентификатор площадки для справки: действующая привязка, а если её нет (SKU снят с площадки) —
  -- последняя историческая. На зерно не влияет: одна строка на marketplace x internal_sku.
  SELECT marketplace, internal_sku,
    ARRAY_AGG(marketplace_sku ORDER BY is_current DESC, valid_from DESC, marketplace_sku LIMIT 1)[OFFSET(0)] marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  GROUP BY 1,2),
wb AS (
  SELECT t.day fact_date, 'WB' marketplace, t.internal_sku, CAST(t.nm_id AS STRING) marketplace_sku,
    t.orders_gross_units orders_qty, t.orders_cancelled_units cancelled_qty,
    t.fin_sale_units sold_qty, t.fin_return_units return_qty,
    t.fin_seller_price_rub seller_revenue_rub,
    t.fin_seller_price_rub - t.credited_for_goods_rub marketplace_commission_rub,
    t.logistics_rub logistics_rub, t.storage_sku_rub storage_rub,
    CAST(NULL AS NUMERIC) acquiring_rub, CAST(NULL AS NUMERIC) other_marketplace_costs_rub,
    t.fin_seller_price_rub - t.credited_for_goods_rub + t.logistics_rub + IFNULL(t.storage_sku_rub, 0) marketplace_costs_total_rub,
    t.ads_attributed_rub advertising_attributed_rub,
    CAST(NULL AS NUMERIC) promotion_billed_rub,
    t.contribution_before_cogs_rub contribution_after_ads_rub,
    t.cogs_rub cogs_rub,
    t.contribution_after_cogs_rub contribution_after_cogs_rub,
    t.contribution_after_cogs_rub IS NOT NULL economics_covered,
    NOT IFNULL(t.finance_is_final, FALSE) OR IFNULL(t.contains_provisional_finance, FALSE) is_provisional,
    'WB_V2: orders = order date; sold, returns, revenue, commission, logistics = finance report date; storage = storage date; ads = ad activity date; cogs = buyout date' fact_date_semantics,
    'wb_mart.SKU_PERFORMANCE_V2_DAILY' source_contract
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.SKU_PERFORMANCE_V2_DAILY` t
  WHERE t.internal_sku IS NOT NULL),
oz AS (
  SELECT d.fact_date, 'OZON' marketplace, d.internal_sku, cm.marketplace_sku,
    d.gross_qty orders_qty, d.cancelled_qty cancelled_qty,
    d.realized_qty sold_qty, CAST(NULL AS INT64) return_qty,
    d.seller_base_revenue_rub seller_revenue_rub,
    d.commission_rub marketplace_commission_rub,
    d.logistics_rub logistics_rub, d.storage_rub storage_rub,
    d.acquiring_rub acquiring_rub, d.other_direct_marketplace_costs_rub other_marketplace_costs_rub,
    d.commission_rub + d.direct_variable_marketplace_costs_rub + d.other_direct_marketplace_costs_rub marketplace_costs_total_rub,
    d.ad_spend_attributed_rub advertising_attributed_rub,
    d.sku_promotion_rub promotion_billed_rub,
    d.seller_base_revenue_rub - d.commission_rub - d.direct_variable_marketplace_costs_rub
      - d.other_direct_marketplace_costs_rub - d.ad_spend_attributed_rub
      - d.sku_promotion_rub contribution_after_ads_rub,
    IF(d.cogs_missing_qty = 0, d.product_cogs_rub, NULL) cogs_rub,
    IF(d.cogs_missing_qty = 0, d.contribution_after_attributed_ads_rub, NULL) contribution_after_cogs_rub,
    d.cogs_missing_qty = 0 AND d.commission_missing_qty = 0
      AND d.buyout_revenue_unproven_qty = 0 economics_covered,
    d.in_transit_qty > 0 is_provisional,
    d.fact_date_semantics fact_date_semantics,
    'ozon_mart.FCT_OZON_SKU_PNL_DAILY' source_contract
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` d
  LEFT JOIN cur_map cm ON cm.marketplace = 'OZON' AND cm.internal_sku = d.internal_sku),
u AS (SELECT * FROM wb UNION ALL SELECT * FROM oz)
SELECT u.fact_date, u.marketplace, u.internal_sku, u.marketplace_sku,
  pm.canonical_product_name product_name, pm.is_bundle is_bundle,
  u.orders_qty, u.cancelled_qty, u.sold_qty, u.return_qty,
  u.seller_revenue_rub, u.marketplace_commission_rub, u.logistics_rub, u.storage_rub,
  u.acquiring_rub, u.other_marketplace_costs_rub, u.marketplace_costs_total_rub,
  u.advertising_attributed_rub, u.promotion_billed_rub,
  u.contribution_after_ads_rub, u.cogs_rub, u.contribution_after_cogs_rub,
  u.economics_covered, u.is_provisional, u.fact_date_semantics, u.source_contract,
  'FACT_SKU_DAILY_V2' contract_version
FROM u LEFT JOIN pm ON pm.internal_sku = u.internal_sku;
