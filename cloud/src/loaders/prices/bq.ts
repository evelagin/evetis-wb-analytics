/**
 * BQ I/O наблюдателя цен (PR-1).
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
import type { RawPriceRow } from './normalize.js';

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

export class PricesBq {
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

  /**
   * Ожидаемый набор товаров = активные WB-позиции справочника.
   * Именно справочник, а не предыдущий снимок: иначе выпавший из ответа товар
   * навсегда исчез бы и из ожиданий, и покрытие всегда показывало бы 100 %.
   */
  async loadExpectedSku(refTable: string): Promise<Map<number, string>> {
    const [rows] = await this.bq.query({
      query: `SELECT nm_id, internal_sku FROM ${this.fqn(refTable)}
              WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL`,
      location: this.location,
    });
    const m = new Map<number, string>();
    for (const r of rows as Array<{ nm_id: unknown; internal_sku: unknown }>) {
      const nm = Number(r.nm_id);
      if (Number.isFinite(nm)) m.set(nm, String(r.internal_sku ?? ''));
    }
    return m;
  }

  /** Append строк снимка. Возвращает REUSED, если этот же снимок уже был загружен. */
  async appendRaw(table: string, rows: RawPriceRow[], jobId: string): Promise<AppendOutcome> {
    if (rows.length === 0) return 'LOADED';
    const file = join(tmpdir(), `wb_prices_${jobId}.jsonl`);
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

  /** Открывает строку манифеста наблюдения. Повтор окна сбрасывает её в STARTED. */
  async observationStart(table: string, p: {
    observationId: string; bucket: string; environment: string; runId: string; startedAtIso: string;
  }): Promise<void> {
    await this.bq.query({
      query: `MERGE ${this.fqn(table)} T
              USING (SELECT @id AS observation_id) S ON T.observation_id = S.observation_id
              WHEN MATCHED AND T.status != 'COMPLETE' THEN UPDATE SET
                status = 'STARTED', started_at = TIMESTAMP(@ts), run_id = @run,
                completed_at = NULL, error_code = NULL, error_message = NULL
              WHEN NOT MATCHED THEN INSERT (observation_id, observation_bucket, environment, run_id, started_at, status)
                VALUES (@id, @bucket, @env, @run, TIMESTAMP(@ts), 'STARTED')`,
      params: { id: p.observationId, bucket: p.bucket, env: p.environment, run: p.runId, ts: p.startedAtIso },
      location: this.location,
    });
  }

  /**
   * Закрывает строку манифеста. Явные типы параметров обязательны: в ветке ошибки
   * null'ами приходят метрики, в ветке успеха — коды ошибок, и без types BigQuery
   * не выводит тип для null (см. тот же фикс в загрузчике остатков).
   */
  async observationFinalize(table: string, p: {
    observationId: string;
    status: 'COMPLETE' | 'ERROR' | 'REUSED';
    observedAtIso: string | null;
    httpStatus: number | null;
    httpAttempts: number | null;
    expected: number | null;
    observed: number | null;
    missing: number | null;
    unexpected: number | null;
    coveragePct: number | null;
    missingNmIds: string | null;
    unexpectedNmIds: string | null;
    rowsWritten: number | null;
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
                http_status = @http_status, http_attempts = @http_attempts,
                expected_products = @expected, observed_products = @observed,
                missing_products = @missing, unexpected_products = @unexpected,
                coverage_pct = @coverage_pct,
                missing_nm_ids = @missing_ids, unexpected_nm_ids = @unexpected_ids,
                rows_written = @rows_written,
                schema_status = @schema_status, schema_unknown_fields = @schema_unknown,
                error_code = @error_code, error_message = @error_message
              WHERE observation_id = @id`,
      params: {
        id: p.observationId, status: p.status, observed_at: p.observedAtIso,
        http_status: p.httpStatus, http_attempts: p.httpAttempts,
        expected: p.expected, observed: p.observed, missing: p.missing, unexpected: p.unexpected,
        coverage_pct: p.coveragePct, missing_ids: p.missingNmIds, unexpected_ids: p.unexpectedNmIds,
        rows_written: p.rowsWritten, schema_status: p.schemaStatus, schema_unknown: p.schemaUnknownFields,
        error_code: p.errorCode, error_message: p.errorMessage,
      },
      types: {
        id: 'STRING', status: 'STRING', observed_at: 'STRING',
        http_status: 'INT64', http_attempts: 'INT64',
        expected: 'INT64', observed: 'INT64', missing: 'INT64', unexpected: 'INT64',
        coverage_pct: 'NUMERIC', missing_ids: 'STRING', unexpected_ids: 'STRING',
        rows_written: 'INT64', schema_status: 'STRING', schema_unknown: 'STRING',
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
