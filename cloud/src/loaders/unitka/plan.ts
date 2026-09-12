/**
 * UNITKA ENGINE v1 — preflight и план записи. Чистый модуль: снимок листа + данные
 * подготовленного слоя → список ячеек, которые ДОЛЖНЫ измениться. Ни Sheets, ни BigQuery.
 *
 * Идемпотентность: единственный ключ — значение. Ячейка попадает в план тогда и только
 * тогда, когда `лист ≠ BigQuery` (факт — допуск 0.005, пусто ≠ 0; ставки — 1e-9).
 * Повтор прогона на тех же источниках даёт пустой план.
 *
 * Fail-closed: любая проверка ниже бросает LoaderError с кодом — до записи.
 */
import { LoaderError } from '../../errors.js';
import type { CommissionRateRow, FactRow, LcdRow, LogisticsRateRow } from './bq.js';
import {
  GRID, OFFSET, FACT_KEYS, CALC_OFFSETS, SUMMARY, NAMED, FORMAT_CONTRACT_KEYS, FORMAT_REF_ROW, EMPTY_FORMAT,
  type Block, type CellValue, type FactKey, type CellFormat, type FormatKey,
  formatEqual, formatDescr,
  findBlocks, colA1, isoToSerial, serialToIso, addDaysIso, monthStartIso, dayRow,
  isEmpty, asNumber, factEqual, rateEqual, round2, round6,
} from './model.js';

/** Снимок листа, как его читает index.ts (строки TOP..MTD, формулы FIRST..FIRST+DAYS-1). */
export interface Snapshot {
  grid: CellValue[][];      // строки GRID.TOP..GRID.MTD (33), колонки 1..NC; строки могут быть «рваными»
  formulas: CellValue[][];  // строки GRID.FIRST..FIRST+DAYS-1, valueRenderOption=FORMULA
  mirrorLcd: CellValue;     // WB736
  mirrorRev: CellValue;     // WB737
  namedLcd: CellValue;      // именованный диапазон LAST_CLOSED_DATE
  /** Статические форматы строк GRID.FIRST..FIRST+DAYS-1 (userEnteredFormat, без УФ). */
  formats: CellFormat[][];
  sheetId: number;
}

export function formatAt(snap: Snapshot, row: number, col: number): CellFormat {
  const r = snap.formats[row - GRID.FIRST];
  const v = r ? r[col - 1] : undefined;
  return v ?? EMPTY_FORMAT;
}

export function cellAt(snap: Snapshot, row: number, col: number): CellValue {
  const r = snap.grid[row - GRID.TOP];
  const v = r ? r[col - 1] : undefined;
  return v === undefined ? null : v;
}
/** Текущее значение ячейки контракта: сетка, зеркала WB736/WB737 или именованный диапазон. */
export function currentValue(snap: Snapshot, e: { row: number; col: number; namedRange?: string }): CellValue {
  if (e.namedRange) return snap.namedLcd;
  if (e.col === GRID.MIR) {
    if (e.row === GRID.HDR) return snap.mirrorLcd;
    if (e.row === GRID.RROW) return snap.mirrorRev;
    return null;
  }
  return cellAt(snap, e.row, e.col);
}
export function formulaAt(snap: Snapshot, row: number, col: number): CellValue {
  const r = snap.formulas[row - GRID.FIRST];
  const v = r ? r[col - 1] : undefined;
  return v === undefined ? null : v;
}
export function isFormula(v: CellValue): boolean {
  return typeof v === 'string' && v.startsWith('=');
}

export type CellKind = 'fact' | 'logistics' | 'commission' | 'reverse' | 'lcd';

/**
 * Классификация изменения (решение владельца 12.09, Stage E2) — чтобы QA и аудит не смешивали
 * типы правок:
 *   FACT_CHANGE             — факт нового закрытого дня (дата > LCD книги);
 *   LATE_SOURCE_CORRECTION  — источник пересчитал уже закрытый в книге день;
 *   MODEL_PARAMETER_REFRESH — ставки rolling-окна (логистика, комиссия, обратное плечо);
 *   LCD_ADVANCE             — продвижение LAST_CLOSED_DATE;
 *   NO_CHANGE               — значение то же, меняется только форма (формула-наследие → значение).
 */
export type ChangeType = 'FACT_CHANGE' | 'LATE_SOURCE_CORRECTION' | 'MODEL_PARAMETER_REFRESH' | 'LCD_ADVANCE' | 'NO_CHANGE';

export interface ExpectedCell {
  row: number;
  col: number;
  want: number | null; // null = должна быть пустой
  kind: CellKind;
  nmId?: number;
  key?: string;
  /** Дата дня (факт) — YYYY-MM-DD. */
  date?: string;
  /** Источник значения (имя вью/таблицы или метка источника воронки). */
  source?: string;
  /** Именованный диапазон вместо A1 (только для LAST_CLOSED_DATE). */
  namedRange?: string;
  /** Допуск сравнения: 'fact' → 0.005 и пусто≠0; иначе 1e-9. */
}

export interface PlannedCell extends ExpectedCell {
  before: CellValue;
  changeType: ChangeType;
  reason: string;
}

/** Ячейка закрытого дня, чей статический формат отличается от эталона строки 737. */
export interface FormatCell {
  row: number;
  col: number;
  nmId: number;
  key: FormatKey;
  date: string;
  before: CellFormat;
  want: CellFormat;
}

export interface RateLine {
  nmId: number;
  n: number;
  direct: number;
  directSource: 'own' | 'store';
  commission: number;
  commissionSource: 'own' | 'store';
}

export interface Plan {
  lcd: string;
  d1Msk: string;
  lagDays: number;
  monthStart: string;
  closedDays: number;         // дней месяца ≤ LCD
  blocks: Block[];
  expected: ExpectedCell[];   // полный контракт «лист == BQ» (для post-write)
  cells: PlannedCell[];       // подмножество expected, где лист ≠ BQ
  rates: RateLine[];
  reverseRate: number;
  invariant: { shipments: number; sales: number; refusals: number; pass: boolean };
  sourcesByDay: Record<string, Record<string, number>>;
  gaps: Record<string, number>;
  legacy: LegacyFormulas;
  /** Ячейки плана, где вместо значения стояла формула-наследие (входят в cells). */
  legacyReplaced: number;
  /** LAST_CLOSED_DATE книги до прогона (зеркало WB736). */
  bookLcd: string;
  byChangeType: Record<ChangeType, number>;
  /** Будущие дни: формулы-проекции остатка (KEEP, решение владельца) — informational. */
  stockProjectionCells: number;
  /** Контракт формата закрытого дня: ячейки, которые нужно привести к эталону (FORMAT_CHANGE). */
  formatCells: FormatCell[];
  /** Сколько ячеек закрытых дней под контрактом формата всего (для отчёта). */
  formatContractCells: number;
}

export interface PlanInputs {
  snapshot: Snapshot;
  lcd: LcdRow;
  facts: FactRow[];
  logistics: LogisticsRateRow[];
  commission: CommissionRateRow[];
  minN: number;       // порог владельца: своя ставка при n >= 10
  maxLagDays: number; // SOURCE_STALE, если D-1 − LCD > maxLagDays
}

/* ───────────────────────── preflight ───────────────────────── */

/**
 * Проверка структуры Master ДО вычислений. Возвращает список нарушений (пусто = PASS).
 * Отдельно: месяц LCD ≠ месяц блоков → MONTH_ROLLOVER_REQUIRED (E1.1, не реализуется).
 */
export interface LegacyFormulas { closed: number; future: number; byKey: Record<string, number> }

export function preflight(snap: Snapshot, lcdIso: string): { issues: string[]; blocks: Block[]; monthMismatch: boolean; legacy: LegacyFormulas } {
  const issues: string[] = [];
  const top = snap.grid[0] ?? [];
  const blocks = findBlocks(top);
  if (blocks.length !== GRID.NB) issues.push(`BLOCKS = ${blocks.length}/${GRID.NB}: не все блоки имеют nmID в строке ${GRID.TOP}`);
  const seen = new Set<number>();
  for (const b of blocks) {
    if (seen.has(b.nmId)) issues.push(`nmID ${b.nmId} встречается в двух блоках`);
    seen.add(b.nmId);
  }
  if (!Number.isFinite(asNumber(snap.namedLcd))) issues.push(`именованный диапазон ${NAMED.LCD} пуст или не дата`);
  if (!Number.isFinite(asNumber(snap.mirrorLcd))) issues.push(`зеркало LAST_CLOSED_DATE (${colA1(GRID.MIR)}${GRID.HDR}) пусто или не дата`);

  const monthStart = monthStartIso(lcdIso);
  let monthMismatch = false;
  const first = blocks[0];
  if (first) {
    const d0 = asNumber(cellAt(snap, GRID.FIRST, first.start));
    if (Number.isFinite(d0) && serialToIso(d0).slice(0, 7) !== lcdIso.slice(0, 7)) monthMismatch = true;
  }
  if (!monthMismatch) {
    for (let i = 0; i < GRID.DAYS; i++) {
      const want = isoToSerial(addDaysIso(monthStart, i));
      const sum = asNumber(cellAt(snap, dayRow(i), SUMMARY.date));
      if (sum !== want) issues.push(`сводка B${dayRow(i)}: ожидалась дата ${addDaysIso(monthStart, i)}`);
      for (const b of blocks) {
        const got = asNumber(cellAt(snap, dayRow(i), b.start + OFFSET.date));
        if (got !== want) { issues.push(`блок #${b.index + 1} ${b.nmId}: дата в строке ${dayRow(i)} ≠ ${addDaysIso(monthStart, i)}`); break; }
      }
    }
  }

  // Формулы: там, где они должны быть — есть. Формулы В факт/ставочных ячейках — не дрейф,
  // а наследие (в Master остались `=stock*0.15` и `=prev-orders+cancels` в колонках остатков
  // и хранения; s82data перезаписывал их значениями, когда значения расходились). Engine
  // ведёт их учёт (legacyFormulas) и в закрытых днях заменяет значением из BigQuery.
  const legacy = { closed: 0, future: 0, byKey: {} as Record<string, number> };
  const closedDays = daysBetween(monthStart, lcdIso) + 1;
  for (const b of blocks) {
    let missing = 0;
    for (let i = 0; i < GRID.DAYS; i++) {
      for (const o of CALC_OFFSETS) if (!isFormula(formulaAt(snap, dayRow(i), b.start + o))) missing++;
      for (const k of [...FACT_KEYS, 'logistics', 'commission'] as const) {
        if (isFormula(formulaAt(snap, dayRow(i), b.start + OFFSET[k]))) {
          if (i < closedDays) legacy.closed++; else legacy.future++;
          legacy.byKey[k] = (legacy.byKey[k] ?? 0) + 1;
        }
      }
    }
    if (missing) issues.push(`блок #${b.index + 1} ${b.nmId}: ${missing} расчётных ячеек без формулы`);
  }
  let sumMissing = 0;
  for (let i = 0; i < GRID.DAYS; i++) {
    for (let c = SUMMARY.bloggers; c <= SUMMARY.drr; c++) if (!isFormula(formulaAt(snap, dayRow(i), c))) sumMissing++;
  }
  if (sumMissing) issues.push(`сводка C..K: ${sumMissing} ячеек без формулы`);
  return { issues, blocks, monthMismatch, legacy };
}

/* ───────────────────────── план ───────────────────────── */

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
}

/** Источник факта по ключу (для diff plan / журнала). */
function factSource(k: FactKey, f: FactRow): string {
  switch (k) {
    case 'views': case 'adsIn': return 'MART_SKU_DAILY';
    case 'opens': case 'carts': case 'orders': return f.ordersSource;
    case 'cancels': return f.cancelsSource;
    case 'stock': return 'FACT_STOCKS_SNAPSHOT';
    case 'price': return 'FACT_ORDERS';
    case 'storage': return 'RAW_WB_PAID_STORAGE';
  }
}

const FACT_FIELD: Record<FactKey, keyof FactRow> = {
  views: 'views', opens: 'opens', carts: 'carts', orders: 'orders', cancels: 'cancels',
  stock: 'stock', adsIn: 'adsIn', price: 'price', storage: 'storage',
};

export function buildPlan(inp: PlanInputs): Plan {
  const { snapshot: snap, facts, minN } = inp;
  const lcd = inp.lcd.lastClosedDate;
  const d1 = inp.lcd.d1Msk;
  const lagDays = daysBetween(lcd, d1);
  if (lagDays > inp.maxLagDays) {
    throw new LoaderError(`LAST_CLOSED_DATE = ${lcd} отстаёт от D-1 (${d1}) на ${lagDays} дн. > ${inp.maxLagDays}`, 'SOURCE_STALE');
  }
  const mirrorSerial = asNumber(snap.mirrorLcd);
  const namedSerial = asNumber(snap.namedLcd);
  const lcdSerial = isoToSerial(lcd);
  const prevSerial = Math.max(Number.isFinite(mirrorSerial) ? mirrorSerial : 0, Number.isFinite(namedSerial) ? namedSerial : 0);
  if (lcdSerial < prevSerial) {
    throw new LoaderError(`LAST_CLOSED_DATE из BigQuery ${lcd} раньше, чем в книге ${serialToIso(prevSerial)}`, 'LCD_REGRESSION');
  }

  const pre = preflight(snap, lcd);
  if (pre.monthMismatch) {
    throw new LoaderError(`месяц LAST_CLOSED_DATE (${lcd.slice(0, 7)}) не совпадает с месяцем September Master — rollover не реализован (Engine v1.1)`, 'MONTH_ROLLOVER_REQUIRED');
  }
  if (pre.issues.length) throw new LoaderError(pre.issues.slice(0, 12).join(' | '), 'STRUCTURE_DRIFT');
  const blocks = pre.blocks;
  const monthStart = monthStartIso(lcd);
  const closedDays = daysBetween(monthStart, lcd) + 1;

  // NO DUPLICATE date×nmID + индекс фактов
  const byKey = new Map<string, FactRow>();
  const sourcesByDay: Record<string, Record<string, number>> = {};
  for (const f of facts) {
    const k = `${f.nmId}|${f.date}`;
    if (byKey.has(k)) throw new LoaderError(`дубль nmID×дата в V_UNITKA_DAILY_FACT: ${k}`, 'DUP_KEY');
    byKey.set(k, f);
    if (f.date > lcd) throw new LoaderError(`V_UNITKA_DAILY_FACT отдал дату ${f.date} > LAST_CLOSED_DATE ${lcd}`, 'FUTURE_LEAKAGE');
    const d = (sourcesByDay[f.date] ??= {});
    d[f.ordersSource] = (d[f.ordersSource] ?? 0) + 1;
  }

  // Ставки: инвариант популяции ДО всего остального (fail-closed).
  const store = inp.logistics.find((r) => r.nmId === 0);
  if (!store || store.shipments <= 0 || store.directRate === null || store.reverseRate === null) {
    throw new LoaderError('V_UNITKA_LOGISTICS_RATES: нет строки магазина или окно пусто', 'INVARIANT_FAIL');
  }
  const invariant = {
    shipments: store.shipments, sales: store.sales ?? -1, refusals: store.refusals,
    pass: store.shipments === (store.sales ?? -1) + store.refusals,
  };
  if (!invariant.pass) {
    throw new LoaderError(`DIRECT SHIPMENTS ≠ SALES + CANCELLED: ${store.shipments} ≠ ${store.sales} + ${store.refusals}`, 'INVARIANT_FAIL');
  }
  const cstore = inp.commission.find((r) => r.nmId === 0);
  if (!cstore || cstore.sales <= 0 || cstore.commissionRate === null || !(cstore.commissionRate > 0)) {
    throw new LoaderError('V_UNITKA_COMMISSION_RATES: нет строки магазина или окно пусто', 'INVARIANT_FAIL');
  }
  const logByNm = new Map(inp.logistics.filter((r) => r.nmId !== 0).map((r) => [r.nmId, r]));
  const comByNm = new Map(inp.commission.filter((r) => r.nmId !== 0).map((r) => [r.nmId, r]));

  const expected: ExpectedCell[] = [];
  const rates: RateLine[] = [];
  const gaps: Record<string, number> = { opens: 0, carts: 0, stock: 0, price: 0, storage: 0 };

  for (const b of blocks) {
    // BLOCKS = 24/24 означает и «у каждого блока есть строки в подготовленном слое».
    let rowsForNm = 0;
    for (let i = 0; i < closedDays; i++) if (byKey.has(`${b.nmId}|${addDaysIso(monthStart, i)}`)) rowsForNm++;
    if (rowsForNm !== closedDays) {
      throw new LoaderError(`блок #${b.index + 1} ${b.nmId}: в V_UNITKA_DAILY_FACT ${rowsForNm} дней из ${closedDays} (SKU не активен в REF_SKU_MASTER?)`, 'BLOCK_MISSING');
    }
    for (let i = 0; i < closedDays; i++) {
      const f = byKey.get(`${b.nmId}|${addDaysIso(monthStart, i)}`)!;
      for (const k of FACT_KEYS) {
        const raw = f[FACT_FIELD[k]];
        const want = typeof raw === 'number' ? raw : null;
        if (want === null && k in gaps) gaps[k] = (gaps[k] ?? 0) + 1;
        expected.push({ row: dayRow(i), col: b.start + OFFSET[k], want, kind: 'fact', nmId: b.nmId, key: k, date: f.date, source: factSource(k, f) });
      }
    }
    // Ставки — константа на все 30 строк блока (как s8brates/s8rates).
    const p = logByNm.get(b.nmId);
    const ownD = !!(p && p.shipments >= minN && p.directRate !== null && p.directRate > 0);
    const direct = round2(ownD ? (p!.directRate as number) : store.directRate);
    const c = comByNm.get(b.nmId);
    const ownC = !!(c && c.sales >= minN && c.logisticsPerUnit !== null && c.logisticsPerUnit > 0 && c.commissionRate !== null && c.commissionRate > 0);
    const comm = round6(ownC ? (c!.commissionRate as number) : cstore.commissionRate);
    rates.push({ nmId: b.nmId, n: p?.shipments ?? 0, direct, directSource: ownD ? 'own' : 'store', commission: comm, commissionSource: ownC ? 'own' : 'store' });
    for (let i = 0; i < GRID.DAYS; i++) {
      expected.push({ row: dayRow(i), col: b.start + OFFSET.logistics, want: direct, kind: 'logistics', nmId: b.nmId, key: 'logistics', source: `V_UNITKA_LOGISTICS_RATES ${store.windowFrom}..${store.windowTo} (${ownD ? 'своя' : 'магазин'})` });
      expected.push({ row: dayRow(i), col: b.start + OFFSET.commission, want: comm, kind: 'commission', nmId: b.nmId, key: 'commission', source: `V_UNITKA_COMMISSION_RATES ${cstore.windowFrom}..${cstore.windowTo} (${ownC ? 'своя' : 'магазин'})` });
    }
  }
  const reverseRate = store.reverseRate;
  expected.push({ row: GRID.RROW, col: GRID.MIR, want: reverseRate, kind: 'reverse', key: 'reverse', source: `V_UNITKA_LOGISTICS_RATES ${store.windowFrom}..${store.windowTo} (магазин)` });
  expected.push({ row: GRID.HDR, col: GRID.MIR, want: lcdSerial, kind: 'lcd', key: 'lcd_mirror', date: lcd, source: 'V_UNITKA_LAST_CLOSED_DATE' });
  expected.push({ row: 0, col: 0, want: lcdSerial, kind: 'lcd', key: 'lcd_named', namedRange: NAMED.LCD, date: lcd, source: 'V_UNITKA_LAST_CLOSED_DATE' });

  // FUTURE LEAKAGE в самой книге: факт-ячейки за датами > LCD должны быть пусты.
  // Решение владельца (E2, KEEP): формульная проекция остатка в будущих днях — visual planning
  // layer, Engine её не трогает и утечкой не считает. Всё остальное непустое (в т.ч. фактическое
  // хранение в будущем дне, от формулы или нет) — FUTURE_LEAKAGE.
  const leaks: string[] = [];
  let stockProjectionCells = 0;
  for (const b of blocks) {
    for (let i = closedDays; i < GRID.DAYS; i++) {
      for (const k of FACT_KEYS) {
        const v = cellAt(snap, dayRow(i), b.start + OFFSET[k]);
        if (isEmpty(v)) continue;
        if (k === 'stock' && isFormula(formulaAt(snap, dayRow(i), b.start + OFFSET[k]))) { stockProjectionCells++; continue; }
        leaks.push(`${colA1(b.start + OFFSET[k])}${dayRow(i)}`);
      }
    }
  }
  if (leaks.length) throw new LoaderError(`факт за датами > ${lcd} уже заполнен: ${leaks.slice(0, 10).join(', ')} (всего ${leaks.length})`, 'FUTURE_LEAKAGE');

  // План = ожидание минус то, что уже стоит в листе.
  const cells: PlannedCell[] = [];
  let legacyReplaced = 0;
  const bookLcd = serialToIso(prevSerial);
  const byChangeType: Record<ChangeType, number> = { FACT_CHANGE: 0, LATE_SOURCE_CORRECTION: 0, MODEL_PARAMETER_REFRESH: 0, LCD_ADVANCE: 0, NO_CHANGE: 0 };
  for (const e of expected) {
    const before = currentValue(snap, e);
    const valueSame = e.kind === 'fact' ? factEqual(before, e.want) : rateEqual(before, e.want as number);
    const wasFormula = !e.namedRange && e.col !== GRID.MIR && isFormula(formulaAt(snap, e.row, e.col));
    // Формула в ячейке контракта (закрытый день / ставка) заменяется значением даже при
    // совпадении результата: после первой записи контракт «факт = значение» становится полным.
    if (valueSame && !wasFormula) continue;
    if (valueSame && wasFormula) legacyReplaced++;
    let changeType: ChangeType;
    let reason: string;
    if (e.kind === 'lcd') { changeType = 'LCD_ADVANCE'; reason = `LAST_CLOSED_DATE ${bookLcd} → ${lcd}`; }
    else if (e.kind !== 'fact') { changeType = 'MODEL_PARAMETER_REFRESH'; reason = `ставка rolling-окна пересчитана (${e.source ?? ''})`; }
    else if (valueSame) { changeType = 'NO_CHANGE'; reason = 'формула-наследие → значение, результат тот же'; }
    else if ((e.date ?? '') > bookLcd) { changeType = 'FACT_CHANGE'; reason = `новый закрытый день ${e.date} (${e.source ?? ''})`; }
    else {
      changeType = 'LATE_SOURCE_CORRECTION';
      reason = wasFormula
        ? `день ${e.date} закрыт в книге ${bookLcd}; в ячейке стояла формула-наследие, источник ${e.source ?? ''} даёт ${e.want === null ? 'GAP (пусто)' : e.want}`
        : `день ${e.date} закрыт в книге ${bookLcd}; источник ${e.source ?? ''} пересчитал значение задним числом`;
    }
    byChangeType[changeType]++;
    cells.push({ ...e, before, changeType, reason });
  }
  const fmt = formatContract(snap, blocks, closedDays, monthStart, expected);
  return {
    lcd, d1Msk: d1, lagDays, monthStart, closedDays, blocks, expected, cells, rates, reverseRate, invariant, sourcesByDay, gaps,
    legacy: pre.legacy, legacyReplaced, bookLcd, byChangeType, stockProjectionCells,
    formatCells: fmt.cells, formatContractCells: fmt.total,
  };
}

/* ───────────────────────── запись ───────────────────────── */

export interface WriteRangeSpec { range: string; values: (number | string)[][] }

/** Группировка плана в вертикальные отрезки колонок — один values.batchUpdate. */
export function toWriteRanges(cells: readonly PlannedCell[], sheetName: string): WriteRangeSpec[] {
  const out: WriteRangeSpec[] = [];
  const q = `'${sheetName.replace(/'/g, "''")}'`;
  const byCol = new Map<number, PlannedCell[]>();
  for (const c of cells) {
    if (c.namedRange) { out.push({ range: c.namedRange, values: [[c.want as number]] }); continue; }
    (byCol.get(c.col) ?? byCol.set(c.col, []).get(c.col)!).push(c);
  }
  for (const [col, list] of [...byCol.entries()].sort((a, b) => a[0] - b[0])) {
    list.sort((a, b) => a.row - b.row);
    let run: PlannedCell[] = [];
    const flush = () => {
      if (!run.length) return;
      const r1 = run[0]!.row, r2 = run[run.length - 1]!.row;
      out.push({ range: `${q}!${colA1(col)}${r1}:${colA1(col)}${r2}`, values: run.map((c) => [c.want === null ? '' : c.want]) });
      run = [];
    };
    for (const c of list) {
      if (run.length && c.row !== run[run.length - 1]!.row + 1) flush();
      run.push(c);
    }
    flush();
  }
  return out;
}

/** Строка diff plan для отчёта/аудита (DATE · SKU · CELL · OLD · NEW · CHANGE_TYPE · SOURCE · REASON). */
export interface DiffRow {
  DATE: string; SKU: string; CELL: string; METRIC: string; OLD: string; NEW: string;
  CHANGE_TYPE: ChangeType; SOURCE: string; REASON: string;
}

export function diffRows(plan: Plan): DiffRow[] {
  const fmt = (v: CellValue | number | null | undefined): string => (v === null || v === undefined || v === '' ? '' : String(v));
  return plan.cells.map((c) => ({
    DATE: c.date ?? (c.kind === 'fact' ? '' : `${plan.monthStart.slice(0, 7)} (все 30 строк)`),
    SKU: c.nmId === undefined ? 'магазин' : String(c.nmId),
    CELL: c.namedRange ?? `${colA1(c.col)}${c.row}`,
    METRIC: c.key ?? c.kind,
    OLD: fmt(c.before),
    NEW: c.kind === 'lcd' && typeof c.want === 'number' ? serialToIso(c.want) : fmt(c.want),
    CHANGE_TYPE: c.changeType,
    SOURCE: c.source ?? '',
    REASON: c.reason,
  }));
}

/* ───────────────────────── формат закрытого дня ───────────────────────── */

/**
 * Контракт формата закрытого дня (см. model.ts, FORMAT_CONTRACT_KEYS): для строк с датой ≤ LCD
 * статический формат ячейки в колонках Engine должен равняться формату эталонной строки 737
 * того же блока. Строки > LCD не рассматриваются вовсе — будущее Engine не форматирует.
 *
 * GAP-ячейки (факт, которого нет в источнике: want = null) в контракт НЕ входят: у Master
 * есть собственная разметка пропусков (например, оранжевый фон остатков 03.09), и Engine её
 * не переопределяет. Формат следует за значением: как только источник даёт факт, ячейка
 * получает значение и формат закрытого дня в одном прогоне. Набор «ячейка будет со значением»
 * берётся из ожидания (expected) — одинаково до и после записи, поэтому post-write QA сходится.
 */
export function formatContract(snap: Snapshot, blocks: readonly Block[], closedDays: number, monthStart: string, expected: readonly ExpectedCell[]): { cells: FormatCell[]; total: number } {
  const cells: FormatCell[] = [];
  let total = 0;
  const valued = new Set<string>();
  for (const e of expected) if (!e.namedRange && e.col !== GRID.MIR && e.want !== null) valued.add(`${e.row}|${e.col}`);
  for (const b of blocks) {
    for (const k of FORMAT_CONTRACT_KEYS) {
      const col = b.start + OFFSET[k];
      const want = formatAt(snap, FORMAT_REF_ROW, col);
      for (let i = 0; i < closedDays; i++) {
        const row = dayRow(i);
        if (row === FORMAT_REF_ROW) continue;
        if (!valued.has(`${row}|${col}`)) continue; // GAP — разметка пропуска остаётся за Master
        total++;
        const before = formatAt(snap, row, col);
        if (!formatEqual(before, want)) cells.push({ row, col, nmId: b.nmId, key: k, date: addDaysIso(monthStart, i), before, want });
      }
    }
  }
  return { cells, total };
}

/** Группировка формат-ячеек в вертикальные отрезки (0-based, конец исключительно) для repeatCell. */
export function toFormatWrites(cells: readonly FormatCell[]): Array<{ startRow: number; endRow: number; startCol: number; endCol: number; format: CellFormat }> {
  const out: Array<{ startRow: number; endRow: number; startCol: number; endCol: number; format: CellFormat }> = [];
  const byCol = new Map<number, FormatCell[]>();
  for (const c of cells) (byCol.get(c.col) ?? byCol.set(c.col, []).get(c.col)!).push(c);
  for (const [col, list] of [...byCol.entries()].sort((a, b) => a[0] - b[0])) {
    list.sort((a, b) => a.row - b.row);
    let run: FormatCell[] = [];
    const flush = () => {
      if (!run.length) return;
      out.push({ startRow: run[0]!.row - 1, endRow: run[run.length - 1]!.row, startCol: col - 1, endCol: col, format: run[0]!.want });
      run = [];
    };
    for (const c of list) {
      // один repeatCell = один формат; разрыв по строке или по формату
      if (run.length && (c.row !== run[run.length - 1]!.row + 1 || !formatEqual(c.want, run[0]!.want))) flush();
      run.push(c);
    }
    flush();
  }
  return out;
}

export function formatRows(plan: Plan): Array<{ DATE: string; SKU: string; CELL: string; METRIC: string; OLD: string; NEW: string; CHANGE_TYPE: 'FORMAT_CHANGE'; SOURCE: string; REASON: string }> {
  return plan.formatCells.map((c) => ({
    DATE: c.date, SKU: String(c.nmId), CELL: `${colA1(c.col)}${c.row}`, METRIC: c.key,
    OLD: formatDescr(c.before), NEW: formatDescr(c.want), CHANGE_TYPE: 'FORMAT_CHANGE' as const,
    SOURCE: `эталон ${colA1(c.col)}${FORMAT_REF_ROW}`, REASON: `закрытый день ${c.date}: статический формат ≠ эталону строки ${FORMAT_REF_ROW}`,
  }));
}
