/**
 * UNITKA CALENDAR V2 (Phase 2C) — структура новой секции месяца: размеры колонок/строк и откат.
 * ЧИСТЫЙ модуль: вход — структура листа (SheetStructure), выход — запросы.
 * Условное форматирование новой секции строится из визуального контракта (visual.ts): правила прошлого
 * месяца НЕ копируются — в живом сентябре их формулы записаны относительно первого диапазона правила и
 * для остальных диапазонов ссылаются в чужие колонки (см. visual.ts).
 */
import type { ConditionalFormatRule, DimensionProps, RawCellFormat, SheetStructure, StructureRequest } from './sheets.js';
import { BLOCK_FIRST_COLUMN, BLOCK_WIDTH, slotStart, type MonthGeometry, type MonthLayout } from './calendar.js';
import { OFFSET, SUMMARY } from './model.js';

/* ───────────────────────── контракт размеров (final polish, F3) ───────────────────────── */

/**
 * Ширина колонки — по СМЫСЛУ, а не по координате. Живая книга: все колонки блока 84 px, разделитель 56 px; итог
 * месяца (строка MTD) набран кеглем 16 — шестизначная сумма в рублях в 84 px не помещается («####»), поэтому итоговые
 * денежные колонки шире: «доходность (общая)» — как I сводки (104), «реклама внутренняя» — как J сводки (100).
 */
const BLOCK_WIDTH_DEFAULT = 84;
const BLOCK_WIDTH_BY_OFFSET: Readonly<Record<number, number>> = { [OFFSET.profitAll]: 104, [OFFSET.adsIn]: 100, [OFFSET.weekday]: 56 };
const SUMMARY_WIDTH: Readonly<Record<number, number>> = {
  [SUMMARY.weekday]: 73, [SUMMARY.date]: 73, [SUMMARY.bloggers]: 92, [SUMMARY.views]: 102, [SUMMARY.opens]: 82, [SUMMARY.orders]: 85,
  [SUMMARY.carts]: 85, [SUMMARY.cancels]: 85, [SUMMARY.profit]: 104, [SUMMARY.ads]: 100, [SUMMARY.drr]: 78, [SUMMARY.drr + 1]: 73,
};
export function blockColumnWidth(offset: number): number { return BLOCK_WIDTH_BY_OFFSET[offset] ?? BLOCK_WIDTH_DEFAULT; }
export function summaryColumnWidth(col: number): number { return SUMMARY_WIDTH[col] ?? BLOCK_WIDTH_DEFAULT; }

/** Высота строки под кегль (пт): заголовок 20 пт → 40 px, итог 16 пт → 33 px. В живой книге эти строки 18 px — текст срезан. */
export function heightForFont(pt: number): number { return Math.ceil(Math.round(pt * 18) / 10) + 4; }
const HEADER_MAX_HEIGHT = 120;
const lineHeight = (pt: number): number => Math.ceil((pt * 4 / 3) * 1.25);

/** Оценка числа строк текста с переносом по словам (полужирный Calibri: ≈0,55 кегля на знак; слово длиннее строки рвётся). */
export function estimateWrappedLines(text: string, widthPx: number, pt: number): number {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return 0;
  const inner = Math.max(8, widthPx - 4), ch = (pt * 4 / 3) * 0.55;
  let lines = 1, cur = 0;
  for (const w of words) {
    const px = w.length * ch;
    if (cur > 0 && cur + ch + px <= inner) { cur += ch + px; continue; }
    if (cur > 0) lines++;
    lines += Math.ceil(px / inner) - 1;
    cur = px > inner ? px - inner * (Math.ceil(px / inner) - 1) : px;
  }
  return lines;
}

export type RowHeights = Record<'title' | 'header' | 'dayFirst' | 'day' | 'mtd' | 'plan', number>;
export interface WidthUpgrade { col: number; from: number; to: number }
export interface DimensionPlan { requests: StructureRequest[]; widthUpgrades: WidthUpgrade[]; rowHeights: RowHeights }

const fontOf = (f: RawCellFormat | undefined): number | undefined => (f?.textFormat as { fontSize?: number } | undefined)?.fontSize;

/**
 * Размеры секции. Колонки: вставленные — ширина по контракту, скрытие — как у блока-шаблона (разделитель виден);
 * существующие колонки раскладки — ТОЛЬКО расширение до контракта (поле pixelSize; скрытие не трогается; ширина колонки
 * общая для всех месяцев — расширения перечислены в widthUpgrades для журнала и отката). Строки (rows=true): заголовок и
 * MTD — не ниже высоты под свой кегль, шапка — не ниже шаблона и вмещает перенос своих подписей, дни и план — как в
 * прошлом месяце. Ни одной координаты месяца: всё из раскладки, шаблона и текстов шапки.
 */
export function planDimensions(a: {
  structure: SheetStructure; prev: MonthGeometry; next: MonthGeometry; layout: MonthLayout;
  inserted: { at: number; count: number } | null; templateSlot: number;
  templateFormats: ReadonlyMap<number, RawCellFormat[]> | null; headerTexts: ReadonlyMap<number, string>; sheetId: number; rows: boolean;
}): DimensionPlan {
  const { structure: st, layout } = a;
  const req: StructureRequest[] = [];
  const ins = a.inserted && a.inserted.count > 0 ? a.inserted : null;
  const isInserted = (col: number): boolean => !!ins && col > ins.at && col <= ins.at + ins.count;
  // Колонка листа ДО вставки (для чтения текущих размеров).
  const before = (col: number): number => (ins && col > ins.at + ins.count ? col - ins.count : col);
  const cm = (col: number): DimensionProps => st.columnMetadata[before(col) - 1] ?? {};
  const rm = (row: number): DimensionProps => st.rowMetadata[row - 1] ?? {};
  const contract = (col: number): number => (col < BLOCK_FIRST_COLUMN ? summaryColumnWidth(col) : blockColumnWidth((col - BLOCK_FIRST_COLUMN) % BLOCK_WIDTH));
  const widthAfter = new Map<number, number>();
  const widthUpgrades: WidthUpgrade[] = [];
  for (let col = 1; col <= layout.lastBlockColumn; col++) {
    const want = contract(col);
    if (isInserted(col)) {
      const off = (col - BLOCK_FIRST_COLUMN) % BLOCK_WIDTH;
      const hidden = off === BLOCK_WIDTH - 1 ? false : st.columnMetadata[slotStart(a.templateSlot) + off - 1]?.hiddenByUser === true;
      req.push({ updateDimensionProperties: { range: { sheetId: a.sheetId, dimension: 'COLUMNS', startIndex: col - 1, endIndex: col }, properties: { pixelSize: want, hiddenByUser: hidden }, fields: 'pixelSize,hiddenByUser' } });
      widthAfter.set(col, want);
      continue;
    }
    const cur = cm(col).pixelSize;
    if (cur !== undefined && cur < want) {
      widthUpgrades.push({ col, from: cur, to: want });
      req.push({ updateDimensionProperties: { range: { sheetId: a.sheetId, dimension: 'COLUMNS', startIndex: col - 1, endIndex: col }, properties: { pixelSize: want }, fields: 'pixelSize' } });
    }
    widthAfter.set(col, Math.max(cur ?? want, want));
  }
  const tpl = (row: number): RawCellFormat[] => a.templateFormats?.get(row) ?? [];
  const maxFont = (row: number): number => tpl(row).reduce((m, f) => Math.max(m, fontOf(f) ?? 0), 0) || 10;
  let header = rm(a.prev.headerRow).pixelSize ?? 21;
  for (const [col, text] of a.headerTexts) {
    const srcCol = isInserted(col) ? slotStart(a.templateSlot) + ((col - BLOCK_FIRST_COLUMN) % BLOCK_WIDTH) : before(col);
    const pt = fontOf(tpl(a.prev.headerRow)[srcCol - 1]) ?? 12;
    header = Math.max(header, estimateWrappedLines(text, widthAfter.get(col) ?? contract(col), pt) * lineHeight(pt) + 8);
  }
  const rowHeights: RowHeights = {
    title: Math.max(rm(a.prev.topRow).pixelSize ?? 0, heightForFont(maxFont(a.prev.topRow))),
    header: Math.min(HEADER_MAX_HEIGHT, header),
    dayFirst: rm(a.prev.firstDailyRow).pixelSize ?? 21,
    day: rm(a.prev.lastDailyRow).pixelSize ?? 21,
    mtd: Math.max(rm(a.prev.mtdRow).pixelSize ?? 0, heightForFont(maxFont(a.prev.mtdRow))),
    plan: rm(a.prev.spacerRow).pixelSize ?? 21,
  };
  if (a.rows) {
    const g = a.next;
    const bands: Array<[keyof RowHeights, number, number]> = [
      ['title', g.topRow, g.topRow], ['header', g.headerRow, g.headerRow], ['dayFirst', g.firstDailyRow, g.firstDailyRow],
      ['day', g.firstDailyRow + 1, g.lastDailyRow], ['mtd', g.mtdRow, g.mtdRow], ['plan', g.spacerRow, g.spacerRow],
    ];
    for (const [k, r1, r2] of bands) {
      req.push({ updateDimensionProperties: { range: { sheetId: a.sheetId, dimension: 'ROWS', startIndex: r1 - 1, endIndex: r2 }, properties: { pixelSize: rowHeights[k], hiddenByUser: false }, fields: 'pixelSize,hiddenByUser' } });
    }
  }
  return { requests: req, widthUpgrades, rowHeights };
}

/* ───────────────────────── группы колонок у конца цепочки ───────────────────────── */

/**
 * Sheets сливает стоящие вплотную группы одной глубины в одну. У последнего блока месяца нет колонки-разделителя, его
 * группа аналитики (смещения 16..22) кончается последней метрикой — а сразу за ней начинается хвост книги, чья группа
 * скрытых расчётов начинается с первой же колонки хвоста. Чтобы «+/−» последнего SKU не сворачивал заодно хвост (и
 * чтобы следующая вставка колонок не попала внутрь слитой группы), первая колонка хвоста выводится из его группы: она
 * остаётся скрытой (hiddenByUser), но между группами появляется несгруппированная колонка. tailStartBefore — первая
 * колонка хвоста ДО вставки (1-based). Хвост уже отделён или группы нет — null.
 */
export function tailGroupDetach(groups: readonly { startIndex: number; endIndex: number }[], tailStartBefore: number, insertCount: number, sheetId: number): { column: number; request: StructureRequest } | null {
  if (insertCount <= 0 || !groups.some((g) => g.startIndex === tailStartBefore - 1 && g.endIndex > tailStartBefore)) return null;
  const idx = tailStartBefore - 1 + insertCount;
  return { column: idx + 1, request: { deleteDimensionGroup: { range: { sheetId, dimension: 'COLUMNS', startIndex: idx, endIndex: idx + 1 } } } };
}

/**
 * insertDimension(inheritFromBefore) РАСШИРЯЕТ группу колонок, которая кончается ровно на колонке перед вставкой (в книге
 * после Calendar V2 это группа аналитики последнего блока: у него нет разделителя, и новый блок вставляется сразу за ней).
 * Вставленные колонки выводятся из такой группы сразу — по одному запросу на каждый уровень глубины; затем новые блоки
 * получают собственные группы. at — 0-based индекс вставки. Проверено на копии книги: без этого группа блока 25
 * накрывала блок 26 целиком, а его группа становилась вложенной.
 */
export function groupInsertTrims(groups: readonly { startIndex: number; endIndex: number }[], at: number, count: number, sheetId: number): StructureRequest[] {
  if (count <= 0) return [];
  return groups.filter((g) => g.endIndex === at && g.startIndex < at)
    .map(() => ({ deleteDimensionGroup: { range: { sheetId, dimension: 'COLUMNS', startIndex: at, endIndex: at + count } } }));
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
  /** Ширины существующих колонок ДО подготовки месяца (из журнала плана: width_upgrades) — возвращаются на место. */
  restoreWidths?: readonly WidthUpgrade[];
  /** Первая колонка хвоста ПОСЛЕ удаления вставленных колонок (1-based), выведенная подготовкой из группы хвоста: вернуть. */
  regroupTailColumn?: number | null;
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
  // Расширенные колонки лежат левее вставки — их индексы удаление не меняет.
  for (const u of a.restoreWidths ?? []) req.push({ updateDimensionProperties: { range: { sheetId: a.sheetId, dimension: 'COLUMNS', startIndex: u.col - 1, endIndex: u.col }, properties: { pixelSize: u.from }, fields: 'pixelSize' } });
  // Возврат первой колонки хвоста в его группу: соседние группы одной глубины Sheets сольёт в исходную.
  if (a.regroupTailColumn) req.push({ addDimensionGroup: { range: { sheetId: a.sheetId, dimension: 'COLUMNS', startIndex: a.regroupTailColumn - 1, endIndex: a.regroupTailColumn } } });
  return { requests: req, deletedCfRules: idx.length, deletedRows: [g.topRow, a.rowCount], deletedColumns: cols, refused: null };
}

/** Колонки, которые не принадлежат ни одному слоту (A..L — сводка). */
export const SUMMARY_LAST_COLUMN = BLOCK_FIRST_COLUMN - 1;
