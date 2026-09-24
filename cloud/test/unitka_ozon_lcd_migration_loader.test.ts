/**
 * Gate 10, этап 5 — admin-загрузчик ozon-unitka-lcd-migration на фейковой книге.
 * По умолчанию только план; запись — prod + флаг + оба ожидания; после записи — перечитывание
 * и сверка; провал сверки → формулы возвращаются; повтор после успеха — NO_CHANGE.
 */
import { describe, it, expect } from 'vitest';
import { ozonLcdMigrationLoader, planWrites } from '../src/loaders/unitka/ozon/lcd_migration_loader.js';
import type { SheetsGateway, WriteRange, SheetMeta, StructureRequest } from '../src/loaders/unitka/sheets.js';
import type { CellValue } from '../src/loaders/unitka/model.js';
import { ozonSlotStart } from '../src/loaders/unitka/ozon/contract.js';
import { loadConfig } from '../src/config.js';
import { LOADERS } from '../src/loaders/registry.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';

const OZ = 'OZON_Юнит_2025', WB = 'WB_Юнит_2025';
const B2 = 46287;                                  // 2026-09-22
const colNum = (s: string): number => [...s].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);

interface Line { level: string; event: string; fields: Record<string, unknown> }
const logger = (lines: Line[]): Logger => {
  const mk = (): Logger => ({
    info: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'info', event, fields }); },
    warn: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'warn', event, fields }); },
    error: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'error', event, fields }); },
    debug: () => {}, child: () => mk(),
  } as unknown as Logger);
  return mk();
};

class MigBook implements SheetsGateway {
  zz: CellValue[][] = Array.from({ length: 40 }, () => ['', '', '']);
  ozF: CellValue[][]; ozV: CellValue[][]; wbF: CellValue[][];
  named = new Map<string, { sheet: string; row: number; col: number }>([
    ['LAST_CLOSED_DATE', { sheet: 'ZZ_CONFIG', row: 2, col: 2 }], ['OZON_LCD_MIRROR', { sheet: OZ, row: 2, col: 573 }],
  ]);
  writes: WriteRange[][] = []; structure: StructureRequest[][] = [];
  dropFormulaAt: string | null = null;
  constructor(public readonly readonlyScope = false) {
    this.zz[1] = ['LAST_CLOSED_DATE', B2, 'комментарий']; this.zz[2] = ['TOTAL_COMMISSION_RATE', 0.4532, '']; this.zz[3] = ['TAX_RESERVE_RATE', 0.02, ''];
    this.ozV = Array.from({ length: 650 }, () => Array<CellValue>(574).fill(''));
    for (let s = 0; s < 22; s++) this.ozV[605]![ozonSlotStart(s) - 1] = 'Дата';
    this.ozV[1]![561] = 'OZON выплаты 2025'; this.ozV[1]![572] = B2;
    this.ozF = this.ozV.map((r) => [...r]);
    this.ozF[1]![572] = '=LAST_CLOSED_DATE';                                        // зеркало VA2 в хвосте
    for (let r = 607; r <= 636; r++) for (const c of [12, 37, 62]) {
      this.ozF[r - 1]![c + 5] = `=IF($L${r}>LAST_CLOSED_DATE;"";N${r}*2)`; this.ozV[r - 1]![c + 5] = r;
    }
    this.ozF[640]![2] = '=SUMIFS(F607:F636;$B607:$B636;"<="&LAST_CLOSED_DATE)';    // MTD
    this.ozF[100]![20] = '=A1+1';                                                    // легаси — без ссылок
    this.wbF = Array.from({ length: 10 }, (_, i) => [`=IF($B${i + 1}>LAST_CLOSED_DATE;"";1)`]);
  }
  private sheetOf(range: string): string { return range.startsWith('ZZ_CONFIG') ? 'ZZ_CONFIG' : range.includes(WB) ? WB : OZ; }
  async readSheetMeta(name: string): Promise<SheetMeta> {
    const dims: Record<string, [number, number, number]> = { ZZ_CONFIG: [10, 40, 3], [OZ]: [20, 650, 574], [WB]: [30, 10, 1] };
    const [sheetId, rowCount, columnCount] = dims[name]!;
    const namedRanges: Record<string, { row: number; col: number }> = {};
    for (const [n, v] of this.named) if (v.sheet === name) namedRanges[n] = { row: v.row, col: v.col };
    return { sheetId, rowCount, columnCount, anchorCol: 0, namedRanges };
  }
  async readSheetStructure(): Promise<never> { throw new Error('не нужно'); }
  async readRowFormats(): Promise<never> { throw new Error('не нужно'); }
  async readFormats(): Promise<never> { throw new Error('не нужно'); }
  async formatWrite(): Promise<never> { throw new Error('миграция форматы не пишет'); }
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    return ranges.map((r) => {
      const n = this.named.get(r);
      if (n) return n.sheet === 'ZZ_CONFIG' ? [[this.zz[n.row - 1]![n.col - 1]!]] : [['?']];
      if (r === 'ZZ_CONFIG!A1:C40') return this.zz.map((x) => [...x]);
      if (r === 'ZZ_CONFIG!A29:B34') return this.zz.slice(28, 34).map((x) => x.slice(0, 2));
      if (this.sheetOf(r) === OZ) return this.ozV.map((x) => [...x]);
      throw new Error(`диапазон ${r}`);
    });
  }
  async readFormulas(range: string): Promise<CellValue[][]> {
    return (this.sheetOf(range) === WB ? this.wbF : this.ozF).map((x) => [...x]);
  }
  async batchWrite(data: WriteRange[]): Promise<number> {
    if (this.readonlyScope) throw new Error('403 readonly');
    this.writes.push(data);
    let n = 0;
    for (const d of data) {
      const m = /!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$/.exec(d.range)!;
      const row = Number(m[2]), c0 = colNum(m[1]!);
      d.values[0]!.forEach((v, i) => {
        n++;
        if (this.sheetOf(d.range) === 'ZZ_CONFIG') { this.zz[row - 1]![c0 + i - 1] = v; return; }
        if (this.dropFormulaAt === `${row}:${c0 + i}`) return;                       // «потерянная» запись
        this.ozF[row - 1]![c0 + i - 1] = v;
      });
    }
    return n;
  }
  async structureWrite(req: StructureRequest[]): Promise<number> {
    if (this.readonlyScope) throw new Error('403 readonly');
    this.structure.push(req);
    for (const r of req as Array<{ addNamedRange?: { namedRange: { name: string; range: { startRowIndex: number; startColumnIndex: number } } } }>) {
      if (r.addNamedRange) this.named.set(r.addNamedRange.namedRange.name, { sheet: 'ZZ_CONFIG', row: r.addNamedRange.namedRange.range.startRowIndex + 1, col: r.addNamedRange.namedRange.range.startColumnIndex + 1 });
    }
    return req.length;
  }
}

const REFS = 3 * 30 + 1 + 1;   // 90 дневных + MTD + зеркало
const run = async (book: MigBook, env: Record<string, string> = {}, environment = 'prod') => {
  const lines: Line[] = [];
  const config = loadConfig({ ENVIRONMENT: environment, GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', GIT_SHA: 't', ...env });
  const ctx = { config, logger: logger(lines), logicalPeriod: 'p', targetDate: 'p', runId: 'r' } as LoaderContext;
  try {
    const res = await ozonLcdMigrationLoader(ctx, { makeSheets: () => book });
    return { res, lines, err: null as null | { code?: string; message: string } };
  } catch (e) { return { res: null, lines, err: e as { code?: string; message: string } }; }
};
const WRITE = { OZON_LCD_MIGRATION_WRITE: '1', OZON_LCD_MIGRATION_EXPECTED_REFS: String(REFS), OZON_LCD_MIGRATION_EXPECTED_B2: '2026-09-22' };

describe('ozon-unitka-lcd-migration: гейты', () => {
  it('зарегистрирован, без флага — только план: ни одной записи', async () => {
    expect(LOADERS['ozon-unitka-lcd-migration']).toBeDefined();
    const book = new MigBook();
    const { lines, err } = await run(book);
    expect(err).toBeNull();
    expect(book.writes).toHaveLength(0); expect(book.structure).toHaveLength(0);
    expect(lines.find((l) => l.event === 'ozon_lcd_migration_plan')!.fields).toMatchObject({ mode: 'PLAN', state: 'FRESH', refs_to_migrate: REFS, in_owner_tail: 1, b2: '2026-09-22', tail_first: 562 });
  });
  it('shadow с флагом — всё равно план', async () => {
    const book = new MigBook(true);
    const { err, lines } = await run(book, WRITE, 'shadow');
    expect(err).toBeNull();
    expect(lines.find((l) => l.event === 'ozon_lcd_migration_plan')!.fields.mode).toBe('PLAN');
  });
  it('флаг без ожиданий или с чужими ожиданиями — отказ до записи', async () => {
    for (const env of [{ OZON_LCD_MIGRATION_WRITE: '1' }, { ...WRITE, OZON_LCD_MIGRATION_EXPECTED_REFS: '32967' }, { ...WRITE, OZON_LCD_MIGRATION_EXPECTED_B2: '2026-09-23' }]) {
      const book = new MigBook();
      const { err } = await run(book, env);
      expect(err?.code).toBe('MIGRATION_EXPECTATION_MISMATCH');
      expect(book.writes).toHaveLength(0);
    }
  });
  it('блок A29:B34 занят владельцем — отказ, ничего не пишется', async () => {
    const book = new MigBook(); book.zz[28] = ['мои заметки', 'x', ''];
    const { err } = await run(book, WRITE);
    expect(err?.code).toBe('MIGRATION_STATE_UNEXPECTED');
    expect(book.writes).toHaveLength(0);
  });
  it('B3 не ставка комиссии — отказ', async () => {
    const book = new MigBook(); book.zz[2] = ['ЧТО-ТО', 1, ''];
    expect((await run(book, WRITE)).err?.code).toBe('MIGRATION_B3_UNEXPECTED');
  });
  it('ссылка на LCD в хвосте владельца вне зеркала — отказ', async () => {
    const book = new MigBook(); book.ozF[4]![565] = '=LAST_CLOSED_DATE+1';
    expect((await run(book, WRITE)).err?.code).toBe('MIGRATION_OWNER_TAIL');
    expect(book.writes).toHaveLength(0);
  });
});

describe('ozon-unitka-lcd-migration: запись', () => {
  it('успех: B30 = то же число, что B2; имя → B30; все ссылки Ozon переведены; WB, B2, B3 не тронуты; повтор — NO_CHANGE', async () => {
    const book = new MigBook();
    const wbBefore = JSON.stringify(book.wbF); const valuesBefore = JSON.stringify(book.ozV);
    const { err, lines, res } = await run(book, WRITE);
    expect(err).toBeNull();
    expect(res!.rowsLoaded).toBe(REFS);
    expect(book.zz[29]).toEqual(['OZON_LAST_CLOSED_DATE', B2, '']);
    expect(typeof book.zz[29]![1]).toBe('number');
    expect(book.zz.slice(28, 34).map((r) => r.slice(0, 2))).toEqual([
      ['ЖИЗНЕННЫЙ ЦИКЛ — AUTO-LCD', ''], ['OZON_LAST_CLOSED_DATE', B2], ['WB_LCD_MODE', 'AUTO'], ['WB_MANUAL_LCD', ''], ['OZON_LCD_MODE', 'AUTO'], ['OZON_MANUAL_LCD', '']]);
    expect(book.named.get('OZON_LAST_CLOSED_DATE')).toEqual({ sheet: 'ZZ_CONFIG', row: 30, col: 2 });
    expect(book.zz[1]).toEqual(['LAST_CLOSED_DATE', B2, 'комментарий']);
    expect(book.zz[2]).toEqual(['TOTAL_COMMISSION_RATE', 0.4532, '']);
    const all = book.ozF.flat().filter((x) => typeof x === 'string' && x.includes('CLOSED_DATE')) as string[];
    expect(all).toHaveLength(REFS);
    expect(all.every((f) => /(^|[^A-Z_])OZON_LAST_CLOSED_DATE/.test(f) && !/OZON_OZON/.test(f))).toBe(true);
    expect(book.ozF[1]![572]).toBe('=OZON_LAST_CLOSED_DATE');
    expect(book.ozF[100]![20]).toBe('=A1+1');
    expect(JSON.stringify(book.wbF)).toBe(wbBefore);
    expect(JSON.stringify(book.ozV)).toBe(valuesBefore);
    expect(lines.find((l) => l.event === 'ozon_lcd_migration_done')!.fields).toMatchObject({ outcome: 'MIGRATED', migrated: REFS, own_refs_after: REFS, ozon_value_delta: 0, wb_formula_mutations: 0, b2_mutation: 0, b3_mutation: 0 });

    const writesBefore = book.writes.length;
    const again = await run(book, WRITE);
    expect(again.err).toBeNull();
    expect(book.writes).toHaveLength(writesBefore);
    expect(again.lines.find((l) => l.event === 'ozon_lcd_migration_done')!.fields.outcome).toBe('NO_CHANGE');
  });
  it('формула не легла → сверка падает, формулы возвращены к исходным, код MIGRATION_VERIFY_FAILED', async () => {
    const book = new MigBook(); book.dropFormulaAt = '607:18';
    const before = JSON.stringify(book.ozF);
    const { err, lines } = await run(book, WRITE);
    expect(err?.code).toBe('MIGRATION_VERIFY_FAILED');
    expect(JSON.stringify(book.ozF)).toBe(before);
    expect(lines.some((l) => l.event === 'ozon_lcd_migration_rolled_back' && l.fields.restored === true)).toBe(true);
  });
  it('повтор после отката (блок и имя уже есть, B30 = B2) — продолжает с формул, а не падает', async () => {
    const book = new MigBook(); book.dropFormulaAt = '607:18';
    await run(book, WRITE);
    book.dropFormulaAt = null;
    const { err, lines } = await run(book, WRITE);
    expect(err).toBeNull();
    expect(lines.find((l) => l.event === 'ozon_lcd_migration_plan')!.fields.state).toBe('RESUME');
    expect(book.structure).toHaveLength(1);                 // имя создано один раз
  });
  it('диапазоны записи — сплошные отрезки строки', () => {
    const w = planWrites("'S'", [{ row: 5, col: 1, before: 'a', after: 'A' }, { row: 5, col: 2, before: 'b', after: 'B' }, { row: 5, col: 4, before: 'd', after: 'D' }], 'after');
    expect(w).toEqual([{ range: "'S'!A5:B5", values: [['A', 'B']] }, { range: "'S'!D5:D5", values: [['D']] }]);
  });
});
