-- ============================================================================
-- EVETIS WB — Stage 3B. Сверка рекламного расхода: биллинг против атрибуции.
-- Дата: 20.08.2026, ревизия 2 (по ревью). Статус: в production НЕ выполнялось.
-- Зависимости: sql/mart/pr_mart1_facts.sql того же change-set (§1.7, §1.8).
--
-- ЗАЧЕМ ЭТА ВЬЮ СУЩЕСТВУЕТ
--   У рекламного расхода в WB две несовпадающие величины, и обе верны:
--     • БИЛЛИНГ    — FACT_ADS_COSTS_DAILY.actual_spend_rub. Что WB списал с баланса.
--                    Грейн (date, advert_id). Разреза по nm_id у источника НЕТ.
--     • АТРИБУЦИЯ  — FACT_ADS_SKU_DAILY.stats_spend_rub. Что WB отнёс на карточку
--                    в статистике кампании. Грейн (date, advert_id, nm_id).
--   Их суммы расходятся систематически, и разница НЕ является ошибкой загрузки.
--
--   🔴 Вью ничего не «исправляет» и не выбирает победителя. Она делает расхождение
--   наблюдаемым по каждой паре (сутки × кампания). Молчаливое приравнивание одной
--   величины к другой — ровно та ошибка, ради которой Stage 3B и делался.
--
-- 🔑 РАЗЛОЖЕНИЕ БИЛЛИНГА — ТРИ ВЗАИМОИСКЛЮЧАЮЩИЕ ЧАСТИ, В СУММЕ РОВНО СПИСАНИЕ:
--
--     actual_spend_rub =  billed_valid_sku_rub              -- дошло до SKU витрины
--                      +  billed_outside_sku_universe_rub   -- дошло до nm_id ВНЕ REF_SKU_MASTER
--                      +  billed_no_allocation_basis_rub    -- делить было не на что (Σ stats = 0)
--
--   Именно здесь, а не в MART_SKU_DAILY, живут ДЕНЬГИ остатка: его natural grain —
--   (date, advert_id) и сутки. В витрине с грейном (day, nm_id) повторённая во всех
--   строках дня сумма завышалась бы в SUM() ровно в число SKU, поэтому там оставлен
--   только булев флаг has_unallocated_billing.
--
-- ГРЕЙН 1/2: (date, advert_id) — ОБЪЕДИНЕНИЕ ключей биллинга и статистики.
--   Пара может существовать только в одном из источников: кампания со списанием,
--   но без статистики (её деньги нераспределимы), и кампания со статистикой, но без
--   списания (атрибуция без счёта). Обе обязаны быть видимы.
-- ГРЕЙН 2/2: date — дневная свёртка.
--
-- ⚠️ О ПОЛЕ billed_complete СМ. РАЗДЕЛ В КОНЦЕ ФАЙЛА. Определение «day <= MAX(date)»
--   ОПРОВЕРГНУТО данными 20.08 и в этой ревизии НЕ используется: до появления
--   coverage-контракта для adv/v1/upd полнота суток объявляется НЕИЗВЕСТНОЙ (NULL).
-- ============================================================================


-- ────────────────────────────────────────────────────────────
-- 1/2 · wb_mart.V_ADS_SPEND_RECONCILIATION — грейн (date, advert_id)
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION` AS
WITH universe AS (
  SELECT nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active = TRUE AND nm_id IS NOT NULL
  GROUP BY nm_id
),
costs AS (
  SELECT `date`, advert_id, actual_spend_rub, cost_rows
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_COSTS_DAILY`
),
stats AS (
  SELECT `date`, advert_id,
         SUM(stats_spend_rub)  AS attributed_spend_rub,
         COUNT(DISTINCT nm_id) AS attributed_sku_cnt
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`
  GROUP BY `date`, advert_id
),
alloc AS (
  SELECT `date`, advert_id,
         SUM(IF(nm_id IN (SELECT nm_id FROM universe), billed_alloc_rub, 0)) AS billed_valid_sku_rub,
         SUM(IF(nm_id IN (SELECT nm_id FROM universe), 0, billed_alloc_rub)) AS billed_outside_sku_universe_rub,
         COUNT(DISTINCT nm_id)                                               AS billed_sku_cnt,
         COUNT(DISTINCT IF(nm_id IN (SELECT nm_id FROM universe), NULL, nm_id)) AS billed_outside_sku_cnt
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`
  GROUP BY `date`, advert_id
),
unalloc AS (
  SELECT `date`, advert_id, unallocated_rub, reason
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY`
),
keys AS (
  SELECT `date`, advert_id FROM costs
  UNION DISTINCT
  SELECT `date`, advert_id FROM stats
)
SELECT
  k.`date`,
  k.advert_id,

  -- две исходные величины
  IFNULL(c.actual_spend_rub, 0)                  AS billing_actual_spend_rub,
  IFNULL(s.attributed_spend_rub, 0)              AS attributed_spend_rub,
  IFNULL(s.attributed_spend_rub, 0)
    - IFNULL(c.actual_spend_rub, 0)              AS attributed_minus_billed_rub,
  (c.advert_id IS NOT NULL)                      AS has_billing,
  (s.advert_id IS NOT NULL)                      AS has_attribution,

  -- 🔑 три взаимоисключающие части списания
  IFNULL(a.billed_valid_sku_rub, 0)              AS billed_valid_sku_rub,
  IFNULL(a.billed_outside_sku_universe_rub, 0)   AS billed_outside_sku_universe_rub,
  IFNULL(u.unallocated_rub, 0)                   AS billed_no_allocation_basis_rub,
  u.reason                                       AS no_allocation_basis_reason,

  -- 🔑 ИНВАРИАНТ: списание = сумма трёх частей. Ожидается ровно 0.
  IFNULL(c.actual_spend_rub, 0)
    - IFNULL(a.billed_valid_sku_rub, 0)
    - IFNULL(a.billed_outside_sku_universe_rub, 0)
    - IFNULL(u.unallocated_rub, 0)               AS residual_rub,

  -- та часть списания, которую витрина (грейн day × nm_id) физически НЕ несёт
  IFNULL(a.billed_outside_sku_universe_rub, 0)
    + IFNULL(u.unallocated_rub, 0)               AS billed_not_carried_by_mart_rub,

  -- диагностика состава
  IFNULL(c.cost_rows, 0)                         AS cost_rows,
  IFNULL(s.attributed_sku_cnt, 0)                AS attributed_sku_cnt,
  IFNULL(a.billed_sku_cnt, 0)                    AS billed_sku_cnt,
  IFNULL(a.billed_outside_sku_cnt, 0)            AS billed_outside_sku_cnt,

  -- ⚠️ ПОЛНОТА СУТОК НЕ ОПРЕДЕЛЕНА. См. раздел в конце файла: доказательства того,
  --    что «day <= MAX(date)» не отличает успешные нулевые сутки от незагруженных.
  CAST(NULL AS BOOL)                             AS billed_complete,
  CAST(NULL AS BOOL)                             AS billing_allocation_complete
FROM keys k
LEFT JOIN costs   c ON c.`date` = k.`date` AND c.advert_id = k.advert_id
LEFT JOIN stats   s ON s.`date` = k.`date` AND s.advert_id = k.advert_id
LEFT JOIN alloc   a ON a.`date` = k.`date` AND a.advert_id = k.advert_id
LEFT JOIN unalloc u ON u.`date` = k.`date` AND u.advert_id = k.advert_id;


-- ────────────────────────────────────────────────────────────
-- 2/2 · wb_mart.V_ADS_SPEND_RECONCILIATION_DAILY — грейн (date)
--       Дневная свёртка. billed_not_carried_by_mart_rub — ровно та величина,
--       наличие которой витрина отмечает флагом has_unallocated_billing.
--       Здесь она в рублях, и здесь ей место: грейн совпадает с natural grain.
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION_DAILY` AS
SELECT
  `date`,
  SUM(billing_actual_spend_rub)            AS billing_actual_spend_rub,
  SUM(attributed_spend_rub)                AS attributed_spend_rub,
  SUM(attributed_minus_billed_rub)         AS attributed_minus_billed_rub,

  -- три части списания
  SUM(billed_valid_sku_rub)                AS billed_valid_sku_rub,
  SUM(billed_outside_sku_universe_rub)     AS billed_outside_sku_universe_rub,
  SUM(billed_no_allocation_basis_rub)      AS billed_no_allocation_basis_rub,
  SUM(billed_not_carried_by_mart_rub)      AS billed_not_carried_by_mart_rub,
  SUM(residual_rub)                        AS residual_rub,

  COUNTIF(has_billing)                          AS campaigns_with_billing,
  COUNTIF(has_attribution)                      AS campaigns_with_attribution,
  COUNTIF(has_billing AND NOT has_attribution)  AS campaigns_billing_only,
  COUNTIF(has_attribution AND NOT has_billing)  AS campaigns_attribution_only,
  COUNTIF(billed_no_allocation_basis_rub > 0)   AS campaigns_no_allocation_basis,

  CAST(NULL AS BOOL)                       AS billed_complete,
  CAST(NULL AS BOOL)                       AS billing_allocation_complete
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_SPEND_RECONCILIATION`
GROUP BY `date`;


-- ============================================================================
-- ПОЧЕМУ billed_complete ЗДЕСЬ NULL, А НЕ ВЫЧИСЛЯЕТСЯ
--
-- Проверено read-only на RAW_WB_ADV_COSTS 20.08.2026 (129 суток, 37 ранов):
--
-- 1. Журнала ранов у adv/v1/upd НЕТ. Для query stats и bids он есть
--    (RAW_WB_ADV_QUERY_STATS_RUNS, RAW_WB_ADV_QUERY_BIDS_RUNS), для расходов —
--    нет. Ран, вернувший ноль строк, не оставляет следа. Поэтому «успешные сутки
--    с 0 ₽» и «сутки, которые не загрузились», СЕЙЧАС неразличимы. Живой пример:
--    18.08 запрошен дважды (19.08 и 20.08) и оба раза пуст, 19.08 запрошен один раз
--    и пуст. Обе даты могут быть и нулевым расходом, и пропуском.
--
-- 2. WB отдаёт строки ВНЕ запрошенного окна. Ран 02.08 с окном 26.07–01.08 вернул
--    строки с updDate = 02.08. Значит period_from/period_to — это то, что мы
--    ПРОСИЛИ, а не то, что нам ОТВЕТИЛИ; строить на нём покрытие нельзя.
--
-- 3. Биллинг НЕ финален на D+1 и НЕ монотонен. Ряд ответов по суткам:
--      02.08:  61 → 2544 (×7 ранов) → 2483    ← изменился на СЕДЬМОМ повторном чтении
--      04.08:   4 → 1463 (×6)       → 1459
--      05.08: 1117 → 1309 → 1117 (×5)         ← разовый выброс, потом откат
--      06.08: 1858 → 2050 (×6)                ← дозапись на D+2
--    Ни одно окно стабильности K не спасает: 02.08 и 04.08 менялись ПОСЛЕ семи
--    одинаковых чтений подряд. А после D+7 сутки просто перестают перечитываться —
--    их «стабильность» становится следствием того, что никто не спрашивает.
--
-- 4. Следствие для FACT: дедуп V_ADV_COSTS по (advertId, updTime, updSum) не умеет
--    выражать ОТЗЫВ записи. Строка, которую WB перестал отдавать, остаётся в
--    каноническом слое навсегда. На 20.08 FACT_ADS_COSTS_DAILY завышен на 334,00 ₽
--    по 4 суткам против последнего ответа WB (10.07, 02.08, 04.08, 05.08).
--
-- Пока эти четыре пункта не закрыты, любая формула billed_complete была бы
-- утверждением без доказательства. Fail-closed здесь — это NULL «неизвестно»,
-- а не FALSE и тем более не TRUE.
-- ============================================================================
