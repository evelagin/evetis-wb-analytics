-- ============================================================================
-- UNITKA WB — КЛАССЫ ОТМЕН: ЛОГИСТИКА И ПРИСУТСТВИЕ В ВОРОНКЕ (2026-11-02, OWNER ACK «CANCEL RECONCILIATION»)
-- ============================================================================
-- Проблема (сверка октября 2026 с официальной выгрузкой воронки WB, 02.11.2026):
--   * 18.10 / 535580776: заказ отменён через 26 минут, логистики нет. Воронка ОСТАВИЛА заказ в Q (Q = заказам Orders
--     API), а S листа брал только отмены следующих дней → отменённый заказ считался продажей (W завышен на AI).
--   * 24.10 / 252442517: заказ отменён на следующий день, логистики нет. Воронка заказ НЕ посчитала (Q = 0),
--     а S = 1 вычитал полную стоимость отмены заказа, которого в Q нет (W занижен на AI + AF + REV).
-- Правило «отмена дня заказа исчезает из воронки, отмена следующих дней остаётся» (контракт 20.09.2026) верно
-- для большинства случаев, но не для каждого заказа. Время отмены не определяет и логистику: в зрелой истории
-- (13.04–17.09.2026) 7 из 147 отмен дня заказа получили оба плеча, а 5 из 23 отмен следующего дня — ни одного.
--
-- Закон, проверенный на 04.09–31.10.2026 (27 SKU-дней с расхождением, ни одного противоречия):
--   Q(воронка) = заказы Orders API без отмены + отмены С логистикой + доказанные заказы вне Orders API
--                (+ ещё не объяснённые заказы вне Orders API); отмены БЕЗ логистики воронка обычно не считает.
-- Поэтому присутствие отменённого заказа в агрегированной воронке (srid в ней нет) доказывается арифметикой
-- суток × SKU:
--   room = Q − (заказы Orders API без отмены) − (доказанные отказы вне Orders API) − (продажи вне Orders API)
-- room — сколько мест в Q остаётся отменённым заказам. Отмены с логистикой занимают места первыми (закон выше),
-- отмены без логистики — оставшиеся. Отмена без логистики, которой не хватило места, в Q отсутствует.
-- Это НЕ правило S = MIN(S, Q): заказы без отмены и доказанные заказы вне Orders API вычитаются из Q до распределения.
-- Если места ровно столько, сколько отмен без логистики, неотличимы два случая: (а) в Q сам отменённый заказ,
-- (б) в Q заказ вне Orders API без следа в финотчёте. На зрелом финотчёте экономика у них одна — единица без продажи
-- и без логистики, вклад 0, — поэтому оба дают V = 1.
--
-- Экономика на листе (формула W строки: Q·AI − X − AG − S·(AI+AF+REV) + Y):
--   * отмена С логистикой в Q        → S (как раньше): вклад −AF − REV;
--   * отмена БЕЗ логистики в Q (V)   → S и поправка «+V·(AF+REVERSE_LEG_RATE)» в формуле W строки: вклад 0;
--   * отмена БЕЗ логистики вне Q     → не входит в S: вклад 0;
--   * отмена С логистикой вне Q      → на листе не представима (нужен вклад −AF − REV без −AI): ANOMALY, лист не меняется.
--
-- Зрелость (решение владельца 02.11.2026; уточнение аудита PR #394): отсутствие логистики доказано только окончательным
-- финотчётом, НЕПРЕРЫВНО покрывающим все сутки [дата заказа, дата заказа + 46] — тот же срок, что у отказов
-- (refusals_v1.sql, REFUSAL_MATURITY_DAYS). «Последний FINAL _rr_date» покрытия не доказывает: пропущенная неделя
-- в середине горизонта давала бы ложную зрелость. Покрытие строится из реестра финотчёта (§0):
--   интервал = загруженный недельный отчёт типа 1 (FINANCE_REPORT_LOADS, COMPLETE, обе даты заданы), чья неделя
--   окончательна по ОБОИМ типам отчёта 1 и 2 (FINANCE_WEEK_STATUS.weekly_final = TRUE);
--   неделя, разрезанная границей месяца, — два интервала одной недели (28–30.09 и 01–04.10), оба обязаны быть;
--   сутки зрелы ⇔ начало и конец горизонта внутри интервалов и у каждого интервала, кончающегося внутри горизонта,
--   есть интервал со следующего дня (разрыва нет).
-- NULL, пропуск недели, неизвестный тип или статус, незагруженная половина разрезанной недели → IMMATURE, никогда FINAL.
-- До зрелости класс IMMATURE: S прежний (legacy_cancels), поправки нет. Зрелый день всегда старше окна сверки
-- (35 суток), поэтому суточный писатель и сверка его не достигают: лист меняет только адресное восстановление
-- (загрузчик unitka-cancel-restore) по манифесту и OWNER ACK.
--
-- Начало — 2026-09-04: первый день счётчика Q из воронки API (01–03.09 — XLSX-бэкфилл со своими отменами).
-- Только SELECT, только wb_raw / wb_mart.
-- ============================================================================

-- ─── 0a. Интервалы окончательного финотчёта (реестр загрузок × статус недель) ──────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_FINANCE_FINAL_INTERVALS` AS
WITH l AS (
  SELECT date_from, date_to, report_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.FINANCE_REPORT_LOADS`
  WHERE report_period = 'WEEKLY' AND report_type = 1 AND status = 'COMPLETE'
    AND date_from IS NOT NULL AND date_to IS NOT NULL AND date_from <= date_to
),
w AS (
  SELECT week_start, report_type
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.FINANCE_WEEK_STATUS`
  WHERE weekly_final = TRUE AND report_type IN (1, 2) AND week_start IS NOT NULL
)
SELECT l.date_from, l.date_to, l.report_id
FROM l
JOIN w ON w.week_start <= l.date_from AND l.date_to < DATE_ADD(w.week_start, INTERVAL 7 DAY)
GROUP BY l.date_from, l.date_to, l.report_id
HAVING COUNT(DISTINCT w.report_type) = 2;

-- ─── 0b. Зрелость суток заказа: окончательный финотчёт непрерывно на [d, d + 46] ─────────────────
-- Только агрегаты по CROSS JOIN (BigQuery не исполняет NOT EXISTS с неравенством). Нет ни одного интервала —
-- строк нет вовсе, и класс суток остаётся IMMATURE (LEFT JOIN даёт NULL → не зрело).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_CANCEL_MATURITY` AS
WITH x AS (
  SELECT DISTINCT order_date AS d, DATE_ADD(order_date, INTERVAL 46 DAY) AS e
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`
  WHERE is_cancel AND order_date >= DATE '2026-09-04'
),
i AS (SELECT date_from AS f, date_to AS t FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_FINANCE_FINAL_INTERVALS`),
n AS (
  -- у интервала есть продолжение: другой интервал покрывает следующий за его концом день
  SELECT a.f, a.t, COUNTIF(b.f <= DATE_ADD(a.t, INTERVAL 1 DAY) AND DATE_ADD(a.t, INTERVAL 1 DAY) <= b.t) > 0 AS has_next
  FROM i a CROSS JOIN i b
  GROUP BY a.f, a.t
)
SELECT
  x.d                                                                    AS order_date,
  x.e                                                                    AS mature_on,
  COUNTIF(n.f <= x.d AND x.d <= n.t) > 0                                 AS start_covered,
  COUNTIF(n.f <= x.e AND x.e <= n.t) > 0                                 AS end_covered,
  COUNTIF(n.t >= x.d AND n.t < x.e AND NOT n.has_next) = 0               AS no_gap
FROM x CROSS JOIN n
GROUP BY x.d, x.e;

-- ─── 1. Отменённые заказы Orders API по srid: логистика из финотчёта ─────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_CANCEL_EVIDENCE` AS
WITH cfg AS (SELECT DATE '2026-09-04' AS funnel_api_from),
o AS (
  SELECT order_srid AS srid, nm_id, order_date, SUM(quantity) AS qty,
         MIN(SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE)) AS cancel_date
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, cfg
  WHERE is_cancel AND order_date >= cfg.funnel_api_from
  GROUP BY 1, 2, 3
)
SELECT
  o.srid,
  o.nm_id,
  o.order_date,
  o.cancel_date,
  DATE_DIFF(o.cancel_date, o.order_date, DAY)          AS cancel_lag_days,
  o.qty,
  IFNULL(e.logistics_legs, 0)                          AS logistics_legs,
  IFNULL(e.logistics_rub, 0)                           AS logistics_rub,
  IFNULL(e.logistics_legs, 0) > 0                      AS has_logistics
FROM o
LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_EVIDENCE` e ON e.srid = o.srid;

-- ─── 2. Сутки × SKU: присутствие отмен в воронке, итоговый S и поправка V ──────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_CANCEL_CLASS_DAILY` AS
WITH cfg AS (
  SELECT DATE '2026-09-04' AS funnel_api_from, 46 AS maturity_days
),
ft AS (
  SELECT MAX(date_to) AS finance_final_through FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_FINANCE_FINAL_INTERVALS`
),
mt AS (
  SELECT order_date AS d, IFNULL(start_covered AND end_covered AND no_gap, FALSE) AS mature
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_CANCEL_MATURITY`
),
fu AS (
  SELECT nm_id, date_msk AS d, MAX(orders_count) AS funnel_orders
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FUNNEL_DAILY`, cfg
  WHERE date_msk >= cfg.funnel_api_from
  GROUP BY 1, 2
),
api AS (
  SELECT nm_id, order_date AS d,
         SUM(IF(is_cancel, 0, quantity))                                                          AS kept_qty,
         -- прежний S из Orders API (V_UNITKA_DAILY_FACT / V_UNITKA_RECON_FACT до этого изменения): отмены следующих дней
         SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) > order_date, quantity, 0)) AS later_cancel_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS`, cfg
  WHERE order_date >= cfg.funnel_api_from
  GROUP BY 1, 2
),
ev AS (
  SELECT nm_id, order_date AS d,
         SUM(IF(has_logistics, qty, 0))                                                         AS cancel_log_qty,
         SUM(IF(has_logistics, 0, qty))                                                         AS cancel_nolog_qty,
         STRING_AGG(IF(has_logistics, srid, NULL), ',' ORDER BY srid)                           AS cancel_log_srids,
         STRING_AGG(IF(has_logistics, NULL, srid), ',' ORDER BY srid)                           AS cancel_nolog_srids,
         STRING_AGG(IF(has_logistics, NULL, CAST(cancel_date AS STRING)), ',' ORDER BY srid)     AS cancel_nolog_dates
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_CANCEL_EVIDENCE`
  GROUP BY 1, 2
),
rf AS (
  SELECT nm_id, date_msk AS d, refusal_counted_qty, sold_not_in_api_qty
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_REFUSAL_DAILY`
),
j AS (
  SELECT
    ev.nm_id, ev.d, fu.funnel_orders,
    IFNULL(api.kept_qty, 0) AS kept_qty, IFNULL(api.later_cancel_qty, 0) AS later_cancel_qty,
    ev.cancel_log_qty, ev.cancel_nolog_qty, ev.cancel_log_srids, ev.cancel_nolog_srids, ev.cancel_nolog_dates,
    IFNULL(rf.refusal_counted_qty, 0) AS refusal_counted_qty, IFNULL(rf.sold_not_in_api_qty, 0) AS sold_not_in_api_qty
  FROM ev
  LEFT JOIN fu  USING (nm_id, d)
  LEFT JOIN api USING (nm_id, d)
  LEFT JOIN rf  USING (nm_id, d)
),
k AS (
  SELECT j.*,
         j.funnel_orders - j.kept_qty - j.refusal_counted_qty - j.sold_not_in_api_qty                       AS room,
         j.later_cancel_qty + j.refusal_counted_qty                                                         AS legacy_cancels
  FROM j
),
a AS (
  SELECT k.*,
         LEAST(k.cancel_log_qty, GREATEST(k.room, 0))                                                       AS log_in_q,
         LEAST(k.cancel_nolog_qty, GREATEST(k.room - LEAST(k.cancel_log_qty, GREATEST(k.room, 0)), 0))       AS nolog_in_q
  FROM k
),
c AS (
  SELECT a.*,
         DATE_ADD(a.d, INTERVAL cfg.maturity_days DAY)                                                      AS mature_on,
         ft.finance_final_through,
         CASE
           WHEN a.funnel_orders IS NULL                                            THEN 'NO_FUNNEL'
           WHEN NOT IFNULL(mt.mature, FALSE)                                      THEN 'IMMATURE'
           WHEN a.room < 0                                                         THEN 'ANOMALY_FUNNEL_BELOW_API'
           WHEN a.cancel_log_qty > a.log_in_q                                      THEN 'ANOMALY_ABSENT_WITH_LOGISTICS'
           ELSE 'FINAL'
         END                                                                                                AS cancel_class_status
  FROM a CROSS JOIN ft CROSS JOIN cfg
  LEFT JOIN mt ON mt.d = a.d
)
SELECT
  nm_id,
  d                                                     AS date_msk,
  funnel_orders,
  kept_qty,
  cancel_log_qty,
  cancel_nolog_qty,
  refusal_counted_qty,
  sold_not_in_api_qty,
  room,
  log_in_q,
  nolog_in_q,
  cancel_nolog_qty - nolog_in_q                         AS nolog_absent_qty,
  legacy_cancels,
  refusal_counted_qty + log_in_q + nolog_in_q           AS final_cancels,
  nolog_in_q                                            AS void_qty,
  cancel_class_status,
  IF(cancel_class_status = 'FINAL', refusal_counted_qty + log_in_q + nolog_in_q, legacy_cancels) AS effective_cancels,
  IF(cancel_class_status = 'FINAL', nolog_in_q, 0)      AS effective_void_qty,
  cancel_class_status = 'FINAL'
    AND (refusal_counted_qty + log_in_q + nolog_in_q <> legacy_cancels OR nolog_in_q > 0) AS sheet_change_required,
  cancel_log_srids,
  cancel_nolog_srids,
  cancel_nolog_dates,
  finance_final_through,
  mature_on
FROM c;
