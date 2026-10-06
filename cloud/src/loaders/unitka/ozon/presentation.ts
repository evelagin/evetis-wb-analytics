/**
 * OZON UNITKA — слой представления (Gate 5V).
 *
 * Принцип: СЕМАНТИКА данных у Ozon своя, ПРЕДСТАВЛЕНИЕ — общее с WB. Источник визуального
 * контракта — живой `WB_Юнит_2025`, секция Сентябрь 2026 (Calendar V2). Отсюда берутся ширины,
 * высоты, шрифты, выравнивания, числовые форматы и ПАЛИТРА. Своих цветов не изобретаем.
 *
 * Перенос идёт по ВИЗУАЛЬНОЙ РОЛИ, а не по номеру колонки: у WB первый SKU-блок в M(13),
 * у Ozon в L(12), но карта смещений внутри блока совпадает 1-в-1 (доказано Gate 3).
 */
import { OZON_OFFSET as OFFSET } from './offsets.js';

/** Визуальные классы ячейки дня — ровно те, что уже существуют в WB. */
export type VisualClass = 'MANUAL' | 'FACT' | 'TARIFF' | 'CALC';

/** Роли полей блока в порядке смещений 0..24 (Gate 6A: 25 колонок, добавлена OTHER_DIRECT). */
export const OZON_FIELD_ROLES = [
  'DATE','MANUAL_EXTERNAL','IMPRESSIONS','CLICKS','ORDERS','CART','CANCELLATIONS','STOCK','TURNOVER',
  'UNIT_PROFIT','TOTAL_PROFIT','INTERNAL_ADS','EXTERNAL_ADS','DRR','SELLER_PRICE','DISCOUNT',
  'BUYER_PRICE','COMMISSION','NET_AFTER_COMMISSION','LOGISTICS','STORAGE','OTHER_DIRECT',
  'TAX_RESERVE','FINAL_UNIT_PROFIT','WEEKDAY',
] as const;
export type FieldRole = (typeof OZON_FIELD_ROLES)[number];

export const ROLE_OFFSET: Readonly<Record<FieldRole, number>> = {
  DATE: OFFSET.date, MANUAL_EXTERNAL: OFFSET.bloggers, IMPRESSIONS: OFFSET.views, CLICKS: OFFSET.opens,
  ORDERS: OFFSET.orders, CART: OFFSET.carts, CANCELLATIONS: OFFSET.cancels, STOCK: OFFSET.stock,
  TURNOVER: OFFSET.turnover, UNIT_PROFIT: OFFSET.profit1, TOTAL_PROFIT: OFFSET.profitAll,
  INTERNAL_ADS: OFFSET.adsIn, EXTERNAL_ADS: OFFSET.adsOut, DRR: OFFSET.drr, SELLER_PRICE: OFFSET.price,
  DISCOUNT: OFFSET.spp, BUYER_PRICE: OFFSET.priceSpp, COMMISSION: OFFSET.commission,
  NET_AFTER_COMMISSION: OFFSET.priceMinusComm, LOGISTICS: OFFSET.logistics, STORAGE: OFFSET.storage,
  OTHER_DIRECT: OFFSET.otherDirect,
  TAX_RESERVE: OFFSET.tax, FINAL_UNIT_PROFIT: OFFSET.unitProfit, WEEKDAY: OFFSET.weekday,
};

/**
 * Визуальный класс поля у Ozon. Там, где он расходится с WB, расхождение продиктовано
 * СЕМАНТИКОЙ площадки, а не вкусом, и перечислено в OZON_VISUAL_DIVERGENCES.
 */
export const OZON_ROLE_CLASS: Readonly<Record<FieldRole, VisualClass>> = {
  DATE: 'CALC',
  MANUAL_EXTERNAL: 'MANUAL',        // «Блогеры + самовыкупы» — ручной ввод, как в WB
  IMPRESSIONS: 'FACT', CLICKS: 'FACT', ORDERS: 'FACT', CANCELLATIONS: 'FACT',
  CART: 'CALC',                     // провенанс не доказан: поле пустое, «ожидаемого факта» тут нет
  STOCK: 'CALC', TURNOVER: 'CALC',  // истории нет до 31.08 — не обещаем импорт
  UNIT_PROFIT: 'CALC', TOTAL_PROFIT: 'CALC',
  INTERNAL_ADS: 'FACT',
  EXTERNAL_ADS: 'CALC',             // расчётный output; точная legacy-формула пока не доказана
  DRR: 'CALC',
  SELLER_PRICE: 'FACT',             // у Ozon цена приходит из финотчёта (у WB вводится руками)
  DISCOUNT: 'FACT',                 // выводится из buyer/seller (у WB вводится руками)
  BUYER_PRICE: 'CALC',
  COMMISSION: 'FACT',               // фактическая ставка сделки (у WB — тариф)
  NET_AFTER_COMMISSION: 'CALC',
  LOGISTICS: 'FACT',                // фактическая логистика (у WB — тариф)
  STORAGE: 'FACT',
  OTHER_DIRECT: 'FACT',             // Gate 6A: начисленные прямые расходы SKU, у WB колонки нет
  TAX_RESERVE: 'CALC', FINAL_UNIT_PROFIT: 'CALC', WEEKDAY: 'CALC',
};

/** Осознанные расхождения представления Ozon и WB с обоснованием от семантики. */
export const OZON_VISUAL_DIVERGENCES: ReadonlyArray<{ role: FieldRole; wb: VisualClass; ozon: VisualClass; why: string }> = [
  { role: 'SELLER_PRICE', wb: 'MANUAL', ozon: 'FACT', why: 'у Ozon цена продавца приходит из RAW_OZON_FINANCE_ACCRUAL, руками не вводится' },
  { role: 'DISCOUNT', wb: 'MANUAL', ozon: 'FACT', why: 'СПП у Ozon выводится из buyer_paid/seller_base, а не задаётся' },
  { role: 'COMMISSION', wb: 'TARIFF', ozon: 'FACT', why: 'у Ozon это фактическая ставка сделки (+эквайринг), а не тарифная константа' },
  { role: 'LOGISTICS', wb: 'TARIFF', ozon: 'FACT', why: 'у Ozon это фактическая логистика начисления, а не тариф' },
  { role: 'CART', wb: 'FACT', ozon: 'CALC', why: 'провенанс метрики не доказан; зелёный «ожидаемый факт» создал бы впечатление наблюдаемого нуля' },
  { role: 'STOCK', wb: 'FACT', ozon: 'CALC', why: 'истории остатков до 31.08.2026 не существует — импорт не обещаем' },
];

/** Роли левой сводки магазина. У Ozon нет колонки ДРР (у WB — K). */
export const OZON_SUMMARY_ROLES: ReadonlyArray<{ col: number; role: FieldRole }> = [
  { col: 1, role: 'WEEKDAY' }, { col: 2, role: 'DATE' }, { col: 3, role: 'MANUAL_EXTERNAL' },
  { col: 4, role: 'IMPRESSIONS' }, { col: 5, role: 'CLICKS' }, { col: 6, role: 'ORDERS' },
  { col: 7, role: 'CART' }, { col: 8, role: 'CANCELLATIONS' }, { col: 9, role: 'TOTAL_PROFIT' },
  { col: 10, role: 'INTERNAL_ADS' },
];

/** Сколько колонок блока накрывает объединённая ячейка заголовка SKU (контракт WB). */
export const SKU_TITLE_MERGE_WIDTH = 7;

/**
 * Роли, класс которых зависит от НАЛИЧИЯ наблюдения в конкретной ячейке (Gate 5C).
 * «Остатки» до 31.08.2026 истории не имеют и красятся как CALC, но там, где есть доказанный
 * снимок RAW_OZON_STOCKS, ячейка — наблюдаемый факт и обязана выглядеть как FACT.
 * Наблюдаемый НОЛЬ при этом остаётся фактом, а не «неизвестно».
 */
export const OBSERVATION_DEPENDENT_ROLES: readonly FieldRole[] = ['STOCK'];

export function roleClassFor(role: FieldRole, observed: boolean): VisualClass {
  if (OBSERVATION_DEPENDENT_ROLES.includes(role)) return observed ? 'FACT' : OZON_ROLE_CLASS[role];
  return OZON_ROLE_CLASS[role];
}

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 5D — ГЕОМЕТРИЯ СТРОК И ЯЗЫК ЦВЕТА
 *
 * Gate 5V сверял представление с ИЗВЛЕЧЁННЫМ контрактом и получил 0 расхождений,
 * но сам контракт был неполон: сводка вовсе не проверялась, а фон строки MTD
 * подменялся классом роли. Ниже — то, что измерено по ЖИВОМУ WB, а не выведено
 * из отдельных свойств ячейки.
 * ════════════════════════════════════════════════════════════════════════════ */

/**
 * Высоты строк секции в пикселях.
 *
 * `rowMetadata.pixelSize` у WB отдаёт 18 для дневных строк, и Google подбирает их высоту
 * по содержимому. Заголовок, шапку и итог текущее поколение движка WB приколачивает явно:
 * их хранит Октябрь 2026 (40 / 108 / 33) — эти три значения Ozon берёт как есть.
 *
 * VISUAL PARITY (2026-09-24). Строка дня 25 px была выведена из PDF-экспорта (шаг WB
 * 18,88 pt) и в браузере НЕ подтвердилась. Замер экрана владельца (масштаб 65 %, калибровка
 * по ширине колонки 84 px): заполненная строка дня WB рисуется ≈ 20,5 px, строка Ozon —
 * 25,0 px, то есть Ozon выше на 22 % и на секцию месяца уходит ≈ 135 px лишнего. 21 — ближайшее
 * целое, не меньшее подобранной высоты WB, при котором кегль 12 не обрезается.
 */
export const OZON_ROW_GEOMETRY = {
  title: 40,
  header: 108,
  day: 21,
  mtd: 33,
  spacer: 18,
} as const;
export type RowRole = keyof typeof OZON_ROW_GEOMETRY;

/** Сплошная серая полоса итога месяца. WB красит ею ВСЮ строку MTD — и сводку, и блоки. */
export const MTD_BAND_COLOUR = '#e8eaed';

/**
 * Роли, чьи булевы правила сами задают фон и поэтому обязаны стоять ВЫШЕ градиента
 * своей секции. В Google Sheets градиент и булево правило — разные слои, и верхним
 * оказывается тот, что выше в списке правил. WB держит белые правила «Доходность
 * на 1 шт» выше градиента: колонка остаётся белой, а знак передаётся цветом текста.
 * «Доходность (общая)» фон не задаёт — там градиент виден, и текст красится поверх.
 */
export const OPAQUE_BOOLEAN_ROLES: readonly FieldRole[] = ['UNIT_PROFIT'];

/** Семья условного форматирования: смысл, а не адрес. */
export interface CfFamily {
  readonly id: string;
  readonly role: FieldRole;
  /** Где действует: только сводка, только блоки, или и там и там. */
  readonly scope: 'summary' | 'block' | 'both';
  /** Пороги считаются от MAX своей секции → правило приходится заводить на секцию. */
  readonly perSection: boolean;
  readonly bg?: string;
  readonly fg?: string;
  readonly why: string;
}

/**
 * Язык цвета WB, перенесённый по ролям. Цвета взяты из живого WB — своих не изобретаем.
 * Порядок внутри массива — порядок вставки; более узкая ступень обязана стоять выше.
 */
export const OZON_CF_FAMILIES: readonly CfFamily[] = [
  { id: 'profit.total.positive', role: 'TOTAL_PROFIT', scope: 'block', perSection: false,
    fg: '#38761d', why: 'знак результата — цветом текста; фон остаётся за градиентом' },
  { id: 'profit.total.negative', role: 'TOTAL_PROFIT', scope: 'block', perSection: false,
    fg: '#cc0000', why: 'то же для убытка' },
  { id: 'profit.final.positive', role: 'FINAL_UNIT_PROFIT', scope: 'block', perSection: false,
    fg: '#38761d', why: 'у Ozon уже было правило «<0 красным», зелёного не хватало' },
  { id: 'drr.above20', role: 'DRR', scope: 'block', perSection: false,
    fg: '#cc0000', why: 'ДРР выше 20 % — красным (в сводке Ozon колонки ДРР нет)' },
  { id: 'date.weekend', role: 'DATE', scope: 'summary', perSection: false,
    bg: '#fcefe3', why: 'выходной день в колонке даты сводки' },
  { id: 'cancellations.alert', role: 'CANCELLATIONS', scope: 'both', perSection: false,
    bg: '#fce8e6', fg: '#a61c00', why: 'день закрыт, заказы были, отмен ≥3 либо ≥2 и ≥30 %' },
  { id: 'orders.noSales', role: 'ORDERS', scope: 'both', perSection: false,
    bg: '#fce8b2', why: 'показы были, заказов нет' },
  { id: 'orders.scale', role: 'ORDERS', scope: 'summary', perSection: true,
    why: 'четыре ступени зелёного от MAX своей секции' },
  { id: 'ads.scale', role: 'INTERNAL_ADS', scope: 'summary', perSection: true,
    why: 'четыре ступени бежевого от MAX своей секции' },
];

/** Ступени четырёхуровневых шкал WB: доля от MAX секции → цвет. */
export const OZON_SCALE_TIERS: Readonly<Partial<Record<FieldRole, readonly [number, string][]>>> = {
  ORDERS: [[0, '#e4f1e9'], [0.25, '#d2e8da'], [0.5, '#bfdecb'], [0.75, '#a9d4b8']],
  CART: [[0, '#eef4f6'], [0.25, '#e6eff2'], [0.5, '#dee9ee'], [0.75, '#d6e4ea']],
  INTERNAL_ADS: [[0, '#faf2e3'], [0.25, '#f5e8cd'], [0.5, '#efdcb4'], [0.75, '#e8cf99']],
};

/** Высота строки по её структурной роли в секции. Никаких «если месяц == …». */
export function rowHeightFor(role: RowRole): number {
  return OZON_ROW_GEOMETRY[role];
}

/**
 * Смещения ролей внутри сводки и внутри блока совпадают, поэтому одно правило с
 * относительными ссылками работает и там, и там: дата на 6 колонок левее «отменили»,
 * заказы — на 2; дата на 4 левее «заказов», показы — на 2.
 */
export function roleOffsetsAlign(): boolean {
  const s = new Map(OZON_SUMMARY_ROLES.map((r) => [r.role, r.col]));
  const b = (r: FieldRole) => ROLE_OFFSET[r] + 1;
  const pairs: [FieldRole, FieldRole][] = [['CANCELLATIONS', 'DATE'], ['CANCELLATIONS', 'ORDERS'],
    ['ORDERS', 'DATE'], ['ORDERS', 'IMPRESSIONS']];
  return pairs.every(([a, c]) => (s.get(a)! - s.get(c)!) === (b(a) - b(c)));
}

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 7 — ОФОРМЛЕНИЕ РОЛЕЙ, КОТОРЫХ У WB НЕТ
 *
 * CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT снят с живого WB и описывает 24 роли.
 * «Прочие прямые» — 25-я, у WB её не существует, и в снятом контракте её нет и быть
 * не может: он правится повторным замером, а не дописыванием ролей руками.
 *
 * Пока оформление таких ролей просто ОТСУТСТВОВАЛО, обе функции (статический формат и
 * ширина колонки) молча пропускали роль без спецификации. Gate 6B выпустил это в production:
 * колонка получила случайные остатки легаси-оформления (шапка #5b9bd5 в одном блоке и
 * #ed7d31 в другом), ширину 63 вместо 84, формат «#,##0» вместо рублёвого и строку итога
 * ВООБЩЕ без рамок — ту самую открытую серую полосу между «Хранение» и «налог».
 *
 * Лечение архитектурное, а не косметическое: роль без собственной спецификации обязана
 * указать роль-ОБРАЗЕЦ того же экономического класса и унаследовать её оформление целиком.
 * Своих цветов не изобретаем — язык остаётся языком WB.
 * ════════════════════════════════════════════════════════════════════════════ */

/** Роли строк, для которых контракт задаёт оформление (у `spacer` его нет). */
export type SectionRowRole = 'title' | 'header' | 'day' | 'mtd';
export const SECTION_ROW_ROLES: readonly SectionRowRole[] = ['title', 'header', 'day', 'mtd'];

/**
 * Роль Ozon → роль-образец из контракта WB.
 *
 * OTHER_DIRECT ← STORAGE. Обоснование от семантики, а не от соседства:
 *   • обе — НАЧИСЛЕННЫЙ маркетплейсом прямой расход зерна сутки × SKU (FCT.storage_rub и
 *     FCT: прочие прямые + продвижение по SKU + логистика нереализованных). В языке шапки WB
 *     это зелёный #b7e1cd — «факт площадки», а не голубой тариф и не серый расчёт;
 *   • OZON_ROLE_CLASS даёт обеим класс FACT, то есть один фон дня #f1f8f4;
 *   • обе — абсолютная сумма суток в рублях, формат «#,##0 ₽», выравнивание вправо.
 * «логистика» отпадает: у WB она TARIFF (голубая шапка ставки). «налог» отпадает: он CALC,
 * расчётный резерв, и его серая шапка сказала бы «это производная», чего о начислении сказать
 * нельзя.
 */
export const OZON_STYLE_TEMPLATE: Readonly<Partial<Record<FieldRole, FieldRole>>> = {
  OTHER_DIRECT: 'STORAGE',
};

export interface BorderOverride {
  readonly row: SectionRowRole;
  readonly role: FieldRole;
  readonly side: 'top' | 'bottom' | 'left' | 'right';
  readonly style: string;
  readonly why: string;
}

/**
 * Правки рамок, вызванные ИМЕННО вставкой 25-й колонки, и никакие другие.
 *
 * В строке итога у WB прямые расходы обведены в общий короб: «логистика» держит левую стену
 * (left SOLID_MEDIUM), «Хранение» — правую (right SOLID_MEDIUM), между ними тонкая SOLID.
 * «Прочие прямые» — прямой расход той же семьи и обязаны оказаться ВНУТРИ короба, иначе
 * жирная линия пройдёт посреди расходной группы, а новая колонка повиснет снаружи —
 * ровно то «отдельной технической полосой», на что смотрит владелец.
 *
 * Наследование от STORAGE само приносит правую стену новой колонке (left SOLID,
 * right SOLID_MEDIUM). Остаётся снять стену с «Хранения»: короб растёт с двух ячеек до трёх.
 * Это единственное касание соседней колонки во всём Gate 7.
 */
export const OZON_BORDER_OVERRIDES: readonly BorderOverride[] = [
  { row: 'mtd', role: 'STORAGE', side: 'right', style: 'SOLID',
    why: 'короб прямых расходов в строке итога растёт вправо и закрывается уже на «Прочих прямых»' },
];
