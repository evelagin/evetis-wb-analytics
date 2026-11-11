/**
 * Оценка квалификации потока по wb_ops.INGEST_QUALIFICATION (чистая функция, тестируется без BigQuery).
 *
 * Вердикт (дата, группа) — ПОСЛЕДНЯЯ строка по `at`: повторная проверка дня заменяет прежнюю.
 * Засчитанные сутки — подряд от самой свежей business_date назад, пока день сопоставим: у каждой daily-группы
 * допустимый вердикт. Первый несопоставимый день обрывает счёт (пять PASS «с дырой» — не пять подряд).
 *   PASS    — сутки набраны, weekly/once выполнены, digest один (если требуется), квалификация свежая;
 *   FAIL    — самый свежий день несопоставим, или в засчитанных днях есть недопустимый weekly-вердикт,
 *             или засчитаны разные образы при sameDigest → BLOCKED;
 *   PENDING — данных пока мало/устарели, но и отказа нет → поток продолжает SHADOW_VALIDATING.
 */
import type { QualRequirement, Verdict } from './profiles.js';

export interface QualRow {
  businessDate: string; // YYYY-MM-DD
  checkGroup: string;
  verdict: Verdict | string;
  imageDigest: string;
  atMs: number;
}

export interface QualEvaluation {
  status: 'PASS' | 'FAIL' | 'PENDING';
  reason: string;
  countedDays: string[];
  digests: string[];
  newestDate: string | null;
}

const dayMs = 86_400_000;
const isoDow = (date: string): number => {
  const d = new Date(`${date}T00:00:00Z`).getUTCDay();
  return d === 0 ? 7 : d;
};
const daysBetween = (a: string, b: string): number =>
  Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / dayMs);

export function evaluateQualification(rows: readonly QualRow[], req: QualRequirement, todayMsk: string): QualEvaluation {
  // Последний вердикт по (дата, группа).
  const latest = new Map<string, QualRow>();
  for (const r of rows) {
    const k = `${r.businessDate}|${r.checkGroup}`;
    const prev = latest.get(k);
    if (!prev || r.atMs > prev.atMs) latest.set(k, r);
  }
  const verdictOf = (date: string, group: string): QualRow | undefined => latest.get(`${date}|${group}`);
  const dates = [...new Set(rows.map((r) => r.businessDate))].sort().reverse();
  const empty = (status: QualEvaluation['status'], reason: string): QualEvaluation =>
    ({ status, reason, countedDays: [], digests: [], newestDate: dates[0] ?? null });
  const newest = dates[0];
  if (newest === undefined) return empty('PENDING', 'вердиктов квалификации ещё нет');

  const daily = req.groups.filter((g) => g.cadence === 'daily');
  const comparable = (date: string): string | null => {
    for (const g of daily) {
      const v = verdictOf(date, g.group);
      if (!v) return `${date}: нет вердикта группы ${g.group}`;
      if (!g.accept.includes(v.verdict as Verdict)) return `${date}: ${g.group} = ${v.verdict}`;
    }
    return null;
  };

  const hardFailOn = (date: string): boolean => daily.some((g) => {
    const v = verdictOf(date, g.group);
    return v !== undefined && !g.accept.includes(v.verdict as Verdict);
  });
  if (hardFailOn(newest)) return empty('FAIL', comparable(newest) ?? 'недопустимый вердикт');
  // Самый свежий день без части вердиктов (ещё досчитывается) — пропускаем его, счёт идёт с предыдущего.
  const start = comparable(newest) === null ? 0 : 1;
  const counted: string[] = [];
  let breakReason: string | null = start === 1 ? comparable(newest) : null;
  for (const d of dates.slice(start)) {
    const why = comparable(d);
    if (why) { breakReason = why; break; }
    counted.push(d);
  }
  if (counted.length === 0) return empty('PENDING', breakReason ?? 'нет сопоставимых суток');

  const window = counted.slice(0, req.minComparableDays);
  // weekly/once: недопустимый вердикт в засчитанных днях — отказ; допустимый — нужен хотя бы один.
  for (const g of req.groups.filter((x) => x.cadence !== 'daily')) {
    for (const d of window) {
      const v = verdictOf(d, g.group);
      if (v && !g.accept.includes(v.verdict as Verdict)) {
        return { status: 'FAIL', reason: `${d}: ${g.group} = ${v.verdict}`, countedDays: window, digests: [], newestDate: newest };
      }
    }
  }

  const digests = [...new Set(window.flatMap((d) => daily.map((g) => verdictOf(d, g.group)?.imageDigest ?? '')))];
  const digest = digests[0] ?? '';
  const result = (status: QualEvaluation['status'], reason: string): QualEvaluation =>
    ({ status, reason, countedDays: window, digests, newestDate: newest });

  if (req.sameDigest && (digests.length !== 1 || digest === '')) {
    return result('FAIL', `засчитанные сутки — не один образ: ${JSON.stringify(digests)}`);
  }
  if (daysBetween(newest, todayMsk) > req.maxStaleDays) {
    return result('PENDING', `квалификация устарела: последняя дата ${newest}, сегодня ${todayMsk}`);
  }
  if (counted.length < req.minComparableDays) {
    return result('PENDING', `сопоставимых суток подряд ${counted.length} из ${req.minComparableDays}`);
  }
  for (const g of req.groups.filter((x) => x.cadence === 'once')) {
    const ok = window.some((d) => {
      const v = verdictOf(d, g.group);
      return v !== undefined && g.accept.includes(v.verdict as Verdict)
        && (!req.sameDigest || v.imageDigest === digest);
    });
    if (!ok) return result('PENDING', `нет допустимого вердикта ${g.group}${req.sameDigest ? ` для образа ${digest}` : ''} в засчитанных сутках`);
  }
  for (const g of req.groups.filter((x) => x.cadence === 'weekly')) {
    const recent = dates.filter((d) => daysBetween(d, newest) < 7);
    const ok = recent.some((d) => {
      const v = verdictOf(d, g.group);
      return v !== undefined && g.accept.includes(v.verdict as Verdict) && (g.weekday === undefined || isoDow(d) === g.weekday);
    });
    if (!ok) {
      return result('PENDING', `нет допустимого недельного вердикта ${g.group}${g.weekday ? ` (день недели ${g.weekday})` : ''} за 7 суток`);
    }
  }
  return result('PASS', `сопоставимых суток подряд: ${counted.length} (нужно ${req.minComparableDays})`);
}
