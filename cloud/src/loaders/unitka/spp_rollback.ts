/**
 * SPP-3 — загрузчик `unitka-spp-rollback`: откат миграции колонки AB по манифесту.
 *
 *   По умолчанию — ПЛАН: читает лист (шлюз readonly), сверяет каждую ячейку манифеста с книгой, пишет в журнал.
 *   Исполнение — только ENVIRONMENT=prod И UNITKA_SPP_ROLLBACK_WRITE=1 И без DRY_RUN=1 И
 *   UNITKA_SPP_ROLLBACK_DIGEST = отпечатку манифеста (явное подтверждение того, какой снимок восстанавливается).
 *   Манифест — UNITKA_SPP_ROLLBACK_MANIFEST (значение manifest_b64 из журнала unitka_spp_undo_manifest).
 *
 * Восстанавливается ровно то, что было: прежнее значение (RAW) или прежняя формула (USER_ENTERED) и прежний формат
 * числа — только в ячейках манифеста. Факты, формулы других колонок, LCD и Ozon не трогаются.
 * Ячейка, которую после миграции изменил кто-то ещё (не «новое» и не «прежнее» значение), — отказ ДО записи.
 * Режим цикла переводится в off отдельно: UNITKA_SPP_MODE=off (иначе следующий прогон снова запишет факт).
 * BigQuery не читается и не пишется. Нет расписания.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { colA1, type CellValue } from './model.js';
import { quoteSheet } from './section.js';
import { defaultUnitkaDeps, ENGINE_VERSION, type UnitkaDeps } from './index.js';
import { parseSppManifest, sppEqual, type SppManifest, type SppManifestCell } from './spp.js';
import type { SheetsGateway, StructureRequest } from './sheets.js';

export const SPP_ROLLBACK_VERSION = `${ENGINE_VERSION}+spp-rollback`;

export function sppRollbackWriteAllowed(config: { environment: string; unitkaSppRollbackWrite?: boolean; unitkaDryRun?: boolean }): boolean {
  return config.environment === 'prod' && config.unitkaSppRollbackWrite === true && config.unitkaDryRun !== true;
}

type CellState = 'AT_NEW' | 'AT_PREVIOUS' | 'DRIFT';
interface LiveCell { value: CellValue; formula: CellValue; numberFormat: { type?: string; pattern?: string } | null }

const isBlank = (v: CellValue): boolean => v === null || v === undefined || (typeof v === 'string' && v.trim() === '');
const isFormula = (v: CellValue): v is string => typeof v === 'string' && v.startsWith('=');
const nfKey = (f: { type?: string; pattern?: string } | null): string => JSON.stringify(f ? { type: f.type, pattern: f.pattern } : null);

function atPrevious(c: SppManifestCell, live: LiveCell): boolean {
  if (isFormula(c.previousFormula)) return live.formula === c.previousFormula;
  if (isBlank(c.previousValue)) return isBlank(live.value);
  return typeof c.previousValue === 'number' ? sppEqual(live.value, c.previousValue) : String(live.value) === String(c.previousValue);
}

/** Прямоугольник по ячейкам одного месяца → значения, формулы, форматы каждой ячейки манифеста. */
async function readLive(sheets: SheetsGateway, sheetName: string, cells: readonly SppManifestCell[]): Promise<Map<string, LiveCell>> {
  const q = quoteSheet(sheetName);
  const out = new Map<string, LiveCell>();
  const byMonth = new Map<string, SppManifestCell[]>();
  for (const c of cells) (byMonth.get(c.month) ?? byMonth.set(c.month, []).get(c.month)!).push(c);
  for (const group of byMonth.values()) {
    const r1 = Math.min(...group.map((c) => c.row)), r2 = Math.max(...group.map((c) => c.row));
    const c1 = Math.min(...group.map((c) => c.col)), c2 = Math.max(...group.map((c) => c.col));
    const range = `${q}!${colA1(c1)}${r1}:${colA1(c2)}${r2}`;
    const [[values], formulas, formats] = await Promise.all([sheets.readValues([range]), sheets.readFormulas(range), sheets.readFormats(range)]);
    for (const c of group) {
      const i = c.row - r1, j = c.col - c1;
      const nf = formats.rows[i]?.[j]?.numberFormat ?? null;
      out.set(c.a1, { value: values?.[i]?.[j] ?? '', formula: formulas[i]?.[j] ?? '', numberFormat: nf ? { type: nf.type, pattern: nf.pattern } : null });
    }
  }
  return out;
}

export async function unitkaSppRollbackLoader(ctx: LoaderContext, deps: UnitkaDeps = defaultUnitkaDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const write = sppRollbackWriteAllowed(config);
  const log = logger.child({ engine: SPP_ROLLBACK_VERSION, mode: write ? 'WRITE' : 'PLAN' });

  const m = parseSppManifest(config.unitkaSppRollbackManifest ?? '');
  if ('error' in m) throw new LoaderError(`UNITKA_SPP_ROLLBACK_MANIFEST: ${m.error}`, 'SPP_ROLLBACK_MANIFEST_INVALID');
  const manifest: SppManifest = m;
  if (manifest.spreadsheetId !== config.unitkaSpreadsheetId || manifest.sheetName !== config.unitkaSheetName) {
    throw new LoaderError(`манифест для ${manifest.spreadsheetId}/${manifest.sheetName}, а загрузчик настроен на ${config.unitkaSpreadsheetId}/${config.unitkaSheetName}`, 'SPP_ROLLBACK_WRONG_BOOK');
  }
  if (write && config.unitkaSppRollbackDigest !== manifest.digest) {
    throw new LoaderError(`UNITKA_SPP_ROLLBACK_DIGEST «${config.unitkaSppRollbackDigest}» ≠ отпечатку манифеста ${manifest.digest}`, 'SPP_ROLLBACK_DIGEST_MISMATCH');
  }
  const sheets = deps.makeSheets(ctx, !write);
  const meta = await sheets.readSheetMeta(config.unitkaSheetName);
  if (meta.sheetId !== manifest.sheetId) throw new LoaderError(`sheetId ${meta.sheetId} ≠ манифесту ${manifest.sheetId}`, 'SPP_ROLLBACK_WRONG_BOOK');

  const live = await readLive(sheets, config.unitkaSheetName, manifest.cells);
  const states = new Map<string, CellState>();
  for (const c of manifest.cells) {
    const l = live.get(c.a1)!;
    states.set(c.a1, atPrevious(c, l) ? 'AT_PREVIOUS' : sppEqual(l.value, c.newValue) ? 'AT_NEW' : 'DRIFT');
  }
  const drift = manifest.cells.filter((c) => states.get(c.a1) === 'DRIFT');
  const todo = manifest.cells.filter((c) => states.get(c.a1) === 'AT_NEW');
  const fmtPlanned = manifest.cells.filter((c) => nfKey(live.get(c.a1)!.numberFormat) !== nfKey(c.numberFormat));
  log.info('unitka_spp_rollback_plan', {
    manifest_digest: manifest.digest, cells: manifest.cells.length, to_restore: todo.length, already_previous: manifest.cells.length - todo.length - drift.length,
    drift: drift.length, drift_sample: drift.slice(0, 10).map((c) => `${c.a1} ${c.date}: в листе ${String(live.get(c.a1)!.value)}, ожидалось ${String(c.newValue ?? '')} или ${String(c.previousValue)}`),
    formats_to_restore: fmtPlanned.length,
  });
  if (drift.length) throw new LoaderError(`${drift.length} ячеек AB изменены после миграции кем-то ещё — откат не перетирает чужие правки`, 'SPP_ROLLBACK_DRIFT');
  if (!write) return { rowsFetched: manifest.cells.length, rowsLoaded: 0 };

  const q = quoteSheet(config.unitkaSheetName);
  const blank = todo.filter((c) => !isFormula(c.previousFormula) && isBlank(c.previousValue)).map((c) => `${q}!${c.a1}:${c.a1}`);
  const raw = todo.filter((c) => !isFormula(c.previousFormula) && !isBlank(c.previousValue)).map((c) => ({ range: `${q}!${c.a1}:${c.a1}`, values: [[c.previousValue]] }));
  const formulas = todo.filter((c) => isFormula(c.previousFormula)).map((c) => ({ range: `${q}!${c.a1}:${c.a1}`, values: [[c.previousFormula]] }));
  if (blank.length && !sheets.batchClear) throw new LoaderError('шлюз Sheets не умеет values.batchClear', 'SPP_CLEAR_UNSUPPORTED');
  let written = 0;
  if (raw.length) written += await sheets.batchWrite(raw, 'RAW');
  if (formulas.length) written += await sheets.batchWrite(formulas, 'USER_ENTERED');
  if (blank.length) written += await sheets.batchClear!(blank);             // очистка без потери формата
  // формат числа — ПОСЛЕ значений и по перечитыванию: запись значения могла его изменить
  const mid = await readLive(sheets, config.unitkaSheetName, manifest.cells);
  const fmtTodo = manifest.cells.filter((c) => nfKey(mid.get(c.a1)!.numberFormat) !== nfKey(c.numberFormat));
  if (fmtTodo.length) {
    const req: StructureRequest[] = fmtTodo.map((c) => ({ repeatCell: {
      range: { sheetId: manifest.sheetId, startRowIndex: c.row - 1, endRowIndex: c.row, startColumnIndex: c.col - 1, endColumnIndex: c.col },
      cell: { userEnteredFormat: c.numberFormat ? { numberFormat: c.numberFormat } : {} }, fields: 'userEnteredFormat.numberFormat' } }));
    await sheets.structureWrite(req);
  }
  // перечитывание: каждая ячейка манифеста — прежнее значение/формула и прежний формат
  const after = await readLive(sheets, config.unitkaSheetName, manifest.cells);
  const bad = manifest.cells.filter((c) => !atPrevious(c, after.get(c.a1)!) || nfKey(after.get(c.a1)!.numberFormat) !== nfKey(c.numberFormat));
  log.info('unitka_spp_rollback_done', { manifest_digest: manifest.digest, restored: todo.length, formats_restored: fmtTodo.length, cells_written: written, verify_failed: bad.length });
  if (bad.length) throw new LoaderError(`после отката ${bad.length} ячеек AB не совпали с манифестом: ${bad.slice(0, 5).map((c) => c.a1).join(', ')}`, 'SPP_ROLLBACK_VERIFY_FAILED');
  return { rowsFetched: manifest.cells.length, rowsLoaded: written };
}
