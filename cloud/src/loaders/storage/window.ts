/** UNITKA E4: календарное окно фактического платного хранения WB. */

/** YYYY-MM-DD minus N calendar days. Date-only is handled in UTC to avoid DST. */
export function minusDays(day: string, n: number): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if (!m) throw new Error(`Invalid date: ${day}`);
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  d.setUTCDate(d.getUTCDate() - n);
  return d.toISOString().slice(0, 10);
}

export function storageWindow(endDate: string, lookbackDays: number): { startDate: string; endDate: string } {
  if (!Number.isInteger(lookbackDays) || lookbackDays < 1 || lookbackDays > 8) {
    throw new Error(`STORAGE_LOOKBACK_DAYS must be 1..8, got ${lookbackDays}`);
  }
  return { startDate: minusDays(endDate, lookbackDays - 1), endDate };
}
