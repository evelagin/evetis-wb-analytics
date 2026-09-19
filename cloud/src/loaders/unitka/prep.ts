/**
 * UNITKA CALENDAR V2 (Phase 2B) — загрузчик `unitka-month-prep`: подготовка секции месяца.
 *
 * ОТДЕЛЬНО от суточного Engine: суточный прогон месяц не создаёт никогда.
 *   По умолчанию — ПЛАН (DRY): читает лист и BigQuery, строит план, пишет его в лог; ничего не меняет.
 *   Запись — только ENVIRONMENT=prod И UNITKA_MONTH_PREP_WRITE=1 (отдельный от UNITKA_WRITE_ENABLED
 *   гейт): один spreadsheets.batchUpdate, затем повторное чтение и проверка контракта секции.
 *   В shadow шлюз readonly: структурная запись отказывает до HTTP.
 * Phase 2B: загрузчик не активирован — нет расписания, нет шага в deploy-*.yml.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { UnitkaBq } from './bq.js';
import { classifyCogsSnapshot } from './integrity.js';
import { validateSection } from './plan.js';
import { formatMonthKey, geometryAt, locateSection, monthKeyOf, nextMonth, parseMonthKey, previousMonth, type MonthKey } from './calendar.js';
import { readColumnA, readSnapshot } from './section.js';
import { planMonthPrep, templateRowsOf, toStructureRequests, type MonthPrepPlan } from './monthprep.js';
import { defaultUnitkaDeps, ENGINE_VERSION, type UnitkaDeps } from './index.js';
import type { Snapshot } from './plan.js';
import type { SheetsGateway, SheetMeta } from './sheets.js';

export const MONTH_PREP_VERSION = `${ENGINE_VERSION}+month-prep`;

/** Запись разрешена ТОЛЬКО так. Суточный UNITKA_WRITE_ENABLED сюда не входит. */
export function monthPrepWriteAllowed(config: { environment: string; unitkaMonthPrepWrite?: boolean }): boolean {
  return config.environment === 'prod' && config.unitkaMonthPrepWrite === true;
}

async function sectionSnapshot(sheets: SheetsGateway, sheetName: string, columnA: readonly unknown[], meta: SheetMeta, key: MonthKey): Promise<Snapshot | null> {
  const loc = locateSection(columnA, key);
  if (loc.status !== 'FOUND') return null;
  const g = geometryAt(key, loc.topRow);
  if (g.spacerRow > meta.rowCount) return null;
  return readSnapshot(sheets, sheetName, g, meta.columnCount);
}

function planLog(plan: MonthPrepPlan): Record<string, unknown> {
  return {
    status: plan.status, code: plan.code, target: plan.target, reasons: plan.reasons.slice(0, 10),
    section: plan.geometry ? { top: plan.geometry.topRow, first: plan.geometry.firstDailyRow, last: plan.geometry.lastDailyRow, mtd: plan.geometry.mtdRow, spacer: plan.geometry.spacerRow, days: plan.geometry.daysInMonth } : null,
    predecessor: plan.predecessor,
    blocks: plan.blocks.map((b) => ({ slot: b.slot, nm_id: b.nmId, origin: b.origin, cogs: b.params.cogsTerm })),
    retired: plan.retiredNmIds, unmapped_active: plan.unmappedActive,
    append_rows: plan.appendRows, append_columns: plan.appendColumns,
    cells: plan.cells.length, format_copies: plan.formatCopies.length, merges: plan.merges.length,
    cf: plan.conditionalFormats ? { carried: plan.conditionalFormats.carried, extended: plan.conditionalFormats.extendedToNewBlocks, cloned: plan.conditionalFormats.cloned, requests: plan.conditionalFormats.requests.length, skipped: plan.conditionalFormats.skipped } : null,
    dimension_requests: plan.dimensionRequests.length, formula_style: plan.formulaStyle,
    manual_blank_cells: plan.manualBlankCells, cogs_provenance: plan.cogsProvenance,
  };
}

export async function unitkaMonthPrepLoader(ctx: LoaderContext, deps: UnitkaDeps = defaultUnitkaDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const write = monthPrepWriteAllowed(config);
  const log = logger.child({ engine: MONTH_PREP_VERSION, mode: write ? 'WRITE' : 'PLAN' });
  const bq = new UnitkaBq(deps.makeRunner(ctx), config.unitkaMartDataset, config.unitkaOpsDataset, config.unitkaRunsTable);
  const sheets = deps.makeSheets(ctx, !write);
  const sheet = config.unitkaSheetName;

  const lcd = await bq.lastClosedDate();
  let target: MonthKey;
  try {
    target = config.unitkaMonthPrepTarget ? parseMonthKey(config.unitkaMonthPrepTarget) : nextMonth(monthKeyOf(lcd.lastClosedDate));
  } catch (e) {
    throw new LoaderError(`UNITKA_MONTH_PREP_TARGET: ${e instanceof Error ? e.message : String(e)}`, 'MONTH_PREP_TARGET_INVALID');
  }
  if (write && !config.unitkaMonthPrepTarget) {
    throw new LoaderError('запись требует явного UNITKA_MONTH_PREP_TARGET=YYYY-MM', 'MONTH_PREP_TARGET_REQUIRED');
  }

  const meta = await sheets.readSheetMeta(sheet);
  const columnA = await readColumnA(sheets, sheet, meta.rowCount);
  const [existing, predecessor, population, cogsRead, structure] = await Promise.all([
    sectionSnapshot(sheets, sheet, columnA, meta, target),
    sectionSnapshot(sheets, sheet, columnA, meta, previousMonth(target)),
    bq.activeSkus(),
    bq.cogsCanonical().catch((e: unknown) => ({ error: e instanceof Error ? e.message : String(e) })),
    sheets.readSheetStructure(sheet, meta.rowCount, meta.columnCount),
  ]);
  // Форматы строк-шаблонов прошлого месяца (только если месяц ещё не создан и прошлый месяц найден).
  const rowFormats = !existing && predecessor
    ? await sheets.readRowFormats(sheet, templateRowsOf(predecessor.geometry), meta.columnCount)
    : null;
  const cogs = classifyCogsSnapshot(cogsRead, deps.now());
  const plan = planMonthPrep({ target, meta, columnA, predecessor, existing, population, cogs, structure, rowFormats });
  log.info('unitka_month_prep_plan', { spreadsheet_id: config.unitkaSpreadsheetId, sheet_id: meta.sheetId, locale: meta.locale ?? null, ...planLog(plan) });

  if (plan.status === 'NO_CHANGE') return { rowsFetched: population.length, rowsLoaded: 0 };
  if (plan.status !== 'PLAN_CREATE') {
    throw new LoaderError(`подготовка ${formatMonthKey(target)}: ${plan.status} ${plan.code ?? ''} — ${plan.reasons.slice(0, 5).join(' | ')}`, plan.code ?? plan.status);
  }
  if (!write) {
    log.info('unitka_month_prep_dry', { target: plan.target, requests: toStructureRequests(plan, meta.sheetId).length, note: 'DRY: запись не выполнялась' });
    return { rowsFetched: population.length, rowsLoaded: 0 };
  }

  // WRITE: один атомарный batchUpdate → повторное чтение → контракт секции.
  const requests = toStructureRequests(plan, meta.sheetId);
  const applied = await sheets.structureWrite(requests);
  log.info('unitka_month_prep_written', { spreadsheet_id: config.unitkaSpreadsheetId, sheet_id: meta.sheetId, target: plan.target, requests: requests.length, applied });
  const meta2 = await sheets.readSheetMeta(sheet);
  const colA2 = await readColumnA(sheets, sheet, meta2.rowCount);
  const after = await sectionSnapshot(sheets, sheet, colA2, meta2, target);
  const v = after ? validateSection(after) : null;
  if (!v || v.sectionIssues.length || v.driftIssues.length) {
    throw new LoaderError(`после записи секция ${plan.target} не проходит контракт: ${v ? [...v.sectionIssues, ...v.driftIssues].slice(0, 8).join(' | ') : 'не найдена'}`, 'MONTH_PREP_VERIFY_FAILED');
  }
  return { rowsFetched: population.length, rowsLoaded: plan.cells.length };
}
