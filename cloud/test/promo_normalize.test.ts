import { describe, it, expect } from 'vitest';
import {
  auditSchema,
  auditNomenclatureSchema,
  buildCalendarRows,
  buildNomenclatureRows,
  buildRangingRows,
  isAutoPromotion,
  num,
  ts,
  type PromoRowContext,
} from '../src/loaders/promo/normalize.js';
import type { RawPromotion } from '../src/loaders/promo/wbApi.js';

const CTX: PromoRowContext = {
  observedAtIso: '2026-09-22T13:24:00.000Z',
  observationBucket: '2026-09-22T09:00',
  observationId: 'WBPROMO_prod_20260922T0900',
  environment: 'prod',
  runId: 'run-1',
  sourceEndpoint: 'GET /api/v1/calendar/promotions',
};

/** Снимок живого ответа WB 2026-09-22, урезанный и обезличенный. */
const LIST: RawPromotion[] = [
  { id: 2940, name: 'Большая распродажа - 2 (автоматические скидки)', startDateTime: '2026-09-27T21:00:00Z', endDateTime: '2026-10-21T20:59:59Z', type: 'auto' },
  { id: 2873, name: 'Осенние скидки (автоматические скидки)', startDateTime: '2026-09-11T21:00:00Z', endDateTime: '2026-10-09T20:59:59Z', type: 'auto' },
];
const DETAILS: RawPromotion[] = [
  {
    id: 2940,
    name: 'Большая распродажа - 2 (автоматические скидки)',
    description: 'Текст условий',
    advantages: ['Плашка на карточке товара', 'Баннер на сайте'],
    startDateTime: '2026-09-27T21:00:00Z',
    endDateTime: '2026-10-21T20:59:59Z',
    inPromoActionLeftovers: 1,
    inPromoActionTotal: 1,
    notInPromoActionLeftovers: 4,
    notInPromoActionTotal: 4,
    participationPercentage: 20,
    type: 'auto',
    exceptionProductsCount: 0,
    ranging: [
      { condition: 'calculateProducts', participationRate: 1, boost: 25 },
      { condition: 'calculateProducts', participationRate: 50, boost: 30 },
      { condition: 'calculateProducts', participationRate: 80, boost: 35 },
    ],
  },
];

describe('num / ts — NULL и 0 не смешиваются', () => {
  it('отсутствующее значение НИКОГДА не превращается в 0', () => {
    expect(num(undefined)).toBeNull();
    expect(num(null)).toBeNull();
    expect(num('')).toBeNull();
    expect(num('не число')).toBeNull();
    expect(num(0)).toBe(0);          // 0 — валидное значение, а не «нет данных»
  });
  it('пустая метка времени источника → null, а не эпоха 1970', () => {
    expect(ts('')).toBeNull();
    expect(ts(null)).toBeNull();
    expect(ts('2026-09-27T21:00:00Z')).toBe('2026-09-27T21:00:00.000Z');
  });
});

describe('buildCalendarRows — агрегат остаётся агрегатом', () => {
  const rows = buildCalendarRows(
    { list: LIST, detailsById: new Map([[2940, DETAILS[0] as RawPromotion]]), nomenclatureStatusById: new Map() },
    CTX,
  );

  it('одна строка на акцию снимка, без размножения по SKU', () => {
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.promotion_id).sort()).toEqual([2873, 2940]);
  });

  it('inPromoActionTotal сохраняется как факт УРОВНЯ АКЦИИ', () => {
    const r = rows.find((x) => x.promotion_id === 2940)!;
    expect(r.in_promo_total).toBe(1);
    expect(r.not_in_promo_total).toBe(4);
    expect(r.participation_pct).toBe(20);
    expect(r.exception_products_count).toBe(0);
  });

  it('автоакция честно помечена как не дающая состав по SKU', () => {
    for (const r of rows) {
      expect(r.is_auto_promotion).toBe(true);
      expect(r.sku_level_data_available).toBe(false);
      expect(r.nomenclature_status).toBe('SKIPPED_AUTO_PROMOTION');
    }
  });

  it('отсутствие деталей — details_available=false, счётчики NULL, а не нули', () => {
    const r = rows.find((x) => x.promotion_id === 2873)!;
    expect(r.details_available).toBe(false);
    expect(r.in_promo_total).toBeNull();
    expect(r.not_in_promo_total).toBeNull();
    expect(r.participation_pct).toBeNull();
    expect(r.ranging_tiers).toBeNull();
  });

  it('сырой payload сохраняется целиком — форензика и эволюция схемы', () => {
    const r = rows.find((x) => x.promotion_id === 2940)!;
    const raw = JSON.parse(r.raw_promotion_json);
    expect(raw.list.id).toBe(2940);
    expect(raw.details.ranging).toHaveLength(3);
  });

  it('hash строки детерминирован и различает акции', () => {
    const again = buildCalendarRows(
      { list: LIST, detailsById: new Map([[2940, DETAILS[0] as RawPromotion]]), nomenclatureStatusById: new Map() },
      CTX,
    );
    expect(again[0]?.source_payload_hash).toBe(rows[0]?.source_payload_hash);
    expect(rows[0]?.source_payload_hash).not.toBe(rows[1]?.source_payload_hash);
  });

  it('строка без id не создаётся — она была бы неадресуема', () => {
    const r = buildCalendarRows(
      { list: [{ name: 'без id', type: 'auto' }], detailsById: new Map(), nomenclatureStatusById: new Map() },
      CTX,
    );
    expect(r).toHaveLength(0);
  });
});

describe('buildRangingRows — лестница бустинга', () => {
  const rows = buildRangingRows(DETAILS, CTX);

  it('одна строка на ступень, порядок источника сохранён', () => {
    expect(rows).toHaveLength(3);
    expect(rows.map((r) => r.tier_ordinal)).toEqual([0, 1, 2]);
    expect(rows.map((r) => r.boost_pct)).toEqual([25, 30, 35]);
    expect(rows.map((r) => r.participation_rate)).toEqual([1, 50, 80]);
  });

  it('condition сохраняется как значение источника, без нормализации', () => {
    expect(new Set(rows.map((r) => r.condition))).toEqual(new Set(['calculateProducts']));
  });

  it('акция без ranging[] не даёт строк', () => {
    expect(buildRangingRows([{ id: 1, type: 'auto' }], CTX)).toHaveLength(0);
  });
});

describe('buildNomenclatureRows — состав акции (regular)', () => {
  const items = [
    { id: 162579635, inAction: true, price: 1500, currencyCode: 'RUB', planPrice: 1000, discount: 15, planDiscount: 34 },
    { id: 999999999, inAction: false, price: 700, currencyCode: 'RUB', planPrice: 500, discount: 60, planDiscount: 70 },
  ];
  const skuByNm = new Map<number, string>([[162579635, 'EVT-TEST-1']]);
  const rows = buildNomenclatureRows(2728, true, items, skuByNm, CTX);

  it('плановая акционная цена сохраняется — это требуемая цена акции', () => {
    expect(rows[0]?.plan_price).toBe(1000);
    expect(rows[0]?.plan_discount_pct).toBe(34);
  });

  it('нерезолвленный nm_id НЕ отбрасывается: internal_sku = NULL, строка остаётся', () => {
    expect(rows).toHaveLength(2);
    expect(rows[0]?.internal_sku).toBe('EVT-TEST-1');
    expect(rows[1]?.internal_sku).toBeNull();
  });

  it('in_action_requested входит в идентичность строки — запросы true и false различаются', () => {
    const other = buildNomenclatureRows(2728, false, items, skuByNm, CTX);
    expect(other[0]?.source_payload_hash).not.toBe(rows[0]?.source_payload_hash);
    expect(other[0]?.in_action_requested).toBe(false);
  });
});

describe('аудит контракта', () => {
  it('известные поля — OK', () => {
    expect(auditSchema(LIST, DETAILS).status).toBe('OK');
  });

  it('новое поле WB не роняет прогон, но видно как дрейф', () => {
    const a = auditSchema([{ ...LIST[0], somethingNew: 1 } as RawPromotion], DETAILS);
    expect(a.status).toBe('DRIFT_NEW_FIELDS');
    expect(a.unknownFields).toContain('list.somethingNew');
  });

  it('новое поле внутри ranging[] тоже видно', () => {
    const d = [{ ...DETAILS[0], ranging: [{ condition: 'x', participationRate: 1, boost: 5, extra: 9 }] } as RawPromotion];
    expect(auditSchema(LIST, d).unknownFields).toContain('ranging.extra');
  });

  it('пропажа ОБЯЗАТЕЛЬНОГО поля — отдельный статус, прогон обязан упасть', () => {
    const a = auditSchema([{ name: 'без типа и id' } as RawPromotion], []);
    expect(a.status).toBe('DRIFT_MISSING_FIELDS');
    expect(a.missingRequired).toEqual(['list.id', 'list.type']);
  });

  it('дрейф схемы состава акции аудируется отдельно', () => {
    expect(auditNomenclatureSchema([{ id: 1, newField: 2 }])).toEqual(['nomenclature.newField']);
  });
});

describe('isAutoPromotion', () => {
  it('различает auto и regular по значению источника', () => {
    expect(isAutoPromotion({ type: 'auto' })).toBe(true);
    expect(isAutoPromotion({ type: 'regular' })).toBe(false);
    expect(isAutoPromotion({})).toBe(false);
  });
});
