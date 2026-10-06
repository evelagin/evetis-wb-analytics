/**
 * OZON UNITKA — движок месячной секции (Gate 5E: перенос из гейтового стенда в боевой код).
 *
 * Здесь живёт ВСЯ расчётная логика месяца: геометрия секции, раскладка «сутки × SKU»,
 * денежные контракты Gate 5A–5C и сборка сетки значений. До Gate 5E это существовало
 * только в Python-стенде, то есть боевого кода на это поведение не было.
 *
 * Контракты, закреплённые здесь:
 *   • шаг секции = дни + 4 (Calendar V2), геометрия выводится из предыдущей секции;
 *   • себестоимость — по дате выкупа, литералом в формулу (не FIFO);
 *   • резерв 2 % считается от ЦЕНЫ ПРОДАВЦА (Gate 4.1);
 *   • эквайринг попадает в экономику РОВНО один раз: в комиссию при выручке, иначе в прочие прямые;
 *   • отмены без штрафа; корзина не заполняется — провенанс не доказан;
 *   • хранение — абсолютная сумма суток, только когда источник привязан к SKU (type 79);
 *   • остаток пишется только при ДОКАЗАННОМ снимке; «неизвестно» никогда не становится нулём;
 *   • после LAST_CLOSED_DATE не пишется НИ ОДНОГО факта — иначе незакрытые сутки
 *     превратились бы в наблюдаемый ноль.
 */
import { daysInMonth, monthTitle } from '../calendar.js';
import { MANAGEMENT_TAX_RESERVE_RATE, OZON_GEOMETRY } from './contract.js';
import { ROLE_OFFSET } from './presentation.js';

const WEEKDAY_RU = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'] as const;

/** Серийный номер даты Google Sheets (эпоха 30.12.1899). */
function ymd(iso: string): [number, number, number] {
  return [Number(iso.slice(0, 4)), Number(iso.slice(5, 7)), Number(iso.slice(8, 10))];
}
export function serialOf(iso: string): number {
  const [y, m, d] = ymd(iso);
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(1899, 11, 30)) / 86400000);
}
export function weekdayRu(iso: string): string {
  const [y, m, d] = ymd(iso);
  return WEEKDAY_RU[(new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7] as string;
}

export interface OzonMonthSpec {
  readonly year: number; readonly month: number; readonly key: string; readonly title: string;
  readonly days: number;
  readonly titleRow: number; readonly headerRow: number;
  readonly firstRow: number; readonly lastRow: number; readonly mtdRow: number; readonly spacerRow: number;
  readonly blocks: readonly string[];
  readonly anchor: Readonly<Record<string, number>>;
  readonly ncols: number;
}

/**
 * Геометрия секции выводится из ПРЕДЫДУЩЕЙ секции, а не задаётся руками.
 * Инварианты проверяются здесь же: расхождение — это ошибка, а не «подвинем на строку».
 */
export function ozonMonthSpec(
  year: number, month: number, prevTitleRow: number, prevDays: number, blocks: readonly string[],
): OzonMonthSpec {
  const days = daysInMonth(year, month);
  const titleRow = prevTitleRow + prevDays + 4;
  const firstRow = titleRow + 2;
  const lastRow = firstRow + days - 1;
  const mtdRow = lastRow + 1;
  const anchor: Record<string, number> = {};
  blocks.forEach((o, i) => { anchor[o] = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * i; });
  const ncols = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * blocks.length - 1;
  if (mtdRow - titleRow !== days + 2) throw new Error('геометрия секции: шаг не равен дням + 2');
  if (lastRow - firstRow + 1 !== days) throw new Error('геометрия секции: число дней');
  const lastBlock = blocks[blocks.length - 1];
  if (lastBlock !== undefined && (anchor[lastBlock] ?? 0) + OZON_GEOMETRY.BLOCK_WIDTH - 1 !== ncols) {
    throw new Error('геометрия секции: правая граница блоков');
  }
  return { year, month, key: `${year}-${String(month).padStart(2, '0')}`,
    title: monthTitle({ year, month }), days, titleRow, headerRow: titleRow + 1,
    firstRow, lastRow, mtdRow, spacerRow: mtdRow + 1, blocks: [...blocks], anchor, ncols };
}

export function monthDates(spec: OzonMonthSpec): string[] {
  return Array.from({ length: spec.days }, (_, i) =>
    `${spec.year}-${String(spec.month).padStart(2, '0')}-${String(i + 1).padStart(2, '0')}`);
}

/** Одна строка канонического факта Ozon (ozon_mart.FCT_OZON_SKU_PNL_DAILY + реклама + цена покупателя). */
export interface OzonFactRow {
  d: string; offer_id: string;
  gross_qty: number; cancelled_qty: number; realized_qty: number; in_transit_qty?: number | null;
  revenue?: number | null; commission?: number | null; logistics?: number | null;
  acquiring?: number | null; storage?: number | null; other_direct?: number | null;
  promo?: number | null; cogs_amt?: number | null;
  ads_spend?: number | null; impr?: number | null; clicks?: number | null;
  buyer_amt?: number | null; seller_amt?: number | null;
  /** Независимая цена заказа: допустима только при полном покрытии ожидаемых единиц. */
  order_reference_price?: number | null;
  order_reference_qty?: number | null;
  order_reference_covered_qty?: number | null;
  /** Взаимоисключающие posting/SKU-популяции базы калькулятора, не accounting revenue. */
  operational_basis_version?: number | null;
  operational_expected_qty?: number | null;
  operational_actual_qty?: number | null;
  operational_provisional_qty?: number | null;
  operational_reference_covered_qty?: number | null;
  operational_actual_basis_rub?: number | null;
  operational_reference_basis_rub?: number | null;
  operational_basis_rub?: number | null;
  operational_basis_units_json?: string | null;
  // Полнота источника. Комиссия имеет три состояния, а не два: отсутствующая комиссия обычной
  // продажи (commission_missing_qty) и неприменимая комиссия выкупа
  // (commission_not_applicable_qty) — разные факты, и смешивать их нельзя.
  cogs_missing_qty?: number | null;
  commission_missing_qty?: number | null;
  commission_not_applicable_qty?: number | null;
  buyout_revenue_unproven_qty?: number | null;
  buyout_revenue_unproven_rub?: number | null;
  // Gate 8: провизорная экономика. `commission`/`logistics` выше — ЭФФЕКТИВНЫЕ величины
  // (факт там, где он есть, оценка там, где Ozon ещё не опубликовал); факт едет рядом
  // отдельно и не переопределяется, состояние компонента — в *_state.
  commission_actual?: number | null; logistics_actual?: number | null;
  commission_estimated_rub?: number | null; logistics_estimated_rub?: number | null;
  commission_state?: string | null; logistics_state?: string | null;
  storage_state?: string | null; other_direct_state?: string | null;
  commission_estimate_method?: string | null; logistics_estimate_method?: string | null;
  period_matured?: boolean | null;
  // Gate 9: заказная (провизорная) база. `expected_realized_qty` = заказано − отменено:
  // единица в пути уже принесла заказ и обязана принести свою экономику. Выручка и
  // себестоимость таких единиц лежат отдельно от факта и не переопределяют его.
  expected_realized_qty?: number | null;
  provisional_revenue_rub?: number | null;
  provisional_cogs_rub?: number | null;
  provisional_cogs_missing_qty?: number | null;
  economics_completeness?: string | null;
}

/** Полнота экономики строки суток × SKU (Gate 9 §7F). */
export type EconomicsCompleteness =
  'ACTUAL' | 'PROVISIONAL_COMPLETE' | 'PROVISIONAL_PARTIAL' | 'NO_ECONOMICS';

/** Состояние компонента расхода: старшинство ACTUAL > ESTIMATED > NOT_APPLICABLE > UNKNOWN. */
export type ComponentState = 'ACTUAL' | 'ESTIMATED' | 'NOT_APPLICABLE' | 'UNKNOWN';

/** Провенанс ячейки: что именно в ней стоит — факт или оценка, и какой оценщик её дал. */
export interface CellProvenance {
  readonly date: string; readonly offerId: string; readonly row: number;
  readonly component: 'COMMISSION' | 'LOGISTICS';
  readonly state: ComponentState;
  readonly method: string | null;
  readonly estimatedRub: number;
  readonly actualRub: number;
}

/** Состояние полноты комиссии для строки суток × SKU. */
export type OzonCommissionState = 'PRESENT' | 'MISSING' | 'NOT_APPLICABLE';

/**
 * Полнота комиссии по строке факта. NOT_APPLICABLE — не пробел в данных: у выкупа товара
 * агентского вознаграждения не существует, поэтому такую строку нельзя считать неполной.
 */
export function ozonCommissionState(row: OzonFactRow): OzonCommissionState {
  // Структурная сигнатура без первичного документа не доказывает неприменимость.
  if ((row.buyout_revenue_unproven_qty ?? 0) > 0) return 'MISSING';
  if ((row.commission_missing_qty ?? 0) > 0) return 'MISSING';
  if ((row.commission_not_applicable_qty ?? 0) > 0) return 'NOT_APPLICABLE';
  return 'PRESENT';
}

export interface OzonDayCell {
  orders: number; cancel: number;
  shows?: number; clicks?: number; adin?: number;
  price?: number; spp?: number; comm?: number; log?: number; stock?: number; stor?: number;
  od?: number; cart?: number;
  bloggers?: number; externalAds?: CellValue;
}
export interface OzonMonthTotals {
  orders: number; cancel: number; realized: number; revenue: number; cogs: number;
  comm: number; acq: number; acqComm: number; acqOther: number;
  logRepr: number; logUnrepr: number; otherFees: number; promo: number; other: number;
  ads: number; tax: number; storage: number; canonical: number;
  /** База калькулятора; revenue выше не подменяется ценой заказа. */
  calculatorRevenue: number;
}
export interface OzonMonthComposition {
  cells: Record<string, OzonDayCell | Record<string, never>>;
  cogs: Record<string, number>;
  other: Record<string, number>;
  audit: Array<{ date: string; offerId: string; otherFees: number; logisticsNonRealized: number;
                 acquiringWithoutRevenue: number; skuPromotion: number; total: number }>;
  /** Ячейки, в которых стоит ОЦЕНКА, а не факт. Основание для пометки в листе. */
  provenance: CellProvenance[];
  partialEconomics: Array<{ row: number; offerId: string; date: string }>;
  totals: OzonMonthTotals;
}

const r6 = (x: number) => Math.round(x * 1e6) / 1e6;
const r8 = (x: number) => Math.round(x * 1e8) / 1e8;

export function provenOrderReferencePrice(row: OzonFactRow | undefined, units: number): number | undefined {
  const p = row?.order_reference_price;
  return units > 0 && typeof p === 'number' && Number.isFinite(p) && p > 0
    && row?.order_reference_qty === units && row.order_reference_covered_qty === units ? p : undefined;
}

/** В production query всегда version=1. Старые whole-day fixtures допустимы только
 * при полной базе; частично признанная выручка структурного кандидата не делится на все qty.
 */
export function operationalPriceBasis(row: OzonFactRow | undefined, units: number): number | undefined {
  if (!row || units <= 0) return undefined;
  if (row.operational_basis_version !== undefined) {
    const a = row.operational_actual_qty, p = row.operational_provisional_qty;
    const actual = row.operational_actual_basis_rub, reference = row.operational_reference_basis_rub;
    const total = row.operational_basis_rub;
    return row.operational_basis_version === 1 && row.operational_expected_qty === units
      && typeof a === 'number' && Number.isInteger(a) && a >= 0
      && typeof p === 'number' && Number.isInteger(p) && p >= 0 && a + p === units
      && row.operational_reference_covered_qty === p
      && typeof actual === 'number' && Number.isFinite(actual) && actual >= 0
      && typeof reference === 'number' && Number.isFinite(reference) && reference >= 0
      && typeof total === 'number' && Number.isFinite(total) && total >= 0
      && (p === 0 || reference > 0) && Math.abs(actual + reference - total) <= 1e-6 ? total : undefined;
  }
  const revenue = row.provisional_revenue_rub ?? row.revenue ?? 0;
  if (revenue > 0) return (row.buyout_revenue_unproven_qty ?? 0) > 0 ? undefined : revenue;
  const price = provenOrderReferencePrice(row, units);
  return price !== undefined ? price * units : undefined;
}

/** Уже наблюдённые поля листа. Внешняя реклама сохраняется, её формула пока не восстановлена. */
export interface OzonSheetInputs {
  bloggers?: Readonly<Record<string, number>>;
  externalAds?: Readonly<Record<string, CellValue>>;
}

/**
 * Раскладка месяца. `stockBy` — только ДОКАЗАННЫЕ снимки остатка (ключ `iso|offer_id`).
 * `cartBy` — уже наблюдённые «Положили в корзину» из самого листа (тот же ключ). Метрика снята
 * с API Ozon («deprecated metrics used»), новых значений взять неоткуда, а старые — настоящие
 * наблюдения. Поэтому они проносятся через запись без изменений: сетка значений заполняется
 * пустыми строками, и колонка, которую никто не выставил, была бы СТЁРТА.
 * `lcd` — LAST_CLOSED_DATE: сутки после него остаются полностью пустыми.
 *
 * `fromDay` — первый день КАНОНИЧЕСКОГО окна месяца (апрель 2026 — гибридный месяц миграции,
 * его 01–16 посчитаны легаси-моделью и не переписываются). Сутки до окна не дают ни ячеек,
 * ни итогов: иначе контрольная сумма секции включила бы половину, которую движок не пишет.
 */
export function composeMonth(
  spec: OzonMonthSpec, facts: readonly OzonFactRow[],
  stockBy: Readonly<Record<string, number>> = {}, lcd: string | null = null,
  fromDay = 1, cartBy: Readonly<Record<string, number>> = {},
  sheetInputs: OzonSheetInputs = {},
): OzonMonthComposition {
  const by = new Map(facts.map((r) => [`${r.d}|${r.offer_id}`, r]));
  const cells: OzonMonthComposition['cells'] = {};
  const cogs: Record<string, number> = {}; const other: Record<string, number> = {};
  const audit: OzonMonthComposition['audit'] = [];
  const provenance: CellProvenance[] = [];
  const partialEconomics: OzonMonthComposition['partialEconomics'] = [];
  const T: OzonMonthTotals = { orders: 0, cancel: 0, realized: 0, revenue: 0, cogs: 0, comm: 0, acq: 0,
    acqComm: 0, acqOther: 0, logRepr: 0, logUnrepr: 0, otherFees: 0, other: 0, ads: 0, tax: 0,
    storage: 0, promo: 0, canonical: 0, calculatorRevenue: 0 };
  for (const ds of monthDates(spec)) {
    const day = Number(ds.slice(8, 10));
    const row = spec.firstRow + day - 1;
    const outside = day < fromDay || (lcd !== null && ds > lcd);
    for (const o of spec.blocks) {
      const key = `${o}|${row}`;
      // вне окна — ни одного факта и ни одного нуля: до окна там легаси, после LCD день не закрыт
      if (outside) { cells[key] = {}; continue; }
      const rec = by.get(`${ds}|${o}`);
      const n = (v: number | null | undefined) => v ?? 0;
      const orders = n(rec?.gross_qty), cancel = n(rec?.cancelled_qty);
      const comm = n(rec?.commission);
      // Gate 9: экономика считается на ОЖИДАЕМО реализованных единицах. Для созревших суток
      // единиц в пути нет, и обе базы совпадают — историю это не двигает (проверено: 3289
      // созревших строк, расхождений 0). Отменённая единица выпадает из базы при следующем
      // же прогоне окна перезаписи, поэтому двойного счёта не возникает.
      const eq = rec?.expected_realized_qty ?? Math.max(0, orders - cancel);
      const rev = rec?.provisional_revenue_rub ?? n(rec?.revenue);
      const basis = operationalPriceBasis(rec, eq);
      const acq = n(rec?.acquiring), log = n(rec?.logistics), oth = n(rec?.other_direct);
      const cg = rec?.provisional_cogs_rub ?? n(rec?.cogs_amt);
      const stor = n(rec?.storage), promo = n(rec?.promo);
      const hasAds = !!rec && rec.impr !== null && rec.impr !== undefined;
      const ads = hasAds ? n(rec?.ads_spend) : 0;
      const acqC = basis !== undefined && basis > 0 ? acq : 0;
      const acqO = acq - acqC;            // ровно один раз, на полной базе того же населения
      const logR = eq > 0 ? log : 0;
      const logU = eq > 0 ? 0 : log;
      // Продвижение с привязкой к SKU (отзывы, звёздные товары, бонусы) — прямой расход
      // этого SKU-дня. В колонку рекламы НЕ идёт: там атрибуция CPC, а здесь факт начисления.
      const od = oth + logU + acqO + promo;
      const c: OzonDayCell = { orders, cancel };
      if (stor) c.stor = r6(stor);
      if (hasAds) { c.shows = n(rec?.impr); c.clicks = n(rec?.clicks); c.adin = r6(ads); }
      if (eq > 0) {
        const price = basis !== undefined ? basis / eq : undefined;
        if (price !== undefined) {
          c.price = r6(price);
          T.calculatorRevenue += price * eq;
          T.tax += MANAGEMENT_TAX_RESERVE_RATE * price * eq;
          // Комиссия и эквайринг имеют ту же полную популяцию, что Price.
          const commissionKnown = rec?.commission_state === 'ACTUAL' || rec?.commission_state === 'ESTIMATED'
            || (rec?.commission_state === undefined && rev > 0)
            || (ozonCommissionState(rec!) === 'NOT_APPLICABLE' && comm === 0);
          if (basis !== undefined && basis > 0 && commissionKnown) c.comm = r8((comm + acqC) / basis);
        }
        // Расходы независимы от доказанности выручки.
        if (rev > 0 || (rec?.logistics !== null && rec?.logistics !== undefined)) c.log = r6(logR / eq);
        if (rev > 0 || ((rec?.provisional_cogs_rub ?? rec?.cogs_amt) !== null
            && (rec?.provisional_cogs_rub ?? rec?.cogs_amt) !== undefined)) cogs[key] = r6(cg / eq);
        if (ozonCommissionState(rec ?? { d: ds, offer_id: o, gross_qty: orders,
          cancelled_qty: cancel, realized_qty: eq }) === 'NOT_APPLICABLE' && comm === 0) c.comm = 0;
      }
      if (eq > 0 && rev) {
        // СПП и цена покупателя существуют ТОЛЬКО в финансовом начислении, то есть после
        // доставки (2003 строки из 2003). Для единицы в пути их не существует, и выводить
        // их нечем. Пустая ячейка честнее выдуманной скидки; известную цену продавца
        // отсутствие СПП не обнуляет.
        if (rec?.buyer_amt !== null && rec?.buyer_amt !== undefined && rec?.seller_amt) {
          c.spp = r6((1 - rec.buyer_amt / rec.seller_amt) * 100);
        }
      }
      if ((rec?.buyout_revenue_unproven_qty ?? 0) > 0 || rec?.economics_completeness === 'PROVISIONAL_PARTIAL') {
        partialEconomics.push({ row, offerId: o, date: ds });
      }
      // Gate 9: единицы в пути НЕ исключаются из экономики — терм `в пути` больше не нужен
      const st = stockBy[`${ds}|${o}`];
      if (st !== undefined) c.stock = st;             // только доказанный снимок
      const ct = cartBy[`${ds}|${o}`];
      if (ct !== undefined) c.cart = ct;              // наблюдение листа, а не выдуманный ноль
      const inputKey = `${ds}|${o}`;
      if (sheetInputs.bloggers?.[inputKey] !== undefined) c.bloggers = sheetInputs.bloggers[inputKey];
      if (sheetInputs.externalAds?.[inputKey] !== undefined) c.externalAds = sheetInputs.externalAds[inputKey];
      if (od) c.od = r6(od);                          // прочие прямые — теперь видимая колонка
      // Gate 8: где в ячейке стоит ОЦЕНКА, там об этом остаётся запись. Молча подменить
      // факт оценкой нельзя — провенанс переезжает в лист пометкой на ячейке.
      if (eq > 0) {
        const cEst = n(rec?.commission_estimated_rub), lEst = n(rec?.logistics_estimated_rub);
        if (cEst) provenance.push({ date: ds, offerId: o, row, component: 'COMMISSION',
          state: 'ESTIMATED', method: rec?.commission_estimate_method ?? null,
          estimatedRub: r6(cEst), actualRub: r6(n(rec?.commission_actual)) });
        if (lEst) provenance.push({ date: ds, offerId: o, row, component: 'LOGISTICS',
          state: 'ESTIMATED', method: rec?.logistics_estimate_method ?? null,
          estimatedRub: r6(lEst), actualRub: r6(n(rec?.logistics_actual)) });
      }
      cells[key] = c;
      if (od) {
        other[key] = r6(od);
        audit.push({ date: ds, offerId: o, otherFees: oth, logisticsNonRealized: logU,
                     acquiringWithoutRevenue: acqO, skuPromotion: promo, total: od });
      }
      T.orders += orders; T.cancel += cancel; T.realized += eq; T.revenue += rev; T.cogs += cg;
      T.comm += comm; T.acq += acq; T.acqComm += acqC; T.acqOther += acqO;
      T.logRepr += logR; T.logUnrepr += logU; T.otherFees += oth; T.promo += promo; T.other += od;
      T.ads += ads; T.storage += stor;
    }
  }
  T.canonical = T.calculatorRevenue - T.cogs - (T.comm + T.acqComm) - T.logRepr - T.storage - T.other - T.ads - T.tax;
  if (Math.abs(T.acqComm + T.acqOther - T.acq) > 1e-6) throw new Error('эквайринг посчитан не один раз');
  return { cells, cogs, other, audit, provenance, partialEconomics, totals: T };
}

export type CellValue = string | number | null;

/** Сетка значений секции: дни + строка итога. Формулы приходят из адаптера формул. */
export function buildGrid(
  spec: OzonMonthSpec, cells: OzonMonthComposition['cells'],
  formulas: { day: Record<string, string>; mtd: Record<string, string>; mtdBlank: readonly string[] },
): CellValue[][] {
  const g: CellValue[][] = Array.from({ length: spec.days + 1 }, () => Array(spec.ncols).fill(''));
  const KEYS = ['orders', 'cancel', 'shows', 'clicks', 'adin', 'price', 'spp', 'comm', 'log', 'stock', 'stor', 'od', 'cart', 'bloggers', 'externalAds'] as const;
  const OFF: Record<(typeof KEYS)[number], number> = {
    orders: ROLE_OFFSET.ORDERS, cancel: ROLE_OFFSET.CANCELLATIONS, shows: ROLE_OFFSET.IMPRESSIONS,
    clicks: ROLE_OFFSET.CLICKS, adin: ROLE_OFFSET.INTERNAL_ADS, price: ROLE_OFFSET.SELLER_PRICE,
    spp: ROLE_OFFSET.DISCOUNT, comm: ROLE_OFFSET.COMMISSION, log: ROLE_OFFSET.LOGISTICS,
    stock: ROLE_OFFSET.STOCK, stor: ROLE_OFFSET.STORAGE,
    od: ROLE_OFFSET.OTHER_DIRECT, cart: ROLE_OFFSET.CART,
    bloggers: ROLE_OFFSET.MANUAL_EXTERNAL, externalAds: ROLE_OFFSET.EXTERNAL_ADS,
  };
  for (const ds of monthDates(spec)) {
    const day = Number(ds.slice(8, 10)); const i = day - 1; const row = spec.firstRow + i;
    const gi = g[i] as CellValue[];
    gi[0] = weekdayRu(ds); gi[1] = serialOf(ds);
    for (const o of spec.blocks) {
      const b = spec.anchor[o] as number; const c = cells[`${o}|${row}`] as OzonDayCell;
      gi[b - 1 + ROLE_OFFSET.DATE] = serialOf(ds);
      gi[b - 1 + ROLE_OFFSET.WEEKDAY] = weekdayRu(ds);
      for (const k of KEYS) { const v = (c as unknown as Record<string, CellValue>)[k]; if (v !== undefined) gi[b - 1 + OFF[k]] = v; }
    }
  }
  for (const [k, v] of Object.entries(formulas.day)) {
    const [r, col] = k.split(':').map(Number) as [number, number];
    (g[r - spec.firstRow] as CellValue[])[col - 1] = v;
  }
  for (const [k, v] of Object.entries(formulas.mtd)) {
    const col = Number(k.split(':')[1]); (g[spec.days] as CellValue[])[col - 1] = v;
  }
  for (const k of formulas.mtdBlank) { const col = Number(k.split(':')[1]); (g[spec.days] as CellValue[])[col - 1] = ''; }
  return g;
}

/**
 * Строки заголовка и шапки.
 *
 * Подпись блока собирается из АВТОРИТЕТНОЙ идентичности: `offer_id` из
 * `evetis_ref.REF_SKU_CHANNEL_MAP` плюс хвост подписи из `skuTitles`. Надпись в листе
 * источником идентичности не является — в ней встречалась опечатка (`9099514444` вместо
 * `909951444`), и вывод состава блоков из надписи ронял SKU из расчёта.
 *
 * `skuTitles[offerId]` — хвост подписи ДОСЛОВНО, вместе с разделителем: у владельца
 * встречается двойной пробел после идентификатора, и нормализовать его движок не вправе.
 * Шапка блока переносится с секции-эталона, потому что это подписи колонок, а не данные.
 */
export function buildHeaderRows(
  spec: OzonMonthSpec, refTitle: readonly CellValue[], refHeader: readonly CellValue[],
  refAnchor: Readonly<Record<string, number>>, skuTitles: Readonly<Record<string, string>> = {},
): { title: CellValue[]; header: CellValue[] } {
  const title: CellValue[] = Array(spec.ncols).fill('');
  const header: CellValue[] = Array(spec.ncols).fill('');
  title[0] = spec.title;
  for (let c = 2; c <= 10; c++) header[c - 1] = refHeader[c - 1] ?? '';
  header[5] = `Заказы ${spec.blocks.length} SKU`;
  header[6] = `Положили в корзину ${spec.blocks.length} SKU`;
  const lastRef = Math.max(...Object.values(refAnchor));
  for (const o of spec.blocks) {
    const b = spec.anchor[o] as number; const src = refAnchor[o];
    const from = src ?? lastRef;
    const name = skuTitles[o];
    title[b - 1] = name !== undefined
      ? `${o}${name}`                               // id авторитетный, хвост подписи дословный
      : (src !== undefined ? (refTitle[src - 1] ?? '') : o);
    for (let k = 0; k < OZON_GEOMETRY.BLOCK_WIDTH; k++) header[b - 1 + k] = refHeader[from - 1 + k] ?? '';
  }
  return { title, header };
}
