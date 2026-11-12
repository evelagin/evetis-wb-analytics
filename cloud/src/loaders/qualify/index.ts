/**
 * Загрузчик `qualify` — писатель wb_ops.INGEST_QUALIFICATION (этап C, повторное ревью #426).
 * Job ingest-qualify-prod, `args = ["qualify"]`, плоскость управления (без lease и LOADER_RUNS), только prod.
 *
 * Для каждого потока: файл паритета из ветки потока (QUALIFY_SQL_DIR, в образе — /app/parity-sql) →
 * его операторы (только SELECT) → вердикты по дням и группам профиля координатора; плюс группы из журнала
 * shadow-прогонов (ночная сверка, недельный аудит рекламы) и digest образа shadow за сутки. Запись — один
 * INSERT в INGEST_QUALIFICATION (только добавление; последняя строка (сутки, группа) заменяет прежние).
 *
 * env: QUALIFY_STREAMS (по умолчанию statistics,ads,finance,supplies,ref), QUALIFY_SQL_DIR, QUALIFY_DRY_RUN=1
 * (посчитать и записать в лог, без INSERT), QUALIFY_D_FROM_STATISTICS (первые полные сутки shadow M2).
 */
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { LoaderError } from '../../errors.js';
import { BqClient } from '../../bq/client.js';
import type { LoaderContext, LoaderResult } from '../types.js';
import { DEFAULT_OWNERSHIP_DATASET, QUALIFICATION_TABLE, isStream, type Stream } from '../../ownership/streams.js';
import { QUALIFY_SPECS, shadowGroups, splitStatements, type QualifyRow } from './spec.js';

export const MISSING_PARITY_SQL = 'MISSING_PARITY_SQL';
export const QUALIFY_CONFIG = 'QUALIFY_CONFIG';
export const QUALIFY_FAILED = 'QUALIFY_FAILED';
const DEFAULT_STREAMS: Stream[] = ['statistics', 'ads', 'finance', 'supplies', 'ref'];
const READ_ONLY = /^\s*(?:--[^\n]*\n\s*)*(?:WITH|SELECT)\b/i;

export interface QualifyQuery {
  query<T = Record<string, unknown>>(sql: string, params?: Record<string, unknown>, types?: Record<string, string>, options?: { jobTimeoutMs?: number }): Promise<T[]>;
  projectId: string;
}

export interface QualifyRuntime {
  env: Record<string, string | undefined>;
  makeBq: (ctx: LoaderContext) => QualifyQuery;
}

export const defaultQualifyRuntime: QualifyRuntime = {
  env: process.env,
  makeBq: (ctx) => new BqClient(ctx.config.projectId, ctx.config.bqLocation),
};

export function shadowRunsSql(project: string, raw: string, loaders: readonly string[]): string {
  return `SELECT loader_name, DATE(started_at, 'Europe/Moscow') AS d, COUNTIF(status = 'COMPLETE') AS ok, COUNTIF(status = 'ERROR') AS err,
       ARRAY_AGG(IFNULL(image_digest, '') ORDER BY started_at DESC LIMIT 1)[OFFSET(0)] AS digest
FROM \`${project}.${raw}.LOADER_RUNS\`
WHERE environment = 'shadow' AND loader_name IN (${loaders.map((l) => `'${l}'`).join(', ')})
  AND started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 21 DAY)
GROUP BY loader_name, d`;
}

export function insertSql(project: string, ops: string): string {
  return `INSERT INTO \`${project}.${ops}.${QUALIFICATION_TABLE}\` (stream, business_date, check_group, verdict, details, image_digest, verdict_at)
SELECT JSON_VALUE(x, '$.stream'), DATE(JSON_VALUE(x, '$.business_date')), JSON_VALUE(x, '$.check_group'), JSON_VALUE(x, '$.verdict'),
       PARSE_JSON(JSON_QUERY(x, '$.details')), NULLIF(JSON_VALUE(x, '$.image_digest'), ''), CURRENT_TIMESTAMP()
FROM UNNEST(JSON_QUERY_ARRAY(@payload)) AS x`;
}

/** Текст оператора файла паритета с подстановкой первых полных суток shadow (если задано). */
export function prepareStatement(sql: string, dFrom: string | undefined): string {
  if (!READ_ONLY.test(sql)) throw new LoaderError('оператор файла паритета не SELECT/WITH — исполнять нельзя', QUALIFY_CONFIG);
  if (!dFrom) return sql;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dFrom)) throw new LoaderError(`d_from '${dFrom}' — не дата`, QUALIFY_CONFIG);
  return sql.replace(/DATE '\d{4}-\d{2}-\d{2}' AS d_from/, `DATE '${dFrom}' AS d_from`);
}

export function makeQualifyLoader(rt: QualifyRuntime = defaultQualifyRuntime) {
  return async function qualifyLoader(ctx: LoaderContext): Promise<LoaderResult> {
    const { logger, config } = ctx;
    const env = rt.env;
    const streams = (env.QUALIFY_STREAMS ?? '').split(',').map((s) => s.trim()).filter(Boolean);
    for (const s of streams) if (!isStream(s) || !QUALIFY_SPECS[s]) throw new LoaderError(`QUALIFY_STREAMS: поток '${s}' не квалифицируется`, QUALIFY_CONFIG);
    const dir = (env.QUALIFY_SQL_DIR ?? '').trim() || '/app/parity-sql';
    const ops = (env.WRITER_OWNERSHIP_DATASET ?? '').trim() || DEFAULT_OWNERSHIP_DATASET;
    const dry = (env.QUALIFY_DRY_RUN ?? '').trim() === '1';
    const bq = rt.makeBq(ctx);
    const rows: Array<QualifyRow & { imageDigest: string }> = [];
    const failures: string[] = [];
    for (const stream of (streams.length ? streams : DEFAULT_STREAMS) as Stream[]) {
      const spec = QUALIFY_SPECS[stream]!;
      const path = join(dir, spec.file);
      if (!existsSync(path)) {
        logger.warn('qualify_stream_skipped', { stream, reason: MISSING_PARITY_SQL, file: spec.file });
        continue;
      }
      try {
        const stmts = splitStatements(readFileSync(path, 'utf8'));
        const results: Record<number, Record<string, unknown>[]> = {};
        for (const n of spec.queries) {
          const sql = stmts[n - 1];
          if (!sql) throw new LoaderError(`${spec.file}: нет QUERY ${n}`, QUALIFY_CONFIG);
          results[n] = await bq.query(prepareStatement(sql, spec.dFromEnv ? env[spec.dFromEnv]?.trim() : undefined), {}, {}, { jobTimeoutMs: 300_000 });
        }
        const runs = await bq.query(shadowRunsSql(bq.projectId, config.rawDataset, spec.shadowLoaders), {}, {}, { jobTimeoutMs: 120_000 });
        const digest = new Map<string, string>();
        for (const r of runs) {
          const d = String((r.d as { value?: unknown })?.value ?? r.d).slice(0, 10);
          if (String(r.digest ?? '') && !digest.has(d)) digest.set(d, String(r.digest));
        }
        const out = [...spec.map(results), ...shadowGroups(stream, spec, runs)];
        for (const r of out) rows.push({ ...r, imageDigest: digest.get(r.businessDate) ?? '' });
        logger.info('qualify_stream', { stream, rows: out.length, verdicts: out.reduce<Record<string, number>>((a, r) => ({ ...a, [r.verdict]: (a[r.verdict] ?? 0) + 1 }), {}) });
      } catch (e) {
        failures.push(`${stream}: ${e instanceof Error ? e.message : String(e)}`);
        logger.error('qualify_stream_failed', { code: QUALIFY_FAILED, stream, error: e instanceof Error ? e.message : String(e) });
      }
    }
    if (rows.length && !dry) {
      const payload = JSON.stringify(rows.map((r) => ({
        stream: r.stream, business_date: r.businessDate, check_group: r.checkGroup, verdict: r.verdict, details: r.details, image_digest: r.imageDigest,
      })));
      await bq.query(insertSql(bq.projectId, ops), { payload }, { payload: 'STRING' }, { jobTimeoutMs: 120_000 });
    }
    logger.info('qualify_done', { rows: rows.length, written: dry ? 0 : rows.length, dryRun: dry });
    if (failures.length) throw new LoaderError(`qualify: ${failures.join('; ')}`, QUALIFY_FAILED);
    return { rowsFetched: rows.length, rowsLoaded: dry ? 0 : rows.length };
  };
}

export const qualifyLoader = makeQualifyLoader();
