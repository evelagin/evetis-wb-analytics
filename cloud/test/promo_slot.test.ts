import { describe, it, expect } from 'vitest';
import { PROMO_SLOT_HOURS_UTC, promoSlot, promoObservationId, promoLoadJobId } from '../src/loaders/promo/slot.js';

describe('promoSlot — логический период наблюдателя акций', () => {
  it('ровно в момент запуска слот равен этому часу', () => {
    expect(promoSlot(new Date('2026-09-22T04:00:00Z'))).toBe('2026-09-22T04:00');
    expect(promoSlot(new Date('2026-09-22T19:00:00Z'))).toBe('2026-09-22T19:00');
  });

  it('внутри слота период не меняется — ретрай подавляется guard-ом', () => {
    const a = promoSlot(new Date('2026-09-22T09:00:10Z'));
    const b = promoSlot(new Date('2026-09-22T13:59:59Z'));
    expect(a).toBe('2026-09-22T09:00');
    expect(b).toBe('2026-09-22T09:00');
  });

  it('следующий слот даёт НОВЫЙ период — наблюдение не схлопывается в сутки', () => {
    const slots = ['02', '05', '10', '15', '20'].map((h) => promoSlot(new Date(`2026-09-22T${h}:30:00Z`)));
    expect(new Set(slots).size).toBe(5);
  });

  it('до первого слота суток принадлежим последнему слоту предыдущих суток', () => {
    expect(promoSlot(new Date('2026-09-22T00:00:00Z'))).toBe('2026-09-21T19:00');
    expect(promoSlot(new Date('2026-09-22T03:59:59Z'))).toBe('2026-09-21T19:00');
  });

  it('переход через границу месяца и года', () => {
    expect(promoSlot(new Date('2026-10-01T02:00:00Z'))).toBe('2026-09-30T19:00');
    expect(promoSlot(new Date('2027-01-01T01:00:00Z'))).toBe('2026-12-31T19:00');
  });

  it('за сутки получается ровно 4 различных слота — это и есть каденс', () => {
    const seen = new Set<string>();
    for (let h = 0; h < 24; h++) seen.add(promoSlot(new Date(`2026-09-22T${String(h).padStart(2, '0')}:30:00Z`)));
    // 4 слота текущих суток + хвост, принадлежащий последнему слоту предыдущих
    expect([...seen].filter((s) => s.startsWith('2026-09-22')).length).toBe(4);
    expect(PROMO_SLOT_HOURS_UTC.length).toBe(4);
  });

  it('некорректный список часов отвергается, а не молча искажает период', () => {
    expect(() => promoSlot(new Date(), [])).toThrow();
    expect(() => promoSlot(new Date(), [24])).toThrow();
    expect(() => promoSlot(new Date(), [-1])).toThrow();
  });
});

describe('идентификаторы снимка', () => {
  it('observationId детерминирован и различает маркетплейсы и окружения', () => {
    expect(promoObservationId('WB', 'prod', '2026-09-22T09:00')).toBe('WBPROMO_prod_2026092209:00'.replace(':', ''));
    expect(promoObservationId('WB', 'prod', '2026-09-22T09:00')).toBe(
      promoObservationId('WB', 'prod', '2026-09-22T09:00'),
    );
    expect(promoObservationId('WB', 'prod', '2026-09-22T09:00')).not.toBe(
      promoObservationId('WB', 'shadow', '2026-09-22T09:00'),
    );
  });

  it('jobId детерминирован по слоту и таблице — ключ идемпотентности записи', () => {
    const a = promoLoadJobId('WB', 'prod', '2026-09-22T09:00', 'RAW_WB_PROMO_CALENDAR');
    const b = promoLoadJobId('WB', 'prod', '2026-09-22T09:00', 'RAW_WB_PROMO_CALENDAR');
    const other = promoLoadJobId('WB', 'prod', '2026-09-22T14:00', 'RAW_WB_PROMO_CALENDAR');
    const otherTable = promoLoadJobId('WB', 'prod', '2026-09-22T09:00', 'RAW_WB_PROMO_RANGING');
    expect(a).toBe(b);
    expect(a).not.toBe(other);
    expect(a).not.toBe(otherTable);
    expect(a).toMatch(/^[A-Za-z0-9_-]+$/);
  });
});
