/**
 * UNITKA FINANCIAL INTEGRITY V1 — самовосстанавливающая сверка. ЧИСТЫЙ модуль: ни Sheets, ни BigQuery.
 *
 * Суточный прогон пересматривает скользящее окно 35 календарных дней [LCD−34, LCD] через границы месяцев
 * (решение владельца). Месяц LCD по-прежнему ведёт buildPlan (факты всех закрытых дней + ставки + якоря);
 * этот модуль добавляет ПРОШЛЫЕ месяцы окна: для каждой секции, найденной Calendar V2 по заголовку месяца, —
 * только факт-ячейки дней окна. Ставки прошлого месяца не трогаются (решение владельца: поведение прежнее),
 * ручные поля (СПП, блогеры) и формулы не адресуются никогда: контракт строится только из FACT_KEYS.
 *
 * Нижняя граница сверки — 2026-09-01: сентябрь 2026 — первый месяц под управлением Engine. Август и раньше
 * заполнял Apps Script из других источников; подготовленный слой их не воспроизводит, сверка стёрла бы значения.
 *
 * Координат месяцев здесь нет: строка дня — dayRowOf(геометрия секции), колонка — блок секции + OFFSET.
 */
import { LoaderError } from '../../errors.js';
import type { FactRow, RepairRecord, IssueRecord } from './bq.js';
import { colA1, addDaysIso, monthStartIso, isEmpty, asNumber, isFormulaError, SUMMARY_TO_OFFSET, FACT_KEYS, type CellValue } from './model.js';
import { daysInMonth, formatMonthKey, monthKeyOf, dayRowOf, type MonthKey, type MonthGeometry } from './calendar.js';
import {
  validateSection, expectedFactCells, diffExpected, formatContract, currentValue, cellAt,
  type Snapshot, type ExpectedCell, type PlannedCell, type FormatCell, type ChangeType,
} from './plan.js';
import { factEqual, type Block } from './model.js';
import type { QaCheck } from './qa.js';
import { contractState, type IntegrityIssue } from './integrity.js';

export const RECONCILE_WINDOW_DAYS = 35;
/**
 * ЭПОХА СВЕРКИ (решение владельца 20.09.2026 №1) — граница миграции домена, которым управляет Engine.
 * Сентябрь 2026 — первый месяц под Engine (Stage E1, 12.09.2026). Август 2026 и всё, что раньше, писали прежние
 * процессы (Apps Script, ручной ввод) из других источников; новая сверка эти месяцы НЕ меняет никогда.
 * Это не дата, случайно попавшая в запрос, а явный параметр домена: он один в коде (здесь) и один в SQL
 * (V_UNITKA_RECON_WINDOW.reconciliation_epoch); Engine сверяет их при каждом прогоне.
 *
 *   начало окна = max(начало скользящих 35 дней, RECONCILIATION_EPOCH)
 */
export const RECONCILIATION_EPOCH = '2026-09-01';

/**
 * off — сверки нет (поведение 2.0.0); observe — план и журнал, прошлые месяцы не пишутся; write — сверка окна пишется
 * целиком; controlled — как observe для месяца LCD, а прошлые месяцы окна пишутся ТОЛЬКО через политику
 * controlledWritePolicy (первое заполнение — да, отзыв источника — никогда, поправки — под потолком). controlled
 * реализован, но нигде не включён: включение — отдельное решение владельца (env Job'а, не код).
 */
export type ReconcileMode = 'off' | 'observe' | 'write' | 'controlled';

export interface ReconcileWindow { from: string; to: string; days: number; epoch: string; rollingFrom: string }

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
}

/**
 * Окно сверки: [max(LCD − (days − 1), эпоха), LCD]. Окно не короче самого длинного месяца (31 день), поэтому месяц LCD
 * входит в него целиком всегда; LCD раньше эпохи — отказ (сверять нечего, домен Engine ещё не начался).
 */
export function reconcileWindow(lcd: string, days = RECONCILE_WINDOW_DAYS, epoch = RECONCILIATION_EPOCH): ReconcileWindow {
  if (!Number.isInteger(days) || days < 31) throw new RangeError(`окно сверки ${days} дн.: меньше месяца — месяц LCD не поместится`);
  if (lcd < epoch) throw new RangeError(`LCD ${lcd} раньше эпохи сверки ${epoch}`);
  const rollingFrom = addDaysIso(lcd, -(days - 1));
  return { from: rollingFrom > epoch ? rollingFrom : epoch, to: lcd, days, epoch, rollingFrom };
}

/** Месяцы, пересекающие окно, от старого к новому. Последний — месяц LCD. */
export function windowMonths(w: ReconcileWindow): MonthKey[] {
  const out: MonthKey[] = [];
  let k = monthKeyOf(w.from);
  const last = monthKeyOf(w.to);
  for (;;) {
    out.push(k);
    if (k.year === last.year && k.month === last.month) break;
    k = k.month === 12 ? { year: k.year + 1, month: 1 } : { year: k.year, month: k.month + 1 };
    if (out.length > 4) throw new RangeError('окно сверки пересекает больше 4 месяцев — ошибка границ');
  }
  return out;
}

/** Дни секции месяца, попадающие в окно: [max(начало месяца, from), min(конец месяца, to)]. */
export function sectionWindowDays(key: MonthKey, w: ReconcileWindow): { fromDay: string; toDay: string; dayIndexes: number[] } | null {
  const ms = `${formatMonthKey(key)}-01`;
  const me = addDaysIso(ms, daysInMonth(key.year, key.month) - 1);
  const fromDay = w.from > ms ? w.from : ms;
  const toDay = w.to < me ? w.to : me;
  if (fromDay > toDay) return null;
  const first = daysBetween(ms, fromDay), last = daysBetween(ms, toDay);
  return { fromDay, toDay, dayIndexes: Array.from({ length: last - first + 1 }, (_, i) => first + i) };
}

export interface SectionRepairPlan {
  monthKey: string;
  geometry: MonthGeometry;
  blocks: Block[];
  fromDay: string;
  toDay: string;
  expected: ExpectedCell[];
  cells: PlannedCell[];
  byChangeType: Record<ChangeType, number>;
  formatCells: FormatCell[];
  /** Блоки секции, у которых слой сверки не дал строк за дни окна: не сверяются (и не стираются). */
  skippedBlocks: Array<{ nmId: number; reason: string }>;
}

/**
 * План ремонта ПРОШЛОЙ секции окна: контракт «лист == источник» по факт-ячейкам дней окна и минимальная разница.
 * Секция обязана проходить контракт Calendar V2 (validateSection) — иначе RECON_SECTION_INVALID: вызывающий
 * пропускает секцию с кодом отказа, суточный прогон месяца LCD от этого не зависит.
 */
export function buildSectionRepairPlan(a: { snapshot: Snapshot; facts: readonly FactRow[]; window: ReconcileWindow; bookLcd: string; lcd: string }): SectionRepairPlan {
  const snap = a.snapshot;
  const g = snap.geometry;
  const v = validateSection(snap);
  if (v.sectionIssues.length || v.driftIssues.length) {
    throw new LoaderError(`секция ${g.monthKey} (A${g.topRow}) не проходит контракт: ${[...v.sectionIssues, ...v.driftIssues].slice(0, 8).join(' | ')}`, 'RECON_SECTION_INVALID');
  }
  const days = sectionWindowDays(g.key, a.window);
  if (!days) throw new LoaderError(`секция ${g.monthKey} вне окна сверки ${a.window.from}..${a.window.to}`, 'RECON_SECTION_OUTSIDE_WINDOW');
  const byKey = new Map<string, FactRow>();
  for (const f of a.facts) {
    if (f.date < days.fromDay || f.date > days.toDay) continue;
    const k = `${f.nmId}|${f.date}`;
    if (byKey.has(k)) throw new LoaderError(`дубль nmID×дата в слое сверки: ${k}`, 'DUP_KEY');
    byKey.set(k, f);
  }
  const ms = `${g.monthKey}-01`;
  const expected: ExpectedCell[] = [];
  const skippedBlocks: Array<{ nmId: number; reason: string }> = [];
  for (const b of v.blocks) {
    const have = days.dayIndexes.filter((i) => byKey.has(`${b.nmId}|${addDaysIso(ms, i)}`)).length;
    if (have !== days.dayIndexes.length) {
      // Частичное покрытие блока не сверяется вовсе: недостающий день источника нельзя отличить от «стереть».
      skippedBlocks.push({ nmId: b.nmId, reason: `в слое сверки ${have} дней из ${days.dayIndexes.length}` });
      continue;
    }
    expected.push(...expectedFactCells(g, b, ms, days.dayIndexes, (nm, d) => byKey.get(`${nm}|${d}`)));
  }
  const { cells, byChangeType } = diffExpected(snap, expected, a.bookLcd, a.lcd);
  assertFactCellsOnly(cells, v.blocks, g);
  const closedDays = days.dayIndexes[days.dayIndexes.length - 1]! + 1;
  const fmt = formatContract(snap, v.blocks, closedDays, ms, expected);
  return { monthKey: g.monthKey, geometry: g, blocks: v.blocks, fromDay: days.fromDay, toDay: days.toDay, expected, cells, byChangeType, formatCells: fmt.cells, skippedBlocks };
}

/** Смещения автоматических факт-колонок блока (FACT_KEYS). СПП (15) и блогеры (1) сюда не входят никогда. */
const AUTOMATIC_FACT_OFFSETS: ReadonlySet<number> = new Set([2, 3, 4, 5, 6, 7, 11, 14, 20]);

/**
 * Предохранитель: сверка адресует ТОЛЬКО автоматические факт-ячейки дней секции в колонках её блоков.
 * Ручные поля (СПП, блогеры), формулы, сводка, MTD, хвост книги — вне контракта; нарушение — отказ до записи.
 */
export function assertFactCellsOnly(cells: readonly PlannedCell[], blocks: readonly Block[], g: MonthGeometry): void {
  const bad: string[] = [];
  for (const c of cells) {
    const b = blocks.find((x) => c.col >= x.start && c.col < x.start + 24);
    const off = b ? c.col - b.start : -1;
    if (c.kind !== 'fact' || !b || !AUTOMATIC_FACT_OFFSETS.has(off) || c.row < g.firstDailyRow || c.row > g.lastDailyRow) bad.push(`${colA1(c.col)}${c.row}`);
  }
  if (bad.length) throw new LoaderError(`сверка вышла за автоматические факт-ячейки: ${bad.slice(0, 8).join(', ')}`, 'RECON_PLAN_NOT_CONFINED');
}

/** Вторая линия защиты эпохи: ни одна ячейка плана и ни одна строка источника не может относиться к дате раньше эпохи. */
export function assertNotBeforeEpoch(dates: Iterable<string | undefined>, what: string, epoch = RECONCILIATION_EPOCH): void {
  const bad = [...dates].filter((d): d is string => typeof d === 'string' && d < epoch);
  if (bad.length) throw new LoaderError(`${what}: даты раньше эпохи сверки ${epoch} (${[...new Set(bad)].sort().slice(0, 5).join(', ')}) — месяцы до эпохи новой сверкой не меняются`, 'RECON_BEFORE_EPOCH');
}

/* ───────────────────────── QA прошлой секции после записи ───────────────────────── */

/**
 * Перечитанная прошлая секция против контракта: лист == источник по дням окна, нет ошибок формул в секции,
 * сводка дней окна = Σ блоков (исправленный факт обязан дать согласованную строку — зависимые поля это формулы).
 */
/**
 * `summaryUpTo` (Gate 10, до коммита LCD): сводка сверяется только по дням ≤ закоммиченного LCD книги —
 * формулы сводки более поздних дней честно пусты, пока LCD не закоммичен. Значения и ошибки формул
 * проверяются по всем дням. После коммита функция вызывается без опции — полностью.
 */
export function evaluateRepairedSection(after: Snapshot, plan: SectionRepairPlan, opts: { summaryUpTo?: string } = {}): QaCheck[] {
  const g = plan.geometry;
  const mism: string[] = [];
  for (const e of plan.expected) {
    const got: CellValue = currentValue(after, e);
    if (!factEqual(got, e.want)) mism.push(`${colA1(e.col)}${e.row} ${e.key ?? ''} лист[${String(got)}] BQ[${e.want === null ? '' : e.want}]`);
  }
  const rows: number[] = [];
  for (let r = g.topRow; r <= g.mtdRow; r++) rows.push(r);
  const errs = rowFormulaErrors(after, plan.blocks, rows);
  const summaryTo = opts.summaryUpTo !== undefined && opts.summaryUpTo < plan.toDay ? opts.summaryUpTo : plan.toDay;
  const days: string[] = [];
  for (let day = plan.fromDay; day <= summaryTo; day = addDaysIso(day, 1)) days.push(day);
  const rec = summaryMismatches(after, g, plan.blocks, days);
  const mk = (name: string, bad: string[]): QaCheck => ({ name: `RECON_${g.monthKey}_${name}`, pass: bad.length === 0, count: bad.length, sample: bad.slice(0, 10) });
  return [mk('BQ_SHEETS_MISMATCH', mism), mk('FORMULA_ERRORS', errs), mk('SUMMARY_RECONCILIATION', rec)];
}

/** Ошибки формул в заданных строках секции: колонки сводки и всех блоков (хвост книги за последним блоком — не секции). */
function rowFormulaErrors(after: Snapshot, blocks: readonly Block[], rows: Iterable<number>): string[] {
  const errs: string[] = [];
  const lastCol = blocks.reduce((m, b) => Math.max(m, b.start + 22), 12);
  for (const r of rows) for (let c = 1; c <= lastCol; c++) {
    const v = cellAt(after, r, c);
    if (isFormulaError(v)) errs.push(`${colA1(c)}${r} ${String(v)}`);
  }
  return errs;
}

/** Сводка дня = Σ блоков секции (|Δ| ≤ 0,01) по заданным дням. */
function summaryMismatches(after: Snapshot, g: MonthGeometry, blocks: readonly Block[], days: Iterable<string>): string[] {
  const rec: string[] = [];
  const ms = `${g.monthKey}-01`;
  for (const day of days) {
    const row = dayRowOf(g, daysBetween(ms, day));
    for (const [sumCol, off, name] of SUMMARY_TO_OFFSET) {
      let total = 0;
      for (const b of blocks) { const v = asNumber(cellAt(after, row, b.start + off)); if (Number.isFinite(v)) total += v; }
      const s = asNumber(cellAt(after, row, sumCol));
      const sv = Number.isFinite(s) ? s : 0;
      if (Math.abs(sv - total) > 0.01) rec.push(`${name} ${colA1(sumCol)}${row} сводка[${sv}] Σблоков[${Math.round(total * 100) / 100}]`);
    }
  }
  return rec;
}

/**
 * Перечитывание ТОЧЕЧНОЙ записи в прошлые секции (закрытие конца месяца, режим controlled): каждая ячейка контракта ==
 * источнику, в строках этих дней нет ошибок формул; с `summary` — ещё и сводка дней `summaryDays` = Σ блоков.
 * В отличие от evaluateRepairedSection, остальная часть секции НЕ проверяется: в observe/controlled поправки, которые
 * прогон сознательно не пишет, законно расходятся с источником — и не должны блокировать коммит LCD.
 */
export function sectionCellsReadback(
  name: string,
  parts: ReadonlyArray<{ after: Snapshot; plan: SectionRepairPlan; cells: readonly ExpectedCell[]; summaryDays?: readonly string[] }>,
  opts: { summary?: boolean } = {},
): QaCheck[] {
  const bad: string[] = [];
  const sum: string[] = [];
  for (const p of parts) {
    for (const e of p.cells) {
      const got: CellValue = currentValue(p.after, e);
      if (!factEqual(got, e.want)) bad.push(`${colA1(e.col)}${e.row} ${e.date ?? ''} ${e.key ?? ''} лист[${String(got)}] BQ[${e.want === null ? '' : e.want}]`);
    }
    bad.push(...rowFormulaErrors(p.after, p.plan.blocks, new Set(p.cells.map((c) => c.row))));
    if (opts.summary) sum.push(...summaryMismatches(p.after, p.plan.geometry, p.plan.blocks, p.summaryDays ?? []));
  }
  const mk = (n: string, list: string[]): QaCheck => ({ name: n, pass: list.length === 0, count: list.length, sample: list.slice(0, 10) });
  return opts.summary ? [mk(`${name}_READBACK`, bad), mk(`${name}_SUMMARY`, sum)] : [mk(`${name}_READBACK`, bad)];
}

/* ───────────────────────── закрытие конца месяца ───────────────────────── */

/**
 * Дни ПРОШЛЫХ месяцев, которые книга ещё не закрыла, а кандидат LCD уже перешагнул: bookLcd < d < 1-е число месяца кандидата.
 *
 * Инцидент 02.10.2026 (прогон kf6rr): книга закрыта по 29.09, кандидат 01.10. buildPlan пишет только месяц кандидата,
 * прошлую секцию достаёт только сверка, а в observe она ничего не пишет — LCD закоммитился 29.09 → 01.10, а вся
 * строка 30.09 сентября осталась пустой (в плане сверки было 208 ячеек FACT_CHANGE за 30.09). Зажать кандидата по
 * месяцу нельзя: V_UNITKA_DAILY_FACT отдаёт только месяц КАНОНИЧЕСКОГО LCD (LCD_CANDIDATE_FACTS_UNAVAILABLE).
 * Пусто — обычный суточный прогон, поведение прежнее.
 */
export function unclosedPreviousMonthDays(bookLcd: string, lcd: string): string[] {
  const ms = monthStartIso(lcd);
  const out: string[] = [];
  for (let d = addDaysIso(bookLcd, 1); d < ms; d = addDaysIso(d, 1)) out.push(d);
  return out;
}

export interface MonthEndClosePlan {
  /** Незакрытые дни прошлых месяцев (пусто — закрывать нечего). */
  days: string[];
  /** Ячейки плана сверки за эти дни (FACT_CHANGE: первое заполнение никогда не закрытых дней). */
  cells: PlannedCell[];
  formatCells: FormatCell[];
  /** По секциям: полный контракт этих дней — для перечитывания (каждая ячейка, а не только записанные). */
  parts: Array<{ plan: SectionRepairPlan; days: string[]; expected: ExpectedCell[] }>;
}

/**
 * План закрытия конца месяца. Дни D (unclosedPreviousMonthDays) пишутся из плана сверки прошлой секции В ЛЮБОМ режиме
 * сверки, кроме off, — и в observe тоже: это не исторический ремонт, а первое заполнение дней, которые книга ни разу не
 * закрывала; без них коммит LCD перешагнул бы дыру (дефект A). Ячейки — те же FACT_CHANGE плана сверки, уже прошедшие
 * assertFactCellsOnly (только автоматические факт-колонки).
 *
 * Fail-closed (MONTH_END_UNCLOSED, ДО любой записи данных, LCD стоит): покрытие D обязано быть ПОЛНЫМ — секция месяца
 * сверялась (нет отказа), ни один блок не пропущен (skippedBlocks пуст), и на каждый день D у каждого блока есть все
 * FACT_KEYS контракта. Частичное закрытие дня хуже незакрытого: формулы сводки после коммита посчитали бы день
 * «закрытым» на половине блоков. Режим off закрыть D не может в принципе (слоя сверки нет) — тоже отказ, а не тихий
 * перескок. Повтор после устранения причины идемпотентен.
 */
export function planMonthEndClose(a: {
  bookLcd: string; lcd: string; mode: ReconcileMode;
  sections: ReadonlyArray<{ plan: SectionRepairPlan }>;
  refused?: ReadonlyArray<{ month: string; code: string; message: string }>;
}): MonthEndClosePlan {
  const days = unclosedPreviousMonthDays(a.bookLcd, a.lcd);
  if (!days.length) return { days, cells: [], formatCells: [], parts: [] };
  const fail = (why: string): LoaderError => new LoaderError(
    `кандидат LCD ${a.lcd} перешагивает незакрытые дни прошлого месяца ${days[0]}..${days[days.length - 1]} (книга закрыта по ${a.bookLcd}): ${why} — LCD не двигается, пока эти дни не записаны`,
    'MONTH_END_UNCLOSED',
  );
  if (a.mode === 'off') throw fail('UNITKA_RECONCILE_MODE=off — слоя сверки нет, прошлую секцию писать нечем');
  const byMonth = new Map<string, string[]>();
  for (const d of days) (byMonth.get(d.slice(0, 7)) ?? byMonth.set(d.slice(0, 7), []).get(d.slice(0, 7))!).push(d);
  const problems: string[] = [];
  const parts: MonthEndClosePlan['parts'] = [];
  for (const [mk, ds] of byMonth) {
    const sec = a.sections.find((s) => s.plan.monthKey === mk);
    if (!sec) {
      const r = a.refused?.find((x) => x.month === mk);
      problems.push(`секция ${mk} не сверялась${r ? ` (${r.code}: ${r.message.slice(0, 160)})` : ''}`);
      continue;
    }
    const p = sec.plan;
    if (p.skippedBlocks.length) {
      problems.push(`секция ${mk}: блоки без полного покрытия слоя сверки — ${p.skippedBlocks.slice(0, 5).map((b) => `${b.nmId} (${b.reason})`).join(', ')}`);
      continue;
    }
    const set = new Set(ds);
    const expected = p.expected.filter((e) => e.date !== undefined && set.has(e.date));
    const perDay = p.blocks.length * FACT_KEYS.length;
    const count = new Map<string, number>();
    for (const e of expected) count.set(e.date!, (count.get(e.date!) ?? 0) + 1);
    const short = ds.filter((d) => (count.get(d) ?? 0) !== perDay);
    if (short.length) {
      problems.push(`секция ${mk}: контракт сверки (окно ${p.fromDay}..${p.toDay}) не покрывает ${short.slice(0, 5).join(', ')}${short.length > 5 ? ` и ещё ${short.length - 5}` : ''}`);
      continue;
    }
    parts.push({ plan: p, days: ds, expected });
  }
  if (problems.length) throw fail(problems.join(' | '));
  const all = new Set(days);
  return {
    days,
    cells: a.sections.flatMap((s) => s.plan.cells).filter((c) => c.date !== undefined && all.has(c.date)),
    formatCells: a.sections.flatMap((s) => s.plan.formatCells).filter((c) => all.has(c.date)),
    parts,
  };
}

/* ───────────────────────── режим controlled: политика записи прошлых месяцев ───────────────────────── */

/** Потолок поправок за прогон в режиме controlled: не больше 50 ячеек И не больше 2 % контракта секций окна. */
export const CONTROLLED_MAX_CORRECTIONS = 50;
export const CONTROLLED_MAX_CORRECTION_SHARE = 0.02;

export type ControlledRefusalCode = 'RECON_WITHDRAWAL_REQUIRES_ACK' | 'RECON_CORRECTION_CAP_EXCEEDED' | 'RECON_OUT_OF_SCOPE'
  | 'RECON_RESTRICTED_REQUIRES_ACK' | 'RECON_CANCELS_DECREASE_REQUIRES_ACK' | 'RECON_CANCELS_EXCEED_ORDERS';

/**
 * Охват режима controlled (Phase 1B, решение владельца 07.10.2026 — «урезанный вариант B»).
 *   restricted (по умолчанию) — без владельца пишутся ТОЛЬКО первые заполнения и РОСТ S (поздние отмены Orders API,
 *     доказанные отказы вне Orders API) не выше Q того же дня. Цены, их откаты, уменьшение S, отзывы источника и прочие
 *     поправки — только владельцем. Обоснование (48 прогонов сверки 20.09–07.10): S монотонен (0 откатов), цены
 *     «плавают» (27.09/438775617 650→660→650) — стабильность цены источником не гарантирована.
 *   full — прежняя политика controlled (все поправки ≤ 50 и ≤ 2 %): ТОЛЬКО разовый прогон, явно одобренный владельцем.
 */
export type ControlledScope = 'restricted' | 'full';

export interface ControlledPolicyResult {
  apply: PlannedCell[];
  refused: Array<{ code: ControlledRefusalCode; cell: PlannedCell }>;
  counts: {
    candidates: number; first_fills: number; corrections: number; withdrawals: number; out_of_scope: number;
    applied: number; refused: number; contract_cells: number; correction_cap: number; cap_exceeded: boolean;
    scope: ControlledScope; restricted_refused: number;
  };
}

/**
 * Политика режима controlled — что из плана сверки ПРОШЛЫХ месяцев окна можно записать без владельца. Чистая функция.
 *   • в рамках — только факт-ячейки прошлых месяцев (дата < 1-го числа месяца LCD); автоматические смещения уже
 *     гарантирует assertFactCellsOnly; всё прочее — RECON_OUT_OF_SCOPE (не пишется);
 *   • первое заполнение (в листе пусто, источник даёт значение) — пишется всегда: стереть нечего, ошибиться не в чем;
 *   • отзыв источника (в листе значение, источник — пусто, SOURCE_WITHDRAWN) — НЕ пишется НИКОГДА:
 *     RECON_WITHDRAWAL_REQUIRES_ACK. Стирание закрытой цифры — решение человека; подтверждение владельца — отдельный
 *     ручной прогон в режиме write;
 *   • поправка (в листе значение, источник — другое значение; формула-наследие тоже) — пишутся ВСЕ, только если их
 *     ≤ 50 и ≤ 2 % контракта секций окна; иначе не пишется НИ ОДНА (RECON_CORRECTION_CAP_EXCEEDED): массовый пересмотр
 *     источника — сигнал сбоя слоя, а не повод тихо переписать месяц. Первые заполнения при этом всё равно пишутся.
 */
export function controlledWritePolicy(
  cells: readonly PlannedCell[],
  ctx: {
    lcdMonthStart: string; contractCells: number; maxCorrections?: number; maxShare?: number;
    /** По умолчанию restricted: шире — только явным решением владельца (UNITKA_CONTROLLED_SCOPE=full). */
    scope?: ControlledScope;
    /** Q источника на день SKU — граница роста S (restricted). Нет Q — рост S не пишется. */
    ordersOf?: (nmId: number, date: string) => number | null;
  },
): ControlledPolicyResult {
  const maxN = ctx.maxCorrections ?? CONTROLLED_MAX_CORRECTIONS;
  const share = ctx.maxShare ?? CONTROLLED_MAX_CORRECTION_SHARE;
  const scope: ControlledScope = ctx.scope ?? 'restricted';
  const apply: PlannedCell[] = [];
  const refused: ControlledPolicyResult['refused'] = [];
  const corrections: PlannedCell[] = [];
  let firstFills = 0, withdrawals = 0, outOfScope = 0, restrictedRefused = 0;
  for (const c of cells) {
    if (c.kind !== 'fact' || c.date === undefined || c.date >= ctx.lcdMonthStart) { outOfScope++; refused.push({ code: 'RECON_OUT_OF_SCOPE', cell: c }); continue; }
    const wasEmpty = isEmpty(c.before);
    if (wasEmpty && c.want !== null) { firstFills++; apply.push(c); continue; }
    if (!wasEmpty && c.want === null) { withdrawals++; refused.push({ code: 'RECON_WITHDRAWAL_REQUIRES_ACK', cell: c }); continue; }
    if (scope === 'restricted') {
      const code = restrictedCorrectionRefusal(c, ctx.ordersOf);
      if (code) { restrictedRefused++; refused.push({ code, cell: c }); continue; }
    }
    corrections.push(c);
  }
  const cap = Math.min(maxN, Math.floor(share * ctx.contractCells));
  const capExceeded = corrections.length > cap;
  if (capExceeded) for (const c of corrections) refused.push({ code: 'RECON_CORRECTION_CAP_EXCEEDED', cell: c });
  else apply.push(...corrections);
  return {
    apply, refused,
    counts: {
      candidates: cells.length, first_fills: firstFills, corrections: corrections.length, withdrawals, out_of_scope: outOfScope,
      applied: apply.length, refused: refused.length, contract_cells: ctx.contractCells, correction_cap: cap, cap_exceeded: capExceeded,
      scope, restricted_refused: restrictedRefused,
    },
  };
}

/**
 * restricted: поправка заполненной ячейки пишется без владельца, только если это РОСТ S (want > before, оба — целые
 * числа ≥ 0) и want ≤ Q источника того же дня. Иначе — код отказа (поправка остаётся владельцу).
 */
export function restrictedCorrectionRefusal(c: PlannedCell, ordersOf?: (nmId: number, date: string) => number | null): ControlledRefusalCode | null {
  if (c.key !== 'cancels') return 'RECON_RESTRICTED_REQUIRES_ACK';
  const before = typeof c.before === 'number' ? c.before : Number(c.before);
  const want = c.want;
  if (typeof want !== 'number' || !Number.isInteger(want) || !Number.isInteger(before) || before < 0) return 'RECON_RESTRICTED_REQUIRES_ACK';
  if (want <= before) return 'RECON_CANCELS_DECREASE_REQUIRES_ACK';
  const q = c.nmId !== undefined && c.date !== undefined && ordersOf ? ordersOf(c.nmId, c.date) : null;
  if (q === null || want > q) return 'RECON_CANCELS_EXCEED_ORDERS';
  return null;
}

/* ───────────────────────── журнал ремонта ───────────────────────── */

const show = (v: CellValue | number | null | undefined): string | null => (v === null || v === undefined || isEmpty(v as CellValue) ? null : String(v));

function reasonOf(c: PlannedCell, f: FactRow | undefined): string {
  const wasEmpty = isEmpty(c.before);
  if (c.key === 'price' && f?.priceSource === 'FUNNEL_FALLBACK' && wasEmpty) return 'PRICE_FUNNEL_FALLBACK';
  if (wasEmpty && c.want !== null) return 'LATE_FIRST_FILL';
  if (!wasEmpty && c.want === null) return 'SOURCE_WITHDRAWN';
  return 'SOURCE_REVISED';
}

function asOfOf(c: PlannedCell, f: FactRow | undefined): string | null {
  if (!f) return null;
  switch (c.key) {
    case 'storage': return f.storageObservedAt ?? null;
    case 'price': return f.priceSource === 'FUNNEL_FALLBACK' ? f.funnelObservedAt ?? null : f.ordersBuiltAt ?? null;
    case 'cancels': return f.cancelsSource.startsWith('PROXY_FACT_ORDERS') ? f.ordersBuiltAt ?? null : null;
    case 'opens': case 'carts': case 'orders': return f.ordersSource === 'FUNNEL_API' ? f.funnelObservedAt ?? null : null;
    default: return null;
  }
}

/**
 * Записи ремонта: ровно по одной на ФАКТИЧЕСКУЮ поправку автоматической ячейки уже закрытого в книге дня
 * (LATE_SOURCE_CORRECTION). Факт нового закрытого дня, обновление ставок, продвижение LCD и замена
 * формулы-наследия тем же значением (NO_CHANGE) ремонтом не являются и в журнал не идут.
 */
export function repairRecords(cells: readonly PlannedCell[], ctx: {
  runId: string; environment: string; engineVersion: string; gitSha: string; detectedAt: string; repairedAt: string | null;
  status: RepairRecord['status']; factOf: (nmId: number, date: string) => FactRow | undefined;
  /**
   * Режим controlled: в журнал идут и ПЕРВЫЕ ЗАПОЛНЕНИЯ прошлых секций (FACT_CHANGE, в листе было пусто) — reason
   * LATE_FIRST_FILL: прошлый месяц под controlled меняется только с происхождением. Режим write этот флаг не ставит —
   * его журнал прежний (только LATE_SOURCE_CORRECTION).
   */
  includeFirstFills?: boolean;
}): RepairRecord[] {
  const out: RepairRecord[] = [];
  for (const c of cells) {
    const firstFill = ctx.includeFirstFills === true && c.changeType === 'FACT_CHANGE' && isEmpty(c.before) && c.want !== null;
    if (c.kind !== 'fact' || (c.changeType !== 'LATE_SOURCE_CORRECTION' && !firstFill) || c.nmId === undefined || !c.date) continue;
    const f = ctx.factOf(c.nmId, c.date);
    const cell = `${colA1(c.col)}${c.row}`;
    out.push({
      repairId: `${ctx.runId}|${cell}`, runId: ctx.runId, environment: ctx.environment, engineVersion: ctx.engineVersion, gitSha: ctx.gitSha,
      detectedAt: ctx.detectedAt, repairedAt: ctx.repairedAt, monthKey: c.date.slice(0, 7), businessDate: c.date, nmId: c.nmId,
      field: c.key ?? '', cellA1: cell, oldValue: show(c.before), newValue: show(c.want), source: c.source ?? '',
      sourceAsOf: asOfOf(c, f), reason: reasonOf(c, f), status: ctx.status,
    });
  }
  return out;
}

/* ───────────────────────── снимок issue и действительность строк ───────────────────────── */

/** Код строки-маркера: «оценка целостности этого прогона завершена и записана целиком». */
export const RUN_MARKER_CODE = 'RUN_MARKER';

/**
 * Снимок issue прогона для наблюдаемости — append-only, ПО ПРОГОНАМ (не «последнее состояние»). INFO не пишется (сотни
 * «заказов нет — цены нет» в месяц), кроме PRICE_FUNNEL_FALLBACK (происхождение цены). Issue уровня SKU с
 * недействительными днями разворачивается по дням — «затронутые SKU-дни» считаются в SQL без разбора строк.
 *
 * Строка-маркер (issue_key = NULL) идёт ПОСЛЕДНЕЙ: bq.insertIssues пишет её отдельным оператором после всех issue.
 * Вью статуса считает завершёнными оценками только маркеры, поэтому оборванный снимок не виден вовсе, а прогон без
 * единой issue отличим от «оценки не было» (иначе OK неотличимо от NO_RUN_YET). В маркере — режим сверки и число строк.
 */
export function issueRecords(issues: readonly IntegrityIssue[], ctx: { runId: string; environment: string; evaluatedAt: string; phase: string; reconcileMode?: ReconcileMode }): IssueRecord[] {
  const out: IssueRecord[] = [];
  for (const i of issues) {
    if (i.severity === 'INFO' && i.code !== 'PRICE_FUNNEL_FALLBACK') continue;
    const days: Array<string | null> = i.day !== null ? [i.day] : (i.invalidDays?.length ? i.invalidDays : [null]);
    for (const d of days) {
      out.push({
        runId: ctx.runId, environment: ctx.environment, evaluatedAt: ctx.evaluatedAt, phase: ctx.phase,
        issueKey: `${d ?? '-'}|${i.nmId ?? '-'}|${i.code}`, businessDate: d, nmId: i.nmId, field: i.field, code: i.code,
        state: contractState(i.severity), severity: i.severity, financialValid: !i.financialInvalid,
        source: i.source, sourceValue: i.sourceValue, diagnosticValue: i.diagnosticValue, message: i.message,
      });
    }
  }
  out.push({
    runId: ctx.runId, environment: ctx.environment, evaluatedAt: ctx.evaluatedAt, phase: ctx.phase, issueKey: null, businessDate: null, nmId: null,
    field: '-', code: RUN_MARKER_CODE, state: 'RUN', severity: 'INFO', financialValid: true, source: 'unitka-engine',
    sourceValue: `reconcile_mode=${ctx.reconcileMode ?? 'write'}; issue_rows=${out.length}`, diagnosticValue: null,
    message: 'маркер прогона: оценка целостности завершена, снимок issue записан целиком',
  });
  return out;
}

/** Финансовая действительность SKU-дня: недействителен, если его покрывает хоть одна issue с financialInvalid. */
export function financialValidity(issues: readonly IntegrityIssue[]): Map<string, { valid: boolean; codes: string[] }> {
  const out = new Map<string, { valid: boolean; codes: string[] }>();
  for (const i of issues) {
    if (i.nmId === null) continue;
    const days = i.day !== null ? [i.day] : (i.invalidDays ?? []);
    for (const d of days) {
      const k = `${i.nmId}|${d}`;
      const cur = out.get(k) ?? { valid: true, codes: [] };
      if (i.financialInvalid) { cur.valid = false; cur.codes.push(i.code); }
      out.set(k, cur);
    }
  }
  return out;
}
