/**
 * UNITKA CALENDAR V2 (Phase 2B) — загрузчик `unitka-month-prep`: подготовка секции месяца.
 *
 * ОТДЕЛЬНО от суточного Engine: суточный прогон месяц не создаёт никогда.
 *   По умолчанию — ПЛАН (DRY): читает лист и BigQuery, строит план, пишет его в лог; ничего не меняет.
 *   Запись — только ENVIRONMENT=prod И UNITKA_MONTH_PREP_WRITE=1 (отдельный от UNITKA_WRITE_ENABLED
 *   гейт): один spreadsheets.batchUpdate, затем повторное чтение и проверка контракта секции.
 *   В shadow шлюз readonly: структурная запись отказывает до HTTP.
 *   МАНИФЕСТ ОТКАТА (monthrollback.ts) строится из плана и состояния листа ДО записи и пишется в журнал и в режиме плана, и
 *   перед записью. Запись создания месяца требует UNITKA_MONTH_PREP_MANIFEST_DIGEST = отпечатку манифеста из ранее
 *   выполненного плана: пишется ровно просмотренный план, а манифест отката существует до записи. DRY_RUN=1 запрещает
 *   любую структурную запись независимо от прочих флагов.
 *   Месяц уже есть, а у активного SKU нет блока → план ДОПИСЫВАНИЯ (monthappend.ts): только в текущий месяц LCD, только
 *   при остатке > 0 или заказах > 0 в этом месяце; запись — дополнительно UNITKA_MONTH_PREP_APPEND=1.
 * Phase 2B: загрузчик не активирован — нет расписания, нет шага в deploy-*.yml.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { UnitkaBq } from './bq.js';
import { classifyCogsSnapshot } from './integrity.js';
import { validateSection } from './plan.js';
import { formatMonthKey, geometryAt, locateSection, monthKeyOf, nextMonth, parseMonthKey, previousMonth, sameMonth, type MonthKey } from './calendar.js';
import { readColumnA, readSnapshot } from './section.js';
import { planMonthPrep, templateRowsOf, toStructureRequests, type MonthPrepPlan } from './monthprep.js';
import { planMonthAppend } from './monthappend.js';
import { buildRollbackManifest, encodeManifest } from './monthrollback.js';
import { defaultUnitkaDeps, ENGINE_VERSION, type UnitkaDeps } from './index.js';
import type { Snapshot } from './plan.js';
import type { SheetsGateway, SheetMeta } from './sheets.js';

export const MONTH_PREP_VERSION = `${ENGINE_VERSION}+month-prep`;

/** Запись разрешена ТОЛЬКО так. Суточный UNITKA_WRITE_ENABLED сюда не входит. DRY_RUN=1 запись запрещает всегда. */
export function monthPrepWriteAllowed(config: { environment: string; unitkaMonthPrepWrite?: boolean; unitkaDryRun?: boolean }): boolean {
  return config.environment === 'prod' && config.unitkaMonthPrepWrite === true && config.unitkaDryRun !== true;
}

async function sectionSnapshot(sheets: SheetsGateway, sheetName: string, columnA: readonly unknown[], meta: SheetMeta, key: MonthKey): Promise<Snapshot | null> {
  const loc = locateSection(columnA, key);
  if (loc.status !== 'FOUND') return null;
  const g = geometryAt(key, loc.topRow);
  if (g.spacerRow > meta.rowCount) return null;
  return readSnapshot(sheets, sheetName, g, meta.columnCount, meta.anchorCol);
}

function planLog(plan: MonthPrepPlan): Record<string, unknown> {
  return {
    status: plan.status, code: plan.code, target: plan.target, reasons: plan.reasons.slice(0, 10),
    section: plan.geometry ? { top: plan.geometry.topRow, first: plan.geometry.firstDailyRow, last: plan.geometry.lastDailyRow, mtd: plan.geometry.mtdRow, spacer: plan.geometry.spacerRow, days: plan.geometry.daysInMonth } : null,
    predecessor: plan.predecessor,
    blocks: plan.blocks.map((b) => ({ slot: b.slot, nm_id: b.nmId, origin: b.origin, cogs: b.params.cogsTerm })),
    // Семейство формулы остатка MTD, перенесённое из месяца-источника (production: native; копия книги: wrapped).
    mtd_stock_family: plan.blocks.reduce<Record<string, number>>((m, b) => { m[b.params.mtdStockFamily] = (m[b.params.mtdStockFamily] ?? 0) + 1; return m; }, {}),
    retired: plan.retiredNmIds, unmapped_active: plan.unmappedActive,
    append_rows: plan.appendRows, insert_columns: plan.insertColumns, anchor_col: plan.anchorCol, chain_gaps: plan.chainGaps, cf_trims: plan.cfTrims.length, width_upgrades: plan.widthUpgrades, row_heights: plan.rowHeights, tail_group_detached: plan.tailGroupDetachedColumn, groups: plan.groupRequests.length,
    cells: plan.cells.length, format_copies: plan.formatCopies.length, merges: plan.merges.length,
    cf: plan.conditionalFormats ? { rules: plan.conditionalFormats.rules.length, start_index: plan.cfStartIndex, families: plan.conditionalFormats.families } : null,
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

  if (plan.status === 'NO_CHANGE') {
    if (plan.unmappedActive.length === 0 || !existing) return { rowsFetched: population.length, rowsLoaded: 0 };
    // Месяц создан, но у активного SKU нет блока: дописывание в конец цепочки (final polish, F4). Fail-closed.
    const current = sameMonth(target, monthKeyOf(lcd.lastClosedDate));
    const [facts, sectionFormats] = await Promise.all([
      current ? bq.facts().catch(() => null) : Promise.resolve(null),
      sheets.readRowFormats(sheet, templateRowsOf(existing.geometry), meta.columnCount),
    ]);
    const ap = planMonthAppend({ target, meta, columnA, existing, population, cogs, structure, rowFormats: sectionFormats, facts, lcd: lcd.lastClosedDate });
    log.info('unitka_month_append_plan', {
      spreadsheet_id: config.unitkaSpreadsheetId, sheet_id: meta.sheetId, ...planLog(ap),
      candidates: ap.candidates, waiting: ap.waiting, cf_delete: ap.cfDeleteIndexes?.length ?? 0, last_closed_date: lcd.lastClosedDate,
    });
    if (ap.status === 'NO_CHANGE') return { rowsFetched: population.length, rowsLoaded: 0 };
    if (ap.status !== 'PLAN_APPEND') throw new LoaderError(`дописывание SKU в ${formatMonthKey(target)}: ${ap.status} ${ap.code ?? ''} — ${ap.reasons.slice(0, 5).join(' | ')}`, ap.code ?? ap.status);
    if (!write || config.unitkaMonthPrepAppend !== true) {
      log.info('unitka_month_append_dry', { target: ap.target, requests: toStructureRequests(ap, meta.sheetId).length, note: 'DRY: запись требует UNITKA_MONTH_PREP_WRITE=1 и UNITKA_MONTH_PREP_APPEND=1' });
      return { rowsFetched: population.length, rowsLoaded: 0 };
    }
    const reqA = toStructureRequests(ap, meta.sheetId);
    const appliedA = await sheets.structureWrite(reqA);
    log.info('unitka_month_append_written', { spreadsheet_id: config.unitkaSpreadsheetId, sheet_id: meta.sheetId, target: ap.target, requests: reqA.length, applied: appliedA, appended: ap.candidates.map((c) => c.nmId) });
    const metaA = await sheets.readSheetMeta(sheet);
    const afterA = await sectionSnapshot(sheets, sheet, await readColumnA(sheets, sheet, metaA.rowCount), metaA, target);
    const vA = afterA ? validateSection(afterA) : null;
    const wantNm = ap.blocks.map((b) => b.nmId).join();
    if (!vA || vA.sectionIssues.length || vA.driftIssues.length || vA.blocks.map((b) => b.nmId).join() !== wantNm || metaA.anchorCol !== ap.anchorCol) {
      throw new LoaderError(`после дописывания секция ${ap.target} не проходит контракт: ${vA ? [...vA.sectionIssues, ...vA.driftIssues, `блоки ${vA.blocks.length}/${ap.blocks.length}`, `якорь ${metaA.anchorCol}/${ap.anchorCol}`].slice(0, 8).join(' | ') : 'не найдена'}`, 'MONTH_PREP_VERIFY_FAILED');
    }
    return { rowsFetched: population.length, rowsLoaded: ap.cells.length };
  }
  if (plan.status !== 'PLAN_CREATE') {
    throw new LoaderError(`подготовка ${formatMonthKey(target)}: ${plan.status} ${plan.code ?? ''} — ${plan.reasons.slice(0, 5).join(' | ')}`, plan.code ?? plan.status);
  }
  // Манифест отката — ДО любой записи: из плана и состояния листа, прочитанного для этого плана.
  const requests = toStructureRequests(plan, meta.sheetId);
  if (!structure) throw new LoaderError('структура листа не прочитана — манифест отката не построить', 'STRUCTURE_UNAVAILABLE');
  const manifest = buildRollbackManifest({
    plan, meta, structure, spreadsheetId: config.unitkaSpreadsheetId, sheetName: sheet, requests: requests.length,
    engine: MONTH_PREP_VERSION, gitSha: config.gitSha, now: deps.now(),
  });
  log.info('unitka_month_prep_rollback_manifest', { target: plan.target, digest: manifest.digest, manifest, manifest_b64: encodeManifest(manifest) });
  if (!write) {
    log.info('unitka_month_prep_dry', { target: plan.target, requests: requests.length, rollback_manifest_digest: manifest.digest, note: 'DRY: запись не выполнялась' });
    return { rowsFetched: population.length, rowsLoaded: 0 };
  }
  // Пишем только просмотренный план: отпечаток манифеста из плана обязан совпасть с манифестом этой записи.
  if (!config.unitkaMonthPrepManifestDigest) {
    throw new LoaderError('запись создания месяца требует UNITKA_MONTH_PREP_MANIFEST_DIGEST — отпечатка манифеста отката из ранее выполненного плана', 'MONTH_PREP_MANIFEST_DIGEST_REQUIRED');
  }
  if (config.unitkaMonthPrepManifestDigest !== manifest.digest) {
    throw new LoaderError(`манифест отката этой записи (${manifest.digest}) не совпадает с предъявленным (${config.unitkaMonthPrepManifestDigest}): книга или план изменились после просмотра — повторите план`, 'MONTH_PREP_MANIFEST_DIGEST_MISMATCH');
  }

  // WRITE: один атомарный batchUpdate → повторное чтение → контракт секции.
  const applied = await sheets.structureWrite(requests);
  log.info('unitka_month_prep_written', { spreadsheet_id: config.unitkaSpreadsheetId, sheet_id: meta.sheetId, target: plan.target, requests: requests.length, applied, rollback_manifest_digest: manifest.digest });
  const meta2 = await sheets.readSheetMeta(sheet);
  const colA2 = await readColumnA(sheets, sheet, meta2.rowCount);
  const after = await sectionSnapshot(sheets, sheet, colA2, meta2, target);
  const v = after ? validateSection(after) : null;
  if (!v || v.sectionIssues.length || v.driftIssues.length) {
    throw new LoaderError(`после записи секция ${plan.target} не проходит контракт: ${v ? [...v.sectionIssues, ...v.driftIssues].slice(0, 8).join(' | ') : 'не найдена'}`, 'MONTH_PREP_VERIFY_FAILED');
  }
  return { rowsFetched: population.length, rowsLoaded: plan.cells.length };
}
