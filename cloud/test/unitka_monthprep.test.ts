/**
 * UNITKA CALENDAR V2 (Phase 2B) — планировщик подготовки месяца и Engine на новых секциях.
 * Октябрь 2026 — золотой случай: 769/770/771..801/802/803, 25 блоков, 909951444 в слоте 25 (WO..XL).
 * Ни Sheets, ни BigQuery: книга — синтетические секции (unitka_calendar_fixture.ts).
 */
import { describe, it, expect } from 'vitest';
import {
  planMonthPrep, toStructureRequests, manualCellsWritten, resolveCanonicalCogs, type PrepInputs, type MonthPrepPlan,
} from '../src/loaders/unitka/monthprep.js';
import { geometryAt, nextMonth, slotStart, isReservedSlot, formatMonthKey, dayRowOf, type MonthKey } from '../src/loaders/unitka/calendar.js';
import { OFFSET, SUMMARY, colA1, isoToSerial, addDaysIso, type CellValue } from '../src/loaders/unitka/model.js';
import { buildPlan, currentValue, type Snapshot } from '../src/loaders/unitka/plan.js';
import type { FactRow } from '../src/loaders/unitka/bq.js';
import { LoaderError } from '../src/errors.js';
import {
  sectionFromSpec, septemberSpec, applyPlan, cogsSnapshot, SEPT_NMS, NEW_NM, WIDTH_SEPT,
} from './unitka_calendar_fixture.js';
import { logistics, commission } from './unitka_fixture.js';

const OCT: MonthKey = { year: 2026, month: 10 };
const population = (extra: number[] = [NEW_NM], drop: number[] = []) =>
  [...SEPT_NMS.filter((n) => !drop.includes(n)), ...extra].map((nmId) => ({ nmId, name: nmId === NEW_NM ? 'Набор анти-акне пудра+сыворотка+крем' : `Товар ${nmId}` }));

/** Книга: секции (по заголовкам), сетка листа. */
interface Book { sections: Snapshot[]; rowCount: number; columnCount: number }
function septemberBook(): Book {
  return { sections: [sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT)], rowCount: 768, columnCount: WIDTH_SEPT };
}
function columnA(b: Book): CellValue[] {
  const a: CellValue[] = Array(b.rowCount).fill(null);
  for (const s of b.sections) a[s.geometry.topRow - 1] = s.grid[0]![0]!;
  return a;
}
function inputs(b: Book, target: MonthKey, over: Partial<PrepInputs> = {}): PrepInputs {
  const find = (k: MonthKey) => b.sections.find((s) => s.geometry.monthKey === formatMonthKey(k)) ?? null;
  const prevKey = target.month === 1 ? { year: target.year - 1, month: 12 } : { year: target.year, month: target.month - 1 };
  return {
    target, meta: { sheetId: 739487431, rowCount: b.rowCount, columnCount: b.columnCount }, columnA: columnA(b),
    predecessor: find(prevKey), existing: find(target),
    population: population(), cogs: cogsSnapshot({ [NEW_NM]: 426.735, 252442517: 231.38 }), ...over,
  };
}
/** Применить план к книге (как batchUpdate) и вернуть новую книгу. */
function apply(b: Book, plan: MonthPrepPlan): Book {
  const width = Math.max(b.columnCount, plan.layout!.lastBlockColumn);
  return { sections: [...b.sections, applyPlan(plan, width)], rowCount: b.rowCount + plan.appendRows, columnCount: b.columnCount + plan.appendColumns };
}
const cellOf = (p: MonthPrepPlan, row: number, col: number) => p.cells.find((c) => c.row === row && c.col === col)?.value;

describe('октябрь 2026 — золотая раскладка', () => {
  const book = septemberBook();
  const plan = planMonthPrep(inputs(book, OCT));

  it('PLAN_CREATE: 769 / 770 / 771..801 / 802 / 803, 31 день', () => {
    expect(plan.status).toBe('PLAN_CREATE');
    expect(plan.geometry).toMatchObject({ topRow: 769, headerRow: 770, firstDailyRow: 771, lastDailyRow: 801, mtdRow: 802, spacerRow: 803, daysInMonth: 31, title: 'Октябрь 2026' });
    expect(plan.predecessor).toEqual({ monthKey: '2026-09', topRow: 735, blocks: 24 });
  });
  it('25 блоков: 24 сентябрьских в прежних слотах + 909951444 в слоте 25 = WO..XL (613..636); слот 24 пуст', () => {
    expect(plan.blocks).toHaveLength(25);
    expect(plan.blocks.slice(0, 24).map((b) => [b.slot, b.nmId, b.origin])).toEqual(SEPT_NMS.map((n, i) => [i, n, 'CARRIED']));
    const nb = plan.blocks[24]!;
    expect([nb.slot, nb.start, nb.start + 23, colA1(nb.start), colA1(nb.start + 23), nb.nmId, nb.origin]).toEqual([25, 613, 636, 'WO', 'XL', NEW_NM, 'NEW']);
    expect(plan.blocks.some((b) => isReservedSlot(b.slot))).toBe(false);
    expect(plan.cells.some((c) => c.col >= slotStart(24) && c.col < slotStart(25))).toBe(false);
    expect(plan.layout!.lastBlockColumn).toBe(636);
  });
  it('сетка: +35 строк (768 → 803), +36 колонок (600 → 636)', () => {
    expect(plan.appendRows).toBe(35);
    expect(plan.appendColumns).toBe(36);
    const req = toStructureRequests(plan, 739487431);
    expect(req[0]).toEqual({ appendDimension: { sheetId: 739487431, dimension: 'ROWS', length: 35 } });
    expect(req[1]).toEqual({ appendDimension: { sheetId: 739487431, dimension: 'COLUMNS', length: 36 } });
  });
  it('даты 01.10..31.10 ровно по разу: сводка B и дата каждого блока', () => {
    for (let i = 0; i < 31; i++) {
      const row = 771 + i, want = isoToSerial(addDaysIso('2026-10-01', i));
      expect(cellOf(plan, row, SUMMARY.date)).toEqual({ kind: 'number', value: want });
      for (const b of plan.blocks) expect(cellOf(plan, row, b.start + OFFSET.date)).toEqual({ kind: 'number', value: want });
    }
    expect(plan.cells.some((c) => c.row === 802 && c.value.kind === 'number')).toBe(false);
  });
  it('заголовки: A769 «Октябрь 2026», заголовок нового блока из REF_SKU_MASTER, «Заказы 25 SKU»', () => {
    expect(cellOf(plan, 769, 1)).toEqual({ kind: 'string', value: 'Октябрь 2026' });
    expect(cellOf(plan, 769, 613)).toEqual({ kind: 'string', value: '909951444 Набор анти-акне пудра+сыворотка+крем' });
    expect(cellOf(plan, 770, 6)).toEqual({ kind: 'string', value: 'Заказы 25 SKU' });
    expect(cellOf(plan, 770, 613)).toEqual({ kind: 'string', value: 'Дата' });
    expect(cellOf(plan, 802, 13)).toEqual({ kind: 'string', value: 'MTD ACTUAL' });
  });
  it('СПП и блогеры — пустые (ручной ввод): план их не пишет вовсе; SPP из сентября не копируется', () => {
    expect(manualCellsWritten(plan)).toBe(0);
    expect(plan.manualBlankCells).toBe(31 * 25 * 2);
    for (const b of plan.blocks) for (let r = 771; r <= 801; r++) {
      expect(cellOf(plan, r, b.start + OFFSET.spp)).toBeUndefined();
      expect(cellOf(plan, r, b.start + OFFSET.bloggers)).toBeUndefined();
    }
  });
  it('факты и ставки не пишутся (их пишет Engine после закрытия дней)', () => {
    for (const b of plan.blocks) for (const off of [OFFSET.views, OFFSET.orders, OFFSET.adsIn, OFFSET.price, OFFSET.commission, OFFSET.logistics]) {
      expect(cellOf(plan, 771, b.start + off)).toBeUndefined();
    }
  });
  it('COGS: сентябрьские слагаемые переносятся как есть (240 через $R$45 и нули НЕ исправляются)', () => {
    expect((cellOf(plan, 771, 13 + OFFSET.unitProfit) as { text: string }).text).toBe('=IF($M771>LAST_CLOSED_DATE,"",AE771-AF771-AH771-$R$45)');
    expect((cellOf(plan, 771, 37 + OFFSET.unitProfit) as { text: string }).text).toMatch(/-0\)$/);
    expect((cellOf(plan, 771, 61 + OFFSET.unitProfit) as { text: string }).text).toMatch(/-0\)$/);
  });
  it('COGS нового блока — из канона (426.735) с происхождением; заметка на AI771', () => {
    expect((cellOf(plan, 771, 613 + OFFSET.unitProfit) as { text: string }).text).toBe('=IF($WO771>LAST_CLOSED_DATE,"",XG771-XH771-XJ771-426.735)');
    expect((cellOf(plan, 801, 613 + OFFSET.adsOut) as { text: string }).text).toContain('-(426.735+');
    expect(plan.cogsProvenance).toEqual([{
      nmId: NEW_NM, cogs: 426.735, day: '2026-09-17', source: 'wb_mart.V_UNITKA_COGS_CANONICAL (копия wb_mart.UNITKA_COGS_EFFECTIVE)',
      publishedAt: '2026-09-18T16:50:03Z', runId: 'df35bcb7-48bb-49da-af16-7daed26f741d',
    }]);
    expect(plan.notes).toHaveLength(1);
    expect(plan.notes[0]).toMatchObject({ row: 771, col: 613 + OFFSET.unitProfit });
    expect(plan.notes[0]!.note).toMatch(/426\.735.*V_UNITKA_COGS_CANONICAL.*2026-09-17.*df35bcb7/);
  });
  it('все записи плана — только в новой секции (≥ 769); якоря WB736..WB739 и сентябрь не адресуются', () => {
    const req = toStructureRequests(plan, 739487431);
    const rowsTouched: number[] = [];
    for (const r of req) {
      const x = r as { copyPaste?: { destination: { startRowIndex: number } }; updateCells?: { start: { rowIndex: number } }; mergeCells?: { range: { startRowIndex: number } } };
      if (x.copyPaste) rowsTouched.push(x.copyPaste.destination.startRowIndex + 1);
      if (x.updateCells) rowsTouched.push(x.updateCells.start.rowIndex + 1);
      if (x.mergeCells) rowsTouched.push(x.mergeCells.range.startRowIndex + 1);
    }
    expect(Math.min(...rowsTouched)).toBe(769);
    expect(plan.cells.every((c) => c.row >= 769 && c.row <= 803)).toBe(true);
    expect(JSON.stringify(req)).not.toMatch(/PASTE_CONDITIONAL_FORMATTING|addConditionalFormatRule|deleteDimension|insertDimension/);
    expect(req.filter((r) => 'updateCells' in r && (r.updateCells as { fields: string }).fields === 'userEnteredValue')).toHaveLength(34);
    expect(req).toHaveLength(75); // 2 append + 12 копий формата + 34 строки + 1 заметка + 26 объединений
  });
  it('форматы: день 1 ← эталон закрытого дня 737, дни 2..31 ← 766, новый блок ← блок 24 (US..VP)', () => {
    const f = plan.formatCopies;
    expect(f).toContainEqual({ source: { r1: 737, r2: 737, c1: 1, c2: 588 }, dest: { r1: 771, r2: 771, c1: 1, c2: 588 } });
    expect(f).toContainEqual({ source: { r1: 766, r2: 766, c1: 1, c2: 588 }, dest: { r1: 772, r2: 801, c1: 1, c2: 588 } });
    expect(f).toContainEqual({ source: { r1: 766, r2: 766, c1: 565, c2: 588 }, dest: { r1: 772, r2: 801, c1: 613, c2: 636 } });
    expect(plan.conditionalFormatIntents).toHaveLength(1);
  });
  it('в формулах нет __xludf.DUMMYFUNCTION и ссылок на строки сентября', () => {
    for (const c of plan.cells) {
      if (c.value.kind !== 'formula') continue;
      expect(c.value.text).not.toMatch(/__xludf|DUMMYFUNCTION/);
      const rows = [...c.value.text.matchAll(/\$?[A-Z]{1,3}\$?(\d+)/g)].map((m) => Number(m[1])).filter((r) => r !== 45);
      expect(rows.every((r) => r >= 771 && r <= 802)).toBe(true);
    }
  });
});

describe('канонический COGS нового блока', () => {
  const book = septemberBook();
  it('значение берётся из канона, а не из кода: 777.7 → 777.7', () => {
    const p = planMonthPrep(inputs(book, OCT, { cogs: cogsSnapshot({ [NEW_NM]: 777.7 }) }));
    expect(p.blocks[24]!.params.cogsTerm).toBe('777.7');
    expect(p.cogsProvenance[0]!.cogs).toBe(777.7);
  });
  it('берётся последний день копии с положительным значением', () => {
    const c = cogsSnapshot({});
    c.rows = [
      { nmId: NEW_NM, internalSku: null, day: '2026-09-10', cogsIntervalCount: 1, canonicalCogs: 400 },
      { nmId: NEW_NM, internalSku: null, day: '2026-09-17', cogsIntervalCount: 1, canonicalCogs: 426.735 },
      { nmId: NEW_NM, internalSku: null, day: '2026-09-18', cogsIntervalCount: 2, canonicalCogs: null },
    ];
    expect(resolveCanonicalCogs(c, NEW_NM)).toMatchObject({ cogs: 426.735, day: '2026-09-17' });
  });
  it.each([
    ['канона нет (COGS_MISSING)', cogsSnapshot({ 252442517: 231.38 }), 'COGS_MISSING'],
    ['канон NULL (COGS_MISSING)', cogsSnapshot({ [NEW_NM]: null }), 'COGS_MISSING'],
    ['канон 0 (COGS_MISSING — 0 не подставляется)', cogsSnapshot({ [NEW_NM]: 0 }), 'COGS_MISSING'],
    ['копия устарела (COGS_STALE)', cogsSnapshot({ [NEW_NM]: 426.735 }, '2026-09-17', { state: 'STALE' }), 'COGS_STALE'],
    ['копия недоступна (COGS_UNAVAILABLE)', cogsSnapshot({}, '2026-09-17', { state: 'UNAVAILABLE', rows: [] }), 'COGS_UNAVAILABLE'],
  ])('%s → BLOCKED, 0 запросов', (_n, cogs, code) => {
    const p = planMonthPrep(inputs(book, OCT, { cogs }));
    expect([p.status, p.code]).toEqual(['BLOCKED', code]);
    expect(toStructureRequests(p, 1)).toEqual([]);
  });
  it('без новых SKU канон COGS не нужен: устаревшая копия не блокирует перенос', () => {
    const p = planMonthPrep(inputs(book, OCT, { population: population([]), cogs: cogsSnapshot({}, '2026-09-17', { state: 'STALE' }) }));
    expect(p.status).toBe('PLAN_CREATE');
    expect(p.blocks).toHaveLength(24);
    expect(p.appendColumns).toBe(0);
  });
});

describe('популяция: снимок на момент подготовки, слоты стабильны', () => {
  const book = septemberBook();
  it('1 SKU: выбыли 23 — их слоты пусты, новый блок не занимает освободившийся слот', () => {
    const p = planMonthPrep(inputs(book, OCT, { population: [{ nmId: 252442517, name: 'x' }] }));
    expect(p.blocks.map((b) => b.slot)).toEqual([0]);
    expect(p.retiredNmIds).toHaveLength(23);
  });
  it('выбывший SKU оставляет пустой слот; новый SKU всё равно в слоте 25', () => {
    const p = planMonthPrep(inputs(book, OCT, { population: population([NEW_NM], [252442341]) }));
    expect(p.retiredNmIds).toEqual([252442341]);
    expect(p.blocks.find((b) => b.slot === 1)).toBeUndefined();
    expect(p.blocks.find((b) => b.nmId === NEW_NM)!.slot).toBe(25);
    expect(p.cells.some((c) => c.col >= slotStart(1) && c.col < slotStart(2))).toBe(false);
  });
  it('26 SKU: два новых по возрастанию nm_id → слоты 25 и 26 (XM..YJ)', () => {
    const p = planMonthPrep(inputs(book, OCT, { population: population([NEW_NM, 900000001]), cogs: cogsSnapshot({ [NEW_NM]: 426.735, 900000001: 99.5 }) }));
    expect(p.blocks.slice(24).map((b) => [b.nmId, b.slot, colA1(b.start)])).toEqual([[900000001, 25, 'WO'], [NEW_NM, 26, 'XM']]);
    expect(p.appendColumns).toBe(660 - 600);
  });
  it('дубль nm_id в популяции — BLOCKED DUPLICATE_SKU; пустая популяция — EMPTY_POPULATION', () => {
    expect(planMonthPrep(inputs(book, OCT, { population: [...population(), { nmId: NEW_NM, name: 'x' }] })).code).toBe('DUPLICATE_SKU');
    expect(planMonthPrep(inputs(book, OCT, { population: [] })).code).toBe('EMPTY_POPULATION');
  });
});

describe('идемпотентность и частичные секции', () => {
  const book = septemberBook();
  const plan = planMonthPrep(inputs(book, OCT));
  const withOct = apply(book, plan);

  it('повторная подготовка того же месяца → NO_CHANGE, 0 запросов, 0 изменений', () => {
    const again = planMonthPrep(inputs(withOct, OCT));
    expect(again.status).toBe('NO_CHANGE');
    expect(toStructureRequests(again, 1)).toEqual([]);
    expect(again.cells).toEqual([]);
    expect(again.layout!.blocks).toHaveLength(25);
  });
  it('ручные СПП (в т.ч. 0) и блогеры уже введены → NO_CHANGE, ничего не перезаписывается', () => {
    const oct = withOct.sections[1]!;
    const g = oct.geometry;
    oct.grid[771 - g.topRow]![13 + OFFSET.spp - 1] = 0;
    oct.grid[772 - g.topRow]![613 + OFFSET.spp - 1] = 17;
    oct.grid[771 - g.topRow]![37 + OFFSET.bloggers - 1] = 2;
    const again = planMonthPrep(inputs(withOct, OCT));
    expect(again.status).toBe('NO_CHANGE');
    expect(toStructureRequests(again, 1)).toHaveLength(0);
    expect(oct.grid[771 - g.topRow]![13 + OFFSET.spp - 1]).toBe(0);
  });
  it('новый активный SKU после принятия месяца — раскладка заморожена, только отчёт unmappedActive', () => {
    const again = planMonthPrep(inputs(withOct, OCT, { population: population([NEW_NM, 900000001]) }));
    expect(again.status).toBe('NO_CHANGE');
    expect(again.unmappedActive).toEqual([900000001]);
  });
  it('заголовок есть, строк в сетке не хватает → MONTH_SECTION_PARTIAL, 0 запросов', () => {
    const partial: Book = { ...withOct, rowCount: 790, sections: [withOct.sections[0]!, { ...withOct.sections[1]! }] };
    const p = planMonthPrep({ ...inputs(partial, OCT), existing: null });
    expect([p.status, p.code]).toEqual(['MONTH_SECTION_PARTIAL', 'MONTH_SECTION_PARTIAL']);
    expect(toStructureRequests(p, 1)).toEqual([]);
  });
  it('заголовок есть, шапки нет → MONTH_SECTION_PARTIAL (авто-починки нет)', () => {
    const oct = structuredClone(withOct.sections[1]!);
    oct.grid[1] = Array(636).fill('');
    const p = planMonthPrep(inputs({ ...withOct, sections: [withOct.sections[0]!, oct] }, OCT));
    expect(p.status).toBe('MONTH_SECTION_PARTIAL');
  });
  it('секция есть, но формула пропала → MONTH_SECTION_INVALID, 0 запросов', () => {
    const oct = structuredClone(withOct.sections[1]!);
    oct.formulas[5]![613 + OFFSET.unitProfit - 1] = 12;
    const p = planMonthPrep(inputs({ ...withOct, sections: [withOct.sections[0]!, oct] }, OCT));
    expect([p.status, p.code]).toEqual(['MONTH_SECTION_INVALID', 'MONTH_SECTION_INVALID']);
    expect(toStructureRequests(p, 1)).toEqual([]);
  });
  it('заголовок месяца дважды → MONTH_SECTION_AMBIGUOUS', () => {
    const inp = inputs(withOct, OCT);
    const a = [...inp.columnA]; a[790] = 'Октябрь 2026';
    expect(planMonthPrep({ ...inp, columnA: a }).code).toBe('MONTH_SECTION_AMBIGUOUS');
  });
  it('предыдущего месяца нет → PREDECESSOR_SECTION_MISSING; хвост строк после него → SHEET_TAIL_NOT_EMPTY', () => {
    expect(planMonthPrep(inputs(book, { year: 2026, month: 11 })).code).toBe('PREDECESSOR_SECTION_MISSING');
    expect(planMonthPrep(inputs({ ...book, rowCount: 800 }, OCT)).code).toBe('SHEET_TAIL_NOT_EMPTY');
  });
  it('построители не воспроизводят прошлый месяц → TEMPLATE_MISMATCH (формулу не «угадываем»)', () => {
    const sept = structuredClone(book.sections[0]!);
    sept.formulas[29]![37 + OFFSET.profitAll - 1] = '=IF($AK766>LAST_CLOSED_DATE,"",AO766*BG766)';
    const p = planMonthPrep(inputs({ ...book, sections: [sept] }, OCT));
    expect([p.status, p.code]).toEqual(['BLOCKED', 'TEMPLATE_MISMATCH']);
    expect(p.reasons[0]).toMatch(/AU766/);
  });
});

/* ───────────── Engine на подготовленных секциях: дни, переходы, колонка 600 ───────────── */

function factsFor(nms: readonly number[], monthStart: string, lcd: string): FactRow[] {
  const out: FactRow[] = [];
  const days = Math.round((Date.parse(lcd) - Date.parse(monthStart)) / 86_400_000) + 1;
  nms.forEach((nm, b) => {
    for (let i = 0; i < days; i++) {
      out.push({ nmId: nm, date: addDaysIso(monthStart, i), views: 100 + b, opens: 10, carts: 3, orders: 1 + (i % 2), cancels: 0, stock: 50, adsIn: 5.5, price: 990.5, storage: 1.25, ordersSource: 'FUNNEL_API', cancelsSource: 'PROXY_FACT_ORDERS' });
    }
  });
  return out;
}
function engineOn(section: Snapshot, lcd: string, bookLcd: string) {
  const snap: Snapshot = { ...section, namedLcd: isoToSerial(bookLcd), mirrorLcd: isoToSerial(bookLcd), mirrorRev: 32.136 };
  const nms = snap.grid[0]!.map((v) => /\d{6,12}/.exec(String(v ?? ''))?.[0]).filter((x): x is string => !!x).map(Number);
  return buildPlan({
    snapshot: snap, lcd: { lastClosedDate: lcd, d1Msk: addDaysIso(lcd, 1) }, facts: factsFor(nms, `${lcd.slice(0, 7)}-01`, lcd),
    logistics: logistics(), commission: commission(), minN: 10, maxLagDays: 2,
  });
}

describe('Engine на октябре: 01.10, 02.10 (первая запись), 31.10', () => {
  const oct = apply(septemberBook(), planMonthPrep(inputs(septemberBook(), OCT))).sections[1]!;
  it('LCD 01.10: факт в строке 771, 25 блоков, блок 25 пишется в WO..XL', () => {
    const p = engineOn(oct, '2026-10-01', '2026-09-30');
    expect(p.layout).toMatchObject({ monthKey: '2026-10', firstDailyRow: 771, lastDailyRow: 801, mtdRow: 802 });
    expect(p.blocks).toHaveLength(25);
    const facts = p.cells.filter((c) => c.kind === 'fact');
    expect(new Set(facts.map((c) => c.row))).toEqual(new Set([771]));
    expect(facts.some((c) => c.nmId === NEW_NM && c.col === 613 + OFFSET.orders)).toBe(true);
    // Ставки — на все 31 строку каждого блока.
    expect(p.expected.filter((e) => e.kind === 'logistics' && e.nmId === NEW_NM).map((e) => e.row)).toEqual(Array.from({ length: 31 }, (_, i) => 771 + i));
  });
  it('LCD 31.10: последняя строка 801, закрыт весь месяц', () => {
    const p = engineOn(oct, '2026-10-31', '2026-10-30');
    expect(p.closedDays).toBe(31);
    expect(Math.max(...p.cells.filter((c) => c.kind === 'fact').map((c) => c.row))).toBe(801);
  });
  it('колонка 600 в строках октября — НЕ якорь: ячейка сетки читается из сетки, якорь — по виду', () => {
    const snap: Snapshot = { ...oct, mirrorLcd: 11111, mirrorRev: 22222, namedLcd: 33333 };
    snap.grid[771 - oct.geometry.topRow]![600 - 1] = 7.77;
    expect(currentValue(snap, { row: 771, col: 600, kind: 'fact' })).toBe(7.77);
    expect(currentValue(snap, { row: 736, col: 600, kind: 'lcd' })).toBe(11111);
    expect(currentValue(snap, { row: 737, col: 600, kind: 'reverse' })).toBe(22222);
    const p = engineOn(oct, '2026-10-01', '2026-09-30');
    // Якоря плана — ровно WB736/WB737 по виду; ни одной ячейки блока в колонке 600 (там резерв).
    expect(p.expected.filter((e) => e.col === 600 && !e.namedRange).map((e) => [e.row, e.kind])).toEqual([[737, 'reverse'], [736, 'lcd']]);
  });
  it('LCD 01.11 на октябрьской секции — MONTH_SECTION_INVALID до любой записи', () => {
    expect(() => engineOn(oct, '2026-11-01', '2026-10-31')).toThrow(LoaderError);
    try { engineOn(oct, '2026-11-01', '2026-10-31'); } catch (e) { expect((e as LoaderError).code).toBe('MONTH_SECTION_INVALID'); }
  });
});

describe('цепочка месяцев: октябрь 2026 → март 2028 (год, 28 и 29 февраля)', () => {
  it('каждый месяц создаётся из предыдущего, повтор — NO_CHANGE; Engine пишет последний день', () => {
    let book = septemberBook();
    let key: MonthKey = { year: 2026, month: 9 };
    const seen: Record<string, number[]> = {};
    for (let i = 0; i < 18; i++) {
      key = nextMonth(key);
      const p = planMonthPrep(inputs(book, key));
      expect(p.status).toBe('PLAN_CREATE');
      book = apply(book, p);
      expect(planMonthPrep(inputs(book, key)).status).toBe('NO_CHANGE');
      const g = p.geometry!;
      seen[g.monthKey] = [g.topRow, g.firstDailyRow, g.lastDailyRow, g.mtdRow, g.daysInMonth, p.blocks.length];
      const sec = book.sections[book.sections.length - 1]!;
      const last = addDaysIso(g.monthStart, g.daysInMonth - 1);
      const plan = engineOn(sec, last, addDaysIso(last, -1));
      expect(Math.max(...plan.cells.filter((c) => c.kind === 'fact').map((c) => c.row))).toBe(dayRowOf(g, g.daysInMonth - 1));
    }
    expect(seen['2026-12']).toEqual([838, 840, 870, 871, 31, 25]);
    expect(seen['2027-01']).toEqual([873, 875, 905, 906, 31, 25]);
    expect(seen['2027-02']).toEqual([908, 910, 937, 938, 28, 25]);
    expect(seen['2028-02']).toEqual([1321, 1323, 1351, 1352, 29, 25]);
    expect(seen['2028-03']).toEqual([1354, 1356, 1386, 1387, 31, 25]);
    expect(book.rowCount).toBe(geometryAt({ year: 2028, month: 3 }, 1354).spacerRow);
    expect(book.columnCount).toBe(636);
  });
});
