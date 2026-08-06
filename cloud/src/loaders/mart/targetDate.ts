/**
 * Целевая дата витрины = D-1 Europe/Moscow (YYYY-MM-DD). Отдельный чистый модуль,
 * чтобы тестировать D-1 без побочных эффектов (mart_manual.ts исполняет main() при импорте).
 * В PR-Mart3b-2 переиспользуется при wiring mart в generic CLI.
 */
import { dailyPeriodMoscow } from '../../period.js';

export function d1Moscow(now: Date = new Date()): string {
  const today = dailyPeriodMoscow(now); // YYYY-MM-DD (МСК)
  const d = new Date(`${today}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1); // арифметика над date-only, tz-агностична
  return d.toISOString().slice(0, 10);
}
