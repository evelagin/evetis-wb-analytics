/**
 * M2 — replay-паритет 1:1 с БОЕВЫМ Apps Script.
 *
 * Один и тот же сценарий (RAW до запуска + лента ответов WB) прогоняется:
 *   A) исполняемым кодом apps-script/ingestion/*.gs в node:vm (BigQuery/Sheets — заглушки);
 *   B) Cloud Run runOrders / runSales / runSalesReconcile (WB и BigQuery — подделки).
 * Сравниваются: строки, ушедшие в BigQuery (байт-в-байт форма NDJSON), итог журнала
 * (COMPLETE ↔ результат, ERROR ↔ код LoaderError), watermark после прогона и сами запросы в WB.
 * Расходиться может только то, что задокументировано в runbook («Отличия»).
 */
import { describe, it, expect } from 'vitest';
import { LoaderError } from '../src/errors.js';
import { runOrders, runSales, runSalesReconcile } from '../src/loaders/statistics/index.js';
import { normalizeOrdersRows, ordersBqRow } from '../src/loaders/statistics/orders.js';
import { normalizeSalesRows, salesBqRow } from '../src/loaders/statistics/sales.js';
import { buildSkuIndex } from '../src/loaders/statistics/common.js';
import {
  FakeStatsBq, REF_ROWS, fakeTransport, orderRow, runAppsScript, saleRow, statsCtx, statsDeps, wbScript,
  type WbResp,
} from './statistics_fixture.js';

type Obj = Record<string, unknown>;
const SKU = buildSkuIndex(REF_ROWS);
const W = '2026-11-09T12:00:00';

async function runCloud(
  fn: typeof runOrders, existing: Obj[], wb: WbResp[], env: Record<string, string> = {},
): Promise<{ bq: FakeStatsBq; urls: string[]; result?: { rowsFetched: number; rowsLoaded: number }; code?: string }> {
  const bq = new FakeStatsBq(existing.map((r) => ({ ...r })));
  const script = wbScript(wb);
  try {
    const result = await fn(statsCtx(env), statsDeps(bq, fakeTransport(script)));
    return { bq, urls: script.urls, result };
  } catch (e) {
    if (!(e instanceof LoaderError)) throw e;
    return { bq, urls: script.urls, code: e.code };
  }
}

/** Совпадение исхода журнала: COMPLETE ↔ результат с теми же счётчиками, ERROR ↔ тот же код. */
function expectSameOutcome(a: ReturnType<typeof runAppsScript>, b: Awaited<ReturnType<typeof runCloud>>) {
  expect(a.ingest).not.toBeNull();
  if (a.ingest!.status === 'COMPLETE') {
    expect(b.code).toBeUndefined();
    expect(b.result).toEqual({ rowsFetched: a.ingest!.rowsFetched, rowsLoaded: a.ingest!.rowsLoaded });
  } else {
    expect(b.result).toBeUndefined();
    expect(b.code).toBe(a.ingest!.code);
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Заказы
// ─────────────────────────────────────────────────────────────────────────

/** RAW до запуска: srid 0 (11:00) и srid 1, 2 ровно на watermark 12:00. */
const ORDERS_EXISTING: Obj[] = normalizeOrdersRows([
  orderRow(0, { lastChangeDate: '2026-11-09T11:00:00' }),
  orderRow(1, { lastChangeDate: W }),
  orderRow(2, { lastChangeDate: W }),
], 'ORD_INC_20261110_033100', '2026-11-10 03:31:00', SKU).map(ordersBqRow);

const ORDERS_PAGE1 = [
  orderRow(1, { lastChangeDate: W }),                                                  // уже в RAW → пропуск
  orderRow(3, { lastChangeDate: W }),                                                  // новая пара на границе → пишется
  orderRow(2, { lastChangeDate: W, isCancel: true, cancelDate: '2026-11-09T11:59:00' }), // та же srid, другой row_hash → пишется
  orderRow(4, { lastChangeDate: '2026-11-09T13:00:00', nmId: 303, barcode: '2000000000033' }), // привязка по barcode
  orderRow(5, { lastChangeDate: '2026-11-09T13:30:00', nmId: 404, barcode: '2999999999999',
    totalPrice: undefined, spp: null, discountPercent: '', priceWithDisc: '' }),       // not_found, SPP-поля пустые
  orderRow(4, { lastChangeDate: '2026-11-09T13:45:00', nmId: 303, barcode: '2000000000033', isCancel: true }), // last-wins по srid
  orderRow(6, { lastChangeDate: '2026-11-09T14:00:00', nmId: 202, barcode: '2000000000022', spp: 0, sticker: '' }),
];

function parityOrders(name: string, wb: WbResp[], expectWritten: number, existing = ORDERS_EXISTING) {
  it(name, async () => {
    const a = runAppsScript('runWbOrdersIncremental', {
      props: { WB_ORDERS_BQ_SINK: '1', WB_TOKEN_STATISTICS: 'synthetic', WB_ORDERS_LAST_CHANGE_WATERMARK: W },
      existing, wb,
    });
    const b = await runCloud(runOrders, existing, wb);
    expect(a.loaded).toHaveLength(expectWritten); // сценарий не пустой: сравнение не проходит «на нулях»
    expect(b.bq.lastRows()).toEqual(a.loaded);
    expectSameOutcome(a, b);
    expect(b.urls).toEqual(a.urls);
    // watermark после: у Apps Script — Script Property, у Cloud Run — MAX таблицы (форма API).
    const after = (await b.bq.ordersBoundary()).watermarkStorage.replace(' ', 'T');
    expect(after).toBe(a.props.WB_ORDERS_LAST_CHANGE_WATERMARK);
  });
}

describe('replay-паритет заказов (runWbOrdersIncremental ↔ orders)', () => {
  parityOrders('две страницы: граница, last-wins, привязка SKU, пустые SPP-поля', [
    { status: 200, body: ORDERS_PAGE1 },
    { status: 200, body: [ORDERS_PAGE1[6]] }, // WB отдаёт граничную строку включительно → дренаж
  ], 5);
  parityOrders('пустой ответ → OK_NO_CHANGES (COMPLETE, 0 строк)', [{ status: 200, body: [] }], 0);
  parityOrders('только уже записанные граничные строки → OK_NO_CHANGES', [
    { status: 200, body: [ORDERS_PAGE1[0]] },
  ], 0);
  parityOrders('строка без srid → ERROR, ничего не пишется', [
    { status: 200, body: [orderRow(7, { srid: '  ', lastChangeDate: '2026-11-09T15:00:00' })] },
  ], 0);
  parityOrders('кандидат старше watermark → ERROR', [
    { status: 200, body: [orderRow(8, { lastChangeDate: '2026-11-09T09:00:00' })] },
  ], 0);
  parityOrders('500 на второй странице после повторов → PARTIAL, ничего не пишется', [
    { status: 200, body: ORDERS_PAGE1 },
    { status: 500, body: 'oops' }, { status: 500, body: 'oops' }, { status: 500, body: 'oops' }, { status: 500, body: 'oops' },
  ], 0);
  parityOrders('429 с Retry-After, затем успех', [
    { status: 429, body: 'limited', retryAfter: '3' },
    { status: 200, body: [ORDERS_PAGE1[6]] },
    { status: 200, body: [] },
  ], 1);
  parityOrders('битый JSON на первой странице → ERROR', [{ status: 200, body: '{not json' }], 0);
});

// ─────────────────────────────────────────────────────────────────────────
// Продажи
// ─────────────────────────────────────────────────────────────────────────

const S1 = saleRow(1, { lastChangeDate: W });
const SALES_EXISTING: Obj[] = normalizeSalesRows([
  saleRow(0, { lastChangeDate: '2026-11-09T10:00:00' }), S1,
], 'SALE_INC_20261110_032200', '2026-11-10 03:22:00', SKU).map(salesBqRow);

const SALES_BATCH = [
  S1,                                                                         // состояние уже в RAW → пропуск
  saleRow(9, { lastChangeDate: W, forPay: 100.005 }),                          // новое состояние на границе
  saleRow(2, { lastChangeDate: '2026-11-09T13:00:00' }),
  saleRow(2, { lastChangeDate: '2026-11-09T13:30:00', warehouseName: 'Склад-2' }), // last-wins по sale_id
  saleRow(3, { lastChangeDate: '2026-11-09T14:00:00', saleID: 'R7000003', forPay: -233.456 }), // возврат
  saleRow(4, { lastChangeDate: '2026-11-09T14:10:00', nmId: 303, barcode: '2000000000033', incomeID: 0 }),
  saleRow(5, { lastChangeDate: '2026-11-09T14:20:00', nmId: 404, barcode: '', isSupply: true, spp: '' }),
];

function paritySales(name: string, wb: WbResp[], expectWritten: number, existing = SALES_EXISTING) {
  it(name, async () => {
    const a = runAppsScript('runWbSalesIncremental', {
      props: { WB_SALES_BQ_SINK: '1', WB_TOKEN_STATISTICS: 'synthetic', WB_SALES_LAST_CHANGE_WATERMARK: W },
      existing, wb,
    });
    const b = await runCloud(runSales, existing, wb);
    expect(a.loaded).toHaveLength(expectWritten);
    expect(b.bq.lastRows()).toEqual(a.loaded);
    expectSameOutcome(a, b);
    expect(b.urls).toEqual(a.urls);
    expect((await b.bq.salesBoundary()).watermarkStorage).toBe(a.props.WB_SALES_LAST_CHANGE_WATERMARK);
  });
}

describe('replay-паритет продаж (runWbSalesIncremental ↔ sales)', () => {
  paritySales('граница по состоянию, last-wins, возврат R…, округление, привязка SKU', [{ status: 200, body: SALES_BATCH }], 5);
  paritySales('пустой ответ → OK_NO_CHANGES', [{ status: 200, body: [] }], 0);
  paritySales('только уже записанное состояние на границе → OK_NO_CHANGES', [{ status: 200, body: [S1] }], 0);
  paritySales('строка без saleID → ERROR', [{ status: 200, body: [saleRow(6, { saleID: '', lastChangeDate: '2026-11-09T15:00:00' })] }], 0);
  paritySales('lastChangeDate без времени → ERROR', [{ status: 200, body: [saleRow(6, { lastChangeDate: '2026-11-09' })] }], 0);
  paritySales('500 после повторов → ERROR', [
    { status: 500, body: 'x' }, { status: 500, body: 'x' }, { status: 500, body: 'x' }, { status: 500, body: 'x' },
  ], 0);
  paritySales('429, затем успех', [{ status: 429, body: 'limited' }, { status: 200, body: SALES_BATCH }], 5);
  paritySales('не массив → ERROR', [{ status: 200, body: { error: 'x' } }], 0);
});

// ─────────────────────────────────────────────────────────────────────────
// Ночная сверка
// ─────────────────────────────────────────────────────────────────────────

describe('replay-паритет ночной сверки (runWbSalesNightReconcile ↔ sales-reconcile)', () => {
  const window = [
    saleRow(0, { lastChangeDate: '2026-11-09T10:00:00' }),               // уже в RAW
    saleRow(0, { lastChangeDate: '2026-11-09T10:00:00', spp: 31 }),     // другое состояние той же продажи — латаем
    saleRow(11, { lastChangeDate: '2026-11-05T08:00:00' }),             // пропуск часового инкремента
    saleRow(11, { lastChangeDate: '2026-11-05T08:00:00' }),             // дубль в пакете — одна строка
    saleRow(12, { lastChangeDate: '2026-11-04T09:00:00', saleID: 'R8000012' }),
  ];
  const cases: Array<[string, WbResp[], number]> = [
    ['пропущенные состояния дописываются, присутствующие — нет', [{ status: 200, body: window }], 3],
    ['всё уже в RAW → OK_NO_GAPS', [{ status: 200, body: [window[0]] }], 0],
    ['пустой ответ → OK_NO_GAPS', [{ status: 200, body: [] }], 0],
    ['строка без даты → ERROR', [{ status: 200, body: [saleRow(13, { date: '' })] }], 0],
    ['500 после повторов → ERROR', [{ status: 500, body: 'x' }, { status: 500, body: 'x' }, { status: 500, body: 'x' }, { status: 500, body: 'x' }], 0],
  ];
  for (const [name, wb, expectWritten] of cases) {
    it(name, async () => {
      const a = runAppsScript('runWbSalesNightReconcile', {
        props: { WB_SALES_BQ_SINK: '1', WB_TOKEN_STATISTICS: 'synthetic', WB_SALES_LAST_CHANGE_WATERMARK: W },
        existing: SALES_EXISTING, wb,
      });
      const b = await runCloud(runSalesReconcile, SALES_EXISTING, wb);
      expect(a.loaded).toHaveLength(expectWritten);
      expect(b.bq.lastRows()).toEqual(a.loaded);
      expect(b.urls).toEqual(a.urls);
      // Сверка в INGEST_RUNS не пишет; её исход — статус результата Apps Script.
      expect(a.ingest).toBeNull();
      const ok = a.result.status === 'OK' || a.result.status === 'OK_NO_GAPS';
      expect(b.code === undefined).toBe(ok);
      if (!ok) expect(b.code).toBe('SALES_RECONCILE_ERROR');
      // Сверка watermark не двигает: у Apps Script свойство прежнее.
      expect(a.props.WB_SALES_LAST_CHANGE_WATERMARK).toBe(W);
      if (ok) expect(b.result!.rowsLoaded).toBe(Number(a.result.rows_written));
    });
  }
});
