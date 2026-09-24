-- ============================================================================
-- PR-PROMO-2 · проверки канонического слоя состояния акций против production. ТОЛЬКО ЧТЕНИЕ.
-- Контракт: docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md §16.
-- Исполнитель: python tools/run_data_checks.py --suite promo_canonical_state …
-- До развёртывания: python tools/promo_canonical_render.py predeploy <этот файл> — тела
-- представлений подставляются из Git, RAW читается живым. V24 до развёртывания FAIL
-- по построению (INFORMATION_SCHEMA ещё не знает представлений) — это ожидаемо.
--
-- Порогов здесь нет, кроме нулевых: каждая проверка сверяет канон с независимым
-- пересчётом из RAW или с инвариантом, который объявлен в контракте. Бизнес-порогов
-- (устаревание, покрытие) нет — они не заданы владельцем, значения только отчётные.
-- ============================================================================

-- @check V01_GRAIN_WB
-- Зерно пяти представлений WB: ровно одна строка на объявленный ключ.
SELECT 'V_WB_PROMO_OBSERVATION_HISTORY' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT observation_id) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT observation_id), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`
UNION ALL
SELECT 'V_WB_PROMO_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`
UNION ALL
SELECT 'V_WB_PROMO_RANGING_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, tier_ordinal))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, tier_ordinal))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_RANGING_HISTORY`
UNION ALL
SELECT 'V_WB_PROMO_SKU_EVIDENCE_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (observation_id, evidence_key))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (observation_id, evidence_key))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`
UNION ALL
SELECT 'V_WB_PROMO_SKU_STATE_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, nm_id))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, nm_id))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY`;

-- @check V02_GRAIN_OZON
-- Зерно четырёх представлений Ozon.
SELECT 'V_OZON_PROMO_OBSERVATION_HISTORY' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT observation_id) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT observation_id), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`
UNION ALL
SELECT 'V_OZON_PROMO_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (observation_id, action_id))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (observation_id, action_id))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`
UNION ALL
SELECT 'V_OZON_PROMO_SKU_EVIDENCE_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (observation_id, evidence_key))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (observation_id, evidence_key))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`;

-- @check V03_GRAIN_OZON_STATE
-- Зерно разрешённого состояния Ozon: одна строка на снимок × акция × товар.
SELECT 'V_OZON_PROMO_SKU_STATE_HISTORY' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (observation_id, action_id, product_id))) AS n_keys,
  COUNTIF(observation_id IS NULL OR action_id IS NULL OR product_id IS NULL) AS null_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (observation_id, action_id, product_id)))
     AND COUNTIF(observation_id IS NULL OR action_id IS NULL OR product_id IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`;

-- @check V04_GRAIN_NEUTRAL_HISTORY
-- Зерно нейтральной истории акций и наблюдений.
SELECT 'V_PROMO_OBSERVATION_HISTORY' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (marketplace, observation_id))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, observation_id))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`
UNION ALL
SELECT 'V_PROMO_STATE_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id, observation_id))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id, observation_id))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`;

-- @check V05_GRAIN_NEUTRAL_SKU
-- Зерно нейтральных свидетельств и разрешённого состояния товаров.
SELECT 'V_PROMO_SKU_EVIDENCE_HISTORY' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (marketplace, observation_id, evidence_key))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, observation_id, evidence_key))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`
UNION ALL
SELECT 'V_PROMO_SKU_STATE_HISTORY', COUNT(*),
  COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id, marketplace_product_id, observation_id))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id, marketplace_product_id, observation_id))),
     'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY`;

-- @check V06_GRAIN_CURRENT
-- Текущее состояние: нет дублей сущности; наблюдаемость: одна строка на площадку × способность.
SELECT 'V_PROMO_STATE_CURRENT' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_CURRENT`
UNION ALL
SELECT 'V_PROMO_SKU_STATE_CURRENT', COUNT(*),
  COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id, marketplace_product_id))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id, marketplace_product_id))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_CURRENT`
UNION ALL
SELECT 'V_PROMO_OBSERVABILITY_CURRENT', COUNT(*), COUNT(DISTINCT FORMAT('%t', (marketplace, capability))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, capability))) AND COUNT(*) = 16, 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVABILITY_CURRENT`;

-- @check V07_RAW_NO_DUPLICATES
-- RAW не содержит дублей по зерну. Канон дедуплицирует детерминированно, но дубль RAW —
-- это дефект идемпотентности наблюдателя, и он должен быть виден, а не спрятан.
WITH d AS (
  SELECT 'RAW_WB_PROMO_CALENDAR' AS t, COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id))) AS dup
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR`
  UNION ALL
  SELECT 'RAW_WB_PROMO_RANGING', COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, tier_ordinal)))
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_RANGING`
  UNION ALL
  SELECT 'RAW_WB_PROMO_NOMENCLATURE', COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, in_action_requested, nm_id)))
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_NOMENCLATURE`
  UNION ALL
  SELECT 'WB_PROMO_OBSERVATIONS', COUNT(*) - COUNT(DISTINCT observation_id)
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_OBSERVATIONS`
  UNION ALL
  SELECT 'RAW_OZON_PROMO_ACTIONS', COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, action_id)))
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`
  UNION ALL
  SELECT 'RAW_OZON_PROMO_PRODUCTS', COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, action_id, membership, product_id)))
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS`
  UNION ALL
  SELECT 'RAW_OZON_PROMO_AUTO_ADD', COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, action_id, auto_add_at, list_kind, product_id)))
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD`
  UNION ALL
  SELECT 'RAW_OZON_PROMO_PRODUCT_MARKETING', COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, offer_id)))
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
  UNION ALL
  SELECT 'RAW_OZON_PROMO_PRODUCT_ACTION', COUNT(*) - COUNT(DISTINCT FORMAT('%t', (observation_id, offer_id, action_ordinal)))
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_ACTION`
  UNION ALL
  SELECT 'OZON_PROMO_OBSERVATIONS', COUNT(*) - COUNT(DISTINCT observation_id)
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_OBSERVATIONS`
)
SELECT t AS raw_table, dup AS duplicate_rows, IF(dup = 0, 'PASS', 'FAIL') AS status FROM d;

-- @check V08_LATEST_OBSERVATION
-- Последний годный снимок площадки в каноне = независимо пересчитанный из манифестов RAW.
WITH raw_latest AS (
  SELECT 'WB' AS marketplace, MAX(observation_id) AS raw_latest_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_OBSERVATIONS`
  WHERE environment = 'prod' AND status IN ('COMPLETE', 'REUSED')
    AND REGEXP_CONTAINS(observation_id, r'^WBPROMO_prod_[0-9]{12}$')
  UNION ALL
  SELECT 'OZON', MAX(observation_id)
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_OBSERVATIONS`
  WHERE environment = 'prod' AND status IN ('COMPLETE', 'REUSED')
    AND REGEXP_CONTAINS(observation_id, r'^OZPROMO_prod_[0-9]{12}$')
),
canon AS (
  SELECT marketplace, MAX(marketplace_latest_observation_id) AS canon_latest_id,
    COUNT(DISTINCT marketplace_latest_observation_id) AS n_distinct
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_CURRENT`
  GROUP BY marketplace
)
SELECT r.marketplace, r.raw_latest_id, c.canon_latest_id, c.n_distinct,
  IF(r.raw_latest_id = c.canon_latest_id AND c.n_distinct = 1, 'PASS', 'FAIL') AS status
FROM raw_latest r LEFT JOIN canon c USING (marketplace);

-- @check V09_OBSERVED_AT_IS_DATA_TIME
-- observed_at снимка единственный во всех таблицах RAW и равен observed_at канона.
WITH raw_times AS (
  SELECT observation_id, observed_at FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR`
  UNION ALL SELECT observation_id, observed_at FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_RANGING`
  UNION ALL SELECT observation_id, observed_at FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS`
  UNION ALL SELECT observation_id, observed_at FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS`
  UNION ALL SELECT observation_id, observed_at FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD`
  UNION ALL SELECT observation_id, observed_at FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
  UNION ALL SELECT observation_id, observed_at FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_ACTION`
),
per_obs AS (
  SELECT observation_id, COUNT(DISTINCT observed_at) AS n_times, MIN(observed_at) AS raw_observed_at
  FROM raw_times GROUP BY observation_id
)
SELECT o.marketplace, o.observation_id, o.observed_at_basis, p.n_times,
  IF(p.n_times = 1 AND o.observed_at = p.raw_observed_at AND o.observed_at_basis = 'RAW_ROWS', 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY` o
LEFT JOIN per_obs p ON p.observation_id = o.observation_id;

-- @check V10_LIFECYCLE_DERIVATION
-- Жизненный цикл совпадает с независимым пересчётом из дат RAW относительно observed_at снимка.
WITH raw AS (
  SELECT 'WB' AS marketplace, CAST(promotion_id AS STRING) AS pid, observation_id, observed_at, starts_at AS s, ends_at AS e
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR` WHERE environment = 'prod'
  UNION ALL
  SELECT 'OZON', CAST(action_id AS STRING), observation_id, observed_at, date_start, date_end
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS` WHERE environment = 'prod'
),
expected AS (
  SELECT marketplace, pid, observation_id,
    CASE WHEN s IS NULL OR e IS NULL THEN 'UNKNOWN' WHEN observed_at < s THEN 'UPCOMING'
         WHEN observed_at > e THEN 'ENDED' ELSE 'ACTIVE' END AS lc
  FROM raw
)
SELECT h.marketplace, COUNT(*) AS n_rows,
  COUNTIF(h.lifecycle_status IS DISTINCT FROM x.lc) AS mismatches,
  COUNTIF(h.lifecycle_status NOT IN ('UPCOMING', 'ACTIVE', 'ENDED', 'UNKNOWN')) AS invalid_values,
  IF(COUNTIF(h.lifecycle_status IS DISTINCT FROM x.lc) = 0
     AND COUNTIF(h.lifecycle_status NOT IN ('UPCOMING', 'ACTIVE', 'ENDED', 'UNKNOWN')) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY` h
LEFT JOIN expected x
  ON x.marketplace = h.marketplace AND x.pid = h.source_promotion_id AND x.observation_id = h.observation_id
GROUP BY h.marketplace;

-- @check V11_LIFECYCLE_MONOTONIC
-- Жизненный цикл не идёт назад (ENDED → ACTIVE, ACTIVE → UPCOMING), если даты источника не менялись.
WITH h AS (
  SELECT marketplace, source_promotion_id, observed_at, starts_at, ends_at,
    CASE lifecycle_status WHEN 'UPCOMING' THEN 1 WHEN 'ACTIVE' THEN 2 WHEN 'ENDED' THEN 3 END AS rk
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`
),
steps AS (
  SELECT *, LAG(rk) OVER w AS prev_rk, LAG(starts_at) OVER w AS prev_s, LAG(ends_at) OVER w AS prev_e
  FROM h WINDOW w AS (PARTITION BY marketplace, source_promotion_id ORDER BY observed_at)
)
SELECT COUNT(*) AS transitions_checked,
  COUNTIF(rk < prev_rk AND starts_at IS NOT DISTINCT FROM prev_s AND ends_at IS NOT DISTINCT FROM prev_e) AS backward_without_date_change,
  IF(COUNTIF(rk < prev_rk AND starts_at IS NOT DISTINCT FROM prev_s AND ends_at IS NOT DISTINCT FROM prev_e) = 0, 'PASS', 'FAIL') AS status
FROM steps WHERE prev_rk IS NOT NULL;

-- @check V12_ROW_CONSERVATION
-- Ни одна строка RAW годного снимка не потеряна и не размножена каноном.
WITH wb_obs AS (SELECT observation_id FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`),
oz_obs AS (SELECT observation_id FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`),
pairs AS (
  SELECT 'WB calendar → V_WB_PROMO_HISTORY' AS flow,
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR` WHERE environment = 'prod' AND observation_id IN (SELECT observation_id FROM wb_obs)) AS raw_rows,
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`) AS canon_rows
  UNION ALL
  SELECT 'WB ranging → V_WB_PROMO_RANGING_HISTORY',
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_RANGING` WHERE environment = 'prod' AND observation_id IN (SELECT observation_id FROM wb_obs)),
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_RANGING_HISTORY`)
  UNION ALL
  SELECT 'WB nomenclature → V_WB_PROMO_SKU_EVIDENCE_HISTORY',
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_NOMENCLATURE` WHERE environment = 'prod' AND observation_id IN (SELECT observation_id FROM wb_obs)),
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`)
  UNION ALL
  SELECT 'OZON actions → V_OZON_PROMO_HISTORY',
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS` WHERE environment = 'prod' AND observation_id IN (SELECT observation_id FROM oz_obs)),
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`)
  UNION ALL
  SELECT 'OZON products+auto_add+marketing_actions → V_OZON_PROMO_SKU_EVIDENCE_HISTORY',
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS` WHERE environment = 'prod' AND observation_id IN (SELECT observation_id FROM oz_obs))
    + (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD` WHERE environment = 'prod' AND observation_id IN (SELECT observation_id FROM oz_obs))
    + (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_ACTION` WHERE environment = 'prod' AND observation_id IN (SELECT observation_id FROM oz_obs)),
    (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`)
)
SELECT flow, raw_rows, canon_rows, IF(raw_rows = canon_rows, 'PASS', 'FAIL') AS status FROM pairs;

-- @check V13_NEUTRAL_UNION_CONSERVATION
-- Нейтральный слой = объединение площадок без потерь и размножения.
SELECT 'state_history' AS layer,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`)
  + (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`) AS marketplace_rows,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`) AS neutral_rows,
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`)
     + (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`)
     = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`), 'PASS', 'FAIL') AS status
UNION ALL
SELECT 'sku_evidence',
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`)
  + (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`),
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`),
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`)
     + (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`)
     = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`), 'PASS', 'FAIL');

-- @check V14_STATE_MAPPING_COMPLETE
-- Каждое свидетельство отображено в известное каноническое состояние; неизвестных значений источника нет.
SELECT marketplace, evidence_source, source_state, COUNT(*) AS n_rows,
  IF(source_state != 'UNRECOGNISED'
     AND evidence_source IN ('PROMOTION_NOMENCLATURES', 'ACTION_PRODUCTS', 'ACTION_CANDIDATES', 'AUTO_ADD_LIST',
                             'AUTO_ADD_CANDIDATES', 'PRODUCT_MARKETING_ACTIONS')
     AND evidence_class IN ('DIRECT_SOURCE'), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`
GROUP BY marketplace, evidence_source, source_state, evidence_class;

-- @check V15_PRECEDENCE_AND_DISTINCT_STATES
-- resolved_state совпадает с независимым пересчётом приоритета из флагов, кандидат ≠
-- запланирован ≠ участник: у каждого состояния своё правило и своя опора.
WITH s AS (
  SELECT marketplace, resolved_state, resolution_rule, resolved_state_evidence_class,
    in_participants_list, in_candidates_list, in_auto_add_scheduled_list, in_auto_add_eligible_list,
    CASE
      WHEN in_participants_list IS TRUE THEN 'PARTICIPATING'
      WHEN in_auto_add_scheduled_list IS TRUE THEN 'SCHEDULED_AUTO_ADD'
      WHEN in_auto_add_eligible_list IS TRUE THEN 'AUTO_ADD_ELIGIBLE'
      WHEN in_candidates_list IS TRUE THEN 'CANDIDATE'
      WHEN resolution_rule = 'R5_ABSENT_FROM_COMPLETE_ENUMERATION'
       AND in_participants_list IS FALSE AND in_candidates_list IS FALSE THEN 'NOT_ELIGIBLE'
      ELSE 'UNKNOWN'
    END AS expected_state
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY`
)
SELECT marketplace, resolved_state, resolution_rule, COUNT(*) AS n_rows,
  COUNTIF(resolved_state != expected_state) AS precedence_mismatches,
  IF(COUNTIF(resolved_state != expected_state) = 0
     AND resolved_state IN ('PARTICIPATING', 'SCHEDULED_AUTO_ADD', 'AUTO_ADD_ELIGIBLE', 'CANDIDATE', 'NOT_ELIGIBLE', 'UNKNOWN')
     AND (resolved_state != 'SCHEDULED_AUTO_ADD' OR LOGICAL_AND(in_participants_list IS NOT TRUE))
     AND (resolved_state != 'CANDIDATE' OR LOGICAL_AND(in_auto_add_scheduled_list IS NOT TRUE AND in_participants_list IS NOT TRUE))
     AND (resolved_state != 'UNKNOWN' OR LOGICAL_AND(resolved_state_evidence_class = 'UNKNOWN')),
     'PASS', 'FAIL') AS status
FROM s
GROUP BY marketplace, resolved_state, resolution_rule;

-- @check V16_UNKNOWN_AND_NOT_OBSERVABLE_PRESERVED
-- NOT_ELIGIBLE только при доказанно полном перечислении; WB-акции NOT_OBSERVABLE не имеют ни
-- одной строки уровня SKU; каждая автоакция WB — NOT_OBSERVABLE.
SELECT 'ozon_not_eligible_only_when_complete' AS invariant,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY` s
   JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY` h USING (observation_id, action_id)
   WHERE s.resolved_state = 'NOT_ELIGIBLE' AND h.sku_membership_observability != 'OBSERVED') AS violations,
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY` s
      JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY` h USING (observation_id, action_id)
      WHERE s.resolved_state = 'NOT_ELIGIBLE' AND h.sku_membership_observability != 'OBSERVED') = 0, 'PASS', 'FAIL') AS status
UNION ALL
SELECT 'wb_auto_promotions_not_observable',
  (SELECT COUNTIF(sku_membership_observability != 'NOT_OBSERVABLE')
   FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY` WHERE promotion_type = 'auto'),
  IF((SELECT COUNTIF(sku_membership_observability != 'NOT_OBSERVABLE')
      FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY` WHERE promotion_type = 'auto') = 0, 'PASS', 'FAIL')
UNION ALL
SELECT 'wb_not_observable_has_no_sku_rows',
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY` s
   JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY` h USING (observation_id, promotion_id)
   WHERE h.sku_membership_observability != 'OBSERVED'),
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY` s
      JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY` h USING (observation_id, promotion_id)
      WHERE h.sku_membership_observability != 'OBSERVED') = 0, 'PASS', 'FAIL');

-- @check V17_WB_NO_AGGREGATE_PUSHDOWN
-- Строк WB уровня SKU ровно столько, сколько пар (снимок, акция, nm_id) отдал /nomenclatures.
-- Агрегаты акции (inPromoActionTotal и др.) в строки SKU не превращаются.
SELECT
  (SELECT COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, nm_id)))
   FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_NOMENCLATURE` n
   WHERE environment = 'prod' AND observation_id IN
     (SELECT observation_id FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`)) AS raw_nm_pairs,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY` WHERE marketplace = 'WB') AS canon_wb_sku_rows,
  IF((SELECT COUNT(DISTINCT FORMAT('%t', (observation_id, promotion_id, nm_id)))
      FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_NOMENCLATURE` n
      WHERE environment = 'prod' AND observation_id IN
        (SELECT observation_id FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`))
     = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY` WHERE marketplace = 'WB'),
     'PASS', 'FAIL') AS status;

-- @check V18_OZON_MAPPING_SURFACED
-- Каждый товар Ozon из RAW последнего снимка есть в каноническом состоянии (несопоставленные
-- не отбрасываются). Покрытие сопоставления — отчётное значение, порога нет.
WITH latest AS (
  SELECT MAX(observation_id) AS oid
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`
),
raw_products AS (
  SELECT DISTINCT product_id FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCTS`
  WHERE observation_id = (SELECT oid FROM latest)
  UNION DISTINCT
  SELECT DISTINCT product_id FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_AUTO_ADD`
  WHERE observation_id = (SELECT oid FROM latest)
  UNION DISTINCT
  SELECT DISTINCT product_id FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
  WHERE observation_id = (SELECT oid FROM latest) AND product_id IS NOT NULL
),
canon AS (
  SELECT product_id, MAX(mapping_status) AS mapping_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`
  WHERE observation_id = (SELECT oid FROM latest)
  GROUP BY product_id
)
SELECT (SELECT oid FROM latest) AS observation_id,
  COUNT(*) AS raw_products,
  COUNTIF(c.product_id IS NULL) AS missing_in_canon,
  COUNTIF(c.mapping_status LIKE 'MAPPED%') AS mapped,
  COUNTIF(c.mapping_status IN ('UNMAPPED', 'AMBIGUOUS_MAPPING')) AS unmapped_or_ambiguous,
  ROUND(100 * SAFE_DIVIDE(COUNTIF(c.mapping_status LIKE 'MAPPED%'), COUNT(*)), 2) AS mapping_coverage_pct,
  IF(COUNT(*) > 0 AND COUNTIF(c.product_id IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM raw_products r LEFT JOIN canon c USING (product_id);

-- @check V19_MARKETING_LINKAGE
-- Связь marketing_actions: каждое DETERMINISTIC_EXACT_MATCH подтверждено независимо (ровно одна
-- акция снимка с тем же title побайтно и тем же окном дат); у AMBIGUOUS и UNRESOLVED id нет.
WITH ev AS (
  SELECT observation_id, action_id, promotion_link_method, source_action_title, source_date_from, source_date_to
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`
  WHERE evidence_source = 'PRODUCT_MARKETING_ACTIONS'
),
act AS (
  SELECT observation_id, action_id, title, date_start, date_end
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS` WHERE environment = 'prod'
),
chk AS (
  SELECT ev.observation_id, ev.promotion_link_method, ev.action_id,
    (SELECT COUNT(*) FROM act a WHERE a.observation_id = ev.observation_id AND a.title = ev.source_action_title) AS title_matches,
    (SELECT COUNT(*) FROM act a WHERE a.observation_id = ev.observation_id AND a.title = ev.source_action_title
       AND a.date_start = ev.source_date_from AND a.date_end = ev.source_date_to AND a.action_id = ev.action_id) AS exact_confirmed
  FROM ev
)
SELECT promotion_link_method, COUNT(*) AS n_rows,
  COUNTIF(action_id IS NOT NULL) AS with_id,
  COUNTIF(title_matches > 1) AS duplicate_titles,
  IF(CASE promotion_link_method
       WHEN 'DETERMINISTIC_EXACT_MATCH' THEN LOGICAL_AND(title_matches = 1 AND exact_confirmed = 1)
       WHEN 'AMBIGUOUS' THEN LOGICAL_AND(action_id IS NULL AND title_matches >= 1)
       WHEN 'UNRESOLVED' THEN LOGICAL_AND(action_id IS NULL AND title_matches = 0)
       ELSE FALSE END, 'PASS', 'FAIL') AS status
FROM chk
GROUP BY promotion_link_method;

-- @check V20_NULL_NOT_ZERO
-- NULL источника остаётся NULL в каноне и даёт UNKNOWN, а не 0 и не NOT_PARTICIPATING.
WITH wb AS (
  SELECT r.in_promo_total AS raw_v, h.source_in_promo_count AS canon_v, h.seller_participation_state AS st
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR` r
  JOIN `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY` h
    ON h.marketplace = 'WB' AND h.source_promotion_id = CAST(r.promotion_id AS STRING) AND h.observation_id = r.observation_id
),
oz AS (
  SELECT r.participating_products_count AS raw_v, h.source_in_promo_count AS canon_v,
    r.is_participating AS raw_flag, h.seller_participation_state AS st
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_ACTIONS` r
  JOIN `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY` h
    ON h.marketplace = 'OZON' AND h.source_promotion_id = CAST(r.action_id AS STRING) AND h.observation_id = r.observation_id
)
SELECT 'WB' AS marketplace, COUNT(*) AS n_rows, COUNTIF(raw_v IS NULL) AS raw_nulls,
  COUNTIF(raw_v IS DISTINCT FROM canon_v) + COUNTIF(raw_v IS NULL AND st != 'UNKNOWN') AS violations,
  IF(COUNTIF(raw_v IS DISTINCT FROM canon_v) + COUNTIF(raw_v IS NULL AND st != 'UNKNOWN') = 0, 'PASS', 'FAIL') AS status
FROM wb
UNION ALL
SELECT 'OZON', COUNT(*), COUNTIF(raw_v IS NULL),
  COUNTIF(raw_v IS DISTINCT FROM canon_v) + COUNTIF(raw_flag IS NULL AND st != 'UNKNOWN'),
  IF(COUNTIF(raw_v IS DISTINCT FROM canon_v) + COUNTIF(raw_flag IS NULL AND st != 'UNKNOWN') = 0, 'PASS', 'FAIL')
FROM oz;

-- @check V21_CURRENT_EQUALS_LATEST_HISTORY
-- Текущее выведено из истории: строка current = строка истории на last_observation_id, и это
-- самое позднее наблюдение сущности.
WITH h AS (
  SELECT marketplace, source_promotion_id, observation_id, observed_at, lifecycle_status, seller_participation_state,
    sku_membership_observability,
    MAX(observed_at) OVER (PARTITION BY marketplace, source_promotion_id) AS max_obs
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_HISTORY`
)
SELECT COUNT(*) AS current_rows,
  COUNTIF(h.observation_id IS NULL) AS without_history_row,
  COUNTIF(h.observed_at != h.max_obs) AS not_latest,
  COUNTIF(c.lifecycle_status_at_last_observation IS DISTINCT FROM h.lifecycle_status
          OR c.seller_participation_state IS DISTINCT FROM h.seller_participation_state
          OR c.sku_membership_observability IS DISTINCT FROM h.sku_membership_observability) AS state_mismatches,
  IF(COUNT(*) > 0 AND COUNTIF(h.observation_id IS NULL) = 0 AND COUNTIF(h.observed_at != h.max_obs) = 0
     AND COUNTIF(c.lifecycle_status_at_last_observation IS DISTINCT FROM h.lifecycle_status
                 OR c.seller_participation_state IS DISTINCT FROM h.seller_participation_state
                 OR c.sku_membership_observability IS DISTINCT FROM h.sku_membership_observability) = 0,
     'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_STATE_CURRENT` c
LEFT JOIN h
  ON h.marketplace = c.marketplace AND h.source_promotion_id = c.source_promotion_id
 AND h.observation_id = c.last_observation_id;

-- @check V22_SKU_CURRENT_EQUALS_LATEST_HISTORY
-- То же для товаров: current = история на last_observation_id, самое позднее наблюдение пары.
WITH h AS (
  SELECT marketplace, source_promotion_id, marketplace_product_id, observation_id, observed_at, resolved_state,
    MAX(observed_at) OVER (PARTITION BY marketplace, source_promotion_id, marketplace_product_id) AS max_obs
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_HISTORY`
)
SELECT COUNT(*) AS current_rows,
  COUNTIF(h.observation_id IS NULL) AS without_history_row,
  COUNTIF(h.observed_at != h.max_obs) AS not_latest,
  COUNTIF(c.resolved_state IS DISTINCT FROM h.resolved_state) AS state_mismatches,
  COUNTIF(c.observation_age_minutes IS NULL) AS freshness_missing,
  IF(COUNT(*) > 0 AND COUNTIF(h.observation_id IS NULL) = 0 AND COUNTIF(h.observed_at != h.max_obs) = 0
     AND COUNTIF(c.resolved_state IS DISTINCT FROM h.resolved_state) = 0
     AND COUNTIF(c.observation_age_minutes IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_SKU_STATE_CURRENT` c
LEFT JOIN h
  ON h.marketplace = c.marketplace AND h.source_promotion_id = c.source_promotion_id
 AND h.marketplace_product_id = c.marketplace_product_id AND h.observation_id = c.last_observation_id;

-- @check V23_HISTORY_CLASS_AND_BACKFILL_GUARD
-- В канонической истории только наблюдённые слоты: history_class = OBSERVED, id по контракту слота.
SELECT marketplace, COUNT(*) AS observations,
  COUNTIF(history_class != 'OBSERVED') AS non_observed_class,
  COUNTIF(NOT REGEXP_CONTAINS(observation_id, r'^(WB|OZ)PROMO_prod_[0-9]{12}$')) AS non_slot_ids,
  IF(COUNTIF(history_class != 'OBSERVED') = 0
     AND COUNTIF(NOT REGEXP_CONTAINS(observation_id, r'^(WB|OZ)PROMO_prod_[0-9]{12}$')) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY`
GROUP BY marketplace;

-- @check V24_NO_PII_NO_ECONOMICS_COLUMNS
-- Ни одно из 16 представлений СОСТОЯНИЯ не выводит ПДн и экономику/рекомендации (контракт PR-PROMO-2 §21–22).
-- Слой экономики PR-PROMO-3 (*_PROMO_ECONOMICS_*) проверяется своим набором (E20) и сюда не входит;
-- контекст запаса PR-PROMO-4 (V_PROMO_INVENTORY_*) несёт экономику PR-PROMO-3 дословно и проверяется I30.
-- До развёртывания FAIL по построению: columns_checked = 0.
WITH cols AS (
  SELECT table_name, column_name FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE STARTS_WITH(table_name, 'V_WB_PROMO_') AND NOT STARTS_WITH(table_name, 'V_WB_PROMO_ECONOMICS_')
  UNION ALL
  SELECT table_name, column_name FROM `project-fa311fc0-4d87-4781-986.ozon_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE STARTS_WITH(table_name, 'V_OZON_PROMO_') AND NOT STARTS_WITH(table_name, 'V_OZON_PROMO_ECONOMICS_')
  UNION ALL
  SELECT table_name, column_name FROM `project-fa311fc0-4d87-4781-986.evetis_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE STARTS_WITH(table_name, 'V_PROMO_') AND NOT STARTS_WITH(table_name, 'V_PROMO_ECONOMICS_')
    AND NOT STARTS_WITH(table_name, 'V_PROMO_INVENTORY_')
)
SELECT COUNT(DISTINCT table_name) AS views_checked, COUNT(*) AS columns_checked,
  COUNTIF(REGEXP_CONTAINS(LOWER(column_name),
    r'customer|first_name|last_name|patronymic|email|phone|user_comment|address|buyer')) AS pii_columns,
  COUNTIF(REGEXP_CONTAINS(LOWER(column_name),
    r'contribution|margin|cogs|uplift|drr|profit|commission|logistic|tax|recommend|decision|enter|exit|stay|pass_|fail_|attractiv|admissib|score')) AS economics_columns,
  IF(COUNT(DISTINCT table_name) = 16
     AND COUNTIF(REGEXP_CONTAINS(LOWER(column_name),
       r'customer|first_name|last_name|patronymic|email|phone|user_comment|address|buyer')) = 0
     AND COUNTIF(REGEXP_CONTAINS(LOWER(column_name),
       r'contribution|margin|cogs|uplift|drr|profit|commission|logistic|tax|recommend|decision|enter|exit|stay|pass_|fail_|attractiv|admissib|score')) = 0,
     'PASS', 'FAIL') AS status
FROM cols;
