/**
 * V_UNITKA_INTEGRITY_STATUS — семантика наблюдаемости: NO_RUN_YET / OK / ERROR / STALE.
 *
 * Две части:
 *  1. Статические проверки текста вью (идут всегда, в CI тоже): выбор оценки по маркеру, отсутствие смешения прогонов,
 *     приоритет состояний, порог свежести, ВЫВЕДЕННЫЙ из расписания Terraform.
 *  2. Сценарии на живом BigQuery (только при UNITKA_LIVE_BQ=1; в CI пропускаются): тело вью исполняется как обычный
 *     SELECT, где обе таблицы wb_ops заменены синтетическими типизированными массивами. Ни одной строки DML, ни одного
 *     обращения к настоящим таблицам — это проверяется для каждого сценария и офлайн.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { RUN_MARKER_CODE } from '../src/loaders/unitka/reconcile.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p: string): string => readFileSync(join(root, p), 'utf8');
const stripComments = (s: string): string => s.split('\n').map((l) => { const i = l.indexOf('--'); return i >= 0 ? l.slice(0, i) : l; }).join('\n');
const P = 'project-fa311fc0-4d87-4781-986';
const ISSUES = `\`${P}.wb_ops.UNITKA_INTEGRITY_ISSUES\``, LEDGER = `\`${P}.wb_ops.UNITKA_REPAIR_LEDGER\``;

function viewBody(): string {
  const sql = read('sql/unitka/reconcile_v1.sql');
  const at = sql.indexOf(`CREATE OR REPLACE VIEW \`${P}.wb_mart.V_UNITKA_INTEGRITY_STATUS\` AS\n`);
  expect(at).toBeGreaterThan(0);
  const body = sql.slice(sql.indexOf('\n', at) + 1);
  return body.slice(0, body.lastIndexOf(';'));
}
const body = viewBody();
const code = stripComments(body);
const flat = code.replace(/\s+/g, ' ');

/* ───────────────────────── 1. статически ───────────────────────── */

describe('V_UNITKA_INTEGRITY_STATUS: модель снимка и выбор оценки', () => {
  it('завершённая оценка = маркер прогона prod; по одному на run_id; выбор детерминирован', () => {
    expect(flat).toContain(`WHERE environment = 'prod' AND issue_key IS NULL AND code = '${RUN_MARKER_CODE}' GROUP BY run_id`);
    expect(flat).toContain('ROW_NUMBER() OVER (ORDER BY evaluated_at DESC, run_id DESC) AS rn');
    expect(flat).toContain('latest AS (SELECT run_id, evaluated_at, marker_value FROM ranked WHERE rn = 1)');
    expect(flat).toContain('previous AS (SELECT run_id FROM ranked WHERE rn = 2)');
  });
  it('строки разных прогонов не смешиваются: каждое чтение issue привязано к run_id выбранного маркера', () => {
    const reads = [...code.matchAll(/FROM `[^`]+UNITKA_INTEGRITY_ISSUES`([\s\S]*?)(?=\n\),|\n\)\n|$)/g)].map((m) => m[1]!.replace(/\s+/g, ' '));
    expect(reads).toHaveLength(3);                                                                // runs (маркеры), cur, prev
    expect(reads[0]).toContain("code = 'RUN_MARKER'");
    expect(reads[1]).toContain('JOIN latest l ON l.run_id = i.run_id');
    expect(reads[2]).toContain('JOIN previous p ON p.run_id = i.run_id');
    for (const r of reads) expect(r).toContain("environment = 'prod'");
    expect(flat).toContain("WHERE status = 'REPAIRED' AND environment = 'prod' AND run_id = (SELECT run_id FROM latest)");   // AUTO_REPAIRED — того же прогона
  });
  it('financial_health: ровно четыре значения, приоритет NO_RUN_YET → STALE → ERROR → OK; пустота никогда не OK', () => {
    const c = flat.match(/CASE (WHEN .*?) END AS financial_health/)![1]!;
    expect(c).toBe("WHEN s.run_id IS NULL THEN 'NO_RUN_YET' WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), s.evaluated_at, MINUTE) > cfg.stale_after_minutes THEN 'STALE' WHEN agg.data_error > 0 THEN 'ERROR' ELSE 'OK'");
    expect([...flat.matchAll(/THEN '(\w+)'|ELSE '(\w+)'/g)].map((m) => m[1] ?? m[2])).toEqual(['NO_RUN_YET', 'STALE', 'ERROR', 'OK']);
  });
  it('здоровье зависит только от DATA_ERROR: MANUAL_REQUIRED / NOT_AVAILABLE / LATE_DATA / WARNING в CASE не участвуют', () => {
    const c = flat.match(/CASE (WHEN .*?) END AS financial_health/)![1]!;
    expect(c).not.toMatch(/manual|not_available|late_data|warning/i);
    expect(flat).toContain('IF(s.run_id IS NULL, NULL, agg.manual_required > 0) AS manual_input_pending');
  });
  it('без завершённой оценки счётчики NULL («неизвестно»), а не 0', () => {
    for (const col of ['data_error', 'late_data', 'manual_required', 'not_available', 'warning', 'affected_sku_days', 'financially_invalid_sku_days', 'prices_from_funnel_fallback', 'repair_available_not_written']) {
      expect(flat, col).toContain(`IF(s.run_id IS NULL, NULL, agg.${col}) AS ${col}`);
    }
    expect(flat).toContain('IF(s.run_id IS NULL, NULL, rep.repaired_cells) AS auto_repaired_cells');
    expect(flat).toContain('IF(s.previous_run_id IS NULL, NULL, agg.new_since_previous_run) AS new_since_previous_run');
  });
  it('вью — только SELECT; читает только две таблицы wb_ops', () => {
    expect(code).not.toMatch(/\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b/i);
    expect(new Set([...code.matchAll(/`[^`]+\.(\w+\.\w+)`/g)].map((m) => m[1]))).toEqual(new Set(['wb_ops.UNITKA_INTEGRITY_ISSUES', 'wb_ops.UNITKA_REPAIR_LEDGER']));
  });
});

describe('порог свежести выведен из рабочего ритма Engine, а не выбран', () => {
  const tf = read('infra/terraform/unitka_engine.tf');
  const minutesOfDay = (cron: string): number => { const [m, h, dom, mon, dow] = cron.split(' '); expect([dom, mon, dow]).toEqual(['*', '*', '*']); return Number(h) * 60 + Number(m); };
  it('1320 мин = самый длинный штатный разрыв между прогонами (12:30 → 10:00 МСК) + timeout Job’а + 20 мин на старт и запись', () => {
    const block = tf.slice(tf.indexOf('unitka_schedules = {'), tf.indexOf('}', tf.indexOf('unitka_schedules = {')));
    const slots = [...block.matchAll(/=\s*"([^"]+)"/g)].map((m) => minutesOfDay(m[1]!)).sort((a, b) => a - b);
    expect(slots).toEqual([7 * 60, 9 * 60 + 30]);                                                // 07:00 и 09:30 UTC = 10:00 и 12:30 МСК
    const gaps = slots.map((t, i) => (slots[(i + 1) % slots.length]! - t + 1440) % 1440 || 1440);
    const maxGap = Math.max(...gaps);
    expect(maxGap).toBe(1290);                                                                   // 21 ч 30 мин
    const prodJob = tf.slice(tf.indexOf('resource "google_cloud_run_v2_job" "unitka_engine_prod"'));
    const timeoutMin = Number(prodJob.match(/timeout\s*=\s*"(\d+)s"/)![1]) / 60;
    expect(timeoutMin).toBe(10);
    const GRACE_MIN = 20;
    expect(flat).toContain(`WITH cfg AS ( SELECT ${maxGap + timeoutMin + GRACE_MIN} AS stale_after_minutes )`);
    expect(maxGap + timeoutMin + GRACE_MIN).toBe(1320);
    expect([...flat.matchAll(/\b1320\b/g)]).toHaveLength(1);                                     // константа задана один раз
  });
  it('порог короче суток: пропущенный утренний прогон виден до резервного окна следующего дня', () => {
    expect(1320).toBeLessThan(1440);
    expect(1320).toBeGreaterThan(1290 + 10);                                                     // и не даёт ложного STALE каждое утро
  });
});

/* ───────────────────────── 2. сценарии (живой BigQuery, только чтение) ───────────────────────── */

interface IssueRow { run: string; env?: string; ageMin: number; key: string | null; state?: string; code?: string; valid?: boolean; day?: string | null; nm?: number | null; marker?: string }
interface LedgerRow { run: string; env?: string; status: string; day: string; nm: number; ageMin: number }
const q = (v: string | null | undefined): string => (v === null || v === undefined ? 'NULL' : `'${v}'`);
const ago = (m: number): string => `TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${m} MINUTE)`;
const marker = (run: string, ageMin: number, mode = 'observe', env = 'prod'): IssueRow => ({ run, env, ageMin, key: null, code: 'RUN_MARKER', state: 'RUN', marker: `reconcile_mode=${mode}; issue_rows=0` });
const issue = (run: string, ageMin: number, state: string, n: number, over: Partial<IssueRow> = {}): IssueRow =>
  ({ run, ageMin, key: `2026-09-${String(n).padStart(2, '0')}|${n}|${over.code ?? state}`, state, code: over.code ?? state, valid: state !== 'DATA_ERROR', day: `2026-09-${String(n).padStart(2, '0')}`, nm: n, ...over });

/** Тело вью, где таблицы заменены синтетикой. Пустой набор — типизированный пустой массив (пустая таблица). */
export function statusQuery(issues: IssueRow[], ledger: LedgerRow[] = []): string {
  const iType = 'STRUCT<run_id STRING, environment STRING, evaluated_at TIMESTAMP, issue_key STRING, business_date DATE, nm_id INT64, code STRING, state STRING, financial_valid BOOL, source_value STRING>';
  const lType = 'STRUCT<run_id STRING, environment STRING, status STRING, business_date DATE, nm_id INT64, detected_at TIMESTAMP>';
  const iRows = issues.map((r) => `(${q(r.run)}, ${q(r.env ?? 'prod')}, ${ago(r.ageMin)}, ${q(r.key)}, ${r.day ? `DATE ${q(r.day)}` : 'NULL'}, ${r.nm ?? 'NULL'}, ${q(r.code)}, ${q(r.state)}, ${r.valid === undefined ? 'TRUE' : String(r.valid).toUpperCase()}, ${q(r.marker)})`);
  const lRows = ledger.map((r) => `(${q(r.run)}, ${q(r.env ?? 'prod')}, ${q(r.status)}, DATE ${q(r.day)}, ${r.nm}, ${ago(r.ageMin)})`);
  return body
    .replaceAll(ISSUES, `(SELECT * FROM UNNEST(ARRAY<${iType}>[${iRows.join(', ')}]))`)
    .replaceAll(LEDGER, `(SELECT * FROM UNNEST(ARRAY<${lType}>[${lRows.join(', ')}]))`);
}

const FRESH = 30, OLDER = 200;
const SCENARIOS: Array<{ name: string; issues: IssueRow[]; ledger?: LedgerRow[]; expect: Record<string, unknown> }> = [
  { name: 'пустая таблица → NO_RUN_YET, никогда не OK; счётчики NULL', issues: [],
    expect: { financial_health: 'NO_RUN_YET', run_id: null, evaluated_at: null, completed_evaluations: 0, data_error: null, auto_repaired_cells: null, manual_input_pending: null, repair_attempts_unconfirmed_7d: 0 } },
  { name: 'оборванный снимок (issue есть, маркера нет) завершённой оценкой не считается → NO_RUN_YET', issues: [issue('r1', FRESH, 'DATA_ERROR', 1)],
    expect: { financial_health: 'NO_RUN_YET', run_id: null, data_error: null } },
  { name: 'оценка только shadow-среды для prod не считается → NO_RUN_YET', issues: [marker('s1', FRESH, 'observe', 'shadow'), { ...issue('s1', FRESH, 'DATA_ERROR', 1), env: 'shadow' }],
    expect: { financial_health: 'NO_RUN_YET', completed_evaluations: 0 } },
  { name: 'observe-оценка с DATA_ERROR → ERROR', issues: [issue('r1', FRESH, 'DATA_ERROR', 1), issue('r1', FRESH, 'DATA_ERROR', 2), issue('r1', FRESH, 'MANUAL_REQUIRED', 3), marker('r1', FRESH)],
    expect: { financial_health: 'ERROR', run_id: 'r1', reconcile_mode: 'observe', data_error: 2, manual_required: 1, financially_invalid_sku_days: 2, affected_sku_days: 3, auto_repaired_cells: 0, new_since_previous_run: null, oldest_unresolved_data_error: '2026-09-01' } },
  { name: 'observe-оценка без DATA_ERROR и без issue вообще (один маркер) → OK; отличима от «оценки не было»', issues: [marker('r1', FRESH)],
    expect: { financial_health: 'OK', run_id: 'r1', completed_evaluations: 1, data_error: 0, manual_input_pending: false, auto_repaired_cells: 0 } },
  { name: 'только MANUAL_REQUIRED → здоровье не ERROR; ручной ввод — отдельным флагом', issues: [...[1, 2, 3, 4, 5].map((n) => issue('r1', FRESH, 'MANUAL_REQUIRED', n)), marker('r1', FRESH)],
    expect: { financial_health: 'OK', manual_required: 5, manual_input_pending: true, data_error: 0, financially_invalid_sku_days: 0 } },
  { name: 'только NOT_AVAILABLE (сколько угодно) → здоровье не ERROR', issues: [...Array.from({ length: 24 }, (_, i) => issue('r1', FRESH, 'NOT_AVAILABLE', i + 1)), marker('r1', FRESH)],
    expect: { financial_health: 'OK', not_available: 24, data_error: 0, affected_sku_days: 0 } },
  { name: 'только WARNING и LATE_DATA → здоровье не ERROR; недействительность LATE_DATA считается отдельно', issues: [issue('r1', FRESH, 'WARNING', 1), issue('r1', FRESH, 'LATE_DATA', 2, { valid: false }), marker('r1', FRESH)],
    expect: { financial_health: 'OK', warning: 1, late_data: 1, data_error: 0, financially_invalid_sku_days: 1 } },
  { name: 'последняя оценка старше порога → STALE (даже с DATA_ERROR: это не текущее состояние)', issues: [issue('r1', 1330, 'DATA_ERROR', 1), marker('r1', 1330)],
    expect: { financial_health: 'STALE', run_id: 'r1', data_error: 1, stale_after_minutes: 1320 } },
  { name: 'чистая оценка старше порога → тоже STALE, не OK', issues: [marker('r1', 1330)], expect: { financial_health: 'STALE', data_error: 0 } },
  { name: 'оценка моложе порога (1310 мин) → не STALE', issues: [marker('r1', 1310)], expect: { financial_health: 'OK' } },
  { name: 'два прогона: берётся последний по маркеру; строки прошлого прогона не подмешиваются; новые и решённые считаются',
    issues: [issue('old', OLDER, 'DATA_ERROR', 1), issue('old', OLDER, 'DATA_ERROR', 2), issue('old', OLDER, 'DATA_ERROR', 3), marker('old', OLDER),
      issue('new', FRESH, 'DATA_ERROR', 2), issue('new', FRESH, 'MANUAL_REQUIRED', 9), marker('new', FRESH, 'write')],
    expect: { financial_health: 'ERROR', run_id: 'new', reconcile_mode: 'write', completed_evaluations: 2, data_error: 1, manual_required: 1, new_since_previous_run: 1, resolved_since_previous_run: 2 } },
  { name: 'два прогона: прошлый с ошибками, последний чистый → OK (ошибки прошлого не протекают)',
    issues: [issue('old', OLDER, 'DATA_ERROR', 1), issue('old', OLDER, 'DATA_ERROR', 2), marker('old', OLDER), marker('new', FRESH)],
    expect: { financial_health: 'OK', run_id: 'new', data_error: 0, new_since_previous_run: 0, resolved_since_previous_run: 2 } },
  { name: 'последний прогон оборвался без маркера → текущей остаётся прошлая ЗАВЕРШЁННАЯ оценка',
    issues: [marker('old', OLDER), issue('new', FRESH, 'DATA_ERROR', 1)],
    expect: { financial_health: 'OK', run_id: 'old', data_error: 0, completed_evaluations: 1 } },
  { name: 'одинаковое время двух маркеров → выбор детерминирован (run_id DESC), строки не смешаны',
    issues: [issue('a', FRESH, 'DATA_ERROR', 1), marker('a', FRESH), marker('b', FRESH)], expect: { run_id: 'b', data_error: 0, financial_health: 'OK' } },
  { name: 'журнал ремонта: AUTO_REPAIRED — только REPAIRED и только того же прогона; неподтверждённые попытки — отдельно',
    issues: [marker('old', OLDER, 'write'), marker('new', FRESH, 'write')],
    ledger: [{ run: 'new', status: 'REPAIRED', day: '2026-09-17', nm: 1, ageMin: FRESH }, { run: 'new', status: 'REPAIRED', day: '2026-09-17', nm: 1, ageMin: FRESH }, { run: 'old', status: 'REPAIRED', day: '2026-09-08', nm: 2, ageMin: OLDER },
      { run: 'new', status: 'WRITE_FAILED', day: '2026-09-09', nm: 3, ageMin: FRESH }, { run: 'x', status: 'APPLIED_UNVERIFIED', day: '2026-09-10', nm: 4, ageMin: OLDER }, { run: 'new', status: 'PLANNED_NOT_WRITTEN', day: '2026-09-11', nm: 5, ageMin: FRESH },
      { run: 'new', env: 'shadow', status: 'REPAIRED', day: '2026-09-12', nm: 6, ageMin: FRESH }],
    expect: { financial_health: 'OK', auto_repaired_cells: 2, auto_repaired_sku_days: 1, repair_attempts_unconfirmed_7d: 2 } },
  { name: 'observe: ремонтов нет и не изображается — auto_repaired 0, режим виден', issues: [issue('r1', FRESH, 'DATA_ERROR', 1, { code: 'PRICE_NOT_ON_SHEET' }), marker('r1', FRESH, 'observe')],
    expect: { financial_health: 'ERROR', reconcile_mode: 'observe', auto_repaired_cells: 0, repair_available_not_written: 1 } },
];

describe('сценарии вью статуса на синтетических данных', () => {
  it.each(SCENARIOS)('запрос сценария — чистый SELECT без обращения к настоящим таблицам: $name', (s) => {
    const sql = statusQuery(s.issues, s.ledger);
    expect(sql.trimStart().startsWith('WITH cfg AS')).toBe(true);
    expect(stripComments(sql)).not.toMatch(/\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b/i);
    expect(sql).not.toMatch(/wb_ops|wb_mart|wb_raw/);
  });

  const live = process.env.UNITKA_LIVE_BQ === '1';
  it.runIf(live).each(SCENARIOS)('BigQuery: $name', async (s) => {
    const { BqClient } = await import('../src/bq/client.js');
    const rows = await new BqClient(P, 'EU').query<Record<string, unknown>>(statusQuery(s.issues, s.ledger));
    expect(rows).toHaveLength(1);                                                                 // вью всегда даёт ровно одну строку, даже на пустоте
    const norm = (v: unknown): unknown => (v !== null && typeof v === 'object' && 'value' in (v as Record<string, unknown>) ? (v as { value: unknown }).value : v);
    const got = Object.fromEntries(Object.entries(rows[0]!).map(([k, v]) => [k, norm(v)]));
    expect(got).toMatchObject(s.expect);
    expect(['NO_RUN_YET', 'OK', 'ERROR', 'STALE']).toContain(got.financial_health);
  }, 60_000);
});
