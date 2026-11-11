import { describe, it, expect, vi, afterEach } from 'vitest';
import { runCli, type CliDeps } from '../src/cli.js';
import { EXIT_OK, EXIT_ERROR, LoaderError } from '../src/errors.js';
import type { ManifestStore, FinalizePatch } from '../src/bq/runManifest.js';
import type { LoaderContext } from '../src/loaders/types.js';
import { LOADERS } from '../src/loaders/registry.js';
import { LOADER_STREAM, STREAMS, gatedStreamOfLoader, loadersOfStream, streamOfLoader } from '../src/ownership/streams.js';
import {
  BqOwnershipReader, checkWriterGate, parseSnapshot, WRITER_EPOCH_CHANGED, WRITER_GATE_UNAVAILABLE, WRITER_NOT_OWNER,
  type OwnershipReader, type OwnershipSnapshot,
} from '../src/ownership/gate.js';
import { PROFILES } from '../src/loaders/cutover/profiles.js';
import { fixedOwner } from './ownership_fixture.js';

/**
 * Этап C — writer-ownership gate в cli.ts. Проверяется на поддельных зависимостях (без облака):
 * неправообладатель не трогает ни lease (LOADER_RUNS), ни handler; отказ чтения — fail-closed;
 * смена epoch во время прогона не даёт записать COMPLETE.
 */

const argv = (loader: string) => ['node', 'cli.js', loader];
const env = (o: Record<string, string> = {}) =>
  // Остатки — единственный поток, чей загрузчик есть в базовой ветке; его gate включается явно (M1, §8).
  ({ GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', LOG_LEVEL: 'info', GIT_SHA: 'sha', ENVIRONMENT: 'prod', CLOUD_RUN_EXECUTION: 'e1', WRITER_GATE_STOCKS: '1', ...o });

let lines: Array<Record<string, unknown>> = [];
function capture() {
  lines = [];
  const grab = (chunk: unknown) => {
    for (const l of String(chunk).split('\n').filter(Boolean)) {
      try { lines.push(JSON.parse(l) as Record<string, unknown>); } catch { /* не JSON */ }
    }
    return true;
  };
  vi.spyOn(process.stdout, 'write').mockImplementation(grab as typeof process.stdout.write);
  vi.spyOn(process.stderr, 'write').mockImplementation(grab as typeof process.stderr.write);
}
afterEach(() => vi.restoreAllMocks());

function harness(reader: OwnershipReader | (() => OwnershipReader), handler?: (ctx: LoaderContext) => Promise<{ rowsFetched: number; rowsLoaded: number }>) {
  const calls = { makeStore: 0, acquire: 0, handler: 0, reader: 0 };
  const finals: FinalizePatch[] = [];
  const store: ManifestStore = {
    acquire: async (p) => { calls.acquire++; return { acquired: true, runId: p.runId, recovered: false }; },
    finalize: async (_k, _r, patch) => { finals.push(patch); },
  };
  const ctxs: LoaderContext[] = [];
  const deps: CliDeps = {
    makeStore: () => { calls.makeStore++; return store; },
    runHandler: async (_s, ctx) => { calls.handler++; ctxs.push(ctx); return handler ? handler(ctx) : { rowsFetched: 1, rowsLoaded: 1 }; },
    nowMs: () => 0,
    sleep: async () => {},
    makeOwnershipReader: () => {
      calls.reader++;
      return typeof reader === 'function' ? reader() : reader;
    },
  };
  return { calls, finals, ctxs, deps };
}

/** Читатель, который отдаёт снимки по очереди (последний повторяется). */
function sequence(...snaps: Array<OwnershipSnapshot | null | Error>): OwnershipReader {
  let i = 0;
  return {
    read: async () => {
      const s = snaps[Math.min(i++, snaps.length - 1)];
      if (s instanceof Error) throw s;
      return s ?? null;
    },
  };
}
const CR = (epoch = 3): OwnershipSnapshot => ({ owner: 'CLOUD_RUN', epoch, state: 'MONITORING' });

describe('карта загрузчик → поток', () => {
  it('ровно 11 загрузчиков шести потоков, как в задаче этапа C', () => {
    expect(LOADER_STREAM).toEqual({
      stocks: 'stocks',
      orders: 'statistics', sales: 'statistics', 'sales-reconcile': 'statistics',
      ads: 'ads', 'ads-query': 'ads', 'ads-costs-audit': 'ads',
      finance: 'finance', 'finance-backfill': 'finance',
      supplies: 'supplies',
      'ref-sync': 'ref',
    });
    expect([...STREAMS].sort()).toEqual(['ads', 'finance', 'ref', 'statistics', 'stocks', 'supplies']);
  });

  it('профили координатора покрывают ровно загрузчики потока', () => {
    for (const s of STREAMS) expect([...PROFILES[s].cloudLoaders].sort()).toEqual(loadersOfStream(s));
  });

  it('прочие загрузчики (mart, prices, unitka, cutover…) под gate не попадают', () => {
    for (const name of ['mart', 'prices', 'promo', 'tariffs', 'funnel', 'storage', 'unitka', 'ozon-unitka', 'cutover', 'noop', 'constructor', '__proto__']) {
      expect(streamOfLoader(name)).toBeUndefined();
    }
  });
});

describe('остатки (M1): gate выключен по умолчанию', () => {
  it('без WRITER_GATE_STOCKS=1 stocks не проверяется — ручная процедура MIG1B работает как раньше', async () => {
    const h = harness(fixedOwner({ owner: 'APPS_SCRIPT', epoch: 1, state: 'LEGACY_ACTIVE' }));
    expect(await runCli(argv('stocks'), env({ WRITER_GATE_STOCKS: '' }), h.deps)).toBe(EXIT_OK);
    expect(h.calls).toMatchObject({ reader: 0, handler: 1 });
    expect(h.ctxs[0]!.writerFence).toBeUndefined();
    expect(gatedStreamOfLoader('stocks', {})).toBeUndefined();
    expect(gatedStreamOfLoader('stocks', { WRITER_GATE_STOCKS: '1' })).toBe('stocks');
    expect(gatedStreamOfLoader('orders', {})).toBe('statistics');
  });
});

describe('gate в cli.ts (только prod, до lease и handler)', () => {
  for (const owner of ['APPS_SCRIPT', 'NONE'] as const) {
    it(`owner = ${owner}: exit 0, ни lease, ни LOADER_RUNS, ни handler; writer_fenced / WRITER_NOT_OWNER`, async () => {
      capture();
      const h = harness(fixedOwner({ owner, epoch: 2, state: owner === 'NONE' ? 'LEGACY_FENCED' : 'LEGACY_ACTIVE' }));
      expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_OK);
      expect(h.calls).toMatchObject({ makeStore: 0, acquire: 0, handler: 0 });
      expect(h.finals).toEqual([]);
      const fenced = lines.find((l) => l.message === 'writer_fenced');
      expect(fenced).toMatchObject({ severity: 'INFO', code: WRITER_NOT_OWNER, stream: 'stocks', owner, epoch: 2 });
      expect(lines.some((l) => l.severity === 'ERROR')).toBe(false);
    });
  }

  it('DRY_RUN в prod тоже под gate: неправообладатель не исполняет handler', async () => {
    capture();
    const h = harness(fixedOwner({ owner: 'APPS_SCRIPT', epoch: 1, state: 'LEGACY_ACTIVE' }));
    expect(await runCli(argv('stocks'), env({ DRY_RUN: '1' }), h.deps)).toBe(EXIT_OK);
    expect(h.calls.handler).toBe(0);
  });

  const unavailable: Array<[string, OwnershipReader | (() => OwnershipReader)]> = [
    ['ошибка чтения BigQuery', fixedOwner(new Error('Access Denied: Table wb_ops.INGEST_WRITER_OWNERSHIP'))],
    ['нет строки потока', fixedOwner(null)],
    ['не создаётся читатель', () => { throw new Error('нет учётных данных'); }],
  ];
  for (const [name, reader] of unavailable) {
    it(`fail-closed: ${name} → WRITER_GATE_UNAVAILABLE, exit 1, ничего не пишется`, async () => {
      capture();
      const h = harness(reader);
      expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_ERROR);
      expect(h.calls).toMatchObject({ makeStore: 0, acquire: 0, handler: 0 });
      const rec = lines.find((l) => l.message === 'writer_gate_unavailable');
      expect(rec).toMatchObject({ severity: 'ERROR', code: WRITER_GATE_UNAVAILABLE, stream: 'stocks' });
    });
  }

  it('мусор в строке (owner/epoch) — тоже fail-closed', async () => {
    expect(() => parseSnapshot('stocks', { owner: 'cloud_run', epoch: 1 })).toThrow(/owner/);
    expect(() => parseSnapshot('stocks', { owner: 'CLOUD_RUN', epoch: 0 })).toThrow(/epoch/);
    expect(() => parseSnapshot('stocks', { owner: 'CLOUD_RUN', epoch: '1.5' })).toThrow(/epoch/);
    expect(parseSnapshot('stocks', { owner: 'CLOUD_RUN', epoch: '7', state: 'MONITORING' })).toEqual({ owner: 'CLOUD_RUN', epoch: 7, state: 'MONITORING' });
    const bad: OwnershipReader = { read: async () => parseSnapshot('ads', { owner: 'X', epoch: 1 }) };
    expect((await checkWriterGate(bad, 'ads')).kind).toBe('UNAVAILABLE');
  });

  it('две строки потока — отказ BqOwnershipReader (fail-closed)', async () => {
    const r = new BqOwnershipReader({ projectId: 'p', query: async () => [{ owner: 'CLOUD_RUN', epoch: 1 }, { owner: 'APPS_SCRIPT', epoch: 1 }] } as never, 'wb_ops');
    expect((await checkWriterGate(r, 'stocks')).kind).toBe('UNAVAILABLE');
  });

  it('BqOwnershipReader: один параметризованный SELECT по своему потоку', async () => {
    const seen: Array<{ sql: string; params: unknown }> = [];
    const r = new BqOwnershipReader({ projectId: 'p', query: async (sql: string, params: unknown) => { seen.push({ sql, params }); return [{ owner: 'CLOUD_RUN', epoch: 4, state: 'COMPLETED' }]; } } as never, 'wb_ops');
    expect(await r.read('finance', 'WRITER_GATE:CR:finance:finance')).toEqual({ owner: 'CLOUD_RUN', epoch: 4, state: 'COMPLETED' });
    expect(seen).toHaveLength(1);
    expect(seen[0]!.sql).toBe('/* WRITER_GATE:CR:finance:finance:v1 */ SELECT owner, epoch, state FROM `p.wb_ops.INGEST_WRITER_OWNERSHIP` WHERE stream = @stream');
    await expect(r.read('finance', "x' OR 1=1")).rejects.toThrow(/метка/);
    expect(seen[0]!.params).toEqual({ stream: 'finance' });
  });

  it('shadow gate не проверяет (пишет только __CR)', async () => {
    const h = harness(fixedOwner(new Error('не должен вызываться')));
    expect(await runCli(argv('stocks'), env({ ENVIRONMENT: 'shadow' }), h.deps)).toBe(EXIT_OK);
    expect(h.calls.reader).toBe(0);
    expect(h.calls.handler).toBe(1);
    expect(h.ctxs[0]!.writerFence).toBeUndefined();
  });

  it('загрузчик вне потоков (prices) в prod gate не проверяет', async () => {
    const h = harness(fixedOwner(new Error('не должен вызываться')));
    expect(await runCli(argv('prices'), env(), h.deps)).toBe(EXIT_OK);
    expect(h.calls.reader).toBe(0);
  });

  it('owner = CLOUD_RUN: прогон идёт, handler получает fencing-токен, COMPLETE после повторной проверки', async () => {
    const reads: number[] = [];
    let n = 0;
    const reader: OwnershipReader = { read: async () => { reads.push(++n); return CR(5); } };
    const h = harness(reader);
    expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_OK);
    expect(h.ctxs[0]!.writerFence).toMatchObject({ stream: 'stocks', epoch: 5 });
    expect(reads).toEqual([1, 2]); // вход + проверка перед COMPLETE
    expect(h.finals).toEqual([expect.objectContaining({ status: 'COMPLETE' })]);
  });

  const changed: Array<[string, OwnershipSnapshot]> = [
    ['epoch вырос (rollback и возврат)', CR(6)],
    ['владелец сменился на NONE (fence отката)', { owner: 'NONE', epoch: 6, state: 'CLOUD_FENCED' }],
    ['владелец сменился на APPS_SCRIPT', { owner: 'APPS_SCRIPT', epoch: 7, state: 'LEGACY_ACTIVE' }],
  ];
  for (const [name, after] of changed) {
    it(`${name} во время прогона → WRITER_EPOCH_CHANGED: COMPLETE не пишется, lease закрыт ERROR, exit 1`, async () => {
      capture();
      const h = harness(sequence(CR(5), after));
      expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_ERROR);
      expect(h.finals).toHaveLength(1);
      expect(h.finals[0]).toMatchObject({ status: 'ERROR', errorCode: WRITER_EPOCH_CHANGED });
      expect(h.finals.some((f) => f.status === 'COMPLETE')).toBe(false);
      // loader_failed: поле ctx `message` (текст ошибки) затирает имя события — ищем по коду и стадии.
      expect(lines.find((l) => l.code === WRITER_EPOCH_CHANGED && l.stage === 'pre_complete')).toMatchObject({ severity: 'ERROR' });
    });
  }

  it('повторная проверка не читается → WRITER_GATE_UNAVAILABLE, COMPLETE не пишется', async () => {
    const h = harness(sequence(CR(5), new Error('backendError')));
    expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_ERROR);
    expect(h.finals).toEqual([expect.objectContaining({ status: 'ERROR', errorCode: WRITER_GATE_UNAVAILABLE })]);
  });

  it('загрузчик сам зовёт assertEpoch перед своим commit-маркером: epoch сменился → маркер не пишется', async () => {
    let markerWritten = false;
    const h = harness(sequence(CR(5), CR(6)), async (ctx) => {
      await ctx.writerFence?.assertEpoch('header_commit');
      markerWritten = true;
      return { rowsFetched: 1, rowsLoaded: 1 };
    });
    expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_ERROR);
    expect(markerWritten).toBe(false);
    expect(h.finals[0]).toMatchObject({ status: 'ERROR', errorCode: WRITER_EPOCH_CHANGED });
  });

  it('assertEpoch бросает LoaderError с кодом — classifyFailure его не повторяет', async () => {
    const h = harness(sequence(CR(5), CR(6)), async (ctx) => {
      await ctx.writerFence!.assertEpoch('x');
      return { rowsFetched: 0, rowsLoaded: 0 };
    });
    await runCli(argv('stocks'), env(), h.deps);
    expect(h.calls.handler).toBe(1);
    await expect(sequence(CR(1), CR(2)).read('ads')).resolves.toMatchObject({ epoch: 1 });
    expect(new LoaderError('x', WRITER_EPOCH_CHANGED).code).toBe(WRITER_EPOCH_CHANGED);
  });

  it('каждый загрузчик потока, который есть в реестре, проходит через gate', async () => {
    for (const name of Object.keys(LOADER_STREAM)) {
      if (!LOADERS[name]) continue; // загрузчики M2–M5 живут в ветках потоков
      const h = harness(fixedOwner({ owner: 'APPS_SCRIPT', epoch: 1, state: 'LEGACY_ACTIVE' }));
      capture();
      expect(await runCli(argv(name), env(), h.deps)).toBe(EXIT_OK);
      expect(h.calls.handler).toBe(0);
      vi.restoreAllMocks();
    }
  });
});

describe('cutover — загрузчик плоскости управления', () => {
  it('без lease и без LOADER_RUNS; только prod', async () => {
    const h = harness(fixedOwner(new Error('gate не нужен')));
    expect(await runCli(argv('cutover'), env(), h.deps)).toBe(EXIT_OK);
    expect(h.calls).toMatchObject({ makeStore: 0, acquire: 0, handler: 1, reader: 0 });
    capture();
    const s = harness(fixedOwner(null));
    expect(await runCli(argv('cutover'), env({ ENVIRONMENT: 'shadow' }), s.deps)).toBe(EXIT_ERROR);
    expect(s.calls.handler).toBe(0);
    expect(LOADERS.cutover).toMatchObject({ prodOnly: true, controlPlane: true });
  });

  it('отказ handler → exit 1 с кодом', async () => {
    capture();
    const h = harness(fixedOwner(null), async () => { throw new LoaderError('нужен человек', 'CUTOVER_ATTENTION'); });
    expect(await runCli(argv('cutover'), env(), h.deps)).toBe(EXIT_ERROR);
    expect(lines.find((l) => l.code === 'CUTOVER_ATTENTION')).toMatchObject({ severity: 'ERROR', stage: 'handler' });
  });
});

describe('условный DML Cloud Run', () => {
  it('ownershipAssertSql: ASSERT по владельцу и epoch, литералы проверены', async () => {
    const { ownershipAssertSql } = await import('../src/ownership/gate.js');
    const sql = ownershipAssertSql('proj-1', 'wb_ops', { stream: 'finance', epoch: 7 });
    expect(sql).toBe("ASSERT (SELECT COUNT(*) FROM `proj-1.wb_ops.INGEST_WRITER_OWNERSHIP` WHERE stream = 'finance' AND owner = 'CLOUD_RUN' AND epoch = 7) = 1 AS 'WRITER_EPOCH_CHANGED: владение потоком finance (epoch 7) утрачено — DML не применён'");
    expect(() => ownershipAssertSql('p', 'wb_ops', { stream: "x' OR 1=1 --" as never, epoch: 1 })).toThrow();
    expect(() => ownershipAssertSql('p', 'wb_ops', { stream: 'ads', epoch: 0 })).toThrow();
  });
});

describe('конкурентность Cloud Run: истечение lease и смена поколения во время загрузки', () => {
  it('зависший прогон эпохи 3 и новый после отката: новый отсечён до lease, старый не коммитит', async () => {
    capture();
    let reg: OwnershipSnapshot = CR(3);
    const reader: OwnershipReader = { read: async () => reg };
    let release!: () => void;
    const loading = new Promise<void>((r) => { release = r; });
    const a = harness(reader, async () => { await loading; return { rowsFetched: 1, rowsLoaded: 1 }; });
    const runA = runCli(argv('stocks'), env({ CLOUD_RUN_EXECUTION: 'a' }), a.deps);
    await new Promise((r) => setTimeout(r, 5));
    // Пока A «грузит» (между подготовкой и commit), координатор отсекает Cloud Run: NONE, epoch 4.
    reg = { owner: 'NONE', epoch: 4, state: 'CLOUD_FENCED' };
    // Lease A «протух» — новое исполнение B его бы перехватило, но gate отсекает B раньше lease.
    const b = harness(reader);
    expect(await runCli(argv('stocks'), env({ CLOUD_RUN_EXECUTION: 'b' }), b.deps)).toBe(EXIT_OK);
    expect(b.calls).toMatchObject({ makeStore: 0, acquire: 0, handler: 0 });
    release();
    expect(await runA).toBe(EXIT_ERROR);
    expect(a.finals).toEqual([expect.objectContaining({ status: 'ERROR', errorCode: WRITER_EPOCH_CHANGED })]);
  });

  it('тот же владелец и поколение: перехват протухшего lease — штатный (один писатель)', async () => {
    const h = harness(fixedOwner(CR(3)));
    h.deps.makeStore = () => ({
      acquire: async (p) => ({ acquired: true, runId: p.runId, recovered: true }),
      finalize: async (_k, _r, patch) => { h.finals.push(patch); },
    });
    expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_OK);
    expect(h.finals).toEqual([expect.objectContaining({ status: 'COMPLETE' })]);
  });
});


describe('метки запросов gate Cloud Run (доказательство для координатора)', () => {
  it('вход — WRITER_GATE:CR:<поток>:<загрузчик>, перечитка перед COMPLETE — WRITER_RECHECK (доказательством не считается)', async () => {
    const marks: Array<string | undefined> = [];
    const reader: OwnershipReader = { read: async (_s, m) => { marks.push(m); return CR(5); } };
    const h = harness(reader);
    expect(await runCli(argv('stocks'), env(), h.deps)).toBe(EXIT_OK);
    expect(marks).toEqual(['WRITER_GATE:CR:stocks:stocks', 'WRITER_RECHECK:CR:stocks:stocks']);
  });
});
