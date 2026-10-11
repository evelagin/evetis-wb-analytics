/**
 * PHASE C (C3) — ПЛАН вкладки «WB Магазин P&L»: одна строка на месяц услуги.
 *
 * Только план. Этот модуль не пишет лист и не создаёт вкладку: создание и запись — отдельным
 * решением владельца («OWNER ACK — CREATE/WRITE WB МАГАЗИН P&L»). Отпечаток плана (sha256) —
 * то, что владелец подтверждает: писатель обязан записать ровно этот план.
 *
 * Строка складывается сама: Чистая прибыль = Вклад SKU + поправки (реклама, хранение, комиссия и цена,
 * логистика когорты) − расходы кабинета (мин. платёж, утилизация, штрафы, транзит, прочие) + доходы.
 * «Мост сроков» — справочно и в прибыль НЕ входит: пока когорта не созрела, разница логистики и
 * ожидаемый вклад ещё не проданных единиц — это сроки WB, а не экономия.
 */
import { createHash } from 'node:crypto';
import type { PnlRow } from './bq.js';

export const TAB_NAME = 'WB Магазин P&L';

/**
 * Контракт представления (OWNER ACK 11.10.2026): 18 колонок A:R, заголовок и заметка каждой — системные данные.
 * Заметки пишутся в ячейки заголовка (Sheets NOTE, не комментарии) и входят в отпечаток плана: изменение текста
 * заметки = изменение контракта. «Налог 2 %» — РАСШИФРОВКА: он уже внутри «Вклада SKU», в прибыль повторно не входит.
 */
export const TAB_COLUMNS = [
  { header: 'Месяц', note: 'Месяц услуги (P&L по месяцу услуги WB). Источник: wb_mart.V_WB_STORE_PNL_PRESENTATION.' },
  { header: 'Выручка', note: 'Продажи по цене продавца (до СПП), за вычетом возвратов. История — база финотчёта WB по проданным единицам; текущие месяцы до созревания — выкупы Юнитки по цене продавца с поправкой на факт. Справочно: в прибыль не складывается отдельно.' },
  { header: 'Вклад SKU', note: 'Сумма вклада товаров: выручка − удержание WB (комиссия, эквайринг) − логистика − налог 2 % − себестоимость − реклама по атрибуции + внешняя реклама. Уже включает налог 2 % и себестоимость. Входит в прибыль.' },
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
  { header: 'Доходы / компенсации от WB', note: 'Компенсации и возвраты WB уровня кабинета (например, форс-мажорная компенсация). Доход: увеличивает прибыль. Возмещения издержек WB — MEMO, сюда не входят.' },
  { header: 'Управленческая чистая прибыль магазина', note: 'Вклад SKU + поправки (реклама, хранение, комиссия и цена, зрелая когорта) − расходы WB + доходы WB. Налог 2 % уже внутри вклада. Не бухгалтерская: УСН, OPEX и фулфилмент не входят.' },
  { header: 'Управленческая чистая маржа', note: 'Управленческая чистая прибыль ÷ выручка.' },
  { header: 'Финансовое состояние', note: 'Закрыт; Закрыт, когорта дозревает; Ждём счета WB; Есть неразобранные операции; Есть неизвестные расходы; Справочно: наследие (август 2026 — сравнение). Не влияет на сумму.' },
  { header: 'Мост сроков (справочно, вне прибыли)', note: 'Пока когорта не созрела: логистика модели минус факт и ожидаемый вклад ещё не проданных единиц. В прибыль НЕ входит — это сроки WB, а не экономия.' },
] as const;

export const TAB_HEADER = TAB_COLUMNS.map((c) => c.header);
export const TAB_NOTES = TAB_COLUMNS.map((c) => c.note);
export const TAB_COLS = TAB_COLUMNS.length;

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

export function buildTabPlan(pnl: readonly PnlRow[]): TabPlan {
  const rows: TabCell[][] = [];
  let maxAbs = 0;
  for (const p of pnl) {
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
    const net = r2(n('management_net_store_profit_rub'));
    const margin = Math.round(n('management_net_store_margin') * 1e6) / 1e6;
    const bridge = r2(n('timing_bridge_logistics_rub') - n('timing_bridge_unsettled_margin_rub'));
    maxAbs = Math.max(maxAbs, Math.abs(sku + ads + stor + comm + logi - minPay - util - pen - tr - other + inc - net));
    // Налог — расшифровка внутри вклада: в сумму строки НЕ входит (инвариант, OWNER ACK 11.10).
    rows.push([String(p.month), r2(n('revenue_rub')), sku, tax, minPay, util, pen, tr, ads, stor, comm, logi, other, inc, net, margin, label, bridge]);
  }
  const header = [...TAB_HEADER];
  const notes = [...TAB_NOTES];
  const range = `'${TAB_NAME}'!A1:${String.fromCharCode(64 + header.length)}${rows.length + 1}`;
  const fingerprint = createHash('sha256').update(JSON.stringify({ tab: TAB_NAME, range, header, notes, rows })).digest('hex');
  return { tab: TAB_NAME, range, header, notes, rows, rowIdentityMaxAbs: Math.round(maxAbs * 100) / 100, fingerprint };
}
