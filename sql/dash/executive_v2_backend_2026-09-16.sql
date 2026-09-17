-- ============================================================================
-- EXECUTIVE V2 — BACKEND GAPS (2026-09-16)
--
-- Закрывает backend-разрывы, найденные Phase A UI/UX-аудитом Executive V2.
-- Действующий контракт FIN CONTRACT V2 (sql/dash/fin_contract_v2_2026-09-16.sql)
-- НЕ меняется: ни один существующий итог не пересчитывается, все изменения
-- существующих view — только ДОБАВЛЕНИЕ колонок.
--
-- Документ:  docs/EXECUTIVE_V2_BACKEND_2026-09-16.md
-- Валидация: sql/dash/executive_v2_backend_validation.sql
-- Откат:     sql/rollback/executive_v2_backend_2026-09-16/
--
--   GAP-01  НДС на вознаграждение WB и вознаграждение ПВЗ — сырые поля отчёта.
--   GAP-02  разложение логистики по плечам (bonusTypeName).
--   GAP-03  когортный процент выкупа (order srid → исход).
--   GAP-04  разложение удержаний после реализации (settlement).
--   GAP-05  СПП явной колонкой.
--   STATUS  суточные флаги статуса данных Executive (FINAL/PROVISIONAL/INCOMPLETE).
--
-- Объекты:
--   NEW  wb_mart.V_WB_FINANCE_PRICE_COMPONENTS      грейн: строка финотчёта «Продажа/Возврат»
--   NEW  wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY   грейн: day, 1:1 к V_DASH_KPI_DAILY
--   NEW  wb_mart.V_DASH_BUYOUT_COHORT_DAILY         грейн: order_date (когорта)
--   ADD  wb_mart.V_DASH_SETTLEMENT_DAILY            + цепочка цены, + разложение удержаний
--   ADD  wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY   + статус данных Executive
-- ============================================================================

-- ── §1. V_WB_FINANCE_PRICE_COMPONENTS — ценовая цепочка строки финотчёта ────
--   Источник: wb_raw.V_WB_FINANCE_SEMANTIC (canonical-слой финотчётов WB).
--   Scope ровно тот же, что у marketplace_fee_gap_rub: «Продажа» и «Возврат».
--
--   ТОЖДЕСТВО WB (проверено построчно, WEEKLY/DAILY, 1 081 строка, |Δ| ≤ 0,01):
--     retailPriceWithDisc − forPay
--       = (retailPriceWithDisc − retailAmount)   СПП
--       + vw                                     вознаграждение WB без НДС
--       + vwNds                                  НДС на вознаграждение WB
--       + acquiringFee                           эквайринг / платёжные услуги
--       + ppvzReward                             вознаграждение ПВЗ
--   Остаток ≤ 0,01 ₽ на строку — округление: vw приходит с 18 знаками, прочие — 2.
--
--   🔴 LEGACY-слой (импорт до 13.07.2026) полей vwNds и ppvzReward НЕ СОДЕРЖИТ
--      (41 ключ JSON, проверено на 38 700 строках). Цепочка там НЕИЗВЕСТНА:
--      компоненты = NULL, price_chain_available = FALSE. Остатком НЕ вычисляются.
--   🔴 ЗНАК. Значения сырые, как в отчёте WB. Для «Возврат» WB отдаёт те же поля
--      с тем же знаком, что для продажи (1 строка 21.07.2026) — так же, как их
--      уже суммируют marketplace_fee_rub и settlement_goods_rub. Направление
--      операции публикуется колонкой supplier_oper_name и не переворачивается.
--   🔴 СПП построчно может быть ОТРИЦАТЕЛЬНОЙ: retailAmount > retailPriceWithDisc,
--      покупатель заплатил больше цены продавца. Замер 2026-09-16: 68 строк из
--      39 791 (WEEKLY 40 из 1 051, −1 312,96 ₽). Знак не подавляется: SUM нетто.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS` AS
WITH
universe AS (
  SELECT DISTINCT nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL
),
src AS (
  SELECT
    CONCAT(s.report_id, '#', s.rrd_id)                                  AS finance_row_key,
    s._rr_date                                                          AS finance_date,
    s.report_id,
    s.rrd_id,
    s.source_layer,
    s.finance_status,
    s.supplier_oper_name,
    NULLIF(TRIM(s.srid), '')                                            AS srid,
    SAFE_CAST(s.wb_nm_id AS INT64)                                      AS nm_id,
    s.sku_match_status,
    SAFE_CAST(REPLACE(s.retail_price_withdisc_rub, ',', '.') AS NUMERIC) AS seller_price_rub,
    SAFE_CAST(REPLACE(s.for_pay, ',', '.') AS NUMERIC)                  AS for_pay_rub,
    s.marketplace_fee_gap_rub,
    COALESCE(SAFE_CAST(JSON_VALUE(s.raw_json, '$.retailAmount')  AS NUMERIC),
             SAFE_CAST(JSON_VALUE(s.raw_json, '$.retail_amount') AS NUMERIC)) AS buyer_paid_rub,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.vw')           AS NUMERIC)      AS vw,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.vwNds')        AS NUMERIC)      AS vw_nds,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.acquiringFee') AS NUMERIC)      AS acquiring_fee,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.ppvzReward')   AS NUMERIC)      AS ppvz_reward
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC` s
  WHERE s.supplier_oper_name IN ('Продажа', 'Возврат')
),
flagged AS (
  SELECT
    src.*,
    (src.vw IS NOT NULL AND src.vw_nds IS NOT NULL AND src.acquiring_fee IS NOT NULL
     AND src.ppvz_reward IS NOT NULL AND src.buyer_paid_rub IS NOT NULL
     AND src.seller_price_rub IS NOT NULL AND src.for_pay_rub IS NOT NULL)  AS price_chain_available
  FROM src
)
SELECT
  f.finance_row_key,
  f.finance_date,
  f.report_id,
  f.rrd_id,
  f.source_layer,
  f.finance_status,
  f.supplier_oper_name,
  f.srid,
  f.nm_id,
  -- Тот же гейт SKU, что у MART_SKU_DAILY.marketplace_fee_rub (finpay CTE).
  COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)          AS is_sku_row,
  (COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)
   AND f.nm_id IN (SELECT nm_id FROM universe))                             AS in_mart_universe,
  f.price_chain_available,
  f.seller_price_rub,
  f.buyer_paid_rub,
  f.for_pay_rub,
  f.marketplace_fee_gap_rub,
  -- СПП: цена продавца минус сумма, оплаченная покупателем (retailAmount).
  (f.seller_price_rub - f.buyer_paid_rub)                                   AS spp_rub,
  IF(f.price_chain_available, f.vw,            NULL)                        AS wb_remuneration_rub,
  IF(f.price_chain_available, f.vw_nds,        NULL)                        AS wb_remuneration_vat_rub,
  IF(f.price_chain_available, f.acquiring_fee, NULL)                        AS acquiring_rub,
  IF(f.price_chain_available, f.ppvz_reward,   NULL)                        AS pvz_reward_rub,
  -- Остаток тождества. Ожидается |x| ≤ 0,01 на строку (округление vw).
  IF(f.price_chain_available,
     (f.seller_price_rub - f.for_pay_rub)
     - ((f.seller_price_rub - f.buyer_paid_rub) + f.vw + f.vw_nds + f.acquiring_fee + f.ppvz_reward),
     NULL)                                                                  AS price_chain_rounding_rub
FROM flagged f;

-- ── §2. V_DASH_EXECUTIVE_BREAKDOWN_DAILY — разложения управленческого слоя ──
--   Грейн day, 1:1 к V_DASH_KPI_DAILY. Каждое разложение живёт на маске
--   своего итога и сходится к нему (валидация EV2-B*).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY` AS
WITH
kpi AS (
  SELECT
    day, sales_covered, finance_covered,
    sales_revenue_seller_base_rub, sales_revenue_buyer_paid_rub,
    marketplace_fee_rub, logistics_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
),
universe AS (
  SELECT DISTINCT nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL
),
-- GAP-01/05: разложение «Удержано WB из цены» на той же популяции, что
-- marketplace_fee_rub (SKU-строки внутри universe витрины).
fee AS (
  SELECT
    finance_date                                   AS day,
    COUNT(*)                                       AS fee_rows,
    COUNTIF(NOT price_chain_available)             AS fee_rows_without_chain,
    SUM(marketplace_fee_gap_rub)                   AS fee_gap_rub,
    SUM(spp_rub)                                   AS spp_rub,
    SUM(wb_remuneration_rub)                       AS wb_remuneration_rub,
    SUM(wb_remuneration_vat_rub)                   AS wb_remuneration_vat_rub,
    SUM(acquiring_rub)                             AS acquiring_rub,
    SUM(pvz_reward_rub)                            AS pvz_reward_rub,
    SUM(price_chain_rounding_rub)                  AS rounding_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`
  WHERE in_mart_universe
  GROUP BY finance_date
),
-- GAP-02: плечи логистики. Итог — ровно logistics_rub витрины: LONG_MAPPED,
-- cost_category = 'logistics', SKU-строки внутри universe.
leg_label AS (
  SELECT CONCAT(report_id, '#', rrd_id) AS row_key, bonus_type_name
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC`
  WHERE supplier_oper_name IN ('Логистика', 'Доставка', 'Коррекция логистики')
),
legs AS (
  SELECT
    l.finance_date AS day,
    CASE
      WHEN l.op_key = 'Коррекция логистики'                  THEN 'CORRECTION'
      WHEN b.bonus_type_name IS NULL                         THEN 'UNLABELED'
      WHEN b.bonus_type_name = 'К клиенту при продаже'       THEN 'SOLD'
      WHEN b.bonus_type_name = 'К клиенту при отмене'        THEN 'CANCEL_TO_CUSTOMER'
      WHEN b.bonus_type_name = 'От клиента при отмене'       THEN 'CANCEL_FROM_CUSTOMER'
      WHEN b.bonus_type_name = 'От клиента при возврате'     THEN 'CUSTOMER_RETURN'
      WHEN b.bonus_type_name = 'Возврат брака (К продавцу)'  THEN 'DEFECT_RETURN_TO_SELLER'
      ELSE                                                        'OTHER_LABEL'
    END AS leg,
    l.cost_amount_positive AS amount_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` l
  LEFT JOIN leg_label b ON b.row_key = l.finance_row_key
  WHERE l.cost_category = 'logistics'
    AND l.is_sku_row
    AND l.cost_amount_positive IS NOT NULL
    AND l.nm_id IN (SELECT nm_id FROM universe)
),
legs_day AS (
  SELECT
    day,
    SUM(IF(leg = 'SOLD',                    amount_rub, 0)) AS sold,
    SUM(IF(leg = 'CANCEL_TO_CUSTOMER',      amount_rub, 0)) AS cancel_to,
    SUM(IF(leg = 'CANCEL_FROM_CUSTOMER',    amount_rub, 0)) AS cancel_from,
    SUM(IF(leg = 'CUSTOMER_RETURN',         amount_rub, 0)) AS customer_return,
    SUM(IF(leg = 'DEFECT_RETURN_TO_SELLER', amount_rub, 0)) AS defect_return,
    SUM(IF(leg = 'CORRECTION',              amount_rub, 0)) AS correction,
    SUM(IF(leg = 'UNLABELED',               amount_rub, 0)) AS unlabeled,
    SUM(IF(leg = 'OTHER_LABEL',             amount_rub, 0)) AS other_label,
    COUNTIF(leg = 'UNLABELED')                              AS unlabeled_rows,
    COUNTIF(leg = 'OTHER_LABEL')                            AS other_label_rows,
    SUM(amount_rub)                                         AS legs_total
  FROM legs
  GROUP BY day
)
SELECT
  k.day,
  k.sales_covered,
  k.finance_covered,

  -- ── GAP-05. СПП, канон Executive: база продавца минус оплачено покупателями.
  --    Та же популяция и дата (sale_date, Statistics API), что у двух карточек
  --    выручки → тождество с экраном точное.
  IF(k.sales_covered, k.sales_revenue_seller_base_rub - k.sales_revenue_buyer_paid_rub, NULL)
                                                                         AS spp_rub,

  -- ── GAP-01/05. Разложение marketplace_fee_rub (дата финотчёта) ──
  --    fee_components_covered: финансы покрыты И у каждой строки дня есть полная
  --    цепочка (LEGACY до 13.07.2026 — нет). Иначе компоненты NULL, не остаток.
  (k.finance_covered AND IFNULL(f.fee_rows_without_chain, 0) = 0)        AS fee_components_covered,
  IF(k.finance_covered AND IFNULL(f.fee_rows_without_chain, 0) = 0, IFNULL(f.spp_rub, 0), NULL)
                                                                         AS fee_spp_rub,
  IF(k.finance_covered AND IFNULL(f.fee_rows_without_chain, 0) = 0, IFNULL(f.wb_remuneration_rub, 0), NULL)
                                                                         AS wb_remuneration_rub,
  IF(k.finance_covered AND IFNULL(f.fee_rows_without_chain, 0) = 0, IFNULL(f.wb_remuneration_vat_rub, 0), NULL)
                                                                         AS wb_remuneration_vat_rub,
  IF(k.finance_covered AND IFNULL(f.fee_rows_without_chain, 0) = 0, IFNULL(f.acquiring_rub, 0), NULL)
                                                                         AS acquiring_rub,
  IF(k.finance_covered AND IFNULL(f.fee_rows_without_chain, 0) = 0, IFNULL(f.pvz_reward_rub, 0), NULL)
                                                                         AS pvz_reward_rub,
  IF(k.finance_covered AND IFNULL(f.fee_rows_without_chain, 0) = 0, IFNULL(f.rounding_rub, 0), NULL)
                                                                         AS fee_components_rounding_rub,
  k.marketplace_fee_rub,
  IFNULL(f.fee_rows, 0)                                                  AS fee_rows,
  IFNULL(f.fee_rows_without_chain, 0)                                    AS fee_rows_without_chain,

  -- ── GAP-02. Плечи логистики. Взаимоисключающие, сумма = logistics_rub. ──
  --    logistics_legs_covered: финансы покрыты И нет неразмеченных строк
  --    (LEGACY до 13.07.2026 плечо не передавал — UNLABELED).
  (k.finance_covered AND IFNULL(g.unlabeled_rows, 0) = 0)                AS logistics_legs_covered,
  IF(k.finance_covered, IFNULL(g.sold, 0),            NULL)              AS logistics_sold_rub,
  IF(k.finance_covered, IFNULL(g.cancel_to, 0) + IFNULL(g.cancel_from, 0), NULL)
                                                                         AS logistics_cancellation_rub,
  IF(k.finance_covered, IFNULL(g.cancel_to, 0),       NULL)              AS logistics_cancel_to_customer_rub,
  IF(k.finance_covered, IFNULL(g.cancel_from, 0),     NULL)              AS logistics_cancel_from_customer_rub,
  IF(k.finance_covered, IFNULL(g.customer_return, 0), NULL)              AS logistics_customer_return_rub,
  IF(k.finance_covered, IFNULL(g.defect_return, 0),   NULL)              AS logistics_defect_return_to_seller_rub,
  IF(k.finance_covered, IFNULL(g.correction, 0),      NULL)              AS logistics_correction_rub,
  IF(k.finance_covered, IFNULL(g.unlabeled, 0),       NULL)              AS logistics_unlabeled_rub,
  IF(k.finance_covered, IFNULL(g.other_label, 0),     NULL)              AS logistics_other_label_rub,
  k.logistics_rub,
  IFNULL(g.unlabeled_rows, 0)                                            AS logistics_unlabeled_rows,
  IFNULL(g.other_label_rows, 0)                                          AS logistics_other_label_rows,

  'EXECUTIVE_V2_BREAKDOWN'                                               AS economics_basis,
  'Разложения существующих итогов Executive. Ни одна колонка не является отдельным расходом сверх итога: fee_* и wb_remuneration*/acquiring/pvz_reward раскладывают marketplace_fee_rub, logistics_* раскладывают logistics_rub. spp_rub — СПП на дате продажи (Statistics API), fee_spp_rub — СПП внутри сбора на дате финотчёта; разница — копеечное округление источников.'
                                                                         AS economics_note,
  CURRENT_TIMESTAMP()                                                    AS generated_at
FROM kpi k
LEFT JOIN fee f      USING (day)
LEFT JOIN legs_day g USING (day);

-- ── §3. V_DASH_BUYOUT_COHORT_DAILY — когортный процент выкупа ────────────────
--   Когорта = order_date (FACT_ORDERS, сутки МСК). Связь заказ → исход по srid:
--     BUYOUT     — есть продажа (V_WB_SALES_RETURNS, NOT is_return) с этим srid;
--     CANCELLED  — FACT_ORDERS.is_cancel и продажи нет;
--     CONFLICT   — и отмена, и продажа (на 2026-09-16 таких 0);
--     UNRESOLVED — ни отмены, ни продажи: исход ещё неизвестен.
--   Доказательство связи (2026-09-16): 4 714 заказов, srid уникален, quantity = 1
--   у всех; для когорт 04–07.2026 неразрешённых 0, конфликтов 0, мульти-продаж 0.
--   Продаж без заказа внутри покрытия заказов — 2 (0,04 %) + продажи текущих суток.
--   Возврат после выкупа — отдельный счётчик; выкупом заказ остаётся (gross).
--   🔴 Ratio-колонки нет: процент — ratio-of-sums у потребителя.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY` AS
WITH
universe AS (
  SELECT DISTINCT nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL
),
sales AS (
  SELECT
    NULLIF(TRIM(srid), '')     AS srid,
    COUNTIF(NOT is_return)     AS sale_events,
    COUNTIF(is_return)         AS return_events,
    MIN(IF(NOT is_return, _sale_date, NULL)) AS first_sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`
  WHERE NULLIF(TRIM(srid), '') IS NOT NULL
  GROUP BY 1
),
orders AS (
  SELECT
    o.order_srid,
    o.order_date,
    o.is_cancel,
    IFNULL(s.sale_events, 0)   AS sale_events,
    IFNULL(s.return_events, 0) AS return_events,
    s.first_sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_ORDERS` o
  LEFT JOIN sales s ON s.srid = o.order_srid
  WHERE o.nm_id IN (SELECT nm_id FROM universe)
),
outcome AS (
  SELECT
    *,
    CASE
      WHEN sale_events > 0 AND is_cancel THEN 'CONFLICT'
      WHEN sale_events > 0               THEN 'BUYOUT'
      WHEN is_cancel                     THEN 'CANCELLED'
      ELSE                                    'UNRESOLVED'
    END AS order_outcome
  FROM orders
),
cov AS (
  SELECT day, orders_covered, is_current_day
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`
)
SELECT
  o.order_date                                               AS cohort_date,
  IFNULL(c.orders_covered, FALSE)                            AS orders_covered,
  COUNT(*)                                                   AS cohort_orders,
  COUNTIF(o.order_outcome = 'BUYOUT')                        AS buyout_orders,
  COUNTIF(o.order_outcome = 'CANCELLED')                     AS cancelled_orders,
  COUNTIF(o.order_outcome = 'UNRESOLVED')                    AS unresolved_orders,
  COUNTIF(o.order_outcome = 'CONFLICT')                      AS conflict_orders,
  -- Знаменатель процента выкупа: заказы с известным исходом.
  COUNTIF(o.order_outcome IN ('BUYOUT', 'CANCELLED'))        AS resolved_orders,
  COUNTIF(o.order_outcome = 'BUYOUT' AND o.return_events > 0) AS returned_after_buyout_orders,
  -- Зрелость без порога: когорта окончательна, когда у неё не осталось заказов
  -- без исхода и нет конфликтов. Иначе — предварительна (процент по известным исходам).
  (COUNTIF(o.order_outcome IN ('UNRESOLVED', 'CONFLICT')) = 0
   AND NOT IFNULL(c.is_current_day, FALSE))                  AS cohort_is_final,
  IF(COUNTIF(o.order_outcome IN ('UNRESOLVED', 'CONFLICT')) = 0
     AND NOT IFNULL(c.is_current_day, FALSE), 0, 1)          AS cohort_provisional_day,
  DATE_DIFF(CURRENT_DATE('Europe/Moscow'), o.order_date, DAY) AS cohort_age_days,
  MAX(DATE_DIFF(o.first_sale_date, o.order_date, DAY))       AS max_days_to_buyout,
  'BUYOUT_COHORT'                                            AS economics_basis,
  'Процент выкупа = SUM(buyout_orders) / SUM(resolved_orders) по когортам заказа. Неразрешённые заказы в знаменатель не входят и показываются unresolved_orders; период окончателен, если все его когорты cohort_is_final. Возвраты после выкупа — returned_after_buyout_orders, выкуп не отменяют (gross).'
                                                             AS economics_note,
  CURRENT_TIMESTAMP()                                        AS generated_at
FROM outcome o
LEFT JOIN cov c ON c.day = o.order_date
GROUP BY o.order_date, c.orders_covered, c.is_current_day;

-- ── §4. V_DASH_SETTLEMENT_DAILY — + цепочка цены, + удержания после реализации ─
--   Существующие колонки и их значения НЕ меняются. Добавлены:
--   • GAP-01 цепочка «цена продавца → к перечислению за товар» (все строки отчёта);
--   • GAP-04 разложение post_realization_deductions_rub по статьям.
--   Знак разложения: «+» уменьшает выплату, «−» — кредит WB (увеличивает).
--   Реклама здесь — ДОКУМЕНТЫ «WB Продвижение» (settlement), а не биллинг по
--   дате услуги (контракт P&L FIN CONTRACT V2). Возмещения WB денежного потока
--   не несут и в разложении отсутствуют.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_SETTLEMENT_DAILY` AS
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
    -- GAP-04: статьи вне «Удержание», которые несут deduction/additional_payment.
    IFNULL(SUM(IF(supplier_oper_name = 'Стоимость участия в программе лояльности',
                  SAFE_CAST(REPLACE(additional_payment, ',', '.') AS NUMERIC), NULL)), NUMERIC '0')
                                                                             AS loyalty_program_rub,
    IFNULL(SUM(IF(supplier_oper_name NOT IN ('Удержание', 'Стоимость участия в программе лояльности'),
                  IFNULL(SAFE_CAST(REPLACE(deduction, ',', '.') AS NUMERIC), 0)
                + IFNULL(SAFE_CAST(REPLACE(additional_payment, ',', '.') AS NUMERIC), 0), NULL)), NUMERIC '0')
                                                                             AS other_ops_deductions_rub,
    -- GAP-01: к перечислению по строкам вне «Продажа/Возврат» (цепочка их не раскладывает).
    IFNULL(SUM(IF(supplier_oper_name NOT IN ('Продажа', 'Возврат'),
                  SAFE_CAST(REPLACE(for_pay, ',', '.') AS NUMERIC), NULL)), NUMERIC '0')
                                                                             AS goods_other_ops_rub,
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
-- GAP-01: цепочка цены на ВСЕХ строках «Продажа/Возврат» (settlement-популяция).
price AS (
  SELECT
    finance_date                          AS day,
    COUNTIF(NOT price_chain_available)    AS price_rows_without_chain,
    SUM(seller_price_rub)                 AS seller_price_rub,
    SUM(spp_rub)                          AS spp_rub,
    SUM(buyer_paid_rub)                   AS buyer_paid_rub,
    SUM(wb_remuneration_rub)              AS wb_remuneration_rub,
    SUM(wb_remuneration_vat_rub)          AS wb_remuneration_vat_rub,
    SUM(acquiring_rub)                    AS acquiring_rub,
    SUM(pvz_reward_rub)                   AS pvz_reward_rub,
    SUM(price_chain_rounding_rub)         AS rounding_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`
  GROUP BY finance_date
),
-- GAP-04: «Удержание» по прямым меткам WB (Stage 3.1F). deduction_amount_rub
-- = deduction + additional_payment строки, знак сырой.
cls AS (
  SELECT
    finance_date AS day,
    SUM(IF(deduction_class = 'AD_BILLING',                   deduction_amount_rub, 0)) AS wb_promotion_documents_rub,
    SUM(IF(deduction_class = 'MINIMUM_PAYMENT_ADJUSTMENT',   deduction_amount_rub, 0)) AS tariff_option_minimum_payment_rub,
    SUM(IF(deduction_class = 'UTILIZATION',                  deduction_amount_rub, 0)) AS utilization_rub,
    SUM(IF(deduction_class = 'TRANSIT_DEDUCTION',            deduction_amount_rub, 0)) AS transit_rub,
    SUM(IF(deduction_class = 'FORCE_MAJEURE_PAYMENT',        deduction_amount_rub, 0)) AS force_majeure_rub,
    SUM(IF(deduction_class = 'REVIEW_POINTS_ADVANCE_REFUND', deduction_amount_rub, 0)) AS review_points_refund_rub,
    SUM(IF(deduction_class NOT IN ('AD_BILLING', 'MINIMUM_PAYMENT_ADJUSTMENT', 'UTILIZATION',
                                   'TRANSIT_DEDUCTION', 'FORCE_MAJEURE_PAYMENT',
                                   'REVIEW_POINTS_ADVANCE_REFUND'),
           deduction_amount_rub, 0))                                                 AS unclassified_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`
  GROUP BY finance_date
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
         + f.additional_payment_rub + f.acceptance_rub + f.loyalty_points_rub ) AS ded_total_positive,
         p.price_rows_without_chain, p.seller_price_rub AS p_seller_price_rub, p.spp_rub AS p_spp_rub,
         p.buyer_paid_rub AS p_buyer_paid_rub, p.wb_remuneration_rub AS p_wb_remuneration_rub,
         p.wb_remuneration_vat_rub AS p_wb_remuneration_vat_rub, p.acquiring_rub AS p_acquiring_rub,
         p.pvz_reward_rub AS p_pvz_reward_rub, p.rounding_rub AS p_rounding_rub,
         c.wb_promotion_documents_rub, c.tariff_option_minimum_payment_rub, c.utilization_rub,
         c.transit_rub, c.force_majeure_rub, c.review_points_refund_rub, c.unclassified_rub
  FROM fin f
  LEFT JOIN gate  g USING (day)
  LEFT JOIN price p USING (day)
  LEFT JOIN cls   c USING (day)
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
  IF(IFNULL(j.finance_covered, FALSE) AND IFNULL(j.finance_is_final, FALSE), 0, 1)
                                                          AS settlement_open_day,

  -- ── МОСТ. Публикуется только на закрытых сутках. ──
  IF(j.finance_covered AND j.finance_is_final, j.settlement_goods_rub, NULL)
                                                          AS settlement_goods_rub,
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

  -- ── EXECUTIVE V2 · GAP-04. Удержания после реализации по статьям ──
  --    «+» уменьшает выплату, «−» — кредит WB. Сумма статей = post_sale_deductions_rub
  --    = −post_realization_deductions_rub. Возмещения WB (MEMO) здесь отсутствуют:
  --    денежного потока они не несут.
  IF(j.finance_covered AND j.finance_is_final, j.ded_total_positive, NULL)          AS post_sale_deductions_rub,
  IF(j.finance_covered AND j.finance_is_final, j.logistics_rub, NULL)               AS post_sale_logistics_rub,
  IF(j.finance_covered AND j.finance_is_final, j.storage_rub, NULL)                 AS post_sale_storage_rub,
  IF(j.finance_covered AND j.finance_is_final, j.acceptance_rub, NULL)              AS post_sale_acceptance_rub,
  IF(j.finance_covered AND j.finance_is_final, j.penalty_rub, NULL)                 AS post_sale_penalty_rub,
  IF(j.finance_covered AND j.finance_is_final, IFNULL(j.wb_promotion_documents_rub, 0), NULL)
                                                                                    AS post_sale_wb_promotion_documents_rub,
  IF(j.finance_covered AND j.finance_is_final, IFNULL(j.tariff_option_minimum_payment_rub, 0), NULL)
                                                                                    AS post_sale_tariff_option_minimum_payment_rub,
  IF(j.finance_covered AND j.finance_is_final, IFNULL(j.utilization_rub, 0), NULL)  AS post_sale_utilization_rub,
  IF(j.finance_covered AND j.finance_is_final, IFNULL(j.transit_rub, 0), NULL)      AS post_sale_transit_rub,
  IF(j.finance_covered AND j.finance_is_final, IFNULL(j.force_majeure_rub, 0), NULL) AS post_sale_force_majeure_rub,
  IF(j.finance_covered AND j.finance_is_final, IFNULL(j.review_points_refund_rub, 0), NULL)
                                                                                    AS post_sale_review_points_refund_rub,
  IF(j.finance_covered AND j.finance_is_final, IFNULL(j.unclassified_rub, 0), NULL) AS post_sale_unclassified_deductions_rub,
  IF(j.finance_covered AND j.finance_is_final, j.loyalty_program_rub, NULL)         AS post_sale_loyalty_program_rub,
  IF(j.finance_covered AND j.finance_is_final, j.loyalty_points_rub, NULL)          AS post_sale_loyalty_points_rub,
  IF(j.finance_covered AND j.finance_is_final, j.other_ops_deductions_rub, NULL)    AS post_sale_other_operations_rub,

  -- ── EXECUTIVE V2 · GAP-01. Цепочка «цена продавца → к перечислению за товар» ──
  --    seller_price − spp − wb_remuneration − wb_remuneration_vat − acquiring − pvz_reward
  --    − rounding + goods_other_operations = settlement_goods_rub.
  --    Покрытие: закрытые сутки И полная цепочка у всех строк «Продажа/Возврат».
  (j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0)
                                                                                    AS settlement_price_chain_covered,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_seller_price_rub, 0), NULL)                                         AS settlement_seller_price_rub,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_spp_rub, 0), NULL)                                                  AS settlement_spp_rub,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_buyer_paid_rub, 0), NULL)                                           AS settlement_buyer_paid_rub,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_wb_remuneration_rub, 0), NULL)                                      AS settlement_wb_remuneration_rub,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_wb_remuneration_vat_rub, 0), NULL)                                  AS settlement_wb_remuneration_vat_rub,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_acquiring_rub, 0), NULL)                                            AS settlement_acquiring_rub,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_pvz_reward_rub, 0), NULL)                                           AS settlement_pvz_reward_rub,
  IF(j.finance_covered AND j.finance_is_final AND IFNULL(j.price_rows_without_chain, 0) = 0,
     IFNULL(j.p_rounding_rub, 0), NULL)                                             AS settlement_price_chain_rounding_rub,
  IF(j.finance_covered AND j.finance_is_final, j.goods_other_ops_rub, NULL)         AS settlement_goods_other_operations_rub,

  'WB_SETTLEMENT'                                         AS economics_basis,
  'Расчёты с Wildberries по финансовым отчётам WB за выбранный период. Это НЕ управленческий результат и НЕ прибыль: здесь не учитываются себестоимость товара, расходы фулфилмента, OPEX и прочие расходы бизнеса. «К выплате от WB» — сумма расчётов по отчётам площадки, а не подтверждённое поступление на банковский счёт. Включаются только закрытые финансовые сутки: открытая неделя WB ещё не рассчитана и в мост не входит. EXECUTIVE V2: post_sale_* раскладывают удержания после реализации (реклама — документы WB Продвижение, а не биллинг P&L); settlement_* раскладывают цепочку от цены продавца до суммы к перечислению.'
                                                          AS economics_note,
  CURRENT_TIMESTAMP()                                     AS generated_at

FROM j;

-- ── §5. V_DASH_EXECUTIVE_ECONOMICS_DAILY — + статус данных Executive ────────
--   Существующие колонки и значения НЕ меняются. Добавлены суточные флаги, из
--   которых потребитель выводит статус выбранного периода:
--     INCOMPLETE  — SUM(executive_incomplete_day) > 0
--     PROVISIONAL — иначе SUM(executive_provisional_day) > 0
--     FINAL       — иначе.
--   Обязательные источники управленческого P&L после себестоимости (FIN CONTRACT V2):
--   продажи, атрибуция рекламы (внутри вклада), финансы, биллинг рекламы,
--   себестоимость. Финальность: финансы (contains_provisional_finance) и биллинг
--   (ads_billing_is_final, SLA 15 сут × 3 чтения — в SQL, не в Metabase).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` AS
WITH
-- Суточный агрегат Product COGS. Fail-closed выполняется ЗДЕСЬ, до join:
-- достаточно одной неразрешённой строки витрины, чтобы суточная величина
-- стала неизвестной. Счётчики выводятся всегда — они делают ноль доказуемым.
cogs_day AS (
  SELECT
    day,
    COUNT(*)                                                                   AS cogs_rows,
    COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE'))      AS cogs_unresolved_rows,
    SUM(buyouts_qty)                                                           AS product_cogs_units,
    SUM(reversal_events)                                                       AS product_cogs_reversal_events,
    SUM(reversal_resolved_events)                                              AS product_cogs_reversal_resolved_events,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(net_product_cogs_operational_rub),      NULL)                       AS product_cogs_rub,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(product_cogs_operational_rub),          NULL)                       AS product_cogs_gross_rub,
    IF(COUNTIF(cogs_resolution_status NOT IN ('RESOLVED', 'NOT_APPLICABLE')) = 0,
       SUM(product_cogs_reversal_operational_rub), NULL)                       AS product_cogs_reversal_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_MART_SKU_DAILY_COGS`
  GROUP BY day
)

SELECT
  f.day,

  -- ── PASS-THROUGH Stage 3.1C PR2. Значения не пересчитываются. ──
  f.period_result_eligible,
  f.contribution_pre_cogs_rub,
  f.sales_revenue_seller_base_rub,
  f.period_result_pre_cogs_corrected_rub,
  f.revenue_base_period_result_rub,

  -- ── PRODUCT COGS (operational). NULL = неизвестно, 0 = доказуемо нет событий ──
  d.product_cogs_rub,
  d.product_cogs_gross_rub,
  d.product_cogs_reversal_rub,
  IFNULL(d.product_cogs_units, 0)                          AS product_cogs_units,
  IFNULL(d.cogs_rows, 0)                                   AS cogs_rows,
  IFNULL(d.cogs_unresolved_rows, 0)                        AS cogs_unresolved_rows,
  IFNULL(d.product_cogs_reversal_events, 0)                AS product_cogs_reversal_events,
  IFNULL(d.product_cogs_reversal_resolved_events, 0)       AS product_cogs_reversal_resolved_events,

  -- ── ПОКРЫТИЕ. Сутки без строк витрины — НЕ покрыты (d.cogs_rows IS NULL),
  --    а не «ноль себестоимости». Fail-closed по умолчанию.
  (d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
                                                           AS product_cogs_covered,
  (f.period_result_eligible
   AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
                                                           AS after_product_cogs_eligible,

  -- ── ЭКОНОМИКА ПОСЛЕ СЕБЕСТОИМОСТИ ТОВАРА ──
  -- 🔴 Числитель и знаменатель живут на ОДНОЙ маске after_product_cogs_eligible.
  --    Разные маски — это дефект, который чинил Stage 3.1C PR2; повторять нельзя.
  IF(f.period_result_eligible
     AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0,
     f.period_result_pre_cogs_corrected_rub - d.product_cogs_rub,
     NULL)                                                 AS period_result_after_product_cogs_rub,
  IF(f.period_result_eligible
     AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0,
     f.revenue_base_period_result_rub,
     NULL)                                                 AS revenue_base_after_product_cogs_rub,

  'AFTER_PRODUCT_COGS'                                     AS economics_basis,
  'Результат после SKU-level и account-level расходов WB и после себестоимости товара (Product COGS). НЕ ВКЛЮЧЕНЫ: fulfilment/FF costs, OPEX, ЗП, аренда, банковские расходы и налог. Это НЕ чистая прибыль и НЕ валовая маржа. Product COGS взят по OPERATIONAL-серии (единицы выкупа), возврат сторнирует себестоимость исходной продажи.'
                                                           AS economics_note,
  CURRENT_TIMESTAMP()                                      AS generated_at,

  -- ── EXECUTIVE V2 · STATUS. Отсутствие покрытия по источнику (1 = нет) ──
  IF(f.is_current_day, 1, 0)                                              AS exec_current_day,
  IF(f.sales_covered, 0, 1)                                               AS exec_missing_sales_day,
  IF(f.ads_covered, 0, 1)                                                 AS exec_missing_ads_attribution_day,
  IF(f.finance_covered AND f.contribution_covered, 0, 1)                  AS exec_missing_finance_day,
  IF(f.ads_billing_covered, 0, 1)                                         AS exec_missing_ads_billing_day,
  IF(d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0, 0, 1)
                                                                          AS exec_missing_cogs_day,
  -- ── Предварительность источника (1 = данные есть, но ещё могут измениться) ──
  IF(f.contains_provisional_finance, 1, 0)                                AS exec_provisional_finance_day,
  IF(f.ads_billing_covered AND NOT f.ads_billing_is_final, 1, 0)          AS exec_provisional_ads_billing_day,
  -- ── Сводные суточные флаги ──
  IF(NOT (f.period_result_eligible
          AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
     OR f.is_current_day, 1, 0)                                           AS executive_incomplete_day,
  IF((f.period_result_eligible
      AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
     AND NOT f.is_current_day
     AND (f.contains_provisional_finance OR NOT f.ads_billing_is_final), 1, 0)
                                                                          AS executive_provisional_day,
  IF((f.period_result_eligible
      AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
     AND NOT f.is_current_day
     AND NOT f.contains_provisional_finance AND f.ads_billing_is_final, 1, 0)
                                                                          AS executive_final_day,
  CASE
    WHEN NOT (f.period_result_eligible
              AND d.cogs_rows IS NOT NULL AND d.cogs_rows > 0 AND d.cogs_unresolved_rows = 0)
         OR f.is_current_day                                              THEN 'INCOMPLETE'
    WHEN f.contains_provisional_finance OR NOT f.ads_billing_is_final     THEN 'PROVISIONAL'
    ELSE                                                                       'FINAL'
  END                                                                     AS executive_data_status

FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` f
LEFT JOIN cogs_day d USING (day);

-- ── §6. ПРИЁМКА (fail-closed) ───────────────────────────────────────────────
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT finance_row_key)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`)
  AS 'EV2 §6.1: V_WB_FINANCE_PRICE_COMPONENTS — ключ строки не уникален';
ASSERT (SELECT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`)
             = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_KPI_DAILY`)
           AND (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`)
             = (SELECT COUNT(DISTINCT day) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`))
  AS 'EV2 §6.2: грейн V_DASH_EXECUTIVE_BREAKDOWN_DAILY нарушен';
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT cohort_date)
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_DASH_BUYOUT_COHORT_DAILY`)
  AS 'EV2 §6.3: грейн V_DASH_BUYOUT_COHORT_DAILY нарушен';
