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
