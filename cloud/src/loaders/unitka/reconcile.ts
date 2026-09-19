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
import { colA1, addDaysIso, monthStartIso, isEmpty, asNumber, isFormulaError, SUMMARY_TO_OFFSET, type CellValue } from './model.js';
import { daysInMonth, formatMonthKey, monthKeyOf, dayRowOf, type MonthKey, type MonthGeometry } from './calendar.js';
import {
  validateSection, expectedFactCells, diffExpected, formatContract, currentValue, cellAt,
  type Snapshot, type ExpectedCell, type PlannedCell, type FormatCell, type ChangeType,
} from './plan.js';
import { factEqual, type Block } from './model.js';
import type { QaCheck } from './qa.js';
import { contractState, type IntegrityIssue } from './integrity.js';

export const RECONCILE_WINDOW_DAYS = 35;
export const RECONCILE_FLOOR_DATE = '2026-09-01';

export type ReconcileMode = 'off' | 'observe' | 'write';

export interface ReconcileWindow { from: string; to: string; days: number; floor: string }

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
}

/** Окно сверки: [LCD − (days − 1), LCD], месяц LCD целиком, но не раньше нижней границы сверки. */
export function reconcileWindow(lcd: string, days = RECONCILE_WINDOW_DAYS, floor = RECONCILE_FLOOR_DATE): ReconcileWindow {
  if (!Number.isInteger(days) || days < 1) throw new RangeError(`окно сверки ${days} дн.`);
  let from = addDaysIso(lcd, -(days - 1));
  const ms = monthStartIso(lcd);
  if (ms < from) from = ms;            // страховка: месяц LCD всегда целиком (месяц ≤ 31 дня < 35)
  if (from < floor) from = floor;
  return { from, to: lcd, days, floor };
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

/* ───────────────────────── QA прошлой секции после записи ───────────────────────── */

/**
 * Перечитанная прошлая секция против контракта: лист == источник по дням окна, нет ошибок формул в секции,
 * сводка дней окна = Σ блоков (исправленный факт обязан дать согласованную строку — зависимые поля это формулы).
 */
export function evaluateRepairedSection(after: Snapshot, plan: SectionRepairPlan): QaCheck[] {
  const g = plan.geometry;
  const mism: string[] = [];
  for (const e of plan.expected) {
    const got: CellValue = currentValue(after, e);
    if (!factEqual(got, e.want)) mism.push(`${colA1(e.col)}${e.row} ${e.key ?? ''} лист[${String(got)}] BQ[${e.want === null ? '' : e.want}]`);
  }
  const errs: string[] = [];
  const lastCol = plan.blocks.reduce((m, b) => Math.max(m, b.start + 22), 12);
  for (let r = g.topRow; r <= g.mtdRow; r++) for (let c = 1; c <= lastCol; c++) {
    const v = cellAt(after, r, c);
    if (isFormulaError(v)) errs.push(`${colA1(c)}${r} ${String(v)}`);
  }
  const rec: string[] = [];
  const ms = `${g.monthKey}-01`;
  for (let day = plan.fromDay; day <= plan.toDay; day = addDaysIso(day, 1)) {
    const row = dayRowOf(g, daysBetween(ms, day));
    for (const [sumCol, off, name] of SUMMARY_TO_OFFSET) {
      let total = 0;
      for (const b of plan.blocks) { const v = asNumber(cellAt(after, row, b.start + off)); if (Number.isFinite(v)) total += v; }
      const s = asNumber(cellAt(after, row, sumCol));
      const sv = Number.isFinite(s) ? s : 0;
      if (Math.abs(sv - total) > 0.01) rec.push(`${name} ${colA1(sumCol)}${row} сводка[${sv}] Σблоков[${Math.round(total * 100) / 100}]`);
    }
  }
  const mk = (name: string, bad: string[]): QaCheck => ({ name: `RECON_${g.monthKey}_${name}`, pass: bad.length === 0, count: bad.length, sample: bad.slice(0, 10) });
  return [mk('BQ_SHEETS_MISMATCH', mism), mk('FORMULA_ERRORS', errs), mk('SUMMARY_RECONCILIATION', rec)];
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
    case 'cancels': return f.cancelsSource === 'PROXY_FACT_ORDERS' ? f.ordersBuiltAt ?? null : null;
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
}): RepairRecord[] {
  const out: RepairRecord[] = [];
  for (const c of cells) {
    if (c.kind !== 'fact' || c.changeType !== 'LATE_SOURCE_CORRECTION' || c.nmId === undefined || !c.date) continue;
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

/**
 * Снимок issue прогона для наблюдаемости. INFO не пишется (сотни «заказов нет — цены нет» в месяц), кроме
 * PRICE_FUNNEL_FALLBACK (происхождение цены). Issue уровня SKU с недействительными днями разворачивается по дням —
 * тогда «затронутые SKU-дни» и «самая старая нерешённая» считаются в SQL без разбора строк.
 */
export function issueRecords(issues: readonly IntegrityIssue[], ctx: { runId: string; environment: string; evaluatedAt: string; phase: string }): IssueRecord[] {
  // Строка-маркер прогона (issue_key = NULL): без неё чистый прогон не оставил бы следа, и V_UNITKA_INTEGRITY_STATUS
  // продолжала бы показывать прошлый прогон с issue как «текущий» — решённые аномалии никогда бы не стали решёнными.
  const out: IssueRecord[] = [{
    runId: ctx.runId, environment: ctx.environment, evaluatedAt: ctx.evaluatedAt, phase: ctx.phase, issueKey: null, businessDate: null, nmId: null,
    field: '-', code: 'RUN_MARKER', state: 'RUN', severity: 'INFO', financialValid: true, source: 'unitka-engine', sourceValue: null, diagnosticValue: null,
    message: 'маркер прогона: снимок issue этого прогона полон',
  }];
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
