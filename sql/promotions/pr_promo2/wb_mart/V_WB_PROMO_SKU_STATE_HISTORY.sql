-- ============================================================================
-- PR-PROMO-2 · CANONICAL PROMOTION STATE · wb_mart.V_WB_PROMO_SKU_STATE_HISTORY (VIEW)
-- Грейн: observation_id × promotion_id × nm_id. Разрешённое состояние SKU в акции WB.
--
-- Только пары, у которых есть свидетельство (V_WB_PROMO_SKU_EVIDENCE_HISTORY). Отрицательных
-- состояний из отсутствия строки WB не выводит: полноту перечисления живыми данными
-- доказать нечем (все акции auto), поэтому NOT_ELIGIBLE здесь не порождается.
-- Приоритет: PARTICIPATING (inAction=TRUE) > CANDIDATE (inAction=FALSE) > UNKNOWN.
-- Пара в обоих списках сразу — evidence_conflict = TRUE, свидетельства сохранены.
-- Автодобавления у WB нет: auto_add_state = NOT_APPLICABLE, флаги автодобавления NULL
-- (не FALSE: источник этого не утверждает).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_STATE_HISTORY`
OPTIONS (description = "PR-PROMO-2. Разрешённое состояние SKU в акциях WB по годным снимкам: одна строка на observation_id × promotion_id × nm_id, только для пар со свидетельством. Приоритет PARTICIPATING > CANDIDATE > UNKNOWN; отрицательные состояния из отсутствия строки не выводятся. Автодобавления у WB нет: NOT_APPLICABLE, флаги NULL.")
AS
WITH ev AS (
  SELECT observation_id, observation_slot, observed_at, promotion_id, nm_id, internal_sku, mapping_status, source_state
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`
),
h AS (
  SELECT observation_id, promotion_id, promotion_type, lifecycle_status
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_HISTORY`
),
pairs AS (
  SELECT observation_id, promotion_id, nm_id,
    MIN(observation_slot) AS observation_slot,
    MIN(observed_at) AS observed_at,
    MAX(internal_sku) AS internal_sku,
    MAX(mapping_status) AS mapping_status,
    LOGICAL_OR(source_state = 'PARTICIPATING') AS in_participants_list,
    LOGICAL_OR(source_state = 'CANDIDATE') AS in_candidates_list
  FROM ev
  GROUP BY observation_id, promotion_id, nm_id
)
SELECT
  'WB' AS marketplace,
  p.observation_id,
  p.observation_slot,
  p.observed_at,
  'OBSERVED' AS history_class,
  p.promotion_id,
  h.promotion_type,
  h.lifecycle_status AS promotion_lifecycle_status,
  p.nm_id,
  p.internal_sku,
  p.mapping_status,
  CASE
    WHEN p.in_participants_list THEN 'PARTICIPATING'
    WHEN p.in_candidates_list THEN 'CANDIDATE'
    ELSE 'UNKNOWN'
  END AS participation_state,
  IF(p.in_participants_list OR p.in_candidates_list, 'DIRECT_SOURCE', 'UNKNOWN') AS participation_evidence_class,
  'NOT_APPLICABLE' AS auto_add_state,
  'DERIVED_DETERMINISTIC' AS auto_add_evidence_class,
  CASE
    WHEN p.in_participants_list THEN 'PARTICIPATING'
    WHEN p.in_candidates_list THEN 'CANDIDATE'
    ELSE 'UNKNOWN'
  END AS resolved_state,
  IF(p.in_participants_list OR p.in_candidates_list, 'DIRECT_SOURCE', 'UNKNOWN') AS resolved_state_evidence_class,
  CASE
    WHEN p.in_participants_list THEN 'R1_PARTICIPANTS_LIST'
    WHEN p.in_candidates_list THEN 'R4_CANDIDATES_LIST'
    ELSE 'R6_INSUFFICIENT_EVIDENCE'
  END AS resolution_rule,
  p.in_participants_list,
  p.in_candidates_list,
  CAST(NULL AS BOOL) AS in_auto_add_scheduled_list,
  CAST(NULL AS BOOL) AS in_auto_add_eligible_list,
  p.in_participants_list AND p.in_candidates_list AS evidence_conflict
FROM pairs p
LEFT JOIN h ON h.observation_id = p.observation_id AND h.promotion_id = p.promotion_id
