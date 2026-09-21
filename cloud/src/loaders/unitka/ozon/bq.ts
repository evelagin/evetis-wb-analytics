/**
 * OZON UNITKA — канонический источник фактов месяца (Gate 5E).
 *
 * Один запрос отдаёт всё, что нужно секции: витрина P&L по SKU за сутки, реклама
 * (показы/переходы из Performance API) и цена покупателя из начислений финотчёта.
 * Приоритет источников соблюдён: ozon_mart поверх ozon_raw, справочник — evetis_ref.
 *
 * Полнота комиссии различается ТРЕМЯ состояниями, а не двумя (Gate 5K):
 *   PRESENT        — commission_missing_qty = 0 и commission_not_applicable_qty = 0;
 *   MISSING        — commission_missing_qty > 0: обычная продажа без комиссии в источнике;
 *   NOT_APPLICABLE — commission_not_applicable_qty > 0: выкуп товара Ozon у продавца, где
 *                    агентского вознаграждения не существует как факта хозяйственной жизни.
 * Юнитка НЕ знает про Беларусь: она получает канонические поля и флаги полноты, а выкуп
 * остаётся обычной строкой суток × SKU. Отдельная величина непроверенной выручки выкупа —
 * buyout_revenue_unproven_qty/_rub: её нельзя молча считать доказанной.
 */
export interface OzonFactsQuery { project: string; from: string; to: string }

const ISO = /^\d{4}-\d{2}-\d{2}$/;

/** SQL фактов месяца. Даты валидируются: в текст запроса попадает только ISO-дата. */
export function ozonMonthFactsSql({ project, from, to }: OzonFactsQuery): string {
  if (!ISO.test(from) || !ISO.test(to)) throw new Error('границы периода: ожидается YYYY-MM-DD');
  if (from > to) throw new Error('границы периода: начало позже конца');
  if (!/^[A-Za-z0-9-]+$/.test(project)) throw new Error('идентификатор проекта');
  return `
WITH m AS (
  SELECT DISTINCT internal_sku, marketplace_sku, offer_id
  FROM \`${project}.evetis_ref.REF_SKU_CHANNEL_MAP\` WHERE marketplace='OZON'),
f AS (
  SELECT * FROM \`${project}.ozon_mart.FCT_OZON_SKU_PNL_DAILY\`
  WHERE fact_date BETWEEN '${from}' AND '${to}'),
ads AS (
  SELECT date d, sku, SUM(impressions) impr, SUM(clicks) clicks
  FROM \`${project}.ozon_raw.RAW_OZON_ADS_SKU_DAILY\`
  WHERE date BETWEEN '${from}' AND '${to}' GROUP BY 1,2),
unitp AS (
  SELECT posting_number, sku, SUM(buyer_paid_price_rub) bp, SUM(seller_base_price_rub) sp
  FROM \`${project}.ozon_raw.RAW_OZON_FINANCE_ACCRUAL\`
  WHERE buyer_paid_price_rub IS NOT NULL GROUP BY 1,2),
buy AS (
  SELECT p.order_date d, mm.internal_sku,
         SUM(u.bp*p.quantity) buyer_amt, SUM(u.sp*p.quantity) seller_amt
  FROM \`${project}.ozon_raw.RAW_OZON_POSTINGS_FBO\` p
  JOIN unitp u USING (posting_number, sku)
  JOIN m mm ON mm.marketplace_sku=p.sku
  WHERE p.status='delivered' AND p.order_date BETWEEN '${from}' AND '${to}' GROUP BY 1,2)
SELECT CAST(f.fact_date AS STRING) d, m.offer_id,
  f.gross_qty, f.cancelled_qty, f.realized_qty, f.in_transit_qty,
  f.seller_base_revenue_rub revenue, f.commission_rub commission, f.logistics_rub logistics,
  f.acquiring_rub acquiring, f.storage_rub storage,
  f.other_direct_marketplace_costs_rub other_direct, f.sku_promotion_rub promo,
  f.product_cogs_rub cogs_amt,
  f.cogs_missing_qty, f.commission_missing_qty,
  f.commission_not_applicable_qty, f.buyout_revenue_unproven_qty, f.buyout_revenue_unproven_rub,
  f.ad_spend_attributed_rub ads_spend, f.contribution_after_attributed_ads_rub contrib_after,
  a.impr, a.clicks, b.buyer_amt, b.seller_amt
FROM f
LEFT JOIN m USING (internal_sku)
LEFT JOIN ads a ON a.d=f.fact_date AND a.sku=m.marketplace_sku
LEFT JOIN buy b ON b.d=f.fact_date AND b.internal_sku=f.internal_sku
ORDER BY d, offer_id`.trim();
}

/**
 * Снимки остатков. В лист попадает только ДОКАЗАННЫЙ снимок — тот, чья дата извлечения
 * совпадает с датой снимка (Gate 5C). Интерполяция и восстановление из продаж запрещены.
 */
export function ozonProvenStockSql({ project, from, to }: OzonFactsQuery): string {
  if (!ISO.test(from) || !ISO.test(to)) throw new Error('границы периода: ожидается YYYY-MM-DD');
  return `
WITH m AS (
  SELECT DISTINCT marketplace_sku, offer_id
  FROM \`${project}.evetis_ref.REF_SKU_CHANNEL_MAP\` WHERE marketplace='OZON'),
s AS (
  SELECT CAST(snapshot_date AS STRING) d, CAST(sku AS STRING) sku,
         SUM(available_stock_count) units,
         MAX(CAST(DATE(extracted_at) AS STRING)) extracted_day
  FROM \`${project}.ozon_raw.RAW_OZON_STOCKS\`
  WHERE snapshot_date BETWEEN '${from}' AND '${to}' GROUP BY 1,2)
SELECT s.d, m.offer_id, s.units
FROM s JOIN m ON m.marketplace_sku = s.sku
WHERE s.extracted_day = s.d           -- снимок доказан только своей датой съёма
ORDER BY 1,2`.trim();
}
