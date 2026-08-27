-- ============================================================================
-- STAGE 3.1C PR1 — ADVERTISING BILLING CLASSIFICATION LAYER
-- Дата: 2026-08-27.  База: HEAD 7c74cde2d320a9bb71397705454bfd1cc7c7ff45.
-- Контракт: docs/STAGE3_1C_AD_BILLING_CLASSIFICATION_2026-08-27.md
-- Откат: tools/stage3_1c_ad_billing_rollback.sh [--dry-run]
--
-- ЗАЧЕМ. Forensic Stage 3.1B/3.1C показал: account-level операция WB
--   «Удержание» — не одна экономическая сущность, а как минимум три, и её
--   основная часть является рекламным биллингом, который в Executive
--   вычитается ВТОРОЙ раз поверх атрибутированной рекламы. Прежде чем править
--   финансовую формулу, нужно уметь эти сущности различать и наблюдать.
--
-- ЧЕГО ЭТОТ PR НЕ ДЕЛАЕТ (owner ACK).
--   Не изменяет V_DASH_KPI_DAILY, V_DASH_SKU_DAILY, Executive, SKU Performance,
--   Metabase, FACT_*, MART_SKU_DAILY, REF_COST_MAP, evetis_ref, V_*_COGS.
--   Не исключает рекламу из deduction_rub — это Stage 3.1C PR2.
--   Не распределяет billed-рекламу по SKU и не подменяет ею attributed.
--   Не начинает Stage 3B. Числа на живых экранах не меняются.
--
-- 🔴 КЛАСС ДОКАЗАТЕЛЬСТВА. Отнесение hex40 к рекламному биллингу —
--   STRONG_RECONSTRUCTION, а НЕ подтверждённый документ. WB не передаёт в
--   операции «Удержание» ни одного описательного атрибута: у всех 388 строк
--   RAW пусты doc_type_name, bonus_type, sa_name, office_name, subject_name,
--   barcode, а nm_id = 0. Основания реконструкции:
--     • 100 % рекламы в V_ADV_COSTS имеет paymentType = «Баланс», то есть
--       оплачивается с баланса счёта, который закрывается финотчётом;
--     • недельная корреляция hex40 ↔ реклама = 0,9932;
--     • отношение clean-window billed/ads = 1,0097, структурный разрыв 0,97 %;
--     • медианное абсолютное расхождение 3,01 %, 17/17 недель в пределах ±20 %;
--     • гипотезы «комиссия / логистика / хранение / штрафы / приёмка / транзит»
--       отвергнуты: каждая из них — отдельная операция того же отчёта.
--   Формулировка CONFIRMED_AD_BILLING в этом слое ЗАПРЕЩЕНА.
--
-- 🔴 FAIL-CLOSED. Правило НЕ имеет вида «hex40 -> реклама, иначе прочее».
--   Форма srid — реконструированный технический признак, контрактом WB не
--   закреплённый. Любой НЕизвестный формат остаётся UNCLASSIFIED_DEDUCTION,
--   увеличивает unknown-долю и виден в мониторинге. UNKNOWN != 0 и
--   UNKNOWN != другая известная статья.
--
-- 🔴 nm_id В КЛАССИФИКАТОРЕ ОТСУТСТВУЕТ. Удержание уровня счёта не является
--   затратой SKU; привязка к товару здесь была бы фальсификацией.
-- ============================================================================


-- ────────────────────────────────────────────────────────────────────────────
-- 1/2 · V_WB_DEDUCTIONS_CLASSIFIED — грейн finance_row_key.
--   Универсум: FACT_FINANCE WHERE supplier_oper_name = 'Удержание'.
--   srid берётся из canonical RAW по ключу report_id#rrd_id: FACT_FINANCE его
--   не несёт (тот же приём, что в Stage 3.1B R3). Проекция сведена GROUP BY,
--   поэтому грейн гарантируется здесь, а не предполагается.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
OPTIONS (description = 'Stage 3.1C PR1. Classification of WB account-level "Удержание" operations. Grain = finance_row_key. Classes: AD_BILLING_RECONSTRUCTED (STRONG_RECONSTRUCTION, hex40 srid pattern - NOT a confirmed WB document), TRANSIT_DEDUCTION (self-describing srid), UNCLASSIFIED_DEDUCTION (fail-closed, unknown economic nature), CLASSIFICATION_CONFLICT. Contains no nm_id: account-level deductions are not SKU costs. Does not change any existing dashboard metric.')
AS
WITH
srid_map AS (
  SELECT
    CONCAT(report_id, '#', rrd_id)         AS row_key,
    ANY_VALUE(NULLIF(TRIM(srid), ''))      AS srid
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FINANCE`
  WHERE supplier_oper_name = 'Удержание'
  GROUP BY row_key
),
supplies AS (
  SELECT DISTINCT CAST(supply_id AS STRING) AS supply_id
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
  WHERE supply_id IS NOT NULL
),
base AS (
  SELECT
    f.finance_row_key,
    f.finance_date,
    f.supplier_oper_name,
    s.srid,
    IFNULL(f.deduction, NUMERIC '0')                                    AS deduction_rub,
    IFNULL(f.additional_payment, NUMERIC '0')                           AS additional_payment_rub,
    IFNULL(f.deduction, NUMERIC '0') + IFNULL(f.additional_payment, NUMERIC '0') AS deduction_amount_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` f
  LEFT JOIN srid_map s ON s.row_key = f.finance_row_key
  WHERE f.supplier_oper_name = 'Удержание'
),
-- Признаки правил вычисляются независимо, чтобы их одновременное срабатывание
-- было ОБНАРУЖИМО, а не поглощено порядком CASE.
matched AS (
  SELECT b.*,
    REGEXP_CONTAINS(IFNULL(b.srid, ''), r'^transit_deduction_[0-9]+$') AS rule_transit,
    REGEXP_CONTAINS(IFNULL(b.srid, ''), r'^[0-9a-f]{40}$')             AS rule_hex40
  FROM base b
),
classified AS (
  SELECT m.*,
    CASE
      WHEN m.rule_transit AND m.rule_hex40 THEN 'CLASSIFICATION_CONFLICT'
      WHEN m.rule_transit                  THEN 'TRANSIT_DEDUCTION'
      WHEN m.rule_hex40                    THEN 'AD_BILLING_RECONSTRUCTED'
      ELSE                                      'UNCLASSIFIED_DEDUCTION'
    END AS deduction_class
  FROM matched m
)
SELECT
  c.finance_row_key,
  c.finance_date,
  c.supplier_oper_name,
  c.srid,
  c.deduction_rub,
  c.additional_payment_rub,
  c.deduction_amount_rub,
  c.deduction_class,
  CASE c.deduction_class
    WHEN 'TRANSIT_DEDUCTION'        THEN 'CONFIRMED_SYSTEM_PATTERN'
    WHEN 'AD_BILLING_RECONSTRUCTED' THEN 'STRONG_RECONSTRUCTION'
    WHEN 'CLASSIFICATION_CONFLICT'  THEN 'CONFLICT'
    ELSE                                 'UNKNOWN'
  END                                                                   AS deduction_class_confidence,
  CASE c.deduction_class
    WHEN 'TRANSIT_DEDUCTION'        THEN 'SRID_TRANSIT_PATTERN'
    WHEN 'AD_BILLING_RECONSTRUCTED' THEN 'HEX40_STRONG_RECONSTRUCTION'
    WHEN 'CLASSIFICATION_CONFLICT'  THEN 'MULTI_RULE_MATCH'
    ELSE                                 'NO_RULE_MATCH'
  END                                                                   AS classification_rule,
  (c.deduction_class = 'AD_BILLING_RECONSTRUCTED')                      AS is_ad_billing_reconstructed,
  (c.deduction_class = 'TRANSIT_DEDUCTION')                             AS is_transit_deduction,
  (c.deduction_class = 'UNCLASSIFIED_DEDUCTION')                        AS is_unclassified,
  IF(c.deduction_class = 'TRANSIT_DEDUCTION',
     REGEXP_EXTRACT(c.srid, r'^transit_deduction_([0-9]+)$'), NULL)      AS transit_supply_id,
  -- Диагностика, а НЕ условие класса: исторический источник поставок неполон,
  -- и неразрешённый supply_id не должен лишать строку корректной классификации.
  IF(c.deduction_class = 'TRANSIT_DEDUCTION', (sup.supply_id IS NOT NULL), NULL)
                                                                        AS transit_supply_resolved,
  CURRENT_TIMESTAMP()                                                   AS generated_at
FROM classified c
LEFT JOIN supplies sup
       ON c.deduction_class = 'TRANSIT_DEDUCTION'
      AND sup.supply_id = REGEXP_EXTRACT(c.srid, r'^transit_deduction_([0-9]+)$');


-- ────────────────────────────────────────────────────────────────────────────
-- 2/2 · V_ADVERTISING_RECONCILIATION_DAILY — грейн day.
--   Сводит рекламный биллинг (из классификатора) с рекламной атрибуцией
--   (FACT_ADS_SKU_DAILY) и показывает структуру удержаний за те же сутки.
--
-- 🔴 ПОКРЫТИЕ АТРИБУЦИИ (owner ACK, поправка №5). Атрибуция существует только
--   с 13.04.2026, а удержания — с 10.2024. Вне окна покрытия
--   ad_spend_attributed_rub и ad_spend_unallocated_rub = NULL, а НЕ 0: иначе
--   разрыв «billed − 0» показал бы миллионы рублей несуществующего
--   unallocated. Факт покрытия выведен явным флагом ads_attribution_covered.
--
-- 🔴 ЗНАК unallocated НЕ СКРЫВАЕТСЯ. Ни ABS, ни GREATEST(...,0): это разница
--   сверки, а не статья затрат. Отрицательное значение — нормальное состояние
--   (биллинг отстал от атрибуции), и оно обязано быть видимым.
--
-- 🔴 ЭТА ВЬЮ НИЧЕГО НЕ ВЫЧИТАЕТ. Она только измеряет. Исключение рекламы из
--   account-level удержаний — предмет Stage 3.1C PR2 и отдельного ACK.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADVERTISING_RECONCILIATION_DAILY`
OPTIONS (description = 'Stage 3.1C PR1. Daily reconciliation of advertising billed (reconstructed from WB account-level deductions) vs advertising attributed (FACT_ADS_SKU_DAILY), plus deduction structure. Outside the attribution coverage window attributed and unallocated are NULL, not zero. Unallocated keeps its sign - it is a reconciliation difference, not a cost bucket. This view subtracts nothing and changes no existing dashboard metric.')
AS
WITH
ded_day AS (
  SELECT
    finance_date                                                                       AS day,
    SUM(IF(deduction_class = 'AD_BILLING_RECONSTRUCTED', deduction_amount_rub, NUMERIC '0')) AS ad_spend_billed_rub,
    SUM(IF(deduction_class = 'TRANSIT_DEDUCTION',        deduction_amount_rub, NUMERIC '0')) AS transit_deduction_rub,
    SUM(IF(deduction_class = 'UNCLASSIFIED_DEDUCTION',   deduction_amount_rub, NUMERIC '0')) AS unclassified_deduction_rub,
    SUM(IF(deduction_class = 'CLASSIFICATION_CONFLICT',  deduction_amount_rub, NUMERIC '0')) AS conflict_deduction_rub,
    SUM(deduction_amount_rub)                                                          AS total_deduction_rub,
    COUNT(*)                                                                           AS deduction_rows,
    COUNTIF(deduction_class = 'AD_BILLING_RECONSTRUCTED')                              AS ad_billing_rows,
    COUNTIF(deduction_class = 'CLASSIFICATION_CONFLICT')                               AS conflict_rows
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  GROUP BY day
),
ads_day AS (
  SELECT `date` AS day, SUM(stats_spend_rub) AS ad_spend_attributed_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`
  GROUP BY day
),
ads_bounds AS (
  SELECT MIN(`date`) AS cov_from, MAX(`date`) AS cov_to
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ADS_SKU_DAILY`
),
spine AS (
  SELECT COALESCE(d.day, a.day) AS day, d.* EXCEPT(day), a.ad_spend_attributed_rub AS attr_raw
  FROM ded_day d FULL OUTER JOIN ads_day a ON a.day = d.day
)
SELECT
  s.day,
  -- Покрытие атрибуции — предусловие всех сравнений ниже.
  (s.day BETWEEN b.cov_from AND b.cov_to)                                    AS ads_attribution_covered,
  IF(s.day BETWEEN b.cov_from AND b.cov_to,
     IFNULL(s.attr_raw, NUMERIC '0'), NULL)                                  AS ad_spend_attributed_rub,
  IFNULL(s.ad_spend_billed_rub, NUMERIC '0')                                 AS ad_spend_billed_rub,
  IF(s.day BETWEEN b.cov_from AND b.cov_to,
     IFNULL(s.ad_spend_billed_rub, NUMERIC '0') - IFNULL(s.attr_raw, NUMERIC '0'),
     NULL)                                                                   AS ad_spend_unallocated_rub,
  IF(IFNULL(s.conflict_rows, 0) > 0, 'CONFLICT',
     IF(IFNULL(s.ad_billing_rows, 0) > 0, 'STRONG_RECONSTRUCTION', 'NOT_APPLICABLE'))
                                                                             AS ad_billing_classification_confidence,
  IFNULL(s.transit_deduction_rub,      NUMERIC '0')                          AS transit_deduction_rub,
  IFNULL(s.unclassified_deduction_rub, NUMERIC '0')                          AS unclassified_deduction_rub,
  IFNULL(s.conflict_deduction_rub,     NUMERIC '0')                          AS conflict_deduction_rub,
  -- Классифицированное = реклама + транзит. Конфликт классифицированным НЕ
  -- считается и в автоматическую корректировку попасть не может.
  IFNULL(s.ad_spend_billed_rub, NUMERIC '0') + IFNULL(s.transit_deduction_rub, NUMERIC '0')
                                                                             AS classified_deduction_rub,
  IFNULL(s.total_deduction_rub, NUMERIC '0')                                 AS total_deduction_rub,
  IFNULL(s.deduction_rows, 0)                                                AS deduction_rows,
  CURRENT_TIMESTAMP()                                                        AS generated_at
FROM spine s
CROSS JOIN ads_bounds b;
