#!/usr/bin/env node
/**
 * UNITKA INTEGRITY GUARD V1 — офлайн сухой прогон (Phase 1C1 → инструмент для 1C3).
 *
 * НИЧЕГО не пишет ни в Sheets, ни в BigQuery, ни в журналы. Прогоняет РЕАЛЬНЫЙ код Guard
 * (UnitkaBq-нормализация → evaluateIntegrity → summarize) на выгрузках:
 *
 *   integrity.json — строки SELECT тела V_UNITKA_INTEGRITY  (bq query --format=json …)
 *   cogs.json      — строки V_UNITKA_COGS_CANONICAL (с snapshot_published_at). До публикации физической
 *                    копии допустимо ЭМУЛИРОВАТЬ её read-only SELECT'ом по канону (см. доку §7).
 *                    Файл отсутствует → COGS_SNAPSHOT_UNAVAILABLE.
 *   --cogs-published-at=ISO — подменить время публикации копии (фикстура STALE / свежей копии).
 *   snapshot.json  — снимок листа: { grid, formulas, refValues } в форме readSnapshot
 *                    (grid — строки GRID.TOP..GRID.MTD, formulas — GRID.FIRST..FIRST+DAYS-1)
 *
 *   npm run build && node scripts/unitka_integrity_dry_run.mjs integrity.json snapshot.json [cogs.json] [--now=ISO]
 *
 * Выход: JSON-отчёт (сводка, issue без INFO, контрольные проверки). Код 0 — отчёт построен.
 */
import { readFileSync, existsSync } from 'node:fs';
import { UnitkaBq } from '../dist/loaders/unitka/bq.js';
import { findBlocks, GRID } from '../dist/loaders/unitka/model.js';
import { evaluateIntegrity, summarize, parseHhMm, aggregateStatus, classifyCogsSnapshot } from '../dist/loaders/unitka/integrity.js';

const args = process.argv.slice(2);
const flags = Object.fromEntries(args.filter((a) => a.startsWith('--')).map((a) => a.slice(2).split('=')));
const [integrityPath, snapPath, cogsPath] = args.filter((a) => !a.startsWith('--'));
if (!integrityPath || !snapPath) {
  console.error('usage: unitka_integrity_dry_run.mjs <integrity.json> <snapshot.json> [cogs.json] [--now=ISO] [--due=HH:MM] [--cogs-published-at=ISO]');
  process.exit(2);
}
const integrityRows = JSON.parse(readFileSync(integrityPath, 'utf8'));
const cogsRows = cogsPath && existsSync(cogsPath) ? JSON.parse(readFileSync(cogsPath, 'utf8')) : null;
const snap = JSON.parse(readFileSync(snapPath, 'utf8'));
const now = flags.now ? new Date(flags.now) : new Date();

const runner = {
  projectId: 'offline',
  async query(sql) {
    if (sql.includes('V_UNITKA_COGS_CANONICAL')) { if (cogsRows === null) throw new Error('offline: COGS не выгружен'); return cogsRows; }
    if (sql.includes('V_UNITKA_INTEGRITY')) return integrityRows;
    throw new Error('offline: неожиданный запрос ' + sql.slice(0, 60));
  },
};
const bq = new UnitkaBq(runner, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS');
const facts = await bq.integrityFacts();
let cogsRead;
try {
  cogsRead = await bq.cogsCanonical();
  if (flags['cogs-published-at']) cogsRead = { ...cogsRead, publishedAt: flags['cogs-published-at'] };
} catch (e) { cogsRead = { error: e instanceof Error ? e.message : String(e) }; }
const cogs = classifyCogsSnapshot(cogsRead, now);

const lcd = facts.reduce((m, r) => (r.lastClosedDate > m ? r.lastClosedDate : m), '');
const monthStart = `${lcd.slice(0, 7)}-01`;
const blocks = findBlocks(snap.grid[0] ?? []);
const cellAt = (row, col) => { const r = snap.grid[row - GRID.TOP]; const v = r ? r[col - 1] : undefined; return v === undefined ? null : v; };
const formulaAt = (row, col) => { const r = snap.formulas[row - GRID.FIRST]; const v = r ? r[col - 1] : undefined; return v === undefined ? null : v; };

const issues = evaluateIntegrity({
  facts, cogs, blocks, lcd, monthStart, cellAt, formulaAt,
  refValues: snap.refValues ?? {}, now, storageDueMinutes: parseHhMm(flags.due ?? '12:15'),
});
const summary = summarize(issues, 'observe', 'PRE_WRITE', now, cogs);
const pick = (code) => issues.filter((i) => i.code === code);
const report = {
  now: now.toISOString(), lcd, month_start: monthStart, facts_rows: facts.length, blocks: blocks.length,
  active_skus: new Set(facts.map((f) => f.nmId)).size, summary,
  controls: {
    price_missing_with_orders: pick('PRICE_MISSING_WITH_ORDERS').map((i) => `${i.day}/${i.nmId}`).sort(),
    cogs_snapshot: { state: cogs.state, published_at: cogs.publishedAt, age_hours: cogs.ageHours, reason: cogs.reason },
    cogs_source_mismatch: pick('COGS_SOURCE_MISMATCH').map((i) => `${i.nmId}: ${i.sourceValue}`),
    cogs_zero_or_missing: pick('COGS_ZERO_OR_MISSING').map((i) => `${i.nmId} ${i.severity}: ${i.sourceValue}`),
    sku_without_block: pick('SKU_WITHOUT_BLOCK').map((i) => i.nmId),
    spp_missing: pick('SPP_MISSING').map((i) => `${i.day}/${i.nmId}`).sort(),
    storage_missing: pick('STORAGE_MISSING').map((i) => `${i.day} ${i.severity}`),
    divergence_by_severity: pick('ORDERS_SOURCE_DIVERGENCE').reduce((a, i) => ((a[i.severity] = (a[i.severity] ?? 0) + 1), a), {}),
    status_without_info: aggregateStatus(issues.filter((i) => i.severity !== 'INFO')),
  },
  issues_non_info: issues.filter((i) => i.severity !== 'INFO'),
};
console.log(JSON.stringify(report, null, 2));
