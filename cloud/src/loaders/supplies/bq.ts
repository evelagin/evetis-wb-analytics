/**
 * M5: BigQuery-слой поставок. Только две операции:
 *   knownUpdated — watermark из ЦЕЛЕВОЙ шапки (prod — RAW_WB_SUPPLIES, shadow — __CR),
 *                  как в Apps Script: MAX(UNIX_MILLIS(updated_dt)) по entity_key;
 *   load         — load job NDJSON (WRITE_APPEND, CREATE_NEVER) с ДЕТЕРМИНИРОВАННЫМ jobId.
 *
 * DML нет вовсе. Apps Script перед записью объекта делал DELETE (run_id, entity_key), потому что
 * повтор того же объекта внутри одного run_id был возможен (курсор, порционный бэкфилл). Здесь run_id
 * уникален на исполнение обработчика, а каждая порция пишется ровно одним load job с jobId от
 * (env, run_id, таблица, порция): повтор вызова получает 409 и не добавляет строк. Дубль внутри
 * run_id невозможен конструкцией, поэтому удалять нечего.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { KnownState } from './normalize.js';

export interface BqLike {
  query(options: { query: string; params?: Record<string, unknown>; location?: string }): Promise<[unknown[]]>;
  dataset(datasetId: string): { table(tableId: string): { load(source: string, metadata: JobLoadMetadata): Promise<unknown> } };
  job(id: string, options?: { location?: string }): { get(): Promise<[{ metadata?: unknown }]> };
}

export type LoadOutcome = 'LOADED' | 'ALREADY_LOADED';

/** jobId BigQuery: [A-Za-z0-9_-], до 1024 символов. */
export function suppliesJobId(env: string, runId: string, table: string, chunk: number): string {
  return `wbsupplies_${env}_${runId}_${table}_c${chunk}`.replace(/[^A-Za-z0-9_-]/g, '_');
}

export class SuppliesBq {
  private readonly bq: BqLike;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    bqClient?: BqLike,
  ) {
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as BqLike);
  }

  private fqn(t: string): string {
    return `\`${this.projectId}.${this.dataset}.${t}\``;
  }

  /**
   * Watermark Apps Script (MAX(UNIX_MILLIS(updated_dt)) по entity_key) + is_completed последней
   * committed-шапки (порядок V_WB_SUPPLIES_CURRENT) — для причины «в RAW не завершён». STRUCT — потому
   * что ARRAY_AGG падает на NULL-элементе, а is_completed формально NULLABLE.
   * Та же одна таблица и один запрос, что у Apps Script.
   */
  async knownUpdated(headerTable: string): Promise<Map<string, KnownState>> {
    const [rows] = await this.bq.query({
      query: `SELECT entity_key, MAX(UNIX_MILLIS(updated_dt)) AS upd_ms,
                ARRAY_AGG(STRUCT(is_completed) ORDER BY snapshot_ts DESC, run_id DESC LIMIT 1)[OFFSET(0)].is_completed AS last_completed
              FROM ${this.fqn(headerTable)} GROUP BY entity_key`,
      location: this.location,
    });
    const m = new Map<string, KnownState>();
    for (const r of rows as Array<{ entity_key: unknown; upd_ms: unknown; last_completed: unknown }>) {
      m.set(String(r.entity_key), {
        updMs: Number(r.upd_ms ?? 0) || 0,
        lastCompleted: r.last_completed === null || r.last_completed === undefined ? null : r.last_completed === true,
      });
    }
    return m;
  }

  async load(table: string, rows: object[], jobId: string): Promise<LoadOutcome> {
    const file = join(tmpdir(), `${jobId}.ndjson`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n'));
    try {
      const meta: JobLoadMetadata = {
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        createDisposition: 'CREATE_NEVER', // таблицами владеет Terraform
        writeDisposition: 'WRITE_APPEND',
        ignoreUnknownValues: true, // как bqLoadRows_ в Apps Script
        maxBadRecords: 0,
        location: this.location,
        jobId,
      };
      await this.bq.dataset(this.dataset).table(table).load(file, meta);
      return 'LOADED';
    } catch (e) {
      const code = (e as { code?: number }).code;
      const msg = e instanceof Error ? e.message : String(e);
      if (code === 409 || /already exists/i.test(msg)) {
        const [job] = await this.bq.job(jobId, { location: this.location }).get();
        const st = (job.metadata as { status?: { state?: string; errorResult?: { message?: string } } } | undefined)?.status;
        if (st?.state === 'DONE' && !st.errorResult) return 'ALREADY_LOADED';
        throw new Error(`load job ${jobId} уже существует и не успешен: ${st?.errorResult?.message ?? st?.state ?? 'unknown'}`);
      }
      throw e;
    } finally {
      rmSync(file, { force: true });
    }
  }
}
