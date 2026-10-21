import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
  normalizeT5, sumT5, evaluateT5Control, t5UnavailableControl, t5RowType,
  extractT5Array, extractT5TaskId, t5TaskState, type T5NormalizeCtx,
} from '../src/loaders/stocks/t5.js';
import { fetchStocksT5 } from '../src/loaders/stocks/wbApi.js';

/**
 * PR-Mig1c — чистое ядро T5 и транспорт задачи warehouse_remains.
 * Сверка 1:1 с исполняемым кодом Apps Script — в stocks_t5_apps_script_parity.test.ts.
 */

const here = dirname(fileURLToPath(import.meta.url));
const fx = JSON.parse(readFileSync(join(here, 'fixtures', 'wb_stocks_t5_anonymized.json'), 'utf8'));

const ctx: T5NormalizeCtx = {
  snapshotId: 'STOCK_SNAP_shadow_20261021',
  snapshotTsIso: '2026-10-21T03:30:00.000Z',
  snapshotDate: '2026-10-21',
  loadId: 'STOCK_LOAD_STOCK_SNAP_shadow_20261021',
  sourceApi: 'WB_API_STOCKS_T5',
  skuByNm: new Map<number, string>([[101, 'SKU-A'], [202, 'SKU-B']]),
};

describe('normalizeT5', () => {
  const rows = normalizeT5(fx.download, ctx);

  it('одна строка на товар × склад; товар без складов строк не даёт', () => {
    expect(rows.length).toBe(9);
    expect(rows.filter((r) => r.nm_id === 202)).toHaveLength(0);
  });

  it('row_type: агрегат, поимённый склад и три псевдо-строки', () => {
    const types = rows.filter((r) => r.nm_id === 101).map((r) => r.row_type);
    expect(types).toEqual(['PSEUDO_TO_CLIENT', 'PSEUDO_FROM_CLIENT', 'PSEUDO_TOTAL', 'AGGREGATE', 'WAREHOUSE']);
  });

  it('SKU по nmId из REF; несопоставленный nm и пустой nm → not_found', () => {
    expect(rows.filter((r) => r.nm_id === 101).every((r) => r.internal_sku === 'SKU-A' && r.sku_match_status === 'matched')).toBe(true);
    expect(rows.filter((r) => r.nm_id === 999).every((r) => r.internal_sku === '' && r.sku_match_status === 'not_found')).toBe(true);
    const noNm = rows.filter((r) => r.nm_id === null);
    expect(noNm).toHaveLength(1);
    expect(noNm[0]!.sku_match_status).toBe('not_found');
  });

  it('имя склада: поле warehouse + trim; дробное quantity в строке → 0, строковое "0" → 0', () => {
    const tula = rows.find((r) => r.warehouse_name === 'Тула')!;
    expect(tula.row_type).toBe('WAREHOUSE');
    expect(tula.quantity).toBe(0);
    expect(rows.find((r) => r.nm_id === null)!.quantity).toBe(0);
  });

  it('volume приводится к числу, идентификаторы снимка проставлены', () => {
    expect(rows.find((r) => r.nm_id === 999)!.volume).toBe(0.15);
    expect(rows.every((r) => r.snapshot_id === ctx.snapshotId && r.source_api === 'WB_API_STOCKS_T5' && r._snapshot_date === '2026-10-21')).toBe(true);
  });
});

describe('sumT5', () => {
  it('псевдо-строки не идут в сумму; дробь входит в сумму и округляется', () => {
    // агрегат 12+3+0=15; поимённые 30+2.5=32.5 → 33; всего 47.5 → 48
    expect(sumT5(fx.download)).toEqual({ sum: 48, aggSum: 15, namedSum: 33 });
  });
  it('пустой отчёт → нули', () => {
    expect(sumT5([])).toEqual({ sum: 0, aggSum: 0, namedSum: 0 });
  });
});

describe('evaluateT5Control (контроль T5↔T6 и детектор доступности)', () => {
  const sums = (aggSum: number) => ({ sum: aggSum + 100, aggSum, namedSum: 100 });

  it('расхождение в пределах допуска 2 → OK; база есть, разрыв в шуме → availability OK', () => {
    const c = evaluateT5Control(sums(1000), 998, 1);
    expect(c).toMatchObject({ control_status: 'OK', control_delta: 2, availability_gap: 2, availability_gap_delta: 1, availability_status: 'OK' });
    expect(c.availability_ratio).toBeCloseTo(0.998, 6);
  });
  it('расхождение 3 → MISMATCH', () => {
    expect(evaluateT5Control(sums(1000), 997, 3).control_status).toBe('MISMATCH');
  });
  it('T6 больше баланса сверх допуска → DATA_ERROR (раньше любой другой проверки)', () => {
    expect(evaluateT5Control(sums(1000), 1003, null).availability_status).toBe('DATA_ERROR');
    expect(evaluateT5Control(sums(1000), 1002, null).availability_status).toBe('NO_BASELINE');
  });
  it('нет базы → NO_BASELINE, gap_delta NULL', () => {
    const c = evaluateT5Control(sums(1000), 990, null);
    expect(c.availability_status).toBe('NO_BASELINE');
    expect(c.availability_gap_delta).toBeNull();
    expect(c.availability_gap_prev).toBeNull();
  });
  it('скачок разрыва ≥ max(100, 5% агрегата) → WAREHOUSE_DROP; на 1 меньше → DEGRADED', () => {
    // агрегат 4000 → порог max(100, 200) = 200
    expect(evaluateT5Control(sums(4000), 3790, 10).availability_status).toBe('WAREHOUSE_DROP'); // gap 210, Δ 200
    expect(evaluateT5Control(sums(4000), 3791, 10).availability_status).toBe('DEGRADED');       // gap 209, Δ 199
  });
  it('устойчивый разрыв выше 1% без скачка → DEGRADED; ровно на пороге → OK', () => {
    // агрегат 1000 → okThreshold max(2, 10) = 10
    expect(evaluateT5Control(sums(1000), 989, 11).availability_status).toBe('DEGRADED'); // gap 11
    expect(evaluateT5Control(sums(1000), 990, 10).availability_status).toBe('OK');       // gap 10
  });
  it('нулевой агрегат → ratio NULL', () => {
    expect(evaluateT5Control(sums(0), 0, 0).availability_ratio).toBeNull();
  });
  it('T5 недоступен: t6_comparable сохраняется, остальное NULL', () => {
    expect(t5UnavailableControl(14)).toEqual({
      t5_control_sum: null, t5_wb_rf_sum: null, t5_named_sum: null,
      control_status: 'T5_UNAVAILABLE', control_delta: null, t6_comparable: 14,
      availability_gap: null, availability_gap_prev: null, availability_gap_delta: null,
      availability_ratio: null, availability_status: 'T5_UNAVAILABLE',
    });
  });
});

describe('разбор ответов задачи T5', () => {
  it('taskId: data.taskId | data.id | taskId', () => {
    expect(extractT5TaskId({ data: { taskId: 'a' } })).toBe('a');
    expect(extractT5TaskId({ data: { id: 'b' } })).toBe('b');
    expect(extractT5TaskId({ taskId: 'c' })).toBe('c');
    expect(extractT5TaskId({})).toBe('');
    expect(extractT5TaskId(null)).toBe('');
  });
  it('статус без учёта регистра; неизвестный — ожидание', () => {
    expect(t5TaskState({ data: { status: 'DONE' } }).kind).toBe('ready');
    expect(t5TaskState({ status: 'success' }).kind).toBe('ready');
    expect(t5TaskState({ data: { status: 'purged' } })).toEqual({ kind: 'failed', status: 'purged' });
    expect(t5TaskState({ data: { status: 'new' } }).kind).toBe('pending');
    expect(t5TaskState(null).kind).toBe('pending');
  });
  it('download: не массив → пустой отчёт', () => {
    expect(extractT5Array({ data: [] })).toEqual([]);
    expect(extractT5Array([1])).toEqual([1]);
  });
  it('t5RowType', () => {
    expect(t5RowType('Склад WB РФ')).toBe('AGGREGATE');
    expect(t5RowType('Коледино')).toBe('WAREHOUSE');
  });
});

describe('fetchStocksT5 (транспорт задачи)', () => {
  type Route = (url: string) => { status: number; body: unknown };
  function stub(route: Route): { calls: string[]; restore: () => void } {
    const calls: string[] = [];
    const orig = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      const r = route(url);
      return new Response(typeof r.body === 'string' ? r.body : JSON.stringify(r.body), { status: r.status });
    }) as typeof fetch;
    return { calls, restore: () => { globalThis.fetch = orig; } };
  }
  const opts = { timeoutMs: 1000, maxRetries: 0 };
  const noSleep = { intervalMs: 0, maxPolls: 3, sleep: async () => {} };

  it('create → status(processing→done) → download: данные и порядок вызовов', async () => {
    let polls = 0;
    const s = stub((u) => {
      if (u.endsWith('/download')) return { status: 200, body: fx.download };
      if (u.endsWith('/status')) return { status: 200, body: fx.status[Math.min(polls++, 1)] };
      return { status: 200, body: fx.create };
    });
    try {
      const r = await fetchStocksT5('https://h', 't', opts, noSleep);
      expect(r.ok).toBe(true);
      if (r.ok) expect(r.data).toHaveLength(4);
      expect(s.calls[0]).toBe('https://h/api/v1/warehouse_remains?groupByBrand=false&groupBySubject=false&groupBySa=true&groupByNm=true&groupByBarcode=true&groupBySize=true');
      expect(s.calls.slice(1)).toEqual([
        'https://h/api/v1/warehouse_remains/tasks/t5-task-0001/status',
        'https://h/api/v1/warehouse_remains/tasks/t5-task-0001/status',
        'https://h/api/v1/warehouse_remains/tasks/t5-task-0001/download',
      ]);
    } finally { s.restore(); }
  });

  it('ошибка status-опроса не прерывает ожидание; исчерпание → таймаут, без download', async () => {
    const s = stub((u) => (u.endsWith('/status') ? { status: 500, body: 'x' } : { status: 200, body: fx.create }));
    try {
      const r = await fetchStocksT5('https://h', 't', opts, noSleep);
      expect(r).toEqual({ ok: false, error: 'T5: таймаут ожидания задачи' });
      expect(s.calls.filter((c) => c.endsWith('/status'))).toHaveLength(3);
      expect(s.calls.some((c) => c.endsWith('/download'))).toBe(false);
    } finally { s.restore(); }
  });

  it('задача purged → ошибка сразу', async () => {
    const s = stub((u) => (u.endsWith('/status') ? { status: 200, body: { data: { status: 'purged' } } } : { status: 200, body: fx.create }));
    try {
      expect(await fetchStocksT5('https://h', 't', opts, noSleep)).toEqual({ ok: false, error: 'T5 задача purged' });
    } finally { s.restore(); }
  });

  it('нет taskId / HTTP-ошибка create / сетевое исключение → {ok:false}, не бросает', async () => {
    let s = stub(() => ({ status: 200, body: { data: {} } }));
    try { expect(await fetchStocksT5('https://h', 't', opts, noSleep)).toEqual({ ok: false, error: 'T5: нет taskId в ответе create' }); } finally { s.restore(); }
    s = stub(() => ({ status: 401, body: 'unauthorized' }));
    try {
      const r = await fetchStocksT5('https://h', 't', opts, noSleep);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toMatch(/^T5 create: HTTP 401/);
    } finally { s.restore(); }
    const orig = globalThis.fetch;
    globalThis.fetch = (async () => { throw new Error('boom'); }) as typeof fetch;
    try {
      const r = await fetchStocksT5('https://h', 't', opts, noSleep);
      expect(r.ok).toBe(false);
    } finally { globalThis.fetch = orig; }
  });
});
