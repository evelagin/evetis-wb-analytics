-- ============================================================================
-- PR-PROMO-3 · проверки слоя экономики акций против production. ТОЛЬКО ЧТЕНИЕ.
-- Контракт: docs/promotions/PR_PROMO_3_ECONOMICS_INTEGRATION_2026-09-24.md §22.
-- Исполнитель: python tools/run_data_checks.py --suite promo_economics …
-- До развёртывания: python tools/promo_economics_render.py run predeploy <этот файл> …
-- Предразвёртывание подставляет вместо таблиц снимка «виртуальный снимок» — SELECT процедуры
-- поверх живых канонических FE-вью (текст берётся из DDL). E20 до развёртывания FAIL по
-- построению: INFORMATION_SCHEMA ещё не знает новых вью.
-- Пороги только нулевые и точность канона: вклад канона округлён до копейки, поэтому
-- |расчёт − канон| ≤ 0,00501 ₽ (половина копейки + эпсилон FLOAT64). Бизнес-порогов нет.
-- ============================================================================

-- @check E01_GRAIN_HISTORY
-- Зерно истории сценариев: площадки и нейтральный слой.
SELECT 'V_PROMO_ECONOMICS_SCENARIO_HISTORY' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (marketplace, economics_slot, scenario_entity_key))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, economics_slot, scenario_entity_key))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
UNION ALL
SELECT 'V_OZON_PROMO_ECONOMICS_BASIS_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (economics_slot, internal_sku))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (economics_slot, internal_sku))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY`
UNION ALL
SELECT 'V_WB_PROMO_ECONOMICS_BASIS_HISTORY', COUNT(*), COUNT(DISTINCT FORMAT('%t', (economics_slot, internal_sku))),
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (economics_slot, internal_sku))), 'PASS', 'FAIL')
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_ECONOMICS_BASIS_HISTORY`;

-- @check E02_GRAIN_CURRENT
-- Нет дублей текущего сценария.
SELECT 'V_PROMO_ECONOMICS_SCENARIO_CURRENT' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (marketplace, scenario_entity_key))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, scenario_entity_key))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT`;

-- @check E02B_GRAIN_COVERAGE
-- Нет дублей текущего покрытия по акции (отдельный оператор: планировщик BigQuery не выдерживает
-- обе глубокие вью в одном запросе).
SELECT 'V_PROMO_ECONOMICS_COVERAGE_CURRENT' AS object_name, COUNT(*) AS n_rows,
  COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id))) AS n_keys,
  IF(COUNT(*) = COUNT(DISTINCT FORMAT('%t', (marketplace, source_promotion_id))), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_COVERAGE_CURRENT`;

-- @check E03_COMPUTABLE_PRECONDITIONS
-- Вычислимый сценарий: цена > 0, RUB, SKU EVETIS есть, COGS есть, базис того же слота есть.
SELECT marketplace, COUNT(*) AS computable_rows,
  COUNTIF(scenario_price_rub IS NULL OR scenario_price_rub <= 0) AS bad_price,
  COUNTIF(currency IS DISTINCT FROM 'RUB') AS bad_currency,
  COUNTIF(internal_sku IS NULL) AS no_sku,
  COUNTIF(cogs_rub IS NULL) AS no_cogs,
  COUNTIF(basis_snapshot_id IS NULL OR baseline_price_rub IS NULL) AS no_basis,
  IF(COUNTIF(scenario_price_rub IS NULL OR scenario_price_rub <= 0) + COUNTIF(currency IS DISTINCT FROM 'RUB')
     + COUNTIF(internal_sku IS NULL) + COUNTIF(cogs_rub IS NULL)
     + COUNTIF(basis_snapshot_id IS NULL OR baseline_price_rub IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
WHERE economics_status = 'COMPUTABLE'
GROUP BY marketplace;

-- @check E04_NON_COMPUTABLE_HAS_NO_NUMBERS
-- Невычислимая строка не несёт ни одного экономического числа и имеет известную причину.
SELECT marketplace, economics_status, COUNT(*) AS n_rows,
  COUNTIF(promo_contribution_expected_rub IS NOT NULL OR baseline_contribution_expected_rub IS NOT NULL
          OR required_sales_uplift_pct IS NOT NULL OR promo_contribution_downside_rub IS NOT NULL) AS leaked_numbers,
  IF(COUNTIF(promo_contribution_expected_rub IS NOT NULL OR baseline_contribution_expected_rub IS NOT NULL
             OR required_sales_uplift_pct IS NOT NULL OR promo_contribution_downside_rub IS NOT NULL) = 0
     AND economics_status IN ('PRICE_SEMANTICS_UNPROVEN', 'MISSING_CANONICAL_SKU', 'MISSING_COGS',
       'MISSING_ECONOMICS_COMPONENT', 'TEMPORAL_ALIGNMENT_UNAVAILABLE', 'UNSUPPORTED_CURRENCY',
       'MISSING_SOURCE_PRICE', 'CANONICAL_ECONOMICS_NOT_READY'), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
WHERE economics_status != 'COMPUTABLE'
GROUP BY marketplace, economics_status;

-- @check E05_PRICE_SOURCE_CONTRACT
-- Каждая цена сценария — из объявленного источника с объявленным смыслом и классом доказательности.
SELECT marketplace, scenario_type, price_interpretation, price_source, price_evidence_status, COUNT(*) AS n_rows,
  IF(CONCAT(marketplace, ' # ', scenario_type, ' # ', price_interpretation, ' # ', price_source, ' # ',
            price_evidence_status) IN (
       'WB # BASELINE # CURRENT_SELLER_PRICE # wb_raw.V_WB_PRICES_CURRENT.seller_effective_price (WB_FE_V1 seller_effective_price_rub) # CANONICAL_SELLER_PRICE',
       'OZON # BASELINE # CURRENT_SELLER_PRICE # ozon_raw.RAW_OZON_PRICES.marketing_seller_price (FORWARD_MODELLED seller_base_price) # CANONICAL_SELLER_PRICE',
       'OZON # PROMO # CURRENT_PARTICIPATION_ACTION_PRICE # OZON POST /v1/actions/products.action_price # PROVEN_SELLER_PRICE_SCALE',
       'OZON # PROMO # CANDIDATE_MAX_QUALIFYING_ENTRY_PRICE # OZON POST /v1/actions/candidates.max_action_price # PROVEN_SELLER_PRICE_SCALE_CEILING',
       'OZON # PROMO # SCHEDULED_AUTO_ADD_PRICE # OZON POST /v1/actions/auto-add/products/list.action_price_to_auto_add # PROVEN_SELLER_PRICE_SCALE',
       'OZON # PROMO # AUTO_ADD_ELIGIBLE_PRICE # OZON POST /v1/actions/auto-add/products/candidates.action_price_to_auto_add # PROVEN_SELLER_PRICE_SCALE',
       'WB # PROMO # WB_NOMENCLATURE_PLAN_PRICE # WB /calendar/promotions/nomenclatures.planPrice # PRICE_SEMANTICS_UNPROVEN'), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
GROUP BY marketplace, scenario_type, price_interpretation, price_source, price_evidence_status;

-- @check E06_LINKAGE_EVIDENCE
-- BASELINE не связан с акцией; PROMO — только прямой id источника (без связи по названию).
SELECT marketplace, scenario_type, promotion_link_evidence, COUNT(*) AS n_rows,
  COUNTIF(scenario_type = 'PROMO' AND source_promotion_id IS NULL) AS promo_without_id,
  COUNTIF(scenario_type = 'BASELINE' AND source_promotion_id IS NOT NULL) AS baseline_with_id,
  IF(((scenario_type = 'BASELINE' AND promotion_link_evidence = 'NO_PROMOTION_LINK')
      OR (scenario_type = 'PROMO' AND promotion_link_evidence = 'DIRECT_ID'))
     AND COUNTIF(scenario_type = 'PROMO' AND source_promotion_id IS NULL) = 0
     AND COUNTIF(scenario_type = 'BASELINE' AND source_promotion_id IS NOT NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
GROUP BY marketplace, scenario_type, promotion_link_evidence;

-- @check E07_NO_SYNTHETIC_WB_SKU
-- Строк WB уровня акции × SKU ровно столько, сколько свидетельств /nomenclatures; для акций с
-- ненаблюдаемым составом строк нет.
SELECT
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
   WHERE marketplace = 'WB' AND scenario_type = 'PROMO') AS wb_promo_scenarios,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`) AS wb_nomenclature_evidence,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY` s
   JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY` h
     ON s.marketplace = 'WB' AND h.observation_id = s.observation_id AND CAST(h.promotion_id AS STRING) = s.source_promotion_id
   WHERE h.sku_membership_observability = 'NOT_OBSERVABLE') AS rows_for_not_observable,
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
      WHERE marketplace = 'WB' AND scenario_type = 'PROMO')
     = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`)
     AND (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY` s
          JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY` h
            ON s.marketplace = 'WB' AND h.observation_id = s.observation_id AND CAST(h.promotion_id AS STRING) = s.source_promotion_id
          WHERE h.sku_membership_observability = 'NOT_OBSERVABLE') = 0, 'PASS', 'FAIL') AS status;

-- @check E08_BASELINE_RECONCILES_WITH_CANONICAL
-- ГЛАВНАЯ ПРОВЕРКА. Примитив сценария при scenario_price = baseline_price даёт ровно канонический
-- вклад Forward economics, снятый в тот же слот: WB_FE_V1 и Ozon FORWARD_MODELLED, для КАЖДОЙ
-- вычислимой строки BASELINE. Допуск — копеечное округление канона.
SELECT marketplace, COUNT(*) AS baseline_rows,
  MAX(ABS(baseline_contribution_expected_rub - canonical_contribution_at_baseline_rub)) AS max_abs_diff_rub,
  COUNTIF(ABS(baseline_contribution_expected_rub - canonical_contribution_at_baseline_rub) > 0.00501) AS mismatches,
  COUNTIF(baseline_contribution_expected_rub != promo_contribution_expected_rub) AS baseline_vs_self,
  IF(COUNT(*) > 0 AND COUNTIF(ABS(baseline_contribution_expected_rub - canonical_contribution_at_baseline_rub) > 0.00501) = 0
     AND COUNTIF(baseline_contribution_expected_rub != promo_contribution_expected_rub) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
WHERE scenario_type = 'BASELINE' AND economics_status = 'COMPUTABLE'
GROUP BY marketplace;

-- @check E09_DOWNSIDE_RECONCILES_AND_BREAK_EVEN_IS_ROOT
-- Ozon: downside при базовой цене = канонический WORST. Обе площадки: канонический break-even —
-- корень той же линейной функции (вклад при P = break-even ≈ 0).
WITH oz AS (
  SELECT s.baseline_contribution_downside_rub AS d, b.canonical_contribution_downside_rub AS canon,
    b.price_retention_expected * b.canonical_break_even_expected_rub - b.fixed_cost_expected_rub AS at_be
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY` s
  JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY` b
    ON b.economics_slot = s.economics_slot AND b.internal_sku = s.internal_sku
  WHERE s.scenario_type = 'BASELINE' AND s.economics_status = 'COMPUTABLE'
),
wb AS (
  SELECT b.price_retention_expected * b.canonical_break_even_expected_rub - b.fixed_cost_expected_rub AS at_be,
    b.price_retention_downside * b.canonical_break_even_downside_rub - b.fixed_cost_downside_rub AS at_be_d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_ECONOMICS_BASIS_HISTORY` b
  WHERE b.basis_status = 'COMPUTABLE'
)
SELECT 'OZON' AS marketplace, COUNT(*) AS n_rows,
  COUNTIF(ABS(d - canon) > 0.00501) AS downside_mismatches,
  COUNTIF(ABS(at_be) > 0.01) AS break_even_not_root,
  IF(COUNT(*) > 0 AND COUNTIF(ABS(d - canon) > 0.00501) = 0 AND COUNTIF(ABS(at_be) > 0.01) = 0, 'PASS', 'FAIL') AS status
FROM oz
UNION ALL
SELECT 'WB', COUNT(*), 0, COUNTIF(ABS(at_be) > 0.01) + COUNTIF(ABS(at_be_d) > 0.01),
  IF(COUNT(*) > 0 AND COUNTIF(ABS(at_be) > 0.01) + COUNTIF(ABS(at_be_d) > 0.01) = 0, 'PASS', 'FAIL')
FROM wb;

-- @check E10_ARITHMETIC_CONSISTENT
-- Вклад, дельта, маржа и break-even-запас пересчитываются из базиса и цены без расхождений.
WITH s AS (
  SELECT s.*, b.price_retention_expected AS r, b.fixed_cost_expected_rub AS f
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY` s
  JOIN `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_BASIS_HISTORY` b
    ON b.economics_slot = s.economics_slot AND b.internal_sku = s.internal_sku
  WHERE s.economics_status = 'COMPUTABLE'
)
SELECT COUNT(*) AS computable_rows,
  COUNTIF(ABS(promo_contribution_expected_rub - (scenario_price_rub * r - f)) > 0.000001) AS contribution_mismatch,
  COUNTIF(ABS(delta_contribution_expected_rub - (promo_contribution_expected_rub - baseline_contribution_expected_rub)) > 0.000001) AS delta_mismatch,
  COUNTIF(ABS(promo_margin_expected_pct - promo_contribution_expected_rub / scenario_price_rub * 100) > 0.000001) AS margin_mismatch,
  COUNTIF(ABS(promo_price_minus_break_even_rub - (scenario_price_rub - break_even_price_expected_rub)) > 0.000001) AS be_mismatch,
  IF(COUNT(*) > 0
     AND COUNTIF(ABS(promo_contribution_expected_rub - (scenario_price_rub * r - f)) > 0.000001)
       + COUNTIF(ABS(delta_contribution_expected_rub - (promo_contribution_expected_rub - baseline_contribution_expected_rub)) > 0.000001)
       + COUNTIF(ABS(promo_margin_expected_pct - promo_contribution_expected_rub / scenario_price_rub * 100) > 0.000001)
       + COUNTIF(ABS(promo_price_minus_break_even_rub - (scenario_price_rub - break_even_price_expected_rub)) > 0.000001) = 0,
     'PASS', 'FAIL') AS status
FROM s;

-- @check E11_UPLIFT_EDGE_CASES
-- Требуемый рост: считается только при обоих вкладах > 0; иначе NULL с явной причиной; знак не обнулён.
SELECT uplift_status, COUNT(*) AS n_rows,
  COUNTIF(required_sales_uplift_pct IS NOT NULL AND uplift_status != 'COMPUTED') AS finite_when_not_computed,
  COUNTIF(uplift_status = 'COMPUTED' AND ABS(required_sales_uplift_pct
          - (baseline_contribution_expected_rub / promo_contribution_expected_rub - 1) * 100) > 0.0001) AS formula_mismatch,
  COUNTIF(uplift_status = 'NOT_ACHIEVABLE_PROMO_NON_POSITIVE' AND promo_contribution_expected_rub > 0) AS wrong_promo_edge,
  COUNTIF(uplift_status = 'NOT_MEANINGFUL_BASELINE_NON_POSITIVE' AND baseline_contribution_expected_rub > 0) AS wrong_baseline_edge,
  IF(COUNTIF(required_sales_uplift_pct IS NOT NULL AND uplift_status != 'COMPUTED')
     + COUNTIF(uplift_status = 'COMPUTED' AND ABS(required_sales_uplift_pct
               - (baseline_contribution_expected_rub / promo_contribution_expected_rub - 1) * 100) > 0.0001)
     + COUNTIF(uplift_status = 'NOT_ACHIEVABLE_PROMO_NON_POSITIVE' AND promo_contribution_expected_rub > 0)
     + COUNTIF(uplift_status = 'NOT_MEANINGFUL_BASELINE_NON_POSITIVE' AND baseline_contribution_expected_rub > 0) = 0
     AND uplift_status IN ('COMPUTED', 'NOT_ACHIEVABLE_PROMO_NON_POSITIVE', 'NOT_MEANINGFUL_BASELINE_NON_POSITIVE',
                           'NOT_COMPUTED_ECONOMICS_UNAVAILABLE'), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
GROUP BY uplift_status;

-- @check E12_DOWNSIDE_MARKETPLACE_SPECIFIC
-- Downside своей площадки: WB — стресс p90, Ozon — WORST по максимальной логистике. Не смешаны.
SELECT marketplace, downside_case, downside_evidence_class, COUNT(*) AS n_rows,
  IF((marketplace = 'WB' AND downside_case = 'WB_STRESS_P90_ACQUIRING_AND_LOGISTICS' AND downside_evidence_class = 'DERIVED_DETERMINISTIC')
     OR (marketplace = 'OZON' AND downside_case = 'OZON_WORST_MAX_LOGISTICS' AND downside_evidence_class = 'DIRECT_CANONICAL_CASE')
     OR (downside_case IS NULL AND COUNTIF(promo_contribution_downside_rub IS NOT NULL) = 0), 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
GROUP BY marketplace, downside_case, downside_evidence_class;

-- @check E13_BASIS_SNAPSHOTS_IMMUTABLE_AND_ALIGNED
-- Снимки базиса: один на (слот, SKU), снят внутри своего слота (не задним числом), экономика
-- строки взята из снимка того же слота.
WITH snap AS (
  SELECT 'WB' AS marketplace, snapshot_slot, internal_sku, captured_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT`
  UNION ALL
  SELECT 'OZON', snapshot_slot, internal_sku, captured_at
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT`
)
SELECT marketplace, COUNT(DISTINCT snapshot_slot) AS slots, COUNT(*) AS rows_,
  COUNT(*) - COUNT(DISTINCT FORMAT('%t', (snapshot_slot, internal_sku))) AS duplicate_rows,
  COUNTIF(captured_at < TIMESTAMP(REPLACE(snapshot_slot, 'T', ' ') || ':00+00')
          OR captured_at >= TIMESTAMP_ADD(TIMESTAMP(REPLACE(snapshot_slot, 'T', ' ') || ':00+00'), INTERVAL 9 HOUR)) AS captured_outside_slot,
  IF(COUNT(*) > 0 AND COUNT(*) = COUNT(DISTINCT FORMAT('%t', (snapshot_slot, internal_sku)))
     AND COUNTIF(captured_at < TIMESTAMP(REPLACE(snapshot_slot, 'T', ' ') || ':00+00')
                 OR captured_at >= TIMESTAMP_ADD(TIMESTAMP(REPLACE(snapshot_slot, 'T', ' ') || ':00+00'), INTERVAL 9 HOUR)) = 0,
     'PASS', 'FAIL') AS status
FROM snap
GROUP BY marketplace;

-- @check E14_ECONOMICS_FROM_SAME_SLOT
-- Ни одна строка не взяла базис чужого слота; строк без базиса своего слота с числами нет.
SELECT marketplace, COUNT(*) AS n_rows,
  COUNTIF(basis_snapshot_id IS NOT NULL
          AND basis_snapshot_id NOT LIKE CONCAT('%', REGEXP_REPLACE(economics_slot, r'[-:T]', ''))) AS foreign_slot_basis,
  COUNTIF(economics_status = 'TEMPORAL_ALIGNMENT_UNAVAILABLE' AND basis_snapshot_id IS NOT NULL) AS temporal_with_basis,
  IF(COUNTIF(basis_snapshot_id IS NOT NULL
             AND basis_snapshot_id NOT LIKE CONCAT('%', REGEXP_REPLACE(economics_slot, r'[-:T]', ''))) = 0
     AND COUNTIF(economics_status = 'TEMPORAL_ALIGNMENT_UNAVAILABLE' AND basis_snapshot_id IS NOT NULL) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
GROUP BY marketplace;

-- @check E15_EVERY_SLOT_SINCE_FIRST_SNAPSHOT_HAS_BASIS
-- После первого снимка каждый годный слот наблюдения акций имеет снимок базиса своей площадки.
-- Пропуск = расписание снимков не отработало, и экономика этого слота невосстановима.
WITH first_snap AS (
  SELECT 'WB' AS marketplace, MIN(snapshot_slot) AS s FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT`
  UNION ALL
  SELECT 'OZON', MIN(snapshot_slot) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT`
),
slots AS (
  SELECT DISTINCT marketplace, economics_slot
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
  WHERE scenario_type = 'BASELINE'
),
obs AS (
  SELECT o.marketplace, o.observation_slot
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_OBSERVATION_HISTORY` o
  JOIN first_snap f ON f.marketplace = o.marketplace AND o.observation_slot >= f.s
)
SELECT o.marketplace, COUNT(*) AS observation_slots_since_first_snapshot,
  COUNTIF(s.economics_slot IS NULL) AS slots_without_basis,
  STRING_AGG(IF(s.economics_slot IS NULL, o.observation_slot, NULL), ',' ORDER BY o.observation_slot) AS missing_slots,
  IF(COUNT(*) > 0 AND COUNTIF(s.economics_slot IS NULL) = 0, 'PASS', 'FAIL') AS status
FROM obs o
LEFT JOIN slots s ON s.marketplace = o.marketplace AND s.economics_slot = o.observation_slot
GROUP BY o.marketplace;

-- @check E16_CURRENT_DERIVED_FROM_HISTORY
-- Текущий сценарий = строка истории на его последнем слоте, и этот слот — самый поздний.
WITH h AS (
  SELECT marketplace, scenario_entity_key, economics_slot, economics_status, promo_contribution_expected_rub,
    MAX(economics_slot) OVER (PARTITION BY marketplace, scenario_entity_key) AS max_slot
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
)
SELECT COUNT(*) AS current_rows,
  COUNTIF(h.economics_slot IS NULL) AS without_history_row,
  COUNTIF(h.economics_slot != h.max_slot) AS not_latest,
  COUNTIF(c.economics_status IS DISTINCT FROM h.economics_status
          OR c.promo_contribution_expected_rub IS DISTINCT FROM h.promo_contribution_expected_rub) AS mismatches,
  IF(COUNT(*) > 0 AND COUNTIF(h.economics_slot IS NULL) + COUNTIF(h.economics_slot != h.max_slot)
     + COUNTIF(c.economics_status IS DISTINCT FROM h.economics_status
               OR c.promo_contribution_expected_rub IS DISTINCT FROM h.promo_contribution_expected_rub) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_CURRENT` c
LEFT JOIN h ON h.marketplace = c.marketplace AND h.scenario_entity_key = c.scenario_entity_key
 AND h.economics_slot = c.last_economics_slot;

-- @check E17_OZON_SCENARIOS_COVER_EVIDENCE
-- Каждое ценовое свидетельство Ozon стало ровно одной строкой сценария (ничего не отброшено,
-- ничего не размножено).
SELECT
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`
   WHERE evidence_source IN ('ACTION_PRODUCTS', 'ACTION_CANDIDATES', 'AUTO_ADD_LIST', 'AUTO_ADD_CANDIDATES')) AS price_evidence_rows,
  (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY`
   WHERE scenario_type = 'PROMO') AS promo_scenarios,
  IF((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`
      WHERE evidence_source IN ('ACTION_PRODUCTS', 'ACTION_CANDIDATES', 'AUTO_ADD_LIST', 'AUTO_ADD_CANDIDATES'))
     = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY`
        WHERE scenario_type = 'PROMO'), 'PASS', 'FAIL') AS status;

-- @check E18_BASELINE_PRICE_MATCHES_SNAPSHOT
-- Базовая цена каждой строки — seller-цена канона из снимка того же слота (для BASELINE она же цена сценария).
SELECT marketplace, COUNT(*) AS n_rows,
  COUNTIF(scenario_type = 'BASELINE' AND scenario_price_rub IS DISTINCT FROM baseline_price_rub) AS baseline_price_mismatch,
  IF(COUNTIF(scenario_type = 'BASELINE' AND scenario_price_rub IS DISTINCT FROM baseline_price_rub) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`
WHERE basis_snapshot_id IS NOT NULL
GROUP BY marketplace;

-- @check E19_NO_RECOMMENDATION_VALUES
-- Слой описывает последствия, решений не выдаёт: ни одного значения-рекомендации.
SELECT COUNT(*) AS n_rows,
  COUNTIF(REGEXP_CONTAINS(CONCAT(IFNULL(uplift_interpretation, ''), '|', IFNULL(economics_status, ''), '|',
          IFNULL(uplift_status, ''), '|', IFNULL(price_binding_evidence, '')),
          r'\b(ENTER|EXIT|STAY|WATCH|APPROVE|REJECT|GOOD_PROMO|BAD_PROMO|PASS_EXPECTED|FAIL_EXPECTED)\b')) AS recommendation_values,
  IF(COUNTIF(REGEXP_CONTAINS(CONCAT(IFNULL(uplift_interpretation, ''), '|', IFNULL(economics_status, ''), '|',
             IFNULL(uplift_status, ''), '|', IFNULL(price_binding_evidence, '')),
             r'\b(ENTER|EXIT|STAY|WATCH|APPROVE|REJECT|GOOD_PROMO|BAD_PROMO|PASS_EXPECTED|FAIL_EXPECTED)\b')) = 0, 'PASS', 'FAIL') AS status
FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PROMO_ECONOMICS_SCENARIO_HISTORY`;

-- @check E20_NO_PII_NO_DECISION_COLUMNS
-- Ни ПДн, ни полей решения, запасов, налога или хранения в семи новых вью.
-- До развёртывания FAIL по построению: columns_checked = 0.
WITH cols AS (
  SELECT table_name, column_name FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE STARTS_WITH(table_name, 'V_WB_PROMO_ECONOMICS_')
  UNION ALL
  SELECT table_name, column_name FROM `project-fa311fc0-4d87-4781-986.ozon_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE STARTS_WITH(table_name, 'V_OZON_PROMO_ECONOMICS_')
  UNION ALL
  SELECT table_name, column_name FROM `project-fa311fc0-4d87-4781-986.evetis_mart.INFORMATION_SCHEMA.COLUMNS`
  WHERE STARTS_WITH(table_name, 'V_PROMO_ECONOMICS_')
)
SELECT COUNT(DISTINCT table_name) AS views_checked, COUNT(*) AS columns_checked,
  COUNTIF(REGEXP_CONTAINS(LOWER(column_name), r'customer|first_name|last_name|patronymic|email|phone|user_comment|address|buyer')) AS pii_columns,
  COUNTIF(column_name != 'tax_model_status' AND REGEXP_CONTAINS(LOWER(column_name), r'recommend|decision|enter|exit|stay|approve|reject|score|stock|inventory|expiry|storage|tax|spp|policy_floor|attractiv')) AS forbidden_columns,
  IF(COUNT(DISTINCT table_name) = 7
     AND COUNTIF(REGEXP_CONTAINS(LOWER(column_name), r'customer|first_name|last_name|patronymic|email|phone|user_comment|address|buyer')) = 0
     AND COUNTIF(column_name != 'tax_model_status' AND REGEXP_CONTAINS(LOWER(column_name), r'recommend|decision|enter|exit|stay|approve|reject|score|stock|inventory|expiry|storage|tax|spp|policy_floor|attractiv')) = 0,
     'PASS', 'FAIL') AS status
FROM cols;
