/**
 * Общая обвязка тестов рекламы: Cloud Run-загрузчик с поддельными WB, BigQuery и Secret Manager.
 * Синтетика — см. ads_fixture.ts. Сетевых вызовов нет.
 */
import { loadConfig } from '../src/config.js';
import { Logger } from '../src/logging.js';
import type { LoaderContext } from '../src/loaders/types.js';
import { AdsHttp, type FetchFn } from '../src/loaders/ads/http.js';
import { toBqRow, type BqRow, type RawRow } from '../src/loaders/ads/normalize.js';
import type { AdsSink, AdsReader } from '../src/loaders/ads/bq.js';
import type { AdsRuntime } from '../src/loaders/ads/index.js';
import { mskNow } from '../src/loaders/ads/time.js';
import { makeWbRouter, scopeFor, COSTS_BY_DAY, type FixtureOpts, type WbRoute } from './ads_fixture.js';

export function fetchFromRoute(route: WbRoute): FetchFn {
  return async (url, init) => {
    const r = route(init.method, url, init.body);
    const h = Object.fromEntries(Object.entries(r.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
    return { status: r.status, headers: { get: (n: string) => h[n.toLowerCase()] ?? null }, text: async () => r.body };
  };
}

/** Память вместо BigQuery: та же нормализация строк, что у AdsBq.append (toBqRow). */
export class MemSink implements AdsSink {
  readonly tables = new Map<string, BqRow[]>();
  readonly order: string[] = [];
  failOn = new Set<string>();
  async append(table: string, columns: readonly string[], rows: RawRow[]): Promise<number> {
    if (!rows.length) return 0;
    if (this.failOn.has(table)) throw new Error(`synthetic BQ failure on ${table}`);
    const norm = rows.map((r) => toBqRow(r, columns));
    const cur = this.tables.get(table) ?? [];
    cur.push(...norm);
    this.tables.set(table, cur);
    this.order.push(table);
    return rows.length;
  }
  rows(table: string): BqRow[] { return this.tables.get(table) ?? []; }
}

export class FakeReader implements AdsReader {
  maxDate: string | Error;
  scopeFail = false;
  costsFail = false;
  constructor(private readonly lp: string) { this.maxDate = lp; }
  async maxStatsDate(): Promise<string> { if (this.maxDate instanceof Error) throw this.maxDate; return this.maxDate; }
  async scopePairsForDay(_v: string, day: string) {
    if (this.scopeFail) throw new Error('synthetic BQ scope failure');
    const s = scopeFor(day, this.lp);
    return { pairs: s.map(({ advertId, nmId }) => ({ advertId, nmId })), spendRub: s.reduce((a, x) => a + x.spend, 0) };
  }
  async costsByDay(): Promise<Record<string, number>> { if (this.costsFail) throw new Error('synthetic'); return COSTS_BY_DAY(this.lp); }
  attempt: string | null = 'COMPLETE';
  attemptCalls: Array<[string, string, string]> = [];
  async adsAttempt(t: string, env: string, lp: string): Promise<string | null> { this.attemptCalls.push([t, env, lp]); return this.attempt; }
}

/** Тихий логгер: тесты проверяют данные, а не вывод. */
export const SILENT = { debug() {}, info() {}, warn() {}, error() {}, child() { return SILENT; } } as unknown as Logger;

export function ctxFor(environment: 'shadow' | 'prod', logicalPeriod: string, executionId = 'wb-ads-test-ab12c'): LoaderContext {
  const config = loadConfig({
    ENVIRONMENT: environment, GCP_PROJECT_ID: 'synthetic-project', BQ_RAW_DATASET: 'wb_raw',
    CLOUD_RUN_EXECUTION: executionId,
  });
  return { config, logger: SILENT, logicalPeriod, runId: `${environment}:ads:${logicalPeriod}`, targetDate: logicalPeriod };
}

export interface Harness {
  rt: AdsRuntime;
  sink: MemSink;
  reader: FakeReader;
  calls: string[];
  tokenCalls: number;
  sleeps: number[];
}

/**
 * Виртуальные часы: sleep и задержка ответа WB двигают время, ничего не ожидая на самом деле.
 * latencyMs(url) — сколько «длится» каждая HTTP-попытка.
 */
export interface VClock { t: number; latencyMs: (url: string) => number; starts: Array<{ t: number; url: string }> }

export function harness(lp: string, env: Record<string, string | undefined> = {}, o: FixtureOpts = {}, clock?: VClock,
  routeOverride?: WbRoute): Harness {
  const made = makeWbRouter(lp, o);
  const calls = made.calls;
  const route: WbRoute = routeOverride
    ? (m, u, b) => { calls.push(`${m.toUpperCase()} ${new URL(u).pathname}`); return routeOverride(m, u, b); }
    : made.route;
  const sink = new MemSink();
  const reader = new FakeReader(lp);
  const h: Harness = { rt: null as unknown as AdsRuntime, sink, reader, calls, tokenCalls: 0, sleeps: [] };
  h.rt = {
    env,
    getToken: async () => { h.tokenCalls++; return 'synthetic-token'; },
    makeDeps: (ctx, cfg, token) => ({
      http: new AdsHttp(token, {
        timeoutMs: 5_000,
        fetchFn: clock
          ? async (url, init) => { clock.starts.push({ t: clock.t, url }); clock.t += clock.latencyMs(url); return fetchFromRoute(route)(url, init); }
          : fetchFromRoute(route),
        sleep: async (ms) => { h.sleeps.push(ms); if (clock) clock.t += ms; },
        nowMs: clock ? () => clock.t : undefined,
      }),
      sink, reader,
      sleep: async (ms) => { h.sleeps.push(ms); if (clock) clock.t += ms; },
      nowMs: clock ? () => clock.t : () => Date.now(),
      nowTs: () => mskNow(new Date()),
      logger: ctx.logger,
      cfg: { ...cfg, fullstatsPauseMs: 0 },
    }),
    now: () => new Date(),
  };
  return h;
}

/** Боевые имена prod-таблиц в env (как выставит Terraform/деплой). */
export const PROD_ENV: Record<string, string> = {
  ADS_CAMPAIGNS_TABLE: 'RAW_WB_ADV_CAMPAIGNS',
  ADS_CAMPAIGN_STATS_TABLE: 'RAW_WB_ADV_CAMPAIGN_STATS',
  ADS_BOOSTER_STATS_TABLE: 'RAW_WB_ADV_BOOSTER_STATS',
  ADS_COSTS_TABLE: 'RAW_WB_ADV_COSTS',
  ADS_COSTS_RUNS_TABLE: 'RAW_WB_ADV_COSTS_RUNS',
  ADS_QUERY_BIDS_TABLE: 'RAW_WB_ADV_QUERY_BIDS',
  ADS_QUERY_BIDS_RUNS_TABLE: 'RAW_WB_ADV_QUERY_BIDS_RUNS',
  ADS_QUERY_STATS_TABLE: 'RAW_WB_ADV_QUERY_STATS',
  ADS_QUERY_STATS_RUNS_TABLE: 'RAW_WB_ADV_QUERY_STATS_RUNS',
};
