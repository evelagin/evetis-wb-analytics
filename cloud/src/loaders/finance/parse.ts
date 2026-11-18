/**
 * M4 · Чистые помощники разбора Finance API — порт Wbfinancedaily.gs 1:1.
 * Ни одного побочного эффекта: всё тестируется офлайн.
 */
import { createHash } from 'node:crypto';
import { FIN_BIGINT_FIELDS } from './constants.js';

/** finDailyIsDigits_ */
export function isDigits(s: unknown): boolean {
  return /^\d+$/.test(String(s));
}

/** finDailyIsDate_ — календарная проверка (2026-99-99 → false). */
export function isCalendarDate(s: unknown): boolean {
  const v = String(s);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
  const d = new Date(`${v}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().substring(0, 10) === v;
}

/** finDailyNum_ — строка/число WB → число (пробелы убраны, запятая → точка, мусор → 0). */
export function finNum(v: unknown): number {
  if (v === null || v === undefined || v === '') return 0;
  if (typeof v === 'number') return Number.isFinite(v) ? v : 0;
  const n = Number(String(v).replace(/\s/g, '').replace(',', '.'));
  return Number.isNaN(n) ? 0 : n;
}

/** finDailyR2_ */
export function r2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** finDailyMd5_ — hex MD5 строки UTF-8 (row_hash). */
export function md5Hex(s: string): string {
  return createHash('md5').update(s, 'utf8').digest('hex');
}

/** finDailyWeekStart_ — понедельник ISO-недели даты 'YYYY-MM-DD' (даты WB уже бизнес-даты МСК). */
export function weekStart(ds: string): string {
  const d = new Date(`${String(ds).substring(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return '';
  const dow = (d.getUTCDay() + 6) % 7; // 0=пн … 6=вс
  return new Date(d.getTime() - dow * 86_400_000).toISOString().substring(0, 10);
}

/** finDailyTodayMsk_ — дата МСК со сдвигом в сутках. */
export function todayMsk(nowMs: number, offsetDays = 0): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date(nowMs + offsetDays * 86_400_000));
}

/** finDailyQuoteBigInts_ — BigInt-поля в кавычки ДО JSON.parse (точность int64 сохраняется). */
export function quoteBigInts(body: string): string {
  const re = new RegExp(`"(${FIN_BIGINT_FIELDS.join('|')})"\\s*:\\s*(-?\\d+)`, 'g');
  return body.replace(re, '"$1":"$2"');
}

export interface SafeParse {
  arr: Array<Record<string, unknown>> | null;
  rawIds: string[];
  mappingError: boolean;
  note: string;
}

/**
 * finDailyParseSafe_ — idField извлекается регексом из сырого body (строки), body с закавыченными
 * BigInt парсится; несовпадение количества или значений → PARSE_MAPPING_ERROR.
 */
export function parseSafe(body: string, idField: string): SafeParse {
  const out: SafeParse = { arr: null, rawIds: [], mappingError: false, note: '' };
  const re = new RegExp(`"${idField}"\\s*:\\s*(-?\\d+)`, 'g');
  for (let m = re.exec(body); m !== null; m = re.exec(body)) out.rawIds.push(m[1]!);
  let safe: unknown;
  try {
    safe = JSON.parse(quoteBigInts(body));
  } catch (e) {
    out.mappingError = true;
    out.note = `safe parse: ${String(e)}`;
    return out;
  }
  if (Array.isArray(safe)) out.arr = safe as Array<Record<string, unknown>>;
  else if (safe && typeof safe === 'object' && (safe as { data?: unknown }).data) {
    // Как в Apps Script: `safe.data ? safe.data : null` — не-массив data дальше даёт «массив не распознан».
    const data = (safe as { data: unknown }).data;
    out.arr = Array.isArray(data) ? (data as Array<Record<string, unknown>>) : null;
  }
  if (!out.arr) {
    out.mappingError = true;
    out.note = 'массив не распознан';
    return out;
  }
  if (out.rawIds.length !== out.arr.length) {
    out.mappingError = true;
    out.note = `PARSE_MAPPING_ERROR: regex ${out.rawIds.length} vs объектов ${out.arr.length}`;
    return out;
  }
  for (let i = 0; i < out.arr.length; i++) {
    if (String(out.arr[i]![idField]) !== out.rawIds[i]) {
      out.mappingError = true;
      out.note = `PARSE_MAPPING_ERROR: raw!=safe @${i}`;
      return out;
    }
  }
  return out;
}

/**
 * Финансовый run_id: FINDAILY_yyyyMMdd_HHmmss_C<0..9999> (время МСК). Формат Apps Script, кроме буквы `C`
 * перед случайным суффиксом (у Apps Script — только цифры): строки и отчёты, записанные Cloud Run, отличимы
 * в RAW/манифесте без отдельной колонки (`processing_run_id LIKE 'FINDAILY_%_C%'`) — нужно для отката.
 * Формат run_id никто не разбирает (поиск по sql/, cloud/, apps-script/ 10.11.2026).
 */
export function newFinanceRunId(nowMs: number, rand: () => number = Math.random): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(new Date(nowMs));
  const g = (t: string): string => parts.find((p) => p.type === t)?.value ?? '00';
  const hh = g('hour') === '24' ? '00' : g('hour');
  return `FINDAILY_${g('year')}${g('month')}${g('day')}_${hh}${g('minute')}${g('second')}_C${Math.floor(rand() * 10000)}`;
}

/**
 * finDailyBqJobId_ — детерминированный jobId load-job'а: fin_<report_id>_<run_id>_<batch>.
 * run_id в ключе обязателен (ownership-контракт V_WB_FINANCE_COMPLETE: r.run_id = m.processing_run_id).
 */
export function financeLoadJobId(reportId: string, runId: string, batchIndex: number): string {
  const safe = `fin_${reportId}_${runId}_${batchIndex}`.replace(/[^A-Za-z0-9_-]/g, '-');
  return safe.length > 1024 ? safe.substring(0, 1024) : safe;
}
