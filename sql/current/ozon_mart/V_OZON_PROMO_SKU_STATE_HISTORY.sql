-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-2 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_2_CANONICAL_STATE_2026-09-22.md.
-- Internal dependencies: V_OZON_PROMO_HISTORY, V_OZON_PROMO_SKU_EVIDENCE_HISTORY.
--
-- Грейн: observation_id × action_id × product_id. Одно разрешённое состояние на пару.
--
-- Две оси, а не одна: PR-PROMO-2 доказал, что автодобавление ортогонально участию —
-- в T1 товар 3874879926 был одновременно участником 4253043 и в списке автодобавления на
-- 22.09 21:00; после этой даты он остался участником, а запись автодобавления исчезла.
--   participation_state (сейчас):  PARTICIPATING | CANDIDATE | NOT_ELIGIBLE | UNKNOWN
--   auto_add_state (дата в будущем): SCHEDULED | ELIGIBLE | NOT_LISTED | NOT_APPLICABLE | UNKNOWN
--
-- resolved_state — единое состояние по приоритету (правило в resolution_rule):
--   R1 в /products                          → PARTICIPATING       DIRECT_SOURCE
--   R2 в /auto-add/products/list            → SCHEDULED_AUTO_ADD  DIRECT_SOURCE
--   R3 в /auto-add/products/candidates      → AUTO_ADD_ELIGIBLE   DIRECT_SOURCE
--   R4 в /candidates                        → CANDIDATE           DIRECT_SOURCE
--   R5 нет ни в одном списке, перечисление
--      полное (счётчики акции совпали)       → NOT_ELIGIBLE        DERIVED_DETERMINISTIC
--   R6 иначе                                 → UNKNOWN             UNKNOWN
-- Приоритет R2 > R3 > R4: чем меньше от продавца требуется для фактического участия
-- («добавит» > «может добавить сам» > «продавец может добавить вручную»).
-- Одновременно в /products и /candidates — evidence_conflict = TRUE (источник так делать
-- не должен; наблюдено 0 раз), состояние PARTICIPATING, свидетельства не теряются.
--
-- Вселенная пар: все пары со свидетельством + (акции снимка × товары каталога того же
-- снимка из /v5/product/info/prices). Только поэтому возможен NOT_ELIGIBLE: у Ozon списки
-- перечисляются полностью и сверяются с объявленными счётчиками. У WB такого нет — там
-- декартово произведение запрещено (см. V_WB_PROMO_HISTORY).
-- Флаги in_*_list: TRUE = товар в списке; FALSE = не в списке при доказанно полном
-- перечислении; NULL = не знаем (перечисление неполное или список не запрашивался).
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`
OPTIONS (description = "PR-PROMO-2. Разрешённое состояние товара в акции Ozon по годным снимкам: одна строка на observation_id × action_id × product_id. Две оси: participation_state (сейчас) и auto_add_state (будущая дата автодобавления); resolved_state по приоритету PARTICIPATING > SCHEDULED_AUTO_ADD > AUTO_ADD_ELIGIBLE > CANDIDATE > NOT_ELIGIBLE > UNKNOWN. NOT_ELIGIBLE только при доказанно полном перечислении, иначе UNKNOWN. Флаги списков: TRUE / FALSE при полном перечислении / NULL когда не знаем.")
AS
WITH h AS (
  SELECT observation_id, observation_slot, observed_at, action_id, promotion_type, lifecycle_status,
    auto_add_dates_count, sku_membership_observability
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_HISTORY`
),
ev AS (
  SELECT observation_id, evidence_source, action_id, product_id, offer_id, auto_add_at, add_mode,
    promotion_link_method
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`
),
list_ev AS (
  SELECT observation_id, action_id, product_id,
    LOGICAL_OR(evidence_source = 'ACTION_PRODUCTS') AS in_participants,
    LOGICAL_OR(evidence_source = 'ACTION_CANDIDATES') AS in_candidates,
    LOGICAL_OR(evidence_source = 'AUTO_ADD_LIST') AS in_scheduled,
    LOGICAL_OR(evidence_source = 'AUTO_ADD_CANDIDATES') AS in_eligible,
    MAX(IF(evidence_source = 'ACTION_PRODUCTS', add_mode, NULL)) AS participation_add_mode,
    MIN(IF(evidence_source = 'AUTO_ADD_LIST', auto_add_at, NULL)) AS next_scheduled_auto_add_at,
    MIN(IF(evidence_source = 'AUTO_ADD_CANDIDATES', auto_add_at, NULL)) AS next_eligible_auto_add_at,
    STRING_AGG(DISTINCT IF(evidence_source = 'AUTO_ADD_LIST', add_mode, NULL), ',' ORDER BY IF(evidence_source = 'AUTO_ADD_LIST', add_mode, NULL)) AS scheduled_add_modes
  FROM ev
  WHERE evidence_source IN ('ACTION_PRODUCTS', 'ACTION_CANDIDATES', 'AUTO_ADD_LIST', 'AUTO_ADD_CANDIDATES')
    AND action_id IS NOT NULL AND product_id IS NOT NULL
  GROUP BY observation_id, action_id, product_id
),
catalog AS (
  SELECT observation_id, product_id, MAX(offer_id) AS offer_id
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING`
  WHERE environment = 'prod' AND product_id IS NOT NULL
  GROUP BY observation_id, product_id
),
mk_ambiguous AS (
  SELECT observation_id, product_id, LOGICAL_OR(promotion_link_method = 'AMBIGUOUS') AS has_ambiguous_link
  FROM ev
  WHERE evidence_source = 'PRODUCT_MARKETING_ACTIONS' AND product_id IS NOT NULL
  GROUP BY observation_id, product_id
),
mk_pair AS (
  SELECT DISTINCT observation_id, action_id, product_id
  FROM ev
  WHERE evidence_source = 'PRODUCT_MARKETING_ACTIONS' AND promotion_link_method = 'DETERMINISTIC_EXACT_MATCH'
),
pairs AS (
  SELECT observation_id, action_id, product_id FROM list_ev
  UNION DISTINCT
  SELECT h.observation_id, h.action_id, c.product_id
  FROM h
  JOIN catalog c ON c.observation_id = h.observation_id
),
product_offer AS (
  SELECT observation_id, product_id, MAX(offer_id) AS offer_id
  FROM (
    SELECT observation_id, product_id, offer_id FROM ev WHERE product_id IS NOT NULL AND offer_id IS NOT NULL
    UNION ALL
    SELECT observation_id, product_id, offer_id FROM catalog WHERE offer_id IS NOT NULL
  )
  GROUP BY observation_id, product_id
),
ref_pid AS (
  SELECT marketplace_product_id, COUNT(DISTINCT internal_sku) AS n_sku, MAX(internal_sku) AS internal_sku,
    MAX(offer_id) AS offer_id, MAX(marketplace_sku) AS marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current AND marketplace_product_id IS NOT NULL
  GROUP BY marketplace_product_id
),
ref_offer AS (
  SELECT offer_id, COUNT(DISTINCT internal_sku) AS n_sku, MAX(internal_sku) AS internal_sku,
    MAX(marketplace_sku) AS marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND is_current AND offer_id IS NOT NULL
  GROUP BY offer_id
),
s AS (
  SELECT
    p.observation_id, h.observation_slot, h.observed_at, p.action_id, h.promotion_type,
    h.lifecycle_status, h.auto_add_dates_count, h.sku_membership_observability,
    p.product_id,
    COALESCE(rp.offer_id, po.offer_id) AS offer_id,
    COALESCE(rp.marketplace_sku, ro.marketplace_sku) AS ozon_sku,
    CASE
      WHEN rp.n_sku = 1 THEN rp.internal_sku
      WHEN rp.marketplace_product_id IS NULL AND ro.n_sku = 1 THEN ro.internal_sku
    END AS internal_sku,
    CASE
      WHEN rp.n_sku = 1 THEN 'MAPPED_BY_PRODUCT_ID'
      WHEN rp.n_sku > 1 THEN 'AMBIGUOUS_MAPPING'
      WHEN ro.n_sku = 1 THEN 'MAPPED_BY_OFFER_ID'
      WHEN ro.n_sku > 1 THEN 'AMBIGUOUS_MAPPING'
      ELSE 'UNMAPPED'
    END AS mapping_status,
    IFNULL(l.in_participants, FALSE) AS in_p,
    IFNULL(l.in_candidates, FALSE) AS in_c,
    IFNULL(l.in_scheduled, FALSE) AS in_s,
    IFNULL(l.in_eligible, FALSE) AS in_e,
    h.sku_membership_observability = 'OBSERVED' AS lists_complete,
    l.participation_add_mode, l.next_scheduled_auto_add_at, l.next_eligible_auto_add_at, l.scheduled_add_modes,
    c.product_id IS NOT NULL AS in_catalog_snapshot,
    IFNULL(ma.has_ambiguous_link, FALSE) AS has_ambiguous_marketing_link,
    mp.action_id IS NOT NULL AS marketing_link_exact
  FROM pairs p
  JOIN h ON h.observation_id = p.observation_id AND h.action_id = p.action_id
  LEFT JOIN list_ev l ON l.observation_id = p.observation_id AND l.action_id = p.action_id AND l.product_id = p.product_id
  LEFT JOIN catalog c ON c.observation_id = p.observation_id AND c.product_id = p.product_id
  LEFT JOIN mk_ambiguous ma ON ma.observation_id = p.observation_id AND ma.product_id = p.product_id
  LEFT JOIN mk_pair mp ON mp.observation_id = p.observation_id AND mp.action_id = p.action_id AND mp.product_id = p.product_id
  LEFT JOIN product_offer po ON po.observation_id = p.observation_id AND po.product_id = p.product_id
  LEFT JOIN ref_pid rp ON rp.marketplace_product_id = CAST(p.product_id AS STRING)
  LEFT JOIN ref_offer ro ON ro.offer_id = po.offer_id
)
SELECT
  'OZON' AS marketplace,
  observation_id,
  observation_slot,
  observed_at,
  'OBSERVED' AS history_class,
  action_id,
  promotion_type,
  lifecycle_status AS promotion_lifecycle_status,
  product_id,
  offer_id,
  ozon_sku,
  internal_sku,
  mapping_status,
  CASE
    WHEN in_p THEN 'PARTICIPATING'
    WHEN in_c THEN 'CANDIDATE'
    WHEN lists_complete THEN 'NOT_ELIGIBLE'
    ELSE 'UNKNOWN'
  END AS participation_state,
  CASE
    WHEN in_p OR in_c THEN 'DIRECT_SOURCE'
    WHEN lists_complete THEN 'DERIVED_DETERMINISTIC'
    ELSE 'UNKNOWN'
  END AS participation_evidence_class,
  CASE
    WHEN in_s THEN 'SCHEDULED'
    WHEN in_e THEN 'ELIGIBLE'
    WHEN auto_add_dates_count > 0 THEN 'NOT_LISTED'
    WHEN auto_add_dates_count = 0 THEN 'NOT_APPLICABLE'
    ELSE 'UNKNOWN'
  END AS auto_add_state,
  CASE
    WHEN in_s OR in_e THEN 'DIRECT_SOURCE'
    WHEN auto_add_dates_count >= 0 THEN 'DERIVED_DETERMINISTIC'
    ELSE 'UNKNOWN'
  END AS auto_add_evidence_class,
  CASE
    WHEN in_p THEN 'PARTICIPATING'
    WHEN in_s THEN 'SCHEDULED_AUTO_ADD'
    WHEN in_e THEN 'AUTO_ADD_ELIGIBLE'
    WHEN in_c THEN 'CANDIDATE'
    WHEN lists_complete AND auto_add_dates_count >= 0 THEN 'NOT_ELIGIBLE'
    ELSE 'UNKNOWN'
  END AS resolved_state,
  CASE
    WHEN in_p OR in_s OR in_e OR in_c THEN 'DIRECT_SOURCE'
    WHEN lists_complete AND auto_add_dates_count >= 0 THEN 'DERIVED_DETERMINISTIC'
    ELSE 'UNKNOWN'
  END AS resolved_state_evidence_class,
  CASE
    WHEN in_p THEN 'R1_PARTICIPANTS_LIST'
    WHEN in_s THEN 'R2_AUTO_ADD_SCHEDULED_LIST'
    WHEN in_e THEN 'R3_AUTO_ADD_ELIGIBLE_LIST'
    WHEN in_c THEN 'R4_CANDIDATES_LIST'
    WHEN lists_complete AND auto_add_dates_count >= 0 THEN 'R5_ABSENT_FROM_COMPLETE_ENUMERATION'
    ELSE 'R6_INSUFFICIENT_EVIDENCE'
  END AS resolution_rule,
  CASE WHEN in_p THEN TRUE WHEN lists_complete THEN FALSE END AS in_participants_list,
  CASE WHEN in_c THEN TRUE WHEN lists_complete THEN FALSE END AS in_candidates_list,
  CASE WHEN in_s THEN TRUE WHEN auto_add_dates_count > 0 THEN FALSE END AS in_auto_add_scheduled_list,
  CASE WHEN in_e THEN TRUE WHEN auto_add_dates_count > 0 THEN FALSE END AS in_auto_add_eligible_list,
  in_p AND in_c AS evidence_conflict,
  participation_add_mode,
  next_scheduled_auto_add_at,
  next_eligible_auto_add_at,
  scheduled_add_modes,
  CASE
    WHEN marketing_link_exact THEN TRUE
    WHEN in_catalog_snapshot AND NOT has_ambiguous_marketing_link THEN FALSE
  END AS marketing_actions_corroborated
FROM s
