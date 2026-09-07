import { describe, it, expect } from 'vitest';
import { num, auditSchema, computeCoverage, normalizeGoods } from '../src/loaders/prices/normalize.js';
import { LoaderError } from '../src/errors.js';

/** Реальная форма ответа WB, снятая с production-аккаунта EVETIS 07.09.2026. */
const ITEM = {
  nmID: 252442517,
  vendorCode: 'Крем Руки',
  sizes: [{ sizeID: 1234567890, price: 1600, discountedPrice: 640, clubDiscountedPrice: 640, techSizeName: '0' }],
  currencyIsoCode4217: 'RUB',
  discount: 60,
  clubDiscount: 0,
  editableSizePrice: false,
};

const CTX = {
  observedAtIso: '2026-09-07T10:20:05.000Z',
  observationBucket: '2026-09-07T10:20',
  observationId: 'WBPX_prod_202609071020',
  environment: 'prod',
  runId: 'run-1',
  skuByNm: new Map<number, string>([[252442517, 'EVT-HC-HAND-300']]),
};

describe('num(): «нет данных» никогда не становится нулём', () => {
  it('пустые значения дают null, а не 0', () => {
    expect(num(undefined)).toBeNull();
    expect(num(null)).toBeNull();
    expect(num('')).toBeNull();
    expect(num('не число')).toBeNull();
    expect(num(NaN)).toBeNull();
  });
  it('ноль остаётся нулём — это валидная цена', () => {
    expect(num(0)).toBe(0);
    expect(num('0')).toBe(0);
  });
  it('обычные значения проходят как есть', () => {
    expect(num(640)).toBe(640);
    expect(num('925.6')).toBe(925.6);
  });
});

describe('auditSchema: дрейф контракта виден', () => {
  it('известный ответ → OK', () => {
    expect(auditSchema([ITEM]).status).toBe('OK');
  });

  it('новое поле WB → DRIFT_NEW_FIELDS, но не отказ', () => {
    const a = auditSchema([{ ...ITEM, somethingNew: 1 }]);
    expect(a.status).toBe('DRIFT_NEW_FIELDS');
    expect(a.unknownFields).toContain('item.somethingNew');
    expect(a.missingRequired).toEqual([]);
  });

  it('новое поле внутри sizes[] тоже замечается', () => {
    const a = auditSchema([{ ...ITEM, sizes: [{ ...ITEM.sizes[0], competitivePrice: 500 }] }]);
    expect(a.status).toBe('DRIFT_NEW_FIELDS');
    expect(a.unknownFields).toContain('sizes.competitivePrice');
  });

  it('исчезнувшее обязательное поле → DRIFT_MISSING_FIELDS', () => {
    const noSizes: Record<string, unknown> = { ...ITEM };
    delete noSizes.sizes;
    const a = auditSchema([noSizes]);
    expect(a.status).toBe('DRIFT_MISSING_FIELDS');
    expect(a.missingRequired).toContain('item.sizes');
  });

  it('исчезнувшая цена внутри размера → DRIFT_MISSING_FIELDS', () => {
    const noPrice: Record<string, unknown> = { ...ITEM.sizes[0] };
    delete noPrice.price;
    const a = auditSchema([{ ...ITEM, sizes: [noPrice] }]);
    expect(a.status).toBe('DRIFT_MISSING_FIELDS');
    expect(a.missingRequired).toContain('sizes.price');
  });

  it('документированные, но не приходящие поля не считаются дрейфом', () => {
    // wholesaleDiscountThreshold / isBadTurnover есть в документации WB, но
    // на аккаунте EVETIS не приходят. Их появление не должно быть «новым полем».
    const a = auditSchema([{ ...ITEM, wholesaleDiscountThreshold: [], isBadTurnover: false }]);
    expect(a.status).toBe('OK');
  });
});

describe('computeCoverage: неполный снимок отличим от полного', () => {
  it('полное покрытие', () => {
    const c = computeCoverage(new Set([1, 2, 3]), new Set([1, 2, 3]));
    expect(c).toMatchObject({ expected: 3, observed: 3, missing: 0, unexpected: 0, coveragePct: 100 });
  });

  it('пропавший товар снижает покрытие и попадает в список', () => {
    const c = computeCoverage(new Set([1, 2, 3, 4]), new Set([1, 2, 3]));
    expect(c.missing).toBe(1);
    expect(c.missingNmIds).toEqual([4]);
    expect(c.coveragePct).toBe(75);
  });

  it('лишний товар НЕ компенсирует пропавший', () => {
    // 24 из 25 ожидаемых + 1 неожиданный не должны давать «100 %».
    const c = computeCoverage(new Set([1, 2, 3, 4]), new Set([1, 2, 3, 99]));
    expect(c.coveragePct).toBe(75);
    expect(c.unexpectedNmIds).toEqual([99]);
    expect(c.observed).toBe(4);
  });

  it('пустое ожидание не делит на ноль', () => {
    expect(computeCoverage(new Set(), new Set([1])).coveragePct).toBe(0);
  });
});

describe('normalizeGoods: строки снимка', () => {
  it('раскладывает товар в строку с сохранением семантики полей', () => {
    const [row] = normalizeGoods([ITEM], CTX);
    expect(row.nm_id).toBe(252442517);
    expect(row.internal_sku).toBe('EVT-HC-HAND-300');
    // seller_list_price — ДО скидки продавца; seller_effective_price — ПОСЛЕ.
    expect(row.seller_list_price).toBe(1600);
    expect(row.seller_discount_pct).toBe(60);
    expect(row.seller_effective_price).toBe(640);
    expect(row.wb_club_discount_pct).toBe(0);
    expect(row.wb_club_price).toBe(640);
    expect(row.observed_at).toBe(CTX.observedAtIso);
    expect(row.observation_bucket).toBe(CTX.observationBucket);
  });

  it('сохраняет сырой элемент для форензики', () => {
    const [row] = normalizeGoods([ITEM], CTX);
    expect(JSON.parse(row.raw_item_json)).toEqual(ITEM);
  });

  it('товар без соответствия в справочнике даёт internal_sku=null, а не падение', () => {
    const [row] = normalizeGoods([{ ...ITEM, nmID: 999 }], CTX);
    expect(row.internal_sku).toBeNull();
    expect(row.nm_id).toBe(999);
  });

  it('отсутствующая цена → отказ, а не 0', () => {
    const noPrice: Record<string, unknown> = { ...ITEM.sizes[0] };
    delete noPrice.price;
    expect(() => normalizeGoods([{ ...ITEM, sizes: [noPrice] }], CTX)).toThrow(LoaderError);
    try {
      normalizeGoods([{ ...ITEM, sizes: [noPrice] }], CTX);
    } catch (e) {
      expect((e as LoaderError).code).toBe('WB_PRICES_NULL_PRICE');
    }
  });

  it('цена 0 — валидна и проходит', () => {
    const [row] = normalizeGoods([{ ...ITEM, sizes: [{ ...ITEM.sizes[0], price: 0 }] }], CTX);
    expect(row.seller_list_price).toBe(0);
  });

  it('пустой sizes[] → отказ', () => {
    expect(() => normalizeGoods([{ ...ITEM, sizes: [] }], CTX)).toThrow(/sizes/);
  });

  it('элемент без nmID → отказ', () => {
    const noNm: Record<string, unknown> = { ...ITEM };
    delete noNm.nmID;
    expect(() => normalizeGoods([noNm], CTX)).toThrow(LoaderError);
  });

  it('хэш строки детерминирован и различается между товарами', () => {
    const a = normalizeGoods([ITEM], CTX)[0].source_payload_hash;
    const b = normalizeGoods([ITEM], CTX)[0].source_payload_hash;
    const c = normalizeGoods([{ ...ITEM, nmID: 111 }], CTX)[0].source_payload_hash;
    expect(a).toBe(b);
    expect(a).not.toBe(c);
  });

  it('многоразмерный товар даёт строку на каждый размер', () => {
    const multi = {
      ...ITEM,
      sizes: [
        { sizeID: 1, price: 100, discountedPrice: 40, clubDiscountedPrice: 40, techSizeName: 'S' },
        { sizeID: 2, price: 200, discountedPrice: 80, clubDiscountedPrice: 80, techSizeName: 'M' },
      ],
    };
    const rows = normalizeGoods([multi], CTX);
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.size_id)).toEqual([1, 2]);
    expect(new Set(rows.map((r) => r.source_payload_hash)).size).toBe(2);
  });
});
