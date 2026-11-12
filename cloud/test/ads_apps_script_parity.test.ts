/**
 * M3 — replay-parity 1:1 с БОЕВЫМ Apps Script рекламы.
 *
 * Одна и та же синтетическая лента WB Advert API прогоняется:
 *   A) через исполняемый код apps-script/ingestion/{WbAdsProbe,WbAdsRawLoader,Wbadsbigquery,Wbadsdaily,
 *      WbAdsQueryBids,Wbadsquerystats,IngestRunLog}.gs + wbFetchWithRetry_ (Utils.gs) + bqLoadRows_/bqQuery_
 *      (WbBigQuery.gs) в node:vm — UrlFetchApp, BigQuery, Utilities и свойства подменены;
 *      точка входа — сам runWbAdsDaily();
 *   B) через Cloud Run `ads` и затем `ads-query` (хвост Ads-3/Ads-2; fetch, BigQuery и Secret Manager подменены).
 * Сравниваются ВСЕ строки всех девяти RAW-таблиц после нормализации в STRING (как их видит BigQuery)
 * и итог журнала. Различаются только поля времени запуска и run_id.
 *
 * Код Apps Script читается из репозитория, а не копируется: его изменение обязано уронить тест.
 */
process.env.TZ = 'Europe/Moscow'; // часовой пояс проекта Apps Script (wbAdsInstallDailyTrigger требует МСК)

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import vm from 'node:vm';
import { makeAdsLoader, makeAdsQueryLoader } from '../src/loaders/ads/index.js';
import { d1Moscow } from '../src/loaders/mart/targetDate.js';
import {
  CAMPAIGNS_COLUMNS, CAMPAIGN_STATS_COLUMNS, BOOSTER_STATS_COLUMNS, COSTS_COLUMNS, COSTS_RUNS_COLUMNS,
  QUERY_BIDS_COLUMNS, QUERY_BIDS_RUNS_COLUMNS, QUERY_STATS_COLUMNS, QUERY_STATS_RUNS_COLUMNS,
} from '../src/loaders/ads/constants.js';
import { makeWbRouter, scopeFor, COSTS_BY_DAY, type FixtureOpts } from './ads_fixture.js';
import { harness, ctxFor } from './ads_harness.js';

const here = dirname(fileURLToPath(import.meta.url));
const gs = (f: string): string => readFileSync(join(here, '..', '..', 'apps-script', 'ingestion', f), 'utf8');
const fn = (src: string, name: string): string => {
  const m = src.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  if (!m) throw new Error(`${name} не найдена`);
  return m[0];
};

const COLS: Record<string, readonly string[]> = {
  RAW_WB_ADV_CAMPAIGNS: CAMPAIGNS_COLUMNS, RAW_WB_ADV_CAMPAIGN_STATS: CAMPAIGN_STATS_COLUMNS,
  RAW_WB_ADV_BOOSTER_STATS: BOOSTER_STATS_COLUMNS, RAW_WB_ADV_COSTS: COSTS_COLUMNS, RAW_WB_ADV_COSTS_RUNS: COSTS_RUNS_COLUMNS,
  RAW_WB_ADV_QUERY_BIDS: QUERY_BIDS_COLUMNS, RAW_WB_ADV_QUERY_BIDS_RUNS: QUERY_BIDS_RUNS_COLUMNS,
  RAW_WB_ADV_QUERY_STATS: QUERY_STATS_COLUMNS, RAW_WB_ADV_QUERY_STATS_RUNS: QUERY_STATS_RUNS_COLUMNS,
};

function formatDate(d: Date, tz: string, pattern: string): string {
  const p: Record<string, string> = {};
  for (const x of new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(d)) p[x.type] = x.value;
  return pattern.replace(/yyyy|MM|dd|HH|mm|ss/g, (t) => ({ yyyy: p.year, MM: p.month, dd: p.day, HH: p.hour, mm: p.minute, ss: p.second }[t] ?? t));
}

interface AsOut { tables: Record<string, Record<string, string>[]>; journal: Array<Record<string, unknown>> }

function runAppsScript(lp: string, o: FixtureOpts): AsOut {
  const { route } = makeWbRouter(lp, o);
  const out: AsOut = { tables: {}, journal: [] };
  const bqResult = (rows: unknown[][], affected?: number) => ({
    jobComplete: true, jobReference: { jobId: 'q', location: 'EU' },
    rows: rows.map((r) => ({ f: r.map((v) => ({ v })) })),
    ...(affected !== undefined ? { numDmlAffectedRows: String(affected) } : {}),
  });
  const props: Record<string, string> = { WB_ADS_BQ_SINK: '1', WB_TOKEN_PROMOTION: 'synthetic-token' };
  const sandbox: Record<string, unknown> = {
    console: { log: () => {}, error: () => {}, warn: () => {} },
    Logger: { log: () => {} },
    Session: { getScriptTimeZone: () => 'Europe/Moscow' },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    SpreadsheetApp: { getActiveSpreadsheet: () => ({}) }, // WB_ADS_STATUS — лист; ошибка его записи ловится wbAdsFinish_
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (k: string) => props[k] ?? null, setProperty: () => {}, deleteProperty: () => {},
    }) },
    Utilities: {
      formatDate, sleep: () => {}, getUuid: () => randomUUID(),
      newBlob: (s: string) => ({ s }),
    },
    UrlFetchApp: {
      fetch: (url: string, opt: { method?: string; payload?: string }) => {
        const r = route(opt.method ?? 'get', url, opt.payload);
        return { getResponseCode: () => r.status, getContentText: () => r.body, getHeaders: () => r.headers ?? {} };
      },
    },
    getBqConfig_: () => ({ projectId: 'synthetic-project', datasetId: 'wb_raw', location: 'EU' }),
    bqEnsureDataset_: () => {},
    BigQuery: {
      Tables: {
        get: (_p: string, _d: string, t: string) => ({ schema: { fields: (COLS[t] ?? []).map((name) => ({ name, type: 'STRING' })) } }),
      },
      Jobs: {
        insert: (job: { configuration: { load: { destinationTable: { tableId: string } } } }, _p: string, blob: { s: string }) => {
          const t = job.configuration.load.destinationTable.tableId;
          const rows = blob.s.split('\n').filter(Boolean).map((l) => JSON.parse(l) as Record<string, string>);
          (out.tables[t] ??= []).push(...rows);
          return { jobReference: { jobId: `load_${t}`, location: 'EU' } };
        },
        get: () => ({ status: { state: 'DONE' } }),
        query: (req: { query: string; queryParameters?: Array<{ name: string; parameterValue: { value: unknown } }> }) => {
          const q = req.query;
          const params = Object.fromEntries((req.queryParameters ?? []).map((x) => [x.name, x.parameterValue.value]));
          if (/INSERT INTO .*INGEST_RUNS/s.test(q)) { out.journal.push({ op: 'start', ...params }); return bqResult([], 1); }
          if (/UPDATE .*INGEST_RUNS/s.test(q)) {
            out.journal.push({ op: /status="COMPLETE"/.test(q) ? 'COMPLETE' : 'ERROR', ...params });
            return bqResult([], 1);
          }
          if (/MAX\(`date`\)/.test(q)) return bqResult([[lp]]);
          const day = /DATE\(`date`\) = DATE\('(\d{4}-\d{2}-\d{2})'\)/.exec(q)?.[1];
          if (day) return bqResult(scopeFor(day, lp).map((s) => [s.advertId, s.nmId, s.spend]));
          if (/V_ADV_COSTS/.test(q)) return bqResult(Object.entries(COSTS_BY_DAY(lp)).map(([d, v]) => [d, v]));
          throw new Error(`неожиданный SQL в Apps Script: ${q.slice(0, 120)}`);
        },
      },
    },
  };
  const ctx = vm.createContext(sandbox);
  const src = [
    fn(gs('Utils.gs'), 'wbFetchWithRetry_'), fn(gs('Utils.gs'), 'wbRetryAfterMs_'),
    fn(gs('WbBigQuery.gs'), 'bqLoadRows_'), fn(gs('WbBigQuery.gs'), 'bqQuery_'),
    gs('WbAdsProbe.gs'), gs('WbAdsRawLoader.gs'), gs('Wbadsbigquery.gs'), gs('IngestRunLog.gs'),
    gs('WbAdsQueryBids.gs'), gs('Wbadsquerystats.gs'), gs('Wbadsdaily.gs'),
  ].join('\n;\n');
  vm.runInContext(src, ctx, { filename: 'apps-script-ads.gs' });
  vm.runInContext('runWbAdsDaily()', ctx);
  return out;
}

const VOLATILE = new Set(['load_ts', 'run_id', 'window_completed_at', 'duration_ms', 'snapshot_ts']);
const canon = (rows: Record<string, string>[] | undefined): string[] =>
  (rows ?? []).map((r) => JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k]) => !VOLATILE.has(k)).sort()))).sort();

async function runCloud(lp: string, o: FixtureOpts) {
  const h = harness(lp, {}, o);
  let error: { code?: string } | null = null;
  let res = null;
  try { res = await makeAdsLoader(h.rt)(ctxFor('shadow', lp)); } catch (e) { error = e as { code?: string }; }
  // Хвост эталона (Ads-3, Ads-2) в Cloud Run — отдельный загрузчик `ads-query` после 'ads' того же D−1.
  h.reader.attempt = error ? 'ERROR' : 'COMPLETE';
  await makeAdsQueryLoader(h.rt)(ctxFor('shadow', lp));
  return { h, res, error };
}

describe('ads: replay-parity Cloud Run ↔ Apps Script runWbAdsDaily', () => {
  const scenarios: Array<[string, FixtureOpts]> = [
    ['штатный день', {}],
    ['fullstats 500 на одной кампании → 50→10→1, PARTIAL', { fullstatsFailId: 1004 }],
    ['upd пустой + 429 с Retry-After', { updEmpty: true, upd429: 2 }],
    ['get-bids 500 и потеря пакета Ads-2', { bidsFail: true, qstatsDropPack: true }],
    ['count 500 → fullstats SKIPPED → ERROR', { countFail: true }],
  ];

  for (const [name, o] of scenarios) {
    it(name, async () => {
      const lp = d1Moscow(new Date());
      const as = runAppsScript(lp, o);
      const cr = await runCloud(lp, o);

      for (const table of Object.keys(COLS)) {
        expect(canon(cr.h.sink.rows(`${table}__CR`)), table).toEqual(canon(as.tables[table]));
      }
      // Журнал: Apps Script открывает 'ads' за D−1 и закрывает whitelist'ом; Cloud Run — тот же исход в LOADER_RUNS.
      const start = as.journal.find((j) => j.op === 'start');
      expect(start?.loader_name).toBe('ads');
      expect(start?.logical_period).toBe(lp);
      const fin = as.journal.find((j) => j.op !== 'start');
      if (fin?.op === 'COMPLETE') {
        expect(cr.error).toBeNull();
        expect(cr.res?.rowsLoaded).toBe(Number(fin.rows_loaded));
      } else {
        expect(cr.error?.code).toBe(fin?.error_code);
      }
    });
  }

  it('сценарии действительно различаются: PARTIAL и ERROR доходят до журнала', () => {
    const lp = d1Moscow(new Date());
    expect(runAppsScript(lp, { fullstatsFailId: 1004 }).journal.find((j) => j.op === 'ERROR')?.error_code).toBe('ADS_PARTIAL');
    expect(runAppsScript(lp, { countFail: true }).journal.find((j) => j.op === 'ERROR')?.error_code).toBe('ADS_ERROR');
    expect(runAppsScript(lp, {}).journal.some((j) => j.op === 'COMPLETE')).toBe(true);
  });
});
