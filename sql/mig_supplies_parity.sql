-- ============================================================================
-- M5 — сверка паритета поставок WB: Apps Script (RAW_WB_SUPPLIES/_GOODS) ↔ Cloud Run (__CR).
-- ТОЛЬКО ЧТЕНИЕ. Регион EU, датасет wb_raw. Каждый запрос — 4 таблицы (минимум ~40 МБ).
--
-- Обе стороны пишут не полный снимок, а инкремент (§7.3 проекта), поэтому сравнивается
-- СОСТОЯНИЕ НА КОНЕЦ СУТОК МСК: по каждой стороне последняя committed-шапка объекта со
-- snapshot_ts до конца дня d (ровно правило V_WB_SUPPLIES_CURRENT, только «as of») и позиции
-- ТОГО ЖЕ run_id (правило V_WB_SUPPLIES_GOODS_CURRENT).
--
-- Вердикт дня:
--   EXACT       — одинаковый набор entity_key; у всех объектов совпали row_hash шапки,
--                 is_completed, card_read_ok, названия складов и отпечаток позиций;
--   DATA_DRIFT  — расхождения есть, но каждое ОБЪЯСНЕНО: у объекта разный updated_dt (WB
--                 изменил его между 02:29 и 02:39) или объект ещё «в пути» (status_id ≠ 5) или
--                 завершён в окне перекрытия 14 суток (карточку WB правит без updated_dt, §7.3);
--   FAIL        — объект есть только на одной стороне, либо расхождение у завершённого вне окна
--                 объекта с тем же updated_dt (такой объект меняться не должен), либо дубль
--                 entity_key внутри run_id (И1) или barcode внутри объекта (И10) на стороне __CR;
--   NO_RUN      — в этот день у одной из сторон не было прогона: день не засчитывается.
-- Приёмка: 5 дней подряд без FAIL и без NO_RUN, из них ≥ 3 EXACT (runbook §4).
--
-- 🔴 С 31.10.2026 Apps Script не пишет (см. runbook §1, диагноз). Пока его токен не заменён,
-- QUERY 1 даёт NO_RUN; доказательство без живого Apps Script — QUERY 3 (повтор замороженного
-- состояния: объекты, не менявшиеся с последнего прогона Apps Script, обязаны совпасть).
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 1 — ВЕРДИКТ ПО ДНЯМ
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT 7 AS lookback_days, 14 AS overlap_days),
days AS (
  SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(
    DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL (SELECT lookback_days FROM params) DAY),
    CURRENT_DATE('Europe/Moscow'))) AS d
),
runs AS (
  SELECT 'p' AS side, DATE(snapshot_ts, 'Europe/Moscow') AS d, COUNT(DISTINCT run_id) AS n
  FROM `wb_raw.RAW_WB_SUPPLIES` GROUP BY 1, 2
  UNION ALL
  SELECT 'c', DATE(snapshot_ts, 'Europe/Moscow'), COUNT(DISTINCT run_id)
  FROM `wb_raw.RAW_WB_SUPPLIES__CR` GROUP BY 1, 2
),
p_h AS (
  SELECT dd.d, h.* FROM days dd JOIN `wb_raw.RAW_WB_SUPPLIES` h
    ON h.snapshot_ts < TIMESTAMP(DATE_ADD(dd.d, INTERVAL 1 DAY), 'Europe/Moscow')
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY dd.d, h.entity_key ORDER BY h.snapshot_ts DESC, h.run_id DESC) = 1
),
c_h AS (
  SELECT dd.d, h.* FROM days dd JOIN `wb_raw.RAW_WB_SUPPLIES__CR` h
    ON h.snapshot_ts < TIMESTAMP(DATE_ADD(dd.d, INTERVAL 1 DAY), 'Europe/Moscow')
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY dd.d, h.entity_key ORDER BY h.snapshot_ts DESC, h.run_id DESC) = 1
),
p_g AS (
  SELECT h.d, g.entity_key, STRING_AGG(g.row_hash, ',' ORDER BY g.barcode, g.row_hash) AS fp, SUM(g.quantity) AS qty, COUNT(*) AS n
  FROM p_h h JOIN `wb_raw.RAW_WB_SUPPLIES_GOODS` g USING (run_id, entity_key) GROUP BY 1, 2
),
c_g AS (
  SELECT h.d, g.entity_key, STRING_AGG(g.row_hash, ',' ORDER BY g.barcode, g.row_hash) AS fp, SUM(g.quantity) AS qty, COUNT(*) AS n,
         COUNT(*) - COUNT(DISTINCT g.barcode) AS dup_barcodes
  FROM c_h h JOIN `wb_raw.RAW_WB_SUPPLIES_GOODS__CR` g USING (run_id, entity_key) GROUP BY 1, 2
),
j AS (
  SELECT d, entity_key,
    CASE WHEN c.entity_key IS NULL THEN 'prod_only' WHEN p.entity_key IS NULL THEN 'cr_only' ELSE 'matched' END AS side,
    p.updated_dt AS p_upd, c.updated_dt AS c_upd,
    COALESCE(c.status_id, p.status_id) AS status_id,
    COALESCE(c.fact_dt, p.fact_dt) AS fact_dt,
    (p.row_hash IS DISTINCT FROM c.row_hash OR p.is_completed IS DISTINCT FROM c.is_completed
      OR p.card_read_ok IS DISTINCT FROM c.card_read_ok OR p.warehouse_name IS DISTINCT FROM c.warehouse_name
      OR p.transit_warehouse_name IS DISTINCT FROM c.transit_warehouse_name
      OR p.actual_warehouse_name IS DISTINCT FROM c.actual_warehouse_name) AS header_diff,
    (pg.fp IS DISTINCT FROM cg.fp) AS goods_diff,
    pg.qty AS p_qty, cg.qty AS c_qty, IFNULL(cg.dup_barcodes, 0) AS c_dup_barcodes
  FROM p_h p FULL OUTER JOIN c_h c USING (d, entity_key)
  LEFT JOIN p_g pg USING (d, entity_key)
  LEFT JOIN c_g cg USING (d, entity_key)
),
classified AS (
  SELECT j.*,
    (header_diff OR goods_diff) AS differs,
    (p_upd IS DISTINCT FROM c_upd
      OR status_id IS DISTINCT FROM 5
      OR DATE(fact_dt, 'Europe/Moscow') >= DATE_SUB(d, INTERVAL (SELECT overlap_days FROM params) DAY)) AS explainable
  FROM j
),
cr_i1 AS (
  SELECT DATE(snapshot_ts, 'Europe/Moscow') AS d, COUNT(*) AS n FROM (
    SELECT snapshot_ts, run_id, entity_key FROM `wb_raw.RAW_WB_SUPPLIES__CR` WHERE TRUE
    QUALIFY COUNT(*) OVER (PARTITION BY run_id, entity_key) > 1) GROUP BY 1
),
per_day AS (
  SELECT d,
    COUNTIF(side = 'matched') AS matched,
    COUNTIF(side = 'prod_only') AS prod_only,
    COUNTIF(side = 'cr_only') AS cr_only,
    COUNTIF(side = 'matched' AND differs) AS differing,
    COUNTIF(side = 'matched' AND differs AND explainable) AS explained,
    COUNTIF(side = 'matched' AND differs AND NOT explainable) AS unexplained,
    COUNTIF(c_dup_barcodes > 0) AS cr_i10,
    SUM(IF(status_id = 5, p_qty, 0)) AS prod_qty_completed,
    SUM(IF(status_id = 5, c_qty, 0)) AS cr_qty_completed
  FROM classified GROUP BY d
)
SELECT pd.d,
  IFNULL(rp.n, 0) AS prod_runs_that_day, IFNULL(rc.n, 0) AS cr_runs_that_day,
  pd.matched, pd.prod_only, pd.cr_only, pd.differing, pd.explained, pd.unexplained,
  IFNULL(i1.n, 0) AS cr_i1_dup_rows, pd.cr_i10,
  pd.prod_qty_completed, pd.cr_qty_completed,
  CASE
    WHEN IFNULL(rp.n, 0) = 0 OR IFNULL(rc.n, 0) = 0 THEN 'NO_RUN'
    WHEN pd.prod_only > 0 OR pd.cr_only > 0 OR pd.unexplained > 0 OR IFNULL(i1.n, 0) > 0 OR pd.cr_i10 > 0 THEN 'FAIL'
    WHEN pd.differing > 0 THEN 'DATA_DRIFT'
    ELSE 'EXACT'
  END AS verdict
FROM per_day pd
LEFT JOIN runs rp ON rp.side = 'p' AND rp.d = pd.d
LEFT JOIN runs rc ON rc.side = 'c' AND rc.d = pd.d
LEFT JOIN cr_i1 i1 ON i1.d = pd.d
ORDER BY pd.d;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 2 — РАСХОЖДЕНИЯ ДНЯ ПООБЪЕКТНО (подставить день в params)
-- ─────────────────────────────────────────────────────────────────────────
WITH params AS (SELECT DATE '2026-11-15' AS d, 14 AS overlap_days),
cut AS (SELECT TIMESTAMP(DATE_ADD((SELECT d FROM params), INTERVAL 1 DAY), 'Europe/Moscow') AS ts),
p_h AS (
  SELECT * FROM `wb_raw.RAW_WB_SUPPLIES` WHERE snapshot_ts < (SELECT ts FROM cut)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY entity_key ORDER BY snapshot_ts DESC, run_id DESC) = 1
),
c_h AS (
  SELECT * FROM `wb_raw.RAW_WB_SUPPLIES__CR` WHERE snapshot_ts < (SELECT ts FROM cut)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY entity_key ORDER BY snapshot_ts DESC, run_id DESC) = 1
),
p_g AS (SELECT g.entity_key, STRING_AGG(g.row_hash, ',' ORDER BY g.barcode, g.row_hash) AS fp, SUM(g.quantity) AS qty
        FROM p_h JOIN `wb_raw.RAW_WB_SUPPLIES_GOODS` g USING (run_id, entity_key) GROUP BY 1),
c_g AS (SELECT g.entity_key, STRING_AGG(g.row_hash, ',' ORDER BY g.barcode, g.row_hash) AS fp, SUM(g.quantity) AS qty
        FROM c_h JOIN `wb_raw.RAW_WB_SUPPLIES_GOODS__CR` g USING (run_id, entity_key) GROUP BY 1)
SELECT entity_key,
  CASE WHEN c.entity_key IS NULL THEN 'prod_only' WHEN p.entity_key IS NULL THEN 'cr_only' ELSE 'matched' END AS side,
  p.run_id AS prod_run, c.run_id AS cr_run, p.status_id AS prod_status, c.status_id AS cr_status,
  p.updated_dt AS prod_upd, c.updated_dt AS cr_upd, p.row_hash = c.row_hash AS header_same,
  pg.fp = cg.fp AS goods_same, pg.qty AS prod_qty, cg.qty AS cr_qty
FROM p_h p FULL OUTER JOIN c_h c USING (entity_key)
LEFT JOIN p_g pg USING (entity_key) LEFT JOIN c_g cg USING (entity_key)
WHERE c.entity_key IS NULL OR p.entity_key IS NULL OR p.row_hash IS DISTINCT FROM c.row_hash OR pg.fp IS DISTINCT FROM cg.fp
ORDER BY side, entity_key;

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 3 — ПОВТОР ЗАМОРОЖЕННОГО СОСТОЯНИЯ (работает без живого Apps Script)
-- Текущее состояние Apps Script ↔ текущее состояние __CR для объектов, у которых updated_dt
-- одинаков на обеих сторонах и которые на момент последнего снимка Apps Script были завершены
-- раньше окна перекрытия (14 суток) — их WB уже не меняет. Cloud Run перечитал их заново, и шапка,
-- позиции, склады и is_completed обязаны совпасть. Ожидание: same_header = same_goods = objects.
-- ─────────────────────────────────────────────────────────────────────────
WITH p_h AS (SELECT * FROM `wb_raw.V_WB_SUPPLIES_CURRENT`),
c_h AS (
  SELECT * FROM `wb_raw.RAW_WB_SUPPLIES__CR` WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY entity_key ORDER BY snapshot_ts DESC, run_id DESC) = 1
),
p_g AS (SELECT g.entity_key, STRING_AGG(g.row_hash, ',' ORDER BY g.barcode, g.row_hash) AS fp
        FROM p_h JOIN `wb_raw.RAW_WB_SUPPLIES_GOODS` g USING (run_id, entity_key) GROUP BY 1),
c_g AS (SELECT g.entity_key, STRING_AGG(g.row_hash, ',' ORDER BY g.barcode, g.row_hash) AS fp
        FROM c_h JOIN `wb_raw.RAW_WB_SUPPLIES_GOODS__CR` g USING (run_id, entity_key) GROUP BY 1)
SELECT
  COUNT(*) AS objects,
  COUNTIF(p.row_hash = c.row_hash AND p.is_completed = c.is_completed AND p.card_read_ok = c.card_read_ok
          AND p.warehouse_name IS NOT DISTINCT FROM c.warehouse_name) AS same_header,
  COUNTIF(pg.fp IS NOT DISTINCT FROM cg.fp) AS same_goods,
  ARRAY_AGG(IF(p.row_hash != c.row_hash OR pg.fp IS DISTINCT FROM cg.fp, p.entity_key, NULL) IGNORE NULLS LIMIT 20) AS differing_keys
FROM p_h p JOIN c_h c USING (entity_key)
LEFT JOIN p_g pg USING (entity_key) LEFT JOIN c_g cg USING (entity_key)
WHERE p.updated_dt IS NOT DISTINCT FROM c.updated_dt
  AND p.is_completed
  AND DATE(p.fact_dt, 'Europe/Moscow') < DATE_SUB(DATE(p.snapshot_ts, 'Europe/Moscow'), INTERVAL 14 DAY);

-- ─────────────────────────────────────────────────────────────────────────
-- QUERY 4 — ОБЯЗАТЕЛЬНАЯ СВЕРКА НА CUTOVER (runbook §4 S4, шаг 5)
-- После первого prod-прогона Cloud Run: текущее состояние prod (V_WB_SUPPLIES_CURRENT) ↔
-- текущее состояние __CR. Shadow начинал с полного снимка, поэтому знает каждый объект «с нуля»;
-- prod после простоя 31.10 → cutover — только то, что выбрал инкремент. Ожидание:
--   only_prod = only_cr = 0 и completed_mismatch = status_mismatch = 0.
-- Расхождение у объекта с разным updated_dt допустимо (WB изменил его между прогонами) и видно в
-- колонке explained; необъяснённое (explained = FALSE) — STOP: prod-расписание не включать,
-- разобрать объект QUERY 2 с подстановкой сегодняшней даты.
-- ─────────────────────────────────────────────────────────────────────────
WITH p AS (SELECT entity_key, status_id, is_completed, updated_dt, run_id FROM `wb_raw.V_WB_SUPPLIES_CURRENT`),
c AS (
  SELECT entity_key, status_id, is_completed, updated_dt, run_id FROM `wb_raw.RAW_WB_SUPPLIES__CR` WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY entity_key ORDER BY snapshot_ts DESC, run_id DESC) = 1
),
j AS (
  SELECT entity_key,
    CASE WHEN c.entity_key IS NULL THEN 'only_prod' WHEN p.entity_key IS NULL THEN 'only_cr' ELSE 'matched' END AS side,
    p.status_id AS prod_status, c.status_id AS cr_status, p.is_completed AS prod_completed, c.is_completed AS cr_completed,
    p.updated_dt AS prod_upd, c.updated_dt AS cr_upd, p.run_id AS prod_run, c.run_id AS cr_run,
    (p.updated_dt IS DISTINCT FROM c.updated_dt) AS explained
  FROM p FULL OUTER JOIN c USING (entity_key)
)
SELECT side, entity_key, prod_status, cr_status, prod_completed, cr_completed, prod_upd, cr_upd, prod_run, cr_run, explained
FROM j
WHERE side != 'matched' OR prod_completed IS DISTINCT FROM cr_completed OR prod_status IS DISTINCT FROM cr_status
ORDER BY explained, side, entity_key;
