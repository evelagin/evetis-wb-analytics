import { describe, it, expect } from 'vitest';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { makeQualifyLoader, insertSql, prepareStatement, shadowRunsSql, MISSING_PARITY_SQL, QUALIFY_FAILED } from '../src/loaders/qualify/index.js';
import { QUALIFY_SPECS, mapVerdict, splitStatements } from '../src/loaders/qualify/spec.js';
import { PROFILES } from '../src/loaders/cutover/profiles.js';
import { LOADERS } from '../src/loaders/registry.js';
import { loadConfig } from '../src/config.js';
import { Logger } from '../src/logging.js';
import type { Stream } from '../src/ownership/streams.js';

/**
 * Этап C, повторное ревью #426: загрузчик `qualify` — писатель INGEST_QUALIFICATION. Файлы паритета —
 * синтетические (настоящие живут в ветках потоков); BigQuery — заглушка, отвечающая по тексту запроса.
 */

const D = (s: string) => ({ value: s });
const ctx = () => ({
  config: loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw' }),
  logger: new Logger({}, 'error'), logicalPeriod: '2026-11-16T10', runId: 'r', targetDate: '2026-11-16T10',
});

function dirWith(files: Record<string, string>): string {
  const d = mkdtempSync(join(tmpdir(), 'parity-'));
  for (const [f, t] of Object.entries(files)) writeFileSync(join(d, f), t);
  return d;
}
const file = (...queries: string[]) => `-- шапка\n-- QUERY 1\n${queries.map((q, i) => `-- QUERY ${i + 1}\n${q};\n`).join('\n')}`;

function fakeBq(answers: Array<[RegExp, Record<string, unknown>[]]>) {
  const calls: Array<{ sql: string; params?: Record<string, unknown> }> = [];
  return {
    calls,
    bq: {
      projectId: 'p',
      async query(sql: string, params?: Record<string, unknown>) {
        calls.push({ sql, params });
        for (const [re, rows] of answers) if (re.test(sql)) return rows as never[];
        return [] as never[];
      },
    },
  };
}

describe('qualify: группы — ровно группы профиля координатора', () => {
  it('у каждого квалифицируемого потока группы профиля = группы из файла паритета + журнала shadow', () => {
    const produced: Record<string, string[]> = {
      statistics: ['STATS_ORDERS', 'STATS_SALES'], ads: ['ADS_COSTS', 'ADS_STATS'], finance: ['FINANCE_DAILY', 'FINANCE_MONEY', 'FINANCE_WEEKLY'],
      supplies: ['SUPPLIES'], ref: ['REF'],
    };
    for (const s of ['statistics', 'ads', 'finance', 'supplies', 'ref'] as Stream[]) {
      const spec = QUALIFY_SPECS[s]!;
      const groups = [...produced[s]!, ...Object.values(spec.shadowRunGroups)].sort();
      expect(groups, s).toEqual(PROFILES[s].qualification.groups.map((g) => g.group).sort());
      for (const l of spec.shadowLoaders) expect(PROFILES[s].cloudLoaders, s).toContain(l);
    }
    expect(QUALIFY_SPECS.stocks).toBeUndefined(); // M1 — вне координатора
    expect(LOADERS.qualify).toMatchObject({ prodOnly: true, controlPlane: true });
  });

  it('перевод вердиктов: EXACT/DATA_DRIFT/FAIL; всё «не засчитывается» — MISSING', () => {
    expect(['EXACT', 'DATA_DRIFT', 'FAIL', 'NO_DATA', 'NO_RUN', 'SOURCE_GAP', 'PENDING', 'REVIEW', null].map(mapVerdict))
      .toEqual(['PASS', 'DRIFT_EXPLAINED', 'FAIL', 'MISSING', 'MISSING', 'MISSING', 'MISSING', 'MISSING', 'MISSING']);
  });

  it('операторы файла — по порядку; исполняется только SELECT/WITH; d_from подставляется', () => {
    expect(splitStatements(file('SELECT 1', 'WITH a AS (SELECT 1) SELECT * FROM a')).length).toBe(2);
    expect(() => prepareStatement('DELETE FROM x WHERE TRUE', undefined)).toThrow(/не SELECT/);
    expect(prepareStatement("WITH params AS (SELECT DATE '2026-11-12' AS d_from) SELECT 1", '2026-11-14')).toContain("DATE '2026-11-14' AS d_from");
    expect(() => prepareStatement('SELECT 1', '14.11')).toThrow(/не дата/);
  });
});

describe('qualify: прогон', () => {
  it('нет файла паритета — поток пропускается (MISSING_PARITY_SQL), записи нет', async () => {
    const { bq, calls } = fakeBq([]);
    const lines: string[] = [];
    const c = ctx();
    c.logger = { info: () => {}, error: () => {}, debug: () => {}, warn: (m: string, x?: Record<string, unknown>) => { lines.push(`${m}:${String(x?.reason)}:${String(x?.stream)}`); } } as never;
    const res = await makeQualifyLoader({ env: { QUALIFY_SQL_DIR: dirWith({}) }, makeBq: () => bq })(c);
    expect(res).toEqual({ rowsFetched: 0, rowsLoaded: 0 });
    expect(calls).toEqual([]);
    expect(lines).toEqual(['statistics', 'ads', 'finance', 'supplies', 'ref'].map((s) => `qualify_stream_skipped:${MISSING_PARITY_SQL}:${s}`));
  });

  it('финансы: суточная группа, деньги (FAIL только при необъяснённом), WEEKLY понедельника; digest суток; один INSERT', async () => {
    const dir = dirWith({ 'mig_finance_parity.sql': file('WITH q1 AS (SELECT 1) SELECT * FROM q1', 'SELECT 2 AS q2', 'WITH ws AS (SELECT 3) SELECT * FROM ws', 'SELECT 4 AS q4') });
    const { bq, calls } = fakeBq([
      [/q1/, [
        { report_date: D('2026-11-14'), verdict: 'EXACT', rows_mismatch: 0, manifest_diff: 0, dup_rrd: 0 },
        { report_date: D('2026-11-15'), verdict: 'DATA_DRIFT', rows_mismatch: 0, manifest_diff: 0, dup_rrd: 0, reports_prod_only: 1 },
        { report_date: D('2026-11-16'), verdict: 'FAIL', rows_mismatch: 3, manifest_diff: 0, dup_rrd: 0 },
      ]],
      [/WITH ws/, [
        { week_start: D('2026-11-09'), report_type: 'A', verdict: 'EXACT' },
        { week_start: D('2026-11-09'), report_type: 'B', verdict: 'PENDING' },
      ]],
      [/LOADER_RUNS/, [{ loader_name: 'finance', d: D('2026-11-16'), ok: 2, err: 0, digest: 'sha256:aa' }]],
    ]);
    const res = await makeQualifyLoader({ env: { QUALIFY_SQL_DIR: dir, QUALIFY_STREAMS: 'finance' }, makeBq: () => bq })(ctx());
    expect(res.rowsLoaded).toBe(7);
    const insert = calls.find((c) => c.sql.startsWith('INSERT INTO'))!;
    expect(calls.filter((c) => /^\s*(INSERT|UPDATE|MERGE|DELETE)/.test(c.sql))).toHaveLength(1);
    expect(insert.sql).toContain('`p.wb_ops.INGEST_QUALIFICATION`');
    const rows = JSON.parse(String(insert.params!.payload)) as Array<Record<string, unknown>>;
    const v = (d: string, g: string) => rows.find((r) => r.business_date === d && r.check_group === g)?.verdict;
    expect(v('2026-11-14', 'FINANCE_DAILY')).toBe('PASS');
    expect(v('2026-11-15', 'FINANCE_MONEY')).toBe('DRIFT_EXPLAINED');
    expect(v('2026-11-16', 'FINANCE_MONEY')).toBe('FAIL');
    expect(v('2026-11-16', 'FINANCE_WEEKLY')).toBe('MISSING'); // худший по типам: PENDING → MISSING; понедельник = неделя + 7
    expect(rows.find((r) => r.business_date === '2026-11-16' && r.check_group === 'FINANCE_DAILY')!.image_digest).toBe('sha256:aa');
    expect(calls.some((c) => c.sql.includes('SELECT 2 AS q2') || c.sql.includes('SELECT 4 AS q4'))).toBe(false); // только QUERY 1 и 3
  });

  it('статистика: заказы/продажи из QUERY 1, ночная сверка — из журнала shadow; QUALIFY_DRY_RUN — без записи', async () => {
    const dir = dirWith({ 'mig_m2_statistics_parity.sql': file("WITH params AS (SELECT DATE '2026-11-12' AS d_from) SELECT * FROM q1", 'SELECT 2', 'SELECT 3') });
    const { bq, calls } = fakeBq([
      [/q1/, [{ stream: 'orders', day: D('2026-11-15'), verdict: 'EXACT' }, { stream: 'sales', day: D('2026-11-15'), verdict: 'DATA_DRIFT' }]],
      [/LOADER_RUNS/, [{ loader_name: 'sales-reconcile', d: D('2026-11-15'), ok: 0, err: 1, digest: '' }]],
    ]);
    const res = await makeQualifyLoader({ env: { QUALIFY_SQL_DIR: dir, QUALIFY_STREAMS: 'statistics', QUALIFY_DRY_RUN: '1', QUALIFY_D_FROM_STATISTICS: '2026-11-13' }, makeBq: () => bq })(ctx());
    expect(res).toEqual({ rowsFetched: 3, rowsLoaded: 0 });
    expect(calls.some((c) => c.sql.startsWith('INSERT'))).toBe(false);
    expect(calls[0]!.sql).toContain("DATE '2026-11-13' AS d_from");
  });

  it('отказ запроса потока — остальные потоки пишутся, итог QUALIFY_FAILED (алерт)', async () => {
    const dir = dirWith({ 'mig_ref_parity.sql': file('SELECT 1 AS ref1'), 'mig_supplies_parity.sql': file('SELECT 1 AS sup1') });
    const bq = {
      projectId: 'p',
      calls: [] as string[],
      async query(sql: string) {
        this.calls.push(sql);
        if (sql.includes('sup1')) throw new Error('Not found: Table wb_raw.RAW_WB_SUPPLIES__CR');
        if (sql.includes('ref1')) return [{ d: D('2026-11-15'), verdict: 'EXACT' }] as never[];
        return [] as never[];
      },
    };
    await expect(makeQualifyLoader({ env: { QUALIFY_SQL_DIR: dir, QUALIFY_STREAMS: 'supplies,ref' }, makeBq: () => bq })(ctx())).rejects.toMatchObject({ code: QUALIFY_FAILED });
    expect(bq.calls.some((s) => s.startsWith('INSERT'))).toBe(true);
  });

  it('SQL записи и журнала shadow — только своя таблица и только чтение LOADER_RUNS', () => {
    expect(insertSql('p', 'wb_ops')).toMatch(/^INSERT INTO `p\.wb_ops\.INGEST_QUALIFICATION` \(stream, business_date, check_group, verdict, details, image_digest, verdict_at\)/);
    expect(shadowRunsSql('p', 'wb_raw', ['ads'])).toMatch(/^SELECT .*FROM `p\.wb_raw\.LOADER_RUNS`/s);
    expect(shadowRunsSql('p', 'wb_raw', ['ads'])).toContain("environment = 'shadow'");
  });
});
