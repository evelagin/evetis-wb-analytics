-- ============================================================================
-- UNITKA WB — ДОКАЗАННЫЕ ОТКАЗЫ ВНЕ ORDERS API (Phase 1A, 2026-10-07)
-- ============================================================================
-- Проблема (аудит 07.10.2026): воронка WB (Q листа) содержит заказы, которых Orders API не отдаёт вовсе.
-- Отмены S листа берутся ТОЛЬКО из Orders API, поэтому такой заказ, ставший отказом, Юнитка считает продажей.
-- В финансовом отчёте у такого srid есть ровно характерный след отказа: прямое и обратное плечо логистики,
-- без продажи и без возврата.
--
-- Доказательство правила на production (заказы Orders API, май–сентябрь 2026):
--   * srid с ≥ 2 ненулевыми плечами «Логистика»/«Доставка», без «Продажа» и «Возврат» — 256 из 256 отменены
--     (is_cancel = true). Единственный srid «2 плеча + продажа» (eAL.r0beccb…, 02.07) — возврат после выкупа:
--     у него есть строка «Возврат», поэтому строки «Продажа»/«Возврат» исключают класс отказа.
--   * продажа приходит не позже 33 дней после заказа (p99 = 18), второе плечо отказа — не позже 46 дней (p99 = 30).
--     REFUSAL_MATURITY_DAYS = 46: только после этого срока отсутствие следа в финотчёте значимо.
--   * order_dt финотчёта — UTC ('…Z'), дата заказа Orders API и воронки — сутки МСК: 200 заказов с мая
--     имеют финансовую дату на день раньше. Дата отказа = DATE(order_dt, 'Europe/Moscow').
--
-- Классы доказательств (суток × SKU, только дни, где Q листа взят из воронки API, т. е. с 04.09.2026):
--   EXPLAINED            — избыток воронки над Orders API полностью объяснён доказанными отказами
--                          (они увеличивают S) и/или продажами вне Orders API (остаются продажей),
--   STILL_OPEN           — необъяснённый избыток моложе срока зрелости: ни продажей, ни отменой не считается,
--   MATURE_BUT_UNPROVEN  — избыток старше срока зрелости без финансового следа: виден в QA, в S НЕ попадает.
-- В S попадает ТОЛЬКО refusal_counted_qty = LEAST(доказанные отказы, избыток воронки): отмена без заказа в Q
-- невозможна, и доказанный отказ, который воронка не посчитала, Q не увеличивает.
-- Доказанным считается только отказ, оба плеча которого — в ОКОНЧАТЕЛЬНОМ (недельном) слое финотчёта.
-- ============================================================================

-- ─── 1. Доказательство по srid: финансовый след заказа, которого нет в Orders API ───────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_EVIDENCE` AS
WITH fin AS (
  SELECT srid, supplier_oper_name AS op, SAFE_CAST(wb_nm_id AS INT64) AS nm, order_dt, _rr_date AS rr, finance_status,
         SAFE_CAST(logistics_amount AS NUMERIC) AS lg
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
  WHERE srid IS NOT NULL AND srid <> ''
    AND supplier_oper_name IN ('Логистика', 'Доставка', 'Продажа', 'Возврат')
),
s AS (
  SELECT
    srid,
    MAX(NULLIF(nm, 0))                                                         AS nm_id,
    DATE(MIN(SAFE.TIMESTAMP(order_dt)), 'Europe/Moscow')                       AS order_date_msk,
    COUNTIF(op IN ('Логистика', 'Доставка') AND lg <> 0)                       AS logistics_legs,
    ROUND(SUM(IF(op IN ('Логистика', 'Доставка'), lg, 0)), 2)                  AS logistics_rub,
    COUNTIF(op = 'Продажа')                                                    AS sale_rows,
    COUNTIF(op = 'Возврат')                                                    AS return_rows,
    MIN(IF(op IN ('Логистика', 'Доставка') AND lg <> 0, rr, NULL))             AS first_leg_date,
    MAX(IF(op IN ('Логистика', 'Доставка') AND lg <> 0, rr, NULL))             AS last_leg_date,
    LOGICAL_AND(IF(op IN ('Логистика', 'Доставка') AND lg <> 0, finance_status = 'FINAL', TRUE)) AS legs_final
  FROM fin
  GROUP BY srid
),
api AS (SELECT DISTINCT order_srid FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`)
SELECT
  s.*,
  api.order_srid IS NOT NULL AS in_orders_api,
  CASE
    WHEN api.order_srid IS NOT NULL                    THEN 'IN_ORDERS_API'
    WHEN s.sale_rows > 0 OR s.return_rows > 0          THEN 'SOLD_NOT_IN_ORDERS_API'
    WHEN s.logistics_legs >= 2 AND s.legs_final        THEN 'PROVEN_REFUSAL'
    WHEN s.logistics_legs >= 2                         THEN 'REFUSAL_PENDING_FINAL'
    WHEN s.logistics_legs = 1                          THEN 'ONE_LEG_NOT_IN_ORDERS_API'
    ELSE 'NO_LOGISTICS_NOT_IN_ORDERS_API'
  END AS evidence_class
FROM s
LEFT JOIN api ON api.order_srid = s.srid
WHERE s.order_date_msk IS NOT NULL;

-- ─── 2. Сутки × SKU: избыток воронки над Orders API и его объяснение ────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_DAILY` AS
WITH cfg AS (
  -- Первый день счётчика Q из воронки API (01–03.09 — XLSX-бэкфилл со своими отменами), срок зрелости — см. шапку.
  SELECT DATE '2026-09-04' AS funnel_api_from, 46 AS maturity_days
),
ft AS (
  SELECT MAX(IF(finance_status = 'FINAL', _rr_date, NULL)) AS finance_final_through
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_CANONICAL`
),
fu AS (
  SELECT nm_id, date_msk AS d, MAX(orders_count) AS funnel_orders
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, cfg
  WHERE date_msk >= cfg.funnel_api_from
  GROUP BY 1, 2
),
api AS (
  SELECT nm_id, order_date AS d, SUM(quantity) AS gross,
         SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) = order_date, quantity, 0)) AS same_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, cfg
  WHERE order_date >= cfg.funnel_api_from
  GROUP BY 1, 2
),
ev AS (
  SELECT nm_id, order_date_msk AS d,
         COUNTIF(evidence_class = 'PROVEN_REFUSAL')                                         AS proven,
         COUNTIF(evidence_class = 'SOLD_NOT_IN_ORDERS_API')                                 AS sold,
         STRING_AGG(IF(evidence_class = 'PROVEN_REFUSAL', srid, NULL), ',' ORDER BY srid)   AS proven_srids,
         ROUND(SUM(IF(evidence_class = 'PROVEN_REFUSAL', logistics_rub, 0)), 2)             AS proven_logistics_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_EVIDENCE`, cfg
  WHERE order_date_msk >= cfg.funnel_api_from AND nm_id IS NOT NULL
  GROUP BY 1, 2
),
j AS (
  SELECT
    fu.nm_id, fu.d, fu.funnel_orders,
    IFNULL(api.gross, 0) AS api_gross, IFNULL(api.same_day, 0) AS api_same_day,
    GREATEST(fu.funnel_orders - (IFNULL(api.gross, 0) - IFNULL(api.same_day, 0)), 0) AS excess,
    IFNULL(ev.proven, 0) AS proven, IFNULL(ev.sold, 0) AS sold,
    ev.proven_srids, IFNULL(ev.proven_logistics_rub, 0) AS proven_logistics_rub
  FROM fu
  LEFT JOIN api USING (nm_id, d)
  LEFT JOIN ev  USING (nm_id, d)
),
k AS (
  SELECT j.*, LEAST(proven, excess) AS counted,
         LEAST(sold, excess - LEAST(proven, excess)) AS sold_counted
  FROM j
)
SELECT
  k.nm_id,
  k.d                                                   AS date_msk,
  k.funnel_orders,
  k.api_gross,
  k.api_same_day,
  k.excess                                              AS funnel_excess_qty,
  k.proven                                              AS proven_refusal_srids,
  k.counted                                             AS refusal_counted_qty,
  k.sold_counted                                        AS sold_not_in_api_qty,
  k.excess - k.counted - k.sold_counted                 AS unexplained_qty,
  DATE_DIFF(ft.finance_final_through, k.d, DAY)         AS finance_age_days,
  ft.finance_final_through,
  cfg.maturity_days,
  CASE
    WHEN k.excess = 0                                                  THEN 'NO_EXCESS'
    WHEN k.excess - k.counted - k.sold_counted = 0                     THEN 'EXPLAINED'
    WHEN DATE_DIFF(ft.finance_final_through, k.d, DAY) >= cfg.maturity_days THEN 'MATURE_BUT_UNPROVEN'
    ELSE 'STILL_OPEN'
  END                                                   AS refusal_evidence_status,
  k.proven_srids,
  k.proven_logistics_rub
FROM k
CROSS JOIN ft
CROSS JOIN cfg
WHERE k.excess > 0 OR k.proven > 0 OR k.sold > 0;
