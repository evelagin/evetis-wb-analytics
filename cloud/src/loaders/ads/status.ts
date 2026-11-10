/**
 * Итоговый статус рекламного прогона — порт wbAdsOverallStatus_ (Wbadsdaily.gs:51) без изменений.
 * 🔴 Ровно три mart-критичных источника: campaigns + costs + fullstats. Ads-3 (ставки) и Ads-2
 *    (query stats) сюда НЕ входят: их сбой не должен закрывать freshness-гейт витрины.
 *   OK+OK+OK               → OK (или STALE, если stale === true)
 *   ≥1 PARTIAL без ошибок  → PARTIAL
 *   null / {} / нет status / неизвестный статус / не 3 результата / не массив → ERROR
 * Успех для журнала (INGEST_SUCCESS_STATUSES_.ads, IngestRunLog.gs:48): только OK и STALE.
 */
export const WB_ADS_EXPECTED_SOURCES = 3;

export interface SourceResult {
  source: string;
  status: string;
  rows: number;
  [k: string]: unknown;
}

export type OverallStatus = 'OK' | 'PARTIAL' | 'STALE' | 'ERROR';

export function overallStatus(results: unknown, stale: boolean | null): OverallStatus {
  const invalid =
    !Array.isArray(results) ||
    results.length !== WB_ADS_EXPECTED_SOURCES ||
    results.some((x) => !x || !(x as { status?: unknown }).status);
  if (invalid) return 'ERROR';
  const arr = results as Array<{ status: unknown }>;
  if (arr.some((x) => { const st = String(x.status).toUpperCase(); return st !== 'OK' && st !== 'PARTIAL'; })) return 'ERROR';
  if (arr.some((x) => String(x.status).toUpperCase() === 'PARTIAL')) return 'PARTIAL';
  return stale === true ? 'STALE' : 'OK';
}

/** Whitelist успеха журнала 'ads': всё прочее закрывает строку LOADER_RUNS как ERROR. */
export function isJournalSuccess(st: OverallStatus): boolean {
  return st === 'OK' || st === 'STALE';
}

/** Устаревание fullstats: today − maxDate > WB_ADS_DAILY_STALE_DAYS (wbAdsDailyFreshness_). */
export function isStale(todayMsk: string, maxDate: string, staleDays: number): boolean {
  if (!maxDate) return true;
  const diff = (Date.parse(`${todayMsk}T00:00:00Z`) - Date.parse(`${maxDate}T00:00:00Z`)) / 86_400_000;
  return diff > staleDays;
}
