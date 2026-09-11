-- ============================================================================
-- EVETIS · Stage B · Operations backend · 06 — тесты записи (ТОЛЬКО песочница)
-- Запускаются на копии объектов с префиксом ZZTEST_ внутри evetis_ops (tools/ops_deploy.py
-- переписывает имена). В production эти тесты не запускаются: там запись выключена.
-- Каждый блок `-- @@TEST <id> <описание>` — отдельный скрипт; PASS = выполнился без ошибки.
-- Каждый тест возвращает песочницу в исходное состояние (сторно), поэтому после серии
-- инварианты I01/I02 обязаны снова выполняться.
-- ============================================================================

-- @@TEST T21 посев идемпотентен по request_id и не выполняется второй раз
DECLARE n_mv INT64;
SET n_mv = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`);
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_seed_opening`('SEED-OPENING-2026-09-09', 'stage-b-test');
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`) = n_mv
  AS 'T21: повтор посева с тем же request_id создал движения';
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_seed_opening`('SEED-OPENING-SECOND', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: второй посев прошёл';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'журнал не пуст') THEN
    RAISE USING MESSAGE = CONCAT('T21: неожиданный отказ: ', @@error.message);
  END IF;
END;
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`) = n_mv
  AS 'T21: отказанный второй посев оставил движения';

-- @@TEST T20 сторно посева возвращает журнал к нулю (полный откат открытия)
DECLARE n_seed INT64;
SET n_seed = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'SEED-OPENING-2026-09-09');
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_seed_reverse`('TST-T20-SEEDREV', 'SEED-OPENING-2026-09-09', 'тест полного отката посева', 'stage-b-test');
ASSERT (SELECT COUNTIF(units != 0) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`) = 0
  AS 'T20: после сторно посева в журнале остались единицы';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'TST-T20-SEEDREV') = n_seed
  AS 'T20: сторнированы не все движения посева';
ASSERT (SELECT COUNTIF(status != 'CANCELLED') FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT`) = 0
  AS 'T20: отгрузки посева не отменены';
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_seed_reverse`('TST-T20-SEEDREV', 'SEED-OPENING-2026-09-09', 'повтор', 'stage-b-test');
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'TST-T20-SEEDREV') = n_seed
  AS 'T20: повтор сторно посева не идемпотентен';

-- @@TEST T05 сборка FBS списывает ровно компоненты по BOM
DECLARE v_b STRING DEFAULT 'EVT-SET-CHERRY-AMBER';
DECLARE v_before ARRAY<STRUCT<k STRING, u INT64>>;
DECLARE v_ff INT64;
DECLARE v_comps INT64;
SET v_comps = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` WHERE card_sku = v_b AND is_bundle);
SET v_before = ARRAY(SELECT AS STRUCT CONCAT(internal_sku, '|', state, '|', IFNULL(location, ''), '|', IFNULL(channel, ''), '|',
                                             IFNULL(container, ''), '|', IFNULL(doc, '')) AS k, units AS u
                     FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`);
SET v_ff = (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE on_ff);
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_bundle_build`('TST-T05-BUILD', v_b, 2, 'FBS_STOCK', NULL, CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` m
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = v_b AND b.component_sku = m.internal_sku AND b.is_bundle
        WHERE m.request_id = 'TST-T05-BUILD' AND m.qty = 2 * b.component_qty AND m.from_state = 'AVAILABLE'
          AND m.from_location = 'SHELF' AND m.to_state = 'FBS_READY' AND m.to_container = v_b) = v_comps
  AS 'T05: движения сборки не равны BOM × 2';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'TST-T05-BUILD') = v_comps
  AS 'T05: у сборки лишние движения';
ASSERT (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE on_ff) = v_ff
  AS 'T05: сборка изменила итог ФФ — это перемещение внутри ФФ';
ASSERT (
  WITH a AS (SELECT * FROM UNNEST(v_before)),
  b AS (SELECT CONCAT(internal_sku, '|', state, '|', IFNULL(location, ''), '|', IFNULL(channel, ''), '|',
                      IFNULL(container, ''), '|', IFNULL(doc, '')) AS k, units AS u
        FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`)
  SELECT COUNT(*) FROM a FULL OUTER JOIN b USING (k) WHERE IFNULL(a.u, 0) != IFNULL(b.u, 0)
) = 2 * v_comps AS 'T05: изменились посторонние бакеты';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_STATE`
        WHERE state = 'FBS_READY' AND container_sku = v_b AND purpose = 'FBS') = v_comps
  AS 'T05: FBS-набор не виден как FBS';

-- @@TEST T06 разборка набора возвращает ровно компоненты; повторное сторно запрещено
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T06-REV', 'TST-T05-BUILD', 'тест разборки', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
ASSERT (
  WITH pair AS (
    SELECT internal_sku, from_state AS st, from_location AS loc, from_channel AS ch, from_container AS ct, from_doc AS dc, -qty AS q
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id IN ('TST-T05-BUILD', 'TST-T06-REV') AND from_state IS NOT NULL
    UNION ALL
    SELECT internal_sku, to_state, to_location, to_channel, to_container, to_doc, qty
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id IN ('TST-T05-BUILD', 'TST-T06-REV') AND to_state IS NOT NULL)
  SELECT COUNT(*) FROM (SELECT internal_sku, st, loc, ch, ct, dc, SUM(q) AS s FROM pair GROUP BY 1, 2, 3, 4, 5, 6 HAVING s != 0)
) = 0 AS 'T06: сборка + разборка не дают ноль по каждому бакету';
ASSERT (SELECT IFNULL(SUM(units), 0) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
        WHERE state = 'FBS_READY' AND container = 'EVT-SET-CHERRY-AMBER') = 0 AS 'T06: набор не разобран';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_BUNDLE_BUILD`
        WHERE event = 'REVERSAL' AND request_id = 'TST-T06-REV') = 1 AS 'T06: нет события разборки';
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T06-REV2', 'TST-T05-BUILD', 'повтор', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: повторное сторно прошло';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'уже сторнировано') THEN
    RAISE USING MESSAGE = CONCAT('T06: неожиданный отказ: ', @@error.message);
  END IF;
END;

-- @@TEST T07 резервы WB/Ozon/B2B не превышают свободный остаток; отказ не оставляет следов
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T07-A', 'OZON', NULL, 'тест', NULL,
    [STRUCT('EVT-FC-MOIST-50' AS card_sku, 3 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: резерв сверх свободного прошёл';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'не хватает свободного') THEN
    RAISE USING MESSAGE = CONCAT('T07a: неожиданный отказ: ', @@error.message);
  END IF;
END;
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'TST-T07-A') = 0 AS 'T07a: отказ оставил движения';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT` WHERE request_id = 'TST-T07-A') = 0 AS 'T07a: отказ оставил отгрузку';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG` WHERE request_id = 'TST-T07-A' AND status = 'REJECTED') = 1 AS 'T07a: отказ не записан в журнал запросов';
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T07-B1', 'WB', NULL, 'тест', NULL,
  [STRUCT('EVT-FC-MOIST-50' AS card_sku, 1 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T07-B2', 'OZON', NULL, 'тест', NULL,
    [STRUCT('EVT-FC-MOIST-50' AS card_sku, 2 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: резерв Ozon поверх резерва WB превысил остаток';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'не хватает свободного') THEN
    RAISE USING MESSAGE = CONCAT('T07b: неожиданный отказ: ', @@error.message);
  END IF;
END;
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T07-B3', 'B2B', NULL, 'тест', NULL,
    [STRUCT('EVT-FC-MOIST-50' AS card_sku, 2 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: резерв B2B поверх резерва WB превысил остаток';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'не хватает свободного') THEN
    RAISE USING MESSAGE = CONCAT('T07c: неожиданный отказ: ', @@error.message);
  END IF;
END;
ASSERT (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
        WHERE internal_sku = 'EVT-FC-MOIST-50' AND state = 'RESERVED') = 1 AS 'T07b: резерв не равен 1';
ASSERT (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
        WHERE internal_sku = 'EVT-FC-MOIST-50' AND state = 'AVAILABLE') = 1 AS 'T07b: свободно не равно 1';
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T07-B1R', 'TST-T07-B1', 'тест: снять резерв', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
ASSERT (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
        WHERE internal_sku = 'EVT-FC-MOIST-50' AND state = 'AVAILABLE') = 2 AS 'T07: остаток не восстановлен';

-- @@TEST T08 повтор запроса с тем же request_id не создаёт ни движений, ни отгрузок, ни строк журнала
DECLARE n_mv INT64;
DECLARE n_sh INT64;
DECLARE n_log INT64;
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T08', 'WB', NULL, 'тест', NULL,
  [STRUCT('EVT-FS-MOIST-30' AS card_sku, 5 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
SET n_mv = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`);
SET n_sh = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT`);
SET n_log = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`);
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T08', 'WB', NULL, 'тест', NULL,
  [STRUCT('EVT-FS-MOIST-30' AS card_sku, 5 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`) = n_mv AS 'T08: повтор создал движения';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT`) = n_sh AS 'T08: повтор создал отгрузку';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`) = n_log AS 'T08: повтор записан в журнал запросов';
ASSERT (SELECT SUM(qty) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'TST-T08') = 5 AS 'T08: зарезервировано не 5';

-- @@TEST T09 отмена резерва сохраняет физический запас
DECLARE v_ship STRING;
DECLARE v_total INT64;
SET v_ship = (SELECT ANY_VALUE(doc_id) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG` WHERE request_id = 'TST-T08' AND status = 'OK');
SET v_total = (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE internal_sku = 'EVT-FS-MOIST-30');
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T09', 'TST-T08', 'тест отмены', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
ASSERT (SELECT IFNULL(SUM(units), 0) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE doc = v_ship) = 0 AS 'T09: резерв не снят';
ASSERT (SELECT status FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` WHERE shipment_id = v_ship) = 'CANCELLED' AS 'T09: статус отгрузки не CANCELLED';
ASSERT (SELECT SUM(units) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` WHERE internal_sku = 'EVT-FS-MOIST-30') = v_total
  AS 'T09: отмена изменила физический итог SKU';

-- @@TEST T13 устаревшие данные API → отказ
UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` SET config_value = '0' WHERE config_key = 'max_api_age_hours';
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T13', 'WB', NULL, 'тест', NULL,
    [STRUCT('EVT-FS-MOIST-30' AS card_sku, 1 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: резерв на устаревших данных прошёл';
EXCEPTION WHEN ERROR THEN
  UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` SET config_value = '30' WHERE config_key = 'max_api_age_hours';
  IF NOT REGEXP_CONTAINS(@@error.message, r'устарели') THEN
    RAISE USING MESSAGE = CONCAT('T13: неожиданный отказ: ', @@error.message);
  END IF;
END;
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'TST-T13') = 0 AS 'T13: движения записаны';

-- @@TEST T14 двойной резерв одного документа площадки → отказ; после отмены — снова можно
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T14-1', 'WB', 'TST-REF-14', 'тест', NULL,
  [STRUCT('EVT-FS-MOIST-30' AS card_sku, 1 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T14-2', 'WB', 'TST-REF-14', 'тест', NULL,
    [STRUCT('EVT-FS-MOIST-30' AS card_sku, 1 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: двойной резерв прошёл';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'уже зарезервирован') THEN
    RAISE USING MESSAGE = CONCAT('T14: неожиданный отказ: ', @@error.message);
  END IF;
END;
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T14-1R', 'TST-T14-1', 'тест: снять', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T14-3', 'WB', 'TST-REF-14', 'тест', NULL,
  [STRUCT('EVT-FS-MOIST-30' AS card_sku, 1 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T14-3R', 'TST-T14-3', 'тест: снять', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');

-- @@TEST T15 неполный BOM → отказ (неизвестная карточка; «набор», который не набор)
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T15-A', 'WB', NULL, 'тест', NULL,
    [STRUCT('EVT-NO-SUCH-CARD' AS card_sku, 1 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: резерв неизвестной карточки прошёл';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'неполный BOM') THEN
    RAISE USING MESSAGE = CONCAT('T15a: неожиданный отказ: ', @@error.message);
  END IF;
END;
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_bundle_build`('TST-T15-B', 'EVT-FS-MOIST-30', 1, 'FBS_STOCK', NULL, CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: сборка не-набора прошла';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'неполный BOM') THEN
    RAISE USING MESSAGE = CONCAT('T15b: неожиданный отказ: ', @@error.message);
  END IF;
END;

-- @@TEST T16 отрицательный остаток невозможен: сборка из паллетного резерва; сторно посева при других движениях
DECLARE v_ship STRING;
DECLARE v_bundle STRING;
-- набор в открытой отгрузке, у которого хотя бы одного компонента на полке в резерве меньше, чем нужно на 1 набор
SET (v_ship, v_bundle) = (
  SELECT AS STRUCT l.shipment_id, l.card_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE` l
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` c ON c.shipment_id = l.shipment_id AND c.status = 'RESERVED'
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = l.card_sku AND b.is_bundle
  LEFT JOIN (
    SELECT doc, internal_sku, SUM(units) AS shelf_res
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
    WHERE state = 'RESERVED' AND location = 'SHELF' GROUP BY 1, 2) s
    ON s.doc = l.shipment_id AND s.internal_sku = b.component_sku
  WHERE l.is_bundle AND IFNULL(s.shelf_res, 0) < b.component_qty
  ORDER BY l.shipment_id, l.card_sku LIMIT 1);
ASSERT v_ship IS NOT NULL AS 'T16: нет отгрузки с набором, компонент которого только на паллетах, — тест не доказателен';
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
        WHERE request_id != 'SEED-OPENING-2026-09-09') > 0
  AS 'T16: в журнале только посев — проверка отказа сторно посева не доказательна (запускать после T05–T14)';
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_bundle_build`('TST-T16-A', v_bundle, 1, 'FBO_SHIPMENT', v_ship, CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: сборка из паллетного резерва прошла';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'снимите с паллет') THEN
    RAISE USING MESSAGE = CONCAT('T16a: неожиданный отказ: ', @@error.message);
  END IF;
END;
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_seed_reverse`('TST-T16-B', 'SEED-OPENING-2026-09-09', 'тест', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: сторно посева при других движениях прошло';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'движения кроме посева') THEN
    RAISE USING MESSAGE = CONCAT('T16b: неожиданный отказ: ', @@error.message);
  END IF;
END;

-- @@TEST T17 выключенная запись владельца → отказ
UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` SET config_value = 'false' WHERE config_key = 'writeback_enabled';
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T17', 'WB', NULL, 'тест', NULL,
    [STRUCT('EVT-FS-MOIST-30' AS card_sku, 1 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: запись при выключенном флаге прошла';
EXCEPTION WHEN ERROR THEN
  UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG` SET config_value = 'true' WHERE config_key = 'writeback_enabled';
  IF NOT REGEXP_CONTAINS(@@error.message, r'выключены') THEN
    RAISE USING MESSAGE = CONCAT('T17: неожиданный отказ: ', @@error.message);
  END IF;
END;
ASSERT (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = 'TST-T17') = 0 AS 'T17: движения записаны';

-- @@TEST T18 полный цикл FBO: резерв → сборка под отгрузку → нельзя отменить собранное → разборка → отмена
DECLARE v_ship STRING;
DECLARE v_b STRING DEFAULT 'EVT-SET-MOIST-TONIC-SERUM';
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`('TST-T18-R', 'WB', 'TST-REF-18', 'тест', NULL,
  [STRUCT(v_b AS card_sku, 2 AS qty_cards)], CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
SET v_ship = (SELECT ANY_VALUE(doc_id) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG` WHERE request_id = 'TST-T18-R' AND status = 'OK');
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_bundle_build`('TST-T18-B', v_b, 2, 'FBO_SHIPMENT', v_ship, CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
ASSERT (SELECT COUNTIF(units = 2 * b.component_qty)
        FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` s
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = v_b AND b.component_sku = s.internal_sku
        WHERE s.state = 'ASSEMBLED' AND s.container = v_b AND s.doc = v_ship)
     = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` WHERE card_sku = v_b AND is_bundle)
  AS 'T18: собранные наборы не равны BOM × 2';
ASSERT (SELECT COUNTIF(purpose = 'FBO') = COUNT(*) AND COUNT(*) > 0 FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_STATE`
        WHERE state = 'ASSEMBLED' AND doc_id = v_ship) AS 'T18: FBO-набор не отмечен как FBO';
ASSERT (SELECT IFNULL(SUM(units), 0) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
        WHERE state = 'FBS_READY' AND container = v_b) = 0 AS 'T18: FBO-сборка попала в FBS';
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_bundle_build`('TST-T18-B2', v_b, 1, 'FBO_SHIPMENT', v_ship, CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: сборка сверх строки отгрузки прошла';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'больше строки') THEN
    RAISE USING MESSAGE = CONCAT('T18a: неожиданный отказ: ', @@error.message);
  END IF;
END;
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T18-X', 'TST-T18-R', 'тест', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
  RAISE USING MESSAGE = 'TEST FAIL: отмена резерва с собранными наборами прошла';
EXCEPTION WHEN ERROR THEN
  IF NOT REGEXP_CONTAINS(@@error.message, r'отрицательным') THEN
    RAISE USING MESSAGE = CONCAT('T18b: неожиданный отказ: ', @@error.message);
  END IF;
END;
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T18-BR', 'TST-T18-B', 'тест разборки', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`('TST-T18-RR', 'TST-T18-R', 'тест отмены', CURRENT_DATE('Europe/Moscow'), 'TEST', 'stage-b-test');
ASSERT (
  WITH legs AS (
    SELECT internal_sku, from_state AS st, from_location AS loc, from_channel AS ch, from_container AS ct, from_doc AS dc, -qty AS q
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE STARTS_WITH(request_id, 'TST-T18') AND from_state IS NOT NULL
    UNION ALL
    SELECT internal_sku, to_state, to_location, to_channel, to_container, to_doc, qty
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE STARTS_WITH(request_id, 'TST-T18') AND to_state IS NOT NULL)
  SELECT COUNT(*) FROM (SELECT internal_sku, st, loc, ch, ct, dc, SUM(q) AS s FROM legs GROUP BY 1, 2, 3, 4, 5, 6 HAVING s != 0)
) = 0 AS 'T18: цикл резерв → сборка → разборка → отмена не вернул запас ровно';
