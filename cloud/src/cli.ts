/**
 * Точка входа Cloud Run Job (PR-Mig0): node dist/cli.js <loader>.
 * Оркестрация: config → атомарный acquire (execution-guard) → loader → finalize.
 * loader выполняется ТОЛЬКО при acquired=true. DRY_RUN=1 прогоняет каркас без BQ.
 */
import { loadConfig } from './config.js';
import { Logger, parseLevel } from './logging.js';
import { EXIT_OK, EXIT_ERROR, LoaderError } from './errors.js';
import { resolveLoader } from './loaders/registry.js';
import { dailyPeriodMoscow } from './period.js';
import { BqClient } from './bq/client.js';
import { BqManifestStore, DEFAULT_STALE_STARTED_MS } from './bq/runManifest.js';
import type { ManifestKey, AcquireParams } from './bq/runManifest.js';

async function main(): Promise<number> {
  const loaderName = (process.argv[2] ?? process.env.LOADER_NAME ?? '').trim();
  const config = loadConfig({ ...process.env, LOADER_NAME: loaderName || process.env.LOADER_NAME });
  const logger = new Logger(
    {
      loader: loaderName,
      environment: config.environment,
      imageDigest: config.imageDigest,
      gitSha: config.gitSha,
    },
    parseLevel(config.logLevel),
  );

  const handler = resolveLoader(loaderName);
  if (!handler) {
    logger.error('unknown_loader', { available: 'noop' });
    return EXIT_ERROR;
  }

  const logicalPeriod = dailyPeriodMoscow();
  const key: ManifestKey = { environment: config.environment, loaderName, logicalPeriod };

  // Локальный/CI прогон каркаса без облака.
  if (process.env.DRY_RUN === '1') {
    logger.info('dry_run', { logicalPeriod });
    const res = await handler({ config, logger, logicalPeriod });
    logger.info('dry_run_done', { rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded });
    return EXIT_OK;
  }

  const bq = new BqClient(config.projectId, config.bqLocation);
  const store = new BqManifestStore(bq, config.rawDataset, config.manifestTable);

  const runId = `${config.environment}:${loaderName}:${logicalPeriod}:${config.gitSha}:${config.executionId || 'na'}`;
  const params: AcquireParams = {
    ...key,
    runId,
    executionId: config.executionId,
    imageDigest: config.imageDigest,
    gitSha: config.gitSha,
    nowMs: Date.now(),
    staleMs: DEFAULT_STALE_STARTED_MS,
  };

  const lock = await store.acquire(params);
  if (!lock.acquired) {
    logger.info('guard_skip', { reason: lock.reason });
    return EXIT_OK; // OK_NO_NEW / ALREADY_RUNNING — не запускаем loader, штатный выход
  }
  logger.info('guard_acquired', { runId: lock.runId, recovered: lock.recovered });

  try {
    const res = await handler({ config, logger, logicalPeriod });
    await store.finalize(key, lock.runId, {
      status: 'COMPLETE',
      rowsFetched: res.rowsFetched,
      rowsLoaded: res.rowsLoaded,
    });
    logger.info('loader_complete', { rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded });
    return EXIT_OK;
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e));
    await store.finalize(key, lock.runId, { status: 'ERROR', errorCode: err.code, errorMessage: err.message });
    logger.error('loader_failed', { code: err.code, message: err.message });
    return EXIT_ERROR;
  }
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    // eslint-disable-next-line no-console
    console.error(JSON.stringify({ severity: 'ERROR', message: 'fatal', error: e instanceof Error ? e.message : String(e) }));
    process.exit(EXIT_ERROR);
  });
