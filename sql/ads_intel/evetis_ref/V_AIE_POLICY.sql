-- ============================================================================
-- AIE V1 · evetis_ref.V_AIE_POLICY (VIEW) — единственный источник политик движка рекомендаций.
-- Грейн: одна строка на политику (в production — ровно одна).
-- Контракт: docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md §1; реестр — quality/unresolved_business_rules.json.
--
-- Почему представление, а не CTE в решении. Порог P7 применяется в трёх доменных представлениях
-- (wb_mart.V_AIE_WB_PAIR_EVIDENCE, wb_mart.V_AIE_WB_ECON_GUARD, ozon_mart.V_AIE_OZON_PAIR_EVIDENCE),
-- а доменные витрины не могут читать evetis_mart (EXTERNAL_DATASET_POLICY). evetis_ref читают все слои,
-- поэтому политика лежит здесь один раз и не дублируется константами.
--
-- Утверждено владельцем 2026-09-27 (Shadow V1, не неизменяемая политика будущего автопилота):
--   P3  = 0.90  уровень доверия интервала ДРР;
--   P7  = 3.0   граница режима — |изменение цены продавца| ≥ 3 % (при NULL — любое наблюдаемое изменение);
--   K   = 14    текущий набор рекомендаций: статус активен и не больше 14 суток с последнего расхода.
-- Не утверждено (NULL, скрытых значений по умолчанию нет): P1 (резерв → INCREASE недостижим), P2, P5 (нижняя
-- и верхняя границы покрытия), P13. P6 — не параметр: роли баз утверждены в коде (дизайн §4).
-- Replay подставляет сетку кандидатов между маркерами @aie:policy (tools/aie_render.py) — это не решение владельца.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ref.V_AIE_POLICY`
OPTIONS (description = "AIE V1. Единственный источник политик движка рекламных рекомендаций. Утверждено владельцем 2026-09-27 для Shadow V1: P3 = 0.90, P7 = 3 % изменения цены продавца, K = 14 суток активности. Не утверждено (NULL): P1, P2, P5, P13 — без P1 INCREASE недостижим. Replay подставляет сетку кандидатов; это не решение владельца.")
AS
WITH
-- @aie:policy:begin
aie_policy AS (
  SELECT
    CAST('V1_SHADOW_2026-09-27' AS STRING) AS policy_id,
    CAST(NULL AS FLOAT64) AS p1_reserve_share,
    CAST(NULL AS FLOAT64) AS p2_band_pp,
    CAST(0.90 AS FLOAT64) AS p3_confidence,
    CAST(NULL AS FLOAT64) AS p5_low_cover_days,
    CAST(NULL AS FLOAT64) AS p5_overstock_cover_days,
    CAST(3.0 AS FLOAT64) AS p7_price_change_pct,
    CAST(NULL AS INT64) AS p13_cooldown_days,
    CAST(14 AS INT64) AS k_inactive_days
),
-- @aie:policy:end
published AS (
  SELECT * FROM aie_policy
)
SELECT * FROM published;
