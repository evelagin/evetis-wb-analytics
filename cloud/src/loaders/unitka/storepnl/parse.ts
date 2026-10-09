/**
 * PHASE C (OWNER ACK 09.10.2026) — снимок компонент вклада SKU из листа `WB_Юнит_2025`.
 *
 * Зачем снимок: исторические ставки комиссии и логистики живут ТОЛЬКО в листе (вью V_UNITKA_*
 * хранят текущее окно), а P&L магазина сверяет вклад SKU с финотчётом WB на тех же единицах.
 * Модуль чистый: сетка значений → строки сутки × nm. Ничего не пишет и не считает P&L.
 *
 * Тождество строки (формула листа, offsets — model.ts):
 *   W = R·AA − R·AA·AD − Q·AF − S·REV − R·AH − R·COGS − X − AG + Y,   R = Q − S,
 *   COGS на единицу = AE − AF − AH − AI (AI — прибыль с единицы, AE — цена минус комиссия).
 * model_gap = W − модель. Для месяцев на формулах Engine он нулевой; ненулевой — наследие старых формул.
 *
 * Fail-closed: ошибка формулы или текст в числовой ячейке, дубль (сутки, nm), дата блока ≠ дата строки.
 * Полнота стороны SKU (ревью Phase C, M1b): по каждым суткам Σ W всех блоков = итоговая колонка листа
 * «Доходность (общая)» (I) и дата сводки (B) = дата строки; сутки месяца идут подряд с 1-го до конца месяца или LCD.
 * Окно — с STORE_PNL_FROM: раньше наследие формул и оценочное хранение (P&L их не считает).
 */
import { LoaderError } from '../../../errors.js';
import { BLOCK_FIRST_COLUMN } from '../calendar.js';
import { OFFSET, SUMMARY, addDaysIso, findBlocks, isEmpty, isFormulaError, serialToIso, type CellValue } from '../model.js';

/** Первый месяц P&L магазина (= нижняя граница в V_WB_STORE_PNL_MONTHLY). */
export const STORE_PNL_FROM = '2026-08-01';

export interface SkuComponentRow {
  date_msk: string;
  nm_id: number;
  sheet_row: number;
  block_col: number;
  orders: number;
  cancels: number;
  price: number;
  commission_rate: number;
  logistics_per_unit: number;
  reverse_leg_rate: number;
  storage: number;
  ads_in: number;
  ads_out: number;
  tax_per_unit: number;
  cogs_per_unit: number;
  profit: number;
  model_gap: number;
}

export interface MonthTotals {
  days: number;
  rows: number;
  orders: number;
  cancels: number;
  profit: number;
  modelGapAbs: number;
  /** Σ итоговой колонки листа (I) за сутки месяца — равна profit, иначе отказ. */
  summaryProfit: number;
}

export interface ParsedComponents {
  rows: SkuComponentRow[];
  months: Record<string, MonthTotals>;
  sections: number;
}

/** Метрики блока, которые читает снимок. Порядок — для сообщений об ошибках. */
const USED = ['orders', 'cancels', 'profitAll', 'adsIn', 'adsOut', 'price', 'commission', 'priceMinusComm',
  'logistics', 'storage', 'tax', 'unitProfit'] as const;

function num(v: CellValue | undefined, where: string): number {
  if (isEmpty(v)) return 0;
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (isFormulaError(v)) throw new LoaderError(`${where}: ошибка формулы ${String(v)}`, 'STORE_PNL_SHEET_ERROR');
  throw new LoaderError(`${where}: не число (${JSON.stringify(v)})`, 'STORE_PNL_SHEET_ERROR');
}

/**
 * Все секции листа (строка заголовков: «Дата» в колонке M), сутки ≤ LCD, блоки с nmID.
 * Пустые SKU-сутки (нет заказов, прибыли, рекламы, хранения) не попадают в снимок: их вклад нулевой.
 */
export function parseUnitkaComponents(grid: readonly (readonly CellValue[])[], lcd: string, reverseLegRate: number,
  from: string = STORE_PNL_FROM): ParsedComponents {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(lcd)) throw new LoaderError(`LAST_CLOSED_DATE: не дата (${lcd})`, 'STORE_PNL_SHEET_ERROR');
  if (!Number.isFinite(reverseLegRate) || reverseLegRate <= 0) {
    throw new LoaderError(`REVERSE_LEG_RATE: недопустимо (${reverseLegRate})`, 'STORE_PNL_SHEET_ERROR');
  }
  const at = (r: number, c: number): CellValue | undefined => grid[r - 1]?.[c - 1];
  const rows: SkuComponentRow[] = [];
  const months: Record<string, MonthTotals> = {};
  const seen = new Set<string>();
  const days = new Map<string, Set<string>>();
  let sections = 0;
  for (let r = 2; r <= grid.length; r++) {
    if (String(at(r, BLOCK_FIRST_COLUMN) ?? '').trim() !== 'Дата') continue;
    sections++;
    const blocks = findBlocks(grid[r - 2] ?? []);
    if (blocks.length === 0) throw new LoaderError(`строка ${r - 1}: секция без блоков nmID`, 'STORE_PNL_SHEET_ERROR');
    for (let i = r + 1; i <= grid.length; i++) {
      const dv = at(i, BLOCK_FIRST_COLUMN);
      if (typeof dv !== 'number') break;
      const day = serialToIso(dv);
      if (day > lcd || day < from) continue;
      const sumDate = at(i, SUMMARY.date);
      if (typeof sumDate !== 'number' || serialToIso(sumDate) !== day) {
        throw new LoaderError(`строка ${i}: дата сводки ${JSON.stringify(sumDate)} ≠ ${day}`, 'STORE_PNL_SHEET_ERROR');
      }
      const sumProfit = num(at(i, SUMMARY.profit), `строка ${i}, итог «Доходность (общая)»`);
      let blocksProfit = 0;
      for (const b of blocks) blocksProfit += num(at(i, b.start + OFFSET.profitAll), `строка ${i}, блок ${b.nmId}, profitAll`);
      if (Math.abs(blocksProfit - sumProfit) > 0.01) {
        throw new LoaderError(`строка ${i} (${day}): Σ блоков ${blocksProfit.toFixed(2)} ≠ итог листа ${sumProfit.toFixed(2)} — блок вне снимка или геометрия`, 'STORE_PNL_SHEET_ERROR');
      }
      const mk = day.slice(0, 7);
      (days.get(mk) ?? days.set(mk, new Set()).get(mk)!).add(day);
      const tm = (months[mk] ??= { days: 0, rows: 0, orders: 0, cancels: 0, profit: 0, modelGapAbs: 0, summaryProfit: 0 });
      tm.summaryProfit += sumProfit;
      for (const b of blocks) {
        const a = b.start;
        const v = {} as Record<(typeof USED)[number], number>;
        for (const k of USED) v[k] = num(at(i, a + OFFSET[k]), `строка ${i}, блок ${b.nmId}, ${k}`);
        if (v.orders === 0 && v.profitAll === 0 && v.adsIn === 0 && v.storage === 0 && v.adsOut === 0) continue;
        // Дата блока сверяется только у суток с данными: выбывший блок может хранить даты-константы прошлого
        // месяца (живой лист, август 2026, блок 252441968 — пустой, даты июля). Данные под чужой датой — отказ.
        const bd = at(i, a + OFFSET.date);
        if (typeof bd !== 'number' || serialToIso(bd) !== day) {
          throw new LoaderError(`строка ${i}, блок ${b.nmId}: дата блока ${JSON.stringify(bd)} ≠ ${day}`, 'STORE_PNL_SHEET_ERROR');
        }
        const key = `${day}|${b.nmId}`;
        if (seen.has(key)) throw new LoaderError(`дубль сутки × nm: ${key}`, 'STORE_PNL_SHEET_ERROR');
        seen.add(key);
        const R = v.orders - v.cancels;
        const cogs = v.priceMinusComm - v.logistics - v.tax - v.unitProfit;
        const model = R * v.price - R * v.price * v.commission - v.orders * v.logistics - v.cancels * reverseLegRate
          - R * v.tax - R * cogs - v.adsIn - v.storage + v.adsOut;
        const row: SkuComponentRow = {
          date_msk: day, nm_id: b.nmId, sheet_row: i, block_col: a,
          orders: v.orders, cancels: v.cancels, price: v.price, commission_rate: v.commission,
          logistics_per_unit: v.logistics, reverse_leg_rate: reverseLegRate, storage: v.storage,
          ads_in: v.adsIn, ads_out: v.adsOut, tax_per_unit: v.tax, cogs_per_unit: cogs,
          profit: v.profitAll, model_gap: v.profitAll - model,
        };
        rows.push(row);
        const m = day.slice(0, 7);
        const t = months[m]!;
        t.rows++; t.orders += row.orders; t.cancels += row.cancels; t.profit += row.profit; t.modelGapAbs += Math.abs(row.model_gap);
      }
    }
  }
  if (sections === 0) throw new LoaderError('в листе нет ни одной секции («Дата» в колонке M)', 'STORE_PNL_SHEET_ERROR');
  // Каждый месяц окна до месяца LCD обязан быть в листе: секция, не опознанная по «Дата»/дате в M, иначе молча
  // выпала бы из P&L целиком.
  for (let m = from.slice(0, 7); m <= lcd.slice(0, 7); m = addDaysIso(`${m}-01`, 31).slice(0, 7)) {
    if (!days.has(m)) throw new LoaderError(`${m}: месяца нет в листе (секция не найдена или без суток)`, 'STORE_PNL_SHEET_ERROR');
  }
  // Сутки месяца подряд: с 1-го до последнего дня месяца или до LCD (пропуск строки секции = пропуск денег).
  for (const [m, s] of days) {
    const first = `${m}-01`;
    const monthEnd = addDaysIso(`${addDaysIso(first, 31).slice(0, 7)}-01`, -1);
    const last = lcd < monthEnd ? lcd : monthEnd;
    let want = 0;
    for (let d = first; d <= last; d = addDaysIso(d, 1)) { want++; if (!s.has(d)) throw new LoaderError(`${m}: в листе нет суток ${d}`, 'STORE_PNL_SHEET_ERROR'); }
    if (s.size !== want) throw new LoaderError(`${m}: ${s.size} суток вместо ${want}`, 'STORE_PNL_SHEET_ERROR');
    months[m]!.days = s.size;
  }
  return { rows, months, sections };
}
