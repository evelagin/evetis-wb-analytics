/**
 * Загрузчик остатков WB → RAW_WB_STOCKS__CR + WB_STOCKS_SNAPSHOTS__CR (shadow).
 * Execution-guard уровня run — в cli (LOADER_RUNS). Здесь — доменный снимок.
 *
 * Идентификаторы (PR-Mig1 fix2):
 *  - dataSnapshotId = f(environment, logical period) — ДЕТЕРМИНИРОВАННЫЙ id набора
 *    данных: пишется в RAW-строки и manifest, по нему идёт post-count. Стабилен
 *    между попытками → идемпотентный повтор (тот же load job) корректно находит
 *    уже загруженные строки.
 *  - attemptId — уникален per-attempt, только для трассировки в логах.
 *  - loadJobId = f(env, period, table) — детерминированный jobId (BQ дедупит).
 *
 * Пишет ТОЛЬКО в переданные таблицы (по умолчанию __CR). prod не трогает.
 *
 * PR-Mig1c (STOCKS_T5_MODE=on): после T6 — T5 warehouse_remains, контроль T5↔T6,
 * детектор доступности и детализация T5 в RAW_WB_STOCKS_T5(__CR). Порядок и семантика —
 * как в боевом wbStocksSnapshotCore_ (WbStocksSnapshot.gs:440-511):
 *   - T5 НЕ блокирует снимок: сбой T5 → control_status/availability_status='T5_UNAVAILABLE';
 *   - детализация T5 — best-effort: её потеря не валит T6 (t5_rows_written=0);
 *   - t6_comparable считается из T6 и сохраняется при любой судьбе T5.
 * STOCKS_T5_MODE=off — поведение до Mig1c (control_status='NOT_RUN'/'REUSED').
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import type { Logger } from '../../logging.js';
import { fetchStocksT6, fetchStocksT5 } from './wbApi.js';
import { validateT6, normalizeT6 } from './normalize.js';
import { normalizeT5, sumT5, evaluateT5Control, t5UnavailableControl } from './t5.js';
import { StocksBq, type ManifestT5Ext } from './bq.js';
import { stocksLoadJobId, stocksDataSnapshotId } from './jobId.js';
import { evaluatePostLoad, planFinalize } from './postLoad.js';
import {
  WB_STOCKS_SOURCE_API,
  WB_STOCKS_T5_SOURCE_API,
  WB_STOCKS_T5_MAX_POLLS,
  WB_STOCKS_T5_HTTP_TIMEOUT_MS,
  WB_STOCKS_T5_RETRY_PAUSE_MS,
  WB_STOCKS_T5_BUDGET_MS,
} from './constants.js';

export async function stocksLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, logicalPeriod } = ctx;
  const snapshotDate = logicalPeriod; // YYYY-MM-DD (Москва) из cli
  const nowIso = new Date().toISOString();

  const dataSnapshotId = stocksDataSnapshotId(config.environment, snapshotDate); // детерминированный
  const attemptId = `STOCK_ATTEMPT_${randomUUID()}`;                             // per-attempt (лог)
  const loadId = `STOCK_LOAD_${dataSnapshotId}`;
  const loadJobId = stocksLoadJobId(config.environment, snapshotDate, config.stocksRawTable);

  const bq = new StocksBq(config.projectId, config.bqLocation, config.rawDataset);
  const token = await new SecretsClient(config.projectId).access(config.wbAnalyticsSecret);

  await bq.manifestStart(config.stocksSnapshotTable, dataSnapshotId, nowIso, snapshotDate, snapshotDate);
  let t5Ext: ManifestT5Ext | undefined;

  try {
    const arr = await fetchStocksT6(
      config.wbAnalyticsHost, token, snapshotDate, snapshotDate,
      { timeoutMs: config.wbHttpTimeoutMs, maxRetries: 3 }, logger,
    );
    const v = validateT6(arr);
    if (!v.ok) throw new LoaderError(v.error ?? 'T6 validation failed', 'WB_T6_VALIDATION');

    const skuByNm = await bq.loadSkuIndex(config.refSkuTable);
    const { rows, metrics } = normalizeT6(arr, {
      snapshotId: dataSnapshotId, snapshotTsIso: nowIso, snapshotDate, loadId,
      sourceApi: WB_STOCKS_SOURCE_API, skuByNm,
    });
    metrics.distinct_keys = v.distinctKeys ?? metrics.distinct_keys;

    // PR-Mig1c: T5 + контроль — ДО записи T6, как в Apps Script; результат идёт в manifest.
    t5Ext = config.stocksT5Mode === 'on'
      ? await runT5Control({
          bq, logger, token, host: config.wbAnalyticsHost, timeoutMs: config.wbHttpTimeoutMs,
          pollIntervalMs: config.stocksT5PollIntervalMs,
          snapshotTable: config.stocksSnapshotTable, t5Table: config.stocksT5Table,
          t5LoadJobId: stocksLoadJobId(config.environment, snapshotDate, config.stocksT5Table),
          t6Comparable: metrics.sum_quantity_physical_t6, anonymizedWarehouseRows: metrics.anonymized_warehouse_rows,
          ctx: { snapshotId: dataSnapshotId, snapshotTsIso: nowIso, snapshotDate, loadId, sourceApi: WB_STOCKS_T5_SOURCE_API, skuByNm },
        })
      : undefined;

    const appendResult = await bq.appendRaw(config.stocksRawTable, rows, dataSnapshotId, loadJobId);
    const cnt = await bq.snapshotCounts(config.stocksRawTable, dataSnapshotId);

    const decision = evaluatePostLoad(appendResult, cnt.count, cnt.distinct, metrics.expected_rows);
    if (!decision.ok) throw new LoaderError(decision.message, decision.code);

    const plan = planFinalize(decision.reused, metrics, cnt.count);
    if (plan.kind === 'markReused') {
      // Повтор: метрики исходной загрузки НЕ трогаем, только статус/written/completed_at.
      await bq.manifestMarkReused(config.stocksSnapshotTable, dataSnapshotId, plan.written);
    } else {
      await bq.manifestFinalize(config.stocksSnapshotTable, dataSnapshotId, 'COMPLETE', plan.metrics, plan.written,
        t5Ext?.control.control_status ?? plan.controlStatus, '', t5Ext);
    }
    logger.info('stocks_complete', {
      dataSnapshotId, attemptId, append: appendResult, reused: decision.reused,
      written: cnt.count, expected: metrics.expected_rows, unique_nm: metrics.unique_nm_ids,
      unmatched: metrics.unmatched_nm_ids.length,
      t5_mode: config.stocksT5Mode, control: t5Ext?.control.control_status ?? plan.kind,
      availability: t5Ext?.control.availability_status, t5_rows: t5Ext?.t5RowsWritten,
    });
    return { rowsFetched: rows.length, rowsLoaded: cnt.count };
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'STOCKS_ERROR');
    try {
      // Как в Apps Script: если T5 уже посчитан, его метрики остаются и в ERROR-строке.
      await bq.manifestFinalize(config.stocksSnapshotTable, dataSnapshotId, 'ERROR', null, 0,
        t5Ext?.control.control_status ?? 'NOT_RUN', err.message, t5Ext);
    } catch (e2) {
      logger.error('stocks_manifest_error_finalize_failed', { attemptId, message: e2 instanceof Error ? e2.message : String(e2) });
    }
    throw err;
  }
}

interface T5RunInput {
  bq: StocksBq;
  logger: Logger;
  token: string;
  host: string;
  timeoutMs: number;
  pollIntervalMs: number;
  snapshotTable: string;
  t5Table: string;
  t5LoadJobId: string;
  t6Comparable: number;
  anonymizedWarehouseRows: number;
  ctx: Parameters<typeof normalizeT5>[1];
}

/**
 * T5 → контроль → детализация (WbStocksSnapshot.gs:443-511). Сбой запроса T5 деградирует в
 * T5_UNAVAILABLE, сбой записи детализации — в t5_rows_written=0; снимок T6 идёт дальше.
 * Как и в Apps Script, НЕ защищён разбор самого отчёта: битый элемент (null) в ответе download
 * роняет снимок целиком — это сбой источника, а не повод писать неполные суммы.
 */
async function runT5Control(i: T5RunInput): Promise<ManifestT5Ext> {
  const t5 = await fetchStocksT5(
    i.host, i.token,
    {
      timeoutMs: Math.min(i.timeoutMs, WB_STOCKS_T5_HTTP_TIMEOUT_MS), maxRetries: 3,
      backoffBaseMs: WB_STOCKS_T5_RETRY_PAUSE_MS, backoffCapMs: WB_STOCKS_T5_RETRY_PAUSE_MS,
    },
    { intervalMs: i.pollIntervalMs, maxPolls: WB_STOCKS_T5_MAX_POLLS, budgetMs: WB_STOCKS_T5_BUDGET_MS }, i.logger,
  );
  if (!t5.ok) {
    i.logger.warn('stocks_t5_unavailable', { error: t5.error });
    return { control: t5UnavailableControl(i.t6Comparable), anonymizedWarehouseRows: i.anonymizedWarehouseRows, t5RowsWritten: 0 };
  }

  let prevGap: number | null = null;
  try {
    prevGap = await i.bq.prevAvailabilityGap(i.snapshotTable);
  } catch (e) {
    i.logger.error('stocks_availability_prev_failed', { message: e instanceof Error ? e.message : String(e) });
  }
  const control = evaluateT5Control(sumT5(t5.data), i.t6Comparable, prevGap);

  let t5RowsWritten = 0;
  try {
    const rows = normalizeT5(t5.data, i.ctx);
    if (rows.length > 0) {
      const appended = await i.bq.appendRaw(i.t5Table, rows, i.ctx.snapshotId, i.t5LoadJobId);
      t5RowsWritten = await i.bq.countRows(i.t5Table, i.ctx.snapshotId);
      if (appended === 'ALREADY_LOADED') {
        // Повтор в те же сутки: детализация осталась от прошлой попытки (load job дня уже успешен),
        // а контроль посчитан по свежему T5. Видно в логе; строки T5 при этом не дублируются.
        i.logger.warn('stocks_t5_detail_reused', { snapshotId: i.ctx.snapshotId, stored: t5RowsWritten, fetched: rows.length });
      }
    }
  } catch (e) {
    i.logger.error('stocks_t5_detail_failed', { message: e instanceof Error ? e.message : String(e) });
  }
  return { control, anonymizedWarehouseRows: i.anonymizedWarehouseRows, t5RowsWritten };
}
