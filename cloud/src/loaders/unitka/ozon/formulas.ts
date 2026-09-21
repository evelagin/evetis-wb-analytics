/**
 * OZON UNITKA ADAPTER — построители формул секции месяца.
 *
 * Формулы строятся в КАНОНИЧЕСКОЙ (en) форме: запятая как разделитель аргументов, точка как
 * десятичный разделитель. Перевод в локаль книги делает общий слой (`toLocaleFormula`) — в ru_RU
 * книге и аргументы становятся «;», и десятичная точка становится запятой. Без этого перевода
 * числовой литерал COGS вида «231.38» даёт #ERROR! (Formula parse error) — проверено на живой книге.
 *
 * Отличия от WB (осознанные, решение владельца Gate 4):
 *   • отмены НЕ штрафуются: нет ни «− отмены × маржа», ни константы 45 ₽ — экономика считается
 *     только по РЕАЛИЗОВАННЫМ единицам (заказы − отмены), что тождественно realized_qty;
 *   • база налогового резерва — «цена с СПП» (цена покупателя), как в исторической Ozon-Юнитке;
 *     у WB база — «цена» (цена продавца). Расхождение осознанное и вынесено владельцу;
 *   • хранение вычитается в «Доходность (общая)», как в WB, но у Ozon всегда пусто.
 */
import { OFFSET, colA1 } from '../model.js';
import { OZON_GEOMETRY, OZON_SUMMARY_TO_OFFSET, MANAGEMENT_TAX_RESERVE_RATE, ozonSlotStart } from './contract.js';

const LCD = 'LAST_CLOSED_DATE';
const c = (start: number, off: number): string => colA1(start + off);

export interface OzonBlockParams {
  /** Первая колонка блока (12 для первого SKU). */
  start: number;
  /**
   * Единицы, отгруженные но ещё НЕ доставленные (in_transit) этих суток и этого SKU.
   * Экономика считается по РЕАЛИЗОВАННЫМ единицам, а realized = заказы − отмены − в пути.
   * В закрытом месяце в пути нечему быть, поэтому терм равен '0' и формула совпадает
   * с принятой в Gate 5A/5B побайтно. В ОТКРЫТОМ месяце он обязателен: без него
   * (заказы − отмены) завышает количество и завышает вклад.
   */
  inTransitTerm?: string;
  /**
   * OTHER_DIRECT_COST_RUB этих суток и этого SKU в en-форме («135.5»), либо '0'.
   * Входит в «Доходность (общая)» явным вычитаемым: видимой колонки нет, но расход
   * не исчезает и остаётся аудируемым по дате, SKU, type_id и сумме (Gate 5A §2/§9).
   */
  otherDirectTerm?: string;
  /**
   * Слагаемое себестоимости в «доходность 1 шт»: канонический COGS на единицу
   * (`evetis_ref.V_PRODUCT_COGS_EFFECTIVE`, на дату) в en-форме («231.38»), либо '0',
   * если канон на эту дату отсутствует. Легаси-константы строки 50 НЕ используются.
   */
  cogsTerm: string;
}

/** Расчётные колонки строки дня: смещение → каноническая формула. */
export function ozonBlockDayFormulas(p: OzonBlockParams, row: number): Map<number, string> {
  const s = p.start;
  const D = `$${c(s, OFFSET.date)}${row}`;
  const x = (off: number): string => `${c(s, off)}${row}`;
  const g = (body: string): string => `=IF(${D}>${LCD},"",${body})`;

  const N_ = x(OFFSET.bloggers), P = x(OFFSET.orders), R = x(OFFSET.cancels);
  const V = x(OFFSET.profitAll), W = x(OFFSET.adsIn), X = x(OFFSET.adsOut);
  const Z = x(OFFSET.price), AA = x(OFFSET.spp), AB = x(OFFSET.priceSpp), AC = x(OFFSET.commission);
  const AD = x(OFFSET.priceMinusComm), AE = x(OFFSET.logistics), AF = x(OFFSET.storage);
  const AG = x(OFFSET.tax), AH = x(OFFSET.unitProfit);
  const taxPct = `${MANAGEMENT_TAX_RESERVE_RATE * 100}%`;

  const m = new Map<number, string>();
  m.set(OFFSET.priceSpp,        g(`IF(N(${Z})=0,"",${Z}-${Z}*N(${AA})%)`));
  m.set(OFFSET.priceMinusComm,  g(`IF(N(${Z})=0,"",${Z}-${Z}*N(${AC}))`));
  // База резерва — ЦЕНА ПРОДАВЦА (Z), единообразно с WB (`AA×2%`), а не цена покупателя после
  // субсидии Ozon: баллы площадки не выручка продавца. Решение владельца, Gate 4.1.
  m.set(OFFSET.tax,             g(`IF(N(${Z})=0,"",${Z}*${taxPct})`));
  m.set(OFFSET.unitProfit,      g(`IF(N(${AD})=0,"",${AD}-N(${AE})-N(${AG})-${p.cogsTerm})`));
  // реализовано = заказы − отмены (тождество проверено: gross−cancelled == realized на всех строках)
  const od = p.otherDirectTerm && p.otherDirectTerm !== '0' ? `-${p.otherDirectTerm}` : '';
  const it = p.inTransitTerm && p.inTransitTerm !== '0' ? `-${p.inTransitTerm}` : '';
  m.set(OFFSET.profitAll,       g(`(${P}-${R}${it})*N(${AH})-N(${W})-N(${AF})+N(${X})${od}`));
  m.set(OFFSET.profit1,         g(`IFERROR(${V}/(${P}-${R}${it}),"")`));
  m.set(OFFSET.drr,             g(`IFERROR(${W}/((${P}-N(${N_}))*${AB}),"")`));
  // Оборачиваемость (дн) = остаток / заказы этих суток — принятая форма WB
  // (`IF(Q=0,"",T/Q)`). Дополнительная защита: без доказанного остатка ячейка ПУСТАЯ,
  // иначе пустой остаток дал бы 0 и «неизвестно» превратилось бы в «ноль дней запаса».
  const T = x(OFFSET.stock);
  m.set(OFFSET.turnover,        g(`IF(OR(${P}=0,${T}=""),"",${T}/${P})`));
  return m;
}

/**
 * Сводка магазина A..J строки дня: колонка → каноническая формула.
 * Диапазон суммирования выводится из числа блоков, шаг MOD(COLUMN(...),24) — как в WB.
 */
export function ozonSummaryDayFormulas(row: number, blockCount: number): Map<number, string> {
  if (!Number.isInteger(blockCount) || blockCount < 1) throw new RangeError(`блоков ${blockCount}`);
  const lastStart = ozonSlotStart(blockCount - 1);
  const m = new Map<number, string>();
  for (const [col, off] of OZON_SUMMARY_TO_OFFSET) {
    const a = `${colA1(OZON_GEOMETRY.BLOCK_FIRST_COLUMN + off)}${row}`;
    const b = `${colA1(lastStart + off)}${row}`;
    const rng = `${a}:${b}`;
    const flt = `FILTER(${rng},MOD(COLUMN(${rng})-COLUMN(${a}),${OZON_GEOMETRY.BLOCK_WIDTH})=0)`;
    // «неизвестно» не превращается в ноль: если ни один блок не дал наблюдения (корзина,
    // блогеры), сводка дня остаётся пустой, а не 0 — иначе ноль утечёт и в итог месяца.
    m.set(col, `=IF($B${row}>${LCD},"",IF(COUNT(${flt})=0,"",SUM(${flt})))`);
  }
  return m;
}

/** Геометрия месяца, необходимая строке MTD. Совместима с MonthGeometry общего слоя. */
export interface OzonMonthGeometry {
  firstDailyRow: number;
  lastDailyRow: number;
  mtdRow: number;
}

/**
 * Строка MTD, сводка магазина A..J: сумма ВСЕГО месяца (исторические замороженные дни +
 * восстановленные) — `=SUM(col_first:col_last)`, как в общем слое (`summaryMtdFormulas`).
 * Легаси-вычет «− отмены × 45 ₽» НЕ переносится (решение владельца D-3).
 */
export function ozonSummaryMtdFormulas(g: OzonMonthGeometry): Map<number, string> {
  const m = new Map<number, string>();
  for (const [col] of OZON_SUMMARY_TO_OFFSET) {
    const L = colA1(col);
    const rng = `${L}${g.firstDailyRow}:${L}${g.lastDailyRow}`;
    // «неизвестно» не превращается в ноль: если за месяц не было НИ ОДНОГО наблюдения
    // (метрика недоступна — корзина, блогеры), итог остаётся пустым, а не 0.
    m.set(col, `=IF(COUNT(${rng})=0,"",SUM(${rng}))`);
  }
  return m;
}

/**
 * Строка MTD внутри SKU-блока. Форма SUMIF с отсечкой по LAST_CLOSED_DATE — из общего слоя.
 * Отличия Ozon: остаток/оборачиваемость/хранение остаются ПУСТЫМИ (не ноль) по политике источника,
 * доходность на 1 шт считается на РЕАЛИЗОВАННЫЕ единицы (заказы − отмены), поунитные колонки
 * (цена, СПП, комиссия, логистика, налог) в MTD не заполняются — как в исторической Ozon-Юнитке.
 */
export function ozonBlockMtdFormulas(start: number, g: OzonMonthGeometry): Map<number, string> {
  const f = g.firstDailyRow, l = g.lastDailyRow, mt = g.mtdRow;
  const dc = colA1(start + OFFSET.date);
  const Dabs = `$${dc}$${f}:$${dc}$${l}`;
  const col = (off: number): string => colA1(start + off);
  const rng = (off: number): string => `${col(off)}${f}:${col(off)}${l}`;
  // «неизвестно» не превращается в ноль: нет ни одного наблюдения за месяц — ячейка пуста.
  const sumif = (off: number): string =>
    `=IF(COUNT(${rng(off)})=0,"",SUMIF(${Dabs},"<="&${LCD},${rng(off)}))`;
  const m = new Map<number, string>();
  for (const off of [OFFSET.bloggers, OFFSET.views, OFFSET.opens, OFFSET.orders, OFFSET.carts, OFFSET.cancels]) {
    m.set(off, sumif(off));
  }
  for (const off of [OFFSET.profitAll, OFFSET.adsIn, OFFSET.adsOut]) m.set(off, sumif(off));
  m.set(OFFSET.profit1, `=IFERROR(${col(OFFSET.profitAll)}${mt}/(${col(OFFSET.orders)}${mt}-${col(OFFSET.cancels)}${mt}),"")`);
  m.set(OFFSET.drr, `=IFERROR(${col(OFFSET.adsIn)}${mt}/SUMPRODUCT((${Dabs}<=${LCD})*(${rng(OFFSET.orders)}-${rng(OFFSET.bloggers)})*${rng(OFFSET.priceSpp)}),"")`);
  return m;
}

/** Смещения блока, которые строка MTD Ozon ОСТАВЛЯЕТ ПУСТЫМИ (нет источника или нет смысла в MTD). */
export const OZON_MTD_BLANK_OFFSETS: readonly number[] = [
  OFFSET.stock, OFFSET.turnover, OFFSET.price, OFFSET.spp, OFFSET.priceSpp,
  OFFSET.commission, OFFSET.priceMinusComm, OFFSET.logistics, OFFSET.storage,
  OFFSET.tax, OFFSET.unitProfit,
];
