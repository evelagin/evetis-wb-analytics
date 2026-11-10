/**
 * M2 — константы загрузчиков statistics-api. Значения перенесены из Apps Script 1:1;
 * у каждой указан исходник. Менять только вместе с CHANGELOG и паритетом.
 */

// ── Заказы (WbOrdersLoader.gs / WbOrdersBigQuery.gs) ──────────────────────────
export const WB_ORDERS_API_PATH = '/api/v1/supplier/orders';
export const ORDERS_SOURCE_API = 'WB_API_ORDERS';
/** load_id инкремента: 'ORD_INC_' + yyyyMMdd_HHmmss МСК (runWbOrdersIncremental). */
export const ORDERS_INC_LOADID_PREFIX = 'ORD_INC_';
/** Лимит страниц одного прохода (WB_ORDERS_API_MAX_PAGES_). */
export const ORDERS_MAX_PAGES = 30;
/** Лимит строк одного ответа WB (WB_ORDERS_API_ROWS_CAP_): отличает «дренаж» от «упор в лимит». */
export const ORDERS_ROWS_CAP = 80_000;
/**
 * Пауза между страницами. В Apps Script 2 с (WB_ORDERS_API_PAGE_PAUSE_), и вторая страница почти
 * всегда получает 429 и ждёт повтора — лимит WB 1 запрос/мин на метод. Здесь пауза сразу 61 с:
 * результат тот же, запросов в WB меньше. Предел времени Cloud Run Job это позволяет (timeout 1800 с).
 */
export const ORDERS_PAGE_PAUSE_MS = 61_000;

/**
 * Канонический порядок колонок RAW_WB_ORDERS (ORDERS_RAW_HEADERS_). Служебная `_order_date` (DATE,
 * партиция) в список не входит — её вычисляет конвертер строки.
 */
export const ORDERS_RAW_HEADERS = [
  'load_id', 'loaded_at', 'source_api', 'row_hash', 'raw_row_number',
  'srid', 'order_uid', 'order_dt', 'cancel_dt', 'is_cancel',
  'wb_nm_id', 'wb_vendor_code', 'barcode', 'internal_sku', 'sku_match_status',
  'category', 'subject', 'brand', 'tech_size', 'warehouse_name',
  'region_name', 'country_name', 'price_with_disc', 'quantity', 'sticker_id',
  'is_duplicate', 'processed_status', 'error_message',
  'last_change_date',
  'total_price', 'discount_percent', 'spp', 'finished_price',
] as const;

// ── Продажи и возвраты (WbSalesReturnsLoader.gs.gs / WbSalesReturnsBigQuery.gs) ──
export const WB_SALES_API_PATH = '/api/v1/supplier/sales';
export const SALES_SOURCE_API = 'WB_API_SALES';
/** load_id инкремента и сверки (runWbSalesIncremental / runWbSalesNightReconcile). */
export const SALES_INC_LOADID_PREFIX = 'SALE_INC_';
export const SALES_RECON_LOADID_PREFIX = 'SALE_RECON_';
/** Лимит строк ответа (WB_SALES_API_ROWS_CAP_): упор = граница обрезана → ничего не пишем. */
export const SALES_ROWS_CAP = 80_000;

/** SALES_RAW_HEADERS_ (порядок). Служебная `_sale_date` (DATE, партиция) — вне списка. */
export const SALES_RAW_HEADERS = [
  'load_id', 'loaded_at', 'source_api', 'row_hash', 'raw_row_number',
  'sale_id', 'srid', 'g_number', 'income_id',
  'sale_dt', 'last_change_date',
  'wb_nm_id', 'wb_vendor_code', 'barcode',
  'internal_sku', 'sku_match_status',
  'category', 'subject', 'brand', 'tech_size',
  'warehouse_name', 'warehouse_type', 'region_name', 'oblast_okrug_name', 'country_name',
  'total_price', 'discount_percent', 'spp', 'payment_sale_amount', 'price_with_disc', 'finished_price', 'for_pay',
  'is_supply', 'is_realization',
  'operation_type', 'is_return',
  'sticker_id',
  'processed_status', 'error_message', 'raw_json',
] as const;

/** Типизация колонок RAW продаж (SALES_BQ_*_FIELDS_); остальные STRING. */
export const SALES_INT_FIELDS: ReadonlySet<string> = new Set(['raw_row_number', 'income_id', 'wb_nm_id']);
export const SALES_NUMERIC_FIELDS: ReadonlySet<string> = new Set([
  'total_price', 'discount_percent', 'spp', 'payment_sale_amount', 'price_with_disc', 'finished_price', 'for_pay',
]);
export const SALES_BOOL_FIELDS: ReadonlySet<string> = new Set(['is_supply', 'is_realization', 'is_return']);

// ── HTTP (wbFetchWithRetry_, Utils.gs:157; параметры вызова у заказов и продаж одинаковы) ──
export const STATS_HTTP_MAX_RETRIES = 3;
export const STATS_HTTP_BASE_DELAY_MS = 20_000;
export const STATS_HTTP_MAX_DELAY_MS = 60_000;
// 401/403 сюда НЕ входят и входить не должны: повтор отклонённого токена не лечит, а тратит лимит WB
// (1 запрос/мин на метод). Отказ авторизации — детерминированный WB_STATS_AUTH (index.ts).
export const STATS_HTTP_RETRY_CODES: ReadonlySet<number> = new Set([429, 500, 502, 503, 504]);
/** Префикс кодов токена потока: WB_STATS_TOKEN_EXPIRED, WB_STATS_AUTH (http/wbToken.ts). */
export const WB_STATS_CODE_PREFIX = 'WB_STATS';
