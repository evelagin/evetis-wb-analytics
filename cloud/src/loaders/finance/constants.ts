/**
 * M4 · Финансовые отчёты WB (Finance API v1) — константы.
 *
 * Эталон поведения — apps-script/ingestion/Wbfinancedaily.gs (PR-Fin1 v1.3.1 + PR-A, #91).
 * Значения перенесены 1:1; отличия от Apps Script помечены «ОТЛИЧИЕ» и разобраны в
 * docs/ops/MIG_FINANCE_RUNBOOK.md §3.
 */

/** Логическое имя загрузчика: LOADER_RUNS.loader_name, args Job'а. */
export const FINANCE_LOADER_NAME = 'finance';
/** Ручная регистрация WEEKLY-отчётов периода (wbFinDeepBackfillRegister). Только регистрация, без загрузки. */
export const FINANCE_BACKFILL_LOADER_NAME = 'finance-backfill';

/** source_api строк RAW (FIN_DAILY_SOURCE_API_). Входит в row_hash — менять нельзя. */
export const FIN_SOURCE_API = 'WB_API_FIN_V1';

export const FIN_API_LIST_PATH = '/api/finance/v1/sales-reports/list';
/** + reportId строкой (вербатим, BigInt-безопасно). */
export const FIN_API_DET_PATH = '/api/finance/v1/sales-reports/detailed/';

/** Первая неделя нового контура (пн, МСК). В prod — строго это значение (targets.ts). */
export const FIN_PROD_CUTOVER = '2026-07-13';

export const FIN_LOOKBACK_DAYS = 3;          // daily discovery: сегодня−3…сегодня (МСК)
export const FIN_STALE_STARTED_MIN = 120;    // recovery зависших STARTED в манифесте отчётов
export const FIN_MAX_ATTEMPTS = 5;           // потолок попыток одного отчёта
export const FIN_PACER_MS = 61_000;          // 1 запрос/мин на ВСЕ методы Finance API
export const FIN_REQ_MARGIN_MS = 30_000;     // запас на сам запрос и обработку сверх ожидания пейсера
export const FIN_PAGE_LIMIT = 100_000;       // limit detailed
export const FIN_LIST_LIMIT = 1_000;         // limit list (+ offset-пагинация)
export const FIN_LIST_PAGE_GUARD = 20;
export const FIN_DET_PAGE_GUARD = 100;
export const FIN_METRIC_TOL = 0.01;          // допуск сверки метрик, ₽
export const FIN_LOAD_BATCH = 10_000;        // строк на load-job (отчёт меньше батча → один атомарный job)

/**
 * ОТЛИЧИЕ: бюджет прогона. В Apps Script — 270 000 мс (лимит исполнения 6 мин), из-за чего
 * понедельничный тик с WEEKLY уходил в PARTIAL и дочитывал хвост в следующем слоте.
 * Cloud Run Job ограничен своим timeout и lease LOADER_RUNS (30 мин), поэтому по умолчанию 20 мин.
 */
export const FIN_DEFAULT_BUDGET_MS = 1_200_000;

/**
 * ОТЛИЧИЕ: повтор запроса на 429/5xx/сетевой сбой (в Apps Script повтора нет: ответ ≠ 200 сразу
 * переводит отчёт в ERROR, повтор — следующим тиком). Каждый повтор проходит через тот же пейсер
 * 61 с, поэтому лимит WB не нарушается.
 */
export const FIN_HTTP_MAX_RETRIES = 2;
/** Верхняя граница паузы по заголовку X-Ratelimit-Retry / Retry-After. */
export const FIN_RETRY_AFTER_CAP_MS = 180_000;

/** Метрики list (имя в ответе → колонка манифеста). Порядок важен: тот же в RECON. */
export const FIN_LIST_METRICS: ReadonlyArray<readonly [string, ListMetricCol]> = [
  ['forPaySum', 'list_forpay'],
  ['retailAmountSum', 'list_retail'],
  ['deliveryServiceSum', 'list_delivery'],
  ['paidStorageSum', 'list_storage'],
  ['paidAcceptanceSum', 'list_acceptance'],
  ['deductionSum', 'list_deduction'],
  ['penaltySum', 'list_penalty'],
];

/** Метрики detailed (поле строки → колонка манифеста). */
export const FIN_DET_METRICS: ReadonlyArray<readonly [string, DetMetricCol]> = [
  ['forPay', 'det_forpay'],
  ['retailAmount', 'det_retail'],
  ['deliveryService', 'det_delivery'],
  ['paidStorage', 'det_storage'],
  ['paidAcceptance', 'det_acceptance'],
  ['deduction', 'det_deduction'],
  ['penalty', 'det_penalty'],
];

export type ListMetricCol =
  | 'list_forpay' | 'list_retail' | 'list_delivery' | 'list_storage'
  | 'list_acceptance' | 'list_deduction' | 'list_penalty';
export type DetMetricCol =
  | 'det_forpay' | 'det_retail' | 'det_delivery' | 'det_storage'
  | 'det_acceptance' | 'det_deduction' | 'det_penalty';

/** Поля, которые WB отдаёт int64 больше 2^53: держим строками (quoted-transform до JSON.parse). */
export const FIN_BIGINT_FIELDS = ['reportId', 'rrdId', 'giId', 'shkId'] as const;

/** reportType строго ∈ {1,2} (v1.3.1): тип 3 не исследован probe'ом — его появление = отказ. */
export const FIN_REPORT_TYPES: ReadonlySet<number> = new Set([1, 2]);

/** Колонки, без которых тик не работает (эквивалент C0-маркера Apps Script). */
export const FIN_RAW_REQUIRED_COLUMNS = ['report_period', 'report_type', 'run_id', 'raw_json', '_rr_date', 'rrd_id', 'report_id'] as const;
