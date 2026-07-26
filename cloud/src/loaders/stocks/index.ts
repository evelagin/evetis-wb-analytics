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
 * T5-контроль в shadow не выполняется (control_status='NOT_RUN'/'REUSED').
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { fetchStocksT6 } from './wbApi.js';
import { validateT6, normalizeT6 } from './normalize.js';
import { StocksBq } from './bq.js';
import { stocksLoadJobId, stocksDataSnapshotId } from './jobId.js';
import { evaluatePostLoad } from './postLoad.js';
import { WB_STOCKS_SOURCE_API } from './constants.js';

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

    const appendResult = await bq.appendRaw(config.stocksRawTable, rows, dataSnapshotId, loadJobId);
    const cnt = await bq.snapshotCounts(config.stocksRawTable, dataSnapshotId);

    const decision = evaluatePostLoad(appendResult, cnt.count, cnt.distinct, metrics.expected_rows);
    if (!decision.ok) throw new LoaderError(decision.message, decision.code);

    const controlStatus = decision.reused ? 'REUSED' : 'NOT_RUN';
    await bq.manifestFinalize(config.stocksSnapshotTable, dataSnapshotId, 'COMPLETE', metrics, cnt.count, controlStatus, '');
    logger.info('stocks_complete', {
      dataSnapshotId, attemptId, append: appendResult, reused: decision.reused,
      written: cnt.count, expected: metrics.expected_rows, unique_nm: metrics.unique_nm_ids,
      unmatched: metrics.unmatched_nm_ids.length,
    });
    return { rowsFetched: rows.length, rowsLoaded: cnt.count };
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'STOCKS_ERROR');
    try {
      await bq.manifestFinalize(config.stocksSnapshotTable, dataSnapshotId, 'ERROR', null, 0, 'NOT_RUN', err.message);
    } catch (e2) {
      logger.error('stocks_manifest_error_finalize_failed', { attemptId, message: e2 instanceof Error ? e2.message : String(e2) });
    }
    throw err;
  }
}
