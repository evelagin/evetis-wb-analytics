-- Reviews & Q&A v3 knowledge registry (Phase 3, 2026-09-28). ADDITIVE ONLY.
-- New tables owned by services/wb-communications/scripts/v3_registry.py (full reload from
-- knowledge_v3/registry/*.yaml on every load). No existing evetis_ref object is modified.
-- Existing authoritative tables are READ by the snapshot builder, never written:
--   evetis_ref.REF_SKU_CHANNEL_MAP (WB identity), REF_PRODUCT_MASTER, REF_BUNDLE_COMPONENTS.
-- Rollback: sql/ref/v3_knowledge_registry_rollback.sql (drops ONLY these tables).

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_KNOWLEDGE_SOURCE` (
  source_id STRING,
  tier STRING,
  source_type STRING,
  document_code STRING,
  document_name STRING,
  document_version STRING,
  document_date STRING,
  file_uri STRING,
  file_sha256 STRING,
  extraction_method STRING,
  extraction_confidence STRING,
  reliability STRING,
  quality_notes STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_KNOWLEDGE_PROFILE` (
  product_id STRING,
  product_type STRING,
  line STRING,
  customer_name_ru STRING,
  internal_aliases_json STRING,
  spec_association_status STRING,
  customer_fact_generation STRING,
  primary_source STRING,
  recipe_status STRING,
  label_status STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_IDENTIFIER` (
  product_id STRING,
  id_type STRING,
  value STRING,
  status STRING,
  source_id STRING,
  locator STRING,
  notes STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_FACT` (
  fact_id STRING,
  product_id STRING,
  fact_type STRING,
  value_json STRING,
  unit STRING,
  customer_value_ru STRING,
  source_id STRING,
  source_tier STRING,
  fact_status STRING,
  reliability STRING,
  extraction_confidence STRING,
  disclosure_policy STRING,
  quality_flags_json STRING,
  locator STRING,
  conflict_id STRING,
  owner_decision_id STRING,
  valid_from STRING,
  valid_to STRING,
  notes STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_USAGE` (
  fact_id STRING,
  product_id STRING,
  fact_type STRING,
  value_json STRING,
  unit STRING,
  customer_value_ru STRING,
  source_id STRING,
  source_tier STRING,
  fact_status STRING,
  reliability STRING,
  extraction_confidence STRING,
  disclosure_policy STRING,
  quality_flags_json STRING,
  locator STRING,
  conflict_id STRING,
  owner_decision_id STRING,
  valid_from STRING,
  valid_to STRING,
  notes STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_INGREDIENT` (
  occurrence_id STRING,
  product_id STRING,
  list_kind STRING,
  position INT64,
  ingredient_id STRING,
  inci_as_written STRING,
  concentration_pct FLOAT64,
  concentration_basis STRING,
  source_id STRING,
  locator STRING,
  fact_status STRING,
  extraction_confidence STRING,
  concentration_disclosure_policy STRING,
  owner_decision_id STRING,
  conflict_id STRING,
  customer_abstraction_ru STRING,
  quality_flags_json STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_INGREDIENT` (
  ingredient_id STRING,
  inci STRING,
  ru_name STRING,
  patterns_json STRING,
  family STRING,
  classes_json STRING,
  note STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_CLAIM` (
  claim_id STRING,
  claim_class STRING,
  text_ru STRING,
  source_id STRING,
  locator STRING,
  evidence_fact_type STRING,
  note STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_KNOWLEDGE_CONFLICT` (
  conflict_id STRING,
  fact_type STRING,
  subject STRING,
  products_json STRING,
  status STRING,
  default_behaviour STRING,
  owner_decision_id STRING,
  blocked_values_json STRING,
  candidates_json STRING,
  note STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_OWNER_DECISION` (
  decision_id STRING,
  status STRING,
  topic STRING,
  decision STRING,
  template_ids_json STRING,
  affected_facts_json STRING,
  safe_default STRING,
  decided_at STRING
);

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.KNOWLEDGE_SNAPSHOT` (
  snapshot_id STRING,
  generated_at TIMESTAMP,
  content_sha256 STRING,
  schema_version STRING,
  engine_version STRING,
  policy_version STRING,
  registry_seed_sha256 STRING,
  origin STRING,
  validation_status STRING,
  manifest_json STRING,
  activated BOOL
);  -- append-only snapshot log
