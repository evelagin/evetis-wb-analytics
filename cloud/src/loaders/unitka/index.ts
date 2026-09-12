/**
 * UNITKA ENGINE v1 — суточный оркестратор (Stage E1). Загрузчик `unitka` на общем образе.
 *
 * Цикл (16 шагов брифа → 5 фаз):
 *   1. свежесть источников + LAST_CLOSED_DATE       (V_UNITKA_SOURCE_FRESHNESS / V_UNITKA_LAST_CLOSED_DATE)
 *   2. снимок листа + preflight                    (структура Master, формулы на месте)
 *   3. факт + ставки → план                        (V_UNITKA_DAILY_FACT / *_RATES; инвариант, дубли, будущее)
 *   4. SHADOW → журнал и выход; PROD → один values.batchUpdate
 *   5. reconciliation: повторное чтение → QA-гейт  (mismatch / формулы / утечка / сводка / LCD)
 *
 * Fail-closed на каждом шаге: LoaderError с кодом → exit 1 → execution FAILED → алерт.
 * Частичной записи нет конструктивно: план уходит одним batchUpdate.
 * Engine не содержит бизнес-SQL и не трогает формулы/УФ/структуру Master.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { BqClient } from '../../bq/client.js';
import type { QueryRunner } from '../mart/bq.js';
import { UnitkaBq, type EngineRunRecord, type FreshnessRow } from './bq.js';
import { SheetsRest, type SheetsGateway } from './sheets.js';
import { GRID, NAMED, colA1 } from './model.js';
import { buildPlan, toWriteRanges, type Plan, type Snapshot } from './plan.js';
import { evaluate, failureCode, qaJson } from './qa.js';

export const ENGINE_VERSION = 'unitka-engine/1.0.0';

export interface UnitkaDeps {
  makeRunner: (ctx: LoaderContext) => QueryRunner;
  makeSheets: (ctx: LoaderContext, readonly: boolean) => SheetsGateway;
  now: () => Date;
}

const defaultDeps: UnitkaDeps = {
  makeRunner: (ctx) => new BqClient(ctx.config.projectId, ctx.config.bqLocation),
  makeSheets: (ctx, readonly) => new SheetsRest(ctx.config.unitkaSpreadsheetId, readonly),
  now: () => new Date(),
};

function quoteSheet(name: string): string {
  return `'${name.replace(/'/g, "''")}'`;
}

export async function readSnapshot(sheets: SheetsGateway, sheetName: string): Promise<Snapshot> {
  const q = quoteSheet(sheetName);
  const gridRange = `${q}!A${GRID.TOP}:${colA1(GRID.NC)}${GRID.MTD}`;
  const mirrorRange = `${q}!${colA1(GRID.MIR)}${GRID.HDR}:${colA1(GRID.MIR)}${GRID.RROW}`;
  const [grid, mirror, named] = await sheets.readValues([gridRange, mirrorRange, NAMED.LCD]);
  const formulas = await sheets.readFormulas(`${q}!A${GRID.FIRST}:${colA1(GRID.NC)}${GRID.FIRST + GRID.DAYS - 1}`);
  return {
    grid: grid ?? [],
    formulas,
    mirrorLcd: mirror?.[0]?.[0] ?? null,
    mirrorRev: mirror?.[1]?.[0] ?? null,
    namedLcd: named?.[0]?.[0] ?? null,
  };
}

function freshnessJson(rows: FreshnessRow[]): string {
  return JSON.stringify(rows.map((r) => ({ source: r.source, max_closed_date: r.maxClosedDate, gating: r.gating, observed_at: r.observedAt })));
}

function planSummary(plan: Plan): Record<string, unknown> {
  const byKind: Record<string, number> = {};
  for (const c of plan.cells) byKind[c.kind] = (byKind[c.kind] ?? 0) + 1;
  return {
    lcd: plan.lcd, lag_days: plan.lagDays, closed_days: plan.closedDays, blocks: plan.blocks.length,
    cells_planned: plan.cells.length, by_kind: byKind,
    invariant: plan.invariant, reverse_rate: plan.reverseRate,
    own_direct: plan.rates.filter((r) => r.directSource === 'own').length,
    own_commission: plan.rates.filter((r) => r.commissionSource === 'own').length,
    book_lcd: plan.bookLcd, by_change_type: plan.byChangeType, stock_projection_future: plan.stockProjectionCells,
    gaps: plan.gaps, legacy_formulas: plan.legacy, legacy_replaced: plan.legacyReplaced, sources_by_day: plan.sourcesByDay,
    sample: plan.cells.slice(0, 40).map((c) => `${c.changeType} ${c.namedRange ?? colA1(c.col) + c.row} ${c.key ?? ''} ${String(c.before)}→${c.want === null ? '' : c.want}`),
  };
}

export async function unitkaLoader(ctx: LoaderContext, deps: UnitkaDeps = defaultDeps): Promise<LoaderResult> {
  const { config, logger, runId } = ctx;
  const startedAt = deps.now();
  const writeMode = config.environment === 'prod' && config.unitkaWriteEnabled;
  const mode: EngineRunRecord['mode'] = writeMode ? 'WRITE' : 'SHADOW';
  const log = logger.child({ engine: ENGINE_VERSION, mode });

  const bq = new UnitkaBq(deps.makeRunner(ctx), config.unitkaMartDataset, config.unitkaOpsDataset, config.unitkaRunsTable);
  const sheets = deps.makeSheets(ctx, !writeMode);

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

    // 2. снимок листа (preflight выполняется внутри buildPlan до любых вычислений)
    const snap = await readSnapshot(sheets, config.unitkaSheetName);

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
      const qa = evaluate(snap, plan, { shadow: true });
      // В SHADOW mismatch и LCD — ожидаемая разница (новый день), дефектами считаются остальные.
      const SHADOW_DIFF = new Set(['BQ_SHEETS_MISMATCH', 'LCD_CONSISTENT']);
      const expectedFail = qa.checks.filter((c) => !c.pass && !SHADOW_DIFF.has(c.name));
      rec.qaStatus = expectedFail.length ? 'SHADOW_FAIL' : (plan.cells.length ? 'SHADOW_DIFF' : 'SHADOW_MATCH');
      rec.qaJson = qaJson(qa, { plan: planSummary(plan) });
      log.info('unitka_shadow', { qa_status: rec.qaStatus, cells_planned: plan.cells.length, checks: qa.checks.map((c) => `${c.name}:${c.pass ? 'PASS' : 'FAIL(' + c.count + ')'}`) });
      if (expectedFail.length) {
        rec.errorCode = failureCode({ pass: false, checks: expectedFail });
        rec.errorMessage = expectedFail.map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ');
        await journal();
        throw new LoaderError(`SHADOW: Master не проходит QA — ${rec.errorMessage}`, rec.errorCode);
      }
      await journal();
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

    // 5. reconciliation — повторное чтение и полный QA-гейт.
    const after = await readSnapshot(sheets, config.unitkaSheetName);
    const qa = evaluate(after, plan);
    rec.qaStatus = qa.pass ? 'PASS' : 'FAIL';
    rec.qaJson = qaJson(qa, { plan: planSummary(plan) });
    log.info('unitka_qa', { pass: qa.pass, checks: qa.checks.map((c) => `${c.name}:${c.pass ? 'PASS' : 'FAIL(' + c.count + ')'}`) });
    if (!qa.pass) {
      const code = rec.cellsWritten > 0 && rec.cellsWritten !== plan.cells.length ? 'PARTIAL_WRITE' : failureCode(qa);
      rec.errorCode = code;
      rec.errorMessage = qa.checks.filter((c) => !c.pass).map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ');
      await journal();
      throw new LoaderError(`QA после записи: ${rec.errorMessage}`, code);
    }
    await journal();
    return { rowsFetched: rec.rowsRead, rowsLoaded: rec.cellsWritten };
  } catch (e) {
    if (rec.errorCode === null) {
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
