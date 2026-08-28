-- ============================================================================
-- STAGE 3.1G — РАСЧЁТЫ С WILDBERRIES (SETTLEMENT BRIDGE) ДЛЯ EXECUTIVE
-- Дата: 2026-08-28.  База: HEAD 33be7ee67d8157f7d51d5e760926adda185a59c0.
-- Контракт: docs/STAGE3_1G_WB_SETTLEMENT_EXECUTIVE_2026-08-28.md
-- Откат: tools/stage3_1g_settlement_rollback.sh [--dry-run]
--
-- ЗАЧЕМ. Executive показывает управленческую экономику, но не отвечает на
--   вопрос «сколько WB реально перечислил». Stage 3.1E доказал формулу выплаты
--   на восьми закрытых еженедельных финотчётах: 110 101,96 руб. за
--   27.07-23.08.2026, все четыре недели до копейки. Этот слой выводит мост
--
--       К перечислению за товар -> Удержания после реализации -> К выплате от WB
--
--   на суточный грейн, пригодный для дашборда.
--
-- 🔴 ЭТО SETTLEMENT / CASH, А НЕ УПРАВЛЕНЧЕСКИЙ РЕЗУЛЬТАТ. Здесь нет
--   себестоимости товара, расходов фулфилмента, OPEX и налога; здесь нет
--   attributed-рекламы и нет seller-base выручки. Величины этого слоя
--   НЕ ДОЛЖНЫ совпадать с period_result_* и сравнивать их напрямую нельзя.
--
-- 🔴 ЭТО РАСЧЁТ ПО ОТЧЁТАМ WB, А НЕ ПОДТВЕРЖДЁННОЕ ПОСТУПЛЕНИЕ НА СЧЁТ.
--   Банковские транзакции в систему не заведены, поэтому формулировки
--   «получено», «деньги на счёте», «cash flow» к wb_payout_rub неприменимы.
--
-- ИСТОЧНИК. wb_raw.V_WB_FINANCE_SEMANTIC — canonical-слой финотчётов, тот же,
--   на котором Stage 3.1E воспроизвёл официальную выплату. Дата — _rr_date,
--   она же finance_date: проверено, 0 расхождений на всех строках FACT_FINANCE.
--   Universe включает ОБА типа еженедельного отчёта (основной и «по выкупам»).
--
-- 🔴 ЗНАКИ ИСТОЧНИКА СОХРАНЯЮТСЯ. Ни ABS, ни GREATEST, ни переворотов.
--   loyalty_points_rub приходит отрицательным (баллы удержаны) и вычитается
--   как есть — именно поэтому он увеличивает выплату.
--   Единственная осознанная инверсия — post_realization_deductions_rub,
--   который выдаётся ОТРИЦАТЕЛЬНЫМ, чтобы мост читался сложением:
--       settlement_goods_rub + post_realization_deductions_rub = wb_payout_rub
--
-- 🔴 rebill_logistics В ФОРМУЛУ НЕ ВХОДИТ. Проверено на всех четырёх закрытых
--   неделях: включение «Возмещения издержек по перевозке» ломает сверку
--   (110 101,96 -> 116 730,70). Это возмещение уже учтено внутри логистики.
--
-- ЧЕГО ЭТОТ ЭТАП НЕ ДЕЛАЕТ. Не меняет FACT_FINANCE, MART_SKU_DAILY,
--   REF_COST_MAP, объекты Product COGS (Stage 3.1A/3.1B/3.1D), классификатор
--   удержаний (Stage 3.1F), V_DASH_KPI_DAILY, V_DASH_FINANCE_CORRECTED_DAILY,
--   V_DASH_EXECUTIVE_ECONOMICS_DAILY и SKU Performance. Управленческие числа
--   Executive этим слоем не затрагиваются вообще.
-- ============================================================================


-- ────────────────────────────────────────────────────────────────────────────
-- 1/1 · V_DASH_SETTLEMENT_DAILY — грейн finance_date, одна строка на сутки.
--
-- 🔴 ГЕЙТ ЗАКРЫТОСТИ. Выплата — это деньги. Показывать незакрытую финансовую
--   неделю как «к выплате» нельзя: WB её ещё не рассчитал. Поэтому денежные
--   поля публикуются ТОЛЬКО для суток, которые одновременно
--     • внутри финансового интервала (finance_covered) и
--     • принадлежат закрытой неделе (finance_is_final).
--   Гейты берутся из действующего V_DASH_KPI_DAILY — своё определение
--   покрытия этот слой НЕ изобретает.
--   Вне гейта денежные поля = NULL, а не ноль: неизвестное остаётся
--   неизвестным. Счётчики суток выведены отдельно, поэтому усечение периода
--   доказуемо, а не молчаливо.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY`
OPTIONS (description = 'Stage 3.1G. WB settlement bridge for Executive, grain finance_date (one row per finance day). settlement_goods_rub + post_realization_deductions_rub = wb_payout_rub. Source: wb_raw.V_WB_FINANCE_SEMANTIC, both weekly report types. Source signs preserved; the only deliberate inversion is post_realization_deductions_rub, published negative so the bridge reads as addition. Money fields are published ONLY for days that are finance_covered AND finance_is_final: an open WB week is not settled cash. Outside that gate the fields are NULL, never zero. THIS IS SETTLEMENT, NOT MANAGEMENT ECONOMICS: no Product COGS, no fulfilment, no OPEX, no tax, no attributed advertising. It is the amount WB reports as payable, NOT a confirmed bank receipt.')
AS
WITH
-- 🔴 IFNULL(...,0) ЗДЕСЬ ДОКАЗУЕМ, А НЕ МАСКИРУЕТ НЕИЗВЕСТНОЕ. Универсум —
--   ВСЕ строки финотчёта за эти сутки. Если статьи в отчёте нет, SUM вернёт
--   NULL, и это означает «такого начисления в отчёте нет», а не «величина
--   неизвестна»: отчёт закрыт и перечисляет всё, что WB начислил. Ноль
--   доказуем счётчиками finance_rows / finance_rows_type_*.
--   Неизвестность выражается ИНАЧЕ — гейтом закрытых суток ниже, где все
--   денежные поля становятся NULL целиком.
fin AS (
  SELECT
    _rr_date                                                                 AS day,
    IFNULL(SUM(SAFE_CAST(REPLACE(for_pay, ',', '.') AS NUMERIC)),            NUMERIC '0') AS settlement_goods_rub,
    IFNULL(SUM(SAFE_CAST(REPLACE(logistics_amount, ',', '.') AS NUMERIC)),   NUMERIC '0') AS logistics_rub,
    IFNULL(SUM(SAFE_CAST(REPLACE(storage_fee, ',', '.') AS NUMERIC)),        NUMERIC '0') AS storage_rub,
    IFNULL(SUM(SAFE_CAST(REPLACE(deduction, ',', '.') AS NUMERIC)),          NUMERIC '0') AS deductions_rub,
    IFNULL(SUM(SAFE_CAST(REPLACE(penalty, ',', '.') AS NUMERIC)),            NUMERIC '0') AS penalty_rub,
    IFNULL(SUM(SAFE_CAST(REPLACE(additional_payment, ',', '.') AS NUMERIC)), NUMERIC '0') AS additional_payment_rub,
    IFNULL(SUM(SAFE_CAST(REPLACE(acceptance, ',', '.') AS NUMERIC)),         NUMERIC '0') AS acceptance_rub,
    IFNULL(SUM(loyalty_points_rub),                                          NUMERIC '0') AS loyalty_points_rub,
    COUNT(*)                                                       AS finance_rows,
    COUNTIF(SAFE_CAST(JSON_VALUE(raw_json, '$.reportType') AS INT64) = 1) AS finance_rows_type_main,
    COUNTIF(SAFE_CAST(JSON_VALUE(raw_json, '$.reportType') AS INT64) = 2) AS finance_rows_type_buyouts,
    -- 🔴 reportType WB начал передавать 2026-07-13; у исторических строк он
    --   пуст. Это НЕ дефект и не потеря: тип отчёта не участвует в расчёте
    --   выплаты, он нужен только для сверки с бумажными отчётами. Строки без
    --   типа считаются отдельно, чтобы разложение оставалось полным.
    COUNTIF(SAFE_CAST(JSON_VALUE(raw_json, '$.reportType') AS INT64) IS NULL) AS finance_rows_type_unknown
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
  GROUP BY day
),
gate AS (
  SELECT day, finance_covered, finance_is_final, contains_provisional_finance, is_current_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
),
j AS (
  SELECT f.*, g.finance_covered, g.finance_is_final,
         g.contains_provisional_finance, g.is_current_day,
         -- Сумма всех удержаний после реализации, знаки источника сохранены.
         ( f.logistics_rub + f.storage_rub + f.deductions_rub + f.penalty_rub
         + f.additional_payment_rub + f.acceptance_rub + f.loyalty_points_rub ) AS ded_total_positive
  FROM fin f LEFT JOIN gate g USING (day)
)

SELECT
  j.day,

  -- ── Гейты. Своё определение покрытия слой не вводит. ──
  IFNULL(j.finance_covered, FALSE)                        AS settlement_covered,
  IFNULL(j.finance_is_final, FALSE)                       AS settlement_is_final,
  j.contains_provisional_finance,
  j.is_current_day,
  (IFNULL(j.finance_covered, FALSE) AND IFNULL(j.finance_is_final, FALSE))
                                                          AS settlement_eligible,
  IF(IFNULL(j.finance_covered, FALSE) AND IFNULL(j.finance_is_final, FALSE), 1, 0)
                                                          AS settlement_eligible_day,
  -- Сутки с финансовыми строками, но ещё не закрытые WB. Делают усечение
  -- выбранного периода доказуемым, а не молчаливым.
  IF(IFNULL(j.finance_covered, FALSE) AND IFNULL(j.finance_is_final, FALSE), 0, 1)
                                                          AS settlement_open_day,

  -- ── МОСТ. Публикуется только на закрытых сутках. ──
  IF(j.finance_covered AND j.finance_is_final, j.settlement_goods_rub, NULL)
                                                          AS settlement_goods_rub,
  -- 🔴 Единственная осознанная инверсия знака в слое: расход выдаётся
  --    отрицательным, чтобы мост читался сложением, а не вычитанием.
  IF(j.finance_covered AND j.finance_is_final, -j.ded_total_positive, NULL)
                                                          AS post_realization_deductions_rub,
  IF(j.finance_covered AND j.finance_is_final,
     j.settlement_goods_rub - j.ded_total_positive, NULL)  AS wb_payout_rub,

  -- ── Компоненты удержаний. Знаки источника, для сверки и будущего drill-down. ──
  IF(j.finance_covered AND j.finance_is_final, j.logistics_rub,          NULL) AS logistics_rub,
  IF(j.finance_covered AND j.finance_is_final, j.storage_rub,            NULL) AS storage_rub,
  IF(j.finance_covered AND j.finance_is_final, j.deductions_rub,         NULL) AS deductions_rub,
  IF(j.finance_covered AND j.finance_is_final, j.penalty_rub,            NULL) AS penalty_rub,
  IF(j.finance_covered AND j.finance_is_final, j.additional_payment_rub, NULL) AS additional_payment_rub,
  IF(j.finance_covered AND j.finance_is_final, j.acceptance_rub,         NULL) AS acceptance_rub,
  IF(j.finance_covered AND j.finance_is_final, j.loyalty_points_rub,     NULL) AS loyalty_points_rub,

  -- ── Диагностика состава (не метрики экрана) ──
  j.finance_rows,
  j.finance_rows_type_main,
  j.finance_rows_type_buyouts,
  j.finance_rows_type_unknown,

  'WB_SETTLEMENT'                                         AS economics_basis,
  'Расчёты с Wildberries по финансовым отчётам WB за выбранный период. Это НЕ управленческий результат и НЕ прибыль: здесь не учитываются себестоимость товара, расходы фулфилмента, OPEX и прочие расходы бизнеса. «К выплате от WB» — сумма расчётов по отчётам площадки, а не подтверждённое поступление на банковский счёт. Включаются только закрытые финансовые сутки: открытая неделя WB ещё не рассчитана и в мост не входит.'
                                                          AS economics_note,
  CURRENT_TIMESTAMP()                                     AS generated_at

FROM j;
