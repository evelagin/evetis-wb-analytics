/**
 * UNITKA CALENDAR V2 (Phase 2B) — календарь и геометрия месячной секции листа `WB_Юнит_2025`.
 *
 * Чистый модуль без I/O. Заменяет сентябрьские константы GRID (735/737/767, 30 дней, 24 блока):
 *   • длина месяца — только календарным вычислением (григорианский календарь, UTC), без таблиц;
 *   • секция месяца = заголовок месяца (A{top}) · шапка · дни · строка MTD · строка-разделитель;
 *     следующая секция начинается сразу за разделителем (шаг days + 4 — подтверждён на всех
 *     21 секциях листа с января 2025, Phase 2A);
 *   • блоки SKU — слоты по 24 колонки от M, СПЛОШНОЙ цепочкой без дыр (решение владельца, visual
 *     hardening 2): новый блок вставляется колонками сразу за последним блоком, унаследованный «хвост»
 *     (расчёты VQ..VZ, подписи WA, якоря книги в колонке WB) сдвигается вправо; колонку якорей Engine
 *     находит по именованному диапазону REVERSE_LEG_RATE, а не по константе.
 *
 * Бизнес-часовой пояс Unitka — Europe/Moscow, но сюда он не попадает: месяц и строка выводятся
 * из ДАТЫ LAST_CLOSED_DATE (её считает BigQuery в МСК), арифметика дат — в UTC на ISO-строках.
 */

export interface MonthKey {
  year: number;
  month: number; // 1..12
}

/** Названия месяцев для заголовка секции (как в листе: «Сентябрь 2026»). Длины месяцев здесь НЕТ. */
export const RU_MONTH_TITLES: readonly string[] = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
];

function assertKey(k: MonthKey): void {
  if (!Number.isInteger(k.year) || k.year < 1900 || k.year > 9999) throw new RangeError(`год вне диапазона: ${k.year}`);
  if (!Number.isInteger(k.month) || k.month < 1 || k.month > 12) throw new RangeError(`месяц вне диапазона: ${k.month}`);
}

/** Число дней месяца: день 0 следующего месяца = последний день этого (Date.UTC, григорианский календарь). */
export function daysInMonth(year: number, month: number): number {
  assertKey({ year, month });
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

export function monthKeyOf(iso: string): MonthKey {
  const m = /^(\d{4})-(\d{2})-\d{2}$/.exec(iso);
  if (!m) throw new RangeError(`ожидалась дата YYYY-MM-DD, получено '${iso}'`);
  const k = { year: Number(m[1]), month: Number(m[2]) };
  assertKey(k);
  return k;
}

export function parseMonthKey(s: string): MonthKey {
  const m = /^(\d{4})-(\d{2})$/.exec(s.trim());
  if (!m) throw new RangeError(`ожидался месяц YYYY-MM, получено '${s}'`);
  const k = { year: Number(m[1]), month: Number(m[2]) };
  assertKey(k);
  return k;
}

export function formatMonthKey(k: MonthKey): string {
  assertKey(k);
  return `${k.year}-${String(k.month).padStart(2, '0')}`;
}

export function nextMonth(k: MonthKey): MonthKey {
  assertKey(k);
  return k.month === 12 ? { year: k.year + 1, month: 1 } : { year: k.year, month: k.month + 1 };
}

export function previousMonth(k: MonthKey): MonthKey {
  assertKey(k);
  return k.month === 1 ? { year: k.year - 1, month: 12 } : { year: k.year, month: k.month - 1 };
}

export function sameMonth(a: MonthKey, b: MonthKey): boolean {
  return a.year === b.year && a.month === b.month;
}

/** Заголовок секции в колонке A: «Октябрь 2026». */
export function monthTitle(k: MonthKey): string {
  assertKey(k);
  return `${RU_MONTH_TITLES[k.month - 1]} ${k.year}`;
}

export function monthStartOf(k: MonthKey): string {
  return `${formatMonthKey(k)}-01`;
}

/** ISO-дата i-го (0-based) дня месяца. */
export function dayIso(k: MonthKey, dayIndex: number): string {
  const n = daysInMonth(k.year, k.month);
  if (!Number.isInteger(dayIndex) || dayIndex < 0 || dayIndex >= n) throw new RangeError(`день ${dayIndex + 1} вне месяца ${formatMonthKey(k)} (${n} дн.)`);
  return `${formatMonthKey(k)}-${String(dayIndex + 1).padStart(2, '0')}`;
}

/** 0-based индекс дня даты в месяце k; дата другого месяца — RangeError (никакой «перетечки»). */
export function dayIndexOf(k: MonthKey, iso: string): number {
  const d = monthKeyOf(iso);
  if (!sameMonth(d, k)) throw new RangeError(`${iso} не принадлежит месяцу ${formatMonthKey(k)}`);
  const day = Number(iso.slice(8, 10));
  if (day < 1 || day > daysInMonth(k.year, k.month)) throw new RangeError(`нет такой даты: ${iso}`);
  return day - 1;
}

/* ───────────────────────── слоты блоков ───────────────────────── */

/** Колонка M — первая колонка слота 0; ширина блока — 24 колонки (карта UNITKA 2.0). */
export const BLOCK_FIRST_COLUMN = 13;
export const BLOCK_WIDTH = 24;

/**
 * Тело блока — смещения 0..22 (дата … доходность 1 шт). Смещение 23 — колонка дня недели — существует только
 * МЕЖДУ блоками: зрительно она начинает следующий SKU. У ПОСЛЕДНЕГО блока месяца её нет — цепочка кончается
 * последней метрикой, а не «стартером» несуществующего блока (final polish, F2).
 */
export const BLOCK_BODY_WIDTH = 23;
/** Унаследованный хвост книги: 10 скрытых расчётных колонок, подписи и колонка якорей (последняя). */
export const TAIL_WIDTH = 12;

export function slotStart(slot: number): number {
  if (!Number.isInteger(slot) || slot < 0) throw new RangeError(`слот ${slot}`);
  return BLOCK_FIRST_COLUMN + slot * BLOCK_WIDTH;
}
export function slotEnd(slot: number): number {
  return slotStart(slot) + BLOCK_WIDTH - 1;
}
/** Слот колонки (или null для колонок до M). */
export function slotOfColumn(col: number): number | null {
  return col < BLOCK_FIRST_COLUMN ? null : Math.floor((col - BLOCK_FIRST_COLUMN) / BLOCK_WIDTH);
}
/** Следующий слот цепочки: сразу за after (дыр между активными блоками нет). */
export function nextFreeSlot(after: number): number {
  return after + 1;
}

/** Слоты-дыры внутри цепочки блоков (между первым и последним занятым). Контракт: пусто. */
export function chainGaps(blocks: readonly { slot: number }[]): number[] {
  if (blocks.length === 0) return [];
  const used = new Set(blocks.map((b) => b.slot));
  const min = Math.min(...used), max = Math.max(...used);
  const gaps: number[] = [];
  for (let s = min; s <= max; s++) if (!used.has(s)) gaps.push(s);
  return gaps;
}

/* ───────────────────────── геометрия секции ───────────────────────── */

export interface MonthGeometry {
  key: MonthKey;
  monthKey: string;       // '2026-10'
  title: string;          // 'Октябрь 2026'
  monthStart: string;     // '2026-10-01'
  daysInMonth: number;
  topRow: number;         // заголовок месяца (A{top}) и заголовки блоков (nmID)
  headerRow: number;      // шапка метрик
  firstDailyRow: number;
  lastDailyRow: number;
  mtdRow: number;
  spacerRow: number;
  nextTopRow: number;     // заголовок следующего месяца
}

export function geometryAt(key: MonthKey, topRow: number): MonthGeometry {
  assertKey(key);
  if (!Number.isInteger(topRow) || topRow < 1) throw new RangeError(`строка заголовка ${topRow}`);
  const days = daysInMonth(key.year, key.month);
  const firstDailyRow = topRow + 2;
  const lastDailyRow = firstDailyRow + days - 1;
  const mtdRow = lastDailyRow + 1;
  const spacerRow = mtdRow + 1;
  return {
    key: { ...key }, monthKey: formatMonthKey(key), title: monthTitle(key), monthStart: monthStartOf(key),
    daysInMonth: days, topRow, headerRow: topRow + 1, firstDailyRow, lastDailyRow, mtdRow, spacerRow,
    nextTopRow: spacerRow + 1,
  };
}

/** Строка листа i-го (0-based) дня. */
export function dayRowOf(g: MonthGeometry, dayIndex: number): number {
  if (!Number.isInteger(dayIndex) || dayIndex < 0 || dayIndex >= g.daysInMonth) throw new RangeError(`день ${dayIndex + 1} вне секции ${g.monthKey}`);
  return g.firstDailyRow + dayIndex;
}

/** Строка листа даты; дата другого месяца — RangeError. */
export function rowOfDate(g: MonthGeometry, iso: string): number {
  return g.firstDailyRow + dayIndexOf(g.key, iso);
}

/** Блок SKU в секции месяца. index — порядковый номер найденного блока (0..n-1), slot — позиция в сетке. */
export interface BlockSlot {
  index: number;
  slot: number;
  start: number;
  nmId: number;
  title: string;
}

export interface MonthLayout extends MonthGeometry {
  blocks: readonly BlockSlot[];
  /** Терминальная колонка цепочки: последняя метрика последнего блока (0, если блоков нет). */
  lastBlockColumn: number;
}

export function layoutOf(g: MonthGeometry, blocks: readonly BlockSlot[]): MonthLayout {
  const maxSlot = blocks.reduce((m, b) => Math.max(m, b.slot), -1);
  return { ...g, blocks, lastBlockColumn: maxSlot < 0 ? 0 : slotStart(maxSlot) + BLOCK_BODY_WIDTH - 1 };
}

/** Последний блок раскладки (у него нет колонки-разделителя). */
export function isLastBlock(layout: MonthLayout, b: { slot: number }): boolean {
  return layout.blocks.every((x) => x.slot <= b.slot);
}
/** Последняя колонка блока В РАСКЛАДКЕ: у последнего — последняя метрика, у остальных — разделитель (день недели). */
export function blockLastColumn(layout: MonthLayout, b: { slot: number; start: number }): number {
  return isLastBlock(layout, b) ? b.start + BLOCK_BODY_WIDTH - 1 : b.start + BLOCK_WIDTH - 1;
}
/** Блок, которому колонка принадлежит в раскладке (колонка за последней метрикой последнего блока — ничья). */
export function blockOfColumn(layout: MonthLayout, col: number): BlockSlot | null {
  return layout.blocks.find((b) => col >= b.start && col <= blockLastColumn(layout, b)) ?? null;
}

/** Первая колонка хвоста книги: якорь — его последняя колонка (колонка якорей — из REVERSE_LEG_RATE). */
export function tailStartOf(anchorCol: number): number {
  return anchorCol - TAIL_WIDTH + 1;
}

/**
 * Вставка колонок под newCount новых блоков за последним блоком lastSlot. Хвост книги обязан начинаться либо
 * сразу за колонкой-разделителем последнего блока (книга до Calendar V2: у последнего блока она физически есть),
 * либо сразу за его последней метрикой (книга после Calendar V2). Вставляется ровно столько колонок, чтобы хвост
 * встал сразу за последней метрикой нового последнего блока. Иное положение хвоста — отказ.
 * at — 0-based индекс вставки (= число колонок перед ней).
 */
export function insertGeometry(lastSlot: number, tailStart: number, newCount: number): { at: number; count: number; tailStartAfter: number } | { error: 'TAIL_GEOMETRY_UNKNOWN' } {
  const bodyEnd = slotStart(lastSlot) + BLOCK_BODY_WIDTH - 1;
  if (tailStart !== bodyEnd + 1 && tailStart !== bodyEnd + 2) return { error: 'TAIL_GEOMETRY_UNKNOWN' };
  const at = tailStart - 1;
  if (newCount <= 0) return { at, count: 0, tailStartAfter: tailStart };
  const tailStartAfter = slotStart(lastSlot + newCount) + BLOCK_BODY_WIDTH;
  return { at, count: tailStartAfter - tailStart, tailStartAfter };
}

/* ───────────────────────── поиск секции ───────────────────────── */

export type SectionLocation =
  | { status: 'FOUND'; topRow: number }
  | { status: 'MISSING' }
  | { status: 'AMBIGUOUS'; rows: number[] };

/**
 * Поиск секции месяца по точному заголовку в колонке A. columnA[i] — значение A{i+1}.
 * Геометрия НЕ угадывается из шага: ищется ровно один заголовок; ноль — MISSING, больше — AMBIGUOUS.
 */
export function locateSection(columnA: readonly unknown[], key: MonthKey): SectionLocation {
  const title = monthTitle(key);
  const rows: number[] = [];
  columnA.forEach((v, i) => { if (typeof v === 'string' && v.trim() === title) rows.push(i + 1); });
  if (rows.length === 0) return { status: 'MISSING' };
  if (rows.length > 1) return { status: 'AMBIGUOUS', rows };
  return { status: 'FOUND', topRow: rows[0]! };
}
