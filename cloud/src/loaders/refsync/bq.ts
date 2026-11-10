/**
 * M5: BigQuery-операции REF Sync — те же, что у Apps Script Refsync.gs, с параметрами вместо
 * склейки строк. Все вставки — load job (как bqLoadRows_: строки сразу доступны для DML),
 * с детерминированным jobId от (run_id, таблица): повтор вызова не дублирует строку.
 *
 * Чего здесь НЕТ, в отличие от Apps Script: refEnsureAll_() (создание таблиц и
 * CREATE OR REPLACE VIEW REF_SKU_MASTER на каждом прогоне). Таблицами и вью владеет Terraform
 * (prod-вью уже существует с тем же телом; __CR-объекты — в ref_sync_loader.tf).
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ValidationMetrics } from './normalize.js';

export interface BqLike {
  query(options: { query: string; params?: Record<string, unknown>; types?: Record<string, string>; location?: string }): Promise<[unknown[]]>;
  dataset(datasetId: string): { table(tableId: string): { load(source: string, metadata: JobLoadMetadata): Promise<unknown> } };
  job(id: string, options?: { location?: string }): { get(): Promise<[{ metadata?: unknown }]> };
}

export const REF_ACTIVE_ID = 'singleton';
export const REF_KEEP_VERSIONS = 10;

export interface RefTables { data: string; active: string; runs: string; tlog: string }

export interface RefStore {
  activeBefore(): Promise<string | null>;
  insertRun(runId: string, startedIso: string, activeBefore: string | null): Promise<void>;
  loadData(runId: string, rows: object[]): Promise<void>;
  validate(runId: string): Promise<ValidationMetrics>;
  tableLog(runId: string, r: { name: string; src: number; norm: number; staged: number; published: number; valErr: number; status: string }): Promise<void>;
  finalizeRun(runId: string, status: 'COMPLETE' | 'ERROR', activeAfter: string | null, errorMessage: string): Promise<void>;
  activate(runId: string): Promise<void>;
  prune(): Promise<void>;
}

export class RefBq implements RefStore {
  private readonly bq: BqLike;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    private readonly t: RefTables,
    bqClient?: BqLike,
  ) {
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as BqLike);
  }

  private fqn(table: string): string {
    return `\`${this.projectId}.${this.dataset}.${table}\``;
  }

  private async q(query: string, params: Record<string, unknown> = {}, types?: Record<string, string>): Promise<unknown[]> {
    const [rows] = await this.bq.query({ query, params, ...(types ? { types } : {}), location: this.location });
    return rows;
  }

  private async load(table: string, rows: object[], jobId: string): Promise<void> {
    if (!rows.length) return;
    const file = join(tmpdir(), `${jobId}.ndjson`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n'));
    try {
      const meta: JobLoadMetadata = {
        sourceFormat: 'NEWLINE_DELIMITED_JSON', createDisposition: 'CREATE_NEVER', writeDisposition: 'WRITE_APPEND',
        ignoreUnknownValues: true, maxBadRecords: 0, location: this.location, jobId,
      };
      await this.bq.dataset(this.dataset).table(table).load(file, meta);
    } catch (e) {
      const code = (e as { code?: number }).code;
      const msg = e instanceof Error ? e.message : String(e);
      if (code === 409 || /already exists/i.test(msg)) {
        const [job] = await this.bq.job(jobId, { location: this.location }).get();
        const st = (job.metadata as { status?: { state?: string; errorResult?: { message?: string } } } | undefined)?.status;
        if (st?.state === 'DONE' && !st.errorResult) return; // идемпотентный повтор
        throw new Error(`load job ${jobId} уже существует и не успешен: ${st?.errorResult?.message ?? st?.state ?? 'unknown'}`);
      }
      throw e;
    } finally {
      rmSync(file, { force: true });
    }
  }

  private jobId(runId: string, table: string, tag = ''): string {
    return `refsync_${runId}_${table}${tag}`.replace(/[^A-Za-z0-9_-]/g, '_');
  }

  async activeBefore(): Promise<string | null> {
    const rows = await this.q(`SELECT active_ref_run_id FROM ${this.fqn(this.t.active)} WHERE id = @id`, { id: REF_ACTIVE_ID });
    const v = (rows[0] as { active_ref_run_id?: unknown } | undefined)?.active_ref_run_id;
    return v === null || v === undefined ? null : String(v);
  }

  async insertRun(runId: string, startedIso: string, activeBefore: string | null): Promise<void> {
    await this.load(this.t.runs, [{
      run_id: runId, status: 'STARTED', started_at: startedIso, finished_at: null,
      active_version_before: activeBefore, active_version_after: null,
      error_message: '', warning_count: 0, warning_fingerprint: '',
    }], this.jobId(runId, this.t.runs));
  }

  async loadData(runId: string, rows: object[]): Promise<void> {
    await this.load(this.t.data, rows, this.jobId(runId, this.t.data));
  }

  async validate(runId: string): Promise<ValidationMetrics> {
    const rows = await this.q(
      `SELECT COUNT(*) AS n, COUNT(DISTINCT nm_id) AS n_nm, COUNTIF(nm_id IS NULL) AS null_nm,
              COUNTIF(internal_sku IS NULL OR TRIM(internal_sku) = '') AS null_sku,
              COUNTIF((is_bundle AND product_type != 'bundle') OR (NOT is_bundle AND product_type != 'single')) AS bundle_mismatch,
              COUNTIF(product_type = 'single') AS n_single, COUNTIF(product_type = 'bundle') AS n_bundle
       FROM ${this.fqn(this.t.data)} WHERE ref_run_id = @run`, { run: runId });
    const r = (rows[0] ?? {}) as Record<string, unknown>;
    const n = (k: string) => Number(r[k] ?? 0);
    return { n: n('n'), n_nm: n('n_nm'), null_nm: n('null_nm'), null_sku: n('null_sku'), bundle_mismatch: n('bundle_mismatch'), n_single: n('n_single'), n_bundle: n('n_bundle') };
  }

  async tableLog(runId: string, r: { name: string; src: number; norm: number; staged: number; published: number; valErr: number; status: string }): Promise<void> {
    await this.load(this.t.tlog, [{
      run_id: runId, ref_name: r.name, source_rows: r.src, normalized_rows: r.norm,
      staged_rows: r.staged, published_rows: r.published, validation_error_count: r.valErr, status: r.status,
    }], this.jobId(runId, this.t.tlog, `_${r.status}`));
  }

  async finalizeRun(runId: string, status: 'COMPLETE' | 'ERROR', activeAfter: string | null, errorMessage: string): Promise<void> {
    await this.q(
      `UPDATE ${this.fqn(this.t.runs)} SET status = @status, finished_at = CURRENT_TIMESTAMP(),
         active_version_after = @after, error_message = @err WHERE run_id = @run`,
      { status, after: activeAfter, err: errorMessage, run: runId },
      { status: 'STRING', after: 'STRING', err: 'STRING', run: 'STRING' });
  }

  /** Одна атомарная DML — переключение активной версии (refActivate_). */
  async activate(runId: string): Promise<void> {
    await this.q(
      `MERGE ${this.fqn(this.t.active)} T
       USING (SELECT @id AS id, @run AS active_ref_run_id, CURRENT_TIMESTAMP() AS activated_at) S ON T.id = S.id
       WHEN MATCHED THEN UPDATE SET active_ref_run_id = S.active_ref_run_id, activated_at = S.activated_at
       WHEN NOT MATCHED THEN INSERT (id, active_ref_run_id, activated_at) VALUES (S.id, S.active_ref_run_id, S.activated_at)`,
      { id: REF_ACTIVE_ID, run: runId });
  }

  /** Retention: последние 10 версий по MAX(_synced_at) + активная (refPrune_). */
  async prune(): Promise<void> {
    const t = this.fqn(this.t.data);
    await this.q(
      `DELETE FROM ${t} WHERE ref_run_id NOT IN (
         SELECT ref_run_id FROM (SELECT ref_run_id, MAX(_synced_at) AS mx FROM ${t} GROUP BY ref_run_id ORDER BY mx DESC LIMIT ${REF_KEEP_VERSIONS}))
       AND ref_run_id != IFNULL((SELECT active_ref_run_id FROM ${this.fqn(this.t.active)} WHERE id = @id), '')`,
      { id: REF_ACTIVE_ID });
  }
}
