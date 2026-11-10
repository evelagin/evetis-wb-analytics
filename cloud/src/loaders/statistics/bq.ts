/**
 * M2 — BigQuery I/O загрузчиков statistics-api. Таблицами владеет Terraform: runtime их не создаёт
 * (CREATE_NEVER) и пишет только в таблицу, прошедшую assertStatsTarget.
 *
 * Отличие от Apps Script, осознанное:
 *   • watermark читается из ЦЕЛЕВОЙ таблицы, а не из Script Properties: MAX(last_change_date) строк
 *     ИНКРЕМЕНТА (load_id ORD_INC_* / SALE_INC_*). У Apps Script свойство двигает только инкремент,
 *     поэтому оно равно ровно этому максимуму; строки ночной сверки (SALE_RECON_*) и ручного импорта
 *     периода (ORD_API_* / SALE_API_*) watermark не двигают — как у Apps Script. Нет ни одной строки
 *     инкремента → MAX по всем API-строкам, кроме сверки (= wb*IncrementalBootstrap).
 *     Граничные ключи — по ВСЕМ API-строкам на отметке watermark (как wb*BqBoundary*Keys_).
 *     Watermark и ключи — ОДНИМ запросом (минимум тарификации 10 МБ на таблицу).
 *   • запись — ОДНИМ load job на весь пакет (у Apps Script пачки по 2000 строк = несколько job):
 *     пакет ложится целиком или не ложится вовсе, «полузаписи» не бывает.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  ORDERS_INC_LOADID_PREFIX, ORDERS_SOURCE_API, SALES_INC_LOADID_PREFIX, SALES_RECON_LOADID_PREFIX, SALES_SOURCE_API,
} from './constants.js';
import type { RefSkuRow } from './common.js';

export interface StatsBqLike {
  query(options: {
    query: string;
    params?: Record<string, unknown>;
    types?: Record<string, string>;
    location?: string;
  }): Promise<[unknown[]]>;
  dataset(datasetId: string): { table(tableId: string): { load(source: string, metadata: JobLoadMetadata): Promise<unknown> } };
  job(id: string, options?: { location?: string }): { get(): Promise<[{ metadata?: unknown }]> };
}

export interface Boundary {
  /** Watermark в форме ХРАНЕНИЯ ('' — в таблице нет API-строк). */
  watermarkStorage: string;
  /** Ключи границы: заказы — srid|row_hash, продажи — sale_id|md5(raw_json). */
  keys: Set<string>;
}

/** BigQuery job id: только [A-Za-z0-9_-]. */
export function statsLoadJobId(environment: string, table: string, loadId: string, runKey: string): string {
  const clean = (s: string) => s.replace(/[^A-Za-z0-9_-]+/g, '_');
  return `stats_${clean(environment)}_${clean(table.toLowerCase())}_${clean(loadId)}_${clean(runKey)}`.slice(0, 1000);
}

export class StatsBq {
  private readonly bq: StatsBqLike;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    bqClient?: StatsBqLike,
  ) {
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as StatsBqLike);
  }

  private fqn(table: string): string {
    return `\`${this.projectId}.${this.dataset}.${table}\``;
  }

  /**
   * Заказы: watermark = MAX(last_change_date) строк инкремента (= Script Property Apps Script; без строк
   * инкремента — MAX всех API-строк, как wbOrdersBqMaxLastChangeDate_) и пары srid|row_hash всех API-строк
   * ровно на этой границе (wbOrdersBqBoundaryKeys_). Хранение — с пробелом вместо 'T'.
   */
  async ordersBoundary(table: string): Promise<Boundary> {
    const t = this.fqn(table);
    const [rows] = await this.bq.query({
      query: `WITH wm AS (
  SELECT COALESCE(
           MAX(IF(STARTS_WITH(IFNULL(load_id, ''), @inc), last_change_date, NULL)),
           MAX(last_change_date)) AS m
  FROM ${t}
  WHERE source_api = @src AND last_change_date <> ''
)
SELECT DISTINCT wm.m AS watermark, r.srid AS k1, r.row_hash AS k2
FROM wm LEFT JOIN ${t} r ON r.source_api = @src AND r.last_change_date = wm.m`,
      params: { src: ORDERS_SOURCE_API, inc: ORDERS_INC_LOADID_PREFIX },
      location: this.location,
    });
    return toBoundary(rows as Array<{ watermark: unknown; k1: unknown; k2: unknown }>);
  }

  /**
   * Продажи: watermark = MAX(last_change_date) строк инкремента (ночная сверка его НЕ двигает — у Apps Script
   * сверка свойство не трогает); без строк инкремента — MAX API-строк, кроме сверки. Состояния
   * sale_id|md5(raw_json) — по ВСЕМ API-строкам на отметке (wbSalesBqBoundaryStateKeys_). Хранение с 'T'.
   */
  async salesBoundary(table: string): Promise<Boundary> {
    const t = this.fqn(table);
    const [rows] = await this.bq.query({
      query: `WITH wm AS (
  SELECT COALESCE(
           MAX(IF(STARTS_WITH(IFNULL(load_id, ''), @inc), last_change_date, NULL)),
           MAX(IF(STARTS_WITH(IFNULL(load_id, ''), @recon), NULL, last_change_date))) AS m
  FROM ${t} WHERE source_api = @src
)
SELECT DISTINCT wm.m AS watermark, r.sale_id AS k1, TO_HEX(MD5(r.raw_json)) AS k2
FROM wm LEFT JOIN ${t} r
  ON r.source_api = @src AND r.last_change_date = wm.m
 AND r.sale_id IS NOT NULL AND TRIM(r.sale_id) <> '' AND r.raw_json IS NOT NULL`,
      params: { src: SALES_SOURCE_API, inc: SALES_INC_LOADID_PREFIX, recon: SALES_RECON_LOADID_PREFIX },
      location: this.location,
    });
    return toBoundary(rows as Array<{ watermark: unknown; k1: unknown; k2: unknown }>);
  }

  /** Ночная сверка: все состояния sale_id|md5(raw_json) с last_change_date >= fromLcd (wbSalesBqStateKeysSince_). */
  async salesStateKeysSince(table: string, fromLcd: string): Promise<Set<string>> {
    const [rows] = await this.bq.query({
      query: `SELECT DISTINCT sale_id AS k1, TO_HEX(MD5(raw_json)) AS k2 FROM ${this.fqn(table)}
WHERE source_api = @src AND last_change_date >= @from
  AND sale_id IS NOT NULL AND TRIM(sale_id) <> '' AND raw_json IS NOT NULL`,
      params: { src: SALES_SOURCE_API, from: fromLcd },
      location: this.location,
    });
    const set = new Set<string>();
    for (const r of rows as Array<{ k1: unknown; k2: unknown }>) set.add(`${r.k1 == null ? '' : String(r.k1)}|${r.k2 == null ? '' : String(r.k2)}`);
    return set;
  }

  /** Справочник для привязки SKU: REF_SKU_MASTER (BigQuery-копия листа SKU_MASTER, см. Refsync.gs). */
  async loadSkuRows(refTable: string): Promise<RefSkuRow[]> {
    const [rows] = await this.bq.query({
      query: `SELECT nm_id, internal_sku, barcode FROM ${this.fqn(refTable)} WHERE nm_id IS NOT NULL`,
      location: this.location,
    });
    return rows as RefSkuRow[];
  }

  /**
   * Один load job (WRITE_APPEND, CREATE_NEVER, без ignoreUnknownValues: колонка, которой нет в
   * таблице, — ошибка, а не тихая потеря). Потерянный ответ: job ищется по jobId, успешный DONE
   * засчитывается. Число записанных строк сверяется со statistics.load.outputRows.
   */
  async appendRows(table: string, rows: object[], jobId: string): Promise<number> {
    if (rows.length === 0) return 0;
    const file = join(tmpdir(), `${jobId}.ndjson`);
    writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n'));
    const meta: JobLoadMetadata = {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      createDisposition: 'CREATE_NEVER',
      writeDisposition: 'WRITE_APPEND',
      maxBadRecords: 0,
      location: this.location,
      jobId,
    };
    let jobMeta: unknown;
    try {
      const res = await this.bq.dataset(this.dataset).table(table).load(file, meta);
      jobMeta = Array.isArray(res) ? res[0] : res;
    } catch (e) {
      let job: { metadata?: unknown } | undefined;
      try {
        [job] = await this.bq.job(jobId, { location: this.location }).get();
      } catch {
        throw e;
      }
      const st = (job?.metadata as { status?: { state?: string; errorResult?: unknown } } | undefined)?.status;
      if (st?.state !== 'DONE' || st.errorResult) throw e;
      jobMeta = job?.metadata;
    } finally {
      rmSync(file, { force: true });
    }
    const out = (jobMeta as { statistics?: { load?: { outputRows?: unknown } } } | undefined)?.statistics?.load?.outputRows;
    if (out !== undefined && Number(out) !== rows.length) {
      throw new Error(`load job ${jobId}: записано ${String(out)} строк из ${rows.length}`);
    }
    return rows.length;
  }
}

function toBoundary(rows: Array<{ watermark: unknown; k1: unknown; k2: unknown }>): Boundary {
  let wm = '';
  const keys = new Set<string>();
  for (const r of rows) {
    if (r.watermark !== null && r.watermark !== undefined) wm = String(r.watermark);
    if (r.k1 === null || r.k1 === undefined) {
      if (r.k2 === null || r.k2 === undefined) continue;
    }
    keys.add(`${r.k1 == null ? '' : String(r.k1)}|${r.k2 == null ? '' : String(r.k2)}`);
  }
  return { watermarkStorage: wm, keys };
}
