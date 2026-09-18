-- ============================================================================
-- UNITKA COGS PUBLICATION V1 — откат.
-- Удаляет ТОЛЬКО производные объекты публикации. Бизнес-данных не касается: истина COGS —
-- evetis_ref.V_PRODUCT_COGS_EFFECTIVE, копия пересобирается из неё в любой момент.
-- Порядок: 1) Scheduler unitka-cogs-publication → pause (scheduler-control / Terraform);
--          2) UNITKA_INTEGRITY_MODE=off, либо принять COGS_SNAPSHOT_UNAVAILABLE (WARNING) в Guard;
--          3) sql/unitka/integrity_v1_rollback.sql (вью V_UNITKA_COGS_CANONICAL читает эту таблицу);
--          4) этот файл; 5) targeted destroy ресурсов infra/terraform/unitka_cogs_publication.tf.
-- ============================================================================
DROP PROCEDURE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.sp_publish_unitka_cogs`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_EFFECTIVE`;
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart._UNITKA_COGS_PUBLISH_LOCK`;
-- Журнал публикаций — свидетельство; удалять только осознанно (раскомментировать):
-- DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.UNITKA_COGS_PUBLISH_LOG`;
