/**
 * UNITKA CALENDAR V2 (Phase 2B) — календарь, геометрия секции, слоты, поиск секции, предпроверка.
 */
import { describe, it, expect } from 'vitest';
import {
  daysInMonth, nextMonth, previousMonth, monthTitle, formatMonthKey, parseMonthKey, monthKeyOf, dayIndexOf, dayIso,
  geometryAt, dayRowOf, rowOfDate, slotStart, slotEnd, nextFreeSlot, chainGaps, locateSection, layoutOf, blockLastColumn, blockOfColumn, insertGeometry, tailStartOf, BLOCK_BODY_WIDTH, TAIL_WIDTH } from '../src/loaders/unitka/calendar.js';
import { colA1, findBlocks, isBookAnchorCell, BOOK_ANCHORS, integrityStatusCells } from '../src/loaders/unitka/model.js';
import { nextMonthPrecheck, resolveSection } from '../src/loaders/unitka/section.js';
import { LoaderError } from '../src/errors.js';

const codeOf = (fn: () => unknown): string => { try { fn(); return 'NO_ERROR'; } catch (e) { return e instanceof LoaderError ? e.code : 'OTHER'; } };

describe('календарь: длина месяца — только вычислением', () => {
  it.each([
    [2026, 9, 30], [2026, 10, 31], [2026, 11, 30], [2026, 12, 31], [2027, 1, 31],
    [2027, 2, 28], [2028, 2, 29], [2100, 2, 28], [2000, 2, 29], [1900, 2, 28], [2024, 2, 29],
  ])('%i-%i = %i', (y, m, d) => { expect(daysInMonth(y, m)).toBe(d); });
  it('декабрь → январь: год увеличивается; январь ← декабрь', () => {
    expect(nextMonth({ year: 2026, month: 12 })).toEqual({ year: 2027, month: 1 });
    expect(previousMonth({ year: 2027, month: 1 })).toEqual({ year: 2026, month: 12 });
    expect(nextMonth({ year: 2026, month: 9 })).toEqual({ year: 2026, month: 10 });
  });
  it('заголовки и ключи', () => {
    expect(monthTitle({ year: 2026, month: 10 })).toBe('Октябрь 2026');
    expect(monthTitle({ year: 2027, month: 1 })).toBe('Январь 2027');
    expect(formatMonthKey(parseMonthKey('2028-02'))).toBe('2028-02');
    expect(monthKeyOf('2026-10-31')).toEqual({ year: 2026, month: 10 });
    expect(() => parseMonthKey('2026-13')).toThrow();
    expect(() => daysInMonth(2026, 0)).toThrow();
  });
  it('дата ↔ индекс дня: никакой межмесячной «перетечки»', () => {
    expect(dayIndexOf({ year: 2026, month: 10 }, '2026-10-31')).toBe(30);
    expect(dayIso({ year: 2028, month: 2 }, 28)).toBe('2028-02-29');
    expect(() => dayIso({ year: 2027, month: 2 }, 28)).toThrow();            // 29.02.2027 нет
    expect(() => dayIndexOf({ year: 2026, month: 10 }, '2026-11-01')).toThrow();
    expect(() => dayIndexOf({ year: 2027, month: 2 }, '2027-02-29')).toThrow();
  });
});

describe('геометрия секции: 28/29/30/31, MTD, разделитель, следующая секция', () => {
  it('сентябрь 2026 (историческая секция): 735/736/737..766/767/768', () => {
    const g = geometryAt({ year: 2026, month: 9 }, 735);
    expect(g).toMatchObject({ topRow: 735, headerRow: 736, firstDailyRow: 737, lastDailyRow: 766, mtdRow: 767, spacerRow: 768, nextTopRow: 769, daysInMonth: 30, title: 'Сентябрь 2026' });
  });
  it('октябрь 2026: 769/770/771..801/802/803', () => {
    const g = geometryAt({ year: 2026, month: 10 }, 769);
    expect(g).toMatchObject({ topRow: 769, headerRow: 770, firstDailyRow: 771, lastDailyRow: 801, mtdRow: 802, spacerRow: 803, nextTopRow: 804, daysInMonth: 31 });
    expect(rowOfDate(g, '2026-10-01')).toBe(771);
    expect(rowOfDate(g, '2026-10-31')).toBe(801);
    expect(() => rowOfDate(g, '2026-11-01')).toThrow();
    expect(() => dayRowOf(g, 31)).toThrow();
  });
  it('цепочка секций от сентября 2026 до марта 2028: шаг days + 4, 28/29 февраля', () => {
    let key = { year: 2026, month: 9 }; let top = 735;
    const seen: Record<string, [number, number, number, number]> = {};
    for (let i = 0; i < 19; i++) {
      const g = geometryAt(key, top);
      seen[g.monthKey] = [g.topRow, g.firstDailyRow, g.lastDailyRow, g.mtdRow];
      expect(g.lastDailyRow - g.firstDailyRow + 1).toBe(g.daysInMonth);
      expect(g.mtdRow).toBe(g.lastDailyRow + 1);
      top = g.nextTopRow; key = nextMonth(key);
    }
    expect(seen['2026-10']).toEqual([769, 771, 801, 802]);
    expect(seen['2026-11']).toEqual([804, 806, 835, 836]);
    expect(seen['2026-12']).toEqual([838, 840, 870, 871]);
    expect(seen['2027-01']).toEqual([873, 875, 905, 906]);
    expect(seen['2027-02']).toEqual([908, 910, 937, 938]);   // 28 дней
    expect(seen['2027-03']).toEqual([940, 942, 972, 973]);
    expect(seen['2028-02']).toEqual([1321, 1323, 1351, 1352]); // 29 дней
    expect(seen['2028-03']).toEqual([1354, 1356, 1386, 1387]);
  });
  it.each([
    ['2026-09-30', '2026-10-01', 766, 771], ['2026-10-31', '2026-11-01', 801, 806], ['2026-12-31', '2027-01-01', 870, 875],
    ['2027-02-28', '2027-03-01', 937, 942], ['2028-02-29', '2028-03-01', 1351, 1356],
  ])('переход %s → %s: последняя строка %i, первая строка следующего %i', (a, b, ra, rb) => {
    let key = { year: 2026, month: 9 }; let top = 735;
    const tops = new Map<string, number>();
    for (let i = 0; i < 19; i++) { tops.set(formatMonthKey(key), top); top = geometryAt(key, top).nextTopRow; key = nextMonth(key); }
    const ka = monthKeyOf(a), kb = monthKeyOf(b);
    expect(rowOfDate(geometryAt(ka, tops.get(formatMonthKey(ka))!), a)).toBe(ra);
    expect(rowOfDate(geometryAt(kb, tops.get(formatMonthKey(kb))!), b)).toBe(rb);
    expect(formatMonthKey(nextMonth(ka))).toBe(formatMonthKey(kb));
  });
});

describe('слоты блоков: сплошная цепочка, дыр нет', () => {
  it('слот 0 = M..AJ, слот 23 = US..VP, слот 24 = VQ..WN (589..612) — сразу за VP, слот 25 = WO..XL', () => {
    expect([colA1(slotStart(0)), colA1(slotEnd(0))]).toEqual(['M', 'AJ']);
    expect([colA1(slotStart(23)), colA1(slotEnd(23))]).toEqual(['US', 'VP']);
    expect([slotStart(24), slotEnd(24), colA1(slotStart(24)), colA1(slotEnd(24))]).toEqual([589, 612, 'VQ', 'WN']);
    expect(slotStart(24)).toBe(slotEnd(23) + 1);
    expect([colA1(slotStart(25)), colA1(slotEnd(25))]).toEqual(['WO', 'XL']);
  });
  it.each([[1], [24], [25], [26], [40]])('распределение %i SKU подряд — слоты 0..n-1 без пропусков', (n) => {
    const slots: number[] = []; let s = -1;
    for (let i = 0; i < n; i++) { s = nextFreeSlot(s); slots.push(s); }
    expect(slots).toEqual(Array.from({ length: n }, (_, i) => i));
    expect(chainGaps(slots.map((slot) => ({ slot })))).toEqual([]);
  });
  it('дыры в цепочке: выбывший слот 1 — отчёт [1]; пустой список — []', () => {
    expect(chainGaps([{ slot: 0 }, { slot: 2 }, { slot: 3 }])).toEqual([1]);
    expect(chainGaps([])).toEqual([]);
  });
  it('findBlocks: пустые слоты пропускаются, 25-й блок найден в VQ, хвост книги за блоками nmID не содержит', () => {
    const row: (string | number | null)[] = Array(636).fill('');
    row[slotStart(0) - 1] = '252442517 Крем';
    row[slotStart(2) - 1] = '252441968 Набор';            // слот 1 пуст (выбывший SKU)
    row[slotStart(24) - 1] = '909951444 Набор анти-акне';
    row[slotStart(25) + 10] = 'LAST_CLOSED_DATE (зеркало)'; // подпись хвоста — не блок
    const b = findBlocks(row, 636);
    expect(b.map((x) => [x.slot, x.nmId])).toEqual([[0, 252442517], [2, 252441968], [24, 909951444]]);
    expect(layoutOf(geometryAt({ year: 2026, month: 10 }, 769), b).lastBlockColumn).toBe(611);   // последняя метрика блока 25 (WM)
  });
  it('якорь книги — только пара (строка, колонка) при заданной колонке якорей; строки статуса Guard следуют за ней', () => {
    expect(isBookAnchorCell(BOOK_ANCHORS.LCD_MIRROR_ROW, 600, 600)).toBe(true);
    expect(isBookAnchorCell(BOOK_ANCHORS.REVERSE_ROW, 624, 624)).toBe(true);
    expect(isBookAnchorCell(771, 600, 600)).toBe(false);
    expect(isBookAnchorCell(736, 600, 624)).toBe(false);
    expect(integrityStatusCells(624)).toEqual({ status: { row: 738, col: 624 }, invalidRows: { row: 739, col: 624 } });
    expect(BOOK_ANCHORS).toEqual({ LCD_MIRROR_ROW: 736, REVERSE_ROW: 737, STATUS_ROW: 738, INVALID_ROW: 739 });
  });
});

describe('поиск секции по заголовку в колонке A', () => {
  const colA = (entries: Record<number, string>, n = 803): (string | null)[] => Array.from({ length: n }, (_, i) => entries[i + 1] ?? null);
  const meta = (rowCount: number) => ({ sheetId: 1, rowCount, columnCount: 636, anchorCol: 600 });
  it('валидная: ровно один заголовок', () => {
    expect(locateSection(colA({ 356: 'Октябрь 2025', 735: 'Сентябрь 2026', 769: 'Октябрь 2026' }), { year: 2026, month: 10 })).toEqual({ status: 'FOUND', topRow: 769 });
    expect(resolveSection(colA({ 769: 'Октябрь 2026' }), meta(803), { year: 2026, month: 10 }).firstDailyRow).toBe(771);
  });
  it('отсутствует — MONTH_SECTION_MISSING (прежний MONTH_ROLLOVER_REQUIRED)', () => {
    expect(codeOf(() => resolveSection(colA({ 735: 'Сентябрь 2026' }, 768), meta(768), { year: 2026, month: 10 }))).toBe('MONTH_SECTION_MISSING');
  });
  it('неоднозначна — MONTH_SECTION_AMBIGUOUS', () => {
    expect(codeOf(() => resolveSection(colA({ 769: 'Октябрь 2026', 804: ' Октябрь 2026 ' }, 900), meta(900), { year: 2026, month: 10 }))).toBe('MONTH_SECTION_AMBIGUOUS');
  });
  it('секция не помещается в сетку — MONTH_SECTION_INVALID', () => {
    expect(codeOf(() => resolveSection(colA({ 769: 'Октябрь 2026' }, 790), meta(790), { year: 2026, month: 10 }))).toBe('MONTH_SECTION_INVALID');
  });
});

describe('предпроверка следующего месяца (только предупреждение)', () => {
  const colA = (entries: Record<number, string>): (string | null)[] => Array.from({ length: 803 }, (_, i) => entries[i + 1] ?? null);
  it('вне окна — NOT_IN_WINDOW', () => {
    expect(nextMonthPrecheck(colA({ 735: 'Сентябрь 2026' }), '2026-09-17', 5)).toMatchObject({ state: 'NOT_IN_WINDOW', code: null, daysLeft: 13, nextMonth: '2026-10' });
  });
  it('в окне и октября нет — NEXT_MONTH_SECTION_MISSING; 30.09 — daysLeft 0', () => {
    expect(nextMonthPrecheck(colA({ 735: 'Сентябрь 2026' }), '2026-09-25', 5)).toMatchObject({ state: 'MISSING', code: 'NEXT_MONTH_SECTION_MISSING', daysLeft: 5 });
    expect(nextMonthPrecheck(colA({ 735: 'Сентябрь 2026' }), '2026-09-30', 5)).toMatchObject({ state: 'MISSING', daysLeft: 0 });
  });
  it('октябрь подготовлен — PRESENT; декабрь → январь следующего года', () => {
    expect(nextMonthPrecheck(colA({ 735: 'Сентябрь 2026', 769: 'Октябрь 2026' }), '2026-09-29', 5)).toMatchObject({ state: 'PRESENT', code: null });
    expect(nextMonthPrecheck(colA({}), '2026-12-31', 5)).toMatchObject({ nextMonth: '2027-01', state: 'MISSING' });
  });
});

describe('терминальный блок: колонка дня недели есть только МЕЖДУ блоками (final polish F2)', () => {
  const G = geometryAt({ year: 2026, month: 10 }, 769);
  const lay = (n: number) => layoutOf(G, Array.from({ length: n }, (_, i) => ({ index: i, slot: i, start: slotStart(i), nmId: 100 + i, title: `${100 + i}` })));
  it.each([1, 24, 25, 26])('%i SKU: цепочка кончается последней метрикой последнего блока — без «стартера» несуществующего следующего', (n) => {
    const L = lay(n);
    const last = L.blocks[n - 1]!;
    expect(BLOCK_BODY_WIDTH).toBe(23);
    expect(L.lastBlockColumn).toBe(last.start + 22);
    expect(blockLastColumn(L, last)).toBe(last.start + 22);
    expect(blockOfColumn(L, last.start + 22)?.nmId).toBe(last.nmId);
    expect(blockOfColumn(L, last.start + 23)).toBeNull();                       // колонки-сироты нет в раскладке
    for (const b of L.blocks.slice(0, -1)) {
      expect(blockLastColumn(L, b)).toBe(slotEnd(b.slot));                      // между блоками разделитель есть
      expect(blockOfColumn(L, slotEnd(b.slot))?.nmId).toBe(b.nmId);
      expect(blockLastColumn(L, b) + 1).toBe(L.blocks[b.index + 1]!.start);      // и сразу следующий блок
    }
  });
  it('геометрия вставки: хвост книги — 12 колонок до якоря; вставка ровно до новой терминальной колонки', () => {
    expect(TAIL_WIDTH).toBe(12);
    expect(tailStartOf(600)).toBe(589);
    // (а) книга с физической колонкой дня недели у последнего блока (живой сентябрь: VP), +1 SKU → 23 колонки после VP.
    expect(insertGeometry(23, tailStartOf(600), 1)).toEqual({ at: 588, count: 23, tailStartAfter: 612 });
    expect(insertGeometry(23, tailStartOf(600), 2)).toEqual({ at: 588, count: 47, tailStartAfter: 636 });
    expect(insertGeometry(23, tailStartOf(600), 0)).toEqual({ at: 588, count: 0, tailStartAfter: 589 });
    // (б) книга, созданная Calendar V2: хвост сразу за последней метрикой (WM), +1 SKU → разделитель + тело = 24 колонки.
    expect(insertGeometry(24, 612, 1)).toEqual({ at: 611, count: 24, tailStartAfter: 636 });
    expect(insertGeometry(24, 612, 0)).toEqual({ at: 611, count: 0, tailStartAfter: 612 });
    expect(insertGeometry(0, 36, 1)).toEqual({ at: 35, count: 24, tailStartAfter: 60 });
    // хвост не там, где его ждёт цепочка, — отказ, без догадок.
    expect(insertGeometry(23, 600, 1)).toEqual({ error: 'TAIL_GEOMETRY_UNKNOWN' });
    expect(insertGeometry(23, 580, 1)).toEqual({ error: 'TAIL_GEOMETRY_UNKNOWN' });
  });
  it('после вставки хвост начинается сразу за истинной последней колонкой цепочки', () => {
    for (const [lastSlot, tail, n] of [[23, 589, 1], [23, 589, 2], [24, 612, 1], [24, 612, 3]] as const) {
      const r = insertGeometry(lastSlot, tail, n) as { tailStartAfter: number };
      expect(r.tailStartAfter).toBe(slotStart(lastSlot + n) + 22 + 1);
    }
  });
});
