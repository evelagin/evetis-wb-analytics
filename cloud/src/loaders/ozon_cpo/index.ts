/**
 * OZON CPO (Phase B, OWNER ACK 08.10.2026) — загрузчик «Оплаты за заказ» по заказам.
 *
 * Отдельный Cloud Run Job `ozon-cpo-orders-prod` в образе wb-loader: своя точка входа, свой SA,
 * свой журнал (`ozon_raw.OZON_CPO_ORDER_RUNS`), своя аренда (`ozon_raw.LOADER_RUNS`). Образ
 * Ozon runtime (24e3c6d6) не меняется и не пересобирается.
 *
 * Авторитет (FIN CONTRACT, Phase B):
 *   итог CPO          = биллинг кампаний `RAW_OZON_ADS_EXPENSE_DAILY` (сверка — residual-вью);
 *   атрибуция заказов = ЭТОТ отчёт (заказанный SKU — финансово, продвигаемый — маркетингово);
 *   финансы type 54   = независимая сверка с задержкой проводки.
 *
 * Прогон: окно (DAILY: 45 закрытых суток списания; BACKFILL: явные границы) → блоки ≤ месяца →
 * на блок: оба отчёта → разбор fail-closed → атомарная замена блока с журналом в той же транзакции.
 * Итог прогона: COMPLETE — все блоки легли; PARTIAL — часть блоков легла, затем отказ (уже легшие
 * блоки верны и самодостаточны, повтор идемпотентен); ERROR — не легло ничего. PARTIAL и ERROR
 * завершают Job ошибкой: алерт и LOADER_RUNS видят отказ.
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { classifyFailure } from '../../failure.js';
import { cpoWindow, mskDayBoundsUtc, type CpoWindow } from './window.js';
import { parseCpoReport, centsToDecimal, type CpoFamily } from './parse.js';
import { normalizeCpoReport, duplicateKeys, type CpoRawRow } from './normalize.js';
import { OzonPerfClient } from './perfApi.js';
import { CpoBq, CPO_FAMILIES, type CpoRunJournal } from './bq.js';

export interface CpoEnv {
  clientIdSecret: string;
  clientSecretSecret: string;
  backfillFrom?: string;
  backfillTo?: string;
  lookbackDays?: number;
  acceptRemovals: boolean;
}

export function cpoEnv(env: NodeJS.ProcessEnv): CpoEnv {
  const name = (k: string, d: string): string => {
    const v = (env[k] ?? d).trim();
    if (!/^[A-Za-z0-9_-]{1,255}$/.test(v)) throw new LoaderError(`${k}: недопустимое имя секрета`, 'CPO_CONFIG');
    return v;
  };
  const lb = (env.OZON_CPO_LOOKBACK_DAYS ?? '').trim();
  return {
    clientIdSecret: name('OZON_PERF_CLIENT_ID_SECRET', 'EVETIS_OZON_PERFORMANCE_CLIENT_ID'),
    clientSecretSecret: name('OZON_PERF_CLIENT_SECRET_SECRET', 'EVETIS_OZON_PERFORMANCE_CLIENT_SECRET'),
    backfillFrom: (env.OZON_CPO_BACKFILL_FROM ?? '').trim() || undefined,
    backfillTo: (env.OZON_CPO_BACKFILL_TO ?? '').trim() || undefined,
    lookbackDays: lb ? Number(lb) : undefined,
    acceptRemovals: (env.OZON_CPO_ACCEPT_REMOVALS ?? '0') === '1',
  };
}

export interface CpoDeps {
  perf: (ctx: LoaderContext, e: CpoEnv) => Pick<OzonPerfClient, 'fetchReport'>;
  bq: (ctx: LoaderContext) => Pick<CpoBq, 'replaceChunk' | 'journalRun'>;
  now: () => Date;
}

export const defaultCpoDeps: CpoDeps = {
  perf: (ctx, e) => {
    const secrets = new SecretsClient(ctx.config.projectId);
    return new OzonPerfClient(async () => ({
      clientId: (await secrets.access(e.clientIdSecret)).trim(),
      clientSecret: (await secrets.access(e.clientSecretSecret)).trim(),
    }), { opts: { timeoutMs: 120_000, maxRetries: 3 } }, ctx.logger);
  },
  bq: (ctx) => new CpoBq(ctx.config.projectId, ctx.config.bqLocation, ctx.config.rawDataset),
  now: () => new Date(),
};

export async function ozonCpoOrdersLoader(ctx: LoaderContext, deps: CpoDeps = defaultCpoDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  if (config.environment !== 'prod') {
    throw new LoaderError('ozon-cpo-orders пишет production RAW и запускается только в prod', 'CPO_PROD_ONLY');
  }
  // Изоляция маркетплейсов: RAW, журнал и аренда — только в ozon_raw.
  if (config.rawDataset !== 'ozon_raw') {
    throw new LoaderError(`BQ_RAW_DATASET=${config.rawDataset}: загрузчик Ozon пишет только в ozon_raw`, 'CPO_CONFIG');
  }
  const e = cpoEnv(process.env);
  let w: CpoWindow;
  try {
    w = cpoWindow({ now: deps.now(), backfillFrom: e.backfillFrom, backfillTo: e.backfillTo, lookbackDays: e.lookbackDays });
  } catch (err) {
    throw new LoaderError(err instanceof Error ? err.message : String(err), 'CPO_WINDOW');
  }
  const runId = `ozon_cpo_${randomUUID()}`;
  const startedAt = new Date().toISOString();
  const perf = deps.perf(ctx, e);
  const bq = deps.bq(ctx);
  logger.info('ozon_cpo_start', { runId, mode: w.mode, from: w.from, to: w.to, chunks: w.chunks.length });

  const meta = { imageDigest: config.imageDigest, gitSha: config.gitSha, executionId: config.executionId };
  const tot = { rowsAllSku: 0, rowsSearch: 0, centsAllSku: 0, centsSearch: 0 };
  let loaded = 0, fetched = 0;
  const finish = async (status: CpoRunJournal['status'], err?: unknown): Promise<void> => {
    const c = err === undefined ? null : classifyFailure(err);
    try {
      await bq.journalRun({ runId, mode: w.mode, status, windowFrom: w.from, windowTo: w.to, chunksTotal: w.chunks.length,
        chunksLoaded: loaded, rowsAllSku: tot.rowsAllSku, rowsSearch: tot.rowsSearch,
        expenseAllSku: centsToDecimal(tot.centsAllSku), expenseSearch: centsToDecimal(tot.centsSearch),
        errorCode: c?.code ?? null, errorMessage: err === undefined ? null : (err instanceof Error ? err.message : String(err)).slice(0, 1000),
        ...meta, startedAt });
    } catch (je) {
      logger.warn('ozon_cpo_journal_failed', { status, error: je instanceof Error ? je.message : String(je) });
    }
  };

  for (const chunk of w.chunks) {
    try {
      const { fromUtc, toUtc } = mskDayBoundsUtc(chunk.from, chunk.to);
      const rows: CpoRawRow[] = [];
      const per: Record<CpoFamily, { rows: number; cents: number; uuid: string }> =
        { ALL_SKU_PROMO: { rows: 0, cents: 0, uuid: '' }, SEARCH_PROMO: { rows: 0, cents: 0, uuid: '' } };
      // Отчёты — строго по одному: Performance API не любит параллельных заказов отчётов.
      for (const family of CPO_FAMILIES) {
        const r = await perf.fetchReport(family, fromUtc, toUtc);
        const rep = parseCpoReport(family, r.body, chunk);
        const norm = normalizeCpoReport(rep, { uuid: r.uuid, fromUtc, toUtc, fetchedAt: new Date().toISOString(), runId });
        rows.push(...norm);
        per[family] = { rows: norm.length, cents: rep.expenseCents, uuid: r.uuid };
      }
      const dup = duplicateKeys(rows);
      if (dup.length) throw new LoaderError(`${chunk.from}..${chunk.to}: ${dup.length} дублей естественного ключа`, 'CPO_DUPLICATE_KEY');
      fetched += rows.length;
      await bq.replaceChunk(rows, chunk, `ozon_cpo_${runId.slice(-36).replace(/-/g, '')}_${chunk.from.replace(/-/g, '')}`, {
        runId, mode: w.mode, windowFrom: w.from, windowTo: w.to,
        rowsAllSku: per.ALL_SKU_PROMO.rows, rowsSearch: per.SEARCH_PROMO.rows,
        expenseAllSku: centsToDecimal(per.ALL_SKU_PROMO.cents), expenseSearch: centsToDecimal(per.SEARCH_PROMO.cents),
        reportUuids: `${per.ALL_SKU_PROMO.uuid},${per.SEARCH_PROMO.uuid}`, ...meta, startedAt,
      }, e.acceptRemovals);
      loaded++;
      tot.rowsAllSku += per.ALL_SKU_PROMO.rows; tot.rowsSearch += per.SEARCH_PROMO.rows;
      tot.centsAllSku += per.ALL_SKU_PROMO.cents; tot.centsSearch += per.SEARCH_PROMO.cents;
      logger.info('ozon_cpo_chunk_loaded', { runId, from: chunk.from, to: chunk.to,
        allSkuRows: per.ALL_SKU_PROMO.rows, searchRows: per.SEARCH_PROMO.rows,
        allSkuRub: centsToDecimal(per.ALL_SKU_PROMO.cents), searchRub: centsToDecimal(per.SEARCH_PROMO.cents) });
    } catch (err) {
      const status = loaded > 0 ? 'PARTIAL' : 'ERROR';
      await finish(status, err);
      logger.warn('ozon_cpo_run_stopped', { runId, status, chunk: `${chunk.from}..${chunk.to}`, chunksLoaded: loaded, chunksTotal: w.chunks.length });
      throw err;
    }
  }
  await finish('COMPLETE');
  logger.info('ozon_cpo_complete', { runId, mode: w.mode, from: w.from, to: w.to, chunks: loaded,
    allSkuRows: tot.rowsAllSku, searchRows: tot.rowsSearch,
    allSkuRub: centsToDecimal(tot.centsAllSku), searchRub: centsToDecimal(tot.centsSearch) });
  return { rowsFetched: fetched, rowsLoaded: tot.rowsAllSku + tot.rowsSearch };
}
