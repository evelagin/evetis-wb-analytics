-- ============================================================================
-- Stage ADS-2 — история конфигурации рекламных кампаний WB и кампанийных ставок.
-- Дата: 06.09.2026. Док: docs/ADS2_CAMPAIGN_CONFIG_SNAPSHOT_2026-09-06.md
--
-- Read-model. Ничего не пересчитывает, ничего не досоздаёт, RAW не трогает.
--
-- 🔴 ЭТО НЕ СТАВКИ ПО ПОИСКОВЫМ ЗАПРОСАМ. Здесь — кампанийная ставка на РАЗМЕЩЕНИЕ
--    (search / recommendations) из RAW_WB_ADV_CAMPAIGNS.raw_json.nm_settings[].bids_kopecks.
--    Ставки по ключам живут отдельно в RAW_WB_ADV_QUERY_BIDS (грейн advert × nm × norm_query,
--    источник normquery/get-bids, история только с 14.08.2026). Это разные сущности с
--    разными грейнами, разной историей и разными единицами. Смешивать их запрещено.
--
-- ── ГРЕЙН: snapshot_ts × advert_id × nm_index ───────────────────────────────
-- Гипотеза ADS-0 «snapshot_date × advert_id × nm_id» ОТВЕРГНУТА по данным:
--   • (load_ts, advert_id) уникален: 22 291 ключ = 22 291 строка, дублей 0;
--   • (date, advert_id) даёт 21 011 — свёртка в сутки потеряла бы 1 280 строк;
--   • 1 280 пар «сутки × кампания» имеют 2 снимка за день;
--   • 🔴 22 пары имеют РАЗНУЮ конфигурацию внутри одних суток (в основном смена
--     status 9↔11) — это реальные внутридневные состояния, молча схлопывать их нельзя.
-- nm_index сохранён в ключе намеренно: сегодня nm_settings всегда массив длины 1
-- либо JSON null (проверено: min_len = max_len = 1), поэтому фактически грейн равен
-- snapshot_ts × advert_id. Если WB начнёт присылать несколько nm_settings, вью
-- честно вырастет в строках, а не потеряет данные, и это будет видно по
-- nm_settings_count. Суточный срез — отдельная вью V_ADV_CAMPAIGN_CONFIG_DAILY.
--
-- ── ИСТОЧНИКИ ПОЛЕЙ (проверено на 22 291 строке) ────────────────────────────
--   advert_id        плоская колонка advertId; сверена с raw_json.$.id — расхождений 0
--   campaign_type_raw плоская колонка type; в raw_json ОТСУТСТВУЕТ. Единственное
--                    наблюдаемое значение '9'. Семантика НЕ ДОКАЗАНА, имя _raw.
--   всё остальное    raw_json (плоские колонки payment_type/name/campName/nm_ids
--                    пусты во всех 22 291 строках — использовать их нельзя)
--
-- ── NULL-СЕМАНТИКА (§4) ─────────────────────────────────────────────────────
--   nm_settings = JSON null у 11 752 строк (52,7 %, 226 кампаний) — у кампании НЕТ
--   пономенклатурной настройки. Тогда nm_id, обе ставки и subject = NULL. Это
--   состояние «настройки не существует», а не «ставка равна нулю».
--   Где nm_settings есть (10 539 элементов), обе ставки присутствуют ВСЕГДА и
--   не бывают NULL. Ноль — РЕАЛЬНОЕ значение WB: search=0 в 208 элементах,
--   recommendations=0 в 2 520. Zero-fill запрещён, NULL и 0 означают разное.
--
-- ── ENUM (§5) ───────────────────────────────────────────────────────────────
--   status_raw ∈ {7, 9, 11} — числовые коды WB. Расшифровка НЕ ДОКАЗАНА, имён не
--   придумываем. payment_type_raw ∈ {cpc, cpm} и bid_type_raw ∈ {manual, unified}
--   приходят строками от WB и самоописательны, но суффикс _raw сохранён: нормализация
--   — задача следующего этапа. appType здесь не участвует вовсе.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_raw.V_ADV_CAMPAIGN_CONFIG_SNAPSHOT`
OPTIONS (description = 'Stage ADS-2. История конфигурации кампаний WB и КАМПАНИЙНЫХ ставок на размещение. Грейн snapshot_ts × advert_id × nm_index. НЕ путать с RAW_WB_ADV_QUERY_BIDS (ставки по поисковым запросам). Ставки в копейках; поля _rub — производные kopecks/100. NULL = настройки нет; 0 = WB реально вернул ноль.') AS
SELECT
  -- ── ключ ──
  SAFE_CAST(c.load_ts AS TIMESTAMP)                                    AS snapshot_ts,
  DATE(SAFE_CAST(c.load_ts AS TIMESTAMP))                              AS snapshot_date,
  SAFE_CAST(c.advertId AS INT64)                                       AS advert_id,
  nm_index,
  -- ── номенклатурная настройка (NULL, если nm_settings отсутствует) ──
  SAFE_CAST(JSON_VALUE(e, '$.nm_id') AS INT64)                         AS nm_id,
  SAFE_CAST(JSON_VALUE(e, '$.subject.id') AS INT64)                    AS subject_id,
  JSON_VALUE(e, '$.subject.name')                                      AS subject_name,
  -- ── кампания ──
  JSON_VALUE(c.raw_json, '$.settings.name')                            AS campaign_name,
  JSON_VALUE(c.raw_json, '$.status')                                   AS status_raw,
  c.type                                                               AS campaign_type_raw,
  JSON_VALUE(c.raw_json, '$.settings.payment_type')                    AS payment_type_raw,
  JSON_VALUE(c.raw_json, '$.bid_type')                                 AS bid_type_raw,
  JSON_VALUE(c.raw_json, '$.currency')                                 AS currency,
  SAFE_CAST(JSON_VALUE(c.raw_json, '$.restrictions.can_change_nms') AS BOOL) AS can_change_nms,
  -- ── размещения: КОНФИГУРАЦИЯ, а не факт показов ──
  SAFE_CAST(JSON_VALUE(c.raw_json, '$.settings.placements.search') AS BOOL)          AS placement_search_enabled,
  SAFE_CAST(JSON_VALUE(c.raw_json, '$.settings.placements.recommendations') AS BOOL) AS placement_recommendations_enabled,
  -- ── ставки: единицы в имени, производные явно ──
  SAFE_CAST(JSON_VALUE(e, '$.bids_kopecks.search') AS INT64)           AS search_bid_kopecks,
  SAFE_CAST(JSON_VALUE(e, '$.bids_kopecks.recommendations') AS INT64)  AS recommendations_bid_kopecks,
  SAFE_CAST(JSON_VALUE(e, '$.bids_kopecks.search') AS INT64) / 100     AS search_bid_rub,
  SAFE_CAST(JSON_VALUE(e, '$.bids_kopecks.recommendations') AS INT64) / 100 AS recommendations_bid_rub,
  -- ── временные метки кампании (не ingestion) ──
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', JSON_VALUE(c.raw_json, '$.timestamps.updated')) AS settings_updated_at,
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', JSON_VALUE(c.raw_json, '$.timestamps.created')) AS campaign_created_at,
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', JSON_VALUE(c.raw_json, '$.timestamps.started')) AS campaign_started_at,
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', JSON_VALUE(c.raw_json, '$.timestamps.deleted')) AS campaign_deleted_at,
  -- ── страховка от молчаливой потери: сегодня всегда 0 или 1 ──
  IFNULL(ARRAY_LENGTH(JSON_QUERY_ARRAY(c.raw_json, '$.nm_settings')), 0) AS nm_settings_count,
  -- ── lineage ──
  c.run_id,
  c.source_method,
  c.processed_status
FROM `wb_raw.RAW_WB_ADV_CAMPAIGNS` c
-- LEFT JOIN обязателен: у 52,7 % снимков nm_settings = JSON null, и они ДОЛЖНЫ
-- остаться строкой конфигурации кампании, а не исчезнуть. Fan-out невозможен:
-- массив либо отсутствует, либо содержит ровно один элемент.
LEFT JOIN UNNEST(JSON_QUERY_ARRAY(c.raw_json, '$.nm_settings')) AS e WITH OFFSET AS nm_index
WHERE c.processed_status = 'raw';

-- ── Детерминированный суточный срез ─────────────────────────────────────────
-- 🔴 ИСПРАВЛЕНО в Stage ADS-3 §0.2 (06.09.2026). Первая редакция брала последнюю
--    строку по (snapshot_date, advert_id, nm_index) НЕЗАВИСИМО для каждого nm_index.
--    Пока nm_settings — массив длины ≤ 1, это безвредно, но контракт был небезопасен:
--    как только у кампании появятся две номенклатуры и одну из них удалят, свёртка
--    склеит СИНТЕТИЧЕСКОЕ состояние из разных снимков. Доказано фикстурой:
--      снимок 10:00 = [nm 111 bid 10000, nm 222 bid 20000]
--      снимок 20:00 = [nm 111 bid 15000]            (nm 222 удалили)
--      старый алгоритм → idx0 nm=111 bid=15000 (20:00) + idx1 nm=222 bid=20000 (10:00)
--      то есть удалённая номенклатура воскресает и подаётся как текущее состояние.
--    Правильный порядок: СНАЧАЛА выбрать ЦЕЛЫЙ снимок кампании за сутки, и только
--    ПОТОМ отдать его nm-строки. Тогда все строки суток принадлежат одному
--    snapshot_ts по построению.
-- Правило выбора: последний снимок суток по snapshot_ts; при равенстве — больший
-- run_id. Детерминизм обеспечен уникальностью (snapshot_ts, advert_id).
-- Вью НЕ заменяет основную: 22 пары «сутки × кампания» имеют разные конфигурации
-- внутри дня, и здесь останется только последняя из них.
CREATE OR REPLACE VIEW `wb_raw.V_ADV_CAMPAIGN_CONFIG_DAILY`
OPTIONS (description = 'Stage ADS-2 (исправлено ADS-3 §0.2). Суточный срез V_ADV_CAMPAIGN_CONFIG_SNAPSHOT: ЦЕЛЫЙ последний снимок кампании за сутки. Строки одних суток гарантированно принадлежат одному snapshot_ts. Внутридневные состояния здесь ТЕРЯЮТСЯ по построению — для них использовать snapshot-вью.') AS
WITH chosen AS (
  SELECT * EXCEPT(_rn) FROM (
    SELECT DISTINCT snapshot_date, advert_id, snapshot_ts, run_id,
           ROW_NUMBER() OVER (PARTITION BY snapshot_date, advert_id
                              ORDER BY snapshot_ts DESC, run_id DESC) AS _rn
    FROM `wb_raw.V_ADV_CAMPAIGN_CONFIG_SNAPSHOT`)
  WHERE _rn = 1)
SELECT s.*
FROM `wb_raw.V_ADV_CAMPAIGN_CONFIG_SNAPSHOT` s
JOIN chosen c
  USING (snapshot_date, advert_id, snapshot_ts, run_id);
