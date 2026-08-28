-- ============================================================================
-- STAGE 3.1F — КЛАССИФИКАЦИЯ УДЕРЖАНИЙ WB ПО ПРЯМЫМ ОСНОВАНИЯМ ПЛАТЕЖА
-- Дата: 2026-08-28.  База: HEAD 3024789cb346f8714597da3d6d5ff10e0c9d74bf.
-- Контракт: docs/STAGE3_1F_DEDUCTION_DIRECT_LABELS_2026-08-28.md
-- Откат: tools/stage3_1f_direct_labels_rollback.sh [--dry-run]
--
-- ЗАЧЕМ. Stage 3.1E сверил production с восемью закрытыми финотчётами WB за
--   27.07–23.08.2026. Денежная часть сошлась точно (выплата 110 101,96 руб.),
--   но классификатор Stage 3.1C PR1 отнёс к рекламному биллингу две операции,
--   рекламой не являющиеся:
--     2026-07-22  −49 479,72  «Добровольная выплата за товары, пострадавшие
--                              в результате обстоятельств непреодолимой силы»
--     2026-07-30  −20 000,00  та же категория, другой документ
--     2026-08-10       +38,00  «Отчет об утилизированном товаре (по складу)»
--   Причина: правило HEX40_STRONG_RECONSTRUCTION срабатывает на ФОРМЕ srid,
--   а не на смысле операции. Контракт PR1 прямо предупреждал, что форма srid
--   контрактом WB не закреплена, — здесь это сработало против нас.
--
-- ЧТО ИЗМЕНИЛОСЬ В ИСТОЧНИКЕ. Stage 3.1F установил, что WB с 2026-07-19
--   передаёт человекочитаемое основание платежа в bonusTypeName. Покрытие:
--   0 % за 2024 и 2025, 57,1 % в июле 2026, 100 % в августе 2026.
--   Там, где поле есть, оно раскладывает корпус в шесть категорий БЕЗ остатка.
--
-- 🔴 DESIGN A (owner ACK). Прямое свидетельство имеет приоритет над эвристикой
--   формы. Порядок строго такой:
--     1. bonus_type_name непуст и распознан  -> семантический класс;
--     2. bonus_type_name непуст и НЕ распознан -> UNKNOWN_SEMANTIC;
--     3. bonus_type_name пуст -> прежняя эвристика по srid как fallback.
--   Шаг 2 — самое важное правило. Откат к форме srid при наличии непустого
--   основания ЗАПРЕЩЁН: именно так возникли все три текущие ошибки.
--
-- 🔴 ИСТОРИЯ НЕ ПЕРЕПИСЫВАЕТСЯ. До 2026-07-19 bonus_type_name пуст на 100 %
--   строк, поэтому 2024 и 2025 классифицируются ровно как раньше, а
--   uuid36-популяция 2025 года (128 строк, 794 732,04 руб.) остаётся
--   UNCLASSIFIED_DEDUCTION. Stage 3.1F её не трогает и не переинтерпретирует.
--
-- 🔴 ФОРС-МАЖОР — OPTION B (owner ACK). Добровольные выплаты за пострадавший
--   товар признаны ИСКЛЮЧИТЕЛЬНЫМИ, вне операционного результата. Они:
--     • остаются в сыром settlement и в реконструкции выплаты WB;
--     • НЕ являются рекламой;
--     • НЕ уменьшают операционные расходы уровня кабинета;
--     • НЕ улучшают period_result_pre_cogs_corrected_rub;
--     • публикуются отдельной величиной exceptional_compensation_rub.
--
-- 🔴 ЗНАК. Инверсия выполняется РОВНО ОДИН РАЗ и только здесь:
--     force_majeure_rub          — сырая величина как в отчёте WB
--                                  (отрицательная = деньги продавцу);
--     exceptional_compensation_rub = −force_majeure_rub
--                                  (положительная = выгода продавца).
--   Ниже по слоям знак больше не переворачивается.
--
-- ЧЕГО ЭТОТ ЭТАП НЕ ДЕЛАЕТ. Не трогает reference-слой Product COGS, объекты
--   Stage 3.1B (V_FACT_FINANCE_COGS, V_MART_SKU_DAILY_COGS), REF_COST_MAP,
--   FACT_FINANCE, MART_SKU_DAILY, SKU Performance. Не меняет сырой settlement
--   и потому не может изменить выплату WB. Не начинает Stage 3B, не открывает
--   fulfilment и не переоткрывает uuid36-популяцию 2025 года.
-- ============================================================================


-- ────────────────────────────────────────────────────────────────────────────
-- 1/3 · V_WB_DEDUCTIONS_CLASSIFIED — грейн finance_row_key.
--   Прямая метка WB главенствует; эвристика формы — только fallback.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
OPTIONS (description = 'Stage 3.1F. Classification of WB account-level "Удержание" operations, grain finance_row_key. PRIORITY: direct WB label (bonus_type_name) wins over srid shape heuristic; a non-empty but unrecognized label yields UNKNOWN_SEMANTIC and NEVER falls back to shape. Classes: AD_BILLING, TRANSIT_DEDUCTION, FORCE_MAJEURE_PAYMENT, REVIEW_POINTS_ADVANCE_REFUND, MINIMUM_PAYMENT_ADJUSTMENT, UTILIZATION, UNKNOWN_SEMANTIC, UNCLASSIFIED_DEDUCTION, CLASSIFICATION_CONFLICT. deduction_class_source states the evidence. WB populates bonus_type_name from 2026-07-19; earlier rows keep the legacy shape-based treatment unchanged. Contains no nm_id: account-level deductions are not SKU costs.')
AS
WITH
-- srid берётся из RAW ровно как в Stage 3.1C PR1 — источник не меняется,
-- чтобы поведение fallback осталось побитово прежним.
srid_map AS (
  SELECT
    CONCAT(report_id, '#', rrd_id)         AS row_key,
    ANY_VALUE(NULLIF(TRIM(srid), ''))      AS srid
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_FINANCE`
  WHERE supplier_oper_name = 'Удержание'
  GROUP BY row_key
),
-- Прямое основание платежа. Разбор JSON живёт в canonical-слое, здесь он не
-- дублируется: читается уже готовое поле V_WB_FINANCE_SEMANTIC.bonus_type_name.
btn_map AS (
  SELECT
    CONCAT(report_id, '#', rrd_id)         AS row_key,
    ANY_VALUE(bonus_type_name)             AS bonus_type_name
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
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
    t.bonus_type_name,
    IFNULL(f.deduction, NUMERIC '0')                                    AS deduction_rub,
    IFNULL(f.additional_payment, NUMERIC '0')                           AS additional_payment_rub,
    IFNULL(f.deduction, NUMERIC '0') + IFNULL(f.additional_payment, NUMERIC '0') AS deduction_amount_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` f
  LEFT JOIN srid_map s ON s.row_key = f.finance_row_key
  LEFT JOIN btn_map  t ON t.row_key = f.finance_row_key
  WHERE f.supplier_oper_name = 'Удержание'
),
-- Признаки эвристики вычисляются независимо, чтобы их одновременное
-- срабатывание было ОБНАРУЖИМО, а не поглощено порядком CASE.
matched AS (
  SELECT b.*,
    -- Сопоставление регистро- и пробелонезависимое, но НЕ расширенное:
    -- ни номера документов, ни отдельные слова основанием класса не служат.
    REGEXP_REPLACE(UPPER(IFNULL(b.bonus_type_name, '')), r'\s+', ' ')   AS btn_norm,
    REGEXP_CONTAINS(IFNULL(b.srid, ''), r'^transit_deduction_[0-9]+$')  AS rule_transit,
    REGEXP_CONTAINS(IFNULL(b.srid, ''), r'^[0-9a-f]{40}$')              AS rule_hex40
  FROM base b
),
classified AS (
  SELECT m.*,
    CASE
      -- ── Приоритет 1-2: прямая метка WB. Fallback к форме ЗАПРЕЩЁН. ──
      WHEN m.bonus_type_name IS NOT NULL THEN
        CASE
          WHEN m.btn_norm LIKE '%WB ПРОДВИЖЕНИЕ%'         THEN 'AD_BILLING'
          WHEN m.btn_norm LIKE '%ТРАНЗИТНЫХ ПОСТАВОК%'    THEN 'TRANSIT_DEDUCTION'
          WHEN m.btn_norm LIKE '%НЕПРЕОДОЛИМОЙ СИЛЫ%'     THEN 'FORCE_MAJEURE_PAYMENT'
          WHEN m.btn_norm LIKE '%БАЛЛЫ ЗА ОТЗЫВЫ%'        THEN 'REVIEW_POINTS_ADVANCE_REFUND'
          WHEN m.btn_norm LIKE '%МИНИМАЛЬНОМУ ПЛАТЕЖУ%'   THEN 'MINIMUM_PAYMENT_ADJUSTMENT'
          WHEN m.btn_norm LIKE '%УТИЛИЗИРОВАННОМ ТОВАРЕ%' THEN 'UTILIZATION'
          ELSE                                                 'UNKNOWN_SEMANTIC'
        END
      -- ── Приоритет 3: прежняя эвристика, побитово как в Stage 3.1C PR1. ──
      WHEN m.rule_transit AND m.rule_hex40                  THEN 'CLASSIFICATION_CONFLICT'
      WHEN m.rule_transit                                   THEN 'TRANSIT_DEDUCTION'
      WHEN m.rule_hex40                                     THEN 'AD_BILLING'
      ELSE                                                       'UNCLASSIFIED_DEDUCTION'
    END AS deduction_class,
    CASE
      WHEN m.bonus_type_name IS NOT NULL THEN
        CASE
          WHEN m.btn_norm LIKE '%WB ПРОДВИЖЕНИЕ%'
            OR m.btn_norm LIKE '%ТРАНЗИТНЫХ ПОСТАВОК%'
            OR m.btn_norm LIKE '%НЕПРЕОДОЛИМОЙ СИЛЫ%'
            OR m.btn_norm LIKE '%БАЛЛЫ ЗА ОТЗЫВЫ%'
            OR m.btn_norm LIKE '%МИНИМАЛЬНОМУ ПЛАТЕЖУ%'
            OR m.btn_norm LIKE '%УТИЛИЗИРОВАННОМ ТОВАРЕ%' THEN 'DIRECT_WB_LABEL'
          ELSE                                                 'UNKNOWN_DIRECT_LABEL'
        END
      WHEN m.rule_transit AND m.rule_hex40                  THEN 'LEGACY_MULTI_RULE'
      WHEN m.rule_transit                                   THEN 'LEGACY_TRANSIT_RULE'
      WHEN m.rule_hex40                                     THEN 'LEGACY_SHAPE_HEURISTIC'
      ELSE                                                       'LEGACY_NO_RULE'
    END AS deduction_class_source
  FROM matched m
)
SELECT
  c.finance_row_key,
  c.finance_date,
  c.supplier_oper_name,
  c.srid,
  c.bonus_type_name,
  c.deduction_rub,
  c.additional_payment_rub,
  c.deduction_amount_rub,
  c.deduction_class,
  c.deduction_class_source,
  -- Уверенность отражает ПРИРОДУ СВИДЕТЕЛЬСТВА, а не только имя класса:
  -- прямая метка WB и реконструкция по форме srid не равнозначны.
  CASE c.deduction_class_source
    WHEN 'DIRECT_WB_LABEL'        THEN 'CONFIRMED_WB_LABEL'
    WHEN 'UNKNOWN_DIRECT_LABEL'   THEN 'UNKNOWN'
    WHEN 'LEGACY_TRANSIT_RULE'    THEN 'CONFIRMED_SYSTEM_PATTERN'
    WHEN 'LEGACY_SHAPE_HEURISTIC' THEN 'STRONG_RECONSTRUCTION'
    WHEN 'LEGACY_MULTI_RULE'      THEN 'CONFLICT'
    ELSE                               'UNKNOWN'
  END                                                                   AS deduction_class_confidence,
  CASE c.deduction_class_source
    WHEN 'DIRECT_WB_LABEL'        THEN 'BONUS_TYPE_NAME_DIRECT'
    WHEN 'UNKNOWN_DIRECT_LABEL'   THEN 'BONUS_TYPE_NAME_UNRECOGNIZED'
    WHEN 'LEGACY_TRANSIT_RULE'    THEN 'SRID_TRANSIT_PATTERN'
    WHEN 'LEGACY_SHAPE_HEURISTIC' THEN 'HEX40_STRONG_RECONSTRUCTION'
    WHEN 'LEGACY_MULTI_RULE'      THEN 'MULTI_RULE_MATCH'
    ELSE                               'NO_RULE_MATCH'
  END                                                                   AS classification_rule,
  (c.deduction_class = 'AD_BILLING')                                    AS is_ad_billing_reconstructed,
  (c.deduction_class = 'TRANSIT_DEDUCTION')                             AS is_transit_deduction,
  (c.deduction_class = 'UNCLASSIFIED_DEDUCTION')                        AS is_unclassified,
  (c.deduction_class = 'FORCE_MAJEURE_PAYMENT')                         AS is_force_majeure,
  (c.deduction_class = 'UNKNOWN_SEMANTIC')                              AS is_unknown_semantic,
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
-- 2/3 · V_ADVERTISING_RECONCILIATION_DAILY — грейн day.
--   Добавлены суточные суммы новых прямых категорий. Прежние поля и их
--   семантика сохранены: ни одно существующее имя не переиспользовано.
--
-- 🔴 ЭТА ВЬЮ НИЧЕГО НЕ ВЫЧИТАЕТ. Она только измеряет. Исключение из
--   операционного результата выполняет слой 3/3.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_ADVERTISING_RECONCILIATION_DAILY`
OPTIONS (description = 'Stage 3.1F. Daily reconciliation of advertising billed vs attributed, plus the full deduction structure by direct WB label. New daily buckets: force_majeure_rub (raw sign, negative = money to seller), utilization_rub, review_points_refund_rub, minimum_payment_adjustment_rub, unknown_semantic_rub. Outside the attribution coverage window attributed and unallocated are NULL, not zero. Unallocated keeps its sign - it is a reconciliation difference, not a cost bucket. This view subtracts nothing.')
AS
WITH
ded_day AS (
  SELECT
    finance_date                                                                            AS day,
    SUM(IF(deduction_class = 'AD_BILLING',                   deduction_amount_rub, NUMERIC '0')) AS ad_spend_billed_rub,
    SUM(IF(deduction_class = 'TRANSIT_DEDUCTION',            deduction_amount_rub, NUMERIC '0')) AS transit_deduction_rub,
    SUM(IF(deduction_class = 'UNCLASSIFIED_DEDUCTION',       deduction_amount_rub, NUMERIC '0')) AS unclassified_deduction_rub,
    SUM(IF(deduction_class = 'CLASSIFICATION_CONFLICT',      deduction_amount_rub, NUMERIC '0')) AS conflict_deduction_rub,
    -- Новые прямые категории Stage 3.1F.
    SUM(IF(deduction_class = 'FORCE_MAJEURE_PAYMENT',        deduction_amount_rub, NUMERIC '0')) AS force_majeure_rub,
    SUM(IF(deduction_class = 'UTILIZATION',                  deduction_amount_rub, NUMERIC '0')) AS utilization_rub,
    SUM(IF(deduction_class = 'REVIEW_POINTS_ADVANCE_REFUND', deduction_amount_rub, NUMERIC '0')) AS review_points_refund_rub,
    SUM(IF(deduction_class = 'MINIMUM_PAYMENT_ADJUSTMENT',   deduction_amount_rub, NUMERIC '0')) AS minimum_payment_adjustment_rub,
    SUM(IF(deduction_class = 'UNKNOWN_SEMANTIC',             deduction_amount_rub, NUMERIC '0')) AS unknown_semantic_rub,
    SUM(deduction_amount_rub)                                                               AS total_deduction_rub,
    COUNT(*)                                                                                AS deduction_rows,
    COUNTIF(deduction_class = 'AD_BILLING')                                                 AS ad_billing_rows,
    COUNTIF(deduction_class = 'CLASSIFICATION_CONFLICT')                                    AS conflict_rows,
    COUNTIF(deduction_class = 'UNKNOWN_SEMANTIC')                                           AS unknown_semantic_rows,
    COUNTIF(deduction_class_source = 'DIRECT_WB_LABEL')                                     AS direct_label_rows
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
  -- Уверенность рекламного класса теперь различает прямую метку и реконструкцию.
  CASE
    WHEN IFNULL(s.conflict_rows, 0) > 0                             THEN 'CONFLICT'
    WHEN IFNULL(s.ad_billing_rows, 0) = 0                           THEN 'NOT_APPLICABLE'
    WHEN IFNULL(s.direct_label_rows, 0) >= IFNULL(s.deduction_rows, 0) THEN 'CONFIRMED_WB_LABEL'
    ELSE                                                                 'STRONG_RECONSTRUCTION'
  END                                                                        AS ad_billing_classification_confidence,
  IFNULL(s.transit_deduction_rub,          NUMERIC '0')                      AS transit_deduction_rub,
  IFNULL(s.unclassified_deduction_rub,     NUMERIC '0')                      AS unclassified_deduction_rub,
  IFNULL(s.conflict_deduction_rub,         NUMERIC '0')                      AS conflict_deduction_rub,
  -- ── Прямые категории Stage 3.1F. Знак сырой, как в отчёте WB. ──
  IFNULL(s.force_majeure_rub,              NUMERIC '0')                      AS force_majeure_rub,
  IFNULL(s.utilization_rub,                NUMERIC '0')                      AS utilization_rub,
  IFNULL(s.review_points_refund_rub,       NUMERIC '0')                      AS review_points_refund_rub,
  IFNULL(s.minimum_payment_adjustment_rub, NUMERIC '0')                      AS minimum_payment_adjustment_rub,
  IFNULL(s.unknown_semantic_rub,           NUMERIC '0')                      AS unknown_semantic_rub,
  -- Классифицированное = всё, чему WB или правило дали имя. Конфликт и
  -- UNKNOWN_SEMANTIC классифицированными НЕ считаются.
  IFNULL(s.ad_spend_billed_rub, NUMERIC '0') + IFNULL(s.transit_deduction_rub, NUMERIC '0')
    + IFNULL(s.force_majeure_rub, NUMERIC '0') + IFNULL(s.utilization_rub, NUMERIC '0')
    + IFNULL(s.review_points_refund_rub, NUMERIC '0')
    + IFNULL(s.minimum_payment_adjustment_rub, NUMERIC '0')                  AS classified_deduction_rub,
  IFNULL(s.total_deduction_rub, NUMERIC '0')                                 AS total_deduction_rub,
  IFNULL(s.deduction_rows, 0)                                                AS deduction_rows,
  IFNULL(s.unknown_semantic_rows, 0)                                         AS unknown_semantic_rows,
  IFNULL(s.direct_label_rows, 0)                                             AS direct_label_rows,
  CURRENT_TIMESTAMP()                                                        AS generated_at
FROM spine s
CROSS JOIN ads_bounds b;


-- ────────────────────────────────────────────────────────────────────────────
-- 3/3 · V_DASH_FINANCE_CORRECTED_DAILY — грейн day, overlay 1:1 к KPI-слою.
--   Пересобран поверх новых категорий. Структура и имена полей Stage 3.1C PR2
--   сохранены; добавлены прямые категории и исключительная компенсация.
--
-- 🔴 OPTION B (owner ACK). Из операционного итога уровня кабинета исключаются
--   ДВЕ величины и только они:
--     • ad_billing_reconstructed_rub — реклама уже вычтена как attributed
--       внутри contribution_pre_cogs_rub, второй раз вычитать нельзя;
--     • force_majeure_rub — исключительная выплата, вне операционного
--       результата по решению владельца.
--   Утилизация, возврат аванса «Баллы за отзывы», корректировка минимального
--   платежа, транзит и всё неопознанное ОСТАЮТСЯ операционными.
--
-- 🔴 ЗНАК. force_majeure_rub отрицателен (деньги продавцу). Вычитание
--   отрицательной величины возвращает её в расходы, то есть НЕ улучшает
--   операционный результат — это и есть OPTION B. Наружу выдаётся ещё и
--   exceptional_compensation_rub = −force_majeure_rub, положительная величина
--   для человеческого чтения. Инверсия выполняется здесь один раз.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`
OPTIONS (description = 'Stage 3.1C PR2 + Stage 3.1F. Executive finance overlay, grain day, 1:1 with V_DASH_KPI_DAILY. Advertising billing is excluded from account-level costs because advertising is already deducted as attributed inside contribution_pre_cogs_rub. Force-majeure compensation is excluded from the OPERATING result by owner decision OPTION B and published separately as exceptional_compensation_rub (positive = benefit to seller). Utilization, review-points refund, minimum-payment adjustment, transit and unknown remain operating. No ratio columns: consumers compute ratio-of-sums.')
AS
WITH
kpi AS (
  SELECT
    day,
    sales_covered, ads_covered, finance_covered, contribution_covered,
    finance_is_final, contains_provisional_finance, is_current_day,
    sales_revenue_seller_base_rub,
    ad_spend_attributed_rub,
    storage_rub, deduction_rub, acceptance_rub,
    penalty_account_rub, reimbursement_account_rub, other_account_rub,
    account_level_total_rub,
    contribution_pre_cogs_rub,
    period_result_pre_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
),
-- Разложение удержаний. Маска — deduction_rub: разложение обязано существовать
-- ровно там, где существует его итог, и отсутствовать там, где итог неизвестен.
ded AS (
  SELECT
    k.day,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.ad_spend_billed_rub,             NUMERIC '0')) AS ad_billing_reconstructed_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.transit_deduction_rub,           NUMERIC '0')) AS transit_deduction_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.unclassified_deduction_rub,      NUMERIC '0')) AS unclassified_deduction_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.conflict_deduction_rub,          NUMERIC '0')) AS classification_conflict_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.force_majeure_rub,               NUMERIC '0')) AS force_majeure_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.utilization_rub,                 NUMERIC '0')) AS utilization_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.review_points_refund_rub,        NUMERIC '0')) AS review_points_refund_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.minimum_payment_adjustment_rub,  NUMERIC '0')) AS minimum_payment_adjustment_rub,
    IF(k.deduction_rub IS NULL, NULL, IFNULL(r.unknown_semantic_rub,            NUMERIC '0')) AS unknown_semantic_rub,
    r.total_deduction_rub                                                                     AS classifier_total_deduction_rub,
    r.deduction_rows,
    r.unknown_semantic_rows,
    r.direct_label_rows,
    r.ads_attribution_covered,
    r.ad_spend_billed_rub,
    r.ad_spend_unallocated_rub,
    r.ad_spend_attributed_rub AS recon_ad_spend_attributed_rub,
    r.ad_billing_classification_confidence
  FROM kpi k
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_ADVERTISING_RECONCILIATION_DAILY` r ON r.day = k.day
)

SELECT
  k.day,

  -- ── Гейты покрытия: PASS-THROUGH. Ничего не пересчитывается. ──
  k.sales_covered,
  k.ads_covered,
  k.finance_covered,
  k.contribution_covered,
  k.finance_is_final,
  k.contains_provisional_finance,
  k.is_current_day,

  -- 🔴 ЕДИНЫЙ ELIGIBLE-DAY UNIVERSE. Числитель и знаменатель процента обязаны
  --    жить на одном множестве суток.
  (k.contribution_covered AND k.finance_covered)          AS period_result_eligible,
  k.contribution_covered                                  AS contribution_eligible,
  IF(k.contribution_covered AND k.finance_covered, 1, 0)  AS period_result_eligible_day,
  IF(k.contribution_covered, 1, 0)                        AS contribution_eligible_day,

  -- ── ВЫРУЧКА: сырая и выровненные знаменатели ──
  k.sales_revenue_seller_base_rub,
  IF(k.contribution_covered AND k.finance_covered, k.sales_revenue_seller_base_rub, NULL)
                                                          AS revenue_base_period_result_rub,
  IF(k.contribution_covered, k.sales_revenue_seller_base_rub, NULL)
                                                          AS revenue_base_contribution_rub,

  -- ── РАСХОДЫ УРОВНЯ СЧЁТА: BEFORE, без единого изменения ──
  k.storage_rub,
  k.deduction_rub,
  k.acceptance_rub,
  k.penalty_account_rub,
  k.reimbursement_account_rub,
  k.other_account_rub,
  k.account_level_total_rub,

  -- ── РАЗЛОЖЕНИЕ УДЕРЖАНИЙ ПО ПРЯМЫМ МЕТКАМ WB (Stage 3.1F) ──
  -- 🔴 deduction_rub НЕ уменьшается и с экрана не исчезает: он остаётся
  --    полным итогом операции WB. Исключения работают только в расчёте.
  d.ad_billing_reconstructed_rub,
  d.transit_deduction_rub,
  d.unclassified_deduction_rub,
  d.classification_conflict_rub,
  d.force_majeure_rub,
  d.utilization_rub,
  d.review_points_refund_rub,
  d.minimum_payment_adjustment_rub,
  d.unknown_semantic_rub,
  -- Исключительная компенсация для человеческого чтения: положительная = выгода.
  (-d.force_majeure_rub)                                  AS exceptional_compensation_rub,
  -- Операционные удержания, остающиеся в результате (без рекламы и форс-мажора).
  ( d.transit_deduction_rub
  + d.unclassified_deduction_rub
  + d.classification_conflict_rub
  + d.utilization_rub
  + d.review_points_refund_rub
  + d.minimum_payment_adjustment_rub
  + d.unknown_semantic_rub )                              AS other_wb_deductions_rub,
  d.deduction_rows,
  d.unknown_semantic_rows,
  d.direct_label_rows,
  d.ad_billing_classification_confidence,
  -- Fail-closed индикатор: части обязаны складываться в целое.
  (d.ad_billing_reconstructed_rub
 + d.transit_deduction_rub
 + d.unclassified_deduction_rub
 + d.classification_conflict_rub
 + d.force_majeure_rub
 + d.utilization_rub
 + d.review_points_refund_rub
 + d.minimum_payment_adjustment_rub
 + d.unknown_semantic_rub = k.deduction_rub)              AS deduction_decomposition_complete,

  -- ── ОПЕРАЦИОННЫЙ УРОВЕНЬ СЧЁТА (OPTION B) ──
  -- Вычитание, а не перечисление. NULL распространяется естественно.
  (k.account_level_total_rub - d.ad_billing_reconstructed_rub - d.force_majeure_rub)
                                                          AS account_level_total_corrected_rub,

  -- ── ЭКОНОМИКА ──
  k.contribution_pre_cogs_rub,
  k.period_result_pre_cogs_rub,
  -- 🔴 Гейт повторяет действующую формулу V_DASH_KPI_DAILY символ в символ.
  IF(k.contribution_covered AND k.finance_covered,
     k.contribution_pre_cogs_rub
       - (k.account_level_total_rub - d.ad_billing_reconstructed_rub - d.force_majeure_rub),
     NULL)                                                AS period_result_pre_cogs_corrected_rub,
  -- Исключительная компенсация в разрезе того же гейта — для отдельной строки
  -- экрана. В операционный результат выше она НЕ входит.
  IF(k.contribution_covered AND k.finance_covered, -d.force_majeure_rub, NULL)
                                                          AS exceptional_compensation_eligible_rub,

  -- ── РЕКЛАМНАЯ СВЕРКА ──
  -- 🔴 attributed НЕ подменяется billed. billed — контрольная величина
  --    уровня счёта, unallocated — разрыв сверки, а НЕ статья затрат.
  k.ad_spend_attributed_rub,
  d.ad_spend_billed_rub,
  d.ad_spend_unallocated_rub,
  d.ads_attribution_covered,

  -- ── Диагностика сверки (не метрики экрана) ──
  d.classifier_total_deduction_rub,
  d.recon_ad_spend_attributed_rub,

  'PRE_COGS_AD_BILLING_CORRECTED_EXCL_EXCEPTIONAL'        AS economics_basis,
  'Результат после SKU-level и account-level расходов, но ДО себестоимости товара, FF, OPEX и налога. Не прибыль. Рекламный биллинг исключён, потому что реклама уже вычтена как attributed внутри contribution_pre_cogs_rub. Форс-мажорные компенсации исключены из ОПЕРАЦИОННОГО результата (owner OPTION B) и публикуются отдельно как exceptional_compensation_rub.'
                                                          AS economics_note,
  CURRENT_TIMESTAMP()                                     AS generated_at

FROM kpi k
LEFT JOIN ded d USING (day);
