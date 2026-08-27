-- ============================================================================
-- STAGE 3.1B PR1 — PRODUCT COGS CONSUMER LAYER (маршрут R3)
-- Дата: 2026-08-27.  База: HEAD cd166e95a2269a1353a701a94cc5364219cd07b8.
-- Контракт: docs/STAGE3_1B_COGS_CONSUMER_2026-08-27.md
-- Предшественник: Stage 3.1A (evetis_ref: 17 / 33 / 21 / 38) — НЕ изменяется.
-- Откат: tools/stage3_1b_cogs_consumer_rollback.sh [--dry-run]
--
-- ЧТО ЭТО. Два consumer-объекта, подключающие marketplace-independent слой
--   себестоимости evetis_ref к WB-контуру. Оба — VIEW, ни один существующий
--   объект не изменяется.
--
-- ЧЕГО ЭТОТ PR НЕ ДЕЛАЕТ (owner ACK, gate 27.08):
--   1. Не изменяет FACT_FINANCE, FACT_SALES, MART_SKU_DAILY, ни одной V_DASH_*,
--      ни одной процедуры (sp_bootstrap_facts, sp_build_mart_sku_daily).
--   2. Не пересобирает FACT/MART. Суточная дельта источников искусственно
--      не поглощается: её заберёт штатный прогон wb-mart-prod.
--   3. Не подключается к Metabase. Карточки 40-71 читают только V_DASH_* и не
--      видят этих вью.
--   4. Не начинает Stage 3B и не трогает FACT_ADS_SPEND_ALLOC_DAILY.
--   5. Не переоткрывает Stage 3.0.3 / 3.0.3B / 3.1A: значения COGS, границы
--      партий и bundle-формулы берутся из evetis_ref как есть.
--
-- 🔴 МАРШРУТ R3 И ПОЧЕМУ srid НЕ ПРОНЕСЁН В FACT.
--   sql/mart/pr_mart1_facts.sql в репозитории содержит §1.7 (Stage 3B:
--   FACT_ADS_SPEND_ALLOC_DAILY / FACT_ADS_SPEND_UNALLOC_DAILY), которого НЕТ
--   в развёрнутой процедуре sp_bootstrap_facts. Публикация этого файла ради
--   одной колонки srid создала бы два объекта Stage 3B. Поэтому srid берётся
--   из canonical-слоя на consumer-границе.
--   Расхождение зарегистрировано как technical debt:
--   docs/TECHDEBT_FACT_DEPLOY_DRIFT_2026-08-27.md
--
-- 🔴 СЕМАНТИКА ВОЗВРАТА — OD-1 = B (owner ACK).
--   Возврат сторнирует Product COGS ИСХОДНОЙ ПРОДАЖИ, найденной по srid.
--   Если исходная продажа не разрешается ровно в одну — COGS возврата NULL
--   (fail-closed). Никогда 0, никогда «COGS на дату возврата».
--
-- 🔴 MAPPING КАНАЛ -> SKU — только актуальный REF_SKU_MASTER по nm_id.
--   Сохранённые FACT_*.internal_sku / sku_match_status НЕ используются:
--   аудит 27.08 показал 8 строк FACT_SALES с internal_sku IS NULL при живом
--   маппинге (nm_id 1083392113). Правка самих FACT-колонок — отдельный DQ-тикет.
--
-- 🔴 PRODUCT COGS != FULFILMENT COST != TAX.
--   Хранение ФФ, сборка, разборка, маркировка, отгрузка ФФ, доставка
--   ФФ -> склад маркетплейса, расходы кабинета WB, ЗП, аренда, банк и налог
--   в эти метрики НЕ входят и войти не могут.
-- ============================================================================


-- ────────────────────────────────────────────────────────────────────────────
-- 1/2 · V_FACT_FINANCE_COGS — грейн finance_row_key (событие реализации).
--
--   Универсум: FACT_FINANCE WHERE supplier_oper_name IN ('Продажа','Возврат').
--   Единица: 1 строка = 1 ед. Это свойство источника (quantity ≡ 1 во всех
--   COGS-требующих строках RAW_WB_FINANCE); проверяется ASSERT S-0 в
--   sql/mart/pr_cogs_consumer_validation.sql, а не предполагается.
--
--   srid берётся из V_WB_FINANCE_SEMANTIC — canonical-надмножества снимка
--   FACT_FINANCE. Надмножество безопасно: продажа всегда старше своего
--   возврата, поэтому оно только расширяет разрешимость и не может создать
--   дубликат строки — уникальность srid среди продаж гарантирует ASSERT S-2.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
OPTIONS (description = 'Stage 3.1B PR1. Product COGS resolved at finance-event grain (finance_row_key). Universe = FACT_FINANCE Продажа/Возврат. Return reversal uses the ORIGINAL SALE cost resolved via srid (OD-1=B), fail-closed on missing/ambiguous linkage. SKU mapping via REF_SKU_MASTER by nm_id only. Product COGS excludes fulfilment operations, WB account costs, OPEX and tax.')
AS
WITH
-- Canonical-проекция: только COGS-требующие операции, только ключ, srid и дата.
src AS (
  SELECT
    CONCAT(report_id, '#', rrd_id) AS row_key,
    supplier_oper_name             AS op,
    NULLIF(TRIM(srid), '')         AS srid,
    _rr_date                       AS rr_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
  WHERE supplier_oper_name IN ('Продажа', 'Возврат')
),
-- srid собственного события. GROUP BY, а не прямой join: одна строка на ключ
-- гарантируется здесь, а не предполагается по контракту соседнего слоя.
srid_self AS (
  SELECT row_key, ANY_VALUE(srid) AS srid
  FROM src GROUP BY row_key
),
-- Продажа по srid. sale_match_count — материал для fail-closed решения.
sale_by_srid AS (
  SELECT
    srid,
    COUNT(*)           AS sale_match_count,
    ANY_VALUE(row_key) AS sale_row_key,
    MIN(rr_date)       AS sale_date
  FROM src
  WHERE op = 'Продажа' AND srid IS NOT NULL
  GROUP BY srid
),
ev AS (
  SELECT
    f.finance_row_key,
    f.finance_date,
    f.nm_id,
    m.internal_sku,
    m.is_bundle,
    f.supplier_oper_name,
    f.finance_status,
    s.srid,
    IF(f.supplier_oper_name = 'Продажа', 1, -1) AS signed_units
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_FINANCE` f
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER` m
         ON m.nm_id = f.nm_id
  LEFT JOIN srid_self s
         ON s.row_key = f.finance_row_key
  WHERE f.supplier_oper_name IN ('Продажа', 'Возврат')
),
linked AS (
  SELECT
    e.finance_row_key, e.finance_date, e.nm_id, e.internal_sku, e.is_bundle,
    e.supplier_oper_name, e.finance_status, e.srid, e.signed_units,
    IF(e.supplier_oper_name = 'Возврат', l.sale_row_key, NULL)         AS linked_sale_row_key,
    IF(e.supplier_oper_name = 'Возврат', l.sale_date,    NULL)         AS linked_sale_date,
    IF(e.supplier_oper_name = 'Возврат', IFNULL(l.sale_match_count, 0), NULL) AS linked_sale_match_count
  FROM ev e
  LEFT JOIN sale_by_srid l
         ON l.srid = e.srid
        AND e.supplier_oper_name = 'Возврат'
),
-- OD-1 = B: продажа разрешается по своей дате, возврат — по дате ИСХОДНОЙ
-- продажи. Неоднозначная или отсутствующая связка -> NULL, а не подстановка.
dated AS (
  SELECT
    finance_row_key, finance_date, nm_id, internal_sku, is_bundle,
    supplier_oper_name, finance_status, srid, signed_units,
    linked_sale_row_key, linked_sale_date, linked_sale_match_count,
    CASE
      WHEN supplier_oper_name = 'Продажа'  THEN finance_date
      WHEN linked_sale_match_count = 1     THEN linked_sale_date
      ELSE NULL
    END AS cogs_effective_date
  FROM linked
),
-- Temporal join к unified resolver. COUNT — материал инварианта matches <= 1.
resolved AS (
  SELECT
    d.finance_row_key, d.finance_date, d.nm_id, d.internal_sku, d.is_bundle,
    d.supplier_oper_name, d.finance_status, d.srid, d.signed_units,
    d.linked_sale_row_key, d.linked_sale_date, d.linked_sale_match_count,
    d.cogs_effective_date,
    COUNT(v.internal_sku)         AS cogs_match_count,
    ANY_VALUE(v.product_cogs_rub) AS unit_cogs_raw
  FROM dated d
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE` v
         ON v.internal_sku = d.internal_sku
        AND d.cogs_effective_date
            BETWEEN v.effective_from AND COALESCE(v.effective_to, DATE '9999-12-31')
  GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13
),
flagged AS (
  SELECT r.*,
    CASE
      WHEN r.internal_sku IS NULL                                              THEN 'UNMAPPED_SKU'
      WHEN r.supplier_oper_name = 'Возврат' AND r.linked_sale_match_count = 0   THEN 'RETURN_LINK_MISSING'
      WHEN r.supplier_oper_name = 'Возврат' AND r.linked_sale_match_count > 1   THEN 'RETURN_LINK_AMBIGUOUS'
      WHEN r.cogs_match_count > 1                                              THEN 'CONTRACT_VIOLATION_MULTI'
      WHEN r.cogs_match_count = 0                                              THEN 'UNKNOWN_NO_INTERVAL'
      ELSE 'RESOLVED'
    END AS cogs_resolution_status
  FROM resolved r
)
SELECT
  finance_row_key,
  finance_date,
  nm_id,
  internal_sku,
  is_bundle,
  supplier_oper_name,
  finance_status,
  srid,
  signed_units,
  linked_sale_row_key,
  linked_sale_date,
  linked_sale_match_count,
  cogs_effective_date,
  cogs_match_count,
  -- F-1: значение выдаётся ТОЛЬКО при ровно одном совпадении. Иначе NULL.
  IF(cogs_resolution_status = 'RESOLVED', unit_cogs_raw, NULL)                 AS unit_product_cogs_rub,
  IF(cogs_resolution_status = 'RESOLVED', signed_units * unit_cogs_raw, NULL)  AS product_cogs_signed_rub,
  cogs_resolution_status,
  IF(supplier_oper_name = 'Возврат', 'ORIGINAL_SALE', NULL)                    AS cogs_return_basis,
  CURRENT_TIMESTAMP()                                                          AS generated_at
FROM flagged;


-- ────────────────────────────────────────────────────────────────────────────
-- 2/2 · V_MART_SKU_DAILY_COGS — грейн day × nm_id, 1:1 с MART_SKU_DAILY.
--
--   ЭТО OVERLAY, А НЕ ЗАМЕНА ВИТРИНЫ. Все 60 колонок MART_SKU_DAILY здесь НЕ
--   дублируются: вью несёт ключи, потреблённые pre-COGS входы и новые
--   COGS-колонки. Соединяется с MART_SKU_DAILY / V_DASH_SKU_DAILY по (day, nm_id).
--
-- 🔴 ДВЕ СЕРИИ НЕ СМЕШИВАЮТСЯ (OD-2 = C).
--   operational — от выкупов FACT_SALES (окно источника с 2026-03-30);
--   settlement  — от событий FACT_FINANCE (полная история с 2024-09-07).
--   Каждая after-COGS метрика вычитает СВОЮ серию из СВОЕГО pre-COGS.
--
-- 🔴 РЕВЕРС OPERATIONAL СЧИТАЕТСЯ НА СОБЫТИЯХ, а не как returns_qty × unit_cogs(day).
--   При OD-1 = B два возврата одних суток могут ссылаться на продажи из разных
--   интервалов; дневной агрегат это выразить не способен.
--
-- 🔴 NOT_APPLICABLE != UNKNOWN. Сутки без единиц дают Product COGS = 0: это
--   отсутствие события, а не неизвестная величина. Неизвестность — только
--   UNMAPPED_SKU / UNKNOWN_NO_INTERVAL / RETURN_LINK_UNRESOLVED /
--   CONTRACT_VIOLATION_MULTI, и там все after-COGS метрики строго NULL.
--   Число событий выведено отдельными колонками, поэтому ноль доказуем.
--
--   Ratio-колонок здесь НЕТ — правило контракта дашборда v2: процент за период
--   считается потребителем как SUM(a)/SUM(b), а не усреднением посуточных.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`
OPTIONS (description = 'Stage 3.1B PR1. Product COGS overlay at MART grain (day x nm_id), 1:1 with MART_SKU_DAILY. Two separate series: operational (FACT_SALES buyouts) and settlement (FACT_FINANCE events). Join to MART_SKU_DAILY / V_DASH_SKU_DAILY by (day, nm_id). Contribution after Product COGS is NOT gross margin: marketplace fee, SKU logistics and attributed ads are already deducted before Product COGS. Fulfilment cost, OPEX and tax are NOT included.')
AS
WITH
map AS (
  SELECT nm_id, internal_sku, is_bundle
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
),
cogs AS (
  SELECT internal_sku, effective_from,
         COALESCE(effective_to, DATE '9999-12-31') AS effective_to,
         product_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
),
-- Canonical-проекция продаж/возвратов: srid для связки возврата с продажей.
sales_src AS (
  SELECT sale_id, NULLIF(TRIM(srid), '') AS srid, is_return, _sale_date AS sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`
),
sales_srid_self AS (
  SELECT sale_id, ANY_VALUE(srid) AS srid FROM sales_src GROUP BY sale_id
),
sales_sale_by_srid AS (
  SELECT srid, COUNT(*) AS sale_match_count, MIN(sale_date) AS sale_date
  FROM sales_src WHERE NOT is_return AND srid IS NOT NULL GROUP BY srid
),
ret_ev AS (
  SELECT
    fs.sale_id, fs.sale_date AS day, fs.nm_id, m.internal_sku,
    IFNULL(l.sale_match_count, 0) AS link_n,
    l.sale_date                   AS orig_sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES` fs
  LEFT JOIN map m                 ON m.nm_id   = fs.nm_id
  LEFT JOIN sales_srid_self s     ON s.sale_id = fs.sale_id
  LEFT JOIN sales_sale_by_srid l  ON l.srid    = s.srid
  WHERE fs.is_return
),
ret_res AS (
  SELECT
    r.sale_id, r.day, r.nm_id,
    ANY_VALUE(r.link_n)           AS link_n,
    COUNT(v.internal_sku)         AS mc,
    ANY_VALUE(v.product_cogs_rub) AS uc
  FROM ret_ev r
  LEFT JOIN cogs v
         ON v.internal_sku = r.internal_sku
        AND IF(r.link_n = 1, r.orig_sale_date, NULL)
            BETWEEN v.effective_from AND v.effective_to
  GROUP BY 1,2,3
),
ret_day AS (
  SELECT
    day, nm_id,
    COUNT(*)                                                            AS reversal_events,
    COUNTIF(link_n = 1 AND mc = 1)                                      AS reversal_resolved_events,
    IF(COUNTIF(link_n = 1 AND mc = 1) = COUNT(*), SUM(uc), NULL)        AS reversal_cogs_rub
  FROM ret_res GROUP BY day, nm_id
),
-- Settlement-серия: агрегат вью 1/2 по (finance_date, nm_id).
stl AS (
  SELECT
    finance_date AS day, nm_id,
    COUNT(*)                                                                                       AS settlement_events,
    IF(COUNTIF(cogs_resolution_status = 'RESOLVED') = COUNT(*), SUM(product_cogs_signed_rub), NULL) AS settlement_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
  GROUP BY finance_date, nm_id
),
base AS (
  SELECT
    d.day, d.nm_id, m.internal_sku, m.is_bundle,
    d.buyouts_qty, d.returns_qty, d.buyouts_rub, d.returns_rub,
    d.hybrid_day_contribution_pre_cogs, d.settlement_day_contribution_pre_cogs,
    d.build_as_of_date, d.built_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` d
  LEFT JOIN map m ON m.nm_id = d.nm_id
),
base_res AS (
  SELECT
    b.day, b.nm_id, b.internal_sku, b.is_bundle,
    b.buyouts_qty, b.returns_qty, b.buyouts_rub, b.returns_rub,
    b.hybrid_day_contribution_pre_cogs, b.settlement_day_contribution_pre_cogs,
    b.build_as_of_date, b.built_at,
    COUNT(v.internal_sku)         AS cogs_match_count,
    ANY_VALUE(v.product_cogs_rub) AS unit_cogs_raw
  FROM base b
  LEFT JOIN cogs v
         ON v.internal_sku = b.internal_sku
        AND b.day BETWEEN v.effective_from AND v.effective_to
  GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12
),
flagged AS (
  SELECT
    br.*,
    rd.reversal_events, rd.reversal_resolved_events, rd.reversal_cogs_rub,
    st.settlement_events, st.settlement_cogs_rub,
    CASE
      WHEN br.buyouts_qty = 0 AND br.returns_qty = 0            THEN 'NOT_APPLICABLE'
      WHEN br.internal_sku IS NULL                              THEN 'UNMAPPED_SKU'
      WHEN br.cogs_match_count > 1                              THEN 'CONTRACT_VIOLATION_MULTI'
      WHEN br.cogs_match_count = 0                              THEN 'UNKNOWN_NO_INTERVAL'
      WHEN br.returns_qty > 0 AND rd.reversal_cogs_rub IS NULL  THEN 'RETURN_LINK_UNRESOLVED'
      ELSE 'RESOLVED'
    END AS cogs_resolution_status
  FROM base_res br
  LEFT JOIN ret_day rd ON rd.day = br.day AND rd.nm_id = br.nm_id
  LEFT JOIN stl     st ON st.day = br.day AND st.nm_id = br.nm_id
)
SELECT
  -- ── Ключи ──
  day,
  nm_id,
  internal_sku,
  is_bundle,

  -- ── Потреблённые входы pre-COGS (pass-through витрины, значения не меняются) ──
  buyouts_qty,
  returns_qty,
  buyouts_rub,
  returns_rub,
  hybrid_day_contribution_pre_cogs,
  settlement_day_contribution_pre_cogs,

  -- ── Разрешение себестоимости ──
  cogs_match_count,
  IF(cogs_resolution_status = 'RESOLVED', unit_cogs_raw, NULL)          AS unit_product_cogs_rub,
  cogs_resolution_status,
  (cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'))            AS cogs_covered,
  IF(cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'), buyouts_qty + returns_qty, 0) AS cogs_covered_qty,
  IF(cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'), 0, buyouts_qty + returns_qty) AS cogs_uncovered_qty,

  -- ── OPERATIONAL: базис выкупов FACT_SALES ──
  CASE cogs_resolution_status
    WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
    WHEN 'RESOLVED'       THEN buyouts_qty * unit_cogs_raw
    ELSE NULL END                                                       AS product_cogs_operational_rub,
  CASE cogs_resolution_status
    WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
    WHEN 'RESOLVED'       THEN IFNULL(reversal_cogs_rub, NUMERIC '0')
    ELSE NULL END                                                       AS product_cogs_reversal_operational_rub,
  CASE cogs_resolution_status
    WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
    WHEN 'RESOLVED'       THEN buyouts_qty * unit_cogs_raw - IFNULL(reversal_cogs_rub, NUMERIC '0')
    ELSE NULL END                                                       AS net_product_cogs_operational_rub,
  IFNULL(reversal_events, 0)                                            AS reversal_events,
  IFNULL(reversal_resolved_events, 0)                                   AS reversal_resolved_events,

  -- ── SETTLEMENT: базис событий FACT_FINANCE ──
  IF(settlement_events IS NULL, NUMERIC '0', settlement_cogs_rub)       AS product_cogs_settlement_rub,
  IFNULL(settlement_events, 0)                                          AS settlement_cogs_event_count,
  (settlement_events IS NULL OR settlement_cogs_rub IS NOT NULL)        AS settlement_cogs_covered,

  -- ── AFTER-PRODUCT-COGS (OD-4). НЕ валовая маржа и НЕ прибыль ──
  IF(cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'),
     hybrid_day_contribution_pre_cogs
       - CASE cogs_resolution_status
           WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
           ELSE buyouts_qty * unit_cogs_raw - IFNULL(reversal_cogs_rub, NUMERIC '0')
         END,
     NULL)                                                              AS contribution_after_product_cogs_rub,
  IF(settlement_events IS NULL OR settlement_cogs_rub IS NOT NULL,
     settlement_day_contribution_pre_cogs
       - IF(settlement_events IS NULL, NUMERIC '0', settlement_cogs_rub),
     NULL)                                                              AS settlement_contribution_after_product_cogs_rub,

  -- ── Маркеры ──
  'AFTER_PRODUCT_COGS'                                                  AS economics_basis,
  'Product COGS = закупка + Китай + таможня + доставка ДО фулфилмента. НЕ включены: операции ФФ, расходы кабинета WB, OPEX, налог. Это НЕ валовая маржа и НЕ прибыль.'
                                                                        AS economics_note,
  build_as_of_date,
  built_at                                                              AS mart_built_at,
  CURRENT_TIMESTAMP()                                                   AS generated_at
FROM flagged;
