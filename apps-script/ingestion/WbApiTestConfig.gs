/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbApiTestConfig.gs  v1.0
 *
 * Конфигурация безопасного ТЕСТОВОГО контура WB API.
 * Выполняет тестовые запросы по docs/wb_api_test_plan.md (T1–T11),
 * сохраняет raw JSON в отдельную Drive-папку, считает контрольные
 * суммы. НЕ пишет в боевые листы, НЕ меняет production-код,
 * НЕ создаёт production-загрузчиков.
 *
 * ВАЖНО — этот контур:
 *   - не вызывает production import-функции;
 *   - не открывает на запись RAW_WB_* / UNIT_SKU_DAILY / PNL_TOTAL;
 *   - читает SKU_MASTER только на чтение (для unmatched nmId);
 *   - не хранит и не логирует токены;
 *   - результаты складывает ТОЛЬКО в Drive.
 *
 * Все публичные функции начинаются с testWbApi… (см. WbApiTestRunner.gs).
 * ══════════════════════════════════════════════════════════════
 */


// ═══════════════════════════════════════
// ЗАЩИТНЫЙ ФЛАГ ТЕСТ-РЕЖИМА
// ═══════════════════════════════════════

/**
 * Главный предохранитель. Если !== true — раннер не запускается
 * (см. assertWbApiTestMode_() в WbApiTestUtils.gs).
 */
var WB_API_TEST_MODE = true;


// ═══════════════════════════════════════
// ХОСТЫ ПО КАТЕГОРИЯМ ТОКЕНОВ
// ═══════════════════════════════════════

var WB_API_TEST_HOST_FINANCE_    = 'https://finance-api.wildberries.ru';
var WB_API_TEST_HOST_STATISTICS_ = 'https://statistics-api.wildberries.ru';
var WB_API_TEST_HOST_ANALYTICS_  = 'https://seller-analytics-api.wildberries.ru';
var WB_API_TEST_HOST_PROMOTION_  = 'https://advert-api.wildberries.ru';


// ═══════════════════════════════════════
// КОНТРОЛЬНЫЙ ПЕРИОД (единый для всех тестов)
// ═══════════════════════════════════════

var WB_API_TEST_DATE_FROM_ = '2026-05-18';
var WB_API_TEST_DATE_TO_   = '2026-05-24';

/** ISO-границы для legacy finance (T2): начало/конец суток. */
var WB_API_TEST_DATE_FROM_ISO_ = '2026-05-18T00:00:00';
var WB_API_TEST_DATE_TO_ISO_   = '2026-05-24T23:59:59';


// ═══════════════════════════════════════
// ТОКЕНЫ (только имена Script Properties — НЕ значения)
// ═══════════════════════════════════════

/** Категория токена → ключ Script Property. */
var WB_API_TEST_TOKEN_KEYS_ = {
  Finance:    'WB_TOKEN_FINANCE',
  Statistics: 'WB_TOKEN_STATISTICS',
  Analytics:  'WB_TOKEN_ANALYTICS',
  Promotion:  'WB_TOKEN_PROMOTION'
};

/** Property с ID Drive-папки для результатов. */
var WB_API_TEST_RESULTS_FOLDER_PROP_ = 'WB_API_TEST_RESULTS_FOLDER_ID';


// ═══════════════════════════════════════
// ЛИСТЫ (только чтение)
// ═══════════════════════════════════════

/** Единственный лист, который тестовый контур читает (read-only). */
var WB_API_TEST_SKU_MASTER_SHEET_ = 'SKU_MASTER';

/** Кандидаты заголовков колонки nmId в SKU_MASTER. */
var WB_API_TEST_SKU_NMID_HEADERS_ = ['wb_nm_id', 'nmid', 'nm_id', 'nmId', 'nm'];


// ═══════════════════════════════════════
// ЛИМИТЫ / ПАУЗЫ / ПОРОГИ
// ═══════════════════════════════════════

/** Пагинация по rrdId/rrd_id — защита от бесконечного цикла. */
var WB_API_TEST_MAX_PAGES_ = 12;

/** Лимит строк за один запрос (finance/legacy). */
var WB_API_TEST_PAGE_LIMIT_ = 100000;

/** Пауза между страницами legacy finance (rate limit ~1 запрос/мин). */
var WB_API_TEST_FINANCE_PAGE_PAUSE_MS_ = 61000;

/** Пауза между страницами прочих GET (мягкая). */
var WB_API_TEST_SOFT_PAUSE_MS_ = 1500;

/** Ретрай при HTTP 429. */
var WB_API_TEST_RETRY_429_ = 3;
var WB_API_TEST_RETRY_429_PAUSE_MS_ = 21000;

/** Task-based опрос статуса (create → status → download). */
var WB_API_TEST_MAX_POLLS_ = 20;
var WB_API_TEST_POLL_INTERVAL_MS_ = 9000;

/** Размер пачки advertId для T8 (WB ограничивает ~50). */
var WB_API_TEST_ADS_BATCH_ = 50;

/** Порог сверки денежных сумм, ₽ (наследуется из FINANCE_RECON_THRESHOLD_ = 1). */
var WB_API_TEST_MONEY_THRESHOLD_ = 1;

/**
 * Порог сверки рекламы Σ fullstats.sum ↔ Σ updSum, ₽.
 * Предварительный API-порог; финальный статус SKU-факта рекламы требует сверки с кабинетом WB.
 */
var WB_API_TEST_ADS_THRESHOLD_ = 1;

/**
 * Процентный порог расхождения T8 ↔ T9 (контроль списаний).
 * T8 (fullstats) — основной управленческий источник рекламного расхода;
 * T9 (upd) — контрольный источник списаний WB. Если |T8 − T9| ≤ 5% от T8 → OK,
 * иначе WARNING (ручная сверка с кабинетом WB). Коэффициент приведения НЕ применяется.
 */
var WB_API_TEST_ADS_DELTA_PCT_ = 5;


// ═══════════════════════════════════════
// СТАТУСЫ РЕШЕНИЯ
// ═══════════════════════════════════════

var WB_API_TEST_DECISION_PASSED_  = 'PASSED';
var WB_API_TEST_DECISION_PARTIAL_ = 'PARTIAL';
var WB_API_TEST_DECISION_FAILED_  = 'FAILED';
var WB_API_TEST_DECISION_BLOCKED_ = 'BLOCKED';
var WB_API_TEST_DECISION_TBD_     = 'TBD';


// ═══════════════════════════════════════
// РЕЕСТР ТЕСТОВ (метаданные, без секретов)
// ═══════════════════════════════════════

/**
 * Описание тестов для логов и summary. method/endpoint —
 * как в утверждённом docs/wb_api_test_plan.md.
 */
var WB_API_TEST_REGISTRY_ = {
  T1:  { name: 'Finance API (new) sales-reports/detailed', category: 'Finance',    method: 'POST', taskBased: false },
  T2:  { name: 'Legacy finance reportDetailByPeriod',      category: 'Statistics', method: 'GET',  taskBased: false },
  T3:  { name: 'Orders supplier/orders',                   category: 'Statistics', method: 'GET',  taskBased: false },
  T4:  { name: 'Sales/Returns supplier/sales',             category: 'Statistics', method: 'GET',  taskBased: false },
  T5:  { name: 'Stocks A warehouse_remains',               category: 'Analytics',  method: 'GET',  taskBased: true  },
  T6:  { name: 'Stocks B stocks-report/wb-warehouses',     category: 'Analytics',  method: 'POST', taskBased: false },
  T7:  { name: 'Ads list promotion/count+adverts',         category: 'Promotion',  method: 'GET/POST', taskBased: false },
  T8:  { name: 'Ads fullstats adv/v3/fullstats',           category: 'Promotion',  method: 'GET',  taskBased: false },
  T9:  { name: 'Ads upd adv/v1/upd',                       category: 'Promotion',  method: 'GET',  taskBased: false },
  T10: { name: 'Paid storage paid_storage',                category: 'Analytics',  method: 'GET',  taskBased: true  },
  T11: { name: 'Acceptance report acceptance_report',      category: 'Analytics',  method: 'GET',  taskBased: true  }
};
