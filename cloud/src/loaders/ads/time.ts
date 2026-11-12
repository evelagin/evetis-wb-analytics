/**
 * Время и окна рекламного загрузчика. Чистые функции — тестируются офлайн.
 *
 * 🔴 ФОРМАТ load_ts / window_completed_at / snapshot_ts — 'yyyy-MM-dd HH:mm:ss' ПО МОСКВЕ,
 *    как wbAdsNow_() в Apps Script (WbAdsProbe.gs:694). Это не косметика: вью V_ADV_* и
 *    V_ADV_COSTS_SNAPSHOT упорядочивают строки по SAFE_CAST(<это поле> AS TIMESTAMP).
 *    Строка без зоны читается как UTC, поэтому ISO-время UTC от Cloud Run «отстало» бы
 *    на 3 часа от строк Apps Script, и на переключении вью выбрали бы не тот снапшот.
 */

import { WB_ADS_FULLSTATS_WINDOW_DAYS } from './constants.js';

const MSK_PARTS = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Europe/Moscow',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  hourCycle: 'h23',
});

function mskParts(d: Date): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of MSK_PARTS.formatToParts(d)) out[p.type] = p.value;
  return out;
}

/** 'yyyy-MM-dd HH:mm:ss' Europe/Moscow — эквивалент wbAdsNow_(). */
export function mskNow(d: Date = new Date()): string {
  const p = mskParts(d);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

/** 'yyyyMMdd_HHmmss' Europe/Moscow — часть run_id, как в wbAdsRawNewRunId_(). */
export function mskCompact(d: Date = new Date()): string {
  const p = mskParts(d);
  return `${p.year}${p.month}${p.day}_${p.hour}${p.minute}${p.second}`;
}

/** 'yyyy-MM-dd' Europe/Moscow. */
export function mskDate(d: Date = new Date()): string {
  return mskNow(d).slice(0, 10);
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

export function isIsoDate(s: string): boolean {
  if (!DATE_RE.test(s)) return false;
  const t = Date.parse(`${s}T00:00:00Z`);
  return Number.isFinite(t) && new Date(t).toISOString().slice(0, 10) === s;
}

/** Арифметика над датой без времени (tz-агностична). */
export function addDays(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export interface Range { from: string; to: string }

/**
 * Окно fullstats от ЛОГИЧЕСКОГО периода (D−1): D−7 … D−1.
 * Apps Script считает то же от часов (wbAdsLast7Range_: вчера−6 … вчера) — при одном и том же
 * моменте запуска значения совпадают, а журнал и окно не могут разъехаться на полуночи.
 */
export function fullstatsRange(logicalPeriod: string): Range {
  return { from: addDays(logicalPeriod, -(WB_ADS_FULLSTATS_WINDOW_DAYS - 1)), to: logicalPeriod };
}

/**
 * Окно «D−fromDaysAgo … D−toDaysAgo» (wbAdsCostsRangeBack_), где D = сегодня по Москве.
 * Аргумент — СЕГОДНЯ МСК (не D−1).
 */
export function rangeBack(todayMsk: string, fromDaysAgo: number, toDaysAgo: number): Range {
  return { from: addDays(todayMsk, -fromDaysAgo), to: addDays(todayMsk, -toDaysAgo) };
}

/** Число суток в окне включительно (wbAdsCostsDayCount_). 0 — если дата не разобрана. */
export function dayCount(from: string, to: string): number {
  if (!DATE_RE.test(from) || !DATE_RE.test(to)) return 0;
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000) + 1;
}

/**
 * Делит [from, to] на смежные неперекрывающиеся окна ≤ maxDays (wbAdsSplitPeriod_).
 * Неразбираемые даты или from > to — одно окно как есть (поведение эталона).
 */
export function splitPeriod(from: string, to: string, maxDays: number): Range[] {
  if (!DATE_RE.test(from) || !DATE_RE.test(to) || from > to) return [{ from, to }];
  const out: Range[] = [];
  let cur = from;
  while (cur <= to) {
    let end = addDays(cur, maxDays - 1);
    if (end > to) end = to;
    out.push({ from: cur, to: end });
    cur = addDays(end, 1);
  }
  return out;
}

/** Список суток [from, to] по возрастанию (wbAdsQsResolveDays_). */
export function daysOf(from: string, to: string): string[] {
  if (!isIsoDate(from) || !isIsoDate(to)) throw new Error(`ожидается yyyy-MM-dd, получено ${from}…${to}`);
  if (from > to) throw new Error(`from позже to: ${from} > ${to}`);
  const out: string[] = [];
  for (let d = from; d <= to; d = addDays(d, 1)) {
    out.push(d);
    if (out.length > 400) throw new Error('окно длиннее 400 суток — вероятно ошибка в датах');
  }
  return out;
}
