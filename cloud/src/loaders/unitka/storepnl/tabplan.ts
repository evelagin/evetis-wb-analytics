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

export const TAB_HEADER = [
  'Месяц', 'Выручка', 'Вклад SKU', 'Минимальный платёж', 'Утилизация', 'Штрафы', 'Транзит',
  'Поправка рекламы', 'Поправка хранения', 'Поправка комиссии и цены', 'Созревшая когорта (логистика и непроданные)',
  'Прочие расходы площадки', 'Доходы площадки', 'Управленческая чистая прибыль магазина',
  'Управленческая чистая маржа', 'Финансовое состояние', 'Мост сроков (справочно, вне прибыли)',
] as const;

export const STATE_LABEL: Readonly<Record<string, string>> = {
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
    rows.push([String(p.month), r2(n('revenue_rub')), sku, minPay, util, pen, tr, ads, stor, comm, logi, other, inc, net, margin, label, bridge]);
  }
  const header = [...TAB_HEADER];
  const range = `'${TAB_NAME}'!A1:${String.fromCharCode(64 + header.length)}${rows.length + 1}`;
  const fingerprint = createHash('sha256').update(JSON.stringify({ tab: TAB_NAME, range, header, rows })).digest('hex');
  return { tab: TAB_NAME, range, header, rows, rowIdentityMaxAbs: Math.round(maxAbs * 100) / 100, fingerprint };
}
