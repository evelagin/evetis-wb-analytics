/**
 * OZON — ОДНОРАЗОВАЯ МИГРАЦИЯ АВТОРИТЕТА LCD (Gate 10, этап 5). Admin-загрузчик
 * `ozon-unitka-lcd-migration`: нет расписания, запускается вручную одним исполнением Job.
 *
 * ЗАЧЕМ ОТДЕЛЬНЫЙ ЗАГРУЗЧИК, А НЕ СКРИПТ. Писать в production-книгу имеет право только штатная
 * учётная запись загрузчиков. Миграция идёт тем же образом, той же учётной записью и тем же
 * модулем `lcd_migration.ts`, что прошёл репетицию, — без второй реализации подстановки.
 *
 * ПО УМОЛЧАНИЮ — ТОЛЬКО ПЛАН. Запись — лишь при ENVIRONMENT=prod, OZON_LCD_MIGRATION_WRITE=1,
 * OZON_LCD_MIGRATION_EXPECTED_REFS = живому числу ссылок и OZON_LCD_MIGRATION_EXPECTED_B2 =
 * живому значению B2. Оба ожидания берутся из плана, просмотренного владельцем: если книга
 * изменилась между планом и записью, запись отказывает, а не «применяет что получится».
 *
 * ПОРЯДОК И ИНВАРИАНТЫ:
 *   1. блок ZZ_CONFIG!A29:B34 (B30 = ТО ЖЕ ЧИСЛО, что в B2) → именованный OZON_LAST_CLOSED_DATE → B30;
 *   2. перечитывание: OZON_LAST_CLOSED_DATE == LAST_CLOSED_DATE, блок разбирается как PRESENT/AUTO;
 *   3. формулы листа Ozon: LAST_CLOSED_DATE → OZON_LAST_CLOSED_DATE (planLcdMigration + verifyPlan);
 *   4. перечитывание: каждая ячейка плана = новой формуле, остальные формулы Ozon не изменились,
 *      ссылок на имя WB не осталось, ЗНАЧЕНИЯ листа Ozon побайтно те же, формулы WB, B2, B3 те же.
 *   Провал шага 4 → формулы возвращаются к исходным, прогон падает. Блок и имя остаются: пока
 *   писатель Ozon в режиме LEGACY (OZON_UNITKA_LCD_CELL = ZZ_CONFIG!B2), они ни на что не влияют.
 *
 * Повторный запуск после успеха: ссылок на имя WB нет → NO_CHANGE, ничего не пишется.
 * Экономику, структуру листа, УФ и форматы загрузчик не трогает.
 */
import type { LoaderContext, LoaderResult } from '../../types.js';
import type { Logger } from '../../../logging.js';
import { LoaderError } from '../../../errors.js';
import { SheetsRest, type SheetsGateway, type WriteRange } from '../sheets.js';
import type { CellValue } from '../model.js';
import { columnName } from './requests.js';
import { deriveOzonGeometry } from './ozon_lifecycle.js';
import { planLcdMigration, verifyPlan, referencesWbLcd, type MigrationCell } from './lcd_migration.js';
import {
  GATE10_BLOCK_RANGE, GATE10_PARAMS, OZON_LCD_NAMED_RANGE, WB_LCD_NAMED_RANGE, ZZ_CONFIG_SHEET,
  gate10SeedCells, parseLifecycleBlock, dateCellToIso,
} from '../config_sheet.js';

export interface OzonLcdMigrationDeps {
  makeSheets: (ctx: LoaderContext, readonly: boolean) => SheetsGateway;
}
export const defaultOzonLcdMigrationDeps: OzonLcdMigrationDeps = {
  makeSheets: (ctx, ro) => new SheetsRest(ctx.config.unitkaSpreadsheetId, ro),
};

const quote = (s: string): string => `'${s.replace(/'/g, "''")}'`;
const BATCH_RANGES = 400;

/** Сетка без хвостовых пустот: API обрезает строки по-разному для значений и формул. */
function norm(grid: readonly (readonly CellValue[])[]): string[] {
  return grid.map((r) => {
    const a = [...r]; while (a.length && (a[a.length - 1] === '' || a[a.length - 1] === null || a[a.length - 1] === undefined)) a.pop();
    return JSON.stringify(a);
  });
}
function sameGrid(a: readonly (readonly CellValue[])[], b: readonly (readonly CellValue[])[]): { equal: boolean; firstDiffRow: number | null } {
  const x = norm(a), y = norm(b);
  const n = Math.max(x.length, y.length);
  for (let i = 0; i < n; i++) if ((x[i] ?? '[]') !== (y[i] ?? '[]')) return { equal: false, firstDiffRow: i + 1 };
  return { equal: true, firstDiffRow: null };
}
const cellAt = (grid: readonly (readonly CellValue[])[], row: number, col: number): CellValue => grid[row - 1]?.[col - 1] ?? '';

/** Ячейки плана → диапазоны values.batchUpdate: сплошные отрезки строки одним диапазоном. */
export function planWrites(sheet: string, cells: readonly MigrationCell[], pick: 'before' | 'after'): WriteRange[] {
  const byRow = new Map<number, Map<number, string>>();
  for (const c of cells) {
    if (!byRow.has(c.row)) byRow.set(c.row, new Map());
    byRow.get(c.row)!.set(c.col, c[pick]);
  }
  const out: WriteRange[] = [];
  for (const row of [...byRow.keys()].sort((a, b) => a - b)) {
    const m = byRow.get(row)!;
    const cols = [...m.keys()].sort((a, b) => a - b);
    let start = cols[0]!, prev = start, run: CellValue[] = [m.get(start)!];
    const flush = (): void => { out.push({ range: `${sheet}!${columnName(start)}${row}:${columnName(prev)}${row}`, values: [run] }); };
    for (const c of cols.slice(1)) {
      if (c === prev + 1) { run.push(m.get(c)!); prev = c; continue; }
      flush(); start = prev = c; run = [m.get(c)!];
    }
    flush();
  }
  return out;
}

async function writeInBatches(sheets: SheetsGateway, data: WriteRange[]): Promise<number> {
  let n = 0;
  for (let i = 0; i < data.length; i += BATCH_RANGES) n += await sheets.batchWrite(data.slice(i, i + BATCH_RANGES), 'USER_ENTERED');
  return n;
}

interface LiveState {
  zz: CellValue[][];
  zzSheetId: number;
  named: Readonly<Record<string, { row: number; col: number }>>;
  ozonFormulas: CellValue[][];
  ozonValues: CellValue[][];
  wbFormulas: CellValue[][];
  ozonMirror: { row: number; col: number } | null;
  ozonCols: number;
}

async function readLive(sheets: SheetsGateway, ozonName: string, wbName: string): Promise<LiveState> {
  const zzMeta = await sheets.readSheetMeta(ZZ_CONFIG_SHEET, false);
  const oMeta = await sheets.readSheetMeta(ozonName, false);
  const wMeta = await sheets.readSheetMeta(wbName, false);
  const oq = quote(ozonName), wq = quote(wbName);
  const oRange = `${oq}!A1:${columnName(oMeta.columnCount)}${oMeta.rowCount}`;
  const [zz, ozonValues] = await sheets.readValues([`${ZZ_CONFIG_SHEET}!A1:C40`, oRange]);
  return {
    zz: zz ?? [], zzSheetId: zzMeta.sheetId, named: zzMeta.namedRanges ?? {},
    ozonValues: ozonValues ?? [], ozonFormulas: await sheets.readFormulas(oRange),
    wbFormulas: await sheets.readFormulas(`${wq}!A1:${columnName(wMeta.columnCount)}${wMeta.rowCount}`),
    ozonMirror: oMeta.namedRanges?.OZON_LCD_MIRROR ?? null, ozonCols: oMeta.columnCount,
  };
}

const zzCell = (zz: CellValue[][], row: number, col: number): CellValue => zz[row - 1]?.[col - 1] ?? '';

export async function ozonLcdMigrationLoader(
  ctx: LoaderContext, deps: OzonLcdMigrationDeps = defaultOzonLcdMigrationDeps,
): Promise<LoaderResult> {
  const c = ctx.config;
  const log: Logger = ctx.logger;
  const write = c.environment === 'prod' && c.ozonLcdMigrationWrite && !c.unitkaDryRun;
  const sheets = deps.makeSheets(ctx, !write);
  const ozonName = c.ozonUnitkaSheetName, wbName = c.unitkaSheetName;
  const oq = quote(ozonName);

  const pre = await readLive(sheets, ozonName, wbName);

  // ── ZZ_CONFIG: B2 — LCD WB, B3 — ставка комиссии; блок цикла — строки 29..34 ─────────────
  const b2 = zzCell(pre.zz, 2, 2), b3 = zzCell(pre.zz, 3, 2);
  const b2Iso = dateCellToIso(b2);
  if (String(zzCell(pre.zz, 2, 1)).trim() !== WB_LCD_NAMED_RANGE || typeof b2 !== 'number' || !b2Iso) {
    throw new LoaderError(`ZZ_CONFIG!A2:B2 = «${String(zzCell(pre.zz, 2, 1))}» / «${String(b2)}»: ожидается LAST_CLOSED_DATE и серийная дата`, 'MIGRATION_B2_UNEXPECTED');
  }
  if (String(zzCell(pre.zz, 3, 1)).trim() !== 'TOTAL_COMMISSION_RATE') {
    throw new LoaderError(`ZZ_CONFIG!A3 = «${String(zzCell(pre.zz, 3, 1))}»: ожидается TOTAL_COMMISSION_RATE`, 'MIGRATION_B3_UNEXPECTED');
  }
  const wbNamed = pre.named[WB_LCD_NAMED_RANGE];
  if (!wbNamed || wbNamed.row !== 2 || wbNamed.col !== 2) {
    throw new LoaderError(`именованный ${WB_LCD_NAMED_RANGE} не указывает на ZZ_CONFIG!B2`, 'MIGRATION_WB_LCD_UNEXPECTED');
  }
  const blockRows = pre.zz.slice(GATE10_PARAMS.heading.row - 1, GATE10_PARAMS.ozonManualLcd.row).map((r) => r.slice(0, 2));
  const block = parseLifecycleBlock(blockRows);
  const ozonNamed = pre.named[OZON_LCD_NAMED_RANGE];
  const fresh = block.state === 'ABSENT' && !ozonNamed;
  const resume = block.state === 'PRESENT' && ozonNamed?.row === GATE10_PARAMS.ozonLcd.row && ozonNamed.col === 2
    && block.ozonLcdCell === b2Iso && typeof zzCell(pre.zz, GATE10_PARAMS.ozonLcd.row, 2) === 'number'
    && zzCell(pre.zz, GATE10_PARAMS.ozonLcd.row, 2) === b2;
  if (!fresh && !resume) {
    throw new LoaderError(`состояние миграции не опознано: блок ${block.state}, ${OZON_LCD_NAMED_RANGE} ${ozonNamed ? `→ строка ${ozonNamed.row}, колонка ${ozonNamed.col}` : 'нет'}; `
      + 'ни «чистая книга», ни «блок и имя уже созданы с B30 = B2» — ничего не пишем', 'MIGRATION_STATE_UNEXPECTED');
  }

  // ── План формул: тот же модуль, что прошёл репетицию ─────────────────────────────────────
  const geo = deriveOzonGeometry({ grid: pre.ozonValues, columnCount: pre.ozonCols, mirror: pre.ozonMirror });
  const plan = planLcdMigration({ grid: pre.ozonFormulas, firstRow: 1, tailFirstColumn: geo.tailFirst });
  const v = verifyPlan(plan);
  if (!v.ok) throw new LoaderError(`план миграции отвергнут: ${v.why}`, 'MIGRATION_PLAN_INVALID');
  const strayTail = plan.inOwnerTail.filter((t) => !(pre.ozonMirror && t.row === pre.ozonMirror.row && t.col === pre.ozonMirror.col));
  if (strayTail.length) {
    throw new LoaderError(`в хвосте владельца ссылки на LCD вне зеркала: ${strayTail.slice(0, 5).map((t) => `${columnName(t.col)}${t.row}`).join(', ')}`, 'MIGRATION_OWNER_TAIL');
  }
  const wbRefs = pre.wbFormulas.reduce((n, r) => n + r.filter((x) => referencesWbLcd(x)).length, 0);
  const summary = {
    mode: write ? 'WRITE' : 'PLAN', state: fresh ? 'FRESH' : 'RESUME', b2: b2Iso, b2_serial: b2, b3,
    refs_to_migrate: plan.cells.length, already_migrated: plan.alreadyMigrated, in_owner_tail: plan.inOwnerTail.length,
    tail_first: geo.tailFirst, wb_refs_untouched: wbRefs,
    first_row: plan.cells[0]?.row ?? null, last_row: plan.cells.at(-1)?.row ?? null,
  };
  log.info('ozon_lcd_migration_plan', summary);

  if (plan.cells.length === 0) {
    if (!resume) throw new LoaderError('ссылок на LAST_CLOSED_DATE в листе Ozon нет, а блок цикла не создан', 'MIGRATION_STATE_UNEXPECTED');
    log.info('ozon_lcd_migration_done', { ...summary, outcome: 'NO_CHANGE' });
    return { rowsFetched: pre.ozonFormulas.length, rowsLoaded: 0 };
  }
  if (!write) return { rowsFetched: pre.ozonFormulas.length, rowsLoaded: 0 };

  const expectedRefs = c.ozonLcdMigrationExpectedRefs;
  if (expectedRefs === null || expectedRefs !== plan.cells.length + plan.alreadyMigrated) {
    throw new LoaderError(`OZON_LCD_MIGRATION_EXPECTED_REFS=${String(expectedRefs)}, в книге ${plan.cells.length + plan.alreadyMigrated}: запись только по просмотренному плану`, 'MIGRATION_EXPECTATION_MISMATCH');
  }
  if (c.ozonLcdMigrationExpectedB2 !== b2Iso) {
    throw new LoaderError(`OZON_LCD_MIGRATION_EXPECTED_B2=${c.ozonLcdMigrationExpectedB2 || '(пусто)'}, в книге ${b2Iso}`, 'MIGRATION_EXPECTATION_MISMATCH');
  }

  // ── 1. блок цикла и имя (только в чистой книге) ──────────────────────────────────────────
  let written = 0;
  if (fresh) {
    const seed = gate10SeedCells(b2Iso).map((s): WriteRange => ({
      range: s.range, values: [[s.range === `${ZZ_CONFIG_SHEET}!B${GATE10_PARAMS.ozonLcd.row}` ? b2 : s.value]],
    }));
    written += await sheets.batchWrite(seed, 'USER_ENTERED');
    const dateFmt = (row: number) => ({ repeatCell: {
      range: { sheetId: pre.zzSheetId, startRowIndex: row - 1, endRowIndex: row, startColumnIndex: 1, endColumnIndex: 2 },
      cell: { userEnteredFormat: { numberFormat: { type: 'DATE', pattern: 'dd.mm.yyyy' } } }, fields: 'userEnteredFormat.numberFormat' } });
    await sheets.structureWrite([
      { addNamedRange: { namedRange: { name: OZON_LCD_NAMED_RANGE, range: {
        sheetId: pre.zzSheetId, startRowIndex: GATE10_PARAMS.ozonLcd.row - 1, endRowIndex: GATE10_PARAMS.ozonLcd.row, startColumnIndex: 1, endColumnIndex: 2 } } } },
      dateFmt(GATE10_PARAMS.ozonLcd.row), dateFmt(GATE10_PARAMS.wbManualLcd.row), dateFmt(GATE10_PARAMS.ozonManualLcd.row),
    ]);
  }
  // ── 2. перечитывание: имя разрешается и равно B2, блок — PRESENT/AUTO ─────────────────────
  const [own, wbl, blk] = await sheets.readValues([OZON_LCD_NAMED_RANGE, WB_LCD_NAMED_RANGE, GATE10_BLOCK_RANGE]);
  const seeded = parseLifecycleBlock(blk ?? []);
  if (own?.[0]?.[0] !== b2 || wbl?.[0]?.[0] !== b2 || seeded.state !== 'PRESENT' || seeded.wb.mode !== 'AUTO' || seeded.ozon.mode !== 'AUTO') {
    throw new LoaderError(`после засева: ${OZON_LCD_NAMED_RANGE}=${String(own?.[0]?.[0])}, ${WB_LCD_NAMED_RANGE}=${String(wbl?.[0]?.[0])}, блок ${seeded.state} — ожидалось ${b2}/${b2}/PRESENT AUTO`, 'MIGRATION_SEED_VERIFY_FAILED');
  }
  log.info('ozon_lcd_migration_seeded', { b30: b2, b2, named_range: OZON_LCD_NAMED_RANGE, state: fresh ? 'CREATED' : 'EXISTING' });

  // ── 3. формулы ─────────────────────────────────────────────────────────────────────────────
  written += await writeInBatches(sheets, planWrites(oq, plan.cells, 'after'));

  // ── 4. перечитывание и сверка; провал → откат формул ─────────────────────────────────────
  const post = await readLive(sheets, ozonName, wbName);
  const problems: string[] = [];
  for (const cell of plan.cells) {
    if (cellAt(post.ozonFormulas, cell.row, cell.col) !== cell.after) { problems.push(`${columnName(cell.col)}${cell.row}: формула не легла`); if (problems.length > 5) break; }
  }
  const expectedFormulas = pre.ozonFormulas.map((r) => [...r]);
  for (const cell of plan.cells) { const r = expectedFormulas[cell.row - 1]!; while (r.length < cell.col) r.push(''); r[cell.col - 1] = cell.after; }
  const fDiff = sameGrid(post.ozonFormulas, expectedFormulas);
  if (!fDiff.equal) problems.push(`формулы Ozon вне плана изменились (строка ${String(fDiff.firstDiffRow)})`);
  const leftover = post.ozonFormulas.reduce((n, r) => n + r.filter((x) => referencesWbLcd(x)).length, 0);
  if (leftover) problems.push(`в листе Ozon осталось ссылок на ${WB_LCD_NAMED_RANGE}: ${leftover}`);
  const vDiff = sameGrid(post.ozonValues, pre.ozonValues);
  if (!vDiff.equal) problems.push(`значения листа Ozon изменились (строка ${String(vDiff.firstDiffRow)})`);
  const refErrors = post.ozonValues.reduce((n, r) => n + r.filter((x) => typeof x === 'string' && x.startsWith('#REF')).length, 0);
  if (refErrors) problems.push(`#REF! в значениях Ozon: ${refErrors}`);
  if (!sameGrid(post.wbFormulas, pre.wbFormulas).equal) problems.push('формулы листа WB изменились');
  if (zzCell(post.zz, 2, 2) !== b2) problems.push(`B2 изменилась: ${String(zzCell(post.zz, 2, 2))}`);
  if (zzCell(post.zz, 3, 2) !== b3 || String(zzCell(post.zz, 3, 1)).trim() !== 'TOTAL_COMMISSION_RATE') problems.push('B3 изменилась');

  if (problems.length) {
    log.error('ozon_lcd_migration_verify_failed', { problems: problems.slice(0, 10), action: 'ROLLBACK_FORMULAS' });
    await writeInBatches(sheets, planWrites(oq, plan.cells, 'before'));
    const back = await sheets.readFormulas(`${oq}!A1:${columnName(pre.ozonCols)}${pre.ozonFormulas.length}`);
    const restored = sameGrid(back, pre.ozonFormulas).equal;
    log.error('ozon_lcd_migration_rolled_back', { restored });
    throw new LoaderError(`миграция не прошла проверку (${problems.slice(0, 3).join(' | ')}); формулы ${restored ? 'возвращены' : 'НЕ удалось вернуть'}`,
      restored ? 'MIGRATION_VERIFY_FAILED' : 'MIGRATION_ROLLBACK_FAILED');
  }
  const ownRefs = post.ozonFormulas.reduce((n, r) => n + r.filter((x) => typeof x === 'string' && x.includes(OZON_LCD_NAMED_RANGE)).length, 0);
  log.info('ozon_lcd_migration_done', {
    ...summary, outcome: 'MIGRATED', migrated: plan.cells.length, own_refs_after: ownRefs, wb_refs_left_in_ozon: 0,
    ozon_value_delta: 0, wb_formula_mutations: 0, b2_mutation: 0, b3_mutation: 0, cells_written: written,
  });
  return { rowsFetched: pre.ozonFormulas.length, rowsLoaded: plan.cells.length };
}
