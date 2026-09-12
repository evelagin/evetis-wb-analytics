import { describe, it, expect } from 'vitest';
import { unitkaLoader, readSnapshot, type UnitkaDeps } from '../src/loaders/unitka/index.js';
import { evaluate } from '../src/loaders/unitka/qa.js';
import { buildPlan } from '../src/loaders/unitka/plan.js';
import type { SheetsGateway, WriteRange } from '../src/loaders/unitka/sheets.js';
import type { QueryRunner } from '../src/loaders/mart/bq.js';
import { GRID, OFFSET, colA1, isoToSerial, dayRow, type CellValue } from '../src/loaders/unitka/model.js';
import { LoaderError } from '../src/errors.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';
import type { Logger } from '../src/logging.js';
import { snapshot, facts, logistics, commission, LCD, NM_IDS } from './unitka_fixture.js';

const silentLogger = { info() {}, warn() {}, error() {}, debug() {}, child() { return silentLogger; } } as unknown as Logger;
const mkConfig = (environment: 'shadow' | 'prod', writeEnabled: boolean): Config => ({
  environment, gitSha: 'sha1', imageDigest: 'img1', projectId: 'proj', bqLocation: 'EU',
  unitkaSpreadsheetId: 'ssid', unitkaSheetName: 'WB_Юнит_2025', unitkaMartDataset: 'wb_mart', unitkaOpsDataset: 'wb_ops',
  unitkaRunsTable: 'UNITKA_ENGINE_RUNS', unitkaMinN: 10, unitkaMaxLagDays: 2, unitkaWriteEnabled: writeEnabled,
} as unknown as Config);
const ctx = (config: Config): LoaderContext =>
  ({ config, logger: silentLogger, logicalPeriod: '2026-09-11T10', targetDate: '2026-09-11T10', runId: 'run-1' });

/** Фейк BigQuery: отдаёт вью из фикстуры, копит INSERT в журнал. */
class FakeRunner implements QueryRunner {
  readonly projectId = 'proj';
  journal: Array<Record<string, unknown>> = [];
  constructor(private readonly o: { lcd?: string; d1?: string; logisticsSales?: number } = {}) {}
  async query<T = Record<string, unknown>>(sql: string, params?: Record<string, unknown>): Promise<T[]> {
    if (sql.includes('V_UNITKA_SOURCE_FRESHNESS')) {
      return [{ source: 'funnel', max_closed_date: { value: this.o.lcd ?? LCD }, gating: true, observed_at: { value: '2026-09-11T06:50:00Z' } }] as T[];
    }
    if (sql.includes('V_UNITKA_LAST_CLOSED_DATE')) return [{ last_closed_date: { value: this.o.lcd ?? LCD }, d1_msk: { value: this.o.d1 ?? '2026-09-11' } }] as T[];
    if (sql.includes('V_UNITKA_DAILY_FACT')) {
      return facts(this.o.lcd ?? LCD).map((r) => ({
        nm_id: r.nmId, date_msk: { value: r.date }, views: r.views, opens: r.opens, carts: r.carts, orders: r.orders, cancels: r.cancels,
        stock: r.stock, ads_in: r.adsIn === null ? null : String(r.adsIn), price: r.price === null ? null : String(r.price),
        storage: r.storage === null ? null : String(r.storage), orders_source: r.ordersSource, cancels_source: r.cancelsSource,
      })) as T[];
    }
    if (sql.includes('V_UNITKA_LOGISTICS_RATES')) {
      return logistics(this.o.logisticsSales === undefined ? {} : { sales: this.o.logisticsSales }).map((r) => ({
        nm_id: r.nmId, shipments: r.shipments, direct_rate: r.directRate === null ? null : String(r.directRate), refusals: r.refusals,
        reverse_rate: r.reverseRate === null ? null : String(r.reverseRate), forward_sum: String(r.forwardSum), reverse_sum: String(r.reverseSum),
        n_logistics: r.nLogistics, n_delivery: r.nDelivery, delivery_component_sum: String(r.deliveryComponentSum), sales: r.sales,
        window_from: { value: r.windowFrom }, window_to: { value: r.windowTo },
      })) as T[];
    }
    if (sql.includes('V_UNITKA_COMMISSION_RATES')) {
      return commission().map((r) => ({
        nm_id: r.nmId, sales: r.sales, logistics_per_unit: String(r.logisticsPerUnit), commission_rate: String(r.commissionRate),
        window_from: { value: r.windowFrom }, window_to: { value: r.windowTo },
      })) as T[];
    }
    if (sql.includes('INSERT INTO') && sql.includes('UNITKA_ENGINE_RUNS')) { this.journal.push(params ?? {}); return [] as T[]; }
    throw new Error(`unexpected sql: ${sql.slice(0, 80)}`);
  }
}

/** Фейк Sheets: держит сетку из фикстуры, применяет batchUpdate, пересчитывает сводку. */
class FakeSheets implements SheetsGateway {
  writes: WriteRange[][] = [];
  constructor(public snap = snapshot(), public readonly readonlyScope = false, private readonly o: { breakSummaryAfterWrite?: boolean } = {}) {}
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    return ranges.map((r) => {
      if (r === 'LAST_CLOSED_DATE') return [[this.snap.namedLcd]];
      if (r.includes(`${colA1(GRID.MIR)}${GRID.HDR}`)) return [[this.snap.mirrorLcd], [this.snap.mirrorRev]];
      return this.snap.grid;
    });
  }
  async readFormulas(): Promise<CellValue[][]> { return this.snap.formulas; }
  async batchWrite(data: WriteRange[]): Promise<number> {
    if (this.readonlyScope) throw new LoaderError('403 insufficient scope', 'SHEETS_API');
    this.writes.push(data);
    let n = 0;
    for (const d of data) {
      if (d.range === 'LAST_CLOSED_DATE') { this.snap.namedLcd = d.values[0]![0]!; n++; continue; }
      const m = d.range.match(/!([A-Z]+)(\d+):([A-Z]+)(\d+)$/)!;
      const col = colFromA1(m[1]!);
      const r1 = Number(m[2]);
      d.values.forEach((row, i) => {
        const r = r1 + i;
        if (col === GRID.MIR && r === GRID.HDR) this.snap.mirrorLcd = row[0]!;
        else if (col === GRID.MIR && r === GRID.RROW) this.snap.mirrorRev = row[0]!;
        else this.snap.grid[r - GRID.TOP]![col - 1] = row[0]!;
        n++;
      });
    }
    this.recalc();
    return n;
  }
  /** «Формулы» сводки: Σ блоков по всем строкам с датой ≤ LCD книги. */
  private recalc(): void {
    const lcd = Number(this.snap.mirrorLcd);
    const g = this.snap.grid;
    const map: Array<[number, number]> = [[3, 1], [4, 2], [5, 3], [6, 4], [7, 5], [8, 6], [9, 10], [10, 11]];
    let mtd = 0;
    for (let i = 0; i < GRID.DAYS; i++) {
      const row = g[dayRow(i) - GRID.TOP]!;
      const closed = Number(row[1]) <= lcd;
      for (const [sc, off] of map) {
        if (!closed) { row[sc - 1] = ''; continue; }
        let t = 0;
        NM_IDS.forEach((_, b) => { const v = row[GRID.B0 + b * GRID.BW - 1 + off]; if (typeof v === 'number') t += v; });
        row[sc - 1] = t;
      }
      if (closed) mtd += Number(row[8]) || 0;
    }
    g[GRID.MTD - GRID.TOP]![8] = this.o.breakSummaryAfterWrite ? mtd + 1 : mtd;
  }
}
function colFromA1(s: string): number { let n = 0; for (const ch of s) n = n * 26 + (ch.charCodeAt(0) - 64); return n; }

const deps = (runner: FakeRunner, sheets: FakeSheets): UnitkaDeps => ({
  makeRunner: () => runner, makeSheets: () => sheets, now: () => new Date('2026-09-11T07:00:00Z'),
});

describe('unitkaLoader — SHADOW', () => {
  it('считает план, ничего не пишет, журналирует SHADOW_DIFF', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }), true);
    const res = await unitkaLoader(ctx(mkConfig('shadow', true)), deps(runner, sheets));
    expect(res.rowsLoaded).toBe(0);
    expect(sheets.writes).toEqual([]);
    expect(runner.journal).toHaveLength(1);
    const j = runner.journal[0]!;
    expect(j.mode).toBe('SHADOW');
    expect(j.qaStatus).toBe('SHADOW_DIFF');
    expect(j.cellsWritten).toBe(0);
    expect(Number(j.cellsPlanned)).toBeGreaterThan(2000);
    expect(j.lcd).toBe(LCD);
  });
  it('лист уже совпадает с BQ → SHADOW_MATCH, cells_planned = 0', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ direct: (nm) => (nm === NM_IDS[0] ? 65.98 : 60.55), commission: (nm) => (nm === NM_IDS[0] ? 0.441234 : 0.45578) }), true);
    await unitkaLoader(ctx(mkConfig('shadow', true)), deps(runner, sheets));
    expect(runner.journal[0]!.qaStatus).toBe('SHADOW_MATCH');
    expect(runner.journal[0]!.cellsPlanned).toBe(0);
  });
  it('prod без UNITKA_WRITE_ENABLED — тоже SHADOW', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
    await unitkaLoader(ctx(mkConfig('prod', false)), deps(runner, sheets));
    expect(sheets.writes).toEqual([]);
    expect(runner.journal[0]!.mode).toBe('SHADOW');
  });
  it('инвариант провален — ERROR в журнале, код INVARIANT_FAIL, записи нет', async () => {
    const runner = new FakeRunner({ logisticsSales: 500 });
    const sheets = new FakeSheets(snapshot(), true);
    await expect(unitkaLoader(ctx(mkConfig('shadow', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'INVARIANT_FAIL' });
    expect(runner.journal[0]!.errorCode).toBe('INVARIANT_FAIL');
    expect(sheets.writes).toEqual([]);
  });
  it('источники отстают — SOURCE_STALE', async () => {
    const runner = new FakeRunner({ d1: '2026-09-14' });
    await expect(unitkaLoader(ctx(mkConfig('shadow', true)), deps(runner, new FakeSheets(snapshot(), true)))).rejects.toMatchObject({ code: 'SOURCE_STALE' });
    expect(runner.journal[0]!.errorCode).toBe('SOURCE_STALE');
  });
});

describe('unitkaLoader — PROD', () => {
  it('пустой лист: один batchUpdate, reconciliation PASS, повтор — пустой план', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
    const cfg = mkConfig('prod', true);
    const res = await unitkaLoader(ctx(cfg), deps(runner, sheets));
    expect(sheets.writes).toHaveLength(1);
    expect(res.rowsLoaded).toBe(runner.journal[0]!.cellsPlanned);
    expect(runner.journal[0]).toMatchObject({ mode: 'WRITE', qaStatus: 'PASS', errorCode: null });
    expect(sheets.snap.namedLcd).toBe(isoToSerial(LCD));
    expect(sheets.snap.mirrorLcd).toBe(isoToSerial(LCD));
    expect(sheets.snap.grid[dayRow(0) - GRID.TOP]![GRID.B0 - 1 + OFFSET.logistics]).toBe(65.98);

    const res2 = await unitkaLoader(ctx(cfg), deps(runner, sheets));
    expect(res2.rowsLoaded).toBe(0);
    expect(sheets.writes).toHaveLength(1);
    expect(runner.journal[1]).toMatchObject({ cellsPlanned: 0, cellsWritten: 0, qaStatus: 'PASS' });
  });
  it('сводка не сошлась после записи — SUMMARY_MISMATCH, журнал FAIL', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }), false, { breakSummaryAfterWrite: true });
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'SUMMARY_MISMATCH' });
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'FAIL', errorCode: 'SUMMARY_MISMATCH' });
  });
  it('ошибка Sheets API — код SHEETS_API, журнал ERROR', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }), true);
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'SHEETS_API' });
    expect(runner.journal[0]!.errorCode).toBe('SHEETS_API');
  });
});

describe('qa.evaluate', () => {
  it('ошибка формулы и утечка будущего ловятся', () => {
    const snap = snapshot({ direct: (nm) => (nm === NM_IDS[0] ? 65.98 : 60.55), commission: (nm) => (nm === NM_IDS[0] ? 0.441234 : 0.45578) });
    const plan = buildPlan({ snapshot: snap, lcd: { lastClosedDate: LCD, d1Msk: '2026-09-11' }, facts: facts(), logistics: logistics(), commission: commission(), minN: 10, maxLagDays: 2 });
    expect(evaluate(snap, plan).pass).toBe(true);
    snap.grid[dayRow(3) - GRID.TOP]![GRID.B0 + 5 * GRID.BW - 1 + OFFSET.unitProfit] = '#REF!';
    snap.grid[dayRow(20) - GRID.TOP]![GRID.B0 - 1 + OFFSET.views] = 3; // значение без формулы в будущем дне — утечка
    snap.formulas[21]![GRID.B0 - 1 + OFFSET.stock] = '=T757-Q758+S757';
    snap.grid[dayRow(21) - GRID.TOP]![GRID.B0 - 1 + OFFSET.stock] = 470; // формула-наследие: не утечка
    const qa = evaluate(snap, plan);
    expect(qa.pass).toBe(false);
    expect(qa.checks.find((c) => c.name === 'FORMULA_ERRORS')!.count).toBe(1);
    expect(qa.checks.find((c) => c.name === 'FUTURE_LEAKAGE')!.count).toBe(1);
    expect(qa.checks.find((c) => c.name === 'LEGACY_FUTURE_FORMULAS')).toMatchObject({ pass: true, count: 1 });
  });
  it('readSnapshot читает три диапазона и формулы', async () => {
    const sheets = new FakeSheets(snapshot());
    const s = await readSnapshot(sheets, 'WB_Юнит_2025');
    expect(s.grid).toHaveLength(GRID.MTD - GRID.TOP + 1);
    expect(s.namedLcd).toBe(isoToSerial(LCD));
    expect(s.mirrorRev).toBe(32.5256);
  });
});
