/**
 * Точка входа Cloud Run Job (PR-Mig0): node dist/cli.js <loader>.
 * Оркестрация: config → execution-guard (env×loader×period) → loader → finalize.
 * DRY_RUN=1 прогоняет каркас без BigQuery (для локальной проверки).
 */
import { loadConfig } from './config.js';
import { Logger, parseLevel } from './logging.js';
import { EXIT_OK, EXIT_ERROR, LoaderError } from './errors.js';
import { resolveLoader } from './loaders/registry.js';
import { dailyPeriodMoscow } from './period.js';
import { BqClient } from './bq/client.js';
import { BqManifestStore, decideRun, DEFAULT_STALE_STARTED_MS } from './bq/runManifest.js';
import type { ManifestKey, ManifestRecord } from './bq/runManifest.js';

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
  const key: ManifestKey = {
    environment: config.environment,
    loaderName,
    logicalPeriod,
  };

  // Локальный/CI прогон каркаса без облака.
  if (process.env.DRY_RUN === '1') {
    logger.info('dry_run', { logicalPeriod });
    const res = await handler({ config, logger, logicalPeriod });
    logger.info('dry_run_done', { rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded });
    return EXIT_OK;
  }

  const bq = new BqClient(config.projectId, config.bqLocation);
  const store = new BqManifestStore(bq, config.rawDataset, config.manifestTable);

  const current = await store.getCurrent(key);
  const decision = decideRun({ current, nowMs: Date.now(), staleStartedMs: DEFAULT_STALE_STARTED_MS });
  logger.info('guard_decision', { action: decision.action, reason: decision.reason });
  if (decision.action === 'OK_NO_NEW') return EXIT_OK;
  if (decision.action === 'ALREADY_RUNNING') return EXIT_OK;

  const runId = `${config.environment}:${loaderName}:${logicalPeriod}:${config.gitSha}:${config.executionId || 'na'}`;
  const started: ManifestRecord = {
    ...key,
    runId,
    executionId: config.executionId,
    imageDigest: config.imageDigest,
    gitSha: config.gitSha,
    status: 'STARTED',
    attemptCount: 1,
    startedAt: new Date().toISOString(),
  };
  await store.insertStarted(started);

  try {
    const res = await handler({ config, logger, logicalPeriod });
    await store.finalize(key, runId, { status: 'COMPLETE', rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded });
    logger.info('loader_complete', { rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded });
    return EXIT_OK;
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e));
    await store.finalize(key, runId, { status: 'ERROR', errorCode: err.code, errorMessage: err.message });
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
