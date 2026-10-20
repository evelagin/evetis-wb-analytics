/**
 * ПРИЁМКА 20.10.2026 · ЗАДАЧА D — смена месяца октябрь → ноябрь 2026 (WB и Ozon), только фейки.
 *
 * Закрывает пробелы стенда, которых нет в unitka_wb_month_boundaries / unitka_ozon*:
 *   WB-1  прогон 01.11 (LCD 30.10 → 31.10): ноябрь НЕ создаётся, предупреждение NEXT_MONTH_SECTION_MISSING
 *         уровня warn (не error → алерт-политика его не ловит); прогон 02.11 создаёт ноябрь один раз; резерв — no-op;
 *   WB-2  то же с конфигурацией production (сверка controlled, целостность observe);
 *   WB-3  выбывший SKU в середине цепочки → прогон 02.11 падает CHAIN_GAP до любой записи, LCD стоит на 31.10;
 *   WB-4  новый активный SKU без канона COGS → COGS_MISSING до записи; с каноном → 26 блоков, вставка колонок,
 *         якорь REVERSE_LEG_RATE сдвигается, LCD коммитится;
 *   WB-5  ручные поля октября (СПП, блогеры) не трогаются созданием ноября; ручные поля ноября пусты;
 *   OZ-1  resolveSections на живой геометрии Ozon (июль 535 … октябрь 639): 01.11 (глубокая сверка 120 сут.) не создаёт
 *         месяц; 02.11 создаёт ноябрь в строке 674 (30 дн.); повтор — isNew=false;
 *   OZ-2  новый SKU ноября — только в ноябре; выбывший SKU не удаляется (слот живёт всю жизнь листа);
 *   OZ-3  план записи 02.11 (октябрь + новый ноябрь): 31.10 в последней строке октября, 01.11 — в первой строке ноября,
 *         02.11 пуст, модельная QA плана чиста, LCD в формулах — OZON_LAST_CLOSED_DATE.
 * Ни Sheets, ни BigQuery production не вызываются.
 */
import { describe, it, expect } from 'vitest';
import { unitkaLoader, type UnitkaDeps } from '../src/loaders/unitka/index.js';
import { planMonthPrep, toStructureRequests, type MonthPrepPlan } from '../src/loaders/unitka/monthprep.js';
import { UnitkaBq } from '../src/loaders/unitka/bq.js';
import { classifyCogsSnapshot } from '../src/loaders/unitka/integrity.js';
import { locateSection, nextMonth, formatMonthKey, dayRowOf, type MonthKey } from '../src/loaders/unitka/calendar.js';
import { validateSection, type Snapshot } from '../src/loaders/unitka/plan.js';
import { OFFSET, addDaysIso, isoToSerial, type CellValue } from '../src/loaders/unitka/model.js';
import { reconcileWindow } from '../src/loaders/unitka/reconcile.js';
import type { StructureRequest } from '../src/loaders/unitka/sheets.js';
import { loadConfig, type Config } from '../src/config.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';
import {
  sectionFromSpec, septemberSpec, applyPlan, insertColumnsInto, cogsSnapshot, septemberStructure, septemberRowFormats,
  NEW_NM, WIDTH_SEPT, SHEET_ID,
} from './unitka_calendar_fixture.js';
import { MemoryBook, ReconRunner, reconFactsFor, ALL_NMS } from './unitka_reconcile_fixture.js';
import { resolveSections, rowsNeeded } from '../src/loaders/unitka/ozon/lifecycle.js';
import { ozonRewriteWindow, ozonDeepWindow, ozonWindowMonths, ozonFromDayFor, isDeepReconciliationDay } from '../src/loaders/unitka/ozon/window.js';
import { composeMonth, ozonMonthSpec, type OzonFactRow } from '../src/loaders/unitka/ozon/month.js';
import { buildOzonPlan, sectionFormulas, type OzonSectionInput } from '../src/loaders/unitka/ozon/monthplan.js';
import { layoutOf } from '../src/loaders/unitka/ozon/requests.js';
import { ozonWrittenReader, verifyOzonModel } from '../src/loaders/unitka/ozon/model_qa.js';
import { OZON_OFFSET } from '../src/loaders/unitka/ozon/offsets.js';

/* ───────────────────────────── WB: харнесс (как в unitka_wb_month_boundaries) ───────────────────────────── */

interface LogLine { level: string; event: string; fields: Record<string, unknown> }
function recordingLogger(lines: LogLine[]): Logger {
  const mk = (): Logger => ({
    info: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'info', event, fields }); },
    warn: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'warn', event, fields }); },
    error: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'error', event, fields }); },
    debug: () => {}, child: () => mk(),
  } as unknown as Logger);
  return mk();
}
const cfg = (extra: Record<string, string> = {}): Config =>
  loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'proj', BQ_RAW_DATASET: 'wb_raw', UNITKA_WRITE_ENABLED: '1', GIT_SHA: 'sha-test', ...extra });

const NAMES: Record<number, string> = Object.fromEntries(ALL_NMS.map((n) => [n, `Товар ${n}`]));
const EXTRA_NM = 999000111;

class NovBook extends MemoryBook {
  order: string[] = [];
  structureCalls: StructureRequest[][] = [];
  expected: MonthPrepPlan | null = null;
  requestsMatchCanonical: boolean[] = [];
  override async readRowFormats(_n: string, rows: readonly number[], lastColumn: number): Promise<Map<number, Array<Record<string, unknown> | null>>> {
    return septemberRowFormats(lastColumn, rows);
  }
  override async structureWrite(requests: StructureRequest[]): Promise<number> {
    this.order.push('structure');
    this.structureCalls.push(requests);
    const p = this.expected;
    if (!p) throw new Error('структурная запись без ожидаемого плана');
    this.requestsMatchCanonical.push(JSON.stringify(requests) === JSON.stringify(toStructureRequests(p, SHEET_ID)));
    const ins = p.insertColumns;
    const width = this.columnCount + (ins?.count ?? 0);
    if (ins) this.sections = this.sections.map((s) => insertColumnsInto(s, ins.at, ins.count));
    this.sections.push({ ...applyPlan(p, width), anchorCol: width });
    this.rowCount += p.appendRows; this.columnCount = width; this.anchorCol = width;
    this.recompute();
    return requests.length;
  }
  override async batchWrite(data: Parameters<MemoryBook['batchWrite']>[0]): Promise<number> {
    const lcd = data.some((d) => d.range === 'LAST_CLOSED_DATE');
    const n = await super.batchWrite(data);
    this.order.push(lcd ? 'lcd' : 'data');
    return n;
  }
}

/** BigQuery: факты/ставки до `lcd`, популяция (управляемая) и канон COGS (управляемый). */
class NovRunner extends ReconRunner {
  population: number[] = [...ALL_NMS];
  cogsCanon: Record<number, number> = { [NEW_NM]: 426.735 };
  constructor(lcd: string, public publishedAt: string, windowed = false, extraNm: number | null = null) {
    const from = windowed ? reconcileWindow(lcd).from : `${lcd.slice(0, 7)}-01`;
    const base = reconFactsFor(from, lcd);
    // V_UNITKA_DAILY_FACT — плотная сетка активных SKU: новый SKU получает строки месяца LCD (как в production)
    const extra = extraNm === null ? [] : base.filter((f) => f.nmId === ALL_NMS[0] && f.date >= `${lcd.slice(0, 7)}-01`).map((f) => ({ ...f, nmId: extraNm }));
    super(lcd, [...base, ...extra], from);
  }
  override async query<T = Record<string, unknown>>(sql: string, params?: Record<string, unknown>): Promise<T[]> {
    if (sql.includes('V_WB_SPP_DAILY')) {
      // SPP-3 (production: UNITKA_SPP_MODE=write): СПП есть там, где есть заказы
      const from = String(params?.from), to = String(params?.to);
      return this.facts.filter((f) => f.date >= from && f.date <= to && (f.orders ?? 0) > 0)
        .map((f) => ({ nm_id: f.nmId, date_msk: { value: f.date }, effective_spp_pct: 21.5, spp_status: 'OK', orders_qty: f.orders })) as T[];
    }
    if (sql.includes('REF_SKU_MASTER')) return [...this.population].sort((a, b) => a - b).map((n) => ({ nm_id: n, product_name_short: NAMES[n] ?? `Новый ${n}` })) as T[];
    if (sql.includes('V_UNITKA_COGS_CANONICAL')) {
      return Object.entries(this.cogsCanon).map(([nm, v]) => ({ nm_id: Number(nm), internal_sku: `EVT-${nm}`, day: { value: addDaysIso(this.lcd, -1) }, cogs_interval_count: 1,
        canonical_cogs: String(v), snapshot_published_at: { value: this.publishedAt }, snapshot_run_id: 'run-cogs-nov' })) as T[];
    }
    return super.query<T>(sql, params);
  }
}

/** Книга от сентября 2026 до октября 2026 включительно, построенная каноническим генератором (как в production). */
function bookThroughOctober(committedIso: string): NovBook {
  let sections: Snapshot[] = [{ ...sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT), anchorCol: WIDTH_SEPT }];
  let rows = 768; let width = WIDTH_SEPT;
  const k: MonthKey = { year: 2026, month: 10 };
  const pred = sections[0]!; const pg = pred.geometry;
  const colA: CellValue[] = Array(rows).fill(null); colA[pg.topRow - 1] = pred.grid[0]![0]!;
  const p = planMonthPrep({
    target: k, meta: { sheetId: SHEET_ID, rowCount: rows, columnCount: width, anchorCol: width }, columnA: colA, predecessor: pred, existing: null,
    population: [...ALL_NMS].sort((a, b) => a - b).map((nmId) => ({ nmId, name: NAMES[nmId]! })), cogs: cogsSnapshot({ [NEW_NM]: 426.735 }),
    structure: septemberStructure(rows, width),
    rowFormats: septemberRowFormats(width, [pg.topRow, pg.headerRow, pg.firstDailyRow, pg.lastDailyRow, pg.mtdRow, pg.spacerRow]),
  });
  if (p.status !== 'PLAN_CREATE') throw new Error(`октябрь: ${p.status} ${p.code}`);
  const ins = p.insertColumns;
  if (ins) { sections = sections.map((s) => ({ ...insertColumnsInto(s, ins.at, ins.count), anchorCol: width + ins.count })); width += ins.count; }
  sections.push({ ...applyPlan(p, width), anchorCol: width });
  rows = p.geometry!.spacerRow;
  return new NovBook(sections, rows, width, width, committedIso);
}

const columnA = (b: NovBook): CellValue[] => { const a: CellValue[] = Array(b.rowCount).fill(null); for (const s of b.sections) a[s.geometry.topRow - 1] = s.grid[0]![0]!; return a; };
async function canonicalPlan(book: NovBook, runner: NovRunner, target: MonthKey, now: Date): Promise<MonthPrepPlan> {
  const bq = new UnitkaBq(runner, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS');
  const pred = book.sections[book.sections.length - 1]!; const pg = pred.geometry;
  return planMonthPrep({
    target, meta: { sheetId: SHEET_ID, rowCount: book.rowCount, columnCount: book.columnCount, locale: 'en_US', anchorCol: book.anchorCol }, columnA: columnA(book),
    predecessor: pred, existing: null, population: await bq.activeSkus(), cogs: classifyCogsSnapshot(await bq.cogsCanonical(), now),
    structure: septemberStructure(book.rowCount, book.columnCount),
    rowFormats: septemberRowFormats(book.columnCount, [pg.topRow, pg.headerRow, pg.firstDailyRow, pg.mtdRow, pg.spacerRow]),
  });
}
let seq = 0;
async function runDaily(book: NovBook, runner: NovRunner, now: Date, config: Config = cfg(), lines: LogLine[] = []): Promise<LogLine[]> {
  const ctx: LoaderContext = { config, logger: recordingLogger(lines), logicalPeriod: 'p', targetDate: 'p', runId: `run-nov-${++seq}` };
  const deps: UnitkaDeps = { makeRunner: () => runner, makeSheets: () => book, now: () => now };
  await unitkaLoader(ctx, deps);
  return lines;
}
const lifecycle = (lines: LogLine[], ev: string) => lines.filter((l) => l.event === 'unitka_lifecycle' && l.fields.lifecycle_event === ev);
const NOV: MonthKey = { year: 2026, month: 11 };
const RUN_0111 = new Date('2026-11-01T07:00:00Z');
const RUN_0211 = new Date('2026-11-02T07:00:00Z');
const RUN_0211_RESERVE = new Date('2026-11-02T09:30:00Z');

async function closeOctober(book: NovBook, config: Config, windowed: boolean): Promise<LogLine[]> {
  // прогон 01.11 07:00Z: D-1 МСК = 31.10, закрывается последний день октября
  return runDaily(book, new NovRunner('2026-10-31', '2026-11-01T06:50:02Z', windowed), RUN_0111, config);
}

/* ───────────────────────────── WB ───────────────────────────── */

describe('WB · 31.10 → 01.11 → 02.11 через боевой цикл unitkaLoader (фейки)', () => {
  for (const variant of [
    { name: 'конфигурация по умолчанию', env: {} as Record<string, string> },
    { name: 'конфигурация production (controlled + observe + SPP write)', env: { UNITKA_RECONCILE_MODE: 'controlled', UNITKA_INTEGRITY_MODE: 'observe', UNITKA_SPP_MODE: 'write' } },
  ]) {
    it(`WB-1/2 · ${variant.name}: 01.11 закрывает 31.10 без создания ноября (warn), 02.11 создаёт ноябрь один раз, резерв — no-op`, async () => {
      const config = cfg(variant.env);
      const book = bookThroughOctober('2026-10-30');
      expect(locateSection(columnA(book), NOV).status).toBe('MISSING');

      const windowed = variant.env.UNITKA_RECONCILE_MODE !== undefined;
      const l1 = await closeOctober(book, config, windowed);
      expect(book.structureCalls, '01.11: структура не пишется').toHaveLength(0);
      expect(book.lcdSerial, '01.11: LCD = 31.10').toBe(isoToSerial('2026-10-31'));
      const warn = l1.filter((l) => l.event === 'unitka_next_month_section');
      expect(warn).toEqual([expect.objectContaining({ level: 'warn', fields: expect.objectContaining({ code: 'NEXT_MONTH_SECTION_MISSING', next_month: '2026-11', days_left: 0 }) })]);
      expect(l1.filter((l) => l.level === 'error' && l.fields.code === 'NEXT_MONTH_SECTION_MISSING'), 'алерт (severity>=ERROR) не сработает').toHaveLength(0);
      // 31.10 — последняя строка октября, записана
      const oct = book.section('2026-10');
      expect(book.get(oct.geometry.lastDailyRow, validateSection(oct).blocks[0]!.start + OFFSET.orders)).not.toBe('');

      const runner2 = new NovRunner('2026-11-01', '2026-11-02T06:50:02Z', windowed);
      book.expected = await canonicalPlan(book, runner2, NOV, RUN_0211);
      expect(book.expected.status).toBe('PLAN_CREATE');
      const order0 = book.order.length;
      const l2 = await runDaily(book, runner2, RUN_0211, config);
      expect(book.structureCalls).toHaveLength(1);
      expect(book.requestsMatchCanonical).toEqual([true]);
      expect(lifecycle(l2, 'NEW_MONTH_CREATED').map((l) => l.fields)).toEqual([expect.objectContaining({ target: '2026-11', days: 30, top_row: 804 })]);
      expect(book.order.slice(order0)).toEqual(['structure', 'data', 'lcd']);
      expect(book.lcdSerial).toBe(isoToSerial('2026-11-01'));
      const nov = book.section('2026-11');
      expect([nov.geometry.topRow, nov.geometry.firstDailyRow, nov.geometry.lastDailyRow, nov.geometry.mtdRow, nov.geometry.spacerRow]).toEqual([804, 806, 835, 836, 837]);
      expect(book.rowCount).toBe(837);
      const v = validateSection(nov);
      expect([...v.sectionIssues, ...v.driftIssues]).toEqual([]);
      expect(v.blocks).toHaveLength(25);
      // данные 01.11 — в первой строке ноября, 02.11 — будущее
      expect(book.get(dayRowOf(nov.geometry, 0), v.blocks[0]!.start + OFFSET.orders)).not.toBe('');
      expect(book.get(dayRowOf(nov.geometry, 1), v.blocks[0]!.start + OFFSET.orders)).toBe('');
      // ничего не записано в октябрь ниже его MTD или выше ноября
      expect(book.sections.filter((s) => s.geometry.monthKey === '2026-11')).toHaveLength(1);

      // резерв 09:30Z того же дня: ни структуры, ни дубля, LCD не двигается
      const l3 = await runDaily(book, runner2, RUN_0211_RESERVE, config);
      expect(book.structureCalls).toHaveLength(1);
      expect(lifecycle(l3, 'NEW_MONTH_CREATED')).toHaveLength(0);
      expect(book.lcdSerial).toBe(isoToSerial('2026-11-01'));
    });
  }

  it('WB-3 · выбывший SKU в середине цепочки (02.11) → CHAIN_GAP до любой записи; LCD стоит на 31.10; повтор — то же', async () => {
    const book = bookThroughOctober('2026-10-31');
    const runner = new NovRunner('2026-11-01', '2026-11-02T06:50:02Z');
    const middle = [...ALL_NMS].sort((a, b) => a - b)[5]!;
    runner.population = ALL_NMS.filter((n) => n !== middle);
    const writesBefore = book.batchWrites.length;
    await expect(runDaily(book, runner, RUN_0211)).rejects.toMatchObject({ code: 'CHAIN_GAP' });
    expect(book.structureCalls).toHaveLength(0);
    expect(book.batchWrites.length).toBe(writesBefore);
    expect(book.lcdSerial).toBe(isoToSerial('2026-10-31'));
    await expect(runDaily(book, runner, RUN_0211_RESERVE)).rejects.toMatchObject({ code: 'CHAIN_GAP' });
    expect(locateSection(columnA(book), NOV).status).toBe('MISSING');
  });

  it('WB-4 · новый SKU без канона COGS → COGS_MISSING до записи; с каноном → 26 блоков, вставка колонок, якорь сдвигается, LCD коммитится', async () => {
    const book = bookThroughOctober('2026-10-31');
    const runner = new NovRunner('2026-11-01', '2026-11-02T06:50:02Z', false, EXTRA_NM);
    runner.population = [...ALL_NMS, EXTRA_NM];
    await expect(runDaily(book, runner, RUN_0211)).rejects.toMatchObject({ code: 'COGS_MISSING' });
    expect(book.structureCalls).toHaveLength(0);
    expect(book.lcdSerial).toBe(isoToSerial('2026-10-31'));

    runner.cogsCanon = { ...runner.cogsCanon, [EXTRA_NM]: 222.5 };
    book.expected = await canonicalPlan(book, runner, NOV, RUN_0211);
    expect(book.expected.status).toBe('PLAN_CREATE');
    const anchorBefore = book.anchorCol;
    expect(book.expected.insertColumns?.count).toBeGreaterThan(0);
    await runDaily(book, runner, RUN_0211);
    expect(book.structureCalls).toHaveLength(1);
    const nov = validateSection(book.section('2026-11'));
    expect(nov.blocks).toHaveLength(26);
    expect(nov.blocks.at(-1)!.nmId).toBe(EXTRA_NM);
    expect(book.anchorCol, 'REVERSE_LEG_RATE / зеркало LCD сдвинулись вместе с хвостом').toBe(anchorBefore + book.expected.insertColumns!.count);
    expect(validateSection(book.section('2026-10')).blocks, 'октябрь не получил новый блок').toHaveLength(25);
    expect(book.lcdSerial).toBe(isoToSerial('2026-11-01'));
  });

  it('WB-5 · ручные СПП и блогеры октября сохраняются; в ноябре ручные поля пусты', async () => {
    const book = bookThroughOctober('2026-10-31');
    const oct = book.section('2026-10'); const ov = validateSection(oct);
    const r = oct.geometry.lastDailyRow; const b0 = ov.blocks[0]!.start;
    book.set(r, b0 + OFFSET.spp, 17); book.set(r, b0 + OFFSET.bloggers, 1500);
    const runner = new NovRunner('2026-11-01', '2026-11-02T06:50:02Z');
    book.expected = await canonicalPlan(book, runner, NOV, RUN_0211);
    await runDaily(book, runner, RUN_0211);
    expect(book.get(r, b0 + OFFSET.spp)).toBe(17);
    expect(book.get(r, b0 + OFFSET.bloggers)).toBe(1500);
    const nov = book.section('2026-11'); const nv = validateSection(nov);
    for (let i = 0; i < nov.geometry.daysInMonth; i++) {
      expect(book.get(dayRowOf(nov.geometry, i), nv.blocks[0]!.start + OFFSET.bloggers)).toBe('');
    }
    expect(book.expected!.cells.some((c) => c.row <= oct.geometry.spacerRow), 'план ноября не адресует строки октября').toBe(false);
  });
});

/* ───────────────────────────── OZON ───────────────────────────── */

/** Живая геометрия Ozon на 20.10.2026 (колонка A листа): июль 535 … октябрь 639, лист кончается строкой 673. */
const OZ_LIVE = [
  { monthKey: '2026-06', titleRow: 501, days: 30 },
  { monthKey: '2026-07', titleRow: 535, days: 31 },
  { monthKey: '2026-08', titleRow: 570, days: 31 },
  { monthKey: '2026-09', titleRow: 605, days: 30 },
  { monthKey: '2026-10', titleRow: 639, days: 31 },
];
const SKUS = ['a-old', 'b-old', 'c-retired', '305101272'];
const live = (extra: Array<{ monthKey: string; titleRow: number; days: number }> = []) => [...OZ_LIVE, ...extra].map((s) => ({
  monthKey: s.monthKey, titleRow: s.titleRow, days: Array.from({ length: s.days }, (_, i) => `${s.monthKey}-${String(i + 1).padStart(2, '0')}`), blocks: SKUS,
}));
const FIRST = { 'a-old': '2026-05', 'b-old': '2026-05', 'c-retired': '2026-05', '305101272': '2026-06' } as Record<string, string>;

describe('Ozon · переход октябрь → ноябрь на живой геометрии', () => {
  it('OZ-1 · 01.11 (глубокая сверка, LCD 31.10) — без нового месяца; 02.11 (LCD 01.11) — ноябрь в 674, 30 дн.; повтор — isNew=false', () => {
    expect(isDeepReconciliationDay('2026-11-01')).toBe(true);
    const w1 = ozonDeepWindow('2026-10-31');
    expect(ozonWindowMonths(w1)).toEqual(['2026-07', '2026-08', '2026-09', '2026-10']);
    const p1 = resolveSections({ live: live(), windowMonths: ozonWindowMonths(w1), firstActivity: FIRST, blockSlots: 22 });
    expect(p1.filter((p) => p.isNew)).toEqual([]);

    expect(isDeepReconciliationDay('2026-11-02')).toBe(false);
    const w2 = ozonRewriteWindow('2026-11-01');
    expect(w2.from).toBe('2026-09-18');
    expect(ozonWindowMonths(w2)).toEqual(['2026-09', '2026-10', '2026-11']);
    const p2 = resolveSections({ live: live(), windowMonths: ozonWindowMonths(w2), firstActivity: FIRST, blockSlots: 22 });
    const nov = p2.find((p) => p.monthKey === '2026-11')!;
    expect(nov).toMatchObject({ isNew: true, titleRow: 674, headerRow: 675, days: 30, blocks: SKUS });
    expect(rowsNeeded(p2, 673)).toBe(674 + 34 - 1 - 673);

    const again = resolveSections({ live: live([{ monthKey: '2026-11', titleRow: 674, days: 30 }]), windowMonths: ozonWindowMonths(w2), firstActivity: FIRST, blockSlots: 22 });
    expect(again.filter((p) => p.isNew)).toEqual([]);
    expect(rowsNeeded(again, 707)).toBe(0);
    // декабрь → январь и февраль 2028 продолжают цепочку
    const dec = resolveSections({ live: live([{ monthKey: '2026-11', titleRow: 674, days: 30 }]), windowMonths: ['2026-11', '2026-12', '2027-01'], firstActivity: FIRST, blockSlots: 22 });
    expect(dec.filter((p) => p.isNew).map((p) => [p.monthKey, p.titleRow, p.days])).toEqual([['2026-12', 708, 31], ['2027-01', 743, 31]]);
  });

  it('OZ-2 · новый SKU ноября — только в ноябре; выбывший SKU остаётся в своём слоте; нет слота → NO_FREE_SKU_SLOT', () => {
    const first = { ...FIRST, 'n-new': '2026-11' };
    const p = resolveSections({ live: live(), windowMonths: ['2026-09', '2026-10', '2026-11'], firstActivity: first, blockSlots: 22 });
    expect(p.find((x) => x.monthKey === '2026-10')!.blocks).toEqual(SKUS);
    expect(p.find((x) => x.monthKey === '2026-11')!.blocks).toEqual([...SKUS, 'n-new']);
    expect(() => resolveSections({ live: live(), windowMonths: ['2026-10', '2026-11'], firstActivity: first, blockSlots: 4 })).toThrow(/нужно слотов 5, размечено 4/);
  });

  it('OZ-3 · план 02.11: 31.10 → последняя строка октября, 01.11 → первая строка ноября, 02.11 пуст; модельная QA плана чиста', () => {
    const lcd = '2026-11-01';
    const w = ozonRewriteWindow(lcd);
    const SKU = '305101272';
    const fact = (d: string): OzonFactRow => ({ d, offer_id: SKU, gross_qty: 1, cancelled_qty: 0,
      realized_qty: 1, expected_realized_qty: 1, revenue: 0, provisional_revenue_rub: 0,
      order_reference_price: 1287, order_reference_qty: 1, order_reference_covered_qty: 1,
      cogs_amt: 130.81, provisional_cogs_rub: 130.81, cogs_missing_qty: 0,
      logistics: 105, logistics_actual: 105, logistics_state: 'ACTUAL',
      commission: 669.24, commission_actual: 0, commission_estimated_rub: 669.24,
      commission_state: 'ESTIMATED', commission_estimate_method: 'COMMISSION_POLICY_TARIFF',
      commission_missing_qty: 1, commission_not_applicable_qty: 0,
      buyout_revenue_unproven_qty: 1, economics_completeness: 'PROVISIONAL_PARTIAL',
      ads_spend: 10, impr: 100, clicks: 3, buyer_amt: null, seller_amt: null });
    const facts = [fact('2026-10-31'), fact('2026-11-01')];
    const oct = ozonMonthSpec(2026, 10, 605, 30, SKUS);
    const nov = ozonMonthSpec(2026, 11, oct.titleRow, 31, SKUS);
    expect([oct.titleRow, oct.lastRow, nov.titleRow, nov.firstRow, nov.lastRow, nov.mtdRow]).toEqual([639, 671, 674, 676, 705, 706]);
    const sheetInputs = { bloggers: {}, externalAds: {} };
    const secOf = (spec: typeof oct): OzonSectionInput => {
      const from = ozonFromDayFor(spec.key, w);
      const inSection = facts.filter((f) => f.d.startsWith(spec.key));
      const comp = composeMonth(spec, inSection, {}, lcd, from, {}, sheetInputs);
      return { spec, facts: inSection, stock: {}, cart: {}, sheetInputs, fromDay: from,
        formulas: sectionFormulas(spec, comp, 'SEMICOLON', 'OZON_LAST_CLOSED_DATE'), refTitle: [], refHeader: [], refAnchor: {} };
    };
    const built = [secOf(oct), secOf(nov)];
    expect(built.map((b) => b.fromDay)).toEqual([1, 1]);
    const plan = buildOzonPlan({ sheetId: 1, sheetName: 'OZON_Юнит_2025', sections: built,
      allSections: [layoutOf(oct), layoutOf(nov)],
      grid: { current: { rows: 673, columns: 600 }, growth: { widenBlockAt: [], insertColumnsBefore: 562, insertColumnCount: 0, appendColumnCount: 0, appendRowCount: 34 } },
      lcd, lcdMirror: null, lcdRef: '$VA$2', blocks: SKUS.length });
    const read = ozonWrittenReader(plan.values);
    expect(verifyOzonModel({ sections: built, lcd, read, mode: 'PLAN' })).toEqual([]);
    const col = oct.anchor[SKU]! + OZON_OFFSET.orders;
    expect(read(oct.lastRow, col), '31.10 — в октябре').toBe(1);
    expect(read(nov.firstRow, nov.anchor[SKU]! + OZON_OFFSET.orders), '01.11 — в ноябре').toBe(1);
    expect([undefined, null, ''].includes(read(nov.firstRow + 1, nov.anchor[SKU]! + OZON_OFFSET.orders) as never), '02.11 — будущее').toBe(true);
    const appendRows = plan.structure.filter((r) => JSON.stringify(r).includes('"appendDimension"') && JSON.stringify(r).includes('ROWS'));
    expect(appendRows, 'строки ноября дописываются ровно один раз').toHaveLength(1);
    const text = JSON.stringify(plan.values);
    expect(text).toContain('OZON_LAST_CLOSED_DATE');
    expect(text).not.toMatch(/(?<![A-Z_])LAST_CLOSED_DATE/);
  });
});

// неиспользуемые импорты-помощники держим явными: nextMonth/formatMonthKey пригодятся для расширения сценария на 12→01
void nextMonth; void formatMonthKey;
