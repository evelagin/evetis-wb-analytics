/**
 * КАЛЕНДАРЬ НА УРОВНЕ ЖИЗНЕННОГО ЦИКЛА (Gate 10 §35).
 *
 * daysInMonth() уже покрыт в unitka_calendar.test.ts. Здесь проверяется другое: какая
 * ГЕОМЕТРИЯ реально создаётся продуктовым движком — заголовок, шапка, первый и последний
 * день, строка итога, — и что цепочка секций не разъезжается через 28/29/30/31 и границу года.
 * Ошибка в один ряд здесь означает факты, записанные в чужой месяц.
 */
import { describe, it, expect } from 'vitest';
import { ozonSectionGeometry } from '../src/loaders/unitka/ozon/contract.js';
import { resolveSections, sectionStep, daysInMonth as ozonDays } from '../src/loaders/unitka/ozon/lifecycle.js';
import { geometryAt, daysInMonth as wbDays, nextMonth } from '../src/loaders/unitka/calendar.js';

const BLOCKS = ['a', 'b'];

/** Живая привязка Ozon: «Сентябрь 2026» — заголовок 605, 30 дней. */
const OZ_SEPT = { titleRow: 605, days: 30 } as const;

describe('Ozon: фактическая геометрия создаваемой секции', () => {
  it('сентябрь 2026 → октябрь: 30 дней, заголовок 639, дни 641..671, итог 672', () => {
    const g = ozonSectionGeometry(2026, 10, OZ_SEPT.titleRow, OZ_SEPT.days, BLOCKS);
    expect(g).toMatchObject({
      key: '2026-10', daysInMonth: 31, titleRow: 639, headerRow: 640,
      firstDailyRow: 641, lastDailyRow: 671, mtdRow: 672,
    });
  });

  it.each([
    ['2026-10 → 2026-11', 2026, 11, 31, 30],
    ['2026-12 → 2027-01 (граница года)', 2027, 1, 31, 31],
    ['2027-01 → 2027-02 (28 дней)', 2027, 2, 31, 28],
    ['2027-02 → 2027-03', 2027, 3, 28, 31],
    ['2028-01 → 2028-02 (29 дней, високосный)', 2028, 2, 31, 29],
    ['2028-02 → 2028-03', 2028, 3, 29, 31],
  ])('%s: число дней и строка итога выводятся, а не задаются', (_n, y, m, prevDays, wantDays) => {
    const g = ozonSectionGeometry(y, m, 1000, prevDays, BLOCKS);
    expect(g.daysInMonth).toBe(wantDays);
    expect(g.titleRow).toBe(1000 + prevDays + 4);
    expect(g.lastDailyRow - g.firstDailyRow + 1).toBe(wantDays);
    expect(g.mtdRow).toBe(g.titleRow + wantDays + 2);
  });

  it('шаг секции = дни + 4 и у WB, и у Ozon — расхождение развалило бы книгу', () => {
    for (const d of [28, 29, 30, 31]) expect(sectionStep(d)).toBe(d + 4);
  });

  it('цепочка от сентября 2026 до марта 2028 идёт без наложений и без дыр', () => {
    let titleRow = OZ_SEPT.titleRow, days = OZ_SEPT.days;
    let y = 2026, m = 9;
    const seen: Array<{ key: string; titleRow: number; mtdRow: number }> = [];
    for (let i = 0; i < 18; i++) {
      m += 1; if (m === 13) { m = 1; y += 1; }
      const g = ozonSectionGeometry(y, m, titleRow, days, BLOCKS);
      if (seen.length) {
        const prev = seen[seen.length - 1]!;
        expect(g.titleRow, `${g.key} наложился на ${prev.key}`).toBeGreaterThan(prev.mtdRow);
        expect(g.titleRow - prev.mtdRow, `между ${prev.key} и ${g.key} не одна строка-разделитель`).toBe(2);
      }
      seen.push({ key: g.key, titleRow: g.titleRow, mtdRow: g.mtdRow });
      titleRow = g.titleRow; days = g.daysInMonth;
    }
    expect(seen.map((s) => s.key)).toContain('2027-02');
    expect(seen.map((s) => s.key)).toContain('2028-02');
    expect(seen.find((s) => s.key === '2027-02')!.mtdRow
      - seen.find((s) => s.key === '2027-02')!.titleRow).toBe(30);   // 28 дней + 2
    expect(seen.find((s) => s.key === '2028-02')!.mtdRow
      - seen.find((s) => s.key === '2028-02')!.titleRow).toBe(31);   // 29 дней + 2
  });

  it('число дней НЕ берётся из подписи листа — только из календаря', () => {
    expect(ozonDays('2027-02')).toBe(28);
    expect(ozonDays('2028-02')).toBe(29);
    expect(ozonDays('2026-10')).toBe(31);
  });
});

describe('Ozon: автосоздание месяца через продуктовый resolveSections', () => {
  const live = [{ monthKey: '2026-09', titleRow: 605, days: Array.from({ length: 30 }, (_, i) => i + 1), blocks: ['x', 'y'] }];

  it('месяц, которого нет в листе, планируется к созданию с верной геометрией', () => {
    const plans = resolveSections({
      live, windowMonths: ['2026-09', '2026-10'], firstActivity: {}, blockSlots: 22,
    });
    const oct = plans.find((p) => p.monthKey === '2026-10')!;
    expect(oct.isNew).toBe(true);
    expect(oct.titleRow).toBe(605 + 30 + 4);
    expect(oct.days).toBe(31);
    expect(oct.blocks).toEqual(['x', 'y']);         // состав наследуется
  });

  it('NEW_MONTH_IDEMPOTENCY: если секция уже есть — isNew=false и ничего не создаётся', () => {
    const withOct = [...live, { monthKey: '2026-10', titleRow: 639, days: Array.from({ length: 31 }, (_, i) => i + 1), blocks: ['x', 'y'] }];
    const plans = resolveSections({ live: withOct, windowMonths: ['2026-09', '2026-10'], firstActivity: {}, blockSlots: 22 });
    expect(plans.filter((p) => p.isNew)).toHaveLength(0);
    expect(plans.filter((p) => p.monthKey === '2026-10')).toHaveLength(1);   // DUPLICATE_MONTH_SECTIONS=0
  });

  it('движок НЕ достраивает прошлое: отсутствующая старая секция — отказ', () => {
    expect(() => resolveSections({
      live, windowMonths: ['2026-08', '2026-09'], firstActivity: {}, blockSlots: 22,
    })).toThrow(/НЕ новее последней|достраивать прошлое/);
  });

  it('новый SKU месяца попадает в состав новой секции, старые сохраняют порядок', () => {
    const plans = resolveSections({
      live, windowMonths: ['2026-09', '2026-10'],
      firstActivity: { z: '2026-10', x: '2026-04' }, blockSlots: 22,
    });
    const oct = plans.find((p) => p.monthKey === '2026-10')!;
    expect(oct.blocks).toEqual(['x', 'y', 'z']);      // z дописан В КОНЕЦ
    expect(oct.blocks.slice(0, 2)).toEqual(['x', 'y']);
  });
});

describe('WB: та же проверка на продуктовой геометрии', () => {
  it('октябрь 2026 создаётся ровно там, где кончается сентябрь', () => {
    const g = geometryAt({ year: 2026, month: 10 }, 769);
    expect(g).toMatchObject({ topRow: 769, firstDailyRow: 771, lastDailyRow: 801, mtdRow: 802, daysInMonth: 31 });
  });

  it.each([[2027, 2, 28], [2028, 2, 29], [2026, 11, 30], [2027, 1, 31]])(
    'WB %i-%i = %i дней в фактической геометрии', (y, m, want) => {
      const g = geometryAt({ year: y, month: m }, 1000);
      expect(g.daysInMonth).toBe(want);
      expect(g.lastDailyRow - g.firstDailyRow + 1).toBe(want);
      expect(wbDays(y, m)).toBe(want);
    });

  it('декабрь → январь увеличивает год', () => {
    expect(nextMonth({ year: 2026, month: 12 })).toEqual({ year: 2027, month: 1 });
  });
});
