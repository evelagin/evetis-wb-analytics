/**
 * UNITKA ENGINE v1 — логический период прогона: часовой слот Europe/Moscow (YYYY-MM-DDTHH).
 * Чистый модуль (тестируется без побочных эффектов).
 */
export function unitkaSlot(now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hour12: false,
  }).formatToParts(now);
  const get = (t: string): string => parts.find((p) => p.type === t)?.value ?? '00';
  const hour = get('hour') === '24' ? '00' : get('hour');
  return `${get('year')}-${get('month')}-${get('day')}T${hour}`;
}
