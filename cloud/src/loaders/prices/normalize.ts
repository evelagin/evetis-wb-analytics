/**
 * Нормализация и контроль качества снимка цен WB (PR-1). Чистые функции — тестируются
 * без сети и без BigQuery.
 *
 * Три правила, ради которых этот файл существует отдельно:
 *  1. Отсутствующая цена НИКОГДА не превращается в 0. 0 — валидная цена; «нет данных» —
 *     это отказ прогона. Смешение этих состояний исказило бы будущий ценовой пол.
 *  2. Незнакомое поле в ответе WB не отбрасывается молча — оно попадает в schema_status.
 *  3. Терминология не схлопывается: seller_effective_price — цена ПРОДАВЦА после его
 *     скидки, а не цена покупателя. СПП и акции WB этот endpoint не отдаёт вовсе.
 */
import { createHash } from 'node:crypto';
import { LoaderError } from '../../errors.js';
import type { RawGoodsItem } from './wbApi.js';
import {
  KNOWN_ITEM_FIELDS, KNOWN_SIZE_FIELDS,
  REQUIRED_ITEM_FIELDS, REQUIRED_SIZE_FIELDS,
  WB_PRICES_SOURCE_ENDPOINT, type SchemaStatus,
} from './constants.js';

export interface RawPriceRow {
  observed_at: string;
  observation_bucket: string;
  observation_id: string;
  environment: string;
  run_id: string;
  nm_id: number;
  internal_sku: string | null;
  vendor_code: string | null;
  size_id: number | null;
  tech_size_name: string | null;
  seller_list_price: number | null;
  seller_discount_pct: number | null;
  seller_effective_price: number | null;
  wb_club_discount_pct: number | null;
  wb_club_price: number | null;
  currency_code: string | null;
  editable_size_price: boolean | null;
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

export interface Coverage {
  expected: number;
  observed: number;
  missing: number;
  unexpected: number;
  coveragePct: number;
  missingNmIds: number[];
  unexpectedNmIds: number[];
}

/** Число или null. Пустая строка, null, undefined, NaN → null. НИКОГДА не 0-подстановка. */
export function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function str(v: unknown): string | null {
  return v === null || v === undefined ? null : String(v);
}

/**
 * Аудит контракта. Незнакомые поля — предупреждение (DRIFT_NEW_FIELDS): WB регулярно
 * добавляет поля, и ронять наблюдение из-за этого нельзя. Отсутствие ОБЯЗАТЕЛЬНЫХ полей —
 * DRIFT_MISSING_FIELDS, и вызывающий обязан считать прогон неуспешным.
 */
export function auditSchema(items: RawGoodsItem[]): SchemaAudit {
  const unknown = new Set<string>();
  const missing = new Set<string>();

  for (const it of items) {
    for (const k of Object.keys(it)) {
      if (!KNOWN_ITEM_FIELDS.includes(k)) unknown.add(`item.${k}`);
    }
    for (const k of REQUIRED_ITEM_FIELDS) {
      if (!(k in it)) missing.add(`item.${k}`);
    }
    const sizes = Array.isArray(it.sizes) ? (it.sizes as Record<string, unknown>[]) : [];
    for (const s of sizes) {
      for (const k of Object.keys(s)) {
        if (!KNOWN_SIZE_FIELDS.includes(k)) unknown.add(`sizes.${k}`);
      }
      for (const k of REQUIRED_SIZE_FIELDS) {
        if (!(k in s)) missing.add(`sizes.${k}`);
      }
    }
  }

  const status: SchemaStatus =
    missing.size > 0 ? 'DRIFT_MISSING_FIELDS' : unknown.size > 0 ? 'DRIFT_NEW_FIELDS' : 'OK';
  return { status, unknownFields: [...unknown].sort(), missingRequired: [...missing].sort() };
}

/** Покрытие: что ожидали по справочнику против того, что вернул WB. */
export function computeCoverage(expectedNmIds: Set<number>, observedNmIds: Set<number>): Coverage {
  const missingNmIds = [...expectedNmIds].filter((n) => !observedNmIds.has(n)).sort((a, b) => a - b);
  const unexpectedNmIds = [...observedNmIds].filter((n) => !expectedNmIds.has(n)).sort((a, b) => a - b);
  const matched = expectedNmIds.size - missingNmIds.length;
  return {
    expected: expectedNmIds.size,
    observed: observedNmIds.size,
    missing: missingNmIds.length,
    unexpected: unexpectedNmIds.length,
    // Знаменатель — ожидание по справочнику. Лишние товары покрытие не «улучшают».
    coveragePct: expectedNmIds.size === 0 ? 0 : Number(((matched / expectedNmIds.size) * 100).toFixed(2)),
    missingNmIds,
    unexpectedNmIds,
  };
}

export interface NormalizeCtx {
  observedAtIso: string;
  observationBucket: string;
  observationId: string;
  environment: string;
  runId: string;
  skuByNm: Map<number, string>;
}

/**
 * listGoods[] → строки RAW_WB_PRICES. Одна строка на (nm_id × size_id).
 * Товар без размеров — аномалия контракта: WB всегда отдаёт хотя бы один элемент sizes[].
 */
export function normalizeGoods(items: RawGoodsItem[], ctx: NormalizeCtx): RawPriceRow[] {
  const rows: RawPriceRow[] = [];
  const ingestedAt = new Date().toISOString();

  for (const it of items) {
    const nmRaw = num(it.nmID);
    if (nmRaw === null) throw new LoaderError('Элемент listGoods без nmID', 'WB_PRICES_NO_NMID');
    const nmId = nmRaw;

    const sizes = Array.isArray(it.sizes) ? (it.sizes as Record<string, unknown>[]) : [];
    if (sizes.length === 0) {
      throw new LoaderError(`nmID ${nmId}: пустой sizes[] — контракт WB нарушен`, 'WB_PRICES_NO_SIZES');
    }

    const itemJson = JSON.stringify(it);
    for (const s of sizes) {
      const price = num(s.price);
      if (price === null) {
        // Осознанный fail-closed: подставить 0 здесь означало бы соврать о цене.
        throw new LoaderError(`nmID ${nmId}: отсутствует sizes[].price`, 'WB_PRICES_NULL_PRICE');
      }
      const sizeId = num(s.sizeID);
      rows.push({
        observed_at: ctx.observedAtIso,
        observation_bucket: ctx.observationBucket,
        observation_id: ctx.observationId,
        environment: ctx.environment,
        run_id: ctx.runId,
        nm_id: nmId,
        internal_sku: ctx.skuByNm.get(nmId) ?? null,
        vendor_code: str(it.vendorCode),
        size_id: sizeId,
        tech_size_name: str(s.techSizeName),
        seller_list_price: price,
        seller_discount_pct: num(it.discount),
        seller_effective_price: num(s.discountedPrice),
        wb_club_discount_pct: num(it.clubDiscount),
        wb_club_price: num(s.clubDiscountedPrice),
        currency_code: str(it.currencyIsoCode4217),
        editable_size_price: typeof it.editableSizePrice === 'boolean' ? it.editableSizePrice : null,
        raw_item_json: itemJson,
        source_endpoint: WB_PRICES_SOURCE_ENDPOINT,
        source_payload_hash: createHash('sha256')
          .update(`${ctx.observationId}|${nmId}|${sizeId ?? ''}`)
          .digest('hex')
          .slice(0, 32),
        ingested_at: ingestedAt,
      });
    }
  }
  return rows;
}
