/**
 * Fail-closed guards ручного прогона mart (PR-Mart3b-1, аудит REV4 блокер #1).
 *
 * Отдельного wb_mart_shadow НЕ существует — процедуры всегда публикуют production
 * `wb_mart.*`. Поэтому ручной entry-point обязан быть жёстко ограничен утверждённым
 * контрактом controlled production run:
 *   - ТОЛЬКО ENVIRONMENT=prod (иначе строка MART_RUNS будет помечена 'shadow',
 *     хотя перезаписан production MART — validation по prod её даже не свяжет);
 *   - ТОЛЬКО target_date = D-1 Europe/Moscow (историческая дата откатила бы витрину назад);
 *   - дата обязана быть РЕАЛЬНОЙ календарной (regex пропускает 2026-02-31).
 *
 * Исторический backfill — ОТДЕЛЬНЫЙ явно опасный entry-point/флаг со своим контрактом
 * (вне PR3b-1); в текущий production runner молча не включается.
 *
 * Чистый модуль без побочных эффектов — тестируется напрямую.
 */
import { d1Moscow } from './targetDate.js';

/** Строка является реальной календарной датой YYYY-MM-DD (не только по форме). */
export function isCanonicalDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

/**
 * Бросает Error при нарушении контракта ручного прогона; молча возвращается при OK.
 * `now` инжектится в тестах.
 */
export function assertManualRunAllowed(environment: string, targetDate: string, now: Date = new Date()): void {
  if (environment !== 'prod') {
    throw new Error(
      `MART_MANUAL_ENV: manual mart публикует production wb_mart и разрешён только с ENVIRONMENT=prod (получено '${environment}')`,
    );
  }
  if (!isCanonicalDate(targetDate)) {
    throw new Error(`MART_MANUAL_DATE: '${targetDate}' не является реальной календарной датой YYYY-MM-DD`);
  }
  const expected = d1Moscow(now);
  if (targetDate !== expected) {
    throw new Error(
      `MART_MANUAL_TARGET: targetDate=${targetDate}, ожидается строго D-1=${expected} (backfill — отдельный entry-point, не этот)`,
    );
  }
}
