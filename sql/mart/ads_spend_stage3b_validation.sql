-- ============================================================================
-- EVETIS WB — Stage 3B. ПРИЁМКА после deploy. Дата: 20.08.2026.
-- 🔴 Только SELECT. Ни одного DDL/DML. Запускать ПОСЛЕ:
--      1) deploy sql/mart/pr_mart1_facts.sql   (пере-создание процедуры)
--      2) deploy sql/mart/pr_mart2b_sku_daily.sql
--      3) deploy sql/mart/ads_spend_reconciliation_v1.sql
--      4) deploy sql/mart/ads4_funnel_v1.sql + sql/mart/dashboard_layer_v1.sql
--      5) CALL wb_mart.sp_bootstrap_facts('');
--         CALL wb_mart.sp_build_mart_sku_daily(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY), NULL, '');
--
-- 🔴 ПРИНЦИП ПРИЁМКИ: НИ ОДНА проверка не сверяется с записанной заранее суммой.
--    Прежние цифры приёмки (523 365,38 / 514 064,00 / 512 997,00 / 1 067,00) в
--    условиях НЕ участвуют: витрина пересобирается ежедневно, и такой гейт через
--    сутки начал бы падать на исправных данных, а его отключение выглядело бы
--    как «проверка есть». Проверяются ОТНОШЕНИЯ внутри ОДНОЙ И ТОЙ ЖЕ сборки.
-- ============================================================================

-- ── V1. K9: репозиторий == production. Хеш каждого изменённого объекта. ──────
--     Сверить с хешами файлов репозитория тем же способом, что и на прошлых
--     раскатках. Расхождение = раскатан не тот текст, дальше не идти.
SELECT routine_name AS obj, TO_HEX(SHA256(routine_definition)) AS prod_sha, LENGTH(routine_definition) AS len
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.ROUTINES`
WHERE routine_name IN ('sp_bootstrap_facts', 'sp_build_mart_sku_daily')
UNION ALL
SELECT table_name, TO_HEX(SHA256(view_definition)), LENGTH(view_definition)
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.VIEWS`
WHERE table_name IN ('V_ADS_SPEND_RECONCILIATION', 'V_ADS_SPEND_RECONCILIATION_DAILY',
                     'V_ADS_FUNNEL_SKU_28D', 'V_ADS_SCREEN_SKU')
UNION ALL
SELECT table_name, TO_HEX(SHA256(view_definition)), LENGTH(view_definition)
FROM `project-fa311fc0-4d87-4781-986.wb_raw.INFORMATION_SCHEMA.VIEWS`
WHERE table_name = 'V_ADV_COSTS_DAY_COVERAGE'
ORDER BY obj;

-- ── V2. Остаток распределения по КАЖДОЙ паре (date, advert_id). ──────────────
--     Ожидается: pairs_nonzero = 0, max_abs_residual = 0.
SELECT
  COUNTIF(residual_rub <> 0)   AS pairs_nonzero,
  MAX(ABS(residual_rub))       AS max_abs_residual,
  COUNT(*)                     AS pairs_total
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION`;

-- ── V3. Полнота биллинга: распределённое + нераспределённое = списанное. ─────
--     Ожидается: diff = 0. Сравниваются ТРИ объекта одной сборки, а не память.
SELECT
  (SELECT SUM(billed_alloc_rub)  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`)   AS allocated,
  (SELECT IFNULL(SUM(unallocated_rub), 0) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY`) AS unallocated,
  (SELECT SUM(actual_spend_rub)  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`)         AS billing_actual,
  (SELECT SUM(billed_alloc_rub)  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`)
+ (SELECT IFNULL(SUM(unallocated_rub), 0) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY`)
- (SELECT SUM(actual_spend_rub)  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`)         AS diff;

-- ── V4. Legacy НЕ тронут: attributed тождественен ad_spend ПОСТРОЧНО. ────────
--     Сумма совпала бы и при перестановке значений между строками — поэтому
--     проверка построчная, по грейну (day, nm_id).
SELECT
  COUNT(*)                                                AS rows_total,
  COUNTIF(ad_spend_attributed_rub <> ad_spend)            AS mismatch_rows,
  SUM(ad_spend) - SUM(ad_spend_attributed_rub)            AS sum_diff,
  SUM(ad_spend_attributed_rub) - (
    SELECT SUM(stats_spend_rub)
    FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY` f
    WHERE f.`date` <= (SELECT MAX(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)
      AND f.nm_id IN (SELECT nm_id FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
                      WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL)) AS attributed_vs_fact_diff
FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`;

-- ── V5. Новые грейны: без дублей и без NULL. Ожидается всё по нулям. ─────────
SELECT 'FACT_ADS_SPEND_ALLOC_DAILY' AS t,
  COUNT(*) - COUNT(DISTINCT FORMAT('%t|%t|%t', `date`, advert_id, nm_id)) AS dup_rows,
  COUNTIF(`date` IS NULL OR advert_id IS NULL OR nm_id IS NULL)           AS null_grain,
  COUNTIF(billed_alloc_rub IS NULL)                                       AS null_value,
  COUNTIF(alloc_weight < 0 OR alloc_weight > 1)                           AS weight_out_of_range
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`
UNION ALL
SELECT 'FACT_ADS_SPEND_UNALLOC_DAILY',
  COUNT(*) - COUNT(DISTINCT FORMAT('%t|%t', `date`, advert_id)),
  COUNTIF(`date` IS NULL OR advert_id IS NULL),
  COUNTIF(unallocated_rub IS NULL),
  COUNTIF(reason NOT IN ('NO_STATS_ROWS', 'ZERO_STATS_SPEND'))
FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY`;

-- ── V6. billed_complete — ТОЧНАЯ копия coverage-контракта, без логики витрины. ─
--     🔴 Сравнение с MAX(FACT_ADS_COSTS_DAILY.date) ЗАПРЕЩЕНО и здесь не делается:
--     правило «day <= MAX(date)» опровергнуто (docs/ADS_COSTS_COVERAGE_CONTRACT_2026-08-20.md).
--     Ожидается: violations = 0. Отсутствие строки покрытия обязано читаться как FALSE.
WITH m AS (SELECT day, ANY_VALUE(billed_complete) flag
           FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` GROUP BY day)
SELECT
  COUNTIF(m.flag <> IFNULL(c.billed_complete, FALSE))          AS violations,
  COUNTIF(NOT m.flag)                                          AS incomplete_days,
  CAST(MAX(IF(NOT m.flag, m.day, NULL)) AS STRING)             AS last_incomplete_day,
  COUNTIF(c.`date` IS NULL)                                    AS days_without_coverage_row
FROM m LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS_DAY_COVERAGE` c
  ON c.`date` = m.day;

-- ── V7. Второй флаг — про ДРУГОЕ. И unallocated не потерян по дороге в MART. ─
--     mart_unallocated_rub из сверки обязан совпасть с day-level колонкой витрины.
--     Ожидается: flag_violations = 0, days_mismatch = 0.
WITH m AS (
  SELECT day,
         SUM(ad_spend_billed_rub)                  AS billed_sum,
         ANY_VALUE(has_unallocated_billing)        AS has_unalloc,
         ANY_VALUE(billed_complete)                AS billed_complete,
         ANY_VALUE(billing_allocation_complete)    AS alloc_complete
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`
  GROUP BY day
),
r AS (SELECT `date`, billed_valid_sku_rub, billed_not_carried_by_mart_rub, billing_actual_spend_rub
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION_DAILY`)
SELECT
  COUNTIF(m.billed_complete AND m.alloc_complete <> (NOT m.has_unalloc))      AS flag_violations,
  COUNTIF(NOT m.billed_complete AND m.alloc_complete IS NOT NULL)             AS flag_not_null_on_incomplete,
  -- булев флаг витрины обязан соответствовать рублям сверки — иначе «всё распределено»
  -- было бы утверждением, не связанным ни с какой суммой.
  COUNTIF(m.has_unalloc <> (IFNULL(r.billed_not_carried_by_mart_rub, 0) > 0)) AS flag_vs_recon_mismatch,
  -- витрина несёт ровно valid-SKU часть, а остальное видно только в сверке.
  COUNTIF(ABS(m.billed_sum - IFNULL(r.billed_valid_sku_rub, 0)) > 0.005)      AS carried_part_mismatch,
  COUNTIF(ABS(m.billed_sum + IFNULL(r.billed_not_carried_by_mart_rub, 0)
              - IFNULL(r.billing_actual_spend_rub, 0)) > 0.005)               AS day_invariant_violations,
  SUM(IFNULL(r.billed_not_carried_by_mart_rub, 0))                           AS not_carried_total_rub
FROM m LEFT JOIN r ON r.`date` = m.day;

-- ── V8. На неполных сутках billed-KPI строго NULL. Ожидается: 0 нарушений. ───
--     И контроль обратного: на полных сутках метрика существует там, где
--     существует знаменатель — иначе «всё NULL» тоже прошло бы этот гейт.
SELECT
  COUNTIF(NOT billed_complete AND (drr_orders_billed IS NOT NULL
        OR drr_buyouts_billed IS NOT NULL OR roas_billed IS NOT NULL
        OR hybrid_day_contribution_pre_cogs_billed IS NOT NULL
        OR settlement_day_contribution_pre_cogs_billed IS NOT NULL
        OR billing_allocation_complete IS NOT NULL))                  AS not_null_on_incomplete,
  COUNTIF(billed_complete AND buyouts_rub > 0 AND drr_buyouts_billed IS NULL) AS null_on_complete_with_denominator,
  COUNTIF(billed_complete AND hybrid_day_contribution_pre_cogs_billed IS NULL) AS hybrid_null_on_complete,
  -- 🔴 денежной колонки остатка в витрине БЫТЬ НЕ ДОЛЖНО (ревью 20.08): её natural grain — сутки.
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
   WHERE table_name = 'MART_SKU_DAILY' AND column_name = 'ad_spend_unallocated_rub') AS forbidden_money_column
FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`;

-- ── V9. Сдвиг ДРР по КАЖДОМУ SKU — и объяснение сдвига. ─────────────────────
--     🔴 Направление обязано быть однонаправленным и полностью объяснимым:
--     drr_billed / drr_attributed = billed / attributed по тому же SKU.
--     explain_gap ожидается ~0 у каждой строки. Считается ТОЛЬКО по полным суткам.
SELECT
  nm_id,
  SUM(ad_spend_attributed_rub)                              AS attributed_rub,
  SUM(ad_spend_billed_rub)                                  AS billed_rub,
  SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(buyouts_rub)) AS drr_buyouts_attributed,
  SAFE_DIVIDE(SUM(ad_spend_billed_rub),     SUM(buyouts_rub)) AS drr_buyouts_billed,
  SAFE_DIVIDE(SUM(ad_spend_billed_rub), SUM(ad_spend_attributed_rub)) AS billed_to_attributed_ratio,
  SAFE_DIVIDE(SUM(ad_spend_billed_rub),     SUM(buyouts_rub))
  - SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(buyouts_rub))
    * SAFE_DIVIDE(SUM(ad_spend_billed_rub), SUM(ad_spend_attributed_rub))  AS explain_gap
FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`
WHERE billed_complete
GROUP BY nm_id
HAVING SUM(ad_spend_attributed_rub) > 0
ORDER BY attributed_rub DESC;

-- ── V10. Сдвиг вклада по КАЖДОМУ SKU. ───────────────────────────────────────
--     🔴 Ожидаемое направление: billed < attributed, значит вычитается МЕНЬШЕ
--     и вклад РАСТЁТ. delta_check обязана быть ~0: вся разница вклада должна
--     объясняться ровно разницей расхода и ничем иным.
--     Сравнение — только по суткам с billed_complete, иначе сравнивались бы
--     разные периоды (у billed-версии последние сутки NULL).
SELECT
  nm_id,
  SUM(hybrid_day_contribution_pre_cogs)          AS hybrid_attributed,
  SUM(hybrid_day_contribution_pre_cogs_billed)   AS hybrid_billed,
  SUM(hybrid_day_contribution_pre_cogs_billed)
    - SUM(hybrid_day_contribution_pre_cogs)      AS hybrid_delta,
  SUM(ad_spend_attributed_rub) - SUM(ad_spend_billed_rub) AS spend_delta,
  (SUM(hybrid_day_contribution_pre_cogs_billed) - SUM(hybrid_day_contribution_pre_cogs))
    - (SUM(ad_spend_attributed_rub) - SUM(ad_spend_billed_rub))          AS delta_check
FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`
WHERE billed_complete
GROUP BY nm_id
ORDER BY ABS(SUM(hybrid_day_contribution_pre_cogs_billed) - SUM(hybrid_day_contribution_pre_cogs)) DESC;

-- ── V10b. Разложение биллинга на ТРИ взаимоисключающие части. ───────────────
--     Ожидается: residual_pairs = 0 и decomposition_diff = 0.
--     actual = valid-SKU + outside-SKU-universe + no-allocation-basis.
SELECT
  COUNTIF(residual_rub <> 0)                                        AS residual_pairs,
  SUM(billing_actual_spend_rub)                                     AS actual_total,
  SUM(billed_valid_sku_rub)                                         AS valid_sku_total,
  SUM(billed_outside_sku_universe_rub)                              AS outside_universe_total,
  SUM(billed_no_allocation_basis_rub)                               AS no_basis_total,
  SUM(billing_actual_spend_rub) - SUM(billed_valid_sku_rub)
    - SUM(billed_outside_sku_universe_rub)
    - SUM(billed_no_allocation_basis_rub)                           AS decomposition_diff
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION`;

-- ── V11. Физика новых FACT: партиционирование и кластеризация не потеряны. ───
SELECT table_name,
       COUNTIF(is_partitioning_column = 'YES')            AS partition_cols,
       COUNTIF(clustering_ordinal_position IS NOT NULL)   AS cluster_cols
FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('FACT_ADS_SPEND_ALLOC_DAILY', 'FACT_ADS_SPEND_UNALLOC_DAILY')
GROUP BY table_name;
-- ожидается: ALLOC — partition 1, cluster 2;  UNALLOC — partition 1, cluster 1.

-- ── V12. Аддитивность: единая сборка, единый run_id, старые колонки на месте. ─
SELECT
  COUNT(DISTINCT mart_run_id)  AS run_ids,
  CAST(MAX(day) AS STRING)     AS max_day,
  COUNT(*)                     AS rows_total,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
   WHERE table_name = 'MART_SKU_DAILY') AS columns_total,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
   WHERE table_name = 'MART_SKU_DAILY'
     AND column_name IN ('ad_spend', 'drr_orders', 'drr_buyouts', 'roas', 'acos',
                         'hybrid_day_contribution_pre_cogs', 'settlement_day_contribution_pre_cogs')) AS legacy_columns_present,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
   WHERE table_name = 'MART_SKU_DAILY'
     AND column_name IN ('ad_spend_attributed_rub', 'ad_spend_billed_rub', 'billed_complete',
                         'has_unallocated_billing', 'billing_allocation_complete')) AS new_flag_columns_present
FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`;
-- ожидается: run_ids = 1; legacy_columns_present = 7 (ни одна старая колонка не исчезла).
