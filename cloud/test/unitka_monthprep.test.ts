/**
 * UNITKA CALENDAR V2 (Phase 2B) — планировщик подготовки месяца и Engine на новых секциях.
 * Октябрь 2026 — золотой случай: 769/770/771..801/802/803, 25 блоков, 909951444 в слоте 25 (WO..XL).
 * Ни Sheets, ни BigQuery: книга — синтетические секции (unitka_calendar_fixture.ts).
 */
import { describe, it, expect } from 'vitest';
import {
  planMonthPrep, toStructureRequests, manualCellsWritten, resolveCanonicalCogs, type PrepInputs, type MonthPrepPlan,
} from '../src/loaders/unitka/monthprep.js';
import { borderSpec, bordersJson, rowKindOf } from '../src/loaders/unitka/visual.js';
import { geometryAt, nextMonth, slotStart, formatMonthKey, dayRowOf, chainGaps, type MonthKey } from '../src/loaders/unitka/calendar.js';
import { OFFSET, SUMMARY, colA1, isoToSerial, addDaysIso, type CellValue } from '../src/loaders/unitka/model.js';
import { buildPlan, currentValue, type Snapshot } from '../src/loaders/unitka/plan.js';
import type { FactRow } from '../src/loaders/unitka/bq.js';
import { LoaderError } from '../src/errors.js';
import {
  sectionFromSpec, septemberSpec, applyPlan, cogsSnapshot, septemberStructure, septemberRowFormats, SEPT_NMS, NEW_NM, WIDTH_SEPT,
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
    target, meta: { sheetId: 739487431, rowCount: b.rowCount, columnCount: b.columnCount, anchorCol: b.columnCount }, columnA: columnA(b),
    predecessor: find(prevKey), existing: find(target),
    population: population(), cogs: cogsSnapshot({ [NEW_NM]: 426.735, 252442517: 231.38 }),
    structure: septemberStructure(b.rowCount, b.columnCount),
    rowFormats: (() => { const p = find(prevKey); return p ? septemberRowFormats(b.columnCount, [p.geometry.topRow, p.geometry.headerRow, p.geometry.firstDailyRow, p.geometry.lastDailyRow, p.geometry.mtdRow, p.geometry.spacerRow]) : null; })(), ...over,
  };
}
/** Применить план к книге (как batchUpdate) и вернуть новую книгу. */
function apply(b: Book, plan: MonthPrepPlan): Book {
  const width = b.columnCount + (plan.insertColumns?.count ?? 0);
  return { sections: [...b.sections, applyPlan(plan, width)], rowCount: b.rowCount + plan.appendRows, columnCount: width };
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
  it('25 блоков сплошной цепочкой: 24 сентябрьских в прежних слотах + 909951444 в слоте 24 = VQ..WM (589..611) сразу за VP; колонки дня недели за последним SKU нет', () => {
    expect(plan.blocks).toHaveLength(25);
    expect(plan.blocks.slice(0, 24).map((b) => [b.slot, b.nmId, b.origin])).toEqual(SEPT_NMS.map((n, i) => [i, n, 'CARRIED']));
    const nb = plan.blocks[24]!;
    const prev = plan.blocks[23]!;
    expect([nb.slot, nb.start, nb.start + 22, colA1(nb.start), colA1(nb.start + 22), nb.nmId, nb.origin]).toEqual([24, 589, 611, 'VQ', 'WM', NEW_NM, 'NEW']);
    expect(nb.start).toBe(prev.start + 24); // сразу за концом предыдущего блока, без разделителя
    expect(chainGaps(plan.blocks)).toEqual([]);
    expect(plan.chainGaps).toEqual([]);
    expect(plan.layout!.lastBlockColumn).toBe(611);
    // F2: блок 24 перестал быть последним — его колонка-разделитель VP заполнена; у блока 25 разделителя нет вовсе.
    expect(cellOf(plan, 771, prev.start + OFFSET.weekday)?.kind).toBe('formula');
    expect(plan.cells.some((c) => c.col > 611)).toBe(false);
    expect(plan.formatCopies.some((f) => f.dest.c2 > 611)).toBe(false);
  });
  it('сетка: вставка 23 колонок сразу за VP — ровно до последней метрики блока 25 (хвост книги и якоря сдвигаются: WB → WY), +35 строк', () => {
    expect(plan.appendRows).toBe(35);
    expect(plan.insertColumns).toEqual({ at: 588, count: 23 });
    expect(plan.anchorCol).toBe(623);
    const req = toStructureRequests(plan, 739487431);
    expect(req[0]).toEqual({ insertDimension: { range: { sheetId: 739487431, dimension: 'COLUMNS', startIndex: 588, endIndex: 611 }, inheritFromBefore: true } });
    expect(req[3]).toEqual({ appendDimension: { sheetId: 739487431, dimension: 'ROWS', length: 35 } });
    expect(JSON.stringify(req)).toContain('$WY$736');
    expect(JSON.stringify(req)).not.toContain('$WB$736');
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
    expect(cellOf(plan, 769, 589)).toEqual({ kind: 'string', value: '909951444 Набор анти-акне пудра+сыворотка+крем' });
    expect(cellOf(plan, 770, 6)).toEqual({ kind: 'string', value: 'Заказы 25 SKU' });
    expect(cellOf(plan, 770, 589)).toEqual({ kind: 'string', value: 'Дата' });
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
    expect((cellOf(plan, 771, 589 + OFFSET.unitProfit) as { text: string }).text).toBe('=IF($VQ771>LAST_CLOSED_DATE,"",WI771-WJ771-WL771-426.735)');
    expect((cellOf(plan, 801, 589 + OFFSET.adsOut) as { text: string }).text).toContain('-(426.735+');
    expect(plan.cogsProvenance).toEqual([{
      nmId: NEW_NM, cogs: 426.735, day: '2026-09-17', source: 'wb_mart.V_UNITKA_COGS_CANONICAL (копия wb_mart.UNITKA_COGS_EFFECTIVE)',
      publishedAt: '2026-09-18T16:50:03Z', runId: 'df35bcb7-48bb-49da-af16-7daed26f741d',
    }]);
    expect(plan.notes).toHaveLength(1);
    expect(plan.notes[0]).toMatchObject({ row: 771, col: 589 + OFFSET.unitProfit });
    expect(plan.notes[0]!.note).toMatch(/426\.735.*V_UNITKA_COGS_CANONICAL.*2026-09-17.*df35bcb7/);
  });
  it('все записи плана — только в новой секции (≥ 769); старые строки — лишь сброс формата во ВСТАВЛЕННЫХ колонках 589..611', () => {
    const req = toStructureRequests(plan, 739487431);
    const rowsTouched: number[] = [];
    for (const r of req) {
      const x = r as { updateCells?: { start: { rowIndex: number } }; mergeCells?: { range: { startRowIndex: number } } };
      if (x.updateCells) rowsTouched.push(x.updateCells.start.rowIndex + 1);
      if (x.mergeCells) rowsTouched.push(x.mergeCells.range.startRowIndex + 1);
    }
    expect(Math.min(...rowsTouched)).toBe(769);
    const reset = req.filter((r) => 'repeatCell' in r).map((r) => (r.repeatCell as { range: Record<string, number>; fields: string })).filter((x) => x.range.startRowIndex! < 768);
    expect(reset).toEqual([{ range: { sheetId: 739487431, startRowIndex: 0, endRowIndex: 768, startColumnIndex: 588, endColumnIndex: 611 }, cell: {}, fields: 'userEnteredFormat' }]);
    expect(plan.cells.every((c) => c.row >= 769 && c.row <= 803)).toBe(true);
    expect(JSON.stringify(req)).not.toMatch(/copyPaste|PASTE_|"deleteDimension"|deleteConditionalFormatRule/);   // ни строк, ни колонок план не удаляет
    expect(req.filter((r) => 'deleteDimensionGroup' in r)).toEqual([{ deleteDimensionGroup: { range: { sheetId: 739487431, dimension: 'COLUMNS', startIndex: 611, endIndex: 612 } } }]);
    const rowsUpd = req.filter((r) => 'updateCells' in r && (r.updateCells as { fields: string }).fields === 'userEnteredValue');
    expect(rowsUpd).toHaveLength(34); // 769..802: значения и формулы (разделитель 803 пуст)
    const fmtRuns = req.filter((r) => 'repeatCell' in r).length - 1; // минус сброс унаследованного
    expect(fmtRuns).toBeGreaterThan(0);
    // вставка + append + 1 сброс + серии форматов + 34 строки + 1 заметка + 26 объединений + УФ + размеры + 1 группа.
    // Размеры: 23 вставленные колонки блока 25 + 6 полос строк + расширение существующих колонок до контракта.
    expect(plan.dimensionRequests).toHaveLength(23 + 6 + plan.widthUpgrades.length);
    expect(plan.rowHeights).toMatchObject({ title: 40, mtd: 33, day: 18 });
    // группа блока 25 (смещения 16..22) + отделение первой колонки хвоста от группы хвоста (иначе Sheets слил бы группы).
    expect(plan.groupRequests.map((r) => Object.keys(r)[0])).toEqual(['deleteDimensionGroup', 'addDimensionGroup']);
    expect(plan.tailGroupDetachedColumn).toBe(612);
    expect(req).toHaveLength(2 + plan.cfTrims.length + 1 + fmtRuns + 34 + 1 + 26 + plan.conditionalFormats!.rules.length + plan.dimensionRequests.length + 2);
    expect(plan.conditionalFormats!.rules).toHaveLength(108);
    expect(plan.cfStartIndex).toBe(septemberStructure().conditionalFormats.length);
    // Правила сентября, кончающиеся на VP, переиздаются сразу за вставкой колонок — иначе Sheets растянул бы их на блок 25.
    expect(plan.cfTrims.map((t) => t.rule.booleanRule!.condition.values![0]!.userEnteredValue)).toEqual(['=$US737>$WY$736', '=WEEKDAY(US737;2)>5']);
    const kinds = toStructureRequests(plan, 739487431).map((r) => Object.keys(r)[0]);
    expect(kinds.slice(0, 3)).toEqual(['insertDimension', 'updateConditionalFormatRule', 'updateConditionalFormatRule']);
    expect(kinds.lastIndexOf('updateConditionalFormatRule')).toBeLessThan(kinds.indexOf('addConditionalFormatRule'));
  });
  it('форматы пишутся явно (repeatCell): все дни ← 737 (эталон E3), блок 25 ← блок 24; резервный слот — сброс', () => {
    const req = toStructureRequests(plan, 739487431);
    type RC = { range: { startRowIndex: number; endRowIndex: number; startColumnIndex: number; endColumnIndex: number }; cell: { userEnteredFormat?: { numberFormat: { pattern: string } } }; fields: string };
    const runs = req.filter((r) => 'repeatCell' in r).map((r) => r.repeatCell as RC);
    const at = (r: number, c: number) => runs.filter((x) => x.range.startRowIndex < r && x.range.endRowIndex >= r && x.range.startColumnIndex < c && x.range.endColumnIndex >= c);
    const pat = (r: number, c: number) => { const m = at(r, c).filter((x) => x.range.startRowIndex >= 768); expect(m).toHaveLength(1); return m[0]!.cell.userEnteredFormat?.numberFormat.pattern; };
    expect(pat(771, 13)).toBe('737:13');
    expect(pat(790, 13)).toBe('737:13');
    expect(pat(769, 1)).toBe('735:1');
    expect(pat(802, 11)).toBe('767:11');
    expect(pat(772, 589)).toBe('737:565');
    expect(pat(772, 611)).toBe('737:587');
    expect(pat(772, 588)).toBe('737:588');                       // разделитель блока 24 — свой шаблон (VP сентября)
    expect(at(772, 612).filter((x) => x.range.startRowIndex >= 768)).toEqual([]);   // за последней метрикой — ничего
    // Дни — две полосы одного шаблона: первый день (над ним линия шапки) и дни 2..31 (волосяная линия сверху).
    expect(at(771, 13).find((x) => x.range.startRowIndex >= 768)!.range).toMatchObject({ startRowIndex: 770, endRowIndex: 771 });
    expect(at(801, 13).find((x) => x.range.startRowIndex >= 768)!.range).toMatchObject({ startRowIndex: 771, endRowIndex: 801 });
    expect(JSON.stringify(req).length).toBeLessThan(5_000_000);
    const cols = plan.dimensionRequests.map((r) => r.updateDimensionProperties as { range: { startIndex: number; dimension: string }; properties: { pixelSize: number; hiddenByUser: boolean } }).filter((d) => d.range.dimension === 'COLUMNS');
    expect(cols.filter((d) => d.range.startIndex >= 588).map((d) => d.range.startIndex + 1)).toEqual(Array.from({ length: 23 }, (_, o) => 589 + o));
  });
  it('строки дней — нейтральный будущий вид: факт/ставка/расчёт без заливки, ручной ввод жёлтый; шапка и MTD — как шаблон', () => {
    const bgTpl = (r: number) => Array.from({ length: 600 }, (_, i) => ({ backgroundColor: { red: 0.5, green: 0.5, blue: 0.5 }, numberFormat: { type: 'TEXT', pattern: `${r}:${i + 1}` }, textFormat: { foregroundColor: { red: 0.7, green: 0.7, blue: 0.7 } } }));
    const rowFormats = new Map([735, 736, 737, 766, 767, 768].map((r) => [r, bgTpl(r)] as const));
    const p = planMonthPrep({ ...inputs(book, OCT), rowFormats });
    const req = toStructureRequests(p, 739487431);
    type RC = { range: { startRowIndex: number; endRowIndex: number; startColumnIndex: number; endColumnIndex: number }; cell: { userEnteredFormat?: Record<string, unknown> } };
    const runs = req.filter((r) => 'repeatCell' in r).map((r) => r.repeatCell as RC).filter((x) => x.range.startRowIndex >= 768);
    const fmt = (row: number, col: number) => runs.find((x) => x.range.startRowIndex < row && x.range.endRowIndex >= row && x.range.startColumnIndex < col && x.range.endColumnIndex >= col)!.cell.userEnteredFormat ?? {};
    for (const row of [771, 772, 801]) {
      expect(fmt(row, 13 + OFFSET.views).backgroundColor).toBeUndefined();     // факт
      expect(fmt(row, 13 + OFFSET.commission).backgroundColor).toBeUndefined(); // ставка
      expect((fmt(row, 13 + OFFSET.commission).textFormat as Record<string, unknown>).foregroundColor).toBeUndefined();
      expect(fmt(row, 13 + OFFSET.unitProfit).backgroundColor).toBeUndefined(); // расчёт
      expect(fmt(row, 13 + OFFSET.spp).backgroundColor).toBeDefined();          // ручной ввод — зона ввода
      expect(fmt(row, 13 + OFFSET.bloggers).backgroundColor).toBeDefined();
      expect(fmt(row, 3).backgroundColor).toBeDefined();                        // сводка C — как ручной
      expect(fmt(row, 4).backgroundColor).toBeUndefined();                      // сводка D — факт
      expect(fmt(row, 589 + OFFSET.orders).backgroundColor).toBeUndefined();    // блок 25
      expect(fmt(row, 589 + OFFSET.spp).backgroundColor).toBeDefined();
    }
    for (const row of [769, 770, 802, 803]) expect(fmt(row, 13 + OFFSET.views).backgroundColor).toBeDefined(); // заголовок, шапка, MTD, план — шаблон
    expect(fmt(771, 13 + OFFSET.views).numberFormat).toEqual({ type: 'TEXT', pattern: `737:${13 + OFFSET.views}` }); // прочие свойства сохранены
  });
  it('без форматов строк-шаблонов — BLOCKED TEMPLATE_FORMATS_UNAVAILABLE', () => {
    expect(planMonthPrep({ ...inputs(book, OCT), rowFormats: null }).code).toBe('TEMPLATE_FORMATS_UNAVAILABLE');
  });
  it('форматы: все дни ← эталон закрытого дня 737, новый блок ← блок 24 (US..VP)', () => {
    const f = plan.formatCopies;
    for (const [d1, d2] of [[771, 771], [772, 801]]) {
      expect(f).toContainEqual({ source: { r1: 737, r2: 737, c1: 1, c2: 12 }, dest: { r1: d1, r2: d2, c1: 1, c2: 12 } });
      expect(f).toContainEqual({ source: { r1: 737, r2: 737, c1: 13, c2: 35 }, dest: { r1: d1, r2: d2, c1: 13, c2: 35 } });      // тело блока 1
      expect(f).toContainEqual({ source: { r1: 737, r2: 737, c1: 36, c2: 36 }, dest: { r1: d1, r2: d2, c1: 36, c2: 36 } });      // его разделитель
      expect(f).toContainEqual({ source: { r1: 737, r2: 737, c1: 588, c2: 588 }, dest: { r1: d1, r2: d2, c1: 588, c2: 588 } });  // разделитель блока 24 (VP)
      expect(f).toContainEqual({ source: { r1: 737, r2: 737, c1: 565, c2: 587 }, dest: { r1: d1, r2: d2, c1: 589, c2: 611 } });  // блок 25 ← тело блока 24
    }
    expect(f.filter((x) => x.dest.c1 === 12 && x.dest.r1 >= 771 && x.dest.r2 <= 801)).toEqual([]);   // L в днях — из своего шаблона, не из A
    expect(f.some((x) => x.source.r1 === 766)).toBe(false);
    expect(plan.conditionalFormats!.families.weekend).toBe(1);
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
    expect(p.insertColumns).toBeNull();
    expect(p.anchorCol).toBe(600);
  });
});

describe('популяция: снимок на момент подготовки, слоты стабильны', () => {
  const book = septemberBook();
  it('1 SKU: выбыли 23 — их слоты пусты, новый блок не занимает освободившийся слот', () => {
    const p = planMonthPrep(inputs(book, OCT, { population: [{ nmId: 252442517, name: 'x' }] }));
    expect(p.blocks.map((b) => b.slot)).toEqual([0]);
    expect(p.retiredNmIds).toHaveLength(23);
  });
  it('выбывший SKU посреди цепочки → BLOCKED CHAIN_GAP (дыра запрещена, решение владельца); выбывший последний — допустим', () => {
    const p = planMonthPrep(inputs(book, OCT, { population: population([NEW_NM], [252442341]) }));
    expect([p.status, p.code]).toEqual(['BLOCKED', 'CHAIN_GAP']);
    expect(p.reasons[0]).toMatch(/252442341.*слоты 1/);
    expect(toStructureRequests(p, 1)).toEqual([]);
    const last = planMonthPrep(inputs(book, OCT, { population: population([], [910330849]) })); // блок 24 выбыл, нового нет
    expect(last.status).toBe('PLAN_CREATE');
    expect(last.blocks).toHaveLength(23);
    expect(last.chainGaps).toEqual([]);
    expect(last.insertColumns).toBeNull();
  });
  it('26 SKU: два новых по возрастанию nm_id → слоты 24 и 25 (VQ.., WO..); хвост сдвигается на 47 (у последнего блока нет разделителя)', () => {
    const p = planMonthPrep(inputs(book, OCT, { population: population([NEW_NM, 900000001]), cogs: cogsSnapshot({ [NEW_NM]: 426.735, 900000001: 99.5 }) }));
    expect(p.blocks.slice(24).map((b) => [b.nmId, b.slot, colA1(b.start)])).toEqual([[900000001, 24, 'VQ'], [NEW_NM, 25, 'WO']]);
    expect(p.insertColumns).toEqual({ at: 588, count: 47 });
    expect(p.anchorCol).toBe(647);
  });
  it.each([[24, [] as number[]], [25, [NEW_NM]], [26, [NEW_NM, 900000001]], [30, [NEW_NM, 900000001, 900000002, 900000003, 900000004, 900000005]]])('%i SKU: новые блоки сплошь за блоком 24, без дыр; вставка ровно до последней метрики; якоря сдвигаются; УФ/размеры/группы на каждый новый блок', (n, extra) => {
    const p = planMonthPrep(inputs(book, OCT, { population: population(extra), cogs: cogsSnapshot(Object.fromEntries(extra.map((x) => [x, 100 + (x % 7)]))) }));
    expect(p.status).toBe('PLAN_CREATE');
    expect(p.blocks).toHaveLength(n);
    const news = p.blocks.filter((b) => b.origin === 'NEW');
    expect(news.map((b) => b.slot)).toEqual(Array.from({ length: n - 24 }, (_, i) => 24 + i));
    for (let i = 1; i < p.blocks.length; i++) expect(p.blocks[i]!.start).toBe(p.blocks[i - 1]!.start + 24); // сплошная цепочка
    expect(p.chainGaps).toEqual([]);
    const end = slotStart(n - 1) + 22;                                    // последняя метрика последнего SKU
    expect(p.layout!.lastBlockColumn).toBe(end);
    expect(p.insertColumns).toEqual(n > 24 ? { at: 588, count: 24 * (n - 24) - 1 } : null);
    expect(p.anchorCol).toBe(600 + (n > 24 ? 24 * (n - 24) - 1 : 0));
    // F2: за последней метрикой последнего SKU план не пишет ничего — ни значений, ни форматов, ни УФ, ни размеров, ни границ.
    expect(p.cells.some((c) => c.col > end)).toBe(false);
    expect(p.formatCopies.some((f) => f.dest.c2 > end)).toBe(false);
    expect(p.conditionalFormats!.rules.flatMap((r) => r.ranges).every((x) => (x.endColumnIndex ?? 0) <= end)).toBe(true);
    expect(p.dimensionRequests.map((r) => r.updateDimensionProperties as { range: { dimension: string; endIndex: number } }).filter((d) => d.range.dimension === 'COLUMNS').every((d) => d.range.endIndex <= end)).toBe(true);
    // у каждого НЕ последнего блока разделитель (день недели) есть; у последнего — нет; хвост встаёт сразу за цепочкой.
    for (const b of p.blocks) expect(p.cells.some((c) => c.row === 771 && c.col === b.start + 23)).toBe(b !== p.blocks[n - 1]);
    expect(p.anchorCol - 11).toBe(n > 24 ? end + 1 : end + 2);             // 24 SKU: физическая VP сентября остаётся пустой в октябре
    expect(p.conditionalFormats!.rules).toHaveLength(105 + 3 * (n - 24));
    const adds = p.groupRequests.filter((r) => 'addDimensionGroup' in r);
    expect(adds).toHaveLength(n - 24);
    expect(p.groupRequests.filter((r) => 'deleteDimensionGroup' in r)).toHaveLength(n > 24 ? 1 : 0);
    expect(p.tailGroupDetachedColumn).toBe(n > 24 ? end + 1 : null);
    for (const [i, b] of news.entries()) expect((adds[i]!.addDimensionGroup as { range: { startIndex: number; endIndex: number } }).range).toEqual({ sheetId: 739487431, dimension: 'COLUMNS', startIndex: b.start + 16 - 1, endIndex: b.start + 22 });
    const req = toStructureRequests(p, 739487431);
    expect(JSON.stringify(req)).toContain(`$A771:$${colA1(end)}771`);
    expect(p.dimensionRequests.filter((r) => (r.updateDimensionProperties as { range: { dimension: string; startIndex: number } }).range.dimension === 'COLUMNS' && (r.updateDimensionProperties as { range: { startIndex: number } }).range.startIndex >= 588)).toHaveLength(n > 24 ? (n - 24) * 24 - 1 : 0);
    const sums = p.cells.find((c) => c.row === 771 && c.col === 3)!.value as { text: string };
    expect(sums.text).toContain(`N771:${colA1(slotStart(n - 1) + 1)}771`);
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
    oct.formulas[5]![589 + OFFSET.unitProfit - 1] = 12;
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
  it('LCD 01.10: факт в строке 771, 25 блоков, блок 25 пишется в VQ..WN', () => {
    const p = engineOn(oct, '2026-10-01', '2026-09-30');
    expect(p.layout).toMatchObject({ monthKey: '2026-10', firstDailyRow: 771, lastDailyRow: 801, mtdRow: 802 });
    expect(p.blocks).toHaveLength(25);
    const facts = p.cells.filter((c) => c.kind === 'fact');
    expect(new Set(facts.map((c) => c.row))).toEqual(new Set([771]));
    expect(facts.some((c) => c.nmId === NEW_NM && c.col === 589 + OFFSET.orders)).toBe(true);
    // Ставки — на все 31 строку каждого блока.
    expect(p.expected.filter((e) => e.kind === 'logistics' && e.nmId === NEW_NM).map((e) => e.row)).toEqual(Array.from({ length: 31 }, (_, i) => 771 + i));
  });
  it('LCD 31.10: последняя строка 801, закрыт весь месяц', () => {
    const p = engineOn(oct, '2026-10-31', '2026-10-30');
    expect(p.closedDays).toBe(31);
    expect(Math.max(...p.cells.filter((c) => c.kind === 'fact').map((c) => c.row))).toBe(801);
  });
  it('колонка 600 в строках октября — ячейка блока 25 (X = реклама); якорь — по виду и по anchorCol (WZ = 624 после вставки)', () => {
    const snap: Snapshot = { ...oct, anchorCol: 624, mirrorLcd: 11111, mirrorRev: 22222, namedLcd: 33333 };
    snap.grid[771 - oct.geometry.topRow]![600 - 1] = 7.77;
    expect(currentValue(snap, { row: 771, col: 600, kind: 'fact' })).toBe(7.77);
    expect(currentValue(snap, { row: 736, col: 624, kind: 'lcd' })).toBe(11111);
    expect(currentValue(snap, { row: 737, col: 624, kind: 'reverse' })).toBe(22222);
    const p = engineOn({ ...oct, anchorCol: 624 }, '2026-10-01', '2026-09-30');
    expect(p.expected.filter((e) => e.col === 624 && !e.namedRange).map((e) => [e.row, e.kind])).toEqual([[737, 'reverse'], [736, 'lcd']]);
    expect(p.expected.filter((e) => e.col === 600 && e.kind === 'fact')).toHaveLength(1); // X771 блока 25
  });
  it('LCD 01.11 на октябрьской секции — MONTH_SECTION_INVALID до любой записи', () => {
    expect(() => engineOn(oct, '2026-11-01', '2026-10-31')).toThrow(LoaderError);
    try { engineOn(oct, '2026-11-01', '2026-10-31'); } catch (e) { expect((e as LoaderError).code).toBe('MONTH_SECTION_INVALID'); }
  });
});

describe('F2 · книга с одним SKU: цепочка из одного блока кончается его последней метрикой', () => {
  const one = septemberSpec().slice(0, 1);
  const width = slotStart(0) + 23 + 12;                                   // блок M..AJ (с колонкой дня недели, как в книге до V2) + хвост 12 колонок
  const sept = sectionFromSpec({ year: 2026, month: 9 }, 735, one, width);
  const colA: CellValue[] = Array(768).fill(null); colA[734] = 'Сентябрь 2026';
  const base = { target: OCT, meta: { sheetId: 739487431, rowCount: 768, columnCount: width, anchorCol: width }, columnA: colA, predecessor: sept, existing: null, cogs: cogsSnapshot({ [NEW_NM]: 426.735 }), structure: septemberStructure(768, width), rowFormats: septemberRowFormats(width) };
  it('1 SKU → 1 SKU: колонки не вставляются, колонка дня недели AJ в новом месяце пуста (ни значений, ни формата, ни УФ)', () => {
    const p = planMonthPrep({ ...base, population: [{ nmId: one[0]!.nmId, name: 'Товар' }] });
    expect([p.status, p.insertColumns, p.layout!.lastBlockColumn]).toEqual(['PLAN_CREATE', null, 35]);
    expect(p.cells.some((c) => c.col > 35)).toBe(false);
    expect(p.formatCopies.some((f) => f.dest.c2 > 35)).toBe(false);
    expect(p.conditionalFormats!.rules.flatMap((r) => r.ranges).every((x) => (x.endColumnIndex ?? 0) <= 35)).toBe(true);
  });
  it('1 SKU → 2 SKU: вставка 23 колонок за AJ; AJ становится разделителем блока 1, у блока 2 разделителя нет', () => {
    const p = planMonthPrep({ ...base, population: [{ nmId: one[0]!.nmId, name: 'Товар' }, { nmId: NEW_NM, name: 'Набор' }] });
    expect([p.status, p.insertColumns, p.layout!.lastBlockColumn, p.anchorCol]).toEqual(['PLAN_CREATE', { at: 36, count: 23 }, 37 + 22, width + 23]);
    expect(p.cells.some((c) => c.row === 771 && c.col === 36)).toBe(true);
    expect(p.cells.some((c) => c.col > 59)).toBe(false);
  });
});

describe('границы в запросах подготовки: каждая строка секции получает контракт СВОЕГО типа строки', () => {
  const plan = planMonthPrep(inputs(septemberBook(), OCT));
  const g = plan.geometry!;
  type Rc = { repeatCell: { range: { startRowIndex: number; endRowIndex: number; startColumnIndex: number; endColumnIndex: number }; cell: { userEnteredFormat?: { borders?: Record<string, unknown> } } } };
  const runs = (toStructureRequests(plan, 739487431) as unknown as Rc[]).filter((r) => r.repeatCell && r.repeatCell.range.startRowIndex >= g.topRow - 1);
  const bordersAt = (row: number, col: number): Record<string, unknown> | undefined =>
    runs.find((r) => { const x = r.repeatCell.range; return row - 1 >= x.startRowIndex && row - 1 < x.endRowIndex && col - 1 >= x.startColumnIndex && col - 1 < x.endColumnIndex; })?.repeatCell.cell.userEnteredFormat?.borders;
  it('все строки 769..803 × все колонки A..WN: границы запроса = bordersJson(borderSpec(тип строки))', () => {
    for (let row = g.topRow; row <= g.spacerRow; row++) {
      const kind = rowKindOf(g, row)!;
      for (const col of [1, 2, 3, 7, 11, 12, 13, 14, 20, 36, 37, 301, 588, 589, 590, 600, 610, 611]) {
        expect(bordersAt(row, col), `${colA1(col)}${row}`).toEqual(bordersJson(borderSpec(kind, col, plan.layout!)!));
      }
    }
  });
  it('первый день — без верхней линии; дни 2..31 — волосяная сверху; ни у одного дня нет линии снизу (полоса дней не одна на весь месяц)', () => {
    const top = (row: number) => (bordersAt(row, 14 + OFFSET.views) as { top: { style: string }; bottom: { style: string } });
    expect(top(g.firstDailyRow).top.style).toBe('NONE');
    for (let row = g.firstDailyRow + 1; row <= g.lastDailyRow; row++) expect(top(row).top.style).toBe('SOLID');
    for (let row = g.firstDailyRow; row <= g.lastDailyRow; row++) expect(top(row).bottom.style).toBe('NONE');
    expect(top(g.mtdRow).top.style).toBe('SOLID_MEDIUM');
  });
});

/** Модель Sheets для формы проекции остатка: =IF($<дата>r>LAST_CLOSED_DATE,"",<остаток>r-1 − <заказы>r + <отмены>r-1); пусто в арифметике = 0. */
function evalStockProjection(formula: string, env: { date: number; lcd: number; prevStock: number | ''; orders: number | ''; prevCancels: number | '' }): number | '' {
  const m = /^=IF\(\$[A-Z]+\d+>LAST_CLOSED_DATE,"",[A-Z]+\d+-[A-Z]+\d+\+[A-Z]+\d+\)$/.exec(formula);
  if (!m) throw new Error(`не форма защищённой проекции остатка: ${formula}`);
  const n = (v: number | ''): number => (v === '' ? 0 : v);
  return env.date > env.lcd ? '' : n(env.prevStock) - n(env.orders) + n(env.prevCancels);
}
/** Формула блока в координатах смещений: буквы колонок → «c<смещение>», строка → относительная. */
function relStock(f: string, start: number, row: number): string {
  const num = (letters: string): number => [...letters].reduce((a, ch) => a * 26 + ch.charCodeAt(0) - 64, 0);
  return f.replace(/(\$?)([A-Z]{1,3})(\d+)/g, (_, d: string, l: string, r: string) => `${d}c${num(l) - start}r${Number(r) - row}`);
}

describe('проекция остатка: у КАЖДОГО созданного блока та же защита LAST_CLOSED_DATE, что у блока 1', () => {
  const plan = planMonthPrep(inputs(septemberBook(), OCT));
  const g = plan.geometry!;
  const stockF = (b: { start: number }, row: number): string | undefined => { const v = cellOf(plan, row, b.start + OFFSET.stock); return v?.kind === 'formula' ? v.text : undefined; };
  it('сентябрь-эталон: блок 1 с защитой, блоки 2..24 без неё — шаблон распознан (нет TEMPLATE_MISMATCH), но в новом месяце защита у всех', () => {
    expect(plan.status).toBe('PLAN_CREATE');
    expect(plan.blocks).toHaveLength(25);
    expect(plan.blocks.map((b) => b.params.stockProjection)).toEqual(Array(25).fill('guarded'));
  });
  it('день 1 — без проекции (нет ссылок на прошлый месяц); дни 2..31 — защищённая форма со своей датой, у блока 1 и блока N одинаковая', () => {
    const ref = relStock(stockF(plan.blocks[0]!, g.firstDailyRow + 1)!, plan.blocks[0]!.start, g.firstDailyRow + 1);
    expect(ref).toBe('=IF($c0r0>LAST_CLOSED_DATE,"",c7r-1-c4r0+c6r-1)');
    for (const b of plan.blocks) {
      expect(stockF(b, g.firstDailyRow)).toBeUndefined();
      for (let row = g.firstDailyRow + 1; row <= g.lastDailyRow; row++) {
        const f = stockF(b, row)!;
        expect(relStock(f, b.start, row)).toBe(ref);
        expect(f.startsWith(`=IF($${colA1(b.start)}${row}>LAST_CLOSED_DATE,"",`)).toBe(true);
      }
    }
  });
  it('будущий день — пусто; закрытый день с нулём — число 0; закрытый положительный — число; граница LCD точная', () => {
    const f = stockF(plan.blocks[24]!, g.firstDailyRow + 4)!;       // 05.10, блок 25
    const d = isoToSerial('2026-10-05');
    expect(evalStockProjection(f, { date: d, lcd: d - 1, prevStock: 40, orders: 3, prevCancels: 1 })).toBe('');      // будущее
    expect(evalStockProjection(f, { date: d, lcd: d, prevStock: 40, orders: 3, prevCancels: 1 })).toBe(38);          // LCD = дата: закрыт
    expect(evalStockProjection(f, { date: d + 1, lcd: d, prevStock: 40, orders: 3, prevCancels: 1 })).toBe('');      // LCD + 1: будущее
    const zero = evalStockProjection(f, { date: d, lcd: d + 3, prevStock: 0, orders: '', prevCancels: '' });
    expect(zero).toBe(0);                                                                                            // настоящий ноль остаётся нулём
    expect(zero === '').toBe(false);
    expect(evalStockProjection(f, { date: d, lcd: d + 3, prevStock: 2, orders: 2, prevCancels: 0 })).toBe(0);        // расчётный ноль
  });
  it('другие проекции не тронуты: хранение с защитой — только у блока 1 (как в сентябре), у нового блока — нет', () => {
    expect(plan.blocks.map((b) => b.params.storageProjection)).toEqual(['guarded', ...Array(24).fill('none')]);
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
      // Месяцы 28/29/30/31 дней: защищённая проекция остатка у всех блоков со 2-го дня по последний, в первом дне её нет.
      for (const b of p.blocks) {
        const f = (row: number) => { const v = cellOf(p, row, b.start + OFFSET.stock); return v?.kind === 'formula' ? v.text : undefined; };
        expect(f(g.firstDailyRow)).toBeUndefined();
        expect(f(g.lastDailyRow)).toBe(`=IF($${colA1(b.start)}${g.lastDailyRow}>LAST_CLOSED_DATE,"",${colA1(b.start + OFFSET.stock)}${g.lastDailyRow - 1}-${colA1(b.start + OFFSET.orders)}${g.lastDailyRow}+${colA1(b.start + OFFSET.cancels)}${g.lastDailyRow - 1})`);
      }
      const sec = book.sections[book.sections.length - 1]!;
      const last = addDaysIso(g.monthStart, g.daysInMonth - 1);
      const plan = engineOn(sec, last, addDaysIso(last, -1));
      expect(Math.max(...plan.cells.filter((c) => c.kind === 'fact').map((c) => c.row))).toBe(dayRowOf(g, g.daysInMonth - 1));
    }
    expect(new Set(Object.values(seen).map((v) => v[4]))).toEqual(new Set([28, 29, 30, 31]));
    expect(seen['2026-12']).toEqual([838, 840, 870, 871, 31, 25]);
    expect(seen['2027-01']).toEqual([873, 875, 905, 906, 31, 25]);
    expect(seen['2027-02']).toEqual([908, 910, 937, 938, 28, 25]);
    expect(seen['2028-02']).toEqual([1321, 1323, 1351, 1352, 29, 25]);
    expect(seen['2028-03']).toEqual([1354, 1356, 1386, 1387, 31, 25]);
    expect(book.rowCount).toBe(geometryAt({ year: 2028, month: 3 }, 1354).spacerRow);
    expect(book.columnCount).toBe(623);   // сентябрь 600 + 23 колонки блока 25 (без колонки-сироты за последним SKU)
  });
});
