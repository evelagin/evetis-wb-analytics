/**
 * PHASE C (C3) + UX (2026-10-16) — ПЛАН вкладки «WB Магазин P&L»: шапка с ключевыми показателями года, группы
 * колонок, месяцы по годам и строка «Итого» каждого года.
 *
 * Только план. Этот модуль не пишет лист и не создаёт вкладку. Отпечаток плана (sha256) — то, что публикуется:
 * писатель обязан записать ровно этот план. Источник — wb_mart.V_WB_STORE_PNL_PRESENTATION; модель и суммы не
 * меняются. Колонки «всего» групп и строки «Итого» — суммы ПОКАЗАННЫХ ячеек тех же колонок; маржа итога — Σ прибыль ÷
 * Σ выручка (определение вью).
 *
 * Строка складывается сама: Чистая прибыль = Вклад SKU + поправки к факту − расходы кабинета WB + доходы WB −
 * списание утраченного товара + компенсация WB за утрату товара. «Налог 2 %» — расшифровка внутри «Вклада SKU», в
 * прибыль повторно не входит. «Чистый эффект форс-мажора» и «Результат без чрезвычайных статей» — справочно.
 * «Мост сроков» — справочно и в прибыль НЕ входит: пока когорта не созрела, это сроки WB, а не экономия.
 *
 * Раскладка (0-based строки): 0 заголовок, 1 подзаголовок, 2 подписи показателей, 3–4 показатели текущего и прошлого
 * года, 5 полоса групп, 6 заголовок колонок (заметки — контракт), с 7 — месяцы по годам и «Итого» года.
 */
import { createHash } from 'node:crypto';
import type { PnlRow } from './bq.js';

export const TAB_NAME = 'WB Магазин P&L';
/** Версия раскладки входит в отпечаток контракта: смена раскладки — только управляемой миграцией. */
export const LAYOUT_VERSION = 'wb-ux-2026-10-16';

export const TITLE_ROW = 0;
export const SUBTITLE_ROW = 1;
export const KPI_HEAD_ROW = 2;
export const KPI_ROWS = [3, 4] as const;
export const BAND_ROW = 5;
export const HEADER_ROW = 6;
export const FIRST_DATA_ROW = 7;
export const FROZEN_ROWS = HEADER_ROW + 1;

export const GROUPS = {
  MONTH: { label: '', color: 'FFFFFF' },
  SALES: { label: 'Продажи', color: 'D9EAD3' },
  CONTRIB: { label: 'Вклад', color: 'DDE7F4' },
  ADJ: { label: 'Поправки', color: 'CFE2F3' },
  CABINET: { label: 'Кабинет WB', color: 'DDE7F4' },
  INCOME: { label: 'Доходы WB', color: 'D9EAD3' },
  EXTRA: { label: 'Чрезвычайные статьи', color: 'FCE5CD' },
  RESULT: { label: 'Финансовый результат', color: 'FFF2CC' },
  TRUST: { label: 'Достоверность данных', color: 'EFEFEF' },
} as const;
export type GroupKey = keyof typeof GROUPS;
/** month — подпись месяца; money — ₽; sum — «всего» группы (сумма подробных колонок); margin — доля; text — статус. */
export type ColKind = 'month' | 'money' | 'sum' | 'margin' | 'text';
export interface ColSpec { header: string; note: string; group: GroupKey; kind: ColKind; detail?: boolean; width: number; ref?: boolean }

/**
 * Контракт представления: 26 колонок A:Z. Заголовок и заметка каждой — системные данные (входят в отпечаток контракта).
 * detail — подробная колонка, свёрнута в группу; ref — справочная, в прибыль не складывается.
 */
export const TAB_COLUMNS: readonly ColSpec[] = [
  { header: 'Месяц', group: 'MONTH', kind: 'month', width: 150, note: 'Месяц услуги (P&L по месяцу услуги WB). Источник: wb_mart.V_WB_STORE_PNL_PRESENTATION. «Итого» года — сумма показанных строк месяцев.' },
  { header: 'Выручка', group: 'SALES', kind: 'money', width: 112, note: 'Продажи по цене продавца (до СПП), за вычетом возвратов. История — база финотчёта WB по проданным единицам; текущие месяцы до созревания — выкупы Юнитки по цене продавца с поправкой на факт. Справочно: в прибыль не складывается отдельно.' },
  { header: 'Вклад SKU', group: 'CONTRIB', kind: 'money', width: 108, note: 'Сумма вклада товаров: выручка − удержание WB (комиссия, эквайринг) − логистика − налог 2 % − себестоимость − хранение по SKU (с 01.09.2026; раньше хранение целиком в «Поправке хранения») − реклама по атрибуции + внешняя реклама. Уже включает налог 2 % и себестоимость. Входит в прибыль.' },
  { header: 'Налог 2% (в составе вклада SKU)', group: 'CONTRIB', kind: 'money', detail: true, ref: true, width: 100, note: 'Расшифровка: управленческий налог 2 % от цены продавца, УЖЕ вычтенный внутри «Вклада SKU». В чистую прибыль повторно НЕ вычитается. Реальный налог УСН не моделируется.' },
  { header: 'Поправки к факту — всего', group: 'ADJ', kind: 'sum', width: 108, note: 'Сумма четырёх колонок справа: поправки рекламы, хранения, комиссии и цены, зрелой когорты. Входит в прибыль. Подробно — кнопка «+».' },
  { header: 'Поправка рекламы', group: 'ADJ', kind: 'money', detail: true, width: 100, note: 'Атрибуция рекламы в SKU минус фактический биллинг WB по дате услуги. «+» — биллинг меньше атрибуции, прибыль выше. Входит в прибыль; документы «WB Продвижение» не используются.' },
  { header: 'Поправка хранения', group: 'ADJ', kind: 'money', detail: true, width: 100, note: 'Хранение, учтённое в SKU, минус факт платного хранения WB. «−» — факт больше. Входит в прибыль. В истории хранения по SKU нет — вся сумма факта здесь.' },
  { header: 'Поправка комиссии и цены', group: 'ADJ', kind: 'money', detail: true, width: 100, note: 'Модельная комиссия и цена листа против фактического удержания WB и базы финотчёта на тех же проданных единицах. В истории 0: вклад уже на факте. Входит в прибыль.' },
  { header: 'Поправка зрелой когорты', group: 'ADJ', kind: 'money', detail: true, width: 100, note: 'После созревания когорты (финотчёты покрыли месяц + 46 суток): фактическая логистика против модели минус ожидаемый вклад так и не проданных единиц. До созревания 0, сумма — в «Мосте сроков». Входит в прибыль.' },
  { header: 'Расходы кабинета WB — всего', group: 'CABINET', kind: 'sum', width: 108, note: 'Сумма пяти колонок справа: минимальный платёж, утилизация, штрафы, транзит, прочие расходы WB. Расход: уменьшает прибыль. Подробно — кнопка «+».' },
  { header: 'Минимальный платёж WB', group: 'CABINET', kind: 'money', detail: true, width: 100, note: 'Счёт WB «Остаток по минимальному платежу», отнесён к месяцу услуги. Расход: уменьшает прибыль. В «Вкладе SKU» не учтён.' },
  { header: 'Утилизация', group: 'CABINET', kind: 'money', detail: true, width: 96, note: 'Счёт WB за утилизацию «за <месяц> <год>», по месяцу услуги из подписи. Расход: уменьшает прибыль.' },
  { header: 'Штрафы', group: 'CABINET', kind: 'money', detail: true, width: 90, note: 'Штрафы WB без привязки к заказу, по месяцу проводки. Расход: уменьшает прибыль.' },
  { header: 'Транзит', group: 'CABINET', kind: 'money', detail: true, width: 90, note: 'Удержания WB за транзит поставок. Расход: уменьшает прибыль.' },
  { header: 'Прочие расходы WB', group: 'CABINET', kind: 'money', detail: true, width: 100, note: 'Прочие счета кабинета WB (платная приёмка, «Баллы за отзывы» — аванс минус возврат остатка и т. п.). Расход: уменьшает прибыль.' },
  { header: 'Доходы / компенсации от WB', group: 'INCOME', kind: 'money', width: 108, note: 'Компенсации и возвраты WB уровня кабинета (добровольная компенсация при возврате, компенсация ущерба по заказу). Доход: увеличивает прибыль. Компенсация за утраченный товар — в «Чрезвычайных статьях»; возмещения издержек WB — MEMO, сюда не входят.' },
  { header: 'Списание утраченного товара', group: 'EXTRA', kind: 'money', width: 108, note: 'Себестоимость товара, утраченного на складах WB (пожары после атак БПЛА 18.07–16.08.2026): количество — поштучно по отчётам WB о пострадавших товарах, себестоимость — каноническая на дату события. Месяц — месяц события. Только непроданный товар: проданные единицы уже в себестоимости «Вклада SKU». Расход: уменьшает прибыль.' },
  { header: 'Компенсация WB за утрату товара', group: 'EXTRA', kind: 'money', width: 108, note: 'Выплаты WB за товар, пострадавший в результате обстоятельств непреодолимой силы (финотчёт WB, «Добровольная выплата…»). Месяц — месяц проводки выплаты. Доход: увеличивает прибыль. Со списанием не неттуется.' },
  { header: 'Чистый эффект форс-мажора (справочно)', group: 'EXTRA', kind: 'money', ref: true, width: 108, note: 'Компенсация WB за утрату товара − списание утраченного товара. Справочно: в прибыль уже вошло через две колонки слева, повторно не складывается.' },
  { header: 'Управленческая чистая прибыль магазина', group: 'RESULT', kind: 'money', width: 120, note: 'Вклад SKU + поправки к факту − расходы кабинета WB + доходы WB − списание утраченного товара + компенсация WB за утрату товара. Налог 2 % уже внутри вклада. Не бухгалтерская: УСН, OPEX и фулфилмент не входят.' },
  { header: 'Управленческая чистая маржа', group: 'RESULT', kind: 'margin', width: 84, note: 'Управленческая чистая прибыль ÷ выручка. У «Итого» — от сумм года.' },
  { header: 'Результат без чрезвычайных статей (справочно)', group: 'RESULT', kind: 'money', ref: true, width: 116, note: 'Управленческая чистая прибыль минус чистый эффект форс-мажора: результат обычной деятельности магазина. Справочно, в итог не складывается.' },
  { header: 'Сопоставимый результат без наследия (справочно)', group: 'RESULT', kind: 'money', ref: true, width: 116, note: 'Управленческая чистая прибыль месяцев БЕЗ статуса «наследие» (LEGACY_PARTIAL_KNOWN_DEFECTS — август 2026, справочная сумма Phase C: ячейка месяца пуста). Месяцы «Ждём счета WB» и прочие неполные — включены. Чрезвычайные статьи остальных месяцев включены. В «Итого» — сумма месяцев без наследия: справочно, НЕ полный финансовый результат года (полный — «Управленческая чистая прибыль магазина»). Решение владельца 16.10.' },
  { header: 'Финансовое состояние', group: 'TRUST', kind: 'text', width: 200, note: 'Закрыт; Закрыт, когорта дозревает; Ждём счета WB; Есть неразобранные операции; Есть неизвестные расходы; Справочно: наследие (август 2026 — сравнение). Не влияет на сумму.' },
  { header: 'Мост сроков (справочно, вне прибыли)', group: 'TRUST', kind: 'money', ref: true, width: 108, note: 'Пока когорта не созрела: логистика модели минус факт и ожидаемый вклад ещё не проданных единиц. В прибыль НЕ входит — это сроки WB, а не экономия.' },
  { header: 'Пояснение', group: 'TRUST', kind: 'text', width: 420, note: 'Причины неполноты месяца — только из полей вью: неразобранные операции и неизвестные расходы финотчёта, нефинальный финотчёт, недостающие дни биллинга рекламы, ожидаемые счета WB, дозревание когорты, утрата товара, источник месяца (история H0 / текущий Phase C / наследие листа).' },
];

export const TAB_HEADER = TAB_COLUMNS.map((c) => c.header);
export const TAB_NOTES = TAB_COLUMNS.map((c) => c.note);
export const TAB_COLS = TAB_COLUMNS.length;
const colOf = (h: string): number => {
  const i = TAB_COLUMNS.findIndex((c) => c.header === h);
  if (i < 0) throw new Error(`нет колонки «${h}»`);
  return i;
};
/** Индексы колонок (0-based) по заголовку — форматы и состояние не зависят от позиций, зашитых числом. */
export const COL = {
  MONTH: 0,
  REVENUE: colOf('Выручка'),
  SKU: colOf('Вклад SKU'),
  TAX: colOf('Налог 2% (в составе вклада SKU)'),
  ADJ_SUM: colOf('Поправки к факту — всего'),
  CABINET_SUM: colOf('Расходы кабинета WB — всего'),
  INCOME: colOf('Доходы / компенсации от WB'),
  LOSS: colOf('Списание утраченного товара'),
  LOSS_COMP: colOf('Компенсация WB за утрату товара'),
  FM_NET: colOf('Чистый эффект форс-мажора (справочно)'),
  NET: colOf('Управленческая чистая прибыль магазина'),
  MARGIN: colOf('Управленческая чистая маржа'),
  NET_EX_FM: colOf('Результат без чрезвычайных статей (справочно)'),
  NET_COMPARABLE: colOf('Сопоставимый результат без наследия (справочно)'),
  STATE: colOf('Финансовое состояние'),
  BRIDGE: colOf('Мост сроков (справочно, вне прибыли)'),
  REASON: colOf('Пояснение'),
} as const;

/** Колонки «всего»: сумма показанных подробных колонок своей группы. */
export const SUM_PARTS: Readonly<Record<number, readonly number[]>> = (() => {
  const out: Record<number, number[]> = {};
  TAB_COLUMNS.forEach((c, i) => {
    if (c.kind !== 'sum') return;
    out[i] = TAB_COLUMNS.map((d, j) => (d.group === c.group && d.detail ? j : -1)).filter((j) => j >= 0);
  });
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
/** Ключевые показатели — над своими же колонками (подпись = короткое имя колонки), значения — из строк «Итого». */
export const KPI: ReadonlyArray<{ label: string; col: number }> = [
  { label: 'Выручка', col: COL.REVENUE },
  { label: 'Вклад SKU', col: COL.SKU },
  { label: 'Поправки к факту', col: COL.ADJ_SUM },
  { label: 'Расходы кабинета WB', col: COL.CABINET_SUM },
  { label: 'Доходы WB', col: COL.INCOME },
  { label: 'Форс-мажор, нетто', col: COL.FM_NET },
  { label: 'Управленческая прибыль', col: COL.NET },
  { label: 'Маржа', col: COL.MARGIN },
  { label: 'Без форс-мажора', col: COL.NET_EX_FM },
  { label: 'Сопоставимый, без наследия', col: COL.NET_COMPARABLE },
];
/** Сводка состояний периода в блоке показателей — объединённые ячейки «Достоверность данных». */
export const KPI_STATE_SPAN = [COL.STATE, COL.REASON + 1] as const;

export const STATE_LABEL: Readonly<Record<string, string>> = {
  LEGACY_PARTIAL_KNOWN_DEFECTS: 'Справочно: наследие, известные дефекты',
  FINANCIAL_COMPLETE: 'Закрыт',
  FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE: 'Закрыт, когорта дозревает',
  PARTIAL_AWAITING_ACCOUNT_INVOICE: 'Ждём счета WB',
  PENDING_CLASSIFICATION: 'Есть неразобранные операции',
  UNKNOWN_COST_PRESENT: 'Есть неизвестные расходы',
};
export const STATE_SHORT: Readonly<Record<string, string>> = {
  'Справочно: наследие, известные дефекты': 'наследие',
  'Закрыт': 'закрыт',
  'Закрыт, когорта дозревает': 'закрыт, когорта дозревает',
  'Ждём счета WB': 'ждём счета WB',
  'Есть неразобранные операции': 'неразобранные операции',
  'Есть неизвестные расходы': 'неизвестные расходы',
};
const MONTH_NAME = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
const MONTH_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
/** «2026-05» → «Май 2026». */
export function monthLabel(ym: string): string {
  const [y, m] = ym.split('-');
  return `${MONTH_NAME[Number(m) - 1]} ${y}`;
}

export type TabCell = string | number;
export type RowKind = 'title' | 'subtitle' | 'kpiHead' | 'kpi' | 'band' | 'header' | 'month' | 'total';
export interface TabPlan {
  tab: string;
  range: string;
  header: readonly string[];
  notes: readonly string[];
  /** Вся сетка вкладки A1:Z — шапка, показатели, полоса, заголовок, месяцы и итоги. Отпечаток считается по ней. */
  grid: TabCell[][];
  kinds: RowKind[];
  /** Только строки месяцев, в порядке вью. */
  rows: TabCell[][];
  yearGroups: Array<{ year: string; start: number; end: number }>;
  /** Проверка сложения каждой строки: |Σ слагаемых − чистая прибыль| ≤ 0,02 ₽. */
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
const r2 = (x: number): number => Math.round(x * 100) / 100;
const r6 = (x: number): number => Math.round(x * 1e6) / 1e6;
/** 1234.5 → «1 234,50» (русская запись суммы в тексте пояснения). */
const rub = (x: number): string => r2(x).toFixed(2).replace('.', ',').replace(/\B(?=(\d{3})+(?!\d))/g, ' ').replace(/^-/, '−');
const str = (v: unknown): string => String(v && typeof v === 'object' && 'value' in v ? (v as { value: unknown }).value : v ?? '');
const bool = (v: unknown): boolean => v === true || v === 'true';

/** Колонки вью, без которых строка вкладки была бы молча нулевой (toNum(undefined) = 0): их отсутствие — отказ. */
export const REQUIRED_PNL_COLUMNS = ['management_tax_rub', 'inventory_loss_writeoff_rub', 'wb_loss_compensation_rub',
  'force_majeure_net_effect_rub', 'management_net_store_profit_rub', 'authority', 'inventory_loss_evidence_rows'] as const;

/** Пояснение месяца — только поля вью, без новых вычислений денег. */
export function reasonText(p: PnlRow): string {
  const n = (k: string): number => toNum(p[k]);
  const out: string[] = [];
  const st = str(p.financial_state);
  const authority = str(p.authority);
  if (st === 'LEGACY_PARTIAL_KNOWN_DEFECTS') out.push('наследие листа (август 2026): известные дефекты формул, только сравнение');
  else if (authority === 'HISTORICAL_CANONICAL') out.push('история: канонический расчёт по финотчёту WB (H0)');
  if (!bool(p.month_finance_final)) out.push('финотчёт месяца ещё не финальный');
  if (n('pending_rows') > 0) out.push(`неразобранных операций финотчёта: ${n('pending_rows')}`);
  if (n('unknown_rub') > 0.5) out.push(`неизвестные расходы: ${rub(n('unknown_rub'))} ₽`);
  if (n('unconsumed_finance_rub') > 0.5) out.push(`деньги финотчёта без колонки P&L: ${rub(n('unconsumed_finance_rub'))} ₽`);
  if (n('ads_days') > 0 && n('ads_final_days') < n('ads_days')) out.push(`реклама: финальный биллинг за ${n('ads_final_days')} из ${n('ads_days')} дн.`);
  if (!bool(p.account_invoice_window_closed)) {
    const miss = [n('minimum_payment_invoices') > 0 ? '' : 'минимальный платёж', n('utilization_invoices') > 0 ? '' : 'утилизация'].filter(Boolean);
    if (miss.length) out.push(`ждём счетов WB: ${miss.join(', ')}`);
  }
  if (!bool(p.sku_month_closed) && st !== 'LEGACY_PARTIAL_KNOWN_DEFECTS') out.push('когорта дозревает (мост сроков — справочно)');
  if (n('inventory_loss_qty') > 0) out.push(`утрата товара: ${n('inventory_loss_qty')} ед. (списание по себестоимости)`);
  // Неполный месяц без раскрытой причины — честная пометка, а не пустая ячейка.
  if (!st.startsWith('FINANCIAL_COMPLETE') && out.every((x) => x.startsWith('история') || x.startsWith('утрата'))) out.push('причина неполноты во вью не раскрыта');
  return out.join('; ');
}

/** Денежные ячейки строки месяца: подробные — из вью (до копеек), «всего» — суммы показанных подробных. */
function monthCells(p: PnlRow): TabCell[] {
  const n = (k: string): number => toNum(p[k]);
  const row: TabCell[] = Array<TabCell>(TAB_COLS).fill('');
  const set = (h: string, v: number): void => { row[colOf(h)] = r2(v); };
  set('Выручка', n('revenue_rub'));
  set('Вклад SKU', n('sku_contribution_rub'));
  set('Налог 2% (в составе вклада SKU)', n('management_tax_rub'));
  set('Поправка рекламы', n('ads_adjustment_rub'));
  set('Поправка хранения', n('storage_adjustment_rub'));
  set('Поправка комиссии и цены', n('commission_adjustment_rub') + n('price_adjustment_rub'));
  set('Поправка зрелой когорты', n('mature_cohort_adjustment_rub'));
  set('Минимальный платёж WB', n('minimum_payment_rub'));
  set('Утилизация', n('utilization_rub'));
  set('Штрафы', n('penalty_rub'));
  set('Транзит', n('transit_rub'));
  set('Прочие расходы WB', n('acceptance_rub') + n('other_marketplace_cost_rub'));
  set('Доходы / компенсации от WB', n('marketplace_income_rub'));
  set('Списание утраченного товара', n('inventory_loss_writeoff_rub'));
  set('Компенсация WB за утрату товара', n('wb_loss_compensation_rub'));
  set('Чистый эффект форс-мажора (справочно)', n('force_majeure_net_effect_rub'));
  set('Управленческая чистая прибыль магазина', n('management_net_store_profit_rub'));
  set('Результат без чрезвычайных статей (справочно)', n('management_net_store_profit_rub') - n('force_majeure_net_effect_rub'));
  set('Мост сроков (справочно, вне прибыли)', n('timing_bridge_logistics_rub') - n('timing_bridge_unsettled_margin_rub'));
  for (const [i, parts] of Object.entries(SUM_PARTS)) row[Number(i)] = r2(parts.reduce((s, j) => s + (row[j] as number), 0));
  row[COL.MARGIN] = r6(n('management_net_store_margin'));
  return row;
}

function periodText(months: readonly string[]): string {
  const a = Number(months[0]!.slice(5, 7)), b = Number(months.at(-1)!.slice(5, 7));
  return a === b ? MONTH_SHORT[a - 1]! : `${MONTH_SHORT[a - 1]}–${MONTH_SHORT[b - 1]}`;
}
function stateSummaryText(labels: readonly string[]): string {
  const counts = new Map<string, number>();
  for (const l of labels) counts.set(STATE_SHORT[l] ?? l, (counts.get(STATE_SHORT[l] ?? l) ?? 0) + 1);
  return [...counts.entries()].map(([k, v]) => `${k} — ${v}`).join(' · ');
}

export function buildTabPlan(pnl: readonly PnlRow[]): TabPlan {
  const rows: TabCell[][] = [];
  const raw: Array<{ year: string; month: string; net: number; revenue: number; state: string }> = [];
  let maxAbs = 0;
  for (const p of pnl) {
    const missing = REQUIRED_PNL_COLUMNS.filter((k) => !(k in p));
    if (missing.length) throw new Error(`${String(p.month)}: во вью нет колонок ${missing.join(', ')} — схема вью старее кода`);
    const n = (k: string): number => toNum(p[k]);
    const state = str(p.financial_state);
    const label = STATE_LABEL[state];
    if (!label) throw new Error(`${String(p.month)}: неизвестное состояние «${state}»`);
    // Тождество — на НЕокруглённых значениях вью (14 слагаемых по ±0,005 съели бы допуск 0,02 ₽).
    maxAbs = Math.max(maxAbs,
      Math.abs(n('sku_contribution_rub') + n('ads_adjustment_rub') + n('storage_adjustment_rub') + n('commission_adjustment_rub')
        + n('price_adjustment_rub') + n('mature_cohort_adjustment_rub') - n('minimum_payment_rub') - n('utilization_rub') - n('penalty_rub')
        - n('transit_rub') - n('acceptance_rub') - n('other_marketplace_cost_rub') + n('marketplace_income_rub')
        - n('inventory_loss_writeoff_rub') + n('wb_loss_compensation_rub') - n('management_net_store_profit_rub')),
      Math.abs(n('wb_loss_compensation_rub') - n('inventory_loss_writeoff_rub') - n('force_majeure_net_effect_rub')));
    const month = str(p.month).slice(0, 7);
    const row = monthCells(p);
    row[COL.MONTH] = monthLabel(month);
    row[COL.STATE] = label;
    // Показатель C (решение владельца 16.10): месяц наследия в сопоставимый результат не входит — ячейка пуста, не 0.
    row[COL.NET_COMPARABLE] = state === 'LEGACY_PARTIAL_KNOWN_DEFECTS' ? '' : r2(n('management_net_store_profit_rub'));
    row[COL.REASON] = reasonText(p);
    rows.push(row);
    raw.push({ year: month.slice(0, 4), month, net: n('management_net_store_profit_rub'), revenue: n('revenue_rub'), state: label });
  }

  // Месяцы по годам + «Итого» года: суммы показанных ячеек, маржа — Σ прибыль ÷ Σ выручка.
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
      // Пустая ячейка (месяц наследия в сопоставимом результате) не число и в сумму не входит.
      if (c.kind !== 'money' && c.kind !== 'sum') return;
      const nums = idx.map((i) => rows[i]![j]).filter((v): v is number => typeof v === 'number');
      t[j] = nums.length ? r2(nums.reduce((s, v) => s + v, 0)) : '';
    });
    const rev = idx.reduce((s, i) => s + raw[i]!.revenue, 0);
    t[COL.MARGIN] = rev === 0 ? '' : r6(idx.reduce((s, i) => s + raw[i]!.net, 0) / rev);
    t[COL.STATE] = stateSummaryText(idx.map((i) => raw[i]!.state));
    // Наследие (август 2026 — справочная сумма Phase C) входит в сумму года как есть; размер раскрыт явно (решение владельца — оставить или исключить).
    const legacy = idx.filter((i) => raw[i]!.state === STATE_LABEL.LEGACY_PARTIAL_KNOWN_DEFECTS);
    const legacyNet = legacy.reduce((x, i) => x + (rows[i]![COL.NET] as number), 0);
    t[COL.REASON] = 'Сумма строк месяцев года. Состояние — сводка месяцев; окончательны только «Закрыт».'
      + (legacy.length ? ` Включает справочный месяц наследия: ${legacy.map((i) => rows[i]![COL.MONTH]).join(', ')} (прибыль ${rub(legacyNet)} ₽).` : '');
    body.push(t); kindsBody.push('total');
    totals.set(y, t);
  }

  const title = blank();
  title[0] = 'WB · управленческий P&L магазина EVETIS';
  const subtitle = blank();
  subtitle[0] = 'Источник: wb_mart.V_WB_STORE_PNL_PRESENTATION (история H0 + Phase C) · не бухгалтерская прибыль: УСН, OPEX и '
    + 'фулфилмент не входят · налог 2 % уже внутри «Вклада SKU» · подробности — кнопки «+» над колонками';
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
    for (const k of KPI) r[k.col] = t[k.col]!;
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
