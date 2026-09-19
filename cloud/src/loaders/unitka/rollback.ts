/**
 * UNITKA CALENDAR V2 — загрузчик `unitka-month-rollback`: исполняемый откат СОЗДАНИЯ месяца по манифесту.
 *
 *   По умолчанию — ПЛАН: читает лист (шлюз readonly), сверяет живую книгу с манифестом, пишет в журнал, что было бы
 *   сделано. Ничего не меняет.
 *   Исполнение — только ENVIRONMENT=prod И UNITKA_MONTH_ROLLBACK_WRITE=1 И без DRY_RUN=1: один атомарный
 *   spreadsheets.batchUpdate, затем повторное чтение и сверка структуры листа с состоянием «до создания» из манифеста.
 *   Манифест — UNITKA_MONTH_ROLLBACK_MANIFEST (JSON или base64url из журнала unitka_month_prep_rollback_manifest);
 *   месяц — явный UNITKA_MONTH_PREP_TARGET, обязан совпасть с месяцем манифеста.
 * Любое несовпадение живой книги с манифестом — отказ с кодом ROLLBACK_* ДО любой записи (monthrollback.ts).
 * BigQuery не читается и не пишется. Нет расписания, нет шага в deploy-*.yml.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { colA1 } from './model.js';
import { readColumnA, quoteSheet } from './section.js';
import { defaultUnitkaDeps, ENGINE_VERSION, type UnitkaDeps } from './index.js';
import { parseManifest, planManifestRollback, verifyRollback, type RollbackLive } from './monthrollback.js';

export const MONTH_ROLLBACK_VERSION = `${ENGINE_VERSION}+month-rollback`;

export function monthRollbackWriteAllowed(config: { environment: string; unitkaMonthRollbackWrite?: boolean; unitkaDryRun?: boolean }): boolean {
  return config.environment === 'prod' && config.unitkaMonthRollbackWrite === true && config.unitkaDryRun !== true;
}

export async function unitkaMonthRollbackLoader(ctx: LoaderContext, deps: UnitkaDeps = defaultUnitkaDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const write = monthRollbackWriteAllowed(config);
  const log = logger.child({ engine: MONTH_ROLLBACK_VERSION, mode: write ? 'WRITE' : 'PLAN' });

  const manifest = parseManifest(config.unitkaMonthRollbackManifest ?? '');
  if ('error' in manifest) throw new LoaderError(`UNITKA_MONTH_ROLLBACK_MANIFEST: ${manifest.error}`, 'ROLLBACK_MANIFEST_INVALID');
  if (!config.unitkaMonthPrepTarget || config.unitkaMonthPrepTarget !== manifest.target) {
    throw new LoaderError(`откат требует явного UNITKA_MONTH_PREP_TARGET, равного месяцу манифеста (${manifest.target}); получено «${config.unitkaMonthPrepTarget ?? ''}»`, 'ROLLBACK_TARGET_REQUIRED');
  }

  const sheets = deps.makeSheets(ctx, !write);
  const sheet = config.unitkaSheetName;
  const q = quoteSheet(sheet);
  const meta = await sheets.readSheetMeta(sheet);
  const columnA = await readColumnA(sheets, sheet, meta.rowCount);
  const structure = await sheets.readSheetStructure(sheet, meta.rowCount, meta.columnCount);
  const c = manifest.created;
  // Значения секции и вставленные колонки выше неё читаются, только если сетка позволяет (иначе планировщик откажет сам).
  let sectionGrid: RollbackLive['sectionGrid'] = null;
  let insertedEmptyAbove: boolean | null = c.insertedColumns ? null : true;
  if (meta.rowCount >= c.mtdRow) {
    const [grid] = await sheets.readValues([`${q}!A${c.topRow}:${colA1(meta.columnCount)}${c.mtdRow}`]);
    sectionGrid = Array.from({ length: c.mtdRow - c.topRow + 1 }, (_, i) => grid?.[i] ?? []);
  }
  if (c.insertedColumns && meta.columnCount >= c.insertedColumns.at + c.insertedColumns.count && c.topRow > 1) {
    const above = await sheets.readFormulas(`${q}!${colA1(c.insertedColumns.at + 1)}1:${colA1(c.insertedColumns.at + c.insertedColumns.count)}${c.topRow - 1}`);
    insertedEmptyAbove = above.every((r) => (r ?? []).every((v) => v === '' || v === null || v === undefined));
  }

  const plan = planManifestRollback(manifest, { spreadsheetId: config.unitkaSpreadsheetId, sheetName: sheet, meta, columnA, structure, sectionGrid, insertedEmptyAbove });
  log.info('unitka_month_rollback_plan', {
    spreadsheet_id: config.unitkaSpreadsheetId, sheet_id: meta.sheetId, target: manifest.target, manifest_digest: manifest.digest, manifest_generated_at: manifest.generatedAt,
    status: plan.status, code: plan.code, reasons: plan.reasons.slice(0, 10), requests: plan.requests.length,
    delete_cf_rules: plan.deletedCfRules, delete_rows: plan.deletedRows, delete_columns: plan.deletedColumns,
    restore_widths: plan.restoredWidths.length, skipped_widths: plan.skippedWidths, regroup_tail_column: plan.regroupTailColumn,
    grid: { rows: meta.rowCount, columns: meta.columnCount, anchor_col: meta.anchorCol }, expect_after: { rows: manifest.before.rowCount, columns: manifest.before.columnCount, anchor_col: manifest.before.anchorCol },
  });
  if (plan.status !== 'PLAN_ROLLBACK') {
    throw new LoaderError(`откат ${manifest.target} отклонён: ${plan.code} — ${plan.reasons.slice(0, 5).join(' | ')}`, plan.code ?? 'ROLLBACK_REFUSED');
  }
  if (!write) {
    log.info('unitka_month_rollback_dry', { target: manifest.target, requests: plan.requests.length, note: 'DRY: откат не выполнялся; исполнение требует ENVIRONMENT=prod, UNITKA_MONTH_ROLLBACK_WRITE=1 и отсутствия DRY_RUN=1' });
    return { rowsFetched: 0, rowsLoaded: 0 };
  }

  const applied = await sheets.structureWrite(plan.requests);
  log.info('unitka_month_rollback_written', { spreadsheet_id: config.unitkaSpreadsheetId, sheet_id: meta.sheetId, target: manifest.target, requests: plan.requests.length, applied, manifest_digest: manifest.digest });
  const metaAfter = await sheets.readSheetMeta(sheet);
  const structureAfter = await sheets.readSheetStructure(sheet, metaAfter.rowCount, metaAfter.columnCount);
  const v = verifyRollback(manifest, { meta: metaAfter, structure: structureAfter });
  log.info('unitka_month_rollback_verified', { target: manifest.target, exact: v.exact, structural: v.structural, view_state: v.viewState, skipped_widths: plan.skippedWidths.length });
  // Пропущенные ширины (их меняли вручную после создания) — объяснимое расхождение ширин; остальное — ошибка сверки.
  const unexplained = v.structural.filter((x) => !(plan.skippedWidths.length > 0 && x.startsWith('ширины колонок')));
  if (unexplained.length) {
    throw new LoaderError(`откат ${manifest.target} выполнен, но структура листа не совпала с состоянием до создания: ${unexplained.slice(0, 8).join(' | ')}`, 'ROLLBACK_VERIFY_FAILED');
  }
  return { rowsFetched: 0, rowsLoaded: plan.requests.length };
}
