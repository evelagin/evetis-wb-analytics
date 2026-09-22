# Gate 5M — откат развёртывания Ozon mart (2026-09-21)

Готовый к исполнению откат. Порядок строго обратен развёртыванию; каждый оператор
восстанавливает тело, снятое с production ДО развёртывания.

Почему это .md, а не .sql: страж C17 запрещает конкурирующие определения канонических
объектов вне `sql/current/**`. Отдельный .sql-файл пришлось бы вносить в allowlist, и он
начал бы участвовать в графе определений. Здесь те же операторы дословно, но без второго
определения объекта в дереве. Дословные тела также лежат в
`artifacts/gate5m/predeploy/*.sql` (только тела, без CREATE).

## Порядок

| # | объект | действие | восстановить хеш |
| --- | --- | --- | --- |
| 1 | `evetis_mart.FACT_SKU_DAILY` | CREATE OR REPLACE VIEW | `2492833d52ff4ae068856e01953830dea3b3ff0bf623b66a363e48b292223032` |
| 2 | `ozon_mart.FCT_OZON_SKU_PNL_MONTHLY` | CREATE OR REPLACE VIEW | `49b85269eaf1c928049621bcacd4f6a4da37bc9c23271f254c4792e87e441552` |
| 3 | `ozon_mart.FCT_OZON_SKU_PNL_DAILY` | CREATE OR REPLACE VIEW | `f7075d6ce1ddf068925b5ff8ddc75cf9241465006af54a4dba6007fbdbe6eb3b` |
| 4 | `ozon_mart.FCT_OZON_PNL_MONTHLY` | CREATE OR REPLACE VIEW | `edeb911c810e4179258ade32983c3496440f361f6f1d4ec5bf854b09ea227b25` |
| 5 | `ozon_mart.V_OZON_CIS_BUYOUT` | DROP VIEW (объект создан Gate 5M) | `—` |

## Проверка после отката

Для каждого объекта `sha256(view_definition)` обязан совпасть с `before_hash` из
`artifacts/gate5m/deploy_log.json`. Для `V_OZON_CIS_BUYOUT` — объект обязан отсутствовать.

## Операторы

```sql
-- ROLLBACK Gate 5M — выполнять СТРОГО в этом порядке (обратном развёртыванию).
-- Каждый оператор восстанавливает тело, снятое с production ДО развёртывания 2026-09-21.
-- Проверка после отката: sha256(view_definition) обязан совпасть с before_hash из deploy_log.json.

-- evetis_mart.FACT_SKU_DAILY: восстановить before_hash 2492833d52ff4ae068856e01953830dea3b3ff0bf623b66a363e48b292223032
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.FACT_SKU_DAILY`
OPTIONS (description = "Нейтральный суточный факт SKU (контракт V1), зерно = fact_date x marketplace x internal_sku. VIEW: приводит wb_mart.SKU_PERFORMANCE_V2_DAILY и ozon_mart.FCT_OZON_SKU_PNL_DAILY к одному контракту, экономику площадок заново не считает. 0 = наблюдаемый ноль, NULL = метрика недоступна или не атрибутируется. contribution_after_ads_rub = выручка продавца - расходы площадки - атрибутированная реклама; contribution_after_cogs_rub = он же - себестоимость. До налога, расходов уровня кабинета, фулфилмента и OPEX: вклад, не прибыль. Реклама - атрибуция, не биллинг. Базис даты различается по площадкам - см. fact_date_semantics.")
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
    d.seller_base_revenue_rub - d.commission_rub - d.direct_variable_marketplace_costs_rub
      - d.other_direct_marketplace_costs_rub - d.ad_spend_attributed_rub contribution_after_ads_rub,
    IF(d.cogs_missing_qty = 0, d.product_cogs_rub, NULL) cogs_rub,
    IF(d.cogs_missing_qty = 0, d.contribution_after_attributed_ads_rub, NULL) contribution_after_cogs_rub,
    d.cogs_missing_qty = 0 AND d.commission_missing_qty = 0 economics_covered,
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
  u.advertising_attributed_rub, u.contribution_after_ads_rub, u.cogs_rub, u.contribution_after_cogs_rub,
  u.economics_covered, u.is_provisional, u.fact_date_semantics, u.source_contract,
  'FACT_SKU_DAILY_V1' contract_version
FROM u LEFT JOIN pm ON pm.internal_sku = u.internal_sku;

-- ozon_mart.FCT_OZON_SKU_PNL_MONTHLY: восстановить before_hash 49b85269eaf1c928049621bcacd4f6a4da37bc9c23271f254c4792e87e441552
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
OPTIONS (description = "P&L Ozon, зерно = месяц x internal_sku. VIEW, не таблица. Атрибуция: DIRECT_POSTING где finance-строка несёт posting_number (привязка к order_date продажи), DIRECT_SKU где несёт только sku (привязка к дате начисления). REVENUE_PROPORTIONAL_ALL не используется. Расходы уровня магазина на SKU НЕ разносятся, поэтому слоя L4 здесь нет.")
AS
WITH rec_comm AS (
  SELECT * FROM UNNEST([
    STRUCT('0107987069-0115-1' AS posting_number, NUMERIC '428.40' AS c),('0107987069-0116-1',NUMERIC '346.50'),
    ('0111865653-0053-1',NUMERIC '356.16'),('0140207342-0283-1',NUMERIC '446.46'),('0140207342-0294-1',NUMERIC '391.44'),
    ('0140425762-0336-1',NUMERIC '350.28'),('0143652501-0071-1',NUMERIC '428.40'),('0148296041-0008-1',NUMERIC '346.50'),
    ('0148983889-0086-1',NUMERIC '262.92'),('0174671740-0036-1',NUMERIC '206.79'),('0180844433-0032-1',NUMERIC '222.65'),
    ('0184479194-0054-1',NUMERIC '350.28'),('0198758548-0051-1',NUMERIC '382.20'),('0201540221-0097-1',NUMERIC '341.88'),
    ('0231520423-0001-2',NUMERIC '254.94'),('0233071107-0012-1',NUMERIC '276.36'),('52826697-0003-15',NUMERIC '254.94'),
    ('57794512-0006-2',NUMERIC '264.00'),('59699516-0310-4',NUMERIC '439.12'),('69799830-0276-1',NUMERIC '229.40'),
    ('71080184-0023-1',NUMERIC '262.92'),('81535887-0077-1',NUMERIC '350.28'),('90312383-0011-1',NUMERIC '385.56'),
    ('90831504-0160-1',NUMERIC '352.80'),('92234251-0008-1',NUMERIC '254.94'),('98041754-0004-1',NUMERIC '369.60'),
    ('98041754-0006-1',NUMERIC '344.40'),('99543424-0109-1',NUMERIC '350.28'),('99543424-0131-1',NUMERIC '387.20')])),
post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace='OZON' AND m.marketplace_sku=p.sku),
pmap AS (SELECT DISTINCT posting_number, sku, order_date FROM post),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.internal_sku, p.status, p.quantity,
    IFNULL(f.sp_unit, p.price_rub) * p.quantity seller_base,
    IFNULL(-f.comm, IFNULL(rc.c, NUMERIC '0')) commission_known,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.quantity, 0) comm_missing_qty,
    c.u * p.quantity cogs_amt, IF(c.u IS NULL, p.quantity, 0) cogs_missing_qty
  FROM post p LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN rec_comm rc ON rc.posting_number=p.posting_number
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (SELECT m, internal_sku,
    SUM(quantity) gross_qty, SUM(IF(status='delivered', quantity, 0)) realized_qty,
    ROUND(SUM(IF(status='delivered', seller_base, 0)),2) seller_base_revenue_rub,
    ROUND(SUM(IF(status='delivered', cogs_amt, 0)),2) product_cogs_rub,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_known, 0)),2) commission_rub,
    SUM(IF(status='delivered', comm_missing_qty, 0)) comm_missing_qty
  FROM sales GROUP BY 1,2),
dcost_post AS (
  SELECT DATE_TRUNC(pm.order_date, MONTH) m, mp.internal_sku,
    ROUND(SUM(IF(f.type_id IN (32,29,28,98,30,1,59,45,78,9,79), -f.amount_rub, 0)),2) direct_var,
    ROUND(SUM(IF(f.type_id IN (15,71,39,38), -f.amount_rub, 0)),2) other_direct
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN pmap pm ON pm.posting_number=f.posting_number AND pm.sku=f.sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.posting_number IS NOT NULL GROUP BY 1,2),
dcost_sku AS (
  SELECT DATE_TRUNC(f.event_date, MONTH) m, mp.internal_sku,
    ROUND(SUM(IF(f.type_id IN (32,29,28,98,30,1,59,45,78,9,79), -f.amount_rub, 0)),2) direct_var,
    ROUND(SUM(IF(f.type_id IN (15,71,39,38), -f.amount_rub, 0)),2) other_direct
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.sku IS NOT NULL AND f.posting_number IS NULL GROUP BY 1,2),
ads AS (SELECT DATE_TRUNC(a.date, MONTH) m, mp.internal_sku, ROUND(SUM(a.attributed_spend_rub),2) ad_attr
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=a.sku GROUP BY 1,2),
j AS (SELECT COALESCE(s.m, dp.m, ds.m, ads.m) month,
    COALESCE(s.internal_sku, dp.internal_sku, ds.internal_sku, ads.internal_sku) internal_sku,
    IFNULL(s.gross_qty,0) gross_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(s.commission_rub,0) commission_rub, IFNULL(s.comm_missing_qty,0) comm_missing_qty,
    IFNULL(dp.direct_var,0) + IFNULL(ds.direct_var,0) direct_variable_marketplace_costs_rub,
    IFNULL(dp.other_direct,0) + IFNULL(ds.other_direct,0) other_direct_marketplace_costs_rub,
    IFNULL(ads.ad_attr,0) ad_spend_attributed_rub
  FROM s FULL JOIN dcost_post dp USING (m, internal_sku)
         FULL JOIN dcost_sku ds USING (m, internal_sku)
         FULL JOIN ads USING (m, internal_sku))
SELECT j.month, j.internal_sku, pm.canonical_product_name product_name,
  IF(pm.is_bundle, 'BUNDLE', 'SINGLE') product_type,
  j.gross_qty, j.realized_qty, j.seller_base_revenue_rub, j.product_cogs_rub,
  CASE WHEN j.cogs_missing_qty > 0 THEN 'MISSING_BOM_INTERVAL' ELSE 'COVERED' END cogs_status,
  j.commission_rub,
  CASE WHEN j.comm_missing_qty > 0 THEN 'MISSING_SOURCE' ELSE 'COVERED' END commission_status,
  j.direct_variable_marketplace_costs_rub, j.other_direct_marketplace_costs_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub, 2) contribution_before_ads_rub,
  j.ad_spend_attributed_rub,
  ROUND(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
        - j.ad_spend_attributed_rub, 2) contribution_after_attributed_ads_rub,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_before_ads_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
        - j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) margin_after_ads_pct,
  ROUND(SAFE_DIVIDE(j.ad_spend_attributed_rub, NULLIF(j.seller_base_revenue_rub,0))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
        - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub,
        NULLIF(j.seller_base_revenue_rub,0))*100, 4) variable_break_even_drr_pct,
  CASE
    WHEN j.cogs_missing_qty > 0 THEN 'COGS_INCOMPLETE'
    WHEN j.realized_qty = 0 THEN 'INSUFFICIENT_DATA'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub <= 0 THEN 'LOSS_BEFORE_ADS'
    WHEN j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
         - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
         - j.ad_spend_attributed_rub > 0 THEN 'PROFITABLE_AFTER_ADS'
    ELSE 'PROFITABLE_BEFORE_ADS_ONLY' END profitability_status,
  'DIRECT_POSTING+DIRECT_SKU+ACTUAL_AD_ATTRIBUTION' attribution_level,
  CURRENT_TIMESTAMP() computed_at
FROM j LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm
  ON pm.internal_sku = j.internal_sku
WHERE j.month IS NOT NULL AND j.internal_sku IS NOT NULL;

-- ozon_mart.FCT_OZON_SKU_PNL_DAILY: восстановить before_hash f7075d6ce1ddf068925b5ff8ddc75cf9241465006af54a4dba6007fbdbe6eb3b
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`
OPTIONS (description = "Фактическая экономика Ozon, зерно = сутки x internal_sku. VIEW. Суточное выражение семантики FCT_OZON_SKU_PNL_MONTHLY: агрегат до месяца сходится с месячным P&L (деньги <= 0,01 руб., штуки точно). Суммы без ROUND, полная точность NUMERIC. Базис даты смешанный, как в месячном P&L: продажи, комиссия, COGS и расходы с posting_number - по order_date; расходы только со sku - по дате начисления; реклама - по дате статистики. Реализация = delivered. Возвраты и FBS не загружаются; налог и расходы уровня магазина не входят.")
AS
WITH post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace='OZON' AND m.marketplace_sku=p.sku),
pmap AS (SELECT DISTINCT posting_number, sku, order_date FROM post),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
sales AS (
  SELECT p.order_date d, p.internal_sku, p.status, p.quantity,
    IFNULL(f.sp_unit, p.price_rub) * p.quantity seller_base,
    IFNULL(-f.comm, IFNULL(rc.commission_rub, NUMERIC '0')) commission_known,
    IF(f.sp_unit IS NULL AND rc.commission_rub IS NULL, p.quantity, 0) comm_missing_qty,
    c.u * p.quantity cogs_amt, IF(c.u IS NULL, p.quantity, 0) cogs_missing_qty
  FROM post p LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_COMMISSION_RECOVERY` rc
    ON rc.posting_number=p.posting_number
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (SELECT d, internal_sku,
    SUM(quantity) gross_qty,
    SUM(IF(status='cancelled', quantity, 0)) cancelled_qty,
    SUM(IF(status IN ('delivering','awaiting_deliver','awaiting_packaging'), quantity, 0)) in_transit_qty,
    SUM(IF(status='delivered', quantity, 0)) realized_qty,
    SUM(IF(status='delivered', seller_base, 0)) seller_base_revenue_rub,
    SUM(IF(status='delivered', cogs_amt, 0)) product_cogs_rub,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    SUM(IF(status='delivered', commission_known, 0)) commission_rub,
    SUM(IF(status='delivered', comm_missing_qty, 0)) comm_missing_qty
  FROM sales GROUP BY 1,2),
dcost AS (
  SELECT pm.order_date d, mp.internal_sku, f.type_id, -f.amount_rub amt
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN pmap pm ON pm.posting_number=f.posting_number AND pm.sku=f.sku
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.posting_number IS NOT NULL
  UNION ALL
  SELECT f.event_date d, mp.internal_sku, f.type_id, -f.amount_rub amt
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=f.sku
  WHERE f.sku IS NOT NULL AND f.posting_number IS NULL),
dc AS (SELECT d, internal_sku,
    SUM(IF(type_id IN (32,29,28,98,30,59,45,78,9), amt, 0)) logistics,
    SUM(IF(type_id = 1, amt, 0)) acquiring,
    SUM(IF(type_id = 79, amt, 0)) storage,
    SUM(IF(type_id IN (32,29,28,98,30,1,59,45,78,9,79), amt, 0)) direct_var,
    SUM(IF(type_id IN (15,71,39,38), amt, 0)) other_direct
  FROM dcost GROUP BY 1,2),
ads AS (SELECT a.date d, mp.internal_sku, SUM(a.attributed_spend_rub) ad_attr
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` mp
    ON mp.marketplace='OZON' AND mp.marketplace_sku=a.sku GROUP BY 1,2),
j AS (SELECT COALESCE(s.d, dc.d, ads.d) fact_date,
    COALESCE(s.internal_sku, dc.internal_sku, ads.internal_sku) internal_sku,
    IFNULL(s.gross_qty,0) gross_qty, IFNULL(s.cancelled_qty,0) cancelled_qty,
    IFNULL(s.in_transit_qty,0) in_transit_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.commission_rub,0) commission_rub, IFNULL(s.comm_missing_qty,0) commission_missing_qty,
    IFNULL(dc.logistics,0) logistics_rub, IFNULL(dc.acquiring,0) acquiring_rub, IFNULL(dc.storage,0) storage_rub,
    IFNULL(dc.direct_var,0) direct_variable_marketplace_costs_rub,
    IFNULL(dc.other_direct,0) other_direct_marketplace_costs_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(ads.ad_attr,0) ad_spend_attributed_rub
  FROM s FULL JOIN dc USING (d, internal_sku)
         FULL JOIN ads USING (d, internal_sku))
SELECT j.fact_date, j.internal_sku,
  j.gross_qty, j.cancelled_qty, j.in_transit_qty, j.realized_qty,
  j.seller_base_revenue_rub, j.commission_rub, j.commission_missing_qty,
  j.logistics_rub, j.acquiring_rub, j.storage_rub,
  j.direct_variable_marketplace_costs_rub, j.other_direct_marketplace_costs_rub,
  j.product_cogs_rub, j.cogs_missing_qty,
  j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
    - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub contribution_before_ads_rub,
  j.ad_spend_attributed_rub,
  j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
    - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
    - j.ad_spend_attributed_rub contribution_after_attributed_ads_rub,
  'OZON_V1: orders, sold, revenue, commission, cogs, posting-linked costs = order date; sku-only costs = accrual date; ads = ad stat date' fact_date_semantics
FROM j
WHERE j.fact_date IS NOT NULL AND j.internal_sku IS NOT NULL;

-- ozon_mart.FCT_OZON_PNL_MONTHLY: восстановить before_hash edeb911c810e4179258ade32983c3496440f361f6f1d4ec5bf854b09ea227b25
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`
OPTIONS (description = "Канонический P&L Ozon, зерно = календарный месяц. VIEW, не таблица: пересчитывается при каждом чтении, устареть относительно ozon_raw не может. Мост L1-L4 по OZON_PNL_POLICY_V1. Продажи и себестоимость привязаны к order_date, расходы уровня магазина - к дате начисления. Отсутствующая комиссия НЕ ноль: см. commission_missing_qty и поля uncertainty.")
AS
WITH rec_comm AS (
  SELECT * FROM UNNEST([
    STRUCT('0107987069-0115-1' AS posting_number, NUMERIC '428.40' AS c),('0107987069-0116-1',NUMERIC '346.50'),
    ('0111865653-0053-1',NUMERIC '356.16'),('0140207342-0283-1',NUMERIC '446.46'),('0140207342-0294-1',NUMERIC '391.44'),
    ('0140425762-0336-1',NUMERIC '350.28'),('0143652501-0071-1',NUMERIC '428.40'),('0148296041-0008-1',NUMERIC '346.50'),
    ('0148983889-0086-1',NUMERIC '262.92'),('0174671740-0036-1',NUMERIC '206.79'),('0180844433-0032-1',NUMERIC '222.65'),
    ('0184479194-0054-1',NUMERIC '350.28'),('0198758548-0051-1',NUMERIC '382.20'),('0201540221-0097-1',NUMERIC '341.88'),
    ('0231520423-0001-2',NUMERIC '254.94'),('0233071107-0012-1',NUMERIC '276.36'),('52826697-0003-15',NUMERIC '254.94'),
    ('57794512-0006-2',NUMERIC '264.00'),('59699516-0310-4',NUMERIC '439.12'),('69799830-0276-1',NUMERIC '229.40'),
    ('71080184-0023-1',NUMERIC '262.92'),('81535887-0077-1',NUMERIC '350.28'),('90312383-0011-1',NUMERIC '385.56'),
    ('90831504-0160-1',NUMERIC '352.80'),('92234251-0008-1',NUMERIC '254.94'),('98041754-0004-1',NUMERIC '369.60'),
    ('98041754-0006-1',NUMERIC '344.40'),('99543424-0109-1',NUMERIC '350.28'),('99543424-0131-1',NUMERIC '387.20')])),
post AS (
  SELECT p.posting_number, p.sku, p.status, p.order_date, p.quantity, p.price_rub, m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace='OZON' AND m.marketplace_sku=p.sku),
fin_econ AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) sp_unit, SUM(buyer_paid_price_rub) bp,
         SUM(ozon_bonus_rub) bonus, SUM(ozon_coinvestment_rub) coinv, SUM(commission_rub) comm
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
cogs AS (SELECT internal_sku, effective_from, COALESCE(effective_to, DATE '9999-12-31') et, product_cogs_rub u
         FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
sales AS (
  SELECT DATE_TRUNC(p.order_date, MONTH) m, p.status, p.quantity, p.posting_number,
    IFNULL(f.sp_unit, p.price_rub) * p.quantity AS seller_base,
    IFNULL(f.bp,0) bp, IFNULL(f.bonus,0) bonus, IFNULL(f.coinv,0) coinv,
    IFNULL(-f.comm, IFNULL(rc.c, NUMERIC '0')) AS commission_known,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.quantity, 0) AS commission_missing_qty,
    IF(f.sp_unit IS NULL AND rc.c IS NULL, p.price_rub*p.quantity, NUMERIC '0') AS commission_missing_revenue,
    c.u * p.quantity AS cogs_amt,
    IF(c.u IS NULL, p.quantity, 0) AS cogs_missing_qty
  FROM post p
  LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN rec_comm rc ON rc.posting_number = p.posting_number
  LEFT JOIN cogs c ON c.internal_sku=p.internal_sku AND p.order_date BETWEEN c.effective_from AND c.et),
s AS (
  SELECT m,
    SUM(quantity) gross_ordered_qty,
    SUM(IF(status='cancelled', quantity, 0)) cancelled_qty,
    SUM(IF(status IN ('delivering','awaiting_deliver','awaiting_packaging'), quantity, 0)) in_transit_qty,
    SUM(IF(status='delivered', quantity, 0)) realized_qty,
    ROUND(SUM(IF(status='delivered', seller_base, 0)),2) seller_base_revenue_rub,
    ROUND(SUM(IF(status='delivered', bp, 0)),2) buyer_paid_revenue_rub,
    ROUND(SUM(IF(status='delivered', bonus, 0)),2) bonus_rub,
    ROUND(SUM(IF(status='delivered', coinv, 0)),2) coinvestment_rub,
    ROUND(SUM(IF(status='delivered', cogs_amt, 0)),2) product_cogs_rub,
    SUM(IF(status='delivered', quantity - cogs_missing_qty, 0)) cogs_covered_qty,
    SUM(IF(status='delivered', cogs_missing_qty, 0)) cogs_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_known, 0)),2) commission_known_rub,
    SUM(IF(status='delivered', quantity - commission_missing_qty, 0)) commission_coverage_qty,
    SUM(IF(status='delivered', commission_missing_qty, 0)) commission_missing_qty,
    ROUND(SUM(IF(status='delivered', commission_missing_revenue, 0)),2) commission_missing_revenue_rub
  FROM sales GROUP BY m),
f AS (
  SELECT DATE_TRUNC(event_date, MONTH) m,
    ROUND(SUM(IF(type_id IN (32,29,28,98,30,1,59,45,78,9,79), -amount_rub, 0)),2) direct_variable_marketplace_costs_rub,
    ROUND(SUM(IF(type_id IN (12,46), -amount_rub, 0)),2) store_level_variable_costs_rub,
    ROUND(SUM(IF(type_id IN (77,76,15,71,39,38,57), -amount_rub, 0)),2) other_marketplace_costs_ex_ads_rub,
    ROUND(SUM(IF(type_id=52, -amount_rub, 0)),2) marketplace_fixed_costs_rub,
    ROUND(SUM(IF(type_id IN (25,10), amount_rub, 0)),2) compensations_rub,
    ROUND(SUM(IF(type_id IN (116,47,96,74,48), -amount_rub, 0)),2) ad_reviews_rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` GROUP BY m),
a AS (SELECT DATE_TRUNC(date, MONTH) m, ROUND(SUM(expense_rub),2) ad_campaign_rub
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` GROUP BY m),
asku AS (SELECT DATE_TRUNC(date, MONTH) m, ROUND(SUM(attributed_spend_rub),2) ad_sku_attr_rub
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` GROUP BY m),
j AS (
  SELECT COALESCE(s.m, f.m, a.m) month,
    IFNULL(s.gross_ordered_qty,0) gross_ordered_qty, IFNULL(s.cancelled_qty,0) cancelled_qty,
    IFNULL(s.in_transit_qty,0) in_transit_qty, IFNULL(s.realized_qty,0) realized_qty,
    IFNULL(s.seller_base_revenue_rub,0) seller_base_revenue_rub,
    IFNULL(s.buyer_paid_revenue_rub,0) buyer_paid_revenue_rub,
    IFNULL(s.bonus_rub,0) bonus_rub, IFNULL(s.coinvestment_rub,0) coinvestment_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub,
    IFNULL(s.cogs_covered_qty,0) cogs_covered_qty, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(s.commission_known_rub,0) commission_known_rub,
    IFNULL(s.commission_coverage_qty,0) commission_coverage_qty,
    IFNULL(s.commission_missing_qty,0) commission_missing_qty,
    IFNULL(s.commission_missing_revenue_rub,0) commission_missing_revenue_rub,
    IFNULL(f.direct_variable_marketplace_costs_rub,0) direct_variable_marketplace_costs_rub,
    IFNULL(f.store_level_variable_costs_rub,0) store_level_variable_costs_rub,
    IFNULL(f.other_marketplace_costs_ex_ads_rub,0) other_marketplace_costs_ex_ads_rub,
    IFNULL(f.marketplace_fixed_costs_rub,0) marketplace_fixed_costs_rub,
    IFNULL(f.compensations_rub,0) compensations_rub,
    IFNULL(a.ad_campaign_rub,0) + IFNULL(f.ad_reviews_rub,0) advertising_rub,
    IFNULL(a.ad_campaign_rub,0) ad_campaign_rub,
    IFNULL(asku.ad_sku_attr_rub,0) ad_sku_attributed_rub
  FROM s FULL JOIN f USING (m) FULL JOIN a USING (m) FULL JOIN asku USING (m))
SELECT
  month, gross_ordered_qty, cancelled_qty, in_transit_qty, realized_qty,
  seller_base_revenue_rub, buyer_paid_revenue_rub, bonus_rub, coinvestment_rub,
  product_cogs_rub, cogs_covered_qty, cogs_missing_qty,
  ROUND(SAFE_DIVIDE(cogs_covered_qty, NULLIF(realized_qty,0))*100, 4) cogs_coverage_pct,
  commission_known_rub, commission_coverage_qty, commission_missing_qty, commission_missing_revenue_rub,
  ROUND(commission_missing_revenue_rub * NUMERIC '0.18', 2) commission_uncertainty_lower_rub,
  ROUND(commission_missing_revenue_rub * NUMERIC '0.52', 2) commission_uncertainty_upper_rub,
  direct_variable_marketplace_costs_rub, store_level_variable_costs_rub, other_marketplace_costs_ex_ads_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub, 2) l1_gross_seller_contribution_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub, 2) l2_contribution_before_ads_rub,
  advertising_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub, 2) l3_after_ads_before_fixed_rub,
  marketplace_fixed_costs_rub, compensations_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub, 2) l4_marketplace_contribution_profit_known_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub - commission_missing_revenue_rub*NUMERIC '0.52', 2) l4_profit_uncertainty_lower_rub,
  ROUND(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - advertising_rub
        - marketplace_fixed_costs_rub + compensations_rub - commission_missing_revenue_rub*NUMERIC '0.18', 2) l4_profit_uncertainty_upper_rub,
  ROUND(SAFE_DIVIDE(advertising_rub, NULLIF(seller_base_revenue_rub,0))*100, 4) actual_drr_pct,
  ROUND(SAFE_DIVIDE(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub, NULLIF(seller_base_revenue_rub,0))*100, 4) variable_break_even_drr_pct,
  ROUND(SAFE_DIVIDE(seller_base_revenue_rub - product_cogs_rub - commission_known_rub - direct_variable_marketplace_costs_rub
        - store_level_variable_costs_rub - other_marketplace_costs_ex_ads_rub - marketplace_fixed_costs_rub + compensations_rub,
        NULLIF(seller_base_revenue_rub,0))*100, 4) full_marketplace_break_even_drr_pct,
  NUMERIC '100' finance_classification_coverage_pct,
  NUMERIC '100' ad_total_coverage_pct,
  ROUND(SAFE_DIVIDE(ad_sku_attributed_rub, NULLIF(ad_campaign_rub,0))*100, 4) ad_sku_attribution_coverage_pct,
  CASE WHEN commission_missing_qty > 0 OR cogs_missing_qty > 0 THEN 'KNOWN_GAP' ELSE 'COMPLETE' END data_confidence,
  'PASS_WITH_KNOWN_COMMISSION_GAP' data_status,
  CURRENT_TIMESTAMP() computed_at
FROM j WHERE month IS NOT NULL;

-- ozon_mart.V_OZON_CIS_BUYOUT: объект создан Gate 5M, до него не существовал
DROP VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT`;
```
