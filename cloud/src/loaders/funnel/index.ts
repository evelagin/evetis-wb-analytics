/**
 * Загрузчик воронки продаж WB (UNITKA 2.0 R2, контракт R7). READ-ONLY.
 *
 * Зачем он вообще: «Переходы» и «Положили в корзину» в юнитке годами были ручными,
 * потому что в BigQuery их нет. Клики по рекламе — НЕ переходы в карточку
 * (01.08.2026: в книге переходов 103, кликов по рекламе в BQ 52), поэтому подменять
 * одно другим нельзя. Источник — воронка карточки целиком (openCount / cartCount).
 *
 * Каденс — сутки, окно с перекрытием: WB досчитывает часть событий до нескольких
 * дней, поэтому забираем D-LOOKBACK..D-1, а не только вчерашний день. Глубина
 * ограничена WB неделей (см. wbApi.ts) — окно длиннее обрезается.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { fetchHistory, chunk, HISTORY_PATH, MAX_DEPTH_DAYS, MAX_NM_PER_REQUEST, type FunnelWindow } from './wbApi.js';
import { normalizeFunnelRows, type HistoryItem } from './normalize.js';
import { FunnelBq } from './bq.js';

/** WB: 3 запроса в минуту на аналитику — не чаще раза в 20 с. */
const REQUEST_INTERVAL_MS = 21_000;
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export function funnelWindow(targetDate: string, lookbackDays: number): FunnelWindow {
  const end = targetDate;
  const d = new Date(`${targetDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - Math.max(0, lookbackDays - 1));
  return { startDate: d.toISOString().slice(0, 10), endDate: end };
}

/** Глубина окна: не меньше суток и не глубже, чем отдаёт WB. */
export function effectiveLookback(configured: number): number {
  return Math.min(Math.max(1, configured), MAX_DEPTH_DAYS);
}

export function funnelObservationId(environment: string, w: FunnelWindow): string {
  return `WBFN_${environment}_${w.startDate.replace(/-/g, '')}_${w.endDate.replace(/-/g, '')}`;
}
export function funnelLoadJobId(environment: string, w: FunnelWindow): string {
  return `wbfunnel_${environment}_${w.startDate.replace(/-/g, '')}_${w.endDate.replace(/-/g, '')}`;
}

function daysBetween(w: FunnelWindow): number {
  const a = Date.parse(`${w.startDate}T00:00:00Z`);
  const b = Date.parse(`${w.endDate}T00:00:00Z`);
  return Math.round((b - a) / 86_400_000) + 1;
}

export async function funnelLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, targetDate, runId } = ctx;
  const lookback = effectiveLookback(config.funnelLookbackDays);
  if (lookback !== config.funnelLookbackDays) {
    logger.warn('wb_funnel_lookback_clamped', { configured: config.funnelLookbackDays, effective: lookback });
  }
  const w = funnelWindow(targetDate, lookback);
  const observationId = funnelObservationId(config.environment, w);
  const startedAtIso = new Date().toISOString();
  const http = { timeoutMs: config.wbHttpTimeoutMs, maxRetries: 3, backoffBaseMs: REQUEST_INTERVAL_MS, backoffCapMs: 63_000 };

  const bq = new FunnelBq(config.projectId, config.bqLocation, config.rawDataset);
  await bq.observationStart(config.funnelObservationsTable, {
    observationId, startDate: w.startDate, endDate: w.endDate,
    environment: config.environment, runId, startedAtIso,
  });

  let requests = 0;
  let httpStatus: number | null = null;
  try {
    const nmIds = await bq.loadNmIds(config.refSkuTable);
    if (nmIds.length === 0) throw new LoaderError('В справочнике SKU нет nmID WB', 'WB_FUNNEL_NO_NM');
    const token = await new SecretsClient(config.projectId).access(config.wbAnalyticsSecret);

    const items: HistoryItem[] = [];
    for (const batch of chunk(nmIds, MAX_NM_PER_REQUEST)) {
      if (requests > 0) await sleep(REQUEST_INTERVAL_MS);
      requests++;
      const res = await fetchHistory(config.wbAnalyticsHost, token, w, batch, http, logger);
      httpStatus = res.httpStatus;
      items.push(...res.items);
    }

    const observedAtIso = new Date().toISOString();
    const { rows, unknownFields, rejected } = normalizeFunnelRows(items, {
      observationId, environment: config.environment, runId, observedAtIso,
      sourceEndpoint: `POST ${HISTORY_PATH}`,
    });
    if (rows.length === 0) {
      // Пустой ответ — отказ, а не «трафика не было»: у активного каталога
      // дневные строки воронки есть даже в нулевой день.
      throw new LoaderError(`Ответ воронки пуст (карточек в ответе: ${items.length}, отбраковано: ${rejected})`, 'WB_FUNNEL_EMPTY');
    }

    const outcome = await bq.appendRaw(config.funnelRawTable, rows, funnelLoadJobId(config.environment, w));
    const written = await bq.countObservationRows(config.funnelRawTable, observationId);
    if (written === 0) throw new LoaderError('После append нет строк воронки', 'WB_FUNNEL_POSTCOUNT_EMPTY');

    const daysCovered = new Set(rows.map((r) => r.date_msk)).size;
    const daysExpected = daysBetween(w);
    const nmCovered = new Set(rows.map((r) => r.nm_id)).size;

    await bq.observationFinalize(config.funnelObservationsTable, {
      observationId, status: outcome === 'REUSED' ? 'REUSED' : 'COMPLETE', observedAtIso,
      rowsFetched: rows.length + rejected, rowsWritten: written, rowsRejected: rejected,
      daysCovered, daysExpected, nmCovered,
      // poll_attempts — колонка манифеста из R2; у синхронного метода это число запросов к WB.
      httpStatus, pollAttempts: requests,
      schemaStatus: unknownFields.length > 0 ? 'DRIFT_UNKNOWN_FIELDS' : 'OK',
      schemaUnknownFields: unknownFields.length > 0 ? unknownFields.join(',') : null,
      errorCode: null, errorMessage: null,
    });

    logger.info('wb_funnel_complete', {
      observationId, window: w, outcome, requests, nmRequested: nmIds.length, rowsWritten: written,
      rejected, daysCovered, daysExpected, nmCovered, unknownFields,
    });
    return { rowsFetched: rows.length + rejected, rowsLoaded: written };
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'WB_FUNNEL_ERROR');
    try {
      await bq.observationFinalize(config.funnelObservationsTable, {
        observationId, status: 'ERROR', observedAtIso: null,
        rowsFetched: null, rowsWritten: null, rowsRejected: null,
        daysCovered: null, daysExpected: daysBetween(w), nmCovered: null,
        httpStatus, pollAttempts: requests, schemaStatus: null, schemaUnknownFields: null,
        errorCode: err.code, errorMessage: err.message.slice(0, 900),
      });
    } catch (e2) {
      logger.error('wb_funnel_manifest_finalize_failed', { error: e2 instanceof Error ? e2.message : String(e2) });
    }
    throw err;
  }
}
