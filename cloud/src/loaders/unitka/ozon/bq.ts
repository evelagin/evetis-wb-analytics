/**
 * OZON UNITKA — канонический источник фактов месяца (Gate 5E).
 *
 * Один запрос отдаёт всё, что нужно секции: витрина P&L по SKU за сутки, реклама
 * (показы/переходы из Performance API) и цена покупателя из начислений финотчёта.
 * Приоритет источников соблюдён: ozon_mart поверх ozon_raw, справочник — evetis_ref.
 *
 * Gate 8: источник — V_OZON_SKU_PNL_DAILY_OPERATIONAL, а не FCT_OZON_SKU_PNL_DAILY напрямую.
 * Операционный слой добавляет к факту ЭФФЕКТИВНЫЕ величины: там, где Ozon ещё не опубликовал
 * комиссию и логистику (в среднем 18 суток после заказа, максимум 36), вместо нуля стоит
 * доказанная оценка. Ноль остался бы мнимой прибылью. Факт при этом не переопределяется:
 * commission_actual/logistics_actual едут рядом, состояние компонента — в *_state.
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
/* ── ГРАНИЦА ТИПОВ BigQuery ───────────────────────────────────────────────────
 * Клиент `@google-cloud/bigquery` отдаёт NUMERIC не числом, а объектом `Big`, а DATE —
 * объектом `{value}`. Для JavaScript это меняет ДВЕ вещи разом:
 *
 *   • объект истинен ВСЕГДА, включая `Big(0)`. Проверка `if (storage)` в сборщике месяца
 *     срабатывала на нуле и писала в лист 0 там, где значения нет — ровно тот выдуманный
 *     ноль, который отличать от пустоты и есть смысл колонки;
 *   • `+` над двумя `Big` склеивает строки, а не складывает числа: комиссия с эквайрингом
 *     давала `"600"+"12" = "60012"`.
 *
 * Локальная отладка этого не ловила: harness приводил NUMERIC к числу сам, и до боевого
 * прогона дефект был невидим. Поэтому границу держит reader — ровно так же, как у WB
 * (`loaders/unitka/bq.ts`, функции `str`/`num`). Дальше по коду ходят только примитивы.
 */
function bqScalar(v: unknown): unknown {
  if (v === null || v === undefined || typeof v !== 'object') return v;
  const o = v as Record<string, unknown>;
  if ('value' in o) return String(o.value);              // DATE/TIMESTAMP → ISO-строка
  const n = Number(String(v));                           // Big → число
  return Number.isFinite(n) ? n : String(v);
}

/** Строка BigQuery → строка с примитивами. Имена полей и их состав не меняются. */
export function normalizeBqRow<T>(raw: Record<string, unknown>): T {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(raw)) out[k] = bqScalar(raw[k]);
  return out as T;
}

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
  SELECT * FROM \`${project}.ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL\`
  WHERE fact_date BETWEEN '${from}' AND '${to}'),
ads AS (
  SELECT date d, sku, SUM(impressions) impr, SUM(clicks) clicks
  FROM \`${project}.ozon_raw.RAW_OZON_ADS_SKU_DAILY\`
  WHERE date BETWEEN '${from}' AND '${to}' GROUP BY 1,2),
order_prices AS (
  -- Цена калькулятора, НЕ признанная выручка и НЕ цена покупателя.
  SELECT p.order_date d, m.offer_id, SUM(p.quantity) units,
    SUM(IF(p.quantity > 0 AND p.price_rub > 0, p.quantity, 0)) covered_units,
    CASE WHEN COUNTIF(p.quantity IS NULL OR p.quantity <= 0
                       OR p.price_rub IS NULL OR p.price_rub <= 0) = 0
         THEN SAFE_DIVIDE(SUM(p.price_rub * p.quantity), SUM(p.quantity)) END price
  FROM \`${project}.ozon_raw.RAW_OZON_POSTINGS_FBO\` p
  JOIN m ON m.marketplace_sku = p.sku
  WHERE p.order_date BETWEEN '${from}' AND '${to}'
    AND p.status IN ('delivered', 'delivering', 'awaiting_deliver', 'awaiting_packaging')
  GROUP BY 1,2),
price_finance AS (
  SELECT posting_number, sku, SUM(seller_base_price_rub) finance_unit_rub
  FROM \`${project}.ozon_raw.RAW_OZON_FINANCE_ACCRUAL\`
  WHERE seller_base_price_rub IS NOT NULL GROUP BY 1,2),
price_units AS (
  -- Один posting/SKU принадлежит ровно одной популяции. Сильное evidence не дополняется
  -- reference ценой той же единицы. Денежные поля канонического факта не изменяются.
  SELECT DISTINCT p.order_date d, mm.offer_id, p.posting_number, p.sku marketplace_sku,
    p.status, p.quantity, p.price_rub reference_unit_rub, pf.finance_unit_rub,
    b.posting_number IS NOT NULL documented_buyout_present,
    b.buyout_proceeds_rub documented_buyout_unit_rub,
    -- Финансы старше застрявшего статуса: seller-base начисление делает единицу ACTUAL_FINANCE
    -- и у недоставленной (конфликт цикла), если это не выкуп по документу.
    CASE WHEN p.status='delivered' AND b.posting_number IS NOT NULL THEN 'DOCUMENTED_BUYOUT'
         WHEN pf.finance_unit_rub IS NOT NULL AND (p.status='delivered' OR b.posting_number IS NULL) THEN 'ACTUAL_FINANCE'
         ELSE 'REFERENCE' END basis_source,
    CASE WHEN p.status='delivered' AND b.posting_number IS NOT NULL THEN b.buyout_proceeds_rub
         WHEN pf.finance_unit_rub IS NOT NULL AND (p.status='delivered' OR b.posting_number IS NULL) THEN pf.finance_unit_rub
         ELSE p.price_rub END basis_unit_rub
  FROM \`${project}.ozon_raw.RAW_OZON_POSTINGS_FBO\` p
  JOIN m mm ON mm.marketplace_sku=p.sku
  LEFT JOIN price_finance pf USING (posting_number, sku)
  LEFT JOIN \`${project}.ozon_mart.V_OZON_CIS_BUYOUT\` b ON b.posting_number=p.posting_number
  WHERE p.order_date BETWEEN '${from}' AND '${to}'
    AND p.status IN ('delivered', 'delivering', 'awaiting_deliver', 'awaiting_packaging')),
operational_prices AS (
  SELECT d, offer_id, SUM(quantity) operational_expected_qty,
    SUM(IF(basis_source <> 'REFERENCE', quantity, 0)) operational_actual_qty,
    SUM(IF(basis_source = 'REFERENCE', quantity, 0)) operational_provisional_qty,
    SUM(IF(basis_source = 'REFERENCE' AND quantity > 0 AND reference_unit_rub > 0, quantity, 0)) operational_reference_covered_qty,
    SUM(IF(basis_source <> 'REFERENCE', basis_unit_rub * quantity, 0)) operational_actual_basis_rub,
    SUM(IF(basis_source = 'REFERENCE', reference_unit_rub * quantity, 0)) operational_reference_basis_rub,
    CASE WHEN COUNTIF(quantity IS NULL OR quantity <= 0 OR basis_unit_rub IS NULL
                       OR basis_unit_rub < 0 OR (basis_source = 'REFERENCE' AND basis_unit_rub = 0)) = 0
         THEN SUM(basis_unit_rub * quantity) END operational_basis_rub,
    TO_JSON_STRING(ARRAY_AGG(STRUCT(posting_number, marketplace_sku, status, quantity,
      reference_unit_rub, finance_unit_rub, documented_buyout_present,
      documented_buyout_unit_rub, basis_source))) operational_basis_units_json
  FROM price_units GROUP BY 1,2),
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
  f.seller_base_revenue_rub revenue,
  -- ЭФФЕКТИВНАЯ величина: факт там, где он пришёл, оценка там, где Ozon ещё не опубликовал.
  -- Факт и оценка едут рядом отдельными полями и остаются раздельно аудируемыми.
  f.commission_effective_rub commission, f.logistics_effective_rub logistics,
  -- Gate 9: база операционной экономики — ОЖИДАЕМО реализованные единицы (заказано − отменено),
  -- а не только доставленные. Единица в пути приносит свою провизорную экономику.
  f.expected_realized_qty, f.provisional_revenue_rub, f.provisional_cogs_rub,
  f.provisional_cogs_missing_qty, f.economics_completeness,
  f.commission_rub commission_actual, f.logistics_rub logistics_actual,
  f.commission_estimated_rub, f.logistics_estimated_rub,
  f.commission_state, f.logistics_state, f.storage_state, f.other_direct_state,
  f.commission_estimate_method, f.logistics_estimate_method, f.period_matured,
  f.lifecycle_conflict_qty, f.lifecycle_conflict_commission_rub, f.commission_unaccounted_qty,
  f.acquiring_rub acquiring, f.storage_rub storage,
  f.other_direct_marketplace_costs_rub other_direct, f.sku_promotion_rub promo,
  f.product_cogs_rub cogs_amt,
  f.cogs_missing_qty, f.commission_missing_qty,
  f.commission_not_applicable_qty, f.buyout_revenue_unproven_qty, f.buyout_revenue_unproven_rub,
  f.ad_spend_attributed_rub ads_spend, f.contribution_after_attributed_ads_rub contrib_after,
  a.impr, a.clicks, b.buyer_amt, b.seller_amt,
  CASE WHEN op.units = f.expected_realized_qty AND op.covered_units = op.units
       THEN op.price END order_reference_price,
  op.units order_reference_qty, op.covered_units order_reference_covered_qty,
  1 operational_basis_version,
  ob.operational_expected_qty, ob.operational_actual_qty, ob.operational_provisional_qty,
  ob.operational_reference_covered_qty, ob.operational_actual_basis_rub,
  ob.operational_reference_basis_rub, ob.operational_basis_rub, ob.operational_basis_units_json
FROM f
LEFT JOIN m USING (internal_sku)
LEFT JOIN ads a ON a.d=f.fact_date AND a.sku=m.marketplace_sku
LEFT JOIN buy b ON b.d=f.fact_date AND b.internal_sku=f.internal_sku
LEFT JOIN order_prices op ON op.d=f.fact_date AND op.offer_id=m.offer_id
LEFT JOIN operational_prices ob ON ob.d=f.fact_date AND ob.offer_id=m.offer_id
ORDER BY d, offer_id`.trim();
}

/** Оценка СПП суток × SKU для ДРР до прихода финансовой пары (Phase 6, оценщик E1m5). */
export interface OzonSppEstimateRow { d: string; offer_id: string; spp_estimate_pct: number; spp_estimate_level: 'SKU' | 'SHOP' }

/**
 * ОЦЕНКА СПП для знаменателя ДРР там, где цены покупателя ещё нет (заказ не доставлен).
 *
 * Цена продавца знаменателем ДРР быть НЕ может: СПП Ozon в среднем 54–65 %, и ДРР от цены продавца
 * занижается втрое (бэктест: смещение −62 п.п.). Оценщик выбран бэктестом на production-данных
 * (E1m5: MAE 6,6 п.п., смещение −0,1 п.п.):
 *   • для суток d берутся ДОСТАВЛЕННЫЕ отправления с финансовой парой buyer/seller (то же зерно,
 *     что у фактической СПП — CTE `unitp` запроса фактов), заказанные в [d−30, d−1] и УЖЕ
 *     известные к d−1 (MIN(event_date) ≤ d−1): оценка суток не видит будущего;
 *   • свой SKU, если в окне ≥ 5 ед.; иначе весь магазин в том же окне (≥ 1 ед.); иначе оценки нет.
 * Оценка привязана к дате строки; для свежих суток она может сдвигаться между прогонами, пока Ozon
 * допубликовывает начисления (event_date приходит с задержкой ~14 сут). Факт заменяет её сам, как только
 * у суток появится своя пара (тогда формула ДРР возвращается к фактической «цене с СПП»).
 */
export function ozonSppEstimateSql({ project, from, to }: OzonFactsQuery): string {
  if (!ISO.test(from) || !ISO.test(to)) throw new Error('границы периода: ожидается YYYY-MM-DD');
  if (from > to) throw new Error('границы периода: начало позже конца');
  if (!/^[A-Za-z0-9-]+$/.test(project)) throw new Error('идентификатор проекта');
  return `
WITH m AS (
  SELECT DISTINCT marketplace_sku, offer_id
  FROM \`${project}.evetis_ref.REF_SKU_CHANNEL_MAP\` WHERE marketplace='OZON'),
unitp AS (
  SELECT posting_number, sku, SUM(buyer_paid_price_rub) bp, SUM(seller_base_price_rub) sp,
         MIN(event_date) known
  FROM \`${project}.ozon_raw.RAW_OZON_FINANCE_ACCRUAL\`
  WHERE buyer_paid_price_rub IS NOT NULL GROUP BY 1,2),
obs AS (
  SELECT p.order_date od, mm.offer_id, u.known, p.quantity q, u.bp * p.quantity b, u.sp * p.quantity s
  FROM \`${project}.ozon_raw.RAW_OZON_POSTINGS_FBO\` p
  JOIN unitp u USING (posting_number, sku)
  JOIN m mm ON mm.marketplace_sku = p.sku
  WHERE p.status = 'delivered' AND u.sp IS NOT NULL AND u.sp <> 0
    AND p.order_date BETWEEN DATE_SUB(DATE '${from}', INTERVAL 30 DAY) AND DATE_SUB(DATE '${to}', INTERVAL 1 DAY)),
days AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(DATE '${from}', DATE '${to}')) d),
-- окно суток d: заказ в [d−30, d−1] и финансовая пара известна к d−1
win AS (
  SELECT days.d, o.offer_id, o.q, o.b, o.s
  FROM days JOIN obs o
    ON o.od BETWEEN DATE_SUB(days.d, INTERVAL 30 DAY) AND DATE_SUB(days.d, INTERVAL 1 DAY)
   AND o.known <= DATE_SUB(days.d, INTERVAL 1 DAY)),
sku_w AS (SELECT d, offer_id, SUM(q) units, SUM(b) b, SUM(s) s FROM win GROUP BY 1,2),
shop_w AS (SELECT d, SUM(q) units, SUM(b) b, SUM(s) s FROM win GROUP BY 1),
k AS (SELECT DISTINCT offer_id FROM m)
SELECT CAST(days.d AS STRING) d, k.offer_id,
  -- правило уровня: свой SKU при ≥ 5 ед. в окне, иначе магазин при ≥ 1 ед.
  -- та же величина, что у фактической СПП: (1 − Σ buyer / Σ seller) × 100, взвешено единицами;
  -- округление r6 — в сборщике, тем же правилом, что у факта
  CAST(IF(sw.units >= 5, 1 - sw.b / sw.s, 1 - sh.b / sh.s) * 100 AS FLOAT64) spp_estimate_pct,
  IF(sw.units >= 5, 'SKU', 'SHOP') spp_estimate_level
FROM days CROSS JOIN k
LEFT JOIN sku_w sw ON sw.d = days.d AND sw.offer_id = k.offer_id
LEFT JOIN shop_w sh ON sh.d = days.d
WHERE (sw.units >= 5 AND sw.s <> 0) OR (sh.units >= 1 AND sh.s <> 0)
ORDER BY 1,2`.trim();
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
