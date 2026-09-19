/**
 * UNITKA CALENDAR V2 (Phase 2B) — канонические построители формул секции месяца.
 *
 * Явный код, НЕ текст XLSX-выгрузки (там FILTER превращается в __xludf.DUMMYFUNCTION). Семантика
 * 1-в-1 с сентябрём 2026 (live-формулы, Phase 2A): меняются только строки и правая граница сводных
 * диапазонов — они выводятся из MonthLayout. Экономика (U V W Y Z AC AE AH AI, взвешивание, налог,
 * комиссия, логистика, хранение, ДРР) НЕ меняется.
 *
 * Перед созданием месяца monthprep.ts прогоняет эти построители по ПОСЛЕДНЕЙ строке дня и строке MTD
 * предыдущего месяца и требует точного совпадения с формулами листа (TEMPLATE_MISMATCH иначе):
 * так доказывается, что построитель воспроизводит живую семантику, а не «похожую».
 */
import { OFFSET, SUMMARY, colA1 } from './model.js';
import { BLOCK_WIDTH, slotStart, type MonthGeometry } from './calendar.js';

const LCD = 'LAST_CLOSED_DATE';
const WEEKDAYS = '"пн","вт","ср","чт","пт","сб","вс"';

/** Параметры блока, которые переносятся из предыдущего месяца (или берутся из канона для нового SKU). */
export interface BlockFormulaParams {
  start: number;
  /** Слагаемое COGS в AI и Y: число («426.735») или разрешённая абсолютная ссылка («$R$45»). */
  cogsTerm: string;
  /** Константа внешней рекламы в Y (блогеры): «10» или «100» — как в последней строке прошлого месяца. */
  overhead: string;
  /**
   * Проекция остатка в будущих днях (визуальный слой, решение E2 KEEP). 'plain' — форма без защиты LCD, встречается
   * только в прошлых месяцах (распознаётся для доказательства шаблона); создаваемый месяц всегда 'guarded'.
   */
  stockProjection: 'guarded' | 'plain' | 'none';
  /** Проекция хранения T×0.15 в будущих днях (только блок 1 сентября): как в прошлом месяце. */
  storageProjection: 'guarded' | 'none';
}

const c = (start: number, off: number): string => colA1(start + off);

/** Формулы расчётных колонок строки дня (смещение → формула). */
export function blockDayFormulas(p: BlockFormulaParams, row: number): Map<number, string> {
  const s = p.start;
  const D = `$${c(s, OFFSET.date)}${row}`;
  const x = (off: number): string => `${c(s, off)}${row}`;
  const g = (body: string): string => `=IF(${D}>${LCD},"",${body})`;
  const Q = x(OFFSET.orders), T = x(OFFSET.stock), W = x(OFFSET.profitAll), X = x(OFFSET.adsIn), Y = x(OFFSET.adsOut);
  const N = x(OFFSET.bloggers), S = x(OFFSET.cancels), AA = x(OFFSET.price), AB = x(OFFSET.spp), AC = x(OFFSET.priceSpp);
  const AD = x(OFFSET.commission), AE = x(OFFSET.priceMinusComm), AF = x(OFFSET.logistics), AG = x(OFFSET.storage);
  const AH = x(OFFSET.tax), AI = x(OFFSET.unitProfit);
  const m = new Map<number, string>();
  m.set(OFFSET.turnover, g(`IF(${Q}=0,"",${T}/${Q})`));
  m.set(OFFSET.profit1, g(`IFERROR(${W}/${Q},"")`));
  m.set(OFFSET.profitAll, g(`${Q}*${AI}-${X}-${AG}-${S}*(${AI}+${AF}+REVERSE_LEG_RATE)+${Y}`));
  m.set(OFFSET.adsOut, g(`-(${p.cogsTerm}+${AA}*(1-${AD})+${p.overhead}-(${AA}*(1-${AD})-${AF}))*${N}`));
  m.set(OFFSET.drr, g(`IFERROR(${X}/((${Q}-${N})*${AC}),"")`));
  m.set(OFFSET.priceSpp, g(`${AA}-${AA}*${AB}%`));
  m.set(OFFSET.priceMinusComm, g(`${AA}-${AA}*${AD}`));
  m.set(OFFSET.tax, g(`${AA}*2%`));
  m.set(OFFSET.unitProfit, g(`${AE}-${AF}-${AH}-${p.cogsTerm}`));
  m.set(OFFSET.weekday, `=IF(${D}="","",CHOOSE(WEEKDAY(${D},2),${WEEKDAYS}))`);
  return m;
}

/**
 * Проекции будущих дней (визуальный слой). Первый день месяца — БЕЗ проекции: ссылка на прошлый
 * месяц запрещена (никакой межмесячной утечки); цепочка строится внутри секции со второго дня.
 */
export function blockProjectionFormulas(p: BlockFormulaParams, row: number, isFirstDay: boolean): Map<number, string> {
  const s = p.start;
  const m = new Map<number, string>();
  const D = `$${c(s, OFFSET.date)}${row}`;
  if (!isFirstDay && p.stockProjection !== 'none') {
    const body = `${c(s, OFFSET.stock)}${row - 1}-${c(s, OFFSET.orders)}${row}+${c(s, OFFSET.cancels)}${row - 1}`;
    m.set(OFFSET.stock, p.stockProjection === 'guarded' ? `=IF(${D}>${LCD},"",${body})` : `=${body}`);
  }
  if (p.storageProjection === 'guarded') m.set(OFFSET.storage, `=IF(${D}>${LCD},"",${c(s, OFFSET.stock)}${row}*0.15)`);
  return m;
}

/** Колонки сводки ↔ смещение блока для сумм FILTER(MOD(…,24)). */
const SUMMARY_SUMS: ReadonlyArray<readonly [number, number]> = [
  [SUMMARY.bloggers, OFFSET.bloggers], [SUMMARY.views, OFFSET.views], [SUMMARY.opens, OFFSET.opens],
  [SUMMARY.orders, OFFSET.orders], [SUMMARY.carts, OFFSET.carts], [SUMMARY.cancels, OFFSET.cancels],
  [SUMMARY.profit, OFFSET.profitAll], [SUMMARY.ads, OFFSET.adsIn],
];

/**
 * Горизонтальная полоса сводки: от колонки смещения в слоте 0 до той же колонки в последнем занятом
 * слоте. MOD(…,24) выбирает ровно колонки смещения во ВСЕХ слотах, включая зарезервированный 24 и
 * пустые слоты выбывших SKU: в строках месяца они пусты, FILTER даёт пустые значения, SUM их не считает.
 */
function band(off: number, lastSlot: number, r1: number, r2: number, abs: boolean): { range: string; first: string } {
  const a = colA1(slotStart(0) + off), b = colA1(slotStart(lastSlot) + off);
  if (abs) return { range: `$${a}$${r1}:$${b}$${r2}`, first: `$${a}$${r1}` };
  return { range: `${a}${r1}:${b}${r2}`, first: `${a}${r1}` };
}
const filt = (bd: { range: string; first: string }): string => `FILTER(${bd.range},MOD(COLUMN(${bd.range})-COLUMN(${bd.first}),${BLOCK_WIDTH})=0)`;

function drrCore(lastSlot: number, r1: number, r2: number, ads: string): string {
  const q = filt(band(OFFSET.orders, lastSlot, r1, r2, true));
  const n = filt(band(OFFSET.bloggers, lastSlot, r1, r2, true));
  const ac = filt(band(OFFSET.priceSpp, lastSlot, r1, r2, true));
  return `IFERROR(${ads}/SUMPRODUCT(IFERROR((${q}-${n})*${ac},0)),"")`;
}

/** Сводка A..L строки дня. firstBlockStart — колонка даты первого блока (L — день недели по ней). */
export function summaryDayFormulas(row: number, lastSlot: number, firstBlockStart: number): Map<number, string> {
  const m = new Map<number, string>();
  const B = `$B${row}`;
  m.set(SUMMARY.weekday, `=IF(${B}="","",CHOOSE(WEEKDAY(${B},2),${WEEKDAYS}))`);
  for (const [col, off] of SUMMARY_SUMS) {
    m.set(col, `=IF(${B}>${LCD},"",SUM(${filt(band(off, lastSlot, row, row, false))}))`);
  }
  m.set(SUMMARY.drr, `=IF(${B}>${LCD},"",${drrCore(lastSlot, row, row, `J${row}`)})`);
  const Mc = `$${colA1(firstBlockStart)}${row}`;
  m.set(SUMMARY.drr + 1, `=IF(${Mc}="","",CHOOSE(WEEKDAY(${Mc},2),${WEEKDAYS}))`);
  return m;
}

/** Сводка строки MTD (C..K). */
export function summaryMtdFormulas(g: MonthGeometry, lastSlot: number): Map<number, string> {
  const m = new Map<number, string>();
  for (const [col] of SUMMARY_SUMS) {
    const L = colA1(col);
    m.set(col, `=SUM(${L}${g.firstDailyRow}:${L}${g.lastDailyRow})`);
  }
  m.set(SUMMARY.drr, `=${drrCore(lastSlot, g.firstDailyRow, g.lastDailyRow, `J${g.mtdRow}`)}`);
  return m;
}

/** Формулы блока в строке MTD (смещение → формула). Подпись MTD — отдельно (только первый блок). */
export function blockMtdFormulas(start: number, g: MonthGeometry): Map<number, string> {
  const f = g.firstDailyRow, l = g.lastDailyRow, mt = g.mtdRow;
  const dc = colA1(start + OFFSET.date);
  const Dabs = `$${dc}$${f}:$${dc}$${l}`;
  const col = (off: number): string => colA1(start + off);
  const rng = (off: number): string => `${col(off)}${f}:${col(off)}${l}`;
  const sumif = (off: number): string => `=SUMIF(${Dabs},"<="&${LCD},${rng(off)})`;
  const closed = (off: number): string => `FILTER(${rng(off)},${Dabs}<=${LCD})`;
  const weighted = (off: number): string => `=IFERROR(SUMPRODUCT(${closed(off)},${closed(OFFSET.orders)})/${col(OFFSET.orders)}${mt},"")`;
  const m = new Map<number, string>();
  for (const off of [OFFSET.bloggers, OFFSET.views, OFFSET.opens, OFFSET.orders, OFFSET.carts, OFFSET.cancels]) m.set(off, sumif(off));
  // Живой лист (Sheets API, 19.09.2026): остаток MTD введён как формула массива — API отдаёт обёртку
  // ARRAY_CONSTRAIN(ARRAYFORMULA(…), 1, 1). XLSX её прячет. Воспроизводим форму 1-в-1.
  m.set(OFFSET.stock, `=ARRAY_CONSTRAIN(ARRAYFORMULA(IFERROR(INDEX(${rng(OFFSET.stock)},MATCH(${LCD},${Dabs},0)),"")), 1, 1)`);
  m.set(OFFSET.turnover, `=IFERROR(${col(OFFSET.stock)}${mt}/(${col(OFFSET.orders)}${mt}/COUNTIF(${Dabs},"<="&${LCD})),"")`);
  m.set(OFFSET.profit1, `=IFERROR(${col(OFFSET.profitAll)}${mt}/${col(OFFSET.orders)}${mt},"")`);
  for (const off of [OFFSET.profitAll, OFFSET.adsIn, OFFSET.adsOut]) m.set(off, sumif(off));
  m.set(OFFSET.drr, `=IFERROR(${col(OFFSET.adsIn)}${mt}/SUMPRODUCT((${Dabs}<=${LCD})*(${rng(OFFSET.orders)}-${rng(OFFSET.bloggers)})*${rng(OFFSET.priceSpp)}),"")`);
  for (const off of [OFFSET.price, OFFSET.priceSpp, OFFSET.commission, OFFSET.priceMinusComm, OFFSET.unitProfit]) m.set(off, weighted(off));
  m.set(OFFSET.logistics, `=SUMPRODUCT((${Dabs}<=${LCD})*${rng(OFFSET.logistics)}*${rng(OFFSET.orders)})+SUMIF(${Dabs},"<="&${LCD},${rng(OFFSET.cancels)})*REVERSE_LEG_RATE`);
  m.set(OFFSET.storage, sumif(OFFSET.storage));
  m.set(OFFSET.tax, `=IFERROR(SUMPRODUCT(${closed(OFFSET.tax)},${closed(OFFSET.orders)}),"")`);
  return m;
}

/** Нормализация для сравнения формул листа с построителем: пробелы и регистр не значимы. */
export function normFormula(f: unknown): string {
  return String(f ?? '').replace(/\s+/g, '').toUpperCase();
}

/* ───────────────────────── локаль формул (Phase 2C) ───────────────────────── */

/**
 * Живая проверка 19.09.2026 (тестовая копия книги, Sheets API): книга в локали ru_RU, и API ОТДАЁТ формулы в
 * форме локали — разделитель аргументов «;», десятичная «,» (`T766*0,15`). Построители пишут каноническую
 * форму en (`,` и `.`). Перевод — только ВНЕ строковых литералов; имена функций в API всегда английские.
 * Неизвестная локаль — отказ (fail-closed), а не догадка.
 */
export type FormulaStyle = 'COMMA' | 'SEMICOLON';

export function formulaStyleOf(locale: string | null | undefined): FormulaStyle {
  const l = String(locale ?? '').trim().toLowerCase();
  if (l === '' || l === 'en' || l.startsWith('en_')) return 'COMMA';
  if (l === 'ru' || l.startsWith('ru_')) return 'SEMICOLON';
  throw new RangeError(`локаль книги «${locale}» не поддержана Calendar V2 (ожидается en_* или ru_*)`);
}

/** Применить fn к кускам формулы вне строковых литералов "…" (кавычки внутри литерала удваиваются). */
function outsideStrings(f: string, fn: (chunk: string) => string): string {
  let out = '';
  let i = 0;
  while (i < f.length) {
    const q = f.indexOf('"', i);
    if (q < 0) { out += fn(f.slice(i)); break; }
    out += fn(f.slice(i, q));
    let j = q + 1;
    for (;;) {
      const e = f.indexOf('"', j);
      if (e < 0) { j = f.length; break; }
      if (f[e + 1] === '"') { j = e + 2; continue; }
      j = e + 1; break;
    }
    out += f.slice(q, j);
    i = j;
  }
  return out;
}

/** Каноническая форма (en) → форма локали книги. */
export function toLocaleFormula(f: string, style: FormulaStyle): string {
  if (style === 'COMMA') return f;
  return outsideStrings(f, (s) => s.replace(/,/g, ';').replace(/(\d)\.(\d)/g, '$1,$2'));
}

/** Форма локали книги → каноническая форма (en). В «;»-локали запятая вне строк — только десятичная. */
export function fromLocaleFormula(f: string, style: FormulaStyle): string {
  if (style === 'COMMA') return f;
  return outsideStrings(f, (s) => s.replace(/(\d),(\d)/g, '$1.$2').replace(/;/g, ','));
}
