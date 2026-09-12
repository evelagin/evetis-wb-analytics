-- ============================================================================
-- EVETIS · Stage C1 · План поставок, сборка наборов, снятие с паллет (ТОЛЬКО ЧТЕНИЕ)
-- Вся бизнес-логика листа «EVETIS OPERATIONS» — здесь. Лист показывает значения этих
-- представлений; в листе нет формул, которые что-то решают.
-- Правила владельца: docs/ops/STAGE_C0_LOGISTICS_MASTER_2026-09-11.md §11 и GO C1 2026-09-12.
--
-- Грейн плана: канал × карточка (одиночный SKU или набор) — там, где есть текущая карточка.
-- Единицы плана — карточки (единицы продажи); в физические единицы раскладываются по BOM
-- только в разделах «сборка наборов» и «снятие с паллет».
--
-- Формулы строки (каждое число видно в листе):
--   позиция              = на площадке + в пути + на приёмке + резерв на ФФ + прочие подтверждённые входящие
--                          (каждая поставка засчитана один раз — по классу сверки заказа)
--   к прибытию           = позиция − спрос до прибытия (по плану, дни от сегодня до прибытия)
--   цель к прибытию      = ⌈план/день × (покрытие + страховой)⌉, план/день — среднее по окну после прибытия
--   потребность          = max(0, цель − max(0, к прибытию))
--   округление           = вверх до кратности; меньше кратности → кратность низкого спроса Ozon,
--                          иначе ЖДАТЬ, если до следующей поставки страховой запас сохранится, иначе минимум
--   ворота               = ПРОВЕРИТЬ: покрытие после прибытия > цель + страховой + 14; распродажа позже
--                          срока годности − 30 дн; приёмка Ozon без отчёта меняет решение ОТГРУЗИТЬ ↔ ЖДАТЬ
--   ФФ                   = один общий пул для WB и Ozon: строки обслуживаются по срочности, никакая
--                          единица не обещана дважды
-- ============================================================================

-- Параметры C1 (решение владельца 2026-09-12). Идемпотентно.
MERGE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` t
USING (
  SELECT 'c1_next_supply_cadence_days' AS config_key, '7' AS config_value,
         'C1 OWNER_CONVENTION 2026-09-12: weekly shipping cadence; next feasible supply = today + 7 d + FF prep + channel lead' AS note
  UNION ALL SELECT 'c1_overstock_tolerance_days', '14',
         'C1 OWNER_CONVENTION 2026-09-12: REVIEW if cover at arrival after the rounded shipment > target + safety + 14 (WB 66, Ozon 69)'
  UNION ALL SELECT 'c1_expiry_margin_days', '30',
         'C1 OWNER_CONVENTION 2026-09-12: REVIEW if projected inventory cannot sell through at least 30 days before expiry'
) s
ON t.config_key = s.config_key
WHEN NOT MATCHED THEN
  INSERT (config_key, config_value, note, updated_at, updated_by)
  VALUES (s.config_key, s.config_value, s.note, CURRENT_TIMESTAMP(), 'stage-c1');

-- Календарь плана по каналу: дата отгрузки после сборки ФФ (рабочие дни), прибытие, следующая возможная поставка.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_CALENDAR` AS
WITH p AS (
  SELECT CURRENT_DATE('Europe/Moscow') AS today,
    (SELECT ANY_VALUE(ff_prep_working_days) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING`
      WHERE channel = 'FF' AND is_planning_default AND effective_from <= CURRENT_DATE('Europe/Moscow')
        AND (effective_to IS NULL OR effective_to >= CURRENT_DATE('Europe/Moscow'))) AS ff_prep_working_days,
    (SELECT SAFE_CAST(ANY_VALUE(config_value) AS INT64) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
      WHERE config_key = 'c1_next_supply_cadence_days') AS cadence_days,
    (SELECT SAFE_CAST(ANY_VALUE(config_value) AS INT64) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
      WHERE config_key = 'c1_overstock_tolerance_days') AS overstock_tolerance_days,
    (SELECT SAFE_CAST(ANY_VALUE(config_value) AS INT64) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
      WHERE config_key = 'c1_expiry_margin_days') AS expiry_margin_days
),
wd_now AS (   -- рабочие дни (пн–пт) после сегодня
  SELECT d, ROW_NUMBER() OVER (ORDER BY d) AS n
  FROM p, UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(p.today, INTERVAL 1 DAY), DATE_ADD(p.today, INTERVAL 30 DAY))) AS d
  WHERE EXTRACT(DAYOFWEEK FROM d) NOT IN (1, 7)
),
wd_next AS (  -- рабочие дни после сегодня + недельный цикл
  SELECT d, ROW_NUMBER() OVER (ORDER BY d) AS n
  FROM p, UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(p.today, INTERVAL p.cadence_days + 1 DAY),
                                     DATE_ADD(p.today, INTERVAL p.cadence_days + 30 DAY))) AS d
  WHERE EXTRACT(DAYOFWEEK FROM d) NOT IN (1, 7)
),
ch AS (
  SELECT channel, shipping_method, target_cover_days, safety_stock_days, lead_time_days,
         max_lot_weight_kg, max_lot_units, max_lot_volume_l
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING`
  WHERE channel IN ('WB', 'OZON') AND is_planning_default AND effective_from <= CURRENT_DATE('Europe/Moscow')
    AND (effective_to IS NULL OR effective_to >= CURRENT_DATE('Europe/Moscow'))
)
SELECT ch.channel, ch.shipping_method, p.today, p.ff_prep_working_days, p.cadence_days,
       p.overstock_tolerance_days, p.expiry_margin_days,
       ch.target_cover_days, ch.safety_stock_days, ch.lead_time_days,
       ch.max_lot_weight_kg, ch.max_lot_units, ch.max_lot_volume_l,
       (SELECT d FROM wd_now WHERE n = p.ff_prep_working_days) AS ship_date,
       (SELECT d FROM wd_next WHERE n = p.ff_prep_working_days) AS next_ship_date,
       ch.target_cover_days + ch.safety_stock_days + p.overstock_tolerance_days AS overstock_limit_days
FROM p CROSS JOIN ch;

-- Правило округления владельца (одно место для основного и худшего случая приёмки).
CREATE OR REPLACE FUNCTION `project-fa311fc0-4d87-4781-986.evetis_ops.fn_ops_round_decision`(
  need INT64, m INT64, m_low INT64, m_min INT64, wait_ok BOOL, has_plan BOOL)
RETURNS STRUCT<rule STRING, rec INT64, multiple_applied INT64> AS (
  CASE
    WHEN NOT has_plan THEN STRUCT('NO_PLAN', 0, m)
    WHEN need = 0 THEN STRUCT('ENOUGH', 0, m)
    WHEN need >= m THEN STRUCT('UP_TO_MULTIPLE', CAST(CEIL(need / m) AS INT64) * m, m)
    WHEN m_low IS NOT NULL AND need >= m_low THEN STRUCT('LOW_DEMAND_MULTIPLE', CAST(CEIL(need / m_low) AS INT64) * m_low, m_low)
    WHEN wait_ok THEN STRUCT('WAIT', 0, m)
    ELSE STRUCT('SAFETY_MIN', m_min, m_min)
  END
);

-- ПЛАН ПОСТАВОК: канал × карточка.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN` AS
WITH cal AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_CALENDAR`),
sku AS (
  SELECT l.internal_sku AS card_sku, l.channel, l.shipment_multiple, l.shipment_multiple_low_demand, l.min_shipment_units,
         l.target_cover_days AS target_override, l.safety_stock_days AS safety_override, l.lead_time_days AS lead_override,
         l.fbs_reserve_units, pm.is_bundle,
         COALESCE(pm.product_name_short, pm.canonical_product_name) AS product_name, pm.canonical_product_name,
         pm.product_line
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS` l
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm USING (internal_sku)
  WHERE l.effective_from <= CURRENT_DATE('Europe/Moscow') AND (l.effective_to IS NULL OR l.effective_to >= CURRENT_DATE('Europe/Moscow'))
),
dated AS (
  SELECT s.*, c.today, c.ship_date, c.next_ship_date, c.overstock_tolerance_days, c.expiry_margin_days, c.ff_prep_working_days,
         COALESCE(s.target_override, c.target_cover_days) AS target_cover_days,
         COALESCE(s.safety_override, c.safety_stock_days) AS safety_stock_days,
         COALESCE(s.lead_override, c.lead_time_days) AS lead_time_days,
         DATE_ADD(c.ship_date, INTERVAL COALESCE(s.lead_override, c.lead_time_days) DAY) AS arrival_date,
         DATE_ADD(c.next_ship_date, INTERVAL COALESCE(s.lead_override, c.lead_time_days) DAY) AS next_arrival_date
  FROM sku s JOIN cal c USING (channel)
),
-- остаток на площадке по карточке (API)
wb_stock AS (
  SELECT internal_sku AS card_sku, SUM(IF(row_type = 'AGGREGATE', quantity, 0)) AS on_marketplace, MAX(_snapshot_date) AS as_of
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_STOCKS_T5_CURRENT` WHERE internal_sku IS NOT NULL GROUP BY 1
),
oz_snap AS (
  SELECT sku, warehouse_id, available_stock_count, snapshot_date FROM (
    SELECT sku, warehouse_id, available_stock_count, snapshot_date,
           ROW_NUMBER() OVER (PARTITION BY sku, warehouse_id ORDER BY extracted_at DESC) AS rn
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_STOCKS`))
  WHERE rn = 1
),
oz_map AS (
  SELECT DISTINCT internal_sku, CAST(marketplace_sku AS STRING) AS msku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` WHERE UPPER(marketplace) = 'OZON' AND is_current
),
oz_stock AS (
  SELECT m.internal_sku AS card_sku, SUM(o.available_stock_count) AS on_marketplace, MAX(o.snapshot_date) AS as_of
  FROM oz_snap o JOIN oz_map m ON m.msku = CAST(o.sku AS STRING) GROUP BY 1
),
-- WB: поставки в пути (API): запланированные, не завершённые, за 60 дней; черновики (status 1) — не входящие
wb_transit AS (
  SELECT m.internal_sku AS card_sku, SUM(GREATEST(g.quantity - IFNULL(g.accepted_quantity, 0), 0)) AS in_transit
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` g
  JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT` s ON s.supply_id = g.supply_id
  JOIN (SELECT DISTINCT internal_sku, CAST(marketplace_sku AS STRING) AS msku
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` WHERE UPPER(marketplace) = 'WB') m
    ON m.msku = CAST(g.nm_id AS STRING)
  WHERE s.status_id >= 2 AND NOT IFNULL(s.is_completed, FALSE)
    AND s.create_dt >= TIMESTAMP(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 60 DAY))
  GROUP BY 1
),
-- Ozon: заказы по классу сверки (один класс на заказ → одна единица в одном разрезе)
oz_lines AS (
  SELECT order_number, supply_id, ozon_sku, ANY_VALUE(card_sku) AS card_sku,
         ANY_VALUE(cards_planned) AS cards_planned, ANY_VALUE(cards_accepted) AS cards_accepted, ANY_VALUE(state) AS api_state
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS`
  GROUP BY order_number, supply_id, ozon_sku
),
oz_cls AS (
  SELECT order_number, ANY_VALUE(recon_class) AS recon_class, LOGICAL_OR(in_acceptance) AS in_acceptance,
         LOGICAL_OR(accepted_unreported) AS accepted_unreported
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON` GROUP BY 1
),
oz_commit AS (
  SELECT l.card_sku,
    SUM(IF(c.in_acceptance, l.cards_planned - IFNULL(l.cards_accepted, 0), 0)) AS in_acceptance,
    SUM(IF(NOT c.in_acceptance AND (c.recon_class = 'IN_TRANSIT_API'
             OR (c.recon_class = 'API_ONLY' AND l.api_state IN ('ACCEPTED_AT_SUPPLY_WAREHOUSE', 'IN_TRANSIT'))),
           l.cards_planned - IFNULL(l.cards_accepted, 0), 0)) AS in_transit,
    SUM(IF(c.recon_class = 'API_RESERVATION_UNCONFIRMED', l.cards_planned, 0)) AS api_reserved_unconfirmed,
    SUM(IF(c.recon_class = 'SHIPPED_UNCONFIRMED', l.cards_planned, 0)) AS shipped_unconfirmed,
    LOGICAL_OR(c.in_acceptance AND c.accepted_unreported) AS acceptance_unreported
  FROM oz_lines l JOIN oz_cls c USING (order_number)
  GROUP BY 1
),
-- журнал ФФ: отгрузки, зарезервированные на ФФ (любой канал), и отгрузки WB/B2B без сверки с API
ff_lines AS (
  SELECT sh.channel, sl.card_sku,
         SUM(IF(sh.status = 'RESERVED', sl.qty_cards, 0)) AS reserved_on_ff,
         SUM(IF(sh.status = 'SHIPPED' AND sh.channel != 'OZON', sl.qty_cards, 0)) AS ledger_shipped_unconfirmed
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE` sl
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` sh USING (shipment_id)
  GROUP BY 1, 2
),
plan AS (
  SELECT marketplace AS channel, internal_sku AS card_sku, plan_date, target_cards
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_ACTIVE`
),
dem AS (
  SELECT d.channel, d.card_sku,
    IFNULL(SUM(IF(p.plan_date >= d.today AND p.plan_date < d.arrival_date, p.target_cards, 0)), 0) AS demand_until_arrival_exact,
    IFNULL(SUM(IF(p.plan_date >= d.today AND p.plan_date < d.next_arrival_date, p.target_cards, 0)), 0) AS demand_until_next_arrival_exact,
    IFNULL(SUM(IF(p.plan_date >= d.arrival_date
                  AND p.plan_date < DATE_ADD(d.arrival_date, INTERVAL d.target_cover_days + d.safety_stock_days DAY),
                  p.target_cards, 0)), 0) AS demand_target_window_exact
  FROM dated d LEFT JOIN plan p ON p.channel = d.channel AND p.card_sku = d.card_sku
  GROUP BY 1, 2
),
act AS (   -- ФАКТ для сравнения (в расчёте не участвует): заказано карточек в день за 30 дней
  SELECT marketplace AS channel, internal_sku AS card_sku, ROUND(SUM(cards_ordered) / 30, 1) AS actual_daily_30d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY`
  WHERE d > DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 30 DAY) AND d < CURRENT_DATE('Europe/Moscow')
  GROUP BY 1, 2
),
expiry AS (   -- срок годности карточки = самый ранний у её компонентов (партии на руках)
  SELECT b.card_sku, MIN(e.expiry_date) AS expiry_date
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b
  JOIN (SELECT internal_sku, MIN(expiry_date) AS expiry_date
        FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_EXPIRY_BATCH` WHERE NOT is_inbound GROUP BY 1) e
    ON e.internal_sku = b.component_sku
  GROUP BY 1
),
inp AS (
  SELECT d.*,
    IFNULL(IF(d.channel = 'WB', ws.on_marketplace, os.on_marketplace), 0) AS on_marketplace,
    IF(d.channel = 'WB', ws.as_of, os.as_of) AS stock_as_of,
    IFNULL(IF(d.channel = 'WB', wt.in_transit, oc.in_transit), 0) AS in_transit,
    IF(d.channel = 'OZON', IFNULL(oc.in_acceptance, 0), 0) AS in_acceptance,
    IFNULL(fl.reserved_on_ff, 0) AS reserved_on_ff,
    IF(d.channel = 'OZON', IFNULL(oc.api_reserved_unconfirmed, 0), 0) AS api_reserved_unconfirmed,
    IF(d.channel = 'OZON', IFNULL(oc.shipped_unconfirmed, 0), IFNULL(fl.ledger_shipped_unconfirmed, 0)) AS shipped_unconfirmed,
    d.channel = 'OZON' AND IFNULL(oc.acceptance_unreported, FALSE) AS acceptance_unreported,
    CAST(ROUND(dm.demand_until_arrival_exact) AS INT64) AS demand_until_arrival,
    CAST(ROUND(dm.demand_until_next_arrival_exact) AS INT64) AS demand_until_next_arrival,
    ROUND(SAFE_DIVIDE(dm.demand_target_window_exact, d.target_cover_days + d.safety_stock_days), 1) AS daily_plan,
    dm.demand_target_window_exact,
    IFNULL(ac.actual_daily_30d, 0) AS actual_daily_30d,
    ex.expiry_date
  FROM dated d
  LEFT JOIN wb_stock ws ON d.channel = 'WB' AND ws.card_sku = d.card_sku
  LEFT JOIN oz_stock os ON d.channel = 'OZON' AND os.card_sku = d.card_sku
  LEFT JOIN wb_transit wt ON d.channel = 'WB' AND wt.card_sku = d.card_sku
  LEFT JOIN oz_commit oc ON d.channel = 'OZON' AND oc.card_sku = d.card_sku
  LEFT JOIN ff_lines fl ON fl.channel = d.channel AND fl.card_sku = d.card_sku
  LEFT JOIN dem dm ON dm.channel = d.channel AND dm.card_sku = d.card_sku
  LEFT JOIN expiry ex ON ex.card_sku = d.card_sku
  LEFT JOIN act ac ON ac.channel = d.channel AND ac.card_sku = d.card_sku
),
calc AS (
  SELECT i.*,
    i.in_transit + i.in_acceptance + i.reserved_on_ff + i.api_reserved_unconfirmed + i.shipped_unconfirmed AS committed_inbound,
    i.on_marketplace + i.in_transit + i.in_acceptance + i.reserved_on_ff + i.api_reserved_unconfirmed + i.shipped_unconfirmed AS position,
    -- приёмка Ozon без отчёта: худший случай — вся приёмка уже в остатке Ozon (засчитана бы дважды).
    -- Принятое количество не угадывается: считается только, может ли это поменять решение.
    IF(i.acceptance_unreported AND i.in_acceptance > 0 AND i.on_marketplace > 0,
       LEAST(i.in_acceptance, i.on_marketplace), 0) AS acceptance_overlap_max,
    CAST(CEIL(i.daily_plan * (i.target_cover_days + i.safety_stock_days) - 1e-9) AS INT64) AS target_at_arrival,
    CAST(CEIL(i.daily_plan * i.safety_stock_days - 1e-9) AS INT64) AS safety_units,
    IFNULL(i.daily_plan, 0) > 0 OR i.demand_until_arrival > 0 AS has_plan
  FROM inp i
),
calc2 AS (
  SELECT c.*,
    c.position - c.demand_until_arrival AS projected_at_arrival_raw,
    GREATEST(c.position - c.demand_until_arrival, 0) AS projected_at_arrival,
    GREATEST(c.demand_until_arrival - c.position, 0) AS shortfall_before_arrival,
    c.target_at_arrival - c.safety_units AS target_cover_units,
    GREATEST(c.target_at_arrival - GREATEST(c.position - c.demand_until_arrival, 0), 0) AS need_math,
    c.position - c.demand_until_next_arrival AS projected_at_next_arrival,
    c.position - c.demand_until_next_arrival >= c.safety_units AS wait_ok,
    GREATEST(c.target_at_arrival - GREATEST(c.position - c.acceptance_overlap_max - c.demand_until_arrival, 0), 0) AS need_alt,
    c.position - c.acceptance_overlap_max - c.demand_until_next_arrival >= c.safety_units AS wait_ok_alt
  FROM calc c
),
dec AS (
  SELECT c.*,
    `project-fa311fc0-4d87-4781-986.evetis_ops.fn_ops_round_decision`(
      c.need_math, c.shipment_multiple, c.shipment_multiple_low_demand, c.min_shipment_units, c.wait_ok, c.has_plan) AS d0,
    IF(c.acceptance_overlap_max > 0,
       `project-fa311fc0-4d87-4781-986.evetis_ops.fn_ops_round_decision`(
         c.need_alt, c.shipment_multiple, c.shipment_multiple_low_demand, c.min_shipment_units, c.wait_ok_alt, c.has_plan),
       NULL) AS d1
  FROM calc2 c
),
base AS (
  SELECT d.* EXCEPT (d0, d1),
         d.d0.rule AS rule, d.d0.rec AS rec_proposed, d.d0.multiple_applied AS multiple_applied,
         d.d1.rule AS rule_alt, d.d1.rec AS rec_alt
  FROM dec d
),
-- ОБЩИЙ ПУЛ ФФ: свободно = AVAILABLE − резерв Ozon из API без отметки ФФ − резерв FBS
ff_bal AS (
  SELECT internal_sku AS component_sku,
         SUM(IF(state = 'AVAILABLE', units, 0)) AS available_total
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY 1
),
ff_unconf AS (
  SELECT internal_sku AS component_sku, SUM(api_unconfirmed_units) AS unconf
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON` GROUP BY 1
),
ff_fbs AS (
  SELECT b.component_sku, SUM(l.fbs_reserve_units * b.component_qty) AS fbs
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS` l
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = l.internal_sku
  WHERE l.effective_from <= CURRENT_DATE('Europe/Moscow') AND (l.effective_to IS NULL OR l.effective_to >= CURRENT_DATE('Europe/Moscow'))
  GROUP BY 1
),
ff_free AS (
  SELECT b.component_sku,
         GREATEST(b.available_total - IFNULL(u.unconf, 0) - IFNULL(f.fbs, 0), 0) AS free_units
  FROM ff_bal b LEFT JOIN ff_unconf u USING (component_sku) LEFT JOIN ff_fbs f USING (component_sku)
),
units_per_pos AS (   -- сколько физических единиц в одной позиции продажи (набор = сумма BOM)
  SELECT card_sku, SUM(component_qty) AS units_per_position
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` GROUP BY 1
),
cap_now AS (   -- сколько позиций этой строки можно обеспечить свободным запасом ФФ прямо сейчас
  SELECT b.card_sku, MIN(DIV(IFNULL(f.free_units, 0), b.component_qty)) AS ff_free_cards_now
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b
  LEFT JOIN ff_free f ON f.component_sku = b.component_sku
  GROUP BY 1
),
alloc_in AS (   -- срочность: дней покрытия к прибытию без отгрузки; меньше — раньше
  SELECT p.channel, p.card_sku, p.multiple_applied, bom.component_sku, bom.component_qty,
         p.rec_proposed * bom.component_qty AS comp_demand,
         IFNULL(SAFE_DIVIDE(p.projected_at_arrival, p.daily_plan), 0) AS urgency_days
  FROM base p
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` bom ON bom.card_sku = p.card_sku
  WHERE p.rec_proposed > 0
),
alloc AS (
  SELECT a.*, IFNULL(f.free_units, 0) AS free_units,
         IFNULL(SUM(a.comp_demand) OVER (PARTITION BY a.component_sku
                                         ORDER BY a.urgency_days, IF(a.channel = 'WB', 0, 1), a.card_sku
                                         ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS cum_before
  FROM alloc_in a LEFT JOIN ff_free f USING (component_sku)
),
line_alloc AS (
  SELECT channel, card_sku,
         LOGICAL_AND(cum_before + comp_demand <= free_units) AS ff_fits,
         MIN(DIV(GREATEST(free_units - cum_before, 0), component_qty)) AS ff_max_cards,
         ARRAY_AGG(component_sku ORDER BY SAFE_DIVIDE(GREATEST(free_units - cum_before, 0), component_qty), component_sku
                   LIMIT 1)[OFFSET(0)] AS ff_limiting_component,
         ANY_VALUE(urgency_days) AS urgency_days
  FROM alloc GROUP BY 1, 2
),
fin AS (
  SELECT b.*, la.ff_fits, la.ff_max_cards, la.ff_limiting_component, la.urgency_days,
    CASE WHEN b.rec_proposed = 0 THEN 0
         WHEN la.ff_fits THEN b.rec_proposed
         ELSE DIV(la.ff_max_cards, b.multiple_applied) * b.multiple_applied END AS rec_final
  FROM base b LEFT JOIN line_alloc la USING (channel, card_sku)
),
fin2 AS (
  SELECT f.*, IFNULL(cn.ff_free_cards_now, 0) AS ff_free_cards_now,
         IFNULL(up.units_per_position, 1) AS units_per_position,
         f.rec_final * IFNULL(up.units_per_position, 1) AS rec_physical_units,
         IFNULL(cn.ff_free_cards_now, 0) * IFNULL(up.units_per_position, 1) AS ff_free_physical_units,
         SAFE_DIVIDE(f.daily_plan, NULLIF(f.actual_daily_30d, 0)) AS plan_actual_ratio
  FROM fin f LEFT JOIN cap_now cn USING (card_sku) LEFT JOIN units_per_pos up USING (card_sku)
),
gates AS (
  SELECT f.*,
    ROUND(SAFE_DIVIDE(f.projected_at_arrival + f.rec_final, f.daily_plan), 1) AS resulting_cover_days,
    f.target_cover_days + f.safety_stock_days + f.overstock_tolerance_days AS overstock_limit_days,
    DATE_ADD(f.arrival_date, INTERVAL CAST(CEIL(IFNULL(SAFE_DIVIDE(f.projected_at_arrival + f.rec_final, f.daily_plan), 0)) AS INT64) DAY) AS sellout_date,
    DATE_SUB(f.expiry_date, INTERVAL f.expiry_margin_days DAY) AS sellout_deadline,
    f.rec_alt IS NOT NULL AND ((f.rec_proposed > 0) != (f.rec_alt > 0)) AS acceptance_flip,
    -- основание спроса: план против факта 30 дней. Рекомендацию НЕ меняет — предупреждение владельцу.
    f.daily_plan > 0 AND (f.actual_daily_30d = 0 OR f.plan_actual_ratio > 2) AS aggressive_plan,
    CASE
      WHEN IFNULL(f.daily_plan, 0) = 0 THEN 'НЕТ ПЛАНА'
      WHEN f.actual_daily_30d = 0 THEN 'LOW'
      WHEN f.plan_actual_ratio > 2 THEN 'LOW'
      WHEN f.plan_actual_ratio > 1.25 THEN 'MEDIUM'
      ELSE 'HIGH'
    END AS demand_confidence
  FROM fin2 f
),
flags AS (
  SELECT g.*,
    g.rec_final > 0 AND g.resulting_cover_days > g.overstock_limit_days AS review_overstock,
    g.rec_final > 0 AND g.expiry_date IS NOT NULL AND g.sellout_date > g.sellout_deadline AS review_expiry,
    g.rec_proposed > 0 AND g.rec_final < g.rec_proposed AS ff_limited
  FROM gates g
)
SELECT
  channel, card_sku, product_name, canonical_product_name, product_line, is_bundle,
  -- площадка сейчас и учтённые входящие (каждая единица один раз)
  on_marketplace, in_transit, in_acceptance, reserved_on_ff, api_reserved_unconfirmed, shipped_unconfirmed,
  committed_inbound, position, stock_as_of,
  -- время
  today, ship_date, arrival_date, DATE_DIFF(arrival_date, today, DAY) AS days_to_arrival, ff_prep_working_days, lead_time_days,
  next_ship_date, next_arrival_date,
  -- спрос и цель
  demand_until_arrival, projected_at_arrival_raw, projected_at_arrival,
  shortfall_before_arrival,
  daily_plan, actual_daily_30d, target_cover_days, safety_stock_days, target_cover_units, safety_units, target_at_arrival,
  need_math,
  -- округление и решение
  shipment_multiple, shipment_multiple_low_demand, min_shipment_units, multiple_applied, rule,
  rec_proposed, rec_final, resulting_cover_days, overstock_limit_days,
  demand_until_next_arrival, projected_at_next_arrival, wait_ok,
  expiry_date, sellout_date, sellout_deadline,
  acceptance_unreported, acceptance_overlap_max, rec_alt, rule_alt, acceptance_flip,
  ff_fits, ff_max_cards, ff_free_cards_now, ff_free_physical_units, ff_limiting_component, urgency_days,
  units_per_position, rec_physical_units, plan_actual_ratio, demand_confidence, aggressive_plan,
  IF(aggressive_plan, 'АГРЕССИВНЫЙ ПЛАН — ОБЪЁМ ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ', NULL) AS demand_warning,
  review_overstock, review_expiry, ff_limited,
  CASE
    WHEN rule = 'NO_PLAN' THEN 'NO PLAN'
    WHEN acceptance_flip THEN 'REVIEW'
    WHEN rule = 'ENOUGH' THEN 'OK'
    WHEN rule = 'WAIT' THEN 'WAIT'
    WHEN rec_final = 0 AND ff_limited THEN 'FF LIMIT'
    WHEN review_overstock OR review_expiry THEN 'REVIEW'
    ELSE 'SHIP'
  END AS status_code,
  CASE
    WHEN rule = 'NO_PLAN' THEN 'НЕТ ПЛАНА'
    WHEN acceptance_flip THEN 'ПРОВЕРИТЬ — приёмка не подтверждена'
    WHEN rule = 'ENOUGH' THEN 'ДОСТАТОЧНО'
    WHEN rule = 'WAIT' THEN 'ЖДАТЬ / В СЛЕДУЮЩУЮ ПОСТАВКУ'
    WHEN rec_final = 0 AND ff_limited THEN 'ФФ: НЕ ХВАТАЕТ'
    WHEN review_overstock THEN 'ПРОВЕРИТЬ — затоваривание'
    WHEN review_expiry THEN 'ПРОВЕРИТЬ — срок годности'
    WHEN ff_limited THEN 'ОТГРУЗИТЬ — урезано ФФ'
    WHEN rule = 'SAFETY_MIN' THEN 'ОТГРУЗИТЬ — страховой запас'
    ELSE 'ОТГРУЗИТЬ'
  END AS status_label,
  -- КОРОТКАЯ причина для верхних блоков владельца (≤ 80 знаков). Полная — в reason ниже.
  CASE
    WHEN acceptance_flip THEN 'приёмка Ozon не подтверждена — проверить'
    WHEN review_overstock THEN FORMAT('покрытие %.0f дн > %d — проверить', resulting_cover_days, overstock_limit_days)
    WHEN review_expiry THEN FORMAT('распродажа позже срока годности − %d дн', expiry_margin_days)
    WHEN rec_final = 0 AND ff_limited THEN FORMAT('на ФФ свободно %d (%s)', ff_max_cards, REPLACE(ff_limiting_component, 'EVT-', ''))
    WHEN ff_limited THEN FORMAT('урезано ФФ до %d (%s)', rec_final, REPLACE(ff_limiting_component, 'EVT-', ''))
    WHEN rule = 'NO_PLAN' THEN 'плана продаж нет'
    WHEN rule = 'ENOUGH' THEN FORMAT('запаса хватает: %d ≥ цели %d', projected_at_arrival, target_at_arrival)
    WHEN rule = 'WAIT' THEN FORMAT('хватит до поставки %s — ждать', FORMAT_DATE('%d.%m', next_arrival_date))
    WHEN rule = 'SAFETY_MIN' THEN FORMAT('страховой запас: минимум %d', rec_proposed)
    WHEN rule = 'LOW_DEMAND_MULTIPLE' THEN FORMAT('нужно %d → %d (низкий спрос, кратность %d)', need_math, rec_final, shipment_multiple_low_demand)
    ELSE FORMAT('нужно %d → %d (кратность %d)', need_math, rec_final, shipment_multiple)
  END AS short_reason,
  -- индикатор риска рекомендации: причина — отношение планового темпа к факту 30 дней
  CASE
    WHEN acceptance_flip OR review_overstock OR review_expiry THEN 'REVIEW'
    WHEN aggressive_plan THEN 'PLAN-DRIVEN'
    WHEN IFNULL(daily_plan, 0) = 0 THEN '—'
    ELSE 'HIGH CONFIDENCE'
  END AS risk_label,
  CASE
    WHEN acceptance_flip OR review_overstock OR review_expiry THEN 'требует решения владельца'
    WHEN aggressive_plan AND actual_daily_30d = 0 THEN 'план есть, продаж за 30 дней нет'
    WHEN aggressive_plan THEN FORMAT('план ×%.1f к факту 30 дн', plan_actual_ratio)
    WHEN IFNULL(daily_plan, 0) = 0 THEN 'плана нет'
    ELSE FORMAT('план ×%.1f к факту 30 дн', IFNULL(plan_actual_ratio, 1))
  END AS risk_note,
  ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST([
    CASE rule
      WHEN 'NO_PLAN' THEN 'плана продаж нет'
      WHEN 'ENOUGH' THEN FORMAT('к прибытию %d ≥ цели %d', projected_at_arrival, target_at_arrival)
      WHEN 'UP_TO_MULTIPLE' THEN FORMAT('нужно %d → вверх до кратности %d = %d', need_math, shipment_multiple, rec_proposed)
      WHEN 'LOW_DEMAND_MULTIPLE' THEN FORMAT('нужно %d < кратности %d → кратность низкого спроса %d = %d',
                                             need_math, shipment_multiple, shipment_multiple_low_demand, rec_proposed)
      WHEN 'WAIT' THEN FORMAT('нужно %d < кратности %d; к следующей поставке (%s) останется %d ≥ страховых %d',
                              need_math, IFNULL(shipment_multiple_low_demand, shipment_multiple),
                              FORMAT_DATE('%d.%m', next_arrival_date), projected_at_next_arrival, safety_units)
      WHEN 'SAFETY_MIN' THEN FORMAT('нужно %d < кратности %d, но к следующей поставке (%s) останется %d < страховых %d → минимум %d',
                                    need_math, IFNULL(shipment_multiple_low_demand, shipment_multiple),
                                    FORMAT_DATE('%d.%m', next_arrival_date), projected_at_next_arrival, safety_units, rec_proposed)
    END,
    IF(shortfall_before_arrival > 0, FORMAT('до прибытия не хватит %d шт — спрос не восполняется', shortfall_before_arrival), NULL),
    IF(ff_limited, FORMAT('на ФФ свободно только на %d (%s) → %d', ff_max_cards, REPLACE(ff_limiting_component, 'EVT-', ''), rec_final), NULL),
    IF(review_overstock, FORMAT('покрытие после прибытия %.1f дн > %d (%d + %d + %d)', resulting_cover_days, overstock_limit_days,
                                target_cover_days, safety_stock_days, overstock_tolerance_days), NULL),
    IF(review_expiry, FORMAT('распродажа к %s позже, чем за %d дн до срока годности %s', FORMAT_DATE('%d.%m.%Y', sellout_date),
                             expiry_margin_days, FORMAT_DATE('%d.%m.%Y', expiry_date)), NULL),
    IF(acceptance_flip, FORMAT('если принятое по приёмке (до %d шт) уже в остатке Ozon — решение другое: %s', acceptance_overlap_max,
                               IF(rec_alt > 0, FORMAT('отгрузить %d', rec_alt), 'ждать')), NULL)
  ]) AS x WHERE x IS NOT NULL), ' · ') AS reason,
  CASE channel WHEN 'WB' THEN 1 ELSE 2 END * 1000
    + IF(is_bundle, 500, 0)
    + CASE REGEXP_EXTRACT(card_sku, r'^EVT-([A-Z]+)-')
        WHEN 'FS' THEN 10 WHEN 'FC' THEN 20 WHEN 'FT' THEN 30 WHEN 'EP' THEN 40 WHEN 'HC' THEN 50 ELSE 90 END AS sort_key,
  CURRENT_TIMESTAMP() AS computed_at
FROM flags;

-- СНЯТИЕ С ПАЛЛЕТ: по физическому компоненту.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE` AS
WITH plan AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN`),
phys AS (
  SELECT internal_sku, COALESCE(product_name_short, canonical_product_name) AS product_name
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` WHERE NOT is_bundle
),
solo AS (
  SELECT card_sku AS internal_sku,
         SUM(IF(channel = 'WB', rec_final, 0)) AS solo_wb_need, SUM(IF(channel = 'OZON', rec_final, 0)) AS solo_ozon_need
  FROM plan WHERE NOT is_bundle GROUP BY 1
),
bund AS (
  SELECT b.component_sku AS internal_sku, SUM(p.rec_final * b.component_qty) AS bundle_component_need
  FROM plan p JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = p.card_sku AND b.is_bundle
  WHERE p.is_bundle GROUP BY 1
),
fbs AS (   -- резерв FBS, который ещё предстоит собрать (0, пока FBS выключен)
  SELECT b.component_sku AS internal_sku, SUM(l.fbs_reserve_units * b.component_qty) AS fbs_component_need
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS` l
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = l.internal_sku
  WHERE l.effective_from <= CURRENT_DATE('Europe/Moscow') AND (l.effective_to IS NULL OR l.effective_to >= CURRENT_DATE('Europe/Moscow'))
  GROUP BY 1
),
bal AS (
  SELECT internal_sku,
         SUM(IF(state = 'AVAILABLE' AND location = 'SHELF', units, 0)) AS shelf_available,
         SUM(IF(state = 'AVAILABLE' AND location = 'PALLET', units, 0)) AS pallet_available,
         SUM(IF(state = 'RESERVED' AND location = 'PALLET', units, 0)) AS reserved_on_pallet
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY 1
),
unconf AS (
  SELECT internal_sku, SUM(api_unconfirmed_units) AS unconf
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON` GROUP BY 1
),
j AS (
  SELECT ph.internal_sku, ph.product_name,
         IFNULL(s.solo_wb_need, 0) AS solo_wb_need, IFNULL(s.solo_ozon_need, 0) AS solo_ozon_need,
         IFNULL(bu.bundle_component_need, 0) AS bundle_component_need, IFNULL(f.fbs_component_need, 0) AS fbs_component_need,
         -- свободно: резерв Ozon из API без отметки ФФ вычитается сначала с полки, затем с паллет
         GREATEST(IFNULL(b.shelf_available, 0) - IFNULL(u.unconf, 0), 0) AS shelf_free,
         IFNULL(b.pallet_available, 0) - GREATEST(IFNULL(u.unconf, 0) - IFNULL(b.shelf_available, 0), 0) AS pallet_free,
         IFNULL(b.reserved_on_pallet, 0) AS reserved_on_pallet_to_pick
  FROM phys ph
  LEFT JOIN solo s USING (internal_sku) LEFT JOIN bund bu USING (internal_sku)
  LEFT JOIN fbs f USING (internal_sku) LEFT JOIN bal b USING (internal_sku) LEFT JOIN unconf u USING (internal_sku)
)
SELECT j.*,
       solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need AS total_physical_demand,
       LEAST(GREATEST(solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need - shelf_free, 0), GREATEST(pallet_free, 0)) AS to_pick_from_pallet,
       GREATEST(solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need - shelf_free - GREATEST(pallet_free, 0), 0) AS shortage,
       shelf_free + LEAST(GREATEST(solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need - shelf_free, 0), GREATEST(pallet_free, 0))
         - (solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need) AS shelf_after_pick,
       GREATEST(pallet_free, 0) - LEAST(GREATEST(solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need - shelf_free, 0), GREATEST(pallet_free, 0)) AS pallet_after_pick,
       LEAST(GREATEST(solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need - shelf_free, 0), GREATEST(pallet_free, 0))
         + reserved_on_pallet_to_pick AS pick_total_with_reserved,
       shelf_free + GREATEST(pallet_free, 0) - (solo_wb_need + solo_ozon_need + bundle_component_need + fbs_component_need) AS free_after_plan,
       CURRENT_TIMESTAMP() AS computed_at
FROM j;

-- СБОРКА НАБОРОВ: сколько собрать сейчас и из чего.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BUNDLE_PRODUCTION` AS
WITH plan AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN`),
bundles AS (
  SELECT b.card_sku AS bundle_sku,
         STRING_AGG(CONCAT(REPLACE(b.component_sku, 'EVT-', ''), IF(b.component_qty > 1, CONCAT(' ×', CAST(b.component_qty AS STRING)), '')),
                    ' + ' ORDER BY b.component_sku) AS bom_text,
         COUNT(*) AS components
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b WHERE b.is_bundle GROUP BY 1
),
rec AS (
  SELECT card_sku AS bundle_sku,
         SUM(IF(channel = 'WB', rec_final, 0)) AS ship_wb, SUM(IF(channel = 'OZON', rec_final, 0)) AS ship_ozon,
         SUM(IF(channel = 'WB', daily_plan, 0)) AS plan_day_wb, SUM(IF(channel = 'OZON', daily_plan, 0)) AS plan_day_ozon,
         STRING_AGG(IF(status_code IN ('REVIEW', 'FF LIMIT'), CONCAT(channel, ': ', status_label), NULL), '; ') AS review_note
  FROM plan WHERE is_bundle GROUP BY 1
),
fbs AS (
  SELECT internal_sku AS bundle_sku, SUM(fbs_reserve_units) AS fbs_reserve
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`
  WHERE effective_from <= CURRENT_DATE('Europe/Moscow') AND (effective_to IS NULL OR effective_to >= CURRENT_DATE('Europe/Moscow'))
  GROUP BY 1
),
reserved AS (   -- наборы в отгрузках, уже зарезервированных на ФФ (компоненты в резерве, набор ещё не собран)
  SELECT sl.card_sku AS bundle_sku, SUM(sl.qty_cards) AS reserved_cards
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE` sl
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` sh USING (shipment_id)
  WHERE sh.status = 'RESERVED' AND sl.is_bundle GROUP BY 1
),
built_reserved AS (
  SELECT bb.bundle_sku, SUM(IF(bb.event = 'BUILD', bb.qty_bundles, -bb.qty_bundles)) AS built
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_BUNDLE_BUILD` bb
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` sh ON sh.shipment_id = bb.shipment_id AND sh.status = 'RESERVED'
  WHERE bb.purpose = 'FBO_SHIPMENT' GROUP BY 1
),
assembled AS (   -- уже собранные наборы на ФФ: FBO (под отгрузки) и FBS
  SELECT container AS bundle_sku,
         SUM(IF(state = 'ASSEMBLED', units, 0)) AS assembled_units, SUM(IF(state = 'FBS_READY', units, 0)) AS fbs_ready_units
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE container IS NOT NULL GROUP BY 1
),
assembled_sets AS (
  SELECT a.bundle_sku,
         DIV(a.assembled_units, (SELECT SUM(component_qty) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` x WHERE x.card_sku = a.bundle_sku)) AS assembled_sets,
         DIV(a.fbs_ready_units, (SELECT SUM(component_qty) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` x WHERE x.card_sku = a.bundle_sku)) AS fbs_ready_sets
  FROM assembled a
),
pick AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE`),
after_plan AS (   -- лимитирующий компонент: меньше всего наборов остаётся после всего плана
  SELECT b.card_sku AS bundle_sku,
         MIN(DIV(GREATEST(p.free_after_plan, 0), b.component_qty)) AS capacity_after_plan,
         ARRAY_AGG(b.component_sku ORDER BY DIV(GREATEST(p.free_after_plan, 0), b.component_qty), b.component_sku LIMIT 1)[OFFSET(0)] AS limiting_after_plan
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b JOIN pick p ON p.internal_sku = b.component_sku
  WHERE b.is_bundle GROUP BY 1
)
SELECT bd.bundle_sku, COALESCE(pm.product_name_short, pm.canonical_product_name) AS product_name, bd.bom_text, bd.components,
       c.capacity_alone, c.capacity_alone_shelf, c.limiting_component AS limiting_component_now, c.shares_components_with,
       IFNULL(r.plan_day_wb, 0) AS plan_day_wb, IFNULL(r.plan_day_ozon, 0) AS plan_day_ozon,
       IFNULL(r.ship_wb, 0) AS ship_wb, IFNULL(r.ship_ozon, 0) AS ship_ozon,
       IFNULL(f.fbs_reserve, 0) AS fbs_reserve,
       IFNULL(s.assembled_sets, 0) AS assembled_sets, IFNULL(s.fbs_ready_sets, 0) AS fbs_ready_sets,
       IFNULL(rs.reserved_cards, 0) AS reserved_cards, IFNULL(br.built, 0) AS built_for_reserved,
       GREATEST(IFNULL(rs.reserved_cards, 0) - IFNULL(br.built, 0), 0) AS to_assemble_reserved,
       IFNULL(r.ship_wb, 0) + IFNULL(r.ship_ozon, 0) AS to_assemble_new,
       GREATEST(IFNULL(f.fbs_reserve, 0) - IFNULL(s.fbs_ready_sets, 0), 0) AS to_assemble_fbs,
       GREATEST(IFNULL(rs.reserved_cards, 0) - IFNULL(br.built, 0), 0) + IFNULL(r.ship_wb, 0) + IFNULL(r.ship_ozon, 0)
         + GREATEST(IFNULL(f.fbs_reserve, 0) - IFNULL(s.fbs_ready_sets, 0), 0) AS to_assemble_now,
       ap.capacity_after_plan, ap.limiting_after_plan, r.review_note,
       CURRENT_TIMESTAMP() AS computed_at
FROM bundles bd
JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` pm ON pm.internal_sku = bd.bundle_sku
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_BUNDLE_CAPACITY` c ON c.bundle_sku = bd.bundle_sku
LEFT JOIN rec r ON r.bundle_sku = bd.bundle_sku
LEFT JOIN fbs f ON f.bundle_sku = bd.bundle_sku
LEFT JOIN assembled_sets s ON s.bundle_sku = bd.bundle_sku
LEFT JOIN reserved rs ON rs.bundle_sku = bd.bundle_sku
LEFT JOIN built_reserved br ON br.bundle_sku = bd.bundle_sku
LEFT JOIN after_plan ap ON ap.bundle_sku = bd.bundle_sku;

-- Раскладка «СОБРАТЬ СЕЙЧАС» по компонентам.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BUNDLE_BOM_EXPANSION` AS
SELECT bp.bundle_sku, bp.product_name, b.component_sku, b.component_qty,
       bp.to_assemble_now, bp.to_assemble_reserved, bp.to_assemble_new, bp.to_assemble_fbs,
       bp.to_assemble_now * b.component_qty AS units_total,
       bp.to_assemble_reserved * b.component_qty AS units_from_reserved,   -- уже в резерве отгрузок
       (bp.to_assemble_new + bp.to_assemble_fbs) * b.component_qty AS units_from_free  -- берутся из свободного пула
FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BUNDLE_PRODUCTION` bp
JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = bp.bundle_sku AND b.is_bundle
WHERE bp.to_assemble_now > 0;

-- ФФ: физическое состояние по SKU (источник правды — журнал evetis_ops).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_STOCK_SHEET` AS
WITH b AS (
  SELECT internal_sku,
    SUM(IF(state = 'AVAILABLE' AND location = 'PALLET', units, 0)) AS pallet_free,
    SUM(IF(state = 'AVAILABLE' AND location = 'SHELF', units, 0)) AS shelf_free,
    SUM(IF(state = 'RESERVED' AND channel = 'WB', units, 0)) AS reserved_wb,
    SUM(IF(state = 'RESERVED' AND channel = 'OZON', units, 0)) AS reserved_ozon,
    SUM(IF(state = 'RESERVED' AND channel = 'OZON' AND location = 'PALLET', units, 0)) AS reserved_ozon_on_pallet,
    SUM(IF(state = 'RESERVED' AND channel = 'B2B', units, 0)) AS reserved_b2b,
    SUM(IF(state = 'ASSEMBLED', units, 0)) AS assembled,
    SUM(IF(state = 'FBS_READY', units, 0)) AS fbs_ready,
    SUM(IF(on_ff, units, 0)) AS total_physical
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY 1
),
s AS (
  SELECT internal_sku,
    SUM(IF(state = 'SHIPPED_UNCONFIRMED', units, 0)) AS shipped_unconfirmed,
    SUM(IF(state = 'RESERVED_UNCONFIRMED_API', units, 0)) AS api_reserved_unconfirmed,
    SUM(IF(side = 'PIPELINE' AND state = 'IN_ACCEPTANCE', units, 0)) AS ozon_in_acceptance,
    SUM(IF(side = 'PIPELINE' AND state = 'IN_TRANSIT', units, 0)) AS in_transit_api,
    SUM(IF(side = 'MARKETPLACE' AND channel = 'OZON', units, 0)) AS on_ozon,
    SUM(IF(side = 'MARKETPLACE' AND channel = 'WB', units, 0)) AS on_wb
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_STATE` GROUP BY 1
)
SELECT p.internal_sku, COALESCE(p.product_name_short, p.canonical_product_name) AS product_name,
       IFNULL(b.pallet_free, 0) AS pallet_free, IFNULL(b.shelf_free, 0) AS shelf_free,
       IFNULL(b.reserved_wb, 0) AS reserved_wb, IFNULL(b.reserved_ozon, 0) AS reserved_ozon,
       IFNULL(b.reserved_ozon_on_pallet, 0) AS reserved_ozon_on_pallet, IFNULL(b.reserved_b2b, 0) AS reserved_b2b,
       IFNULL(b.assembled, 0) AS assembled, IFNULL(b.fbs_ready, 0) AS fbs_ready,
       IFNULL(s.shipped_unconfirmed, 0) AS shipped_unconfirmed,
       IFNULL(b.total_physical, 0) AS total_physical,
       IFNULL(s.api_reserved_unconfirmed, 0) AS api_reserved_unconfirmed,
       IFNULL(s.ozon_in_acceptance, 0) AS ozon_in_acceptance, IFNULL(s.in_transit_api, 0) AS in_transit_api,
       IFNULL(s.on_ozon, 0) AS on_ozon, IFNULL(s.on_wb, 0) AS on_wb,
       CURRENT_TIMESTAMP() AS computed_at
FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p
LEFT JOIN b USING (internal_sku) LEFT JOIN s USING (internal_sku)
WHERE NOT p.is_bundle;

-- ОТГРУЗКИ: журнал ФФ и API площадок, по отгрузке.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SHIPMENTS_SHEET` AS
WITH r AS (
  SELECT order_number, ANY_VALUE(api_state) AS api_state, ANY_VALUE(recon_class) AS recon_class, ANY_VALUE(shipment_id) AS shipment_id,
         SUM(api_units_planned) AS physical_units, SUM(reserved_on_ff_units) AS reserved_on_ff_units,
         SUM(pipeline_units) AS pipeline_units, SUM(handed_over_units) AS handed_over_units,
         LOGICAL_OR(in_acceptance) AS in_acceptance, LOGICAL_OR(accepted_unreported) AS accepted_unreported,
         ANY_VALUE(created_at) AS created_at
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON` GROUP BY 1
),
cards AS (
  SELECT order_number, SUM(cards) AS cards, SUM(IF(is_bundle, cards, 0)) AS bundle_cards,
         ANY_VALUE(dropoff) AS dropoff, ANY_VALUE(planned_from) AS planned_from
  FROM (SELECT order_number, supply_id, ozon_sku, ANY_VALUE(cards_planned) AS cards, LOGICAL_OR(is_bundle) AS is_bundle,
               ANY_VALUE(dropoff_warehouse_name) AS dropoff, ANY_VALUE(planned_arrival_from) AS planned_from
        FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` GROUP BY 1, 2, 3)
  GROUP BY 1
),
res_loc AS (
  SELECT doc, SUM(IF(location = 'SHELF', units, 0)) AS reserved_shelf, SUM(IF(location = 'PALLET', units, 0)) AS reserved_pallet
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE state = 'RESERVED' GROUP BY 1
),
ev AS (SELECT order_number, source_ref FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE`)
SELECT 'OZON' AS channel, r.order_number, r.api_state, sh.status AS ledger_status, r.recon_class,
       CASE r.recon_class
         WHEN 'RESERVED_ON_FF' THEN 'RESERVED_OZON_ON_FF — на ФФ, резерв'
         WHEN 'IN_TRANSIT_API' THEN IF(r.in_acceptance, 'IN_ACCEPTANCE — на приёмке Ozon', 'IN_TRANSIT — в пути')
         WHEN 'HANDED_OVER' THEN 'ON_OZON — принято Ozon'
         WHEN 'SHIPPED_UNCONFIRMED' THEN 'SHIPPED UNCONFIRMED — уехало, Ozon не видит'
         WHEN 'API_RESERVATION_UNCONFIRMED' THEN 'резерв в Ozon без отметки ФФ'
         WHEN 'API_ONLY' THEN 'только в API Ozon'
         ELSE r.recon_class END AS stage,
       c.cards, c.bundle_cards, r.physical_units, r.reserved_on_ff_units,
       IFNULL(rl.reserved_shelf, 0) AS reserved_shelf, IFNULL(rl.reserved_pallet, 0) AS reserved_pallet,
       r.pipeline_units, r.handed_over_units, r.accepted_unreported,
       c.dropoff, DATE(c.planned_from, 'Europe/Moscow') AS planned_date, DATE(r.created_at, 'Europe/Moscow') AS created_date,
       r.shipment_id, e.source_ref AS evidence,
       CURRENT_TIMESTAMP() AS computed_at
FROM r
LEFT JOIN cards c USING (order_number)
LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` sh ON sh.shipment_id = r.shipment_id
LEFT JOIN res_loc rl ON rl.doc = r.shipment_id
LEFT JOIN ev e USING (order_number);

-- ТЗ ДЛЯ ФУЛФИЛМЕНТА: что физически сделать на складе по утверждённому плану (только чтение).
-- Ничего не решает: пересобирает уже посчитанные снятие с паллет, сборку наборов и расход компонентов
-- в вид, который владелец отдаёт складу как задание.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_TASK` AS
WITH pick AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE`),
res AS (
  SELECT internal_sku,
         SUM(IF(state = 'RESERVED', units, 0)) AS reserved_units_total,
         SUM(IF(state = 'RESERVED' AND location = 'PALLET', units, 0)) AS reserved_units_on_pallet
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY 1
)
SELECT
  k.internal_sku, k.product_name,
  -- 1. снять с паллет на полку
  k.to_pick_from_pallet        AS move_pallet_to_shelf_new,
  k.reserved_on_pallet_to_pick AS move_pallet_to_shelf_reserved,
  k.pick_total_with_reserved   AS move_pallet_to_shelf_total,
  -- 2. на что уйдут эти единицы
  k.solo_wb_need               AS solo_units_wb,
  k.solo_ozon_need             AS solo_units_ozon,
  k.bundle_component_need      AS bundle_component_units,
  k.fbs_component_need         AS fbs_component_units,
  k.total_physical_demand      AS total_physical_units,
  -- 3. уже зарезервировано под существующие отгрузки
  IFNULL(r.reserved_units_total, 0)     AS reserved_units_already,
  IFNULL(r.reserved_units_on_pallet, 0) AS reserved_units_on_pallet,
  -- 4. что останется на ФФ после операции
  k.shelf_after_pick, k.pallet_after_pick, k.free_after_plan AS free_ff_after_operation,
  k.shortage,
  CURRENT_TIMESTAMP() AS computed_at
FROM pick k LEFT JOIN res r ON r.internal_sku = k.internal_sku;
