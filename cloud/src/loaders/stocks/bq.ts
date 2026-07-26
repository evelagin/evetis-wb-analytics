/**
 * BQ I/O загрузчика остатков (PR-Mig1). Пишет ТОЛЬКО в переданные таблицы
 * (по умолчанию __CR — теневые). Идемпотентный append через load job с
 * детерминированным jobId. Все задачи с явным location.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { RawStockRow, StockMetrics } from './normalize.js';

export class StocksBq {
  private readonly bq: BigQuery;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
  ) {
    this.bq = new BigQuery({ projectId });
  }

  private fqn(table: string): string {
    return `\`${this.projectId}.${this.dataset}.${table}\``;
  }

  /** SKU-индекс nm_id → internal_sku из REF_SKU_MASTER. */
  async loadSkuIndex(refTable: string): Promise<Map<number, string>> {
    const [rows] = await this.bq.query({
      query: `SELECT nm_id, internal_sku FROM ${this.fqn(refTable)} WHERE nm_id IS NOT NULL`,
      location: this.location,
    });
    const m = new Map<number, string>();
    for (const r of rows as Array<{ nm_id: unknown; internal_sku: unknown }>) {
      const nm = Number(r.nm_id);
      if (Number.isFinite(nm)) m.set(nm, String(r.internal_sku ?? ''));
    }
    return m;
  }

  async manifestStart(table: string, snapshotId: string, startedAtIso: string, from: string, to: string): Promise<void> {
    await this.bq.query({
      query: `INSERT INTO ${this.fqn(table)} (snapshot_id, started_at, status, period_from, period_to)
              VALUES (@id, TIMESTAMP(@ts), 'STARTED', @from, @to)`,
      params: { id: snapshotId, ts: startedAtIso, from, to },
      location: this.location,
    });
  }

  async manifestFinalize(
    table: string,
    snapshotId: string,
    status: 'COMPLETE' | 'ERROR',
    metrics: StockMetrics | null,
    writtenRows: number,
    controlStatus: string,
    errorMessage: string,
  ): Promise<void> {
    await this.bq.query({
      query: `UPDATE ${this.fqn(table)} SET
                status=@status, completed_at=CURRENT_TIMESTAMP(),
                expected_rows=@expected, written_rows=@written,
                distinct_keys=@distinct, duplicate_keys=@dup,
                unique_nm_ids=@uniqnm, warehouses_count=@wh,
                qty_positive_rows=@qpos, qty_zero_rows=@qzero,
                aggregate_warehouse_rows=@agg,
                sum_quantity_all_t6=@sall, sum_quantity_physical_t6=@sphys,
                unmatched_nm_ids=@unmatched,
                control_status=@control, control_delta=NULL,
                error_message=@err
              WHERE snapshot_id=@id AND status='STARTED'`,
      params: {
        status,
        expected: metrics?.expected_rows ?? 0,
        written: writtenRows,
        distinct: metrics?.distinct_keys ?? 0,
        dup: metrics?.duplicate_keys ?? 0,
        uniqnm: metrics?.unique_nm_ids ?? 0,
        wh: metrics?.warehouses_count ?? 0,
        qpos: metrics?.qty_positive_rows ?? 0,
        qzero: metrics?.qty_zero_rows ?? 0,
        agg: metrics?.aggregate_warehouse_rows ?? 0,
        sall: metrics?.sum_quantity_all_t6 ?? 0,
        sphys: metrics?.sum_quantity_physical_t6 ?? 0,
        unmatched: JSON.stringify(metrics?.unmatched_nm_ids ?? []),
        control: controlStatus,
        err: errorMessage,
        id: snapshotId,
      },
      location: this.location,
    });
  }

  /** Идемпотентный append: load job с детерминированным jobId (BQ дедупит по jobId). */
  async appendRaw(table: string, rows: RawStockRow[], snapshotId: string): Promise<void> {
    const file = join(tmpdir(), `stocks_${snapshotId}.ndjson`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n'));
    try {
      const meta: JobLoadMetadata = {
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        writeDisposition: 'WRITE_APPEND',
        location: this.location,
        jobId: `stocks_load_${snapshotId}`,
      };
      await this.bq.dataset(this.dataset).table(table).load(file, meta);
    } finally {
      rmSync(file, { force: true });
    }
  }

  /** Пост-проверка фактически записанного снимка. */
  async snapshotCounts(table: string, snapshotId: string): Promise<{ count: number; distinct: number }> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS c,
                COUNT(DISTINCT CONCAT(CAST(nm_id AS STRING),'|',CAST(chrt_id AS STRING),'|',CAST(warehouse_id AS STRING))) AS d
              FROM ${this.fqn(table)} WHERE snapshot_id=@id`,
      params: { id: snapshotId },
      location: this.location,
    });
    const r = (rows as Array<{ c: unknown; d: unknown }>)[0];
    return { count: Number(r?.c ?? 0), distinct: Number(r?.d ?? 0) };
  }
}
