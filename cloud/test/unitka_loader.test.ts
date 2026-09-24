import { describe, it, expect } from 'vitest';
import { unitkaLoader, readSnapshot, type UnitkaDeps } from '../src/loaders/unitka/index.js';
import { evaluate } from '../src/loaders/unitka/qa.js';
import { buildPlan } from '../src/loaders/unitka/plan.js';
import type { SheetsGateway, WriteRange, FormatWrite, FormatGrid, SheetMeta, SheetStructure } from '../src/loaders/unitka/sheets.js';
import type { QueryRunner } from '../src/loaders/mart/bq.js';
import { OFFSET, colA1, isoToSerial, type CellValue } from '../src/loaders/unitka/model.js';
import { LoaderError } from '../src/errors.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';
import type { Logger } from '../src/logging.js';
import { snapshot, facts, logistics, commission, LCD, NM_IDS, BLACK, DIM, AUTO_BG, GRID, SEPT, dayRow } from './unitka_fixture.js';

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
  /** Тест E3: хранение за 10.09 «появилось» в источнике (вместо GAP). */
  storageFor10: number | null = null;
  /** Integrity Guard V1: запросы к вью Guard (и переданные им серверные таймауты). */
  integrityQueries = 0;
  jobTimeouts: Array<number | undefined> = [];
  /** Любая запись, кроме журнала прогона, — нарушение (журнал issue отложен, решение D4). */
  otherWrites: string[] = [];
  integrity: {
    rows?: () => Array<Record<string, unknown>>;
    throwFacts?: boolean;
    throwCogs?: boolean;
    cogsRows?: Array<Record<string, unknown>>;
    /** Эмуляция зависшего запроса: перед отказом «сдвинуть часы» на столько мс. */
    hangMs?: number;
    clock?: { t: number };
  } = {};
  /** Gate 10: журнал прогонов гейтящих загрузчиков. По умолчанию каждые сутки до D-1 покрыты. */
  coverage: Array<{ loader_name: string; logical_period: string }> | null = null;
  coverageQueries = 0;
  constructor(private readonly o: { lcd?: string; d1?: string; logisticsSales?: number } = {}) {}
  async query<T = Record<string, unknown>>(sql: string, params?: Record<string, unknown>, _types?: Record<string, string>, options?: { jobTimeoutMs?: number }): Promise<T[]> {
    if (sql.includes('V_UNITKA_COGS_CANONICAL')) {
      this.integrityQueries++;
      this.jobTimeouts.push(options?.jobTimeoutMs);
      if (this.integrity.throwCogs !== false) throw new Error('Not found: Table wb_mart.UNITKA_COGS_EFFECTIVE (копия ещё не опубликована)');
      return (this.integrity.cogsRows ?? []) as T[];
    }
    if (sql.includes('V_UNITKA_INTEGRITY')) {
      this.integrityQueries++;
      this.jobTimeouts.push(options?.jobTimeoutMs);
      if (this.integrity.hangMs !== undefined && this.integrity.clock) {
        this.integrity.clock.t += this.integrity.hangMs;
        throw new Error('Job execution was cancelled: Job timed out after 90 sec');
      }
      if (this.integrity.throwFacts) throw new Error('Not found: View wb_mart.V_UNITKA_INTEGRITY');
      return (this.integrity.rows ? this.integrity.rows() : []) as T[];
    }
    if (/\b(INSERT|UPDATE|DELETE|MERGE)\b/.test(sql) && !sql.includes('UNITKA_ENGINE_RUNS')) this.otherWrites.push(sql.slice(0, 60));
    if (sql.includes('LOADER_RUNS')) {
      this.coverageQueries++;
      if (this.coverage) return this.coverage.filter((r) => r.logical_period > String(params?.since)) as T[];
      const out: Array<{ loader_name: string; logical_period: string }> = [];
      const end = this.o.d1 ?? '2026-09-11';
      for (let d = new Date(`${String(params?.since)}T00:00:00Z`); ; ) {
        d = new Date(d.getTime() + 86_400_000);
        const iso = d.toISOString().slice(0, 10);
        if (iso > end) break;
        out.push({ loader_name: 'funnel', logical_period: iso }, { loader_name: 'mart', logical_period: iso });
      }
      return out as T[];
    }
    if (sql.includes('V_UNITKA_SOURCE_FRESHNESS')) {
      return [{ source: 'funnel', max_closed_date: { value: this.o.lcd ?? LCD }, gating: true, observed_at: { value: '2026-09-11T06:50:00Z' } }] as T[];
    }
    if (sql.includes('V_UNITKA_LAST_CLOSED_DATE')) return [{ last_closed_date: { value: this.o.lcd ?? LCD }, d1_msk: { value: this.o.d1 ?? '2026-09-11' } }] as T[];
    if (sql.includes('V_UNITKA_DAILY_FACT')) {
      return facts(this.o.lcd ?? LCD).map((r) => ({
        nm_id: r.nmId, date_msk: { value: r.date }, views: r.views, opens: r.opens, carts: r.carts, orders: r.orders, cancels: r.cancels,
        stock: r.stock, ads_in: r.adsIn === null ? null : String(r.adsIn), price: r.price === null ? null : String(r.price),
        storage: r.date === '2026-09-10' && this.storageFor10 !== null ? String(this.storageFor10) : (r.storage === null ? null : String(r.storage)),
        orders_source: r.ordersSource, cancels_source: r.cancelsSource,
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
  structureWrites: unknown[][] = [];
  /** Gate 10: строки ZZ_CONFIG!A29:B34. Пусто — блока нет (production до миграции). */
  lifecycle: unknown[][] = [];
  /** Gate 10: сбой ТОЛЬКО записи LCD (данные пишутся). 'throw' | 'noop' — модель ответа API. */
  failLcdWrite: 'throw' | 'noop' | null = null;
  /** Gate 10: кто-то меняет B2 после записи данных — до коммита. */
  tamperLcdAfterDataWrite: number | null = null;
  async readSheetMeta(): Promise<SheetMeta> { return { sheetId: this.snap.sheetId, rowCount: this.snap.geometry.spacerRow, columnCount: this.snap.width, anchorCol: this.snap.anchorCol }; }
  async readSheetStructure(): Promise<SheetStructure> { throw new LoaderError('суточный Engine не читает структуру листа', 'TEST_FORBIDDEN'); }
  async readRowFormats(): Promise<Map<number, Array<Record<string, unknown> | null>>> { throw new LoaderError('суточный Engine не читает форматы строк', 'TEST_FORBIDDEN'); }
  async structureWrite(requests: Record<string, unknown>[]): Promise<number> { this.structureWrites.push(requests); throw new LoaderError('суточный Engine не должен вызывать structureWrite', 'TEST_FORBIDDEN'); }
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    return ranges.map((r) => {
      if (r === 'LAST_CLOSED_DATE') return [[this.snap.namedLcd]];
      if (r === 'ZZ_CONFIG!A29:B34') return this.lifecycle as CellValue[][];
      const colA = /!A1:A(\d+)$/.exec(r);
      if (colA) {
        const top = this.snap.geometry.topRow;
        return Array.from({ length: Number(colA[1]) }, (_, i) => [this.snap.grid[i + 1 - top]?.[0] ?? null]);
      }
      if (r.includes(`${colA1(GRID.MIR)}${GRID.HDR}`)) return [[this.snap.mirrorLcd], [this.snap.mirrorRev]];
      if (/!R45$/.test(r)) return [[240]];
      return this.snap.grid;
    });
  }
  async readFormulas(): Promise<CellValue[][]> { return this.snap.formulas; }
  async readFormats(): Promise<FormatGrid> { return { sheetId: this.snap.sheetId, rows: this.snap.formats.map((r) => r.map((f) => ({ ...f }))) }; }
  formatWrites: FormatWrite[][] = [];
  async formatWrite(sheetId: number, writes: FormatWrite[]): Promise<number> {
    if (this.readonlyScope) throw new LoaderError('403 insufficient scope', 'SHEETS_API');
    if (sheetId !== this.snap.sheetId) throw new LoaderError('wrong sheetId', 'SHEETS_API');
    this.formatWrites.push(writes);
    for (const w of writes) for (let r = w.startRow; r < w.endRow; r++) for (let c = w.startCol; c < w.endCol; c++) {
      this.snap.formats[r - (GRID.FIRST - 1)]![c] = { ...w.format };
    }
    return writes.length;
  }
  /** Gate 10: сбой следующей записи ДАННЫХ (модель: Job не отработал / Sheets отказал). */
  failNextDataWrite = false;
  async batchWrite(data: WriteRange[]): Promise<number> {
    if (this.readonlyScope) throw new LoaderError('403 insufficient scope', 'SHEETS_API');
    const isLcdCommit = data.some((d) => d.range === 'LAST_CLOSED_DATE');
    if (!isLcdCommit && this.failNextDataWrite) { this.failNextDataWrite = false; throw new LoaderError('Sheets API 503 на записи данных', 'SHEETS_API'); }
    if (isLcdCommit && this.failLcdWrite === 'throw') throw new LoaderError('Sheets API 503 на записи LCD', 'SHEETS_API');
    this.writes.push(data);
    if (isLcdCommit && this.failLcdWrite === 'noop') return data.length;          // «успех» без применения
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
    if (!isLcdCommit && this.tamperLcdAfterDataWrite !== null) this.snap.namedLcd = this.tamperLcdAfterDataWrite;
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
    // Gate 10: данные — одним batchUpdate, LCD — ОТДЕЛЬНОЙ записью ПОСЛЕ перечитывания и QA.
    // До Gate 10 LCD уходил в том же пакете, что факты, — до проверки (так 23.09 закрылось 22.09 у Ozon).
    expect(sheets.writes).toHaveLength(2);
    expect(sheets.writes[0]!.map((w) => w.range)).not.toContain('LAST_CLOSED_DATE');
    expect(sheets.writes[1]!.map((w) => w.range)).toEqual(['LAST_CLOSED_DATE', `'WB_Юнит_2025'!${colA1(GRID.MIR)}${GRID.HDR}:${colA1(GRID.MIR)}${GRID.HDR}`]);
    expect(res.rowsLoaded).toBe(runner.journal[0]!.cellsPlanned);
    expect(runner.journal[0]).toMatchObject({ mode: 'WRITE', qaStatus: 'PASS', errorCode: null });
    expect(sheets.snap.namedLcd).toBe(isoToSerial(LCD));
    expect(sheets.snap.mirrorLcd).toBe(isoToSerial(LCD));
    expect(sheets.snap.grid[dayRow(0) - GRID.TOP]![GRID.B0 - 1 + OFFSET.logistics]).toBe(65.98);

    const res2 = await unitkaLoader(ctx(cfg), deps(runner, sheets));
    expect(res2.rowsLoaded).toBe(0);
    expect(sheets.writes, 'повтор: ни данных, ни второго коммита LCD').toHaveLength(2);
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
    expect(qa.checks.find((c) => c.name === 'STOCK_PROJECTION_FUTURE')).toMatchObject({ pass: true, count: 1 });
  });
  it('readSnapshot читает три диапазона и формулы', async () => {
    const sheets = new FakeSheets(snapshot());
    const s = await readSnapshot(sheets, 'WB_Юнит_2025', SEPT, GRID.NC, GRID.MIR);
    expect(s.grid).toHaveLength(GRID.MTD - GRID.TOP + 1);
    expect(s.namedLcd).toBe(isoToSerial(LCD));
    expect(s.mirrorRev).toBe(32.5256);
  });
});

describe('unitkaLoader — контракт формата закрытого дня (E3)', () => {
  const dimSheet = () => snapshot({
    lcdInSheet: '2026-09-09', futureStyleFrom: 9,  // как в Master 12.09: строки 10.09+ в «будущем» виде
    direct: (nm) => (nm === NM_IDS[0] ? 65.98 : 60.55), commission: (nm) => (nm === NM_IDS[0] ? 0.441234 : 0.45578),
  });
  it('SHADOW: считает FORMAT_CHANGE только для закрытых дней 10.09 (2 ставки × 24 блока; хранение 10.09 — GAP, вне контракта) и не пишет', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(dimSheet(), true);
    await unitkaLoader(ctx(mkConfig('shadow', true)), deps(runner, sheets));
    const j = JSON.parse(String(runner.journal[0]!.qaJson));
    expect(j.plan.format_cells_planned).toBe(2 * 24);
    expect(sheets.formatWrites).toEqual([]);
    expect(runner.journal[0]!.qaStatus).toBe('SHADOW_DIFF');
  });
  it('PROD: приводит закрытые 10.09 к эталону строки 737, будущие 11.09+ остаются «будущими», повтор — 0', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(dimSheet());
    const cfg = mkConfig('prod', true);
    await unitkaLoader(ctx(cfg), deps(runner, sheets));
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS' });
    expect(sheets.formatWrites).toHaveLength(1);
    const j = JSON.parse(String(runner.journal[0]!.qaJson));
    expect(j.format_cells_written).toBe(48);
    expect(j.checks.find((c: { name: string }) => c.name === 'CLOSED_FORMAT_CONTRACT')).toMatchObject({ pass: true, count: 0 });
    // 10.09 (индекс 9) закрыт → чёрный шрифт ставок; хранение 10.09 — GAP, его «будущий» вид не тронут;
    // 11.09 (индекс 10) — по-прежнему «будущий»
    const st = GRID.B0;
    expect(sheets.snap.formats[9]![st - 1 + OFFSET.logistics]!.fg).toEqual(BLACK);
    expect(sheets.snap.formats[9]![st - 1 + OFFSET.storage]!.bg).toBeNull();
    expect(sheets.snap.formats[10]![st - 1 + OFFSET.logistics]!.fg).toEqual(DIM);
    expect(sheets.snap.formats[10]![st - 1 + OFFSET.storage]!.bg).toBeNull();
    // повтор — идемпотентно
    await unitkaLoader(ctx(cfg), deps(runner, sheets));
    expect(sheets.formatWrites).toHaveLength(1);
    expect(JSON.parse(String(runner.journal[1]!.qaJson)).plan.format_cells_planned).toBe(0);
    expect(runner.journal[1]).toMatchObject({ cellsPlanned: 0, qaStatus: 'PASS' });
  });
  it('PROD: хранение 10.09 появилось в источнике → значение и формат закрытого дня в одном прогоне', async () => {
    const runner = new FakeRunner();
    runner.storageFor10 = 7.77;
    const sheets = new FakeSheets(dimSheet());
    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS' });
    const st = GRID.B0;
    expect(sheets.snap.grid[dayRow(9) - GRID.TOP]![st - 1 + OFFSET.storage]).toBe(7.77);
    expect(sheets.snap.formats[9]![st - 1 + OFFSET.storage]!.bg).toEqual(AUTO_BG);
    expect(JSON.parse(String(runner.journal[0]!.qaJson)).format_cells_written).toBe(72);
  });
  it('PROD: формат-запись не применилась → CLOSED_FORMAT_CONTRACT FAIL, код FORMAT_CONTRACT', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(dimSheet());
    sheets.formatWrite = async (_id, w) => { sheets.formatWrites.push(w); return w.length; }; // «применил», но лист не изменился
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'FORMAT_CONTRACT' });
  });
});

/* ───────────────────────── Integrity Guard V1 (Phase 1C1) ───────────────────────── */

import { runCli, type CliDeps } from '../src/cli.js';
import { EXIT_OK } from '../src/errors.js';
import { loadConfig } from '../src/config.js';
import type { IntegritySummary } from '../src/loaders/unitka/integrity.js';

/** Строки V_UNITKA_INTEGRITY из той же фикстуры; одна строка — «заказ есть, цены нет». */
function integrityRows(lcd = LCD): Array<Record<string, unknown>> {
  return facts(lcd).map((r) => {
    const bad = r.nmId === NM_IDS[4] && r.date === '2026-09-05';
    return {
      marketplace: 'WB', nm_id: r.nmId, internal_sku: `SKU-${r.nmId}`, product_name: 'Товар', day: { value: r.date },
      last_closed_date: { value: lcd }, orders_unitka: bad ? 1 : r.orders, cancels_unitka: r.cancels, orders_source: r.ordersSource,
      factual_order_price: bad ? null : String(r.price), orders_funnel: r.orders, fact_order_rows: bad ? null : 1,
      fact_order_qty: bad ? null : r.orders, observed_price_diagnostic: bad ? '777' : null,
      observed_price_at: bad ? { value: '2026-09-05T20:40:00Z' } : null,
      storage_value: r.storage === null ? null : String(r.storage), storage_date_covered: r.storage !== null,
      price_state: bad ? 'MISSING_WITH_ACTIVITY' : 'PRESENT', divergence_class: bad ? 'ONLY_FUNNEL' : 'EXACT',
    };
  });
}

const withIntegrity = (cfg: Config, mode: string, extra: Partial<Config> = {}): Config =>
  ({ ...cfg, unitkaIntegrityMode: mode, unitkaStorageDueMsk: '12:15', unitkaIntegrityBudgetMs: 90_000, ...extra } as Config);
const qaOf = (runner: FakeRunner, i = 0): { integrity?: IntegritySummary } => JSON.parse(String(runner.journal[i]!.qaJson)) as { integrity?: IntegritySummary };

describe('Integrity Guard V1 — интеграция с Engine', () => {
  it('QA_PASS_PLUS_DATA_ERROR: prod observe → qa_status PASS, integrity DATA_ERROR, прогон не падает', async () => {
    const runner = new FakeRunner();
    runner.integrity = { rows: () => integrityRows() };
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
    const res = await unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'observe')), deps(runner, sheets));
    expect(res.rowsLoaded).toBeGreaterThan(0);
    expect(runner.journal[0]).toMatchObject({ mode: 'WRITE', qaStatus: 'PASS', errorCode: null });
    const integ = qaOf(runner).integrity;
    expect(integ).toMatchObject({ status: 'DATA_ERROR', mode: 'observe', phase: 'POST_WRITE', cogs_source: 'UNAVAILABLE' });
    expect(integ!.error_keys).toContain(`2026-09-05/${NM_IDS[4]}/PRICE_MISSING_WITH_ORDERS`);
    expect(integ!.financially_invalid_rows).toBe(1);
    expect(integ!.counts.EXPECTED_DELAY).toBe(1); // хранение 10.09 в 10:00 МСК 11.09
  });

  it('факт-запись идентична режиму off: Guard ничего не меняет в листе', async () => {
    const run = async (mode: string): Promise<WriteRange[][]> => {
      const runner = new FakeRunner();
      runner.integrity = { rows: () => integrityRows() };
      const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
      await unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), mode)), deps(runner, sheets));
      return sheets.writes;
    };
    expect(JSON.stringify(await run('observe'))).toBe(JSON.stringify(await run('off')));
  });

  it('off (по умолчанию): ни одного запроса к вью Guard, в qa_json нет integrity', async () => {
    const runner = new FakeRunner();
    runner.integrity = { rows: () => integrityRows() };
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets)); // mkConfig без полей Guard
    expect(runner.integrityQueries).toBe(0);
    expect(qaOf(runner).integrity).toBeUndefined();
  });

  it('сбой Guard (вью нет) в observe → integrity SYSTEM_ERROR, qa PASS, прогон НЕ падает', async () => {
    const runner = new FakeRunner();
    runner.integrity = { throwFacts: true };
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
    await expect(unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'observe')), deps(runner, sheets))).resolves.toBeDefined();
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS', errorCode: null });
    expect(qaOf(runner).integrity).toMatchObject({ status: 'SYSTEM_ERROR', subsystem_failure: { code: 'INTEGRITY_SUBSYSTEM_FAILURE' } });
  });

  it('enforce (зарезервирован): падает ТОЛЬКО на сбое Guard; DATA_ERROR не бросает', async () => {
    const r1 = new FakeRunner();
    r1.integrity = { throwFacts: true };
    await expect(unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'enforce')), deps(r1, new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' })))))
      .rejects.toMatchObject({ code: 'INTEGRITY_SUBSYSTEM_FAILURE' });
    expect(r1.journal).toHaveLength(1); // ровно одна строка журнала на прогон
    expect(r1.journal[0]).toMatchObject({ qaStatus: 'PASS', errorCode: 'INTEGRITY_SUBSYSTEM_FAILURE' });
    const r2 = new FakeRunner();
    r2.integrity = { rows: () => integrityRows() };
    await expect(unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'enforce')), deps(r2, new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' })))))
      .resolves.toBeDefined();
    expect(qaOf(r2).integrity!.status).toBe('DATA_ERROR');
    expect(r2.journal).toHaveLength(1);
    expect(r2.journal[0]).toMatchObject({ errorCode: null });
  });

  it('SHADOW observe: оценка PRE_WRITE, записи нет', async () => {
    const runner = new FakeRunner();
    runner.integrity = { rows: () => integrityRows() };
    const sheets = new FakeSheets(snapshot(), true);
    await unitkaLoader(ctx(withIntegrity(mkConfig('shadow', false), 'observe')), deps(runner, sheets));
    expect(sheets.writes).toHaveLength(0);
    expect(qaOf(runner).integrity).toMatchObject({ status: 'DATA_ERROR', phase: 'PRE_WRITE' });
  });

  it('журнал issue отложен (D4): Guard не делает ни одной записи, кроме строки журнала прогона', async () => {
    const runner = new FakeRunner();
    runner.integrity = { rows: () => integrityRows() };
    await unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'observe')), deps(runner, new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }))));
    expect(runner.otherWrites).toEqual([]);
    expect(runner.journal).toHaveLength(1);
  });

  it('копия COGS свежая → cogs_source AVAILABLE; устаревшая → STALE + WARNING-issue; нет копии → UNAVAILABLE + WARNING-issue', async () => {
    const cogsRows = (publishedAt: string): Array<Record<string, unknown>> => NM_IDS.map((nm) => ({
      nm_id: nm, internal_sku: `SKU-${nm}`, day: { value: '2026-09-01' }, cogs_interval_count: 1, canonical_cogs: '100',
      snapshot_published_at: { value: publishedAt }, snapshot_run_id: 'pub-1',
    }));
    const run = async (integ: FakeRunner['integrity']): Promise<IntegritySummary> => {
      const runner = new FakeRunner();
      runner.integrity = { rows: () => integrityRows(), ...integ };
      await unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'observe')), deps(runner, new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }))));
      return qaOf(runner).integrity!;
    };
    const fresh = await run({ throwCogs: false, cogsRows: cogsRows('2026-09-11T06:50:00Z') });
    expect(fresh.cogs_source).toBe('AVAILABLE');
    expect(fresh.issue_codes.COGS_SNAPSHOT_STALE).toBeUndefined();
    const stale = await run({ throwCogs: false, cogsRows: cogsRows('2026-09-09T06:50:00Z') });
    expect(stale).toMatchObject({ cogs_source: 'STALE', issue_codes: { COGS_SNAPSHOT_STALE: 1 } });
    expect(stale.issue_codes.COGS_ZERO_OR_MISSING).toBeUndefined();
    const none = await run({});
    expect(none).toMatchObject({ cogs_source: 'UNAVAILABLE', issue_codes: { COGS_SNAPSHOT_UNAVAILABLE: 1 } });
  });

  it('F3: бюджет времени — серверный jobTimeoutMs ≤ бюджета; «зависший» запрос → SYSTEM_ERROR/INTEGRITY_TIME_BUDGET_EXCEEDED, журнал записан, прогон не падает', async () => {
    const clock = { t: Date.parse('2026-09-11T07:00:00Z') };
    const runner = new FakeRunner();
    runner.integrity = { rows: () => integrityRows(), hangMs: 91_000, clock };
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
    const d: UnitkaDeps = { makeRunner: () => runner, makeSheets: () => sheets, now: () => new Date(clock.t) };
    await expect(unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'observe')), d)).resolves.toBeDefined();
    expect(runner.jobTimeouts.every((t) => typeof t === 'number' && t > 0 && t <= 90_000)).toBe(true);
    expect(runner.journal).toHaveLength(1);
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS', errorCode: null });
    // Gate 10: факт-запись и ОТДЕЛЬНЫЙ коммит LCD — оба состоялись, ни один не откатывался
    expect(sheets.writes).toHaveLength(2);
    expect(sheets.writes[1]!.map((w) => w.range)).toContain('LAST_CLOSED_DATE');
    expect(qaOf(runner).integrity).toMatchObject({ status: 'SYSTEM_ERROR', subsystem_failure: { code: 'INTEGRITY_TIME_BUDGET_EXCEEDED' } });
  });

  it('Engine упал до оценки (SOURCE_STALE) при observe → integrity SYSTEM_ERROR в журнале, код ошибки прежний', async () => {
    const runner = new FakeRunner({ lcd: '2026-09-05', d1: '2026-09-11' });
    runner.integrity = { rows: () => integrityRows() };
    await expect(unitkaLoader(ctx(withIntegrity(mkConfig('shadow', false), 'observe')), deps(runner, new FakeSheets(snapshot(), true))))
      .rejects.toMatchObject({ code: 'SOURCE_STALE' });
    expect(runner.journal[0]).toMatchObject({ errorCode: 'SOURCE_STALE' });
    expect(qaOf(runner).integrity).toMatchObject({ status: 'SYSTEM_ERROR', subsystem_failure: { code: 'SOURCE_STALE' } });
  });
});

describe('Integrity Guard V1 — конфигурация и код выхода', () => {
  const env = (o: Record<string, string>): NodeJS.ProcessEnv => ({ GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', ENVIRONMENT: 'prod', ...o });
  it('UNITKA_INTEGRITY_MODE: по умолчанию off; регистр не важен; мусор → off без ConfigError', () => {
    expect(loadConfig(env({})).unitkaIntegrityMode).toBe('off');
    expect(loadConfig(env({ UNITKA_INTEGRITY_MODE: 'OBSERVE' })).unitkaIntegrityMode).toBe('observe');
    const bad = loadConfig(env({ UNITKA_INTEGRITY_MODE: 'observ' }));
    expect(bad.unitkaIntegrityMode).toBe('off');
    expect(bad.unitkaIntegrityModeInvalid).toBe('observ');
  });
  it('UNITKA_STORAGE_DUE_MSK: мусор → 12:15; UNITKA_INTEGRITY_BUDGET_MS: вне 5–300 с → 90 с', () => {
    expect(loadConfig(env({ UNITKA_STORAGE_DUE_MSK: '25:99' })).unitkaStorageDueMsk).toBe('12:15');
    expect(loadConfig(env({ UNITKA_STORAGE_DUE_MSK: '12:40' })).unitkaStorageDueMsk).toBe('12:40');
    expect(loadConfig(env({})).unitkaIntegrityBudgetMs).toBe(90_000);
    expect(loadConfig(env({ UNITKA_INTEGRITY_BUDGET_MS: '60000' })).unitkaIntegrityBudgetMs).toBe(60_000);
    expect(loadConfig(env({ UNITKA_INTEGRITY_BUDGET_MS: '1' })).unitkaIntegrityBudgetMs).toBe(90_000);
    expect(loadConfig(env({ UNITKA_INTEGRITY_BUDGET_MS: 'abc' })).unitkaIntegrityBudgetMs).toBe(90_000);
  });

  it('DATA_ERROR_EXIT_CODE = 0: runCli с реальным unitkaLoader и DATA_ERROR → EXIT_OK', async () => {
    const runner = new FakeRunner();
    runner.integrity = { rows: () => integrityRows() };
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }));
    const cliDeps: CliDeps = {
      makeStore: () => ({ acquire: async () => ({ acquired: true, runId: 'r', recovered: false }), finalize: async () => {} }),
      runHandler: (_spec, c) => unitkaLoader({ ...c, logger: silentLogger, config: withIntegrity(c.config, 'observe') }, deps(runner, sheets)),
      nowMs: () => 0,
    };
    const code = await runCli(['node', 'cli.js', 'unitka'], env({ LOG_LEVEL: 'error', UNITKA_WRITE_ENABLED: '1' }), cliDeps);
    expect(code).toBe(EXIT_OK);
    expect(qaOf(runner).integrity!.status).toBe('DATA_ERROR');
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS' });
  });

  it('⚠ DRY_RUN=1 НЕ защищает от записи: для unitka handler исполняется (no-write — только UNITKA_WRITE_ENABLED=0 / shadow)', async () => {
    let handlerCalls = 0;
    const cliDeps: CliDeps = {
      makeStore: () => { throw new Error('lease не должен браться в DRY_RUN'); },
      runHandler: async () => { handlerCalls++; return { rowsFetched: 0, rowsLoaded: 0 }; },
      nowMs: () => 0,
    };
    const code = await runCli(['node', 'cli.js', 'unitka'], env({ LOG_LEVEL: 'error', DRY_RUN: '1', UNITKA_WRITE_ENABLED: '1' }), cliDeps);
    expect(code).toBe(EXIT_OK);
    expect(handlerCalls).toBe(1); // unitka не prodOnly → DRY_RUN вызывает настоящий handler
  });
});

/* ═══════════════════════ Gate 10: AUTO-LCD WB в настоящем оркестраторе ═══════════════════════ */

const serial = (iso: string): number => isoToSerial(iso);
/** Блок цикла ZZ_CONFIG!A29:B34 ровно в том виде, в каком его отдаёт Sheets API. */
const lifecycleBlock = (wbMode: string, wbManual: unknown = ''): unknown[][] => [
  ['ЖИЗНЕННЫЙ ЦИКЛ — AUTO-LCD', ''], ['OZON_LAST_CLOSED_DATE', 46287], ['WB_LCD_MODE', wbMode],
  ['WB_MANUAL_LCD', wbManual], ['OZON_LCD_MODE', 'AUTO'], ['OZON_MANUAL_LCD', ''],
];
const COMMITTED = '2026-09-01';
const freshBook = (): FakeSheets => new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: COMMITTED }));
const dataWrites = (s: FakeSheets) => s.writes.filter((w) => !w.some((d) => d.range === 'LAST_CLOSED_DATE'));
const lcdCommits = (s: FakeSheets) => s.writes.filter((w) => w.some((d) => d.range === 'LAST_CLOSED_DATE'));

describe('Gate 10 · WB AUTO-LCD: провал любой стадии оставляет LCD на месте', () => {
  it('SUCCESS: B2 сдвигается РОВНО один раз и только ПОСЛЕ записи и проверки; повтор — без мутаций', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(dataWrites(sheets)).toHaveLength(1);
    expect(lcdCommits(sheets)).toHaveLength(1);
    expect(sheets.writes.indexOf(lcdCommits(sheets)[0]!)).toBeGreaterThan(sheets.writes.indexOf(dataWrites(sheets)[0]!));
    expect(sheets.snap.namedLcd).toBe(serial(LCD));
    expect(sheets.snap.mirrorLcd).toBe(serial(LCD));
    const qa = JSON.parse(String(runner.journal[0]!.qaJson));
    expect(qa.lifecycle).toMatchObject({ mode: 'AUTO', committed_before: COMMITTED, candidate: LCD, commit: { code: 'LCD_COMMITTED' } });

    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(sheets.writes, 'LCD_DUPLICATE_ADVANCE=0, данных тоже нет').toHaveLength(2);
    expect(JSON.parse(String(runner.journal[1]!.qaJson)).lifecycle.commit.code).toBe('LCD_NOT_ADVANCED');
  });

  it('SOURCE_STALE → ни одной записи, LCD прежний (прежний код, прежний порядок проверок)', async () => {
    const runner = new FakeRunner({ d1: '2026-09-14' }); const sheets = freshBook();
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'SOURCE_STALE' });
    expect(sheets.writes).toEqual([]);
    expect(sheets.snap.namedLcd).toBe(serial(COMMITTED));
  });

  it('GAP_IN_REQUIRED_COVERAGE → кандидат останавливается ПЕРЕД дыркой, день после неё не пишется', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    // воронка отработала по 09.09 включительно (её окно покрывает 03..09), mart — по 11.09
    runner.coverage = [
      ...['02', '03', '04', '05', '06', '07', '08', '09'].map((d) => ({ loader_name: 'funnel', logical_period: `2026-09-${d}` })),
      ...['02', '03', '04', '05', '06', '07', '08', '09', '10', '11'].map((d) => ({ loader_name: 'mart', logical_period: `2026-09-${d}` })),
    ];
    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(sheets.snap.namedLcd).toBe(serial('2026-09-09'));
    const qa = JSON.parse(String(runner.journal[0]!.qaJson));
    expect(qa.lifecycle).toMatchObject({ candidate: '2026-09-09', gap_at: '2026-09-10', canonical_ceiling: LCD });
    expect(sheets.snap.grid[dayRow(9) - GRID.TOP]![GRID.B0 - 1 + OFFSET.views], 'факт 10.09 не опубликован').toBe('');
  });

  it('SHEET_WRITE_FAILED → LCD прежний', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.failNextDataWrite = true;
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'SHEETS_API' });
    expect(lcdCommits(sheets)).toHaveLength(0);
    expect(sheets.snap.namedLcd).toBe(serial(COMMITTED));
  });

  it('READBACK/QA FAILED → LCD прежний: коммит не начинается', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: COMMITTED }), false, { breakSummaryAfterWrite: true });
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'SUMMARY_MISMATCH' });
    expect(lcdCommits(sheets)).toHaveLength(0);
    expect(sheets.snap.namedLcd).toBe(serial(COMMITTED));
  });

  it('INTEGRITY_FAILED (enforce + сбой Guard) → LCD прежний, данные записаны', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    runner.integrity = { throwFacts: true };
    await expect(unitkaLoader(ctx(withIntegrity(mkConfig('prod', true), 'enforce')), deps(runner, sheets))).rejects.toMatchObject({ code: 'INTEGRITY_SUBSYSTEM_FAILURE' });
    expect(dataWrites(sheets)).toHaveLength(1);
    expect(lcdCommits(sheets)).toHaveLength(0);
    expect(sheets.snap.namedLcd).toBe(serial(COMMITTED));
  });

  it('LCD_COMMIT_CONFLICT: B2 изменили между планированием и коммитом → чужое значение НЕ перетирается', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.tamperLcdAfterDataWrite = serial('2026-09-05');
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'LCD_COMMIT_CONFLICT' });
    expect(lcdCommits(sheets)).toHaveLength(0);
    expect(sheets.snap.namedLcd).toBe(serial('2026-09-05'));
  });

  it('LCD_WRITE_FAILED: данные опубликованы, LCD прежний; повтор коммитит РОВНО один раз без повторной записи данных', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.failLcdWrite = 'throw';
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'LCD_WRITE_FAILED' });
    expect(dataWrites(sheets)).toHaveLength(1);
    expect(sheets.snap.namedLcd).toBe(serial(COMMITTED));

    sheets.failLcdWrite = null;
    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(dataWrites(sheets), 'повтор не переписывает данные').toHaveLength(1);
    expect(lcdCommits(sheets)).toHaveLength(1);
    expect(sheets.snap.namedLcd).toBe(serial(LCD));

    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(sheets.writes).toHaveLength(2);
  });

  it('API ответил успехом, но LCD не лёг → LCD_WRITE_FAILED, а не «закоммичено»', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.failLcdWrite = 'noop';
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'LCD_WRITE_FAILED' });
    expect(sheets.snap.namedLcd).toBe(serial(COMMITTED));
  });
});

describe('Gate 10 · WB: регрессия инцидента 23.09 на уровне писателя', () => {
  it('источники ГОТОВЫ, запись не состоялась → LCD НЕ двигается; тот же кандидат, успех → двигается', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.failNextDataWrite = true;
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toBeDefined();
    expect(sheets.snap.namedLcd, 'LCD_ADVANCE=NO').toBe(serial(COMMITTED));

    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(sheets.snap.namedLcd, 'LCD_ADVANCE=YES').toBe(serial(LCD));
  });
});

describe('Gate 10 · WB MANUAL: явный выбор кандидата, а не «force everything»', () => {
  it('MANUAL 05.09 → коммит ровно 05.09, событие MANUAL_OVERRIDE_ACTIVE', async () => {
    const lines: Array<{ event: string; fields: Record<string, unknown> }> = [];
    const logger = { info: (e: string, f: Record<string, unknown>) => lines.push({ event: e, fields: f }), warn: (e: string, f: Record<string, unknown>) => lines.push({ event: e, fields: f }), error() {}, debug() {}, child() { return logger; } } as unknown as Logger;
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.lifecycle = lifecycleBlock('MANUAL', serial('2026-09-05'));
    await unitkaLoader({ ...ctx(mkConfig('prod', true)), logger }, deps(runner, sheets));
    expect(sheets.snap.namedLcd).toBe(serial('2026-09-05'));
    expect(lines.some((l) => l.event === 'unitka_lifecycle' && l.fields.lifecycle_event === 'MANUAL_OVERRIDE_ACTIVE' && l.fields.candidate === '2026-09-05')).toBe(true);
  });

  it('MANUAL-дата не дата → MANUAL_LCD_INVALID, ни одной записи', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.lifecycle = lifecycleBlock('MANUAL', 'вчера');
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'MANUAL_LCD_INVALID' });
    expect(sheets.writes).toEqual([]);
  });

  it('MANUAL позже готовности источников → отказ: override не создаёт данных', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.lifecycle = lifecycleBlock('MANUAL', serial('2026-09-11'));      // канон 10.09
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'MANUAL_LCD_INVALID' });
    expect(sheets.writes).toEqual([]);
  });

  it('MANUAL-откат назад → MANUAL_LCD_REGRESSION: опубликованное не стирается', async () => {
    const runner = new FakeRunner(); const sheets = new FakeSheets(snapshot({ lcdInSheet: '2026-09-10' }));
    sheets.lifecycle = lifecycleBlock('MANUAL', serial('2026-09-03'));
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'MANUAL_LCD_REGRESSION' });
    expect(sheets.writes).toEqual([]);
  });

  it('MANUAL НЕ обходит QA: провал проверки → LCD прежний', async () => {
    const runner = new FakeRunner();
    const sheets = new FakeSheets(snapshot({ applyFacts: false, lcdInSheet: COMMITTED }), false, { breakSummaryAfterWrite: true });
    sheets.lifecycle = lifecycleBlock('MANUAL', serial('2026-09-05'));
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'SUMMARY_MISMATCH' });
    expect(sheets.snap.namedLcd).toBe(serial(COMMITTED));
  });

  it('блок цикла переставлен (DRIFT) → LIFECYCLE_CONFIG_INVALID до чтения фактов', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    const b = lifecycleBlock('AUTO'); [b[2], b[4]] = [b[4]!, b[2]!];
    sheets.lifecycle = b;
    await expect(unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets))).rejects.toMatchObject({ code: 'LIFECYCLE_CONFIG_INVALID' });
    expect(sheets.writes).toEqual([]);
    expect(runner.coverageQueries).toBe(0);
  });

  it('возврат в AUTO продолжает цикл от закоммиченного MANUAL-состояния', async () => {
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.lifecycle = lifecycleBlock('MANUAL', serial('2026-09-05'));
    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    sheets.lifecycle = lifecycleBlock('AUTO');
    await unitkaLoader(ctx(mkConfig('prod', true)), deps(runner, sheets));
    expect(sheets.snap.namedLcd).toBe(serial(LCD));
  });
});

describe('Gate 10 · WB: долгое удержание — видимый инцидент, а не молчание', () => {
  const recLogger = (lines: Array<{ level: string; event: string; fields: Record<string, unknown> }>): Logger => {
    const l = {
      info: (e: string, f: Record<string, unknown>) => lines.push({ level: 'info', event: e, fields: f }),
      warn: (e: string, f: Record<string, unknown>) => lines.push({ level: 'warn', event: e, fields: f }),
      error: (e: string, f: Record<string, unknown>) => lines.push({ level: 'error', event: e, fields: f }),
      debug() {}, child() { return l; },
    };
    return l as unknown as Logger;
  };

  it('AUTO придержан дыркой > 2 суток: безопасная часть опубликована, событие ERROR с кодом ловит алерт', async () => {
    const lines: Array<{ level: string; event: string; fields: Record<string, unknown> }> = [];
    const runner = new FakeRunner(); const sheets = freshBook();
    // воронка отработала только по 06.09 (окно 31.08..06.09), mart — по 11.09 → кандидат 06.09, D-1 = 11.09
    runner.coverage = [
      ...['02', '03', '04', '05', '06'].map((d) => ({ loader_name: 'funnel', logical_period: `2026-09-${d}` })),
      ...['02', '03', '04', '05', '06', '07', '08', '09', '10', '11'].map((d) => ({ loader_name: 'mart', logical_period: `2026-09-${d}` })),
    ];
    await unitkaLoader({ ...ctx(mkConfig('prod', true)), logger: recLogger(lines) }, deps(runner, sheets));
    expect(sheets.snap.namedLcd).toBe(serial('2026-09-06'));
    const held = lines.find((l) => l.fields.lifecycle_event === 'LCD_NOT_ADVANCED' && l.fields.code === 'LCD_HELD_BY_COVERAGE_GAP');
    expect(held?.level, 'уровень ERROR и поле code — ровно то, что ловит алерт-политика').toBe('error');
    expect(held?.fields).toMatchObject({ lcd: '2026-09-06', held_days: 5, gap_at: '2026-09-07' });
    expect(runner.journal[0]!.errorCode, 'прогон не падает: опубликовано то, что безопасно').toBeNull();
    // событие не затирает своё имя полем message (известный долг логгера)
    expect(held?.event).toBe('unitka_lifecycle');
    expect('message' in (held?.fields ?? {})).toBe(false);
  });

  it('MANUAL-удержание — выбор владельца: алерт-событие НЕ генерируется', async () => {
    const lines: Array<{ level: string; event: string; fields: Record<string, unknown> }> = [];
    const runner = new FakeRunner(); const sheets = freshBook();
    sheets.lifecycle = lifecycleBlock('MANUAL', serial('2026-09-05'));
    await unitkaLoader({ ...ctx(mkConfig('prod', true)), logger: recLogger(lines) }, deps(runner, sheets));
    expect(lines.some((l) => l.fields.code === 'LCD_HELD_BY_COVERAGE_GAP')).toBe(false);
    expect(lines.filter((l) => l.level === 'error')).toEqual([]);
  });
});
