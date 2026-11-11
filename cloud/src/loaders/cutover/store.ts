/**
 * Координатор cutover: хранилище состояния и доказательства (BigQuery).
 *
 * Запись — ТОЛЬКО две таблицы wb_ops: INGEST_WRITER_OWNERSHIP (compare-and-swap) и INGEST_CUTOVER_LOG
 * (append). CAS и строка журнала — одна транзакция: перехода без записи в журнал не бывает, повтор
 * перехода невозможен (условие MERGE по epoch + state + revision больше не совпадёт).
 * Всё остальное — чтение журналов и RAW потока (права dataViewer поресурсно, infra/terraform/ingest_cutover.tf).
 *
 * Время запуска в журналах берётся как время СТАРТА прогона (started_at или штамп в run_id/load_id), а не
 * время записи: прогон, начатый до fence и завершённый после, — штатный дренаж, а не нарушение.
 */
import type { BqClient } from '../../bq/client.js';
import { OWNERSHIP_TABLE, CUTOVER_LOG_TABLE, QUALIFICATION_TABLE, isOwner, isStream, type Owner, type Stream } from '../../ownership/streams.js';
import type { QualRow } from './qualification.js';
import { PROFILES } from './profiles.js';

export const STATES = [
  'LEGACY_ACTIVE', 'SHADOW_VALIDATING', 'QUALIFIED', 'LEGACY_FENCED', 'CLOUD_ACTIVE', 'MONITORING', 'COMPLETED',
  'BLOCKED', 'STOP', 'ROLLBACK_REQUIRED', 'UNKNOWN_OUTCOME', 'CLOUD_FENCED',
] as const;
export type State = (typeof STATES)[number];
export const isState = (s: string): s is State => (STATES as readonly string[]).includes(s);

export interface OwnershipRow {
  stream: Stream;
  owner: Owner;
  state: State;
  epoch: number;
  revision: number;
  legacyWatermark: string | null;
  cloudWatermark: string | null;
  autoCutoverEnabled: boolean;
  fencedAtMs: number | null;
  activeSinceMs: number | null;
  changedAtMs: number;
  changedBy: string;
  reason: string;
}

export interface CasExpect { stream: Stream; epoch: number; state: State; revision: number }
export interface CasNext {
  owner: Owner; state: State; epoch: number; revision: number;
  legacyWatermark: string | null; cloudWatermark: string | null;
  fencedAtMs: number | null; activeSinceMs: number | null;
  changedBy: string; reason: string;
}
export interface CutoverLogEntry {
  transitionId: string; stream: Stream; fromState: State; toState: State; epoch: number;
  runId: string; sourceWatermark: string | null; reason: string; evidence: unknown;
}

/** Прогон писателя: журнал, id, время СТАРТА, статус. shared — источник, куда пишут оба писателя (RAW). */
export interface RunRecord {
  journal: string;
  id: string;
  startMs: number;
  status: string;
  shared: boolean;
}

export interface CloudRun {
  id: string;
  loader: string;
  startMs: number;
  status: string;
  logicalPeriod: string;
  errorCode: string;
}

/** Незавершённое задание BigQuery, которое пишет (или скриптом может писать) в таблицы потока. */
export interface PendingJob {
  jobId: string;
  user: string;
  createdMs: number;
  state: string;
  table: string;
  statementType: string;
}

export interface CutoverStore {
  read(stream: Stream): Promise<OwnershipRow | null>;
  /** true — переход применён (ровно одна строка), false — CAS не совпал. */
  cas(expect: CasExpect, next: CasNext, log: CutoverLogEntry): Promise<boolean>;
  qualification(stream: Stream, sinceDate: string): Promise<QualRow[]>;
  /** Прогоны Apps Script со стартом ≥ fromMs. includeShared — добавить RAW, куда пишут оба писателя. */
  legacyRuns(stream: Stream, fromMs: number, includeShared: boolean): Promise<RunRecord[]>;
  /** Строки LOADER_RUNS (environment='prod') загрузчиков потока со стартом ≥ fromMs. */
  cloudRuns(stream: Stream, fromMs: number): Promise<CloudRun[]>;
  /** Текущая отметка потока по таблицам (legacy watermark при дренаже). null — отметки нет. */
  currentWatermark(stream: Stream): Promise<string | null>;
  /** Строки, записанные Cloud Run после sinceMs, СТАРШЕ legacy watermark (только table_watermark). */
  continuityViolations(stream: Stream, legacyWatermark: string, sinceMs: number): Promise<number>;
  /** Дубли после fence (остатки: сутки с двумя COMPLETE-снимками). */
  duplicates(stream: Stream, sinceMs: number): Promise<number>;
  downstreamAlive(kind: 'mart' | 'ref_active', sinceMs: number): Promise<boolean>;
  /**
   * INFORMATION_SCHEMA.JOBS_BY_PROJECT: задания со state ≠ DONE, созданные с fromMs, у которых
   * destination — таблица потока, либо SCRIPT, чей текст упоминает таблицу потока. Фильтр по таблицам, а не
   * по пользователю: Apps Script работает от учётной записи владельца.
   */
  pendingWriteJobs(stream: Stream, fromMs: number): Promise<PendingJob[]>;
  /**
   * statistics: MAX(last_change_date) строк инкремента, загруженных прогонами со стартом < beforeMs, —
   * ровно то состояние, которое прочитал первый прогон Cloud Run. JSON {orders, sales} в форме хранения.
   */
  statsWatermarkBefore(beforeMs: number): Promise<string | null>;
}

// ── BigQuery ──────────────────────────────────────────────────────────────────────────────────

type Q = Pick<BqClient, 'query' | 'projectId' | 'location'>;
const JOB_TIMEOUT_MS = 120_000;
const tsFromStamp = (expr: string): string =>
  `TIMESTAMP(SAFE.PARSE_DATETIME('%Y%m%d_%H%M%S', ${expr}), 'Europe/Moscow')`;

export class BqCutoverStore implements CutoverStore {
  constructor(
    private readonly bq: Q,
    private readonly opsDataset: string,
    private readonly rawDataset: string,
  ) {}

  private ops(t: string): string { return `\`${this.bq.projectId}.${this.opsDataset}.${t}\``; }
  private raw(t: string): string { return `\`${this.bq.projectId}.${this.rawDataset}.${t}\``; }

  private q<T>(sql: string, params: Record<string, unknown>, types: Record<string, string>): Promise<T[]> {
    return this.bq.query<T>(sql, params, types, { jobTimeoutMs: JOB_TIMEOUT_MS });
  }

  async read(stream: Stream): Promise<OwnershipRow | null> {
    const rows = await this.q<Record<string, unknown>>(
      `SELECT stream, owner, state, epoch, revision, legacy_watermark, cloud_watermark, auto_cutover_enabled,
              UNIX_MILLIS(fenced_at) AS fenced_at_ms, UNIX_MILLIS(active_since) AS active_since_ms,
              UNIX_MILLIS(changed_at) AS changed_at_ms, changed_by, reason
       FROM ${this.ops(OWNERSHIP_TABLE)} WHERE stream = @stream`,
      { stream }, { stream: 'STRING' },
    );
    if (rows.length === 0) return null;
    if (rows.length > 1) throw new Error(`${OWNERSHIP_TABLE}: ${rows.length} строк потока ${stream}`);
    return parseRow(rows[0] ?? {});
  }

  async cas(expect: CasExpect, next: CasNext, log: CutoverLogEntry): Promise<boolean> {
    const iso = (ms: number | null): string | null => (ms === null ? null : new Date(ms).toISOString());
    const sql = `
BEGIN
  DECLARE affected INT64 DEFAULT 0;
  BEGIN TRANSACTION;
  MERGE ${this.ops(OWNERSHIP_TABLE)} T
  USING (SELECT @stream AS stream) S
  ON T.stream = S.stream
  WHEN MATCHED AND T.epoch = @epoch AND T.state = @from_state AND T.revision = @revision THEN UPDATE SET
    owner = @owner, state = @to_state, epoch = @new_epoch, revision = @new_revision,
    legacy_watermark = @legacy_wm, cloud_watermark = @cloud_wm,
    fenced_at = TIMESTAMP(@fenced_at), active_since = TIMESTAMP(@active_since),
    changed_at = CURRENT_TIMESTAMP(), changed_by = @changed_by, reason = @reason;
  SET affected = @@row_count;
  IF affected = 1 THEN
    INSERT INTO ${this.ops(CUTOVER_LOG_TABLE)}
      (transition_id, stream, from_state, to_state, epoch, run_id, source_watermark, reason, evidence, at)
    VALUES (@transition_id, @stream, @from_state, @to_state, @new_epoch, @run_id, @source_wm, @reason,
            PARSE_JSON(@evidence), CURRENT_TIMESTAMP());
  END IF;
  COMMIT TRANSACTION;
  SELECT affected AS affected;
END`;
    const params = {
      stream: expect.stream, epoch: expect.epoch, from_state: expect.state, revision: expect.revision,
      owner: next.owner, to_state: next.state, new_epoch: next.epoch, new_revision: next.revision,
      legacy_wm: next.legacyWatermark, cloud_wm: next.cloudWatermark,
      fenced_at: iso(next.fencedAtMs), active_since: iso(next.activeSinceMs),
      changed_by: next.changedBy, reason: next.reason.slice(0, 1000),
      transition_id: log.transitionId, run_id: log.runId, source_wm: log.sourceWatermark,
      evidence: JSON.stringify(log.evidence ?? {}),
    };
    const types: Record<string, string> = {
      stream: 'STRING', epoch: 'INT64', from_state: 'STRING', revision: 'INT64', owner: 'STRING', to_state: 'STRING',
      new_epoch: 'INT64', new_revision: 'INT64', legacy_wm: 'STRING', cloud_wm: 'STRING', fenced_at: 'STRING',
      active_since: 'STRING', changed_by: 'STRING', reason: 'STRING', transition_id: 'STRING', run_id: 'STRING',
      source_wm: 'STRING', evidence: 'STRING',
    };
    try {
      const rows = await this.q<{ affected?: unknown }>(sql, params, types);
      return Number(rows[0]?.affected ?? 0) === 1;
    } catch (e) {
      // Конкурентная транзакция на той же строке: BigQuery прерывает одну из них — это проигранный CAS.
      if (/Transaction is aborted due to concurrent update|Could not serialize access/i.test(String(e instanceof Error ? e.message : e))) return false;
      throw e;
    }
  }

  async qualification(stream: Stream, sinceDate: string): Promise<QualRow[]> {
    const rows = await this.q<Record<string, unknown>>(
      `SELECT CAST(business_date AS STRING) AS bd, check_group, verdict, IFNULL(image_digest, '') AS image_digest,
              UNIX_MILLIS(at) AS at_ms
       FROM ${this.ops(QUALIFICATION_TABLE)} WHERE stream = @stream AND business_date >= DATE(@since)`,
      { stream, since: sinceDate }, { stream: 'STRING', since: 'STRING' },
    );
    return rows.map((r) => ({
      businessDate: String(r.bd), checkGroup: String(r.check_group), verdict: String(r.verdict),
      imageDigest: String(r.image_digest ?? ''), atMs: Number(r.at_ms),
    }));
  }

  /** SQL источников Apps Script по потоку. Каждая ветка: journal, id, start_ts, status, shared. */
  legacySql(stream: Stream, includeShared: boolean): string {
    const ingest = (loaders: string[]): string =>
      `SELECT 'INGEST_RUNS' AS journal, run_id AS id, started_at AS start_ts, status, FALSE AS shared
       FROM ${this.raw('INGEST_RUNS')} WHERE loader_name IN (${loaders.map((l) => `'${l}'`).join(', ')}) AND started_at >= TIMESTAMP(@from_ts)`;
    const stamped = (journal: string, table: string, col: string, re: string, where: string, shared: boolean, prune = ''): string =>
      `SELECT '${journal}' AS journal, id, start_ts, 'LOADED' AS status, ${shared ? 'TRUE' : 'FALSE'} AS shared FROM (
         SELECT DISTINCT ${col} AS id, ${tsFromStamp(`REGEXP_EXTRACT(${col}, r'${re}')`)} AS start_ts
         FROM ${this.raw(table)} WHERE ${where}${prune}) WHERE start_ts >= TIMESTAMP(@from_ts)`;
    const parts: string[] = [];
    switch (stream) {
      case 'stocks':
        // Apps Script: STOCK_SNAP_yyyyMMdd_HHmmss_<uuid>; Cloud Run: STOCK_SNAP_<env>_yyyyMMdd (не попадает).
        parts.push(`SELECT 'WB_STOCKS_SNAPSHOTS' AS journal, snapshot_id AS id, started_at AS start_ts, status, FALSE AS shared
          FROM ${this.raw('WB_STOCKS_SNAPSHOTS')}
          WHERE started_at >= TIMESTAMP(@from_ts) AND REGEXP_CONTAINS(snapshot_id, r'^STOCK_SNAP_[0-9]{8}_[0-9]{6}_')`);
        break;
      case 'statistics':
        parts.push(ingest(['orders', 'sales']));
        if (includeShared) {
          parts.push(stamped('RAW_WB_ORDERS', 'RAW_WB_ORDERS', 'load_id', '^ORD_INC_([0-9]{8}_[0-9]{6})', "STARTS_WITH(load_id, 'ORD_INC_')", true));
          parts.push(stamped('RAW_WB_SALES_RETURNS', 'RAW_WB_SALES_RETURNS', 'load_id', '^SALE_(?:INC|RECON)_([0-9]{8}_[0-9]{6})',
            "(STARTS_WITH(load_id, 'SALE_INC_') OR STARTS_WITH(load_id, 'SALE_RECON_'))", true));
        }
        break;
      case 'ads':
        parts.push(ingest(['ads']));
        // run_id Apps Script — ADSRAW_/ADSAUDIT_yyyyMMdd_HHmmss_<цифры>; у Cloud Run суффикс _CR… (ads/index.ts adsRunId).
        parts.push(stamped('RAW_WB_ADV_COSTS_RUNS', 'RAW_WB_ADV_COSTS_RUNS', 'run_id', '^ADS(?:RAW|AUDIT)_([0-9]{8}_[0-9]{6})',
          "NOT REGEXP_CONTAINS(run_id, r'_CR')", false));
        break;
      case 'finance':
        // Apps Script: FINDAILY_yyyyMMdd_HHmmss_<цифры>; Cloud Run: …_C<цифры> (finance/parse.ts newFinanceRunId).
        parts.push(`SELECT 'FINANCE_LOADER_RUNS' AS journal, run_id AS id, SAFE_CAST(started_at AS TIMESTAMP) AS start_ts, status, FALSE AS shared
          FROM ${this.raw('FINANCE_LOADER_RUNS')}
          WHERE SAFE_CAST(started_at AS TIMESTAMP) >= TIMESTAMP(@from_ts) AND REGEXP_CONTAINS(run_id, r'^FINDAILY_[0-9]{8}_[0-9]{6}_[0-9]+$')`);
        break;
      case 'supplies':
        // Журнала прогонов у поставок нет; run_id DL_yyyyMMdd_HHmmss одинаков у обоих писателей.
        if (includeShared) {
          parts.push(stamped('RAW_WB_SUPPLIES', 'RAW_WB_SUPPLIES', 'run_id', '^DL_([0-9]{8}_[0-9]{6})', "STARTS_WITH(run_id, 'DL_')", true,
            ' AND snapshot_ts >= TIMESTAMP_SUB(TIMESTAMP(@from_ts), INTERVAL 1 DAY)'));
        }
        break;
      case 'ref':
        // REF_yyyyMMdd_HHmmss_<hex8> одинаков у обоих писателей → shared.
        if (includeShared) {
          parts.push(`SELECT 'REF_SYNC_RUNS' AS journal, run_id AS id, SAFE_CAST(started_at AS TIMESTAMP) AS start_ts, status, TRUE AS shared
            FROM ${this.raw('REF_SYNC_RUNS')} WHERE SAFE_CAST(started_at AS TIMESTAMP) >= TIMESTAMP(@from_ts)`);
        }
        break;
    }
    return parts.join('\nUNION ALL\n');
  }

  async legacyRuns(stream: Stream, fromMs: number, includeShared: boolean): Promise<RunRecord[]> {
    const sql = this.legacySql(stream, includeShared);
    if (!sql) return [];
    const rows = await this.q<Record<string, unknown>>(
      `SELECT journal, id, UNIX_MILLIS(start_ts) AS start_ms, status, shared FROM (\n${sql}\n) ORDER BY start_ms`,
      { from_ts: new Date(fromMs).toISOString() }, { from_ts: 'STRING' },
    );
    return rows.map((r) => ({
      journal: String(r.journal), id: String(r.id), startMs: Number(r.start_ms), status: String(r.status ?? ''), shared: r.shared === true,
    }));
  }

  async cloudRuns(stream: Stream, fromMs: number): Promise<CloudRun[]> {
    const loaders = cloudLoadersOf(stream);
    const rows = await this.q<Record<string, unknown>>(
      `SELECT run_id, loader_name, UNIX_MILLIS(started_at) AS start_ms, status, logical_period, IFNULL(error_code, '') AS error_code
       FROM ${this.raw('LOADER_RUNS')}
       WHERE environment = 'prod' AND loader_name IN (${loaders.map((l) => `'${l}'`).join(', ')})
         AND started_at >= TIMESTAMP(@from_ts)
       ORDER BY started_at`,
      { from_ts: new Date(fromMs).toISOString() }, { from_ts: 'STRING' },
    );
    return rows.map((r) => ({
      id: String(r.run_id), loader: String(r.loader_name), startMs: Number(r.start_ms), status: String(r.status ?? ''),
      logicalPeriod: String(r.logical_period ?? ''), errorCode: String(r.error_code ?? ''),
    }));
  }

  jobsSql(stream: Stream): string {
    const t = PROFILES[stream].writeTables;
    const region = `region-${this.bq.location.toLowerCase()}`;
    const exact = t.exact.map((x) => `'${x}'`).join(', ');
    const dest = [
      exact ? `destination_table.table_id IN (${exact})` : 'FALSE',
      ...t.prefixes.map((p) => `STARTS_WITH(destination_table.table_id, '${p}')`),
    ].join(' OR ');
    const names = [...t.exact, ...t.prefixes.map((p) => `${p}[A-Za-z0-9_]*`)].join('|');
    return `SELECT job_id, user_email, UNIX_MILLIS(creation_time) AS created_ms, state,
       IFNULL(destination_table.table_id, '') AS dest, IFNULL(statement_type, job_type) AS stmt
FROM \`${region}\`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP(@from_ts) AND state != 'DONE'
  AND ((destination_table.dataset_id = '${this.rawDataset}' AND (${dest}))
       OR (statement_type = 'SCRIPT' AND REGEXP_CONTAINS(query, r'(?:^|[^A-Za-z0-9_])(?:${names})(?:[^A-Za-z0-9_]|$)')))`;
  }

  async pendingWriteJobs(stream: Stream, fromMs: number): Promise<PendingJob[]> {
    const rows = await this.q<Record<string, unknown>>(this.jobsSql(stream), { from_ts: new Date(fromMs).toISOString() }, { from_ts: 'STRING' });
    return rows.map((r) => ({
      jobId: String(r.job_id), user: String(r.user_email ?? ''), createdMs: Number(r.created_ms), state: String(r.state ?? ''),
      table: String(r.dest ?? ''), statementType: String(r.stmt ?? ''),
    }));
  }

  async statsWatermarkBefore(beforeMs: number): Promise<string | null> {
    const before = new Date(beforeMs).toISOString();
    const rows = await this.q<{ wm?: unknown }>(
      `SELECT TO_JSON_STRING(STRUCT(
         (SELECT MAX(last_change_date) FROM ${this.raw('RAW_WB_ORDERS')}
           WHERE source_api = 'WB_API_ORDERS' AND STARTS_WITH(load_id, 'ORD_INC_') AND last_change_date <> ''
             AND ${tsFromStamp(`REGEXP_EXTRACT(load_id, r'^ORD_INC_([0-9]{8}_[0-9]{6})')`)} < TIMESTAMP(@before)) AS orders,
         (SELECT MAX(last_change_date) FROM ${this.raw('RAW_WB_SALES_RETURNS')}
           WHERE source_api = 'WB_API_SALES' AND STARTS_WITH(load_id, 'SALE_INC_')
             AND ${tsFromStamp(`REGEXP_EXTRACT(load_id, r'^SALE_INC_([0-9]{8}_[0-9]{6})')`)} < TIMESTAMP(@before)) AS sales)) AS wm`,
      { before }, { before: 'STRING' },
    );
    const v = rows[0]?.wm;
    return v === null || v === undefined || v === '' ? null : String(v);
  }

  watermarkSql(stream: Stream): string {
    switch (stream) {
      case 'stocks':
        return `SELECT CAST(MAX(period_to) AS STRING) AS wm FROM ${this.raw('WB_STOCKS_SNAPSHOTS')} WHERE status = 'COMPLETE'`;
      case 'statistics':
        // Те же выражения, что у загрузчика M2 (statistics/bq.ts ordersBoundary/salesBoundary): форма хранения.
        return `SELECT TO_JSON_STRING(STRUCT(
          (SELECT COALESCE(MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'ORD_INC_'), last_change_date, NULL)), MAX(last_change_date))
             FROM ${this.raw('RAW_WB_ORDERS')} WHERE source_api = 'WB_API_ORDERS' AND last_change_date <> '') AS orders,
          (SELECT COALESCE(MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'SALE_INC_'), last_change_date, NULL)),
                           MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'SALE_RECON_'), NULL, last_change_date)))
             FROM ${this.raw('RAW_WB_SALES_RETURNS')} WHERE source_api = 'WB_API_SALES') AS sales)) AS wm`;
      case 'ads':
        return `SELECT MAX(lp) AS wm FROM (
          SELECT logical_period AS lp FROM ${this.raw('INGEST_RUNS')} WHERE loader_name = 'ads' AND status = 'COMPLETE'
          UNION ALL
          SELECT logical_period FROM ${this.raw('LOADER_RUNS')} WHERE environment = 'prod' AND loader_name = 'ads' AND status = 'COMPLETE')`;
      case 'finance':
        return `SELECT CAST(MAX(DATE(SAFE_CAST(started_at AS TIMESTAMP), 'Europe/Moscow')) AS STRING) AS wm
          FROM ${this.raw('FINANCE_LOADER_RUNS')} WHERE status NOT IN ('RUNNING', 'STARTED', 'ERROR')`;
      case 'supplies':
        return `SELECT CAST(MAX(DATE(snapshot_ts, 'Europe/Moscow')) AS STRING) AS wm FROM ${this.raw('RAW_WB_SUPPLIES')}`;
      case 'ref':
        return `SELECT active_ref_run_id AS wm FROM ${this.raw('REF_ACTIVE_VERSION')} WHERE id = 'singleton'`;
    }
  }

  async currentWatermark(stream: Stream): Promise<string | null> {
    const rows = await this.q<{ wm?: unknown }>(this.watermarkSql(stream), {}, {});
    const v = rows[0]?.wm;
    return v === null || v === undefined || v === '' ? null : String(v);
  }

  async continuityViolations(stream: Stream, legacyWatermark: string, sinceMs: number): Promise<number> {
    if (stream !== 'statistics') return 0;
    const wm = parseStatsWatermark(legacyWatermark);
    if (!wm) throw new Error(`legacy watermark statistics не разобран: ${legacyWatermark}`);
    const since = new Date(sinceMs).toISOString();
    const rows = await this.q<{ n?: unknown }>(
      `SELECT
         (SELECT COUNT(*) FROM ${this.raw('RAW_WB_ORDERS')}
           WHERE STARTS_WITH(load_id, 'ORD_INC_') AND last_change_date < @orders_wm
             AND ${tsFromStamp(`REGEXP_EXTRACT(load_id, r'^ORD_INC_([0-9]{8}_[0-9]{6})')`)} > TIMESTAMP(@since))
       + (SELECT COUNT(*) FROM ${this.raw('RAW_WB_SALES_RETURNS')}
           WHERE STARTS_WITH(load_id, 'SALE_INC_') AND last_change_date < @sales_wm
             AND ${tsFromStamp(`REGEXP_EXTRACT(load_id, r'^SALE_INC_([0-9]{8}_[0-9]{6})')`)} > TIMESTAMP(@since)) AS n`,
      { orders_wm: wm.orders, sales_wm: wm.sales, since }, { orders_wm: 'STRING', sales_wm: 'STRING', since: 'STRING' },
    );
    return Number(rows[0]?.n ?? 0);
  }

  async duplicates(stream: Stream, sinceMs: number): Promise<number> {
    if (stream !== 'stocks') return 0;
    // Два COMPLETE-снимка за одни сутки витрина «молча подменит» (MIG_FINAL_INTEGRATION_GATE §A.7).
    const rows = await this.q<{ n?: unknown }>(
      `SELECT COUNT(*) AS n FROM (
         SELECT CAST(period_to AS STRING) AS d FROM ${this.raw('WB_STOCKS_SNAPSHOTS')}
         WHERE status = 'COMPLETE' AND CAST(period_to AS STRING) >= @since_date
         GROUP BY d HAVING COUNT(*) > 1)`,
      { since_date: new Date(sinceMs + 3 * 3_600_000).toISOString().slice(0, 10) }, { since_date: 'STRING' },
    );
    return Number(rows[0]?.n ?? 0);
  }

  async downstreamAlive(kind: 'mart' | 'ref_active', sinceMs: number): Promise<boolean> {
    const since = new Date(sinceMs).toISOString();
    const sql = kind === 'mart'
      ? `SELECT COUNT(*) AS n FROM ${this.raw('LOADER_RUNS')}
         WHERE environment = 'prod' AND loader_name = 'mart' AND status = 'COMPLETE' AND started_at > TIMESTAMP(@since)`
      : `SELECT COUNT(*) AS n FROM ${this.raw('REF_ACTIVE_VERSION')}
         WHERE id = 'singleton' AND ${tsFromStamp(`REGEXP_EXTRACT(active_ref_run_id, r'^REF_([0-9]{8}_[0-9]{6})_')`)} > TIMESTAMP(@since)`;
    const rows = await this.q<{ n?: unknown }>(sql, { since }, { since: 'STRING' });
    return Number(rows[0]?.n ?? 0) > 0;
  }
}

// ── разбор ───────────────────────────────────────────────────────────────────────────────────


export function cloudLoadersOf(stream: Stream): string[] {
  return [...PROFILES[stream].cloudLoaders];
}

export function parseStatsWatermark(s: string): { orders: string; sales: string } | null {
  try {
    const o = JSON.parse(s) as { orders?: unknown; sales?: unknown };
    if (typeof o.orders !== 'string' || typeof o.sales !== 'string' || !o.orders || !o.sales) return null;
    return { orders: o.orders, sales: o.sales };
  } catch {
    return null;
  }
}

const num = (v: unknown): number | null => (v === null || v === undefined || v === '' ? null : Number(v));

export function parseRow(r: Record<string, unknown>): OwnershipRow {
  const stream = String(r.stream ?? '');
  const owner = String(r.owner ?? '');
  const state = String(r.state ?? '');
  if (!isStream(stream)) throw new Error(`${OWNERSHIP_TABLE}: неизвестный поток '${stream}'`);
  if (!isOwner(owner)) throw new Error(`${OWNERSHIP_TABLE}: неизвестный owner '${owner}' у ${stream}`);
  if (!isState(state)) throw new Error(`${OWNERSHIP_TABLE}: неизвестное состояние '${state}' у ${stream}`);
  const epoch = Number(r.epoch);
  const revision = Number(r.revision);
  if (!Number.isSafeInteger(epoch) || epoch < 1) throw new Error(`${OWNERSHIP_TABLE}: невалидный epoch у ${stream}`);
  if (!Number.isSafeInteger(revision) || revision < 0) throw new Error(`${OWNERSHIP_TABLE}: невалидный revision у ${stream}`);
  return {
    stream, owner, state, epoch, revision,
    legacyWatermark: r.legacy_watermark == null ? null : String(r.legacy_watermark),
    cloudWatermark: r.cloud_watermark == null ? null : String(r.cloud_watermark),
    autoCutoverEnabled: r.auto_cutover_enabled === true,
    fencedAtMs: num(r.fenced_at_ms),
    activeSinceMs: num(r.active_since_ms),
    changedAtMs: Number(r.changed_at_ms ?? 0),
    changedBy: String(r.changed_by ?? ''),
    reason: String(r.reason ?? ''),
  };
}
