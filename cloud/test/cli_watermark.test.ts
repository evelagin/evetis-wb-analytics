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
      const rebuild = p.sourceWatermark !== undefined && latest?.status === 'COMPLETE'
        ? { rebuildReason: p.sourceWatermark === null ? 'SOURCE_VERSION_UNKNOWN' as const
            : latest.sourceWatermark === null ? 'PREVIOUS_VERSION_UNKNOWN' as const : 'SOURCE_CHANGED' as const }
        : {};
      this.rows.push({
        environment: p.environment, loaderName: p.loaderName, logicalPeriod: p.logicalPeriod, runId: p.runId,
        executionId: p.executionId, status: 'STARTED', startedAt: p.nowMs, sourceWatermark: p.sourceWatermark ?? null,
      });
      const recovered = p.sourceWatermark === undefined ? latest !== null : latest !== null && latest.status !== 'COMPLETE';
      return { acquired: true, runId: p.runId, recovered, ...(p.sourceWatermark !== undefined ? { previousWatermark: prevComplete?.sourceWatermark ?? null } : {}), ...rebuild };
    }
    if (latest?.status === 'STARTED' && latest.runId === p.runId) return { acquired: true, runId: p.runId, recovered: true };
    if (p.sourceWatermark === undefined) return { acquired: false, reason: latest?.status === 'COMPLETE' ? 'COMPLETE' : 'ALREADY_RUNNING' };
    return { acquired: false, reason: freshStarted > 0 ? 'ALREADY_RUNNING' : 'COMPLETE' };
  }
  async finalize(key: ManifestKey, runId: string, patch: FinalizePatch): Promise<void> {
    const r = this.rows.find((x) => x.runId === runId && x.logicalPeriod === key.logicalPeriod);
    if (!r) return;
    this.finalizes.push(patch);
    r.status = patch.status as Row['status'];
    // как SQL: колонка пишется в том же UPDATE только при наличии ключа
    if (patch.sourceWatermark !== undefined) r.sourceWatermark = patch.sourceWatermark;
  }
  finalizes: FinalizePatch[] = [];
}

const env = (o: Record<string, string> = {}): NodeJS.ProcessEnv =>
  ({ GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', LOG_LEVEL: 'error', GIT_SHA: 'sha', ENVIRONMENT: 'prod', ...o });
const argv = (l: string) => ['node', 'cli.js', l];
const W0 = 'fin:v2:100:' + '0'.repeat(64);
const W1 = 'fin:v2:101:' + '1'.repeat(64);
const PERIOD = LOADERS.mart.logicalPeriod();

/** probe: одна функция на оба зонда (до lease и после сборки) или массив ответов по очереди. */
function harness(store: MemWatermarkStore, probeArg: (() => Promise<string>) | Array<() => Promise<string>>, nowMs = Date.parse('2026-10-13T06:00:00Z')) {
  let call = 0;
  const probe = () => (Array.isArray(probeArg) ? probeArg[Math.min(call++, probeArg.length - 1)]!() : probeArg());
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

describe('подтверждение версии после сборки (P3-1: устаревшая сборка не объявляется актуальной)', () => {
  const lines = (spy: { mock: { calls: unknown[][] } }) => spy.mock.calls.map((c) => JSON.parse(String(c[0])) as Record<string, unknown>);

  it('W1 = W0 → COMPLETE с W0, finalize пишет версию в том же UPDATE', async () => {
    const s = new MemWatermarkStore();
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(s.finalizes).toEqual([{ status: 'COMPLETE', rowsFetched: 1, rowsLoaded: 1, sourceWatermark: W0 }]);
    expect(s.rows[0]).toMatchObject({ status: 'COMPLETE', sourceWatermark: W0 });
  });

  it('W1 ≠ W0 (финансы пришли во время сборки) → COMPLETE с NULL, WARNING с кодом; следующий слот пересобирает', async () => {
    const s = new MemWatermarkStore();
    const err = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, [async () => W0, async () => W1]);
    expect(await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1', LOG_LEVEL: 'info' }), h.deps)).toBe(EXIT_OK);
    expect(s.rows[0]).toMatchObject({ status: 'COMPLETE', sourceWatermark: null });
    expect(lines(err).find((l) => l.message === 'source_changed_during_build')).toMatchObject({
      severity: 'WARN', code: 'MART_SOURCE_CHANGED_DURING_BUILD', sourceWatermark: W0, sourceWatermarkAfterBuild: W1,
    });
    const h2 = harness(s, async () => W1, NOW + HOUR);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e2' }), h2.deps);
    expect(h2.handled).toHaveLength(1);
    expect(s.rows[1]).toMatchObject({ status: 'COMPLETE', sourceWatermark: W1 });
  });

  it('повторный зонд упал → COMPLETE с NULL, ERROR с кодом и stage=post_build', async () => {
    const s = new MemWatermarkStore();
    const err = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, [async () => W0, async () => { throw new Error('quota'); }]);
    expect(await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps)).toBe(EXIT_OK);
    expect(s.rows[0]).toMatchObject({ status: 'COMPLETE', sourceWatermark: null });
    expect(lines(err).find((l) => l.message === 'source_watermark_probe_failed')).toMatchObject({
      severity: 'ERROR', code: MART_SOURCE_WATERMARK_PROBE_FAILED, stage: 'post_build',
    });
  });

  it('версия до сборки неизвестна → повторного зонда нет, COMPLETE с NULL', async () => {
    const s = new MemWatermarkStore();
    vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, async () => { throw new Error('boom'); });
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.order.filter((x) => x === 'probe')).toHaveLength(1);
    expect(s.rows[0]).toMatchObject({ status: 'COMPLETE', sourceWatermark: null });
  });

  it('сборка упала → повторного зонда нет; finalize(ERROR) без ключа версии (W0 строки STARTED сохраняется)', async () => {
    const s = new MemWatermarkStore();
    vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), {
      ...h.deps, runHandler: async () => { h.order.push('handler'); throw new Error('FRESHNESS_GATE'); },
    });
    expect(h.order.filter((x) => x === 'probe')).toHaveLength(1);
    expect(s.finalizes).toHaveLength(1);
    expect(s.finalizes[0]!.status).toBe('ERROR');
    expect(Object.prototype.hasOwnProperty.call(s.finalizes[0], 'sourceWatermark')).toBe(false);
    expect(s.rows[0]).toMatchObject({ status: 'ERROR', sourceWatermark: W0 });
  });

  it('прочие загрузчики: finalize без ключа версии (SQL finalize прежний)', async () => {
    const s = new MemWatermarkStore();
    const h = harness(s, async () => W0);
    await runCli(argv('stocks'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(s.finalizes).toHaveLength(1);
    expect(Object.prototype.hasOwnProperty.call(s.finalizes[0], 'sourceWatermark')).toBe(false);
  });
});

describe('P3-6: пересборка по новой версии ≠ восстановление зависшего прогона', () => {
  const acquiredLog = async (seedFn: (s: MemWatermarkStore) => void, w: string) => {
    const s = new MemWatermarkStore();
    seedFn(s);
    const out = vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1', LOG_LEVEL: 'info' }), harness(s, async () => w).deps);
    const l = out.mock.calls.map((c) => JSON.parse(String(c[0])) as Record<string, unknown>).find((x) => x.message === 'guard_acquired');
    out.mockRestore();
    return l!;
  };
  it('COMPLETE(W0) + W1 → recovered=false, rebuildReason=SOURCE_CHANGED', async () => {
    expect(await acquiredLog((s) => seed(s, 'COMPLETE', W0, NOW - HOUR), W1)).toMatchObject({ recovered: false, rebuildReason: 'SOURCE_CHANGED' });
  });
  it('legacy COMPLETE(NULL) + W0 → recovered=false, rebuildReason=PREVIOUS_VERSION_UNKNOWN', async () => {
    expect(await acquiredLog((s) => seed(s, 'COMPLETE', null, NOW - HOUR), W0)).toMatchObject({ recovered: false, rebuildReason: 'PREVIOUS_VERSION_UNKNOWN' });
  });
  it('устаревший STARTED → recovered=true, rebuildReason нет', async () => {
    const l = await acquiredLog((s) => seed(s, 'STARTED', W0, NOW - 2 * HOUR), W0);
    expect(l).toMatchObject({ recovered: true });
    expect(l.rebuildReason).toBeUndefined();
  });
  it('первый прогон периода → recovered=false, rebuildReason нет', async () => {
    const l = await acquiredLog(() => {}, W0);
    expect(l).toMatchObject({ recovered: false });
    expect(l.rebuildReason).toBeUndefined();
  });
});

describe('проводка cli', () => {
  it('mart: зонд ДО acquire (W0) и повторно ПОСЛЕ сборки, до finalize; W0 передаётся в acquire', async () => {
    const s = new MemWatermarkStore();
    const h = harness(s, async () => W0);
    await runCli(argv('mart'), env({ CLOUD_RUN_EXECUTION: 'e1' }), h.deps);
    expect(h.order).toEqual(['makeStore', 'probe', 'acquire', 'handler', 'probe']);
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
