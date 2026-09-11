-- ============================================================================
-- EVETIS · Stage B · Operations backend · 04 — процедуры
--
-- ЕДИНСТВЕННЫЙ путь записи в журнал — sp_ops_post_movements (внутренняя, без своей
-- транзакции). Её вызывают только процедуры ниже; каждая из них:
--   * идемпотентна по request_id (успешный запрос не повторяется — возвращается прежний результат);
--   * работает в ОДНОЙ транзакции: шапка + строки + движения + журнал запроса — всё или ничего;
--   * первой записью транзакции обновляет строку-замок OPS_CONFIG.ledger_lock — пишущие
--     транзакции не идут параллельно;
--   * при отказе откатывает транзакцию, пишет REJECTED в журнал запросов и возвращает причину.
-- Операции владельца (резерв, сборка, отмена, сторно) закрыты флагом writeback_enabled.
-- Административные — посев и сторно посева — флагом не закрыты, но защищены своими условиями.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- Ядро: проверить и записать пакет движений. Отказ = RAISE, частичной записи нет.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_post_movements`(
  p_request_id STRING, p_doc_type STRING, p_doc_id STRING, p_event_date DATE,
  p_movements ARRAY<STRUCT<internal_sku STRING, qty INT64,
    from_state STRING, from_location STRING, from_channel STRING, from_container STRING, from_doc STRING,
    to_state STRING, to_location STRING, to_channel STRING, to_container STRING, to_doc STRING,
    reversal_of STRING, note STRING>>,
  p_source STRING, p_changed_by STRING)
BEGIN
  DECLARE v_bad STRING;

  IF p_request_id IS NULL OR p_doc_type IS NULL OR p_event_date IS NULL OR p_source IS NULL THEN
    RAISE USING MESSAGE = 'OPS: request_id, doc_type, event_date и source обязательны';
  END IF;
  IF IFNULL(ARRAY_LENGTH(p_movements), 0) = 0 THEN
    RAISE USING MESSAGE = 'OPS: пустой пакет движений';
  END IF;
  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = p_request_id) > 0 THEN
    RAISE USING MESSAGE = CONCAT('OPS: движения запроса ', p_request_id, ' уже записаны');
  END IF;

  -- 1. Количество > 0, SKU — физический товар каталога (не набор).
  SET v_bad = (
    SELECT STRING_AGG(DISTINCT IFNULL(m.internal_sku, '∅'), ', ')
    FROM UNNEST(p_movements) m
    LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p
      ON p.internal_sku = m.internal_sku AND NOT p.is_bundle
    WHERE m.qty IS NULL OR m.qty <= 0 OR p.internal_sku IS NULL);
  IF v_bad IS NOT NULL THEN
    RAISE USING MESSAGE = CONCAT('OPS: недопустимый физический SKU или количество: ', v_bad);
  END IF;

  -- 2. Форма бакета: у каждого состояния свой набор обязательных и запрещённых атрибутов.
  SET v_bad = (
    WITH sides AS (
      SELECT internal_sku, from_state AS st, from_location AS loc, from_channel AS ch, from_container AS ct, from_doc AS dc
      FROM UNNEST(p_movements) WHERE from_state IS NOT NULL
      UNION ALL
      SELECT internal_sku, to_state, to_location, to_channel, to_container, to_doc
      FROM UNNEST(p_movements) WHERE to_state IS NOT NULL)
    SELECT STRING_AGG(DISTINCT CONCAT(st, '/', IFNULL(loc, '∅'), '/', IFNULL(ch, '∅'), '/', IFNULL(ct, '∅'), '/', IFNULL(dc, '∅')), '; ')
    FROM sides
    WHERE NOT (
         (st = 'AVAILABLE'   AND loc IN ('PALLET', 'SHELF') AND ch IS NULL AND ct IS NULL AND dc IS NULL)
      OR (st = 'RESERVED'    AND loc IN ('PALLET', 'SHELF') AND ch IN ('WB', 'OZON', 'B2B') AND ct IS NULL AND dc IS NOT NULL)
      OR (st = 'ASSEMBLED'   AND loc = 'SHELF' AND ch IN ('WB', 'OZON', 'B2B') AND ct IS NOT NULL AND dc IS NOT NULL)
      OR (st = 'FBS_READY'   AND loc = 'SHELF' AND ch = 'FBS' AND dc IS NULL)
      OR (st = 'SHIPPED'     AND loc IS NULL AND ch IN ('WB', 'OZON', 'B2B') AND dc IS NOT NULL)
      OR (st = 'WRITTEN_OFF' AND loc IS NULL AND ch IS NULL AND ct IS NULL)));
  IF v_bad IS NOT NULL THEN
    RAISE USING MESSAGE = CONCAT('OPS: недопустимая форма бакета: ', v_bad);
  END IF;

  -- 3. Контейнер — набор, и единица действительно его компонент.
  SET v_bad = (
    WITH c AS (
      SELECT internal_sku, from_container AS ct FROM UNNEST(p_movements) WHERE from_container IS NOT NULL
      UNION ALL SELECT internal_sku, to_container FROM UNNEST(p_movements) WHERE to_container IS NOT NULL)
    SELECT STRING_AGG(DISTINCT CONCAT(c.ct, '∌', c.internal_sku), ', ')
    FROM c
    LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b
      ON b.card_sku = c.ct AND b.component_sku = c.internal_sku AND b.is_bundle
    WHERE b.card_sku IS NULL);
  IF v_bad IS NOT NULL THEN
    RAISE USING MESSAGE = CONCAT('OPS: контейнер не набор или единица не его компонент: ', v_bad);
  END IF;

  -- 4. Белый список переходов.
  SET v_bad = (
    SELECT STRING_AGG(DISTINCT CONCAT(IFNULL(from_state, '∅'), '→', IFNULL(to_state, '∅')), ', ')
    FROM UNNEST(p_movements)
    WHERE NOT (
         (from_state IS NULL AND to_state = 'AVAILABLE' AND p_doc_type IN ('SEED', 'INBOUND'))
      OR (from_state IS NULL AND to_state = 'SHIPPED'   AND p_doc_type = 'SEED')
      OR (from_state = 'AVAILABLE' AND to_state IN ('AVAILABLE', 'RESERVED', 'FBS_READY', 'WRITTEN_OFF'))
      OR (from_state = 'RESERVED'  AND to_state IN ('AVAILABLE', 'RESERVED', 'ASSEMBLED', 'SHIPPED'))
      OR (from_state = 'ASSEMBLED' AND to_state IN ('RESERVED', 'SHIPPED'))
      OR (from_state = 'FBS_READY' AND to_state = 'AVAILABLE')
      OR (p_doc_type = 'REVERSAL' AND reversal_of IS NOT NULL)));
  IF v_bad IS NOT NULL THEN
    RAISE USING MESSAGE = CONCAT('OPS: переход не разрешён: ', v_bad);
  END IF;

  -- 5. Сторно: только в документе REVERSAL, строго зеркально исходному движению, один раз.
  IF p_doc_type != 'REVERSAL' AND (SELECT COUNTIF(reversal_of IS NOT NULL) FROM UNNEST(p_movements)) > 0 THEN
    RAISE USING MESSAGE = 'OPS: reversal_of допустим только в документе REVERSAL';
  END IF;
  IF p_doc_type = 'REVERSAL' THEN
    SET v_bad = (
      SELECT STRING_AGG(IFNULL(m.reversal_of, '∅'), ', ')
      FROM UNNEST(p_movements) m
      LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` o ON o.movement_id = m.reversal_of
      WHERE o.movement_id IS NULL OR o.doc_type = 'REVERSAL'
         OR o.internal_sku != m.internal_sku OR o.qty != m.qty
         OR IFNULL(o.to_state, '~')     != IFNULL(m.from_state, '~')     OR IFNULL(o.from_state, '~')     != IFNULL(m.to_state, '~')
         OR IFNULL(o.to_location, '~')  != IFNULL(m.from_location, '~')  OR IFNULL(o.from_location, '~')  != IFNULL(m.to_location, '~')
         OR IFNULL(o.to_channel, '~')   != IFNULL(m.from_channel, '~')   OR IFNULL(o.from_channel, '~')   != IFNULL(m.to_channel, '~')
         OR IFNULL(o.to_container, '~') != IFNULL(m.from_container, '~') OR IFNULL(o.from_container, '~') != IFNULL(m.to_container, '~')
         OR IFNULL(o.to_doc, '~')       != IFNULL(m.from_doc, '~')       OR IFNULL(o.from_doc, '~')       != IFNULL(m.to_doc, '~'));
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS: сторно не зеркально исходному движению: ', v_bad);
    END IF;
    SET v_bad = (
      SELECT STRING_AGG(DISTINCT r.reversal_of, ', ')
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` r
      WHERE r.reversal_of IN (SELECT reversal_of FROM UNNEST(p_movements)));
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS: движение уже сторнировано: ', v_bad);
    END IF;
  END IF;

  -- 6. Ни один бакет не уходит в минус (с учётом всего пакета целиком).
  SET v_bad = (
    WITH legs AS (
      SELECT internal_sku, from_state AS st, from_location AS loc, from_channel AS ch, from_container AS ct, from_doc AS dc, -qty AS q
      FROM UNNEST(p_movements) WHERE from_state IS NOT NULL
      UNION ALL
      SELECT internal_sku, to_state, to_location, to_channel, to_container, to_doc, qty
      FROM UNNEST(p_movements) WHERE to_state IS NOT NULL),
    delta AS (SELECT internal_sku, st, loc, ch, ct, dc, SUM(q) AS d FROM legs GROUP BY 1, 2, 3, 4, 5, 6)
    SELECT STRING_AGG(FORMAT('%s %s/%s/%s/%s/%s: было %d, станет %d', d.internal_sku, d.st, IFNULL(d.loc, '∅'),
                             IFNULL(d.ch, '∅'), IFNULL(d.ct, '∅'), IFNULL(d.dc, '∅'),
                             IFNULL(b.units, 0), IFNULL(b.units, 0) + d.d), '; ' LIMIT 12)
    FROM delta d
    LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` b
      ON b.internal_sku = d.internal_sku AND b.state = d.st
     AND IFNULL(b.location, '') = IFNULL(d.loc, '') AND IFNULL(b.channel, '') = IFNULL(d.ch, '')
     AND IFNULL(b.container, '') = IFNULL(d.ct, '') AND IFNULL(b.doc, '') = IFNULL(d.dc, '')
    WHERE IFNULL(b.units, 0) + d.d < 0);
  IF v_bad IS NOT NULL THEN
    RAISE USING MESSAGE = CONCAT('OPS: остаток станет отрицательным — ', v_bad);
  END IF;

  -- 7. Запись.
  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
    (movement_id, request_id, seq, event_date, recorded_at, internal_sku, qty,
     from_state, from_location, from_channel, from_container, from_doc,
     to_state, to_location, to_channel, to_container, to_doc,
     doc_type, doc_id, reversal_of, source, changed_by, note)
  SELECT CONCAT(p_request_id, '#', LPAD(CAST(off + 1 AS STRING), 4, '0')), p_request_id, off + 1, p_event_date,
         CURRENT_TIMESTAMP(), m.internal_sku, m.qty,
         m.from_state, m.from_location, m.from_channel, m.from_container, m.from_doc,
         m.to_state, m.to_location, m.to_channel, m.to_container, m.to_doc,
         p_doc_type, p_doc_id, m.reversal_of, p_source, p_changed_by, m.note
  FROM UNNEST(p_movements) m WITH OFFSET off;
END;

-- ─────────────────────────────────────────────────────────────────────────────
-- Сторно всех движений одного запроса (внутренняя, без своей транзакции).
-- Проверки зеркальности, однократности и неотрицательности — в ядре.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_reverse_core`(
  p_request_id STRING, p_original_request_id STRING, p_event_date DATE, p_note STRING,
  p_source STRING, p_changed_by STRING)
BEGIN
  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
      WHERE request_id = p_original_request_id) = 0 THEN
    RAISE USING MESSAGE = CONCAT('OPS: у запроса ', IFNULL(p_original_request_id, '∅'), ' нет движений');
  END IF;
  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
      WHERE request_id = p_original_request_id AND doc_type = 'REVERSAL') > 0 THEN
    RAISE USING MESSAGE = 'OPS: сторно сторно запрещено — исправление делается новым документом';
  END IF;
  CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_post_movements`(
    p_request_id, 'REVERSAL', p_original_request_id, p_event_date,
    ARRAY(
      SELECT AS STRUCT internal_sku, qty,
             to_state AS from_state, to_location AS from_location, to_channel AS from_channel,
             to_container AS from_container, to_doc AS from_doc,
             from_state AS to_state, from_location AS to_location, from_channel AS to_channel,
             from_container AS to_container, from_doc AS to_doc,
             movement_id AS reversal_of, p_note AS note
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
      WHERE request_id = p_original_request_id
      ORDER BY seq DESC),
    p_source, p_changed_by);
END;

-- ─────────────────────────────────────────────────────────────────────────────
-- Предохранители.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_guard_writeback`()
BEGIN
  IF IFNULL((SELECT ANY_VALUE(config_value) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
             WHERE config_key = 'writeback_enabled'), 'false') != 'true' THEN
    RAISE USING MESSAGE = 'OPS: операции владельца выключены (OPS_CONFIG.writeback_enabled = false)';
  END IF;
END;

CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_guard_fresh`()
BEGIN
  DECLARE v_max_h INT64;
  DECLARE v_ozon_h INT64;
  DECLARE v_ct_h INT64;
  SET v_max_h = SAFE_CAST((SELECT ANY_VALUE(config_value) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
                           WHERE config_key = 'max_api_age_hours') AS INT64);
  SET v_ozon_h = (SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(extracted_at), HOUR)
                  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SUPPLY_ORDERS`);
  SET v_ct_h = (SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(SAFE_CAST(ct_snapshot_ts AS TIMESTAMP)), HOUR)
                FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH`);
  IF v_max_h IS NULL OR v_ozon_h IS NULL OR v_ct_h IS NULL OR v_ozon_h >= v_max_h OR v_ct_h >= v_max_h THEN
    RAISE USING MESSAGE = FORMAT('OPS: данные API устарели — заказы Ozon %s ч, остатки площадок %s ч при допуске %s ч',
      IFNULL(CAST(v_ozon_h AS STRING), '∅'), IFNULL(CAST(v_ct_h AS STRING), '∅'), IFNULL(CAST(v_max_h AS STRING), '∅'));
  END IF;
END;

-- ─────────────────────────────────────────────────────────────────────────────
-- ПОСЕВ: открытие журнала физическим срезом ФФ на 09.09.2026 + доказанные резервы.
-- Административная операция, один раз. Отказывает, если:
--   журнал не пуст; у открытого на дату заказа Ozon нет доказательства; строка заказа не
--   сопоставлена SKU; резерв превышает физический остаток SKU.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_seed_opening`(
  p_request_id STRING, p_changed_by STRING)
BEGIN
  DECLARE v_cutoff TIMESTAMP DEFAULT TIMESTAMP '2026-09-09 23:59:59+03';
  DECLARE v_open_date DATE DEFAULT DATE '2026-09-09';
  DECLARE v_bad STRING;
  DECLARE v_moves INT64;

  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
    SELECT request_id, status, doc_id, movements, 'IDEMPOTENT_REPLAY' AS note
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
    WHERE request_id = p_request_id AND status = 'OK';
    RETURN;
  END IF;

  BEGIN
    BEGIN TRANSACTION;
    UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
       SET config_value = p_request_id, updated_at = CURRENT_TIMESTAMP(), updated_by = p_changed_by
     WHERE config_key = 'ledger_lock';

    IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`) > 0 THEN
      RAISE USING MESSAGE = 'OPS SEED: журнал не пуст — посев выполняется один раз';
    END IF;

    -- Открытые на дату заказы Ozon: созданы до среза и не закрыты (приняты / отменены) до среза.
    SET v_bad = (
      SELECT STRING_AGG(DISTINCT u.order_number, ', ')
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` u
      LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` e USING (order_number)
      WHERE u.created_at <= v_cutoff
        AND NOT (u.state IN ('CANCELLED', 'COMPLETED') AND u.state_updated_at <= v_cutoff)
        AND e.order_number IS NULL);
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS SEED: нет доказательства физического статуса заказов Ozon: ', v_bad);
    END IF;
    SET v_bad = (
      SELECT STRING_AGG(DISTINCT u.order_number, ', ')
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` u
      JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` e USING (order_number)
      WHERE u.internal_sku IS NULL);
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS SEED: строки заказов не сопоставлены SKU / BOM: ', v_bad);
    END IF;
    -- Доказательство есть, а заказ в API уже не открыт на дату (закрыт до среза) — противоречие.
    SET v_bad = (
      SELECT STRING_AGG(DISTINCT e.order_number, ', ')
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` e
      LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` u USING (order_number)
      WHERE u.order_number IS NULL
         OR (u.state IN ('CANCELLED', 'COMPLETED') AND u.state_updated_at <= v_cutoff));
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS SEED: доказательство на заказ, не открытый на дату: ', v_bad);
    END IF;
    -- Резерв не может превысить физический остаток SKU.
    SET v_bad = (
      WITH need AS (
        SELECT u.internal_sku, SUM(u.units_planned) AS units
        FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` u
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` e USING (order_number)
        WHERE e.ff_status_at_opening = 'ON_FF' GROUP BY 1)
      SELECT STRING_AGG(FORMAT('%s: резерв %d > остаток %d', n.internal_sku, n.units, IFNULL(s.shelf_units + s.pallet_units, 0)), '; ')
      FROM need n
      LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_FF_SNAPSHOT` s
        ON s.internal_sku = n.internal_sku AND s.snapshot_date = v_open_date AND s.in_ledger
      WHERE n.units > IFNULL(s.shelf_units + s.pallet_units, 0));
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS SEED: резерв превышает физический остаток: ', v_bad);
    END IF;

    -- Отгрузки: заказы «на ФФ» — RESERVED, уехавшие до открытия — SHIPPED.
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT`
      (shipment_id, event_seq, status, channel, counterparty, destination, marketplace_order_id, marketplace_ref,
       planned_date, event_at, request_id, source, changed_by, note)
    SELECT CONCAT('SH-OZ-', u.order_number), 1,
           IF(e.ff_status_at_opening = 'ON_FF', 'RESERVED', 'SHIPPED'),
           'OZON', 'OZON', ANY_VALUE(u.dropoff_warehouse_name), CAST(ANY_VALUE(u.order_id) AS STRING), u.order_number,
           DATE(ANY_VALUE(u.planned_arrival_from), 'Europe/Moscow'), CURRENT_TIMESTAMP(), p_request_id, 'SEED', p_changed_by,
           CONCAT('Открытие журнала 09.09.2026: ', e.ff_status_at_opening, ' — ', e.source_ref)
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` u
    JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` e USING (order_number)
    GROUP BY u.order_number, e.ff_status_at_opening, e.source_ref;

    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE`
      (shipment_id, line_no, card_sku, marketplace_sku, qty_cards, is_bundle, physical_units, created_at, request_id)
    WITH lines AS (
      SELECT order_number, card_sku, ozon_sku, ANY_VALUE(cards_planned) AS qty_cards,
             LOGICAL_OR(is_bundle) AS is_bundle, SUM(units_planned) AS physical_units
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS`
      WHERE order_number IN (SELECT order_number FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE`)
      GROUP BY order_number, card_sku, ozon_sku, supply_id)
    SELECT CONCAT('SH-OZ-', order_number), ROW_NUMBER() OVER (PARTITION BY order_number ORDER BY card_sku, ozon_sku),
           card_sku, ozon_sku, qty_cards, is_bundle, physical_units, CURRENT_TIMESTAMP(), p_request_id
    FROM lines;

    -- Движения посева.
    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_post_movements`(
      p_request_id, 'SEED', 'SEED-OPENING-2026-09-09', v_open_date,
      ARRAY(
        WITH snap AS (
          SELECT internal_sku, shelf_units, pallet_units
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_FF_SNAPSHOT`
          WHERE snapshot_date = v_open_date AND in_ledger),
        ord AS (
          SELECT u.order_number, e.ff_status_at_opening AS status, u.internal_sku,
                 ANY_VALUE(u.planned_arrival_from) AS arrival, SUM(u.units_planned) AS need
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_OZON_ORDER_UNITS` u
          JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPENING_OZON_EVIDENCE` e USING (order_number)
          GROUP BY 1, 2, 3),
        alloc AS (   -- резерв «на ФФ»: сначала полка, затем паллеты; порядок заказов — по плановой приёмке
          SELECT o.order_number, o.internal_sku, o.need, s.shelf_units,
                 IFNULL(SUM(o.need) OVER (PARTITION BY o.internal_sku ORDER BY o.arrival, o.order_number
                                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS cum_before
          FROM ord o JOIN snap s USING (internal_sku)
          WHERE o.status = 'ON_FF'),
        moves AS (
          SELECT 1 AS k, internal_sku, pallet_units AS qty, CAST(NULL AS STRING) AS fs, CAST(NULL AS STRING) AS fl,
                 CAST(NULL AS STRING) AS fd, 'AVAILABLE' AS ts, 'PALLET' AS tl, CAST(NULL AS STRING) AS tch,
                 CAST(NULL AS STRING) AS td, 'открытие: паллеты (длительное хранение)' AS note
          FROM snap WHERE pallet_units > 0
          UNION ALL
          SELECT 2, internal_sku, shelf_units, NULL, NULL, NULL, 'AVAILABLE', 'SHELF', NULL, NULL, 'открытие: оперативная полка'
          FROM snap WHERE shelf_units > 0
          UNION ALL
          SELECT 3, internal_sku, LEAST(need, GREATEST(shelf_units - cum_before, 0)), 'AVAILABLE', 'SHELF', NULL,
                 'RESERVED', 'SHELF', 'OZON', CONCAT('SH-OZ-', order_number), CONCAT('резерв Ozon ', order_number, ' с полки')
          FROM alloc
          UNION ALL
          SELECT 4, internal_sku, need - LEAST(need, GREATEST(shelf_units - cum_before, 0)), 'AVAILABLE', 'PALLET', NULL,
                 'RESERVED', 'PALLET', 'OZON', CONCAT('SH-OZ-', order_number), CONCAT('резерв Ozon ', order_number, ' на паллетах (снять)')
          FROM alloc
          UNION ALL
          SELECT 5, internal_sku, need, NULL, NULL, NULL, 'SHIPPED', CAST(NULL AS STRING), 'OZON',
                 CONCAT('SH-OZ-', order_number), CONCAT('уехало до открытия журнала: Ozon ', order_number)
          FROM ord WHERE status = 'LEFT_FF')
        SELECT AS STRUCT internal_sku, qty,
               fs AS from_state, fl AS from_location, CAST(NULL AS STRING) AS from_channel,
               CAST(NULL AS STRING) AS from_container, fd AS from_doc,
               ts AS to_state, tl AS to_location, tch AS to_channel, CAST(NULL AS STRING) AS to_container, td AS to_doc,
               CAST(NULL AS STRING) AS reversal_of, note
        FROM moves WHERE qty > 0
        ORDER BY k, internal_sku, to_doc),
      'SEED', p_changed_by);

    SET v_moves = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = p_request_id);
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_seed_opening', 'OK', 'opening seed 2026-09-09', 'SEED-OPENING-2026-09-09',
            v_moves, NULL, CURRENT_TIMESTAMP(), 'SEED', p_changed_by);
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_seed_opening', 'REJECTED', @@error.message, NULL, 0, NULL, CURRENT_TIMESTAMP(), 'SEED', p_changed_by);
    RAISE USING MESSAGE = @@error.message;
  END;

  SELECT request_id, status, doc_id, movements, 'DONE' AS note
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
  WHERE request_id = p_request_id AND status = 'OK';
END;

-- ─────────────────────────────────────────────────────────────────────────────
-- СТОРНО ПОСЕВА (административная): возвращает журнал к нулю. Только пока в журнале нет
-- ничего, кроме посева. Это штатный способ отменить открытие без разрушающего удаления.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_seed_reverse`(
  p_request_id STRING, p_seed_request_id STRING, p_reason STRING, p_changed_by STRING)
BEGIN
  DECLARE v_moves INT64;
  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
    SELECT request_id, status, doc_id, movements, 'IDEMPOTENT_REPLAY' AS note
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
    WHERE request_id = p_request_id AND status = 'OK';
    RETURN;
  END IF;
  BEGIN
    BEGIN TRANSACTION;
    UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
       SET config_value = p_request_id, updated_at = CURRENT_TIMESTAMP(), updated_by = p_changed_by
     WHERE config_key = 'ledger_lock';
    IF p_reason IS NULL OR TRIM(p_reason) = '' THEN
      RAISE USING MESSAGE = 'OPS: причина сторно посева обязательна';
    END IF;
    IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT`
        WHERE request_id != p_seed_request_id) > 0 THEN
      RAISE USING MESSAGE = 'OPS: в журнале есть движения кроме посева — сначала сторнировать их';
    END IF;
    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_reverse_core`(
      p_request_id, p_seed_request_id, CURRENT_DATE('Europe/Moscow'), CONCAT('сторно посева: ', p_reason), 'ADMIN', p_changed_by);
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT`
      (shipment_id, event_seq, status, channel, counterparty, destination, marketplace_order_id, marketplace_ref,
       planned_date, event_at, request_id, source, changed_by, note)
    SELECT shipment_id, event_seq + 1, 'CANCELLED', channel, counterparty, destination, marketplace_order_id, marketplace_ref,
           planned_date, CURRENT_TIMESTAMP(), p_request_id, 'ADMIN', p_changed_by, CONCAT('сторно посева: ', p_reason)
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT`
    WHERE request_id = p_seed_request_id OR shipment_id IN (
      SELECT shipment_id FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT` WHERE request_id = p_seed_request_id);
    SET v_moves = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = p_request_id);
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_seed_reverse', 'OK', p_reason, p_seed_request_id, v_moves, NULL, CURRENT_TIMESTAMP(), 'ADMIN', p_changed_by);
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_seed_reverse', 'REJECTED', @@error.message, p_seed_request_id, 0, NULL, CURRENT_TIMESTAMP(), 'ADMIN', p_changed_by);
    RAISE USING MESSAGE = @@error.message;
  END;
END;

-- ─────────────────────────────────────────────────────────────────────────────
-- РЕЗЕРВ ПОД ОТГРУЗКУ (операция владельца). Строки — в единицах продажи; в журнал идут
-- физические единицы по BOM. Свободно = AVAILABLE − резерв из API без отметки ФФ.
-- Размещение: сначала полка, затем паллеты.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_shipment_reserve`(
  p_request_id STRING, p_channel STRING, p_marketplace_ref STRING, p_destination STRING, p_planned_date DATE,
  p_lines ARRAY<STRUCT<card_sku STRING, qty_cards INT64>>,
  p_event_date DATE, p_source STRING, p_changed_by STRING)
BEGIN
  DECLARE v_shipment_id STRING DEFAULT CONCAT('SH-', UPPER(SUBSTR(TO_HEX(MD5(p_request_id)), 1, 10)));
  DECLARE v_bad STRING;
  DECLARE v_moves INT64;
  DECLARE v_plan ARRAY<STRUCT<internal_sku STRING, need INT64, shelf_free INT64, pallet_free INT64>>;

  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
    SELECT request_id, status, doc_id, movements, 'IDEMPOTENT_REPLAY' AS note
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
    WHERE request_id = p_request_id AND status = 'OK';
    RETURN;
  END IF;

  BEGIN
    BEGIN TRANSACTION;
    UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
       SET config_value = p_request_id, updated_at = CURRENT_TIMESTAMP(), updated_by = p_changed_by
     WHERE config_key = 'ledger_lock';
    IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
        WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
      COMMIT TRANSACTION;
      RETURN;
    END IF;

    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_guard_writeback`();
    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_guard_fresh`();

    IF IFNULL(p_channel, '') NOT IN ('WB', 'OZON', 'B2B') THEN
      RAISE USING MESSAGE = CONCAT('OPS: неизвестный канал ', IFNULL(p_channel, '∅'));
    END IF;
    IF IFNULL(ARRAY_LENGTH(p_lines), 0) = 0 THEN
      RAISE USING MESSAGE = 'OPS: в отгрузке нет строк';
    END IF;
    SET v_bad = (SELECT STRING_AGG(card_sku, ', ') FROM (
      SELECT card_sku FROM UNNEST(p_lines) GROUP BY card_sku HAVING COUNT(*) > 1));
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS: карточка повторяется в строках: ', v_bad);
    END IF;
    -- BOM: карточка известна, все компоненты — физические SKU каталога.
    SET v_bad = (
      SELECT STRING_AGG(DISTINCT IFNULL(l.card_sku, '∅'), ', ')
      FROM UNNEST(p_lines) l
      WHERE l.qty_cards IS NULL OR l.qty_cards <= 0
         OR (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b WHERE b.card_sku = l.card_sku) = 0);
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS: неполный BOM или неверное количество: ', v_bad);
    END IF;
    SET v_bad = (
      SELECT STRING_AGG(DISTINCT CONCAT(b.card_sku, '→', b.component_sku), ', ')
      FROM UNNEST(p_lines) l
      JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = l.card_sku
      LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p
        ON p.internal_sku = b.component_sku AND NOT p.is_bundle
      WHERE p.internal_sku IS NULL);
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS: неполный BOM — компонент не физический SKU каталога: ', v_bad);
    END IF;
    -- Двойной резерв: открытая отгрузка того же канала на тот же документ площадки.
    IF p_marketplace_ref IS NOT NULL AND (
        SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT`
        WHERE channel = p_channel AND marketplace_ref = p_marketplace_ref AND status NOT IN ('CANCELLED', 'CLOSED')) > 0 THEN
      RAISE USING MESSAGE = CONCAT('OPS: документ ', p_marketplace_ref, ' уже зарезервирован в открытой отгрузке');
    END IF;

    -- Потребность в физических единицах и свободный остаток.
    SET v_plan = ARRAY(
      WITH need AS (
        SELECT b.component_sku AS internal_sku, SUM(l.qty_cards * b.component_qty) AS need
        FROM UNNEST(p_lines) l
        JOIN `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b ON b.card_sku = l.card_sku
        GROUP BY 1),
      bal AS (
        SELECT internal_sku,
               SUM(IF(state = 'AVAILABLE' AND location = 'SHELF', units, 0)) AS shelf,
               SUM(IF(state = 'AVAILABLE' AND location = 'PALLET', units, 0)) AS pallet
        FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE` GROUP BY 1),
      unconf AS (
        SELECT internal_sku, SUM(api_unconfirmed_units) AS u
        FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON` GROUP BY 1)
      SELECT AS STRUCT n.internal_sku, n.need,
             GREATEST(IFNULL(b.shelf, 0) - IFNULL(u.u, 0), 0) AS shelf_free,
             IFNULL(b.pallet, 0) - GREATEST(IFNULL(u.u, 0) - IFNULL(b.shelf, 0), 0) AS pallet_free
      FROM need n LEFT JOIN bal b USING (internal_sku) LEFT JOIN unconf u USING (internal_sku));
    SET v_bad = (
      SELECT STRING_AGG(FORMAT('%s: нужно %d, свободно %d', internal_sku, need, GREATEST(shelf_free + pallet_free, 0)), '; ')
      FROM UNNEST(v_plan) WHERE need > shelf_free + pallet_free);
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS: не хватает свободного запаса — ', v_bad);
    END IF;

    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT`
      (shipment_id, event_seq, status, channel, counterparty, destination, marketplace_order_id, marketplace_ref,
       planned_date, event_at, request_id, source, changed_by, note)
    VALUES (v_shipment_id, 1, 'RESERVED', p_channel, p_channel, p_destination, NULL, p_marketplace_ref,
            p_planned_date, CURRENT_TIMESTAMP(), p_request_id, p_source, p_changed_by, NULL);
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE`
      (shipment_id, line_no, card_sku, marketplace_sku, qty_cards, is_bundle, physical_units, created_at, request_id)
    SELECT v_shipment_id, off + 1, l.card_sku, NULL, l.qty_cards,
           (SELECT LOGICAL_OR(is_bundle) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b WHERE b.card_sku = l.card_sku),
           l.qty_cards * (SELECT SUM(component_qty) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b WHERE b.card_sku = l.card_sku),
           CURRENT_TIMESTAMP(), p_request_id
    FROM UNNEST(p_lines) l WITH OFFSET off;

    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_post_movements`(
      p_request_id, 'SHIPMENT_RESERVE', v_shipment_id, p_event_date,
      ARRAY(
        SELECT AS STRUCT p.internal_sku, x.qty,
               'AVAILABLE' AS from_state, x.loc AS from_location, CAST(NULL AS STRING) AS from_channel,
               CAST(NULL AS STRING) AS from_container, CAST(NULL AS STRING) AS from_doc,
               'RESERVED' AS to_state, x.loc AS to_location, p_channel AS to_channel,
               CAST(NULL AS STRING) AS to_container, v_shipment_id AS to_doc,
               CAST(NULL AS STRING) AS reversal_of, CONCAT('резерв ', v_shipment_id) AS note
        FROM UNNEST(v_plan) p,
             UNNEST([STRUCT('SHELF' AS loc, LEAST(p.need, p.shelf_free) AS qty),
                     STRUCT('PALLET' AS loc, p.need - LEAST(p.need, p.shelf_free) AS qty)]) x
        WHERE x.qty > 0
        ORDER BY p.internal_sku, x.loc DESC),
      p_source, p_changed_by);

    SET v_moves = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = p_request_id);
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_shipment_reserve', 'OK', NULL, v_shipment_id, v_moves,
            TO_JSON_STRING(STRUCT(p_channel AS channel, p_marketplace_ref AS marketplace_ref, p_lines AS lines)),
            CURRENT_TIMESTAMP(), p_source, p_changed_by);
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_shipment_reserve', 'REJECTED', @@error.message, v_shipment_id, 0,
            TO_JSON_STRING(STRUCT(p_channel AS channel, p_marketplace_ref AS marketplace_ref, p_lines AS lines)),
            CURRENT_TIMESTAMP(), p_source, p_changed_by);
    RAISE USING MESSAGE = @@error.message;
  END;

  SELECT request_id, status, doc_id, movements, 'DONE' AS note
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
  WHERE request_id = p_request_id AND status = 'OK';
END;

-- ─────────────────────────────────────────────────────────────────────────────
-- СБОРКА НАБОРОВ (операция владельца).
--   FBO_SHIPMENT: компоненты RESERVED(SHELF, отгрузка) → ASSEMBLED(контейнер = набор, отгрузка).
--   FBS_STOCK:    компоненты AVAILABLE(SHELF) → FBS_READY(контейнер = набор) — постоянный запас FBS.
-- Сборка берёт только с полки: единицы на паллетах сначала снимаются (Stage E).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_bundle_build`(
  p_request_id STRING, p_bundle_sku STRING, p_qty_bundles INT64, p_purpose STRING, p_shipment_id STRING,
  p_event_date DATE, p_source STRING, p_changed_by STRING)
BEGIN
  DECLARE v_build_id STRING DEFAULT CONCAT('BLD-', UPPER(SUBSTR(TO_HEX(MD5(p_request_id)), 1, 10)));
  DECLARE v_channel STRING;
  DECLARE v_bad STRING;
  DECLARE v_moves INT64;

  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
    SELECT request_id, status, doc_id, movements, 'IDEMPOTENT_REPLAY' AS note
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
    WHERE request_id = p_request_id AND status = 'OK';
    RETURN;
  END IF;

  BEGIN
    BEGIN TRANSACTION;
    UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
       SET config_value = p_request_id, updated_at = CURRENT_TIMESTAMP(), updated_by = p_changed_by
     WHERE config_key = 'ledger_lock';
    IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
        WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
      COMMIT TRANSACTION;
      RETURN;
    END IF;
    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_guard_writeback`();

    IF IFNULL(p_purpose, '') NOT IN ('FBO_SHIPMENT', 'FBS_STOCK') THEN
      RAISE USING MESSAGE = CONCAT('OPS: назначение сборки должно быть FBO_SHIPMENT или FBS_STOCK, получено ', IFNULL(p_purpose, '∅'));
    END IF;
    IF p_qty_bundles IS NULL OR p_qty_bundles <= 0 THEN
      RAISE USING MESSAGE = 'OPS: количество наборов должно быть > 0';
    END IF;
    IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM`
        WHERE card_sku = p_bundle_sku AND is_bundle) = 0 THEN
      RAISE USING MESSAGE = CONCAT('OPS: неполный BOM — ', IFNULL(p_bundle_sku, '∅'), ' не набор или без компонентов');
    END IF;
    SET v_bad = (
      SELECT STRING_AGG(b.component_sku, ', ')
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` b
      LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p
        ON p.internal_sku = b.component_sku AND NOT p.is_bundle
      WHERE b.card_sku = p_bundle_sku AND b.is_bundle AND p.internal_sku IS NULL);
    IF v_bad IS NOT NULL THEN
      RAISE USING MESSAGE = CONCAT('OPS: неполный BOM — компонент не физический SKU: ', v_bad);
    END IF;

    IF p_purpose = 'FBO_SHIPMENT' THEN
      SET v_channel = (SELECT ANY_VALUE(channel) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT`
                       WHERE shipment_id = p_shipment_id AND status = 'RESERVED');
      IF v_channel IS NULL THEN
        RAISE USING MESSAGE = CONCAT('OPS: FBO-сборка требует открытую отгрузку в статусе RESERVED: ', IFNULL(p_shipment_id, '∅'));
      END IF;
      -- Нельзя собрать больше, чем наборов в строке отгрузки.
      SET v_bad = (
        WITH line AS (
          SELECT SUM(qty_cards) AS cards FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT_LINE`
          WHERE shipment_id = p_shipment_id AND card_sku = p_bundle_sku),
        built AS (
          SELECT SUM(IF(event = 'BUILD', qty_bundles, -qty_bundles)) AS n
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_BUNDLE_BUILD`
          WHERE shipment_id = p_shipment_id AND bundle_sku = p_bundle_sku)
        SELECT FORMAT('в отгрузке %d, уже собрано %d, просят ещё %d', IFNULL(line.cards, 0), IFNULL(built.n, 0), p_qty_bundles)
        FROM line, built WHERE IFNULL(built.n, 0) + p_qty_bundles > IFNULL(line.cards, 0));
      IF v_bad IS NOT NULL THEN
        RAISE USING MESSAGE = CONCAT('OPS: сборка больше строки отгрузки — ', v_bad);
      END IF;
      SET v_bad = (
        WITH need AS (
          SELECT component_sku AS internal_sku, component_qty * p_qty_bundles AS need
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` WHERE card_sku = p_bundle_sku AND is_bundle),
        res AS (
          SELECT internal_sku, SUM(units) AS shelf_reserved
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
          WHERE state = 'RESERVED' AND location = 'SHELF' AND doc = p_shipment_id GROUP BY 1)
        SELECT STRING_AGG(FORMAT('%s: нужно %d, в резерве на полке %d', n.internal_sku, n.need, IFNULL(r.shelf_reserved, 0)), '; ')
        FROM need n LEFT JOIN res r USING (internal_sku) WHERE n.need > IFNULL(r.shelf_reserved, 0));
      IF v_bad IS NOT NULL THEN
        RAISE USING MESSAGE = CONCAT('OPS: для сборки не хватает зарезервированного на полке (снимите с паллет) — ', v_bad);
      END IF;
      CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_post_movements`(
        p_request_id, 'BUILD', v_build_id, p_event_date,
        ARRAY(
          SELECT AS STRUCT component_sku AS internal_sku, component_qty * p_qty_bundles AS qty,
                 'RESERVED' AS from_state, 'SHELF' AS from_location, v_channel AS from_channel,
                 CAST(NULL AS STRING) AS from_container, p_shipment_id AS from_doc,
                 'ASSEMBLED' AS to_state, 'SHELF' AS to_location, v_channel AS to_channel,
                 p_bundle_sku AS to_container, p_shipment_id AS to_doc,
                 CAST(NULL AS STRING) AS reversal_of, CONCAT('FBO-сборка ', p_bundle_sku, ' × ', CAST(p_qty_bundles AS STRING)) AS note
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM`
          WHERE card_sku = p_bundle_sku AND is_bundle ORDER BY component_sku),
        p_source, p_changed_by);
    ELSE
      CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_guard_fresh`();
      SET v_bad = (
        WITH need AS (
          SELECT component_sku AS internal_sku, component_qty * p_qty_bundles AS need
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM` WHERE card_sku = p_bundle_sku AND is_bundle),
        shelf AS (
          SELECT internal_sku, SUM(units) AS shelf_available
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_STOCK_BALANCE`
          WHERE state = 'AVAILABLE' AND location = 'SHELF' GROUP BY 1),
        unconf AS (
          SELECT internal_sku, SUM(api_unconfirmed_units) AS u
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_OZON_ORDER_RECON` GROUP BY 1)
        SELECT STRING_AGG(FORMAT('%s: нужно %d, свободно на полке %d', n.internal_sku, n.need,
                                 GREATEST(IFNULL(s.shelf_available, 0) - IFNULL(u.u, 0), 0)), '; ')
        FROM need n LEFT JOIN shelf s USING (internal_sku) LEFT JOIN unconf u USING (internal_sku)
        WHERE n.need > GREATEST(IFNULL(s.shelf_available, 0) - IFNULL(u.u, 0), 0));
      IF v_bad IS NOT NULL THEN
        RAISE USING MESSAGE = CONCAT('OPS: для FBS-сборки не хватает свободного на полке — ', v_bad);
      END IF;
      SET v_channel = 'FBS';
      CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_post_movements`(
        p_request_id, 'BUILD', v_build_id, p_event_date,
        ARRAY(
          SELECT AS STRUCT component_sku AS internal_sku, component_qty * p_qty_bundles AS qty,
                 'AVAILABLE' AS from_state, 'SHELF' AS from_location, CAST(NULL AS STRING) AS from_channel,
                 CAST(NULL AS STRING) AS from_container, CAST(NULL AS STRING) AS from_doc,
                 'FBS_READY' AS to_state, 'SHELF' AS to_location, 'FBS' AS to_channel,
                 p_bundle_sku AS to_container, CAST(NULL AS STRING) AS to_doc,
                 CAST(NULL AS STRING) AS reversal_of, CONCAT('FBS-сборка ', p_bundle_sku, ' × ', CAST(p_qty_bundles AS STRING)) AS note
          FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_OPS_BOM`
          WHERE card_sku = p_bundle_sku AND is_bundle ORDER BY component_sku),
        p_source, p_changed_by);
    END IF;

    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_BUNDLE_BUILD`
      (build_id, event, bundle_sku, qty_bundles, purpose, shipment_id, channel, reversal_of_build_id,
       event_at, request_id, source, changed_by, note)
    VALUES (v_build_id, 'BUILD', p_bundle_sku, p_qty_bundles, p_purpose,
            IF(p_purpose = 'FBO_SHIPMENT', p_shipment_id, NULL), v_channel, NULL,
            CURRENT_TIMESTAMP(), p_request_id, p_source, p_changed_by, NULL);
    SET v_moves = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = p_request_id);
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_bundle_build', 'OK', NULL, v_build_id, v_moves,
            TO_JSON_STRING(STRUCT(p_bundle_sku AS bundle_sku, p_qty_bundles AS qty, p_purpose AS purpose, p_shipment_id AS shipment_id)),
            CURRENT_TIMESTAMP(), p_source, p_changed_by);
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_bundle_build', 'REJECTED', @@error.message, v_build_id, 0,
            TO_JSON_STRING(STRUCT(p_bundle_sku AS bundle_sku, p_qty_bundles AS qty, p_purpose AS purpose, p_shipment_id AS shipment_id)),
            CURRENT_TIMESTAMP(), p_source, p_changed_by);
    RAISE USING MESSAGE = @@error.message;
  END;

  SELECT request_id, status, doc_id, movements, 'DONE' AS note
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
  WHERE request_id = p_request_id AND status = 'OK';
END;

-- ─────────────────────────────────────────────────────────────────────────────
-- СТОРНО ЗАПРОСА (операция владельца): резерв, сборка. Не для посева (для него — sp_ops_seed_reverse).
-- Сторно резерва = отмена отгрузки; сторно сборки = разборка набора в исходные компоненты.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_request_reverse`(
  p_request_id STRING, p_original_request_id STRING, p_reason STRING,
  p_event_date DATE, p_source STRING, p_changed_by STRING)
BEGIN
  DECLARE v_proc STRING;
  DECLARE v_doc STRING;
  DECLARE v_moves INT64;

  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
    SELECT request_id, status, doc_id, movements, 'IDEMPOTENT_REPLAY' AS note
    FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
    WHERE request_id = p_request_id AND status = 'OK';
    RETURN;
  END IF;

  BEGIN
    BEGIN TRANSACTION;
    UPDATE `project-fa311fc0-4d87-4781-986.evetis_ops.OPS_CONFIG`
       SET config_value = p_request_id, updated_at = CURRENT_TIMESTAMP(), updated_by = p_changed_by
     WHERE config_key = 'ledger_lock';
    IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
        WHERE request_id = p_request_id AND status = 'OK') > 0 THEN
      COMMIT TRANSACTION;
      RETURN;
    END IF;
    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_guard_writeback`();
    IF p_reason IS NULL OR TRIM(p_reason) = '' THEN
      RAISE USING MESSAGE = 'OPS: причина сторно обязательна';
    END IF;

    SET (v_proc, v_doc) = (
      SELECT AS STRUCT ANY_VALUE(procedure_name), ANY_VALUE(doc_id)
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      WHERE request_id = p_original_request_id AND status = 'OK');
    IF v_proc IS NULL OR v_proc NOT IN ('sp_ops_shipment_reserve', 'sp_ops_bundle_build') THEN
      RAISE USING MESSAGE = CONCAT('OPS: сторнировать можно только успешный резерв или сборку; запрос ',
                                   IFNULL(p_original_request_id, '∅'), ' — ', IFNULL(v_proc, 'не найден'));
    END IF;

    CALL `project-fa311fc0-4d87-4781-986.evetis_ops.sp_ops_reverse_core`(
      p_request_id, p_original_request_id, p_event_date, CONCAT('сторно: ', p_reason), p_source, p_changed_by);

    IF v_proc = 'sp_ops_shipment_reserve' THEN
      INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_SHIPMENT`
        (shipment_id, event_seq, status, channel, counterparty, destination, marketplace_order_id, marketplace_ref,
         planned_date, event_at, request_id, source, changed_by, note)
      SELECT shipment_id, event_seq + 1, 'CANCELLED', channel, counterparty, destination, marketplace_order_id, marketplace_ref,
             planned_date, CURRENT_TIMESTAMP(), p_request_id, p_source, p_changed_by, CONCAT('сторно резерва: ', p_reason)
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.V_CT_SHIPMENT_CURRENT` WHERE shipment_id = v_doc;
    ELSE
      INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_BUNDLE_BUILD`
        (build_id, event, bundle_sku, qty_bundles, purpose, shipment_id, channel, reversal_of_build_id,
         event_at, request_id, source, changed_by, note)
      SELECT CONCAT(build_id, '-R'), 'REVERSAL', bundle_sku, qty_bundles, purpose, shipment_id, channel, build_id,
             CURRENT_TIMESTAMP(), p_request_id, p_source, p_changed_by, CONCAT('разборка: ', p_reason)
      FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_BUNDLE_BUILD` WHERE build_id = v_doc AND event = 'BUILD';
    END IF;

    SET v_moves = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_STOCK_MOVEMENT` WHERE request_id = p_request_id);
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_request_reverse', 'OK', p_reason, p_original_request_id, v_moves, NULL,
            CURRENT_TIMESTAMP(), p_source, p_changed_by);
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
      (request_id, procedure_name, status, message, doc_id, movements, payload, logged_at, source, changed_by)
    VALUES (p_request_id, 'sp_ops_request_reverse', 'REJECTED', @@error.message, p_original_request_id, 0, NULL,
            CURRENT_TIMESTAMP(), p_source, p_changed_by);
    RAISE USING MESSAGE = @@error.message;
  END;

  SELECT request_id, status, doc_id, movements, 'DONE' AS note
  FROM `project-fa311fc0-4d87-4781-986.evetis_ops.CT_OPS_REQUEST_LOG`
  WHERE request_id = p_request_id AND status = 'OK';
END;
