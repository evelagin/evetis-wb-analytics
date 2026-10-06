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
import { randomUUID } from 'node:crypto';
import { loadConfig, type Config } from './config.js';
import { Logger, parseLevel } from './logging.js';
import { EXIT_OK, EXIT_ERROR } from './errors.js';
import { resolveLoader, availableLoaderNames, type LoaderSpec } from './loaders/registry.js';
import { BqClient } from './bq/client.js';
import { BqManifestStore, DEFAULT_STALE_STARTED_MS } from './bq/runManifest.js';
import type { ManifestKey, AcquireParams, ManifestStore } from './bq/runManifest.js';
import type { LoaderContext, LoaderResult } from './loaders/types.js';
import { classifyFailure, fatalRecord, type FailureClass } from './failure.js';

/** Пауза перед повтором захвата lease (до lease ничего не исполнялось — повтор безопасен всегда). */
export const PRE_LOCK_RETRY_DELAY_MS = 30_000;
/** Пауза перед повтором идемпотентного писателя после транзиентного отказа (тот же часовой слот). */
export const POST_LOCK_RETRY_DELAY_MS = 60_000;
/** Повтор после lease — только если с начала прогона прошло не больше этого (таймаут WB-Job 600 с). */
export const POST_LOCK_RETRY_MAX_ELAPSED_MS = 240_000;
/** Пауза перед повтором finalize(COMPLETE): работа уже сделана, закрываем только журнал. */
export const FINALIZE_RETRY_DELAY_MS = 10_000;

/** Инъектируемые зависимости — реальные в проде, поддельные в тестах. */
export interface CliDeps {
  makeStore: (config: Config) => ManifestStore;
  runHandler: (spec: LoaderSpec, ctx: LoaderContext) => Promise<LoaderResult>;
  nowMs: () => number;
  sleep?: (ms: number) => Promise<void>;
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
    logger.error('unknown_loader', { code: 'UNKNOWN_LOADER', available: availableLoaderNames() });
    return EXIT_ERROR;
  }

  // prodOnly-загрузчик (витрина публикует production wb_mart) запрещён вне prod — отклоняем
  // ДО захвата lease, чтобы не плодить строку LOADER_RUNS для заведомо неразрешённого прогона.
  if (spec.prodOnly && config.environment !== 'prod') {
    logger.error('prod_only_loader', { code: 'PROD_ONLY_LOADER', loader: loaderName, environment: config.environment });
    return EXIT_ERROR;
  }

  // Логический период = политика загрузчика (mart → D-1 МСК; stocks/noop → сегодня МСК).
  // run_id и targetDate вычисляются ОДИН раз; handler их не пересчитывает.
  // Инвариант контракта LoaderContext: targetDate === logicalPeriod (см. types.ts).
  const logicalPeriod = spec.logicalPeriod();
  const key: ManifestKey = { environment: config.environment, loaderName, logicalPeriod };
  // Без CLOUD_RUN_EXECUTION (локально/CI) — уникальный id прогона: иначе два прогона одного часа
  // получили бы одинаковый run_id и «узнали» бы чужую строку STARTED как свою.
  const execId = config.executionId || `local-${randomUUID()}`;
  const runId = `${config.environment}:${loaderName}:${logicalPeriod}:${config.gitSha}:${execId}`;
  const startedMs = deps.nowMs();
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

  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((res) => setTimeout(res, ms)));
  const failed = (stage: string, c: FailureClass, message: string): number => {
    // `code` непустой всегда — по нему срабатывает алерт (jsonPayload.code!="").
    logger.error('loader_failed', { code: c.code, category: c.category, stage, message });
    return EXIT_ERROR;
  };

  let store: ManifestStore;
  try {
    store = deps.makeStore(config);
  } catch (e) {
    return failed('make_store', classifyFailure(e), e instanceof Error ? e.message : String(e));
  }

  // Захват lease. До lease ничего не исполнялось, поэтому транзиентный отказ повторяем один раз
  // для ЛЮБОГО загрузчика. Инцидент 2026-10-06 (zbfk7): BigQuery упал именно здесь.
  const acquire = async (runIdForAttempt: string) => {
    const params: AcquireParams = {
      ...key,
      runId: runIdForAttempt,
      executionId: config.executionId,
      imageDigest: config.imageDigest,
      gitSha: config.gitSha,
      nowMs: deps.nowMs(),
      staleMs: DEFAULT_STALE_STARTED_MS,
    };
    try {
      return await store.acquire(params);
    } catch (e) {
      const c = classifyFailure(e);
      if (c.category !== 'TRANSIENT') throw e;
      logger.warn('transient_retry', { stage: 'acquire', code: c.code, delayMs: PRE_LOCK_RETRY_DELAY_MS, error: e instanceof Error ? e.message : String(e) });
      await sleep(PRE_LOCK_RETRY_DELAY_MS);
      return await store.acquire({ ...params, nowMs: deps.nowMs() });
    }
  };

  let lock: Awaited<ReturnType<ManifestStore['acquire']>>;
  try {
    lock = await acquire(runId);
  } catch (e) {
    return failed('acquire', classifyFailure(e), e instanceof Error ? e.message : String(e));
  }
  if (!lock.acquired) {
    logger.info('guard_skip', { reason: lock.reason });
    return EXIT_OK; // OK_NO_NEW / ALREADY_RUNNING — не запускаем loader, штатный выход
  }
  logger.info('guard_acquired', { runId: lock.runId, recovered: lock.recovered });

  // Повтор после lease — только транзиентный отказ и только идемпотентный писатель (retryTransient).
  // Повтор получает СВОЙ run_id (executionId-r2): строка ERROR первой попытки остаётся в журнале,
  // а не перезаписывается COMPLETE второй попытки.
  const postLockRetries = spec.retryTransient ? 1 : 0;
  for (let attempt = 1; ; attempt++) {
    let res: LoaderResult;
    try {
      res = await deps.runHandler(spec, { ...ctxBase, runId: lock.runId, targetDate: logicalPeriod });
    } catch (e) {
      const c = classifyFailure(e);
      const message = e instanceof Error ? e.message : String(e);
      try {
        await store.finalize(key, lock.runId, { status: 'ERROR', errorCode: c.code, errorMessage: message });
      } catch (fe) {
        logger.warn('finalize_failed', { status: 'ERROR', code: classifyFailure(fe).code, error: fe instanceof Error ? fe.message : String(fe) });
      }
      const elapsed = deps.nowMs() - startedMs;
      if (c.category === 'TRANSIENT' && attempt <= postLockRetries && elapsed > POST_LOCK_RETRY_MAX_ELAPSED_MS) {
        logger.warn('transient_retry_skipped', { stage: 'handler', code: c.code, elapsedMs: elapsed, budgetMs: POST_LOCK_RETRY_MAX_ELAPSED_MS });
      }
      if (c.category === 'TRANSIENT' && attempt <= postLockRetries && elapsed <= POST_LOCK_RETRY_MAX_ELAPSED_MS) {
        logger.warn('transient_retry', { stage: 'handler', code: c.code, attempt, delayMs: POST_LOCK_RETRY_DELAY_MS, error: message });
        await sleep(POST_LOCK_RETRY_DELAY_MS);
        const retryRunId = `${config.environment}:${loaderName}:${logicalPeriod}:${config.gitSha}:${execId}-r${attempt + 1}`;
        let again: Awaited<ReturnType<ManifestStore['acquire']>>;
        try {
          again = await acquire(retryRunId);
        } catch (ae) {
          return failed('retry_acquire', classifyFailure(ae), ae instanceof Error ? ae.message : String(ae));
        }
        if (!again.acquired) {
          // Слот занят другим исполнением (или уже COMPLETE) — сами не пишем; отказ первой попытки виден.
          return failed('retry_acquire', { code: 'RETRY_LOCK_UNAVAILABLE', category: 'DETERMINISTIC' }, `${c.code}: ${again.reason}`);
        }
        logger.info('guard_acquired', { runId: again.runId, recovered: again.recovered, retryOf: lock.runId });
        lock = again;
        continue;
      }
      return failed('handler', c, message);
    }
    // Работа выполнена; закрываем журнал. Handler НЕ перезапускаем — повторяем только UPDATE (идемпотентен).
    const complete = { status: 'COMPLETE' as const, rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded };
    try {
      await store.finalize(key, lock.runId, complete);
    } catch (e) {
      if (classifyFailure(e).category !== 'TRANSIENT') {
        return failed('finalize', { code: 'MANIFEST_FINALIZE_FAILED', category: 'DETERMINISTIC' }, e instanceof Error ? e.message : String(e));
      }
      await sleep(FINALIZE_RETRY_DELAY_MS);
      try {
        await store.finalize(key, lock.runId, complete);
      } catch (e2) {
        return failed('finalize', { code: 'MANIFEST_FINALIZE_FAILED', category: 'TRANSIENT' }, e2 instanceof Error ? e2.message : String(e2));
      }
    }
    logger.info('loader_complete', { rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded, attempt });
    return EXIT_OK;
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
      // Код есть всегда (fatalRecord) — иначе алерт `jsonPayload.code!=""` отказ не видит (инцидент zbfk7).
      console.error(JSON.stringify(fatalRecord(e)));
      process.exit(EXIT_ERROR);
    });
}
