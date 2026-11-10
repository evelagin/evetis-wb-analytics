/**
 * M2 — поведение Cloud Run-загрузчиков, которого нет у Apps Script или которое Apps Script
 * обеспечивал иначе: контур до WB, watermark из таблицы, стартовый watermark shadow, повтор без
 * дублей, справочник только при записи, DRY_RUN, сбой записи.
 */
import { describe, it, expect } from 'vitest';
import { LoaderError } from '../src/errors.js';
import { runOrders, runSales, runSalesReconcile } from '../src/loaders/statistics/index.js';
import { normalizeOrdersRows, ordersBqRow } from '../src/loaders/statistics/orders.js';
import { normalizeSalesRows, salesBqRow } from '../src/loaders/statistics/sales.js';
import { buildSkuIndex } from '../src/loaders/statistics/common.js';
import { FakeStatsBq, REF_ROWS, fakeTransport, orderRow, saleRow, statsCtx, statsDeps, wbScript } from './statistics_fixture.js';

const SKU = buildSkuIndex(REF_ROWS);
const W = '2026-11-09T12:00:00';
const existingOrders = () => normalizeOrdersRows([orderRow(1, { lastChangeDate: W })], 'ORD_INC_X', '2026-11-10 03:31:00', SKU).map(ordersBqRow);
const existingSales = () => normalizeSalesRows([saleRow(1, { lastChangeDate: W })], 'SALE_INC_X', '2026-11-10 03:22:00', SKU).map(salesBqRow);

async function codeOf(p: Promise<unknown>): Promise<string> {
  try { await p; return 'OK'; } catch (e) { return e instanceof LoaderError ? e.code : `RAW:${String(e)}`; }
}

describe('контур проверяется ДО BigQuery и WB API', () => {
  it('prod с теневой таблицей → STATS_TARGET_MISMATCH, ни запроса в WB, ни чтения BQ', async () => {
    const bq = new FakeStatsBq(existingOrders());
    const script = wbScript([{ status: 200, body: [] }]);
    let boundaryCalls = 0;
    const spy = { ...statsDeps(bq, fakeTransport(script)) };
    spy.bq = { ...bq, ordersBoundary: async () => { boundaryCalls++; return bq.ordersBoundary(); } } as unknown as typeof spy.bq;
    let tokenCalls = 0;
    spy.getToken = async () => { tokenCalls++; return 't'; };
    const code = await codeOf(runOrders(statsCtx({ ORDERS_RAW_TABLE: 'RAW_WB_ORDERS__CR' }, 'prod'), spy));
    expect(code).toBe('STATS_TARGET_MISMATCH');
    expect(script.urls).toHaveLength(0);
    expect(boundaryCalls).toBe(0);
    expect(tokenCalls).toBe(0);
  });

  it('shadow с боевой таблицей продаж → отказ и в sales, и в sales-reconcile', async () => {
    for (const fn of [runSales, runSalesReconcile]) {
      const script = wbScript([{ status: 200, body: [] }]);
      const code = await codeOf(fn(statsCtx({ SALES_RAW_TABLE: 'RAW_WB_SALES_RETURNS' }), statsDeps(new FakeStatsBq(), fakeTransport(script))));
      expect(code).toBe('STATS_TARGET_MISMATCH');
      expect(script.urls).toHaveLength(0);
    }
  });
});

describe('watermark из целевой таблицы', () => {
  it('dateFrom = MAX(last_change_date) таблицы в форме API (заказы хранят пробел)', async () => {
    const script = wbScript([{ status: 200, body: [] }]);
    await runOrders(statsCtx(), statsDeps(new FakeStatsBq(existingOrders()), fakeTransport(script)));
    expect(script.urls[0]).toContain(`dateFrom=${encodeURIComponent(W)}`);
  });

  it('пустая таблица без стартового значения → ERROR до WB', async () => {
    const script = wbScript([{ status: 200, body: [] }]);
    expect(await codeOf(runOrders(statsCtx(), statsDeps(new FakeStatsBq(), fakeTransport(script))))).toBe('ORDERS_ERROR');
    expect(await codeOf(runSales(statsCtx(), statsDeps(new FakeStatsBq(), fakeTransport(script))))).toBe('SALES_ERROR');
    expect(script.urls).toHaveLength(0);
  });

  it('shadow: пустая __CR + валидный стартовый watermark → запрос от него', async () => {
    const script = wbScript([{ status: 200, body: [] }]);
    const res = await runSales(statsCtx({ SALES_BOOTSTRAP_WATERMARK: '2026-11-08T00:00:00' }), statsDeps(new FakeStatsBq(), fakeTransport(script)));
    expect(res).toEqual({ rowsFetched: 0, rowsLoaded: 0 });
    expect(script.urls[0]).toContain('dateFrom=2026-11-08T00%3A00%3A00');
  });

  it('стартовый watermark игнорируется, если таблица не пуста, и запрещён в prod', async () => {
    const s1 = wbScript([{ status: 200, body: [] }]);
    await runOrders(statsCtx({ ORDERS_BOOTSTRAP_WATERMARK: '2020-01-01T00:00:00' }), statsDeps(new FakeStatsBq(existingOrders()), fakeTransport(s1)));
    expect(s1.urls[0]).toContain(encodeURIComponent(W));
    const s2 = wbScript([{ status: 200, body: [] }]);
    const code = await codeOf(runOrders(statsCtx({ ORDERS_BOOTSTRAP_WATERMARK: '2026-11-08T00:00:00' }, 'prod'), statsDeps(new FakeStatsBq(), fakeTransport(s2))));
    expect(code).toBe('ORDERS_ERROR');
    expect(s2.urls).toHaveLength(0);
  });

  it('битый стартовый watermark → ERROR', async () => {
    const code = await codeOf(runOrders(statsCtx({ ORDERS_BOOTSTRAP_WATERMARK: '2026-11-08 00:00:00' }), statsDeps(new FakeStatsBq(), fakeTransport(wbScript([])))));
    expect(code).toBe('ORDERS_ERROR');
  });
});

describe('идемпотентность и устойчивость', () => {
  const newer = [orderRow(2, { lastChangeDate: '2026-11-09T13:00:00' }), orderRow(3, { lastChangeDate: '2026-11-09T13:00:00' })];

  it('заказы: повтор того же ответа WB после успешной записи не дублирует строки', async () => {
    const bq = new FakeStatsBq(existingOrders());
    const r1 = await runOrders(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: newer }, { status: 200, body: [] }]))));
    expect(r1.rowsLoaded).toBe(2);
    // Второй запуск: watermark уже 13:00 (MAX таблицы), WB отдаёт обе граничные строки включительно.
    const script = wbScript([{ status: 200, body: newer }]);
    const r2 = await runOrders(statsCtx(), statsDeps(bq, fakeTransport(script)));
    expect(r2.rowsLoaded).toBe(0);
    expect(script.urls[0]).toContain(encodeURIComponent('2026-11-09T13:00:00'));
    expect(bq.table).toHaveLength(3);
  });

  it('продажи: повтор после записи — только граничные состояния, дублей нет; новое состояние на границе дописывается', async () => {
    const bq = new FakeStatsBq(existingSales());
    const batch = [saleRow(2, { lastChangeDate: '2026-11-09T13:00:00' })];
    await runSales(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: batch }]))));
    const r2 = await runSales(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: batch }]))));
    expect(r2.rowsLoaded).toBe(0);
    const changed = [saleRow(2, { lastChangeDate: '2026-11-09T13:00:00', forPay: 1 })];
    const r3 = await runSales(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: changed }]))));
    expect(r3.rowsLoaded).toBe(1);
  });

  it('сверка: повторный прогон после записи ничего не добавляет', async () => {
    const bq = new FakeStatsBq(existingSales());
    const win = [saleRow(5, { lastChangeDate: '2026-11-05T08:00:00' })];
    const a = await runSalesReconcile(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: win }]))));
    const b = await runSalesReconcile(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: win }]))));
    expect([a.rowsLoaded, b.rowsLoaded]).toEqual([1, 0]);
  });

  it('сверка не двигает watermark инкремента: окно старше watermark не меняет MAX', async () => {
    const bq = new FakeStatsBq(existingSales());
    await runSalesReconcile(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: [saleRow(5, { lastChangeDate: '2026-11-05T08:00:00' })] }]))));
    expect((await bq.salesBoundary()).watermarkStorage).toBe(W);
  });

  it('сбой записи в BigQuery → исключение (cli пишет ERROR), watermark прежний', async () => {
    const bq = new FakeStatsBq(existingOrders(), REF_ROWS, true);
    await expect(runOrders(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: newer }, { status: 200, body: [] }])))))
      .rejects.toThrow(/BQ load error/);
    expect((await bq.ordersBoundary()).watermarkStorage).toBe('2026-11-09 12:00:00');
  });

  it('справочник SKU читается только когда есть что писать', async () => {
    const bq = new FakeStatsBq(existingOrders());
    await runOrders(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: [] }]))));
    await runSales(statsCtx(), statsDeps(new FakeStatsBq(existingSales()), fakeTransport(wbScript([{ status: 200, body: [] }]))));
    expect(bq.skuQueries).toBe(0);
    await runOrders(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: newer }, { status: 200, body: [] }]))));
    expect(bq.skuQueries).toBe(1);
  });

  it('DRY_RUN=1: план считается, в BigQuery не пишется', async () => {
    const bq = new FakeStatsBq(existingOrders());
    const r = await runOrders(statsCtx({ DRY_RUN: '1' }), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: newer }, { status: 200, body: [] }]))));
    expect(r).toEqual({ rowsFetched: 2, rowsLoaded: 0 });
    expect(bq.appended).toHaveLength(0);
  });

  it('запись — один load job на пакет, в целевую таблицу контура', async () => {
    const bq = new FakeStatsBq(existingOrders());
    await runOrders(statsCtx(), statsDeps(bq, fakeTransport(wbScript([{ status: 200, body: newer }, { status: 200, body: [] }]))));
    expect(bq.appended).toHaveLength(1);
    expect(bq.appended[0]!.table).toBe('RAW_WB_ORDERS__CR');
    expect(bq.appended[0]!.jobId).toMatch(/^stats_shadow_raw_wb_orders__cr_ORD_INC_20261110_093100_[0-9a-f]{12}$/);
  });
});
