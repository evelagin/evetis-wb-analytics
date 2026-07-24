/** Вычисление logical_period. Для дневных загрузчиков — дата в TZ Москва. */
export function dailyPeriodMoscow(now: Date = new Date()): string {
  // en-CA даёт формат YYYY-MM-DD
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(now);
}
