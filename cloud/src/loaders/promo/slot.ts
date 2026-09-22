/**
 * Логический период наблюдателя акций = слот расписания (4 раза в сутки, UTC).
 *
 * Почему слот, а не сутки и не фиксированное окно.
 *
 *   - Суточный период схлопнул бы четыре наблюдения в одно: execution-guard
 *     (LOADER_RUNS идемпотентен по (environment, loader, logical_period))
 *     пропустил бы второй и последующие прогоны дня. Состав акции меняется
 *     внутри суток, и каждое наблюдение обязано остаться отдельной строкой.
 *   - Окно фиксированной ширины (как 20 минут у наблюдателя цен) здесь не
 *     подходит: расписание 04/09/14/19 UTC не делится на равные части, между
 *     19:00 и 04:00 девять часов. Слот — это ближайшее ПРЕДШЕСТВУЮЩЕЕ время
 *     запуска, поэтому ретрай внутри слота подавляется, а следующий слот
 *     всегда даёт новый логический период.
 *
 * Расписание объявлено здесь и в infra/terraform/promo_observer.tf. Расхождение
 * между ними означало бы, что часть слотов никогда не наступает или что два
 * запуска попадают в один слот; оба случая проверяются тестом.
 */

/** Часы запуска наблюдателя, UTC. 07/12/17/22 МСК. Обоснование каденса — PROMOTION_DECISION_ENGINE_SPEC §9. */
export const PROMO_SLOT_HOURS_UTC: readonly number[] = [4, 9, 14, 19];

const p = (n: number, w = 2): string => String(n).padStart(w, '0');

/**
 * Слот наблюдения: YYYY-MM-DDTHH:00 (UTC) ближайшего предшествующего запуска.
 * До первого слота суток относится к последнему слоту предыдущих суток.
 */
export function promoSlot(now: Date = new Date(), hours: readonly number[] = PROMO_SLOT_HOURS_UTC): string {
  if (hours.length === 0) throw new Error('Список часов слотов пуст');
  const sorted = [...hours].sort((a, b) => a - b);
  for (const h of sorted) {
    if (!Number.isInteger(h) || h < 0 || h > 23) throw new Error(`Час слота вне 0..23: ${h}`);
  }
  const hour = now.getUTCHours();
  const past = sorted.filter((h) => h <= hour);
  if (past.length > 0) {
    const slotHour = past[past.length - 1] as number;
    return `${p(now.getUTCFullYear(), 4)}-${p(now.getUTCMonth() + 1)}-${p(now.getUTCDate())}T${p(slotHour)}:00`;
  }
  // Раньше первого слота суток — принадлежим последнему слоту вчерашнего дня.
  const prev = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - 24 * 3600 * 1000);
  const slotHour = sorted[sorted.length - 1] as number;
  return `${p(prev.getUTCFullYear(), 4)}-${p(prev.getUTCMonth() + 1)}-${p(prev.getUTCDate())}T${p(slotHour)}:00`;
}

/** Детерминированный id снимка. Стабилен между попытками одного слота. */
export function promoObservationId(marketplace: string, environment: string, slot: string): string {
  return `${marketplace}PROMO_${environment}_${slot.replace(/[-:T]/g, '')}`;
}

/**
 * Детерминированный jobId load-джобы. BigQuery дедуплицирует load по jobId,
 * поэтому повтор слота не создаёт вторую копию строк.
 */
export function promoLoadJobId(marketplace: string, environment: string, slot: string, targetTable: string): string {
  const table = targetTable.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return `${marketplace.toLowerCase()}promo_${environment}_${slot.replace(/[-:T]/g, '')}_${table}`;
}
