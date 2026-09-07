/**
 * Нормализация тарифов WB (PR-2). Чистые функции.
 *
 * Главное правило файла: WB отдаёт числа СТРОКАМИ с запятой как разделителем
 * ("62,1", "0,08") и сентинелами ("-", "не принимает"). Сентинел означает
 * «WB не даёт значения», а не ноль. Логистика, равная нулю, и логистика,
 * которой нет, — разные состояния, и подмена первой второй занизила бы
 * ценовой пол ровно на стоимость доставки.
 */
import { LoaderError } from '../../errors.js';

export type TariffKind = 'COMMISSION' | 'BOX' | 'RETURN' | 'PALLET';

export interface TariffRow {
  observed_at: string;
  observation_date: string;
  observation_id: string;
  environment: string;
  run_id: string;
  tariff_kind: TariffKind;
  entity_key: string | null;
  entity_name: string | null;
  parent_key: string | null;
  parent_name: string | null;
  metric: string;
  value_num: number | null;
  value_raw: string | null;
  is_parsed: boolean;
  effective_next: string | null;
  effective_till_max: string | null;
  source_endpoint: string;
  raw_row_json: string;
  ingested_at: string;
}

/** Сентинелы WB, означающие отсутствие значения. Список закрытый и проверяемый. */
export const SENTINELS: readonly string[] = ['-', '—', '', 'не принимает', 'нет'];

/**
 * "62,1" → 62.1 ; "-" → null ; "не принимает" → null.
 * Никогда не возвращает 0 для неразобранного значения.
 */
export function parseTariffNumber(raw: unknown): { value: number | null; parsed: boolean } {
  if (raw === null || raw === undefined) return { value: null, parsed: false };
  if (typeof raw === 'number') {
    return Number.isFinite(raw) ? { value: raw, parsed: true } : { value: null, parsed: false };
  }
  const s = String(raw).trim();
  if (SENTINELS.includes(s.toLowerCase()) || SENTINELS.includes(s)) return { value: null, parsed: false };
  // Пробелы-разделители тысяч ("1 039") и запятая как десятичный разделитель.
  const n = Number(s.replace(/[\s\u00a0]/g, '').replace(',', '.'));
  return Number.isFinite(n) ? { value: n, parsed: true } : { value: null, parsed: false };
}

function isoDateOrNull(v: unknown): string | null {
  const s = v === null || v === undefined ? '' : String(v).trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : null;
}

export interface NormalizeCtx {
  observedAtIso: string;
  observationDate: string;
  observationId: string;
  environment: string;
  runId: string;
}

/** Поля-идентификаторы, которые не являются метриками тарифа. */
const ID_FIELDS = new Set([
  'parentID', 'parentName', 'subjectID', 'subjectName', 'warehouseName', 'geoName',
]);

/**
 * Любой массив строк тарифа → длинный формат «одна строка на метрику».
 * Новое поле WB автоматически становится новой метрикой и не теряется:
 * схема таблицы от состава полей не зависит.
 */
export function normalizeTariffRows(
  kind: TariffKind,
  rows: Array<Record<string, unknown>>,
  endpoint: string,
  ctx: NormalizeCtx,
  effective?: { next?: unknown; tillMax?: unknown },
): TariffRow[] {
  if (!Array.isArray(rows)) throw new LoaderError(`${kind}: ожидался массив строк тарифа`, 'WB_TARIFF_SHAPE');
  const out: TariffRow[] = [];
  const ingestedAt = new Date().toISOString();
  const effNext = isoDateOrNull(effective?.next);
  const effTill = isoDateOrNull(effective?.tillMax);

  for (const r of rows) {
    const isCommission = kind === 'COMMISSION';
    const entityKey = isCommission ? (r.subjectID ?? null) : (r.warehouseName ?? null);
    const entityName = isCommission ? (r.subjectName ?? null) : (r.warehouseName ?? null);
    const parentKey = isCommission ? (r.parentID ?? null) : (r.geoName ?? null);
    const parentName = isCommission ? (r.parentName ?? null) : (r.geoName ?? null);
    const rowJson = JSON.stringify(r);

    for (const [metric, raw] of Object.entries(r)) {
      if (ID_FIELDS.has(metric)) continue;
      const { value, parsed } = parseTariffNumber(raw);
      out.push({
        observed_at: ctx.observedAtIso,
        observation_date: ctx.observationDate,
        observation_id: ctx.observationId,
        environment: ctx.environment,
        run_id: ctx.runId,
        tariff_kind: kind,
        entity_key: entityKey === null ? null : String(entityKey),
        entity_name: entityName === null ? null : String(entityName),
        parent_key: parentKey === null ? null : String(parentKey),
        parent_name: parentName === null ? null : String(parentName),
        metric,
        value_num: value,
        value_raw: raw === null || raw === undefined ? null : String(raw),
        is_parsed: parsed,
        effective_next: effNext,
        effective_till_max: effTill,
        source_endpoint: endpoint,
        raw_row_json: rowJson,
        ingested_at: ingestedAt,
      });
    }
  }
  if (out.length === 0) throw new LoaderError(`${kind}: ноль метрик — тариф пуст`, 'WB_TARIFF_EMPTY');
  return out;
}
