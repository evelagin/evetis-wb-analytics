/**
 * SPP-3 — откат миграции AB по манифесту (`unitka-spp-rollback`): план по умолчанию, восстановление значения,
 * формулы и формата числа только в ячейках манифеста, отказ при чужой правке и неверном отпечатке.
 */
import { describe, it, expect } from 'vitest';
import { unitkaSppRollbackLoader } from '../src/loaders/unitka/spp_rollback.js';
import { buildSppManifest, encodeSppManifest, planSpp } from '../src/loaders/unitka/spp.js';
import { LOADERS } from '../src/loaders/registry.js';
import { OFFSET, colA1, type CellValue } from '../src/loaders/unitka/model.js';
import type { SheetsGateway, WriteRange, FormatGrid, SheetMeta, StructureRequest } from '../src/loaders/unitka/sheets.js';
import type { UnitkaDeps } from '../src/loaders/unitka/index.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';
import { loadConfig } from '../src/config.js';
import { snapshot, NM_IDS, GRID, dayRow } from './unitka_fixture.js';

const SHEET = 'WB_Юнит_2025';
const NF = { type: 'NUMBER', pattern: '#,##0' };
const AB = (b: number): number => GRID.B0 + b * GRID.BW + OFFSET.spp;
const colNum = (s: string): number => [...s].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);

/** Книга-ячейки: значение, формула, формат числа по адресу «строка:колонка». */
class CellBook implements Partial<SheetsGateway> {
  cells = new Map<string, { v: CellValue; f: CellValue; nf: typeof NF | null }>();
  writes: Array<{ data: WriteRange[]; opt: string }> = [];
  structures: StructureRequest[][] = [];
  constructor(public readonly readonlyScope = false) {}
  at(r: number, c: number) { return this.cells.get(`${r}:${c}`) ?? { v: '', f: '', nf: NF }; }
  set(r: number, c: number, v: CellValue, f: CellValue = '') { this.cells.set(`${r}:${c}`, { ...this.at(r, c), v, f: f || v }); }
  private rect(range: string) {
    const m = /!([A-Z]+)(\d+):([A-Z]+)(\d+)$/.exec(range)!;
    return { r1: Number(m[2]), r2: Number(m[4]), c1: colNum(m[1]!), c2: colNum(m[3]!) };
  }
  private grid<T>(range: string, pick: (x: { v: CellValue; f: CellValue; nf: typeof NF | null }) => T): T[][] {
    const { r1, r2, c1, c2 } = this.rect(range);
    return Array.from({ length: r2 - r1 + 1 }, (_, i) => Array.from({ length: c2 - c1 + 1 }, (_, j) => pick(this.at(r1 + i, c1 + j))));
  }
  async readSheetMeta(): Promise<SheetMeta> { return { sheetId: 739487431, rowCount: 803, columnCount: 623, anchorCol: 623 }; }
  async readValues(ranges: string[]): Promise<CellValue[][][]> { return ranges.map((r) => this.grid(r, (x) => x.v)); }
  async readFormulas(range: string): Promise<CellValue[][]> { return this.grid(range, (x) => x.f); }
  async readFormats(range: string): Promise<FormatGrid> {
    return { sheetId: 739487431, rows: this.grid(range, (x) => ({ bg: null, fg: null, numberFormat: x.nf })) } as FormatGrid;
  }
  async batchWrite(data: WriteRange[], opt = 'RAW'): Promise<number> {
    if (this.readonlyScope) throw new Error('403 readonly');
    this.writes.push({ data, opt });
    for (const d of data) {
      const { r1, c1 } = this.rect(d.range); const v = d.values[0]![0]!;
      const cur = this.at(r1, c1);
      this.cells.set(`${r1}:${c1}`, { ...cur, v: typeof v === 'string' && v.startsWith('=') ? 99 : v, f: v });
    }
    return data.length;
  }
  clears: string[][] = [];
  async batchClear(ranges: string[]): Promise<number> {
    this.clears.push(ranges);
    for (const r of ranges) { const { r1, c1 } = this.rect(r); this.cells.set(`${r1}:${c1}`, { ...this.at(r1, c1), v: '', f: '' }); }
    return ranges.length;
  }
  async structureWrite(req: StructureRequest[]): Promise<number> {
    this.structures.push(req);
    for (const r of req as Array<{ repeatCell: { range: { startRowIndex: number; startColumnIndex: number }; cell: { userEnteredFormat: { numberFormat?: typeof NF } } } }>) {
      const k = `${r.repeatCell.range.startRowIndex + 1}:${r.repeatCell.range.startColumnIndex + 1}`;
      this.cells.set(k, { ...this.at(r.repeatCell.range.startRowIndex + 1, r.repeatCell.range.startColumnIndex + 1), nf: r.repeatCell.cell.userEnteredFormat.numberFormat ?? null });
    }
    return req.length;
  }
}

const silent = { info() {}, warn() {}, error() {}, debug() {}, child() { return silent; } } as unknown as Logger;
const cfg = (env: Record<string, string>) => loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', GIT_SHA: 't', UNITKA_SPREADSHEET_ID: 'ssid', UNITKA_SHEET_NAME: SHEET, ...env });
const run = (book: CellBook, env: Record<string, string>) => unitkaSppRollbackLoader(
  { config: cfg(env), logger: silent, logicalPeriod: 'p', targetDate: 'p', runId: 'r' } as LoaderContext,
  { makeSheets: () => book as unknown as SheetsGateway } as unknown as UnitkaDeps);

/** Миграция: ручные 20 на 01–03.09 блока 1, формула на 04.09; манифест; книга после записи факта. */
function migrated() {
  const snap = snapshot({ lcdInSheet: '2026-09-10' });
  const setSnap = (r: number, c: number, v: CellValue) => { const row = snap.grid[r - snap.geometry.topRow]!; while (row.length < c) row.push(''); row[c - 1] = v; };
  const book = new CellBook();
  for (let i = 0; i < 3; i++) { setSnap(dayRow(i), AB(0), 20); book.set(dayRow(i), AB(0), 20); }
  setSnap(dayRow(3), AB(0), 25); book.set(dayRow(3), AB(0), 25, '=5*5');
  snap.formulas[3]![AB(0) - 1] = '=5*5';
  for (let i = 0; i < 30; i++) snap.formats[i]![AB(0) - 1] = { bg: null, fg: null, numberFormat: NF };
  const rows = Array.from({ length: 4 }, (_, i) => ({ nmId: NM_IDS[0]!, date: `2026-09-0${i + 1}`, effectiveSppPct: 41.8, status: 'OK', ordersQty: 1 }));
  const plan = planSpp({ sections: [snap], rows, candidate: '2026-09-10' });
  const manifest = buildSppManifest(plan, { spreadsheetId: 'ssid', sheetId: 739487431, sheetName: SHEET });
  for (const c of plan.cells) book.set(c.row, c.col, c.want ?? '');       // «запись» миграции
  return { book, manifest, plan };
}

describe('unitka-spp-rollback', () => {
  it('зарегистрирован; по умолчанию — план, ни одной записи', async () => {
    expect(LOADERS['unitka-spp-rollback']).toBeDefined();
    const { book, manifest } = migrated();
    await run(book, { UNITKA_SPP_ROLLBACK_MANIFEST: encodeSppManifest(manifest) });
    expect(book.writes).toEqual([]); expect(book.structures).toEqual([]);
  });

  it('исполнение: прежние значения (RAW), формула (USER_ENTERED) и формат; только ячейки манифеста', async () => {
    const { book, manifest } = migrated();
    book.set(dayRow(20), AB(1), 7);                                        // чужая ячейка вне манифеста
    book.cells.set(`${dayRow(1)}:${AB(0)}`, { ...book.at(dayRow(1), AB(0)), nf: { type: 'PERCENT', pattern: '0.0%' } });
    await run(book, { UNITKA_SPP_ROLLBACK_MANIFEST: encodeSppManifest(manifest), UNITKA_SPP_ROLLBACK_WRITE: '1', UNITKA_SPP_ROLLBACK_DIGEST: manifest.digest });
    for (let i = 0; i < 3; i++) expect(book.at(dayRow(i), AB(0)).v).toBe(20);
    expect(book.at(dayRow(3), AB(0)).f).toBe('=5*5');
    expect(book.writes.map((w) => w.opt).sort()).toEqual(['RAW', 'USER_ENTERED']);
    expect(book.at(dayRow(1), AB(0)).nf).toEqual(NF);
    expect(book.at(dayRow(20), AB(1)).v).toBe(7);
    const touched = new Set(book.writes.flatMap((w) => w.data.map((d) => d.range.split('!')[1]!.split(':')[0]!)));
    expect([...touched].every((a) => manifest.cells.some((c) => c.a1 === a))).toBe(true);
    expect(manifest.cells.map((c) => c.a1)).toContain(`${colA1(AB(0))}${dayRow(0)}`);
  });

  it('повтор после отката — ничего не пишет (уже «прежнее»)', async () => {
    const { book, manifest } = migrated();
    const env = { UNITKA_SPP_ROLLBACK_MANIFEST: encodeSppManifest(manifest), UNITKA_SPP_ROLLBACK_WRITE: '1', UNITKA_SPP_ROLLBACK_DIGEST: manifest.digest };
    await run(book, env);
    const n = book.writes.length;
    await run(book, env);
    expect(book.writes).toHaveLength(n);
  });

  it('ячейку после миграции правил кто-то ещё — отказ SPP_ROLLBACK_DRIFT до записи', async () => {
    const { book, manifest } = migrated();
    book.set(dayRow(0), AB(0), 33);
    await expect(run(book, { UNITKA_SPP_ROLLBACK_MANIFEST: encodeSppManifest(manifest), UNITKA_SPP_ROLLBACK_WRITE: '1', UNITKA_SPP_ROLLBACK_DIGEST: manifest.digest }))
      .rejects.toMatchObject({ code: 'SPP_ROLLBACK_DRIFT' });
    expect(book.writes).toEqual([]);
  });

  it('отпечаток не подтверждён / чужая книга / не prod — запись невозможна', async () => {
    const { book, manifest } = migrated();
    const m = encodeSppManifest(manifest);
    await expect(run(book, { UNITKA_SPP_ROLLBACK_MANIFEST: m, UNITKA_SPP_ROLLBACK_WRITE: '1', UNITKA_SPP_ROLLBACK_DIGEST: 'abc' })).rejects.toMatchObject({ code: 'SPP_ROLLBACK_DIGEST_MISMATCH' });
    await expect(run(book, { UNITKA_SPP_ROLLBACK_MANIFEST: m, UNITKA_SPREADSHEET_ID: 'other' })).rejects.toMatchObject({ code: 'SPP_ROLLBACK_WRONG_BOOK' });
    await unitkaSppRollbackLoader(
      { config: loadConfig({ ENVIRONMENT: 'shadow', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', GIT_SHA: 't', UNITKA_SPREADSHEET_ID: 'ssid', UNITKA_SHEET_NAME: SHEET, UNITKA_SPP_ROLLBACK_MANIFEST: m, UNITKA_SPP_ROLLBACK_WRITE: '1', UNITKA_SPP_ROLLBACK_DIGEST: manifest.digest }), logger: silent, logicalPeriod: 'p', targetDate: 'p', runId: 'r' } as LoaderContext,
      { makeSheets: () => book as unknown as SheetsGateway } as unknown as UnitkaDeps);
    expect(book.writes).toEqual([]);
    await expect(run(book, { UNITKA_SPP_ROLLBACK_MANIFEST: 'мусор' })).rejects.toMatchObject({ code: 'SPP_ROLLBACK_MANIFEST_INVALID' });
  });
});
