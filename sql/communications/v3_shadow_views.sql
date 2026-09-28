-- Reviews & Q&A v3 SHADOW evaluation views (Phase 3 §41). ADDITIVE, read-only views over
-- evetis_communications.communication_v3_decisions. No composite score: separate dimensions.
-- Rollback: DROP VIEW for each of the three views below.

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_communications.V_V3_SHADOW_LATEST` AS
SELECT * EXCEPT(rn) FROM (
  SELECT d.*, ROW_NUMBER() OVER (PARTITION BY communication_id, knowledge_snapshot_id ORDER BY created_at DESC) rn
  FROM `project-fa311fc0-4d87-4781-986.evetis_communications.communication_v3_decisions` d
  WHERE run_kind = 'shadow')
WHERE rn = 1;

CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_communications.V_V3_SHADOW_METRICS` AS
WITH l AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_communications.V_V3_SHADOW_LATEST`),
drafts AS (SELECT * FROM l WHERE draft_text IS NOT NULL)
SELECT
  knowledge_snapshot_id,
  COUNT(*) AS total_shadow_cases,
  COUNTIF(final_outcome IN ('FACT_ANSWER','SERVICE','SAFETY_TEMPLATE','ACKNOWLEDGEMENT','CLARIFICATION_REQUIRED','UNKNOWN_FACT')) AS v3_answered,
  COUNTIF(final_outcome IN ('HUMAN_REVIEW','UNKNOWN_FACT')) AS v3_abstained,
  COUNTIF(final_outcome = 'BLOCK') AS v3_blocked,
  COUNTIF(final_outcome = 'HUMAN_REVIEW') AS v3_human_review,
  SAFE_DIVIDE(COUNTIF(final_outcome = 'UNKNOWN_FACT' OR failure_code = 'REQUIRED_FACT_UNKNOWN'), COUNT(*)) AS unknown_fact_rate,
  SAFE_DIVIDE(COUNTIF(failure_code = 'KNOWLEDGE_CONFLICT'), COUNT(*)) AS knowledge_conflict_rate,
  SAFE_DIVIDE(COUNTIF(risk_level IN ('R3','R4')), COUNT(*)) AS safety_route_rate,
  COUNTIF(risk_level = 'R4') AS r4_cases,
  SAFE_DIVIDE(COUNTIF(verifier_verdict = 'BLOCK'), (SELECT COUNT(*) FROM drafts)) AS verifier_block_rate,
  SAFE_DIVIDE(COUNTIF(verifier_block_rules LIKE '%V-FACT%'), (SELECT COUNT(*) FROM drafts)) AS unsupported_fact_rate,
  SAFE_DIVIDE(COUNTIF(verifier_block_rules LIKE '%V-RESTRICTED%'), (SELECT COUNT(*) FROM drafts)) AS restricted_disclosure_rate,
  SAFE_DIVIDE(COUNTIF(verifier_block_rules LIKE '%V-GENERAL%'), (SELECT COUNT(*) FROM drafts)) AS general_guidance_violation_rate,
  APPROX_QUANTILES(latency_ms_total, 100)[OFFSET(50)] AS latency_p50_ms,
  APPROX_QUANTILES(latency_ms_total, 100)[OFFSET(95)] AS latency_p95_ms,
  SAFE_DIVIDE(COUNTIF(error_class IS NOT NULL OR failure_code IN ('ENGINE_ERROR','GENERATION_ERROR')), COUNT(*)) AS error_rate,
  SUM(llm_calls) AS llm_calls, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out,
  SUM(cost_estimate_usd) AS cost_estimate_usd
FROM l
GROUP BY knowledge_snapshot_id;

-- v2 (production text) vs v3 by failure class, same verifier on both.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_communications.V_V3_V2_FAILURE_CLASSES` AS
WITH l AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_communications.V_V3_SHADOW_LATEST`),
rules AS (SELECT r FROM UNNEST(['V-FACT','V-NUM','V-RESTRICTED','V-CONFLICT','V-GENERAL','V-MEDICAL','V-CLAIM',
  'V-FREEFROM','V-USE','V-TM','V-CER','V-SERVICE','V-CONTACT','V-DISPENSER','V-SUITABILITY','V-SAFETY',
  'V-UNKNOWN','V-ID','V-TEMPLATE']) r)
SELECT rules.r AS rule_id,
  COUNTIF(l.v2_ai_block_rules LIKE CONCAT('%"', rules.r, '"%')) AS v2_ai_drafts_with_violation,
  COUNTIF(l.v2_final_block_rules LIKE CONCAT('%"', rules.r, '"%')) AS v2_final_texts_with_violation,
  COUNTIF(l.verifier_block_rules LIKE CONCAT('%"', rules.r, '"%')) AS v3_drafts_blocked_by_rule,
  COUNTIF(l.v2_ai_answer_sha256 IS NOT NULL) AS v2_ai_drafts_checked,
  COUNTIF(l.draft_text IS NOT NULL) AS v3_drafts
FROM l CROSS JOIN rules
GROUP BY rule_id;
