/**
 * UNITKA CALENDAR V2 (Phase 2C) — структура новой секции месяца: размеры колонок/строк и откат.
 * ЧИСТЫЙ модуль: вход — структура листа (SheetStructure), выход — запросы.
 * Условное форматирование новой секции строится из визуального контракта (visual.ts): правила прошлого
 * месяца НЕ копируются — в живом сентябре их формулы записаны относительно первого диапазона правила и
 * для остальных диапазонов ссылаются в чужие колонки (см. visual.ts).
 */
import type { ConditionalFormatRule, DimensionProps, SheetStructure, StructureRequest } from './sheets.js';
import { BLOCK_FIRST_COLUMN, BLOCK_WIDTH, slotStart, type MonthGeometry } from './calendar.js';

/* ───────────────────────── размеры колонок и строк ───────────────────────── */

function dimReq(sheetId: number, dimension: 'ROWS' | 'COLUMNS', start1: number, end1: number, p: DimensionProps): StructureRequest {
  const properties: Record<string, unknown> = {};
  const fields: string[] = [];
  if (p.pixelSize !== undefined) { properties.pixelSize = p.pixelSize; fields.push('pixelSize'); }
  properties.hiddenByUser = p.hiddenByUser === true; fields.push('hiddenByUser');
  return { updateDimensionProperties: { range: { sheetId, dimension, startIndex: start1 - 1, endIndex: end1 }, properties, fields: fields.join(',') } };
}

/**
 * Колонки новых блоков — ширина и скрытие по смещению шаблонного блока (последний блок прошлого месяца;
 * у блоков 2–24 скрыты смещения 16–22). Скрытие резервного слота не копируется никогда. Строки новой
 * секции — высоты строк того же типа в прошлом месяце (заголовок, шапка, день 1, дни 2..N, MTD, разделитель).
 */
export function dimensionRequests(st: SheetStructure, prev: MonthGeometry, next: MonthGeometry, templateSlot: number, newSlots: readonly number[], sheetId: number): StructureRequest[] {
  const req: StructureRequest[] = [];
  const cm = (col: number): DimensionProps => st.columnMetadata[col - 1] ?? {};
  const rm = (row: number): DimensionProps => st.rowMetadata[row - 1] ?? {};
  for (const s of newSlots) {
    for (let o = 0; o < BLOCK_WIDTH; o++) {
      const p = cm(slotStart(templateSlot) + o);
      req.push(dimReq(sheetId, 'COLUMNS', slotStart(s) + o, slotStart(s) + o, { pixelSize: p.pixelSize, hiddenByUser: p.hiddenByUser === true }));
    }
  }
  const rows: Array<[number, number, number]> = [
    [prev.topRow, next.topRow, next.topRow], [prev.headerRow, next.headerRow, next.headerRow],
    [prev.firstDailyRow, next.firstDailyRow, next.firstDailyRow], [prev.lastDailyRow, next.firstDailyRow + 1, next.lastDailyRow],
    [prev.mtdRow, next.mtdRow, next.mtdRow], [prev.spacerRow, next.spacerRow, next.spacerRow],
  ];
  for (const [src, a, b] of rows) {
    const p = rm(src);
    req.push(dimReq(sheetId, 'ROWS', a, b, { pixelSize: p.pixelSize, hiddenByUser: p.hiddenByUser === true }));
  }
  return req;
}

/* ───────────────────────── вставка колонок и УФ прошлых месяцев ───────────────────────── */

const colNumber = (letters: string): number => [...letters].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
function colLetters(n: number): string {
  let s = '';
  for (let x = n; x > 0; x = Math.floor((x - 1) / 26)) s = String.fromCharCode(65 + ((x - 1) % 26)) + s;
  return s;
}

/** Строковый литерал | ссылка на ячейку ($WB$736) | диапазон колонок ($WA:$WB). Имена функций и диапазонов не задеваются. */
const REF_TOKEN = /"(?:[^"]|"")*"|(?<![A-Za-z0-9_.$])(\$?)([A-Z]{1,3})(\$?)(\d+)(?![A-Za-z0-9_(])|(?<![A-Za-z0-9_.$])(\$?)([A-Z]{1,3}):(\$?)([A-Z]{1,3})(?![A-Za-z0-9_($])/g;

/**
 * Сдвиг A1-ссылок формулы УФ при вставке count колонок после колонки insertAt (1-based номер колонки перед
 * вставкой = 0-based индекс вставки): ссылки на колонки правее едут на count — ровно то, что делает сам Sheets.
 * Не формула — возвращается как есть. Ссылка на другой лист — отказ: такие формулы не переиздаём.
 */
export function shiftColumnRefs(formula: string, insertAt: number, count: number): string {
  if (!formula.startsWith('=')) return formula;
  const shift = (letters: string): string => (colNumber(letters) > insertAt ? colLetters(colNumber(letters) + count) : letters);
  if (formula.replace(/"(?:[^"]|"")*"/g, '').includes('!')) throw new RangeError(`формула УФ ссылается на другой лист: ${formula}`);
  return formula.replace(REF_TOKEN, (m, d1: string | undefined, c: string | undefined, d2: string, row: string, e1: string, a: string | undefined, e2: string, b: string) => {
    if (c !== undefined) return `${d1}${shift(c)}${d2}${row}`;
    if (a !== undefined) return `${e1}${shift(a)}:${e2}${shift(b)}`;
    return m;
  });
}

export interface CfTrim { index: number; rule: ConditionalFormatRule }

/**
 * insertDimension(inheritFromBefore) РАСШИРЯЕТ диапазоны УФ, которые кончаются ровно на колонке перед вставкой
 * (живая книга: правило выходных и «будущий день» блока 24 кончаются на VP). Их формулы записаны относительно
 * первого диапазона правила, во вставленных колонках они ссылаются за край листа, WEEKDAY(пусто)=6 — и сентябрь
 * получает сплошную полосу на месте нового блока. Поэтому такие правила переиздаются сразу за вставкой в том
 * виде, какой они имели бы без расширения: те же диапазоны в том же порядке (диапазоны правее вставки едут с
 * хвостом, диапазон через вставку расширяется), ссылки формул сдвинуты как у Sheets. Остальные правила не трогаем.
 */
export function cfInsertTrims(rules: readonly ConditionalFormatRule[], insertAt: number, count: number): { trims: CfTrim[]; unsafe: string[] } {
  const trims: CfTrim[] = [];
  const unsafe: string[] = [];
  rules.forEach((src, index) => {
    if (!src.ranges.some((r) => r.endColumnIndex === insertAt && (r.startColumnIndex ?? 0) < insertAt)) return;
    const rule = JSON.parse(JSON.stringify(src)) as ConditionalFormatRule;
    rule.ranges = rule.ranges.map((r) => ({
      ...r,
      ...(r.startColumnIndex !== undefined && r.startColumnIndex >= insertAt ? { startColumnIndex: r.startColumnIndex + count } : {}),
      ...(r.endColumnIndex !== undefined && r.endColumnIndex > insertAt ? { endColumnIndex: r.endColumnIndex + count } : {}),
    }));
    try {
      for (const v of rule.booleanRule?.condition.values ?? []) if (v.userEnteredValue !== undefined) v.userEnteredValue = shiftColumnRefs(v.userEnteredValue, insertAt, count);
      for (const pt of Object.values(rule.gradientRule ?? {}) as Array<{ value?: string }>) if (typeof pt?.value === 'string') pt.value = shiftColumnRefs(pt.value, insertAt, count);
      trims.push({ index, rule });
    } catch (e) {
      unsafe.push(`правило УФ ${index}: ${(e as Error).message}`);
    }
  });
  return { trims, unsafe };
}

/* ───────────────────────── откат ───────────────────────── */

export interface RollbackPlan {
  requests: StructureRequest[];
  deletedCfRules: number;
  deletedRows: [number, number] | null;      // 1-based включительно
  deletedColumns: [number, number] | null;
  refused: string | null;
}

/**
 * Откат созданного месяца (секция — последняя в листе): удалить правила УФ, целиком лежащие в строках
 * ≥ topRow, затем вставленные колонки новых блоков (только если они пусты во всех строках < topRow;
 * группы колонок внутри них удаляются вместе с ними), затем строки topRow..rowCount. Строки и колонки до секции не адресуются.
 */
export function planMonthRollback(a: {
  geometry: MonthGeometry; rowCount: number; sheetId: number;
  rules: readonly ConditionalFormatRule[];
  /** Колонки блоков, вставленных этим месяцем (1-based, включительно), или null. */
  insertedColumns: [number, number] | null; newColumnsEmptyAbove: boolean;
}): RollbackPlan {
  const g = a.geometry;
  if (a.rowCount < g.spacerRow) return { requests: [], deletedCfRules: 0, deletedRows: null, deletedColumns: null, refused: 'в листе нет полной секции' };
  if (a.insertedColumns && !a.newColumnsEmptyAbove) {
    return { requests: [], deletedCfRules: 0, deletedRows: null, deletedColumns: null, refused: 'вставленные колонки непусты выше секции — откат колонок небезопасен' };
  }
  const req: StructureRequest[] = [];
  const idx: number[] = [];
  a.rules.forEach((r, i) => { if (r.ranges.length && r.ranges.every((x) => (x.startRowIndex ?? 0) >= g.topRow - 1)) idx.push(i); });
  // Правила «через границу» (старые правила, расширенные на новую секцию) удалять нельзя: удаление строк и
  // колонок новой секции само обрезает их диапазоны. Итог отката проверяется сверкой с предснимком.
  for (const i of idx.sort((x, y) => y - x)) req.push({ deleteConditionalFormatRule: { sheetId: a.sheetId, index: i } });
  let cols: [number, number] | null = null;
  if (a.insertedColumns) {
    cols = a.insertedColumns;
    // Удаление вставленных колонок возвращает хвост книги (и именованный диапазон REVERSE_LEG_RATE) на место.
    req.push({ deleteDimension: { range: { sheetId: a.sheetId, dimension: 'COLUMNS', startIndex: cols[0] - 1, endIndex: cols[1] } } });
  }
  req.push({ deleteDimension: { range: { sheetId: a.sheetId, dimension: 'ROWS', startIndex: g.topRow - 1, endIndex: a.rowCount } } });
  return { requests: req, deletedCfRules: idx.length, deletedRows: [g.topRow, a.rowCount], deletedColumns: cols, refused: null };
}

/** Колонки, которые не принадлежат ни одному слоту (A..L — сводка). */
export const SUMMARY_LAST_COLUMN = BLOCK_FIRST_COLUMN - 1;
