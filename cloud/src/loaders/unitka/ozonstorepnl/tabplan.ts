/**
 * D4 (OWNER ACK 15.10.2026) — ПЛАН вкладки «Ozon Магазин P&L»: одна строка на месяц P&L.
 *
 * Вкладка — только ПРЕДСТАВЛЕНИЕ канонического ozon_mart.V_OZON_STORE_PNL_MONTHLY (D3). Ни одной собственной
 * финансовой формулы: каждая денежная колонка — колонка вью; две колонки-группировки (расходы кабинета без Premium,
 * прибыль без оценки X) — суммы колонок той же вью, а не новые определения. Строка складывается сама:
 *   Y = B − C − D − E − F − G − H − I − J − K − L − M − N − O − P + Q − R + S   (R — налог 2 %, S — оценка X)
 * Это то же тождество D3 (V = B − … − T + U + X; Y = V − W); проверяется на НЕокруглённых значениях вью.
 *
 * Модуль чистый: план и отпечаток. Запись — publish.ts, I/O — index.ts.
 */
import { createHash } from 'node:crypto';

export const TAB_NAME = 'Ozon Магазин P&L';
export const SOURCE_VIEW = 'ozon_mart.V_OZON_STORE_PNL_MONTHLY';

/**
 * Контракт представления: 26 колонок A:Z, заголовок и заметка каждой — системные данные (входят в отпечаток
 * контракта). Изменение заголовка, заметки или ширины — только управляемой миграцией (OZON_STORE_PNL_MIGRATE_CONTRACT).
 */
export const TAB_COLUMNS = [
  { header: 'Месяц', note: 'Месяц P&L магазина Ozon. Источник — ozon_mart.V_OZON_STORE_PNL_MONTHLY (D3): расходы по отправлению — месяц заказа, расходы кабинета — месяц начисления, Premium — месяц периода подписки.' },
  { header: 'Выручка продавца', note: 'Реализация по цене продавца × количество из начислений Ozon (за вычетом сторно возвратов) + выкуп СНГ по документу. Бонусы и соинвестирование Ozon не входят.' },
  { header: 'Себестоимость', note: 'Себестоимость проданных единиц на дату заказа (evetis_ref.V_PRODUCT_COGS_EFFECTIVE). Возвраты себестоимость не уменьшают (решение 12.10). В части месяцев — юридическая база импорта, без полной управленческой себестоимости (follow-up D3.1).' },
  { header: 'Комиссия Ozon', note: 'Вознаграждение Ozon из экономического блока начислений (сторно уменьшает). Расход.' },
  { header: 'Логистика', note: 'Тип начисления 32. Расход, месяц заказа.' },
  { header: 'Последняя миля', note: 'Типы 29, 28, 98, 30. Расход, месяц заказа.' },
  { header: 'Обратная логистика', note: 'Типы 59, 45, 9, 6 (месяц заказа) и 78 (месяц начисления). Расход.' },
  { header: 'Эквайринг', note: 'Тип 1, месяц заказа; положительные строки — возврат эквайринга. Расход.' },
  { header: 'Хранение', note: 'Тип 79 — временное размещение по SKU, месяц начисления. Расход.' },
  { header: 'Продвижение SKU', note: 'Типы 116, 74, 48 (сбор отзывов, звёздные товары, бонусы продавца), месяц заказа. Расход.' },
  { header: 'Прочие прямые расходы', note: 'Типы 39, 38, 10 (месяц заказа) и 15, 71 (месяц начисления): упаковка, материалы, утилизация, вывоз. Расход.' },
  { header: 'Реклама CPC (биллинг)', note: 'Оплата за клик и трафареты по финансовым начислениям Ozon: типы 41, 75, 33 и 54 по CPC-кампаниям, сутки услуги. Биллинг магазина, не атрибуция SKU. Расход.' },
  { header: 'Реклама CPO (биллинг)', note: 'Оплата за заказ: тип 54 по CPO-кампаниям, дата списания. Биллинг магазина. Расход.' },
  { header: 'Реклама магазина без SKU', note: 'Типы 47, 96 (баллы за отзывы, ускоренный сбор отзывов), месяц начисления. Расход.' },
  { header: 'Расходы кабинета Ozon (без Premium)', note: 'Кросс-докинг (12) + обработка товара (77) + размещение на складах (46) + страхование (76) + корректировки услуг (57), месяц начисления. По SKU не распределяются. Расход.' },
  { header: 'Premium', note: 'Подписка Premium (52), разложена по месяцам периода подписки. Расход.' },
  { header: 'Компенсации Ozon', note: 'Товарные компенсации (25), месяц начисления. Доход.' },
  { header: 'Управленческий налог 2%', note: '0,02 × выручка продавца, ровно один раз (провизорные единицы несут свой налог внутри оценки X). Управленческий резерв, не налог УСН.' },
  { header: 'Предварительная оценка X', note: 'PROVISIONAL: вклад доставленных, но ещё не начисленных Ozon отправлений (цена − комиссия из выплаты − средняя логистика SKU − себестоимость − налог 2 %). Входит в прибыль, но не является фактом начислений. Неизвестный вход — не ноль: месяц «не хватает денег источника».' },
  { header: 'Управленческая чистая прибыль магазина', note: 'Management Net Store Profit (D3): выручка − все расходы Ozon (включая кабинет, Premium и биллинг рекламы) + компенсации + оценка X − налог 2 %. Не бухгалтерская: УСН, OPEX и фулфилмент не входят. Окончательной прибыли во вкладке пока нет: себестоимость в части единиц — юридическая база импорта до D3.1; деньги Ozon окончательны только у «сверены» при закрытом месяце.' },
  { header: 'Управленческая чистая маржа', note: 'Управленческая чистая прибыль ÷ (выручка продавца + провизорная выручка X).' },
  { header: 'Прибыль без оценки X (справочно)', note: 'Управленческая чистая прибыль минус предварительная оценка X: только подтверждённые начисления. Справочно, в итог не складывается.' },
  { header: 'Финансовое состояние', note: 'Достоверность месяца (V_OZON_STORE_MONTH_STATE): «Деньги Ozon сверены, себестоимость предварительная»; «Сверены, ждём поздних начислений»; «Ждём начислений Ozon — предварительно»; «Не доказан — нужна сверка»; «Не хватает денег источника»; «Есть неразобранные операции» (публикация блокируется). Деньги Ozon окончательны только у первого при закрытом месяце; себестоимость в части единиц — юридическая база импорта до D3.1, поэтому окончательной прибыли во вкладке пока нет.' },
  { header: 'Закрытие месяца', note: 'По водяному знаку финансов Ozon: «Открыт»; «Ждём поздних начислений» (до конца месяца + 36 суток); «Закрыт по сроку».' },
  { header: 'Свежесть источников', note: 'Текущее состояние загрузок Ozon (V_OZON_MART_FRESHNESS) на момент публикации. Публикация выполняется только при «Свежие».' },
  { header: 'Причина неполноты', note: 'Раскрытие причин из ворот D3 (V_OZON_STORE_MONTH_STATE): известные дефекты источника (D1 — не загружены сторно реализации при возвратах; M2 — сумма реализации хранится не полностью), открытые сутки контроля начислений, деньги без колонки или отправления, SKU вне карты, себестоимость не найдена, признаки незагруженного сторно, ремонт D1 без сверки, оценка X и её входы, единицы в пути, предварительная себестоимость.' },
] as const;

export const TAB_HEADER = TAB_COLUMNS.map((c) => c.header);
export const TAB_NOTES = TAB_COLUMNS.map((c) => c.note);
export const TAB_COLS = TAB_COLUMNS.length;
const colOf = (h: (typeof TAB_COLUMNS)[number]['header']): number => TAB_COLUMNS.findIndex((c) => c.header === h);
/** Индексы колонок (0-based) по заголовку — форматы не зависят от позиций, зашитых числом. */
export const COL = {
  FIRST_MONEY: colOf('Выручка продавца'),
  X: colOf('Предварительная оценка X'),
  NET: colOf('Управленческая чистая прибыль магазина'),
  MARGIN: colOf('Управленческая чистая маржа'),
  NET_WITHOUT_X: colOf('Прибыль без оценки X (справочно)'),
  STATE: colOf('Финансовое состояние'),
  CLOSURE: colOf('Закрытие месяца'),
  FRESHNESS: colOf('Свежесть источников'),
  REASON: colOf('Причина неполноты'),
} as const;

export const STATE_LABEL: Readonly<Record<string, string>> = {
  FINANCIAL_COMPLETE: 'Деньги Ozon сверены, себестоимость предварительная',
  FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE: 'Сверены, ждём поздних начислений',
  PARTIAL_AWAITING_ACCRUALS: 'Ждём начислений Ozon — предварительно',
  UNPROVEN: 'Не доказан — нужна сверка',
  PARTIAL_SOURCE_MISSING: 'Не хватает денег источника',
  PENDING_CLASSIFICATION: 'Есть неразобранные операции',
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
  D1_REVERSAL_NOT_LOADED: 'D1: не загружены сторно реализации при возвратах',
  M2_SALE_AMOUNT_REPRESENTATION: 'M2: сумма реализации хранится не полностью',
};

export type PnlRow = Record<string, unknown>;
export type TabCell = string | number;
export interface TabPlan {
  tab: string;
  range: string;
  header: readonly string[];
  notes: readonly string[];
  rows: TabCell[][];
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

/** Колонки вью, без которых строка вкладки была бы молча нулевой (toNum(undefined) = 0): их отсутствие — отказ. */
export const REQUIRED_PNL_COLUMNS = [
  'pnl_month', 'seller_base_revenue_rub', 'product_cogs_rub', 'commission_rub', 'logistics_rub', 'last_mile_rub',
  'return_logistics_rub', 'acquiring_rub', 'sku_storage_rub', 'sku_promotion_rub', 'other_direct_rub', 'cpc_billing_rub',
  'cpo_billing_rub', 'store_ads_rub', 'crossdock_rub', 'processing_rub', 'fbo_placement_rub', 'insurance_rub',
  'other_store_costs_rub', 'premium_rub', 'marketplace_compensation_rub', 'provisional_open_rub',
  'store_contribution_pre_tax_rub', 'management_tax_rub', 'management_net_store_profit_rub', 'management_net_store_margin',
  'sku_contribution_rub', 'ads_adjustment_rub', 'unattributed_account_net_rub', 'financial_state', 'month_closure_status',
  'source_freshness_state', 'known_source_defects', 'provisional_units', 'in_transit_units',
] as const;

/**
 * Раскрытие причин — только поля D3 (P&L и ворота V_OZON_STORE_MONTH_STATE), без новых вычислений денег. Себестоимость
 * в части единиц стоит на юридической базе импорта (проверка 15.10, follow-up D3.1): до D3.1 она раскрывается у каждого
 * месяца «сверено», чтобы ни одна строка не выглядела окончательной прибылью.
 */
export function reasonText(p: PnlRow, s: Record<string, unknown> = {}): string {
  const out: string[] = [];
  const defects = toStr(p.known_source_defects).split(',').map((x) => x.trim()).filter(Boolean);
  for (const d of defects) out.push(DEFECT_LABEL[d] ?? `известный дефект ${d}`);
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
  'other_store_costs_rub', 'premium_rub', 'marketplace_compensation_rub', 'provisional_open_rub', 'store_contribution_pre_tax_rub',
  'management_tax_rub', 'management_net_store_profit_rub', 'sku_contribution_rub', 'ads_adjustment_rub',
  'unattributed_account_net_rub'] as const;

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

export function buildTabPlan(pnl: readonly PnlRow[], states: readonly Record<string, unknown>[] = [], requireStates = false): TabPlan {
  const rows: TabCell[][] = [];
  let maxAbs = 0;
  const byMonth = new Map(states.map((s) => [toStr(s.pnl_month).slice(0, 7), s]));
  if (requireStates && pnl.length && !states.length) throw new Error('нет строк V_OZON_STORE_MONTH_STATE — причины неполноты не раскрыть');
  for (const p of pnl) {
    const missing = REQUIRED_PNL_COLUMNS.filter((k) => !(k in p));
    if (missing.length) throw new Error(`${toStr(p.pnl_month)}: во вью нет колонок ${missing.join(', ')} — схема вью старее кода`);
    const nulls = MONEY_COLUMNS.filter((k) => p[k] === null || p[k] === undefined);
    if (nulls.length) throw new Error(`${toStr(p.pnl_month)}: NULL в денежных колонках ${nulls.join(', ')} — неизвестное не равно нулю`);
    const n = (k: string): number => toNum(p[k]);
    const month = toStr(p.pnl_month).slice(0, 7);
    const st = byMonth.get(month);
    if ((requireStates || states.length) && !st) throw new Error(`${month}: нет строки состояния V_OZON_STORE_MONTH_STATE`);
    const state = STATE_LABEL[toStr(p.financial_state)];
    if (!state) throw new Error(`${month}: неизвестное состояние «${toStr(p.financial_state)}»`);
    const closure = CLOSURE_LABEL[toStr(p.month_closure_status)];
    if (!closure) throw new Error(`${month}: неизвестное закрытие «${toStr(p.month_closure_status)}»`);
    const fresh = toStr(p.source_freshness_state);
    maxAbs = Math.max(maxAbs, identityResidual(p));
    const account = n('crossdock_rub') + n('processing_rub') + n('fbo_placement_rub') + n('insurance_rub') + n('other_store_costs_rub');
    rows.push([
      month,
      r2(n('seller_base_revenue_rub')), r2(n('product_cogs_rub')), r2(n('commission_rub')), r2(n('logistics_rub')),
      r2(n('last_mile_rub')), r2(n('return_logistics_rub')), r2(n('acquiring_rub')), r2(n('sku_storage_rub')),
      r2(n('sku_promotion_rub')), r2(n('other_direct_rub')), r2(n('cpc_billing_rub')), r2(n('cpo_billing_rub')),
      r2(n('store_ads_rub')), r2(account), r2(n('premium_rub')), r2(n('marketplace_compensation_rub')),
      r2(n('management_tax_rub')), r2(n('provisional_open_rub')), r2(n('management_net_store_profit_rub')),
      p.management_net_store_margin === null || p.management_net_store_margin === undefined ? '' : Math.round(n('management_net_store_margin') * 1e6) / 1e6,
      r2(n('management_net_store_profit_rub') - n('provisional_open_rub')),
      state, closure, FRESHNESS_LABEL[fresh] ?? fresh, reasonText(p, st ?? {}),
    ]);
  }
  const header = [...TAB_HEADER];
  const notes = [...TAB_NOTES];
  const range = `'${TAB_NAME}'!A1:${String.fromCharCode(64 + header.length)}${rows.length + 1}`;
  const fingerprint = createHash('sha256').update(JSON.stringify({ tab: TAB_NAME, range, header, notes, rows })).digest('hex');
  return { tab: TAB_NAME, range, header, notes, rows, rowIdentityMaxAbs: Math.round(maxAbs * 100) / 100, fingerprint };
}
