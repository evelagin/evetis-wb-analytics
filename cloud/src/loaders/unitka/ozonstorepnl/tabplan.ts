/**
 * D4 (OWNER ACK 15.10.2026) — ПЛАН вкладки «Ozon Магазин P&L». Редизайн представления (UX, 16.10.2026): шапка с
 * ключевыми показателями года, группы колонок, итоги по годам; финансовая модель D3 не меняется.
 *
 * Вкладка — только ПРЕДСТАВЛЕНИЕ канонического ozon_mart.V_OZON_STORE_PNL_MONTHLY (D3). Ни одной собственной
 * финансовой формулы: каждая подробная денежная колонка — колонка вью (округлённая до копеек), а колонки «всего»
 * групп и строки «Итого» года — суммы ПОКАЗАННЫХ ячеек тех же колонок (сходятся с калькулятором до копейки).
 * Маржа итога — по определению D3: результат ÷ (выручка + провизорная выручка X). Строка складывается сама:
 *   Управленческий результат = Выручка − Себестоимость − Расходы Ozon всего + Компенсации − Налог 2 % + Оценка X
 * Это то же тождество D3 (V = B − … − T + U + X; Y = V − W); проверяется на НЕокруглённых значениях вью.
 *
 * Раскладка (0-based строки): 0 заголовок, 1 подзаголовок, 2 подписи показателей, 3–4 показатели текущего и прошлого
 * года, 5 полоса групп, 6 заголовок колонок (заметки — контракт), с 7 — месяцы по годам и строка «Итого» каждого года.
 *
 * Модуль чистый: план и отпечаток. Запись — publish.ts, I/O — index.ts.
 */
import { createHash } from 'node:crypto';

export const TAB_NAME = 'Ozon Магазин P&L';
export const SOURCE_VIEW = 'ozon_mart.V_OZON_STORE_PNL_MONTHLY';
/** Версия раскладки входит в отпечаток контракта: смена раскладки — только управляемой миграцией. */
export const LAYOUT_VERSION = 'ux-2026-10-16';

export const TITLE_ROW = 0;
export const SUBTITLE_ROW = 1;
export const KPI_HEAD_ROW = 2;
export const KPI_ROWS = [3, 4] as const;
export const BAND_ROW = 5;
export const HEADER_ROW = 6;
export const FIRST_DATA_ROW = 7;
export const FROZEN_ROWS = HEADER_ROW + 1;

/** Группы колонок: подпись полосы и цвет полосы. */
export const GROUPS = {
  MONTH: { label: '', color: 'FFFFFF' },
  SALES: { label: 'Продажи', color: 'D9EAD3' },
  COGS: { label: 'Себестоимость', color: 'FCE5CD' },
  COMMISSION: { label: 'Комиссия', color: 'DDE7F4' },
  LOGISTICS: { label: 'Логистика', color: 'CFE2F3' },
  ADS: { label: 'Реклама', color: 'DDE7F4' },
  CABINET: { label: 'Кабинет', color: 'CFE2F3' },
  RESULT: { label: 'Финансовый результат', color: 'FFF2CC' },
  TRUST: { label: 'Достоверность данных', color: 'EFEFEF' },
} as const;
export type GroupKey = keyof typeof GROUPS;
/** month — подпись месяца; money — ₽; sum — «всего» группы (сумма подробных колонок); margin — доля; text — статус. */
export type ColKind = 'month' | 'money' | 'sum' | 'margin' | 'text';

export interface ColSpec { header: string; note: string; group: GroupKey; kind: ColKind; detail?: boolean; width: number }

/**
 * Контракт представления: 31 колонка A:AE. Заголовок и заметка каждой — системные данные (входят в отпечаток
 * контракта). detail — подробная колонка, свёрнута в группу (доступна кнопкой «+», основной вид не перегружает).
 */
export const TAB_COLUMNS: readonly ColSpec[] = [
  { header: 'Месяц', group: 'MONTH', kind: 'month', width: 156, note: 'Месяц P&L магазина Ozon. Источник — ozon_mart.V_OZON_STORE_PNL_MONTHLY (D3): расходы по отправлению — месяц заказа, расходы кабинета — месяц начисления, Premium — месяц периода подписки. «Итого» года — сумма показанных строк месяцев.' },
  { header: 'Выручка продавца', group: 'SALES', kind: 'money', width: 118, note: 'Реализация по цене продавца × количество из начислений Ozon (за вычетом сторно возвратов) + выкуп СНГ по документу. Бонусы и соинвестирование Ozon не входят.' },
  { header: 'Себестоимость', group: 'COGS', kind: 'money', width: 112, note: 'Себестоимость проданных единиц на дату заказа (evetis_ref.V_PRODUCT_COGS_EFFECTIVE). Возвраты себестоимость не уменьшают (решение 12.10). В части месяцев — юридическая база импорта, без полной управленческой себестоимости (follow-up D3.1).' },
  { header: 'Комиссия и эквайринг', group: 'COMMISSION', kind: 'sum', width: 112, note: 'Сумма двух колонок справа: комиссия Ozon + эквайринг. Расход. Подробно — кнопка «+» над колонками.' },
  { header: 'Комиссия Ozon', group: 'COMMISSION', kind: 'money', detail: true, width: 104, note: 'Вознаграждение Ozon из экономического блока начислений (сторно уменьшает). Расход.' },
  { header: 'Эквайринг', group: 'COMMISSION', kind: 'money', detail: true, width: 96, note: 'Тип 1, месяц заказа; положительные строки — возврат эквайринга. Расход.' },
  { header: 'Логистика и хранение', group: 'LOGISTICS', kind: 'sum', width: 112, note: 'Сумма четырёх колонок справа: логистика до покупателя, последняя миля, обратная логистика, хранение. Расход. Подробно — кнопка «+».' },
  { header: 'Логистика до покупателя', group: 'LOGISTICS', kind: 'money', detail: true, width: 104, note: 'Тип начисления 32. Расход, месяц заказа.' },
  { header: 'Последняя миля', group: 'LOGISTICS', kind: 'money', detail: true, width: 96, note: 'Типы 29, 28, 98, 30. Расход, месяц заказа.' },
  { header: 'Обратная логистика', group: 'LOGISTICS', kind: 'money', detail: true, width: 96, note: 'Типы 59, 45, 9, 6 (месяц заказа) и 78 (месяц начисления). Расход.' },
  { header: 'Хранение', group: 'LOGISTICS', kind: 'money', detail: true, width: 90, note: 'Тип 79 — временное размещение по SKU, месяц начисления. Расход.' },
  { header: 'Реклама и продвижение', group: 'ADS', kind: 'sum', width: 112, note: 'Сумма четырёх колонок справа: продвижение SKU, CPC, CPO, реклама магазина без SKU. Биллинг магазина, не атрибуция SKU. Расход. Подробно — кнопка «+».' },
  { header: 'Продвижение SKU', group: 'ADS', kind: 'money', detail: true, width: 96, note: 'Типы 116, 74, 48 (сбор отзывов, звёздные товары, бонусы продавца), месяц заказа. Расход.' },
  { header: 'Реклама CPC (биллинг)', group: 'ADS', kind: 'money', detail: true, width: 100, note: 'Оплата за клик и трафареты по финансовым начислениям Ozon: типы 41, 75, 33 и 54 по CPC-кампаниям, сутки услуги. Биллинг магазина, не атрибуция SKU. Расход.' },
  { header: 'Реклама CPO (биллинг)', group: 'ADS', kind: 'money', detail: true, width: 100, note: 'Оплата за заказ: тип 54 по CPO-кампаниям, дата списания. Биллинг магазина. Расход.' },
  { header: 'Реклама магазина без SKU', group: 'ADS', kind: 'money', detail: true, width: 100, note: 'Типы 47, 96 (баллы за отзывы, ускоренный сбор отзывов), месяц начисления. Расход.' },
  { header: 'Кабинет и прочие расходы', group: 'CABINET', kind: 'sum', width: 112, note: 'Сумма трёх колонок справа: расходы кабинета без Premium, Premium, прочие прямые расходы. Расход. Подробно — кнопка «+».' },
  { header: 'Расходы кабинета (без Premium)', group: 'CABINET', kind: 'money', detail: true, width: 104, note: 'Кросс-докинг (12) + обработка товара (77) + размещение на складах (46) + страхование (76) + корректировки услуг (57), месяц начисления. По SKU не распределяются. Расход.' },
  { header: 'Premium', group: 'CABINET', kind: 'money', detail: true, width: 90, note: 'Подписка Premium (52), разложена по месяцам периода подписки. Расход.' },
  { header: 'Прочие прямые расходы', group: 'CABINET', kind: 'money', detail: true, width: 96, note: 'Типы 39, 38, 10 (месяц заказа) и 15, 71 (месяц начисления): упаковка, материалы, утилизация, вывоз. Расход.' },
  { header: 'Расходы Ozon — всего', group: 'RESULT', kind: 'sum', width: 118, note: 'Комиссия и эквайринг + логистика и хранение + реклама и продвижение + кабинет и прочие расходы. Все расходы площадки, кроме себестоимости и налога 2 %.' },
  { header: 'Компенсации Ozon', group: 'RESULT', kind: 'money', width: 104, note: 'Товарные компенсации (25), месяц начисления. Доход.' },
  { header: 'Управленческий налог 2%', group: 'RESULT', kind: 'money', width: 104, note: '0,02 × выручка продавца, ровно один раз (провизорные единицы несут свой налог внутри оценки X). Управленческий резерв, не налог УСН.' },
  { header: 'Предварительная оценка X', group: 'RESULT', kind: 'money', width: 108, note: 'PROVISIONAL: вклад доставленных, но ещё не начисленных Ozon отправлений (цена − комиссия из выплаты − средняя логистика SKU − себестоимость − налог 2 %). Входит в результат, но не является фактом начислений. Неизвестный вход — не ноль: месяц помечается как неполный.' },
  { header: 'Управленческий результат', group: 'RESULT', kind: 'money', width: 122, note: 'Management Net Store Profit (D3) = выручка − себестоимость − расходы Ozon всего + компенсации − налог 2 % + оценка X. Не бухгалтерская прибыль: УСН, OPEX и фулфилмент не входят. Окончательного результата во вкладке пока нет: себестоимость в части единиц — юридическая база импорта до D3.1; деньги Ozon окончательны только у «сверены» при закрытом месяце.' },
  { header: 'Маржа', group: 'RESULT', kind: 'margin', width: 76, note: 'Управленческий результат ÷ (выручка продавца + провизорная выручка X). У «Итого» — от сумм года.' },
  { header: 'Результат без оценки X', group: 'RESULT', kind: 'money', width: 112, note: 'Управленческий результат минус предварительная оценка X: только подтверждённые начисления. Справочно, в итог не складывается.' },
  { header: 'Состояние месяца', group: 'TRUST', kind: 'text', width: 236, note: 'Достоверность месяца (V_OZON_STORE_MONTH_STATE): «Деньги Ozon сверены, себестоимость предварительная»; «Сверены, ждём поздних начислений»; «Ждём начислений Ozon — предварительно»; «Не доказан — нужна сверка»; «Требуется корректировка сторно D1» и «Ошибка представления начисления M2» — известные дефекты загрузки части операций, выручка месяца при этом есть; «Неполные данные источника — нужна проверка»; «Есть неразобранные операции» (публикация блокируется).' },
  { header: 'Закрытие месяца', group: 'TRUST', kind: 'text', width: 150, note: 'По водяному знаку финансов Ozon: «Открыт»; «Ждём поздних начислений» (до конца месяца + 36 суток); «Закрыт по сроку».' },
  { header: 'Свежесть источников', group: 'TRUST', kind: 'text', width: 96, note: 'Текущее состояние загрузок Ozon (V_OZON_MART_FRESHNESS) на момент публикации. Публикация выполняется только при «Свежие».' },
  { header: 'Пояснение', group: 'TRUST', kind: 'text', width: 460, note: 'Раскрытие причин из ворот D3 (V_OZON_STORE_MONTH_STATE): известные дефекты загрузки (D1 — нет сторно продажи по части возвратов; M2 — ошибка представления суммы реализации), открытые сутки контроля начислений, деньги без колонки или отправления, SKU вне карты, себестоимость не найдена, признаки незагруженного сторно, ремонт D1 без сверки, оценка X и её входы, единицы в пути, предварительная себестоимость.' },
];

export const TAB_HEADER = TAB_COLUMNS.map((c) => c.header);
export const TAB_NOTES = TAB_COLUMNS.map((c) => c.note);
export const TAB_COLS = TAB_COLUMNS.length;
const colOf = (h: string): number => {
  const i = TAB_COLUMNS.findIndex((c) => c.header === h);
  if (i < 0) throw new Error(`нет колонки «${h}»`);
  return i;
};
/** Индексы колонок (0-based) по заголовку — форматы не зависят от позиций, зашитых числом. */
export const COL = {
  MONTH: 0,
  REVENUE: colOf('Выручка продавца'),
  COGS: colOf('Себестоимость'),
  COMMISSION_SUM: colOf('Комиссия и эквайринг'),
  LOGISTICS_SUM: colOf('Логистика и хранение'),
  ADS_SUM: colOf('Реклама и продвижение'),
  CABINET_SUM: colOf('Кабинет и прочие расходы'),
  OZON_COSTS: colOf('Расходы Ozon — всего'),
  COMPENSATION: colOf('Компенсации Ozon'),
  TAX: colOf('Управленческий налог 2%'),
  X: colOf('Предварительная оценка X'),
  NET: colOf('Управленческий результат'),
  MARGIN: colOf('Маржа'),
  NET_WITHOUT_X: colOf('Результат без оценки X'),
  STATE: colOf('Состояние месяца'),
  CLOSURE: colOf('Закрытие месяца'),
  FRESHNESS: colOf('Свежесть источников'),
  REASON: colOf('Пояснение'),
} as const;

/** Колонка вью за каждой подробной денежной колонкой; «account» — сумма пяти колонок кабинета (как в D4 v1). */
const SOURCE_OF: Readonly<Record<string, string>> = {
  'Выручка продавца': 'seller_base_revenue_rub', 'Себестоимость': 'product_cogs_rub', 'Комиссия Ozon': 'commission_rub',
  'Эквайринг': 'acquiring_rub', 'Логистика до покупателя': 'logistics_rub', 'Последняя миля': 'last_mile_rub',
  'Обратная логистика': 'return_logistics_rub', 'Хранение': 'sku_storage_rub', 'Продвижение SKU': 'sku_promotion_rub',
  'Реклама CPC (биллинг)': 'cpc_billing_rub', 'Реклама CPO (биллинг)': 'cpo_billing_rub', 'Реклама магазина без SKU': 'store_ads_rub',
  'Расходы кабинета (без Premium)': 'account', 'Premium': 'premium_rub', 'Прочие прямые расходы': 'other_direct_rub',
  'Компенсации Ozon': 'marketplace_compensation_rub', 'Управленческий налог 2%': 'management_tax_rub',
  'Предварительная оценка X': 'provisional_open_rub', 'Управленческий результат': 'management_net_store_profit_rub',
};
/** Колонки «всего»: сумма показанных подробных колонок своей группы; «Расходы Ozon — всего» — сумма четырёх «всего». */
export const SUM_PARTS: Readonly<Record<number, readonly number[]>> = (() => {
  const out: Record<number, number[]> = {};
  TAB_COLUMNS.forEach((c, i) => {
    if (c.kind !== 'sum' || c.group === 'RESULT') return;
    out[i] = TAB_COLUMNS.map((d, j) => (d.group === c.group && d.detail ? j : -1)).filter((j) => j >= 0);
  });
  out[COL.OZON_COSTS] = [COL.COMMISSION_SUM, COL.LOGISTICS_SUM, COL.ADS_SUM, COL.CABINET_SUM];
  return out;
})();
/** Свёрнутые группы подробных колонок: [start, end) 0-based. */
export const DETAIL_GROUPS: ReadonlyArray<readonly [number, number]> = (() => {
  const out: Array<[number, number]> = [];
  TAB_COLUMNS.forEach((c, i) => {
    if (!c.detail) return;
    const last = out.at(-1);
    if (last && last[1] === i) last[1] = i + 1; else out.push([i, i + 1]);
  });
  return out;
})();
/** Полоса групп: подряд идущие колонки одной группы. */
export const BANDS: ReadonlyArray<{ start: number; end: number; label: string; color: string }> = (() => {
  const out: Array<{ start: number; end: number; label: string; color: string }> = [];
  TAB_COLUMNS.forEach((c, i) => {
    const last = out.at(-1);
    if (last && TAB_COLUMNS[last.start]!.group === c.group) last.end = i + 1;
    else out.push({ start: i, end: i + 1, label: GROUPS[c.group].label, color: GROUPS[c.group].color });
  });
  return out;
})();
/**
 * Блок ключевых показателей (строки 2–4) — в колонках, которые видны и при свёрнутых подробностях. Значения —
 * ячейки строк «Итого» тех же колонок вкладки, ничего нового не считается.
 */
export const KPI = [
  { label: 'Выручка', col: COL.REVENUE, from: COL.REVENUE },
  { label: 'Расходы Ozon', col: COL.COGS, from: COL.OZON_COSTS },
  { label: 'Себестоимость', col: COL.COMMISSION_SUM, from: COL.COGS },
  { label: 'Управленческий результат', col: COL.LOGISTICS_SUM, from: COL.NET },
  { label: 'Маржа', col: COL.ADS_SUM, from: COL.MARGIN },
  { label: 'в т. ч. оценка X', col: COL.CABINET_SUM, from: COL.X },
] as const;
/** Состояние периода в блоке показателей — объединённые ячейки от «Расходы Ozon — всего» до «Результат без оценки X». */
export const KPI_STATE_SPAN = [COL.OZON_COSTS, COL.NET_WITHOUT_X + 1] as const;

export const STATE_LABEL: Readonly<Record<string, string>> = {
  FINANCIAL_COMPLETE: 'Деньги Ozon сверены, себестоимость предварительная',
  FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE: 'Сверены, ждём поздних начислений',
  PARTIAL_AWAITING_ACCRUALS: 'Ждём начислений Ozon — предварительно',
  UNPROVEN: 'Не доказан — нужна сверка',
  PARTIAL_SOURCE_MISSING: 'Неполные данные источника — нужна проверка',
  PENDING_CLASSIFICATION: 'Есть неразобранные операции',
};
/**
 * PARTIAL_SOURCE_MISSING из-за известного дефекта загрузки называется дефектом, а не «нехваткой денег»: выручка
 * месяца есть, не загружена или неверно представлена часть операций (OWNER 16.10). Первым — D1.
 */
export const DEFECT_STATE_LABEL: Readonly<Record<string, string>> = {
  D1_REVERSAL_NOT_LOADED: 'Требуется корректировка сторно D1',
  M2_SALE_AMOUNT_REPRESENTATION: 'Ошибка представления начисления M2',
};
/** Короткая форма состояния — для сводки периода. */
export const STATE_SHORT: Readonly<Record<string, string>> = {
  'Деньги Ozon сверены, себестоимость предварительная': 'сверено',
  'Сверены, ждём поздних начислений': 'ждём поздних начислений',
  'Ждём начислений Ozon — предварительно': 'ждём начислений',
  'Не доказан — нужна сверка': 'нужна сверка',
  'Требуется корректировка сторно D1': 'корректировка D1',
  'Ошибка представления начисления M2': 'ошибка M2',
  'Неполные данные источника — нужна проверка': 'неполные данные',
  'Есть неразобранные операции': 'неразобранные операции',
};
export const CLOSURE_LABEL: Readonly<Record<string, string>> = {
  OPEN: 'Открыт',
  AWAITING_LATE_ACCRUALS: 'Ждём поздних начислений',
  CLOSED: 'Закрыт по сроку',
};
export const FRESHNESS_LABEL: Readonly<Record<string, string>> = {
  FRESH: 'Свежие',
  STALE: 'Устарели',
  PARTIAL_SOURCE_UPDATE: 'Обновлены не все',
  REFRESH_FAILED: 'Обновление упало',
  NEVER_INGESTED: 'Не загружались',
};
export const DEFECT_LABEL: Readonly<Record<string, string>> = {
  D1_REVERSAL_NOT_LOADED: 'D1: в загрузке нет сторно продажи по части возвратов — выручка месяца завышена на эти возвраты, остальные деньги месяца есть',
  M2_SALE_AMOUNT_REPRESENTATION: 'M2: сумма реализации записана как цена × количество, а не фактическая сумма продажи — ошибка представления, выручка месяца есть',
};
const MONTH_NAME = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
const MONTH_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
/** «2025-04» → «Апрель 2025». */
export function monthLabel(ym: string): string {
  const [y, m] = ym.split('-');
  return `${MONTH_NAME[Number(m) - 1]} ${y}`;
}

export type PnlRow = Record<string, unknown>;
export type TabCell = string | number;
export type RowKind = 'title' | 'subtitle' | 'kpiHead' | 'kpi' | 'band' | 'header' | 'month' | 'total';
export interface TabPlan {
  tab: string;
  range: string;
  header: readonly string[];
  notes: readonly string[];
  /** Вся сетка вкладки A1:AE — шапка, показатели, полоса, заголовок, месяцы и итоги. Отпечаток считается по ней. */
  grid: TabCell[][];
  /** Тип каждой строки сетки — для оформления. */
  kinds: RowKind[];
  /** Только строки месяцев, в порядке вью. */
  rows: TabCell[][];
  /** Строки месяцев каждого года в сетке: [start, end) 0-based — группы строк. */
  yearGroups: Array<{ year: string; start: number; end: number }>;
  /** Наибольшее нарушение тождеств D3 на неокруглённых значениях вью, ₽. */
  rowIdentityMaxAbs: number;
  fingerprint: string;
}

/** BigQuery отдаёт NUMERIC объектом Big, DATE — объектом с value: всё к числу/строке явно. */
export function toNum(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const n = typeof v === 'number' ? v : Number(typeof v === 'object' && v !== null && 'value' in v ? (v as { value: unknown }).value : String(v));
  if (!Number.isFinite(n)) throw new Error(`не число: ${String(v)}`);
  return n;
}
export function toStr(v: unknown): string {
  return String(v && typeof v === 'object' && 'value' in v ? (v as { value: unknown }).value : v ?? '');
}
const r2 = (x: number): number => Math.round(x * 100) / 100;
const r6 = (x: number): number => Math.round(x * 1e6) / 1e6;

/** Колонки вью, без которых строка вкладки была бы молча нулевой (toNum(undefined) = 0): их отсутствие — отказ. */
export const REQUIRED_PNL_COLUMNS = [
  'pnl_month', 'seller_base_revenue_rub', 'product_cogs_rub', 'commission_rub', 'logistics_rub', 'last_mile_rub',
  'return_logistics_rub', 'acquiring_rub', 'sku_storage_rub', 'sku_promotion_rub', 'other_direct_rub', 'cpc_billing_rub',
  'cpo_billing_rub', 'store_ads_rub', 'crossdock_rub', 'processing_rub', 'fbo_placement_rub', 'insurance_rub',
  'other_store_costs_rub', 'premium_rub', 'marketplace_compensation_rub', 'provisional_open_rub', 'provisional_revenue_rub',
  'store_contribution_pre_tax_rub', 'management_tax_rub', 'management_net_store_profit_rub', 'management_net_store_margin',
  'sku_contribution_rub', 'ads_adjustment_rub', 'unattributed_account_net_rub', 'financial_state', 'month_closure_status',
  'source_freshness_state', 'known_source_defects', 'provisional_units', 'in_transit_units',
] as const;

function defectsOf(p: PnlRow): string[] {
  return toStr(p.known_source_defects).split(',').map((x) => x.trim()).filter(Boolean);
}

/** Подпись состояния месяца: известный дефект называется дефектом (D1, затем M2), остальное — по STATE_LABEL. */
export function stateLabel(p: PnlRow): string | undefined {
  const st = toStr(p.financial_state);
  if (st === 'PARTIAL_SOURCE_MISSING') {
    const d = defectsOf(p);
    for (const code of Object.keys(DEFECT_STATE_LABEL)) if (d.includes(code)) return DEFECT_STATE_LABEL[code];
  }
  return STATE_LABEL[st];
}

/**
 * Раскрытие причин — только поля D3 (P&L и ворота V_OZON_STORE_MONTH_STATE), без новых вычислений денег. Себестоимость
 * в части единиц стоит на юридической базе импорта (проверка 15.10, follow-up D3.1): до D3.1 она раскрывается у каждого
 * месяца «сверено», чтобы ни одна строка не выглядела окончательной прибылью.
 */
export function reasonText(p: PnlRow, s: Record<string, unknown> = {}): string {
  const out: string[] = [];
  for (const d of defectsOf(p)) out.push(DEFECT_LABEL[d] ?? `известный дефект ${d}`);
  const g = (k: string): number => toNum(s[k]);
  if (g('finance_control_open_days') > 0) out.push(`контроль начислений D1: открытых суток ${g('finance_control_open_days')}`);
  if (g('unconsumed_rub') !== 0) out.push('деньги без колонки P&L');
  if (g('unlinked_posting_rub') !== 0) out.push('деньги без отправления');
  if (g('unmapped_sku_rows') > 0) out.push(`SKU вне карты: ${g('unmapped_sku_rows')} строк`);
  if (g('cogs_gap_units') > 0) out.push(`себестоимость не найдена: ${g('cogs_gap_units')} ед.`);
  if (g('reversal_suspected_postings') > 0) out.push(`признак незагруженного сторно: ${g('reversal_suspected_postings')} отпр.`);
  if (g('reversal_weak_signal_postings') > 0) out.push(`слабый признак сторно, нужна сверка: ${g('reversal_weak_signal_postings')} отпр.`);
  if (g('d1_repair_rows') > 0) out.push('ремонт D1 загружен, ждёт сверки D0.5');
  const pu = toNum(p.provisional_units), tu = toNum(p.in_transit_units);
  if (pu > 0) out.push(`доставлено без начисления: ${pu} ед. (оценка X${g('cis_without_document_postings') > 0 ? `, из них выкуп СНГ без документа: ${g('cis_without_document_postings')}` : ''})`);
  if (g('provisional_inputs_missing') > 0) out.push(`у оценки X нет входа: ${g('provisional_inputs_missing')}`);
  if (tu > 0) out.push(`в пути: ${tu} ед.${g('in_transit_without_accrual_units') > 0 ? ` (без начисления: ${g('in_transit_without_accrual_units')})` : ''}`);
  // TODO(D3.1): заменить безусловное раскрытие колонкой доказанности себестоимости из V_OZON_STORE_MONTH_STATE.
  if (toStr(p.financial_state).startsWith('FINANCIAL_COMPLETE')) out.push('себестоимость предварительная: часть единиц на юридической базе импорта (D3.1)');
  return out.join('; ');
}

/** Денежные колонки вью: NULL — не ноль, а отказ (вью обязана отдавать число). */
export const MONEY_COLUMNS = ['seller_base_revenue_rub', 'product_cogs_rub', 'commission_rub', 'logistics_rub', 'last_mile_rub',
  'return_logistics_rub', 'acquiring_rub', 'sku_storage_rub', 'sku_promotion_rub', 'other_direct_rub', 'cpc_billing_rub',
  'cpo_billing_rub', 'store_ads_rub', 'crossdock_rub', 'processing_rub', 'fbo_placement_rub', 'insurance_rub',
  'other_store_costs_rub', 'premium_rub', 'marketplace_compensation_rub', 'provisional_open_rub', 'provisional_revenue_rub',
  'store_contribution_pre_tax_rub', 'management_tax_rub', 'management_net_store_profit_rub', 'sku_contribution_rub',
  'ads_adjustment_rub', 'unattributed_account_net_rub'] as const;

/** Наибольшее нарушение тождеств D3 в строке (на неокруглённых значениях). */
export function identityResidual(p: PnlRow): number {
  const n = (k: string): number => toNum(p[k]);
  const costs = ['product_cogs_rub', 'commission_rub', 'logistics_rub', 'last_mile_rub', 'return_logistics_rub', 'acquiring_rub',
    'sku_storage_rub', 'sku_promotion_rub', 'other_direct_rub', 'cpc_billing_rub', 'cpo_billing_rub', 'store_ads_rub',
    'crossdock_rub', 'processing_rub', 'fbo_placement_rub', 'insurance_rub', 'other_store_costs_rub', 'premium_rub'];
  const v = n('seller_base_revenue_rub') - costs.reduce((s, k) => s + n(k), 0) + n('marketplace_compensation_rub') + n('provisional_open_rub');
  return Math.max(
    Math.abs(v - n('store_contribution_pre_tax_rub')),
    Math.abs(n('management_tax_rub') - 0.02 * n('seller_base_revenue_rub')),
    Math.abs(n('store_contribution_pre_tax_rub') - n('management_tax_rub') - n('management_net_store_profit_rub')),
    Math.abs(n('sku_contribution_rub') + n('ads_adjustment_rub') + n('unattributed_account_net_rub') + n('provisional_open_rub')
      - n('management_net_store_profit_rub')),
  );
}

/** Ячейки денежных колонок строки месяца: подробные — из вью (до копеек), «всего» — суммы показанных подробных. */
function moneyCells(p: PnlRow): number[] {
  const n = (k: string): number => toNum(p[k]);
  const cells = TAB_COLUMNS.map((c) => {
    const src = SOURCE_OF[c.header];
    if (src === 'account') return r2(n('crossdock_rub') + n('processing_rub') + n('fbo_placement_rub') + n('insurance_rub') + n('other_store_costs_rub'));
    return src ? r2(n(src)) : 0;
  });
  for (const i of [COL.COMMISSION_SUM, COL.LOGISTICS_SUM, COL.ADS_SUM, COL.CABINET_SUM, COL.OZON_COSTS]) {
    cells[i] = r2(SUM_PARTS[i]!.reduce((s, j) => s + cells[j]!, 0));
  }
  cells[COL.NET_WITHOUT_X] = r2(n('management_net_store_profit_rub') - n('provisional_open_rub'));
  return cells;
}

/** «апр–дек» по месяцам года в плане. */
function periodText(months: readonly string[]): string {
  const a = Number(months[0]!.slice(5, 7)), b = Number(months.at(-1)!.slice(5, 7));
  return a === b ? MONTH_SHORT[a - 1]! : `${MONTH_SHORT[a - 1]}–${MONTH_SHORT[b - 1]}`;
}

/** Сводка состояний периода: «сверено 4 · корректировка D1 5». Порядок — первое появление. */
function stateSummaryText(labels: readonly string[]): string {
  const counts = new Map<string, number>();
  for (const l of labels) counts.set(STATE_SHORT[l] ?? l, (counts.get(STATE_SHORT[l] ?? l) ?? 0) + 1);
  return [...counts.entries()].map(([k, v]) => `${k} — ${v}`).join(' · ');
}

export function buildTabPlan(pnl: readonly PnlRow[], states: readonly Record<string, unknown>[] = [], requireStates = false): TabPlan {
  const rows: TabCell[][] = [];
  const raw: Array<{ year: string; month: string; net: number; revenue: number; provRevenue: number; state: string }> = [];
  let maxAbs = 0;
  const byMonth = new Map(states.map((s) => [toStr(s.pnl_month).slice(0, 7), s]));
  if (requireStates && pnl.length && !states.length) throw new Error('нет строк V_OZON_STORE_MONTH_STATE — причины неполноты не раскрыть');
  for (const p of pnl) {
    const missing = REQUIRED_PNL_COLUMNS.filter((k) => !(k in p));
    if (missing.length) throw new Error(`${toStr(p.pnl_month)}: во вью нет колонок ${missing.join(', ')} — схема вью старее кода`);
    const nulls = MONEY_COLUMNS.filter((k) => p[k] === null || p[k] === undefined);
    if (nulls.length) throw new Error(`${toStr(p.pnl_month)}: NULL в денежных колонках ${nulls.join(', ')} — неизвестное не равно нулю`);
    const month = toStr(p.pnl_month).slice(0, 7);
    const st = byMonth.get(month);
    if ((requireStates || states.length) && !st) throw new Error(`${month}: нет строки состояния V_OZON_STORE_MONTH_STATE`);
    const state = stateLabel(p);
    if (!state) throw new Error(`${month}: неизвестное состояние «${toStr(p.financial_state)}»`);
    const closure = CLOSURE_LABEL[toStr(p.month_closure_status)];
    if (!closure) throw new Error(`${month}: неизвестное закрытие «${toStr(p.month_closure_status)}»`);
    const fresh = toStr(p.source_freshness_state);
    maxAbs = Math.max(maxAbs, identityResidual(p));
    const row: TabCell[] = moneyCells(p);
    row[COL.MONTH] = monthLabel(month);
    row[COL.MARGIN] = p.management_net_store_margin === null || p.management_net_store_margin === undefined ? '' : r6(toNum(p.management_net_store_margin));
    row[COL.STATE] = state;
    row[COL.CLOSURE] = closure;
    row[COL.FRESHNESS] = FRESHNESS_LABEL[fresh] ?? fresh;
    row[COL.REASON] = reasonText(p, st ?? {});
    rows.push(row);
    raw.push({ year: month.slice(0, 4), month, net: toNum(p.management_net_store_profit_rub), revenue: toNum(p.seller_base_revenue_rub),
      provRevenue: toNum(p.provisional_revenue_rub), state });
  }

  // Месяцы по годам + строка «Итого» года: суммы показанных ячеек (копейка в копейку), маржа — по определению D3.
  const blank = (): TabCell[] => Array<TabCell>(TAB_COLS).fill('');
  const body: TabCell[][] = [];
  const kindsBody: RowKind[] = [];
  const yearGroups: TabPlan['yearGroups'] = [];
  const totals = new Map<string, TabCell[]>();
  const years = [...new Set(raw.map((r) => r.year))];
  for (const y of years) {
    const idx = raw.map((r, i) => (r.year === y ? i : -1)).filter((i) => i >= 0);
    const start = FIRST_DATA_ROW + body.length;
    for (const i of idx) { body.push(rows[i]!); kindsBody.push('month'); }
    yearGroups.push({ year: y, start, end: start + idx.length });
    const t = blank();
    t[COL.MONTH] = `Итого ${y} · ${periodText(idx.map((i) => raw[i]!.month))}`;
    TAB_COLUMNS.forEach((c, j) => {
      if (c.kind === 'money' || c.kind === 'sum') t[j] = r2(idx.reduce((s, i) => s + (rows[i]![j] as number), 0));
    });
    const den = idx.reduce((s, i) => s + raw[i]!.revenue + raw[i]!.provRevenue, 0);
    t[COL.MARGIN] = den === 0 ? '' : r6(idx.reduce((s, i) => s + raw[i]!.net, 0) / den);
    t[COL.STATE] = stateSummaryText(idx.map((i) => raw[i]!.state));
    t[COL.REASON] = 'Сумма строк месяцев года. Окончательного результата нет: себестоимость предварительная (D3.1), у отмеченных месяцев — дефекты загрузки или ожидание начислений.';
    body.push(t); kindsBody.push('total');
    totals.set(y, t);
  }

  // Шапка: заголовок, подзаголовок, показатели текущего и прошлого года (значения — ячейки строк «Итого»).
  const title = blank();
  title[0] = 'Ozon · управленческий P&L магазина EVETIS';
  const subtitle = blank();
  const wm = pnl.length ? toStr(pnl[pnl.length - 1]!.finance_watermark_date).slice(0, 10) : '';
  subtitle[0] = `Источник: ${SOURCE_VIEW} (D3)${wm ? ` · финансы Ozon загружены по ${wm}` : ''} · `
    + 'не бухгалтерская прибыль: УСН, OPEX и фулфилмент не входят · подробные расходы — кнопки «+» над колонками';
  const kpiHead = blank();
  kpiHead[0] = 'Ключевые показатели';
  for (const k of KPI) kpiHead[k.col] = k.label;
  kpiHead[KPI_STATE_SPAN[0]] = 'Состояние периода';
  const kpiRows = KPI_ROWS.map((_, i) => {
    const r = blank();
    const y = years[years.length - 1 - i];
    if (y === undefined) return r;
    const t = totals.get(y)!;
    r[0] = String(t[COL.MONTH]).replace('Итого ', '');
    for (const k of KPI) r[k.col] = t[k.from]!;
    r[KPI_STATE_SPAN[0]] = String(t[COL.STATE]);
    return r;
  });
  const band = blank();
  for (const b of BANDS) band[b.start] = b.label;
  const header = [...TAB_HEADER];
  const grid: TabCell[][] = [title, subtitle, kpiHead, ...kpiRows, band, header, ...body];
  const kinds: RowKind[] = ['title', 'subtitle', 'kpiHead', ...KPI_ROWS.map((): RowKind => 'kpi'), 'band', 'header', ...kindsBody];
  if (grid[HEADER_ROW] !== header || grid.length !== kinds.length) throw new Error('раскладка вкладки рассогласована');
  const notes = [...TAB_NOTES];
  const range = `'${TAB_NAME}'!A1:${colLetter(TAB_COLS)}${grid.length}`;
  const fingerprint = createHash('sha256').update(JSON.stringify({ tab: TAB_NAME, range, layout: LAYOUT_VERSION, notes, grid })).digest('hex');
  return { tab: TAB_NAME, range, header, notes, grid, kinds, rows, yearGroups, rowIdentityMaxAbs: Math.round(maxAbs * 100) / 100, fingerprint };
}

/** 1 → A, 27 → AA. */
export function colLetter(n: number): string {
  let s = '';
  for (let x = n; x > 0; x = Math.floor((x - 1) / 26)) s = String.fromCharCode(65 + ((x - 1) % 26)) + s;
  return s;
}
