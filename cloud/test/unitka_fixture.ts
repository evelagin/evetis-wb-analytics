/**
 * Синтетический September Master для тестов Engine: 24 блока, 30 дней, формулы на месте,
 * сводка = Σ блоков. Строится из «истины» BigQuery, чтобы проверять идемпотентность
 * (лист == BQ → пустой план) и все ветки fail-closed.
 */
import { GRID, OFFSET, FACT_KEYS, CALC_OFFSETS, SUMMARY, SUMMARY_TO_OFFSET, isoToSerial, addDaysIso, dayRow, type CellValue } from '../src/loaders/unitka/model.js';
import type { Snapshot } from '../src/loaders/unitka/plan.js';
import type { FactRow, LogisticsRateRow, CommissionRateRow, LcdRow } from '../src/loaders/unitka/bq.js';

export const MONTH = '2026-09-01';
export const LCD = '2026-09-10';
export const NM_IDS = Array.from({ length: GRID.NB }, (_, i) => 100000000 + i);

export function lcdRow(lcd = LCD, d1 = '2026-09-11'): LcdRow {
  return { lastClosedDate: lcd, d1Msk: d1 };
}

/** Факт: детерминированные числа, у SKU #3 нет остатков (GAP), у всех storage за 10.09 пуст. */
export function facts(lcd = LCD): FactRow[] {
  const out: FactRow[] = [];
  const days = Math.round((Date.parse(lcd) - Date.parse(MONTH)) / 86_400_000) + 1;
  NM_IDS.forEach((nm, b) => {
    for (let i = 0; i < days; i++) {
      const d = addDaysIso(MONTH, i);
      out.push({
        nmId: nm, date: d,
        views: 100 + b + i, opens: 10 + i, carts: 3 + (i % 4), orders: 1 + (i % 3), cancels: i % 2,
        stock: b === 2 ? null : 500 - i, adsIn: 12.34 + i, price: 990.5, storage: i === 9 ? null : 4.56,
        ordersSource: i < 3 ? 'XLSX_BACKFILL' : 'FUNNEL_API', cancelsSource: i < 3 ? 'XLSX_BACKFILL' : 'PROXY_FACT_ORDERS',
      });
    }
  });
  return out;
}

export function logistics(over: Partial<LogisticsRateRow> = {}): LogisticsRateRow[] {
  const rows: LogisticsRateRow[] = [{
    nmId: 0, shipments: 597, directRate: 60.5487, refusals: 52, reverseRate: 32.5256, forwardSum: 36147.59, reverseSum: 1691.33,
    nLogistics: 444, nDelivery: 153, deliveryComponentSum: 9408.77, sales: 545, windowFrom: '2026-08-12', windowTo: LCD, ...over,
  }];
  // SKU #1 — своя ставка (n=226), SKU #2 — ниже порога (n=7)
  rows.push({ nmId: NM_IDS[0]!, shipments: 226, directRate: 65.9829, refusals: 14, reverseRate: 32, forwardSum: 14912.13, reverseSum: 448, nLogistics: 163, nDelivery: 63, deliveryComponentSum: 0, sales: null, windowFrom: '2026-08-12', windowTo: LCD });
  rows.push({ nmId: NM_IDS[1]!, shipments: 7, directRate: 82.74, refusals: 2, reverseRate: 30, forwardSum: 579.21, reverseSum: 60, nLogistics: 4, nDelivery: 3, deliveryComponentSum: 0, sales: null, windowFrom: '2026-08-12', windowTo: LCD });
  return rows;
}

export function commission(): CommissionRateRow[] {
  return [
    { nmId: 0, sales: 545, logisticsPerUnit: 69.4292, commissionRate: 0.45578, windowFrom: '2026-08-12', windowTo: LCD },
    { nmId: NM_IDS[0]!, sales: 212, logisticsPerUnit: 70.35, commissionRate: 0.441234, windowFrom: '2026-08-12', windowTo: LCD },
    { nmId: NM_IDS[1]!, sales: 5, logisticsPerUnit: 80, commissionRate: 0.5, windowFrom: '2026-08-12', windowTo: LCD },
  ];
}

export interface FixtureOpts {
  lcdInSheet?: string;          // дата в зеркале и имени (по умолчанию = LCD)
  applyFacts?: boolean;         // факт уже в листе (идемпотентность)
  direct?: (nm: number) => number;
  commission?: (nm: number) => number;
  reverse?: number;
  mutate?: (snap: Snapshot) => void;
}

/** Снимок как его отдал бы Sheets API для листа, уже совпадающего с BQ. */
export function snapshot(opts: FixtureOpts = {}): Snapshot {
  const lcdSheet = opts.lcdInSheet ?? LCD;
  const closed = Math.round((Date.parse(lcdSheet) - Date.parse(MONTH)) / 86_400_000) + 1;
  const rows = GRID.MTD - GRID.TOP + 1;
  const grid: CellValue[][] = Array.from({ length: rows }, () => Array<CellValue>(GRID.NC).fill(''));
  const formulas: CellValue[][] = Array.from({ length: GRID.DAYS }, () => Array<CellValue>(GRID.NC).fill(''));
  const g = (row: number) => grid[row - GRID.TOP]!;
  const f = (row: number) => formulas[row - GRID.FIRST]!;

  const fx = new Map<string, FactRow>();
  for (const r of facts(lcdSheet)) fx.set(`${r.nmId}|${r.date}`, r);

  NM_IDS.forEach((nm, b) => {
    const st = GRID.B0 + b * GRID.BW;
    g(GRID.TOP)[st - 1] = `#${b + 1} · ${nm} Товар ${b + 1}`;
    for (let i = 0; i < GRID.DAYS; i++) {
      const row = dayRow(i);
      g(row)[st - 1 + OFFSET.date] = isoToSerial(addDaysIso(MONTH, i));
      g(row)[st - 1 + OFFSET.weekday] = 'пн';
      for (const o of CALC_OFFSETS) { f(row)[st - 1 + o] = '=1'; g(row)[st - 1 + o] = i < closed ? 1 : ''; }
      g(row)[st - 1 + OFFSET.logistics] = opts.direct ? opts.direct(nm) : 60.55;
      g(row)[st - 1 + OFFSET.commission] = opts.commission ? opts.commission(nm) : 0.45578;
      if (opts.applyFacts !== false && i < closed) {
        const r = fx.get(`${nm}|${addDaysIso(MONTH, i)}`)!;
        for (const k of FACT_KEYS) {
          const v = r[k];
          g(row)[st - 1 + OFFSET[k]] = v === null ? '' : v;
        }
      }
    }
  });
  // Сводка: даты, формулы, суммы блоков по закрытым дням.
  for (let i = 0; i < GRID.DAYS; i++) {
    const row = dayRow(i);
    g(row)[SUMMARY.date - 1] = isoToSerial(addDaysIso(MONTH, i));
    for (let c = SUMMARY.bloggers; c <= SUMMARY.drr; c++) f(row)[c - 1] = '=1';
    if (i < closed) {
      for (const [sumCol, off] of SUMMARY_TO_OFFSET) {
        let t = 0;
        NM_IDS.forEach((_, b) => { const v = g(row)[GRID.B0 + b * GRID.BW - 1 + off]; if (typeof v === 'number') t += v; });
        g(row)[sumCol - 1] = t;
      }
    }
  }
  let mtd = 0;
  for (let i = 0; i < closed; i++) { const v = g(dayRow(i))[SUMMARY.profit - 1]; if (typeof v === 'number') mtd += v; }
  g(GRID.MTD)[SUMMARY.profit - 1] = mtd;

  const snap: Snapshot = {
    grid, formulas,
    mirrorLcd: isoToSerial(lcdSheet), mirrorRev: opts.reverse ?? 32.5256, namedLcd: isoToSerial(lcdSheet),
  };
  opts.mutate?.(snap);
  return snap;
}
