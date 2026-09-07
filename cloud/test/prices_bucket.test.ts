import { describe, it, expect } from 'vitest';
import { observationBucket, pricesObservationId, pricesLoadJobId, OBSERVATION_BUCKET_MINUTES } from '../src/loaders/prices/bucket.js';
import { LOADERS, resolveLoader } from '../src/loaders/registry.js';
import { dailyPeriodMoscow } from '../src/period.js';

/**
 * PR-1 — контракт логического периода наблюдателя цен.
 *
 * Главный защищаемый инвариант: период НЕ суточный. Суточный ключ в LOADER_RUNS
 * подавил бы все прогоны после первого за день, и интрадей-наблюдатель молча
 * выродился бы в один снимок в сутки — ровно та ошибка, которую PR-1 устраняет.
 */
describe('observationBucket: окно наблюдения', () => {
  it('округляет минуты ВНИЗ до границы окна', () => {
    expect(observationBucket(new Date('2026-09-07T10:00:00Z'))).toBe('2026-09-07T10:00');
    expect(observationBucket(new Date('2026-09-07T10:19:59Z'))).toBe('2026-09-07T10:00');
    expect(observationBucket(new Date('2026-09-07T10:20:00Z'))).toBe('2026-09-07T10:20');
    expect(observationBucket(new Date('2026-09-07T10:39:59Z'))).toBe('2026-09-07T10:20');
    expect(observationBucket(new Date('2026-09-07T10:40:00Z'))).toBe('2026-09-07T10:40');
    expect(observationBucket(new Date('2026-09-07T10:59:59Z'))).toBe('2026-09-07T10:40');
  });

  it('работает в UTC, а не в локальной зоне', () => {
    // 23:50 UTC = 02:50 МСК следующих суток. Окно обязано остаться в UTC-сутках.
    expect(observationBucket(new Date('2026-09-07T23:50:00Z'))).toBe('2026-09-07T23:40');
  });

  it('детерминирован: два вызова на один момент дают одно окно', () => {
    const t = new Date('2026-09-07T10:25:33Z');
    expect(observationBucket(t)).toBe(observationBucket(t));
  });

  it('соседние окна различаются — иначе наблюдения схлопнулись бы', () => {
    const a = observationBucket(new Date('2026-09-07T10:10:00Z'));
    const b = observationBucket(new Date('2026-09-07T10:30:00Z'));
    expect(a).not.toBe(b);
  });

  it('отвергает ширину окна, не делящую час нацело', () => {
    expect(() => observationBucket(new Date(), 7)).toThrow();
    expect(() => observationBucket(new Date(), 0)).toThrow();
    expect(() => observationBucket(new Date(), 61)).toThrow();
  });

  it('ширина окна по умолчанию — 20 минут', () => {
    expect(OBSERVATION_BUCKET_MINUTES).toBe(20);
  });
});

describe('детерминированные идентификаторы', () => {
  it('observationId стабилен для окна и различается между окнами', () => {
    expect(pricesObservationId('prod', '2026-09-07T10:20')).toBe('WBPX_prod_202609071020');
    expect(pricesObservationId('prod', '2026-09-07T10:20')).toBe(pricesObservationId('prod', '2026-09-07T10:20'));
    expect(pricesObservationId('prod', '2026-09-07T10:40')).not.toBe(pricesObservationId('prod', '2026-09-07T10:20'));
  });

  it('observationId разделяет окружения', () => {
    expect(pricesObservationId('shadow', '2026-09-07T10:20')).not.toBe(pricesObservationId('prod', '2026-09-07T10:20'));
  });

  it('loadJobId стабилен внутри окна — BigQuery дедуплицирует повтор', () => {
    const a = pricesLoadJobId('prod', '2026-09-07T10:20', 'RAW_WB_PRICES');
    expect(a).toBe(pricesLoadJobId('prod', '2026-09-07T10:20', 'RAW_WB_PRICES'));
    expect(a).toBe('wbprices_prod_202609071020_raw_wb_prices');
  });

  it('loadJobId различается между окнами — законное наблюдение не дедуплицируется', () => {
    expect(pricesLoadJobId('prod', '2026-09-07T10:20', 'RAW_WB_PRICES'))
      .not.toBe(pricesLoadJobId('prod', '2026-09-07T10:40', 'RAW_WB_PRICES'));
  });
});

describe('registry: регистрация наблюдателя цен', () => {
  it('prices зарегистрирован и не prodOnly', () => {
    expect(resolveLoader('prices')).toBeDefined();
    expect(LOADERS.prices.prodOnly).toBeFalsy();
  });

  it('период prices — окно, а НЕ сутки', () => {
    const now = new Date('2026-09-07T10:25:00Z');
    expect(LOADERS.prices.logicalPeriod(now)).toBe('2026-09-07T10:25'.slice(0, 14) + '20');
    expect(LOADERS.prices.logicalPeriod(now)).not.toBe(dailyPeriodMoscow(now));
  });
});
