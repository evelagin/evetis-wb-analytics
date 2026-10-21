/**
 * BQ I/O загрузчика остатков (PR-Mig1). Пишет ТОЛЬКО в переданные таблицы
 * (по умолчанию __CR — теневые). Идемпотентный append через load job с
 * детерминированным jobId. Все задачи с явным location.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { StockMetrics } from './normalize.js';
import type { StockControl } from './t5.js';
import type { AppendResult } from './postLoad.js';

/**
 * PR-Mig1c: расширение manifest при включённом T5. Передаётся ТОЛЬКО когда в целевой
 * таблице уже есть колонки t5_*, t6_comparable, availability_* и anonymized_warehouse_rows
 * (их добавляет Terraform вместе с включением STOCKS_T5_MODE=on). Без него finalize
 * пишет прежний набор колонок — образ безопасен для выката раньше схемы.
 */
export interface ManifestT5Ext {
  control: StockControl;
  anonymizedWarehouseRows: number;
  t5RowsWritten: number;
}

function isAlreadyExists(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 409 || /already exists/i.test(msg);
}

/**
 * Ключ грейна снимка для пост-проверки уникальности.
 *
 * 🔴 26.08.2026. Ключ обязан быть ЗЕРКАЛОМ ключа из normalize.ts:
 *   nm_id | chrt_id | warehouse_code.
 * Раньше здесь стоял warehouse_id, и после обезличивания складов (PR #121, где
 * warehouse_id стал NULL для сентинела -999999) выражение ломалось молча:
 * в BigQuery CONCAT с NULL-аргументом возвращает NULL, а COUNT(DISTINCT NULL) = 0.
 * Пост-проверка получала count=23 distinct=0 и падала с STOCKS_POSTCOUNT_DUP,
 * хотя все 23 строки уникальны. Держать ключ на nullable-колонке нельзя.
 *
 * IFNULL — защита от NULL в warehouse_code: колонка добавлена ALTER'ом, у строк
 * из снимков до 26.08.2026 она пуста. Пост-проверка смотрит только на текущий
 * snapshot_id, но выражение обязано быть NULL-безопасным само по себе.
 */
export const STOCKS_GRAIN_KEY_SQL =
  "CONCAT(CAST(nm_id AS STRING),'|',CAST(chrt_id AS STRING),'|',IFNULL(warehouse_code,''))";

/** Минимальный контракт BQ-клиента (для инъекции фейка в тестах). */
export interface BqLike {
  query(options: {
    query: string;
    params?: Record<string, unknown>;
    /** Типы параметров — обязательны для NULL-значений (иначе клиент не выведет тип). */
    types?: Record<string, string>;
    location?: string;
  }): Promise<[unknown[]]>;
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
    t5?: ManifestT5Ext,
  ): Promise<void> {
    // Без T5 — прежний контракт байт-в-байт (control_delta=NULL, новых колонок нет).
    const t5Sets = t5
      ? `,
                control_delta=@cdelta, anonymized_warehouse_rows=@anon,
                t5_control_sum=@t5sum, t5_wb_rf_sum=@t5agg, t5_named_sum=@t5named, t5_rows_written=@t5rows,
                t6_comparable=@t6cmp, availability_gap=@agap, availability_gap_prev=@agapprev,
                availability_gap_delta=@agapdelta, availability_ratio=@aratio, availability_status=@astatus`
      : `, control_delta=NULL`;
    const t5Params = t5
      ? {
          cdelta: t5.control.control_delta,
          anon: t5.anonymizedWarehouseRows,
          t5sum: t5.control.t5_control_sum,
          t5agg: t5.control.t5_wb_rf_sum,
          t5named: t5.control.t5_named_sum,
          t5rows: t5.t5RowsWritten,
          t6cmp: t5.control.t6_comparable,
          agap: t5.control.availability_gap,
          agapprev: t5.control.availability_gap_prev,
          agapdelta: t5.control.availability_gap_delta,
          aratio: t5.control.availability_ratio,
          astatus: t5.control.availability_status,
        }
      : {};
    const t5Types: Record<string, string> = t5
      ? {
          cdelta: 'INT64', anon: 'INT64', t5sum: 'INT64', t5agg: 'INT64', t5named: 'INT64', t5rows: 'INT64',
          t6cmp: 'INT64', agap: 'INT64', agapprev: 'INT64', agapdelta: 'INT64', aratio: 'FLOAT64', astatus: 'STRING',
        }
      : {};
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
                control_status=@control${t5Sets},
                error_message=@err
              WHERE snapshot_id=@id AND status='STARTED'`,
      ...(t5 ? { types: t5Types } : {}),
      params: {
        ...t5Params,
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
        // При T5 статус контроля — из самого контроля (единственный источник правды).
        control: t5 ? t5.control.control_status : controlStatus,
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
    // PR-Mig1c: у уже COMPLETE-снимка control_status НЕ затираем — это результат реального
    // контроля T5/T6, а DRO-1 трактует любой статус кроме OK как CONTROL_MISMATCH
    // (sql/health/dro1_02_probe.sql:260). 'REUSED' ставится только снимку, который не
    // успел финализироваться (STARTED): его метрик действительно нет.
    await this.bq.query({
      query: `UPDATE ${this.fqn(table)} SET
                control_status=IF(status='COMPLETE', control_status, 'REUSED'),
                status='COMPLETE', completed_at=CURRENT_TIMESTAMP(),
                written_rows=@written, error_message=''
              WHERE snapshot_id=@id AND status IN ('STARTED', 'COMPLETE')`,
      params: { written: writtenRows, id: snapshotId },
      location: this.location,
    });
  }

  async appendRaw(table: string, rows: object[], snapshotId: string, loadJobId: string): Promise<AppendResult> {
    // Имя файла — по loadJobId: он уникален на (env × период × таблица), поэтому T6 и T5
    // одного снимка не делят временный файл. snapshotId в имени оставлен для трассировки.
    const file = join(tmpdir(), `stocks_${snapshotId}_${loadJobId}.ndjson`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n'));
    try {
      const meta: JobLoadMetadata = {
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        // CREATE_NEVER: таблицами владеет Terraform; runtime-SA НЕ создаёт таблицы.
        // Без этого BigQuery берёт дефолт CREATE_IF_NEEDED и требует dataset-level
        // bigquery.tables.create даже для заранее существующей целевой таблицы.
        createDisposition: 'CREATE_NEVER',
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

  /**
   * PR-Mig1c. availability_gap предыдущего COMPLETE-снимка с непустым gap — база детектора.
   * Читается из ТОЙ ЖЕ manifest-таблицы, куда пишет загрузчик (shadow → __CR): контуры
   * не смешиваются. Текущий снимок в выборку не попадает — он ещё STARTED.
   */
  async prevAvailabilityGap(table: string): Promise<number | null> {
    const [rows] = await this.bq.query({
      query: `SELECT availability_gap FROM ${this.fqn(table)}
              WHERE status='COMPLETE' AND availability_gap IS NOT NULL
              ORDER BY completed_at DESC, snapshot_id DESC LIMIT 1`,
      location: this.location,
    });
    const r = (rows as Array<{ availability_gap: unknown }>)[0];
    if (!r || r.availability_gap === null || r.availability_gap === undefined) return null;
    return Number(r.availability_gap);
  }

  /** PR-Mig1c. Фактическое число строк снимка в таблице (детализация T5). */
  async countRows(table: string, snapshotId: string): Promise<number> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS c FROM ${this.fqn(table)} WHERE snapshot_id=@id`,
      params: { id: snapshotId },
      location: this.location,
    });
    return Number((rows as Array<{ c: unknown }>)[0]?.c ?? 0);
  }

  /** Пост-проверка фактически записанного снимка. */
  async snapshotCounts(table: string, snapshotId: string): Promise<{ count: number; distinct: number }> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS c,
                COUNT(DISTINCT ${STOCKS_GRAIN_KEY_SQL}) AS d
              FROM ${this.fqn(table)} WHERE snapshot_id=@id`,
      params: { id: snapshotId },
      location: this.location,
    });
    const r = (rows as Array<{ c: unknown; d: unknown }>)[0];
    return { count: Number(r?.c ?? 0), distinct: Number(r?.d ?? 0) };
  }
}
