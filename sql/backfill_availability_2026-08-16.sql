-- ═══════════════════════════════════════════════════════════════
-- EVETIS · Ретроспективное заполнение детектора доступности
-- Редакция 4 (16.08.2026) — формула без поправки на возвраты, точный
-- откат через backup-таблицу, шесть строк. APPROVE аудитора получен.
--
-- 🔴 ВЫПОЛНЯТЬ ТОЛЬКО ПОСЛЕ того, как в Apps Script вставлен
-- WbStocksSnapshot.gs с `t6Comparable = m.sum_quantity_physical_t6`.
-- Иначе следующий прогон снова запишет по старой формуле.
--
-- 🔴 ВЫПОЛНЯТЬ БЛОКИ ПО ПОРЯДКУ. Шаг 0 обязателен: без него откат
-- невозможен (см. ниже).
--
-- Затрагивает ТОЛЬКО WB_STOCKS_SNAPSHOTS, шесть строк.
-- RAW_WB_STOCKS и RAW_WB_STOCKS_T5 не трогаются.
--
-- ── ФОРМУЛА ────────────────────────────────────────────────────
--   t6_comparable    = sum_quantity_physical_t6      (потоки в инвариант не входят)
--   availability_gap = t5_wb_rf_sum − t6_comparable
--   пороги: drop = max(100, 5% агрегата) · ok = max(2, 1% агрегата)
--   приоритет: DATA_ERROR → NO_BASELINE → WAREHOUSE_DROP → DEGRADED → OK
--
-- ── СТАТУС ЗАПИСЕЙ: ЧТО ВОСПРОИЗВОДИМО, А ЧТО НЕТ ──────────────
-- ⚠️ Строки 11:45 и 13:59 — HISTORICAL RECONSTRUCTION, а НЕ полностью
-- воспроизводимые manifest-записи. Их `t5_wb_rf_sum` остаётся NULL:
-- те прогоны его не измеряли (старый код поле не писал), а значения
-- 2445 и 2440 взяты из проб 11:52 и 14:01. Мы сознательно не пишем
-- реконструкцию в измеряемое поле — provenance важнее математической
-- полноты. Следствие, которое надо знать: пересчитать их gap из самой
-- строки нельзя, делитель существует только здесь, в комментарии.
--
-- Строки 14:03, 14:25, 14:29, 14:53 — полностью воспроизводимы: у них
-- `t5_wb_rf_sum` измерен самим прогоном, и gap = t5_wb_rf_sum − t6_comparable
-- пересчитывается из сохранённых полей.
--
-- ── РАСЧЁТ ─────────────────────────────────────────────────────
--  время  comparable   T5 агр   источник T5     gap    prev    delta   статус
--  11:45      2405      2445    проба 11:52      40   NULL    NULL    NO_BASELINE
--  13:59      1293      2440    проба 14:01    1147     40   +1107    WAREHOUSE_DROP
--  14:03      1293      2440    сам прогон     1147   1147       0    DEGRADED
--  14:25      1293      1316    сам прогон       23   1147   −1124    DEGRADED
--  14:29      1292      1315    сам прогон       23     23       0    DEGRADED
--  14:53      1292      1315    сам прогон       23     23       0    DEGRADED
--
--  Прогон 14:53 уже сделан НОВОЙ формулой и записал верные t6_comparable 1292,
--  gap 23, ratio 0,9825, статус DEGRADED. Неверны только prev и delta: базой ему
--  послужила строка 14:29 со старым значением −18, отсюда delta 41 вместо 0.
--  Поэтому у него правим ровно два поля, остальные не трогаем.
--  На последующие прогоны это не влияет: они читают `availability_gap` (23), а он верен.
--
--  ⚠️ Переход 14:03 → 14:25 даёт delta −1124: WB переложил объём из
--  обезличенного агрегата в поимённую часть (aggregate 2440→1316 при
--  named 2333→3484). Таксономия называет это DEGRADED. Отдельный статус
--  RECOVERY сознательно НЕ вводим — не усложняем классификатор по одному
--  наблюдению; факт сохранён в gap_delta, статус можно добавить позже.
-- ═══════════════════════════════════════════════════════════════


-- ═══ ШАГ 0 · BACKUP — ВЫПОЛНИТЬ ПЕРВЫМ ═════════════════════════
-- Без него откат невозможен. Строка 14:29 уже содержит значения,
-- записанные прогоном по старой формуле (t6_comparable 1333,
-- availability_gap −18, status DATA_ERROR). Обнуление вернуло бы её
-- не в состояние «до backfill», а в состояние, которого никогда не было.
-- Поэтому откат восстанавливается ИЗ КОПИИ, а не из угаданных значений.
CREATE OR REPLACE TABLE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS_BAK_20260816` AS
SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
WHERE snapshot_id IN ('STOCK_SNAP_20260816_114511_e8807e5f',
                      'STOCK_SNAP_20260816_135911_c7429715',
                      'STOCK_SNAP_20260816_140300_84e458c2',
                      'STOCK_SNAP_20260816_142549_7021c181',
                      'STOCK_SNAP_20260816_142957_e22326a5',
                      'STOCK_SNAP_20260816_145324_e7f0d06c');

-- Контроль: должно быть ровно 6 строк.
SELECT COUNT(*) AS backup_rows
FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS_BAK_20260816`;


-- ═══ ШАГ 1 · BACKFILL ══════════════════════════════════════════

-- ── 1/6 · 11:45 — база (historical reconstruction) ─────────────
UPDATE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
SET t6_comparable          = 2405,
    availability_gap       = 40,
    availability_gap_prev  = NULL,
    availability_gap_delta = NULL,
    availability_ratio     = 2405 / 2445,
    availability_status    = 'NO_BASELINE'
WHERE snapshot_id = 'STOCK_SNAP_20260816_114511_e8807e5f' AND status = 'COMPLETE';

-- ── 2/6 · 13:59 — точка перелома (historical reconstruction) ───
UPDATE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
SET t6_comparable          = 1293,
    availability_gap       = 1147,
    availability_gap_prev  = 40,
    availability_gap_delta = 1107,
    availability_ratio     = 1293 / 2440,
    availability_status    = 'WAREHOUSE_DROP'
WHERE snapshot_id = 'STOCK_SNAP_20260816_135911_c7429715' AND status = 'COMPLETE';

-- ── 3/6 · 14:03 — состояние после выпадения ────────────────────
UPDATE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
SET t6_comparable          = 1293,
    availability_gap       = 1147,
    availability_gap_prev  = 1147,
    availability_gap_delta = 0,
    availability_ratio     = 1293 / 2440,
    availability_status    = 'DEGRADED'
WHERE snapshot_id = 'STOCK_SNAP_20260816_140300_84e458c2' AND status = 'COMPLETE';

-- ── 4/6 · 14:25 — WB переложил объём в поимённую часть ─────────
UPDATE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
SET t6_comparable          = 1293,
    availability_gap       = 23,
    availability_gap_prev  = 1147,
    availability_gap_delta = -1124,
    availability_ratio     = 1293 / 1316,
    availability_status    = 'DEGRADED'
WHERE snapshot_id = 'STOCK_SNAP_20260816_142549_7021c181' AND status = 'COMPLETE';

-- ── 5/6 · 14:29 — переписываем ложный DATA_ERROR ───────────────
-- Единственная строка со значениями по старой формуле. Guard делает
-- запуск идемпотентным: повторное выполнение ничего не изменит.
UPDATE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
SET t6_comparable          = 1292,
    availability_gap       = 23,
    availability_gap_prev  = 23,
    availability_gap_delta = 0,
    availability_ratio     = 1292 / 1315,
    availability_status    = 'DEGRADED'
WHERE snapshot_id = 'STOCK_SNAP_20260816_142957_e22326a5' AND status = 'COMPLETE'
  AND availability_gap IS DISTINCT FROM 23;

-- ── 6/6 · 14:53 — правим только базу и дельту ──────────────────
-- Прогон уже отработал новой формулой: gap 23, ratio 0,9825, DEGRADED — верно.
-- Ошибочны только prev/delta, потому что базой была строка 14:29 со старым −18.
-- t6_comparable, gap, ratio и статус НЕ трогаем: они измерены и посчитаны верно.
UPDATE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
SET availability_gap_prev  = 23,
    availability_gap_delta = 0
WHERE snapshot_id = 'STOCK_SNAP_20260816_145324_e7f0d06c' AND status = 'COMPLETE'
  AND availability_gap_prev IS DISTINCT FROM 23;


-- ═══ ШАГ 2 · ПРОВЕРКА ══════════════════════════════════════════
-- Ожидаем: NO_BASELINE → WAREHOUSE_DROP → DEGRADED ×4
SELECT FORMAT_TIMESTAMP('%H:%M', started_at, 'Europe/Moscow') t,
       t6_comparable, t5_wb_rf_sum, availability_gap, availability_gap_prev,
       availability_gap_delta, ROUND(availability_ratio, 4) ratio,
       availability_status, control_status
FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS`
WHERE DATE(started_at) = '2026-08-16' AND status = 'COMPLETE'
ORDER BY started_at;


-- ═══ ОТКАТ (выполнять только при необходимости) ════════════════
-- Точное восстановление из копии, снятой на шаге 0. Возвращает все
-- шесть строк ровно в то состояние, в котором они были до backfill,
-- включая старые значения строки 14:29.
-- UPDATE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS` t
-- SET t6_comparable          = b.t6_comparable,
--     availability_gap       = b.availability_gap,
--     availability_gap_prev  = b.availability_gap_prev,
--     availability_gap_delta = b.availability_gap_delta,
--     availability_ratio     = b.availability_ratio,
--     availability_status    = b.availability_status
-- FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS_BAK_20260816` b
-- WHERE t.snapshot_id = b.snapshot_id;

-- Копию удалять не раньше, чем детектор отработает несколько суток штатно:
-- DROP TABLE `project-fa311fc0-4d87-4781-986.wb_raw.WB_STOCKS_SNAPSHOTS_BAK_20260816`;
