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
import { colA1 } from '../model.js';
import { OZON_OFFSET as OFFSET } from './offsets.js';
import { OZON_GEOMETRY, OZON_SUMMARY_TO_OFFSET, MANAGEMENT_TAX_RESERVE_RATE, ozonSlotStart } from './contract.js';
import { OZON_MTD_NOT_APPLICABLE_OFFSETS } from './summary.js';
import { STOCK_POLICY } from './contract.js';

/** Дата, с которой остаток становится ДОКАЗАННЫМ фактом. Раньше истории не существует. */
const STOCK_FACTUAL_FROM = STOCK_POLICY.factualFrom;

/**
 * Имя LCD в формулах Ozon. До Gate 10 — общий LAST_CLOSED_DATE (принадлежит WB); после миграции —
 * OZON_LAST_CLOSED_DATE. Имя приходит ПАРАМЕТРОМ от писателя: суточный прогон переписывает окно
 * 45 суток, и зашитое имя вернуло бы мигрированные формулы к имени WB на следующий же день —
 * платформы снова оказались бы связаны. Значение по умолчанию = прежнее поведение.
 */
export const OZON_LEGACY_LCD_NAME = 'LAST_CLOSED_DATE';
export const OZON_OWN_LCD_NAME = 'OZON_LAST_CLOSED_DATE';
const LCD_NAME_RE = /^[A-Z][A-Z0-9_]*$/;
function lcdOf(name: string): string {
  if (!LCD_NAME_RE.test(name)) throw new RangeError(`имя LCD в формуле: ${name}`);
  return name;
}
const c = (start: number, off: number): string => colA1(start + off);

export interface OzonBlockParams {
  /** Первая колонка блока (12 для первого SKU). */
  start: number;
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
export function ozonBlockDayFormulas(p: OzonBlockParams, row: number, lcdName: string = OZON_LEGACY_LCD_NAME): Map<number, string> {
  const LCD = lcdOf(lcdName);
  const s = p.start;
  const D = `$${c(s, OFFSET.date)}${row}`;
  const x = (off: number): string => `${c(s, off)}${row}`;
  const g = (body: string): string => `=IF(${D}>${LCD},"",${body})`;

  const N_ = x(OFFSET.bloggers), P = x(OFFSET.orders), R = x(OFFSET.cancels);
  const V = x(OFFSET.profitAll), W = x(OFFSET.adsIn), X = x(OFFSET.adsOut);
  const Z = x(OFFSET.price), AA = x(OFFSET.spp), AB = x(OFFSET.priceSpp), AC = x(OFFSET.commission);
  const AD = x(OFFSET.priceMinusComm), AE = x(OFFSET.logistics), AF = x(OFFSET.storage);
  const AG = x(OFFSET.tax), AH = x(OFFSET.unitProfit), OD = x(OFFSET.otherDirect);
  const taxPct = `${MANAGEMENT_TAX_RESERVE_RATE * 100}%`;

  const m = new Map<number, string>();
  // Gate 9: без СПП цена покупателя НЕ выводится. Прежняя форма при пустой СПП давала
  // N("")=0 и молча утверждала «скидки нет», то есть покупатель заплатил цену продавца.
  // Замер: СПП существует в 2002 начислениях из 2003 и в среднем равна 54,4 % — утверждение
  // было бы неверным вдвое. Источника цены покупателя до доставки нет: commission_amount_rub
  // отправления пуст во всех 2003 строках, total_discount_value_rub и
  // marketing_seller_price_rub цену покупателя не воспроизводят (проверено).
  m.set(OFFSET.priceSpp,        g(`IF(OR(N(${Z})=0,${AA}=""),"",${Z}-${Z}*N(${AA})%)`));
  m.set(OFFSET.priceMinusComm,  g(`IF(N(${Z})=0,"",${Z}-${Z}*N(${AC}))`));
  // База резерва — ЦЕНА ПРОДАВЦА (Z), единообразно с WB (`AA×2%`), а не цена покупателя после
  // субсидии Ozon: баллы площадки не выручка продавца. Решение владельца, Gate 4.1.
  m.set(OFFSET.tax,             g(`IF(N(${Z})=0,"",${Z}*${taxPct})`));
  m.set(OFFSET.unitProfit,      g(`IF(N(${AD})=0,"",${AD}-N(${AE})-N(${AG})-${p.cogsTerm})`));
  // реализовано = заказы − отмены (тождество проверено: gross−cancelled == realized на всех строках)
  // Gate 6A: прочие прямые больше не подставляются литералом в формулу — они лежат в своей
  // колонке и вычитаются ссылкой. Сумма та же, но величина стала видимой в листе.
  // Gate 9: терм «в пути» УДАЛЁН. Он вычитал из количества единицы, которые ещё не доставлены,
  // потому что экономика считалась только по доставленным. Теперь единица в пути приносит свою
  // провизорную экономику (цена отправления, комиссия по тарифу, логистика по оценщику,
  // себестоимость по справочнику), и вычитать её из количества значило бы выбросить и доход,
  // и расход этого заказа. Для созревших суток в пути ничего нет — формула там побайтно прежняя.
  m.set(OFFSET.profitAll,       g(`(${P}-${R})*N(${AH})-N(${W})-N(${AF})+N(${X})-N(${OD})`));
  m.set(OFFSET.profit1,         g(`IFERROR(${V}/(${P}-${R}),"")`));
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
export function ozonSummaryDayFormulas(row: number, blockCount: number, lcdName: string = OZON_LEGACY_LCD_NAME): Map<number, string> {
  const LCD = lcdOf(lcdName);
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
 * Строка MTD внутри SKU-блока (Gate 8: полная, а не наполовину пустая).
 *
 * Политику для каждого из 25 смещений задаёт `OZON_UNITKA_SUMMARY_POLICY`; здесь только
 * её механическое выражение в формулах. Ни одного AVERAGE по дневным ячейкам: величины на
 * единицу собираются отношением агрегатов, иначе сутки с одной продажей весили бы столько
 * же, сколько сутки с двадцатью.
 *
 * Все агрегаты отсечены по `дата <= LAST_CLOSED_DATE` — как принятые SUMIF-итоги. Формулы
 * ссылаются на дневные ячейки, поэтому смена оценки на факт пересчитывает итог сама.
 */
export function ozonBlockMtdFormulas(start: number, g: OzonMonthGeometry, lcdName: string = OZON_LEGACY_LCD_NAME): Map<number, string> {
  const LCD = lcdOf(lcdName);
  const f = g.firstDailyRow, l = g.lastDailyRow, mt = g.mtdRow;
  const dc = colA1(start + OFFSET.date);
  const Dabs = `$${dc}$${f}:$${dc}$${l}`;
  const col = (off: number): string => colA1(start + off);
  const rng = (off: number): string => `${col(off)}${f}:${col(off)}${l}`;
  const at = (off: number): string => `${col(off)}${mt}`;
  // «неизвестно» не превращается в ноль: нет ни одного наблюдения за месяц — ячейка пуста.
  const sumif = (off: number): string =>
    `=IF(COUNT(${rng(off)})=0,"",SUMIF(${Dabs},"<="&${LCD},${rng(off)}))`;
  // маска закрытых суток и вес — реализованные единицы, тот же счётчик, что у «Доходности на 1 шт»
  const closed = `(${Dabs}<=${LCD})`;
  const units = `(${rng(OFFSET.orders)}-${rng(OFFSET.cancels)})`;
  /** Σ(вес × величина) по закрытым суткам. */
  const wsum = (off: number): string => `SUMPRODUCT(${closed}*${units}*${rng(off)})`;
  /** Σ(вес) только по суткам, где величина наблюдалась: пустые сутки не разбавляют среднее. */
  const wcount = (off: number): string => `SUMPRODUCT(${closed}*${units}*(${rng(off)}<>""))`;
  /** Среднее, взвешенное реализованными единицами. */
  const weighted = (off: number): string => `=IFERROR(${wsum(off)}/${wcount(off)},"")`;

  const m = new Map<number, string>();
  // ── счётчики и абсолютные суммы: сложение ────────────────────────────────────────────
  for (const off of [OFFSET.bloggers, OFFSET.views, OFFSET.opens, OFFSET.orders, OFFSET.carts,
                     OFFSET.cancels, OFFSET.profitAll, OFFSET.adsIn, OFFSET.adsOut,
                     OFFSET.storage, OFFSET.otherDirect]) {
    m.set(off, sumif(off));
  }
  // ── величины на единицу: среднее, взвешенное объёмом ────────────────────────────────
  for (const off of [OFFSET.price, OFFSET.priceSpp, OFFSET.priceMinusComm,
                     OFFSET.logistics, OFFSET.unitProfit]) {
    m.set(off, weighted(off));
  }
  // ── налог: дневная ячейка — резерв НА ЕДИНИЦУ, поэтому сумма периода собирается
  //    взвешиванием на единицы. Сложение поединичных величин дало бы бессмыслицу.
  m.set(OFFSET.tax, `=IFERROR(IF(${wcount(OFFSET.tax)}=0,"",${wsum(OFFSET.tax)}),"")`);
  // ── отношения агрегатов ─────────────────────────────────────────────────────────────
  // эффективная ставка комиссии периода = вся комиссия / вся выручка
  const revenue = `SUMPRODUCT(${closed}*${units}*${rng(OFFSET.price)})`;
  m.set(OFFSET.commission,
    `=IFERROR(SUMPRODUCT(${closed}*${units}*${rng(OFFSET.price)}*${rng(OFFSET.commission)})/${revenue},"")`);
  // СПП периода = 1 − цена покупателя периода / цена продавца периода, в процентах
  m.set(OFFSET.spp,
    `=IFERROR((1-SUMPRODUCT(${closed}*${units}*${rng(OFFSET.priceSpp)})/${revenue})*100,"")`);
  // доходность на 1 шт — принятая форма, не трогается
  m.set(OFFSET.profit1, `=IFERROR(${at(OFFSET.profitAll)}/(${at(OFFSET.orders)}-${at(OFFSET.cancels)}),"")`);
  m.set(OFFSET.drr, `=IFERROR(${at(OFFSET.adsIn)}/SUMPRODUCT(${closed}*(${rng(OFFSET.orders)}-${rng(OFFSET.bloggers)})*${rng(OFFSET.priceSpp)}),"")`);
  // ── остаток: запас, а не поток ──────────────────────────────────────────────────────
  //
  // Берётся ПОСЛЕДНИЙ ДОКАЗАННЫЙ снимок. Два ограничения, и оба содержательные:
  //   • только закрытые сутки — как у всех остальных агрегатов;
  //   • только сутки от STOCK_POLICY.factualFrom. Раньше этой даты истории остатка НЕ
  //     СУЩЕСТВУЕТ (Gate 4.1: реестр движений дал 276 шт против фактических 198), и то,
  //     что лежит в легаси-половине апреля, — отвергнутая синтетическая проекция, местами
  //     ОТРИЦАТЕЛЬНАЯ. Показать её как «остаток на конец месяца» значило бы вернуть в лист
  //     ровно ту величину, которую проект признал невоспроизводимой.
  //
  // Идиома LOOKUP(2;1/(…)) здесь НЕ используется: в Google Sheets она возвращает ошибку на
  // массиве с #DIV/0!, и ячейка молча оставалась пустой даже там, где снимок есть.
  const stockOk = `${closed}*(${Dabs}>=DATE(${STOCK_FACTUAL_FROM.slice(0, 4)},`
    + `${Number(STOCK_FACTUAL_FROM.slice(5, 7))},${Number(STOCK_FACTUAL_FROM.slice(8, 10))}))`
    + `*(${rng(OFFSET.stock)}<>"")`;
  const proven = `FILTER(${rng(OFFSET.stock)},${stockOk})`;
  m.set(OFFSET.stock, `=IFERROR(INDEX(${proven},ROWS(${proven})),"")`);
  // ── оборачиваемость: дней запаса = конечный остаток / средний суточный темп заказов ──
  const closedDays = `SUMPRODUCT(${closed}*(${Dabs}<>""))`;
  m.set(OFFSET.turnover,
    `=IFERROR(IF(OR(${at(OFFSET.stock)}="",${at(OFFSET.orders)}=0),"",${at(OFFSET.stock)}/(${at(OFFSET.orders)}/${closedDays})),"")`);
  return m;
}

/**
 * Смещения блока, которые строка MTD Ozon ОСТАВЛЯЕТ ПУСТЫМИ. После Gate 8 это только
 * подписи: у периода нет одной даты и одного дня недели. Все содержательные метрики
 * получили итог — перечень ведёт `OZON_UNITKA_SUMMARY_POLICY`.
 */
export const OZON_MTD_BLANK_OFFSETS: readonly number[] = OZON_MTD_NOT_APPLICABLE_OFFSETS;
