-- ============================================================================
-- DRO-1 · Откат. Порядок — обратный деплою. Применять только по owner ACK.
-- Док: docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md, раздел «Откат».
--
-- 0. Сначала Terraform: убрать Scheduler dro-health-eval (или поставить на паузу),
--    иначе он продолжит вызывать удаляемую процедуру. Политики Cloud Monitoring
--    и метрику — тем же откатом файла infra/terraform/evetis_health.tf.
-- 1. Затем объекты BigQuery ниже.
-- 2. Датасет evetis_health удаляет Terraform (delete_contents_on_destroy = false:
--    сначала этот файл, иначе destroy упадёт на непустом датасете — так задумано).
--
-- Что откат НЕ трогает: строки scope = 'DRO_PIPELINE' в wb_ops.OPS_HEALTH_STATE /
-- OPS_INCIDENT / OPS_ALERT_EVENT. Это журналы (append-only, expiry 400 дн.), на
-- ADS-1B они не влияют. Удаление — отдельная деструктивная операция по ACK:
--   DELETE FROM `wb_ops.OPS_ALERT_EVENT` WHERE scope = 'DRO_PIPELINE';
--   DELETE FROM `wb_ops.OPS_INCIDENT`    WHERE scope = 'DRO_PIPELINE';
--   DELETE FROM `wb_ops.OPS_HEALTH_STATE` WHERE scope = 'DRO_PIPELINE';
-- ============================================================================
DROP VIEW IF EXISTS `evetis_health.V_DATA_PERIOD_STATE`;
DROP VIEW IF EXISTS `evetis_health.V_HEALTH_CHECK_CURRENT`;
DROP VIEW IF EXISTS `evetis_health.V_DATA_HEALTH_CURRENT`;
DROP PROCEDURE IF EXISTS `evetis_health.sp_evaluate_data_health`;
DROP PROCEDURE IF EXISTS `evetis_health.sp_dispatch_alerts`;
DROP FUNCTION IF EXISTS `evetis_health.f_label_value`;
-- Таблицы — история снимков и доставки. Удалять только если откат окончательный.
DROP TABLE IF EXISTS `evetis_health.ALERT_DISPATCH_LOG`;
DROP TABLE IF EXISTS `evetis_health.DATA_HEALTH_SNAPSHOT`;
DROP TABLE FUNCTION IF EXISTS `evetis_health.TVF_DATA_HEALTH`;
DROP TABLE FUNCTION IF EXISTS `evetis_health.TVF_DATA_PERIOD_STATE`;
DROP TABLE FUNCTION IF EXISTS `evetis_health.TVF_PIPELINE_PROBE`;
DROP VIEW IF EXISTS `evetis_health.V_PIPELINE_CONTRACT`;
