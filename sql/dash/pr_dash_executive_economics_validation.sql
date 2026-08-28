-- ============================================================================
-- STAGE 3.1D — VALIDATION для V_DASH_EXECUTIVE_ECONOMICS_DAILY
-- Дата: 2026-08-28.  Запускать ПОСЛЕ pr_dash_executive_economics_v1.sql.
--
-- Проверки динамические: сверяются ОТНОШЕНИЯ, а не зашитые значения.
-- Абсолютные числа контрольных окон живут в docs/, а не в ASSERT: FACT_*
-- штатно обновляются, и захардкоженный снимок сломал бы валидацию by design.
-- Любой FAIL прерывает скрипт — выкат считается неуспешным.
-- ============================================================================

-- ── D-1. Грейн 1:1 с источником. Overlay не создаёт и не теряет строк. ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`)
       = (SELECT COUNT(*) FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
) AS 'D-1 FAIL: грейн нового слоя не 1:1 с V_DASH_FINANCE_CORRECTED_DAILY';

-- ── D-2. Ключ `day` уникален. Дубликат означал бы размножение join-ом. ──
ASSERT (
  SELECT COUNT(*) = 0 FROM (
    SELECT day FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` GROUP BY day HAVING COUNT(*) > 1)
) AS 'D-2 FAIL: дубликаты day в новом слое';

-- ── D-3. Pass-through pre-COGS полей идентичен источнику на КАЖДОЙ строке. ──
--        Новый слой не имеет права незаметно сдвинуть принятые числа PR2.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` e
  JOIN `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`   f USING (day)
  WHERE e.period_result_pre_cogs_corrected_rub IS DISTINCT FROM f.period_result_pre_cogs_corrected_rub
     OR e.revenue_base_period_result_rub       IS DISTINCT FROM f.revenue_base_period_result_rub
     OR e.contribution_pre_cogs_rub            IS DISTINCT FROM f.contribution_pre_cogs_rub
     OR e.sales_revenue_seller_base_rub        IS DISTINCT FROM f.sales_revenue_seller_base_rub
     OR e.period_result_eligible               IS DISTINCT FROM f.period_result_eligible
) AS 'D-3 FAIL: pass-through pre-COGS поля разошлись с источником';

-- ── D-4. Суточный COGS == независимый пересчёт по SKU на том же универсуме. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` e
  LEFT JOIN (
    SELECT day,
      IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED','NOT_APPLICABLE')) = 0,
         SUM(net_product_cogs_operational_rub), NULL) AS cogs
    FROM `wb_mart.V_MART_SKU_DAILY_COGS` GROUP BY day
  ) s USING (day)
  WHERE e.product_cogs_rub IS DISTINCT FROM s.cogs
) AS 'D-4 FAIL: product_cogs_rub != независимый агрегат по SKU';

-- ── D-5. after = pre - COGS на КАЖДОЙ eligible-строке, без исключений. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
  WHERE after_product_cogs_eligible
    AND period_result_after_product_cogs_rub
        IS DISTINCT FROM (period_result_pre_cogs_corrected_rub - product_cogs_rub)
) AS 'D-5 FAIL: after != pre - COGS на eligible-строках';

-- ── D-6. Числитель и знаменатель имеют ОДНУ И ТУ ЖЕ маску. ──
--        Это ровно тот дефект, который чинил Stage 3.1C PR2.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
  WHERE (period_result_after_product_cogs_rub IS NULL)
     <> (revenue_base_after_product_cogs_rub IS NULL)
) AS 'D-6 FAIL: числитель и знаменатель after-COGS живут на разных масках';

-- ── D-7. Непокрытый COGS НИКОГДА не превращается в ноль. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
  WHERE NOT product_cogs_covered
    AND (product_cogs_rub IS NOT NULL
      OR period_result_after_product_cogs_rub IS NOT NULL
      OR revenue_base_after_product_cogs_rub  IS NOT NULL)
) AS 'D-7 FAIL: непокрытые сутки отдают значение вместо NULL';

-- ── D-8. Ноль COGS допустим ТОЛЬКО как доказуемое отсутствие событий. ──
--        product_cogs_rub = 0 при непустом числе единиц выкупа — противоречие.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`
  WHERE product_cogs_rub = 0 AND product_cogs_units > 0
) AS 'D-8 FAIL: нулевой COGS при ненулевых единицах выкупа';

-- ── D-9. Fail-closed статусов Stage 3.1B транслируется в суточный слой. ──
--        Каждые сутки с неразрешённой строкой витрины обязаны быть непокрыты.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` e
  JOIN (
    SELECT day, COUNTIF(cogs_resolution_status IN
      ('UNMAPPED_SKU','UNKNOWN_NO_INTERVAL','RETURN_LINK_UNRESOLVED','CONTRACT_VIOLATION_MULTI')) AS bad
    FROM `wb_mart.V_MART_SKU_DAILY_COGS` GROUP BY day
  ) s USING (day)
  WHERE s.bad > 0 AND e.product_cogs_covered
) AS 'D-9 FAIL: сутки с UNMAPPED/UNKNOWN/RETURN_LINK/CONTRACT_VIOLATION помечены покрытыми';

-- ── D-10. Семантика возврата Stage 3.1B не изменена: реверс берёт
--          себестоимость ИСХОДНОЙ продажи, знак сохранён. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_FACT_FINANCE_COGS`
  WHERE supplier_oper_name = 'Возврат'
    AND (cogs_return_basis IS DISTINCT FROM 'ORIGINAL_SALE'
      OR (linked_sale_match_count = 1 AND cogs_effective_date IS DISTINCT FROM linked_sale_date)
      OR product_cogs_signed_rub > 0)
) AS 'D-10 FAIL: семантика original-sale reversal нарушена';

-- ── D-11. Bundle COGS приходит ТОЛЬКО через существующий resolver. ──
--          Ни один набор не имеет себестоимости вне evetis_ref.
-- ANTI-JOIN, а не коррелированный NOT EXISTS: BigQuery не декоррелирует
-- подзапрос с BETWEEN по внешней колонке.
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.V_MART_SKU_DAILY_COGS` c
  LEFT JOIN `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` v
         ON v.internal_sku     = c.internal_sku
        AND v.product_cogs_rub = c.unit_product_cogs_rub
        AND c.day BETWEEN v.effective_from AND COALESCE(v.effective_to, DATE '9999-12-31')
  WHERE c.is_bundle
    AND c.unit_product_cogs_rub IS NOT NULL
    AND v.internal_sku IS NULL
) AS 'D-11 FAIL: bundle COGS не разрешается через evetis_ref.V_PRODUCT_COGS_EFFECTIVE';

-- ── D-12. Объекты Stage 3.1A / 3.1B / 3.1C не изменены (структурный контроль). ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `evetis_ref.INFORMATION_SCHEMA.TABLES`) = 4
     AND (SELECT COUNT(*) FROM `wb_mart.INFORMATION_SCHEMA.TABLES`)    = 41
) AS 'D-12 FAIL: состав датасетов отличается от ожидаемого (evetis_ref 4, wb_mart 41)';

-- ── D-13. Прежний слой PR2 не потерял ни строки. ──
ASSERT (
  SELECT (SELECT COUNT(*) FROM `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`)
       = (SELECT COUNT(*) FROM `wb_mart.V_DASH_KPI_DAILY`)
) AS 'D-13 FAIL: V_DASH_FINANCE_CORRECTED_DAILY разошёлся с V_DASH_KPI_DAILY';

-- ── D-14. В слое нет ratio-колонок (правило контракта дашборда v2). ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name = 'V_DASH_EXECUTIVE_ECONOMICS_DAILY'
    AND (column_name LIKE '%_pct%' OR column_name LIKE '%margin%' OR column_name LIKE '%ratio%')
) AS 'D-14 FAIL: в слое появилась ratio-колонка';

-- ── D-15. Запрещённая терминология не просочилась в имена колонок. ──
ASSERT (
  SELECT COUNT(*) = 0
  FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE table_name = 'V_DASH_EXECUTIVE_ECONOMICS_DAILY'
    AND (column_name LIKE '%profit%' OR column_name LIKE '%gross_margin%' OR column_name LIKE '%ebitda%')
) AS 'D-15 FAIL: имя колонки использует запрещённую терминологию прибыли';
