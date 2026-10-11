/**
 * PHASE C (C3) — ПЛАН вкладки «WB Магазин P&L»: одна строка на месяц услуги.
 *
 * Только план. Этот модуль не пишет лист и не создаёт вкладку: создание и запись — отдельным
 * решением владельца («OWNER ACK — CREATE/WRITE WB МАГАЗИН P&L»). Отпечаток плана (sha256) —
 * то, что владелец подтверждает: писатель обязан записать ровно этот план.
 *
 * Строка складывается сама: Чистая прибыль = Вклад SKU + поправки (реклама, хранение, комиссия и цена,
 * логистика когорты) − расходы кабинета (мин. платёж, утилизация, штрафы, транзит, прочие) + доходы
 * − списание утраченного товара + компенсация WB за утрату товара. «Чистый эффект форс-мажора» — справочно.
 * «Мост сроков» — справочно и в прибыль НЕ входит: пока когорта не созрела, разница логистики и
 * ожидаемый вклад ещё не проданных единиц — это сроки WB, а не экономия.
 */
import { createHash } from 'node:crypto';
import type { PnlRow } from './bq.js';

export const TAB_NAME = 'WB Магазин P&L';

/**
 * Контракт представления (OWNER ACK 11.10.2026): 21 колонка A:U, заголовок и заметка каждой — системные данные.
 * Утрата товара (FIRE/LOST INVENTORY, OWNER ACK 11.10): списание и компенсация — две отдельные строки, не неттуются.
 * Заметки пишутся в ячейки заголовка (Sheets NOTE, не комментарии) и входят в отпечаток плана: изменение текста
 * заметки = изменение контракта. «Налог 2 %» — РАСШИФРОВКА: он уже внутри «Вклада SKU», в прибыль повторно не входит.
 */
export const TAB_COLUMNS = [
  { header: 'Месяц', note: 'Месяц услуги (P&L по месяцу услуги WB). Источник: wb_mart.V_WB_STORE_PNL_PRESENTATION.' },
  { header: 'Выручка', note: 'Продажи по цене продавца (до СПП), за вычетом возвратов. История — база финотчёта WB по проданным единицам; текущие месяцы до созревания — выкупы Юнитки по цене продавца с поправкой на факт. Справочно: в прибыль не складывается отдельно.' },
  { header: 'Вклад SKU', note: 'Сумма вклада товаров: выручка − удержание WB (комиссия, эквайринг) − логистика − налог 2 % − себестоимость − хранение по SKU (с 01.09.2026; раньше хранение целиком в «Поправке хранения») − реклама по атрибуции + внешняя реклама. Уже включает налог 2 % и себестоимость. Входит в прибыль.' },
  { header: 'Налог 2% (в составе вклада SKU)', note: 'Расшифровка: управленческий налог 2 % от цены продавца, УЖЕ вычтенный внутри «Вклада SKU». В чистую прибыль повторно НЕ вычитается. Реальный налог УСН 15 % не моделируется.' },
  { header: 'Минимальный платёж WB', note: 'Счёт WB «Остаток по минимальному платежу», отнесён к месяцу услуги. Расход: уменьшает прибыль. В «Вкладе SKU» не учтён.' },
  { header: 'Утилизация', note: 'Счёт WB за утилизацию «за <месяц> <год>», по месяцу услуги из подписи. Расход: уменьшает прибыль.' },
  { header: 'Штрафы', note: 'Штрафы WB без привязки к заказу, по месяцу проводки. Расход: уменьшает прибыль.' },
  { header: 'Транзит', note: 'Удержания WB за транзит поставок. Расход: уменьшает прибыль.' },
  { header: 'Поправка рекламы', note: 'Атрибуция рекламы в SKU минус фактический биллинг WB по дате услуги. «+» — биллинг меньше атрибуции, прибыль выше. Входит в прибыль; документы «WB Продвижение» не используются.' },
  { header: 'Поправка хранения', note: 'Хранение, учтённое в SKU, минус факт платного хранения WB. «−» — факт больше. Входит в прибыль. В истории хранения по SKU нет — вся сумма факта здесь.' },
  { header: 'Поправка комиссии и цены', note: 'Модельная комиссия и цена листа против фактического удержания WB и базы финотчёта на тех же проданных единицах. В истории 0: вклад уже на факте. Входит в прибыль.' },
  { header: 'Поправка зрелой когорты', note: 'После созревания когорты (финотчёты покрыли месяц + 46 суток): фактическая логистика против модели минус ожидаемый вклад так и не проданных единиц. До созревания 0, сумма — в «Мосте сроков». Входит в прибыль.' },
  { header: 'Прочие расходы WB', note: 'Прочие счета кабинета WB (платная приёмка, «Баллы за отзывы» — аванс минус возврат остатка и т. п.). Расход: уменьшает прибыль.' },
  { header: 'Доходы / компенсации от WB', note: 'Компенсации и возвраты WB уровня кабинета (добровольная компенсация при возврате, компенсация ущерба по заказу). Доход: увеличивает прибыль. Компенсация за утраченный товар — в отдельной колонке; возмещения издержек WB — MEMO, сюда не входят.' },
  { header: 'Списание утраченного товара', note: 'Себестоимость товара, утраченного на складах WB (пожары после атак БПЛА 18.07–16.08.2026): количество — поштучно по отчётам WB о пострадавших товарах, себестоимость — каноническая на дату события. Месяц — месяц события. Только непроданный товар: проданные единицы уже в себестоимости «Вклада SKU». Расход: уменьшает прибыль.' },
  { header: 'Компенсация WB за утрату товара', note: 'Выплаты WB за товар, пострадавший в результате обстоятельств непреодолимой силы (финотчёт WB, «Добровольная выплата…»). Месяц — месяц проводки выплаты. Доход: увеличивает прибыль. Со списанием не неттуется.' },
  { header: 'Чистый эффект форс-мажора (справочно)', note: 'Компенсация WB за утрату товара − списание утраченного товара. Справочно: в прибыль уже вошло через две колонки слева, повторно не складывается.' },
  { header: 'Управленческая чистая прибыль магазина', note: 'Вклад SKU + поправки (реклама, хранение, комиссия и цена, зрелая когорта) − расходы WB + доходы WB − списание утраченного товара + компенсация WB за утрату товара. Налог 2 % уже внутри вклада. Не бухгалтерская: УСН, OPEX и фулфилмент не входят.' },
  { header: 'Управленческая чистая маржа', note: 'Управленческая чистая прибыль ÷ выручка.' },
  { header: 'Финансовое состояние', note: 'Закрыт; Закрыт, когорта дозревает; Ждём счета WB; Есть неразобранные операции; Есть неизвестные расходы; Справочно: наследие (август 2026 — сравнение). Не влияет на сумму.' },
  { header: 'Мост сроков (справочно, вне прибыли)', note: 'Пока когорта не созрела: логистика модели минус факт и ожидаемый вклад ещё не проданных единиц. В прибыль НЕ входит — это сроки WB, а не экономия.' },
] as const;

export const TAB_HEADER = TAB_COLUMNS.map((c) => c.header);
export const TAB_NOTES = TAB_COLUMNS.map((c) => c.note);
export const TAB_COLS = TAB_COLUMNS.length;
const colOf = (h: (typeof TAB_COLUMNS)[number]['header']): number => TAB_COLUMNS.findIndex((c) => c.header === h);
/** Индексы колонок (0-based) по заголовку — форматы и состояние не зависят от позиций, зашитых числом. */
export const COL = {
  FIRST_MONEY: colOf('Выручка'),
  NET: colOf('Управленческая чистая прибыль магазина'),
  MARGIN: colOf('Управленческая чистая маржа'),
  STATE: colOf('Финансовое состояние'),
  BRIDGE: colOf('Мост сроков (справочно, вне прибыли)'),
  FM_NET: colOf('Чистый эффект форс-мажора (справочно)'),
} as const;

export const STATE_LABEL: Readonly<Record<string, string>> = {
  LEGACY_PARTIAL_KNOWN_DEFECTS: 'Справочно: наследие, известные дефекты',
  FINANCIAL_COMPLETE: 'Закрыт',
  FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE: 'Закрыт, когорта дозревает',
  PARTIAL_AWAITING_ACCOUNT_INVOICE: 'Ждём счета WB',
  PENDING_CLASSIFICATION: 'Есть неразобранные операции',
  UNKNOWN_COST_PRESENT: 'Есть неизвестные расходы',
};

export type TabCell = string | number;
export interface TabPlan {
  tab: string;
  range: string;
  header: readonly string[];
  notes: readonly string[];
  rows: TabCell[][];
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

/** Колонки вью, без которых строка вкладки была бы молча нулевой (toNum(undefined) = 0): их отсутствие — отказ. */
export const REQUIRED_PNL_COLUMNS = ['management_tax_rub', 'inventory_loss_writeoff_rub', 'wb_loss_compensation_rub',
  'force_majeure_net_effect_rub', 'management_net_store_profit_rub', 'authority', 'inventory_loss_evidence_rows'] as const;

export function buildTabPlan(pnl: readonly PnlRow[]): TabPlan {
  const rows: TabCell[][] = [];
  let maxAbs = 0;
  for (const p of pnl) {
    const missing = REQUIRED_PNL_COLUMNS.filter((k) => !(k in p));
    if (missing.length) throw new Error(`${String(p.month)}: во вью нет колонок ${missing.join(', ')} — схема вью старее кода`);
    const n = (k: string): number => toNum(p[k]);
    const state = String(p.financial_state ?? '');
    const label = STATE_LABEL[state];
    if (!label) throw new Error(`${String(p.month)}: неизвестное состояние «${state}»`);
    const sku = r2(n('sku_contribution_rub'));
    const tax = r2(n('management_tax_rub'));
    const minPay = r2(n('minimum_payment_rub')), util = r2(n('utilization_rub')), pen = r2(n('penalty_rub')), tr = r2(n('transit_rub'));
    const other = r2(n('acceptance_rub') + n('other_marketplace_cost_rub'));
    const ads = r2(n('ads_adjustment_rub')), stor = r2(n('storage_adjustment_rub'));
    const comm = r2(n('commission_adjustment_rub') + n('price_adjustment_rub'));
    const logi = r2(n('mature_cohort_adjustment_rub'));
    const inc = r2(n('marketplace_income_rub'));
    const loss = r2(n('inventory_loss_writeoff_rub')), lossComp = r2(n('wb_loss_compensation_rub'));
    const fmNet = r2(n('force_majeure_net_effect_rub'));
    const net = r2(n('management_net_store_profit_rub'));
    const margin = Math.round(n('management_net_store_margin') * 1e6) / 1e6;
    const bridge = r2(n('timing_bridge_logistics_rub') - n('timing_bridge_unsettled_margin_rub'));
    // Тождество — на НЕокруглённых значениях вью (14 слагаемых по ±0,005 съели бы допуск 0,02 ₽).
    maxAbs = Math.max(maxAbs,
      Math.abs(n('sku_contribution_rub') + n('ads_adjustment_rub') + n('storage_adjustment_rub') + n('commission_adjustment_rub')
        + n('price_adjustment_rub') + n('mature_cohort_adjustment_rub') - n('minimum_payment_rub') - n('utilization_rub') - n('penalty_rub')
        - n('transit_rub') - n('acceptance_rub') - n('other_marketplace_cost_rub') + n('marketplace_income_rub')
        - n('inventory_loss_writeoff_rub') + n('wb_loss_compensation_rub') - n('management_net_store_profit_rub')),
      Math.abs(n('wb_loss_compensation_rub') - n('inventory_loss_writeoff_rub') - n('force_majeure_net_effect_rub')));
    // Налог и чистый эффект форс-мажора — расшифровки: в сумму строки НЕ входят (инварианты, OWNER ACK 11.10).
    rows.push([String(p.month), r2(n('revenue_rub')), sku, tax, minPay, util, pen, tr, ads, stor, comm, logi, other, inc,
      loss, lossComp, fmNet, net, margin, label, bridge]);
  }
  const header = [...TAB_HEADER];
  const notes = [...TAB_NOTES];
  const range = `'${TAB_NAME}'!A1:${String.fromCharCode(64 + header.length)}${rows.length + 1}`;
  const fingerprint = createHash('sha256').update(JSON.stringify({ tab: TAB_NAME, range, header, notes, rows })).digest('hex');
  return { tab: TAB_NAME, range, header, notes, rows, rowIdentityMaxAbs: Math.round(maxAbs * 100) / 100, fingerprint };
}
