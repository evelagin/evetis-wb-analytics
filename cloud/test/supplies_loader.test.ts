import { describe, it, expect, vi, afterEach } from 'vitest';
import type { JobLoadMetadata } from '@google-cloud/bigquery';
import { suppliesLoader, type SuppliesDeps, type SuppliesStore } from '../src/loaders/supplies/index.js';
import { assertSuppliesTargets, suppliesEnv } from '../src/loaders/supplies/targets.js';
import { WbSuppliesApi, rowsOf, type Fetcher, type SuppliesApi } from '../src/loaders/supplies/wbApi.js';
import { SuppliesBq, suppliesJobId, type BqLike } from '../src/loaders/supplies/bq.js';
import { wbFetch } from '../src/http/wbHttp.js';
import { LoaderError } from '../src/errors.js';
import { classifyFailure } from '../src/failure.js';
import { WB_TOKEN_EXPIRING_SOON } from '../src/http/wbToken.js';
import { Logger } from '../src/logging.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';
import { SUPPLY_LIST, SUPPLY_CARD, SUPPLY_GOODS } from './supplies_fixture.js';
import type { KnownState } from '../src/loaders/supplies/normalize.js';

const NOW = Date.parse('2026-11-10T23:29:00Z');
const jwt = (expSec: number) => ['eyJhbGciOiJub25lIn0', Buffer.from(JSON.stringify({ exp: expSec })).toString('base64url'), 'sig'].join('.');
const GOOD_TOKEN = jwt(Math.floor(NOW / 1000) + 90 * 86400);

function ctx(environment: 'shadow' | 'prod', rawDataset = 'wb_raw'): LoaderContext {
  const config = { environment, rawDataset, projectId: 'p', bqLocation: 'EU', wbHttpTimeoutMs: 1000 } as unknown as Config;
  return { config, logger: new Logger({}, 'error'), logicalPeriod: '2026-11-11', runId: 'r', targetDate: '2026-11-11' };
}

class FakeApi implements SuppliesApi {
  calls: string[] = [];
  constructor(private readonly items = SUPPLY_LIST, private readonly failGoodsFor?: number) {}
  async list() { this.calls.push('list'); return this.items; }
  async card(id: number) { this.calls.push(`card:${id}`); return SUPPLY_CARD; }
  async goods(id: number) {
    this.calls.push(`goods:${id}`);
    if (id === this.failGoodsFor) throw new LoaderError('boom', 'WB_SUPPLIES_HTTP');
    return SUPPLY_GOODS;
  }
}

class FakeStore implements SuppliesStore {
  loads: Array<{ table: string; rows: Array<Record<string, unknown>>; jobId: string }> = [];
  knownFrom: string[] = [];
  constructor(private readonly known = new Map<string, KnownState>()) {}
  async knownUpdated(t: string) { this.knownFrom.push(t); return this.known; }
  async load(table: string, rows: object[], jobId: string) {
    this.loads.push({ table, rows: rows as Array<Record<string, unknown>>, jobId });
    return 'LOADED' as const;
  }
}

function deps(over: Partial<SuppliesDeps> & { api?: FakeApi; store?: FakeStore; token?: string } = {}): SuppliesDeps & { api: FakeApi; store: FakeStore; readToken: ReturnType<typeof vi.fn> } {
  const api = over.api ?? new FakeApi();
  const store = over.store ?? new FakeStore();
  const readToken = vi.fn(async () => over.token ?? GOOD_TOKEN);
  return {
    env: {}, readToken, makeApi: () => api, makeStore: () => store, nowMs: () => NOW,
    sleep: async () => undefined, pauseMs: 0, budgetMs: 60_000, chunk: 40, ...over, api, store,
  } as SuppliesDeps & { api: FakeApi; store: FakeStore; readToken: ReturnType<typeof vi.fn> };
}

describe('поставки — проверка контура до записи и до WB', () => {
  it('shadow по умолчанию пишет в __CR; prod по умолчанию отклоняется', () => {
    const e = suppliesEnv({});
    expect(e.headerTable).toBe('RAW_WB_SUPPLIES__CR');
    expect(() => assertSuppliesTargets({ environment: 'shadow', rawDataset: 'wb_raw' }, e)).not.toThrow();
    expect(() => assertSuppliesTargets({ environment: 'prod', rawDataset: 'wb_raw' }, e)).toThrow(/SUPPLIES_GOODS_TABLE|боевые/);
  });
  it('shadow с боевой таблицей и prod с __CR — отказ; чужой датасет — отказ', () => {
    const prodE = suppliesEnv({ SUPPLIES_HEADER_TABLE: 'RAW_WB_SUPPLIES', SUPPLIES_GOODS_TABLE: 'RAW_WB_SUPPLIES_GOODS' });
    expect(() => assertSuppliesTargets({ environment: 'prod', rawDataset: 'wb_raw' }, prodE)).not.toThrow();
    expect(() => assertSuppliesTargets({ environment: 'shadow', rawDataset: 'wb_raw' }, prodE)).toThrow(/__CR/);
    expect(() => assertSuppliesTargets({ environment: 'prod', rawDataset: 'ozon_raw' }, prodE)).toThrow(/wb_raw/);
    expect(() => assertSuppliesTargets({ environment: 'shadow', rawDataset: 'wb_raw' }, suppliesEnv({ SUPPLIES_GOODS_TABLE: 'X__CR' }))).toThrow(/__CR/);
  });
  it('нарушение контура — ни токена, ни WB, ни BigQuery', async () => {
    const d = deps({ env: { SUPPLIES_HEADER_TABLE: 'RAW_WB_SUPPLIES' } });
    await expect(suppliesLoader(ctx('shadow'), d)).rejects.toMatchObject({ code: 'SUPPLIES_TARGET_MISMATCH' });
    expect(d.readToken).not.toHaveBeenCalled();
    expect(d.api.calls).toEqual([]);
    expect(d.store.loads).toEqual([]);
  });
});

describe('поставки — токен (причина 100 % отказов Apps Script), общий http/wbToken.ts', () => {
  /** Синтетический JWT со «секретной» подписью-маркером: её не должно быть ни в ошибке, ни в логе. */
  const SECRET_SIG = 'c3ludGhldGljLXNpZ25hdHVyZS1kby1ub3QtbGVhaw';
  const synth = (expSec: number) => [
    Buffer.from(JSON.stringify({ alg: 'ES256', typ: 'JWT' })).toString('base64url'),
    Buffer.from(JSON.stringify({ exp: expSec, id: 'synthetic-supplies' })).toString('base64url'),
    SECRET_SIG,
  ].join('.');
  /** Лог в память: Logger пишет в stdout/stderr — перехватываем оба. */
  function captureLog(): { lines: string[]; restore: () => void } {
    const lines: string[] = [];
    const out = vi.spyOn(process.stdout, 'write').mockImplementation((c: string | Uint8Array) => { lines.push(String(c)); return true; });
    const err = vi.spyOn(process.stderr, 'write').mockImplementation((c: string | Uint8Array) => { lines.push(String(c)); return true; });
    return { lines, restore: () => { out.mockRestore(); err.mockRestore(); } };
  }
  function infoCtx(environment: 'shadow' | 'prod' = 'shadow'): LoaderContext {
    return { ...ctx(environment), logger: new Logger({}, 'debug') };
  }
  /** Настоящий WbSuppliesApi со шпионом-fetcher: считаем фактические HTTP-запросы. */
  function realApiDeps(token: string, fetcher: Fetcher) {
    const store = new FakeStore();
    const makeApi = vi.fn((host: string, tok: string, logger: Logger, secret: string) =>
      new WbSuppliesApi(host, tok, { timeoutMs: 10, maxRetries: 3 }, logger, fetcher, secret));
    const d: SuppliesDeps = {
      env: {}, readToken: async () => token, makeApi, makeStore: () => store, nowMs: () => NOW,
      sleep: async () => undefined, pauseMs: 0, budgetMs: 60_000, chunk: 40,
    };
    return { d, store, makeApi };
  }

  it('истёкший по exp токен — WB_SUPPLIES_TOKEN_EXPIRED, DETERMINISTIC, ни одного fetch и записи', async () => {
    const token = synth(Math.floor(NOW / 1000) - 60);
    const fetcher = vi.fn<Fetcher>();
    const { d, store, makeApi } = realApiDeps(token, fetcher);
    const cap = captureLog();
    let err: unknown;
    try { await suppliesLoader(infoCtx(), d); } catch (e) { err = e; } finally { cap.restore(); }
    expect(classifyFailure(err)).toEqual({ code: 'WB_SUPPLIES_TOKEN_EXPIRED', category: 'DETERMINISTIC' });
    expect((err as Error).message).toContain('WB_TOKEN_SUPPLIES');
    expect(fetcher).not.toHaveBeenCalled();
    expect(makeApi).not.toHaveBeenCalled();
    expect(store.loads).toEqual([]);
    expect(store.knownFrom).toEqual([]);
    const text = (err as Error).message + cap.lines.join('');
    expect(text).not.toContain(SECRET_SIG);
    expect(text).not.toContain(token.split('.')[1]!);
  });

  it('пустой секрет — WB_SUPPLIES_AUTH до WB', async () => {
    const fetcher = vi.fn<Fetcher>();
    const { d } = realApiDeps('  \n', fetcher);
    await expect(suppliesLoader(ctx('shadow'), d)).rejects.toMatchObject({ code: 'WB_SUPPLIES_AUTH' });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('скоро истекает — warn wb_token_expiring с кодом WB_TOKEN_EXPIRING_SOON, прогон идёт дальше', async () => {
    const token = synth(Math.floor(NOW / 1000) + 3 * 86_400 + 60);
    const fetcher = vi.fn<Fetcher>(async () => ({ ok: true, status: 200, body: '[]', attempts: 1 }));
    const { d } = realApiDeps(token, fetcher);
    const cap = captureLog();
    try { await suppliesLoader(infoCtx(), d); } finally { cap.restore(); }
    const warn = cap.lines.map((l) => JSON.parse(l) as Record<string, unknown>).filter((r) => r.message === 'wb_token_expiring');
    expect(warn).toHaveLength(1);
    expect(warn[0]).toMatchObject({ severity: 'WARN', code: WB_TOKEN_EXPIRING_SOON, secret: 'WB_TOKEN_SUPPLIES', daysLeft: 3 });
    expect(fetcher).toHaveBeenCalledTimes(1); // список
    const log = cap.lines.join('');
    expect(log).not.toContain(SECRET_SIG);
    expect(log).not.toContain(token.split('.')[1]!);
  });

  it.each([401, 403])('HTTP %i от WB — WB_SUPPLIES_AUTH, DETERMINISTIC, ровно один запрос; токена нет в тексте и логе', async (status) => {
    const token = synth(Math.floor(NOW / 1000) + 90 * 86_400);
    // WB иногда эхом возвращает заголовок — проверяем, что и это не утекает.
    const body = `{"title":"unauthorized","detail":"token is expired","echo":"Bearer ${token}"}`;
    const fetcher = vi.fn<Fetcher>(async () => ({ ok: false, status, body, attempts: 1 }));
    const { d, store } = realApiDeps(token, fetcher);
    const cap = captureLog();
    let err: unknown;
    try { await suppliesLoader(infoCtx(), d); } catch (e) { err = e; } finally { cap.restore(); }
    expect(classifyFailure(err)).toEqual({ code: 'WB_SUPPLIES_AUTH', category: 'DETERMINISTIC' });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(store.loads).toEqual([]);
    const msg = (err as Error).message;
    expect(msg).toContain(`HTTP ${status}`);
    expect(msg).toContain('WB_TOKEN_SUPPLIES');
    expect(msg).toContain(status === 401 ? 'истёк или отозван' : 'нет категории доступа');
    const text = msg + cap.lines.join('');
    expect(text).not.toContain(SECRET_SIG);
    expect(text).not.toContain(token.split('.')[1]!);
  });

  it('имя секрета в ошибке — из env контура (WB_SUPPLIES_SECRET), а не константа', async () => {
    const fetcher = vi.fn<Fetcher>(async () => ({ ok: false, status: 401, body: '', attempts: 1 }));
    const { d } = realApiDeps(synth(Math.floor(NOW / 1000) + 90 * 86_400), fetcher);
    d.env = { WB_SUPPLIES_SECRET: 'WB_TOKEN_SUPPLIES_V2' };
    await expect(suppliesLoader(ctx('shadow'), d)).rejects.toThrow(/WB_TOKEN_SUPPLIES_V2/);
  });
});

describe('поставки — WB API: 429, форма ответа, усечение', () => {
  afterEach(() => vi.unstubAllGlobals());
  it('429 → повтор → 200 (общий wbFetch)', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(new Response('{"title":"too many requests"}', { status: 429 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(SUPPLY_LIST), { status: 200 }));
    vi.stubGlobal('fetch', f);
    const api = new WbSuppliesApi('https://h', 't', { timeoutMs: 1000, maxRetries: 3, backoffBaseMs: 1, backoffCapMs: 1 }, undefined, wbFetch);
    expect(await api.list()).toHaveLength(2);
    expect(f).toHaveBeenCalledTimes(2);
    const [url, init] = f.mock.calls[0]!;
    expect(url).toBe('https://h/api/v1/supplies?limit=1000&offset=0');
    expect((init as RequestInit).method).toBe('POST');
    expect((init as RequestInit).body).toBe('{}');
  });
  it('пустой список — [] (не ошибка); {data:[…]} тоже разбирается', () => {
    expect(rowsOf([])).toEqual([]);
    expect(rowsOf({ data: [{ a: 1 }] })).toEqual([{ a: 1 }]);
    expect(rowsOf({ items: [] })).toBeNull();
  });
  it('неразобранный состав — отказ, а не «0 позиций»; ≥1000 — отказ', async () => {
    const ok = (body: string) => vi.fn(async () => ({ ok: true, status: 200, body, attempts: 1 }));
    await expect(new WbSuppliesApi('h', 't', { timeoutMs: 1, maxRetries: 0 }, undefined, ok('{"x":1}')).goods(1)).rejects.toMatchObject({ code: 'WB_SUPPLIES_SHAPE' });
    await expect(new WbSuppliesApi('h', 't', { timeoutMs: 1, maxRetries: 0 }, undefined, ok(JSON.stringify(new Array(1000).fill({})))).list()).rejects.toMatchObject({ code: 'WB_SUPPLIES_TRUNCATED' });
    expect(await new WbSuppliesApi('h', 't', { timeoutMs: 1, maxRetries: 0 }, undefined, ok('not json')).card(1)).toBeNull();
  });
});

describe('поставки — запись: commit-marker, порции, идемпотентность', () => {
  it('новый объект с supplyID: карточка+состав; goods раньше header; jobId детерминирован', async () => {
    const d = deps();
    const res = await suppliesLoader(ctx('shadow'), d);
    expect(d.store.knownFrom).toEqual(['RAW_WB_SUPPLIES__CR']); // watermark — из своей таблицы контура
    expect(d.api.calls).toEqual(['list', 'card:800001', 'goods:800001']); // у заявки без supplyID — ни карточки, ни состава
    expect(d.store.loads.map((l) => l.table)).toEqual(['RAW_WB_SUPPLIES_GOODS__CR', 'RAW_WB_SUPPLIES__CR']);
    const [g, h] = d.store.loads;
    expect(h!.rows.map((r) => r.entity_key)).toEqual(['P:900001', 'P:900002']);
    expect(g!.rows).toHaveLength(2);
    expect(new Set([...g!.rows, ...h!.rows].map((r) => r.run_id))).toEqual(new Set(['DL_20261111_022900']));
    expect(h!.jobId).toBe(suppliesJobId('shadow', 'DL_20261111_022900', 'RAW_WB_SUPPLIES__CR', 1));
    expect(res).toEqual({ rowsFetched: 2, rowsLoaded: 4 });
  });
  it('пустой ответ — ни одного load job', async () => {
    const d = deps({ api: new FakeApi([]) });
    expect(await suppliesLoader(ctx('shadow'), d)).toEqual({ rowsFetched: 0, rowsLoaded: 0 });
    expect(d.store.loads).toEqual([]);
  });
  it('всё известное и без причины — запись не нужна', async () => {
    const known = new Map([['P:900001', { updMs: Date.parse('2026-10-27T06:00:00Z'), lastCompleted: true }]]);
    const d = deps({ api: new FakeApi([SUPPLY_LIST[0]!]), store: new FakeStore(known) });
    await suppliesLoader(ctx('shadow'), d);
    expect(d.api.calls).toEqual(['list']);
    expect(d.store.loads).toEqual([]);
  });
  it('порции: каждая — goods, затем header; номера порций в jobId различны', async () => {
    const many = Array.from({ length: 5 }, (_, i) => ({ ...SUPPLY_LIST[0]!, preorderID: 910000 + i, supplyID: 810000 + i }));
    const d = deps({ api: new FakeApi(many), chunk: 2 });
    await suppliesLoader(ctx('shadow'), d);
    expect(d.store.loads.map((l) => `${l.table}:${l.rows.length}`)).toEqual([
      'RAW_WB_SUPPLIES_GOODS__CR:4', 'RAW_WB_SUPPLIES__CR:2',
      'RAW_WB_SUPPLIES_GOODS__CR:4', 'RAW_WB_SUPPLIES__CR:2',
      'RAW_WB_SUPPLIES_GOODS__CR:2', 'RAW_WB_SUPPLIES__CR:1',
    ]);
    expect(new Set(d.store.loads.map((l) => l.jobId)).size).toBe(6);
  });
  it('отказ WB на объекте: уже собранные — коммитятся, несобранный — нет, прогон падает', async () => {
    const many = Array.from({ length: 3 }, (_, i) => ({ ...SUPPLY_LIST[0]!, preorderID: 920000 + i, supplyID: 820000 + i }));
    const d = deps({ api: new FakeApi(many, 820001) });
    await expect(suppliesLoader(ctx('shadow'), d)).rejects.toMatchObject({ code: 'WB_SUPPLIES_HTTP' });
    const header = d.store.loads.find((l) => l.table === 'RAW_WB_SUPPLIES__CR')!;
    expect(header.rows.map((r) => r.entity_key)).toEqual(['P:920000']);
    expect(d.store.loads.find((l) => l.table === 'RAW_WB_SUPPLIES_GOODS__CR')!.rows.every((r) => r.entity_key === 'P:920000')).toBe(true);
  });
  it('бюджет времени: собранное коммитится, исход — SUPPLIES_PARTIAL (не тихий PARTIAL Apps Script)', async () => {
    let t = NOW;
    const many = Array.from({ length: 3 }, (_, i) => ({ ...SUPPLY_LIST[0]!, preorderID: 930000 + i, supplyID: 830000 + i }));
    const d = deps({ api: new FakeApi(many), budgetMs: 1500, nowMs: () => (t += 1000) - 1000 });
    await expect(suppliesLoader(ctx('shadow'), d)).rejects.toMatchObject({ code: 'SUPPLIES_PARTIAL' });
    expect(d.store.loads.find((l) => l.table === 'RAW_WB_SUPPLIES__CR')!.rows.length).toBeGreaterThan(0);
  });
});

describe('поставки — load job: повтор с тем же jobId не дублирует', () => {
  class Bq409 implements BqLike {
    metas: JobLoadMetadata[] = [];
    constructor(private readonly state: { state: string; errorResult?: { message: string } }) {}
    queries: string[] = [];
    async query(o: { query: string }): Promise<[unknown[]]> {
      this.queries.push(o.query);
      return [[{ entity_key: 'P:1', upd_ms: '1793316565000', last_completed: false }, { entity_key: 'P:2', upd_ms: null, last_completed: null }]];
    }
    dataset() {
      return { table: () => ({ load: async (_s: string, m: JobLoadMetadata) => { this.metas.push(m); throw Object.assign(new Error('Already Exists: Job'), { code: 409 }); } }) };
    }
    job() { return { get: async () => [{ metadata: { status: this.state } }] as [{ metadata?: unknown }] }; }
  }
  it('409 + прежняя задача DONE без ошибки → ALREADY_LOADED; метаданные — CREATE_NEVER/WRITE_APPEND', async () => {
    const fake = new Bq409({ state: 'DONE' });
    const bq = new SuppliesBq('p', 'EU', 'wb_raw', fake);
    expect(await bq.load('RAW_WB_SUPPLIES__CR', [{ a: 1 }], 'j1')).toBe('ALREADY_LOADED');
    expect(fake.metas[0]).toMatchObject({ createDisposition: 'CREATE_NEVER', writeDisposition: 'WRITE_APPEND', jobId: 'j1', maxBadRecords: 0 });
  });
  it('409 + прежняя задача с ошибкой → отказ', async () => {
    const bq = new SuppliesBq('p', 'EU', 'wb_raw', new Bq409({ state: 'DONE', errorResult: { message: 'bad' } }));
    await expect(bq.load('t', [{}], 'j2')).rejects.toThrow(/не успешен: bad/);
  });
  it('watermark: INT64 строкой → число; is_completed последней шапки — в порядке V_WB_SUPPLIES_CURRENT', async () => {
    const fake = new Bq409({ state: 'DONE' });
    const known = await new SuppliesBq('p', 'EU', 'wb_raw', fake).knownUpdated('RAW_WB_SUPPLIES__CR');
    expect(known.get('P:1')).toEqual({ updMs: 1793316565000, lastCompleted: false });
    expect(known.get('P:2')).toEqual({ updMs: 0, lastCompleted: null });
    expect(fake.queries[0]).toMatch(/ORDER BY snapshot_ts DESC, run_id DESC LIMIT 1/);
    expect(fake.queries[0]).toMatch(/`p\.wb_raw\.RAW_WB_SUPPLIES__CR`/);
  });
});
