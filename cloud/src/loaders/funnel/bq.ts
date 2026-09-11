/**
 * BQ I/O воронки. Append-only с ДЕТЕРМИНИРОВАННЫМ jobId: повтор того же окна
 * не удваивает строки (BigQuery дедуплицирует load по jobId), а новое окно
 * пишется всегда. MERGE намеренно нет: RAW — это журнал наблюдений.
 *
 * Дедупликация «одна строка на (date_msk, nm_id)» живёт НЕ здесь, а в витрине
 * V_WB_FUNNEL_DAILY: берём последнее наблюдение. Так исправление задним числом
 * (WB пересчитывает воронку несколько дней) видно в истории, а потребитель
 * получает актуальное значение.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { FunnelRow } from './normalize.js';

export function isAlreadyExists(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 409 || /already exists/i.test(msg);
}

export interface BqLike {
  query(o: { query: string; params?: Record<string, unknown>; types?: Record<string, string>; location?: string }): Promise<[unknown[]]>;
  dataset(d: string): { table(t: string): { load(src: string, md: JobLoadMetadata): Promise<unknown> } };
}

export type AppendOutcome = 'LOADED' | 'REUSED';

export class FunnelBq {
  private readonly bq: BqLike;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    bqClient?: BqLike,
  ) {
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as BqLike);
  }
  private fqn(t: string): string { return `\`${this.projectId}.${this.dataset}.${t}\``; }

  /** nmID активных карточек WB из справочника: метод воронки требует явный список. */
  async loadNmIds(refTable: string): Promise<number[]> {
    const [rows] = await this.bq.query({
      query: `SELECT DISTINCT nm_id FROM ${this.fqn(refTable)}
              WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL
              ORDER BY nm_id`,
      location: this.location,
    });
    return (rows as Array<{ nm_id: unknown }>).map((r) => Number(r.nm_id)).filter((n) => Number.isFinite(n));
  }

  async appendRaw(table: string, rows: FunnelRow[], jobId: string): Promise<AppendOutcome> {
    if (rows.length === 0) return 'LOADED';
    const file = join(tmpdir(), `wb_funnel_${jobId}.jsonl`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n') + '\n', 'utf8');
    const md: JobLoadMetadata = {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      writeDisposition: 'WRITE_APPEND',
      createDisposition: 'CREATE_NEVER',
      schemaUpdateOptions: [],
      jobId,
      location: this.location,
    };
    try {
      await this.bq.dataset(this.dataset).table(table).load(file, md);
      return 'LOADED';
    } catch (e) {
      if (isAlreadyExists(e)) return 'REUSED';
      throw e;
    } finally {
      try { rmSync(file, { force: true }); } catch { /* временный файл */ }
    }
  }

  async observationStart(table: string, p: {
    observationId: string; startDate: string; endDate: string;
    environment: string; runId: string; startedAtIso: string;
  }): Promise<void> {
    await this.bq.query({
      query: `MERGE ${this.fqn(table)} T
              USING (SELECT @id AS observation_id) S ON T.observation_id = S.observation_id
              WHEN MATCHED AND T.status != 'COMPLETE' THEN UPDATE SET
                status='STARTED', started_at=TIMESTAMP(@ts), run_id=@run,
                completed_at=NULL, error_code=NULL, error_message=NULL
              WHEN NOT MATCHED THEN INSERT
                (observation_id, window_start, window_end, environment, run_id, started_at, status)
                VALUES (@id, DATE(@s), DATE(@e), @env, @run, TIMESTAMP(@ts), 'STARTED')`,
      params: { id: p.observationId, s: p.startDate, e: p.endDate, env: p.environment, run: p.runId, ts: p.startedAtIso },
      location: this.location,
    });
  }

  async observationFinalize(table: string, p: {
    observationId: string;
    status: 'COMPLETE' | 'ERROR' | 'REUSED';
    observedAtIso: string | null;
    rowsFetched: number | null;
    rowsWritten: number | null;
    rowsRejected: number | null;
    daysCovered: number | null;
    daysExpected: number | null;
    nmCovered: number | null;
    httpStatus: number | null;
    pollAttempts: number | null;
    schemaStatus: string | null;
    schemaUnknownFields: string | null;
    errorCode: string | null;
    errorMessage: string | null;
  }): Promise<void> {
    await this.bq.query({
      query: `UPDATE ${this.fqn(table)} SET
                status=@status,
                observed_at = CASE WHEN @observed_at IS NULL THEN NULL ELSE TIMESTAMP(@observed_at) END,
                completed_at = CURRENT_TIMESTAMP(),
                rows_fetched=@rf, rows_written=@rw, rows_rejected=@rj,
                days_covered=@dc, days_expected=@de, nm_covered=@nc,
                http_status=@hs, poll_attempts=@pa,
                schema_status=@ss, schema_unknown_fields=@su,
                error_code=@ec, error_message=@em
              WHERE observation_id=@id`,
      params: {
        id: p.observationId, status: p.status, observed_at: p.observedAtIso,
        rf: p.rowsFetched, rw: p.rowsWritten, rj: p.rowsRejected,
        dc: p.daysCovered, de: p.daysExpected, nc: p.nmCovered,
        hs: p.httpStatus, pa: p.pollAttempts,
        ss: p.schemaStatus, su: p.schemaUnknownFields,
        ec: p.errorCode, em: p.errorMessage,
      },
      types: {
        id: 'STRING', status: 'STRING', observed_at: 'STRING',
        rf: 'INT64', rw: 'INT64', rj: 'INT64', dc: 'INT64', de: 'INT64', nc: 'INT64',
        hs: 'INT64', pa: 'INT64', ss: 'STRING', su: 'STRING', ec: 'STRING', em: 'STRING',
      },
      location: this.location,
    });
  }

  /** Пост-проверка: сколько строк снимка реально легло в RAW. */
  async countObservationRows(table: string, observationId: string): Promise<number> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS n FROM ${this.fqn(table)} WHERE observation_id=@id`,
      params: { id: observationId }, types: { id: 'STRING' }, location: this.location,
    });
    const r = (rows as Array<{ n: unknown }>)[0];
    return r ? Number(r.n) : 0;
  }
}
