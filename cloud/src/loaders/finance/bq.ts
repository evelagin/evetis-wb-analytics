/**
 * M4 · BigQuery-реализация FinanceStore. SQL перенесён из Wbfinancedaily.gs без изменения смысла.
 *
 * Правила (как в Apps Script и в остальных загрузчиках репозитория):
 *  - все значения из внешнего мира — ТОЛЬКО named-параметры; в текст SQL подставляются лишь
 *    имена таблиц из конфигурации (их форму проверяет targets.ts) и числовые константы;
 *  - даты и NUMERIC передаются строкой и приводятся в SQL (CAST(@x AS DATE/NUMERIC)): Node-клиент
 *    сериализует «сырую» JS-строку с типом DATE как NULL (урок mart/bq.ts);
 *  - запись в RAW — атомарный load-job (не streaming) с детерминированным jobId;
 *  - DDL нет: таблицы создаёт Terraform (shadow `__CR`) или они уже существуют (prod).
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { LoaderError } from '../../errors.js';
import { FIN_LIST_METRICS, FIN_DET_METRICS, FIN_RAW_REQUIRED_COLUMNS, type ListMetricCol, type DetMetricCol } from './constants.js';
import { finNum } from './parse.js';
import type { RawFinanceRow, RunCounters, RunStatus, ReportPeriod } from './normalize.js';
import type {
  FinanceStore, FinanceTables, QueueItem, ManifestEntry, DiscoveredInput, WeeklyCandidate, DailySums, ReconRow,
} from './store.js';

export type ParamType = 'STRING' | 'INT64' | ['STRING'];

export interface SqlResult {
  rows: Array<Record<string, unknown>>;
  /** numDmlAffectedRows для DML; null для SELECT. */
  affected: number | null;
}

/** Узкий порт к BigQuery — фейк в тестах, реальный клиент в проде. */
export interface SqlRunner {
  run(sql: string, params?: Record<string, unknown>, types?: Record<string, ParamType>): Promise<SqlResult>;
  /** Load-job NDJSON; 'ALREADY_LOADED' — job с этим jobId уже успешно выполнен. */
  load(table: string, ndjson: string, jobId: string): Promise<'LOADED' | 'ALREADY_LOADED'>;
  /** Колонки таблицы по метаданным (бесплатно); null — таблицы нет. */
  columns(table: string): Promise<string[] | null>;
}

const str = (v: unknown): string => (v === null || v === undefined ? '' : String((v as { value?: unknown }).value ?? v));

function listMetricsOf(o: Record<string, unknown>): Record<ListMetricCol, number> {
  const m = {} as Record<ListMetricCol, number>;
  for (const [, col] of FIN_LIST_METRICS) m[col] = finNum(str(o[col]));
  return m;
}

function toQueueItem(o: Record<string, unknown>): QueueItem {
  return {
    report_id: str(o.report_id),
    report_period: str(o.report_period) as ReportPeriod,
    report_type: Number(str(o.report_type)) || 0,
    date_from: str(o.date_from),
    date_to: str(o.date_to),
  };
}

export class BqFinanceStore implements FinanceStore {
  constructor(
    private readonly sql: SqlRunner,
    private readonly projectId: string,
    private readonly dataset: string,
    private readonly t: FinanceTables,
  ) {}

  private tbl(name: string): string {
    return `\`${this.projectId}.${this.dataset}.${name}\``;
  }

  async assertReady(): Promise<void> {
    const missing: string[] = [];
    for (const name of [this.t.raw, this.t.runs, this.t.loads, this.t.weekStatus, this.t.weekRecon]) {
      const cols = await this.sql.columns(name);
      if (cols === null) { missing.push(name); continue; }
      if (name === this.t.raw) {
        const lack = FIN_RAW_REQUIRED_COLUMNS.filter((c) => !cols.includes(c));
        if (lack.length) {
          throw new LoaderError(`${name}: нет колонок контура ${lack.join(', ')} (C0 не выполнен)`, 'FINANCE_SCHEMA_NOT_READY');
        }
      }
    }
    if (missing.length) throw new LoaderError(`нет таблиц контура: ${missing.join(', ')}`, 'FINANCE_SCHEMA_NOT_READY');
  }

  async runOpen(runId: string, triggerType: string, startedAtIso: string): Promise<void> {
    const row = { run_id: runId, started_at: startedAtIso, status: 'RUNNING', trigger_type: triggerType };
    await this.sql.load(this.t.runs, JSON.stringify(row), `finrun_open_${runId}`.replace(/[^A-Za-z0-9_-]/g, '-'));
  }

  async runClose(runId: string, status: RunStatus, c: RunCounters, errMsg: string): Promise<void> {
    await this.sql.run(
      `MERGE ${this.tbl(this.t.runs)} t
USING (SELECT @run_id AS run_id) s ON t.run_id = s.run_id
WHEN MATCHED THEN UPDATE SET finished_at = CURRENT_TIMESTAMP(), status = @status,
  reports_discovered = @rd, reports_loaded = @rl2, reports_errors = @re,
  requests_made = @rq, error_message = @msg`,
      { run_id: runId, status, rd: c.discovered, rl2: c.loaded, re: c.errors, rq: c.requests, msg: String(errMsg || '').substring(0, 900) },
      { run_id: 'STRING', status: 'STRING', rd: 'INT64', rl2: 'INT64', re: 'INT64', rq: 'INT64', msg: 'STRING' },
    );
  }

  async staleRecovery(staleMinutes: number): Promise<number> {
    const r = await this.sql.run(
      `MERGE ${this.tbl(this.t.loads)} t
USING (SELECT 1 AS one) s ON t.status='STARTED'
  AND t.started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${Math.trunc(staleMinutes)} MINUTE)
WHEN MATCHED THEN UPDATE SET status='DISCOVERED',
  processing_run_id=NULL, started_at=NULL,
  error_message=CONCAT('stale STARTED recovered ', CAST(CURRENT_TIMESTAMP() AS STRING))`,
    );
    return r.affected ?? 0;
  }

  async ensureWeekRows(cutover: string, lastCompletedWeek: string): Promise<void> {
    await this.sql.run(
      `MERGE ${this.tbl(this.t.weekStatus)} t
USING (
  SELECT ws AS week_start, rt AS report_type
  FROM UNNEST(GENERATE_DATE_ARRAY(CAST(@cutover AS DATE), CAST(@last_week AS DATE), INTERVAL 7 DAY)) ws,
       UNNEST([1, 2]) rt
) s ON t.week_start = s.week_start AND t.report_type = s.report_type
WHEN NOT MATCHED THEN INSERT (week_start, report_type, weekly_final)
VALUES (s.week_start, s.report_type, FALSE)`,
      { cutover, last_week: lastCompletedWeek },
      { cutover: 'STRING', last_week: 'STRING' },
    );
  }

  async assertManifestPk(stage: string): Promise<void> {
    const r = await this.sql.run(`SELECT COUNT(*) AS c, COUNT(DISTINCT report_id) AS d FROM ${this.tbl(this.t.loads)}`);
    const c = str(r.rows[0]?.c);
    const d = str(r.rows[0]?.d);
    if (c !== d) throw new LoaderError(`MANIFEST_PK_VIOLATION (${stage}): COUNT=${c} != DISTINCT=${d}`, 'FINANCE_MANIFEST_PK_VIOLATION');
  }

  private async selectItems(where: string): Promise<QueueItem[]> {
    const r = await this.sql.run(
      `SELECT report_id, report_period, report_type,
  CAST(date_from AS STRING) AS date_from, CAST(date_to AS STRING) AS date_to
FROM ${this.tbl(this.t.loads)}
WHERE ${where}
ORDER BY date_from, report_id`,
    );
    return r.rows.map(toQueueItem);
  }

  queue(maxAttempts: number): Promise<QueueItem[]> {
    return this.selectItems(`status = 'DISCOVERED' OR (status = 'ERROR' AND attempt_count < ${Math.trunc(maxAttempts)})`);
  }

  discovered(): Promise<QueueItem[]> {
    return this.selectItems(`status = 'DISCOVERED'`);
  }

  async finalWeeks(): Promise<Set<string>> {
    const r = await this.sql.run(
      `SELECT CAST(week_start AS STRING) AS ws, report_type FROM ${this.tbl(this.t.weekStatus)} WHERE weekly_final = TRUE`,
    );
    return new Set(r.rows.map((o) => `${str(o.ws)}|${str(o.report_type)}`));
  }

  async hasOpenWeeks(): Promise<boolean> {
    const r = await this.sql.run(`SELECT COUNT(*) AS n FROM ${this.tbl(this.t.weekStatus)} WHERE weekly_final = FALSE`);
    return Number(str(r.rows[0]?.n)) > 0;
  }

  async manifestByIds(ids: string[]): Promise<Map<string, ManifestEntry>> {
    const out = new Map<string, ManifestEntry>();
    if (!ids.length) return out;
    const cols = FIN_LIST_METRICS.map(([, c]) => `CAST(${c} AS STRING) AS ${c}`).join(', ');
    const r = await this.sql.run(
      `SELECT report_id, status, ${cols} FROM ${this.tbl(this.t.loads)}
WHERE report_id IN UNNEST(@ids)`,
      { ids },
      { ids: ['STRING'] },
    );
    for (const o of r.rows) out.set(str(o.report_id), { report_id: str(o.report_id), status: str(o.status), metrics: listMetricsOf(o) });
    return out;
  }

  async mergeDiscovered(rep: DiscoveredInput, runId: string): Promise<void> {
    const params: Record<string, unknown> = {
      report_id: rep.reportId, period: rep.period, rtype: rep.reportType,
      dfrom: rep.dateFrom, dto: rep.dateTo, run_id: runId, mjson: rep.metricsJson,
    };
    const types: Record<string, ParamType> = {
      report_id: 'STRING', period: 'STRING', rtype: 'INT64', dfrom: 'STRING', dto: 'STRING', run_id: 'STRING', mjson: 'STRING',
    };
    FIN_LIST_METRICS.forEach(([, col], i) => { params[`m${i}`] = String(rep.metrics[col]); types[`m${i}`] = 'STRING'; });
    await this.sql.run(
      `MERGE ${this.tbl(this.t.loads)} t
USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id
WHEN MATCHED AND t.status IN ('DISCOVERED','ERROR') THEN UPDATE SET
  list_forpay=CAST(@m0 AS NUMERIC), list_retail=CAST(@m1 AS NUMERIC), list_delivery=CAST(@m2 AS NUMERIC),
  list_storage=CAST(@m3 AS NUMERIC), list_acceptance=CAST(@m4 AS NUMERIC), list_deduction=CAST(@m5 AS NUMERIC),
  list_penalty=CAST(@m6 AS NUMERIC), list_metrics_json=@mjson
WHEN NOT MATCHED THEN INSERT (report_id, report_period, report_type,
  date_from, date_to, status, discovered_run_id, attempt_count, discovered_at,
  list_forpay, list_retail, list_delivery, list_storage, list_acceptance,
  list_deduction, list_penalty, list_metrics_json)
VALUES (@report_id, @period, @rtype, CAST(@dfrom AS DATE), CAST(@dto AS DATE), 'DISCOVERED', @run_id, 0,
  CURRENT_TIMESTAMP(), CAST(@m0 AS NUMERIC), CAST(@m1 AS NUMERIC), CAST(@m2 AS NUMERIC), CAST(@m3 AS NUMERIC),
  CAST(@m4 AS NUMERIC), CAST(@m5 AS NUMERIC), CAST(@m6 AS NUMERIC), @mjson)`,
      params,
      types,
    );
  }

  async mergeStarted(reportId: string, runId: string, maxAttempts: number): Promise<boolean> {
    const r = await this.sql.run(
      `MERGE ${this.tbl(this.t.loads)} t
USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id
WHEN MATCHED AND t.status IN ('DISCOVERED','ERROR')
  AND t.attempt_count < ${Math.trunc(maxAttempts)} THEN UPDATE SET
  status='STARTED', processing_run_id=@run_id, started_at=CURRENT_TIMESTAMP(),
  attempt_count=t.attempt_count+1, error_message=NULL`,
      { report_id: reportId, run_id: runId },
      { report_id: 'STRING', run_id: 'STRING' },
    );
    return r.affected === 1;
  }

  async mergeComplete(reportId: string, runId: string, rowsFetched: number, rowsLoaded: number, det: Record<DetMetricCol, number>): Promise<boolean> {
    const params: Record<string, unknown> = { report_id: reportId, run_id: runId, rf: rowsFetched, rl: rowsLoaded };
    const types: Record<string, ParamType> = { report_id: 'STRING', run_id: 'STRING', rf: 'INT64', rl: 'INT64' };
    FIN_DET_METRICS.forEach(([, col], i) => { params[`d${i}`] = String(det[col]); types[`d${i}`] = 'STRING'; });
    const r = await this.sql.run(
      `MERGE ${this.tbl(this.t.loads)} t
USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id
WHEN MATCHED AND t.status='STARTED' AND t.processing_run_id=@run_id THEN UPDATE SET
  status='COMPLETE', completed_at=CURRENT_TIMESTAMP(),
  rows_fetched=@rf, rows_loaded=@rl,
  det_forpay=CAST(@d0 AS NUMERIC), det_retail=CAST(@d1 AS NUMERIC), det_delivery=CAST(@d2 AS NUMERIC),
  det_storage=CAST(@d3 AS NUMERIC), det_acceptance=CAST(@d4 AS NUMERIC), det_deduction=CAST(@d5 AS NUMERIC),
  det_penalty=CAST(@d6 AS NUMERIC), error_message=NULL`,
      params,
      types,
    );
    return r.affected === 1;
  }

  async mergeError(reportId: string, runId: string, msg: string): Promise<void> {
    await this.sql.run(
      `MERGE ${this.tbl(this.t.loads)} t
USING (SELECT @report_id AS report_id) s ON t.report_id = s.report_id
WHEN MATCHED AND t.status='STARTED' AND t.processing_run_id=@run_id THEN UPDATE SET
  status='ERROR', error_message=@msg`,
      { report_id: reportId, run_id: runId, msg: String(msg || '').substring(0, 900) },
      { report_id: 'STRING', run_id: 'STRING', msg: 'STRING' },
    );
  }

  async manifestDiag(reportId: string): Promise<string> {
    const r = await this.sql.run(
      `SELECT status, processing_run_id, attempt_count FROM ${this.tbl(this.t.loads)}
WHERE report_id = @rid`,
      { rid: reportId },
      { rid: 'STRING' },
    );
    const o = r.rows[0] ?? {};
    return `manifest: status=${str(o.status)}, processing_run_id=${str(o.processing_run_id)}, attempts=${str(o.attempt_count)}`;
  }

  async orphanPruned(reportId: string, runId: string, dateFrom: string, dateTo: string): Promise<{ thisRun: number; otherRun: number }> {
    const r = await this.sql.run(
      `SELECT COUNTIF(run_id = @run)                AS this_run,
       COUNTIF(run_id IS DISTINCT FROM @run) AS other_run
FROM ${this.tbl(this.t.raw)}
WHERE report_id = @rid
  AND _rr_date BETWEEN CAST(@dfrom AS DATE) AND CAST(@dto AS DATE)`,
      { rid: reportId, run: runId, dfrom: dateFrom, dto: dateTo },
      { rid: 'STRING', run: 'STRING', dfrom: 'STRING', dto: 'STRING' },
    );
    return { thisRun: Number(str(r.rows[0]?.this_run)), otherRun: Number(str(r.rows[0]?.other_run)) };
  }

  async orphanFull(reportId: string, dateFrom: string, dateTo: string): Promise<{ full: number; nullRr: number; outside: number; runCount: number }> {
    const r = await this.sql.run(
      `SELECT COUNT(*)                     AS full_count,
       COUNTIF(_rr_date IS NULL)    AS null_rr_date,
       COUNTIF(_rr_date < CAST(@dfrom AS DATE)
            OR _rr_date > CAST(@dto AS DATE)) AS outside_period,
       COUNT(DISTINCT run_id)       AS run_count
FROM ${this.tbl(this.t.raw)}
WHERE report_id = @rid`,
      { rid: reportId, dfrom: dateFrom, dto: dateTo },
      { rid: 'STRING', dfrom: 'STRING', dto: 'STRING' },
    );
    const o = r.rows[0] ?? {};
    return {
      full: Number(str(o.full_count)), nullRr: Number(str(o.null_rr_date)),
      outside: Number(str(o.outside_period)), runCount: Number(str(o.run_count)),
    };
  }

  async loadRaw(rows: RawFinanceRow[], jobId: string): Promise<number> {
    if (!rows.length) return 0;
    await this.sql.load(this.t.raw, rows.map((r) => JSON.stringify(r)).join('\n'), jobId);
    return rows.length;
  }

  private async counts(where: string, params: Record<string, unknown>, types: Record<string, ParamType>): Promise<{ count: number; distinct: number }> {
    const r = await this.sql.run(
      `SELECT COUNT(*) AS c, COUNT(DISTINCT rrd_id) AS d FROM ${this.tbl(this.t.raw)}
WHERE ${where}`,
      params,
      types,
    );
    return { count: Number(str(r.rows[0]?.c)), distinct: Number(str(r.rows[0]?.d)) };
  }

  postLoadThisRun(reportId: string, runId: string): Promise<{ count: number; distinct: number }> {
    return this.counts('report_id=@rid AND run_id=@run', { rid: reportId, run: runId }, { rid: 'STRING', run: 'STRING' });
  }

  postLoadCrossRun(reportId: string, dateFrom: string, dateTo: string): Promise<{ count: number; distinct: number }> {
    return this.counts(
      'report_id=@rid AND _rr_date BETWEEN CAST(@dfrom AS DATE) AND CAST(@dto AS DATE)',
      { rid: reportId, dfrom: dateFrom, dto: dateTo },
      { rid: 'STRING', dfrom: 'STRING', dto: 'STRING' },
    );
  }

  async finalizeCandidates(cutover: string): Promise<WeeklyCandidate[]> {
    const cols = FIN_LIST_METRICS.map(([, c]) => `CAST(m.${c} AS STRING) AS ${c}`).join(', ');
    const r = await this.sql.run(
      `SELECT m.report_id, m.report_type,
  CAST(DATE_TRUNC(m.date_from, WEEK(MONDAY)) AS STRING) AS week_start, ${cols}
FROM ${this.tbl(this.t.loads)} m
LEFT JOIN ${this.tbl(this.t.weekStatus)} w
  ON w.week_start = DATE_TRUNC(m.date_from, WEEK(MONDAY)) AND w.report_type = m.report_type
WHERE m.status = 'COMPLETE' AND m.report_period = 'WEEKLY'
  AND m.date_from >= CAST(@cutover AS DATE) AND COALESCE(w.weekly_final, FALSE) = FALSE`,
      { cutover },
      { cutover: 'STRING' },
    );
    return r.rows.map((o) => ({
      report_id: str(o.report_id), report_type: Number(str(o.report_type)) || 0,
      week_start: str(o.week_start), metrics: listMetricsOf(o),
    }));
  }

  async dailySums(reportType: number, weekStartDate: string): Promise<DailySums> {
    const cols = FIN_LIST_METRICS.map(([, c]) => `CAST(IFNULL(SUM(${c}), 0) AS STRING) AS ${c}`).join(', ');
    const r = await this.sql.run(
      `SELECT COUNT(*) AS days_loaded, ${cols} FROM ${this.tbl(this.t.loads)}
WHERE report_period = 'DAILY' AND status = 'COMPLETE' AND report_type = @rt
  AND date_from BETWEEN CAST(@ws AS DATE) AND DATE_ADD(CAST(@ws AS DATE), INTERVAL 6 DAY)`,
      { rt: reportType, ws: weekStartDate },
      { rt: 'INT64', ws: 'STRING' },
    );
    const o = r.rows[0] ?? {};
    return { days_loaded: Number(str(o.days_loaded)) || 0, metrics: listMetricsOf(o) };
  }

  async mergeRecon(weekStartDate: string, reportType: number, rows: ReconRow[], tol: number): Promise<void> {
    const params: Record<string, unknown> = { ws: weekStartDate, rt: reportType };
    const types: Record<string, ParamType> = { ws: 'STRING', rt: 'INT64' };
    const parts = rows.map((row, i) => {
      params[`mn${i}`] = row.metric; types[`mn${i}`] = 'STRING';
      params[`sd${i}`] = String(row.sum_daily); types[`sd${i}`] = 'STRING';
      params[`sw${i}`] = String(row.sum_weekly); types[`sw${i}`] = 'STRING';
      return `SELECT @mn${i} AS metric, CAST(@sd${i} AS NUMERIC) AS sum_daily, CAST(@sw${i} AS NUMERIC) AS sum_weekly`;
    });
    const tolSql = String(Number(tol));
    await this.sql.run(
      `MERGE ${this.tbl(this.t.weekRecon)} t
USING (${parts.join(' UNION ALL ')}) s
ON t.week_start = CAST(@ws AS DATE) AND t.report_type = @rt AND t.metric = s.metric
WHEN MATCHED THEN UPDATE SET sum_daily = s.sum_daily, sum_weekly = s.sum_weekly,
  delta = s.sum_daily - s.sum_weekly,
  recon_status = IF(ABS(s.sum_daily - s.sum_weekly) <= ${tolSql}, 'OK', 'WARN'),
  checked_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (week_start, report_type, metric, sum_daily, sum_weekly,
  delta, recon_status, checked_at)
VALUES (CAST(@ws AS DATE), @rt, s.metric, s.sum_daily, s.sum_weekly, s.sum_daily - s.sum_weekly,
  IF(ABS(s.sum_daily - s.sum_weekly) <= ${tolSql}, 'OK', 'WARN'),
  CURRENT_TIMESTAMP())`,
      params,
      types,
    );
  }

  async markWeekFinal(weekStartDate: string, reportType: number, weeklyReportId: string, daysLoaded: number): Promise<void> {
    await this.sql.run(
      `MERGE ${this.tbl(this.t.weekStatus)} t
USING (SELECT CAST(@ws AS DATE) AS week_start, @rt AS report_type) s
ON t.week_start = s.week_start AND t.report_type = s.report_type
WHEN MATCHED THEN UPDATE SET weekly_final = TRUE, weekly_report_id = @rid,
  finalized_at = CURRENT_TIMESTAMP(), daily_days_loaded = @days
WHEN NOT MATCHED THEN INSERT (week_start, report_type, weekly_report_id,
  weekly_final, finalized_at, daily_days_loaded)
VALUES (s.week_start, s.report_type, @rid, TRUE, CURRENT_TIMESTAMP(), @days)`,
      { ws: weekStartDate, rt: reportType, rid: weeklyReportId, days: daysLoaded },
      { ws: 'STRING', rt: 'INT64', rid: 'STRING', days: 'INT64' },
    );
  }
}

// ─────────────────────────── реальный SqlRunner ───────────────────────────

function isAlreadyExists(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 409 || /already exists/i.test(msg);
}

function isNotFound(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 404 || /not found|notFound/i.test(msg);
}

interface JobStatusLike { state?: string; errorResult?: { message?: string } }

/**
 * BigQuery-проводка. DML-счётчик берётся из метаданных задания (jobs.get — бесплатно), а не
 * отдельным SELECT: захват STARTED и COMPLETE решаются ровно по «обновлена одна строка».
 */
export class BigQuerySqlRunner implements SqlRunner {
  private readonly bq: BigQuery;
  constructor(private readonly projectId: string, private readonly dataset: string, private readonly location: string, bq?: BigQuery) {
    this.bq = bq ?? new BigQuery({ projectId });
  }

  async run(sql: string, params?: Record<string, unknown>, types?: Record<string, ParamType>): Promise<SqlResult> {
    const [job] = await this.bq.createQueryJob({
      query: sql,
      params: params ?? {},
      ...(types ? { types: types as Record<string, string | string[]> } : {}),
      location: this.location,
    });
    const [rows] = await job.getQueryResults();
    const [meta] = await job.getMetadata();
    const n = (meta as { statistics?: { query?: { numDmlAffectedRows?: string } } }).statistics?.query?.numDmlAffectedRows;
    return { rows: rows as Array<Record<string, unknown>>, affected: n === undefined ? null : Number(n) };
  }

  async columns(table: string): Promise<string[] | null> {
    try {
      const [meta] = await this.bq.dataset(this.dataset).table(table).getMetadata();
      const fields = (meta as { schema?: { fields?: Array<{ name?: string }> } }).schema?.fields ?? [];
      return fields.map((f) => String(f.name));
    } catch (e) {
      if (isNotFound(e)) return null;
      throw e;
    }
  }

  private async jobStatus(jobId: string): Promise<JobStatusLike | 'NOT_FOUND'> {
    try {
      const [job] = await this.bq.job(jobId, { location: this.location }).get();
      return ((job.metadata as { status?: JobStatusLike } | undefined)?.status) ?? {};
    } catch (e) {
      if (isNotFound(e)) return 'NOT_FOUND';
      throw e; // не смогли проверить → fail-closed
    }
  }

  private async waitDone(jobId: string): Promise<void> {
    for (let i = 0; i < 120; i++) {
      const st = await this.jobStatus(jobId);
      if (st === 'NOT_FOUND') throw new Error(`load job ${jobId} пропал`);
      if (st.errorResult) throw new Error(`BQ load error (${jobId}): ${st.errorResult.message ?? 'unknown'}`);
      if (st.state === 'DONE') return;
      await new Promise((r) => setTimeout(r, 1500));
    }
    throw new Error(`BQ load job не завершился: ${jobId}`);
  }

  /**
   * A5b · load-job с детерминированным jobId — exactly-once для повторов ВНУТРИ прогона.
   * Ответ на insert потерялся, а BigQuery задание принял → второй append не делаем: находим job по
   * тому же jobId и ждём DONE. ignoreUnknownValues=false: расхождение маппера и схемы падает громко.
   */
  async load(table: string, ndjson: string, jobId: string): Promise<'LOADED' | 'ALREADY_LOADED'> {
    const file = join(tmpdir(), `fin_${jobId}.ndjson`);
    writeFileSync(file, ndjson);
    try {
      const meta: JobLoadMetadata = {
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        createDisposition: 'CREATE_NEVER',
        writeDisposition: 'WRITE_APPEND',
        ignoreUnknownValues: false,
        maxBadRecords: 0,
        location: this.location,
        jobId,
      };
      for (let attempt = 1; ; attempt++) {
        try {
          await this.bq.dataset(this.dataset).table(table).load(file, meta);
          return 'LOADED';
        } catch (e) {
          if (isAlreadyExists(e)) {
            const st = await this.jobStatus(jobId);
            if (st !== 'NOT_FOUND' && st.state === 'DONE' && !st.errorResult) return 'ALREADY_LOADED';
            throw new Error(`load job ${jobId} уже существует и не успешен: ${st === 'NOT_FOUND' ? 'not found' : st.errorResult?.message ?? st.state ?? 'unknown'}`);
          }
          // Сетевая неопределённость: BigQuery мог УЖЕ принять задание.
          const st = await this.jobStatus(jobId);
          if (st !== 'NOT_FOUND') {
            if (st.errorResult) throw e;
            await this.waitDone(jobId);
            return 'LOADED';
          }
          if (attempt >= 3) throw e;
          await new Promise((r) => setTimeout(r, 2000));
        }
      }
    } finally {
      rmSync(file, { force: true });
    }
  }
}
