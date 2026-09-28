/**
 * SPP-3 — колонка AB «СПП %» листа WB из факта WB (wb_mart.V_WB_SPP_DAILY). Чистый модуль без I/O.
 *
 * Контракт записи (решение владельца 28.09.2026):
 *   дата < 2026-09-01        → AB не трогается никогда (история ручного ввода);
 *   дата > кандидата цикла    → не трогается (будущее; факта ещё нет);
 *   строка вью есть           → AB = ROUND(MAX(effective_spp_pct, 0), 1) — процентные пункты (20 = 20 %);
 *   строки вью нет            → AB пустая (ручные значения с 01.09 очищаются: одна семантика колонки).
 * Окно — то же, что у сверки (35 дней от кандидата), но не раньше старта: поздние заказы прошлых дней
 * пересчитывают AB так же, как пересчитываются факты.
 *
 * Формат AB — NUMBER «#,##0», значение хранится в п.п. (формулы: AC = AA − AA*AB%). Запись значений
 * в режиме RAW формат не меняет (проверено на тестовой книге 28.09); перечитывание это доказывает
 * на каждой ячейке, а не предполагает.
 */
import { createHash } from 'node:crypto';
import { gzipSync, gunzipSync } from 'node:zlib';
import { OFFSET, colA1, findBlocks, addDaysIso, type CellValue } from './model.js';
import { dayRowOf } from './calendar.js';
import { cellAt, formatAt, type Snapshot } from './plan.js';
import { reconcileWindow } from './reconcile.js';
import { canonicalJson } from './monthrollback.js';
import type { SppDayRow } from './bq.js';
import type { QaCheck } from './qa.js';
import { LoaderError } from '../../errors.js';

export const SPP_START = '2026-09-01';
export type SppMode = 'off' | 'observe' | 'write';

export type SppClass =
  | 'ACTUAL_FILL'                // факт в пустую ячейку
  | 'ACTUAL_REPLACE'             // факт вместо другого значения (в т.ч. ручного)
  | 'NEGATIVE_MARKUP_TO_ZERO'    // рассрочка с наценкой: effective < 0 → в AB 0
  | 'MANUAL_CLEAR_NO_ORDER'      // строки вью нет, заказов в листе нет (Q = 0) → очистить
  | 'MANUAL_CLEAR_FUNNEL_ONLY'   // строки вью нет, Q > 0 (заказы только в воронке) → очистить
  | 'PRICE_DATA_MISSING_CLEAR';  // строка вью есть, но effective = NULL → пусто, значение не выдумывается

export interface SppCell {
  row: number;
  col: number;
  a1: string;
  month: string;
  date: string;
  nmId: number;
  before: CellValue;
  beforeFormula: CellValue;
  numberFormat: { type?: string; pattern?: string } | null;
  want: number | null;          // null — очистить
  cls: SppClass;
}

export interface SppCounts {
  sku_day_rows: number;
  cells_to_write: number;
  cells_to_clear: number;
  already_correct: number;
  no_change: number;
  negative_markup_to_zero: number;
  missing_with_q_gt_0: number;
  by_class: Record<string, number>;
  dates_before_start_touched: number;
  future_dates_touched: number;
  dates_after_candidate_touched: number;
  future_nonblank_untouched: number;
}

export interface SppPlan {
  window: { from: string; to: string };
  candidate: string;
  cells: SppCell[];
  counts: SppCounts;
  /** Строки вью в окне, для которых в листе нет блока SKU (не пишется, только отчёт). */
  rowsWithoutBlock: string[];
}

/** Окно записи: окно сверки кандидата, но не раньше старта AUTO. null — окно ещё не началось. */
export function sppWindow(candidate: string): { from: string; to: string } | null {
  if (candidate < SPP_START) return null;
  const w = reconcileWindow(candidate);
  return { from: w.from > SPP_START ? w.from : SPP_START, to: candidate };
}

/** Значение для AB: п.п., не меньше 0, шаг 0,1. null — писать нечего (данных цены нет). */
export function sppWriteValue(r: Pick<SppDayRow, 'effectiveSppPct'>): number | null {
  if (r.effectiveSppPct === null || !Number.isFinite(r.effectiveSppPct)) return null;
  return Math.round(Math.max(r.effectiveSppPct, 0) * 10) / 10;
}

const isBlank = (v: CellValue): boolean => v === null || v === undefined || (typeof v === 'string' && v.trim() === '');
const asNum = (v: CellValue): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
/** Значение в ячейке равно желаемому: пусто = пусто, числа — с точностью до 0,0005 п.п. */
export function sppEqual(before: CellValue, want: number | null): boolean {
  if (want === null) return isBlank(before);
  const b = asNum(before);
  return b !== null && Math.abs(b - want) < 0.0005;
}

/**
 * План AB по секциям окна. Каждая изменяемая ячейка получает класс; даты вне [from, to] и до старта
 * не попадают в план конструктивно, а счётчики «touched» проверяют это ещё раз — нарушение = исключение.
 */
export function planSpp(a: {
  sections: readonly Snapshot[]; rows: readonly SppDayRow[]; candidate: string;
}): SppPlan {
  const window = sppWindow(a.candidate);
  const byKey = new Map(a.rows.map((r) => [`${r.nmId}|${r.date}`, r]));
  const cells: SppCell[] = [];
  const counts: SppCounts = {
    sku_day_rows: 0, cells_to_write: 0, cells_to_clear: 0, already_correct: 0, no_change: 0, negative_markup_to_zero: 0,
    missing_with_q_gt_0: 0, by_class: {}, dates_before_start_touched: 0, future_dates_touched: 0, dates_after_candidate_touched: 0,
    future_nonblank_untouched: 0,
  };
  const covered = new Set<string>();
  if (!window) return { window: { from: SPP_START, to: a.candidate }, candidate: a.candidate, cells, counts, rowsWithoutBlock: [] };
  for (const snap of a.sections) {
    const g = snap.geometry;
    const blocks = findBlocks(snap.grid[0] ?? [], snap.width);
    for (let i = 0; i < g.daysInMonth; i++) {
      const date = addDaysIso(g.monthStart, i);
      const row = dayRowOf(g, i);
      for (const b of blocks) {
        const col = b.start + OFFSET.spp;
        const before = cellAt(snap, row, col);
        if (date > a.candidate) {                         // будущее: не трогаем, только видим
          if (!isBlank(before) && date >= SPP_START) counts.future_nonblank_untouched++;
          continue;
        }
        if (date < window.from) continue;                 // до старта / вне окна — не трогаем
        const r = byKey.get(`${b.nmId}|${date}`);
        if (r) { counts.sku_day_rows++; covered.add(`${b.nmId}|${date}`); }
        const q = asNum(cellAt(snap, row, b.start + OFFSET.orders)) ?? 0;
        if (!r && q > 0) counts.missing_with_q_gt_0++;
        const want = r ? sppWriteValue(r) : null;
        if (sppEqual(before, want)) {
          if (want === null) counts.no_change++; else counts.already_correct++;
          continue;
        }
        const cls: SppClass = r
          ? (r.effectiveSppPct === null ? 'PRICE_DATA_MISSING_CLEAR'
            : r.effectiveSppPct < 0 ? 'NEGATIVE_MARKUP_TO_ZERO'
              : isBlank(before) ? 'ACTUAL_FILL' : 'ACTUAL_REPLACE')
          : (q > 0 ? 'MANUAL_CLEAR_FUNNEL_ONLY' : 'MANUAL_CLEAR_NO_ORDER');
        const f = formatAt(snap, row, col).numberFormat;
        cells.push({
          row, col, a1: `${colA1(col)}${row}`, month: g.monthKey, date, nmId: b.nmId, before,
          beforeFormula: snap.formulas[row - g.firstDailyRow]?.[col - 1] ?? '',
          numberFormat: f ? { type: f.type, pattern: f.pattern } : null, want, cls,
        });
        counts.by_class[cls] = (counts.by_class[cls] ?? 0) + 1;
        if (want === null) counts.cells_to_clear++; else counts.cells_to_write++;
        if (cls === 'NEGATIVE_MARKUP_TO_ZERO') counts.negative_markup_to_zero++;
      }
    }
  }
  counts.dates_before_start_touched = cells.filter((c) => c.date < SPP_START).length;
  counts.dates_after_candidate_touched = cells.filter((c) => c.date > a.candidate).length;
  counts.future_dates_touched = counts.dates_after_candidate_touched;
  if (counts.dates_before_start_touched || counts.dates_after_candidate_touched) {
    throw new LoaderError(`план AB вышел за границы: до ${SPP_START} — ${counts.dates_before_start_touched}, после кандидата — ${counts.dates_after_candidate_touched}`, 'SPP_PLAN_OUT_OF_BOUNDS');
  }
  const rowsWithoutBlock = a.rows.filter((r) => r.date >= window.from && r.date <= window.to && !covered.has(`${r.nmId}|${r.date}`))
    .map((r) => `${r.date} ${r.nmId}`);
  return { window, candidate: a.candidate, cells, counts, rowsWithoutBlock };
}

/**
 * Запись: только числа (п.п.), RAW — формат ячейки сохраняется. Очистка — ОТДЕЛЬНО, values.batchClear:
 * запись '' через values.batchUpdate стирает numberFormat (замер на тестовой книге 28.09).
 */
export function sppWriteRanges(cells: readonly SppCell[], sheetName: string): Array<{ range: string; values: CellValue[][] }> {
  const q = `'${sheetName.replace(/'/g, "''")}'`;
  return cells.filter((c) => c.want !== null).map((c) => ({ range: `${q}!${c.a1}:${c.a1}`, values: [[c.want as number]] }));
}
export function sppClearRanges(cells: readonly SppCell[], sheetName: string): string[] {
  const q = `'${sheetName.replace(/'/g, "''")}'`;
  return cells.filter((c) => c.want === null).map((c) => `${q}!${c.a1}:${c.a1}`);
}

/* ─────────────────────────── манифест отката ─────────────────────────── */

export const SPP_MANIFEST_KIND = 'unitka-spp/ab-migration';
export const SPP_MANIFEST_VERSION = 1;

export interface SppManifestCell {
  a1: string; row: number; col: number; month: string; date: string; nmId: number;
  previousValue: CellValue; previousFormula: CellValue; numberFormat: { type?: string; pattern?: string } | null;
  newValue: number | null; cls: SppClass;
}
export interface SppManifest {
  kind: typeof SPP_MANIFEST_KIND; version: number;
  spreadsheetId: string; sheetId: number; sheetName: string;
  candidate: string; window: { from: string; to: string };
  cells: SppManifestCell[];
  digest: string;
}

const sha = (v: unknown): string => createHash('sha256').update(canonicalJson(v)).digest('hex');
const digestBody = (m: Omit<SppManifest, 'digest'>): string => sha(m);

/** Неизменяемый снимок всех ячеек AB, которые план изменит: прежнее значение, формула, формат. */
export function buildSppManifest(plan: SppPlan, a: { spreadsheetId: string; sheetId: number; sheetName: string }): SppManifest {
  const body: Omit<SppManifest, 'digest'> = {
    kind: SPP_MANIFEST_KIND, version: SPP_MANIFEST_VERSION, spreadsheetId: a.spreadsheetId, sheetId: a.sheetId, sheetName: a.sheetName,
    candidate: plan.candidate, window: plan.window,
    cells: plan.cells.map((c) => ({ a1: c.a1, row: c.row, col: c.col, month: c.month, date: c.date, nmId: c.nmId,
      previousValue: c.before ?? '', previousFormula: c.beforeFormula ?? '', numberFormat: c.numberFormat, newValue: c.want, cls: c.cls })),
  };
  return { ...body, digest: digestBody(body) };
}

/** gzip + base64url с префиксом «gz.»: манифест сотен ячеек обязан помещаться в переменную окружения Job. */
export function encodeSppManifest(m: SppManifest): string {
  return `gz.${gzipSync(Buffer.from(canonicalJson(m), 'utf8')).toString('base64url')}`;
}

export function parseSppManifest(raw: string): SppManifest | { error: string } {
  const text = raw.trim();
  if (!text) return { error: 'манифест отката AB не передан' };
  let obj: unknown;
  try {
    const json = text.startsWith('{') ? text
      : text.startsWith('gz.') ? gunzipSync(Buffer.from(text.slice(3), 'base64url')).toString('utf8')
        : Buffer.from(text, 'base64url').toString('utf8');
    obj = JSON.parse(json);
  } catch { return { error: 'манифест отката AB не разбирается' }; }
  const m = obj as Partial<SppManifest> | null;
  if (!m || typeof m !== 'object' || m.kind !== SPP_MANIFEST_KIND || m.version !== SPP_MANIFEST_VERSION) return { error: 'вид/версия манифеста отката AB' };
  if (typeof m.spreadsheetId !== 'string' || typeof m.sheetName !== 'string' || !Number.isInteger(m.sheetId) || !Array.isArray(m.cells) || typeof m.digest !== 'string') {
    return { error: 'в манифесте отката AB нет обязательных полей' };
  }
  const { digest, ...body } = m as SppManifest;
  if (digestBody(body) !== digest) return { error: 'отпечаток манифеста отката AB не совпадает с содержимым — изменён или обрезан' };
  for (const c of m.cells) {
    if (c.date < SPP_START || !/^[A-Z]+\d+$/.test(c.a1)) return { error: `манифест: ячейка ${c.a1} ${c.date} вне контракта AB` };
    if (c.col < 1 || (c.col - 13 - OFFSET.spp) % 24 !== 0) return { error: `манифест: ${c.a1} — не колонка СПП блока` };
  }
  return m as SppManifest;
}

/* ─────────────────────────── перечитывание ─────────────────────────── */

/**
 * Каждая записанная ячейка AB после записи: значение = плану, формат числа = прежнему. snapFor — перечитанный
 * снимок секции месяца ячейки. Провал → проверка QA FAIL → коммит LCD не начинается.
 */
export function verifySppReadback(cells: readonly SppCell[], snapFor: (month: string) => Snapshot | undefined): QaCheck {
  const bad: string[] = [];
  for (const c of cells) {
    const s = snapFor(c.month);
    if (!s) { bad.push(`${c.a1}: секция ${c.month} не перечитана`); continue; }
    const v = cellAt(s, c.row, c.col);
    if (!sppEqual(v, c.want)) bad.push(`${c.a1} ${c.date}: в листе ${String(v)}, план ${c.want === null ? 'пусто' : c.want}`);
    const f = formatAt(s, c.row, c.col).numberFormat;
    const now = f ? { type: f.type, pattern: f.pattern } : null;
    if (JSON.stringify(now) !== JSON.stringify(c.numberFormat)) bad.push(`${c.a1}: формат числа ${JSON.stringify(c.numberFormat)} → ${JSON.stringify(now)}`);
  }
  return { name: 'SPP_READBACK', pass: bad.length === 0, count: bad.length, sample: bad.slice(0, 8) };
}

export function sppSummary(plan: SppPlan, mode: SppMode, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    mode, window_from: plan.window.from, window_to: plan.window.to, candidate: plan.candidate, ...plan.counts,
    rows_without_block: plan.rowsWithoutBlock.length, rows_without_block_sample: plan.rowsWithoutBlock.slice(0, 10),
    sample: plan.cells.slice(0, 40).map((c) => `${c.a1} ${c.date} ${c.nmId} ${String(c.before ?? '')}→${c.want === null ? '' : c.want} ${c.cls}`),
    ...extra,
  };
}
