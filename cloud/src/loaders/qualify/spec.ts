/**
 * Загрузчик `qualify` (этап C, повторное ревью #426): писатель wb_ops.INGEST_QUALIFICATION.
 * Чистые части — разбор файлов паритета потоков и перевод их вердиктов в группы профиля координатора.
 *
 * SQL паритета живёт в ветках потоков (sql/mig_*_parity.sql) и попадает в образ при сборке
 * (deploy-shadow копирует в cloud/parity-sql). Файла нет — поток пропускается с MISSING_PARITY_SQL.
 * Группы проверок — ровно группы profiles.ts (тест cloud/test/qualify.test.ts).
 */
import type { Stream } from '../../ownership/streams.js';
import type { Verdict } from '../cutover/profiles.js';

export interface QualifyRow {
  stream: Stream;
  businessDate: string;
  checkGroup: string;
  verdict: Verdict;
  details: Record<string, unknown>;
}

/** Вердикты файлов паритета → вердикты квалификации. Всё, что «день не засчитывается», — MISSING. */
export function mapVerdict(v: unknown): Verdict {
  switch (String(v ?? '').toUpperCase()) {
    case 'EXACT': return 'PASS';
    case 'DATA_DRIFT': return 'DRIFT_EXPLAINED';
    case 'FAIL': return 'FAIL';
    default: return 'MISSING'; // NO_DATA, NO_RUN, MISSING, SOURCE_GAP, PENDING, REVIEW, пусто
  }
}

const WORST: Record<Verdict, number> = { PASS: 0, DRIFT_EXPLAINED: 1, MISSING: 2, FAIL: 3 };
export const worst = (a: Verdict, b: Verdict): Verdict => (WORST[a] >= WORST[b] ? a : b);

/** DATE из клиента BigQuery приходит объектом { value } — к строке YYYY-MM-DD. */
export function dateOf(v: unknown): string {
  const s = v !== null && typeof v === 'object' && 'value' in (v as object) ? String((v as { value: unknown }).value) : String(v ?? '');
  if (!/^\d{4}-\d{2}-\d{2}/.test(s)) throw new Error(`ожидалась дата, получено '${s}'`);
  return s.slice(0, 10);
}

const addDays = (d: string, n: number): string => new Date(Date.parse(`${d}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
const num = (v: unknown): number => Number(v ?? 0) || 0;

/**
 * Операторы файла паритета: QUERY 1, QUERY 2… — по порядку. Комментарии вне операторов отброшены.
 * Разделитель — `;` в конце строки (внутри файлов паритета `;` в строках нет).
 */
export function splitStatements(text: string): string[] {
  return text.split(/;[ \t]*(?:--[^\n]*)?\n/)
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.split('\n').some((l) => l.trim() !== '' && !l.trim().startsWith('--')));
}

type Row = Record<string, unknown>;

export interface StreamQualifySpec {
  stream: Stream;
  /** Имя файла паритета (в ветке потока — sql/<file>). */
  file: string;
  /** Номера операторов (1…), которые исполняются. */
  queries: number[];
  /** Группы из результатов операторов (индекс = номер оператора). */
  map: (results: Record<number, Row[]>) => QualifyRow[];
  /** Группы из журнала shadow-прогонов: загрузчик → группа (COMPLETE за сутки → PASS, только ERROR → FAIL). */
  shadowRunGroups: Readonly<Record<string, string>>;
  /** Загрузчики потока в shadow — для digest суток. */
  shadowLoaders: readonly string[];
  /** env с первой полной сутью shadow (подстановка в `DATE '…' AS d_from` файла). */
  dFromEnv?: string;
}

const day = (stream: Stream, businessDate: string, checkGroup: string, verdict: Verdict, details: Row): QualifyRow =>
  ({ stream, businessDate, checkGroup, verdict, details });

export const QUALIFY_SPECS: Readonly<Partial<Record<Stream, StreamQualifySpec>>> = Object.freeze({
  statistics: {
    stream: 'statistics',
    file: 'mig_m2_statistics_parity.sql',
    queries: [1],
    // QUERY 1: stream (orders|sales), day, verdict. Сверка продаж входит в состояния 'sales'; сама ночная
    // сверка — отдельной группой по журналу shadow.
    map: (r) => (r[1] ?? []).map((x) => day('statistics', dateOf(x.day), String(x.stream) === 'orders' ? 'STATS_ORDERS' : 'STATS_SALES',
      mapVerdict(x.verdict), { source: 'QUERY 1', ...pick(x, ['states_both', 'states_prod_only', 'states_cr_only', 'prod_only_unexplained', 'cr_only_unexplained', 'same_point_diff', 'sku_diff']) })),
    shadowRunGroups: { 'sales-reconcile': 'STATS_SALES_RECONCILE' },
    shadowLoaders: ['orders', 'sales', 'sales-reconcile'],
    dFromEnv: 'QUALIFY_D_FROM_STATISTICS',
  },
  ads: {
    stream: 'ads',
    file: 'mig_ads_parity.sql',
    queries: [1],
    // QUERY 1: run_day, costs_verdict (расходы — только EXACT), verdict (campaigns/costs/fullstats).
    map: (r) => (r[1] ?? []).flatMap((x) => [
      day('ads', dateOf(x.run_day), 'ADS_COSTS', mapVerdict(x.costs_verdict), { source: 'QUERY 1', ...pick(x, ['cost_matched', 'cost_one_sided', 'cost_struct_diff', 'cost_attr_diff']) }),
      day('ads', dateOf(x.run_day), 'ADS_STATS', mapVerdict(x.verdict), { source: 'QUERY 1', ...pick(x, ['prod_journal', 'cr_journal', 'fs_one_sided', 'fs_metric_diff']) }),
    ]),
    shadowRunGroups: { 'ads-costs-audit': 'ADS_COSTS_AUDIT' },
    shadowLoaders: ['ads', 'ads-query', 'ads-costs-audit'],
  },
  finance: {
    stream: 'finance',
    file: 'mig_finance_parity.sql',
    queries: [1, 3],
    map: (r) => {
      const out: QualifyRow[] = [];
      for (const x of r[1] ?? []) {
        const d = dateOf(x.report_date);
        const v = mapVerdict(x.verdict);
        out.push(day('finance', d, 'FINANCE_DAILY', v, { source: 'QUERY 1', ...pick(x, ['reports_both', 'reports_prod_only', 'reports_shadow_only']) }));
        // «Ноль необъяснённых денежных расхождений»: расхождение строк RAW, манифеста или дубль rrd_id = FAIL;
        // отчёт только на одной стороне — объяснимый лаг публикации (DRIFT_EXPLAINED).
        const money = num(x.rows_mismatch) + num(x.manifest_diff) + num(x.dup_rrd);
        const mv: Verdict = String(x.verdict) === 'NO_DATA' ? 'MISSING' : money > 0 ? 'FAIL' : v === 'DRIFT_EXPLAINED' ? 'DRIFT_EXPLAINED' : 'PASS';
        out.push(day('finance', d, 'FINANCE_MONEY', mv, { source: 'QUERY 1', ...pick(x, ['rows_equal', 'rows_mismatch', 'manifest_diff', 'dup_rrd']) }));
      }
      // QUERY 3: неделя × тип отчёта. WEEKLY-цикл — понедельник после недели (week_start + 7); худший по типам.
      const weekly = new Map<string, { v: Verdict; rows: Row[] }>();
      for (const x of r[3] ?? []) {
        const d = addDays(dateOf(x.week_start), 7);
        const cur = weekly.get(d) ?? { v: 'PASS' as Verdict, rows: [] };
        cur.v = worst(cur.v, mapVerdict(x.verdict));
        cur.rows.push(pick(x, ['report_type', 'p_final', 'c_final', 'same_weekly_report', 'recon_diff', 'verdict']));
        weekly.set(d, cur);
      }
      for (const [d, w] of weekly) out.push(day('finance', d, 'FINANCE_WEEKLY', w.v, { source: 'QUERY 3', weeks: w.rows }));
      return out;
    },
    shadowRunGroups: {},
    shadowLoaders: ['finance'],
  },
  supplies: {
    stream: 'supplies',
    file: 'mig_supplies_parity.sql',
    queries: [1],
    map: (r) => (r[1] ?? []).map((x) => day('supplies', dateOf(x.d), 'SUPPLIES', mapVerdict(x.verdict),
      { source: 'QUERY 1', ...pick(x, ['prod_runs_that_day', 'cr_runs_that_day', 'matched', 'prod_only', 'cr_only', 'unexplained']) })),
    shadowRunGroups: {},
    shadowLoaders: ['supplies'],
  },
  ref: {
    stream: 'ref',
    file: 'mig_ref_parity.sql',
    queries: [1],
    map: (r) => (r[1] ?? []).map((x) => day('ref', dateOf(x.d), 'REF', mapVerdict(x.verdict),
      { source: 'QUERY 1', ...pick(x, ['prod_rows', 'cr_rows', 'prod_only_rows', 'cr_only_rows', 'cr_errors']) })),
    shadowRunGroups: {},
    shadowLoaders: ['ref-sync'],
  },
});

function pick(x: Row, keys: string[]): Row {
  const o: Row = {};
  for (const k of keys) if (k in x) o[k] = typeof x[k] === 'object' && x[k] !== null ? String((x[k] as { value?: unknown }).value ?? x[k]) : x[k];
  return o;
}

/** Группы из журнала shadow: COMPLETE за сутки → PASS; только ERROR → FAIL. */
export function shadowGroups(stream: Stream, spec: StreamQualifySpec, runs: Row[]): QualifyRow[] {
  const out: QualifyRow[] = [];
  for (const r of runs) {
    const group = spec.shadowRunGroups[String(r.loader_name)];
    if (!group) continue;
    const v: Verdict = num(r.ok) > 0 ? 'PASS' : num(r.err) > 0 ? 'FAIL' : 'MISSING';
    out.push(day(stream, dateOf(r.d), group, v, { source: 'LOADER_RUNS shadow', complete: num(r.ok), error: num(r.err) }));
  }
  return out;
}
