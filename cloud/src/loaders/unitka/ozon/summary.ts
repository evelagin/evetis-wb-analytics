/**
 * OZON UNITKA — ПОЛИТИКА АГРЕГАЦИИ СТРОКИ ИТОГА МЕСЯЦА (Gate 8).
 *
 * До Gate 8 строка итога SKU-блока была наполовину пустой: из 25 колонок она заполняла 11,
 * а одиннадцать финансовых оставляла пустыми (`OZON_MTD_BLANK_OFFSETS`). Пустыми они были не
 * потому, что итог бессмысленен, а потому, что шаблон достался от WB, где их тоже не было.
 *
 * Главное правило: НИКАКИХ AVERAGE(дневные ячейки). Среднее средних — это не средняя величина
 * периода. Цена, комиссия, логистика и доходность на единицу живут в дневных ячейках КАК
 * ВЕЛИЧИНЫ НА ЕДИНИЦУ, и сутки с одной продажей весят в таком среднем столько же, сколько
 * сутки с двадцатью. Поэтому итог считается отношением АГРЕГАТОВ: числитель и знаменатель
 * собираются по всему периоду, и только потом делятся.
 *
 * ВЕС. Вес суток — реализованные единицы, выраженные в листе как «заказы − отмены». Это тот
 * же счётчик, которым уже пользуется принятый итог «Доходность на 1 шт» (V/(P−R)), и второй
 * счётчик заводить нельзя. В открытых сутках он завышен на единицы «в пути», но в отношении
 * числитель и знаменатель завышаются одинаково, поэтому средние остаются верными.
 *
 * ОТСЕЧКА. Все агрегаты ограничены закрытыми сутками (`дата <= LAST_CLOSED_DATE`) — так же,
 * как принятые SUMIF-итоги. Незакрытые сутки в итог не попадают ни через какую формулу.
 *
 * ПРОВИЗОРНОСТЬ. Формулы ссылаются на дневные ячейки, а не на замороженные числа. Когда
 * оценка комиссии или логистики сменяется фактом, дневная ячейка меняется — и итог месяца
 * пересчитывается сам, без перезаписи строки итога.
 */
import { OZON_OFFSET as OFFSET } from './offsets.js';
import { ROLE_OFFSET, type FieldRole } from './presentation.js';

/** Как величина периода получается из суточных наблюдений. */
export type SummaryPolicy =
  | 'SUM'                  // сложение: счётчики и абсолютные суммы
  | 'WEIGHTED_AVERAGE'     // среднее, взвешенное реализованными единицами
  | 'RATIO_OF_AGGREGATES'  // отношение двух агрегатов периода
  | 'ENDING_BALANCE'       // последнее наблюдение закрытых суток
  | 'NOT_APPLICABLE';      // итога не существует

/** Чем является ДНЕВНАЯ ячейка — от этого зависит, что с ней законно делать. */
export type DailyBasis = 'COUNT' | 'ABSOLUTE' | 'PER_UNIT' | 'RATIO' | 'LABEL' | 'BALANCE';

export interface SummaryRule {
  readonly policy: SummaryPolicy;
  readonly basis: DailyBasis;
  readonly why: string;
}

/**
 * Политика для ВСЕХ 25 смещений блока. Неклассифицированных колонок не остаётся —
 * регрессия это проверяет.
 */
export const OZON_UNITKA_SUMMARY_POLICY: Readonly<Record<FieldRole, SummaryRule>> = {
  DATE: { policy: 'NOT_APPLICABLE', basis: 'LABEL', why: 'у периода нет одной даты' },
  WEEKDAY: { policy: 'NOT_APPLICABLE', basis: 'LABEL', why: 'у периода нет дня недели' },

  MANUAL_EXTERNAL: { policy: 'SUM', basis: 'COUNT', why: 'единицы блогеров и самовыкупов складываются' },
  IMPRESSIONS: { policy: 'SUM', basis: 'COUNT', why: 'показы складываются' },
  CLICKS: { policy: 'SUM', basis: 'COUNT', why: 'переходы складываются' },
  ORDERS: { policy: 'SUM', basis: 'COUNT', why: 'заказы складываются' },
  CART: { policy: 'SUM', basis: 'COUNT', why: 'складывается там, где наблюдение вообще есть; пусто остаётся пустым' },
  CANCELLATIONS: { policy: 'SUM', basis: 'COUNT', why: 'отмены складываются' },

  STOCK: { policy: 'ENDING_BALANCE', basis: 'BALANCE',
    why: 'остаток — запас, а не поток: за месяц он не складывается. Берётся последний ДОКАЗАННЫЙ снимок закрытых суток' },
  TURNOVER: { policy: 'RATIO_OF_AGGREGATES', basis: 'RATIO',
    why: 'дней запаса = конечный остаток / средний суточный темп заказов периода' },

  UNIT_PROFIT: { policy: 'RATIO_OF_AGGREGATES', basis: 'PER_UNIT',
    why: 'доходность общая периода / реализованные единицы периода — принятая форма, не трогается' },
  TOTAL_PROFIT: { policy: 'SUM', basis: 'ABSOLUTE', why: 'доходность суток — абсолютная величина' },
  INTERNAL_ADS: { policy: 'SUM', basis: 'ABSOLUTE', why: 'расход рекламы — абсолютная величина' },
  EXTERNAL_ADS: { policy: 'SUM', basis: 'ABSOLUTE', why: 'внешняя реклама — абсолютная величина' },
  DRR: { policy: 'RATIO_OF_AGGREGATES', basis: 'RATIO',
    why: 'реклама периода / выручка периода — принятая форма, не трогается' },

  SELLER_PRICE: { policy: 'WEIGHTED_AVERAGE', basis: 'PER_UNIT',
    why: 'СРЕДНЯЯ ЦЕНА РЕАЛИЗАЦИИ периода = вся выручка / все реализованные единицы. Простое среднее по суткам уравняло бы день с одной продажей и день с двадцатью' },
  DISCOUNT: { policy: 'RATIO_OF_AGGREGATES', basis: 'RATIO',
    why: 'СПП периода = 1 − цена покупателя периода / цена продавца периода' },
  BUYER_PRICE: { policy: 'WEIGHTED_AVERAGE', basis: 'PER_UNIT', why: 'цена покупателя на единицу, взвешенная объёмом' },
  COMMISSION: { policy: 'RATIO_OF_AGGREGATES', basis: 'RATIO',
    why: 'ЭФФЕКТИВНАЯ СТАВКА периода = вся комиссия / вся выручка. Среднее суточных ставок дало бы ставку несуществующего дня' },
  NET_AFTER_COMMISSION: { policy: 'WEIGHTED_AVERAGE', basis: 'PER_UNIT', why: 'цена за вычетом комиссии на единицу, взвешенная объёмом' },
  LOGISTICS: { policy: 'WEIGHTED_AVERAGE', basis: 'PER_UNIT',
    why: 'СРЕДНЯЯ ЛОГИСТИКА НА РЕАЛИЗОВАННУЮ ЕДИНИЦУ, а не сумма логистики: решение владельца Gate 8 §I' },
  STORAGE: { policy: 'SUM', basis: 'ABSOLUTE', why: 'ХРАНЕНИЕ ЗА ПЕРИОД: дневная ячейка — абсолютная сумма суток' },
  OTHER_DIRECT: { policy: 'SUM', basis: 'ABSOLUTE', why: 'ПРОЧИЕ ПРЯМЫЕ ЗА ПЕРИОД: дневная ячейка — абсолютная сумма суток' },
  TAX_RESERVE: { policy: 'SUM', basis: 'PER_UNIT',
    why: 'РЕЗЕРВ ЗА ПЕРИОД. Дневная ячейка — резерв НА ЕДИНИЦУ (цена × 2 %), поэтому сумма периода собирается взвешиванием на единицы, а не сложением поединичных величин' },
  FINAL_UNIT_PROFIT: { policy: 'WEIGHTED_AVERAGE', basis: 'PER_UNIT',
    why: 'доходность 1 шт периода = вся доходность единиц / все реализованные единицы (Gate 8 §M)' },
};

/** Смещения, которые строка итога ОСТАВЛЯЕТ пустыми. После Gate 8 — только подписи. */
export const OZON_MTD_NOT_APPLICABLE_OFFSETS: readonly number[] =
  (Object.keys(OZON_UNITKA_SUMMARY_POLICY) as FieldRole[])
    .filter((r) => OZON_UNITKA_SUMMARY_POLICY[r].policy === 'NOT_APPLICABLE')
    .map((r) => ROLE_OFFSET[r]);

/** Роли, у которых политика не задана. Регрессия требует пустоты. */
export function unclassifiedSummaryRoles(): string[] {
  return (Object.keys(ROLE_OFFSET) as FieldRole[])
    .filter((r) => OZON_UNITKA_SUMMARY_POLICY[r] === undefined);
}

/** Машиночитаемая карта «смещение → политика» для отчёта и приёмки. */
export function summaryPolicyMap(): Array<{ offset: number; role: FieldRole; policy: SummaryPolicy; basis: DailyBasis }> {
  return (Object.keys(ROLE_OFFSET) as FieldRole[])
    .map((role) => ({ offset: ROLE_OFFSET[role], role,
      policy: OZON_UNITKA_SUMMARY_POLICY[role].policy, basis: OZON_UNITKA_SUMMARY_POLICY[role].basis }))
    .sort((a, b) => a.offset - b.offset);
}

/** Смещения, чей итог считается взвешиванием на реализованные единицы. */
export const UNIT_WEIGHTED_OFFSETS: readonly number[] = [
  OFFSET.price, OFFSET.priceSpp, OFFSET.priceMinusComm, OFFSET.logistics, OFFSET.unitProfit, OFFSET.tax,
];
