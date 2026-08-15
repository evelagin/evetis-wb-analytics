-- ══════════════════════════════════════════════════════════════
-- Dashboard Data Layer v1 — контракт 1 (`V_DATA_FRESHNESS`) и контракт 2 (вью экрана)
--
-- Спека: docs/DASHBOARD_DATA_LAYER_DESIGN_2026-08-15.md (ред. 4)
--
-- 🔴 ИНВАРИАНТЫ РЕАЛИЗАЦИИ (нарушение = откат, не правка):
--   1. Только CREATE OR REPLACE VIEW. Ни таблиц, ни INSERT, ни изменений
--      загрузчиков, job'ов и heartbeat. Откат = DROP VIEW (§0).
--   2. Существующие MART/FACT/Ads-4 объекты не затрагиваются — только чтение.
--   3. `last_attempt` и `last_success` — ЦЕЛЫЕ строки лога, выбранные одним и тем же
--      ROW_NUMBER() ... ORDER BY started_at DESC, run_id DESC. Ни одного агрегата
--      внутри last_success: два независимых MAX() дали бы несуществующий run (§1.5).
--   4. `built_at` — информационная колонка, в статусе НЕ участвует (§1.6).
--   5. CURRENT_DATE только с явной таймзоной 'Europe/Moscow'; CURRENT_DATE()
--      без таймзоны запрещён. Разрешено только в V_DATA_FRESHNESS (§1.9).
--   6. Пороги — свойство слоя: живут в CTE-константе `layers`, не в CASE (§1.8).
--   7. Справочник подключается LEFT JOIN и предварительно сведён к одной строке
--      на nm_id — fan-out структурно невозможен (§2.1).
--   8. Сигналы сворачиваются в строку запроса одной функцией приоритета,
--      той же самой, что использует гейт S7 (§2.2).
--   9. Ни одной колонки вклада без суффикса `_pre_cogs`, пока нет REF_COGS (§2.3).
--
-- 🔴 Отступления от буквы ред. 4 — перечислены в сопроводительной записке,
--    приняты БЕЗ решения аудитора быть не могут.
-- ══════════════════════════════════════════════════════════════


-- ────────────────────────────────────────────────────────────
-- 1/3 · wb_mart.V_DATA_FRESHNESS — светофор здоровья, 13 строк
--       12 слоёв данных + 1 детектор сирот (§1.3, §1.7)
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DATA_FRESHNESS` AS
WITH
-- реестр слоёв и порогов: порог есть свойство слоя, а не ветка CASE (§1.8)
layers AS (
  SELECT * FROM UNNEST([
    STRUCT(
      'ads_query_stats'     AS layer_code, 'ads'     AS layer_group, 'ads'     AS run_source,
      3                     AS data_age_ok_days,      36 AS success_age_ok_hours),
    ('ads_query_bids',       'ads',     'ads',      1, 36),
    ('ads_costs',            'ads',     'ads',      2, 36),
    ('ads_fullstats',        'ads',     'ads',      2, 36),
    ('orders',               'ops',     'orders',   1,  3),
    ('sales',                'ops',     'sales',    2,  4),
    ('stocks',               'ops',     'stocks',   1, 36),
    ('finance',              'finance', 'finance',  3, 20),
    ('mart_sku_daily',       'mart',    'mart',     2, 36),
    ('fact_ads_costs_daily', 'mart',    'mart',     2, 36),
    ('fact_ads_sku_daily',   'mart',    'mart',     2, 36),
    ('ref_sku_master',       'ref',     'ref',      CAST(NULL AS INT64), 72)
  ])
),

-- 🔴 нормализация форматов даты — здесь, один раз (§1.2)
data_as_of AS (
  SELECT 'ads_query_stats' AS layer_code,
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', period_from))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_STATS`) AS data_as_of
  UNION ALL SELECT 'ads_query_bids',
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', snapshot_date))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_BIDS`)
  UNION ALL SELECT 'ads_costs',
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', updDate))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_COSTS`)
  UNION ALL SELECT 'ads_fullstats',
         (SELECT MAX(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(date, 1, 10)))
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_CAMPAIGN_STATS`)
  UNION ALL SELECT 'orders',
         (SELECT MAX(order_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`)
  UNION ALL SELECT 'sales',
         (SELECT MAX(sale_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES`)
  UNION ALL SELECT 'stocks',
         (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`)
  UNION ALL SELECT 'finance',
         (SELECT MAX(finance_date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`)
  UNION ALL SELECT 'mart_sku_daily',
         (SELECT MAX(day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)
  UNION ALL SELECT 'fact_ads_costs_daily',
         (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`)
  UNION ALL SELECT 'fact_ads_sku_daily',
         (SELECT MAX(date) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`)
  UNION ALL SELECT 'ref_sku_master', CAST(NULL AS DATE)
),

-- built_at: справочная колонка. У сырых ads-слоёв её физически нет — остаётся NULL (§1.6)
built AS (
  SELECT 'orders' AS layer_code,
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`) AS built_at
  UNION ALL SELECT 'sales',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES`)
  UNION ALL SELECT 'stocks',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_STOCKS_SNAPSHOT`)
  UNION ALL SELECT 'finance',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE`)
  UNION ALL SELECT 'mart_sku_daily',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY`)
  UNION ALL SELECT 'fact_ads_costs_daily',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`)
  UNION ALL SELECT 'fact_ads_sku_daily',
         (SELECT MAX(built_at) FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`)
  UNION ALL SELECT 'ref_sku_master',
         (SELECT MAX(d._synced_at)
            FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER_DATA` d
            JOIN `project-fa311fc0-4d87-4781-986.wb_raw.REF_ACTIVE_VERSION` v
              ON d.ref_run_id = v.active_ref_run_id)
),

-- run-логи, приведённые к общей схеме. `selftest` не выбирается вовсе (§1.3)
run_attempts AS (
  SELECT 'ads' AS run_source, run_id, started_at, completed_at, status AS raw_status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.INGEST_RUNS` WHERE loader_name = 'ads'
  UNION ALL
  SELECT 'orders', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.INGEST_RUNS` WHERE loader_name = 'orders'
  UNION ALL
  SELECT 'sales', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.INGEST_RUNS` WHERE loader_name = 'sales'
  UNION ALL
  SELECT 'stocks', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.LOADER_RUNS` WHERE loader_name = 'stocks'
  UNION ALL
  SELECT 'mart', run_id, started_at, completed_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.LOADER_RUNS` WHERE loader_name = 'mart'
  UNION ALL
  SELECT 'finance', run_id, started_at, finished_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.FINANCE_LOADER_RUNS`
  UNION ALL
  SELECT 'ref', run_id, started_at, finished_at, status
    FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SYNC_RUNS`
),

-- карта нормализации статусов (§1.5). UNMAPPED = статус вне карты: падаем громко
attempts AS (
  SELECT
    l.layer_code,
    r.run_source,
    r.run_id,
    r.started_at,
    r.completed_at,
    r.raw_status,
    CASE
      WHEN r.raw_status IN ('COMPLETE', 'OK')   THEN 'SUCCESS'
      WHEN r.raw_status = 'OK_NO_NEW'           THEN 'SUCCESS_EMPTY'
      WHEN r.raw_status = 'PARTIAL'             THEN 'PARTIAL'
      WHEN r.raw_status IN ('ERROR', 'FAILED')  THEN 'FAILED'
      WHEN r.raw_status = 'STARTED'
           AND TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.started_at, HOUR) <= 6 THEN 'RUNNING'
      WHEN r.raw_status = 'STARTED'             THEN 'STUCK'
      ELSE 'UNMAPPED'
    END AS run_state
  FROM layers l
  JOIN run_attempts r ON r.run_source = l.run_source
),

-- 🔴 последняя попытка — целая строка
attempt_ranked AS (
  SELECT *, ROW_NUMBER() OVER (
             PARTITION BY layer_code
             ORDER BY started_at DESC, run_id DESC
           ) AS rn
  FROM attempts
),
last_attempt AS (SELECT * FROM attempt_ranked WHERE rn = 1),

-- 🔴 последний успех — тоже целая строка, тем же порядком. Агрегатов нет (§1.5)
success_ranked AS (
  SELECT *, ROW_NUMBER() OVER (
             PARTITION BY layer_code
             ORDER BY started_at DESC, run_id DESC
           ) AS rn
  FROM attempts
  WHERE run_state IN ('SUCCESS', 'SUCCESS_EMPTY')
),
last_success AS (SELECT * FROM success_ranked WHERE rn = 1),

base AS (
  SELECT
    l.layer_code,
    l.layer_group,
    l.run_source,
    d.data_as_of,
    IF(d.data_as_of IS NULL, NULL,
       DATE_DIFF(CURRENT_DATE('Europe/Moscow'), d.data_as_of, DAY))      AS data_age_days,
    l.data_age_ok_days,
    COALESCE(a.run_state, 'NO_RUN')                                     AS run_state,
    a.raw_status                                                        AS last_attempt_status,
    a.run_id                                                            AS last_attempt_run_id,
    a.started_at                                                        AS last_attempt_started_at,
    a.completed_at                                                      AS last_attempt_completed_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), a.started_at, HOUR)             AS run_age_hours,
    s.run_id                                                            AS success_run_id,
    s.raw_status                                                        AS success_raw_status,
    s.started_at                                                        AS success_started_at,
    s.completed_at                                                      AS success_completed_at,
    -- 🔴 обе метки — из ОДНОЙ строки last_success
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(),
                   COALESCE(s.completed_at, s.started_at), HOUR)        AS success_age_hours,
    l.success_age_ok_hours,
    b.built_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), b.built_at, HOUR)               AS build_age_hours
  FROM layers l
  LEFT JOIN data_as_of  d USING (layer_code)
  LEFT JOIN last_attempt a USING (layer_code)
  LEFT JOIN last_success s USING (layer_code)
  LEFT JOIN built       b USING (layer_code)
),

scored AS (
  SELECT
    base.*,
    CASE
      WHEN run_state IN ('FAILED', 'STUCK', 'NO_RUN', 'UNMAPPED')            THEN 'ERROR'
      WHEN success_run_id IS NULL                                           THEN 'ERROR'
      WHEN run_state = 'PARTIAL'                                            THEN 'STALE'
      WHEN success_age_hours > success_age_ok_hours                         THEN 'STALE'
      WHEN data_age_ok_days IS NOT NULL
           AND data_age_days > data_age_ok_days                             THEN 'STALE'
      ELSE 'OK'
    END AS status,
    CASE
      WHEN run_state = 'NO_RUN'
        THEN 'для слоя не найдено ни одной релевантной попытки'
      WHEN run_state = 'UNMAPPED'
        THEN FORMAT('статус вне карты §1.5: %s', COALESCE(last_attempt_status, 'NULL'))
      WHEN run_state = 'STUCK'
        THEN FORMAT('прогон STUCK %d ч', run_age_hours)
      WHEN run_state = 'FAILED'
        THEN FORMAT('последняя попытка FAILED (%s)', COALESCE(last_attempt_status, 'NULL'))
      WHEN success_run_id IS NULL
        THEN 'нет ни одного успешного прогона'
      WHEN run_state = 'PARTIAL'
        THEN 'последняя попытка PARTIAL'
      WHEN success_age_hours > success_age_ok_hours
        THEN FORMAT('success_age=%dч>%dч', success_age_hours, success_age_ok_hours)
      WHEN data_age_ok_days IS NOT NULL AND data_age_days > data_age_ok_days
        THEN FORMAT('data_age=%d>%d', data_age_days, data_age_ok_days)
      ELSE NULL
    END AS status_reason
  FROM base
),

-- детектор сирот: смотрит на факты, а не на метаданные синка (§1.7)
orphans AS (
  SELECT COUNT(DISTINCT nm_id) AS orphan_nm_ids
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
  WHERE sku_match_status = 'not_found'
    AND order_date >= DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 90 DAY)
)

SELECT
  layer_code, layer_group, run_source,
  data_as_of, data_age_days, data_age_ok_days,
  run_state, last_attempt_status, last_attempt_run_id,
  last_attempt_started_at, last_attempt_completed_at, run_age_hours,
  success_run_id, success_raw_status, success_started_at, success_completed_at,
  success_age_hours, success_age_ok_hours,
  built_at, build_age_hours,
  CAST(NULL AS INT64) AS metric_value,
  status, status_reason,
  CURRENT_TIMESTAMP() AS generated_at
FROM scored

UNION ALL

SELECT
  'sku_orphans', 'qc', CAST(NULL AS STRING),
  CAST(NULL AS DATE), CAST(NULL AS INT64), CAST(NULL AS INT64),
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
  CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP), CAST(NULL AS INT64),
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP),
  CAST(NULL AS INT64), CAST(NULL AS INT64),
  CAST(NULL AS TIMESTAMP), CAST(NULL AS INT64),
  orphan_nm_ids,
  IF(orphan_nm_ids > 0, 'ERROR', 'OK'),
  FORMAT('SKU с продажами вне справочника за 90 сут: %d', orphan_nm_ids),
  CURRENT_TIMESTAMP()
FROM orphans;


-- ────────────────────────────────────────────────────────────
-- 2/3 · wb_mart.V_ADS_SCREEN_SKU — верхняя таблица экрана «Реклама»
--       Universe = рекламные SKU окна 28д (§2.1). Грейн (as_of_date, window_days, nm_id)
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SCREEN_SKU` AS
WITH ref_ranked AS (
  SELECT
    r.*,
    COUNT(*)     OVER (PARTITION BY nm_id)                      AS ref_rows_for_nm_id,
    ROW_NUMBER() OVER (PARTITION BY nm_id ORDER BY internal_sku) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` r
),
-- справочник сведён к одной строке на nm_id ДО join — fan-out невозможен
ref AS (SELECT * EXCEPT (rn) FROM ref_ranked WHERE rn = 1)
SELECT
  f.as_of_date,
  f.window_days,
  f.nm_id,
  -- атрибуты справочника
  r.internal_sku,
  r.product_name_short,
  r.product_name_full,
  r.category,
  r.line,
  r.product_type,
  r.brand,
  r.is_bundle,
  r.status                                    AS sku_status,
  r.active                                    AS sku_active,
  r.include_in_ads_analysis,
  (r.nm_id IS NULL)                           AS is_orphan,
  COALESCE(r.ref_rows_for_nm_id, 0)           AS ref_rows_for_nm_id,
  -- состав запросов по доказательности
  f.queries_total,
  f.queries_actionable,
  f.queries_observational,
  f.queries_insufficient,
  f.queries_zero_order,
  f.zero_order_spend_rub,
  f.queries_non_monotonic,
  f.queries_with_bid,
  -- воронка
  f.views_sum,
  f.clicks_sum,
  f.clicks_on_imp,
  f.atbs_sum,
  f.orders_sum,
  f.shks_sum,
  f.query_spend_rub,
  f.query_spend_on_imp_rub,
  f.ctr,
  f.cart_cr_clicks,
  f.order_cr_carts,
  f.order_cr_clicks,
  f.cpc_calc,
  f.cpo_ads,
  f.ctr_coverage_spend_share,
  -- сверка с денежной витриной
  f.mart_ad_spend_rub,
  f.query_spend_share_of_total,
  -- торговая часть и вклад ДО себестоимости (§2.3)
  f.orders_rub,
  f.buyouts_rub,
  f.orders_qty,
  f.hybrid_contribution_pre_cogs,
  f.settlement_contribution_pre_cogs,
  'PRE_COGS'                                                              AS economics_basis,
  'Себестоимость не подключена: вклад посчитан до COGS'                   AS economics_note
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SKU_28D` f
LEFT JOIN ref r ON r.nm_id = f.nm_id;


-- ────────────────────────────────────────────────────────────
-- 3/3 · wb_mart.V_ADS_SCREEN_QUERY — нижняя таблица экрана
--       Грейн (as_of_date, window_days, nm_id, norm_query); оба окна в одной вью
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SCREEN_QUERY` AS
WITH q AS (
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_28D`
  UNION ALL
  SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_90D`
),
-- 🔴 функция приоритета сигналов — контракт §2.2. Тот же CASE проверяет гейт S7
sig_prio AS (
  SELECT
    as_of_date, window_days, nm_id, norm_query, signal_code, signal_strength,
    CASE signal_code
      WHEN 'CTR_BELOW_BASELINE'            THEN 1
      WHEN 'CART_CR_BELOW_BASELINE'        THEN 2
      WHEN 'ORDER_CR_CARTS_BELOW_BASELINE' THEN 3
      WHEN 'CPO_BELOW_BASELINE'            THEN 4
      WHEN 'CPC_ABOVE_BASELINE'            THEN 5
      WHEN 'CPC_BELOW_BASELINE'            THEN 6
      WHEN 'CTR_ABOVE_BASELINE'            THEN 7
      WHEN 'ZERO_ORDER_SPEND'              THEN 8
      ELSE 99
    END AS prio
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_SIGNALS`
),
-- свёртка один-к-одному: строки экрана размножиться не могут
sig AS (
  SELECT
    as_of_date, window_days, nm_id, norm_query,
    ARRAY_AGG(signal_code ORDER BY prio, signal_code)                     AS signals,
    STRING_AGG(signal_code, ', ' ORDER BY prio, signal_code)              AS signals_text,
    ARRAY_AGG(signal_code ORDER BY prio, signal_code LIMIT 1)[OFFSET(0)]  AS signal_top,
    LOGICAL_OR(signal_strength = 'STRONG')                                AS has_strong_signal,
    COUNT(*)                                                              AS signal_count
  FROM sig_prio
  GROUP BY as_of_date, window_days, nm_id, norm_query
),
-- знаменатель доли расхода внутри SKU; агрегат ДО join — fan-out невозможен
sku_totals AS (
  SELECT as_of_date, window_days, nm_id, SUM(spend_sum) AS sku_spend_rub
  FROM q
  GROUP BY as_of_date, window_days, nm_id
),
ref_ranked AS (
  SELECT
    nm_id, internal_sku, product_name_short,
    ROW_NUMBER() OVER (PARTITION BY nm_id ORDER BY internal_sku) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
),
ref AS (SELECT * EXCEPT (rn) FROM ref_ranked WHERE rn = 1)
SELECT
  q.as_of_date,
  q.window_days,
  q.nm_id,
  q.norm_query,
  r.internal_sku,
  r.product_name_short,
  -- 🔴 единственная денежная колонка вью
  q.spend_sum                                              AS spend_rub,
  SAFE_DIVIDE(q.spend_sum, NULLIF(t.sku_spend_rub, 0))     AS spend_share_of_sku,
  q.spend_share_window,
  -- диагностическая строка воронки
  q.views_sum,
  q.clicks_sum,
  q.clicks_on_imp,
  q.ctr,
  q.cpc_calc,
  q.atbs_sum,
  q.cart_cr_clicks,
  q.orders_sum,
  q.order_cr_carts,
  q.order_cr_clicks,
  q.shks_sum,
  q.buyout_ratio_shk,
  q.cpo_ads,
  q.cpm_calc,
  q.avg_pos_w_views,
  q.avg_pos_w_clicks,
  q.advert_count,
  q.days_with_data,
  q.days_with_spend,
  q.rows_with_impressions,
  q.ctr_coverage_spend_share,
  q.funnel_monotonic,
  -- доказательность (пороги Ads-4, новых не заводим)
  q.evidence_status,
  q.evidence_reason,
  q.can_compare_ctr,
  q.can_judge_cart_cr,
  q.can_judge_order_cr,
  q.can_judge_order_carts,
  q.can_compare_cpc,
  -- baseline: диагностический ориентир, не норматив
  q.baseline_level,
  q.baseline_fallback_reason,
  q.baseline_ctr,       q.baseline_ctr_level,
  q.baseline_cart_cr,   q.baseline_cart_level,
  q.baseline_order_cr,  q.baseline_order_level,
  q.baseline_order_carts, q.baseline_order_carts_level,
  q.baseline_cpc,       q.baseline_cpc_level,
  q.baseline_cpo,       q.baseline_cpo_level,
  q.ctr_vs_baseline,
  q.cart_cr_vs_baseline,
  q.order_cr_vs_baseline,
  q.order_carts_vs_baseline,
  q.cpc_vs_baseline,
  q.cpo_vs_baseline,
  q.sku_ex_self_sample_clicks,
  q.sku_ex_self_pair_count,
  -- ставка
  q.bid_min,
  q.bid_max,
  q.bid_is_uniform,
  q.bid_campaign_count,
  q.payment_type,
  q.bid_type,
  q.campaign_status,
  q.bid_snapshot_date,
  q.bid_snapshot_status,
  q.bid_snapshot_offset_days,
  q.bid_is_after_stats_as_of,
  -- динамика к предыдущему окну
  q.delta_cpc,
  q.delta_order_cr_clicks,
  q.delta_spend,
  -- сигналы, свёрнутые в строку запроса
  IFNULL(s.signals, ARRAY<STRING>[])                       AS signals,
  s.signals_text,
  s.signal_top,
  IFNULL(s.has_strong_signal, FALSE)                       AS has_strong_signal,
  IFNULL(s.signal_count, 0)                                AS signal_count
FROM q
LEFT JOIN sku_totals t
       ON  t.as_of_date  = q.as_of_date
       AND t.window_days = q.window_days
       AND t.nm_id       = q.nm_id
LEFT JOIN sig s
       ON  s.as_of_date  = q.as_of_date
       AND s.window_days = q.window_days
       AND s.nm_id       = q.nm_id
       AND s.norm_query  = q.norm_query
LEFT JOIN ref r ON r.nm_id = q.nm_id;
