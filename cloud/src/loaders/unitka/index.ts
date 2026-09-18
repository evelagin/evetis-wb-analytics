/**
 * UNITKA ENGINE v1 — суточный оркестратор (Stage E1). Загрузчик `unitka` на общем образе.
 *
 * Цикл (16 шагов брифа → 5 фаз):
 *   1. свежесть источников + LAST_CLOSED_DATE       (V_UNITKA_SOURCE_FRESHNESS / V_UNITKA_LAST_CLOSED_DATE)
 *   2. секция месяца LCD (Phase 2B, section.ts) + снимок + preflight (контракт секции, формулы на месте);
 *      предпроверка следующего месяца у конца месяца — только предупреждение
 *   3. факт + ставки → план                        (V_UNITKA_DAILY_FACT / *_RATES; инвариант, дубли, будущее)
 *   4. SHADOW → журнал и выход; PROD → один values.batchUpdate
 *   5. reconciliation: повторное чтение → QA-гейт  (mismatch / формулы / утечка / сводка / LCD)
 *
 * Fail-closed на каждом шаге: LoaderError с кодом → exit 1 → execution FAILED → алерт.
 * Частичной записи нет конструктивно: план уходит одним batchUpdate.
 * Engine не содержит бизнес-SQL и не трогает формулы/УФ/структуру Master. Месяц не создаёт никогда
 * (MONTH_SECTION_MISSING до записи); подготовка месяца — отдельный загрузчик unitka-month-prep.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { BqClient } from '../../bq/client.js';
import type { QueryRunner } from '../mart/bq.js';
import { UnitkaBq, type EngineRunRecord, type FreshnessRow } from './bq.js';
import { SheetsRest, type SheetsGateway } from './sheets.js';
import { colA1 } from './model.js';
import { discoverSection, nextMonthPrecheck, quoteSheet, readSnapshot, type NextMonthPrecheck } from './section.js';
import { buildPlan, toWriteRanges, toFormatWrites, cellAt, formulaAt, type Plan, type Snapshot } from './plan.js';
import { evaluate, failureCode, qaJson } from './qa.js';
import {
  evaluateIntegrity, summarize, classifyCogsSnapshot, parseHhMm, DEFAULT_STORAGE_DUE_MSK, APPROVED_COGS_REFS,
  type IntegrityIssue, type IntegritySummary, type EvaluationPhase, type IntegrityMode,
} from './integrity.js';
import type { CellValue } from './model.js';
import type { Logger } from '../../logging.js';
import type { Config } from '../../config.js';

// 1.2.0 — Integrity Guard V1 (Phase 1C1). При UNITKA_INTEGRITY_MODE=off (по умолчанию) поведение = 1.1.0.
// 2.0.0 — Calendar V2 (Phase 2B): секция месяца по заголовку, любые 28–31 день, слоты блоков (24 — резерв).
export const ENGINE_VERSION = 'unitka-engine/2.0.0';

export interface UnitkaDeps {
  makeRunner: (ctx: LoaderContext) => QueryRunner;
  makeSheets: (ctx: LoaderContext, readonly: boolean) => SheetsGateway;
  now: () => Date;
}

export const defaultUnitkaDeps: UnitkaDeps = {
  makeRunner: (ctx) => new BqClient(ctx.config.projectId, ctx.config.bqLocation),
  makeSheets: (ctx, readonly) => new SheetsRest(ctx.config.unitkaSpreadsheetId, readonly),
  now: () => new Date(),
};

export { readSnapshot } from './section.js';

/* ───────────────────────── Integrity Guard V1 ───────────────────────── */

export interface IntegrityOutcome {
  summary: IntegritySummary;
  issues: IntegrityIssue[];
}

/** Значения разрешённых абсолютных ссылок COGS (например, $R$45) — одно чтение, только для чтения. */
async function readCogsRefs(sheets: SheetsGateway, sheetName: string): Promise<Record<string, CellValue>> {
  const q = quoteSheet(sheetName);
  const vals = await sheets.readValues(APPROVED_COGS_REFS.map((r) => `${q}!${r}`));
  const out: Record<string, CellValue> = {};
  APPROVED_COGS_REFS.forEach((r, i) => { out[r] = vals[i]?.[0]?.[0] ?? null; });
  return out;
}

/** Сбой Guard с явной причиной (для subsystem_failure). */
class IntegrityFailure extends Error {
  constructor(message: string, readonly code: string) { super(message); }
}

/**
 * Оценка целостности. НИКОГДА не бросает: сбой самого Guard превращается в
 * integrity_status = SYSTEM_ERROR + subsystem_failure, факт-писатель не страдает.
 *
 * Бюджет времени (UNITKA_INTEGRITY_BUDGET_MS, 90 с): остаток бюджета уходит в BigQuery как серверный
 * jobTimeoutMs — зависшее задание отменяет сам BigQuery (никаких брошенных промисов с живой работой).
 * Исчерпанный бюджет → SYSTEM_ERROR / INTEGRITY_TIME_BUDGET_EXCEEDED, журнал прогона всё равно пишется.
 * Чтение $R$45 из Sheets ограничено собственным таймаутом шлюза (120 с) и выполняется только при
 * оставшемся бюджете.
 *
 * Копия COGS читается изолированно: её сбой — не SYSTEM_ERROR, а COGS_SNAPSHOT_UNAVAILABLE (WARNING).
 */
export async function evaluateIntegrityPhase(a: {
  bq: UnitkaBq; sheets: SheetsGateway; config: Config; snap: Snapshot; plan: Plan;
  phase: EvaluationPhase; now: () => Date; log: Logger;
}): Promise<IntegrityOutcome> {
  const mode: IntegrityMode = a.config.unitkaIntegrityMode ?? 'off';
  const budget = a.config.unitkaIntegrityBudgetMs ?? 90_000;
  const deadline = a.now().getTime() + budget;
  const remaining = (): number => deadline - a.now().getTime();
  const timeLeft = (): number => {
    const r = remaining();
    if (r <= 0) throw new IntegrityFailure(`бюджет Guard ${budget} мс исчерпан`, 'INTEGRITY_TIME_BUDGET_EXCEEDED');
    return r;
  };
  const unavailable = { state: 'UNAVAILABLE' as const, publishedAt: null, ageHours: null };
  try {
    const t = timeLeft();
    const [factsRes, cogsRes] = await Promise.allSettled([a.bq.integrityFacts(t), a.bq.cogsCanonical(t)]);
    if (factsRes.status === 'rejected') {
      if (remaining() <= 0) throw new IntegrityFailure(`V_UNITKA_INTEGRITY: бюджет ${budget} мс исчерпан (${String(factsRes.reason)})`, 'INTEGRITY_TIME_BUDGET_EXCEEDED');
      throw factsRes.reason;
    }
    const cogs = classifyCogsSnapshot(
      cogsRes.status === 'fulfilled' ? cogsRes.value : { error: cogsRes.reason instanceof Error ? cogsRes.reason.message : String(cogsRes.reason) },
      a.now(),
    );
    if (cogs.state !== 'AVAILABLE') a.log.warn('unitka_integrity_cogs_snapshot', { state: cogs.state, reason: cogs.reason, published_at: cogs.publishedAt });
    timeLeft();
    const refValues = await readCogsRefs(a.sheets, a.config.unitkaSheetName);
    timeLeft();
    const issues = evaluateIntegrity({
      facts: factsRes.value, cogs, blocks: a.plan.blocks, lcd: a.plan.lcd, monthStart: a.plan.monthStart,
      firstDailyRow: a.plan.layout.firstDailyRow,
      cellAt: (r, c) => cellAt(a.snap, r, c), formulaAt: (r, c) => formulaAt(a.snap, r, c),
      refValues, now: a.now(),
      storageDueMinutes: parseHhMm(a.config.unitkaStorageDueMsk ?? DEFAULT_STORAGE_DUE_MSK) ?? parseHhMm(DEFAULT_STORAGE_DUE_MSK)!,
    });
    return { issues, summary: summarize(issues, mode, a.phase, a.now(), cogs) };
  } catch (e) {
    const failure = {
      code: e instanceof IntegrityFailure || e instanceof LoaderError ? e.code : 'INTEGRITY_SUBSYSTEM_FAILURE',
      message: e instanceof Error ? e.message : String(e),
    };
    a.log.error('unitka_integrity_failed', failure);
    return { issues: [], summary: summarize([], mode, a.phase, a.now(), unavailable, failure) };
  }
}

/** Лог-событие для будущего алерта (контракт Phase 1B §13). Журнал issue отложен (решение D4). */
function publishIntegrity(
  o: IntegrityOutcome, a: { config: Config; log: Logger; runId: string; lcd: string },
): void {
  const s = o.summary;
  const payload = {
    run_id: a.runId, marketplace: 'WB', environment: a.config.environment, last_closed_date: a.lcd,
    integrity_status: s.status, mode: s.mode, phase: s.phase, counts: s.counts,
    financially_invalid_rows: s.financially_invalid_rows, issue_codes: s.issue_codes,
    error_keys: s.error_keys.slice(0, 20), cogs_source: s.cogs_source, cogs_published_at: s.cogs_published_at,
    spreadsheet_id: a.config.unitkaSpreadsheetId, sheet: a.config.unitkaSheetName,
    ...(s.subsystem_failure ? { subsystem_failure: s.subsystem_failure } : {}),
  };
  if (s.status === 'DATA_ERROR' || s.status === 'SYSTEM_ERROR') a.log.warn('unitka_integrity', payload);
  else a.log.info('unitka_integrity', payload);
}

/**
 * enforce (зарезервирован): ошибка ТОЛЬКО при сбое самого Guard, никогда из-за DATA_ERROR.
 * Возвращает ошибку, а не бросает: вызывающий ставит error_code ДО единственной записи журнала.
 */
function enforceGate(o: IntegrityOutcome | null, mode: IntegrityMode): LoaderError | null {
  if (mode === 'enforce' && o?.summary.subsystem_failure) {
    return new LoaderError(`Integrity Guard недоступен (enforce): ${o.summary.subsystem_failure.message}`, 'INTEGRITY_SUBSYSTEM_FAILURE');
  }
  return null;
}

function freshnessJson(rows: FreshnessRow[]): string {
  return JSON.stringify(rows.map((r) => ({ source: r.source, max_closed_date: r.maxClosedDate, gating: r.gating, observed_at: r.observedAt })));
}

function planSummary(plan: Plan): Record<string, unknown> {
  const byKind: Record<string, number> = {};
  for (const c of plan.cells) byKind[c.kind] = (byKind[c.kind] ?? 0) + 1;
  return {
    lcd: plan.lcd, lag_days: plan.lagDays, closed_days: plan.closedDays, blocks: plan.blocks.length,
    section: { month: plan.layout.monthKey, top_row: plan.layout.topRow, first_row: plan.layout.firstDailyRow, last_row: plan.layout.lastDailyRow, mtd_row: plan.layout.mtdRow, days: plan.layout.daysInMonth, slots: plan.blocks.map((b) => b.slot) },
    cells_planned: plan.cells.length, by_kind: byKind,
    format_cells_planned: plan.formatCells.length, format_contract_cells: plan.formatContractCells,
    format_sample: plan.formatCells.slice(0, 20).map((c) => `${colA1(c.col)}${c.row} ${c.key}`),
    invariant: plan.invariant, reverse_rate: plan.reverseRate,
    own_direct: plan.rates.filter((r) => r.directSource === 'own').length,
    own_commission: plan.rates.filter((r) => r.commissionSource === 'own').length,
    book_lcd: plan.bookLcd, by_change_type: plan.byChangeType, stock_projection_future: plan.stockProjectionCells,
    gaps: plan.gaps, legacy_formulas: plan.legacy, legacy_replaced: plan.legacyReplaced, sources_by_day: plan.sourcesByDay,
    sample: plan.cells.slice(0, 40).map((c) => `${c.changeType} ${c.namedRange ?? colA1(c.col) + c.row} ${c.key ?? ''} ${String(c.before)}→${c.want === null ? '' : c.want}`),
  };
}

/** Добавить integrity в уже собранный qa_json, не ломая остальные поля. */
function withIntegrity(qaJsonStr: string, summary: IntegritySummary): string {
  let o: Record<string, unknown> = {};
  try { o = JSON.parse(qaJsonStr) as Record<string, unknown>; } catch { o = {}; }
  return JSON.stringify({ ...o, integrity: summary });
}

export async function unitkaLoader(ctx: LoaderContext, deps: UnitkaDeps = defaultUnitkaDeps): Promise<LoaderResult> {
  const { config, logger, runId } = ctx;
  const startedAt = deps.now();
  const writeMode = config.environment === 'prod' && config.unitkaWriteEnabled;
  const mode: EngineRunRecord['mode'] = writeMode ? 'WRITE' : 'SHADOW';
  const log = logger.child({ engine: ENGINE_VERSION, mode });

  const bq = new UnitkaBq(deps.makeRunner(ctx), config.unitkaMartDataset, config.unitkaOpsDataset, config.unitkaRunsTable);
  const sheets = deps.makeSheets(ctx, !writeMode);

  const integrityMode: IntegrityMode = config.unitkaIntegrityMode ?? 'off';
  if (config.unitkaIntegrityModeInvalid) {
    log.warn('unitka_integrity_mode_invalid', { value: config.unitkaIntegrityModeInvalid, effective: 'off' });
  }
  let integrity: IntegrityOutcome | null = null;
  let calendar: Record<string, unknown> | null = null;

  const rec: EngineRunRecord = {
    runId, environment: config.environment, mode,
    startedAtIso: startedAt.toISOString(), completedAtIso: startedAt.toISOString(),
    lastClosedDate: null, sourceFreshnessJson: '[]', rowsRead: 0, cellsPlanned: 0, cellsWritten: 0,
    qaStatus: 'NOT_RUN', qaJson: '{}', errorCode: null, errorMessage: null,
    gitSha: config.gitSha, imageDigest: config.imageDigest, engineVersion: ENGINE_VERSION,
  };

  const journal = async (): Promise<void> => {
    rec.completedAtIso = deps.now().toISOString();
    try {
      await bq.insertRun(rec);
    } catch (e) {
      // Журнал не должен маскировать исход прогона; ошибку журнала видно в логах.
      log.error('unitka_journal_failed', { message: e instanceof Error ? e.message : String(e) });
    }
  };

  try {
    // 1. свежесть и LAST_CLOSED_DATE
    const fresh = await bq.freshness();
    rec.sourceFreshnessJson = freshnessJson(fresh);
    const lcd = await bq.lastClosedDate();
    rec.lastClosedDate = lcd.lastClosedDate;
    log.info('unitka_sources', { lcd: lcd.lastClosedDate, d1_msk: lcd.d1Msk, freshness: fresh });

    // 2. секция месяца LCD (MONTH_SECTION_MISSING/AMBIGUOUS/INVALID — до любой записи) и снимок;
    //    preflight (контракт секции + формулы) выполняется внутри buildPlan до любых вычислений.
    const found = await discoverSection(sheets, config.unitkaSheetName, lcd.lastClosedDate);
    const precheck: NextMonthPrecheck = nextMonthPrecheck(found.columnA, lcd.lastClosedDate, config.unitkaMonthPrepWindowDays ?? 5);
    calendar = { section: found.geometry.monthKey, top_row: found.geometry.topRow, next_month: precheck };
    if (precheck.code) log.warn('unitka_next_month_section', { code: precheck.code, next_month: precheck.nextMonth, days_left: precheck.daysLeft });
    const snap = await readSnapshot(sheets, config.unitkaSheetName, found.geometry, found.meta.columnCount);

    // 3. факт + ставки → план
    const [facts, logistics, commission] = await Promise.all([bq.facts(), bq.logisticsRates(), bq.commissionRates()]);
    rec.rowsRead = facts.length + logistics.length + commission.length;
    const plan = buildPlan({
      snapshot: snap, lcd, facts, logistics, commission,
      minN: config.unitkaMinN, maxLagDays: config.unitkaMaxLagDays,
    });
    rec.cellsPlanned = plan.cells.length;
    log.info('unitka_plan', planSummary(plan));

    // 4. SHADOW: QA текущего листа (mismatch = что изменил бы Engine), журнал, выход без записи.
    if (!writeMode) {
      if (integrityMode !== 'off') {
        integrity = await evaluateIntegrityPhase({ bq, sheets, config, snap, plan, phase: 'PRE_WRITE', now: deps.now, log });
        publishIntegrity(integrity, { config, log, runId, lcd: plan.lcd });
      }
      const qa = evaluate(snap, plan, { shadow: true });
      // В SHADOW mismatch и LCD — ожидаемая разница (новый день), дефектами считаются остальные.
      const SHADOW_DIFF = new Set(['BQ_SHEETS_MISMATCH', 'LCD_CONSISTENT', 'CLOSED_FORMAT_CONTRACT']);
      const expectedFail = qa.checks.filter((c) => !c.pass && !SHADOW_DIFF.has(c.name));
      rec.qaStatus = expectedFail.length ? 'SHADOW_FAIL' : (plan.cells.length || plan.formatCells.length ? 'SHADOW_DIFF' : 'SHADOW_MATCH');
      rec.qaJson = qaJson(qa, { plan: planSummary(plan), calendar, ...(integrity ? { integrity: integrity.summary } : {}) });
      log.info('unitka_shadow', { qa_status: rec.qaStatus, cells_planned: plan.cells.length, checks: qa.checks.map((c) => `${c.name}:${c.pass ? 'PASS' : 'FAIL(' + c.count + ')'}`) });
      if (expectedFail.length) {
        rec.errorCode = failureCode({ pass: false, checks: expectedFail });
        rec.errorMessage = expectedFail.map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ');
        await journal();
        throw new LoaderError(`SHADOW: Master не проходит QA — ${rec.errorMessage}`, rec.errorCode);
      }
      const gate = enforceGate(integrity, integrityMode);
      if (gate) { rec.errorCode = gate.code; rec.errorMessage = gate.message; }
      await journal();
      if (gate) throw gate;
      return { rowsFetched: rec.rowsRead, rowsLoaded: 0 };
    }

    // 4'. PROD: одна запись.
    if (plan.cells.length === 0) {
      log.info('unitka_nothing_to_write', { lcd: plan.lcd });
    } else {
      const ranges = toWriteRanges(plan.cells, config.unitkaSheetName);
      const updated = await sheets.batchWrite(ranges);
      rec.cellsWritten = updated;
      log.info('unitka_written', { ranges: ranges.length, cells_planned: plan.cells.length, cells_updated: updated });
      if (updated !== plan.cells.length) {
        // Один batchUpdate либо применяется целиком, либо отвергается; расхождение счётчика —
        // сигнал, что контракт API нарушен. Фиксируем и всё равно идём на reconciliation.
        log.warn('unitka_updated_count_mismatch', { planned: plan.cells.length, updated });
      }
    }

    // 4''. Контракт формата закрытого дня — отдельным spreadsheets.batchUpdate ПОСЛЕ значений.
    // Только строки ≤ LCD; будущее не форматируется. Идемпотентно: при совпадении — 0 запросов.
    if (plan.formatCells.length > 0) {
      const writes = toFormatWrites(plan.formatCells);
      const applied = await sheets.formatWrite(snap.sheetId, writes);
      log.info('unitka_format_written', { cells: plan.formatCells.length, requests: writes.length, applied });
    }

    // 5. reconciliation — повторное чтение и полный QA-гейт.
    const after = await readSnapshot(sheets, config.unitkaSheetName, snap.geometry, snap.width);
    const qa = evaluate(after, plan);
    rec.qaStatus = qa.pass ? 'PASS' : 'FAIL';
    // Integrity — ПОСЛЕ записи и reconciliation, на перечитанном листе. Не влияет на qa_status.
    if (integrityMode !== 'off') {
      integrity = await evaluateIntegrityPhase({ bq, sheets, config, snap: after, plan, phase: 'POST_WRITE', now: deps.now, log });
      publishIntegrity(integrity, { config, log, runId, lcd: plan.lcd });
    }
    rec.qaJson = qaJson(qa, {
      plan: planSummary(plan), calendar, format_cells_written: plan.formatCells.length,
      ...(integrity ? { integrity: integrity.summary } : {}),
    });
    log.info('unitka_qa', { pass: qa.pass, checks: qa.checks.map((c) => `${c.name}:${c.pass ? 'PASS' : 'FAIL(' + c.count + ')'}`) });
    if (!qa.pass) {
      const code = rec.cellsWritten > 0 && rec.cellsWritten !== plan.cells.length ? 'PARTIAL_WRITE' : failureCode(qa);
      rec.errorCode = code;
      rec.errorMessage = qa.checks.filter((c) => !c.pass).map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ');
      await journal();
      throw new LoaderError(`QA после записи: ${rec.errorMessage}`, code);
    }
    const gate = enforceGate(integrity, integrityMode);
    if (gate) { rec.errorCode = gate.code; rec.errorMessage = gate.message; }
    await journal();
    if (gate) throw gate;
    return { rowsFetched: rec.rowsRead, rowsLoaded: rec.cellsWritten };
  } catch (e) {
    if (rec.errorCode === null) {
      // Сбой Engine до оценки целостности: integrity_status = SYSTEM_ERROR (если Guard включён).
      if (integrityMode !== 'off' && integrity === null) {
        rec.qaJson = withIntegrity(rec.qaJson, summarize([], integrityMode, writeMode ? 'POST_WRITE' : 'PRE_WRITE', deps.now(), { state: 'UNAVAILABLE', publishedAt: null, ageHours: null }, {
          code: e instanceof LoaderError ? e.code : 'ENGINE_ERROR', message: 'Engine упал до оценки целостности',
        }));
      }
      const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'ENGINE_ERROR');
      rec.errorCode = err.code;
      rec.errorMessage = err.message;
      if (rec.qaStatus === 'NOT_RUN') rec.qaStatus = 'FAIL';
      await journal();
      throw err;
    }
    throw e;
  }
}
