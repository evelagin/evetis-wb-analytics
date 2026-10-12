import { describe, it, expect } from 'vitest';
import { BqManifestStore, type AcquireParams } from '../src/bq/runManifest.js';
import type { BqClient } from '../src/bq/client.js';

/**
 * Source-aware guard (2026-10-12): SQL acquire.
 *  1) Без sourceWatermark SQL, параметры и арность вызова БАЙТ-В-БАЙТ как в origin/main 409f3a3.
 *     Образ общий для 13 Job'ов, а ozon_raw.LOADER_RUNS колонки source_watermark не имеет.
 *  2) С sourceWatermark — блокирует только ПОСЛЕДНЯЯ строка COMPLETE той же версии или свежий STARTED;
 *     версия пишется в STARTED в той же транзакции; null типизирован.
 */

// Дословная копия SQL acquire из origin/main 409f3a3 (cloud/src/bq/runManifest.ts), таблица подставлена.
const LEGACY_SQL_409F3A3: string = "\nBEGIN\n  DECLARE active INT64 DEFAULT 0;\n  DECLARE cur_status STRING DEFAULT NULL;\n  DECLARE cur_run_id STRING DEFAULT NULL;\n  DECLARE cur_execution_id STRING DEFAULT NULL;\n  BEGIN TRANSACTION;\n  SET active = (\n    SELECT COUNT(*) FROM `proj.ozon_raw.LOADER_RUNS`\n    WHERE environment=@environment AND loader_name=@loaderName AND logical_period=@logicalPeriod\n      AND (status='COMPLETE' OR (status='STARTED' AND started_at > TIMESTAMP(@staleIso)))\n  );\n  SET cur_status = (\n    SELECT status FROM `proj.ozon_raw.LOADER_RUNS`\n    WHERE environment=@environment AND loader_name=@loaderName AND logical_period=@logicalPeriod\n    ORDER BY started_at DESC LIMIT 1\n  );\n  SET cur_run_id = (\n    SELECT run_id FROM `proj.ozon_raw.LOADER_RUNS`\n    WHERE environment=@environment AND loader_name=@loaderName AND logical_period=@logicalPeriod\n    ORDER BY started_at DESC LIMIT 1\n  );\n  SET cur_execution_id = (\n    SELECT execution_id FROM `proj.ozon_raw.LOADER_RUNS`\n    WHERE environment=@environment AND loader_name=@loaderName AND logical_period=@logicalPeriod\n    ORDER BY started_at DESC LIMIT 1\n  );\n  IF active = 0 THEN\n    INSERT INTO `proj.ozon_raw.LOADER_RUNS`\n      (environment, loader_name, logical_period, run_id, execution_id, image_digest,\n       git_sha, status, attempt_count, started_at)\n    VALUES\n      (@environment, @loaderName, @logicalPeriod, @runId, @executionId, @imageDigest,\n       @gitSha, 'STARTED', 1, CURRENT_TIMESTAMP());\n  END IF;\n  COMMIT TRANSACTION;\n  SELECT active AS active, cur_status AS cur_status, cur_run_id AS cur_run_id, cur_execution_id AS cur_execution_id;\nEND";

interface Call { sql: string; params?: Record<string, unknown>; types?: Record<string, string>; argc: number }

class SpyBq {
  projectId = 'proj';
  location = 'EU';
  calls: Call[] = [];
  constructor(private readonly reply: () => unknown[] = () => [{ active: 0, cur_status: null }]) {}
  async query(...args: unknown[]): Promise<unknown[]> {
    const [sql, params, types] = args as [string, Record<string, unknown> | undefined, Record<string, string> | undefined];
    this.calls.push({ sql, params, types, argc: args.length });
    return this.reply();
  }
}

const NOW = Date.parse('2026-10-13T06:00:00Z');
const base: AcquireParams = {
  environment: 'prod', loaderName: 'mart', logicalPeriod: '2026-10-12', runId: 'prod:mart:2026-10-12:sha:exec-2',
  executionId: 'exec-2', imageDigest: 'sha256:x', gitSha: 'sha', nowMs: NOW, staleMs: 30 * 60 * 1000,
};
const LEGACY_PARAMS = {
  environment: 'prod', loaderName: 'mart', logicalPeriod: '2026-10-12', runId: 'prod:mart:2026-10-12:sha:exec-2',
  executionId: 'exec-2', imageDigest: 'sha256:x', gitSha: 'sha', staleIso: new Date(NOW - 30 * 60 * 1000).toISOString(),
};
const W = 'fin:v2:208423:' + 'a'.repeat(64);

describe('acquire без sourceWatermark — байт-в-байт как до изменения (все загрузчики, кроме mart)', () => {
  for (const dataset of ['wb_raw', 'ozon_raw']) {
    it(`${dataset}: SQL, параметры и отсутствие types совпадают с 409f3a3`, async () => {
      const spy = new SpyBq();
      const store = new BqManifestStore(spy as unknown as BqClient, dataset, 'LOADER_RUNS');
      const r = await store.acquire(base);
      expect(r).toEqual({ acquired: true, runId: base.runId, recovered: false });
      expect(spy.calls).toHaveLength(1);
      const c = spy.calls[0]!;
      const table = '`proj.' + dataset + '.LOADER_RUNS`';
      expect(c.sql).toBe(LEGACY_SQL_409F3A3.split('`proj.ozon_raw.LOADER_RUNS`').join(table));
      expect(c.params).toEqual(LEGACY_PARAMS);
      expect(c.types).toBeUndefined();
      expect(c.argc).toBe(2);
      expect(c.sql).not.toContain('source_watermark');
    });
  }
  it('явный undefined = режим без версии (ключ в params не появляется)', async () => {
    const spy = new SpyBq();
    const store = new BqManifestStore(spy as unknown as BqClient, 'wb_raw', 'LOADER_RUNS');
    await store.acquire({ ...base, sourceWatermark: undefined });
    expect(spy.calls[0]!.sql).not.toContain('source_watermark');
    expect(Object.keys(spy.calls[0]!.params!)).not.toContain('sourceWatermark');
  });
});

describe('acquire с sourceWatermark — SQL', () => {
  const run = async (w: string | null, reply?: () => unknown[]) => {
    const spy = new SpyBq(reply);
    const store = new BqManifestStore(spy as unknown as BqClient, 'wb_raw', 'LOADER_RUNS');
    const r = await store.acquire({ ...base, sourceWatermark: w });
    return { spy, r, c: spy.calls[0]! };
  };

  it('блокирует только ПОСЛЕДНЯЯ строка COMPLETE той же версии + свежий STARTED', async () => {
    const { c } = await run(W);
    const sql = c.sql.replace(/\s+/g, ' ');
    expect(sql).toContain("status='STARTED' AND started_at > TIMESTAMP(@staleIso)");
    expect(sql).toContain("SELECT IF(status='COMPLETE' AND source_watermark = @sourceWatermark, 1, 0) FROM `proj.wb_raw.LOADER_RUNS`");
    expect(sql).toContain('ORDER BY started_at DESC, run_id DESC LIMIT 1');
    expect(sql).toContain('SET active = fresh_started + IFNULL(latest_complete_match, 0);');
    // «любой COMPLETE блокирует» (прежнее условие) в этом режиме отсутствует
    expect(sql).not.toContain("(status='COMPLETE' OR (status='STARTED'");
  });

  it('версия пишется в строку STARTED в ТОЙ ЖЕ транзакции, что и проверка', async () => {
    const { c } = await run(W);
    const sql = c.sql;
    const begin = sql.indexOf('BEGIN TRANSACTION;');
    const insert = sql.indexOf('INSERT INTO');
    const commit = sql.indexOf('COMMIT TRANSACTION;');
    expect(begin).toBeGreaterThan(0);
    expect(insert).toBeGreaterThan(begin);
    expect(commit).toBeGreaterThan(insert);
    expect(sql.replace(/\s+/g, ' ')).toContain("git_sha, status, attempt_count, started_at, source_watermark)");
    expect(sql.replace(/\s+/g, ' ')).toContain("'STARTED', 1, CURRENT_TIMESTAMP(), @sourceWatermark)");
    expect(c.params!.sourceWatermark).toBe(W);
  });

  it('версия неизвестна (null): параметр null, у КАЖДОГО параметра явный тип', async () => {
    const { c } = await run(null);
    expect(c.params!.sourceWatermark).toBeNull();
    expect(c.types!.sourceWatermark).toBe('STRING');
    for (const k of Object.keys(c.params!)) expect(c.types, `нет типа для @${k}`).toHaveProperty(k);
  });

  it('active=0 → acquired; previousWatermark из последнего COMPLETE', async () => {
    const { r } = await run(W, () => [{ active: 0, fresh_started: 0, cur_status: 'COMPLETE', prev_watermark: 'fin:v2:1:' + 'b'.repeat(64) }]);
    expect(r).toEqual({ acquired: true, runId: base.runId, recovered: true, previousWatermark: 'fin:v2:1:' + 'b'.repeat(64) });
  });
  it('active>0 из-за COMPLETE той же версии → COMPLETE (guard_skip)', async () => {
    const { r } = await run(W, () => [{ active: 1, fresh_started: 0, cur_status: 'COMPLETE', cur_run_id: 'old', prev_watermark: W }]);
    expect(r).toEqual({ acquired: false, reason: 'COMPLETE' });
  });
  it('active>0 из-за свежего STARTED → ALREADY_RUNNING (lease не изменился)', async () => {
    const { r } = await run(W, () => [{ active: 1, fresh_started: 1, cur_status: 'STARTED', cur_run_id: 'other', cur_execution_id: 'exec-1' }]);
    expect(r).toEqual({ acquired: false, reason: 'ALREADY_RUNNING' });
  });
  it('свой STARTED после потерянного ответа → тот же lease (recovered)', async () => {
    const { r } = await run(W, () => [{ active: 1, fresh_started: 1, cur_status: 'STARTED', cur_run_id: base.runId, cur_execution_id: 'exec-2' }]);
    expect(r).toMatchObject({ acquired: true, runId: base.runId, recovered: true });
  });
  it('конфликт транзакции → ALREADY_RUNNING; прочая ошибка пробрасывается', async () => {
    const spy = { projectId: 'proj', query: async () => { throw new Error('Transaction is aborted due to concurrent update against table'); } };
    const s1 = new BqManifestStore(spy as unknown as BqClient, 'wb_raw', 'LOADER_RUNS');
    await expect(s1.acquire({ ...base, sourceWatermark: W })).resolves.toEqual({ acquired: false, reason: 'ALREADY_RUNNING' });
    const spy2 = { projectId: 'proj', query: async () => { throw new Error('ECONNRESET'); } };
    const s2 = new BqManifestStore(spy2 as unknown as BqClient, 'wb_raw', 'LOADER_RUNS');
    await expect(s2.acquire({ ...base, sourceWatermark: W })).rejects.toThrow('ECONNRESET');
  });
  it('finalize не трогает source_watermark (версия STARTED переживает COMPLETE)', async () => {
    const spy = new SpyBq();
    const store = new BqManifestStore(spy as unknown as BqClient, 'wb_raw', 'LOADER_RUNS');
    await store.finalize({ environment: 'prod', loaderName: 'mart', logicalPeriod: '2026-10-12' }, base.runId, { status: 'COMPLETE' });
    expect(spy.calls[0]!.sql).not.toContain('source_watermark');
    expect(spy.calls[0]!.sql).toContain('AND run_id=@runId');
  });
});
