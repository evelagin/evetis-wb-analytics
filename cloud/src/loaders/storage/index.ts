/**
 * UNITKA E4 — production loader фактического платного хранения WB.
 *
 * Инварианты:
 *  - только закрытые сутки: logicalPeriod = D-1 МСК из registry;
 *  - overlap 1..8 суток, по умолчанию 8;
 *  - никаких оценок stock × rate: источник только WB Paid Storage API;
 *  - неполный/пустой отчёт не заменяет уже лежащие факты;
 *  - окно перед записью заменяется целиком, поэтому overlap не удваивает хранение.
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { fetchPaidStorage } from './wbApi.js';
import { normalizePaidStorage } from './normalize.js';
import { StorageBq } from './bq.js';
import { storageWindow } from './window.js';

export async function storageLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, logicalPeriod } = ctx;
  if (config.environment !== 'prod') {
    throw new LoaderError('Paid storage loader writes production RAW and is prod-only', 'STORAGE_PROD_ONLY');
  }

  const w = storageWindow(logicalPeriod, config.storageLookbackDays);
  const runId = `storage_${randomUUID()}`;
  const observationId = `WBPS_${w.startDate.replace(/-/g, '')}_${w.endDate.replace(/-/g, '')}`;
  const observedAtIso = new Date().toISOString();
  const bq = new StorageBq(config.projectId, config.bqLocation, config.rawDataset);
  const token = await new SecretsClient(config.projectId).access(config.wbAnalyticsSecret);

  logger.info('storage_start', { runId, observationId, startDate: w.startDate, endDate: w.endDate, lookbackDays: config.storageLookbackDays });

  const fetched = await fetchPaidStorage(
    config.wbAnalyticsHost,
    token,
    w.startDate,
    w.endDate,
    { timeoutMs: config.wbHttpTimeoutMs, maxRetries: 3 },
    logger,
  );
  const normalized = normalizePaidStorage(fetched.rows, {
    observationId, runId, observedAtIso, startDate: w.startDate, endDate: w.endDate,
  });

  // Fail closed before DELETE: every requested calendar day must be present.
  if (normalized.days.length !== config.storageLookbackDays) {
    throw new LoaderError(
      `Paid storage coverage incomplete: expected ${config.storageLookbackDays} days ${w.startDate}..${w.endDate}, got ${normalized.days.length}: ${normalized.days.join(',')}`,
      'WB_STORAGE_INCOMPLETE_COVERAGE',
    );
  }
  if (normalized.rows.length === 0) {
    throw new LoaderError('Paid storage normalized to zero rows', 'WB_STORAGE_ZERO_ROWS');
  }

  const loadJobId = `wb_paid_storage_${logicalPeriod.replace(/-/g, '')}_${runId.replace(/-/g, '_')}`;
  const rowsLoaded = await bq.replaceWindow(config.storageRawTable, normalized.rows, w.startDate, w.endDate, loadJobId);
  const qa = await bq.qaWindow(config.storageRawTable, w.startDate, w.endDate);

  if (rowsLoaded !== normalized.rows.length || qa.rows !== normalized.rows.length || qa.days !== config.storageLookbackDays) {
    throw new LoaderError(
      `Paid storage post-load QA failed: normalized=${normalized.rows.length}, loaded=${rowsLoaded}, qa_rows=${qa.rows}, qa_days=${qa.days}`,
      'WB_STORAGE_POSTLOAD_QA',
    );
  }
  const amountDelta = Math.abs(qa.storageRub - normalized.storageRub);
  if (amountDelta > 0.02) {
    throw new LoaderError(
      `Paid storage amount mismatch after load: source=${normalized.storageRub.toFixed(2)}, bq=${qa.storageRub.toFixed(2)}, delta=${amountDelta.toFixed(2)}`,
      'WB_STORAGE_AMOUNT_QA',
    );
  }

  logger.info('storage_complete', {
    runId,
    taskId: fetched.taskId,
    startDate: w.startDate,
    endDate: w.endDate,
    rowsFetched: fetched.rows.length,
    rowsLoaded,
    rejected: normalized.rejected,
    days: qa.days,
    nm: qa.nm,
    storageRub: qa.storageRub,
    pollAttempts: fetched.pollAttempts,
  });
  return { rowsFetched: fetched.rows.length, rowsLoaded };
}
