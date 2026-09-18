/**
 * UNITKA ENGINE v1 — геометрия September Master (лист `WB_Юнит_2025`, строки 735–767).
 *
 * Контракт 1-в-1 с Apps Script (`S8`, `S8_M`, `S8_ORDER` в apps-script/unitka/UnitkaS8.gs):
 * Master — immutable reference implementation, Engine его ОБСЛУЖИВАЕТ. Любое отклонение
 * листа от этой геометрии — STRUCTURE_DRIFT, а не повод «подстроиться».
 *
 * Чистый модуль без I/O: всё, что здесь, тестируется без Sheets и BigQuery.
 */

/** Строки и колонки Master. Колонки 1-based (A = 1), как в Apps Script. */
export const GRID = {
  TOP: 735,   // заголовки блоков (nmID в тексте)
  HDR: 736,   // шапка метрик; в колонке MIR — зеркало LAST_CLOSED_DATE
  FIRST: 737, // первый день месяца
  DAYS: 30,   // строк-дней в блоке (сентябрь)
  MTD: 767,   // строка MTD (формулы)
  B0: 13,     // колонка M — первая колонка блока #1
  BW: 24,     // ширина блока
  NB: 24,     // блоков (SKU)
  NC: 589,    // ширина читаемой сетки (A..VQ)
  MIR: 600,   // колонка WB: зеркала LAST_CLOSED_DATE (736) и REVERSE_LEG_RATE (737)
  RROW: 737,  // строка зеркала REVERSE_LEG_RATE
} as const;

/** Смещения метрик внутри блока (0 = дата). Карта колонок UNITKA 2.0 PHASE 3. */
export const OFFSET = {
  date: 0, bloggers: 1, views: 2, opens: 3, orders: 4, carts: 5, cancels: 6, stock: 7,
  turnover: 8, profit1: 9, profitAll: 10, adsIn: 11, adsOut: 12, drr: 13, price: 14,
  spp: 15, priceSpp: 16, commission: 17, priceMinusComm: 18, logistics: 19, storage: 20,
  tax: 21, unitProfit: 22, weekday: 23,
} as const;

/** Девять факт-колонок, которые пишет Engine (= S8_ORDER в Apps Script). */
export const FACT_KEYS = ['views', 'opens', 'carts', 'orders', 'cancels', 'stock', 'adsIn', 'price', 'storage'] as const;
export type FactKey = (typeof FACT_KEYS)[number];

/** Колонки формул внутри блока — Engine их НИКОГДА не пишет; preflight требует там формулы. */
export const CALC_OFFSETS: readonly number[] = [
  OFFSET.turnover, OFFSET.profit1, OFFSET.profitAll, OFFSET.adsOut, OFFSET.drr,
  OFFSET.priceSpp, OFFSET.priceMinusComm, OFFSET.tax, OFFSET.unitProfit,
];

/** Смещения, которым РАЗРЕШЕНО быть заполненными в будущих днях (как в s8qa Apps Script). */
export const FUTURE_ALLOWED_OFFSETS: ReadonlySet<number> = new Set([
  OFFSET.date, OFFSET.weekday, OFFSET.commission, OFFSET.logistics, OFFSET.spp,
]);

/** Левая сводка магазина (колонки A..K). */
export const SUMMARY = {
  weekday: 1, date: 2, bloggers: 3, views: 4, opens: 5, orders: 6, carts: 7, cancels: 8, profit: 9, ads: 10, drr: 11,
} as const;

/** Сводка ↔ смещение в блоке: сумма 24 блоков должна равняться колонке сводки. */
export const SUMMARY_TO_OFFSET: ReadonlyArray<readonly [number, number, string]> = [
  [SUMMARY.bloggers, OFFSET.bloggers, 'bloggers'],
  [SUMMARY.views, OFFSET.views, 'views'],
  [SUMMARY.opens, OFFSET.opens, 'opens'],
  [SUMMARY.orders, OFFSET.orders, 'orders'],
  [SUMMARY.carts, OFFSET.carts, 'carts'],
  [SUMMARY.cancels, OFFSET.cancels, 'cancels'],
  [SUMMARY.profit, OFFSET.profitAll, 'profit'],
  [SUMMARY.ads, OFFSET.adsIn, 'ads'],
];

/**
 * КАНОНИЧЕСКИЙ КОНТРАКТ ФОРМАТА ЗАКРЫТОГО ДНЯ (E3, post-write аудит 12.09.2026).
 * В Master «будущий» вид колонок ставок (серый шрифт) и хранения (без заливки) был СТАТИЧЕСКИМ
 * и заканчивался на строке последнего закрытого дня в момент вёрстки (745 = 09.09). Никто —
 * ни Apps Script при s82lcd, ни Engine при LCD_ADVANCE — эту границу не двигал.
 * Контракт: для каждой колонки, которую пишет Engine (9 факт + logistics + commission),
 * статический формат ячейки ЗАКРЫТОГО дня (дата ≤ LCD) = формат эталонной строки 737
 * того же блока и той же колонки (первый день месяца — всегда закрыт). Сравниваются и
 * восстанавливаются ровно три свойства: заливка, цвет шрифта, числовой формат.
 * Будущие дни Engine не трогает — они сохраняют «будущий» статический вид до закрытия.
 * Условное форматирование (УФ) не затрагивается вовсе.
 */
export const FORMAT_CONTRACT_KEYS = [...FACT_KEYS, 'logistics', 'commission'] as const;
export type FormatKey = (typeof FORMAT_CONTRACT_KEYS)[number];
/** Строка-эталон формата закрытого дня — первый день месяца. */
export const FORMAT_REF_ROW = GRID.FIRST;

export interface RgbColor { red?: number; green?: number; blue?: number }
export interface NumberFormat { type?: string; pattern?: string }
/** Нормализованный статический формат ячейки: отсутствие = null. */
export interface CellFormat {
  bg: RgbColor | null;
  fg: RgbColor | null;
  numberFormat: NumberFormat | null;
}

function colorEqual(a: RgbColor | null, b: RgbColor | null): boolean {
  if (a === null || b === null) return a === b;
  const k = ['red', 'green', 'blue'] as const;
  return k.every((c) => Math.abs((a[c] ?? 0) - (b[c] ?? 0)) < 1 / 255 / 2);
}
export function formatEqual(a: CellFormat, b: CellFormat): boolean {
  const nf = (x: NumberFormat | null): string => (x === null ? '' : `${x.type ?? ''}|${x.pattern ?? ''}`);
  return colorEqual(a.bg, b.bg) && colorEqual(a.fg, b.fg) && nf(a.numberFormat) === nf(b.numberFormat);
}
export const EMPTY_FORMAT: CellFormat = { bg: null, fg: null, numberFormat: null };
export function formatDescr(f: CellFormat): string {
  const c = (x: RgbColor | null): string => (x === null ? '-' : '#' + ['red', 'green', 'blue'].map((k) => Math.round(((x as Record<string, number | undefined>)[k] ?? 0) * 255).toString(16).padStart(2, '0')).join(''));
  return `bg ${c(f.bg)} fg ${c(f.fg)} nf ${f.numberFormat === null ? '-' : (f.numberFormat.pattern ?? f.numberFormat.type ?? '')}`;
}

/**
 * Integrity Guard V1 — ЗАРЕЗЕРВИРОВАННЫЕ ячейки статуса (решение владельца Q2, Phase 1C1).
 * Проверено 18.09.2026 на live-экспорте книги: WB738 и WB739 пусты, на них не ссылаются ни формулы,
 * ни УФ, ни проверки данных, ни именованные диапазоны; в коде используются только WB736/WB737.
 * Phase 1C1: ТОЛЬКО константы. Engine в эти ячейки НЕ пишет (запись — отдельный гейт).
 *   WB738 — integrity_status прогона; WB739 — число финансово недостоверных SKU-дней.
 * Строки заданы относительно HDR, а не литералом сентября.
 */
export const INTEGRITY_STATUS_CELLS = {
  status: { row: GRID.HDR + 2, col: GRID.MIR },
  invalidRows: { row: GRID.HDR + 3, col: GRID.MIR },
} as const;

/** Именованные диапазоны книги. */
export const NAMED = { LCD: 'LAST_CLOSED_DATE', REVERSE: 'REVERSE_LEG_RATE' } as const;

/** Значение ячейки как отдаёт Sheets API (UNFORMATTED_VALUE + SERIAL_NUMBER). */
export type CellValue = string | number | boolean | null;

export interface Block {
  index: number;   // 0..23
  start: number;   // 1-based колонка даты блока
  nmId: number;
  title: string;
}

/** Номер колонки (1-based) → A1-буквы. */
export function colA1(col: number): string {
  let n = col;
  let s = '';
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/** Серийная дата Sheets (эпоха 1899-12-30) ↔ ISO YYYY-MM-DD. */
const SERIAL_EPOCH_MS = Date.UTC(1899, 11, 30);
export function serialToIso(serial: number): string {
  return new Date(SERIAL_EPOCH_MS + Math.round(serial) * 86_400_000).toISOString().slice(0, 10);
}
export function isoToSerial(iso: string): number {
  return Math.round((Date.parse(`${iso}T00:00:00Z`) - SERIAL_EPOCH_MS) / 86_400_000);
}
export function addDaysIso(iso: string, days: number): string {
  return new Date(Date.parse(`${iso}T00:00:00Z`) + days * 86_400_000).toISOString().slice(0, 10);
}
export function monthStartIso(iso: string): string {
  return `${iso.slice(0, 7)}-01`;
}

/** Строка листа для i-го дня месяца (0-based). */
export function dayRow(dayIndex: number): number {
  return GRID.FIRST + dayIndex;
}

export function blockStart(index: number): number {
  return GRID.B0 + index * GRID.BW;
}

/**
 * Блоки по строке 735: nmID — первое 6–12-значное число в первой непустой ячейке блока
 * (та же эвристика, что в s82data/s8brates Apps Script).
 */
export function findBlocks(topRow: readonly CellValue[]): Block[] {
  const out: Block[] = [];
  for (let b = 0; b < GRID.NB; b++) {
    const st = blockStart(b);
    let title = '';
    for (let c = 0; c < GRID.BW && !title; c++) title = String(topRow[st - 1 + c] ?? '').trim();
    const m = title.match(/\d{6,12}/);
    if (!m) continue;
    out.push({ index: b, start: st, nmId: Number(m[0]), title });
  }
  return out;
}

/** Пустая ячейка: null, undefined или ''. */
export function isEmpty(v: CellValue | undefined): boolean {
  return v === null || v === undefined || v === '';
}

/** Число из ячейки; NaN, если не число. */
export function asNumber(v: CellValue | undefined): number {
  if (typeof v === 'number') return v;
  if (typeof v === 'string' && v.trim() !== '') return Number(v.replace(',', '.'));
  return NaN;
}

/**
 * Сравнение факта: пусто ≠ 0; числа — с допуском 0.005 (как QA в s82data).
 * `want === null` означает «ячейка должна быть пустой» (GAP источника).
 */
export function factEqual(got: CellValue | undefined, want: number | null): boolean {
  if (want === null) return isEmpty(got);
  if (isEmpty(got)) return false;
  const g = asNumber(got);
  return Number.isFinite(g) && Math.abs(g - want) < 0.005;
}

/** Сравнение ставки: допуск 1e-9 (как в s8brates/s8rates). */
export function rateEqual(got: CellValue | undefined, want: number): boolean {
  const g = asNumber(got);
  return Number.isFinite(g) && Math.abs(g - want) <= 1e-9;
}

const ERROR_RE = /^#(REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|ERROR!|NULL!)/;
export function isFormulaError(v: CellValue | undefined): boolean {
  return typeof v === 'string' && ERROR_RE.test(v);
}

export function round2(x: number): number {
  return Math.round(x * 100) / 100;
}
export function round6(x: number): number {
  return Math.round(x * 1e6) / 1e6;
}
