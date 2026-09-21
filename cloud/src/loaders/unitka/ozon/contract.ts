/**
 * OZON UNITKA ADAPTER — контракт листа `OZON_Юнит_2025`.
 *
 * Общий слой (календарь, локаль формул, colA1, карта смещений OFFSET) переиспользуется из WB без
 * изменений. Здесь — ТОЛЬКО то, чем Ozon отличается от WB:
 *   • сводка магазина A..J (10 колонок, БЕЗ ДРР) против A..K у WB;
 *   • первый SKU-блок в колонке L (12) против M (13) у WB;
 *   • источники фактов и правила доступности — ozon_raw / ozon_mart;
 *   • отмены НЕ штрафуются (решение владельца D-3), в отличие от WB.
 * Ширина блока с Gate 6A — 25 против 24 у WB: добавлена колонка «Прочие прямые», которой
 * у WB нет. Карта смещений вынесена в ./offsets.js; WB-константа не меняется.
 */
import { OZON_BLOCK_WIDTH } from './offsets.js';

/**
 * Геометрия листа Ozon. Ширина берётся из числа смещений OZON_OFFSET, а не задаётся числом
 * отдельно, — иначе карта и геометрия могли бы разойтись незаметно.
 */
export const OZON_GEOMETRY = {
  BLOCK_FIRST_COLUMN: 12, // L
  BLOCK_WIDTH: OZON_BLOCK_WIDTH,
  BLOCK_BODY_WIDTH: OZON_BLOCK_WIDTH - 1,
  SHEET_TITLE: 'OZON_Юнит_2025',
} as const;

export function ozonSlotStart(slot: number): number {
  if (!Number.isInteger(slot) || slot < 0) throw new RangeError(`слот ${slot}`);
  return OZON_GEOMETRY.BLOCK_FIRST_COLUMN + slot * OZON_GEOMETRY.BLOCK_WIDTH;
}

/** Левая сводка магазина: A..J. ДРР в сводке Ozon НЕТ (у WB — колонка K). */
export const OZON_SUMMARY = {
  weekday: 1, date: 2, bloggers: 3, views: 4, opens: 5,
  orders: 6, carts: 7, cancels: 8, profit: 9, ads: 10,
} as const;

/** Колонка сводки ↔ смещение в блоке. Сумма блоков секции обязана сходиться с колонкой сводки. */
export const OZON_SUMMARY_TO_OFFSET: ReadonlyArray<readonly [number, number, string]> = [
  [OZON_SUMMARY.bloggers, 1, 'bloggers'],
  [OZON_SUMMARY.views, 2, 'views'],
  [OZON_SUMMARY.opens, 3, 'opens'],
  [OZON_SUMMARY.orders, 4, 'orders'],
  [OZON_SUMMARY.carts, 5, 'carts'],
  [OZON_SUMMARY.cancels, 6, 'cancels'],
  [OZON_SUMMARY.profit, 10, 'profit'],
  [OZON_SUMMARY.ads, 11, 'ads'],
];

/**
 * Управленческий резерв под налог. ЭТО НЕ БУХГАЛТЕРСКАЯ СТАВКА.
 * Правовой режим: ИП, УСН «доходы минус расходы», номинальная ставка 15 %. Налог считается
 * агрегированно нарастающим итогом (п. 5 ст. 346.18 НК РФ) с минимальным налогом 1 % (п. 6)
 * ВНЕ Юнитки; моделирование настоящей налоговой базы — отдельный будущий проект.
 * 2 % — временное управленческое допущение юнит-экономики на инвестиционной стадии.
 * Источник истины — ZZ_CONFIG!TAX_RESERVE_RATE. Имя USN_RATE намеренно НЕ используется.
 */
export const MANAGEMENT_TAX_RESERVE_RATE = 0.02;

/**
 * База резерва — ЦЕНА ПРОДАВЦА (seller base price, смещение 14), единообразно с WB Unitka
 * (`=AA×2%`). НЕ цена покупателя после субсидии Ozon: баллы и софинансирование площадки
 * не являются выручкой продавца и не могут быть базой управленческого резерва.
 * Решение владельца, Gate 4.1.
 */
export const TAX_RESERVE_BASE = 'SELLER_BASE_PRICE' as const;

/** Правило доступности значения для ячейки. «Неизвестно» НИКОГДА не пишется нулём. */
export type Availability = 'FACT' | 'FORMULA' | 'BLANK_NOT_INGESTED' | 'BLANK_SOURCE_ABSENT' | 'BLANK_MANUAL';

/** Смещение блока → источник факта и правило доступности (доказано Gate 2/3/4). */
export const OZON_FIELD_SOURCE_MAP: ReadonlyArray<{
  offset: number; field: string; availability: Availability; source: string;
}> = [
  { offset: 0,  field: 'Дата',               availability: 'FACT',                source: 'календарь секции' },
  { offset: 1,  field: 'Блогеры+самовыкупы', availability: 'BLANK_MANUAL',        source: '—' },
  { offset: 2,  field: 'Показы',             availability: 'FACT',                source: 'RAW_OZON_ADS_SKU_DAILY.impressions' },
  { offset: 3,  field: 'Переходы',           availability: 'FACT',                source: 'RAW_OZON_ADS_SKU_DAILY.clicks' },
  { offset: 4,  field: 'Заказы факт',        availability: 'FACT',                source: 'FCT_OZON_SKU_PNL_DAILY.gross_qty' },
  { offset: 5,  field: 'Положили в корзину', availability: 'BLANK_SOURCE_ABSENT', source: 'провенанс не доказан (Gate 2): ads cart_adds ≠ метрика листа' },
  { offset: 6,  field: 'Отменили товаров',   availability: 'FACT',                source: 'FCT_OZON_SKU_PNL_DAILY.cancelled_qty' },
  { offset: 7,  field: 'Остатки',            availability: 'BLANK_NOT_INGESTED',  source: 'истории нет до 2026-08-31; реестр движений не сходится' },
  { offset: 8,  field: 'Оборачиваемость',    availability: 'BLANK_NOT_INGESTED',  source: 'производная от остатка' },
  { offset: 9,  field: 'Доходность на 1 шт', availability: 'FORMULA',             source: '= Доходность общая / реализовано' },
  { offset: 10, field: 'Доходность (общая)', availability: 'FORMULA',             source: '= реализовано × доходность 1 шт − реклама' },
  { offset: 11, field: 'Реклама внутренняя', availability: 'FACT',                source: 'FCT_OZON_SKU_PNL_DAILY.ad_spend_attributed_rub (АТРИБУЦИЯ, не биллинг)' },
  { offset: 12, field: 'Внешняя реклама',    availability: 'BLANK_MANUAL',        source: '—' },
  { offset: 13, field: 'ДРР',                availability: 'FORMULA',             source: '= реклама / (заказы × цена с СПП)' },
  { offset: 14, field: 'цена',               availability: 'FACT',                source: 'FCT.seller_base_revenue_rub / realized_qty' },
  { offset: 15, field: 'СПП %',              availability: 'FACT',                source: '1 − buyer_paid/seller_base (баллы Ozon + софинансирование)' },
  { offset: 16, field: 'цена с СПП',         availability: 'FORMULA',             source: '= цена × (1 − СПП%)' },
  { offset: 17, field: 'комиссия',           availability: 'FACT',                source: 'FCT.commission_rub / seller_base_revenue_rub (ФАКТ, не 0,396)' },
  { offset: 18, field: 'цена минус комиссия',availability: 'FORMULA',             source: '= цена × (1 − комиссия)' },
  { offset: 19, field: 'логистика',          availability: 'FACT',                source: 'FCT.logistics_rub / realized_qty (ФАКТ, не 72 ₽)' },
  { offset: 20, field: 'Хранение',           availability: 'FACT',                source: 'FCT.storage_rub (type 79, со sku); FBO-хранение type 46 без sku остаётся расходом кабинета' },
  { offset: 21, field: 'Прочие прямые',      availability: 'FACT',                source: 'FCT: прочие прямые + продвижение по SKU + логистика по нереализованным + эквайринг без выручки (Gate 6A)' },
  { offset: 22, field: 'налог',              availability: 'FORMULA',             source: '= цена (seller base) × MANAGEMENT_TAX_RESERVE_RATE' },
  { offset: 23, field: 'доходность 1 шт',    availability: 'FORMULA',             source: '= цена−комиссия − логистика − налог − канон. COGS' },
  { offset: 24, field: 'день недели',        availability: 'FACT',                source: 'календарь секции' },
];

/**
 * Расходы, которые ЕСТЬ в каноническом источнике, но которым нет колонки в контракте Юнитки.
 * Перечислены явно, чтобы результат листа нельзя было спутать с полным P&L площадки.
 */
export const OZON_COSTS_OUTSIDE_CONTRACT = [
  'acquiring_rub — эквайринг (нет колонки)',
  'other_direct_marketplace_costs_rub — утилизация/вывоз/упаковка (нет колонки)',
  'логистика по ОТМЕНЁННЫМ заказам — нет реализованных единиц, не на что умножать',
  'storage — приходит без sku',
  'расходы уровня кабинета (подписка, биллинг рекламы) — вне зерна SKU',
  'возвраты — не загружаются (NULL, не 0)',
] as const;

/**
 * ПОЛИТИКА ОСТАТКОВ — закрыта решением владельца (Gate 4.1). Историческая реконструкция ОТКЛОНЕНА.
 * Доказательство: реестр движений на 31.08.2026 дал 276 шт против фактических 198 (+78, +39,4 %).
 * Причины неустранимы из источника: quantity_accepted = NULL во всех строках поставок,
 * arrival_date = NULL, количества в списаниях/вывозе/утилизации = NULL, возвраты не загружаются.
 * Синтетическая легаси-проекция остатка НЕ воспроизводится ни при каких условиях.
 */
export const STOCK_POLICY = {
  blankThrough: '2026-08-30',
  factualFrom: '2026-08-31',
  factualSource: 'ozon_raw.RAW_OZON_STOCKS',
  syntheticProjectionAllowed: false,
} as const;

/** Дата, начиная с которой остаток/оборачиваемость берутся из факта. Раньше — только BLANK. */
export function stockAvailability(isoDate: string): Availability | 'FACT' {
  return isoDate >= STOCK_POLICY.factualFrom ? 'FACT' : 'BLANK_NOT_INGESTED';
}

/**
 * ПОЛИТИКА КОРЗИНЫ — остаётся закрытой. Провенанс исторической метрики не доказан (Gate 2:
 * лист 596 против ads cart_adds 413 за 01–16.04). Рекламная атрибуция НЕ является заменой.
 * Не блокирует писателя Юнитки; владелец может позднее предоставить выгрузку Ozon Seller.
 */
export const CART_POLICY = { status: 'SOURCE_NOT_PROVEN', substituteWithAdsCartAdds: false } as const;

/* ───────────────────────── прямые расходы вне 23-польного контракта ───────────────────────── */

/** Куда расход может быть отнесён по зерну источника. */
export type CostAttribution = 'SKU_ATTRIBUTABLE' | 'DATE_ATTRIBUTABLE' | 'ORDER_ATTRIBUTABLE' | 'ACCOUNT_ONLY' | 'NOT_ATTRIBUTABLE';

export interface DirectCostCategory {
  key: string;
  label: string;
  typeIds: readonly number[];
  attribution: CostAttribution;
  /** Экономически принадлежит юнит-экономике Юнитки? */
  belongsInUnitka: boolean;
  /** Представим ли БЕЗ изменения видимого контракта из 23 колонок? */
  representableWithoutNewColumn: boolean;
  /** Существующий механизм WB, если он есть. */
  wbMechanism: string | null;
  note: string;
}

/**
 * Расходы, которые ЕСТЬ в каноническом источнике, но сейчас не попадают в результат листа.
 * Перечень существует, чтобы разрыв нельзя было потерять молча: сверка обязана его показать.
 */
export const OZON_DIRECT_COST_GAP: readonly DirectCostCategory[] = [
  {
    key: 'acquiring', label: 'Эквайринг', typeIds: [1],
    attribution: 'SKU_ATTRIBUTABLE', belongsInUnitka: true, representableWithoutNewColumn: true,
    wbMechanism: 'WB складывает эквайринг в ставку комиссии (ZZ_CONFIG!TOTAL_COMMISSION_RATE = тариф + эквайринг; bq.ts commissionRate)',
    note: 'приходит с sku, без posting_number — зерно сутки × SKU',
  },
  {
    key: 'cancellation_logistics', label: 'Логистика по отменённым отправлениям', typeIds: [32, 59, 29],
    attribution: 'ORDER_ATTRIBUTABLE', belongsInUnitka: true, representableWithoutNewColumn: false,
    wbMechanism: 'WB моделирует обратное плечо ставкой REVERSE_LEG_RATE × отменённые единицы',
    note: 'у Ozon есть ФАКТИЧЕСКАЯ сумма, а не ставка; в дни без реализованных единиц умножать не на что',
  },
  {
    key: 'other_direct', label: 'Прочие прямые сборы', typeIds: [45, 15, 71, 39, 38],
    attribution: 'SKU_ATTRIBUTABLE', belongsInUnitka: true, representableWithoutNewColumn: false,
    wbMechanism: null,
    note: 'обработка возвратов/отмен, утилизация, вывоз, упаковка',
  },
  {
    key: 'storage_fbo', label: 'Хранение FBO', typeIds: [46],
    attribution: 'ACCOUNT_ONLY', belongsInUnitka: false, representableWithoutNewColumn: true,
    wbMechanism: 'у WB хранение приходит по SKU и живёт в колонке «Хранение»',
    note: 'у Ozon приходит БЕЗ sku — на SKU не раскладывается',
  },
];

/** Суммарный неучтённый прямой расход. Возвращает разрыв, который обязана показать сверка. */
export function directCostGapTotal(a: { acquiring: number; cancellationLogistics: number; otherDirect: number }): number {
  return a.acquiring + a.cancellationLogistics + a.otherDirect;
}

/**
 * Тождество сверки: результат листа = вклад BQ после рекламы + неучтённые прямые расходы
 * − управленческий налоговый резерв. Разрыв входит в тождество ЯВНО и исчезнуть молча не может.
 */
export function expectedSheetResult(a: {
  bqContributionAfterAds: number; acquiring: number; cancellationLogistics: number;
  otherDirect: number; taxReserve: number;
}): number {
  return a.bqContributionAfterAds + directCostGapTotal(a) - a.taxReserve;
}

/* ───────────────────────── якорь LCD листа Ozon (Gate 5A §5) ───────────────────────── */

/**
 * Зеркало LAST_CLOSED_DATE на листе Ozon. Нужно потому, что Sheets API НЕ принимает именованный
 * диапазон внутри CUSTOM_FORMULA условного форматирования (проверено: «Invalid ConditionValue»),
 * а ссылку на ячейку принимает. WB решает это так же — зеркалом $WY$736.
 * Ячейка лежит ПРАВЕЕ хвоста книги, вне блочной геометрии, поэтому вставка колонок новых SKU
 * её не задевает: Google сдвигает и ячейку, и именованный диапазон вместе с хвостом.
 * Значение — формула, а не записанное число: обновляется от канонического источника само.
 */
export const OZON_LCD = {
  namedRange: 'OZON_LCD_MIRROR',
  formula: '=LAST_CLOSED_DATE',
  source: 'ZZ_CONFIG!B2 (именованный диапазон LAST_CLOSED_DATE)',
  label: 'LAST_CLOSED_DATE (зеркало для условного форматирования)',
  /** Колонку ищем по именованному диапазону, а не по константе — как WB ищет anchorCol. */
  resolveByNamedRange: true,
} as const;

/**
 * КОМИССИЯ = фактическая комиссия + фактический эквайринг (Gate 5A §2).
 * Концепция взята у WB (ZZ_CONFIG!TOTAL_COMMISSION_RATE = тариф + эквайринг), но ставка НЕ
 * копируется: для Ozon доля считается из фактов своей площадки на своём зерне.
 * Эквайринг, вошедший в комиссию, НЕ попадает в OTHER_DIRECT_COST_RUB — двойного счёта нет.
 */
export const COMMISSION_INCLUDES_ACQUIRING = true;

/**
 * OTHER_DIRECT_COST_RUB — внутренняя каноническая величина зерна СУТКИ × SKU.
 * Содержит ТОЛЬКО доказанные фактические расходы площадки, не представленные другим полем Юнитки:
 *   • прочие прямые сборы (type 45/15/71/39/38);
 *   • логистика суток, где нет реализованных единиц (её не на что умножить в поунитной колонке);
 *   • эквайринг суток без выручки (его не во что вложить — ставка комиссии не определена).
 * НЕ содержит: эквайринг суток с выручкой (он уже в комиссии), представленную логистику,
 * хранение уровня магазина, биллинг рекламы уровня кабинета.
 * Видимой колонки не создаёт; попадает в «Доходность (общая)» явным слагаемым и остаётся
 * аудируемым по дате, SKU, type_id и сумме.
 */
export interface OtherDirectCostInput {
  otherDirectFees: number;      // прочие прямые сборы
  logisticsUnrepresented: number; // логистика суток без реализованных единиц
  acquiringUnrepresented: number; // эквайринг суток без выручки
}
export function otherDirectCostRub(i: OtherDirectCostInput): number {
  return i.otherDirectFees + i.logisticsUnrepresented + i.acquiringUnrepresented;
}

/**
 * Канонический результат Юнитки (Gate 5A §9). Эквайринг входит ровно один раз —
 * либо через комиссию, либо через OTHER_DIRECT_COST_RUB.
 */
export function canonicalUnitkaResult(a: {
  sellerRevenue: number; cogs: number; commissionInclAcquiring: number;
  representedLogistics: number; otherDirectCost: number; internalAds: number; taxReserve: number;
}): number {
  return a.sellerRevenue - a.cogs - a.commissionInclAcquiring - a.representedLogistics
       - a.otherDirectCost - a.internalAds - a.taxReserve;
}

/**
 * АПРЕЛЬ 2026 — ГИБРИДНЫЙ месяц периода миграции, НЕ канонический эталон (Gate 5A §3).
 * 01–16 посчитаны легаси-моделью и заморожены; 17–30 восстановлены по контракту Gate 4.1,
 * в котором эквайринг ещё НЕ входил в комиссию и OTHER_DIRECT_COST_RUB ещё не существовал.
 * Итог апреля 7 994,64 ₽ эталоном полного месяца считать нельзя.
 * Первый полностью канонический месяц — МАЙ 2026.
 */
export const APRIL_2026_IS_HYBRID_MIGRATION_MONTH = true;
export const FIRST_FULLY_CANONICAL_MONTH = '2026-05' as const;

/* ───────────────────────── геометрия произвольного закрытого месяца (Gate 5B) ───────────────────────── */

const RU_MONTH = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'] as const;

export interface OzonSectionGeometry {
  key: string; title: string; daysInMonth: number;
  titleRow: number; headerRow: number; firstDailyRow: number; lastDailyRow: number; mtdRow: number;
  blocks: readonly string[]; lastColumn: number;
}

/**
 * Секция месяца выводится из ПРЕДЫДУЩЕЙ секции по правилу Calendar V2: шаг = дни предыдущего + 4.
 * Ни одна строка не задаётся вручную — движок одинаков для любого закрытого месяца.
 */
export function ozonSectionGeometry(
  year: number, month: number, prevTitleRow: number, prevDaysInMonth: number, blocks: readonly string[],
): OzonSectionGeometry {
  if (month < 1 || month > 12) throw new RangeError(`месяц ${month}`);
  if (blocks.length < 1) throw new RangeError('нужен хотя бы один блок');
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const titleRow = prevTitleRow + prevDaysInMonth + 4;
  const headerRow = titleRow + 1;
  const firstDailyRow = titleRow + 2;
  const lastDailyRow = firstDailyRow + daysInMonth - 1;
  const mtdRow = lastDailyRow + 1;
  const lastColumn = ozonSlotStart(blocks.length - 1) + OZON_GEOMETRY.BLOCK_WIDTH - 1;
  if (mtdRow - titleRow !== daysInMonth + 2) throw new Error('геометрия секции');
  return { key: `${year}-${String(month).padStart(2,'0')}`, title: `${RU_MONTH[month-1]} ${year}`,
           daysInMonth, titleRow, headerRow, firstDailyRow, lastDailyRow, mtdRow, blocks, lastColumn };
}

/**
 * Жизненный цикл SKU: блок появляется с даты первой ДОКАЗАННОЙ активности и не переписывает
 * более ранние месяцы. Возвращает набор блоков секции = принятые ранее + впервые активные.
 */
export function resolveMonthBlocks(
  previousBlocks: readonly string[], firstActivity: Readonly<Record<string, string>>, monthKey: string,
): { blocks: string[]; added: string[] } {
  const added = Object.entries(firstActivity)
    .filter(([o, d]) => !previousBlocks.includes(o) && d.slice(0, 7) <= monthKey)
    .map(([o]) => o)
    .sort();
  return { blocks: [...previousBlocks, ...added], added };
}

/**
 * ХРАНЕНИЕ. Политика: пусто, пока источник не доказан привязанным к SKU. Тип 79
 * («Временное размещение товара партнерами») приходит С sku — значит атрибутируем и пишется
 * как АБСОЛЮТНАЯ сумма суток (как «Хранение» у WB), а не как величина на единицу.
 */
export function storageIsAttributable(typeId: number, hasSku: boolean): boolean {
  return typeId === 79 && hasSku;
}

/* ───────────────────────── доказанность снимка остатков (Gate 5C) ───────────────────────── */

export type StockSnapshotVerdict = 'PROVEN_SNAPSHOT' | 'UNPROVEN_SNAPSHOT' | 'NO_SNAPSHOT';

/**
 * Снимок остатка доказателен ТОЛЬКО если снят в тот же календарный день, которым помечен.
 * `/v1/analytics/stocks` отдаёт ТЕКУЩЕЕ состояние, поэтому съём на следующий день уже включает
 * движения этого следующего дня и состояние помеченной даты не описывает.
 * Именно на этом основании 31.08.2026 остался пустым (снят 01.09) — решение Gate 5B.
 * Интерполяция, forward-fill, backward-fill и реконструкция остатка из продаж запрещены.
 */
export function classifyStockSnapshot(snapshotDate: string | null, extractedAtIso: string | null): StockSnapshotVerdict {
  if (!snapshotDate || !extractedAtIso) return 'NO_SNAPSHOT';
  return extractedAtIso.slice(0, 10) === snapshotDate ? 'PROVEN_SNAPSHOT' : 'UNPROVEN_SNAPSHOT';
}

/** Остаток пишется в лист только для доказанного снимка И только за закрытые сутки. */
export function stockIsWritable(snapshotDate: string, extractedAtIso: string | null, lastClosedDate: string): boolean {
  return classifyStockSnapshot(snapshotDate, extractedAtIso) === 'PROVEN_SNAPSHOT' && snapshotDate <= lastClosedDate;
}

/** Сутки закрыты (можно писать факты) или будущие (только формулы под защитой LCD). */
export function isClosedDay(isoDate: string, lastClosedDate: string): boolean {
  return isoDate <= lastClosedDate;
}
