/**
 * Загрузчик остатков WB → RAW_WB_STOCKS__CR + WB_STOCKS_SNAPSHOTS__CR (shadow).
 * Execution-guard уровня run — в cli (LOADER_RUNS). Здесь — доменный снимок:
 * manifest STARTED → T6 fetch → валидация → SKU из REF → нормализация →
 * идемпотентный append → пост-COUNT → manifest COMPLETE/ERROR.
 * Пишет ТОЛЬКО в переданные таблицы (по умолчанию __CR). prod не трогает.
 * T5-контроль в shadow-пилоте НЕ выполняется (control_status='NOT_RUN') — отдельно.
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { fetchStocksT6 } from './wbApi.js';
import { validateT6, normalizeT6 } from './normalize.js';
import { StocksBq } from './bq.js';
import { stocksLoadJobId } from './jobId.js';
import { WB_STOCKS_SOURCE_API } from './constants.js';

export async function stocksLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, logicalPeriod } = ctx;
  const snapshotDate = logicalPeriod; // YYYY-MM-DD (Москва) из cli
  const nowIso = new Date().toISOString();
  const snapshotId = `STOCK_SNAP_${snapshotDate.replace(/-/g, '')}_${randomUUID().slice(0, 8)}`;
  const loadId = `STOCK_LOAD_${snapshotId}`;
  const loadJobId = stocksLoadJobId(config.environment, snapshotDate, config.stocksRawTable);
  const bq = new StocksBq(config.projectId, config.bqLocation, config.rawDataset);

  const token = await new SecretsClient(config.projectId).access(config.wbAnalyticsSecret);
  await bq.manifestStart(config.stocksSnapshotTable, snapshotId, nowIso, snapshotDate, snapshotDate);

  try {
    const arr = await fetchStocksT6(
      config.wbAnalyticsHost,
      token,
      snapshotDate,
      snapshotDate,
      { timeoutMs: config.wbHttpTimeoutMs, maxRetries: 3 },
      logger,
    );

    const v = validateT6(arr);
    if (!v.ok) throw new LoaderError(v.error ?? 'T6 validation failed', 'WB_T6_VALIDATION');

    const skuByNm = await bq.loadSkuIndex(config.refSkuTable);
    const { rows, metrics } = normalizeT6(arr, {
      snapshotId, snapshotTsIso: nowIso, snapshotDate, loadId, sourceApi: WB_STOCKS_SOURCE_API, skuByNm,
    });
    metrics.distinct_keys = v.distinctKeys ?? metrics.distinct_keys;

    await bq.appendRaw(config.stocksRawTable, rows, snapshotId, loadJobId);

    const cnt = await bq.snapshotCounts(config.stocksRawTable, snapshotId);
    if (cnt.count !== metrics.expected_rows || cnt.distinct !== metrics.expected_rows) {
      throw new LoaderError(
        `Пост-проверка не сошлась: expected=${metrics.expected_rows} written=${cnt.count} distinct=${cnt.distinct}`,
        'STOCKS_POSTCOUNT',
      );
    }

    await bq.manifestFinalize(config.stocksSnapshotTable, snapshotId, 'COMPLETE', metrics, cnt.count, 'NOT_RUN', '');
    logger.info('stocks_complete', {
      snapshotId, written: cnt.count, unique_nm: metrics.unique_nm_ids,
      unmatched: metrics.unmatched_nm_ids.length, aggregate_rows: metrics.aggregate_warehouse_rows,
    });
    return { rowsFetched: rows.length, rowsLoaded: cnt.count };
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'STOCKS_ERROR');
    try {
      await bq.manifestFinalize(config.stocksSnapshotTable, snapshotId, 'ERROR', null, 0, 'NOT_RUN', err.message);
    } catch (e2) {
      logger.error('stocks_manifest_error_finalize_failed', { message: e2 instanceof Error ? e2.message : String(e2) });
    }
    throw err;
  }
}
