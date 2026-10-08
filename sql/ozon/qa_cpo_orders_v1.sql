-- ============================================================================
-- QA (read-only) — Phase B: «Оплата за заказ» (CPO) Ozon. Инварианты 1–11 OWNER ACK 2026-10-08.
-- Каждая проверка — один SELECT со столбцом status (PASS / FAIL / INFO). Блоки разделены «-- @check <ID>».
-- Запуск — после развёртывания RAW, вью и бэкфилла. Док: docs/finance/OZON_CPO_PHASE_B_2026-10-08.md.
-- ============================================================================

-- @check C0_CAMPAIGN_RESOLUTION
-- Ровно одна CPO-кампания на семейство отчёта, и каждая строка RAW получает кампанию. type_id не участвует.
SELECT COUNTIF(campaign_status NOT IN ('RESOLVED', 'RESOLVED_BY_BILLING_DAY')) unresolved_rows,
  COUNT(DISTINCT IF(campaign_status IN ('RESOLVED', 'RESOLVED_BY_BILLING_DAY'), CONCAT(report_family, ':', campaign_id), NULL)) family_campaigns,
  STRING_AGG(DISTINCT CONCAT(report_family, ':', IFNULL(campaign_id, campaign_status))) mapping,
  IF(COUNTIF(campaign_status NOT IN ('RESOLVED', 'RESOLVED_BY_BILLING_DAY')) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`;

-- @check I1_ONCE_ONLY
-- Каждый рубль отчёта — ровно один раз: сумма cpo_expense_rub суточного факта = сумма сопоставленных строк,
-- по каждым суткам × SKU; естественный ключ RAW уникален.
WITH v AS (
  SELECT business_date d, ordered_internal_sku sku, SUM(expense_rub) rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`
  WHERE ordered_mapping_status = 'MAPPED' AND campaign_status IN ('RESOLVED', 'RESOLVED_BY_BILLING_DAY') GROUP BY 1, 2),
f AS (
  SELECT fact_date d, internal_sku sku, SUM(cpo_expense_rub) rub
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY`
  WHERE cpo_expense_rub != 0 GROUP BY 1, 2),
k AS (SELECT COUNT(*) - COUNT(DISTINCT row_key) dup FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`)
SELECT (SELECT SUM(rub) FROM v) view_rub, (SELECT SUM(rub) FROM f) fact_rub,
  COUNTIF(ABS(IFNULL(v.rub, 0) - IFNULL(f.rub, 0)) > 0.000001) mismatched_cells, ANY_VALUE(k.dup) raw_duplicate_keys,
  IF(COUNTIF(ABS(IFNULL(v.rub, 0) - IFNULL(f.rub, 0)) > 0.000001) = 0 AND ANY_VALUE(k.dup) = 0, 'PASS', 'FAIL') status
FROM v FULL JOIN f USING (d, sku) CROSS JOIN k;

-- @check I2_NO_CPC_OVERLAP
-- CPO-кампаний нет в CPC-атрибуции (RAW_OZON_ADS_SKU_DAILY): сумма CPC + CPO в «Реклама внутренняя» не удваивает рубль.
WITH cpo AS (SELECT DISTINCT campaign_id FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS` WHERE payment_type = 'CPO')
SELECT COUNT(*) cpc_rows_of_cpo_campaigns, IFNULL(SUM(a.attributed_spend_rub), 0) rub,
  IF(COUNT(*) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY` a JOIN cpo USING (campaign_id);

-- @check I3_ORDERED_NE_PROMOTED
-- Финансово расход несёт ЗАКАЗАННЫЙ SKU, маркетингово — ПРОДВИГАЕМЫЙ. Итоги двух атрибуций равны, по SKU — нет.
WITH o AS (SELECT SUM(expense_rub) rub, SUM(IF(ordered_differs_from_promoted, expense_rub, 0)) cross_rub,
                  COUNTIF(ordered_differs_from_promoted) cross_rows
           FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`),
p AS (SELECT SUM(cpo_promoted_expense_rub) rub, SUM(cross_sku_expense_rub) cross_rub
      FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_PROMOTED_DAILY`)
SELECT o.rub ordered_total_rub, p.rub promoted_total_rub, o.cross_rows, o.cross_rub,
  IF(ABS(o.rub - p.rub) < 0.000001 AND ABS(o.cross_rub - p.cross_rub) < 0.000001, 'PASS', 'FAIL') status
FROM o CROSS JOIN p;

-- @check I4_CANCELLED_KEEPS_EXPENSE
-- Отменённый заказ сохраняет расход: его строки входят в суточный факт (сторно в источнике нет).
WITH c AS (
  SELECT business_date d, ordered_internal_sku sku, SUM(expense_rub) rub, COUNT(*) n
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`
  WHERE order_cancelled AND ordered_mapping_status = 'MAPPED' AND campaign_status IN ('RESOLVED', 'RESOLVED_BY_BILLING_DAY') GROUP BY 1, 2)
SELECT IFNULL(SUM(c.n), 0) cancelled_rows, IFNULL(SUM(c.rub), 0) cancelled_rub,
  COUNTIF(f.cpo_expense_rub IS NULL OR f.cpo_expense_rub < c.rub - 0.000001) dropped,
  (SELECT COUNTIF(expense_rub < 0) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`) negative_rows,
  IF(COUNTIF(f.cpo_expense_rub IS NULL OR f.cpo_expense_rub < c.rub - 0.000001) = 0, 'PASS', 'FAIL') status
FROM c LEFT JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_SKU_PNL_DAILY` f
  ON f.fact_date = c.d AND f.internal_sku = c.sku;

-- @check I5_IDEMPOTENT
-- Повторы и перекрытия окон не удваивают строку: естественный ключ уникален, и одна и та же
-- (семейство, заказ, заказанный, продвигаемый) не живёт на двух датах списания (перенос даты источником).
SELECT
  (SELECT COUNT(*) - COUNT(DISTINCT FORMAT('%s|%t|%s|%s|%s', report_family, charge_date, order_id, ordered_sku, promoted_sku))
   FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`) natural_key_dups,
  (SELECT COUNT(*) FROM (SELECT report_family, order_id, ordered_sku, promoted_sku
     FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`
     GROUP BY 1, 2, 3, 4 HAVING COUNT(DISTINCT charge_date) > 1)) redated_orders,
  (SELECT COUNT(DISTINCT run_id) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`) loading_runs,
  IF((SELECT COUNT(*) - COUNT(DISTINCT FORMAT('%s|%t|%s|%s|%s', report_family, charge_date, order_id, ordered_sku, promoted_sku))
      FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`) = 0
     AND (SELECT COUNT(*) FROM (SELECT report_family, order_id, ordered_sku, promoted_sku
          FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`
          GROUP BY 1, 2, 3, 4 HAVING COUNT(DISTINCT charge_date) > 1)) = 0, 'PASS', 'FAIL') status;

-- @check I5B_NO_CROSS_FAMILY_DUPLICATION
-- Один заказ × заказанный SKU не учтён в двух семействах (информационно: если Ozon спишет за оба — это две
-- реальные оплаты, и биллинг это подтвердит; здесь доказывается, что загрузка сама строк не размножает).
SELECT COUNT(*) orders_in_both_families,
  IF(COUNT(*) = 0, 'PASS', 'INFO') status
FROM (SELECT order_id, ordered_sku FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`
      GROUP BY 1, 2 HAVING COUNT(DISTINCT report_family) > 1);

-- @check I6_PARTIAL_NOT_COMPLETE
-- В RAW только полные сутки: статус COMPLETE, сутки закрыты на момент выгрузки, каждая дата покрыта
-- COMPLETE-блоком журнала (неполный отчёт отклоняется загрузчиком и не попадает сюда вовсе).
WITH cov AS (SELECT DISTINCT d FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_CPO_ORDER_RUNS`,
               UNNEST(GENERATE_DATE_ARRAY(chunk_from, chunk_to)) d
             WHERE record_type = 'CHUNK' AND status = 'COMPLETE')
SELECT COUNTIF(r.completeness_status != 'COMPLETE') not_complete,
  COUNTIF(r.charge_date >= DATE(r.fetched_at, 'Europe/Moscow')) fetched_while_open,
  COUNTIF(cov.d IS NULL) not_covered_by_complete_chunk,
  IF(COUNTIF(r.completeness_status != 'COMPLETE') + COUNTIF(r.charge_date >= DATE(r.fetched_at, 'Europe/Moscow'))
     + COUNTIF(cov.d IS NULL) = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS` r LEFT JOIN cov ON cov.d = r.charge_date;

-- @check I7_RECLASSIFICATION_KEEPS_NET_STORE
-- Тождество: биллинг = SKU-атрибуция + несопоставленное + остаток по каждой CPO-кампании × суткам. Перенос
-- в SKU не меняет чистую прибыль магазина: FCT_OZON_PNL_MONTHLY вычитает весь биллинг кампаний.
SELECT COUNT(*) campaign_days,
  ROUND(SUM(billed_rub), 2) billed_rub, ROUND(SUM(attributed_sku_rub), 2) attributed_sku_rub,
  ROUND(SUM(store_level_rub), 2) store_level_rub,
  COUNTIF(ABS(billed_rub - attributed_sku_rub - unmapped_rub - residual_rub) > 0.000001) identity_breaks,
  IF(COUNTIF(ABS(billed_rub - attributed_sku_rub - unmapped_rub - residual_rub) > 0.000001) = 0
     AND ABS(SUM(billed_rub) - SUM(attributed_sku_rub) - SUM(store_level_rub)) < 0.000001, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_RESIDUAL_DAILY`;

-- @check I8_REPORT_EQUALS_BILLING
-- Отчёт = биллинг по каждой покрытой CPO-кампании × суткам (допуск 0,02 ₽). Отрицательный остаток — ERROR.
SELECT COUNTIF(residual_status = 'MATCH') match_days, COUNTIF(residual_status = 'POSITIVE_RESIDUAL') positive_days,
  ROUND(SUM(IF(residual_status = 'POSITIVE_RESIDUAL', residual_rub, 0)), 2) positive_rub,
  COUNTIF(residual_status = 'NEGATIVE_RESIDUAL_ERROR') negative_days,
  COUNTIF(residual_status = 'BILLING_PENDING') billing_pending_days, COUNTIF(residual_status = 'NOT_COVERED') not_covered_days,
  IF(COUNTIF(residual_status = 'NEGATIVE_RESIDUAL_ERROR') = 0, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_RESIDUAL_DAILY`;

-- @check I9_FINANCE_TYPE54_RECONCILES
-- Независимая сверка: финансы type 54 по campaign_id = биллинг нарастающим итогом по дату последней проводки
-- кампании (проводка отстаёт от списания, поэтому сравнение — по эту дату, допуск 0,05 ₽).
WITH b AS (
  SELECT campaign_id, MAX(IF(finance_type54_rub IS NOT NULL, charge_date, NULL)) fin_through
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_RESIDUAL_DAILY` GROUP BY 1)
SELECT r.campaign_id, b.fin_through,
  ROUND(SUM(IF(r.charge_date <= b.fin_through, r.billed_rub, 0)), 2) billed_through,
  ROUND(SUM(IFNULL(r.finance_type54_rub, 0)), 2) finance_type54,
  ROUND(SUM(IF(r.charge_date > b.fin_through, r.billed_rub, 0)), 2) billed_after_last_posting,
  IF(ABS(SUM(IF(r.charge_date <= b.fin_through, r.billed_rub, 0)) - SUM(IFNULL(r.finance_type54_rub, 0))) <= 0.05, 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_RESIDUAL_DAILY` r JOIN b USING (campaign_id)
GROUP BY 1, 2 ORDER BY 1;

-- @check I10_TYPE54_CPC_HISTORY_IS_NOT_CPO
-- type 54 в 2025-05…08 нёс и CPC-кампании. Ни одна из них не CPO, и ни рубль их не попал в CPO-слой.
WITH t54 AS (
  SELECT f.unit_number campaign_id, -SUM(f.amount_rub) rub, MIN(f.event_date) first_d, MAX(f.event_date) last_d
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` f
  WHERE f.type_id = 54 AND f.unit_number_meaning = 'campaign_id' GROUP BY 1),
pt AS (SELECT campaign_id, ANY_VALUE(payment_type) payment_type
       FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS` GROUP BY 1),
cpo_ids AS (SELECT DISTINCT campaign_id FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`)
SELECT COUNTIF(pt.payment_type != 'CPO') non_cpo_campaigns_in_type54,
  ROUND(SUM(IF(pt.payment_type != 'CPO', t54.rub, 0)), 2) non_cpo_type54_rub,
  COUNTIF(pt.payment_type != 'CPO' AND c.campaign_id IS NOT NULL) leaked_into_cpo,
  STRING_AGG(IF(pt.payment_type != 'CPO', CONCAT(t54.campaign_id, ' ', pt.payment_type), NULL)) non_cpo,
  IF(COUNTIF(pt.payment_type != 'CPO' AND c.campaign_id IS NOT NULL) = 0, 'PASS', 'FAIL') status
FROM t54 LEFT JOIN pt USING (campaign_id) LEFT JOIN cpo_ids c USING (campaign_id);

-- @check I11_UNMAPPED_STAYS_VISIBLE
-- Ни одна строка RAW не теряется соединениями вью; несопоставленные видны и уходят в уровень магазина.
SELECT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`) raw_rows,
  COUNT(*) view_rows, COUNTIF(ordered_mapping_status != 'MAPPED') unmapped_ordered_rows,
  ROUND(SUM(IF(ordered_mapping_status != 'MAPPED', expense_rub, 0)), 2) unmapped_ordered_rub,
  COUNTIF(promoted_mapping_status != 'MAPPED') unmapped_promoted_rows,
  COUNTIF(business_date_basis = 'CHARGE_DATE_FALLBACK') charge_date_fallback_rows,
  IF(COUNT(*) = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS`), 'PASS', 'FAIL') status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`;
