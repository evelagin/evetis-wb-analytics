/**
 * Окно fence (чистая функция). QUALIFIED → LEGACY_FENCED допустим в момент t, только если интервал
 * [t, t + дренаж + запас] не пересекается ни с одним плановым запуском потока — ни Apps Script, ни Cloud Run.
 * Занятость запуска: [старт − jitter, старт + jitter + maxRun] (МСК, без перехода на летнее время — его нет).
 */
import type { ScheduleEvent } from './profiles.js';

const MSK_OFFSET_MIN = 180;

/** Минута суток МСК и день недели (0 = вс) для момента. */
function msk(ms: number): { minuteOfDay: number; dow: number; dayIndex: number } {
  const m = Math.floor(ms / 60_000) + MSK_OFFSET_MIN;
  const dayIndex = Math.floor(m / 1440);
  // 1970-01-01 — четверг (4).
  return { minuteOfDay: ((m % 1440) + 1440) % 1440, dow: (((dayIndex + 4) % 7) + 7) % 7, dayIndex };
}

function startsAt(e: ScheduleEvent, minuteMs: number): boolean {
  const { minuteOfDay, dow } = msk(minuteMs);
  if (e.dow && !e.dow.includes(dow)) return false;
  const h = Math.floor(minuteOfDay / 60);
  const mi = minuteOfDay % 60;
  if (e.hours !== 'any' && !e.hours.includes(h)) return false;
  // Минута неизвестна: занят весь час — моделируем стартом в каждую минуту часа.
  return e.minute === 'any' ? true : mi === e.minute;
}

export interface WindowCheck {
  allowed: boolean;
  /** Конфликтующие запуски (метка и старт МСК), если окно занято. */
  conflicts: string[];
}

export function fenceWindowCheck(nowMs: number, events: readonly ScheduleEvent[], busyAheadMin: number): WindowCheck {
  const t0 = Math.floor(nowMs / 60_000) * 60_000;
  const t1 = t0 + busyAheadMin * 60_000;
  const conflicts: string[] = [];
  for (const e of events) {
    const before = e.jitterMin + e.maxRunMin + 1;
    for (let s = t0 - before * 60_000; s <= t1 + e.jitterMin * 60_000; s += 60_000) {
      if (!startsAt(e, s)) continue;
      const busyFrom = s - e.jitterMin * 60_000;
      const busyTo = s + (e.jitterMin + e.maxRunMin) * 60_000;
      if (busyFrom <= t1 && busyTo >= t0) {
        const { minuteOfDay } = msk(s);
        conflicts.push(`${e.writer}:${e.label}@${String(Math.floor(minuteOfDay / 60)).padStart(2, '0')}:${String(minuteOfDay % 60).padStart(2, '0')}`);
        break;
      }
    }
  }
  return { allowed: conflicts.length === 0, conflicts };
}

/** Дата МСК YYYY-MM-DD. */
export function mskDate(ms: number): string {
  return new Date(ms + MSK_OFFSET_MIN * 60_000).toISOString().slice(0, 10);
}

/** ISO-день недели МСК (1 = пн). */
export function mskIsoDow(ms: number): number {
  const d = msk(ms).dow;
  return d === 0 ? 7 : d;
}
