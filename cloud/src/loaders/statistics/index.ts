/**
 * M2 — загрузчики statistics-api WB: `orders`, `sales`, `sales-reconcile`.
 * Перенос runWbOrdersIncremental / runWbSalesIncremental / runWbSalesNightReconcile (Apps Script)
 * с той же бизнес-семантикой. Журнал прогона (LOADER_RUNS) пишет cli.ts: handler вернул результат →
 * COMPLETE (= OK / OK_NO_CHANGES / OK_NO_GAPS у Apps Script), бросил → ERROR с кодом
 * ORDERS_PARTIAL / ORDERS_ERROR / SALES_ERROR / SALES_RECONCILE_ERROR (whitelist INGEST_SUCCESS_STATUSES_).
 *
 * Порядок внутри прогона — fail-closed:
 *   1) контур записи (targets.ts) — до секрета, WB API и BigQuery;
 *   2) токен (http/wbToken.ts): секрет читается и срок проверяется ДО BigQuery и WB API —
 *      истёкший → WB_STATS_TOKEN_EXPIRED без единого запроса, скоро истекает → warn
 *      WB_TOKEN_EXPIRING_SOON; ответ WB 401/403 → WB_STATS_AUTH (детерминированный, без повтора);
 *   3) watermark и граничные ключи из целевой таблицы (один запрос);
 *   4) WB API; любая неполнота/невалидность → ERROR, ничего не пишем;
 *   5) справочник SKU — только если есть что писать;
 *   6) один load job; watermark «двигается» самим фактом записи (MAX таблицы).
 * Повтор прогона идемпотентен: следующий запуск читает watermark из таблицы, граничные строки
 * сверяет по ключам, поэтому записанное не дублируется (Apps Script при сбое setProperty дописывал
 * строки повторно — см. runbook, «Отличия»).
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import type { Config } from '../../config.js';
import { LoaderError } from '../../errors.js';
import type { Logger } from '../../logging.js';
import { SecretsClient } from '../../secrets.js';
import { assertWbTokenUsable, wbAuthError } from '../../http/wbToken.js';
import { assertStatsTarget } from './targets.js';
import { StatsBq, statsLoadJobId } from './bq.js';
import { defaultTransport, fetchOrders, fetchSales, type StatsTransport, type WbAuthFailure } from './wbApi.js';
import { buildSkuIndex, isValidWatermark, md5Hex, mskDate, mskDateTime, mskStamp, type SkuIndex } from './common.js';
import {
  ORDERS_INC_LOADID_PREFIX, SALES_INC_LOADID_PREFIX, SALES_RECON_LOADID_PREFIX, WB_ORDERS_API_PATH, WB_SALES_API_PATH,
  WB_STATS_CODE_PREFIX,
} from './constants.js';
import { normalizeOrdersRows, ordersBatchSums, ordersBqRow, precheckOrders, selectOrdersToAppend, type OrdersPlan } from './orders.js';
import {
  normalizeSalesRows, precheckSales, reconcileFromLcd, salesBqRow, selectReconcileGaps, selectSalesToAppend, validateSalesRaw,
  type SalesPlan,
} from './sales.js';

export const ORDERS_PARTIAL = 'ORDERS_PARTIAL';
export const ORDERS_ERROR = 'ORDERS_ERROR';
export const SALES_ERROR = 'SALES_ERROR';
export const SALES_RECONCILE_ERROR = 'SALES_RECONCILE_ERROR';

/** Инъектируемые зависимости: реальные в проде, поддельные в тестах. */
export interface StatsDeps {
  bq: Pick<StatsBq, 'ordersBoundary' | 'salesBoundary' | 'salesStateKeysSince' | 'loadSkuRows' | 'appendRows'>;
  getToken: () => Promise<string>;
  transport: StatsTransport;
  now: () => Date;
}

export function makeStatsDeps(config: Config): StatsDeps {
  return {
    bq: new StatsBq(config.projectId, config.bqLocation, config.rawDataset),
    getToken: async () => (await new SecretsClient(config.projectId).access(config.wbStatisticsSecret)).trim(),
    transport: defaultTransport,
    now: () => new Date(),
  };
}

const EMPTY_SKU: SkuIndex = { byNm: new Map(), byBarcode: new Map() };
const STORAGE_WM = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$/;

/**
 * Watermark из таблицы. Пустая таблица: в shadow — стартовое значение из env (если валидно),
 * иначе ERROR, как wb*IncrementalBootstrap без строк в RAW.
 */
function resolveWatermark(
  stream: 'orders' | 'sales', storage: string, config: Config, code: string,
): { watermark: string; bootstrapped: boolean } {
  if (storage) {
    const api = stream === 'orders' ? storage.replace(' ', 'T') : storage;
    if (stream === 'orders' && !STORAGE_WM.test(storage)) {
      throw new LoaderError(`Неожиданный формат last_change_date в RAW: ${storage}`, code);
    }
    if (!isValidWatermark(api)) throw new LoaderError(`MAX(last_change_date) в RAW невалиден как watermark: ${storage}`, code);
    return { watermark: api, bootstrapped: false };
  }
  const boot = stream === 'orders' ? config.ordersBootstrapWatermark : config.salesBootstrapWatermark;
  const envName = stream === 'orders' ? 'ORDERS_BOOTSTRAP_WATERMARK' : 'SALES_BOOTSTRAP_WATERMARK';
  if (config.environment === 'shadow' && boot) {
    if (!isValidWatermark(boot)) throw new LoaderError(`${envName} невалиден: ${boot}`, code);
    return { watermark: boot, bootstrapped: true };
  }
  throw new LoaderError(`В целевой таблице нет last_change_date API-строк — watermark отсутствует (${envName} допустим только в shadow).`, code);
}

const runKey = (runId: string): string => md5Hex(runId).slice(0, 12);

/** Секрет → токен, годный для WB: срок проверяется до первого запроса (значение токена не логируется). */
async function usableToken(deps: StatsDeps, config: Config, logger: Logger): Promise<string> {
  const token = await deps.getToken();
  assertWbTokenUsable(token, config.wbStatisticsSecret, { codePrefix: WB_STATS_CODE_PREFIX, logger, nowMs: deps.now().getTime() });
  return token;
}

/** WB отклонил токен (401/403) → WB_STATS_AUTH: не ORDERS_PARTIAL/SALES_ERROR, повтор не лечит. */
function throwIfAuthRejected(fetched: { auth?: WbAuthFailure }, config: Config, path: string): void {
  if (fetched.auth) throw wbAuthError(WB_STATS_CODE_PREFIX, config.wbStatisticsSecret, fetched.auth.status, `GET ${path}`, fetched.auth.body);
}

// ─────────────────────────────────────────────────────────────────────────
// orders
// ─────────────────────────────────────────────────────────────────────────

export async function runOrders(ctx: LoaderContext, deps: StatsDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const table = assertStatsTarget(config, 'orders');
  const token = await usableToken(deps, config, logger);
  const started = deps.now();
  const loadId = `${ORDERS_INC_LOADID_PREFIX}${mskStamp(started)}`;
  const loadedAt = mskDateTime(started);

  const boundary = await deps.bq.ordersBoundary(table);
  const { watermark, bootstrapped } = resolveWatermark('orders', boundary.watermarkStorage, config, ORDERS_ERROR);
  const fetched = await fetchOrders(config.wbStatisticsHost, WB_ORDERS_API_PATH, token, watermark, config.wbHttpTimeoutMs, deps.transport, logger);
  throwIfAuthRejected(fetched, config, WB_ORDERS_API_PATH);

  let plan: OrdersPlan | null = precheckOrders({
    watermark, fetchedOk: fetched.ok, fetchedPartial: fetched.partial, fetchError: fetched.error,
    data: fetched.data, apiRowsReceived: fetched.apiRowsReceived, cursorEnd: fetched.cursorEnd,
  });
  let written = 0;
  let sums = { uniqueSrid: 0, cancelCount: 0 };
  if (!plan) {
    // Сначала отбор без справочника (row_hash от SKU не зависит), справочник — только если есть что писать.
    plan = selectOrdersToAppend(normalizeOrdersRows(fetched.data, loadId, loadedAt, EMPTY_SKU), watermark, fetched.cursorEnd, boundary.keys);
    if (plan.status === 'OK') {
      const sku = buildSkuIndex(await deps.bq.loadSkuRows(config.refSkuTable));
      plan = selectOrdersToAppend(normalizeOrdersRows(fetched.data, loadId, loadedAt, sku), watermark, fetched.cursorEnd, boundary.keys);
      sums = ordersBatchSums(plan.toAppend);
      if (!config.statsDryRun) {
        written = await deps.bq.appendRows(table, plan.toAppend.map(ordersBqRow), statsLoadJobId(config.environment, table, loadId, runKey(ctx.runId)));
      }
    }
  }

  const log = {
    status: plan.status, load_id: loadId, table, bootstrapped,
    watermark_before: plan.watermarkBefore, watermark_candidate: plan.watermarkCandidate,
    watermark_after: plan.status === 'OK' && !config.statsDryRun ? plan.watermarkAfter : plan.watermarkBefore,
    pages_fetched: fetched.pages, api_rows_received: fetched.apiRowsReceived,
    rows_appended: written, planned_rows: plan.toAppend.length, unique_srid: sums.uniqueSrid, cancel_count: sums.cancelCount,
    dry_run: config.statsDryRun, ...(plan.error ? { error: plan.error } : {}),
  };
  if (plan.status === 'OK' || plan.status === 'OK_NO_CHANGES') {
    logger.info('orders_increment', log);
    return { rowsFetched: fetched.apiRowsReceived, rowsLoaded: written };
  }
  logger.warn('orders_increment', log);
  throw new LoaderError(plan.error, plan.status === 'PARTIAL' ? ORDERS_PARTIAL : ORDERS_ERROR);
}

// ─────────────────────────────────────────────────────────────────────────
// sales
// ─────────────────────────────────────────────────────────────────────────

export async function runSales(ctx: LoaderContext, deps: StatsDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const table = assertStatsTarget(config, 'sales');
  const token = await usableToken(deps, config, logger);
  const started = deps.now();
  const loadId = `${SALES_INC_LOADID_PREFIX}${mskStamp(started)}`;
  const loadedAt = mskDateTime(started);

  const boundary = await deps.bq.salesBoundary(table);
  const { watermark, bootstrapped } = resolveWatermark('sales', boundary.watermarkStorage, config, SALES_ERROR);
  const fetched = await fetchSales(config.wbStatisticsHost, WB_SALES_API_PATH, token, watermark, config.wbHttpTimeoutMs, deps.transport, logger);
  throwIfAuthRejected(fetched, config, WB_SALES_API_PATH);

  const pre = precheckSales(watermark, fetched);
  let plan: SalesPlan;
  let written = 0;
  if ('plan' in pre) {
    plan = pre.plan;
  } else {
    plan = selectSalesToAppend(normalizeSalesRows(fetched.data, loadId, loadedAt, EMPTY_SKU), fetched.data.length, watermark, pre.candidate, boundary.keys);
    if (plan.status === 'OK') {
      const sku = buildSkuIndex(await deps.bq.loadSkuRows(config.refSkuTable));
      plan = selectSalesToAppend(normalizeSalesRows(fetched.data, loadId, loadedAt, sku), fetched.data.length, watermark, pre.candidate, boundary.keys);
      if (!config.statsDryRun) {
        written = await deps.bq.appendRows(table, plan.toAppend.map(salesBqRow), statsLoadJobId(config.environment, table, loadId, runKey(ctx.runId)));
      }
    }
  }

  const log = {
    status: plan.status, load_id: loadId, table, bootstrapped,
    watermark_before: plan.watermarkBefore, watermark_candidate: plan.watermarkCandidate,
    watermark_after: plan.status === 'OK' && !config.statsDryRun ? plan.watermarkAfter : plan.watermarkBefore,
    api_rows_received: fetched.apiRowsReceived, rows_after_boundary_dedup: plan.rowsAfterBoundaryDedup,
    rows_written: written, unique_sale_id: plan.toAppend.length, dry_run: config.statsDryRun,
    ...(plan.error ? { error: plan.error } : {}),
  };
  if (plan.status === 'OK' || plan.status === 'OK_NO_CHANGES') {
    logger.info('sales_increment', log);
    return { rowsFetched: fetched.apiRowsReceived, rowsLoaded: written };
  }
  logger.warn('sales_increment', log);
  throw new LoaderError(plan.error, SALES_ERROR);
}

// ─────────────────────────────────────────────────────────────────────────
// sales-reconcile
// ─────────────────────────────────────────────────────────────────────────

export async function runSalesReconcile(ctx: LoaderContext, deps: StatsDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const table = assertStatsTarget(config, 'sales');
  const started = deps.now();
  const loadId = `${SALES_RECON_LOADID_PREFIX}${mskStamp(started)}`;
  const loadedAt = mskDateTime(started);
  const days = config.salesReconcileDays;
  const fromLcd = reconcileFromLcd(days, started.getTime(), mskDate);
  if (!isValidWatermark(fromLcd)) throw new LoaderError(`Невалидный fromLcd окна: ${fromLcd}`, SALES_RECONCILE_ERROR);

  const token = await usableToken(deps, config, logger);
  const fetched = await fetchSales(config.wbStatisticsHost, WB_SALES_API_PATH, token, fromLcd, config.wbHttpTimeoutMs, deps.transport, logger);
  throwIfAuthRejected(fetched, config, WB_SALES_API_PATH);
  const base = { load_id: loadId, table, days, period_from: fromLcd, period_to: loadedAt, api_rows_received: fetched.apiRowsReceived };
  const fail = (error: string): never => {
    logger.warn('sales_reconcile', { ...base, status: 'ERROR', error });
    throw new LoaderError(error, SALES_RECONCILE_ERROR);
  };
  if (!fetched.ok) fail(fetched.error || 'Незавершённая выгрузка Sales API');
  if (fetched.data.length === 0 && fetched.apiRowsReceived === 0) {
    logger.info('sales_reconcile', { ...base, status: 'OK_NO_GAPS', rows_after_boundary_dedup: 0, gaps_filled: 0, rows_written: 0 });
    return { rowsFetched: 0, rowsLoaded: 0 };
  }
  const v = validateSalesRaw(fetched.data);
  if (v.badRow !== null) fail(`Невалидная сырая API-строка #${v.badRow} (saleID/date/lastChangeDate) — пакет отклонён до записи.`);

  const existing = await deps.bq.salesStateKeysSince(table, fromLcd);
  let plan = selectReconcileGaps(normalizeSalesRows(fetched.data, loadId, loadedAt, EMPTY_SKU), fetched.data.length, existing);
  let written = 0;
  if (plan.status === 'OK') {
    const sku = buildSkuIndex(await deps.bq.loadSkuRows(config.refSkuTable));
    plan = selectReconcileGaps(normalizeSalesRows(fetched.data, loadId, loadedAt, sku), fetched.data.length, existing);
    if (!config.statsDryRun) {
      written = await deps.bq.appendRows(table, plan.toAppend.map(salesBqRow), statsLoadJobId(config.environment, table, loadId, runKey(ctx.runId)));
    }
  }
  if (plan.status === 'ERROR') fail(plan.error);
  logger.info('sales_reconcile', {
    ...base, status: plan.status, rows_after_boundary_dedup: plan.statesInBatch, gaps_filled: plan.gapsFilled,
    rows_written: written, dry_run: config.statsDryRun,
  });
  return { rowsFetched: fetched.apiRowsReceived, rowsLoaded: written };
}

export const ordersLoader = (ctx: LoaderContext): Promise<LoaderResult> => runOrders(ctx, makeStatsDeps(ctx.config));
export const salesLoader = (ctx: LoaderContext): Promise<LoaderResult> => runSales(ctx, makeStatsDeps(ctx.config));
export const salesReconcileLoader = (ctx: LoaderContext): Promise<LoaderResult> => runSalesReconcile(ctx, makeStatsDeps(ctx.config));
