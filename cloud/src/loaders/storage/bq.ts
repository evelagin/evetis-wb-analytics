/** BigQuery I/O для фактического платного хранения WB. */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { PaidStorageRow } from './normalize.js';

export interface StorageBqLike {
  query(o: { query: string; params?: Record<string, unknown>; types?: Record<string, string>; location?: string }): Promise<[unknown[]]>;
  dataset(d: string): { table(t: string): { load(src: string, md: JobLoadMetadata): Promise<unknown> } };
}

export class StorageBq {
  private readonly bq: StorageBqLike;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    bqClient?: StorageBqLike,
  ) {
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as StorageBqLike);
  }
  private fqn(t: string): string { return `\`${this.projectId}.${this.dataset}.${t}\``; }

  /**
   * Replace window after the complete WB report is already downloaded and normalized.
   * Empty input is forbidden by the caller, so a transient empty WB response cannot erase facts.
   */
  async replaceWindow(table: string, rows: PaidStorageRow[], startDate: string, endDate: string, jobId: string): Promise<number> {
    if (rows.length === 0) throw new Error('Refusing to replace paid-storage window with zero rows');

    await this.bq.query({
      query: `DELETE FROM ${this.fqn(table)} WHERE date_msk BETWEEN DATE(@start) AND DATE(@end)`,
      params: { start: startDate, end: endDate },
      types: { start: 'STRING', end: 'STRING' },
      location: this.location,
    });

    const file = join(tmpdir(), `wb_paid_storage_${jobId}.jsonl`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n') + '\n', 'utf8');
    const md: JobLoadMetadata = {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      writeDisposition: 'WRITE_APPEND',
      createDisposition: 'CREATE_NEVER',
      jobId,
      location: this.location,
    };
    try {
      await this.bq.dataset(this.dataset).table(table).load(file, md);
    } finally {
      try { rmSync(file, { force: true }); } catch { /* noop */ }
    }
    return this.countWindow(table, startDate, endDate);
  }

  async countWindow(table: string, startDate: string, endDate: string): Promise<number> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS n FROM ${this.fqn(table)} WHERE date_msk BETWEEN DATE(@start) AND DATE(@end)`,
      params: { start: startDate, end: endDate },
      types: { start: 'STRING', end: 'STRING' },
      location: this.location,
    });
    return Number((rows as Array<{ n?: unknown }>)[0]?.n ?? 0);
  }

  async qaWindow(table: string, startDate: string, endDate: string): Promise<{ rows: number; days: number; nm: number; storageRub: number }> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS rows, COUNT(DISTINCT date_msk) AS days, COUNT(DISTINCT nm_id) AS nm,
                     ROUND(SUM(IFNULL(warehouse_price,0)), 2) AS storage_rub
              FROM ${this.fqn(table)} WHERE date_msk BETWEEN DATE(@start) AND DATE(@end)`,
      params: { start: startDate, end: endDate },
      types: { start: 'STRING', end: 'STRING' },
      location: this.location,
    });
    const r = (rows as Array<Record<string, unknown>>)[0] ?? {};
    return { rows: Number(r.rows ?? 0), days: Number(r.days ?? 0), nm: Number(r.nm ?? 0), storageRub: Number(r.storage_rub ?? 0) };
  }
}
