/**
 * Фикстуры Calendar V2 (Phase 2B): синтетическая книга из секций месяцев.
 *   sectionFromSpec — секция месяца в форме Snapshot, формулы — канонические построители;
 *   applyPlan       — «исполнить» план подготовки месяца в памяти (как это сделал бы batchUpdate)
 *                     и получить Snapshot новой секции — для идемпотентности и цепочки месяцев.
 * Ни Sheets, ни BigQuery.
 */
import { OFFSET, SUMMARY, isoToSerial, addDaysIso, type CellValue } from '../src/loaders/unitka/model.js';
import { BLOCK_WIDTH, dayRowOf, geometryAt, slotStart, type MonthGeometry, type MonthKey } from '../src/loaders/unitka/calendar.js';
import { DATE_HEADER, MTD_LABEL, type Snapshot } from '../src/loaders/unitka/plan.js';
import { blockDayFormulas, blockProjectionFormulas, blockMtdFormulas, summaryDayFormulas, summaryMtdFormulas, type BlockFormulaParams } from '../src/loaders/unitka/formulas.js';
import type { MonthPrepPlan } from '../src/loaders/unitka/monthprep.js';
import type { CogsSnapshot } from '../src/loaders/unitka/integrity.js';

/** 24 живых nmID сентября 2026 в порядке слотов (Phase 2A) + 909951444 (активен, блока нет). */
export const SEPT_NMS = [
  252442517, 252442341, 252441968, 305101272, 305101361, 438775437, 438775617, 567668636, 567668635, 535581674,
  535581675, 535580776, 773170315, 773170316, 868597351, 593111986, 593111985, 930334396, 930334395, 930334397,
  952068582, 910584041, 1083392113, 910330849,
] as const;
export const NEW_NM = 909951444;
/** COGS-слагаемые: структура как в живом листе (блок 1 — $R$45, блоки 2–3 — 0), прочие числа синтетические. */
export const SEPT_COGS: readonly string[] = ['$R$45', '0', '0', ...Array.from({ length: 21 }, (_, i) => `${100 + i * 10}.25`)];

export const BLOCK_HEADERS = [DATE_HEADER, 'Блогеры + самовыкупы ', 'Показы', 'Переходы', 'Заказы факт', 'Положили в корзину', 'Отменили товаров',
  'Остатки', 'Оборачиваемость (дн)', 'Доходность на 1 шт', 'Доходность (общая)', 'Реклама внутрення (затраты)',
  'Внешняя реклама (затраты на блогеров)', 'ДРР', 'цена ', 'СПП %', 'цена с СПП', 'комиссия', 'цена минус комиссия WB', 'логистика',
  'Хранение', 'налог (резерв)', 'доходность 1 шт'];
export const SUMMARY_HEADERS: Array<[number, string]> = [[2, DATE_HEADER], [3, 'Блогеры + самовыкупы '], [4, 'Показы'], [5, 'Переходы'],
  [6, 'Заказы 24 SKU'], [7, 'Положили в корзину 24 SKU'], [8, 'Отменили товаров'], [9, 'Доходность (общая)'], [10, 'Реклама (затраты)'], [11, 'ДРР']];

export interface SpecBlock { slot: number; nmId: number; title: string; params: Omit<BlockFormulaParams, 'start'> }

/** Параметры сентябрьских блоков как в живом листе: блок 1 — +100, проекции с обёрткой; прочие — +10, проекция остатка без обёртки. */
export function septemberSpec(): SpecBlock[] {
  return SEPT_NMS.map((nm, i) => ({
    slot: i, nmId: nm, title: `${nm} Товар ${i + 1}`,
    params: i === 0
      ? { cogsTerm: SEPT_COGS[i]!, overhead: '100', stockProjection: 'guarded', storageProjection: 'guarded' }
      : { cogsTerm: SEPT_COGS[i]!, overhead: '10', stockProjection: 'plain', storageProjection: 'none' },
  }));
}

function blank(g: MonthGeometry, width: number): Snapshot {
  const rows = g.mtdRow - g.topRow + 1;
  return {
    geometry: g, width,
    grid: Array.from({ length: rows }, () => Array<CellValue>(width).fill('')),
    formulas: Array.from({ length: g.mtdRow - g.firstDailyRow + 1 }, () => Array<CellValue>(width).fill('')),
    formats: [], sheetId: 739487431, anchorCol: 600, mirrorLcd: null, mirrorRev: 32.136, namedLcd: null,
  };
}
function setVal(s: Snapshot, row: number, col: number, v: CellValue): void {
  s.grid[row - s.geometry.topRow]![col - 1] = v;
  const fi = row - s.geometry.firstDailyRow;
  if (fi >= 0 && s.formulas[fi]) s.formulas[fi]![col - 1] = v;
}
function setFormula(s: Snapshot, row: number, col: number, f: string): void {
  s.grid[row - s.geometry.topRow]![col - 1] = '';          // вычисленное значение неизвестно — пусто
  s.formulas[row - s.geometry.firstDailyRow]![col - 1] = f;
}

/** Секция месяца по спецификации блоков; closedThrough — даты ≤ этой считаются закрытыми (проекции заменены значениями). */
export function sectionFromSpec(key: MonthKey, topRow: number, spec: readonly SpecBlock[], width: number): Snapshot {
  const g = geometryAt(key, topRow);
  const s = blank(g, width);
  const lastSlot = Math.max(...spec.map((b) => b.slot));
  const firstStart = slotStart(Math.min(...spec.map((b) => b.slot)));
  setVal(s, g.topRow, 1, g.title);
  for (const [c, t] of SUMMARY_HEADERS) setVal(s, g.headerRow, c, t);
  for (const b of spec) {
    const st = slotStart(b.slot);
    setVal(s, g.topRow, st, b.title);
    BLOCK_HEADERS.forEach((h, o) => setVal(s, g.headerRow, st + o, h));
  }
  for (let i = 0; i < g.daysInMonth; i++) {
    const row = dayRowOf(g, i);
    const serial = isoToSerial(addDaysIso(g.monthStart, i));
    setVal(s, row, SUMMARY.date, serial);
    for (const [c, f] of summaryDayFormulas(row, lastSlot, firstStart)) setFormula(s, row, c, f);
    for (const b of spec) {
      const p = { ...b.params, start: slotStart(b.slot) };
      setVal(s, row, p.start + OFFSET.date, serial);
      for (const [o, f] of blockDayFormulas(p, row)) setFormula(s, row, p.start + o, f);
      for (const [o, f] of blockProjectionFormulas(p, row, i === 0)) setFormula(s, row, p.start + o, f);
    }
  }
  for (const [c, f] of summaryMtdFormulas(g, lastSlot)) setFormula(s, g.mtdRow, c, f);
  setVal(s, g.mtdRow, firstStart + OFFSET.date, MTD_LABEL);
  for (const b of spec) for (const [o, f] of blockMtdFormulas(slotStart(b.slot), g)) setFormula(s, g.mtdRow, slotStart(b.slot) + o, f);
  return s;
}

/** Исполнить план в памяти: секция нового месяца как Snapshot (значения + формулы). */
export function applyPlan(plan: MonthPrepPlan, width: number): Snapshot {
  if (plan.status !== 'PLAN_CREATE' || !plan.geometry) throw new Error(`план ${plan.status} не исполняется`);
  const s = blank(plan.geometry, width);
  for (const c of plan.cells) {
    if (c.value.kind === 'formula') setFormula(s, c.row, c.col, c.value.text);
    else setVal(s, c.row, c.col, c.value.value);
  }
  return s;
}

/** Вставка пустых колонок в снимок секции (как insertDimension): at — число колонок перед вставкой. */
export function insertColumnsInto(s: Snapshot, at: number, count: number): Snapshot {
  const ins = (rows: CellValue[][]): CellValue[][] => rows.map((r) => [...r.slice(0, at), ...Array<CellValue>(count).fill(''), ...r.slice(at)]);
  return { ...s, width: s.width + count, grid: ins(s.grid), formulas: ins(s.formulas), anchorCol: s.anchorCol + count };
}
/** Удаление колонок c1..c2 (1-based, включительно) из снимка секции (как deleteDimension). */
export function deleteColumnsFrom(s: Snapshot, c1: number, c2: number): Snapshot {
  const del = (rows: CellValue[][]): CellValue[][] => rows.map((r) => [...r.slice(0, c1 - 1), ...r.slice(c2)]);
  return { ...s, width: s.width - (c2 - c1 + 1), grid: del(s.grid), formulas: del(s.formulas), anchorCol: s.anchorCol - (c2 - c1 + 1) };
}
/** Записать ячейки плана в снимок (значения и формулы). */
export function applyCells(s: Snapshot, cells: MonthPrepPlan['cells']): Snapshot {
  const out: Snapshot = { ...s, grid: s.grid.map((r) => [...r]), formulas: s.formulas.map((r) => [...r]) };
  for (const c of cells) {
    if (c.value.kind === 'formula') setFormula(out, c.row, c.col, c.value.text);
    else setVal(out, c.row, c.col, c.value.value);
  }
  return out;
}

/** Копия COGS «свежая» (AVAILABLE) по строкам nm → значение на дату. */
export function cogsSnapshot(values: Record<number, number | null>, day = '2026-09-17', over: Partial<CogsSnapshot> = {}): CogsSnapshot {
  return {
    state: 'AVAILABLE', ageHours: 0.17, reason: null, publishedAt: '2026-09-18T16:50:03Z', runId: 'df35bcb7-48bb-49da-af16-7daed26f741d',
    rows: Object.entries(values).map(([nm, v]) => ({ nmId: Number(nm), internalSku: null, day, cogsIntervalCount: v === null ? 0 : 1, canonicalCogs: v })),
    ...over,
  };
}

/** Все колонки, которые план записал в данной строке (для проверок). */
export function colsWritten(plan: MonthPrepPlan, row: number): number[] {
  return plan.cells.filter((c) => c.row === row).map((c) => c.col);
}

export const WIDTH_SEPT = 600;
export { BLOCK_WIDTH };

/* ───────────── Phase 2C: структура листа (УФ, размеры) по образцу живой книги 19.09.2026 ───────────── */

import type { ConditionalFormatRule, SheetStructure } from '../src/loaders/unitka/sheets.js';
import { colA1 } from '../src/loaders/unitka/model.js';

export const SHEET_ID = 739487431;
const rng = (r1: number, r2: number, c1: number, c2 = c1) => ({ sheetId: SHEET_ID, startRowIndex: r1 - 1, endRowIndex: r2, startColumnIndex: c1 - 1, endColumnIndex: c2 });
const FMT = { backgroundColor: { red: 0.9, green: 0.9, blue: 0.9 } };
const boolRule = (ranges: ConditionalFormatRule['ranges'], formula: string): ConditionalFormatRule => ({ ranges, booleanRule: { condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: formula }] }, format: FMT } });
const gradRule = (ranges: ConditionalFormatRule['ranges']): ConditionalFormatRule => ({ ranges, gradientRule: { minpoint: { type: 'MIN', color: {} }, midpoint: { type: 'PERCENTILE', value: '50', color: {} }, maxpoint: { type: 'MAX', color: {} } } });

/** Категории правил живой книги (формулы — в локали ru_RU, как отдаёт API). */
export function septemberCfRules(): ConditionalFormatRule[] {
  const S = (slot: number) => slotStart(slot);
  const rules: ConditionalFormatRule[] = [];
  // 1) per-block «будущий день»: =$<дата блока>737>$WB$736 на смещения 0..5 блока (блок 1 — ещё сводка B..K).
  for (let s = 0; s < 24; s++) {
    const ranges = [rng(737, 766, S(s), S(s) + 5)];
    if (s === 0) ranges.push(rng(737, 766, 2, 11));
    rules.push(boolRule(ranges, `=$${colA1(S(s))}737>$WB$736`));
  }
  // 1б) как живое правило 62: «будущий день» на ВЕСЬ блок 24 — диапазон кончается ровно на VP (588), перед вставкой.
  rules.push(boolRule([rng(737, 766, S(23), S(23) + 23)], '=$US737>$WB$736'));
  // 2) на все блоки: выходной день (колонка дня недели, смещение 23), первая — блок 24.
  rules.push(boolRule([rng(737, 766, S(23) + 23), ...Array.from({ length: 23 }, (_, s) => rng(737, 766, S(s) + 23))], '=WEEKDAY(US737;2)>5'));
  // 3) на все блоки + сводка G: доля от максимума в секции (абсолютные строки 737..766).
  rules.push(boolRule([rng(737, 766, S(23) + 5), ...Array.from({ length: 23 }, (_, s) => rng(737, 766, S(s) + 5)), rng(737, 766, 7)], '=AND(UX737<>"";UX737>0,75*MAX(UX$737:UX$766))'));
  // 4) наследие «август+сентябрь»: одна шкала на два месяца (блок 1, смещение 9).
  rules.push(gradRule([rng(702, 732, S(0) + 9), rng(737, 766, S(0) + 9)]));
  // 5) наследие: шкала на строках заголовка/шапки (735..736) — не переносится.
  rules.push(gradRule([rng(735, 736, S(0) + 10)]));
  // 6) per-block шкала на смещение 10 для блоков 2..24 (шаблон — блок 24).
  for (let s = 1; s < 24; s++) rules.push(gradRule([rng(737, 766, S(s) + 10)]));
  // 7) частичный диапазон дней (737..750) — не переносится (ничего не угадываем).
  rules.push(boolRule([rng(737, 750, 3)], '=C737=""'));
  return rules;
}

/** Размеры: ширина 100, блоки 2..24 скрывают смещения 16..22, VQ..VZ (резерв) скрыты; строки 18 px. */
export function septemberStructure(rowCount = 768, columnCount = 600): SheetStructure {
  const columnMetadata = Array.from({ length: columnCount }, (_, i) => {
    const c = i + 1;
    const slot = c >= 13 ? Math.floor((c - 13) / 24) : -1, off = c >= 13 ? (c - 13) % 24 : -1;
    const hidden = (slot >= 1 && slot <= 23 && off >= 16 && off <= 22) || (c >= 589 && c <= 598);
    return { pixelSize: off === 0 ? 80 : 100, hiddenByUser: hidden };
  });
  const rowMetadata = Array.from({ length: rowCount }, (_, i) => ({ pixelSize: i + 1 === 735 ? 30 : 18 }));
  // Группы колонок как в живом листе: у каждого блока — аналитика (смещения 16..22), у скрытых блоков 2–3 — целиком.
  const columnGroups = [
    ...Array.from({ length: 24 }, (_, s) => ({ startIndex: slotStart(s) + 16 - 1, endIndex: slotStart(s) + 22, depth: 1 })).filter((g) => g.startIndex !== slotStart(1) + 15 && g.startIndex !== slotStart(2) + 15),
    { startIndex: slotStart(1) - 1, endIndex: slotStart(2) + 23, depth: 1 },
    // хвост книги (12 колонок до якоря): группа скрытых расчётов начинается с ПЕРВОЙ колонки хвоста — как в живой книге (VQ..VZ).
    { startIndex: columnCount - 12, endIndex: columnCount - 2, depth: 1 },
  ];
  return { conditionalFormats: septemberCfRules(), columnMetadata, rowMetadata, merges: [], columnGroups };
}

/** Форматы строк-шаблонов: у каждой ячейки метка «строка:колонка» (чтобы проверять, откуда взят формат). */
export function septemberRowFormats(width = 600, rows: readonly number[] = [735, 736, 737, 766, 767, 768]): Map<number, Array<Record<string, unknown> | null>> {
  const m = new Map<number, Array<Record<string, unknown> | null>>();
  // Кегли как в живой книге: заголовок месяца/блока 20 пт, шапка 12, дни 10, MTD 16 (порядок строк: заголовок, шапка, дни…, MTD, план).
  const order = [...rows].sort((a, b) => a - b);
  const fontOf = (r: number): number => (r === order[0] ? 20 : r === order[1] ? 12 : r === order[order.length - 2] ? 16 : 10);
  for (const r of rows) m.set(r, Array.from({ length: width }, (_, i) => ({ numberFormat: { type: 'TEXT', pattern: `${r}:${i + 1}` }, textFormat: { fontSize: fontOf(r), bold: true } })));
  return m;
}
