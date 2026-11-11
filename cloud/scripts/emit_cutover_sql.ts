/**
 * Этап C, ревью #426: выгрузка ВСЕГО SQL координатора и gate (store.ts, gate.ts, WriterGate.gs) с параметрами —
 * для проверки парсером BigQuery (tools/bq_dryrun_cutover_sql.py, только dryRun). Ничего не исполняет.
 *
 *   cd cloud && npx vite-node --config vitest.config.ts scripts/emit_cutover_sql.ts > /tmp/cutover_sql.json
 */
import vm from 'node:vm';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { BqCutoverStore } from '../src/loaders/cutover/store.js';
import { BqOwnershipReader, gateMark, ownershipAssertSql, recheckMark } from '../src/ownership/gate.js';
import { insertSql, prepareStatement, shadowRunsSql } from '../src/loaders/qualify/index.js';
import { QUALIFY_SPECS, splitStatements } from '../src/loaders/qualify/spec.js';
import { existsSync } from 'node:fs';
import { STREAMS, type Stream } from '../src/ownership/streams.js';

const PROJECT = process.env.GCP_PROJECT_ID || 'project-fa311fc0-4d87-4781-986';
type Param = { name: string; type: string; value: string | null };
type Stmt = { name: string; sql: string; params: Param[] };
const out: Stmt[] = [];

function typed(params: Record<string, unknown> = {}, types: Record<string, string> = {}): Param[] {
  return Object.entries(params).map(([name, v]) => ({
    name,
    type: types[name] ?? (typeof v === 'number' ? 'INT64' : 'STRING'),
    value: v === null || v === undefined ? null : String(v),
  }));
}

let current = '';
const client = {
  projectId: PROJECT,
  location: 'EU',
  async query(sql: string, params?: Record<string, unknown>, types?: Record<string, string>) {
    out.push({ name: current, sql, params: typed(params, types) });
    return [] as never[];
  },
};

const T = Date.parse('2026-11-16T07:00:00Z');
const store = new BqCutoverStore(client as never, 'wb_ops', 'wb_raw');
const reader = new BqOwnershipReader(client as never, 'wb_ops');

async function main(): Promise<void> {
  current = 'store.read';
  await store.read('ads');
  current = 'store.cas';
  await store.cas(
    { stream: 'ads', epoch: 1, state: 'QUALIFIED', revision: 2 },
    { owner: 'NONE', state: 'LEGACY_FENCED', epoch: 2, revision: 3, legacyWatermark: null, cloudWatermark: null,
      fencedAtMs: T, activeSinceMs: null, checksSinceMs: T, changedBy: 'dryrun', reason: 'dry-run' },
    { transitionId: 'ads:r3', stream: 'ads', fromState: 'QUALIFIED', toState: 'LEGACY_FENCED', epoch: 2, runId: 'r',
      sourceWatermark: null, reason: 'dry-run', evidence: { a: 1 } },
  );
  current = 'store.qualification';
  await store.qualification('finance', '2026-10-26');
  for (const s of STREAMS as readonly Stream[]) {
    current = `store.legacyRuns.${s}`;
    await store.legacyRuns(s, T - 86_400_000, true);
    current = `store.cloudRuns.${s}`;
    await store.cloudRuns(s, T - 86_400_000);
    current = `store.currentWatermark.${s}`;
    await store.currentWatermark(s);
    current = `store.writeJobs.${s}`;
    await store.writeJobs(s, T - 86_400_000);
    current = `store.gateEvidence.${s}`;
    await store.gateEvidence(s, T - 86_400_000);
    current = `gate.reader.${s}`;
    await reader.read(s, gateMark(s, 'loader'));
    current = `gate.recheck.${s}`;
    await reader.read(s, recheckMark(s, 'loader'));
  }
  current = 'store.continuityViolations';
  await store.continuityViolations('statistics', '{"orders":"2026-11-16 09:31:05","sales":"2026-11-16T09:22:40"}', T);
  current = 'store.statsWatermarkBefore';
  await store.statsWatermarkBefore(T);
  current = 'store.duplicates.stocks';
  await store.duplicates('stocks', T);
  current = 'store.downstreamAlive.mart';
  await store.downstreamAlive('mart', T);
  current = 'store.downstreamAlive.ref_active';
  await store.downstreamAlive('ref_active', T);
  out.push({ name: 'gate.ownershipAssertSql', sql: `${ownershipAssertSql(PROJECT, 'wb_ops', { stream: 'finance', epoch: 3 })};\nSELECT 1`, params: [] });

  // Apps Script: запрос gate и пересинхронизация watermark — снимаются с исполняемого WriterGate.gs в node:vm.
  const here = dirname(fileURLToPath(import.meta.url));
  const dir = join(here, '..', '..', 'apps-script', 'ingestion');
  const asStmts: Stmt[] = [];
  const props: Record<string, string> = { BQ_PROJECT_ID: PROJECT, BQ_DATASET: 'wb_raw', BQ_LOCATION: 'EU' };
  const sb: Record<string, unknown> = {
    console: { log: () => {}, error: () => {} },
    PropertiesService: { getScriptProperties: () => ({ getProperty: (k: string) => props[k] ?? null, setProperty: (k: string, v: string) => { props[k] = v; } }) },
    BigQuery: { Jobs: { query: (req: { query: string; queryParameters?: Array<{ name: string; parameterType: { type: string }; parameterValue: { value: string } }> }) => {
      asStmts.push({ name: req.query.includes('INGEST_WRITER_OWNERSHIP') ? 'apps_script.gate' : 'apps_script.resync_statistics', sql: req.query,
        params: (req.queryParameters ?? []).map((p) => ({ name: p.name, type: p.parameterType.type, value: p.parameterValue.value })) });
      return req.query.includes('INGEST_WRITER_OWNERSHIP')
        ? { jobComplete: true, rows: [{ f: [{ v: 'APPS_SCRIPT' }, { v: '5' }] }] }
        : { jobComplete: true, rows: [{ f: [{ v: '2026-11-16 09:31:05' }, { v: '2026-11-16T09:22:40' }] }] };
    } } },
  };
  vm.createContext(sb);
  for (const f of readdirSync(dir).filter((x) => x.endsWith('.gs')).sort()) vm.runInContext(readFileSync(join(dir, f), 'utf8'), sb, { filename: f });
  (sb.wbWriterGate_ as (s: string, e: string) => unknown)('statistics', 'runWbOrdersIncremental');
  const seen = new Set<string>();
  for (const st of asStmts) if (!seen.has(st.name)) { seen.add(st.name); out.push(st); }

  // qualify: журнал shadow, запись вердиктов и операторы файлов паритета (если есть — QUALIFY_SQL_DIR).
  for (const [s, spec] of Object.entries(QUALIFY_SPECS)) {
    out.push({ name: `qualify.shadowRuns.${s}`, sql: shadowRunsSql(PROJECT, 'wb_raw', spec!.shadowLoaders), params: [] });
  }
  out.push({ name: 'qualify.insert', sql: insertSql(PROJECT, 'wb_ops'), params: [{ name: 'payload', type: 'STRING',
    value: JSON.stringify([{ stream: 'ref', business_date: '2026-11-15', check_group: 'REF', verdict: 'PASS', details: { a: 1 }, image_digest: '' }]) }] });
  const pdir = process.env.QUALIFY_SQL_DIR;
  if (pdir) {
    for (const [s, spec] of Object.entries(QUALIFY_SPECS)) {
      const f = join(pdir, spec!.file);
      if (!existsSync(f)) continue;
      const stmts = splitStatements(readFileSync(f, 'utf8'));
      for (const n of spec!.queries) out.push({ name: `qualify.parity.${s}.query${n}`, sql: prepareStatement(stmts[n - 1]!, undefined), params: [] });
    }
  }

  process.stdout.write(JSON.stringify(out, null, 1));
}

main().catch((e) => { process.stderr.write(String(e) + '\n'); process.exit(1); });
