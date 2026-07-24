/**
 * Нормализация значения BigQuery TIMESTAMP в ISO-строку (PR-Mig0, фикс аудита №9).
 * @google-cloud/bigquery возвращает TIMESTAMP как объект BigQueryTimestamp { value },
 * а не как простую строку. Наивная проверка `typeof === 'string'` молча подменяла
 * время на текущее, из-за чего stale-recovery никогда не срабатывал.
 */
export function normalizeBqTimestamp(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === 'string') return v;
  if (v instanceof Date) return v.toISOString();
  if (typeof v === 'object') {
    const val = (v as { value?: unknown }).value;
    if (typeof val === 'string') return val;
    if (val instanceof Date) return val.toISOString();
  }
  return null;
}
