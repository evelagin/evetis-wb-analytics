/**
 * M5: поставки WB (supplies-api) — константы. Значения 1:1 с Apps Script
 * `apps-script/ingestion/Wbsuppliesrawloader.gs` (WBS_*), кроме помеченных «Cloud Run».
 */

export const WB_SUPPLIES_HOST = 'https://supplies-api.wildberries.ru';
/** Секрет категории «Поставки». В Secret Manager его на 10.11.2026 НЕТ — создаёт владелец. */
export const WB_SUPPLIES_SECRET = 'WB_TOKEN_SUPPLIES';

/** Боевые таблицы — те же, что пишет Apps Script (WBS_TBL_HEADER_ / WBS_TBL_GOODS_). */
export const WB_SUPPLIES_PROD_TABLES = {
  header: 'RAW_WB_SUPPLIES',
  goods: 'RAW_WB_SUPPLIES_GOODS',
} as const;
export const SHADOW_SUFFIX = '__CR';

/** Список и состав: limit=1000, offset=0; выборка ≥ limit — отказ (как в Apps Script). */
export const WB_SUPPLIES_PAGE_LIMIT = 1000;
/** Состоявшаяся поставка: statusID 5 и непустой factDate (WBS_STATUS_DONE_). */
export const WB_SUPPLIES_STATUS_DONE = 5;
/** Пауза после каждого запроса карточки и состава, мс (WBS_PAUSE_MS_). */
export const WB_SUPPLIES_PAUSE_MS = 900;
/** Скользящее перекрытие по завершённым, суток (WBS_OVERLAP_DAYS_, §7.3 проекта). */
export const WB_SUPPLIES_OVERLAP_DAYS = 14;
/**
 * HTTP-повторы: Apps Script — 3 попытки с паузой 4 с × номер попытки на 429/5xx/сеть.
 * Cloud Run: 4 попытки (3 повтора) 4 → 8 → 16 с — на одну больше, потому что прогон больше
 * не упирается в 6 минут Apps Script.
 */
export const WB_SUPPLIES_HTTP_MAX_RETRIES = 3;
export const WB_SUPPLIES_HTTP_BACKOFF_BASE_MS = 4000;
export const WB_SUPPLIES_HTTP_BACKOFF_CAP_MS = 16000;
/**
 * Cloud Run: мягкий бюджет прогона. Apps Script держал 240 с из-за лимита 6 минут и выходил
 * «PARTIAL» без ошибки (тихо). Здесь бюджет 20 минут при timeout Job'а 30 минут; при упоре
 * уже собранное коммитится, а прогон завершается ОШИБКОЙ SUPPLIES_PARTIAL (видно в LOADER_RUNS).
 */
export const WB_SUPPLIES_TIME_BUDGET_MS = 20 * 60 * 1000;
/** Cloud Run: объектов на один коммит (goods-load → header-load). */
export const WB_SUPPLIES_COMMIT_CHUNK = 40;
/** Cloud Run: предупреждать о сроке токена заранее, суток. */
export const WB_TOKEN_EXPIRY_WARN_DAYS = 14;
export const TZ_MSK = 'Europe/Moscow';
