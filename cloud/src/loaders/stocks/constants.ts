/** Константы загрузчика остатков WB (Cloud Run Job, shadow). */
export const WB_STOCKS_SOURCE_API = 'WB_API_ANALYTICS_STOCKS';
export const WB_STOCKS_T6_PATH = '/api/analytics/v1/stocks-report/wb-warehouses';
export const WB_STOCKS_AGG_WAREHOUSE = 'Остальные';

/**
 * Сентинел WB «склад не раскрываем» — появился с обезличиванием складов
 * (WAREHOUSE_DISCLOSURE_DEGRADED_FROM = 2026-08-15, см. DATA_MODEL.md).
 * Это НЕ идентификатор склада: любое отрицательное значение трактуется так же.
 * Зеркало Apps Script WB_STOCKS_ANON_WH_ID_ (WbStocksSnapshot.gs:85).
 */
export const WB_STOCKS_ANON_WH_ID = -999999;
