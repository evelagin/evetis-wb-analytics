/**
 * M4 · Контракт хранилища финансового контура (манифест отчётов, журнал прогонов, недели, RAW).
 * Реализация над BigQuery — bq.ts; в тестах — модель в памяти с той же семантикой переходов.
 *
 * Семантика переходов манифеста FINANCE_REPORT_LOADS (дизайн rev3-final §4, как в Apps Script):
 *   DISCOVERED → STARTED → COMPLETE | ERROR;  ERROR → STARTED (пока attempt_count < 5);
 *   attempt_count растёт ТОЛЬКО на STARTED; COMPLETE неизменяем (повторный list лишь сверяет метрики);
 *   STARTED старше 120 мин → DISCOVERED (attempt_count не трогается).
 */
import type { ListMetricCol, DetMetricCol } from './constants.js';
import type { ReportPeriod, RawFinanceRow, RunCounters, RunStatus } from './normalize.js';

export interface FinanceTables {
  raw: string;
  runs: string;
  loads: string;
  weekStatus: string;
  weekRecon: string;
}

export interface QueueItem {
  report_id: string;
  report_period: ReportPeriod;
  report_type: number;
  date_from: string;
  date_to: string;
}

export interface ManifestEntry {
  report_id: string;
  status: string;
  metrics: Record<ListMetricCol, number>;
}

export interface DiscoveredInput {
  reportId: string;
  period: ReportPeriod;
  reportType: number;
  dateFrom: string;
  dateTo: string;
  metrics: Record<ListMetricCol, number>;
  metricsJson: string;
}

export interface WeeklyCandidate {
  report_id: string;
  report_type: number;
  week_start: string;
  metrics: Record<ListMetricCol, number>;
}

export interface DailySums {
  days_loaded: number;
  metrics: Record<ListMetricCol, number>;
}

export interface ReconRow {
  metric: string;
  sum_daily: number;
  sum_weekly: number;
}

export interface FinanceStore {
  /** Эквивалент C0-маркера: таблицы есть, у RAW есть служебные колонки контура. Только метаданные. */
  assertReady(): Promise<void>;

  runOpen(runId: string, triggerType: string, startedAtIso: string): Promise<void>;
  runClose(runId: string, status: RunStatus, c: RunCounters, errMsg: string): Promise<void>;

  staleRecovery(staleMinutes: number): Promise<number>;
  ensureWeekRows(cutover: string, lastCompletedWeek: string): Promise<void>;
  assertManifestPk(stage: string): Promise<void>;

  /** Очередь: DISCOVERED или ERROR с attempt_count < max, по date_from, report_id. */
  queue(maxAttempts: number): Promise<QueueItem[]>;
  /** Только DISCOVERED (шаг 5 «свежеоткрытое»). */
  discovered(): Promise<QueueItem[]>;
  /** Финализированные недели: 'YYYY-MM-DD|type'. */
  finalWeeks(): Promise<Set<string>>;
  hasOpenWeeks(): Promise<boolean>;
  manifestByIds(ids: string[]): Promise<Map<string, ManifestEntry>>;

  mergeDiscovered(rep: DiscoveredInput, runId: string): Promise<void>;
  /** true — строка захвачена этим прогоном (ровно одна строка обновлена). */
  mergeStarted(reportId: string, runId: string, maxAttempts: number): Promise<boolean>;
  mergeComplete(reportId: string, runId: string, rowsFetched: number, rowsLoaded: number, det: Record<DetMetricCol, number>): Promise<boolean>;
  mergeError(reportId: string, runId: string, msg: string): Promise<void>;
  manifestDiag(reportId: string): Promise<string>;

  /** A5a шаг 1: строки отчёта в RAW в партициях периода (этот прогон / другие). */
  orphanPruned(reportId: string, runId: string, dateFrom: string, dateTo: string): Promise<{ thisRun: number; otherRun: number }>;
  /** A5a шаг 2: full-scan по report_id без фильтра партиций. */
  orphanFull(reportId: string, dateFrom: string, dateTo: string): Promise<{ full: number; nullRr: number; outside: number; runCount: number }>;

  /** Атомарный load-job с детерминированным jobId. Возвращает число строк. */
  loadRaw(rows: RawFinanceRow[], jobId: string): Promise<number>;
  postLoadThisRun(reportId: string, runId: string): Promise<{ count: number; distinct: number }>;
  postLoadCrossRun(reportId: string, dateFrom: string, dateTo: string): Promise<{ count: number; distinct: number }>;

  finalizeCandidates(cutover: string): Promise<WeeklyCandidate[]>;
  dailySums(reportType: number, weekStart: string): Promise<DailySums>;
  mergeRecon(weekStart: string, reportType: number, rows: ReconRow[], tol: number): Promise<void>;
  markWeekFinal(weekStart: string, reportType: number, weeklyReportId: string, daysLoaded: number): Promise<void>;
}
