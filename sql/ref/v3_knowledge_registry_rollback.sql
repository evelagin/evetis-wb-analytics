-- Rollback of sql/ref/v3_knowledge_registry.sql: drops ONLY the v3-owned tables.
-- The runtime never reads BigQuery (it reads the snapshot baked into the image), so dropping
-- these tables does not affect production; the snapshot log is kept unless explicitly dropped.

DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_KNOWLEDGE_SOURCE`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_KNOWLEDGE_PROFILE`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_IDENTIFIER`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_FACT`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_USAGE`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_INGREDIENT`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_INGREDIENT`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_CLAIM`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_KNOWLEDGE_CONFLICT`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_OWNER_DECISION`;
-- DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.KNOWLEDGE_SNAPSHOT`;  -- audit log, keep
