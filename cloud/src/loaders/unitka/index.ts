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
  reconcileWindow, windowMonths, assertNotBeforeEpoch, buildSectionRepairPlan, evaluateRepairedSection, repairRecords, issueRecords,
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
import { commitLcd, revertLcd, type CommitOutcome } from './lcd.js';
import { resolveWbCandidate, ensureWbMonthSection, WbLcdCell, lifecycleLog, type WbCandidate, type MonthEnsureResult } from './wb_lifecycle.js';
import { asNumber, isoToSerial } from './model.js';
import { planSpp, sppWindow, sppWriteRanges, sppClearRanges, buildSppManifest, encodeSppManifest, verifySppReadback, sppSummary, type SppPlan, type SppMode } from './spp.js';
import { monthKeyOf, nextMonth } from './calendar.js';

// 1.2.0 — Integrity Guard V1 (Phase 1C1). При UNITKA_INTEGRITY_MODE=off (по умолчанию) поведение = 1.1.0.
// 2.0.0 — Calendar V2 (Phase 2B): секция месяца по заголовку, любые 28–31 день, слоты блоков (24 — резерв).
// 2.1.0 — Financial Integrity V1: сверка окна 35 дней через границы месяцев, цена с происхождением, журнал ремонта.
// 2.2.0 — SPP-3: колонка AB из wb_mart.V_WB_SPP_DAILY (UNITKA_SPP_MODE=off|observe|write, по умолчанию off).
//         При UNITKA_RECONCILE_MODE=off (по умолчанию) поведение = 2.0.0.
export const ENGINE_VERSION = 'unitka-engine/2.2.0';

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
  /** PRE_WRITE: ячейки, которые прогон собирается записать (для PRICE_NOT_ON_SHEET). После записи не задаётся. */
  pendingWrite?: (row: number, col: number) => boolean;
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
      refValues, now: a.now(), storageDueMinutes, checkSheetCells: true, ...(a.pendingWrite ? { pendingWrite: a.pendingWrite } : {}),
    });
    for (const sec of a.recon?.sections ?? []) {
      const mk = sec.plan.monthKey;
      issues.push(...evaluateIntegrity({
        facts: allFacts.filter((f) => f.day >= sec.plan.fromDay && f.day <= sec.plan.toDay),
        cogs, blocks: sec.plan.blocks, lcd: a.plan.lcd, monthStart: `${mk}-01`, firstDailyRow: sec.plan.geometry.firstDailyRow,
        cellAt: (r, c) => cellAt(sec.snap, r, c), formulaAt: (r, c) => formulaAt(sec.snap, r, c),
        refValues, now: a.now(), storageDueMinutes, fromDay: sec.plan.fromDay, toDay: sec.plan.toDay, checkSheetCells: true, ...(a.pendingWrite ? { pendingWrite: a.pendingWrite } : {}),
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
    affected_sku_days: s.affected_sku_days, repair_available_sku_days: s.repair_available_sku_days, oldest_unresolved: s.oldest_unresolved, oldest_unresolved_data_error: s.oldest_unresolved_data_error,
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
  // ── ТРИ НЕЗАВИСИМЫЕ СПОСОБНОСТИ ПРОГОНА (Rollout Gate 4.1) ──────────────────────────────────
  // Раньше их заменял один флаг writeMode, поэтому контролируемый production-observe с
  // UNITKA_WRITE_ENABLED=0 уходил в ветку SHADOW и физически не мог сохранить снимок наблюдаемости.
  //   SHEET_BUSINESS_WRITE — обычная суточная запись фактов в книгу (и выбор scope Sheets);
  //   OBSERVABILITY_WRITE  — снимок issue в wb_ops (состояние наблюдаемости, НЕ ремонт);
  //   REPAIR_EXECUTION     — исторические поправки сверки и журнал ремонта.
  // Среду различает канонический сигнал config.environment ('prod' | 'shadow', проверяется в config.ts):
  // настоящая shadow-среда снимок не пишет НАМЕРЕННО, а не потому, что у её SA нет прав (IAM — вторая линия).
  const sheetBusinessWriteAllowed = config.environment === 'prod' && config.unitkaWriteEnabled;
  const writeMode = sheetBusinessWriteAllowed;
  const mode: EngineRunRecord['mode'] = writeMode ? 'WRITE' : 'SHADOW';
  const log = logger.child({ engine: ENGINE_VERSION, mode });

  const bq = new UnitkaBq(deps.makeRunner(ctx), config.unitkaMartDataset, config.unitkaOpsDataset, config.unitkaRunsTable);
  const sheets = deps.makeSheets(ctx, !writeMode);

  const integrityMode: IntegrityMode = config.unitkaIntegrityMode ?? 'off';
  if (config.unitkaIntegrityModeInvalid) {
    log.warn('unitka_integrity_mode_invalid', { value: config.unitkaIntegrityModeInvalid, effective: 'off' });
  }
  const reconcileMode: ReconcileMode = config.unitkaReconcileMode ?? 'off';
  // SPP-3: AB (СПП) из wb_mart.V_WB_SPP_DAILY. off — не читается; observe — план и манифест отката в журнал;
  // write — AB входит в ту же единственную запись цикла, проверяется перечитыванием ДО коммита LCD.
  const sppMode: SppMode = config.unitkaSppMode ?? 'off';
  if (config.unitkaSppModeInvalid) log.warn('unitka_spp_mode_invalid', { value: config.unitkaSppModeInvalid, effective: 'off' });
  if (config.unitkaReconcileModeInvalid) {
    log.warn('unitka_reconcile_mode_invalid', { value: config.unitkaReconcileModeInvalid, effective: 'off' });
  }
  // Снимок наблюдаемости не зависит от права писать в книгу: он разрешён в production в любом режиме
  // сверки, кроме off, — и запрещён в настоящей shadow-среде.
  const observabilityWriteAllowed = config.environment === 'prod' && reconcileMode !== 'off';
  let integrity: IntegrityOutcome | null = null;
  let calendar: Record<string, unknown> | null = null;
  let reconcile: ReconcileOutcome | null = null;
  // Стадия записи листа — чтобы журнал ремонта никогда не объявил состоявшимся то, что не подтверждено:
  // NOT_ATTEMPTED → ATTEMPTED (вызов ушёл) → ACKNOWLEDGED (лист ответил успехом) → VERIFIED (перечитано, QA PASS).
  let writeStage: 'NOT_ATTEMPTED' | 'ATTEMPTED' | 'ACKNOWLEDGED' | 'VERIFIED' = 'NOT_ATTEMPTED';
  let pendingRepairs: RepairRecord[] = [];
  let attemptRecorded = false;
  // AUTO-LCD (Gate 10): три разных LCD — закоммиченный, кандидат, итог по перечитыванию.
  let life: WbCandidate | null = null;
  let monthEnsure: MonthEnsureResult | null = null;
  let commit: CommitOutcome | null = null;
  let reverted: string | null = null;
  let spp: SppPlan | null = null;
  let sppManifestDigest: string | null = null;
  const sppSnaps = new Map<string, Snapshot>();
  const sppJson = (written: number): Record<string, unknown> => (spp ? { spp: { mode: sppMode, window: spp.window, manifest_digest: sppManifestDigest, written, ...spp.counts } } : {});
  const lifecycleSummary = (): Record<string, unknown> => ({
    mode: life?.mode ?? null, committed_before: life?.committed ?? null, candidate: life?.candidate ?? null,
    canonical_ceiling: life?.ceiling ?? null, gap_at: life?.decision.gapAt ?? null, clamped_from: life?.clampedToMonth ?? null,
    lifecycle_block: life?.lifecycle ?? null, notes: life?.decision.notes ?? [],
    month: monthEnsure, commit: commit ? { code: commit.code, book_after: commit.bookAfter, message: commit.message } : 'NOT_ATTEMPTED',
    reverted,
  });

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

  /**
   * Попытка ремонта без подтверждения: запись листа упала (WRITE_FAILED) либо прошла, но не проверена
   * (APPLIED_UNVERIFIED). Никогда не REPAIRED. Best-effort: сбой журнала исход прогона не маскирует; записи уже в логе.
   */
  const recordUnconfirmed = async (why: string): Promise<void> => {
    if (attemptRecorded || pendingRepairs.length === 0 || writeStage === 'NOT_ATTEMPTED' || writeStage === 'VERIFIED') return;
    attemptRecorded = true;
    const status = writeStage === 'ATTEMPTED' ? 'WRITE_FAILED' as const : 'APPLIED_UNVERIFIED' as const;
    const rows = pendingRepairs.map((r) => ({ ...r, repairedAt: null, status, reason: `${r.reason}; ${status}: ${why.slice(0, 160)}` }));
    log.warn('unitka_repairs_unconfirmed', { status, count: rows.length, records: rows.slice(0, 200) });
    try {
      await bq.insertRepairs(rows);
    } catch (e) {
      log.error('unitka_repairs_unconfirmed_not_recorded', { message: e instanceof Error ? e.message : String(e) });
    }
  };

  try {
    // 1. свежесть и LAST_CLOSED_DATE
    const fresh = await bq.freshness();
    rec.sourceFreshnessJson = freshnessJson(fresh);
    // Канон V_UNITKA_LAST_CLOSED_DATE — теперь ПОТОЛОК, а не LCD: день закрывает только завершённый цикл.
    const canonical = await bq.lastClosedDate();
    life = await resolveWbCandidate({ sheets, bq, config, log, canonical });
    const lcd = { lastClosedDate: life.candidate, d1Msk: canonical.d1Msk };
    rec.lastClosedDate = lcd.lastClosedDate;
    log.info('unitka_sources', { lcd: lcd.lastClosedDate, committed_lcd: life.committed, canonical_lcd: canonical.lastClosedDate, d1_msk: lcd.d1Msk, freshness: fresh });

    // 1'. Секция месяца кандидата: есть — или создаётся тем же генератором, что у unitka-month-prep.
    //     Провал — исключение ДО записи данных: LCD не двигается.
    monthEnsure = await ensureWbMonthSection({ sheets, bq, config, log, candidate: lcd.lastClosedDate, committed: life.committed, write: writeMode, now: deps.now });

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
      // Две реализации ОДНОГО правила окна обязаны совпасть. SQL якорит окно на КАНОНИЧЕСКОМ LCD, поэтому
      // и сверяем правило на нём. Окно ЭТОГО прогона якорится на КАНДИДАТЕ (Gate 10): если барьер
      // придержал день, факты после кандидата в окно не входят, а начало окна не раньше того, что отдала вью.
      const ruleWindow = reconcileWindow(canonical.lastClosedDate);
      const w = await bq.reconWindow();
      if (w.lastClosedDate !== canonical.lastClosedDate || w.from !== ruleWindow.from || w.to !== ruleWindow.to || w.epoch !== ruleWindow.epoch || w.days !== ruleWindow.days) {
        throw new LoaderError(`окно сверки: вью ${w.from}..${w.to} (LCD ${w.lastClosedDate}, эпоха ${w.epoch}, ${w.days} дн.) ≠ коду ${ruleWindow.from}..${ruleWindow.to} (LCD ${canonical.lastClosedDate}, эпоха ${ruleWindow.epoch}, ${ruleWindow.days} дн.)`, 'RECON_WINDOW_INCONSISTENT');
      }
      window = reconcileWindow(lcd.lastClosedDate);
      const effFrom = window.from > w.from ? window.from : w.from;
      const effTo = window.to;
      // Эпоха сверки — вторая линия защиты: строка источника раньше эпохи = отказ до любой записи.
      // Проверяется на СЫРЫХ строках вью, ДО фильтра окна: иначе фильтр молча выбросил бы именно те
      // строки, по которым видна ошибка вью, и защита перестала бы срабатывать.
      const rawRecon = await bq.reconFacts();
      assertNotBeforeEpoch(rawRecon.map((f) => f.date), 'слой сверки');
      reconFacts = rawRecon.filter((f) => f.date >= effFrom && f.date <= effTo);
    }
    const lcdMonthStart = monthStartIso(lcd.lastClosedDate);
    // Месяц LCD из слоя сверки: только активные SKU — правило BLOCK_MISSING (SKU выбыл посреди месяца) сохраняется.
    const reconLcdFacts = reconFacts?.filter((f) => f.date >= lcdMonthStart && f.skuActive !== false) ?? null;
    const [legacyFacts, logistics, commission] = await Promise.all([
      reconcileMode === 'write' ? Promise.resolve<FactRow[]>([]) : bq.facts(), bq.logisticsRates(), bq.commissionRates(),
    ]);
    // Суточная вью фактов якорится в SQL на КАНОНИЧЕСКОМ LCD. Её прежний инвариант «ни одной даты позже
    // LCD» проверяется против канона (сама вью не должна забегать вперёд), а затем факты обрезаются по
    // КАНДИДАТУ: если барьер придержал день, более поздние сутки этим прогоном не публикуются.
    const beyond = legacyFacts.find((f) => f.date > canonical.lastClosedDate);
    if (beyond) throw new LoaderError(`V_UNITKA_DAILY_FACT отдал дату ${beyond.date} > LAST_CLOSED_DATE ${canonical.lastClosedDate}`, 'FUTURE_LEAKAGE');
    const legacyAtCandidate = legacyFacts.filter((f) => f.date <= lcd.lastClosedDate);
    const facts = reconcileMode === 'write' ? reconLcdFacts! : legacyAtCandidate;
    rec.rowsRead = (reconFacts?.length ?? 0) + legacyFacts.length + logistics.length + commission.length;
    const plan = buildPlan({
      snapshot: snap, lcd, facts, logistics, commission,
      // SOURCE_STALE — свойство ИСТОЧНИКОВ, и оно уже проверено на каноне (resolveWbCandidate). Кандидат,
      // придержанный барьером или владельцем (MANUAL), — не устаревший источник: план его не отвергает.
      minN: config.unitkaMinN, maxLagDays: Number.MAX_SAFE_INTEGER, deferLcdCommit: true,
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
      assertNotBeforeEpoch(histCells.map((c) => c.date), 'план сверки прошлых секций');
      const byFact = new Map(reconFacts.map((f) => [`${f.nmId}|${f.date}`, f]));
      // В observe месяц LCD записывается по старому слою; что изменил бы слой сверки — считаем отдельным планом.
      const lcdCellsUnderRecon = reconcileMode === 'write' ? plan.cells
        : buildPlan({ snapshot: snap, lcd, facts: reconLcdFacts!, logistics, commission, minN: config.unitkaMinN, maxLagDays: Number.MAX_SAFE_INTEGER, deferLcdCommit: true }).cells;
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
    // 3''. SPP-3: план AB по секциям окна (35 дней от кандидата, не раньше 01.09.2026). Снимки секций — до записи.
    // observe не меняет исход прогона фактов: сбой плана AB — предупреждение, факты и LCD идут как без SPP.
    // write — fail-closed: без плана AB прогон падает ДО любой записи (факты без AB не пишутся).
    if (sppMode !== 'off') {
      const sw = sppWindow(lcd.lastClosedDate);
      if (sw) {
        try {
          sppSnaps.set(snap.geometry.monthKey, snap);
          for (let k = monthKeyOf(sw.from); formatMonthKey(k) <= formatMonthKey(monthKeyOf(sw.to)); k = nextMonth(k)) {
            const key = formatMonthKey(k);
            if (sppSnaps.has(key)) continue;
            const sec = await discoverSection(sheets, config.unitkaSheetName, `${key}-01`);
            sppSnaps.set(key, await readSnapshot(sheets, config.unitkaSheetName, sec.geometry, sec.meta.columnCount, sec.meta.anchorCol));
          }
          spp = planSpp({ sections: [...sppSnaps.values()], rows: await bq.sppDaily(sw.from, sw.to), candidate: lcd.lastClosedDate });
          const manifest = buildSppManifest(spp, { spreadsheetId: config.unitkaSpreadsheetId, sheetId: snap.sheetId, sheetName: config.unitkaSheetName });
          sppManifestDigest = manifest.digest;
          log.info('unitka_spp_plan', sppSummary(spp, sppMode, { manifest_digest: manifest.digest }));
          // Снимок отката — ДО любой записи, в каждом прогоне с изменениями (как манифест подготовки месяца).
          if (spp.cells.length) log.info('unitka_spp_undo_manifest', { digest: manifest.digest, cells: manifest.cells.length, manifest_b64: encodeSppManifest(manifest) });
        } catch (e) {
          if (sppMode === 'write') throw e;
          spp = null;
          log.warn('unitka_spp_plan_failed', { mode: sppMode, code: (e as { code?: string }).code ?? null, error: (e as Error).message });
        }
      }
    }
    const sppWrite = sppMode === 'write' && writeMode && spp !== null && spp.cells.length > 0;

    // Плановое накопление (какие ячейки входят в контракт записи) — writeHistory;
    // фактическое ИСПОЛНЕНИЕ ремонта (проба журнала, записи REPAIRED) — REPAIR_EXECUTION:
    // оно требует ещё и права записи в книгу, поэтому в контролируемом observe невозможно.
    const writeHistory = reconcileMode === 'write';
    const repairExecutionAllowed = sheetBusinessWriteAllowed && writeHistory;
    if (repairExecutionAllowed) pendingRepairs = repairs;
    const allCells: PlannedCell[] = writeHistory ? [...plan.cells, ...histCells] : plan.cells;
    const allFormatCells = writeHistory && reconcile ? [...plan.formatCells, ...reconcile.sections.flatMap((x) => x.plan.formatCells)] : plan.formatCells;
    rec.cellsPlanned = allCells.length;
    const reconArg = reconcile && reconcileMode !== 'off' ? { sections: reconcile.sections } : undefined;

    // Снимок issue — СОСТОЯНИЕ НАБЛЮДАЕМОСТИ, а не ремонт: одна и та же запись в обычном прогоне и в
    // контролируемом observe с отключённой записью в книгу. Условие ровно одно — OBSERVABILITY_WRITE.
    // Маркер прогона пишется ПОСЛЕДНИМ (bq.insertIssues): без него вью статуса оценку завершённой не считает.
    // Сбой снимка прогон не роняет — статус честно станет STALE, когда истечёт порог свежести.
    let issueSnapshot: 'PERSISTED' | 'FAILED' | 'SKIPPED_GUARD_OFF' | 'SKIPPED_GUARD_FAILED' | 'SKIPPED_NOT_PRODUCTION' | 'OFF' = 'OFF';
    const persistIssueSnapshot = async (): Promise<void> => {
      if (reconcileMode === 'off') return;                       // режим off снимок не пишет никогда
      if (!observabilityWriteAllowed) {
        // Настоящая shadow-среда: подавляем запись НАМЕРЕННО, не полагаясь на отказ IAM.
        issueSnapshot = 'SKIPPED_NOT_PRODUCTION';
        log.info('unitka_issue_snapshot_skipped', { reason: `environment=${config.environment}: снимок наблюдаемости пишет только production`, reconcile_mode: reconcileMode });
        return;
      }
      if (!integrity) {
        // Оценку целостности даёт Guard: без UNITKA_INTEGRITY_MODE снимка нет, и статус остаётся NO_RUN_YET.
        issueSnapshot = 'SKIPPED_GUARD_OFF';
        log.warn('unitka_issue_snapshot_skipped', { reason: 'UNITKA_INTEGRITY_MODE=off — оценки целостности нет, снимок не пишется; статус останется NO_RUN_YET / STALE', reconcile_mode: reconcileMode });
        return;
      }
      if (integrity.summary.subsystem_failure) { issueSnapshot = 'SKIPPED_GUARD_FAILED'; return; }
      try {
        const rows = issueRecords(integrity.issues, { runId, environment: config.environment, evaluatedAt: integrity.summary.evaluated_at, phase: integrity.summary.phase, reconcileMode });
        await bq.insertIssues(rows);
        issueSnapshot = 'PERSISTED';
        log.info('unitka_issue_snapshot', { reconcile_mode: reconcileMode, rows: rows.length, sheet_business_write: sheetBusinessWriteAllowed });
      } catch (e) {
        issueSnapshot = 'FAILED';
        log.error('unitka_issue_snapshot_failed', { message: e instanceof Error ? e.message : String(e) });
      }
    };

    // 4. SHADOW: QA текущего листа (mismatch = что изменил бы Engine), журнал, выход без записи.
    if (!writeMode) {
      if (integrityMode !== 'off') {
        const pending = new Set(allCells.filter((c) => c.namedRange === undefined).map((c) => `${c.row}|${c.col}`));
        integrity = await evaluateIntegrityPhase({ bq, sheets, config, snap, plan, phase: 'PRE_WRITE', now: deps.now, log, pendingWrite: (r, c) => pending.has(`${r}|${c}`), ...(reconArg ? { recon: reconArg } : {}) });
        publishIntegrity(integrity, { config, log, runId, lcd: plan.lcd });
      }
      // Контролируемый production-observe (UNITKA_WRITE_ENABLED=0): оценка завершена — снимок сохраняется
      // здесь, ДО QA-гейта листа, потому что он описывает данные, а не исход записи.
      await persistIssueSnapshot();
      const qa = evaluate(snap, plan, { shadow: true });
      // В SHADOW mismatch и LCD — ожидаемая разница (новый день), дефектами считаются остальные.
      const SHADOW_DIFF = new Set(['BQ_SHEETS_MISMATCH', 'LCD_CONSISTENT', 'CLOSED_FORMAT_CONTRACT']);
      const expectedFail = qa.checks.filter((c) => !c.pass && !SHADOW_DIFF.has(c.name));
      rec.qaStatus = expectedFail.length ? 'SHADOW_FAIL' : (allCells.length || allFormatCells.length ? 'SHADOW_DIFF' : 'SHADOW_MATCH');
      rec.qaJson = qaJson(qa, { plan: planSummary(plan), calendar, ...sppJson(0), ...(reconcile ? { reconcile: reconcileSummary(reconcile, { repairs_planned: repairs.length, repairs_recorded: 0, issue_snapshot: issueSnapshot }) } : {}), ...(integrity ? { integrity: integrity.summary } : {}) });
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
    if (repairExecutionAllowed) await bq.ledgerProbe();
    const sppCells = sppWrite ? spp!.cells : [];
    const sppClears = sppClearRanges(sppCells, config.unitkaSheetName);
    if (sppClears.length > 0 && !sheets.batchClear) throw new LoaderError('шлюз Sheets не умеет values.batchClear — очистка AB невозможна без потери формата', 'SPP_CLEAR_UNSUPPORTED');
    if (allCells.length === 0 && sppCells.length === 0) {
      log.info('unitka_nothing_to_write', { lcd: plan.lcd });
    } else {
      // Факты и AB — ОДНОЙ записью (values.batchUpdate, RAW): AB не может лечь без фактов и наоборот.
      const ranges = [...toWriteRanges(allCells, config.unitkaSheetName), ...sppWriteRanges(sppCells, config.unitkaSheetName)];
      // План ремонта — в лог ДО мутации листа: происхождение не теряется при любом исходе записи.
      if (repairExecutionAllowed && repairs.length > 0) log.info('unitka_repairs_planned', { count: repairs.length, records: repairs.slice(0, 200) });
      writeStage = 'ATTEMPTED';
      const updated = await sheets.batchWrite(ranges);
      writeStage = 'ACKNOWLEDGED';
      // Очистка AB — отдельным values.batchClear (формат сохраняется); перечитывание ниже проверяет обе операции.
      const cleared = sppClears.length ? await sheets.batchClear!(sppClears) : 0;
      rec.cellsWritten = updated + cleared;
      log.info('unitka_written', { ranges: ranges.length, cells_planned: allCells.length, cells_lcd_month: plan.cells.length, cells_history: allCells.length - plan.cells.length, cells_spp: sppCells.length, cells_spp_cleared: cleared, cells_updated: updated });
      if (updated !== allCells.length + sppCells.length - sppClears.length) {
        // Один batchUpdate либо применяется целиком, либо отвергается; расхождение счётчика —
        // сигнал, что контракт API нарушен. Фиксируем и всё равно идём на reconciliation.
        log.warn('unitka_updated_count_mismatch', { planned: allCells.length + sppCells.length - sppClears.length, updated });
      }
    }

    // 4''. Контракт формата закрытого дня — отдельным spreadsheets.batchUpdate ПОСЛЕ значений.
    // Только строки ≤ LCD; будущее не форматируется. Идемпотентно: при совпадении — 0 запросов.
    if (allFormatCells.length > 0) {
      const writes = toFormatWrites(allFormatCells);
      const applied = await sheets.formatWrite(snap.sheetId, writes);
      log.info('unitka_format_written', { cells: allFormatCells.length, requests: writes.length, applied });
    }

    // 5. reconciliation ДО КОММИТА LCD (Gate 10): данные записаны, B2 ещё прежний. QA сверяет сводку по
    //    дням, закрытым В КНИГЕ, а LCD_CONSISTENT работает как барьер compare-before-commit: B2 обязан
    //    держать ожидаемый закоммиченный LCD. Прежний инвариант «имя = зеркало = LCD» — после коммита.
    const cycle = life!;
    const after = await readSnapshot(sheets, config.unitkaSheetName, snap.geometry, snap.width, snap.anchorCol);
    const qaLcd = evaluate(after, plan, { commitBarrier: { committedIso: cycle.committed } });
    const reconChecks: QaCheck[] = [];
    const afterSections: ReconcileSection[] = [];
    if (writeHistory && reconcile) {
      for (const sec of reconcile.sections) {
        const again = await readSnapshot(sheets, config.unitkaSheetName, sec.snap.geometry, sec.snap.width, sec.snap.anchorCol);
        afterSections.push({ snap: again, plan: sec.plan });
        reconChecks.push(...evaluateRepairedSection(again, sec.plan, { summaryUpTo: cycle.committed }));
      }
    }
    // SPP-3: каждая записанная ячейка AB — значение = плану, формат числа прежний. Провал → коммит LCD не начинается.
    const sppChecks: QaCheck[] = [];
    if (sppCells.length > 0) {
      const reread = new Map<string, Snapshot>([[after.geometry.monthKey, after]]);
      for (const m of new Set(sppCells.map((c) => c.month))) {
        if (reread.has(m)) continue;
        const s0 = sppSnaps.get(m)!;
        reread.set(m, await readSnapshot(sheets, config.unitkaSheetName, s0.geometry, s0.width, s0.anchorCol));
      }
      sppChecks.push(verifySppReadback(sppCells, (m) => reread.get(m)));
    }
    const qa = { pass: qaLcd.pass && reconChecks.every((c) => c.pass) && sppChecks.every((c) => c.pass), checks: [...qaLcd.checks, ...reconChecks, ...sppChecks] };
    rec.qaStatus = qa.pass ? 'PASS' : 'FAIL';
    // Integrity — ПОСЛЕ записи и reconciliation, на перечитанном листе, ДО коммита LCD. Guard читает
    // значения фактов и ТЕКСТ формул, а не результаты формул под отсечкой LCD, поэтому незакоммиченный
    // день-кандидат ложных issue не даёт. Не влияет на qa_status.
    if (integrityMode !== 'off') {
      const reconAfter = reconArg ? { sections: writeHistory ? afterSections : reconcile!.sections } : undefined;
      integrity = await evaluateIntegrityPhase({ bq, sheets, config, snap: after, plan, phase: 'POST_WRITE', now: deps.now, log, ...(reconAfter ? { recon: reconAfter } : {}) });
      publishIntegrity(integrity, { config, log, runId, lcd: plan.lcd });
    }
    // Журнал ремонта — ТОЛЬКО после подтверждённой записи (QA PASS): несостоявшийся ремонт не объявляется состоявшимся.
    let repairsRecorded = 0;
    if (repairExecutionAllowed && qa.pass && repairs.length > 0) {
      writeStage = 'VERIFIED';
      attemptRecorded = true;
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
    await persistIssueSnapshot();
    const setQaJson = (checks: QaCheck[] = qa.checks, pass: boolean = qa.pass): void => {
      rec.qaJson = qaJson({ pass, checks }, {
        plan: planSummary(plan), calendar, format_cells_written: allFormatCells.length, ...sppJson(sppCells.length),
        ...(reconcile ? { reconcile: reconcileSummary(reconcile, { repairs_planned: repairs.length, repairs_recorded: repairsRecorded, issue_snapshot: issueSnapshot }) } : {}),
        ...(integrity ? { integrity: integrity.summary } : {}),
        lifecycle: lifecycleSummary(),
      });
    };
    setQaJson();
    log.info('unitka_qa', { pass: qa.pass, phase: 'PRE_COMMIT', checks: qa.checks.map((c) => `${c.name}:${c.pass ? 'PASS' : 'FAIL(' + c.count + ')'}`) });
    // Любой провал ДО коммита: LCD_AFTER = LCD_BEFORE конструктивно — коммит просто не начинается.
    if (rec.errorCode === 'LEDGER_WRITE_FAILED') {
      await journal();
      throw new LoaderError(rec.errorMessage ?? 'журнал ремонта недоступен', 'LEDGER_WRITE_FAILED');
    }
    if (!qa.pass) {
      await recordUnconfirmed('проверка перечитыванием не пройдена');
      const barrier = qaLcd.checks.find((c) => c.name === 'LCD_CONSISTENT' && !c.pass);
      const code = barrier ? 'LCD_COMMIT_CONFLICT'
        : sppChecks.some((c) => !c.pass) ? 'SPP_READBACK_FAILED'
          : rec.cellsWritten > 0 && rec.cellsWritten !== allCells.length + sppCells.length ? 'PARTIAL_WRITE' : failureCode(qa);
      rec.errorCode = code;
      rec.errorMessage = qa.checks.filter((c) => !c.pass).map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ');
      if (barrier) lifecycleLog(log, 'LCD_COMMIT_CONFLICT', { stage: 'PRE_COMMIT_QA', committed: cycle.committed, candidate: cycle.candidate, sample: barrier.sample }, 'error');
      await journal();
      throw new LoaderError(`QA после записи: ${rec.errorMessage}`, code);
    }
    const gate = enforceGate(integrity, integrityMode);
    if (gate) {
      rec.errorCode = gate.code; rec.errorMessage = gate.message;
      lifecycleLog(log, 'INTEGRITY_FAILED', { committed: cycle.committed, candidate: cycle.candidate, code: gate.code }, 'error');
      await journal();
      throw gate;
    }

    // 6. КОММИТ LCD. Только здесь, после записи, перечитывания, QA и целостности. Барьер: книга обязана
    //    держать тот же LCD, что при планировании; исход решает перечитывание, а не ответ API.
    const cell = new WbLcdCell(sheets, config.unitkaSheetName, snap.anchorCol);
    commit = await commitLcd(cell, { expectedCommitted: cycle.committed, candidate: cycle.candidate });
    if (commit.code === 'LCD_COMMIT_CONFLICT' || commit.code === 'LCD_WRITE_FAILED') {
      // Данные уже опубликованы и проверены, LCD — прежний. Повтор идемпотентен: план данных будет
      // пуст (лист = BigQuery), и коммит пройдёт ровно один раз.
      lifecycleLog(log, commit.code, { committed: cycle.committed, candidate: cycle.candidate, book_after: commit.bookAfter, detail: commit.message }, 'error');
      rec.errorCode = commit.code; rec.errorMessage = commit.message;
      setQaJson();
      await journal();
      throw new LoaderError(commit.message, commit.code);
    }
    if (commit.code === 'LCD_NOT_ADVANCED') {
      lifecycleLog(log, 'LCD_NOT_ADVANCED', { committed: cycle.committed, notes: cycle.decision.notes });
      // зеркало — производное состояние: выравниваем, не трогая LCD
      if (asNumber(after.mirrorLcd) !== isoToSerial(cycle.committed)) {
        await cell.repairMirror(cycle.committed);
        lifecycleLog(log, 'LCD_MIRROR_REPAIRED', { mirror_before: after.mirrorLcd, lcd: cycle.committed });
      }
    } else {
      // 7. Проверка ПОСЛЕ коммита: полный прежний инвариант (имя = зеркало = кандидат) и сводка дня-кандидата,
      //    который теперь впервые считается формулами. Провал — компенсирующий откат собственного коммита.
      const committedSnap = await readSnapshot(sheets, config.unitkaSheetName, snap.geometry, snap.width, snap.anchorCol);
      const qaPostLcd = evaluate(committedSnap, plan);
      // прошлые секции окна, чьи дни стали видны только с коммитом (догон через границу месяца)
      const postRecon: QaCheck[] = [];
      if (writeHistory && reconcile) {
        for (const sec of reconcile.sections) {
          if (sec.plan.toDay <= cycle.committed) continue;       // до коммита уже проверена целиком
          const again = await readSnapshot(sheets, config.unitkaSheetName, sec.snap.geometry, sec.snap.width, sec.snap.anchorCol);
          postRecon.push(...evaluateRepairedSection(again, sec.plan));
        }
      }
      const qaPost = { pass: qaPostLcd.pass && postRecon.every((c) => c.pass), checks: [...qaPostLcd.checks, ...postRecon] };
      log.info('unitka_qa', { pass: qaPost.pass, phase: 'POST_COMMIT', checks: qaPost.checks.map((c) => `${c.name}:${c.pass ? 'PASS' : 'FAIL(' + c.count + ')'}`) });
      if (!qaPost.pass) {
        const rv = await revertLcd(cell, { committed: cycle.committed, from: cycle.candidate });
        reverted = rv.code;
        lifecycleLog(log, 'LCD_REVERTED', { committed: cycle.committed, candidate: cycle.candidate, revert: rv.code, book_after: rv.bookAfter,
          failed: qaPost.checks.filter((c) => !c.pass).map((c) => c.name) }, 'error');
        rec.errorCode = rv.code === 'REVERTED' ? 'POST_COMMIT_QA_FAILED' : 'POST_COMMIT_REVERT_FAILED';
        rec.errorMessage = qaPost.checks.filter((c) => !c.pass).map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ');
        rec.qaStatus = 'FAIL';
        setQaJson(qaPost.checks, false);
        await journal();
        throw new LoaderError(`QA после коммита LCD: ${rec.errorMessage} (откат: ${rv.code})`, rec.errorCode);
      }
      lifecycleLog(log, 'LCD_COMMITTED', { committed_before: cycle.committed, lcd_after: commit.bookAfter, mode: cycle.mode });
    }
    // AUTO придержан дыркой покрытия дольше допуска: безопасная часть опубликована и закоммичена, но это
    // нерешённый сбой источника — событие уровня ERROR с кодом попадает в действующую алерт-политику
    // (severity>=ERROR AND jsonPayload.code!=""), не роняя прогон. MANUAL-удержание — выбор владельца, без алерта.
    const held = Math.round((Date.parse(`${canonical.d1Msk}T00:00:00Z`) - Date.parse(`${cycle.candidate}T00:00:00Z`)) / 86_400_000);
    if (cycle.mode === 'AUTO' && held > config.unitkaMaxLagDays) {
      lifecycleLog(log, 'LCD_NOT_ADVANCED', {
        code: 'LCD_HELD_BY_COVERAGE_GAP', lcd: cycle.candidate, d1_msk: canonical.d1Msk, held_days: held,
        gap_at: cycle.decision.gapAt, canonical_ceiling: cycle.ceiling, notes: cycle.decision.notes,
      }, 'error');
    }
    setQaJson();
    await journal();
    return { rowsFetched: rec.rowsRead, rowsLoaded: rec.cellsWritten };  } catch (e) {
    await recordUnconfirmed(e instanceof Error ? e.message : String(e));
    if (rec.errorCode === null) {
      // Сбой Engine до оценки целостности: integrity_status = SYSTEM_ERROR (если Guard включён).
      if (integrityMode !== 'off' && integrity === null) {
        rec.qaJson = withIntegrity(rec.qaJson, summarize([], integrityMode, writeMode ? 'POST_WRITE' : 'PRE_WRITE', deps.now(), { state: 'UNAVAILABLE', publishedAt: null, ageHours: null }, {
          code: e instanceof LoaderError ? e.code : 'ENGINE_ERROR', message: 'Engine упал до оценки целостности',
        }));
      }
      const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'ENGINE_ERROR', { cause: e });
      try { rec.qaJson = JSON.stringify({ ...JSON.parse(rec.qaJson || '{}'), lifecycle: lifecycleSummary() }); } catch { /* журнал не должен падать */ }
      rec.errorCode = err.code;
      rec.errorMessage = err.message;
      if (rec.qaStatus === 'NOT_RUN') rec.qaStatus = 'FAIL';
      await journal();
      throw err;
    }
    throw e;
  }
}
