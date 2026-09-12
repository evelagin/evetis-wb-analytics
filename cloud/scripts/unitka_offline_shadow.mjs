#!/usr/bin/env node
/**
 * UNITKA ENGINE v1 — офлайн SHADOW-прогон без облачных учёток.
 *
 * Вход: снимок листа (JSON той же формы, что читает readSnapshot: grid/formulas/mirrorLcd/
 * mirrorRev/namedLcd — например, из экспорта книги в xlsx) и строки вью V_UNITKA_*
 * (JSON: {facts, logistics, commission, lcd, freshness} — TO_JSON_STRING(ARRAY_AGG(t))).
 * Прогоняет РЕАЛЬНЫЙ код Engine (UnitkaBq → buildPlan → evaluate{shadow}) и печатает отчёт.
 *
 *   npm run build && node scripts/unitka_offline_shadow.mjs snapshot.json bq.json
 *
 * Ничего не пишет ни в Sheets, ни в BigQuery.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { UnitkaBq } from '../dist/loaders/unitka/bq.js';
import { buildPlan, diffRows } from '../dist/loaders/unitka/plan.js';
import { evaluate } from '../dist/loaders/unitka/qa.js';

const [snapPath, bqPath, diffOut] = process.argv.slice(2);
if (!snapPath || !bqPath) { console.error('usage: unitka_offline_shadow.mjs <snapshot.json> <bq.json> [diff.csv]'); process.exit(2); }
const snap = JSON.parse(readFileSync(snapPath, 'utf8'));
const data = JSON.parse(readFileSync(bqPath, 'utf8'));

const runner = {
  projectId: 'offline',
  async query(sql) {
    if (sql.includes('V_UNITKA_SOURCE_FRESHNESS')) return data.freshness;
    if (sql.includes('V_UNITKA_LAST_CLOSED_DATE')) return data.lcd;
    if (sql.includes('V_UNITKA_DAILY_FACT')) return data.facts;
    if (sql.includes('V_UNITKA_LOGISTICS_RATES')) return data.logistics;
    if (sql.includes('V_UNITKA_COMMISSION_RATES')) return data.commission;
    throw new Error('offline: unexpected query ' + sql.slice(0, 60));
  },
};
const bq = new UnitkaBq(runner, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS');
const lcd = await bq.lastClosedDate();
const [facts, logistics, commission] = await Promise.all([bq.facts(), bq.logisticsRates(), bq.commissionRates()]);

let plan;
try {
  plan = buildPlan({ snapshot: snap, lcd, facts, logistics, commission, minN: 10, maxLagDays: 2 });
} catch (e) {
  console.log(JSON.stringify({ result: 'FAIL_CLOSED', code: e.code, message: e.message }, null, 2));
  process.exit(1);
}
const qa = evaluate(snap, plan, { shadow: true });
const byKind = {};
for (const c of plan.cells) byKind[c.kind] = (byKind[c.kind] ?? 0) + 1;
const report = {
  lcd: plan.lcd, d1_msk: plan.d1Msk, lag_days: plan.lagDays, closed_days: plan.closedDays, blocks: plan.blocks.length,
  invariant: plan.invariant, reverse_rate: plan.reverseRate,
  rates_own_direct: plan.rates.filter((r) => r.directSource === 'own').length,
  rates_own_commission: plan.rates.filter((r) => r.commissionSource === 'own').length,
  expected_cells: plan.expected.length, cells_planned: plan.cells.length, by_kind: byKind, gaps: plan.gaps,
  qa: qa.checks.map((c) => `${c.name}: ${c.pass ? 'PASS' : 'FAIL'} (${c.count})`),
  qa_samples: Object.fromEntries(qa.checks.filter((c) => !c.pass).map((c) => [c.name, c.sample])),
  book_lcd: plan.bookLcd, by_change_type: plan.byChangeType, stock_projection_future: plan.stockProjectionCells,
  legacy_formulas: plan.legacy, legacy_replaced: plan.legacyReplaced,
  diff: diffRows(plan),
};
if (diffOut) {
  const cols = ['DATE', 'SKU', 'CELL', 'METRIC', 'OLD', 'NEW', 'CHANGE_TYPE', 'SOURCE', 'REASON'];
  const esc = (v) => `"${String(v).replace(/"/g, '""')}"`;
  writeFileSync(diffOut, [cols.join(','), ...report.diff.map((r) => cols.map((c) => esc(r[c])).join(','))].join('\n') + '\n');
  report.diff = `${report.diff.length} строк → ${diffOut}`;
}
console.log(JSON.stringify(report, null, 2));
