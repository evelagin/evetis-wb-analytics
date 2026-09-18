/**
 * UNITKA CALENDAR V2 (Phase 2B) — чтение секции месяца из книги. ТОЛЬКО чтение.
 *
 * Суточный Engine: LAST_CLOSED_DATE → месяц → ровно один заголовок «<Месяц> <год>» в колонке A →
 * геометрия (calendar.ts) → снимок секции. Все отказы — до любой записи:
 *   MONTH_SECTION_MISSING   — заголовка месяца нет (месяц не подготовлен владельцем);
 *   MONTH_SECTION_AMBIGUOUS — заголовок встречается больше одного раза;
 *   MONTH_SECTION_INVALID   — секция не помещается в сетку листа (или не проходит контракт — plan.ts).
 * Месяц Engine НЕ создаёт никогда: это отдельный загрузчик unitka-month-prep (monthprep.ts).
 */
import { LoaderError } from '../../errors.js';
import type { SheetMeta, SheetsGateway } from './sheets.js';
import type { Snapshot } from './plan.js';
import { BOOK_ANCHORS, NAMED, colA1, type CellValue } from './model.js';
import {
  geometryAt, locateSection, monthKeyOf, nextMonth, formatMonthKey, dayIndexOf, daysInMonth,
  type MonthGeometry, type MonthKey,
} from './calendar.js';

export function quoteSheet(name: string): string {
  return `'${name.replace(/'/g, "''")}'`;
}

/** Колонка A целиком (A1:A{rowCount}); элемент i — значение A{i+1}. */
export async function readColumnA(sheets: SheetsGateway, sheetName: string, rowCount: number): Promise<CellValue[]> {
  const [col] = await sheets.readValues([`${quoteSheet(sheetName)}!A1:A${rowCount}`]);
  return (col ?? []).map((r) => (r?.[0] ?? null));
}

export interface SectionDiscovery {
  meta: SheetMeta;
  columnA: CellValue[];
  geometry: MonthGeometry;
}

/** Геометрия найденной секции месяца; отказ — LoaderError с кодом MONTH_SECTION_*. */
export function resolveSection(columnA: readonly CellValue[], meta: SheetMeta, key: MonthKey): MonthGeometry {
  const loc = locateSection(columnA, key);
  const mk = formatMonthKey(key);
  if (loc.status === 'MISSING') {
    throw new LoaderError(`секция месяца ${mk} не найдена в колонке A — месяц не подготовлен (загрузчик unitka-month-prep)`, 'MONTH_SECTION_MISSING');
  }
  if (loc.status === 'AMBIGUOUS') {
    throw new LoaderError(`секция месяца ${mk} встречается ${loc.rows.length} раза: строки ${loc.rows.join(', ')}`, 'MONTH_SECTION_AMBIGUOUS');
  }
  const g = geometryAt(key, loc.topRow);
  if (g.spacerRow > meta.rowCount) {
    throw new LoaderError(`секция ${mk} (A${g.topRow}) требует строк до ${g.spacerRow}, в листе ${meta.rowCount} — секция неполная`, 'MONTH_SECTION_INVALID');
  }
  return g;
}

export async function discoverSection(sheets: SheetsGateway, sheetName: string, lcdIso: string): Promise<SectionDiscovery> {
  const meta = await sheets.readSheetMeta(sheetName);
  const columnA = await readColumnA(sheets, sheetName, meta.rowCount);
  return { meta, columnA, geometry: resolveSection(columnA, meta, monthKeyOf(lcdIso)) };
}

/** Снимок секции: значения заголовок..MTD, формулы дней и MTD, форматы дней, якоря книги, имя LCD. */
export async function readSnapshot(sheets: SheetsGateway, sheetName: string, g: MonthGeometry, width: number): Promise<Snapshot> {
  const q = quoteSheet(sheetName);
  const last = colA1(width);
  const gridRange = `${q}!A${g.topRow}:${last}${g.mtdRow}`;
  const anchorRange = `${q}!${colA1(BOOK_ANCHORS.COL)}${BOOK_ANCHORS.LCD_MIRROR_ROW}:${colA1(BOOK_ANCHORS.COL)}${BOOK_ANCHORS.REVERSE_ROW}`;
  const [grid, anchors, named] = await sheets.readValues([gridRange, anchorRange, NAMED.LCD]);
  // Формулы — дни + строка MTD (подготовка месяца сверяет построители и с MTD); форматы — только дни.
  const formulas = await sheets.readFormulas(`${q}!A${g.firstDailyRow}:${last}${g.mtdRow}`);
  const fmt = await sheets.readFormats(`${q}!A${g.firstDailyRow}:${last}${g.lastDailyRow}`);
  return {
    geometry: g,
    width,
    grid: grid ?? [],
    formulas,
    mirrorLcd: anchors?.[0]?.[0] ?? null,
    mirrorRev: anchors?.[1]?.[0] ?? null,
    namedLcd: named?.[0]?.[0] ?? null,
    formats: fmt.rows,
    sheetId: fmt.sheetId,
  };
}

/* ───────────────────────── предпроверка следующего месяца ───────────────────────── */

export type NextMonthState = 'NOT_IN_WINDOW' | 'PRESENT' | 'MISSING' | 'AMBIGUOUS';
export interface NextMonthPrecheck {
  state: NextMonthState;
  nextMonth: string;
  daysLeft: number;      // дней месяца после LCD (0 — LCD = последний день)
  windowDays: number;
  code: 'NEXT_MONTH_SECTION_MISSING' | null;
}

/**
 * Только чтение: когда до конца месяца LCD осталось ≤ windowDays дней, проверяет, подготовлен ли
 * следующий месяц. Отсутствие — предупреждение NEXT_MONTH_SECTION_MISSING; прогон НЕ падает,
 * месяц НЕ создаётся, структура НЕ пишется.
 */
export function nextMonthPrecheck(columnA: readonly CellValue[], lcdIso: string, windowDays: number): NextMonthPrecheck {
  const k = monthKeyOf(lcdIso);
  const n = nextMonth(k);
  const daysLeft = daysInMonth(k.year, k.month) - (dayIndexOf(k, lcdIso) + 1);
  const base = { nextMonth: formatMonthKey(n), daysLeft, windowDays };
  if (daysLeft > windowDays) return { ...base, state: 'NOT_IN_WINDOW', code: null };
  const loc = locateSection(columnA, n);
  if (loc.status === 'FOUND') return { ...base, state: 'PRESENT', code: null };
  if (loc.status === 'AMBIGUOUS') return { ...base, state: 'AMBIGUOUS', code: null };
  return { ...base, state: 'MISSING', code: 'NEXT_MONTH_SECTION_MISSING' };
}
