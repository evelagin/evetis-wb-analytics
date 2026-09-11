/**
 * Нормализация строк воронки. RAW хранит ОРИГИНАЛЬНЫЕ значения API —
 * никаких пересчётов, округлений и «улучшений» здесь нет: любой пересчёт
 * на этом слое делает невозможным разбор расхождения с кабинетом.
 *
 * Единственное, что делаем, — раскладываем поля дневной записи по колонкам и
 * фиксируем неизвестные поля в schema_unknown_fields, чтобы дрейф схемы WB
 * был виден в манифесте, а не обнаруживался через полгода по кривым цифрам.
 *
 * UNITKA 2.0 R7: источник — POST /api/analytics/v3/sales-funnel/products/history.
 * Ответ: [{ product: { nmId, … }, history: [{ date, openCount, cartCount, … }], currency }].
 * openCount — переходы в карточку (вся воронка, включая органику), cartCount —
 * положили в корзину. Колонки RAW прежние; отмен в этом методе нет — NULL.
 */

/** Поля дневной записи history[], которые раскладываем по колонкам. */
export const KNOWN_DAY_FIELDS = [
  'date', 'openCount', 'cartCount', 'orderCount', 'orderSum', 'buyoutCount', 'buyoutSum',
  'buyoutPercent', 'addToCartConversion', 'cartToOrderConversion', 'addToWishlistCount',
] as const;
/** Поля карточки. В колонки идёт только nmId: название и бренд есть в справочнике SKU. */
export const KNOWN_PRODUCT_FIELDS = ['nmId', 'title', 'vendorCode', 'brandName', 'subjectId', 'subjectName'] as const;
export const KNOWN_ITEM_FIELDS = ['product', 'history', 'currency'] as const;

export interface HistoryItem {
  product?: Record<string, unknown>;
  history?: Array<Record<string, unknown>>;
  currency?: unknown;
  [k: string]: unknown;
}

export interface FunnelRow {
  observation_id: string;
  environment: string;
  run_id: string;
  observed_at: string;
  date_msk: string;
  nm_id: number | null;
  open_card_count: number | null;
  add_to_cart_count: number | null;
  orders_count: number | null;
  orders_sum_rub: number | null;
  buyouts_count: number | null;
  buyouts_sum_rub: number | null;
  cancel_count: number | null;
  cancel_sum_rub: number | null;
  add_to_cart_conversion: number | null;
  cart_to_order_conversion: number | null;
  buyout_percent: number | null;
  add_to_wishlist: number | null;
  source_endpoint: string;
  raw_row_json: string;
  ingested_at: string;
}

const num = (v: unknown): number | null => {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

/** `date` приходит как YYYY-MM-DD — сутки по Москве. */
const day = (v: unknown): string | null => {
  const s = String(v ?? '').slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : null;
};

export interface NormalizeMeta {
  observationId: string;
  environment: string;
  runId: string;
  observedAtIso: string;
  sourceEndpoint: string;
}

export interface NormalizeResult {
  rows: FunnelRow[];
  /** Поля ответа, которых нет в KNOWN_* — дрейф схемы WB (с префиксом уровня). */
  unknownFields: string[];
  /** Дневные записи без nmId или даты — в RAW не пишем, но считаем. */
  rejected: number;
}

export function normalizeFunnelRows(items: HistoryItem[], meta: NormalizeMeta): NormalizeResult {
  const knownItem = new Set<string>(KNOWN_ITEM_FIELDS as readonly string[]);
  const knownProduct = new Set<string>(KNOWN_PRODUCT_FIELDS as readonly string[]);
  const knownDay = new Set<string>(KNOWN_DAY_FIELDS as readonly string[]);
  const unknown = new Set<string>();
  const rows: FunnelRow[] = [];
  let rejected = 0;
  const nowIso = new Date().toISOString();

  for (const it of items) {
    for (const k of Object.keys(it)) if (!knownItem.has(k)) unknown.add(`item.${k}`);
    const product = it.product ?? {};
    for (const k of Object.keys(product)) if (!knownProduct.has(k)) unknown.add(`product.${k}`);
    const nm = num(product.nmId);
    for (const h of Array.isArray(it.history) ? it.history : []) {
      for (const k of Object.keys(h)) if (!knownDay.has(k)) unknown.add(`history.${k}`);
      const d = day(h.date);
      if (d === null || nm === null) { rejected++; continue; }
      rows.push({
        observation_id: meta.observationId,
        environment: meta.environment,
        run_id: meta.runId,
        observed_at: meta.observedAtIso,
        date_msk: d,
        nm_id: nm,
        open_card_count: num(h.openCount),
        add_to_cart_count: num(h.cartCount),
        orders_count: num(h.orderCount),
        orders_sum_rub: num(h.orderSum),
        buyouts_count: num(h.buyoutCount),
        buyouts_sum_rub: num(h.buyoutSum),
        cancel_count: null,
        cancel_sum_rub: null,
        add_to_cart_conversion: num(h.addToCartConversion),
        cart_to_order_conversion: num(h.cartToOrderConversion),
        buyout_percent: num(h.buyoutPercent),
        add_to_wishlist: num(h.addToWishlistCount),
        source_endpoint: meta.sourceEndpoint,
        raw_row_json: JSON.stringify({ nmId: product.nmId, currency: it.currency, ...h }),
        ingested_at: nowIso,
      });
    }
  }
  return { rows, unknownFields: [...unknown].sort(), rejected };
}

/**
 * Документация показывает массив на верхнем уровне; обёртку `{data:[]}` WB
 * использует в соседних методах, поэтому разбираем обе, а всё прочее — отказ.
 */
export function parseHistoryBody(body: string): HistoryItem[] {
  const parsed: unknown = JSON.parse(body);
  if (Array.isArray(parsed)) return parsed as HistoryItem[];
  const data = (parsed as { data?: unknown }).data;
  if (Array.isArray(data)) return data as HistoryItem[];
  throw new Error('Неизвестная форма ответа воронки: ожидался массив или {data:[]}');
}
