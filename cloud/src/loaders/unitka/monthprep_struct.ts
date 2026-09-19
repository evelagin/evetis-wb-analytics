/**
 * UNITKA CALENDAR V2 (Phase 2C) — структура новой секции месяца: условное форматирование, размеры
 * колонок/строк, откат. ЧИСТЫЙ модуль: вход — структура листа (SheetStructure), выход — запросы.
 *
 * Условное форматирование (живая книга 19.09.2026: 154 правила, 151 — целиком в сентябре):
 *   • переносится правило, у которого есть диапазоны ровно на дни прошлого месяца (first..last);
 *     строки → дни нового месяца, колонки сводки и существующих слотов — те же;
 *   • правило на ВСЕ блоки прошлого месяца → новым блокам добавляются диапазоны тех же смещений
 *     (формула с относительными ссылками сама «сдвигается» по диапазону — семантика Sheets);
 *   • правило только последнего блока (per-block) → клон для каждого нового блока, ссылки на колонки
 *     этого блока в формуле сдвигаются на новый слот;
 *   • диапазоны вне дней прошлого месяца (строки заголовка/шапки, прошлые месяцы) — наследие, не
 *     переносятся (для правил «август+сентябрь» переносится только сентябрьская часть);
 *   • частичные диапазоны дней, резервный слот, ссылки на строки вне секции (кроме якорей книги
 *     WB736..WB739) — правило НЕ переносится и попадает в отчёт (ничего не угадываем).
 * Формулы УФ берутся из листа как есть (в локали книги); меняются только адреса.
 */
import type { ConditionalFormatRule, DimensionProps, SheetStructure, StructureRequest } from './sheets.js';
import { BOOK_ANCHORS, colA1 } from './model.js';
import { BLOCK_FIRST_COLUMN, BLOCK_WIDTH, isReservedSlot, slotOfColumn, slotStart, type MonthGeometry } from './calendar.js';

export interface CfCarry {
  requests: StructureRequest[];                 // addConditionalFormatRule, по порядку прошлого месяца
  carried: number;                              // правил перенесено
  extendedToNewBlocks: number;                  // из них расширено на новые блоки
  cloned: number;                               // клонов per-block правил для новых блоков
  skipped: Array<{ index: number; reason: string }>;
}

const REF = /(^|[^A-Za-z0-9_$.])(\$?)([A-Z]{1,3})(\$?)(\d+)(?![A-Za-z0-9_(])/g;

function colIndex(letters: string): number {
  let n = 0;
  for (const ch of letters) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n;
}

/** Переадресация ссылок формулы УФ из секции prev в секцию next. null — ссылка вне допустимого (правило не переносится). */
export function remapCfFormula(f: string, prev: MonthGeometry, next: MonthGeometry, colShift?: { from: number; to: number; delta: number }): string | null {
  let bad = false;
  // Строковые литералы не трогаем.
  const parts = f.split('"');
  for (let i = 0; i < parts.length; i += 2) {
    parts[i] = parts[i]!.replace(REF, (m, pre: string, cAbs: string, letters: string, rAbs: string, rowS: string) => {
      const col = colIndex(letters);
      const row = Number(rowS);
      if (col === BOOK_ANCHORS.COL && row >= BOOK_ANCHORS.LCD_MIRROR_ROW && row <= BOOK_ANCHORS.INVALID_ROW) return m; // якорь книги
      if (row < prev.firstDailyRow || row > prev.lastDailyRow) { bad = true; return m; }
      let r: number;
      if (rAbs === '$') {
        if (row === prev.firstDailyRow) r = next.firstDailyRow;
        else if (row === prev.lastDailyRow) r = next.lastDailyRow;
        else { bad = true; return m; }
      } else {
        r = row - prev.firstDailyRow + next.firstDailyRow; // относительная строка
      }
      let c = col;
      if (colShift && col >= colShift.from && col <= colShift.to) c = col + colShift.delta;
      return `${pre}${cAbs}${colA1(c)}${rAbs}${r}`;
    });
  }
  return bad ? null : parts.join('"');
}

function mapRule(rule: ConditionalFormatRule, fn: (f: string) => string | null): ConditionalFormatRule | null {
  if (!rule.booleanRule) return { ...rule };
  const values = rule.booleanRule.condition.values ?? [];
  const out: typeof values = [];
  for (const v of values) {
    if (v.userEnteredValue !== undefined && v.userEnteredValue.startsWith('=')) {
      const m = fn(v.userEnteredValue);
      if (m === null) return null;
      out.push({ ...v, userEnteredValue: m });
    } else out.push({ ...v });
  }
  return { ...rule, booleanRule: { ...rule.booleanRule, condition: { ...rule.booleanRule.condition, values: out } } };
}

/**
 * УФ новой секции из правил прошлого месяца. predSlots — слоты блоков прошлого месяца; newSlots — новые блоки
 * (шаблон — последний блок прошлого месяца).
 */
export function carryConditionalFormats(
  rules: readonly ConditionalFormatRule[], prev: MonthGeometry, next: MonthGeometry,
  predSlots: readonly number[], newSlots: readonly number[], sheetId: number, startIndex: number,
): CfCarry {
  const res: CfCarry = { requests: [], carried: 0, extendedToNewBlocks: 0, cloned: 0, skipped: [] };
  const r0 = prev.firstDailyRow - 1, r1 = prev.lastDailyRow; // 0-based, конец исключительно
  const allPred = new Set(predSlots);
  const template = Math.max(...predSlots);
  const add = (rule: ConditionalFormatRule): void => {
    res.requests.push({ addConditionalFormatRule: { rule, index: startIndex + res.requests.length } });
  };
  rules.forEach((rule, index) => {
    const daily = rule.ranges.filter((g) => (g.startRowIndex ?? 0) === r0 && g.endRowIndex === r1);
    const partial = rule.ranges.some((g) => (g.startRowIndex ?? 0) < r1 && (g.endRowIndex ?? 0) > r0 && !daily.includes(g));
    if (partial) { res.skipped.push({ index, reason: 'частичный диапазон дней — не переносится' }); return; }
    if (daily.length === 0) { res.skipped.push({ index, reason: 'нет диапазонов на дни прошлого месяца (наследие: шапка/прошлые месяцы)' }); return; }
    const slots = new Set<number>();
    for (const g of daily) {
      for (let c = (g.startColumnIndex ?? 0) + 1; c <= (g.endColumnIndex ?? 0); c++) {
        const s = slotOfColumn(c);
        if (s === null) continue;
        if (isReservedSlot(s)) { res.skipped.push({ index, reason: 'диапазон в зарезервированном слоте' }); return; }
        slots.add(s);
      }
    }
    const shiftRows = (g: ConditionalFormatRule['ranges'][number]) => ({ ...g, sheetId, startRowIndex: next.firstDailyRow - 1, endRowIndex: next.lastDailyRow });
    const base = mapRule(rule, (f) => remapCfFormula(f, prev, next));
    if (!base) { res.skipped.push({ index, reason: 'формула ссылается на строки вне секции' }); return; }
    const ranges = daily.map(shiftRows);
    const allBlocks = slots.size > 0 && [...allPred].every((s) => slots.has(s));
    if (allBlocks && newSlots.length) {
      // Смещения, которые правило покрывает в шаблонном блоке → те же смещения в новых блоках.
      const offs: Array<[number, number]> = [];
      for (const g of daily) {
        const a = (g.startColumnIndex ?? 0) + 1, b = g.endColumnIndex ?? 0;
        const lo = Math.max(a, slotStart(template)), hi = Math.min(b, slotStart(template) + BLOCK_WIDTH - 1);
        if (lo <= hi) offs.push([lo - slotStart(template), hi - slotStart(template)]);
      }
      for (const s of newSlots) for (const [o1, o2] of offs) {
        ranges.push({ sheetId, startRowIndex: next.firstDailyRow - 1, endRowIndex: next.lastDailyRow, startColumnIndex: slotStart(s) + o1 - 1, endColumnIndex: slotStart(s) + o2 });
      }
      res.extendedToNewBlocks++;
    }
    add({ ...base, ranges });
    res.carried++;
    // Правило только шаблонного блока → клон для новых блоков.
    if (slots.size === 1 && slots.has(template) && newSlots.length) {
      for (const s of newSlots) {
        const delta = slotStart(s) - slotStart(template);
        const shift = { from: slotStart(template), to: slotStart(template) + BLOCK_WIDTH - 1, delta };
        const clone = mapRule(rule, (f) => remapCfFormula(f, prev, next, shift));
        if (!clone) { res.skipped.push({ index, reason: 'клон: формула вне секции' }); continue; }
        const cr = daily.map((g) => {
          const a = (g.startColumnIndex ?? 0), b = g.endColumnIndex ?? 0;
          const inTpl = a + 1 >= slotStart(template) && b <= slotStart(template) + BLOCK_WIDTH - 1;
          return inTpl ? { sheetId, startRowIndex: next.firstDailyRow - 1, endRowIndex: next.lastDailyRow, startColumnIndex: a + delta, endColumnIndex: b + delta } : null;
        }).filter((x): x is NonNullable<typeof x> => x !== null);
        if (cr.length) { add({ ...clone, ranges: cr }); res.cloned++; }
      }
    }
  });
  return res;
}

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
 * ≥ topRow, затем добавленные колонки (только если они пусты во всех строках < topRow), затем строки
 * topRow..rowCount. Строки и колонки до секции не адресуются.
 */
export function planMonthRollback(a: {
  geometry: MonthGeometry; rowCount: number; columnCount: number; preColumnCount: number; sheetId: number;
  rules: readonly ConditionalFormatRule[]; newColumnsEmptyAbove: boolean;
}): RollbackPlan {
  const g = a.geometry;
  if (a.rowCount < g.spacerRow) return { requests: [], deletedCfRules: 0, deletedRows: null, deletedColumns: null, refused: 'в листе нет полной секции' };
  if (a.columnCount > a.preColumnCount && !a.newColumnsEmptyAbove) {
    return { requests: [], deletedCfRules: 0, deletedRows: null, deletedColumns: null, refused: 'добавленные колонки непусты выше секции — откат колонок небезопасен' };
  }
  const req: StructureRequest[] = [];
  const idx: number[] = [];
  a.rules.forEach((r, i) => { if (r.ranges.length && r.ranges.every((x) => (x.startRowIndex ?? 0) >= g.topRow - 1)) idx.push(i); });
  // Правила «через границу» (старые правила, расширенные на новую секцию) удалять нельзя: удаление строк и
  // колонок новой секции само обрезает их диапазоны. Итог отката проверяется сверкой с предснимком.
  for (const i of idx.sort((x, y) => y - x)) req.push({ deleteConditionalFormatRule: { sheetId: a.sheetId, index: i } });
  let cols: [number, number] | null = null;
  if (a.columnCount > a.preColumnCount) {
    cols = [a.preColumnCount + 1, a.columnCount];
    req.push({ deleteDimension: { range: { sheetId: a.sheetId, dimension: 'COLUMNS', startIndex: a.preColumnCount, endIndex: a.columnCount } } });
  }
  req.push({ deleteDimension: { range: { sheetId: a.sheetId, dimension: 'ROWS', startIndex: g.topRow - 1, endIndex: a.rowCount } } });
  return { requests: req, deletedCfRules: idx.length, deletedRows: [g.topRow, a.rowCount], deletedColumns: cols, refused: null };
}

/** Колонки, которые не принадлежат ни одному слоту (A..L — сводка). */
export const SUMMARY_LAST_COLUMN = BLOCK_FIRST_COLUMN - 1;
