-- ============================================================================
-- PR-A · Инварианты целостности ingestion финансового контура (A2 / A3 / A5d)
--
-- Назначение: регулярный детектор. Отвечает на один вопрос — не нарушился ли
--   ключ на слое RAW и не начал ли дедуп CANONICAL съедать реальные данные.
--
-- РЕЖИМ: read-only. Ни одной записи.
-- ОКНО: без ограничения по дате — проверяется вся история (в отличие от
--   sql/finance/pr_a_regression_check.sql, где окно заморожено на срезе REV2.4).
-- КРИТЕРИЙ: все строки verdict = 'PASS' (violations = 0). Любое срабатывание →
--   ingestion alert / failed run, разбор до следующей загрузки.
--
-- Спека: docs/FINANCE_PR_A_INGESTION_INTEGRITY_2026-08-11.md §2 A2, A3, A5d.
-- Прогнано 12.08.2026 на 205 638 строках RAW / 203 928 rrd_id: 4/4 PASS.
-- ============================================================================

WITH raw_src AS (
  SELECT * FROM `wb_raw.RAW_WB_FINANCE`
),

-- ---------------------------------------------------------------------------
-- QC-0 (A1, зеркало ASSERT из pr_mart1_facts.sql) — инвариант CANONICAL.
--   Ровно ОДНА строка на rrd_id. Срабатывание = дедуп weekly/daily не сработал,
--   витрина задвоится. В mart это ASSERT (fail-closed), здесь — детектор.
-- ---------------------------------------------------------------------------
qc0 AS (
  SELECT COUNT(*) - COUNT(DISTINCT rrd_id) AS violations
  FROM `wb_raw.V_WB_FINANCE_CANONICAL`
),

-- ---------------------------------------------------------------------------
-- QC-1 (A3) — настоящий первичный ключ RAW: (report_id, rrd_id).
--   Ожидание всегда 0, для legacy и для нового лоадера одинаково.
--   Ловит повторную запись одного и того же отчёта после network-таймаута
--   или падения execution — то, от чего защищает A5a/A5b.
-- ---------------------------------------------------------------------------
qc1 AS (
  SELECT COUNT(*) AS violations FROM (
    SELECT report_id, rrd_id
    FROM raw_src
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
  )
),

-- ---------------------------------------------------------------------------
-- QC-2 (A3) — повтор rrd_id в RAW допустим ТОЛЬКО как ожидаемая пара
--   DAILY + WEEKLY (двухслойность RAW — задокументированный дизайн,
--   docs/FINANCE_DAILY_DESIGN_2026-07-22.md:41,53-57). Любая другая
--   комбинация — дефект ingestion.
--
--   Отличие от формулировки спеки: фильтр `report_type IS NOT NULL` заменён на
--   покрытие всей таблицы с сентинелом 'LEGACY' для строк до cutover 13.07.2026.
--   Причина: фильтр создавал слепое пятно — rrd_id, у которого одна строка
--   legacy и одна DAILY, после фильтра остался бы с COUNT(*) = 1 и прошёл бы
--   проверку насквозь. Сентинел закрывает этот случай.
--   Проверено 12.08.2026: rrd_id, живущих одновременно в legacy и новом
--   контуре, — 0; обе формы дают одинаковый результат на текущих данных,
--   форма ниже строго сильнее.
-- ---------------------------------------------------------------------------
qc2 AS (
  SELECT COUNT(*) AS violations FROM (
    SELECT rrd_id
    FROM raw_src
    GROUP BY rrd_id
    HAVING COUNT(*) > 1
       AND (COUNT(*) > 2
            OR STRING_AGG(DISTINCT IFNULL(report_period, 'LEGACY')
                          ORDER BY IFNULL(report_period, 'LEGACY')) <> 'DAILY,WEEKLY')
  )
),

-- ---------------------------------------------------------------------------
-- QC-3 (A2) — natural key: под одним rrd_id WB не может прислать РАЗНЫЕ данные.
--   payload_hash считается на лету, НЕ материализуется: в RAW_WB_FINANCE 74
--   колонки, такой колонки среди них нет, а загрузчик работает с
--   ignoreUnknownValues:true (WbBigQuery.gs:181-223) — материализация была бы
--   молчаливой потерей поля при зелёном load-job.
--
--   В хэш входят ТОЛЬКО источниковые поля. Намеренно исключены report_id,
--   run_id, loaded_at, load_id, report_period, report_type — иначе пара
--   DAILY/WEEKLY по одному rrd_id всегда давала бы два разных хэша.
--   Этим payload_hash отличается от row_hash лоадера
--   (MD5('WB_API_FIN_V1|' || report_id || '|' || rrd_id)).
--
--   Формула обязана совпадать с CTE `payload` в pr_a_regression_check.sql.
--   Срабатывание = WB прислал под тем же rrd_id другие суммы, и дедуп
--   CANONICAL начал съедать реальную корректировку. Контракт «rrd_id —
--   natural key» доказан на наблюдаемой истории, а не гарантирован WB.
-- ---------------------------------------------------------------------------
payload AS (
  SELECT rrd_id, TO_HEX(MD5(CONCAT(
      IFNULL(supplier_oper_name,'~'),'|',IFNULL(doc_type_name,'~'),'|',IFNULL(srid,'~'),'|',
      IFNULL(wb_nm_id,'~'),'|',IFNULL(barcode,'~'),'|',IFNULL(sa_name,'~'),'|',IFNULL(ts_name,'~'),'|',
      IFNULL(order_dt,'~'),'|',IFNULL(sale_dt,'~'),'|',IFNULL(rr_dt,'~'),'|',
      IFNULL(quantity,'~'),'|',IFNULL(retail_price,'~'),'|',IFNULL(retail_amount,'~'),'|',
      IFNULL(retail_price_withdisc_rub,'~'),'|',IFNULL(sale_percent,'~'),'|',
      IFNULL(commission_percent,'~'),'|',IFNULL(spp_percent,'~'),'|',IFNULL(for_pay,'~'),'|',
      IFNULL(acquiring_fee,'~'),'|',IFNULL(logistics_amount,'~'),'|',IFNULL(rebill_logistics,'~'),'|',
      IFNULL(storage_fee,'~'),'|',IFNULL(deduction,'~'),'|',IFNULL(penalty,'~'),'|',
      IFNULL(acceptance,'~'),'|',IFNULL(additional_payment,'~'),'|',IFNULL(office_name,'~'),'|',
      IFNULL(warehouse_name,'~'),'|',IFNULL(region_name,'~')
  ))) AS payload_hash
  FROM raw_src
),
qc3 AS (
  SELECT COUNT(*) AS violations FROM (
    SELECT rrd_id
    FROM payload
    GROUP BY rrd_id
    HAVING COUNT(DISTINCT payload_hash) > 1
  )
),

checks AS (
  SELECT 'QC-0' AS check_id,
         'CANONICAL: одна строка на rrd_id (A1)'              AS check_name,
         (SELECT violations FROM qc0) AS violations,
         'Дедуп weekly/daily не сработал → витрина задвоится' AS on_violation
  UNION ALL
  SELECT 'QC-1',
         'RAW: PK (report_id, rrd_id) не нарушен (A3)',
         (SELECT violations FROM qc1),
         'Повторная запись отчёта → проверить A5a/A5b и манифест'
  UNION ALL
  SELECT 'QC-2',
         'RAW: повтор rrd_id только как пара DAILY+WEEKLY (A3)',
         (SELECT violations FROM qc2),
         'Незадокументированный третий слой или повтор внутри слоя'
  UNION ALL
  SELECT 'QC-3',
         'NATURAL KEY: один rrd_id — один payload (A2)',
         (SELECT violations FROM qc3),
         'WB прислал другие суммы под тем же rrd_id → дедуп съедает корректировку'
)

SELECT check_id, check_name, violations,
       IF(violations = 0, 'PASS', '🔴 FAIL') AS verdict,
       IF(violations = 0, '', on_violation)  AS action
FROM checks
ORDER BY check_id;


-- ============================================================================
-- ДЕТАЛИЗАЦИЯ ПРИ СРАБАТЫВАНИИ — раскомментировать нужный блок.
-- Держим закомментированными, чтобы файл оставался одним ответом «PASS/FAIL».
-- ============================================================================

-- QC-1: какие именно (report_id, rrd_id) задвоились
-- SELECT report_id, rrd_id, COUNT(*) c, STRING_AGG(DISTINCT run_id) run_ids,
--        MIN(loaded_at) first_load, MAX(loaded_at) last_load
-- FROM `wb_raw.RAW_WB_FINANCE`
-- GROUP BY 1,2 HAVING c > 1
-- ORDER BY c DESC, report_id LIMIT 100;

-- QC-2: какие rrd_id пришли в неожиданной комбинации слоёв
-- SELECT rrd_id, COUNT(*) c,
--        STRING_AGG(DISTINCT IFNULL(report_period,'LEGACY') ORDER BY IFNULL(report_period,'LEGACY')) periods,
--        STRING_AGG(DISTINCT report_id) report_ids
-- FROM `wb_raw.RAW_WB_FINANCE`
-- GROUP BY rrd_id
-- HAVING COUNT(*) > 1
--    AND (COUNT(*) > 2
--         OR STRING_AGG(DISTINCT IFNULL(report_period,'LEGACY') ORDER BY IFNULL(report_period,'LEGACY')) <> 'DAILY,WEEKLY')
-- ORDER BY c DESC LIMIT 100;

-- QC-3: расхождение payload — построчно, чтобы увидеть, ЧТО именно изменилось
-- WITH payload AS ( /* тот же CTE, что выше */ SELECT 1 )
-- SELECT r.*
-- FROM `wb_raw.RAW_WB_FINANCE` r
-- WHERE r.rrd_id IN (/* rrd_id из QC-3 */)
-- ORDER BY r.rrd_id, r.report_id;
