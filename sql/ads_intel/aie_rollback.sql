-- ============================================================================
-- AIE V1 — откат. Исполнять только если представления были развёрнуты (отдельный ACK).
-- У представлений AIE нет потребителей: ни дашборды, ни процедуры, ни другие витрины их не читают,
-- поэтому откат — удаление в обратном порядке зависимостей. Данные не затрагиваются: AIE не пишет таблиц.
-- Этап PR-4 откатывается первой строкой, PR-3 — второй и третьей, PR-2 — четвёртой, PR-1 — пятой и шестой;
-- представление политики (финализация 2026-09-27) читают все остальные — оно удаляется последним.
-- ============================================================================
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_mart.V_AIE_DECISION_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_ECON_GUARD`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_AIE_OZON_PAIR_EVIDENCE`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_QUERY_CLASS`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_ECON_GUARD`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.wb_mart.V_AIE_WB_PAIR_EVIDENCE`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.V_AIE_POLICY`;
