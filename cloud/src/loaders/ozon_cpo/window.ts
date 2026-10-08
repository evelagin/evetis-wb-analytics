/**
 * OZON CPO (Phase B) — окна загрузки отчёта «Оплата за заказ» по ДАТЕ СПИСАНИЯ (колонка «Дата»).
 *
 * Отчёт фильтруется датой списания, а не датой заказа. Между заказом и списанием проходит
 * 0–41 сутки (замер 08.10.2026 на 315 строках 2025-05…2026-10: p99 = 27, max = 41). Ежедневный
 * прогон перечитывает 45 закрытых суток списания: это максимум наблюдённой задержки с запасом и
 * то же окно, что у перезаписи Ozon-Юнитки, поэтому поздняя правка Ozon в любой строке, которая
 * влияет на окно Юнитки, перечитывается штатно.
 *
 * Только ЗАКРЫТЫЕ сутки МСК: сегодняшние списания ещё накапливаются (08.10 отчёт уже видел
 * списания дня, которых не было в финансах). Окно, включающее открытые сутки, отклоняется —
 * частичный отчёт никогда не загружается как полный.
 *
 * Границы запроса — моменты времени UTC, а сутки отчёта — московские (см. память
 * ozon-performance-api-quirks, п. 5): сутки D МСК = [D−1 21:00Z, D 20:59:59Z].
 */
import { dailyPeriodMoscow } from '../../period.js';

export const CPO_DAILY_LOOKBACK_DAYS = 45;
/** Длина одного запроса. Отчёт статистики Ozon молча отдаёт ноль на окне > 62 дней — берём месяц. */
export const CPO_CHUNK_MAX_DAYS = 31;
/** Нижняя граница истории: первое списание «Оплаты за заказ» EVETIS — май 2025. */
export const CPO_HISTORY_START = '2025-05-01';
/** Предел одного запуска бэкфилла: 20 месяцев × 2 семейства отчётов. */
export const CPO_BACKFILL_MAX_DAYS = 620;

const ISO = /^\d{4}-\d{2}-\d{2}$/;

export function addDays(day: string, n: number): string {
  if (!ISO.test(day)) throw new Error(`Invalid date: ${day}`);
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000) + 1;
}

export interface CpoChunk { from: string; to: string }
export interface CpoWindow { mode: 'DAILY' | 'BACKFILL'; from: string; to: string; chunks: CpoChunk[] }

/** Сутки МСК → границы запроса (RFC 3339, UTC). */
export function mskDayBoundsUtc(from: string, to: string): { fromUtc: string; toUtc: string } {
  const start = new Date(Date.parse(`${from}T00:00:00Z`) - 3 * 3_600_000);
  const end = new Date(Date.parse(`${to}T23:59:59Z`) - 3 * 3_600_000);
  return { fromUtc: start.toISOString().replace('.000Z', 'Z'), toUtc: end.toISOString().replace('.000Z', 'Z') };
}

/** Деление окна на запросы: границы календарных месяцев, не длиннее CPO_CHUNK_MAX_DAYS. */
export function chunkWindow(from: string, to: string): CpoChunk[] {
  const out: CpoChunk[] = [];
  let cur = from;
  while (cur <= to) {
    const monthEnd = new Date(Date.UTC(Number(cur.slice(0, 4)), Number(cur.slice(5, 7)), 0)).toISOString().slice(0, 10);
    const end = monthEnd < to ? monthEnd : to;
    out.push({ from: cur, to: end });
    cur = addDays(end, 1);
  }
  for (const c of out) if (daysBetween(c.from, c.to) > CPO_CHUNK_MAX_DAYS) throw new Error(`chunk ${c.from}..${c.to}`);
  return out;
}

/**
 * Окно прогона. DAILY: [вчера−44, вчера] МСК. BACKFILL: явные границы, только закрытые сутки,
 * не раньше начала истории и не длиннее предела запуска.
 */
export function cpoWindow(a: { now: Date; backfillFrom?: string; backfillTo?: string; lookbackDays?: number }): CpoWindow {
  const yesterday = addDays(dailyPeriodMoscow(a.now), -1);
  if (!a.backfillFrom && !a.backfillTo) {
    const n = a.lookbackDays ?? CPO_DAILY_LOOKBACK_DAYS;
    if (!Number.isInteger(n) || n < 1 || n > 62) throw new Error(`lookback ${n}: ожидается 1..62`);
    const from = addDays(yesterday, -(n - 1));
    return { mode: 'DAILY', from, to: yesterday, chunks: chunkWindow(from, yesterday) };
  }
  const from = a.backfillFrom ?? '', to = a.backfillTo ?? '';
  if (!ISO.test(from) || !ISO.test(to)) throw new Error('бэкфилл: нужны обе границы OZON_CPO_BACKFILL_FROM/TO в формате YYYY-MM-DD');
  if (from > to) throw new Error('бэкфилл: начало позже конца');
  if (from < CPO_HISTORY_START) throw new Error(`бэкфилл: раньше начала истории ${CPO_HISTORY_START}`);
  if (to > yesterday) throw new Error(`бэкфилл: ${to} — открытые сутки (закрыты по ${yesterday}); частичный отчёт не загружается`);
  if (daysBetween(from, to) > CPO_BACKFILL_MAX_DAYS) throw new Error(`бэкфилл: длиннее ${CPO_BACKFILL_MAX_DAYS} суток`);
  return { mode: 'BACKFILL', from, to, chunks: chunkWindow(from, to) };
}
