-- ============================================================================
-- EVETIS · Stage B · Operations backend · 05 — инварианты (только чтение)
-- Каждый блок `-- @@TEST <id> <описание>` — отдельный скрипт; PASS = выполнился без ошибки.
-- Запускаются на production после посева и на песочнице (тот же код, префикс ZZTEST_).
-- ============================================================================

-- @@TEST I01 открывающий итог ФФ = сумма всех состояний ФФ = авторитетный срез
ASSERT (
  SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE on_ff
) = (
  SELECT SUM(shelf_units + pallet_units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_FF_SNAPSHOT`
  WHERE snapshot_date = DATE '2026-09-09' AND in_ledger
) AS 'I01: итог ФФ в журнале не равен авторитетному срезу 09.09';

-- @@TEST I02 по каждому SKU: состояния ФФ = физический баланс; разложение = сверка посева
ASSERT (
  WITH led AS (
    SELECT internal_sku,
           SUM(IF(on_ff, units, 0)) AS ff,
           SUM(IF(state = 'AVAILABLE' AND location = 'PALLET', units, 0)) AS av_pallet,
           SUM(IF(state = 'AVAILABLE' AND location = 'SHELF', units, 0)) AS av_shelf,
           SUM(IF(state = 'RESERVED' AND channel = 'OZON', units, 0)) AS res_ozon,
           SUM(IF(state = 'SHIPPED', units, 0)) AS shipped
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY internal_sku)
  SELECT COUNT(*)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON` r
  LEFT JOIN led l USING (internal_sku)
  WHERE IFNULL(l.ff, 0) != r.open_ff_total
     OR IFNULL(l.av_pallet, 0) != r.open_available_pallet
     OR IFNULL(l.av_shelf, 0) != r.open_available_shelf
     OR IFNULL(l.res_ozon, 0) != r.reserved_ozon_units
     OR IFNULL(l.shipped, 0) != r.shipped_before_opening_units
) = 0 AS 'I02: разложение по SKU не совпадает со сверкой посева';

-- @@TEST I03 ни одного отрицательного бакета
ASSERT (
  SELECT COUNTIF(units < 0) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
) = 0 AS 'I03: есть отрицательный бакет';

-- @@TEST I04 одна единица — одно состояние: сохранение по SKU и уникальность бакетов
ASSERT (
  WITH ext AS (   -- приход извне минус выбытие наружу
    SELECT internal_sku,
           SUM(IF(from_state IS NULL, qty, 0)) - SUM(IF(to_state IS NULL, qty, 0)) AS net_external
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` GROUP BY internal_sku),
  bal AS (
    SELECT internal_sku, SUM(units) AS all_states
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY internal_sku)
  SELECT COUNT(*) FROM ext FULL OUTER JOIN bal USING (internal_sku)
  WHERE IFNULL(ext.net_external, 0) != IFNULL(bal.all_states, 0)
) = 0 AS 'I04: сумма по всем состояниям SKU не равна чистому приходу — единица посчитана дважды или потеряна';
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT internal_sku, state, location, channel, container, doc
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
    GROUP BY 1, 2, 3, 4, 5, 6 HAVING COUNT(*) > 1)
) = 0 AS 'I04: бакет встречается дважды';
ASSERT (
  SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
  WHERE qty <= 0 OR (from_state IS NULL AND to_state IS NULL)
     OR (IFNULL(from_state, '~') = IFNULL(to_state, '~') AND IFNULL(from_location, '~') = IFNULL(to_location, '~')
         AND IFNULL(from_channel, '~') = IFNULL(to_channel, '~') AND IFNULL(from_container, '~') = IFNULL(to_container, '~')
         AND IFNULL(from_doc, '~') = IFNULL(to_doc, '~'))
) = 0 AS 'I04: движение без количества, без сторон или в тот же бакет';
ASSERT (
  SELECT COUNT(*) - COUNT(DISTINCT movement_id) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
) = 0 AS 'I04: movement_id не уникален';

-- @@TEST I05 целостность наборов: единицы компонентов в каждом контейнере кратны BOM одинаково
ASSERT (
  WITH c AS (
    SELECT b.container, b.state, IFNULL(b.doc, '') AS doc, IFNULL(b.channel, '') AS ch, b.internal_sku, b.units, bom.component_qty
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` b
    JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` bom ON bom.card_sku = b.container AND bom.component_sku = b.internal_sku
    WHERE b.container IS NOT NULL AND b.units != 0),
  k AS (
    SELECT container, state, doc, ch, MIN(SAFE_DIVIDE(units, component_qty)) AS kmin, MAX(SAFE_DIVIDE(units, component_qty)) AS kmax,
           COUNT(*) AS comps, COUNTIF(MOD(units, component_qty) != 0) AS non_integral
    FROM c GROUP BY 1, 2, 3, 4),
  expected AS (SELECT card_sku, COUNT(*) AS comps FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` WHERE is_bundle GROUP BY 1)
  SELECT COUNT(*) FROM k JOIN expected e ON e.card_sku = k.container
  WHERE k.kmin != k.kmax OR k.non_integral > 0 OR k.comps != e.comps
) = 0 AS 'I05: собранный набор не целый — компоненты не в пропорции BOM';

-- @@TEST I10 Ozon: у каждого заказа один класс, резерв и «в пути» не пересекаются, журнал = API
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT order_number FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
    GROUP BY order_number HAVING COUNT(DISTINCT recon_class) > 1)
) = 0 AS 'I10: заказ Ozon получил больше одного класса';
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT order_number FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
    GROUP BY order_number
    HAVING (IF(SUM(reserved_on_ff_units) > 0, 1, 0) + IF(SUM(pipeline_units) > 0, 1, 0)
          + IF(SUM(handed_over_units) > 0, 1, 0) + IF(SUM(api_unconfirmed_units) > 0, 1, 0)) > 1)
) = 0 AS 'I10: единицы заказа засчитаны в двух разрезах сразу';
ASSERT (
  SELECT COUNTIF(ledger_matches_api IS FALSE OR recon_class = 'UNCLASSIFIED' OR unmapped_lines > 0)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
) = 0 AS 'I10: журнал и API расходятся по объёму заказа, либо заказ не классифицирован / не сопоставлен';
ASSERT (
  SELECT COUNTIF(ledger_behind_api) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
) = 0 AS 'I10: журнал отстал от API — резерв на ФФ, а Ozon видит поставку уехавшей или отменённой';
ASSERT (
  -- резерв Ozon в журнале = сумма резерва по сверке заказов (единый источник)
  (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE state IN ('RESERVED', 'ASSEMBLED') AND channel = 'OZON')
  = (SELECT SUM(reserved_on_ff_units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`)
) AS 'I10: резерв Ozon в журнале не равен резерву по сверке заказов';

-- @@TEST I11 изоляция: объектов Stage B нет вне evetis_ops
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT table_name AS n FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.TABLES`
    UNION ALL SELECT table_name FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.TABLES`
    UNION ALL SELECT table_name FROM `project-fa311fc0-4d87-4781-986.wb_raw.INFORMATION_SCHEMA.TABLES`
    UNION ALL SELECT routine_name FROM `project-fa311fc0-4d87-4781-986.wb_mart.INFORMATION_SCHEMA.ROUTINES`
    UNION ALL SELECT routine_name FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INFORMATION_SCHEMA.ROUTINES`)
  WHERE n IN ('OPS_CONFIG', 'REF_SKU_LOGISTICS', 'REF_CHANNEL_SHIPPING', 'CT_OPENING_FF_SNAPSHOT', 'CT_OPENING_OZON_EVIDENCE', 'CT_STOCK_MOVEMENT',
              'CT_SHIPMENT', 'CT_SHIPMENT_LINE', 'CT_BUNDLE_BUILD', 'CT_OPS_REQUEST_LOG', 'V_OPS_BOM', 'V_OPS_OZON_ORDER_UNITS',
              'V_CT_STOCK_BALANCE', 'V_CT_SHIPMENT_CURRENT', 'V_CT_OZON_ORDER_RECON', 'V_CT_STOCK_STATE',
              'V_CT_BUNDLE_CAPACITY', 'V_CT_SEED_RECON')
     OR STARTS_WITH(n, 'sp_ops_') OR STARTS_WITH(n, 'ZZTEST_')
) = 0 AS 'I11: объект Stage B найден вне evetis_ops';

-- @@TEST I13 сверка посева: все тождества 369 выполняются
ASSERT (
  SELECT COUNTIF(NOT (id_a_seed369 AND id_b_shelf_rule AND id_c_letter AND id_p_pallet))
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON`
) = 0 AS 'I13: тождество сверки посева нарушено';
ASSERT (
  (SELECT SUM(seed369) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON`) = 369
  AND (SELECT SUM(shipped_before_opening_units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON`) = 178
  AND (SELECT SUM(seed_deducted_from_shelf) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON`) = 191
  AND (SELECT SUM(reserved_ozon_units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON`) = 314
  AND (SELECT SUM(reserved_from_pallet) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SEED_RECON`) = 123
) AS 'I13: итоги сверки 369 = 178 + 191, 314 = 191 + 123 не сходятся';

-- @@TEST I14 BOM evetis_ops совпадает с BOM Control Tower
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT card_sku, component_sku, component_qty FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM`
    EXCEPT DISTINCT
    SELECT card_sku, component_sku, component_qty FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`)
) + (
  SELECT COUNT(*) FROM (
    SELECT card_sku, component_sku, component_qty FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BOM_CURRENT`
    EXCEPT DISTINCT
    SELECT card_sku, component_sku, component_qty FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM`)
) = 0 AS 'I14: BOM evetis_ops расходится с BOM Control Tower';

-- @@TEST I15 мощность наборов не аддитивна: общий компонент не выдерживает суммы мощностей
ASSERT (
  WITH bom AS (SELECT card_sku, component_sku, component_qty FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` WHERE is_bundle),
  demand AS (
    SELECT bom.component_sku, SUM(c.capacity_alone * bom.component_qty) AS demand_if_all_built, COUNT(*) AS bundles
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_BUNDLE_CAPACITY` c JOIN bom ON bom.card_sku = c.bundle_sku
    GROUP BY 1),
  free AS (SELECT internal_sku, SUM(IF(state = 'AVAILABLE', units, 0)) AS available FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY 1)
  SELECT COUNT(*) FROM demand d JOIN free f ON f.internal_sku = d.component_sku
  WHERE d.bundles > 1 AND d.demand_if_all_built > f.available
) > 0 AS 'I15: не найдено ни одного общего компонента, где сумма мощностей превышает остаток — проверка не доказательна';
ASSERT (
  SELECT COUNTIF(capacity_is_independent AND shares_components_with IS NOT NULL)
       + COUNTIF(NOT capacity_is_independent AND shares_components_with IS NULL)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_BUNDLE_CAPACITY`
) = 0 AS 'I15: флаг независимости мощности противоречит списку общих компонентов';

-- @@TEST I16 настройки логистики: версии одного SKU × канал не пересекаются
ASSERT (
  SELECT COUNT(*)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS` b
    ON a.internal_sku = b.internal_sku AND a.channel = b.channel AND a.effective_from < b.effective_from
   AND (a.effective_to IS NULL OR a.effective_to >= b.effective_from)
) = 0 AS 'I16: версии настроек логистики пересекаются';
ASSERT (
  SELECT COUNT(*)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING` a
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING` b
    ON a.channel = b.channel AND a.shipping_method = b.shipping_method AND a.effective_from < b.effective_from
   AND (a.effective_to IS NULL OR a.effective_to >= b.effective_from)
) = 0 AS 'I16: версии настроек канала пересекаются';

-- @@TEST I18 Logistics Master (C0): полнота, правила владельца, заводской короб только как FACT
ASSERT (
  -- у каждой текущей карточки канала ровно одна действующая строка настроек, и лишних строк нет
  WITH cards AS (
    SELECT DISTINCT internal_sku, UPPER(marketplace) AS channel
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
    WHERE is_current AND UPPER(marketplace) IN ('WB', 'OZON')),
  cur AS (
    SELECT internal_sku, channel, COUNT(*) AS n
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`
    WHERE effective_from <= CURRENT_DATE('Europe/Moscow') AND (effective_to IS NULL OR effective_to >= CURRENT_DATE('Europe/Moscow'))
    GROUP BY 1, 2)
  SELECT COUNT(*) FROM cards FULL OUTER JOIN cur USING (internal_sku, channel) WHERE IFNULL(cur.n, 0) != 1 OR cards.internal_sku IS NULL
) = 0 AS 'I18: не у каждой карточки канала ровно одна действующая строка логистики';
ASSERT (
  SELECT COUNTIF(factory_carton_qty IS NOT NULL AND IFNULL(JSON_VALUE(field_provenance, '$.factory_carton_qty.class'), '') != 'FACT')
       + COUNTIF(shipment_multiple IS NULL OR shipment_multiple <= 0)
       + COUNTIF(min_shipment_units IS NULL OR min_shipment_units != shipment_multiple)
       + COUNTIF(channel = 'OZON' AND (shipment_multiple_low_demand IS NULL OR shipment_multiple_low_demand <= 0
                                       OR shipment_multiple_low_demand > shipment_multiple))
       + COUNTIF(channel != 'OZON' AND shipment_multiple_low_demand IS NOT NULL)
       + COUNTIF(fbs_reserve_units IS NULL OR fbs_reserve_units < 0)
       + COUNTIF(unit_weight_kg IS NOT NULL AND IFNULL(JSON_VALUE(field_provenance, '$.unit_weight_kg.class'), '') = 'OWNER_REQUIRED')
       + COUNTIF(field_provenance IS NULL OR SAFE.PARSE_JSON(field_provenance) IS NULL)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`
) = 0 AS 'I18: нарушено правило C0 (короб не FACT, кратность, минимум, кратность низкого спроса, FBS, происхождение)';
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT channel FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING`
    WHERE is_planning_default AND effective_from <= CURRENT_DATE('Europe/Moscow')
      AND (effective_to IS NULL OR effective_to >= CURRENT_DATE('Europe/Moscow'))
    GROUP BY channel HAVING COUNT(*) != 1)
) + (
  SELECT 3 - COUNT(DISTINCT channel) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_CHANNEL_SHIPPING`
  WHERE is_planning_default AND channel IN ('WB', 'OZON', 'FF')
) = 0 AS 'I18: у канала WB / OZON / FF нет ровно одного действующего способа по умолчанию';

-- @@TEST I17 состояние: FBO- и FBS-наборы различимы, остатки площадок не продублированы в журнал
ASSERT (
  SELECT COUNTIF(state = 'ASSEMBLED' AND IFNULL(purpose, '') != 'FBO') + COUNTIF(state = 'FBS_READY' AND IFNULL(purpose, '') != 'FBS')
       + COUNTIF(side = 'MARKETPLACE' AND source != 'API') + COUNTIF(side = 'FF' AND state NOT IN ('RESERVED_UNCONFIRMED_API') AND source != 'LEDGER')
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_STATE`
) = 0 AS 'I17: назначение набора или источник состояния указан неверно';
ASSERT (
  SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
  WHERE from_state IN ('IN_TRANSIT', 'IN_ACCEPTANCE', 'ON_MARKETPLACE') OR to_state IN ('IN_TRANSIT', 'IN_ACCEPTANCE', 'ON_MARKETPLACE')
) = 0 AS 'I17: состояния площадки записаны в журнал ФФ';
ASSERT (
  -- в пути по API = план − принятое (когда Ozon его сообщил); на приёмке без сообщённого принятого — флаг разрыва
  SELECT COUNTIF(recon_class = 'IN_TRANSIT_API' AND pipeline_units != api_units_planned - IFNULL(api_units_accepted, 0))
       + COUNTIF(accepted_unreported AND NOT in_acceptance)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON`
) = 0 AS 'I17: единицы в пути по API не очищены от принятого Ozon или флаг приёмки неверен';

-- @@TEST I19 План поставок C1: каждая строка логистики ровно одна; решения по правилам владельца
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT channel, card_sku, COUNT(*) n FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN` GROUP BY 1, 2 HAVING n != 1)
) + ABS((SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN`)
      - (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.REF_SKU_LOGISTICS`
         WHERE effective_from <= CURRENT_DATE('Europe/Moscow') AND (effective_to IS NULL OR effective_to >= CURRENT_DATE('Europe/Moscow'))))
  = 0 AS 'I19: строка плана не соответствует строке логистики один к одному';
ASSERT (
  SELECT COUNTIF(rec_final < 0 OR rec_final > rec_proposed)
       + COUNTIF(rec_final > 0 AND MOD(rec_final, multiple_applied) != 0)
       + COUNTIF(rule = 'UP_TO_MULTIPLE' AND (rec_proposed < need_math OR rec_proposed - need_math >= shipment_multiple))
       + COUNTIF(rule = 'LOW_DEMAND_MULTIPLE' AND (channel != 'OZON' OR need_math >= shipment_multiple OR need_math < shipment_multiple_low_demand))
       + COUNTIF(rule = 'WAIT' AND (rec_proposed != 0 OR NOT wait_ok))
       + COUNTIF(rule = 'SAFETY_MIN' AND (wait_ok OR rec_proposed != min_shipment_units))
       + COUNTIF(rule IN ('ENOUGH', 'NO_PLAN') AND rec_proposed != 0)
       + COUNTIF(status_code = 'REVIEW' AND NOT (acceptance_flip OR review_overstock OR review_expiry))
       + COUNTIF(review_overstock AND status_code NOT IN ('REVIEW'))
       + COUNTIF(projected_at_arrival_raw != position - demand_until_arrival)
       + COUNTIF(need_math != GREATEST(target_at_arrival - GREATEST(projected_at_arrival_raw, 0), 0))
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN`
) = 0 AS 'I19: округление / ЖДАТЬ / страховой / ворота ПРОВЕРИТЬ нарушают правила C1';

-- @@TEST I20 Общий пул ФФ: WB и Ozon не претендуют на одну и ту же единицу; нет отрицательного остатка
ASSERT (
  SELECT COUNTIF(shortage > 0 OR shelf_after_pick < 0 OR pallet_after_pick < 0 OR free_after_plan < 0)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE`
) = 0 AS 'I20: план требует больше, чем свободно на ФФ, или остаток после снятия отрицательный';
ASSERT (
  WITH dem AS (
    SELECT b.component_sku, SUM(p.rec_final * b.component_qty) AS demand
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN` p
    JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = p.card_sku GROUP BY 1),
  free AS (
    SELECT internal_sku AS component_sku, shelf_free + GREATEST(pallet_free, 0) AS free
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE`)
  SELECT COUNT(*) FROM dem LEFT JOIN free USING (component_sku) WHERE dem.demand > IFNULL(free.free, 0)
) = 0 AS 'I20: сумма рекомендаций WB + Ozon по компоненту превышает свободный остаток ФФ';

-- @@TEST I21 Снятие с паллет сходится с рекомендациями: соло + компоненты наборов + FBS
ASSERT (
  WITH plan AS (
    SELECT b.component_sku AS internal_sku,
           SUM(IF(NOT p.is_bundle AND p.channel = 'WB', p.rec_final, 0)) AS solo_wb,
           SUM(IF(NOT p.is_bundle AND p.channel = 'OZON', p.rec_final, 0)) AS solo_oz,
           SUM(IF(p.is_bundle, p.rec_final * b.component_qty, 0)) AS bund
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN` p
    JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = p.card_sku GROUP BY 1),
  exp AS (
    SELECT component_sku AS internal_sku, SUM(units_from_free) AS from_free
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BUNDLE_BOM_EXPANSION` GROUP BY 1)
  SELECT COUNT(*)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE` k
  LEFT JOIN plan USING (internal_sku) LEFT JOIN exp USING (internal_sku)
  WHERE k.solo_wb_need != IFNULL(plan.solo_wb, 0) OR k.solo_ozon_need != IFNULL(plan.solo_oz, 0)
     OR k.bundle_component_need != IFNULL(plan.bund, 0)
     OR k.bundle_component_need + k.fbs_component_need != IFNULL(exp.from_free, 0)
     OR k.total_physical_demand != k.solo_wb_need + k.solo_ozon_need + k.bundle_component_need + k.fbs_component_need
     OR k.to_pick_from_pallet != GREATEST(k.total_physical_demand - k.shelf_free, 0)
) = 0 AS 'I21: снятие с паллет не сходится с рекомендациями соло + наборов + FBS';

-- @@TEST I22 Ozon: ON_OZON, IN_ACCEPTANCE, RESERVED_OZON_ON_FF раздельны; резерв на ФФ не считается остатком площадки
ASSERT (
  (SELECT SUM(reserved_on_ff) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN` WHERE channel = 'OZON')
  = (SELECT SUM(sl.qty_cards) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE` sl
     JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` sh USING (shipment_id)
     WHERE sh.channel = 'OZON' AND sh.status = 'RESERVED')
) AS 'I22: резерв Ozon на ФФ в плане не равен строкам зарезервированных отгрузок';
ASSERT (
  (SELECT SUM(reserved_ozon) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_STOCK_SHEET`)
  = (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE state = 'RESERVED' AND channel = 'OZON')
  AND (SELECT SUM(total_physical) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_STOCK_SHEET`)
      = (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE on_ff)
  AND (SELECT SUM(on_ozon) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_STOCK_SHEET`)
      = (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_STATE` WHERE side = 'MARKETPLACE' AND channel = 'OZON')
) AS 'I22: итоги листа ФФ не сходятся с журналом / резерв Ozon попал в остаток площадки';
ASSERT (
  SELECT COUNTIF(channel = 'WB' AND in_acceptance != 0)
       + COUNTIF(committed_inbound != in_transit + in_acceptance + reserved_on_ff + api_reserved_unconfirmed + shipped_unconfirmed)
       + COUNTIF(position != on_marketplace + committed_inbound)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN`
) = 0 AS 'I22: позиция запаса считает входящие не один раз или приёмка вне Ozon';

-- @@TEST I23 C1.1: единицы продажи ↔ физические единицы; ТЗ для ФФ сходится с планом
ASSERT (
  SELECT COUNTIF(rec_physical_units != rec_final * units_per_position)
       + COUNTIF(units_per_position <= 0)
       + COUNTIF(NOT is_bundle AND units_per_position != 1)
       + COUNTIF(demand_confidence IS NULL)
       + COUNTIF(aggressive_plan != (daily_plan > 0 AND (actual_daily_30d = 0 OR plan_actual_ratio > 2)))
       + COUNTIF(aggressive_plan AND demand_confidence != 'LOW')
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN`
) = 0 AS 'I23: физические единицы или основание спроса посчитаны неверно';
ASSERT (
  -- сумма рекомендаций в физических единицах = спрос, разложенный по BOM в снятии с паллет
  (SELECT SUM(rec_physical_units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_SUPPLY_PLAN`)
  = (SELECT SUM(total_physical_demand) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE`)
) AS 'I23: рекомендации в физических единицах не равны спросу в снятии с паллет';
ASSERT (
  SELECT COUNTIF(t.move_pallet_to_shelf_total != t.move_pallet_to_shelf_new + t.move_pallet_to_shelf_reserved)
       + COUNTIF(t.total_physical_units != t.solo_units_wb + t.solo_units_ozon + t.bundle_component_units + t.fbs_component_units)
       + COUNTIF(t.free_ff_after_operation < 0 OR t.shortage > 0)
       + COUNTIF(t.move_pallet_to_shelf_new != k.to_pick_from_pallet OR t.total_physical_units != k.total_physical_demand)
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_FF_TASK` t
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_PICK_FROM_STORAGE` k USING (internal_sku)
) = 0 AS 'I23: ТЗ для фулфилмента не сходится с планом снятия с паллет';
