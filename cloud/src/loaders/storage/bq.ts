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
   * Atomic window replacement with a Terraform-managed fixed staging table.
   * Runtime SA does not need tables.create/drop: only table-level dataEditor on target + stage.
   * A failed API/download/staging load leaves the previous target window intact.
   *
   * FAIL-CLOSED (инцидент 14.09.2026, execution wb-paid-storage-prod-dxkt5): КАЖДАЯ блокирующая
   * проверка выполняется ВНУТРИ той же транзакции и ДО COMMIT. Раньше post-load QA шёл отдельным
   * запросом после COMMIT — его падение оставляло target уже изменённым при статусе ERROR.
   * Теперь любой провал ассерта поднимает ошибку внутри транзакции, обработчик делает ROLLBACK,
   * и target остаётся в прежнем состоянии. Заполненный stage при этом — норма: следующий
   * прогон перезапишет его WRITE_TRUNCATE.
   *
   * Используются IF/RAISE, а не ASSERT: это базовые процедурные операторы, чьё поведение внутри
   * multi-statement transaction однозначно, и они дают сообщение с ожидаемым и фактическим
   * значением. Форма обработчика — канонический паттерн BigQuery (ROLLBACK + RAISE).
   */
  async replaceWindow(
    table: string,
    rows: PaidStorageRow[],
    startDate: string,
    endDate: string,
    jobId: string,
    expected: { rows: number; days: number; storageRub: number },
  ): Promise<number> {
    if (rows.length === 0) throw new Error('Refusing to replace paid-storage window with zero rows');
    if (!/^[A-Za-z0-9_]+$/.test(table)) throw new Error(`Invalid BigQuery table name '${table}'`);

    const stageTable = `${table}__STAGE`;
    const target = this.fqn(table);
    const stage = this.fqn(stageTable);
    const suffix = jobId.replace(/[^A-Za-z0-9_]/g, '_').slice(-70);
    const file = join(tmpdir(), `wb_paid_storage_${suffix}.jsonl`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n') + '\n', 'utf8');

    const md: JobLoadMetadata = {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      writeDisposition: 'WRITE_TRUNCATE',
      createDisposition: 'CREATE_NEVER',
      jobId,
      location: this.location,
    };

    const inWindow = 'date_msk BETWEEN DATE(@start) AND DATE(@end)';

    try {
      await this.bq.dataset(this.dataset).table(stageTable).load(file, md);
      const stageCount = await this.countWindow(stageTable, startDate, endDate);
      if (stageCount !== rows.length) {
        throw new Error(`Paid-storage staging QA failed: expected ${rows.length}, got ${stageCount}`);
      }

      await this.bq.query({
        query: `BEGIN
  DECLARE stage_rows, stage_days, stage_outside, target_rows, target_days INT64;
  DECLARE stage_rub, target_rub FLOAT64;

  BEGIN TRANSACTION;

  -- Gate 1: stage обязан быть ровно тем, что мы собираемся вставить. Проверяется ДО DELETE,
  -- поэтому на этом этапе target не тронут вообще.
  SET stage_rows = (SELECT COUNT(*) FROM ${stage});
  SET stage_days = (SELECT COUNT(DISTINCT date_msk) FROM ${stage});
  SET stage_outside = (SELECT COUNT(*) FROM ${stage} WHERE NOT (${inWindow}));
  SET stage_rub = (SELECT CAST(ROUND(IFNULL(SUM(warehouse_price), 0), 2) AS FLOAT64) FROM ${stage});
  IF stage_rows != @expectedRows THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_ROWS: ожидалось %t, в stage %t', @expectedRows, stage_rows);
  END IF;
  IF stage_outside != 0 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_OUT_OF_WINDOW: %t строк вне окна %t..%t', stage_outside, @start, @end);
  END IF;
  IF stage_days != @expectedDays THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_DAYS: ожидалось %t, в stage %t', @expectedDays, stage_days);
  END IF;
  IF ABS(stage_rub - @expectedRub) > 0.02 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_AMOUNT: источник %t, stage %t', @expectedRub, stage_rub);
  END IF;

  -- Атомарная замена окна. Гранулярность — окно дат, всё вне [start, end] не затрагивается.
  DELETE FROM ${target} WHERE ${inWindow};
  INSERT INTO ${target} SELECT * FROM ${stage};

  -- Gate 2: тот же post-load QA, что раньше выполнялся ПОСЛЕ коммита. Транзакция видит
  -- собственные изменения, поэтому проверка честная — но провал ещё откатывается.
  SET target_rows = (SELECT COUNT(*) FROM ${target} WHERE ${inWindow});
  SET target_days = (SELECT COUNT(DISTINCT date_msk) FROM ${target} WHERE ${inWindow});
  SET target_rub = (SELECT CAST(ROUND(IFNULL(SUM(warehouse_price), 0), 2) AS FLOAT64) FROM ${target} WHERE ${inWindow});
  IF target_rows != @expectedRows THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_ROWS: ожидалось %t, в target %t', @expectedRows, target_rows);
  END IF;
  IF target_days != @expectedDays THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_DAYS: ожидалось %t, в target %t', @expectedDays, target_days);
  END IF;
  IF ABS(target_rub - @expectedRub) > 0.02 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_AMOUNT: источник %t, target %t', @expectedRub, target_rub);
  END IF;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;`,
        params: {
          start: startDate,
          end: endDate,
          expectedRows: expected.rows,
          expectedDays: expected.days,
          expectedRub: expected.storageRub,
        },
        types: {
          start: 'STRING',
          end: 'STRING',
          expectedRows: 'INT64',
          expectedDays: 'INT64',
          expectedRub: 'FLOAT64',
        },
        location: this.location,
      });
    } finally {
      try { rmSync(file, { force: true }); } catch { /* noop */ }
    }

    return this.countWindow(table, startDate, endDate);
  }

  async countWindow(table: string, startDate: string, endDate: string): Promise<number> {
    if (!/^[A-Za-z0-9_]+$/.test(table)) throw new Error(`Invalid BigQuery table name '${table}'`);
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS n FROM ${this.fqn(table)} WHERE date_msk BETWEEN DATE(@start) AND DATE(@end)`,
      params: { start: startDate, end: endDate },
      types: { start: 'STRING', end: 'STRING' },
      location: this.location,
    });
    return Number((rows as Array<{ n?: unknown }>)[0]?.n ?? 0);
  }

  /** ВНИМАНИЕ: `rows` — зарезервированное слово GoogleSQL (оконная фраза ROWS BETWEEN).
   *  Алиас COUNT(*) назван row_count; имя поля результата (.rows) намеренно не менялось. */
  async qaWindow(table: string, startDate: string, endDate: string): Promise<{ rows: number; days: number; nm: number; storageRub: number }> {
    if (!/^[A-Za-z0-9_]+$/.test(table)) throw new Error(`Invalid BigQuery table name '${table}'`);
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS row_count, COUNT(DISTINCT date_msk) AS days, COUNT(DISTINCT nm_id) AS nm,
                     ROUND(SUM(IFNULL(warehouse_price,0)), 2) AS storage_rub
              FROM ${this.fqn(table)} WHERE date_msk BETWEEN DATE(@start) AND DATE(@end)`,
      params: { start: startDate, end: endDate },
      types: { start: 'STRING', end: 'STRING' },
      location: this.location,
    });
    const r = (rows as Array<Record<string, unknown>>)[0] ?? {};
    return { rows: Number(r.row_count ?? 0), days: Number(r.days ?? 0), nm: Number(r.nm ?? 0), storageRub: Number(r.storage_rub ?? 0) };
  }
}
