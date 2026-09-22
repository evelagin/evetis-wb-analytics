/**
 * Нормализация снимка акций WB (PR-PROMO-1). Чистые функции — тестируются
 * без сети и без BigQuery.
 *
 * Три правила, ради которых этот файл существует отдельно:
 *  1. Агрегат остаётся агрегатом. inPromoActionTotal — факт УРОВНЯ АКЦИИ.
 *     Ни одна функция здесь не имеет права породить строку уровня SKU из
 *     агрегатного счётчика: для автоакций WB состав не отдаёт вовсе.
 *  2. Отсутствующее значение НИКОГДА не превращается в 0. 0 — валидное
 *     значение счётчика и валидная цена; «нет данных» — это NULL.
 *  3. Незнакомое поле в ответе WB не отбрасывается молча — оно попадает в
 *     schema_status и в raw_*_json.
 */
import { createHash } from 'node:crypto';
import {
  KNOWN_DETAILS_FIELDS,
  KNOWN_LIST_FIELDS,
  KNOWN_NOMENCLATURE_FIELDS,
  KNOWN_RANGING_FIELDS,
  REQUIRED_LIST_FIELDS,
  WB_AUTO_PROMOTION_TYPE,
  type NomenclatureStatus,
  type SchemaStatus,
} from './constants.js';
import type { RawNomenclature, RawPromotion } from './wbApi.js';

export interface PromoRowContext {
  observedAtIso: string;
  observationBucket: string;
  observationId: string;
  environment: string;
  runId: string;
  sourceEndpoint: string;
}

export interface CalendarRow {
  observed_at: string;
  observation_bucket: string;
  observation_id: string;
  environment: string;
  run_id: string;
  promotion_id: number;
  promotion_name: string | null;
  promotion_type: string | null;
  is_auto_promotion: boolean | null;
  starts_at: string | null;
  ends_at: string | null;
  details_available: boolean;
  description: string | null;
  advantages_csv: string | null;
  in_promo_total: number | null;
  in_promo_leftovers: number | null;
  not_in_promo_total: number | null;
  not_in_promo_leftovers: number | null;
  participation_pct: number | null;
  exception_products_count: number | null;
  ranging_tiers: number | null;
  ranging_condition: string | null;
  sku_level_data_available: boolean;
  nomenclature_status: NomenclatureStatus;
  raw_promotion_json: string;
  source_endpoint: string;
  source_payload_hash: string;
  ingested_at: string;
}

export interface RangingRow {
  observed_at: string;
  observation_bucket: string;
  observation_id: string;
  environment: string;
  run_id: string;
  promotion_id: number;
  tier_ordinal: number;
  condition: string | null;
  participation_rate: number | null;
  boost_pct: number | null;
  raw_tier_json: string;
  source_endpoint: string;
  source_payload_hash: string;
  ingested_at: string;
}

export interface NomenclatureRow {
  observed_at: string;
  observation_bucket: string;
  observation_id: string;
  environment: string;
  run_id: string;
  promotion_id: number;
  in_action_requested: boolean;
  nm_id: number;
  internal_sku: string | null;
  in_action: boolean | null;
  price: number | null;
  plan_price: number | null;
  discount_pct: number | null;
  plan_discount_pct: number | null;
  currency_code: string | null;
  raw_item_json: string;
  source_endpoint: string;
  source_payload_hash: string;
  ingested_at: string;
}

export interface SchemaAudit {
  status: SchemaStatus;
  unknownFields: string[];
  missingRequired: string[];
}

/** Число или null. Пустая строка, null, undefined, NaN → null. НИКОГДА не 0-подстановка. */
export function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Целое или null. */
export function int(v: unknown): number | null {
  const n = num(v);
  return n === null ? null : Math.trunc(n);
}

function str(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  const s = String(v);
  return s === '' ? null : s;
}

function bool(v: unknown): boolean | null {
  return typeof v === 'boolean' ? v : null;
}

/** Метка времени источника → ISO. Пустая строка источника → null, а не эпоха. */
export function ts(v: unknown): string | null {
  const s = str(v);
  if (s === null) return null;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function sha(...parts: Array<string | number | boolean>): string {
  return createHash('sha256').update(parts.join('|')).digest('hex');
}

/**
 * Аудит контракта. Незнакомые поля — предупреждение (DRIFT_NEW_FIELDS): WB
 * регулярно добавляет поля, и ронять наблюдение из-за этого нельзя. Отсутствие
 * ОБЯЗАТЕЛЬНЫХ полей — DRIFT_MISSING_FIELDS, и вызывающий обязан считать прогон
 * неуспешным.
 */
export function auditSchema(list: RawPromotion[], details: RawPromotion[]): SchemaAudit {
  const unknown = new Set<string>();
  const missing = new Set<string>();

  for (const p of list) {
    for (const k of Object.keys(p)) if (!KNOWN_LIST_FIELDS.includes(k)) unknown.add(`list.${k}`);
    for (const k of REQUIRED_LIST_FIELDS) if (!(k in p)) missing.add(`list.${k}`);
  }
  for (const d of details) {
    for (const k of Object.keys(d)) if (!KNOWN_DETAILS_FIELDS.includes(k)) unknown.add(`details.${k}`);
    const tiers = Array.isArray(d.ranging) ? (d.ranging as Record<string, unknown>[]) : [];
    for (const t of tiers) {
      for (const k of Object.keys(t)) if (!KNOWN_RANGING_FIELDS.includes(k)) unknown.add(`ranging.${k}`);
    }
  }

  const status: SchemaStatus =
    missing.size > 0 ? 'DRIFT_MISSING_FIELDS' : unknown.size > 0 ? 'DRIFT_NEW_FIELDS' : 'OK';
  return { status, unknownFields: [...unknown].sort(), missingRequired: [...missing].sort() };
}

/** Аудит контракта состава акции — отдельно: он приходит другим методом. */
export function auditNomenclatureSchema(items: RawNomenclature[]): string[] {
  const unknown = new Set<string>();
  for (const it of items) {
    for (const k of Object.keys(it)) if (!KNOWN_NOMENCLATURE_FIELDS.includes(k)) unknown.add(`nomenclature.${k}`);
  }
  return [...unknown].sort();
}

export function isAutoPromotion(p: RawPromotion): boolean {
  return String(p.type ?? '') === WB_AUTO_PROMOTION_TYPE;
}

export interface BuildCalendarInput {
  list: RawPromotion[];
  detailsById: Map<number, RawPromotion>;
  /** Исход запроса состава по акции; отсутствие ключа = SKIPPED_AUTO_PROMOTION. */
  nomenclatureStatusById: Map<number, NomenclatureStatus>;
}

/**
 * Строки календаря. Одна строка на акцию снимка.
 * Отсутствие деталей — штатное состояние (завершённые акции): details_available=false,
 * счётчики остаются NULL, а не нулями.
 */
export function buildCalendarRows(input: BuildCalendarInput, ctx: PromoRowContext): CalendarRow[] {
  const rows: CalendarRow[] = [];
  for (const p of input.list) {
    const id = int(p.id);
    if (id === null) continue; // без идентификатора строка неадресуема; учитывается аудитом схемы
    const d = input.detailsById.get(id);
    const tiers = d && Array.isArray(d.ranging) ? (d.ranging as Record<string, unknown>[]) : [];
    const auto = isAutoPromotion(p);
    const status = input.nomenclatureStatusById.get(id) ?? 'SKIPPED_AUTO_PROMOTION';
    rows.push({
      observed_at: ctx.observedAtIso,
      observation_bucket: ctx.observationBucket,
      observation_id: ctx.observationId,
      environment: ctx.environment,
      run_id: ctx.runId,
      promotion_id: id,
      promotion_name: str(p.name) ?? (d ? str(d.name) : null),
      promotion_type: str(p.type),
      is_auto_promotion: auto,
      starts_at: ts(p.startDateTime),
      ends_at: ts(p.endDateTime),
      details_available: d !== undefined,
      description: d ? str(d.description) : null,
      advantages_csv:
        d && Array.isArray(d.advantages) ? (d.advantages as unknown[]).map((a) => String(a)).join(', ') || null : null,
      in_promo_total: d ? int(d.inPromoActionTotal) : null,
      in_promo_leftovers: d ? int(d.inPromoActionLeftovers) : null,
      not_in_promo_total: d ? int(d.notInPromoActionTotal) : null,
      not_in_promo_leftovers: d ? int(d.notInPromoActionLeftovers) : null,
      participation_pct: d ? num(d.participationPercentage) : null,
      exception_products_count: d ? int(d.exceptionProductsCount) : null,
      ranging_tiers: d ? tiers.length : null,
      ranging_condition: tiers.length > 0 ? str(tiers[0]?.condition) : null,
      // Состав по SKU принципиально недоступен для автоакций — это контракт WB,
      // а не наш дефект. Для прочих акций доступность подтверждается исходом запроса.
      sku_level_data_available: !auto && status === 'FETCHED',
      nomenclature_status: status,
      raw_promotion_json: JSON.stringify({ list: p, details: d ?? null }),
      source_endpoint: ctx.sourceEndpoint,
      source_payload_hash: sha(ctx.observationId, id),
      ingested_at: new Date().toISOString(),
    });
  }
  return rows;
}

/** Длинная проекция ranging[]. Порядок источника сохраняется в tier_ordinal. */
export function buildRangingRows(details: RawPromotion[], ctx: PromoRowContext): RangingRow[] {
  const rows: RangingRow[] = [];
  for (const d of details) {
    const id = int(d.id);
    if (id === null) continue;
    const tiers = Array.isArray(d.ranging) ? (d.ranging as Record<string, unknown>[]) : [];
    tiers.forEach((t, i) => {
      rows.push({
        observed_at: ctx.observedAtIso,
        observation_bucket: ctx.observationBucket,
        observation_id: ctx.observationId,
        environment: ctx.environment,
        run_id: ctx.runId,
        promotion_id: id,
        tier_ordinal: i,
        condition: str(t.condition),
        participation_rate: num(t.participationRate),
        boost_pct: num(t.boost),
        raw_tier_json: JSON.stringify(t),
        source_endpoint: ctx.sourceEndpoint,
        source_payload_hash: sha(ctx.observationId, id, i),
        ingested_at: new Date().toISOString(),
      });
    });
  }
  return rows;
}

/**
 * Строки состава акции. Вызывается только там, где состав реально получен.
 * Нерезолвленный nm_id НЕ отбрасывается: internal_sku остаётся NULL, а факт
 * попадает в манифест.
 */
export function buildNomenclatureRows(
  promotionId: number,
  inActionRequested: boolean,
  items: RawNomenclature[],
  skuByNm: Map<number, string>,
  ctx: PromoRowContext,
): NomenclatureRow[] {
  const rows: NomenclatureRow[] = [];
  for (const it of items) {
    const nm = int(it.id);
    if (nm === null) continue;
    rows.push({
      observed_at: ctx.observedAtIso,
      observation_bucket: ctx.observationBucket,
      observation_id: ctx.observationId,
      environment: ctx.environment,
      run_id: ctx.runId,
      promotion_id: promotionId,
      in_action_requested: inActionRequested,
      nm_id: nm,
      internal_sku: skuByNm.get(nm) ?? null,
      in_action: bool(it.inAction),
      price: num(it.price),
      plan_price: num(it.planPrice),
      discount_pct: num(it.discount),
      plan_discount_pct: num(it.planDiscount),
      currency_code: str(it.currencyCode),
      raw_item_json: JSON.stringify(it),
      source_endpoint: ctx.sourceEndpoint,
      source_payload_hash: sha(ctx.observationId, promotionId, inActionRequested, nm),
      ingested_at: new Date().toISOString(),
    });
  }
  return rows;
}
