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
import type { AppendResult } from './postLoad.js';

function isAlreadyExists(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 409 || /already exists/i.test(msg);
}

/** Минимальный контракт BQ-клиента (для инъекции фейка в тестах). */
export interface BqLike {
  query(options: { query: string; params?: Record<string, unknown>; location?: string }): Promise<[unknown[]]>;
  dataset(datasetId: string): { table(tableId: string): { load(source: string, metadata: JobLoadMetadata): Promise<unknown> } };
  job(id: string, options?: { location?: string }): { get(): Promise<[{ metadata?: unknown }]> };
}

export class StocksBq {
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
    // Upsert: одна строка на детерминированный snapshot_id; повтор сбрасывает её в STARTED.
    await this.bq.query({
      query: `MERGE ${this.fqn(table)} T
              USING (SELECT @id AS snapshot_id) S ON T.snapshot_id = S.snapshot_id
              WHEN MATCHED AND T.status != 'COMPLETE' THEN UPDATE SET status='STARTED', started_at=TIMESTAMP(@ts),
                completed_at=NULL, error_message=NULL, period_from=@from, period_to=@to
              WHEN NOT MATCHED THEN INSERT (snapshot_id, started_at, status, period_from, period_to)
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

  /**
   * Идемпотентный append: load job с ДЕТЕРМИНИРОВАННЫМ jobId (стабилен для
   * env×period×table, см. jobId.ts). Повтор периода → тот же jobId → BQ отдаёт
   * 409 Already Exists; если прошлая джоба успешна — трактуем как идемпотентный
   * повтор (данные уже загружены), иначе — ошибка (jobId «сожжён» неуспехом).
   * snapshotId используется только для имени временного файла.
   */
  /**
   * Пометить снимок как COMPLETE(REUSED) при идемпотентном повторе. НЕ перезаписывает
   * метрики исходной успешной загрузки (expected_rows/unique_nm_ids/суммы/unmatched/
   * distinct/duplicate) — они относятся к УЖЕ загруженным данным, а не к новому fetch.
   */
  async manifestMarkReused(table: string, snapshotId: string, writtenRows: number): Promise<void> {
    await this.bq.query({
      query: `UPDATE ${this.fqn(table)} SET
                status='COMPLETE', control_status='REUSED', completed_at=CURRENT_TIMESTAMP(),
                written_rows=@written, error_message=''
              WHERE snapshot_id=@id AND status IN ('STARTED', 'COMPLETE')`,
      params: { written: writtenRows, id: snapshotId },
      location: this.location,
    });
  }

  async appendRaw(table: string, rows: RawStockRow[], snapshotId: string, loadJobId: string): Promise<AppendResult> {
    const file = join(tmpdir(), `stocks_${snapshotId}.ndjson`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n'));
    try {
      const meta: JobLoadMetadata = {
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        writeDisposition: 'WRITE_APPEND',
        location: this.location,
        jobId: loadJobId,
      };
      await this.bq.dataset(this.dataset).table(table).load(file, meta);
      return 'LOADED';
    } catch (e) {
      if (isAlreadyExists(e)) {
        const [job] = await this.bq.job(loadJobId, { location: this.location }).get();
        const st = (job.metadata as { status?: { state?: string; errorResult?: { message?: string } } } | undefined)?.status;
        if (st?.state === 'DONE' && !st.errorResult) return 'ALREADY_LOADED'; // идемпотентный повтор
        throw new Error(`load job ${loadJobId} уже существует и не успешен: ${st?.errorResult?.message ?? st?.state ?? 'unknown'}`);
      }
      throw e;
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
