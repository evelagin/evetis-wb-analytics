-- ============================================================================
-- STAGE 3.1B PR1 — ACCEPTANCE / ВАЛИДАЦИЯ (READ-ONLY)
-- Дата: 2026-08-27.  Основной скрипт: sql/mart/pr_cogs_consumer_v1.sql
--
-- 🔴 ИНВАРИАНТЫ ЗДЕСЬ ДИНАМИЧЕСКИЕ, А НЕ SNAPSHOT-АБСОЛЮТНЫЕ (owner ACK).
--   FACT_* штатно обновляются прогоном wb-mart-prod, поэтому «39 481 строка» и
--   «8 686 478,50 ₽» — это acceptance snapshot на момент выката, зафиксированный
--   в docs/STAGE3_1B_COGS_CONSUMER_2026-08-27.md, а НЕ вечный ASSERT.
--   Постоянные проверки сверяют вью с НЕЗАВИСИМЫМ пересчётом на ТОМ ЖЕ снимке.
--
-- Все запросы — только SELECT. Ничего не создают и не меняют.
-- ============================================================================

-- ── S-0. Неявная единица: 1 строка = 1 ед. Основание умножения на COGS. ─────
SELECT 'S-0 quantity ≡ 1' AS check_name,
       COUNT(*) AS cogs_requiring_raw_rows,
       COUNTIF(SAFE_CAST(quantity AS INT64) IS DISTINCT FROM 1) AS violations,
       IF(COUNTIF(SAFE_CAST(quantity AS INT64) IS DISTINCT FROM 1) = 0, 'PASS', 'FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FINANCE`
WHERE supplier_oper_name IN ('Продажа', 'Возврат');

-- ── S-1. srid непуст там, где он обязателен по контракту ───────────────────
--   Вне «Продажа»/«Возврат» srid пуст штатно — проверка обязана быть scoped.
SELECT 'S-1 srid непуст' AS check_name, 'finance' AS layer,
       COUNT(*) AS rows_, COUNTIF(NULLIF(TRIM(srid), '') IS NULL) AS empty_srid,
       IF(COUNTIF(NULLIF(TRIM(srid), '') IS NULL) = 0, 'PASS', 'FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
WHERE supplier_oper_name IN ('Продажа', 'Возврат')
UNION ALL
SELECT 'S-1 srid непуст', 'sales', COUNT(*), COUNTIF(NULLIF(TRIM(srid), '') IS NULL),
       IF(COUNTIF(NULLIF(TRIM(srid), '') IS NULL) = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`;

-- ── S-2. srid — уникальный ключ продажи (иначе связка возврата неоднозначна) ─
SELECT 'S-2 srid уникален среди продаж' AS check_name, 'finance' AS layer,
       COUNT(*) AS distinct_srid, COUNTIF(n > 1) AS multi,
       IF(COUNTIF(n > 1) = 0, 'PASS', 'FAIL') AS verdict
FROM (SELECT NULLIF(TRIM(srid), '') s, COUNT(*) n
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
      WHERE supplier_oper_name = 'Продажа' GROUP BY s)
UNION ALL
SELECT 'S-2 srid уникален среди продаж', 'sales', COUNT(*), COUNTIF(n > 1),
       IF(COUNTIF(n > 1) = 0, 'PASS', 'FAIL')
FROM (SELECT NULLIF(TRIM(srid), '') s, COUNT(*) n
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`
      WHERE NOT is_return GROUP BY s);

-- ── S-3. Каждый возврат связывается РОВНО с одной продажей (OD-1 = B) ───────
SELECT 'S-3 linkage возвратов' AS check_name,
       COUNT(*) AS return_events,
       COUNTIF(linked_sale_match_count = 1) AS linked_exactly_one,
       COUNTIF(linked_sale_match_count = 0) AS link_missing,
       COUNTIF(linked_sale_match_count > 1) AS link_ambiguous,
       IF(COUNTIF(linked_sale_match_count IS DISTINCT FROM 1) = 0, 'PASS', 'FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
WHERE supplier_oper_name = 'Возврат';

-- ── S-4. multi-match ЗАПРЕЩЁН на обоих грейнах (нарушение контракта данных) ──
SELECT 'S-4 multi-match' AS check_name, 'V_FACT_FINANCE_COGS' AS obj,
       COUNTIF(cogs_match_count > 1) AS multi,
       IF(COUNTIF(cogs_match_count > 1) = 0, 'PASS', 'FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
UNION ALL
SELECT 'S-4 multi-match', 'V_MART_SKU_DAILY_COGS', COUNTIF(cogs_match_count > 1),
       IF(COUNTIF(cogs_match_count > 1) = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`;

-- ── S-5. cogs_resolution_status никогда не NULL ────────────────────────────
SELECT 'S-5 status not null' AS check_name, 'V_FACT_FINANCE_COGS' AS obj,
       COUNTIF(cogs_resolution_status IS NULL) AS nulls,
       IF(COUNTIF(cogs_resolution_status IS NULL) = 0, 'PASS', 'FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
UNION ALL
SELECT 'S-5 status not null', 'V_MART_SKU_DAILY_COGS', COUNTIF(cogs_resolution_status IS NULL),
       IF(COUNTIF(cogs_resolution_status IS NULL) = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`;

-- ── S-6. UNKNOWN никогда не превращается в 0 (F-1) ─────────────────────────
SELECT 'S-6 unknown != 0' AS check_name, 'V_FACT_FINANCE_COGS' AS obj,
       COUNTIF(cogs_resolution_status <> 'RESOLVED'
               AND (unit_product_cogs_rub IS NOT NULL OR product_cogs_signed_rub IS NOT NULL)) AS leaks,
       IF(COUNTIF(cogs_resolution_status <> 'RESOLVED'
               AND (unit_product_cogs_rub IS NOT NULL OR product_cogs_signed_rub IS NOT NULL)) = 0,
          'PASS', 'FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
UNION ALL
SELECT 'S-6 unknown != 0', 'V_MART_SKU_DAILY_COGS',
       COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')
               AND (product_cogs_operational_rub IS NOT NULL
                 OR net_product_cogs_operational_rub IS NOT NULL
                 OR contribution_after_product_cogs_rub IS NOT NULL)),
       IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')
               AND (product_cogs_operational_rub IS NOT NULL
                 OR net_product_cogs_operational_rub IS NOT NULL
                 OR contribution_after_product_cogs_rub IS NOT NULL)) = 0, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`;

-- ── S-7 / S-8. ДИНАМИЧЕСКИЙ грейн: вью не теряет и не размножает строки ─────
SELECT 'S-7 грейн V_FACT_FINANCE_COGS' AS check_name,
       (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`) AS view_rows,
       (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`
         WHERE supplier_oper_name IN ('Продажа', 'Возврат')) AS source_rows,
       IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`)
        = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`
            WHERE supplier_oper_name IN ('Продажа', 'Возврат')), 'PASS', 'FAIL') AS verdict
UNION ALL
SELECT 'S-8 грейн V_MART_SKU_DAILY_COGS',
       (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`),
       (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`),
       IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`)
        = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`), 'PASS', 'FAIL');

-- ── S-9. ДИНАМИЧЕСКАЯ сверка settlement-серии с независимым пересчётом ──────
--   Независимый путь: FACT_FINANCE × REF_SKU_MASTER × resolver, правило OD-1=B
--   воспроизведено заново, без обращения к V_FACT_FINANCE_COGS.
WITH src AS (
  SELECT CONCAT(report_id, '#', rrd_id) k, supplier_oper_name op,
         NULLIF(TRIM(srid), '') srid, _rr_date d
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
  WHERE supplier_oper_name IN ('Продажа', 'Возврат')),
self AS (SELECT k, ANY_VALUE(srid) srid FROM src GROUP BY k),
sale AS (SELECT srid, COUNT(*) n, MIN(d) sd FROM src WHERE op = 'Продажа' AND srid IS NOT NULL GROUP BY srid),
ev AS (
  SELECT f.finance_row_key k, f.supplier_oper_name op, m.internal_sku,
         IF(f.supplier_oper_name = 'Продажа', 1, -1) su,
         IF(f.supplier_oper_name = 'Продажа', f.finance_date,
            IF(sale.n = 1, sale.sd, NULL)) AS eff_d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` f
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` m ON m.nm_id = f.nm_id
  LEFT JOIN self ON self.k = f.finance_row_key
  LEFT JOIN sale ON sale.srid = self.srid AND f.supplier_oper_name = 'Возврат'
  WHERE f.supplier_oper_name IN ('Продажа', 'Возврат')),
indep AS (
  SELECT SUM(e.su * v.product_cogs_rub) AS cogs
  FROM ev e JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` v
    ON v.internal_sku = e.internal_sku
   AND e.eff_d BETWEEN v.effective_from AND COALESCE(v.effective_to, DATE '9999-12-31'))
SELECT 'S-9 settlement reconciliation' AS check_name,
       (SELECT SUM(product_cogs_signed_rub) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`) AS view_cogs,
       (SELECT cogs FROM indep) AS independent_cogs,
       IF((SELECT SUM(product_cogs_signed_rub) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`)
        = (SELECT cogs FROM indep), 'PASS', 'FAIL') AS verdict;

-- ── S-10. ДИНАМИЧЕСКАЯ сверка operational-серии с событийным пересчётом ─────
--   Независимый путь: построчно по FACT_SALES, без агрегата витрины.
WITH ssrc AS (
  SELECT sale_id, NULLIF(TRIM(srid), '') srid, is_return, _sale_date d
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`),
sself AS (SELECT sale_id, ANY_VALUE(srid) srid FROM ssrc GROUP BY sale_id),
ssale AS (SELECT srid, COUNT(*) n, MIN(d) sd FROM ssrc WHERE NOT is_return AND srid IS NOT NULL GROUP BY srid),
sev AS (
  SELECT fs.sale_id, m.internal_sku, IF(fs.is_return, -1, 1) su,
         IF(fs.is_return, IF(ssale.n = 1, ssale.sd, NULL), fs.sale_date) AS eff_d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES` fs
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` m ON m.nm_id = fs.nm_id
  LEFT JOIN sself ON sself.sale_id = fs.sale_id
  LEFT JOIN ssale ON ssale.srid = sself.srid AND fs.is_return),
indep AS (
  SELECT SUM(e.su * v.product_cogs_rub) AS cogs
  FROM sev e JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` v
    ON v.internal_sku = e.internal_sku
   AND e.eff_d BETWEEN v.effective_from AND COALESCE(v.effective_to, DATE '9999-12-31'))
SELECT 'S-10 operational reconciliation' AS check_name,
       (SELECT SUM(net_product_cogs_operational_rub) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`) AS view_cogs,
       (SELECT cogs FROM indep) AS independent_cogs,
       IF((SELECT SUM(net_product_cogs_operational_rub) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`)
        = (SELECT cogs FROM indep), 'PASS', 'FAIL') AS verdict;

-- ── S-11. Наборы разрешаются ровно один раз, без double counting ────────────
SELECT 'S-11 bundles' AS check_name,
       COUNT(DISTINCT internal_sku) AS bundle_skus_traded,
       COUNTIF(cogs_resolution_status <> 'RESOLVED') AS unresolved_bundle_events,
       COUNTIF(cogs_match_count <> 1) AS wrong_match_count,
       IF(COUNTIF(cogs_resolution_status <> 'RESOLVED') = 0
          AND COUNTIF(cogs_match_count <> 1) = 0, 'PASS', 'FAIL') AS verdict
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
WHERE is_bundle;

-- ── S-14. Настоящее состояние покрытия (диагностика, не гейт) ───────────────
SELECT 'S-14 coverage' AS check_name, cogs_resolution_status,
       COUNT(*) AS events, SUM(signed_units) AS net_units,
       SUM(product_cogs_signed_rub) AS product_cogs_rub
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
GROUP BY cogs_resolution_status ORDER BY events DESC;
