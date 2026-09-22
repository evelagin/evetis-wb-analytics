/**
 * BQ I/O наблюдателя акций WB (PR-PROMO-1).
 *
 * Append-only: строки добавляются load-джобой с ДЕТЕРМИНИРОВАННЫМ jobId.
 * BigQuery дедуплицирует load по jobId, поэтому повтор того же слота наблюдения
 * не создаёт вторую копию строк, а новый слот (другой jobId) записывается всегда —
 * даже если состав акций не изменился. MERGE и UPDATE здесь отсутствуют
 * намеренно: схлопывание наблюдений уничтожило бы предмет наблюдения.
 *
 * Это та же механика, что у наблюдателя цен (PR-1), и тот же контракт
 * идемпотентности — см. cloud/src/loaders/prices/bq.ts.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export function isAlreadyExists(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 409 || /already exists/i.test(msg);
}

/** Минимальный контракт BQ-клиента — для инъекции фейка в тестах. */
export interface BqLike {
  query(options: {
    query: string;
    params?: Record<string, unknown>;
    types?: Record<string, string>;
    location?: string;
  }): Promise<[unknown[]]>;
  dataset(datasetId: string): {
    table(tableId: string): { load(source: string, metadata: JobLoadMetadata): Promise<unknown> };
  };
}

export type AppendOutcome = 'LOADED' | 'REUSED';

export interface ObservationStart {
  observationId: string;
  bucket: string;
  environment: string;
  runId: string;
  startedAtIso: string;
  windowFromIso: string;
  windowToIso: string;
}

export interface ObservationFinalize {
  observationId: string;
  status: 'COMPLETE' | 'ERROR' | 'REUSED';
  observedAtIso: string | null;
  httpStatus: number | null;
  httpAttempts: number | null;
  promotionsListed: number | null;
  promotionsDetailed: number | null;
  detailsEmpty: number | null;
  autoPromotions: number | null;
  regularPromotions: number | null;
  rangingRows: number | null;
  nomenclatureRows: number | null;
  nomenclatureAttempts: number | null;
  capabilityGapCount: number | null;
  unmappedNmIds: number | null;
  mappingCoveragePct: number | null;
  rowsWritten: number | null;
  schemaStatus: string | null;
  schemaUnknownFields: string | null;
  errorCode: string | null;
  errorMessage: string | null;
}

export class PromoBq {
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

  /** Справочник nm_id → internal_sku. Нужен ТОЛЬКО для состава акций (regular). */
  async loadSkuByNm(refTable: string): Promise<Map<number, string>> {
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

  /**
   * Append строк снимка. REUSED, если этот же снимок уже был загружен.
   *
   * Тип строки намеренно параметризован, а не сведён к Record<string, unknown>:
   * три таблицы наблюдателя имеют три разные схемы, и общий индексный тип
   * позволил бы передать сюда что угодно.
   */
  async appendRows<T extends object>(table: string, rows: T[], jobId: string): Promise<AppendOutcome> {
    if (rows.length === 0) return 'LOADED';
    const file = join(tmpdir(), `wb_promo_${jobId}.jsonl`);
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
      try {
        rmSync(file, { force: true });
      } catch {
        /* временный файл не критичен */
      }
    }
  }

  /** Число строк снимка в таблице — постпроверка записи. */
  async countObservationRows(table: string, observationId: string): Promise<number> {
    const [rows] = await this.bq.query({
      query: `SELECT COUNT(*) AS n FROM ${this.fqn(table)} WHERE observation_id = @oid`,
      params: { oid: observationId },
      types: { oid: 'STRING' },
      location: this.location,
    });
    const r = (rows as Array<{ n?: unknown }>)[0];
    return Number(r?.n ?? 0);
  }

  /**
   * Открытие строки манифеста. Идемпотентно по observation_id.
   *
   * Безусловный INSERT дал бы вторую строку на тот же снимок при повторе слота —
   * грейн манифеста (observation_id) нарушался бы, а проверка «дублей грейна нет»
   * падала бы на собственной телеметрии. Дефект пойман валидацией 2026-09-22:
   * три прогона одного слота оставили три строки на один observation_id.
   */
  async observationStart(table: string, s: ObservationStart): Promise<void> {
    await this.bq.query({
      query: `INSERT INTO ${this.fqn(table)}
                (observation_id, observation_bucket, environment, run_id, started_at, status,
                 window_from, window_to)
              SELECT @oid, @bucket, @env, @run, TIMESTAMP(@started), 'STARTED',
                     TIMESTAMP(@wfrom), TIMESTAMP(@wto)
              FROM UNNEST([1])
              WHERE NOT EXISTS (
                SELECT 1 FROM ${this.fqn(table)} WHERE observation_id = @oid)`,
      params: {
        oid: s.observationId,
        bucket: s.bucket,
        env: s.environment,
        run: s.runId,
        started: s.startedAtIso,
        wfrom: s.windowFromIso,
        wto: s.windowToIso,
      },
      types: {
        oid: 'STRING',
        bucket: 'STRING',
        env: 'STRING',
        run: 'STRING',
        started: 'STRING',
        wfrom: 'STRING',
        wto: 'STRING',
      },
      location: this.location,
    });
  }

  /**
   * Финализация снимка. UPDATE по строке манифеста — это не правка истории
   * наблюдений: манифест описывает ПРОГОН, а не состояние маркетплейса.
   */
  async observationFinalize(table: string, f: ObservationFinalize): Promise<void> {
    await this.bq.query({
      query: `UPDATE ${this.fqn(table)} SET
                status = @status,
                observed_at = IF(@observed IS NULL, NULL, TIMESTAMP(@observed)),
                completed_at = CURRENT_TIMESTAMP(),
                http_status = @httpStatus,
                http_attempts = @httpAttempts,
                promotions_listed = @promotionsListed,
                promotions_detailed = @promotionsDetailed,
                details_empty = @detailsEmpty,
                auto_promotions = @autoPromotions,
                regular_promotions = @regularPromotions,
                ranging_rows = @rangingRows,
                nomenclature_rows = @nomenclatureRows,
                nomenclature_attempts = @nomenclatureAttempts,
                capability_gap_count = @capabilityGapCount,
                unmapped_nm_ids = @unmappedNmIds,
                mapping_coverage_pct = @mappingCoveragePct,
                rows_written = @rowsWritten,
                schema_status = @schemaStatus,
                schema_unknown_fields = @schemaUnknownFields,
                error_code = @errorCode,
                error_message = @errorMessage
              WHERE observation_id = @oid`,
      params: {
        oid: f.observationId,
        status: f.status,
        observed: f.observedAtIso,
        httpStatus: f.httpStatus,
        httpAttempts: f.httpAttempts,
        promotionsListed: f.promotionsListed,
        promotionsDetailed: f.promotionsDetailed,
        detailsEmpty: f.detailsEmpty,
        autoPromotions: f.autoPromotions,
        regularPromotions: f.regularPromotions,
        rangingRows: f.rangingRows,
        nomenclatureRows: f.nomenclatureRows,
        nomenclatureAttempts: f.nomenclatureAttempts,
        capabilityGapCount: f.capabilityGapCount,
        unmappedNmIds: f.unmappedNmIds,
        mappingCoveragePct: f.mappingCoveragePct,
        rowsWritten: f.rowsWritten,
        schemaStatus: f.schemaStatus,
        schemaUnknownFields: f.schemaUnknownFields,
        errorCode: f.errorCode,
        errorMessage: f.errorMessage,
      },
      types: {
        oid: 'STRING',
        status: 'STRING',
        observed: 'STRING',
        httpStatus: 'INT64',
        httpAttempts: 'INT64',
        promotionsListed: 'INT64',
        promotionsDetailed: 'INT64',
        detailsEmpty: 'INT64',
        autoPromotions: 'INT64',
        regularPromotions: 'INT64',
        rangingRows: 'INT64',
        nomenclatureRows: 'INT64',
        nomenclatureAttempts: 'INT64',
        capabilityGapCount: 'INT64',
        unmappedNmIds: 'INT64',
        mappingCoveragePct: 'NUMERIC',
        rowsWritten: 'INT64',
        schemaStatus: 'STRING',
        schemaUnknownFields: 'STRING',
        errorCode: 'STRING',
        errorMessage: 'STRING',
      },
      location: this.location,
    });
  }
}
