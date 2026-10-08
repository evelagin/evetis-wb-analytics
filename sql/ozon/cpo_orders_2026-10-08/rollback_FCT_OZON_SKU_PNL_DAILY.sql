-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.FCT_OZON_SKU_PNL_DAILY (VIEW)
-- Git-first object (SCALE 1, 2026-09-20): not in production until deployed. Rules:
-- sql/current/README.md. Metadata: MANIFEST.json.
--
-- Суточное выражение ТОЙ ЖЕ фактической экономики, что и FCT_OZON_SKU_PNL_MONTHLY: те же
-- источники, те же соединения, те же списки type_id, та же атрибуция, те же COGS. Отличия —
-- только зерно (сутки вместо месяца) и отсутствие ROUND: суммы хранятся с полной точностью
-- NUMERIC, округление — дело потребителя. Контракт приёмки: агрегат этого объекта до
-- месяц × internal_sku сходится с FCT_OZON_SKU_PNL_MONTHLY (деньги ≤ 0,01 ₽, штуки точно);
-- проверка — sql/scale1/fact_sku_daily_validation.sql.
--
-- Базис даты (fact_date) — как в месячном P&L, он СМЕШАННЫЙ и это не скрывается:
--   • заказы, реализация, выручка, комиссия, COGS          → бизнес-дата заказа (МСК);
--   • расходы финансов с posting_number                     → бизнес-дата заказа этого отправления;
--   • расходы финансов только со sku (без posting_number)   → event_date начисления;
--   • реклама                                               → дата рекламной статистики.
-- Реализация = status 'delivered'. Поздняя доставка меняет ПРОШЛЫЕ сутки (дату заказа).
--
-- Разложение прямых переменных расходов — по OZON_FINANCE_TAXONOMY_V1 (§2), без выдумывания:
--   logistics_rub = LOGISTICS direct (32) + LAST_MILE (29,28,98,30) + RETURN_LOGISTICS (59,45,78,9)
--   acquiring_rub = ACQUIRING (1);  storage_rub = STORAGE direct (79)
--   их сумма = direct_variable_marketplace_costs_rub — ровно корзина месячного P&L.
--   other_direct = утилизация/вывоз/упаковка/материалы (15,71,39,38) + обработка отменённых (6)
--   sku_promotion_rub = платные механики продвижения С ПРИВЯЗКОЙ К SKU: сбор первых отзывов (116),
--     звёздные товары (74), бонусы продавца (48). Gate 5L: эти начисления приходили со SKU, но не
--     попадали НИ В ОДНУ корзину и молча терялись (−10 428,67 ₽ за всю историю). Это маркетинг,
--     а не логистика, поэтому отдельная корзина, а не досыпка в logistics_rub. От CPC-рекламы
--     (ad_spend_attributed_rub) отличается источником: это факт начисления, а не атрибуция.
--
-- 🔴 UBR-010 (решение владельца 2026-09-22): sku_promotion_rub — самостоятельный фактически
--   начисленный расход на продвижение, СЛОЙ L3. Он НЕ уменьшает выручку, НЕ входит в
--   other_direct_marketplace_costs_rub и НЕ смешивается с ad_spend_attributed_rub.
--   Поэтому он вычитается ВМЕСТЕ с рекламой, ПОСЛЕ contribution_before_ads_rub, а не внутри него.
--   Основание: комиссия Ozon начисляется на полную цену реализации (84 из 84 отправлений),
--   выплата = цена − комиссия без вычета продвижения (84 из 84), тождество цены
--   «оплатил покупатель + баллы + соинвестирование = цена продавца» выполняется без него
--   (0 нарушений). Разбор: docs/ozon/OZON_SKU_PROMOTION_CLASSIFICATION_2026-09-22.md.
--   До 2026-09-22 (Gate 5L) он вычитался внутри contribution_before_ads_rub, что противоречило
--   и OZON_FINANCE_TAXONOMY_V1 (PROMOTION → L3), и магазинной витрине FCT_OZON_PNL_MONTHLY,
--   где эти же типы уже учитывались в advertising_rub на L3.
--
-- Тип операции (Gate 5K) — две несводимые хозяйственные формы, различаются ДО арифметики:
--   MARKETPLACE_SALE — агентская реализация: выручка продавца, комиссия Ozon, эквайринг;
--   CIS_BUYOUT      — выкуп товара Ozon у продавца (Беларусь): Ozon ПОКУПАТЕЛЬ, а не агент.
-- У выкупа агентского вознаграждения не существует как факта, поэтому комиссия здесь
-- NOT_APPLICABLE, а не MISSING: commission_not_applicable_qty, а не commission_missing_qty.
-- Эквайринга у выкупа тоже нет — это факт источника (ни одно отправление-выкуп не встречается
-- в детальном отчёте по эквайрингу), а не обнуление. Логистика начисляется и остаётся расходом.
-- Выручка выкупа = buyout_proceeds_rub из V_OZON_CIS_BUYOUT (сумма по первичному документу).
-- Выкуп без документа: тип известен по структурной сигнатуре (delivered, payout_rub = 0,
-- начисления выручки нет), но сумма выкупа НЕ доказана и НЕ выводится ставкой. UBR-012,
-- решение владельца 2026-09-22: такая строка даёт выручку 0 (fail-closed) и одновременно
-- buyout_revenue_unproven_qty/_rub — величина названа и измерена, но НЕ признана выручкой.
-- Цена заказа выручкой выкупа не становится ни при каких условиях.
--
-- Ограничения V1 (унаследованы, этим объектом НЕ исправляются): возвраты и FBS не загружаются;
-- отсутствующая комиссия обычной продажи считается 0 и видна в commission_missing_qty;
-- отсутствующий COGS не входит в product_cogs_rub и виден в cogs_missing_qty. Расходы уровня
-- магазина на SKU не разносятся (слоя L4 нет), налог не моделируется.
-- Internal dependencies: V_OZON_CIS_BUYOUT.
-- ============================================================================
-- Дата заказа отправления (order_date в CTE) — бизнес-дата МСК: DATE(created_at, 'Europe/Moscow'),
-- как в Ozon Seller Analytics. RAW order_date — UTC-дата created_at, остаётся в RAW для происхождения
-- (2026-10-06: с UTC заказ 00:00–02:59 МСК уезжал в предыдущие сутки).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`
OPTIONS (description = "Фактическая экономика Ozon, зерно = сутки x internal_sku. VIEW. Суточное выражение семантики FCT_OZON_SKU_PNL_MONTHLY: агрегат до месяца сходится с месячным P&L (деньги <= 0,01 руб., штуки точно). Суммы без ROUND, полная точность NUMERIC. Базис даты смешанный, как в месячном P&L: продажи, комиссия, COGS и расходы с posting_number - по order_date; расходы только со sku - по дате начисления; реклама - по дате статистики. Реализация = delivered. Тип операции: MARKETPLACE_SALE (агентская реализация) и CIS_BUYOUT (выкуп товара Ozon у продавца, Беларусь). У выкупа комиссии не существует как факта: commission_not_applicable_qty, а не commission_missing_qty; выручка выкупа - сумма по первичному документу. Выкуп без документа виден в buyout_revenue_unproven_qty/_rub. Возвраты и FBS не загружаются; налог и расходы уровня магазина не входят.")
AS
WITH post AS (
  SELECT p.posting_number, p.sku, p.status, DATE(p.created_at, 'Europe/Moscow') order_date, p.quantity, p.price_rub,
         p.payout_rub, m.internal_sku
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
cls AS (
  SELECT p.*, f.sp_unit, f.comm, b.buyout_proceeds_rub,
    CASE WHEN b.posting_number IS NOT NULL THEN 'CIS_BUYOUT'
         WHEN p.status='delivered' AND f.sp_unit IS NULL AND IFNULL(p.payout_rub, NUMERIC '0')=0
           THEN 'CIS_BUYOUT'
         ELSE 'MARKETPLACE_SALE' END op_type
  FROM post p LEFT JOIN fin_econ f USING (posting_number, sku)
  LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_CIS_BUYOUT` b
    ON b.posting_number=p.posting_number),
sales AS (
  SELECT p.order_date d, p.internal_sku, p.status, p.quantity,
    CASE WHEN p.buyout_proceeds_rub IS NOT NULL THEN p.buyout_proceeds_rub * p.quantity
         WHEN p.op_type='CIS_BUYOUT' THEN NUMERIC '0'
         ELSE IFNULL(p.sp_unit, p.price_rub) * p.quantity END seller_base,
    IF(p.op_type='MARKETPLACE_SALE', IFNULL(-p.comm, NUMERIC '0'), NUMERIC '0') commission_known,
    IF(p.op_type='MARKETPLACE_SALE' AND p.sp_unit IS NULL, p.quantity, 0) comm_missing_qty,
    IF(p.op_type='CIS_BUYOUT', p.quantity, 0) comm_na_qty,
    IF(p.op_type='CIS_BUYOUT' AND p.buyout_proceeds_rub IS NULL, p.quantity, 0) buyout_unproven_qty,
    IF(p.op_type='CIS_BUYOUT' AND p.buyout_proceeds_rub IS NULL,
       p.price_rub * p.quantity, NUMERIC '0') buyout_unproven_rub,
    c.u * p.quantity cogs_amt, IF(c.u IS NULL, p.quantity, 0) cogs_missing_qty
  FROM cls p
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
    SUM(IF(status='delivered', comm_missing_qty, 0)) comm_missing_qty,
    SUM(IF(status='delivered', comm_na_qty, 0)) comm_na_qty,
    SUM(IF(status='delivered', buyout_unproven_qty, 0)) buyout_unproven_qty,
    SUM(IF(status='delivered', buyout_unproven_rub, 0)) buyout_unproven_rub
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
    SUM(IF(type_id IN (15,71,39,38,6), amt, 0)) other_direct,
    SUM(IF(type_id IN (116,74,48), amt, 0)) sku_promotion
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
    IFNULL(s.comm_na_qty,0) commission_not_applicable_qty,
    IFNULL(s.buyout_unproven_qty,0) buyout_revenue_unproven_qty,
    IFNULL(s.buyout_unproven_rub,0) buyout_revenue_unproven_rub,
    IFNULL(dc.logistics,0) logistics_rub, IFNULL(dc.acquiring,0) acquiring_rub, IFNULL(dc.storage,0) storage_rub,
    IFNULL(dc.direct_var,0) direct_variable_marketplace_costs_rub,
    IFNULL(dc.other_direct,0) other_direct_marketplace_costs_rub,
    IFNULL(dc.sku_promotion,0) sku_promotion_rub,
    IFNULL(s.product_cogs_rub,0) product_cogs_rub, IFNULL(s.cogs_missing_qty,0) cogs_missing_qty,
    IFNULL(ads.ad_attr,0) ad_spend_attributed_rub
  FROM s FULL JOIN dc USING (d, internal_sku)
         FULL JOIN ads USING (d, internal_sku))
SELECT j.fact_date, j.internal_sku,
  j.gross_qty, j.cancelled_qty, j.in_transit_qty, j.realized_qty,
  j.seller_base_revenue_rub, j.commission_rub, j.commission_missing_qty,
  j.commission_not_applicable_qty, j.buyout_revenue_unproven_qty, j.buyout_revenue_unproven_rub,
  j.logistics_rub, j.acquiring_rub, j.storage_rub,
  j.direct_variable_marketplace_costs_rub, j.other_direct_marketplace_costs_rub,
  j.product_cogs_rub, j.cogs_missing_qty, j.sku_promotion_rub,
  j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
    - j.direct_variable_marketplace_costs_rub
    - j.other_direct_marketplace_costs_rub contribution_before_ads_rub,
  j.ad_spend_attributed_rub,
  j.seller_base_revenue_rub - j.product_cogs_rub - j.commission_rub
    - j.direct_variable_marketplace_costs_rub - j.other_direct_marketplace_costs_rub
    - j.sku_promotion_rub - j.ad_spend_attributed_rub contribution_after_attributed_ads_rub,
  'OZON_V1: orders, sold, revenue, commission, cogs, posting-linked costs = order date; sku-only costs = accrual date; ads = ad stat date' fact_date_semantics
FROM j
WHERE j.fact_date IS NOT NULL AND j.internal_sku IS NOT NULL;
