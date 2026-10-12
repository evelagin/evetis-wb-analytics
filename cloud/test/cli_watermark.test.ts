import { describe, it, expect, vi, afterEach } from 'vitest';
import { runCli, type CliDeps } from '../src/cli.js';
import { EXIT_OK } from '../src/errors.js';
import { LOADERS } from '../src/loaders/registry.js';
import { MART_SOURCE_WATERMARK_PROBE_FAILED } from '../src/loaders/mart/watermark.js';
import type {
  AcquireParams, AcquireResult, FinalizePatch, ManifestKey, ManifestStore,
} from '../src/bq/runManifest.js';
import type { LoaderContext } from '../src/loaders/types.js';

/**
 * Source-aware guard витрины (2026-10-12): матрица решений и проводка cli.
 *
 * MemWatermarkStore моделирует SQL BqManifestStore.acquireWithWatermark (та же семантика, что
 * проверяется живым read-only прогоном предикатов в BigQuery, см. IMPLEMENTATION.txt §3):
 *   блокирует свежий STARTED ИЛИ последняя строка периода = COMPLETE той же версии.
 */
interface Row extends ManifestKey {
  runId: string; executionId: string; status: 'STARTED' | 'COMPLETE' | 'ERROR'; startedAt: number; sourceWatermark: string | null;
}

class MemWatermarkStore implements ManifestStore {
  rows: Row[] = [];
  acquires: AcquireParams[] = [];
  async acquire(p: AcquireParams): Promise<AcquireResult> {
    this.acquires.push(p);
    const mine = this.rows.filter((r) => r.environment === p.environment && r.loaderName === p.loaderName && r.logicalPeriod === p.logicalPeriod);
    const latest = [...mine].sort((a, b) => b.startedAt - a.startedAt || (b.runId < a.runId ? -1 : 1))[0] ?? null;
    const freshStarted = mine.filter((r) => r.status === 'STARTED' && r.startedAt > p.nowMs - p.staleMs).length;
    let active: number;
    if (p.sourceWatermark === undefined) {
      active = mine.filter((r) => r.status === 'COMPLETE').length + freshStarted;
    } else {
      // SQL: source_watermark = @W; NULL с любой стороны → не совпадает.
      const match = latest !== null && latest.status === 'COMPLETE' && latest.sourceWatermark !== null
        && p.sourceWatermark !== null && latest.sourceWatermark === p.sourceWatermark ? 1 : 0;
      active = freshStarted + match;
    }
    const prevComplete = [...mine].filter((r) => r.status === 'COMPLETE').sort((a, b) => b.startedAt - a.startedAt)[0];
    if (active === 0) {
      this.rows.push({
        environment: p.environment, loaderName: p.loaderName, logicalPeriod: p.logicalPeriod, runId: p.runId,
        executionId: p.executionId, status: 'STARTED', startedAt: p.nowMs, sourceWatermark: p.sourceWatermark ?? null,
      });
      return { acquired: true, runId: p.runId, recovered: latest !== null, ...(p.sourceWatermark !== undefined ? { previousWatermark: prevComplete?.sourceWatermark ?? null } : {}) };
    }
    if (latest?.status === 'STARTED' && latest.runId === p.runId) return { acquired: true, runId: p.runId, recovered: true };
    if (p.sourceWatermark === undefined) return { acquired: false, reason: latest?.status === 'COMPLETE' ? 'COMPLETE' : 'ALREADY_RUNNING' };
    return { acquired: false, reason: freshStarted > 0 ? 'ALREADY_RUNNING' : 'COMPLETE' };
  }
  async finalize(key: ManifestKey, runId: string, patch: FinalizePatch): Promise<void> {
    const r = this.rows.find((x) => x.runId === runId && x.logicalPeriod === key.logicalPeriod);
    if (r) r.status = patch.status as Row['status']; // source_watermark finalize не трогает
  }
}

const env = (o: Record<string, string> = {}): NodeJS.ProcessEnv =>
  ({ GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', LOG_LEVEL: 'error', GIT_SHA: 'sha', ENVIRONMENT: 'prod', ...o });
const argv = (l: string) => ['node', 'cli.js', l];
const W0 = 'fin:v2:100:' + '0'.repeat(64);
const W1 = 'fin:v2:101:' + '1'.repeat(64);
const PERIOD = LOADERS.mart.logicalPeriod();

function harness(store: MemWatermarkStore, probe: () => Promise<string>, nowMs = Date.parse('2026-10-13T06:00:00Z')) {
  const order: string[] = [];
  const handled: LoaderContext[] = [];
  const deps: CliDeps = {
    makeStore: () => {
      order.push('makeStore');
      return {
        acquire: async (p) => { order.push('acquire'); return store.acquire(p); },
        finalize: (k, r, patch) => store.finalize(k, r, patch),
      };
    },
    runHandler: async (_spec, ctx) => { order.push('handler'); handled.push(ctx); return { rowsFetched: 1, rowsLoaded: 1 }; },
    nowMs: () => nowMs,
    probeWatermark: async () => { order.push('probe'); return probe(); },
  };
  return { deps, order, handled };
}

const seed = (s: MemWatermarkStore, status: Row['status'], w: string | null, startedAt: number, runId = `seed-${s.rows.length}`) =>
  s.rows.push({ environment: 'prod', loaderName: 'mart', logicalPeriod: PERIOD, runId, executionId: runId, status, startedAt, sourceWatermark: w });

const NOW = Date.parse('2026-10-13T06:00:00Z');
const HOUR = 3600_000;

afterEach(() => vi.restoreAllMocks());

describe('матрица решений guard витрины', () => {
  it('ТА ЖЕ версия после COMPLETE → guard_skip, handler не вызван, строка не пишется', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 2 * HOUR);
    const h = harness(s, async () => W0);
    expect(await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'wb-mart-prod-b' }), h.deps)).toBe(EXIT_OK);
    expect(h.order).not.toContain('handler');
    expect(s.rows).toHaveLength(1);
  });

  it('ИЗМЕНИЛАСЬ версия → пересборка того же D-1 с НОВЫМ run_id; W1 записан и пережил COMPLETE', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 2 * HOUR, `prod:mart:${PERIOD}:sha:wb-mart-prod-a`);
    const h = harness(s, async () => W1);
    expect(await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'wb-mart-prod-b' }), h.deps)).toBe(EXIT_OK);
    expect(h.handled).toHaveLength(1);
    expect(h.handled[0]!.runId).toBe(`prod:mart:${PERIOD}:sha:wb-mart-prod-b`);
    expect(h.handled[0]!.targetDate).toBe(PERIOD);
    const last = s.rows[s.rows.length - 1]!;
    expect(last).toMatchObject({ status: 'COMPLETE', sourceWatermark: W1 });
  });

  it('legacy COMPLETE с NULL версией НЕ блокирует (одна догоняющая сборка после выкатки), затем skip', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', null, NOW - 2 * HOUR);
    const h1 = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h1.deps);
    expect(h1.handled).toHaveLength(1);
    const h2 = harness(s, async () => W0, NOW + HOUR);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e2' }), h2.deps);
    expect(h2.handled).toHaveLength(0);
  });

  it('ОТКАЗ зонда → версия неизвестна: сборка НЕ пропускается, пишется NULL, ERROR с кодом', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 2 * HOUR);
    const err = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, async () => { throw new Error('Access Denied: Table wb_raw.RAW_WB_FINANCE'); });
    expect(await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1', LOG_LEVEL: 'error' }), h.deps)).toBe(EXIT_OK);
    expect(h.handled).toHaveLength(1);
    expect(s.acquires[0]!.sourceWatermark).toBeNull();
    expect(s.rows[s.rows.length - 1]).toMatchObject({ status: 'COMPLETE', sourceWatermark: null });
    const lines = err.mock.calls.map((c) => JSON.parse(String(c[0])) as Record<string, unknown>);
    const ev = lines.find((l) => l.message === 'source_watermark_probe_failed');
    expect(ev).toMatchObject({ severity: 'ERROR', code: MART_SOURCE_WATERMARK_PROBE_FAILED });
    expect(String(ev!.code)).not.toBe('');
    // после NULL следующий слот с известной версией снова собирает (NULL не покрывает)
    const h2 = harness(s, async () => W0, NOW + HOUR);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e2' }), h2.deps);
    expect(h2.handled).toHaveLength(1);
  });

  it('ОТКАЗ зонда без COMPLETE → обычная первая сборка (как раньше)', async () => {
    const s = new MemWatermarkStore();
    vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, async () => { throw new Error('boom'); });
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.handled).toHaveLength(1);
  });

  it('пустая строка версии = отказ зонда, а не версия', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 2 * HOUR);
    vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, async () => '');
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(s.acquires[0]!.sourceWatermark).toBeNull();
    expect(h.handled).toHaveLength(1);
  });

  it('свежий STARTED (любая версия) → ALREADY_RUNNING, lease не изменился', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'STARTED', W0, NOW - 5 * 60_000);
    const h = harness(s, async () => W1);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.handled).toHaveLength(0);
    expect(s.rows).toHaveLength(1);
  });

  it('устаревший STARTED → рекавери новой строкой, даже при той же версии (сборка могла опубликовать)', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 3 * HOUR);
    seed(s, 'STARTED', W0, NOW - 2 * HOUR);
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.handled).toHaveLength(1);
  });

  it('последняя строка ERROR при той же версии, что у прошлого COMPLETE → пересборка (ERROR мог опубликовать FACT)', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 3 * HOUR);
    seed(s, 'ERROR', W1, NOW - 2 * HOUR);
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.handled).toHaveLength(1);
  });

  it('A→B→A между слотами: блокирует только ПОСЛЕДНИЙ COMPLETE, поэтому возврат к A пересобирается', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 3 * HOUR);
    seed(s, 'COMPLETE', W1, NOW - 2 * HOUR);
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.handled).toHaveLength(1);
  });

  it('другой logical_period не влияет (новые сутки — всегда сборка)', async () => {
    const s = new MemWatermarkStore();
    s.rows.push({ environment: 'prod', loaderName: 'mart', logicalPeriod: '2000-01-01', runId: 'x', executionId: 'x', status: 'COMPLETE', startedAt: NOW - HOUR, sourceWatermark: W0 });
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.handled).toHaveLength(1);
  });
});

describe('проводка cli', () => {
  it('mart: версия считается ДО acquire и ровно один раз; передаётся в acquire', async () => {
    const s = new MemWatermarkStore();
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.order.filter((x) => x === 'probe')).toHaveLength(1);
    expect(h.order.indexOf('probe')).toBeLessThan(h.order.indexOf('acquire'));
    expect(h.order.indexOf('acquire')).toBeLessThan(h.order.indexOf('handler'));
    expect(s.acquires[0]!.sourceWatermark).toBe(W0);
  });

  it('прочие загрузчики: зонд не зовётся, ключа sourceWatermark в params НЕТ, «любой COMPLETE блокирует» как раньше', async () => {
    for (const name of Object.keys(LOADERS).filter((n) => n !== 'mart')) {
      const s = new MemWatermarkStore();
      const h = harness(s, async () => W0);
      await runCli(argv(name), env({ CLOUD_RUN_EXECUTION: 'e1', DRY_RUN: '0' }), {
        ...h.deps, runHandler: async () => ({ rowsFetched: 0, rowsLoaded: 0 }),
      });
      expect(h.order, name).not.toContain('probe');
      for (const p of s.acquires) expect(Object.prototype.hasOwnProperty.call(p, 'sourceWatermark'), name).toBe(false);
    }
  });

  it('DRY_RUN=1 mart: ни зонда, ни acquire, ни handler', async () => {
    const s = new MemWatermarkStore();
    const h = harness(s, async () => W0);
    expect(await runCli(argv('mart'), env({ DRY_RUN: '1' }), h.deps)).toBe(EXIT_OK);
    expect(h.order).toEqual([]);
  });

  it('mart вне prod: отказ до зонда', async () => {
    const s = new MemWatermarkStore();
    vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ ENVIRONMENT: 'shadow' }), h.deps);
    expect(h.order).toEqual([]);
  });

  it('guard_acquired несёт версию и признак продвижения; guard_skip — версию', async () => {
    const s = new MemWatermarkStore();
    seed(s, 'COMPLETE', W0, NOW - 2 * HOUR);
    const out = vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
    const h = harness(s, async () => W1);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1', LOG_LEVEL: 'info' }), h.deps);
    const lines = out.mock.calls.map((c) => JSON.parse(String(c[0])) as Record<string, unknown>);
    expect(lines.find((l) => l.message === 'guard_acquired')).toMatchObject({
      sourceWatermark: W1, previousWatermark: W0, watermarkAdvanced: true,
    });
    const h2 = harness(s, async () => W1, NOW + HOUR);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e2', LOG_LEVEL: 'info' }), h2.deps);
    const lines2 = out.mock.calls.map((c) => JSON.parse(String(c[0])) as Record<string, unknown>);
    expect(lines2.find((l) => l.message === 'guard_skip')).toMatchObject({ reason: 'COMPLETE', sourceWatermark: W1 });
  });
});
