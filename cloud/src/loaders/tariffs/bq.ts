/**
 * BQ I/O загрузчика тарифов (PR-2).
 *
 * Append-only: строки добавляются load-джобой с ДЕТЕРМИНИРОВАННЫМ jobId.
 * BigQuery дедуплицирует load по jobId, поэтому повтор того же окна наблюдения
 * не создаёт вторую копию строк, а новое окно (другой jobId) записывается всегда —
 * даже если цена не изменилась. MERGE и UPDATE здесь отсутствуют намеренно:
 * схлопывание наблюдений уничтожило бы предмет наблюдения.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { TariffRow } from './normalize.js';

export function isAlreadyExists(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 409 || /already exists/i.test(msg);
}

/** Минимальный контракт BQ-клиента — для инъекции фейка в тестах. */
export interface BqLike {
  query(options: { query: string; params?: Record<string, unknown>; types?: Record<string, string>; location?: string }): Promise<[unknown[]]>;
  dataset(datasetId: string): { table(tableId: string): { load(source: string, metadata: JobLoadMetadata): Promise<unknown> } };
}

export type AppendOutcome = 'LOADED' | 'REUSED';

export class TariffsBq {
  private readonly bq: BqLike;

  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    bqClient?: BqLike,
  ) {
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as BqLike);
  }

  private fqn(table: string): string {
    return `\`${this.projectId}.${this.dataset}.${table}\``;
  }

  /** Append строк снимка. Возвращает REUSED, если этот же снимок уже был загружен. */
  async appendRaw(table: string, rows: TariffRow[], jobId: string): Promise<AppendOutcome> {
    if (rows.length === 0) return 'LOADED';
    const file = join(tmpdir(), `wb_tariffs_${jobId}.jsonl`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n') + '\n', 'utf8');
    const metadata: JobLoadMetadata = {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      writeDisposition: 'WRITE_APPEND',
      createDisposition: 'CREATE_NEVER',
      schemaUpdateOptions: [],
      jobId,
      location: this.location,
    };
    try {
      await this.bq.dataset(this.dataset).table(table).load(file, metadata);
      return 'LOADED';
    } catch (e) {
      if (isAlreadyExists(e)) return 'REUSED';
      throw e;
    } finally {
      try { rmSync(file, { force: true }); } catch { /* временный файл, потеря не важна */ }
    }
  }

  /** Открывает строку манифеста наблюдения тарифов. Повтор суток сбрасывает её в STARTED. */
  async observationStart(table: string, p: {
    observationId: string; date: string; environment: string; runId: string; startedAtIso: string;
  }): Promise<void> {
    await this.bq.query({
      query: `MERGE ${this.fqn(table)} T
              USING (SELECT @id AS observation_id) S ON T.observation_id = S.observation_id
              WHEN MATCHED AND T.status != 'COMPLETE' THEN UPDATE SET
                status = 'STARTED', started_at = TIMESTAMP(@ts), run_id = @run,
                completed_at = NULL, error_code = NULL, error_message = NULL
              WHEN NOT MATCHED THEN INSERT (observation_id, observation_date, environment, run_id, started_at, status)
                VALUES (@id, DATE(@d), @env, @run, TIMESTAMP(@ts), 'STARTED')`,
      params: { id: p.observationId, d: p.date, env: p.environment, run: p.runId, ts: p.startedAtIso },
      location: this.location,
    });
  }

  /** Закрывает строку манифеста. Явные типы обязательны: null'ы приходят в обеих ветках. */
  async observationFinalize(table: string, p: {
    observationId: string;
    status: 'COMPLETE' | 'ERROR' | 'REUSED';
    observedAtIso: string | null;
    kindsRequested: string | null;
    kindsOk: string | null;
    kindsFailed: string | null;
    rowsWritten: number | null;
    httpStatus: number | null;
    schemaStatus: string | null;
    schemaUnknownFields: string | null;
    errorCode: string | null;
    errorMessage: string | null;
  }): Promise<void> {
    await this.bq.query({
      query: `UPDATE ${this.fqn(table)} SET
                status = @status,
                observed_at = CASE WHEN @observed_at IS NULL THEN NULL ELSE TIMESTAMP(@observed_at) END,
                completed_at = CURRENT_TIMESTAMP(),
                kinds_requested = @kinds_req, kinds_ok = @kinds_ok, kinds_failed = @kinds_failed,
                rows_written = @rows_written, http_status = @http_status,
                schema_status = @schema_status, schema_unknown_fields = @schema_unknown,
                error_code = @error_code, error_message = @error_message
              WHERE observation_id = @id`,
      params: {
        id: p.observationId, status: p.status, observed_at: p.observedAtIso,
        kinds_req: p.kindsRequested, kinds_ok: p.kindsOk, kinds_failed: p.kindsFailed,
        rows_written: p.rowsWritten, http_status: p.httpStatus,
        schema_status: p.schemaStatus, schema_unknown: p.schemaUnknownFields,
        error_code: p.errorCode, error_message: p.errorMessage,
      },
      types: {
        id: 'STRING', status: 'STRING', observed_at: 'STRING',
        kinds_req: 'STRING', kinds_ok: 'STRING', kinds_failed: 'STRING',
        rows_written: 'INT64', http_status: 'INT64',
        schema_status: 'STRING', schema_unknown: 'STRING',
        error_code: 'STRING', error_message: 'STRING',
      },
      location: this.location,
    });
  }

  /** Фактическое число строк снимка в RAW — пост-проверка записи. */
  async countObservationRows(table: string, observationId: string): Promise<number> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS n FROM ${this.fqn(table)} WHERE observation_id = @id`,
      params: { id: observationId },
      types: { id: 'STRING' },
      location: this.location,
    });
    const r = (rows as Array<{ n: unknown }>)[0];
    return r ? Number(r.n) : 0;
  }
}
