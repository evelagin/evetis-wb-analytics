/**
 * Синтетический September Master для тестов Engine: 24 блока, 30 дней, формулы на месте,
 * сводка = Σ блоков. Строится из «истины» BigQuery, чтобы проверять идемпотентность
 * (лист == BQ → пустой план) и все ветки fail-closed.
 */
import { GRID, OFFSET, FACT_KEYS, CALC_OFFSETS, SUMMARY, SUMMARY_TO_OFFSET, FORMAT_CONTRACT_KEYS, isoToSerial, addDaysIso, dayRow, type CellValue, type CellFormat } from '../src/loaders/unitka/model.js';
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

export const BLACK = { red: 0, green: 0, blue: 0 };
export const DIM = { red: 0.718, green: 0.718, blue: 0.718 }; // #b7b7b7
export const AUTO_BG = { red: 0.945, green: 0.973, blue: 0.957 }; // #f1f8f4
export const NF_INT = { type: 'NUMBER', pattern: '#,##0' };
export const NF_RUB = { type: 'NUMBER', pattern: '#,##0 "₽"' };

/** Эталонный формат закрытого дня по ключу колонки (как в Master). */
export function refFormat(key: (typeof FORMAT_CONTRACT_KEYS)[number]): CellFormat {
  if (key === 'logistics' || key === 'commission') return { bg: null, fg: BLACK, numberFormat: key === 'commission' ? { type: 'PERCENT', pattern: '0.0%' } : NF_RUB };
  if (key === 'storage' || key === 'adsIn' || key === 'price') return { bg: AUTO_BG, fg: BLACK, numberFormat: NF_RUB };
  return { bg: AUTO_BG, fg: BLACK, numberFormat: NF_INT };
}
/** «Будущий» статический вид Master: ставки серым, хранение без заливки и формата. */
export function futureFormat(key: (typeof FORMAT_CONTRACT_KEYS)[number]): CellFormat {
  if (key === 'logistics' || key === 'commission') return { ...refFormat(key), fg: DIM };
  if (key === 'storage') return { bg: null, fg: null, numberFormat: null };
  return refFormat(key);
}

export interface FixtureOpts {
  lcdInSheet?: string;          // дата в зеркале и имени (по умолчанию = LCD)
  applyFacts?: boolean;         // факт уже в листе (идемпотентность)
  direct?: (nm: number) => number;
  commission?: (nm: number) => number;
  reverse?: number;
  /** С какого индекса дня (0-based) строки несут «будущий» статический вид (как в Master: 9 = 10.09). */
  futureStyleFrom?: number;
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

  const formats: CellFormat[][] = Array.from({ length: GRID.DAYS }, () => Array<CellFormat>(GRID.NC).fill({ bg: null, fg: null, numberFormat: null }));
  NM_IDS.forEach((_, b) => {
    const st = GRID.B0 + b * GRID.BW;
    for (let i = 0; i < GRID.DAYS; i++) {
      for (const k of FORMAT_CONTRACT_KEYS) {
        formats[i]![st - 1 + OFFSET[k]] = opts.futureStyleFrom !== undefined && i >= opts.futureStyleFrom ? futureFormat(k) : refFormat(k);
      }
    }
  });
  const snap: Snapshot = {
    grid, formulas, formats, sheetId: 739487431,
    mirrorLcd: isoToSerial(lcdSheet), mirrorRev: opts.reverse ?? 32.5256, namedLcd: isoToSerial(lcdSheet),
  };
  opts.mutate?.(snap);
  return snap;
}
