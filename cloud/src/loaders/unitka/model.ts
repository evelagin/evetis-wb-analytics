/**
 * UNITKA ENGINE — модель листа `WB_Юнит_2025`: смещения метрик в блоке, якоря книги, форматы.
 *
 * Геометрия МЕСЯЦА (строки, число дней, блоки) — в calendar.ts (MonthLayout, Phase 2B). Здесь нет
 * ни одной сентябрьской строки: сентябрь 2026 — лишь одна из секций (заголовок A735, 30 дней).
 * Контракт блока 1-в-1 с Apps Script (`S8_M`, `S8_ORDER` в apps-script/unitka/UnitkaS8.gs).
 *
 * Чистый модуль без I/O: всё, что здесь, тестируется без Sheets и BigQuery.
 */
import { BLOCK_WIDTH, isReservedSlot, slotStart, type BlockSlot } from './calendar.js';

/**
 * ЯКОРЯ КНИГИ — фиксированные ячейки, НЕ зависящие от месяца (решение владельца 2, Phase 2B).
 * Они физически стоят в строках сентябрьской секции (WB736..WB739), но принадлежат книге целиком.
 * Ячейка определяется как якорь ТОЛЬКО по паре (строка, колонка) или по виду ячейки плана,
 * НИКОГДА по одной колонке: с октября колонка WB (600) внутри строк месяца — обычная ячейка блока 25.
 */
export const BOOK_ANCHORS = {
  COL: 600,             // WB
  LCD_MIRROR_ROW: 736,  // WB736 — зеркало LAST_CLOSED_DATE
  REVERSE_ROW: 737,     // WB737 — REVERSE_LEG_RATE (именованный диапазон)
  STATUS_ROW: 738,      // WB738 — integrity_status (зарезервировано, не пишется)
  INVALID_ROW: 739,     // WB739 — число финансово недостоверных SKU-дней (зарезервировано)
} as const;

/** true только для самих ячеек-якорей (строка И колонка). */
export function isBookAnchorCell(row: number, col: number): boolean {
  return col === BOOK_ANCHORS.COL && row >= BOOK_ANCHORS.LCD_MIRROR_ROW && row <= BOOK_ANCHORS.INVALID_ROW;
}

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

/** Сводка ↔ смещение в блоке: сумма всех блоков секции должна равняться колонке сводки. */
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
 * и заканчивался на строке последнего закрытого дня в момент вёрстки (сентябрь: 745 = 09.09). Никто —
 * ни Apps Script при s82lcd, ни Engine при LCD_ADVANCE — эту границу не двигал.
 * Контракт: для каждой колонки, которую пишет Engine (9 факт + logistics + commission),
 * статический формат ячейки ЗАКРЫТОГО дня (дата ≤ LCD) = формат эталонной строки (первый день
 * секции месяца) того же блока и той же колонки (первый день месяца — всегда закрыт). Сравниваются и
 * восстанавливаются ровно три свойства: заливка, цвет шрифта, числовой формат.
 * Будущие дни Engine не трогает — они сохраняют «будущий» статический вид до закрытия.
 * Условное форматирование (УФ) не затрагивается вовсе.
 */
export const FORMAT_CONTRACT_KEYS = [...FACT_KEYS, 'logistics', 'commission'] as const;
export type FormatKey = (typeof FORMAT_CONTRACT_KEYS)[number];

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
 * ни УФ, ни проверки данных, ни именованные диапазоны. Engine в эти ячейки НЕ пишет.
 *   WB738 — integrity_status прогона; WB739 — число финансово недостоверных SKU-дней.
 * Phase 2B: фиксированные якоря книги, а не «HDR + 2» — не переезжают с месяцем.
 */
export const INTEGRITY_STATUS_CELLS = {
  status: { row: BOOK_ANCHORS.STATUS_ROW, col: BOOK_ANCHORS.COL },
  invalidRows: { row: BOOK_ANCHORS.INVALID_ROW, col: BOOK_ANCHORS.COL },
} as const;

/** Именованные диапазоны книги. */
export const NAMED = { LCD: 'LAST_CLOSED_DATE', REVERSE: 'REVERSE_LEG_RATE' } as const;

/** Значение ячейки как отдаёт Sheets API (UNFORMATTED_VALUE + SERIAL_NUMBER). */
export type CellValue = string | number | boolean | null;

/** Блок SKU: index — порядковый номер, slot — позиция в сетке (слот 24 зарезервирован). */
export type Block = BlockSlot;

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


/**
 * Блоки по строке заголовков секции: nmID — первое 6–12-значное число в первой непустой ячейке
 * слота (та же эвристика, что в s82data/s8brates Apps Script). Слоты перебираются, пока слот
 * помещается в прочитанную ширину; зарезервированные слоты пропускаются, пустые — тоже
 * (выбывший SKU оставляет пустой слот, новые блоки не сдвигают старые).
 */
export function findBlocks(topRow: readonly CellValue[], width: number = topRow.length): Block[] {
  const out: Block[] = [];
  for (let slot = 0; slotStart(slot) <= width; slot++) {
    if (isReservedSlot(slot)) continue;
    const st = slotStart(slot);
    let title = '';
    for (let c = 0; c < BLOCK_WIDTH && !title; c++) title = String(topRow[st - 1 + c] ?? '').trim();
    const m = title.match(/\d{6,12}/);
    if (!m) continue;
    out.push({ index: out.length, slot, start: st, nmId: Number(m[0]), title });
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
