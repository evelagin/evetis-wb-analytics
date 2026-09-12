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
  GRID, OFFSET, FACT_KEYS, CALC_OFFSETS, SUMMARY, NAMED,
  type Block, type CellValue, type FactKey,
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

export interface ExpectedCell {
  row: number;
  col: number;
  want: number | null; // null = должна быть пустой
  kind: CellKind;
  nmId?: number;
  key?: string;
  /** Именованный диапазон вместо A1 (только для LAST_CLOSED_DATE). */
  namedRange?: string;
  /** Допуск сравнения: 'fact' → 0.005 и пусто≠0; иначе 1e-9. */
}

export interface PlannedCell extends ExpectedCell {
  before: CellValue;
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
        expected.push({ row: dayRow(i), col: b.start + OFFSET[k], want, kind: 'fact', nmId: b.nmId, key: k });
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
      expected.push({ row: dayRow(i), col: b.start + OFFSET.logistics, want: direct, kind: 'logistics', nmId: b.nmId, key: 'logistics' });
      expected.push({ row: dayRow(i), col: b.start + OFFSET.commission, want: comm, kind: 'commission', nmId: b.nmId, key: 'commission' });
    }
  }
  const reverseRate = store.reverseRate;
  expected.push({ row: GRID.RROW, col: GRID.MIR, want: reverseRate, kind: 'reverse', key: 'reverse' });
  expected.push({ row: GRID.HDR, col: GRID.MIR, want: lcdSerial, kind: 'lcd', key: 'lcd_mirror' });
  expected.push({ row: 0, col: 0, want: lcdSerial, kind: 'lcd', key: 'lcd_named', namedRange: NAMED.LCD });

  // FUTURE LEAKAGE в самой книге: факт-ячейки за датами > LCD должны быть пусты.
  const leaks: string[] = [];
  for (const b of blocks) {
    for (let i = closedDays; i < GRID.DAYS; i++) {
      for (const k of FACT_KEYS) {
        const v = cellAt(snap, dayRow(i), b.start + OFFSET[k]);
        // Формула-наследие в будущем дне (проекция остатка) — не утечка Engine; учитывается в legacy.
        if (!isEmpty(v) && !isFormula(formulaAt(snap, dayRow(i), b.start + OFFSET[k]))) leaks.push(`${colA1(b.start + OFFSET[k])}${dayRow(i)}`);
      }
    }
  }
  if (leaks.length) throw new LoaderError(`факт за датами > ${lcd} уже заполнен: ${leaks.slice(0, 10).join(', ')} (всего ${leaks.length})`, 'FUTURE_LEAKAGE');

  // План = ожидание минус то, что уже стоит в листе.
  const cells: PlannedCell[] = [];
  let legacyReplaced = 0;
  for (const e of expected) {
    const before = currentValue(snap, e);
    let same = e.kind === 'fact' ? factEqual(before, e.want) : rateEqual(before, e.want as number);
    // Формула в ячейке контракта (закрытый день / ставка) заменяется значением даже при
    // совпадении результата: после первой записи контракт «факт = значение» становится полным.
    if (same && !e.namedRange && e.col !== GRID.MIR && isFormula(formulaAt(snap, e.row, e.col))) { same = false; legacyReplaced++; }
    if (!same) cells.push({ ...e, before });
  }
  return { lcd, d1Msk: d1, lagDays, monthStart, closedDays, blocks, expected, cells, rates, reverseRate, invariant, sourcesByDay, gaps, legacy: pre.legacy, legacyReplaced };
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
