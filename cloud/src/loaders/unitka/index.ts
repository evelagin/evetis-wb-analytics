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
 *
 * Financial Integrity V1 (UNITKA_RECONCILE_MODE, по умолчанию off = поведение 2.0.0): самовосстанавливающая сверка
 * окна 35 дней через границы месяцев (reconcile.ts). Месяц LCD — как раньше (buildPlan), но факты берутся из слоя
 * сверки (цена с происхождением); прошлые месяцы окна находятся по заголовку (Calendar V2) и получают только
 * факт-ячейки дней окна. Всё уходит ОДНИМ values.batchUpdate; каждая историческая поправка — в журнал ремонта.
 * Сбой прошлой секции (нет секции, контракт, дубль) — отказ С КОДОМ только для неё: месяц LCD пишется как обычно.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { BqClient } from '../../bq/client.js';
import type { QueryRunner } from '../mart/bq.js';
import { UnitkaBq, type EngineRunRecord, type FreshnessRow } from './bq.js';
import { SheetsRest, type SheetsGateway } from './sheets.js';
import { colA1 } from './model.js';
import { discoverSection, nextMonthPrecheck, quoteSheet, readSnapshot, type NextMonthPrecheck, type SectionDiscovery } from './section.js';
import { buildPlan, toWriteRanges, toFormatWrites, cellAt, formulaAt, type Plan, type Snapshot, type PlannedCell } from './plan.js';
import { evaluate, failureCode, qaJson, type QaCheck } from './qa.js';
import { formatMonthKey, geometryAt, locateSection } from './calendar.js';
import { monthStartIso } from './model.js';
import {
  reconcileWindow, windowMonths, buildSectionRepairPlan, evaluateRepairedSection, repairRecords, issueRecords,
  type ReconcileMode, type ReconcileWindow, type SectionRepairPlan,
} from './reconcile.js';
import type { FactRow, RepairRecord } from './bq.js';
import {
  evaluateIntegrity, summarize, classifyCogsSnapshot, parseHhMm, DEFAULT_STORAGE_DUE_MSK, APPROVED_COGS_REFS,
  type IntegrityIssue, type IntegritySummary, type EvaluationPhase, type IntegrityMode,
} from './integrity.js';
import type { CellValue } from './model.js';
import type { Logger } from '../../logging.js';
import type { Config } from '../../config.js';

// 1.2.0 — Integrity Guard V1 (Phase 1C1). При UNITKA_INTEGRITY_MODE=off (по умолчанию) поведение = 1.1.0.
// 2.0.0 — Calendar V2 (Phase 2B): секция месяца по заголовку, любые 28–31 день, слоты блоков (24 — резерв).
// 2.1.0 — Financial Integrity V1: сверка окна 35 дней через границы месяцев, цена с происхождением, журнал ремонта.
//         При UNITKA_RECONCILE_MODE=off (по умолчанию) поведение = 2.0.0.
export const ENGINE_VERSION = 'unitka-engine/2.1.0';

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
  /** Сверка окна: прошлые секции (снимок + дни окна). Задано → факты целостности и канон COGS читаются из слоя сверки. */
  recon?: { sections: ReadonlyArray<{ snap: Snapshot; plan: SectionRepairPlan }> };
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
    const useRecon = a.recon !== undefined;
    const [factsRes, cogsRes] = await Promise.allSettled([a.bq.integrityFacts(t, useRecon), a.bq.cogsCanonical(t, useRecon)]);
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
    const storageDueMinutes = parseHhMm(a.config.unitkaStorageDueMsk ?? DEFAULT_STORAGE_DUE_MSK) ?? parseHhMm(DEFAULT_STORAGE_DUE_MSK)!;
    const allFacts = factsRes.value;
    // Месяц LCD — как в Guard V1; в режиме сверки факты делятся по месяцам, прошлые секции оцениваются за дни окна.
    const lcdMonth = a.plan.monthStart.slice(0, 7);
    const issues = evaluateIntegrity({
      facts: useRecon ? allFacts.filter((f) => f.day.slice(0, 7) === lcdMonth) : allFacts,
      cogs, blocks: a.plan.blocks, lcd: a.plan.lcd, monthStart: a.plan.monthStart,
      firstDailyRow: a.plan.layout.firstDailyRow,
      cellAt: (r, c) => cellAt(a.snap, r, c), formulaAt: (r, c) => formulaAt(a.snap, r, c),
      refValues, now: a.now(), storageDueMinutes, sheetFinal: a.phase === 'POST_WRITE',
    });
    for (const sec of a.recon?.sections ?? []) {
      const mk = sec.plan.monthKey;
      issues.push(...evaluateIntegrity({
        facts: allFacts.filter((f) => f.day >= sec.plan.fromDay && f.day <= sec.plan.toDay),
        cogs, blocks: sec.plan.blocks, lcd: a.plan.lcd, monthStart: `${mk}-01`, firstDailyRow: sec.plan.geometry.firstDailyRow,
        cellAt: (r, c) => cellAt(sec.snap, r, c), formulaAt: (r, c) => formulaAt(sec.snap, r, c),
        refValues, now: a.now(), storageDueMinutes, fromDay: sec.plan.fromDay, toDay: sec.plan.toDay, sheetFinal: a.phase === 'POST_WRITE',
      }));
    }
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
    integrity_status: s.status, mode: s.mode, phase: s.phase, counts: s.counts, states: s.states,
    affected_sku_days: s.affected_sku_days, oldest_unresolved: s.oldest_unresolved, oldest_unresolved_data_error: s.oldest_unresolved_data_error,
    price_provenance: s.price_provenance,
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

/* ───────────────────────── Financial Integrity V1: сверка окна ───────────────────────── */

export interface ReconcileSection { snap: Snapshot; plan: SectionRepairPlan }
export interface ReconcileOutcome {
  mode: ReconcileMode;
  window: ReconcileWindow;
  sections: ReconcileSection[];
  /** Прошлые секции окна, которые не сверялись: код и причина. Месяц LCD от них не зависит. */
  refused: Array<{ month: string; code: string; message: string }>;
}

/**
 * Прошлые месяцы окна сверки: секция по заголовку месяца (Calendar V2) → снимок → план ремонта факт-ячеек дней окна.
 * Любая проблема прошлой секции — отказ с кодом ТОЛЬКО для неё (fail-closed для секции, не для прогона).
 */
async function prepareReconcile(a: {
  sheets: SheetsGateway; sheetName: string; found: SectionDiscovery; window: ReconcileWindow; mode: ReconcileMode;
  facts: readonly FactRow[]; bookLcd: string; lcd: string; log: Logger;
}): Promise<ReconcileOutcome> {
  const out: ReconcileOutcome = { mode: a.mode, window: a.window, sections: [], refused: [] };
  const months = windowMonths(a.window).slice(0, -1); // последний — месяц LCD, его ведёт buildPlan
  for (const key of months) {
    const month = formatMonthKey(key);
    try {
      const loc = locateSection(a.found.columnA, key);
      if (loc.status !== 'FOUND') throw new LoaderError(`секция ${month}: ${loc.status === 'AMBIGUOUS' ? 'заголовок встречается несколько раз' : 'заголовок не найден'}`, loc.status === 'AMBIGUOUS' ? 'RECON_SECTION_AMBIGUOUS' : 'RECON_SECTION_MISSING');
      const g = geometryAt(key, loc.topRow);
      if (g.spacerRow > a.found.meta.rowCount) throw new LoaderError(`секция ${month} не помещается в сетку листа`, 'RECON_SECTION_INVALID');
      const snap = await readSnapshot(a.sheets, a.sheetName, g, a.found.meta.columnCount, a.found.meta.anchorCol);
      out.sections.push({ snap, plan: buildSectionRepairPlan({ snapshot: snap, facts: a.facts, window: a.window, bookLcd: a.bookLcd, lcd: a.lcd }) });
    } catch (e) {
      const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'RECON_SECTION_FAILED');
      out.refused.push({ month, code: err.code, message: err.message.slice(0, 400) });
      a.log.warn('unitka_reconcile_section_refused', { month, code: err.code, message: err.message.slice(0, 400) });
    }
  }
  return out;
}

const cellKey = (c: { row: number; col: number; namedRange?: string }): string => c.namedRange ?? `${c.row}|${c.col}`;

function reconcileSummary(o: ReconcileOutcome, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    mode: o.mode, window: o.window,
    sections: o.sections.map((s) => {
      const byKey: Record<string, number> = {};
      for (const c of s.plan.cells) byKey[c.key ?? ''] = (byKey[c.key ?? ''] ?? 0) + 1;
      return {
        month: s.plan.monthKey, top_row: s.plan.geometry.topRow, from: s.plan.fromDay, to: s.plan.toDay, blocks: s.plan.blocks.length,
        contract_cells: s.plan.expected.length, cells_planned: s.plan.cells.length, by_key: byKey, by_change_type: s.plan.byChangeType,
        format_cells: s.plan.formatCells.length, skipped_blocks: s.plan.skippedBlocks,
        sample: s.plan.cells.slice(0, 20).map((c) => `${c.changeType} ${colA1(c.col)}${c.row} ${c.key ?? ''} ${String(c.before)}→${c.want === null ? '' : c.want} (${c.source ?? ''})`),
      };
    }),
    refused: o.refused, ...extra,
  };
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
  const reconcileMode: ReconcileMode = config.unitkaReconcileMode ?? 'off';
  if (config.unitkaReconcileModeInvalid) {
    log.warn('unitka_reconcile_mode_invalid', { value: config.unitkaReconcileModeInvalid, effective: 'off' });
  }
  let integrity: IntegrityOutcome | null = null;
  let calendar: Record<string, unknown> | null = null;
  let reconcile: ReconcileOutcome | null = null;

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
    const snap = await readSnapshot(sheets, config.unitkaSheetName, found.geometry, found.meta.columnCount, found.meta.anchorCol);

    // 3. факт + ставки → план
    // Слой сверки (observe | write): окно 35 дней, цена с происхождением. Окно считается и в SQL, и в коде —
    // расхождение границ = отказ до записи (две реализации одного правила обязаны совпасть).
    let reconFacts: FactRow[] | null = null;
    let window: ReconcileWindow | null = null;
    if (reconcileMode !== 'off') {
      window = reconcileWindow(lcd.lastClosedDate);
      const w = await bq.reconWindow();
      if (w.lastClosedDate !== lcd.lastClosedDate || w.from !== window.from || w.to !== window.to || w.floor !== window.floor) {
        throw new LoaderError(`окно сверки: вью ${w.from}..${w.to} (LCD ${w.lastClosedDate}, граница ${w.floor}) ≠ коду ${window.from}..${window.to} (LCD ${lcd.lastClosedDate}, граница ${window.floor})`, 'RECON_WINDOW_INCONSISTENT');
      }
      reconFacts = await bq.reconFacts();
    }
    const lcdMonthStart = monthStartIso(lcd.lastClosedDate);
    // Месяц LCD из слоя сверки: только активные SKU — правило BLOCK_MISSING (SKU выбыл посреди месяца) сохраняется.
    const reconLcdFacts = reconFacts?.filter((f) => f.date >= lcdMonthStart && f.skuActive !== false) ?? null;
    const [legacyFacts, logistics, commission] = await Promise.all([
      reconcileMode === 'write' ? Promise.resolve<FactRow[]>([]) : bq.facts(), bq.logisticsRates(), bq.commissionRates(),
    ]);
    const facts = reconcileMode === 'write' ? reconLcdFacts! : legacyFacts;
    rec.rowsRead = (reconFacts?.length ?? 0) + legacyFacts.length + logistics.length + commission.length;
    const plan = buildPlan({
      snapshot: snap, lcd, facts, logistics, commission,
      minN: config.unitkaMinN, maxLagDays: config.unitkaMaxLagDays,
    });
    log.info('unitka_plan', planSummary(plan));

    // 3'. Прошлые месяцы окна сверки. observe: только журнал (что изменил бы write); write: входит в запись.
    let histCells: PlannedCell[] = [];
    let repairs: RepairRecord[] = [];
    if (reconcileMode !== 'off' && window && reconFacts) {
      reconcile = await prepareReconcile({
        sheets, sheetName: config.unitkaSheetName, found, window, mode: reconcileMode, facts: reconFacts,
        bookLcd: plan.bookLcd, lcd: plan.lcd, log,
      });
      histCells = reconcile.sections.flatMap((x) => x.plan.cells);
      const byFact = new Map(reconFacts.map((f) => [`${f.nmId}|${f.date}`, f]));
      // В observe месяц LCD записывается по старому слою; что изменил бы слой сверки — считаем отдельным планом.
      const lcdCellsUnderRecon = reconcileMode === 'write' ? plan.cells
        : buildPlan({ snapshot: snap, lcd, facts: reconLcdFacts!, logistics, commission, minN: config.unitkaMinN, maxLagDays: config.unitkaMaxLagDays }).cells;
      repairs = repairRecords([...lcdCellsUnderRecon, ...histCells], {
        runId, environment: config.environment, engineVersion: ENGINE_VERSION, gitSha: config.gitSha, detectedAt: deps.now().toISOString(),
        repairedAt: null, status: 'PLANNED_NOT_WRITTEN', factOf: (nm, d) => byFact.get(`${nm}|${d}`),
      });
      const have = new Set(plan.cells.map(cellKey));
      log.info('unitka_reconcile_plan', reconcileSummary(reconcile, {
        repairs_planned: repairs.length,
        lcd_month_delta: lcdCellsUnderRecon.filter((c) => !have.has(cellKey(c))).slice(0, 40).map((c) => `${colA1(c.col)}${c.row} ${c.key ?? ''} ${String(c.before)}→${c.want === null ? '' : c.want} (${c.source ?? ''})`),
        repairs_sample: repairs.slice(0, 40).map((r) => `${r.businessDate} ${r.nmId} ${r.field} ${r.cellA1} ${r.oldValue ?? ''}→${r.newValue ?? ''} ${r.reason} [${r.source}]`),
      }));
    }
    const writeHistory = reconcileMode === 'write';
    const allCells: PlannedCell[] = writeHistory ? [...plan.cells, ...histCells] : plan.cells;
    const allFormatCells = writeHistory && reconcile ? [...plan.formatCells, ...reconcile.sections.flatMap((x) => x.plan.formatCells)] : plan.formatCells;
    rec.cellsPlanned = allCells.length;
    const reconArg = reconcile && reconcileMode !== 'off' ? { sections: reconcile.sections } : undefined;

    // 4. SHADOW: QA текущего листа (mismatch = что изменил бы Engine), журнал, выход без записи.
    if (!writeMode) {
      if (integrityMode !== 'off') {
        integrity = await evaluateIntegrityPhase({ bq, sheets, config, snap, plan, phase: 'PRE_WRITE', now: deps.now, log, ...(reconArg ? { recon: reconArg } : {}) });
        publishIntegrity(integrity, { config, log, runId, lcd: plan.lcd });
      }
      const qa = evaluate(snap, plan, { shadow: true });
      // В SHADOW mismatch и LCD — ожидаемая разница (новый день), дефектами считаются остальные.
      const SHADOW_DIFF = new Set(['BQ_SHEETS_MISMATCH', 'LCD_CONSISTENT', 'CLOSED_FORMAT_CONTRACT']);
      const expectedFail = qa.checks.filter((c) => !c.pass && !SHADOW_DIFF.has(c.name));
      rec.qaStatus = expectedFail.length ? 'SHADOW_FAIL' : (allCells.length || allFormatCells.length ? 'SHADOW_DIFF' : 'SHADOW_MATCH');
      rec.qaJson = qaJson(qa, { plan: planSummary(plan), calendar, ...(reconcile ? { reconcile: reconcileSummary(reconcile, { repairs_planned: repairs.length }) } : {}), ...(integrity ? { integrity: integrity.summary } : {}) });
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

    // 4'. PROD: одна запись. В режиме write в неё входят и исторические поправки прошлых месяцев окна.
    // Ремонт без происхождения не выполняется: журнал ремонта обязан быть доступен ДО записи (fail-closed).
    if (writeHistory) await bq.ledgerProbe();
    if (allCells.length === 0) {
      log.info('unitka_nothing_to_write', { lcd: plan.lcd });
    } else {
      const ranges = toWriteRanges(allCells, config.unitkaSheetName);
      const updated = await sheets.batchWrite(ranges);
      rec.cellsWritten = updated;
      log.info('unitka_written', { ranges: ranges.length, cells_planned: allCells.length, cells_lcd_month: plan.cells.length, cells_history: allCells.length - plan.cells.length, cells_updated: updated });
      if (updated !== allCells.length) {
        // Один batchUpdate либо применяется целиком, либо отвергается; расхождение счётчика —
        // сигнал, что контракт API нарушен. Фиксируем и всё равно идём на reconciliation.
        log.warn('unitka_updated_count_mismatch', { planned: allCells.length, updated });
      }
    }

    // 4''. Контракт формата закрытого дня — отдельным spreadsheets.batchUpdate ПОСЛЕ значений.
    // Только строки ≤ LCD; будущее не форматируется. Идемпотентно: при совпадении — 0 запросов.
    if (allFormatCells.length > 0) {
      const writes = toFormatWrites(allFormatCells);
      const applied = await sheets.formatWrite(snap.sheetId, writes);
      log.info('unitka_format_written', { cells: allFormatCells.length, requests: writes.length, applied });
    }

    // 5. reconciliation — повторное чтение и полный QA-гейт (месяц LCD + каждая прошлая секция окна в режиме write).
    const after = await readSnapshot(sheets, config.unitkaSheetName, snap.geometry, snap.width, snap.anchorCol);
    const qaLcd = evaluate(after, plan);
    const reconChecks: QaCheck[] = [];
    const afterSections: ReconcileSection[] = [];
    if (writeHistory && reconcile) {
      for (const sec of reconcile.sections) {
        const again = await readSnapshot(sheets, config.unitkaSheetName, sec.snap.geometry, sec.snap.width, sec.snap.anchorCol);
        afterSections.push({ snap: again, plan: sec.plan });
        reconChecks.push(...evaluateRepairedSection(again, sec.plan));
      }
    }
    const qa = { pass: qaLcd.pass && reconChecks.every((c) => c.pass), checks: [...qaLcd.checks, ...reconChecks] };
    rec.qaStatus = qa.pass ? 'PASS' : 'FAIL';
    // Integrity — ПОСЛЕ записи и reconciliation, на перечитанном листе. Не влияет на qa_status.
    if (integrityMode !== 'off') {
      const reconAfter = reconArg ? { sections: writeHistory ? afterSections : reconcile!.sections } : undefined;
      integrity = await evaluateIntegrityPhase({ bq, sheets, config, snap: after, plan, phase: 'POST_WRITE', now: deps.now, log, ...(reconAfter ? { recon: reconAfter } : {}) });
      publishIntegrity(integrity, { config, log, runId, lcd: plan.lcd });
    }
    // Журнал ремонта — ТОЛЬКО после подтверждённой записи (QA PASS): несостоявшийся ремонт не объявляется состоявшимся.
    let repairsRecorded = 0;
    if (writeHistory && qa.pass && repairs.length > 0) {
      const done = repairs.map((r) => ({ ...r, repairedAt: deps.now().toISOString(), status: 'REPAIRED' as const }));
      // Сначала лог (происхождение не теряется, даже если BigQuery откажет), затем таблица.
      log.info('unitka_repairs', { count: done.length, records: done.slice(0, 200) });
      try {
        repairsRecorded = await bq.insertRepairs(done);
      } catch (e) {
        rec.errorCode = 'LEDGER_WRITE_FAILED';
        rec.errorMessage = `поправки записаны в лист (${done.length}), но журнал ремонта не принял их: ${e instanceof Error ? e.message : String(e)}`;
      }
    }
    // Снимок открытых issue — наблюдаемость (новые / решённые / самая старая). Сбой снимка прогон не роняет.
    if (writeHistory && integrity && !integrity.summary.subsystem_failure) {
      try {
        await bq.insertIssues(issueRecords(integrity.issues, { runId, environment: config.environment, evaluatedAt: integrity.summary.evaluated_at, phase: integrity.summary.phase }));
      } catch (e) {
        log.error('unitka_issue_snapshot_failed', { message: e instanceof Error ? e.message : String(e) });
      }
    }
    rec.qaJson = qaJson(qa, {
      plan: planSummary(plan), calendar, format_cells_written: allFormatCells.length,
      ...(reconcile ? { reconcile: reconcileSummary(reconcile, { repairs_planned: repairs.length, repairs_recorded: repairsRecorded }) } : {}),
      ...(integrity ? { integrity: integrity.summary } : {}),
    });
    log.info('unitka_qa', { pass: qa.pass, checks: qa.checks.map((c) => `${c.name}:${c.pass ? 'PASS' : 'FAIL(' + c.count + ')'}`) });
    if (rec.errorCode === 'LEDGER_WRITE_FAILED') {
      await journal();
      throw new LoaderError(rec.errorMessage ?? 'журнал ремонта недоступен', 'LEDGER_WRITE_FAILED');
    }
    if (!qa.pass) {
      const code = rec.cellsWritten > 0 && rec.cellsWritten !== allCells.length ? 'PARTIAL_WRITE' : failureCode(qa);
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
