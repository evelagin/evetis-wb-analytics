/**
 * UNITKA CALENDAR V2 (Phase 2B) — загрузчики на фейковой книге:
 *   • суточный Engine: MONTH_SECTION_MISSING до записи, предпроверка NEXT_MONTH_SECTION_MISSING не валит
 *     прогон, structureWrite не вызывается никогда;
 *   • unitka-month-prep: по умолчанию ПЛАН (без записи), запись — только prod + UNITKA_MONTH_PREP_WRITE=1,
 *     readonly-шлюз отказывает до HTTP, повтор — NO_CHANGE.
 * Ни Sheets, ни BigQuery.
 */
import { describe, it, expect } from 'vitest';
import { unitkaLoader, type UnitkaDeps } from '../src/loaders/unitka/index.js';
import { unitkaMonthPrepLoader, monthPrepWriteAllowed } from '../src/loaders/unitka/prep.js';
import { planMonthPrep } from '../src/loaders/unitka/monthprep.js';
import { SheetsRest, type SheetsGateway, type WriteRange, type FormatWrite, type FormatGrid, type SheetMeta, type StructureRequest } from '../src/loaders/unitka/sheets.js';
import type { QueryRunner } from '../src/loaders/mart/bq.js';
import { geometryAt } from '../src/loaders/unitka/calendar.js';
import { classifyCogsSnapshot } from '../src/loaders/unitka/integrity.js';
import { LoaderError } from '../src/errors.js';
import type { CellValue } from '../src/loaders/unitka/model.js';
import type { Snapshot } from '../src/loaders/unitka/plan.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Config } from '../src/config.js';
import type { Logger } from '../src/logging.js';
import { loadConfig } from '../src/config.js';
import { LOADERS } from '../src/loaders/registry.js';
import { sectionFromSpec, septemberSpec, applyPlan, SEPT_NMS, NEW_NM, WIDTH_SEPT } from './unitka_calendar_fixture.js';

interface LogLine { level: string; event: string; fields: Record<string, unknown> }
function recordingLogger(lines: LogLine[]): Logger {
  const mk = (): Logger => ({
    info: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'info', event, fields }); },
    warn: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'warn', event, fields }); },
    error: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'error', event, fields }); },
    child: () => mk(),
  } as unknown as Logger);
  return mk();
}

const baseConfig = (environment: 'shadow' | 'prod', over: Partial<Config> = {}): Config => ({
  ...loadConfig({ ENVIRONMENT: environment, GCP_PROJECT_ID: 'proj', BQ_RAW_DATASET: 'wb_raw', BQ_LOCATION: 'EU', LOADER_NAME: 'unitka', GIT_SHA: 'test', IMAGE_DIGEST: 'sha256:test' } as Record<string, string>),
  ...over,
});
const ctx = (config: Config, lines: LogLine[] = []): LoaderContext =>
  ({ config, logger: recordingLogger(lines), logicalPeriod: '2026-09-28T10', targetDate: '2026-09-28T10', runId: 'run-prep' });

/** Книга из секций: чтение по диапазонам, как Sheets API. */
class BookSheets implements SheetsGateway {
  structureWrites: StructureRequest[][] = [];
  batchWrites = 0;
  onStructureWrite: ((req: StructureRequest[]) => void) | null = null;
  constructor(public sections: Snapshot[], public rowCount: number, public columnCount: number, private readonly readonlyScope: boolean) {}
  private byTop(row: number): Snapshot { const s = this.sections.find((x) => x.geometry.topRow === row); if (!s) throw new Error(`нет секции ${row}`); return s; }
  private byFirst(row: number): Snapshot { const s = this.sections.find((x) => x.geometry.firstDailyRow === row); if (!s) throw new Error(`нет секции ${row}`); return s; }
  async readSheetMeta(): Promise<SheetMeta> { return { sheetId: 739487431, rowCount: this.rowCount, columnCount: this.columnCount }; }
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    return ranges.map((r) => {
      if (r === 'LAST_CLOSED_DATE') return [[46283]];
      if (/!WB736:WB737$/.test(r)) return [[46283], [32.136]];
      if (/!R45$/.test(r)) return [[240]];
      const colA = /!A1:A(\d+)$/.exec(r);
      if (colA) {
        const a: CellValue[][] = Array.from({ length: Number(colA[1]) }, () => [null]);
        for (const s of this.sections) a[s.geometry.topRow - 1] = [s.grid[0]![0]!];
        return a;
      }
      const g = /!A(\d+):[A-Z]+\d+$/.exec(r);
      if (g) return this.byTop(Number(g[1])).grid;
      throw new Error(`неожиданный диапазон ${r}`);
    });
  }
  async readFormulas(range: string): Promise<CellValue[][]> { return this.byFirst(Number(/!A(\d+):/.exec(range)![1])).formulas; }
  async readFormats(): Promise<FormatGrid> { return { sheetId: 739487431, rows: [] }; }
  async batchWrite(data: WriteRange[]): Promise<number> { this.batchWrites += data.length || 1; throw new LoaderError('не ожидалось', 'TEST'); }
  async formatWrite(sheetId: number, writes: FormatWrite[]): Promise<number> { throw new LoaderError(`не ожидалось: ${sheetId}/${writes.length}`, 'TEST'); }
  async structureWrite(requests: StructureRequest[]): Promise<number> {
    if (this.readonlyScope) throw new LoaderError('структурная запись на readonly-шлюзе запрещена', 'STRUCTURE_WRITE_FORBIDDEN');
    this.structureWrites.push(requests);
    this.onStructureWrite?.(requests);
    return requests.length;
  }
}

/** BigQuery: LCD, популяция, копия COGS, журнал Engine. */
class PrepRunner implements QueryRunner {
  readonly projectId = 'proj';
  queries: string[] = [];
  constructor(private readonly o: { lcd: string; cogs?: number | null; extraActive?: number[] }) {}
  async query<T = Record<string, unknown>>(sql: string): Promise<T[]> {
    this.queries.push(sql);
    if (sql.includes('V_UNITKA_LAST_CLOSED_DATE')) return [{ last_closed_date: { value: this.o.lcd }, d1_msk: { value: '2026-09-28' } }] as T[];
    if (sql.includes('V_UNITKA_SOURCE_FRESHNESS')) return [] as T[];
    if (sql.includes('REF_SKU_MASTER')) return [...SEPT_NMS, ...(this.o.extraActive ?? [NEW_NM])].map((n) => ({ nm_id: n, product_name_short: n === NEW_NM ? 'Набор анти-акне пудра+сыворотка+крем' : `Товар ${n}` })) as T[];
    if (sql.includes('V_UNITKA_COGS_CANONICAL')) {
      return [{ nm_id: NEW_NM, internal_sku: 'EVT-SET', day: { value: '2026-09-27' }, cogs_interval_count: 1, canonical_cogs: this.o.cogs === undefined ? '426.735' : this.o.cogs, snapshot_published_at: { value: '2026-09-28T06:50:02Z' }, snapshot_run_id: 'run-cogs-1' }] as T[];
    }
    if (sql.includes('UNITKA_ENGINE_RUNS')) return [] as T[];
    throw new Error(`неожиданный SQL в тесте: ${sql.slice(0, 60)}`);
  }
}

const septemberOnly = (readonly: boolean) => new BookSheets([sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT)], 768, WIDTH_SEPT, readonly);
const depsOf = (runner: QueryRunner, sheets: BookSheets, made: boolean[] = []): UnitkaDeps => ({
  makeRunner: () => runner, makeSheets: (_c, ro) => { made.push(ro); return sheets; }, now: () => new Date('2026-09-28T07:00:00Z'),
});

describe('суточный Engine: поиск секции', () => {
  it('LCD 01.10, октября нет → MONTH_SECTION_MISSING до любой записи; structureWrite не вызывается', async () => {
    const sheets = septemberOnly(false);
    const runner = new PrepRunner({ lcd: '2026-10-01' });
    await expect(unitkaLoader(ctx(baseConfig('prod', { unitkaWriteEnabled: true })), depsOf(runner, sheets))).rejects.toMatchObject({ code: 'MONTH_SECTION_MISSING' });
    expect(sheets.batchWrites).toBe(0);
    expect(sheets.structureWrites).toHaveLength(0);
    expect(runner.queries.some((q) => q.includes('V_UNITKA_DAILY_FACT'))).toBe(false); // отказ раньше чтения фактов
  });
  it('суточный Engine не импортирует и не вызывает планировщик/структурную запись (статически)', async () => {
    const { readFileSync } = await import('node:fs');
    const src = readFileSync(new URL('../src/loaders/unitka/index.ts', import.meta.url), 'utf8');
    expect(src).not.toMatch(/structureWrite|monthprep|toStructureRequests/);
  });
});

describe('unitka-month-prep: гейты записи', () => {
  it('гейт: только prod И UNITKA_MONTH_PREP_WRITE=1; UNITKA_WRITE_ENABLED не даёт права', () => {
    expect(monthPrepWriteAllowed({ environment: 'prod', unitkaMonthPrepWrite: true })).toBe(true);
    expect(monthPrepWriteAllowed({ environment: 'prod', unitkaMonthPrepWrite: false })).toBe(false);
    expect(monthPrepWriteAllowed({ environment: 'shadow', unitkaMonthPrepWrite: true })).toBe(false);
    expect(monthPrepWriteAllowed({ environment: 'prod' })).toBe(false);
    expect(baseConfig('prod', { unitkaWriteEnabled: true }).unitkaMonthPrepWrite).toBe(false);
    expect(loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', UNITKA_MONTH_PREP_WRITE: '1' } as Record<string, string>).unitkaMonthPrepWrite).toBe(true);
    expect(loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', UNITKA_MONTH_PREP_WINDOW_DAYS: 'abc' } as Record<string, string>).unitkaMonthPrepWindowDays).toBe(5);
  });
  it('зарегистрирован отдельным загрузчиком, суточный unitka — прежний', () => {
    expect(LOADERS['unitka-month-prep']?.handler).toBe(unitkaMonthPrepLoader);
    expect(LOADERS.unitka?.handler).toBe(unitkaLoader);
  });
  it('по умолчанию (prod, без UNITKA_MONTH_PREP_WRITE) — ПЛАН: readonly-шлюз, 0 структурных записей', async () => {
    const sheets = septemberOnly(true);
    const made: boolean[] = [];
    const lines: LogLine[] = [];
    const res = await unitkaMonthPrepLoader(ctx(baseConfig('prod', { unitkaWriteEnabled: true }), lines), depsOf(new PrepRunner({ lcd: '2026-09-27' }), sheets, made));
    expect(res.rowsLoaded).toBe(0);
    expect(made).toEqual([true]);
    expect(sheets.structureWrites).toHaveLength(0);
    const planLine = lines.find((l) => l.event === 'unitka_month_prep_plan')!;
    expect(planLine.fields).toMatchObject({ status: 'PLAN_CREATE', target: '2026-10', append_rows: 35, append_columns: 36 });
    expect(lines.some((l) => l.event === 'unitka_month_prep_dry')).toBe(true);
  });
  it('shadow с UNITKA_MONTH_PREP_WRITE=1 — всё равно ПЛАН (readonly)', async () => {
    const sheets = septemberOnly(true);
    const made: boolean[] = [];
    await unitkaMonthPrepLoader(ctx(baseConfig('shadow', { unitkaMonthPrepWrite: true, unitkaMonthPrepTarget: '2026-10' })), depsOf(new PrepRunner({ lcd: '2026-09-27' }), sheets, made));
    expect(made).toEqual([true]);
    expect(sheets.structureWrites).toHaveLength(0);
  });
  it('readonly-шлюз SheetsRest отказывает в структурной записи ДО HTTP', async () => {
    await expect(new SheetsRest('x', true).structureWrite([{ appendDimension: {} }])).rejects.toMatchObject({ code: 'STRUCTURE_WRITE_FORBIDDEN' });
  });
  it('запись без явного UNITKA_MONTH_PREP_TARGET — отказ MONTH_PREP_TARGET_REQUIRED', async () => {
    const sheets = septemberOnly(false);
    await expect(unitkaMonthPrepLoader(ctx(baseConfig('prod', { unitkaMonthPrepWrite: true })), depsOf(new PrepRunner({ lcd: '2026-09-27' }), sheets)))
      .rejects.toMatchObject({ code: 'MONTH_PREP_TARGET_REQUIRED' });
    expect(sheets.structureWrites).toHaveLength(0);
  });
  it('канона COGS нового SKU нет → BLOCKED COGS_MISSING, 0 записей', async () => {
    const sheets = septemberOnly(false);
    await expect(unitkaMonthPrepLoader(ctx(baseConfig('prod', { unitkaMonthPrepWrite: true, unitkaMonthPrepTarget: '2026-10' })), depsOf(new PrepRunner({ lcd: '2026-09-27', cogs: null }), sheets)))
      .rejects.toMatchObject({ code: 'COGS_MISSING' });
    expect(sheets.structureWrites).toHaveLength(0);
  });
  it('WRITE (prod + флаг + цель): один batchUpdate, проверка после записи, повтор — NO_CHANGE', async () => {
    const sheets = septemberOnly(false);
    const runner = new PrepRunner({ lcd: '2026-09-27' });
    sheets.onStructureWrite = () => {
      // Фейк «исполняет» тот же план (как это сделал бы API) — для проверки после записи.
      const sept = sheets.sections[0]!;
      const cogs = classifyCogsSnapshot({ rows: [{ nmId: NEW_NM, internalSku: null, day: '2026-09-27', cogsIntervalCount: 1, canonicalCogs: 426.735 }], publishedAt: '2026-09-28T06:50:02Z', runId: 'run-cogs-1' }, new Date('2026-09-28T07:00:00Z'));
      const colA: CellValue[] = Array(768).fill(null); colA[734] = 'Сентябрь 2026';
      const plan = planMonthPrep({
        target: { year: 2026, month: 10 }, meta: { sheetId: 1, rowCount: 768, columnCount: 600 }, columnA: colA, predecessor: sept, existing: null,
        population: [...SEPT_NMS, NEW_NM].map((n) => ({ nmId: n, name: n === NEW_NM ? 'Набор анти-акне пудра+сыворотка+крем' : `Товар ${n}` })), cogs,
      });
      sheets.sections.push(applyPlan(plan, 636));
      sheets.rowCount = 803; sheets.columnCount = 636;
    };
    const cfg = baseConfig('prod', { unitkaMonthPrepWrite: true, unitkaMonthPrepTarget: '2026-10' });
    const res = await unitkaMonthPrepLoader(ctx(cfg), depsOf(runner, sheets));
    expect(sheets.structureWrites).toHaveLength(1);
    expect(sheets.structureWrites[0]![0]).toEqual({ appendDimension: { sheetId: 739487431, dimension: 'ROWS', length: 35 } });
    expect(res.rowsLoaded).toBeGreaterThan(0);
    expect(sheets.batchWrites).toBe(0);
    // Повтор того же месяца: NO_CHANGE, новых запросов нет.
    const again = await unitkaMonthPrepLoader(ctx(cfg), depsOf(runner, sheets));
    expect(again.rowsLoaded).toBe(0);
    expect(sheets.structureWrites).toHaveLength(1);
    expect(geometryAt({ year: 2026, month: 10 }, 769).mtdRow).toBe(802);
  });
});

describe('предпроверка следующего месяца в суточном Engine', () => {
  it('в окне и октября нет: предупреждение NEXT_MONTH_SECTION_MISSING пишется в лог; месяц не создаётся', async () => {
    const lines: LogLine[] = [];
    const sheets = septemberOnly(true);
    // Engine упадёт позже (синтетический лист без фактов/ставок), но предупреждение — ДО этого и без записи.
    await unitkaLoader(ctx(baseConfig('shadow'), lines), depsOf(new PrepRunner({ lcd: '2026-09-27' }), sheets)).catch(() => undefined);
    const w = lines.find((l) => l.event === 'unitka_next_month_section');
    expect(w?.level).toBe('warn');
    expect(w?.fields).toMatchObject({ code: 'NEXT_MONTH_SECTION_MISSING', next_month: '2026-10', days_left: 3 });
    expect(sheets.structureWrites).toHaveLength(0);
  });
});
