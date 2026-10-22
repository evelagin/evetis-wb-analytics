/** Константы загрузчика остатков WB (Cloud Run Job, shadow). */
/**
 * PR-Mig1c: значение source_api ВЫРОВНЕНО с боевым Apps Script (WbStocksBigQuery.gs:40).
 * Прежнее 'WB_API_ANALYTICS_STOCKS' расходилось с прод-RAW: на cutover в RAW_WB_STOCKS
 * легли бы два разных значения одного источника. Потребителей, фильтрующих по полю, нет
 * (INFORMATION_SCHEMA.VIEWS, 21.10.2026), но parity обязана быть точной.
 */
export const WB_STOCKS_SOURCE_API = 'WB_API_STOCKS';
export const WB_STOCKS_T6_PATH = '/api/analytics/v1/stocks-report/wb-warehouses';
export const WB_STOCKS_AGG_WAREHOUSE = 'Остальные';

/**
 * Сентинел WB «склад не раскрываем» — появился с обезличиванием складов
 * (WAREHOUSE_DISCLOSURE_DEGRADED_FROM = 2026-08-15, см. DATA_MODEL.md).
 * Это НЕ идентификатор склада: любое отрицательное значение трактуется так же.
 * Зеркало Apps Script WB_STOCKS_ANON_WH_ID_ (WbStocksSnapshot.gs:85).
 */
export const WB_STOCKS_ANON_WH_ID = -999999;

// ── PR-Mig1c: T5 warehouse_remains (зеркало WbStocksSnapshot.gs:25-54) ──────────
export const WB_STOCKS_T5_PATH = '/api/v1/warehouse_remains';
export const WB_STOCKS_T5_CREATE_QUERY =
  '?groupByBrand=false&groupBySubject=false&groupBySa=true&groupByNm=true&groupByBarcode=true&groupBySize=true';
export const WB_STOCKS_T5_SOURCE_API = 'WB_API_STOCKS_T5';
/** С 16.08.2026 T5 отдаёт один агрегат «Склад WB РФ» + поимённые склады + три псевдо-строки. */
export const WB_STOCKS_T5_AGG_NAME = 'Склад WB РФ';
export const WB_STOCKS_T5_PSEUDO: Readonly<Record<string, string>> = {
  'Всего находится на складах': 'PSEUDO_TOTAL',
  'В пути до получателей': 'PSEUDO_TO_CLIENT',
  'В пути возвраты на склад WB': 'PSEUDO_FROM_CLIENT',
};
/**
 * Опрос задачи T5: число опросов (как в Apps Script: 20 × 9 с). Пауза перед опросом —
 * config.stocksT5PollIntervalMs (STOCKS_T5_POLL_INTERVAL_MS, по умолчанию 9000).
 */
export const WB_STOCKS_T5_MAX_POLLS = 20;
/** 429 на T5 — пауза 21 с (как wbStocksHttp_, WbStocksSnapshot.gs:131-134), до 3 повторов. */
export const WB_STOCKS_T5_RETRY_PAUSE_MS = 21000;
/** Таймаут одного HTTP-запроса T5 и общий бюджет T5 (20 × 9 с опросов + повторы с запасом). */
export const WB_STOCKS_T5_HTTP_TIMEOUT_MS = 30000;
export const WB_STOCKS_T5_BUDGET_MS = 8 * 60 * 1000;

// ── Контроль T5/T6 и детектор доступности (WbStocksSnapshot.gs:30, 46-48) ──────
/** Допуск T5/T6, ед.: снимки сняты с разницей в секунды. */
export const WB_STOCKS_CONTROL_TOLERANCE = 2;
/** «Резкий скачок» разрыва = не меньше 100 ед И не меньше 5% агрегата. */
export const WB_STOCKS_AVAIL_DROP_ABS = 100;
export const WB_STOCKS_AVAIL_DROP_REL = 0.05;
/** Разрыв ≤1% агрегата — шум. */
export const WB_STOCKS_AVAIL_OK_REL = 0.01;
