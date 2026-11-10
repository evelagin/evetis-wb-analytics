/**
 * M4 · Загрузчик финансовых отчётов WB (Finance API v1) — Cloud Run-замена runWbFinanceDaily.
 *
 * Порядок fail-closed (как в Apps Script: токен → C0 → журнал):
 *   1) контур и время прогона (targets.ts) — чистая проверка env, ДО любого I/O;
 *   2) готовность схемы (метаданные таблиц, эквивалент C0-маркера) — ДО токена и WB API;
 *   3) токен из Secret Manager; 4) тик (run.ts) — пишет FINANCE_LOADER_RUNS и манифест.
 *
 * Исход для LOADER_RUNS (его пишет cli.ts):
 *   OK / OK_NO_NEW / PARTIAL → COMPLETE (PARTIAL — хвост очереди или ошибка отдельного отчёта,
 *   дочитывается следующим слотом; виден в FINANCE_LOADER_RUNS и WARNING-событием);
 *   ERROR → LoaderError: LOADER_RUNS=ERROR, событие loader_failed с кодом — алерт.
 * ScriptLock Apps Script заменён lease LOADER_RUNS (один прогон на слот, ALREADY_RUNNING — выход 0) плюс
 * взаимоисключение тика и finance-backfill (mutex.ts): тик при активной регистрации пропускается (SKIPPED_LOCKED),
 * регистрация при активном тике отказывает (FINANCE_LOCKED) — до токена, WB API и записи.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { assertFinanceTargets } from './targets.js';
import { BqFinanceStore, BigQuerySqlRunner } from './bq.js';
import { FinanceApi, realClock, type FetchFn, type Clock } from './wbApi.js';
import { runFinanceTick, runBackfillRegister, type TickResult } from './run.js';
import { newFinanceRunId, isCalendarDate } from './parse.js';
import type { FinanceStore } from './store.js';
import { bqPeerProbe, peerOf, type PeerProbe } from './mutex.js';

/** Инъекция для тестов: подменяются хранилище, сеть, секрет и часы. */
export interface FinanceLoaderDeps {
  makeStore?: (ctx: LoaderContext) => FinanceStore;
  fetchFn?: FetchFn;
  readToken?: (ctx: LoaderContext) => Promise<string>;
  clock?: Clock;
  peerProbe?: PeerProbe;
}

function realStore(ctx: LoaderContext): FinanceStore {
  const c = ctx.config;
  return new BqFinanceStore(new BigQuerySqlRunner(c.projectId, c.rawDataset, c.bqLocation), c.projectId, c.rawDataset, c.financeTables);
}

type Prepared = { kind: 'ready'; store: FinanceStore; api: FinanceApi; clock: Clock; runId: string; log: LoaderContext['logger'] }
  | { kind: 'dry_run' } | { kind: 'peer_active'; peer: string; active: number };

async function prepare(ctx: LoaderContext, deps: FinanceLoaderDeps, mode: 'tick' | 'backfill'): Promise<Prepared> {
  const { config, logger } = ctx;
  assertFinanceTargets(config);
  const store = (deps.makeStore ?? realStore)(ctx);
  await store.assertReady();
  // cli в DRY_RUN исполняет handler не-prodOnly загрузчика: финансы в сухом прогоне ничего не пишут и не ходят в WB.
  if (config.financeDryRun) return { kind: 'dry_run' };
  // Взаимоисключение с соседним режимом (mutex.ts) — до токена, WB API и любой записи.
  const peer = peerOf(mode);
  const active = await (deps.peerProbe ?? bqPeerProbe)(ctx, peer);
  if (active > 0) return { kind: 'peer_active', peer, active };
  const token = (await (deps.readToken ?? ((x: LoaderContext) => new SecretsClient(x.config.projectId).access(x.config.wbFinanceSecret)))(ctx)).trim();
  if (!token) throw new LoaderError(`Нет токена ${config.wbFinanceSecret}`, 'FINANCE_TOKEN_MISSING');
  const clock = deps.clock ?? realClock;
  const t0 = clock.now();
  const api = new FinanceApi(
    { host: config.wbFinanceHost, token, t0, budgetMs: config.financeBudgetMs, timeoutMs: config.financeHttpTimeoutMs },
    clock,
    deps.fetchFn ?? (fetch as unknown as FetchFn),
    logger,
  );
  const runId = newFinanceRunId(t0);
  const log = logger.child({ financeRunId: runId, cloudRunId: ctx.runId, tables: config.financeTables.raw });
  return { kind: 'ready', store, api, clock, runId, log };
}

function toResult(res: TickResult, code: string): LoaderResult {
  if (res.status === 'ERROR') {
    throw new LoaderError(`finance ${res.runId}: ${res.errorMessage || 'ERROR'}`.substring(0, 900), code);
  }
  return { rowsFetched: res.rowsFetched, rowsLoaded: res.rowsLoaded };
}

export async function financeLoader(ctx: LoaderContext, deps: FinanceLoaderDeps = {}): Promise<LoaderResult> {
  const p = await prepare(ctx, deps, 'tick');
  if (p.kind === 'dry_run') { ctx.logger.info('finance_dry_run', { tables: ctx.config.financeTables }); return { rowsFetched: 0, rowsLoaded: 0 }; }
  if (p.kind === 'peer_active') {
    // Как SKIPPED_LOCKED Apps Script: тик пропускается без журнала и без записи; следующий слот дочитает очередь.
    ctx.logger.warn('finance_skipped_locked', { code: 'FINANCE_SKIPPED_LOCKED', peer: p.peer, active: p.active });
    return { rowsFetched: 0, rowsLoaded: 0 };
  }
  const res = await runFinanceTick({
    store: p.store, api: p.api, clock: p.clock, logger: p.log, runId: p.runId,
    triggerType: ctx.config.financeTriggerType, cutover: ctx.config.financeCutover,
  });
  if (res.status === 'PARTIAL') {
    p.log.warn('finance_partial', { code: 'FINANCE_PARTIAL', queueLeft: res.queueLeft, errors: res.counters.errors });
  }
  return toResult(res, res.anomalies.length ? 'FINANCE_IMMUTABLE_ANOMALY' : 'FINANCE_RUN_ERROR');
}

/** finance-backfill: регистрация WEEKLY-отчётов периода (FINANCE_BACKFILL_FROM/TO), без загрузки. */
export async function financeBackfillLoader(ctx: LoaderContext, deps: FinanceLoaderDeps = {}): Promise<LoaderResult> {
  const { financeBackfillFrom: from, financeBackfillTo: to } = ctx.config;
  if (!isCalendarDate(from) || !isCalendarDate(to) || from > to) {
    throw new LoaderError(`FINANCE_BACKFILL_FROM/TO: нужен период YYYY-MM-DD (from<=to), получено '${from}'…'${to}'`, 'FINANCE_BACKFILL_PERIOD');
  }
  const p = await prepare(ctx, deps, 'backfill');
  if (p.kind === 'dry_run') { ctx.logger.info('finance_dry_run', { tables: ctx.config.financeTables }); return { rowsFetched: 0, rowsLoaded: 0 }; }
  if (p.kind === 'peer_active') {
    // Как в Apps Script («ScriptLock занят — повторите позже»): регистрация не идёт параллельно тику.
    throw new LoaderError(`идёт тик ${p.peer} (${p.active} активн.) — повторите регистрацию вне слота`, 'FINANCE_LOCKED');
  }
  const res = await runBackfillRegister({
    store: p.store, api: p.api, clock: p.clock, logger: p.log, runId: p.runId,
    triggerType: 'BACKFILL', cutover: ctx.config.financeCutover,
  }, from, to);
  return toResult(res, 'FINANCE_BACKFILL_ERROR');
}
