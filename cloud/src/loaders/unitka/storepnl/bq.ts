/**
 * PHASE C — запись снимка компонент SKU в `wb_ops.UNITKA_SKU_COMPONENTS_DAILY` и чтение P&L магазина.
 *
 * Шаблон StorageBq / CpoBq: постоянный stage под Terraform, загрузка WRITE_TRUNCATE, затем
 * multi-statement transaction, где каждая проверка выполняется ДО COMMIT (IF … RAISE → ROLLBACK).
 * Снимок только ДОБАВЛЯЕТСЯ (история снимков — аудит того, какой лист видел P&L); вью берёт последний
 * snapshot_id. Новый snapshot_id обязан быть больше всех существующих — иначе «последний» стал бы старым.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { LoaderError } from '../../../errors.js';
import type { SkuComponentRow } from './parse.js';

export interface StorePnlBqLike {
  query(o: { query: string; params?: Record<string, unknown>; types?: Record<string, unknown>; location?: string }): Promise<[unknown[]]>;
  dataset(d: string): { table(t: string): { load(src: string, md: JobLoadMetadata): Promise<unknown> } };
}

export const SNAPSHOT_TABLE = 'UNITKA_SKU_COMPONENTS_DAILY';
export const PNL_VIEW = 'V_WB_STORE_PNL_MONTHLY';
export const NEW_OPS_VIEW = 'V_WB_FINANCE_NEW_OPERATIONS';

export interface SnapshotMeta {
  snapshotId: string;
  runId: string;
  snapshotAt: string;
  lcd: string;
  imageDigest: string;
  gitSha: string;
}

/** Строка снимка — ровно колонки таблицы (Terraform: infra/terraform/unitka_store_pnl.tf). */
export function snapshotRecord(r: SkuComponentRow, m: SnapshotMeta): Record<string, unknown> {
  return {
    snapshot_id: m.snapshotId, run_id: m.runId, snapshot_at: m.snapshotAt, lcd: m.lcd,
    month_key: r.date_msk.slice(0, 7), ...r, image_digest: m.imageDigest, git_sha: m.gitSha,
  };
}

/** SQL добавления снимка. Вынесен отдельно — его форма закреплена тестом. */
export function snapshotAppendSql(target: string, stage: string): string {
  return `BEGIN
  DECLARE stage_rows, stage_foreign, stage_dup, stage_after_lcd, target_rows, newer INT64;
  DECLARE stage_profit, target_profit FLOAT64;

  BEGIN TRANSACTION;

  -- Gate 1: stage — ровно этот снимок.
  SET stage_rows = (SELECT COUNT(*) FROM ${stage});
  SET stage_foreign = (SELECT COUNTIF(snapshot_id != @snapshotId OR run_id != @runId) FROM ${stage});
  SET stage_dup = (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(date_msk AS STRING), '|', CAST(nm_id AS STRING))) FROM ${stage});
  SET stage_after_lcd = (SELECT COUNTIF(date_msk > DATE(@lcd)) FROM ${stage});
  SET stage_profit = (SELECT IFNULL(SUM(profit), 0) FROM ${stage});
  IF stage_rows != @expectedRows THEN
    RAISE USING MESSAGE = FORMAT('STORE_PNL_STAGE_ROWS: ожидалось %t, в stage %t', @expectedRows, stage_rows);
  END IF;
  IF stage_foreign != 0 THEN
    RAISE USING MESSAGE = FORMAT('STORE_PNL_STAGE_FOREIGN_RUN: %t строк stage от другого прогона', stage_foreign);
  END IF;
  IF stage_dup != 0 OR stage_after_lcd != 0 THEN
    RAISE USING MESSAGE = FORMAT('STORE_PNL_STAGE_KEY: %t дублей сутки × nm, %t суток после LCD', stage_dup, stage_after_lcd);
  END IF;
  IF ABS(stage_profit - @expectedProfit) > 0.01 THEN
    RAISE USING MESSAGE = FORMAT('STORE_PNL_STAGE_AMOUNT: лист %t, stage %t', @expectedProfit, stage_profit);
  END IF;

  -- Gate 2: снимок новее всех существующих (вью берёт MAX(snapshot_id)).
  SET newer = (SELECT COUNTIF(snapshot_id >= @snapshotId) FROM ${target});
  IF newer != 0 THEN
    RAISE USING MESSAGE = FORMAT('STORE_PNL_SNAPSHOT_ORDER: %t строк со snapshot_id ≥ %t', newer, @snapshotId);
  END IF;

  INSERT INTO ${target} SELECT * FROM ${stage};

  -- Gate 3: легло ровно то, что проверено.
  SET (target_rows, target_profit) = (SELECT AS STRUCT COUNT(*), IFNULL(SUM(profit), 0) FROM ${target} WHERE snapshot_id = @snapshotId);
  IF target_rows != @expectedRows OR ABS(target_profit - @expectedProfit) > 0.01 THEN
    RAISE USING MESSAGE = FORMAT('STORE_PNL_TARGET_MISMATCH: строк %t (ожидалось %t), прибыль %t (ожидалось %t)', target_rows, @expectedRows, target_profit, @expectedProfit);
  END IF;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;`;
}

export function storePnlRaiseError(e: unknown): unknown {
  const m = /\b(STORE_PNL_[A-Z_]+):/.exec(e instanceof Error ? e.message : String(e));
  return m ? new LoaderError(e instanceof Error ? e.message : String(e), m[1]!) : e;
}

/** Строка V_WB_STORE_PNL_MONTHLY в том виде, в каком её читает план вкладки. */
export type PnlRow = Record<string, unknown>;
export interface NewOperationRow { supplier_oper_name: string; treatment: string; rows_n: number; last_seen: unknown }

export class StorePnlBq {
  private readonly bq: StorePnlBqLike;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly opsDataset: string,
    private readonly martDataset: string,
    bqClient?: StorePnlBqLike,
  ) {
    for (const d of [opsDataset, martDataset]) if (!/^[A-Za-z0-9_]+$/.test(d)) throw new Error(`Invalid dataset '${d}'`);
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as StorePnlBqLike);
  }
  private fqn(d: string, t: string): string { return `\`${this.projectId}.${d}.${t}\``; }

  async appendSnapshot(rows: readonly SkuComponentRow[], m: SnapshotMeta, jobId: string): Promise<void> {
    if (rows.length === 0) throw new LoaderError('пустой снимок: в листе нет ни одной SKU-суток ≤ LCD', 'STORE_PNL_EMPTY');
    const stage = `${SNAPSHOT_TABLE}__STAGE`;
    const file = join(tmpdir(), `store_pnl_${jobId.replace(/[^A-Za-z0-9_]/g, '_').slice(-80)}.jsonl`);
    writeFileSync(file, rows.map((r) => JSON.stringify(snapshotRecord(r, m))).join('\n') + '\n', 'utf8');
    try {
      await this.bq.dataset(this.opsDataset).table(stage).load(file, {
        sourceFormat: 'NEWLINE_DELIMITED_JSON', writeDisposition: 'WRITE_TRUNCATE', createDisposition: 'CREATE_NEVER',
        jobId, location: this.location,
      });
    } finally {
      try { rmSync(file, { force: true }); } catch { /* noop */ }
    }
    const profit = rows.reduce((s, r) => s + r.profit, 0);
    try {
      await this.bq.query({
        query: snapshotAppendSql(this.fqn(this.opsDataset, SNAPSHOT_TABLE), this.fqn(this.opsDataset, stage)),
        params: { snapshotId: m.snapshotId, runId: m.runId, lcd: m.lcd, expectedRows: rows.length, expectedProfit: profit },
        types: { snapshotId: 'STRING', runId: 'STRING', lcd: 'STRING', expectedRows: 'INT64', expectedProfit: 'FLOAT64' },
        location: this.location,
      });
    } catch (e) {
      throw storePnlRaiseError(e);
    }
  }

  async readPnl(): Promise<PnlRow[]> {
    const [rows] = await this.bq.query({ query: `SELECT * FROM ${this.fqn(this.martDataset, PNL_VIEW)} ORDER BY service_month`, location: this.location });
    return rows as PnlRow[];
  }

  async readNewOperations(): Promise<NewOperationRow[]> {
    const [rows] = await this.bq.query({
      query: `SELECT supplier_oper_name, treatment, rows_n, last_seen FROM ${this.fqn(this.martDataset, NEW_OPS_VIEW)} ORDER BY last_seen DESC`,
      location: this.location,
    });
    return rows as NewOperationRow[];
  }
}
