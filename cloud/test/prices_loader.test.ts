import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';
import { Logger } from '../src/logging.js';
import { LoaderError } from '../src/errors.js';

/**
 * PR-1 — поведение наблюдателя цен на уровне оркестрации.
 *
 * Проверяем ровно то, что делает наблюдатель пригодным для ценовых решений:
 *  - fail-closed (пустой ответ, исчезнувшее поле, ошибка API) — никаких выдуманных цен;
 *  - неполное покрытие фиксируется, но остальные товары не теряются;
 *  - повтор окна не задваивает историю;
 *  - манифест закрывается в любом исходе.
 */

const fetchMock = vi.fn();
const appendMock = vi.fn();
const startMock = vi.fn();
const finalizeMock = vi.fn();
const countMock = vi.fn();
const expectedMock = vi.fn();

vi.mock('../src/loaders/prices/wbApi.js', () => ({
  fetchGoodsPrices: (...a: unknown[]) => fetchMock(...a),
}));
vi.mock('../src/secrets.js', () => ({
  SecretsClient: class { async access() { return 'not-a-real-token'; } },
}));
vi.mock('../src/loaders/prices/bq.js', () => ({
  PricesBq: class {
    loadExpectedSku(...a: unknown[]) { return expectedMock(...a); }
    appendRaw(...a: unknown[]) { return appendMock(...a); }
    observationStart(...a: unknown[]) { return startMock(...a); }
    observationFinalize(...a: unknown[]) { return finalizeMock(...a); }
    countObservationRows(...a: unknown[]) { return countMock(...a); }
  },
}));

const { pricesLoader } = await import('../src/loaders/prices/index.js');

const ITEM = (nm: number) => ({
  nmID: nm, vendorCode: 'v',
  sizes: [{ sizeID: nm * 10, price: 1600, discountedPrice: 640, clubDiscountedPrice: 640, techSizeName: '0' }],
  currencyIsoCode4217: 'RUB', discount: 60, clubDiscount: 0, editableSizePrice: false,
});

function ctx(): LoaderContext {
  const config = {
    projectId: 'p', bqLocation: 'EU', rawDataset: 'wb_raw', environment: 'prod',
    wbHttpTimeoutMs: 1000, refSkuTable: 'REF_SKU_MASTER',
    wbPricesHost: 'https://host', wbPricesSecret: 'WB_PRICES_READ_TOKEN',
    pricesRawTable: 'RAW_WB_PRICES', pricesObservationsTable: 'WB_PRICES_OBSERVATIONS',
  } as unknown as Config;
  return {
    config, logger: new Logger({}, 'error'),
    logicalPeriod: '2026-09-07T10:20', runId: 'run-1', targetDate: '2026-09-07T10:20',
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  expectedMock.mockResolvedValue(new Map([[1, 'EVT-A'], [2, 'EVT-B']]));
  appendMock.mockResolvedValue('LOADED');
  startMock.mockResolvedValue(undefined);
  finalizeMock.mockResolvedValue(undefined);
  countMock.mockResolvedValue(2);
});

describe('успешное наблюдение', () => {
  it('пишет строки и закрывает манифест как COMPLETE со 100 % покрытием', async () => {
    fetchMock.mockResolvedValue({ items: [ITEM(1), ITEM(2)], httpStatus: 200, attempts: 1, pages: 1 });
    const res = await pricesLoader(ctx());
    expect(res).toEqual({ rowsFetched: 2, rowsLoaded: 2 });
    const fin = finalizeMock.mock.calls[0][1];
    expect(fin.status).toBe('COMPLETE');
    expect(fin.coveragePct).toBe(100);
    expect(fin.missing).toBe(0);
    expect(fin.schemaStatus).toBe('OK');
    expect(fin.observedAtIso).toBeTruthy();
  });

  it('использует детерминированный jobId окна', async () => {
    fetchMock.mockResolvedValue({ items: [ITEM(1), ITEM(2)], httpStatus: 200, attempts: 1, pages: 1 });
    await pricesLoader(ctx());
    expect(appendMock.mock.calls[0][2]).toBe('wbprices_prod_202609071020_raw_wb_prices');
  });

  it('повтор окна → REUSED, история не задваивается', async () => {
    fetchMock.mockResolvedValue({ items: [ITEM(1), ITEM(2)], httpStatus: 200, attempts: 1, pages: 1 });
    appendMock.mockResolvedValue('REUSED');
    await pricesLoader(ctx());
    expect(finalizeMock.mock.calls[0][1].status).toBe('REUSED');
  });
});

describe('fail-closed: никаких выдуманных наблюдений', () => {
  it('пустой ответ WB → отказ, а не «снимок без товаров»', async () => {
    fetchMock.mockResolvedValue({ items: [], httpStatus: 200, attempts: 1, pages: 1 });
    await expect(pricesLoader(ctx())).rejects.toThrow(LoaderError);
    expect(appendMock).not.toHaveBeenCalled();
    expect(finalizeMock.mock.calls[0][1].status).toBe('ERROR');
    expect(finalizeMock.mock.calls[0][1].errorCode).toBe('WB_PRICES_EMPTY');
  });

  it('исчезнувшее обязательное поле → отказ, ничего не пишем', async () => {
    const broken = { ...ITEM(1) } as Record<string, unknown>;
    delete broken.sizes;
    fetchMock.mockResolvedValue({ items: [broken], httpStatus: 200, attempts: 1, pages: 1 });
    await expect(pricesLoader(ctx())).rejects.toThrow(/обязательные поля/);
    expect(appendMock).not.toHaveBeenCalled();
    expect(finalizeMock.mock.calls[0][1].errorCode).toBe('WB_PRICES_SCHEMA_MISSING');
  });

  it('ошибка API → манифест ERROR, исключение наружу', async () => {
    fetchMock.mockRejectedValue(new LoaderError('HTTP 429', 'WB_PRICES_HTTP'));
    await expect(pricesLoader(ctx())).rejects.toThrow(/429/);
    expect(finalizeMock.mock.calls[0][1].status).toBe('ERROR');
    expect(finalizeMock.mock.calls[0][1].observedAtIso).toBeNull();
  });

  it('пустой RAW после append → отказ (пост-проверка записи)', async () => {
    fetchMock.mockResolvedValue({ items: [ITEM(1), ITEM(2)], httpStatus: 200, attempts: 1, pages: 1 });
    countMock.mockResolvedValue(0);
    await expect(pricesLoader(ctx())).rejects.toThrow(LoaderError);
    expect(finalizeMock.mock.calls[0][1].errorCode).toBe('WB_PRICES_POSTCOUNT_EMPTY');
  });
});

describe('покрытие: неполный снимок отличим от полного', () => {
  it('пропавший товар фиксируется, остальные сохраняются', async () => {
    fetchMock.mockResolvedValue({ items: [ITEM(1)], httpStatus: 200, attempts: 1, pages: 1 });
    countMock.mockResolvedValue(1);
    const res = await pricesLoader(ctx());
    expect(res.rowsLoaded).toBe(1);           // наблюдение первого товара НЕ потеряно
    const fin = finalizeMock.mock.calls[0][1];
    expect(fin.status).toBe('COMPLETE');
    expect(fin.coveragePct).toBe(50);
    expect(fin.missingNmIds).toBe('2');
  });

  it('неожиданный товар записан и помечен, internal_sku пуст', async () => {
    fetchMock.mockResolvedValue({ items: [ITEM(1), ITEM(2), ITEM(77)], httpStatus: 200, attempts: 1, pages: 1 });
    countMock.mockResolvedValue(3);
    await pricesLoader(ctx());
    const fin = finalizeMock.mock.calls[0][1];
    expect(fin.unexpectedNmIds).toBe('77');
    expect(fin.coveragePct).toBe(100);        // лишний товар покрытие не портит, но виден отдельно
    const rows = appendMock.mock.calls[0][1] as Array<{ nm_id: number; internal_sku: string | null }>;
    expect(rows.find((r) => r.nm_id === 77)?.internal_sku).toBeNull();
  });
});

describe('дрейф схемы не роняет наблюдение', () => {
  it('новое поле WB → COMPLETE со статусом DRIFT_NEW_FIELDS', async () => {
    fetchMock.mockResolvedValue({
      items: [{ ...ITEM(1), brandNewField: 1 }, ITEM(2)], httpStatus: 200, attempts: 1, pages: 1,
    });
    await pricesLoader(ctx());
    const fin = finalizeMock.mock.calls[0][1];
    expect(fin.status).toBe('COMPLETE');
    expect(fin.schemaStatus).toBe('DRIFT_NEW_FIELDS');
    expect(fin.schemaUnknownFields).toContain('item.brandNewField');
  });
});

describe('манифест открывается до похода в WB', () => {
  it('observationStart вызван раньше запроса к API', async () => {
    fetchMock.mockResolvedValue({ items: [ITEM(1), ITEM(2)], httpStatus: 200, attempts: 1, pages: 1 });
    await pricesLoader(ctx());
    expect(startMock).toHaveBeenCalledTimes(1);
    expect(startMock.mock.calls[0][1]).toMatchObject({
      observationId: 'WBPX_prod_202609071020', bucket: '2026-09-07T10:20', environment: 'prod',
    });
  });
});
