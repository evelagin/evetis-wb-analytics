import { describe, it, expect, vi } from 'vitest';
import { buildSkuMaster, refBool, refInt, refStr, refRunId, validationErrors, SKU_MASTER_REQUIRED, type Cell } from '../src/loaders/refsync/normalize.js';
import { assertRefTargets, refEnv, REF_SOURCE_SPREADSHEET_ID } from '../src/loaders/refsync/targets.js';
import { refSyncLoader, type RefDeps } from '../src/loaders/refsync/index.js';
import type { RefStore } from '../src/loaders/refsync/bq.js';
import type { ValidationMetrics } from '../src/loaders/refsync/normalize.js';
import { Logger } from '../src/logging.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';

/** Синтетический лист: выдуманные SKU и nmID, без реальных данных EVETIS. */
const HEADER: Cell[] = ['note', ...SKU_MASTER_REQUIRED];
const row = (o: Partial<Record<(typeof SKU_MASTER_REQUIRED)[number], Cell>>): Cell[] =>
  ['', ...SKU_MASTER_REQUIRED.map((k) => (k in o ? o[k]! : ''))];
const GRID: Cell[][] = [
  HEADER,
  row({ active: true, internal_sku: ' SKU-T1 ', wb_nm_id: 10000001, product_type: 'single', is_bundle: false, barcode: 2000000000011, volume_ml: 50, include_in_pnl: 'ДА' }),
  row({ active: 'TRUE', internal_sku: 'SET-T1', wb_nm_id: '10000002', product_type: 'bundle', is_bundle: 'yes', wb_subject_id: 1234 }),
  [], // пустая строка в середине
  ['', 'TRUE'], // строка без SKU и nm_id (хвост строки API не присылает)
];

describe('REF — нормализация как refStr_/refInt_/refBool_', () => {
  it('refStr: trim, null → пусто, число → строка', () => {
    expect(refStr('  a ')).toBe('a');
    expect(refStr(null)).toBe('');
    expect(refStr(2000000000011)).toBe('2000000000011');
  });
  it('refInt: только целые; пробелы внутри убираются', () => {
    expect(refInt('10 000 001')).toBe(10000001);
    expect(refInt(1.5)).toBeNull();
    expect(refInt('')).toBeNull();
    expect(refInt('abc')).toBeNull();
  });
  it('refBool: TRUE/ДА/1/YES/Y и boolean', () => {
    for (const v of [true, 'TRUE', 'да', '1', 'yes', 'Y']) expect(refBool(v)).toBe(true);
    for (const v of [false, '', null, 'нет', 0]) expect(refBool(v)).toBe(false);
  });
});

describe('REF — строки из листа SKU_MASTER', () => {
  it('пропуск пустых строк, типы, marketplace=WB, ref_run_id и _synced_at на каждой строке', () => {
    const { rows, source } = buildSkuMaster(GRID, 'REF_X', '2026-11-11T05:22:00.000Z');
    expect(source).toBe(2);
    expect(rows[0]).toMatchObject({
      ref_run_id: 'REF_X', marketplace: 'WB', nm_id: 10000001, internal_sku: 'SKU-T1', is_bundle: false,
      active: true, barcode: '2000000000011', volume_ml: '50', include_in_pnl: true, include_in_ads_analysis: false,
      status: '', _synced_at: '2026-11-11T05:22:00.000Z',
    });
    expect(rows[1]).toMatchObject({ nm_id: 10000002, is_bundle: true, active: true, wb_subject_id: '1234' });
  });
  it('нет обязательной колонки — отказ (защита от рассинхрона схемы); нет данных — отказ', () => {
    expect(() => buildSkuMaster([HEADER.filter((h) => h !== 'barcode'), row({ internal_sku: 'A' })], 'R', 'T')).toThrow(/barcode/);
    expect(() => buildSkuMaster([HEADER], 'R', 'T')).toThrow(/нет данных/);
  });
  it('дубль заголовка — побеждает последняя колонка, как в Apps Script', () => {
    const g: Cell[][] = [[...HEADER, 'internal_sku'], [...row({ internal_sku: 'first', wb_nm_id: 1 }), 'second']];
    expect(buildSkuMaster(g, 'R', 'T').rows[0]!.internal_sku).toBe('second');
  });
});

describe('REF — жёсткие валидации', () => {
  const ok: ValidationMetrics = { n: 2, n_nm: 2, null_nm: 0, null_sku: 0, bundle_mismatch: 0, n_single: 1, n_bundle: 1 };
  it('чистая версия — без ошибок', () => expect(validationErrors(ok, 2)).toEqual([]));
  it('пустой nm_id, дубль nm_id, расхождение со staged, bundle ⇎ product_type', () => {
    const e = validationErrors({ ...ok, n_nm: 1, null_nm: 1, bundle_mismatch: 1 }, 3);
    expect(e).toEqual(['строк 2 != загруженным 3', 'пустой nm_id: 1', 'nm_id не уникален: строк 2, различных 1', 'is_bundle ⇎ product_type: 1']);
    expect(validationErrors({ ...ok, n: 0, n_nm: 0 }, 0)).toEqual(['0 строк — лист пуст/не прочитан']);
  });
  it('run_id — формат Apps Script REF_yyyyMMdd_HHmmss_<8>', () => {
    expect(refRunId(Date.parse('2026-11-11T05:22:27Z'), 'abcd1234')).toBe('REF_20261111_082227_abcd1234');
  });
});

describe('REF — контур', () => {
  it('shadow по умолчанию — __CR и книга-источник; prod требует боевые таблицы', () => {
    const e = refEnv({});
    expect(e.spreadsheetId).toBe(REF_SOURCE_SPREADSHEET_ID);
    expect(e.tables.data).toBe('REF_SKU_MASTER_DATA__CR');
    expect(() => assertRefTargets({ environment: 'shadow', rawDataset: 'wb_raw' }, e)).not.toThrow();
    expect(() => assertRefTargets({ environment: 'prod', rawDataset: 'wb_raw' }, e)).toThrow(/REF_DATA_TABLE/);
    const p = refEnv({ REF_DATA_TABLE: 'REF_SKU_MASTER_DATA', REF_ACTIVE_TABLE: 'REF_ACTIVE_VERSION', REF_RUNS_TABLE: 'REF_SYNC_RUNS', REF_TLOG_TABLE: 'REF_SYNC_TABLE_LOG' });
    expect(() => assertRefTargets({ environment: 'prod', rawDataset: 'wb_raw' }, p)).not.toThrow();
    expect(() => assertRefTargets({ environment: 'shadow', rawDataset: 'wb_raw' }, p)).toThrow(/__CR/);
  });
  it('чужая книга или лист — отказ', () => {
    expect(() => assertRefTargets({ environment: 'shadow', rawDataset: 'wb_raw' }, refEnv({ REF_SPREADSHEET_ID: 'other' }))).toThrow(/книга/);
    expect(() => assertRefTargets({ environment: 'shadow', rawDataset: 'wb_raw' }, refEnv({ REF_SHEET_NAME: 'X' }))).toThrow(/лист/);
  });
});

class FakeRefStore implements RefStore {
  calls: string[] = [];
  data: object[] = [];
  constructor(private readonly m?: Partial<ValidationMetrics>, private readonly failPrune = false) {}
  async activeBefore() { this.calls.push('activeBefore'); return 'REF_OLD'; }
  async insertRun(r: string) { this.calls.push(`insertRun:${r}`); }
  async loadData(_r: string, rows: object[]) { this.calls.push(`loadData:${rows.length}`); this.data = rows; }
  async validate(): Promise<ValidationMetrics> {
    this.calls.push('validate');
    const n = this.data.length;
    return { n, n_nm: n, null_nm: 0, null_sku: 0, bundle_mismatch: 0, n_single: 1, n_bundle: 1, ...this.m };
  }
  async tableLog(_r: string, x: { status: string; published: number }) { this.calls.push(`tableLog:${x.status}:${x.published}`); }
  async finalizeRun(_r: string, s: string, after: string | null) { this.calls.push(`finalize:${s}:${after}`); }
  async activate(r: string) { this.calls.push(`activate:${r}`); }
  async prune() { this.calls.push('prune'); if (this.failPrune) throw new Error('prune failed'); }
}

function refCtx(environment: 'shadow' | 'prod' = 'shadow'): LoaderContext {
  const config = { environment, rawDataset: 'wb_raw', projectId: 'p', bqLocation: 'EU' } as unknown as Config;
  return { config, logger: new Logger({}, 'error'), logicalPeriod: '2026-11-11T08', runId: 'r', targetDate: '2026-11-11T08' };
}
const RUN = 'REF_20261111_082227_abcd1234';
function refDeps(store: FakeRefStore, grid: Cell[][] | Error = GRID, env: NodeJS.ProcessEnv = {}): RefDeps & { read: ReturnType<typeof vi.fn> } {
  const read = vi.fn(async () => { if (grid instanceof Error) throw grid; return grid; });
  return { env, makeStore: () => store, makeReader: () => ({ readGrid: read }), nowMs: () => Date.parse('2026-11-11T05:22:27Z'), hex8: () => 'abcd1234', read };
}

describe('REF — прогон all-or-nothing', () => {
  it('успех: журнал STARTED → данные → валидация → ОДНА активация → журнал COMPLETE → retention', async () => {
    const s = new FakeRefStore();
    const res = await refSyncLoader(refCtx(), refDeps(s));
    expect(s.calls).toEqual([
      'activeBefore', `insertRun:${RUN}`, 'loadData:2', 'validate', `activate:${RUN}`,
      'tableLog:COMPLETE:2', `finalize:COMPLETE:${RUN}`, 'prune',
    ]);
    expect(res).toEqual({ rowsFetched: 2, rowsLoaded: 2 });
  });
  it('провал валидации: указатель НЕ трогается, журнал ERROR с прежней версией, прогон — REF_VALIDATION', async () => {
    const s = new FakeRefStore({ n_nm: 1 });
    await expect(refSyncLoader(refCtx(), refDeps(s))).rejects.toMatchObject({ code: 'REF_VALIDATION' });
    expect(s.calls.some((c) => c.startsWith('activate'))).toBe(false);
    expect(s.calls.slice(-2)).toEqual(['tableLog:ERROR:0', 'finalize:ERROR:REF_OLD']);
  });
  it('лист недоступен: ничего не загружено, журнал ERROR, указатель прежний', async () => {
    const s = new FakeRefStore();
    await expect(refSyncLoader(refCtx(), refDeps(s, new Error('Sheets 403')))).rejects.toThrow(/403/);
    expect(s.calls).toEqual(['activeBefore', `insertRun:${RUN}`, 'finalize:ERROR:REF_OLD']);
  });
  it('нет обязательной колонки — REF_SHEET_SHAPE до загрузки строк', async () => {
    const s = new FakeRefStore();
    await expect(refSyncLoader(refCtx(), refDeps(s, [['internal_sku'], ['A']]))).rejects.toMatchObject({ code: 'REF_SHEET_SHAPE' });
    expect(s.calls.some((c) => c.startsWith('loadData'))).toBe(false);
  });
  it('сбой retention после активации — прогон COMPLETE (уборка не отменяет публикацию)', async () => {
    const s = new FakeRefStore(undefined, true);
    await expect(refSyncLoader(refCtx(), refDeps(s))).resolves.toEqual({ rowsFetched: 2, rowsLoaded: 2 });
    expect(s.calls).toContain(`finalize:COMPLETE:${RUN}`);
  });
  it('нарушение контура — ни листа, ни BigQuery', async () => {
    const s = new FakeRefStore();
    const d = refDeps(s, GRID, { REF_DATA_TABLE: 'REF_SKU_MASTER_DATA' });
    await expect(refSyncLoader(refCtx(), d)).rejects.toMatchObject({ code: 'REF_TARGET_MISMATCH' });
    expect(s.calls).toEqual([]);
    expect(d.read).not.toHaveBeenCalled();
  });
});
