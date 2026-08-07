/**
 * Точка входа Cloud Run Job (PR-Mig0): node dist/cli.js <loader>.
 * Оркестрация: config → атомарный acquire (execution-guard) → loader → finalize.
 * loader выполняется ТОЛЬКО при acquired=true.
 *
 * PR-Mart3b-2: логический период берётся из политики загрузчика (spec.logicalPeriod) —
 * stocks/noop=сегодня МСК, mart=D-1 МСК; prodOnly-загрузчик (mart) отклоняется вне prod ДО lease.
 *
 * PR-Mart3b-2 REV2 (аудит, блокер #4): DRY_RUN=1 для prodOnly-загрузчика НЕ исполняет handler —
 *   handler витрины создаёт реальный MartBq и публикует production wb_mart В ОБХОД lease. В сухом
 *   прогоне проверяем только регистрацию/период/контекст и выходим OK. Ядро вынесено в runCli()
 *   с инъекцией зависимостей — чтобы тестами доказать, что в DRY_RUN acquire и handler НЕ зовутся.
 */
import { pathToFileURL } from 'node:url';
import { loadConfig, type Config } from './config.js';
import { Logger, parseLevel } from './logging.js';
import { EXIT_OK, EXIT_ERROR, LoaderError } from './errors.js';
import { resolveLoader, availableLoaderNames, type LoaderSpec } from './loaders/registry.js';
import { BqClient } from './bq/client.js';
import { BqManifestStore, DEFAULT_STALE_STARTED_MS } from './bq/runManifest.js';
import type { ManifestKey, AcquireParams, ManifestStore } from './bq/runManifest.js';
import type { LoaderContext, LoaderResult } from './loaders/types.js';

/** Инъектируемые зависимости — реальные в проде, поддельные в тестах. */
export interface CliDeps {
  makeStore: (config: Config) => ManifestStore;
  runHandler: (spec: LoaderSpec, ctx: LoaderContext) => Promise<LoaderResult>;
  nowMs: () => number;
}

const defaultDeps: CliDeps = {
  makeStore: (config) =>
    new BqManifestStore(new BqClient(config.projectId, config.bqLocation), config.rawDataset, config.manifestTable),
  runHandler: (spec, ctx) => spec.handler(ctx),
  nowMs: () => Date.now(),
};

export async function runCli(
  argv: string[],
  env: NodeJS.ProcessEnv,
  deps: CliDeps = defaultDeps,
): Promise<number> {
  const loaderName = (argv[2] ?? env.LOADER_NAME ?? '').trim();
  const config = loadConfig({ ...env, LOADER_NAME: loaderName || env.LOADER_NAME });
  const logger = new Logger(
    {
      loader: loaderName,
      environment: config.environment,
      imageDigest: config.imageDigest,
      gitSha: config.gitSha,
    },
    parseLevel(config.logLevel),
  );

  const spec = resolveLoader(loaderName);
  if (!spec) {
    logger.error('unknown_loader', { available: availableLoaderNames() });
    return EXIT_ERROR;
  }

  // prodOnly-загрузчик (витрина публикует production wb_mart) запрещён вне prod — отклоняем
  // ДО захвата lease, чтобы не плодить строку LOADER_RUNS для заведомо неразрешённого прогона.
  if (spec.prodOnly && config.environment !== 'prod') {
    logger.error('prod_only_loader', { loader: loaderName, environment: config.environment });
    return EXIT_ERROR;
  }

  // Логический период = политика загрузчика (mart → D-1 МСК; stocks/noop → сегодня МСК).
  // run_id и targetDate вычисляются ОДИН раз; handler их не пересчитывает.
  // Инвариант контракта LoaderContext: targetDate === logicalPeriod (см. types.ts).
  const logicalPeriod = spec.logicalPeriod();
  const key: ManifestKey = { environment: config.environment, loaderName, logicalPeriod };
  const runId = `${config.environment}:${loaderName}:${logicalPeriod}:${config.gitSha}:${config.executionId || 'na'}`;
  const ctxBase: LoaderContext = { config, logger, logicalPeriod, runId, targetDate: logicalPeriod };

  // Локальный/CI прогон каркаса без облака.
  if (env.DRY_RUN === '1') {
    // Блокер #4: prodOnly-handler публикует production В ОБХОД lease — в DRY_RUN его НЕ зовём.
    if (spec.prodOnly) {
      logger.info('dry_run_skip_prod_only', { loader: loaderName, logicalPeriod, targetDate: logicalPeriod });
      return EXIT_OK;
    }
    logger.info('dry_run', { logicalPeriod });
    const res = await deps.runHandler(spec, ctxBase);
    logger.info('dry_run_done', { rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded });
    return EXIT_OK;
  }

  const store = deps.makeStore(config);
  const params: AcquireParams = {
    ...key,
    runId,
    executionId: config.executionId,
    imageDigest: config.imageDigest,
    gitSha: config.gitSha,
    nowMs: deps.nowMs(),
    staleMs: DEFAULT_STALE_STARTED_MS,
  };

  const lock = await store.acquire(params);
  if (!lock.acquired) {
    logger.info('guard_skip', { reason: lock.reason });
    return EXIT_OK; // OK_NO_NEW / ALREADY_RUNNING — не запускаем loader, штатный выход
  }
  logger.info('guard_acquired', { runId: lock.runId, recovered: lock.recovered });

  try {
    const res = await deps.runHandler(spec, { ...ctxBase, runId: lock.runId, targetDate: logicalPeriod });
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

// Запускаем ТОЛЬКО как прямой entry-point (`node dist/cli.js`), НЕ при импорте из тестов —
// иначе import { runCli } в cli.test.ts исполнил бы main с argv vitest и убил бы раннер.
const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  // eslint-disable-next-line @typescript-eslint/no-floating-promises
  runCli(process.argv, process.env)
    .then((code) => process.exit(code))
    .catch((e) => {
      // eslint-disable-next-line no-console
      console.error(JSON.stringify({ severity: 'ERROR', message: 'fatal', error: e instanceof Error ? e.message : String(e) }));
      process.exit(EXIT_ERROR);
    });
}
