/**
 * BigQuery I/O рекламного загрузчика.
 *   • ЗАПИСЬ — только append load-job'ом в целевые RAW-таблицы (как bqLoadRows_ в Apps Script:
 *     NDJSON, WRITE_APPEND). Таблицы не создаются (CREATE_NEVER: ими владеют Apps Script/Terraform),
 *     схема не расширяется, вью не создаются и не меняются. DML нет вообще.
 *   • jobId детерминирован на (run_id × таблица × порядковый номер загрузки в прогоне): повтор того же
 *     load-вызова клиентом BigQuery не удвоит строки — BigQuery вернёт 409 на существующий jobId.
 *   • ЧТЕНИЕ — три SELECT, ровно те же, что делает Apps Script: свежесть fullstats, scope Ads-2 за сутки,
 *     телеметрия расхода по балансу за окно Ads-2.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { toBqRow, type RawRow } from './normalize.js';

export interface BqLike {
  query(o: { query: string; params?: Record<string, unknown>; types?: Record<string, string>; location?: string }): Promise<[unknown[]]>;
  dataset(d: string): { table(t: string): { load(src: string, md: JobLoadMetadata): Promise<unknown> } };
  job(id: string, options?: { location?: string }): { get(): Promise<[{ metadata?: unknown }]> };
}

/** Контракт записи, который видят источники (в тестах — память). */
export interface AdsSink {
  /** Пишет строки; возвращает число записанных. 0 строк — без обращения к BigQuery. */
  append(table: string, columns: readonly string[], rows: RawRow[]): Promise<number>;
}

/** Контракт чтения (в тестах — фейк). */
export interface AdsReader {
  /** MAX(date) по строкам 'raw' fullstats; '' — строк нет. */
  maxStatsDate(statsTable: string): Promise<string>;
  /** Пары advert×nm за сутки из scope-вью + сумма расхода атрибуции. */
  scopePairsForDay(view: string, day: string): Promise<{ pairs: Array<{ advertId: number; nmId: number }>; spendRub: number }>;
  /** Расход по балансу по суткам окна (телеметрия Ads-2). */
  costsByDay(view: string, from: string, to: string): Promise<Record<string, number>>;
  /**
   * Статус ПОСЛЕДНЕЙ попытки 'ads' за logical_period в своём контуре (LOADER_RUNS, LATEST-ATTEMPT как гейт
   * витрины, только попытки после полуночи МСК D). null — попыток нет.
   */
  adsAttempt(manifestTable: string, environment: string, logicalPeriod: string): Promise<string | null>;
}

export function isAlreadyExists(e: unknown): boolean {
  const code = (e as { code?: number }).code;
  const msg = e instanceof Error ? e.message : String(e);
  return code === 409 || /already exists/i.test(msg);
}

/** Детерминированный jobId load-задачи: только [A-Za-z0-9_-], ≤ 1024. */
export function adsLoadJobId(environment: string, runId: string, table: string, seq: number): string {
  const clean = (s: string): string => s.replace(/[^A-Za-z0-9_-]+/g, '_');
  return `ads_${clean(environment)}_${clean(runId)}_${clean(table).toLowerCase()}_${seq}`.slice(0, 1024);
}

export class AdsBq implements AdsSink, AdsReader {
  private readonly bq: BqLike;
  private seq = 0;

  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    private readonly environment: string,
    private readonly runId: string,
    bqClient?: BqLike,
  ) {
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as BqLike);
  }

  private fqn(t: string): string { return `\`${this.projectId}.${this.dataset}.${t}\``; }

  async append(table: string, columns: readonly string[], rows: RawRow[]): Promise<number> {
    if (!rows.length) return 0;
    const lines = rows.map((r) => JSON.stringify(toBqRow(r, columns)));
    const jobId = adsLoadJobId(this.environment, this.runId, table, ++this.seq);
    const file = join(tmpdir(), `${jobId}.ndjson`);
    writeFileSync(file, lines.join('\n') + '\n', 'utf8');
    const md: JobLoadMetadata = {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      writeDisposition: 'WRITE_APPEND',
      createDisposition: 'CREATE_NEVER',
      schemaUpdateOptions: [],
      ignoreUnknownValues: false,
      maxBadRecords: 0,
      jobId,
      location: this.location,
    };
    try {
      await this.bq.dataset(this.dataset).table(table).load(file, md);
      return rows.length;
    } catch (e) {
      if (isAlreadyExists(e)) {
        const [job] = await this.bq.job(jobId, { location: this.location }).get();
        const st = (job.metadata as { status?: { state?: string; errorResult?: { message?: string } } } | undefined)?.status;
        if (st?.state === 'DONE' && !st.errorResult) return rows.length; // тот же load уже лёг
        throw new Error(`load job ${jobId} уже существует и не успешен: ${st?.errorResult?.message ?? st?.state ?? 'unknown'}`);
      }
      throw e;
    } finally {
      try { rmSync(file, { force: true }); } catch { /* временный файл */ }
    }
  }

  /**
   * Эквивалент wbAdsDailyFreshness_ (SELECT MAX(date) FROM V_ADV_CAMPAIGN_STATS): вью — это дедуп
   * строк processed_status='raw', а дедуп не меняет максимум. Поэтому MAX по целевой RAW с тем же
   * фильтром даёт то же значение — и в shadow смотрит на то, что записал именно этот контур.
   */
  async maxStatsDate(statsTable: string): Promise<string> {
    const [rows] = await this.bq.query({
      query: `SELECT MAX(\`date\`) AS d FROM ${this.fqn(statsTable)} WHERE processed_status = 'raw'`,
      location: this.location,
    });
    const v = (rows as Array<{ d: unknown }>)[0]?.d;
    return v == null ? '' : String(v).substring(0, 10);
  }

  /** wbAdsQsPairsForDay_: тот же SQL, дата — параметром, а не текстом. */
  async scopePairsForDay(view: string, day: string): Promise<{ pairs: Array<{ advertId: number; nmId: number }>; spendRub: number }> {
    const [rows] = await this.bq.query({
      query: `SELECT advertId, nmId, SUM(SAFE_CAST(\`sum\` AS FLOAT64)) AS spend
              FROM ${this.fqn(view)}
              WHERE DATE(\`date\`) = DATE(@day)
                AND advertId IS NOT NULL AND nmId IS NOT NULL
              GROUP BY advertId, nmId`,
      params: { day },
      types: { day: 'STRING' },
      location: this.location,
    });
    const pairs: Array<{ advertId: number; nmId: number }> = [];
    let spendRub = 0;
    for (const r of rows as Array<{ advertId: unknown; nmId: unknown; spend: unknown }>) {
      const advertId = Number(r.advertId), nmId = Number(r.nmId);
      if (!advertId || !nmId) continue;
      pairs.push({ advertId, nmId });
      spendRub += Number(r.spend) || 0;
    }
    return { pairs, spendRub };
  }

  async adsAttempt(manifestTable: string, environment: string, logicalPeriod: string): Promise<string | null> {
    const [rows] = await this.bq.query({
      query: `SELECT status FROM ${this.fqn(manifestTable)}
              WHERE environment = @env AND loader_name = 'ads' AND logical_period = @lp
                AND started_at >= TIMESTAMP(DATE_ADD(CAST(@lp AS DATE), INTERVAL 1 DAY), 'Europe/Moscow')
              ORDER BY started_at DESC, run_id DESC LIMIT 1`,
      params: { env: environment, lp: logicalPeriod },
      types: { env: 'STRING', lp: 'STRING' },
      location: this.location,
    });
    const v = (rows as Array<{ status: unknown }>)[0]?.status;
    return v == null ? null : String(v);
  }

  /** wbAdsQsCostsByDay_: одним запросом на всё окно. */
  async costsByDay(view: string, from: string, to: string): Promise<Record<string, number>> {
    const [rows] = await this.bq.query({
      query: `SELECT FORMAT_DATE('%Y-%m-%d', DATE(updDate)) AS d, SUM(SAFE_CAST(updSum AS FLOAT64)) AS cost
              FROM ${this.fqn(view)}
              WHERE DATE(updDate) BETWEEN DATE(@from) AND DATE(@to)
              GROUP BY d`,
      params: { from, to },
      types: { from: 'STRING', to: 'STRING' },
      location: this.location,
    });
    const out: Record<string, number> = {};
    for (const r of rows as Array<{ d: unknown; cost: unknown }>) {
      out[String(r.d)] = Math.round((Number(r.cost) || 0) * 100) / 100;
    }
    return out;
  }
}
