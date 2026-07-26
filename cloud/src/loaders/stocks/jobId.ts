/**
 * Детерминированный jobId для load-джобы остатков (PR-Mig1 fix аудита).
 * Стабилен для стабильного ключа (environment × logical period × target table),
 * НЕ зависит от случайного snapshot_id → повтор периода не создаёт дубль-запись.
 */
export function stocksLoadJobId(environment: string, logicalPeriod: string, targetTable: string): string {
  const period = logicalPeriod.replace(/-/g, '');
  const table = targetTable
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return `stocks_${environment}_${period}_${table}`;
}
