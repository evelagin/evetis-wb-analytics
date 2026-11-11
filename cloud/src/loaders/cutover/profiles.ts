/**
 * Координатор cutover (этап C): декларативные требования по потокам.
 *
 * Здесь только ДАННЫЕ — что считать квалификацией, когда можно ставить fence, сколько ждать дренажа,
 * что считать штатным циклом. Логика — machine.ts / qualification.ts / window.ts, SQL — store.ts.
 * Любое число здесь — решение, которое видно в одном месте и проверяется тестом.
 *
 * Расписания (МСК) — по коду установщиков Apps Script и Terraform потоков на 11.11.2026. Живые триггеры
 * Apps Script могли быть изменены вручную (заказы/продажи — раз в 6 ч с 10.11): перед включением
 * auto_cutover_enabled владелец сверяет их со страницей «Триггеры» (docs/ops/INGEST_CUTOVER_COORDINATOR.md §5).
 */
import type { Stream } from '../../ownership/streams.js';

export type Verdict = 'PASS' | 'FAIL' | 'DRIFT_EXPLAINED' | 'MISSING';
export const VERDICTS: readonly Verdict[] = ['PASS', 'FAIL', 'DRIFT_EXPLAINED', 'MISSING'];

/**
 * Группа проверок квалификации.
 *   daily  — вердикт обязателен в КАЖДЫЙ засчитанный день;
 *   weekly — хотя бы один допустимый вердикт за последние 7 суток окна (weekday — только в этот день недели,
 *            1 = понедельник, ISO) и ни одного недопустимого в засчитанных днях;
 *   once   — хотя бы один допустимый вердикт в засчитанных днях (квалификация digest).
 */
export interface QualGroup {
  group: string;
  cadence: 'daily' | 'weekly' | 'once';
  weekday?: number;
  accept: readonly Verdict[];
}

export interface QualRequirement {
  /** Сопоставимых суток подряд (от самой свежей business_date назад). */
  minComparableDays: number;
  groups: readonly QualGroup[];
  /** Все засчитанные вердикты — по ОДНОМУ image_digest (квалифицируется конкретный образ). */
  sameDigest: boolean;
  /** Самая свежая business_date не старше стольких суток от «сегодня» МСК — иначе квалификация устарела. */
  maxStaleDays: number;
}

/** Плановый запуск писателя потока (МСК). minute 'any' — минута неизвестна: занят весь час. */
export interface ScheduleEvent {
  writer: 'APPS_SCRIPT' | 'CLOUD_RUN';
  label: string;
  hours: readonly number[] | 'any';
  minute: number | 'any';
  /** Дни недели 0..6 (0 = воскресенье, как cron). Нет — каждый день. */
  dow?: readonly number[];
  /** Разброс старта: nearMinute Apps Script — окно ±15 мин; Cloud Scheduler — ±1. */
  jitterMin: number;
  /** Верхняя граница длительности: Apps Script — лимит исполнения, Cloud Run — timeout Job'а. */
  maxRunMin: number;
}

export type Continuity =
  /**
   * Watermark читается загрузчиком Cloud Run из целевой таблицы. Проверки в MONITORING: отметка не
   * откатилась; Cloud Run не писал строк старше legacy; состояние таблицы, которое прочитал ПЕРВЫЙ прогон
   * (MAX строк инкремента до его старта), равно legacy watermark; первая ночная сверка (окно
   * reconcileDays суток) накрыла legacy watermark.
   */
  | { kind: 'table_watermark'; reconcileLoader: string; reconcileDays: number }
  /** Окно/перекрытие загрузчика: первый период Cloud Run − legacy ≤ maxGapDays. */
  | { kind: 'period_gap'; maxGapDays: number }
  /** Полное состояние на каждом прогоне (версия справочника): пропуска быть не может. */
  | { kind: 'state' };

export interface MonitoringRequirement {
  /** Минимум COMPLETE-циклов prod по загрузчику с момента активации. */
  cycles: Readonly<Record<string, number>>;
  /** Хотя бы один COMPLETE этого загрузчика в понедельник МСК (недельный цикл finance). */
  mondayCycleLoader?: string;
  /** Нет ни одного COMPLETE за столько часов после активации → ROLLBACK_REQUIRED (писатель не работает). */
  firstCycleDeadlineH: number;
  /** Живой downstream: mart — COMPLETE витрины после активации; ref_active — указатель REF на версию после неё. */
  downstream: 'mart' | 'ref_active' | null;
}

/** Таблицы потока в датасете wb_raw (точные имена и префиксы) — те же, что WB_WRITER_STREAM_TABLES_ в WriterGate.gs. */
export interface WriteTables {
  exact: readonly string[];
  prefixes: readonly string[];
}

export interface StreamProfile {
  stream: Stream;
  /**
   * Поток ведёт координатор. Остатки (M1) — НЕТ до приёмки координатора: их переключение идёт утверждённой
   * процедурой MIG1B (снятие триггера с readback, MIG_FINAL_INTEGRATION_GATE §A.7). Включить —
   * CUTOVER_INCLUDE_STOCKS=1 после отдельного ACK.
   */
  coordinated: boolean;
  /** Таблицы, куда пишет поток (оба писателя): дренаж ищет по ним незавершённые задания BigQuery. */
  writeTables: WriteTables;
  /** Загрузчики Cloud Run потока (LOADER_RUNS.loader_name). */
  cloudLoaders: readonly string[];
  qualification: QualRequirement;
  schedules: readonly ScheduleEvent[];
  /** Наибольший timeout Job'а потока (мин): дренаж Cloud Run при откате ждёт не меньше + запас. */
  cloudJobTimeoutMin: number;
  continuity: Continuity;
  monitoring: MonitoringRequirement;
}

/** Лимит исполнения Apps Script (консьюмерский аккаунт) — 6 мин; дренаж ждёт не меньше 7. */
export const LEGACY_DRAIN_MIN = 7;
/** Запас поверх timeout Job'а Cloud Run при откате. */
export const CLOUD_DRAIN_MARGIN_MIN = 5;
/** Окно поиска «STARTED без исхода» перед fence. */
export const DRAIN_LOOKBACK_H = 24;
/** Fence ставится, только если [t, t + дренаж + запас] свободно от запусков потока. */
export const FENCE_GUARD_MIN = 5;

const PASS: readonly Verdict[] = ['PASS'];
const PASS_OR_EXPLAINED: readonly Verdict[] = ['PASS', 'DRIFT_EXPLAINED'];
const AS = (label: string, hours: readonly number[] | 'any', minute: number | 'any', extra: Partial<ScheduleEvent> = {}): ScheduleEvent =>
  ({ writer: 'APPS_SCRIPT', label, hours, minute, jitterMin: 15, maxRunMin: 6, ...extra });
const CR = (label: string, hours: readonly number[], minute: number, maxRunMin: number, extra: Partial<ScheduleEvent> = {}): ScheduleEvent =>
  ({ writer: 'CLOUD_RUN', label, hours, minute, jitterMin: 1, maxRunMin, ...extra });

export const PROFILES: Readonly<Record<Stream, StreamProfile>> = Object.freeze({
  stocks: {
    stream: 'stocks',
    coordinated: false,
    writeTables: { exact: ['RAW_WB_STOCKS', 'RAW_WB_STOCKS_T5', 'WB_STOCKS_SNAPSHOTS'], prefixes: [] },
    cloudLoaders: ['stocks'],
    // MIG_FINAL_INTEGRATION_GATE §A.2/A.4: 5 суток паритета T5/T6 + квалификация конкретного digest D′.
    qualification: {
      minComparableDays: 5,
      groups: [
        { group: 'STOCKS_T6', cadence: 'daily', accept: PASS },
        { group: 'STOCKS_T5', cadence: 'daily', accept: PASS },
        { group: 'STOCKS_DIGEST', cadence: 'once', accept: PASS },
      ],
      sameDigest: true,
      maxStaleDays: 2,
    },
    schedules: [
      AS('runWbStocksSnapshot', [6], 30),
      CR('wb-stocks-prod', [6], 30, 30),
    ],
    cloudJobTimeoutMin: 30,
    continuity: { kind: 'period_gap', maxGapDays: 1 },
    monitoring: { cycles: { stocks: 3 }, firstCycleDeadlineH: 26, downstream: 'mart' },
  },
  statistics: {
    stream: 'statistics',
    coordinated: true,
    writeTables: { exact: ['RAW_WB_ORDERS', 'RAW_WB_SALES_RETURNS', 'INGEST_RUNS'], prefixes: [] },
    cloudLoaders: ['orders', 'sales', 'sales-reconcile'],
    // §B.2: 5 суток без FAIL, включая ночную сверку. Дрейф атрибута SKU (лист ↔ REF, §B.3 п.4) — объяснимый.
    qualification: {
      minComparableDays: 5,
      groups: [
        { group: 'STATS_ORDERS', cadence: 'daily', accept: PASS_OR_EXPLAINED },
        { group: 'STATS_SALES', cadence: 'daily', accept: PASS_OR_EXPLAINED },
        { group: 'STATS_SALES_RECONCILE', cadence: 'daily', accept: PASS_OR_EXPLAINED },
      ],
      sameDigest: false,
      maxStaleDays: 2,
    },
    schedules: [
      // Заказы с 10.11 раз в 6 ч, последний наблюдённый запуск 09:31 → фаза :31 в 03/09/15/21.
      AS('runWbOrdersIncremental', [3, 9, 15, 21], 31),
      // Продажи: раз в 6 ч, фаза неизвестна (наблюдались :22) — консервативно каждый час в :22.
      AS('runWbSalesIncremental', 'any', 22),
      AS('runWbSalesNightReconcile', [4], 20),
      CR('wb-orders-prod', [0, 6, 12, 18], 41, 60),
      CR('wb-sales-prod', [0, 6, 12, 18], 22, 15),
      CR('wb-sales-reconcile-prod', [4], 20, 15),
    ],
    cloudJobTimeoutMin: 60,
    // SALES_RECONCILE_DAYS = 7 у обоих писателей (wb_statistics_loaders.tf, WB_SALES_RECONCILE_DAYS).
    continuity: { kind: 'table_watermark', reconcileLoader: 'sales-reconcile', reconcileDays: 7 },
    monitoring: { cycles: { orders: 3, sales: 3, 'sales-reconcile': 3 }, firstCycleDeadlineH: 8, downstream: 'mart' },
  },
  ads: {
    stream: 'ads',
    coordinated: true,
    writeTables: { exact: ['INGEST_RUNS'], prefixes: ['RAW_WB_ADV_'] },
    cloudLoaders: ['ads', 'ads-query', 'ads-costs-audit'],
    // §B.2: расходы EXACT все 5 суток; недельный аудит ADSAUDIT_ — обязателен в окне 7 суток.
    qualification: {
      minComparableDays: 5,
      groups: [
        { group: 'ADS_COSTS', cadence: 'daily', accept: PASS },
        { group: 'ADS_STATS', cadence: 'daily', accept: PASS_OR_EXPLAINED },
        { group: 'ADS_COSTS_AUDIT', cadence: 'weekly', accept: PASS },
      ],
      sameDigest: false,
      maxStaleDays: 2,
    },
    schedules: [
      AS('runWbAdsDaily', [5], 0),
      AS('runWbAdsDailyCatchUp', [6, 8], 15),
      CR('wb-ads-prod', [5, 6, 8], 0, 40),
      CR('wb-ads-query-prod', [5, 6, 8], 30, 15),
      CR('wb-ads-costs-audit-prod', [4], 30, 15, { dow: [0] }),
    ],
    cloudJobTimeoutMin: 40,
    // fullstats читается окном D−7…D−1, расходы — D−14…D−1: разрыв до 7 суток перекрыт.
    continuity: { kind: 'period_gap', maxGapDays: 7 },
    monitoring: { cycles: { ads: 3, 'ads-query': 3 }, firstCycleDeadlineH: 26, downstream: 'mart' },
  },
  finance: {
    stream: 'finance',
    coordinated: true,
    writeTables: { exact: ['RAW_WB_FINANCE', 'FINANCE_LOADER_RUNS', 'FINANCE_REPORT_LOADS', 'FINANCE_WEEK_STATUS', 'FINANCE_WEEK_RECON'], prefixes: [] },
    cloudLoaders: ['finance', 'finance-backfill'],
    // Обязателен WEEKLY-цикл понедельника и ноль необъяснённых денежных расхождений (FAIL = необъяснённое).
    qualification: {
      minComparableDays: 5,
      groups: [
        { group: 'FINANCE_DAILY', cadence: 'daily', accept: PASS },
        { group: 'FINANCE_MONEY', cadence: 'daily', accept: PASS_OR_EXPLAINED },
        { group: 'FINANCE_WEEKLY', cadence: 'weekly', weekday: 1, accept: PASS },
      ],
      sameDigest: false,
      maxStaleDays: 2,
    },
    schedules: [
      AS('runWbFinanceDaily', [7, 12, 18], 30),
      CR('wb-finance-prod', [7, 12, 18], 30, 30),
    ],
    cloudJobTimeoutMin: 30,
    // Очередь FINANCE_REPORT_LOADS общая (таблица); discovery DAILY смотрит сегодня−3…сегодня.
    continuity: { kind: 'period_gap', maxGapDays: 3 },
    monitoring: { cycles: { finance: 3 }, mondayCycleLoader: 'finance', firstCycleDeadlineH: 8, downstream: 'mart' },
  },
  supplies: {
    stream: 'supplies',
    coordinated: true,
    writeTables: { exact: ['RAW_WB_SUPPLIES', 'RAW_WB_SUPPLIES_GOODS'], prefixes: [] },
    cloudLoaders: ['supplies'],
    qualification: {
      minComparableDays: 5,
      groups: [{ group: 'SUPPLIES', cadence: 'daily', accept: PASS }],
      sameDigest: false,
      maxStaleDays: 2,
    },
    schedules: [
      // Триггер поставок ставился вручную, минута в коде не зафиксирована: консервативно — весь час.
      AS('runWbSuppliesDaily', [2, 5], 'any'),
      CR('wb-supplies-prod', [2, 5], 29, 30),
    ],
    cloudJobTimeoutMin: 30,
    // Перекрытие 14 суток по factDate + правило «в RAW не завершён».
    continuity: { kind: 'period_gap', maxGapDays: 14 },
    monitoring: { cycles: { supplies: 3 }, firstCycleDeadlineH: 26, downstream: null },
  },
  ref: {
    stream: 'ref',
    coordinated: true,
    writeTables: { exact: ['REF_SKU_MASTER_DATA', 'REF_ACTIVE_VERSION', 'REF_SYNC_RUNS', 'REF_SYNC_TABLE_LOG', 'REF_SKU_MASTER'], prefixes: [] },
    cloudLoaders: ['ref-sync'],
    qualification: {
      minComparableDays: 5,
      groups: [{ group: 'REF', cadence: 'daily', accept: PASS }],
      sameDigest: false,
      maxStaleDays: 2,
    },
    schedules: [
      AS('runRefSync', [7], 30),
      CR('ref-sync-prod', [8], 22, 10),
    ],
    cloudJobTimeoutMin: 10,
    continuity: { kind: 'state' },
    monitoring: { cycles: { 'ref-sync': 3 }, firstCycleDeadlineH: 26, downstream: 'ref_active' },
  },
});
