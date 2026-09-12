import { describe, it, expect } from 'vitest';
import { normalizeFunnelRows, parseHistoryBody } from '../src/loaders/funnel/normalize.js';
import { funnelWindow, effectiveLookback, funnelObservationId, funnelLoadJobId } from '../src/loaders/funnel/index.js';
import { chunk, MAX_NM_PER_REQUEST } from '../src/loaders/funnel/wbApi.js';

const meta = {
  observationId: 'WBFN_prod_20260904_20260910',
  environment: 'prod',
  runId: 'run-1',
  observedAtIso: '2026-09-11T06:30:00.000Z',
  sourceEndpoint: 'POST /api/analytics/v3/sales-funnel/products/history',
};

// Форма ответа — из примера dev.wildberries.ru/docs/openapi/analytics.
const item = {
  product: { nmId: 252442517, title: 'Крем для рук', vendorCode: 'x', brandName: 'EVETIS', subjectId: 1, subjectName: 'Кремы' },
  history: [
    { date: '2026-09-04', openCount: 340, cartCount: 41, orderCount: 11, orderSum: 5489, buyoutCount: 8, buyoutSum: 3992,
      buyoutPercent: 72, addToCartConversion: 12, cartToOrderConversion: 27, addToWishlistCount: 3 },
    { date: '2026-09-05', openCount: 0, cartCount: 0, orderCount: 0, orderSum: 0, buyoutCount: 0, buyoutSum: 0,
      buyoutPercent: 0, addToCartConversion: 0, cartToOrderConversion: 0, addToWishlistCount: 0 },
  ],
  currency: 'RUB',
};

describe('воронка: нормализация ответа products/history', () => {
  it('переходы = openCount, корзины = cartCount, оригинал сохраняется в raw_row_json', () => {
    const { rows, unknownFields, rejected } = normalizeFunnelRows([item], meta);
    expect(rejected).toBe(0);
    expect(unknownFields).toEqual([]);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ date_msk: '2026-09-04', nm_id: 252442517, open_card_count: 340, add_to_cart_count: 41,
      orders_count: 11, orders_sum_rub: 5489, add_to_wishlist: 3, cancel_count: null });
    // RAW обязан хранить оригинал: без него расхождение с кабинетом не разобрать
    const raw = JSON.parse(rows[0]!.raw_row_json);
    expect(raw).toMatchObject({ nmId: 252442517, currency: 'RUB', date: '2026-09-04', openCount: 340, cartCount: 41 });
  });

  it('нулевой день — строка с нулями, а не пропуск', () => {
    const { rows } = normalizeFunnelRows([item], meta);
    expect(rows[1]).toMatchObject({ date_msk: '2026-09-05', open_card_count: 0, add_to_cart_count: 0 });
  });

  it('дрейф схемы WB попадает в unknownFields с уровнем, а не теряется молча', () => {
    const { unknownFields } = normalizeFunnelRows([{
      product: { nmId: 1, rating: 5 }, history: [{ date: '2026-09-04', someNewMetric: 5 }], currency: 'RUB', extra: 1,
    }], meta);
    expect(unknownFields).toEqual(['history.someNewMetric', 'item.extra', 'product.rating']);
  });

  it('запись без nmId или даты отбраковывается и считается', () => {
    const { rows, rejected } = normalizeFunnelRows([
      { product: {}, history: [{ date: '2026-09-04' }] },
      { product: { nmId: 1 }, history: [{ openCount: 1 }, { date: 'нет' }] },
    ], meta);
    expect(rows).toHaveLength(0);
    expect(rejected).toBe(3);
  });

  it('разбирает массив и обёртку {data:[]}, прочее — отказ', () => {
    expect(parseHistoryBody(JSON.stringify([item]))).toHaveLength(1);
    expect(parseHistoryBody(JSON.stringify({ data: [item, item] }))).toHaveLength(2);
    expect(() => parseHistoryBody('{"x":1}')).toThrow();
  });
});

describe('воронка: окно, батчи и идентификаторы', () => {
  it('окно включает целевой день и перекрытие назад', () => {
    expect(funnelWindow('2026-09-10', 7)).toEqual({ startDate: '2026-09-04', endDate: '2026-09-10' });
    expect(funnelWindow('2026-09-10', 1)).toEqual({ startDate: '2026-09-10', endDate: '2026-09-10' });
  });

  it('глубина не превышает недели WB и не меньше суток', () => {
    expect(effectiveLookback(14)).toBe(7);
    expect(effectiveLookback(7)).toBe(7);
    expect(effectiveLookback(0)).toBe(1);
  });

  it('nmID режутся на запросы по 20', () => {
    const ids = Array.from({ length: 25 }, (_, i) => i + 1);
    const batches = chunk(ids, MAX_NM_PER_REQUEST);
    expect(batches.map((b) => b.length)).toEqual([20, 5]);
    expect(batches.flat()).toEqual(ids);
  });

  it('идентификаторы детерминированы — повтор окна не удваивает строки', () => {
    const w = funnelWindow('2026-09-10', 7);
    expect(funnelObservationId('prod', w)).toBe('WBFN_prod_20260904_20260910');
    expect(funnelLoadJobId('prod', w)).toBe('wbfunnel_prod_20260904_20260910');
  });
});
