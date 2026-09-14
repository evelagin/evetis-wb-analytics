/**
 * UNITKA E4 — production loader фактического платного хранения WB.
 *
 * Инварианты:
 *  - только закрытые сутки: logicalPeriod = D-1 МСК из registry;
 *  - overlap 1..8 суток, по умолчанию 8;
 *  - никаких оценок stock × rate: источник только WB Paid Storage API;
 *  - неполный/пустой отчёт не заменяет уже лежащие факты;
 *  - окно перед записью заменяется целиком, поэтому overlap не удваивает хранение;
 *  - весь блокирующий QA — внутри транзакции до COMMIT: провал откатывает окно целиком.
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { fetchPaidStorage } from './wbApi.js';
import { normalizePaidStorage } from './normalize.js';
import { StorageBq } from './bq.js';
import { storageWindow } from './window.js';

function storageEnv(): { lookbackDays: number; rawTable: string } {
  const lookbackRaw = (process.env.STORAGE_LOOKBACK_DAYS ?? '8').trim();
  const lookbackDays = Number(lookbackRaw);
  if (!Number.isInteger(lookbackDays) || lookbackDays < 1 || lookbackDays > 8) {
    throw new LoaderError(`STORAGE_LOOKBACK_DAYS must be an integer 1..8, got '${lookbackRaw}'`, 'STORAGE_BAD_LOOKBACK');
  }
  const rawTable = (process.env.STORAGE_RAW_TABLE ?? 'RAW_WB_PAID_STORAGE').trim();
  if (!/^[A-Za-z0-9_]+$/.test(rawTable)) {
    throw new LoaderError(`Invalid STORAGE_RAW_TABLE '${rawTable}'`, 'STORAGE_BAD_TABLE');
  }
  return { lookbackDays, rawTable };
}

export async function storageLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, logicalPeriod } = ctx;
  if (config.environment !== 'prod') {
    throw new LoaderError('Paid storage loader writes production RAW and is prod-only', 'STORAGE_PROD_ONLY');
  }

  const storage = storageEnv();
  const w = storageWindow(logicalPeriod, storage.lookbackDays);
  const runId = `storage_${randomUUID()}`;
  const observationId = `WBPS_${w.startDate.replace(/-/g, '')}_${w.endDate.replace(/-/g, '')}`;
  const observedAtIso = new Date().toISOString();
  const bq = new StorageBq(config.projectId, config.bqLocation, config.rawDataset);
  const token = await new SecretsClient(config.projectId).access(config.wbAnalyticsSecret);

  logger.info('storage_start', { runId, observationId, startDate: w.startDate, endDate: w.endDate, lookbackDays: storage.lookbackDays });

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

  // Fail closed before replacement: every requested calendar day must be present.
  if (normalized.days.length !== storage.lookbackDays) {
    throw new LoaderError(
      `Paid storage coverage incomplete: expected ${storage.lookbackDays} days ${w.startDate}..${w.endDate}, got ${normalized.days.length}: ${normalized.days.join(',')}`,
      'WB_STORAGE_INCOMPLETE_COVERAGE',
    );
  }
  if (normalized.rows.length === 0) {
    throw new LoaderError('Paid storage normalized to zero rows', 'WB_STORAGE_ZERO_ROWS');
  }

  const loadJobId = `wb_paid_storage_${logicalPeriod.replace(/-/g, '')}_${runId.replace(/-/g, '_')}`;

  // Блокирующий post-load QA (строки / дни / сумма) выполняется ВНУТРИ транзакции замены,
  // до COMMIT — см. StorageBq.replaceWindow. Провал любой проверки откатывает окно, и target
  // остаётся в прежнем состоянии; отдельного «QA после коммита» здесь больше нет.
  const rowsLoaded = await bq.replaceWindow(storage.rawTable, normalized.rows, w.startDate, w.endDate, loadJobId, {
    rows: normalized.rows.length,
    days: storage.lookbackDays,
    storageRub: normalized.storageRub,
  });
  if (rowsLoaded !== normalized.rows.length) {
    throw new LoaderError(
      `Paid storage post-commit count mismatch: normalized=${normalized.rows.length}, loaded=${rowsLoaded}`,
      'WB_STORAGE_POSTLOAD_QA',
    );
  }

  // Пост-коммит телеметрия. Запись уже зафиксирована и провалидирована транзакционно, поэтому
  // сбой самого замера не имеет права превратить успешный прогон в ERROR.
  let qa: { rows: number; days: number; nm: number; storageRub: number } | null = null;
  try {
    qa = await bq.qaWindow(storage.rawTable, w.startDate, w.endDate);
  } catch (e) {
    logger.warn('storage_qa_telemetry_failed', { message: e instanceof Error ? e.message : String(e) });
  }

  logger.info('storage_complete', {
    runId,
    taskId: fetched.taskId,
    startDate: w.startDate,
    endDate: w.endDate,
    rowsFetched: fetched.rows.length,
    rowsLoaded,
    rejected: normalized.rejected,
    days: qa?.days ?? null,
    nm: qa?.nm ?? null,
    storageRub: qa?.storageRub ?? null,
    pollAttempts: fetched.pollAttempts,
  });
  return { rowsFetched: fetched.rows.length, rowsLoaded };
}
