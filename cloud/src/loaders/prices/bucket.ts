/**
 * Логический период наблюдателя цен = 20-минутное окно UTC.
 *
 * Зачем окно, а не сутки. Execution-guard (LOADER_RUNS) идемпотентен по
 * (environment, loader_name, logical_period). Для суточных загрузчиков период —
 * дата, и повтор в те же сутки корректно подавляется. Наблюдателю цен ровно это
 * поведение нужно ВНУТРИ окна и категорически не нужно МЕЖДУ окнами:
 *
 *   - ретрай того же окна после инфраструктурного сбоя → guard видит COMPLETE → пропуск;
 *   - следующее окно через 20 минут → другой logical_period → законное новое наблюдение,
 *     даже если цена не изменилась.
 *
 * Поэтому период здесь — не дата, а метка окна. Колонка LOADER_RUNS.logical_period
 * имеет тип STRING и формат не навязывает; политика периода объявляется загрузчиком
 * в registry.ts. UTC, а не Москва: наблюдение — событие мирового времени, локальное
 * представление — забота витрин.
 */

/** Ширина окна наблюдения в минутах. Совпадает с каденсом Cloud Scheduler. */
export const OBSERVATION_BUCKET_MINUTES = 20;

/** Окно наблюдения: YYYY-MM-DDTHH:MM (UTC), минуты выровнены вниз по ширине окна. */
export function observationBucket(now: Date = new Date(), widthMin = OBSERVATION_BUCKET_MINUTES): string {
  if (!Number.isInteger(widthMin) || widthMin <= 0 || widthMin > 60 || 60 % widthMin !== 0) {
    throw new Error(`Ширина окна должна быть делителем 60 минут, получено ${widthMin}`);
  }
  const floored = Math.floor(now.getUTCMinutes() / widthMin) * widthMin;
  const p = (n: number, w = 2): string => String(n).padStart(w, '0');
  return (
    `${p(now.getUTCFullYear(), 4)}-${p(now.getUTCMonth() + 1)}-${p(now.getUTCDate())}` +
    `T${p(now.getUTCHours())}:${p(floored)}`
  );
}

/**
 * Детерминированный id снимка. Стабилен между попытками одного окна —
 * по нему манифест находит свою строку, а RAW-строки склеиваются с манифестом.
 */
export function pricesObservationId(environment: string, bucket: string): string {
  return `WBPX_${environment}_${bucket.replace(/[-:T]/g, '')}`;
}

/**
 * Детерминированный jobId для load-джобы. BigQuery дедуплицирует load по jobId,
 * поэтому повтор окна не создаёт вторую копию тех же строк.
 */
export function pricesLoadJobId(environment: string, bucket: string, targetTable: string): string {
  const table = targetTable.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return `wbprices_${environment}_${bucket.replace(/[-:T]/g, '')}_${table}`;
}
