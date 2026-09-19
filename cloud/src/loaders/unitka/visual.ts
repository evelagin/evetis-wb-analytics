/**
 * UNITKA CALENDAR V2 — ВИЗУАЛЬНЫЙ КОНТРАКТ секции месяца (Phase 2C hardening). Чистый модуль.
 *
 * Источник контракта — живой лист (Sheets API, тестовая копия 19.09.2026), а не догадки по XLSX:
 *   • семантика колонок блока (факт / ручной ввод / ставка / расчёт / дата / день недели);
 *   • статические заливки: ручной ввод — всегда (зона ввода); факт/ставка — ТОЛЬКО у закрытого дня со
 *     значением (правило УФ), пустой будущий день — без заливки, читается как пустой;
 *   • выходные: суббота и воскресенье по дате строки (колонка B сводки) — заливка #fcefe3 на ячейке даты
 *     и ячейке дня недели сводки и каждого блока (не вся строка). В живом сентябре это работало только
 *     у сводки A/B и блока 1: формулы `=WEEKDAY(US737;2)>5` записаны относительно первого диапазона
 *     правила (B / AJ), для остальных блоков ссылка уезжает в чужие колонки (пусто → WEEKDAY(0)=6 →
 *     «выходной» каждый день). Здесь формулы не зависят от якоря;
 *   • будущие дни — серый текст (первое совпавшее правило УФ побеждает: Sheets применяет ОДНО правило
 *     на ячейку, поэтому порядок правил = приоритет, как в живом сентябре);
 *   • пороги, уровни и шкалы метрик — те же цвета и границы, что в живом сентябре.
 *
 * Правило независимости от якоря: формулы УФ используют только абсолютные колонки ($B, $WB$736),
 * строку относительно первого дня секции (у всех диапазонов правила одинаковые строки) и идиому
 * «эта ячейка» INDEX($A<r>:$<last><r>;1;COLUMN()) — проверено на тестовой копии (COLUMN() и INDEX
 * вычисляются по каждой ячейке). Ни одной относительной ссылки на колонку.
 */
import { BOOK_ANCHORS, OFFSET, SUMMARY, colA1 } from './model.js';
import { BLOCK_WIDTH, type MonthLayout } from './calendar.js';
import type { ConditionalFormatRule, RawCellFormat, StructureRequest } from './sheets.js';
import { toLocaleFormula, type FormulaStyle } from './formulas.js';

/* ───────────────────────── семантика колонок ───────────────────────── */

export type ColumnKind = 'date' | 'weekday' | 'manual' | 'fact' | 'rate' | 'calc';

/** Смещение в блоке → вид колонки (карта UNITKA 2.0). */
export const BLOCK_KIND: readonly ColumnKind[] = (() => {
  const k = Array<ColumnKind>(BLOCK_WIDTH).fill('calc');
  k[OFFSET.date] = 'date'; k[OFFSET.weekday] = 'weekday';
  k[OFFSET.bloggers] = 'manual'; k[OFFSET.spp] = 'manual';
  for (const o of [OFFSET.views, OFFSET.opens, OFFSET.orders, OFFSET.carts, OFFSET.cancels, OFFSET.stock, OFFSET.adsIn, OFFSET.price, OFFSET.storage]) k[o] = 'fact';
  k[OFFSET.commission] = 'rate'; k[OFFSET.logistics] = 'rate';
  return k;
})();

/** Колонки сводки A..L → вид. C (Σ блогеров) окрашена как ручной ввод — как в живом листе. */
export const SUMMARY_KIND: Readonly<Record<number, ColumnKind>> = {
  [SUMMARY.weekday]: 'weekday', [SUMMARY.date]: 'date', [SUMMARY.bloggers]: 'manual',
  [SUMMARY.views]: 'fact', [SUMMARY.opens]: 'fact', [SUMMARY.orders]: 'fact', [SUMMARY.carts]: 'fact', [SUMMARY.cancels]: 'fact',
  [SUMMARY.profit]: 'calc', [SUMMARY.ads]: 'fact', [SUMMARY.drr]: 'calc', [SUMMARY.drr + 1]: 'weekday',
};

export function columnKind(col: number, layout: MonthLayout): ColumnKind | null {
  if (col <= SUMMARY.drr + 1) return SUMMARY_KIND[col] ?? null;
  const b = layout.blocks.find((x) => col >= x.start && col < x.start + BLOCK_WIDTH);
  return b ? BLOCK_KIND[col - b.start]! : null;
}

/* ───────────────────────── цвета (живой сентябрь 2026) ───────────────────────── */

export const COLOR = {
  weekendBg: '#fcefe3', futureFg: '#c0c0c0',
  factBg: '#f1f8f4', manualBg: '#fffbe8', rateBg: '#f3f8fd',
  tierCarts: ['#d6e4ea', '#dee9ee', '#e6eff2', '#eef4f6'], tierAds: ['#e8cf99', '#efdcb4', '#f5e8cd', '#faf2e3'], tierOrders: ['#a9d4b8', '#bfdecb', '#d2e8da', '#e4f1e9'],
  gradMin: '#f8696b', gradMid: '#ffeb84', gradMax: '#63be7b',
  turnover: [['#fce8e6', '#a61c00'], ['#fff2cc', '#7f6000'], ['#e6f4ea', '#274e13'], ['#efefef', '#434343']],
  negFg: '#cc0000', posFg: '#38761d', white: '#ffffff', cancelsBg: '#fce8e6', cancelsFg: '#a61c00', zeroOrdersBg: '#fce8b2',
} as const;

export function rgb(hex: string): { red: number; green: number; blue: number } {
  const n = parseInt(hex.slice(1), 16);
  return { red: ((n >> 16) & 255) / 255, green: ((n >> 8) & 255) / 255, blue: (n & 255) / 255 };
}
export function hexOf(c: { red?: number; green?: number; blue?: number } | undefined): string | null {
  if (!c) return null;
  return '#' + ['red', 'green', 'blue'].map((k) => Math.round(((c as Record<string, number | undefined>)[k] ?? 0) * 255).toString(16).padStart(2, '0')).join('');
}

/* ───────────────────────── статический формат строки дня ───────────────────────── */

/**
 * Нейтральный будущий вид: у факта, ставки и расчёта нет заливки (закрытый день со значением
 * получит её правилом УФ); у ставки нет статического цвета текста (будущее — серым по УФ, закрытое —
 * обычным). Ручной ввод, дата и день недели — как в шаблоне. Прочие свойства (числовой формат,
 * выравнивание, границы, шрифт) — из шаблона строки прошлого месяца.
 */
export function neutralizeDayFormat(raw: RawCellFormat, kind: ColumnKind | null): RawCellFormat {
  if (!raw || kind === null || kind === 'manual' || kind === 'date' || kind === 'weekday') return raw;
  const f: Record<string, unknown> = { ...raw };
  delete f.backgroundColor; delete f.backgroundColorStyle;
  if (kind === 'rate' && typeof f.textFormat === 'object' && f.textFormat) {
    const tf = { ...(f.textFormat as Record<string, unknown>) };
    delete tf.foregroundColor; delete tf.foregroundColorStyle;
    f.textFormat = tf;
  }
  return f;
}

/* ───────────────────────── предикаты правил (семантика, тестируется офлайн) ───────────────────────── */

export type CellV = number | '' | null;
const num = (v: CellV): v is number => typeof v === 'number';

/** Уровень «доля от максимума колонки»: 0 = ≥75 %, 1 = ≥50 %, 2 = ≥25 %, 3 = >0; null — пусто/0. */
export function tierOf(v: CellV, colMax: number): 0 | 1 | 2 | 3 | null {
  if (!num(v) || !(v > 0)) return null;
  if (v > 0.75 * colMax) return 0;
  if (v > 0.5 * colMax) return 1;
  if (v > 0.25 * colMax) return 2;
  return 3;
}
/** Оборачиваемость: 0 = ≤15 (красный), 1 = 15–30, 2 = 30–60, 3 = >60; null — не число. */
export function turnoverBand(v: CellV): 0 | 1 | 2 | 3 | null {
  if (!num(v)) return null;
  if (v <= 15) return 0;
  if (v <= 30) return 1;
  if (v <= 60) return 2;
  return 3;
}
export function isWeekend(iso: string): boolean {
  const d = new Date(`${iso}T00:00:00Z`).getUTCDay(); // 0 = вс
  return d === 0 || d === 6;
}
/** Отмены тревожны: закрытый день, заказы > 0 и (отмен ≥ 3 или отмен ≥ 2 и ≥ 30 % заказов). */
export function cancelsAlarm(closed: boolean, orders: CellV, cancels: CellV): boolean {
  return closed && num(orders) && orders > 0 && num(cancels) && (cancels >= 3 || (cancels >= 2 && cancels * 10 >= orders * 3));
}
export function zeroOrdersAlarm(closed: boolean, views: CellV, orders: CellV): boolean {
  return closed && num(views) && views > 0 && num(orders) && orders === 0;
}
/** Заливка закрытого дня: день закрыт и в ячейке есть значение (пропуск источника остаётся белым). */
export function closedFill(closed: boolean, v: CellV): boolean {
  return closed && v !== '' && v !== null;
}

/* ───────────────────────── построение правил УФ ───────────────────────── */

interface Rng { c1: number; c2: number }
type Cond = { formula: string } | { gradient: true };

function ruleJson(sheetId: number, layout: MonthLayout, ranges: Rng[], cond: Cond, format: Record<string, unknown> | null, style: FormulaStyle): ConditionalFormatRule {
  const gr = ranges.map((r) => ({ sheetId, startRowIndex: layout.firstDailyRow - 1, endRowIndex: layout.lastDailyRow, startColumnIndex: r.c1 - 1, endColumnIndex: r.c2 }));
  if ('gradient' in cond) {
    return { ranges: gr, gradientRule: { minpoint: { color: rgb(COLOR.gradMin), type: 'MIN' }, midpoint: { color: rgb(COLOR.gradMid), type: 'PERCENTILE', value: '50' }, maxpoint: { color: rgb(COLOR.gradMax), type: 'MAX' } } };
  }
  return { ranges: gr, booleanRule: { condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: toLocaleFormula(cond.formula, style) }] }, format: format ?? {} } };
}
const bg = (hex: string): Record<string, unknown> => ({ backgroundColor: rgb(hex) });
const fg = (hex: string): Record<string, unknown> => ({ textFormat: { foregroundColor: rgb(hex) } });
const bgfg = (b: string, f: string): Record<string, unknown> => ({ backgroundColor: rgb(b), textFormat: { foregroundColor: rgb(f) } });

/** Идиомы, не зависящие от якоря правила. */
export function cfIdioms(layout: MonthLayout): { SELF: string; NEIGH: (k: number) => string; COLMAX: string; CLOSED: string; FUTURE: string; WEEKEND: string } {
  const f = layout.firstDailyRow, l = layout.lastDailyRow, last = colA1(layout.lastBlockColumn);
  const row = `$A${f}:$${last}${f}`;
  const anchor = `$${colA1(BOOK_ANCHORS.COL)}$${BOOK_ANCHORS.LCD_MIRROR_ROW}`;
  return {
    SELF: `INDEX(${row},1,COLUMN())`,
    NEIGH: (k) => `INDEX(${row},1,COLUMN()-${k})`,
    COLMAX: `MAX(INDEX($A$${f}:$${last}$${l},0,COLUMN()))`,
    CLOSED: `$B${f}<=${anchor}`,
    FUTURE: `$B${f}>${anchor}`,
    WEEKEND: `WEEKDAY($B${f},2)>5`,
  };
}

export interface VisualCf { rules: ConditionalFormatRule[]; families: Record<string, number> }

/**
 * Все правила УФ новой секции, в порядке приоритета (первое совпавшее побеждает) — порядок семей как в
 * живом сентябре; новые семьи «заливка закрытого дня» стоят после всех метрических правил.
 */
export function buildConditionalFormats(layout: MonthLayout, sheetId: number, style: FormulaStyle): VisualCf {
  const I = cfIdioms(layout);
  const S = I.SELF;
  const rules: ConditionalFormatRule[] = [];
  const families: Record<string, number> = {};
  const add = (family: string, ranges: Rng[], cond: Cond, format: Record<string, unknown> | null): void => {
    if (!ranges.length) return;
    rules.push(ruleJson(sheetId, layout, ranges, cond, format, style));
    families[family] = (families[family] ?? 0) + 1;
  };
  const one = (c: number): Rng => ({ c1: c, c2: c });
  const off = (o: number): Rng[] => layout.blocks.map((b) => one(b.start + o));
  const offRun = (o1: number, o2: number): Rng[] => layout.blocks.map((b) => ({ c1: b.start + o1, c2: b.start + o2 }));
  const tiers = (family: string, sumCol: number, o: number, colors: readonly string[]): void => {
    const cut = ['0.75', '0.5', '0.25', null];
    cut.forEach((c, i) => add(family, [one(sumCol), ...off(o)], { formula: c ? `=AND(${S}<>"",${S}>${c}*${I.COLMAX})` : `=AND(${S}<>"",${S}>0)` }, bg(colors[i]!)));
  };
  // 1–3. Уровни доли от максимума колонки: корзина, внутренняя реклама, заказы.
  tiers('tier_carts', SUMMARY.carts, OFFSET.carts, COLOR.tierCarts);
  tiers('tier_ads', SUMMARY.ads, OFFSET.adsIn, COLOR.tierAds);
  tiers('tier_orders', SUMMARY.orders, OFFSET.orders, COLOR.tierOrders);
  // 4. Выходные: дата и день недели сводки и каждого блока.
  add('weekend', [one(SUMMARY.weekday), one(SUMMARY.date), ...off(OFFSET.date), ...off(OFFSET.weekday)], { formula: `=${I.WEEKEND}` }, bg(COLOR.weekendBg));
  // 5. Будущий день — серый текст: сводка A..K и весь блок.
  add('future', [{ c1: SUMMARY.weekday, c2: SUMMARY.drr }, ...offRun(0, BLOCK_WIDTH - 1)], { formula: `=${I.FUTURE}` }, fg(COLOR.futureFg));
  // 6. Шкала «Доходность (общая)»: сводка I и каждый блок отдельно (шкала считается по своему диапазону).
  add('grad_profit', [one(SUMMARY.profit)], { gradient: true }, null);
  for (const r of off(OFFSET.profitAll)) add('grad_profit', [r], { gradient: true }, null);
  // 7. Оборачиваемость (дни).
  const T = [`=AND(ISNUMBER(${S}),${S}<=15)`, `=AND(ISNUMBER(${S}),${S}>15,${S}<=30)`, `=AND(ISNUMBER(${S}),${S}>30,${S}<=60)`, `=AND(ISNUMBER(${S}),${S}>60)`];
  T.forEach((f, i) => add('turnover', off(OFFSET.turnover), { formula: f }, bgfg(COLOR.turnover[i]![0]!, COLOR.turnover[i]![1]!)));
  // 8. Доходность на 1 шт: знак цветом текста; 0 и не-число — белый фон (перекрывают шкалу ниже).
  add('profit1', off(OFFSET.profit1), { formula: `=AND(ISNUMBER(${S}),${S}<0)` }, bgfg(COLOR.white, COLOR.negFg));
  add('profit1', off(OFFSET.profit1), { formula: `=AND(ISNUMBER(${S}),${S}>0)` }, bgfg(COLOR.white, COLOR.posFg));
  add('profit1', off(OFFSET.profit1), { formula: `=AND(ISNUMBER(${S}),${S}=0)` }, bg(COLOR.white));
  add('profit1', off(OFFSET.profit1), { formula: `=NOT(ISNUMBER(${S}))` }, bg(COLOR.white));
  // 9–10. Доходность общая / на единицу: знак.
  add('profit_sign', [one(SUMMARY.profit), ...off(OFFSET.profitAll), ...off(OFFSET.unitProfit)], { formula: `=AND(ISNUMBER(${S}),${S}>0)` }, fg(COLOR.posFg));
  add('profit_sign', off(OFFSET.unitProfit), { formula: `=AND(ISNUMBER(${S}),${S}<0)` }, fg(COLOR.negFg));
  // 11. Отмены тревожны (заказы — на 2 колонки левее и в сводке, и в блоке).
  add('cancels', [one(SUMMARY.cancels), ...off(OFFSET.cancels)], { formula: `=AND(${I.CLOSED},ISNUMBER(${I.NEIGH(2)}),${I.NEIGH(2)}>0,ISNUMBER(${S}),OR(${S}>=3,AND(${S}>=2,${S}*10>=${I.NEIGH(2)}*3)))` }, bgfg(COLOR.cancelsBg, COLOR.cancelsFg));
  // 12–13. Шкалы: корзина (сводка G и блоки), доходность на 1 шт (блоки).
  add('grad_carts', [one(SUMMARY.carts)], { gradient: true }, null);
  for (const r of off(OFFSET.carts)) add('grad_carts', [r], { gradient: true }, null);
  for (const r of off(OFFSET.profit1)) add('grad_profit1', [r], { gradient: true }, null);
  // 14–16. Отрицательная доходность, ДРР > 20 %, «показы есть — заказов нет».
  add('profit_sign', [one(SUMMARY.profit), ...off(OFFSET.profitAll)], { formula: `=AND(${S}<>"",${S}<0)` }, fg(COLOR.negFg));
  add('drr', [one(SUMMARY.drr), ...off(OFFSET.drr)], { formula: `=AND(${S}<>"",${S}>0.2)` }, fg(COLOR.negFg));
  add('zero_orders', [one(SUMMARY.orders), ...off(OFFSET.orders)], { formula: `=AND(${I.CLOSED},ISNUMBER(${I.NEIGH(2)}),${I.NEIGH(2)}>0,ISNUMBER(${S}),${S}=0)` }, bg(COLOR.zeroOrdersBg));
  // 17. Заливка закрытого дня со значением: факт — зелёная (цена — жёлтая), ставка — голубая.
  const closedFill = `=AND(${I.CLOSED},${S}<>"")`;
  add('closed_fact', [{ c1: SUMMARY.views, c2: SUMMARY.cancels }, one(SUMMARY.ads), ...offRun(OFFSET.views, OFFSET.stock), ...off(OFFSET.adsIn), ...off(OFFSET.storage)], { formula: closedFill }, bg(COLOR.factBg));
  add('closed_price', off(OFFSET.price), { formula: closedFill }, bg(COLOR.manualBg));
  add('closed_rate', [...off(OFFSET.commission), ...off(OFFSET.logistics)], { formula: closedFill }, bg(COLOR.rateBg));
  return { rules, families };
}

export function cfRequests(cf: VisualCf, sheetId: number, startIndex: number): StructureRequest[] {
  return cf.rules.map((rule, i) => ({ addConditionalFormatRule: { rule, index: startIndex + i } }));
}

/** Относительные ссылки на колонку (без $ перед буквами) вне строковых литералов — запрещены. */
export function relativeColumnRefs(formula: string): string[] {
  const noStr = formula.replace(/"[^"]*"/g, '""');
  return [...noStr.matchAll(/(?<![A-Z$])([A-Z]{1,3})\$?\d+(?![A-Za-z0-9_(])/g)].map((m) => m[0]);
}
