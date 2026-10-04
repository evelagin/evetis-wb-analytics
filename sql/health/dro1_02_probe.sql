-- ============================================================================
-- DRO-1 · §2 Проба: что известно о каждом конвейере на момент as_of.
-- Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md
--
-- Только наблюдение, никакой классификации и никаких порогов (они в контракте §1).
-- Функция параметризована моментом as_of, а не CURRENT_TIMESTAMP(): та же логика
-- исполняется детектором «сейчас» и backtest-ом «как было» (tools/dro1_health.py).
-- Поэтому каждая строка источника отбирается по времени её появления <= as_of.
--
-- 🔴 Часовые пояса — источник ошибок, проверено 2026-10-04 по журналам запусков:
--   * строковые loaded_at / load_ts у Apps Script (заказы, продажи, реклама, ставки,
--     статистика запросов, списания) записаны в МСК без зоны → PARSE_TIMESTAMP(.., 'Europe/Moscow');
--   * RAW_WB_FINANCE.loaded_at — ISO-8601 в UTC с суффиксом Z;
--   * все колонки типа TIMESTAMP — UTC.
--   Детектор ADS-1B (H4) читает snapshot_ts ставок как UTC и ошибается на 3 часа.
--
-- Нормализация статуса запуска:
--   SUCCESS | PARTIAL | FAILED | SKIPPED | RUNNING (ещё не завершён на as_of) | UNKNOWN.
--   ABANDONED (RUNNING дольше stale_run_minutes) вычисляется в §3 — порог в контракте.
-- Окно наблюдения — 45 суток: шире окна периодов (35 суток) с запасом на слот и лаг.
-- ============================================================================
CREATE OR REPLACE TABLE FUNCTION `evetis_health.TVF_PIPELINE_PROBE`(as_of TIMESTAMP)
OPTIONS (description = 'DRO-1. Наблюдение конвейеров на момент as_of: последняя бизнес-дата, последняя успешная загрузка, даты с данными, последний запуск, сигналы полноты. Без порогов и классификации.') AS (
WITH
clock AS (
  SELECT DATE(as_of, 'Europe/Moscow') AS d_msk,
         TIMESTAMP_SUB(as_of, INTERVAL 45 DAY) AS win_ts),

-- ── Журналы запусков, приведённые к одной форме ──────────────────────────────
runs_raw AS (
  SELECT CASE loader_name WHEN 'ads' THEN 'ads_daily' ELSE loader_name END AS pipeline_id,
         started_at, completed_at AS finished_at, status AS raw_status, error_code, logical_period AS period
  FROM `wb_raw.INGEST_RUNS`, clock
  WHERE loader_name IN ('orders', 'sales', 'ads') AND started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'finance', started_at, finished_at, status, CAST(NULL AS STRING), CAST(NULL AS DATE)
  FROM `wb_raw.FINANCE_LOADER_RUNS`, clock
  WHERE started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'ads_costs', ts, ts, IF(UPPER(http_success) = 'TRUE', status, 'FAILED'), CAST(NULL AS STRING), CAST(NULL AS DATE)
  FROM (SELECT SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', load_ts, 'Europe/Moscow') AS ts, status, http_success
        FROM `wb_raw.RAW_WB_ADV_COSTS_RUNS`), clock
  WHERE ts BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'ads_query_bids', ts, ts, IF(UPPER(http_success) = 'TRUE', status, 'FAILED'), CAST(NULL AS STRING), period
  FROM (SELECT SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', load_ts, 'Europe/Moscow') AS ts, status, http_success,
               SAFE.PARSE_DATE('%Y-%m-%d', snapshot_date) AS period
        FROM `wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS`, clock
        WHERE _PARTITIONTIME >= TIMESTAMP_SUB(win_ts, INTERVAL 1 DAY)), clock
  WHERE ts BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'ads_query_stats', ts, ts, IF(UPPER(http_success) = 'TRUE', status, IF(status = 'EMPTY', 'PARTIAL', 'FAILED')),
         CAST(NULL AS STRING), period
  FROM (SELECT SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', load_ts, 'Europe/Moscow') AS ts, status, http_success,
               SAFE.PARSE_DATE('%Y-%m-%d', period_from) AS period
        FROM `wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS`, clock
        WHERE _PARTITIONTIME >= TIMESTAMP_SUB(win_ts, INTERVAL 1 DAY)), clock
  WHERE ts BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'stocks_snapshot', started_at, completed_at, status, CAST(NULL AS STRING), DATE(started_at, 'Europe/Moscow')
  FROM `wb_raw.WB_STOCKS_SNAPSHOTS`, clock
  WHERE started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'wb_prices_observer', started_at, completed_at, status, error_code, DATE(started_at, 'Europe/Moscow')
  FROM `wb_raw.WB_PRICES_OBSERVATIONS`, clock
  WHERE environment = 'prod' AND started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'wb_tariffs_loader', started_at, completed_at, status, error_code, observation_date
  FROM `wb_raw.WB_TARIFF_OBSERVATIONS`, clock
  WHERE environment = 'prod' AND started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'ref_sync', started_at, finished_at, status, CAST(NULL AS STRING), CAST(NULL AS DATE)
  FROM `wb_raw.REF_SYNC_RUNS`, clock
  WHERE started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'mart', started_at, completed_at, status, error_code, target_date
  FROM `wb_mart.MART_RUNS`, clock
  WHERE environment = 'prod' AND started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT CASE loader_name WHEN 'funnel' THEN 'wb_funnel' WHEN 'storage' THEN 'wb_paid_storage'
                          WHEN 'promo' THEN 'wb_promo' WHEN 'ozon-unitka' THEN 'unitka_ozon' END,
         started_at, completed_at, status, error_code, CAST(NULL AS DATE)
  FROM `wb_raw.LOADER_RUNS`, clock
  WHERE environment = 'prod' AND loader_name IN ('funnel', 'storage', 'promo', 'ozon-unitka')
    AND started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'executive_v2', started_at, finished_at, status, CAST(NULL AS STRING), source_data_through
  FROM `wb_mart.EXECUTIVE_V2_BUILD_LOG`, clock
  WHERE started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'sku_performance_v2', started_at, finished_at, status, CAST(NULL AS STRING), source_data_through
  FROM `wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`, clock
  WHERE started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'unitka_wb', started_at, completed_at,
         IF(error_code IS NULL AND qa_status = 'PASS', 'COMPLETE', 'ERROR'), error_code, last_closed_date
  FROM `wb_ops.UNITKA_ENGINE_RUNS`, clock
  WHERE environment = 'prod' AND mode = 'WRITE' AND started_at BETWEEN win_ts AND as_of
  UNION ALL
  SELECT 'ct_refresh', run_ts, run_ts, status, CAST(NULL AS STRING), CAST(NULL AS DATE)
  FROM `evetis_ref.CT_REFRESH_LOG`, clock
  WHERE step = 'sp_ct_refresh_daily' AND run_ts BETWEEN win_ts AND as_of
  UNION ALL
  -- Журнал Ozon пишется только по завершении: строка видна с момента completed_at.
  SELECT CASE entity WHEN 'fbo_postings' THEN 'ozon_fbo_postings' WHEN 'finance_accrual' THEN 'ozon_finance_accrual'
                     WHEN 'ads_expense_daily' THEN 'ozon_ads_expense_daily' WHEN 'ads_sku_daily' THEN 'ozon_ads_sku_daily'
                     WHEN 'ads_campaigns' THEN 'ozon_ads_campaigns' WHEN 'catalog' THEN 'ozon_catalog'
                     WHEN 'prices' THEN 'ozon_prices' WHEN 'stocks' THEN 'ozon_stocks'
                     WHEN 'seller_info' THEN 'ozon_seller_info' WHEN 'supplies' THEN 'ozon_supplies'
                     WHEN 'clusters' THEN 'ozon_clusters' WHEN 'promo' THEN 'ozon_promo' END,
         started_at, completed_at, status, CAST(NULL AS STRING),
         IF(entity = 'ads_campaigns', DATE(completed_at, 'Europe/Moscow'), CAST(NULL AS DATE))
  FROM `ozon_raw.OZON_INGESTION_RUNS`, clock
  WHERE completed_at BETWEEN win_ts AND as_of),

runs AS (
  SELECT pipeline_id, started_at, finished_at, error_code, period,
         CASE
           WHEN finished_at IS NULL OR finished_at > as_of THEN 'RUNNING'
           WHEN raw_status IN ('COMPLETE', 'OK', 'OK_NO_NEW', 'SUCCESS') THEN 'SUCCESS'
           WHEN raw_status IN ('SKIP', 'SKIPPED_LOCKED') THEN 'SKIPPED'
           WHEN raw_status IN ('PARTIAL', 'EMPTY')
             OR (raw_status = 'ERROR' AND IFNULL(error_code, '') LIKE '%PARTIAL%') THEN 'PARTIAL'
           WHEN raw_status IN ('ERROR', 'FAILED') THEN 'FAILED'
           ELSE 'UNKNOWN'
         END AS run_status
  FROM runs_raw
  WHERE pipeline_id IS NOT NULL),

runs_agg AS (
  SELECT pipeline_id,
         ARRAY_AGG(STRUCT(run_status, error_code, started_at, finished_at) ORDER BY started_at DESC LIMIT 1)[OFFSET(0)] AS last_run,
         MAX(IF(run_status = 'SUCCESS', finished_at, NULL)) AS last_success_ts,
         ARRAY_AGG(IF(run_status = 'SUCCESS', finished_at, NULL) IGNORE NULLS) AS ok_run_ts,
         ARRAY_AGG(DISTINCT IF(run_status = 'SUCCESS', period, NULL) IGNORE NULLS) AS ok_periods,
         ARRAY_AGG(DISTINCT IF(run_status IN ('FAILED', 'PARTIAL'), period, NULL) IGNORE NULLS) AS failed_periods
  FROM runs
  GROUP BY pipeline_id),

-- ── Бизнес-даты, для которых данные уже лежали в таблице на as_of ────────────
data_dates AS (
  SELECT 'orders' AS pipeline_id, _order_date AS d,
         MAX(SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow')) AS loaded_ts
  FROM `wb_raw.RAW_WB_ORDERS`, clock
  WHERE _order_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk
    AND SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow') <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'sales', _sale_date, MAX(SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow'))
  FROM `wb_raw.RAW_WB_SALES_RETURNS`, clock
  WHERE _sale_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk
    AND SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', loaded_at, 'Europe/Moscow') <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'finance', _rr_date, MAX(ts)
  FROM (SELECT _rr_date, COALESCE(SAFE_CAST(loaded_at AS TIMESTAMP),
                                  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', loaded_at)) AS ts
        FROM `wb_raw.RAW_WB_FINANCE`, clock
        WHERE _rr_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk)
  WHERE ts <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ads_daily', d, MAX(ts)
  FROM (SELECT SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(date, 1, 10)) AS d,
               SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', load_ts, 'Europe/Moscow') AS ts
        FROM `wb_raw.RAW_WB_ADV_CAMPAIGN_STATS`, clock
        WHERE _PARTITIONTIME >= TIMESTAMP_SUB(win_ts, INTERVAL 1 DAY))
  WHERE ts <= as_of AND d IS NOT NULL
  GROUP BY 1, 2
  UNION ALL
  SELECT 'stocks_snapshot', _snapshot_date, MAX(snapshot_ts)
  FROM `wb_raw.RAW_WB_STOCKS`, clock
  WHERE _snapshot_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND snapshot_ts <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'wb_funnel', date_msk, MAX(observed_at)
  FROM `wb_raw.RAW_WB_FUNNEL_DAILY`, clock
  WHERE date_msk BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND observed_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'wb_paid_storage', date_msk, MAX(observed_at)
  FROM `wb_raw.RAW_WB_PAID_STORAGE`, clock
  WHERE date_msk BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND observed_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'promo_basis_wb', DATE(captured_at, 'Europe/Moscow'), MAX(captured_at)
  FROM `wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT`, clock
  WHERE captured_at BETWEEN win_ts AND as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'promo_basis_ozon', DATE(captured_at, 'Europe/Moscow'), MAX(captured_at)
  FROM `ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT`, clock
  WHERE captured_at BETWEEN win_ts AND as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ozon_fbo_postings', order_date, MAX(extracted_at)
  FROM `ozon_raw.RAW_OZON_POSTINGS_FBO`, clock
  WHERE order_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND extracted_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ozon_finance_accrual', event_date, MAX(extracted_at)
  FROM `ozon_raw.RAW_OZON_FINANCE_ACCRUAL`, clock
  WHERE event_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND extracted_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ozon_ads_expense_daily', date, MAX(extracted_at)
  FROM `ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`, clock
  WHERE date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND extracted_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ozon_ads_sku_daily', date, MAX(extracted_at)
  FROM `ozon_raw.RAW_OZON_ADS_SKU_DAILY`, clock
  WHERE date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND extracted_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ozon_catalog', snapshot_date, MAX(extracted_at)
  FROM `ozon_raw.RAW_OZON_CATALOG`, clock
  WHERE snapshot_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND extracted_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ozon_prices', snapshot_date, MAX(extracted_at)
  FROM `ozon_raw.RAW_OZON_PRICES`, clock
  WHERE snapshot_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND extracted_at <= as_of
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ozon_stocks', snapshot_date, MAX(extracted_at)
  FROM `ozon_raw.RAW_OZON_STOCKS`, clock
  WHERE snapshot_date BETWEEN DATE_SUB(d_msk, INTERVAL 45 DAY) AND d_msk AND extracted_at <= as_of
  GROUP BY 1, 2),

data_agg AS (
  SELECT pipeline_id, MAX(d) AS max_d, MAX(loaded_ts) AS max_loaded, ARRAY_AGG(DISTINCT d) AS dates
  FROM data_dates
  WHERE d IS NOT NULL
  GROUP BY pipeline_id),

-- ── Бизнес-дата, которую нельзя взять из строк данных ────────────────────────
-- Реклама Ozon: день без расходов не даёт строк, но успешный запуск окна 7 дн.
-- доказывает, что вчерашний день загрузчик уже видел. Списания WB — то же (D-1).
-- Executive / SKU V2: через какую дату собран слой (source_data_through).
-- Чисто журнальные конвейеры (без строк с бизнес-датой) — дата последнего успеха (МСК).
-- 🔴 Список закрытый: для конвейера, у которого есть строки данных, «успешный запуск»
--    не заменяет наличие данных за дату — иначе COMPLETE-запуск без строк спрятал бы пропуск.
extra_bd AS (
  SELECT pipeline_id,
         CASE
           WHEN pipeline_id IN ('ozon_ads_expense_daily', 'ozon_ads_sku_daily', 'ads_costs')
             THEN DATE_SUB(DATE(last_success_ts, 'Europe/Moscow'), INTERVAL 1 DAY)
           WHEN pipeline_id IN ('executive_v2', 'sku_performance_v2', 'unitka_wb', 'mart')
             THEN (SELECT MAX(p) FROM UNNEST(ok_periods) p)
           WHEN pipeline_id IN ('ref_sync', 'wb_prices_observer', 'wb_promo', 'unitka_ozon', 'ct_refresh',
                                'ozon_seller_info', 'ozon_supplies', 'ozon_clusters', 'ozon_promo')
             THEN DATE(last_success_ts, 'Europe/Moscow')
         END AS bd
  FROM runs_agg),

-- ── Сигналы полноты, которые уже пишут сами загрузчики ───────────────────────
completeness AS (
  SELECT 'stocks_snapshot' AS pipeline_id,
         IF(IFNULL(control_status, 'OK') = 'OK', 'COMPLETE', 'INCOMPLETE') AS completeness_status,
         IF(IFNULL(control_status, 'OK') = 'OK', CAST(NULL AS STRING), 'CONTROL_MISMATCH') AS completeness_reason,
         FORMAT('control_status=%s, availability_status=%s, снимок %s',
                IFNULL(control_status, '-'), IFNULL(availability_status, '-'),
                FORMAT_TIMESTAMP('%Y-%m-%d %H:%M UTC', completed_at)) AS completeness_detail
  FROM (SELECT control_status, availability_status, completed_at
        FROM `wb_raw.WB_STOCKS_SNAPSHOTS`, clock
        WHERE status = 'COMPLETE' AND completed_at <= as_of AND started_at >= win_ts
        ORDER BY completed_at DESC LIMIT 1)
  UNION ALL
  SELECT 'wb_prices_observer',
         IF(coverage_pct >= 95, 'COMPLETE', 'INCOMPLETE'),
         IF(coverage_pct >= 95, CAST(NULL AS STRING), 'COVERAGE_LOW'),
         FORMAT('coverage_pct=%s, наблюдение %s', CAST(coverage_pct AS STRING),
                FORMAT_TIMESTAMP('%Y-%m-%d %H:%M UTC', completed_at))
  FROM (SELECT coverage_pct, completed_at
        FROM `wb_raw.WB_PRICES_OBSERVATIONS`, clock
        WHERE environment = 'prod' AND status = 'COMPLETE' AND completed_at <= as_of
          AND started_at BETWEEN TIMESTAMP_SUB(as_of, INTERVAL 3 DAY) AND as_of
        ORDER BY completed_at DESC LIMIT 1)),

-- Списания рекламы WB: дни, которые вышли из окна перечитывания D-14 без доказанной
-- стабильности. Источник — живое вью покрытия, поэтому в backtest это значение
-- «на сегодня», а не на as_of (ограничение задокументировано).
ads_billing AS (
  SELECT 'ads_costs' AS pipeline_id,
         COUNTIF(NOT stable_ok AND NOT IFNULL(zero_spend_day, FALSE) AND age_days > 14
                 AND date >= DATE_SUB(d_msk, INTERVAL 35 DAY)) AS provisional_stuck_count
  FROM `wb_raw.V_ADV_COSTS_DAY_COVERAGE`, clock)

SELECT
  c.pipeline_id,
  as_of AS probe_as_of,
  (SELECT MAX(x) FROM UNNEST([da.max_d, e.bd, (SELECT MAX(p) FROM UNNEST(r.ok_periods) p)]) x) AS latest_business_date,
  (SELECT MAX(x) FROM UNNEST([r.last_success_ts, da.max_loaded]) x) AS latest_load_ts,
  ARRAY(SELECT DISTINCT x
        FROM UNNEST(ARRAY_CONCAT(IFNULL(da.dates, []), IFNULL(r.ok_periods, []))) x
        WHERE x IS NOT NULL ORDER BY x) AS dates_present,
  IFNULL(r.ok_periods, []) AS ok_periods,
  IFNULL(r.failed_periods, []) AS failed_periods,
  IFNULL(r.ok_run_ts, []) AS ok_run_ts,
  r.last_run.run_status AS run_status,
  r.last_run.error_code AS run_error_code,
  r.last_run.started_at AS run_started_at,
  r.last_run.finished_at AS run_finished_at,
  IFNULL(q.completeness_status, 'NOT_MEASURED') AS completeness_status,
  q.completeness_reason,
  q.completeness_detail,
  IFNULL(b.provisional_stuck_count, 0) AS provisional_stuck_count
FROM `evetis_health.V_PIPELINE_CONTRACT` c
LEFT JOIN runs_agg r USING (pipeline_id)
LEFT JOIN data_agg da USING (pipeline_id)
LEFT JOIN extra_bd e USING (pipeline_id)
LEFT JOIN completeness q USING (pipeline_id)
LEFT JOIN ads_billing b USING (pipeline_id)
WHERE c.evaluation_mode = 'EVALUATED'
);
