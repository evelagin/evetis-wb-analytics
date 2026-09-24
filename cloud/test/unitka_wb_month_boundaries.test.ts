/**
 * GATE 10 · §6 — границы месяца через БОЕВОЙ цикл WB (unitkaLoader), без production.
 *
 * Для каждой границы 30.09→01.10.2026, 31.10→01.11.2026, 31.12.2026→01.01.2027, 28.02→01.03.2027,
 * 29.02→01.03.2028:
 *   секции целевого месяца нет → суточный прогон создаёт её ОДИН раз каноническим генератором (запросы
 *   побайтно = toStructureRequests(planMonthPrep(...))) → ровно days+4 строки → контракт секции (формулы,
 *   шапка, MTD) и сводка сходятся → LCD коммитится только ПОСЛЕ записи и проверки → повтор ничего не меняет;
 *   сбой записи данных после создания месяца оставляет LCD прежним, повтор не создаёт месяц второй раз.
 *
 * Книга — в памяти (MemoryBook: эмулятор формул листа, как в тестах сверки); структурная запись применяет
 * ровно тот план, чьи запросы пришли. Ни Sheets, ни BigQuery.
 */
import { describe, it, expect } from 'vitest';
import { unitkaLoader, type UnitkaDeps } from '../src/loaders/unitka/index.js';
import { planMonthPrep, toStructureRequests, type MonthPrepPlan } from '../src/loaders/unitka/monthprep.js';
import { UnitkaBq } from '../src/loaders/unitka/bq.js';
import { classifyCogsSnapshot } from '../src/loaders/unitka/integrity.js';
import { geometryAt, locateSection, nextMonth, formatMonthKey, dayRowOf, type MonthKey } from '../src/loaders/unitka/calendar.js';
import { validateSection, type Snapshot } from '../src/loaders/unitka/plan.js';
import { OFFSET, SUMMARY, SUMMARY_TO_OFFSET, addDaysIso, isoToSerial, type CellValue } from '../src/loaders/unitka/model.js';
import type { StructureRequest } from '../src/loaders/unitka/sheets.js';
import { loadConfig, type Config } from '../src/config.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';
import {
  sectionFromSpec, septemberSpec, applyPlan, insertColumnsInto, cogsSnapshot, septemberStructure, septemberRowFormats,
  NEW_NM, WIDTH_SEPT, SHEET_ID,
} from './unitka_calendar_fixture.js';
import { MemoryBook, ReconRunner, reconFactsFor, ALL_NMS } from './unitka_reconcile_fixture.js';

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
const cfg = (): Config =>
  loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'proj', BQ_RAW_DATASET: 'wb_raw', UNITKA_WRITE_ENABLED: '1', GIT_SHA: 'sha-test' });

const NAMES: Record<number, string> = Object.fromEntries(ALL_NMS.map((n) => [n, n === NEW_NM ? 'Набор анти-акне пудра+сыворотка+крем' : `Товар ${n}`]));
const population = () => [...ALL_NMS].sort((a, b) => a - b).map((nmId) => ({ nmId, name: NAMES[nmId]! }));

/** Книга с журналом порядка мутаций; структурная запись применяет план, чьи запросы пришли. */
class BoundaryBook extends MemoryBook {
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
    // «Sheets» исполняет запросы: вставка колонок сдвигает все секции и якоря, затем новая секция в конце
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

/** BigQuery: слой фактов/ставок сверки + популяция и канон COGS для генератора месяца. */
class BoundaryRunner extends ReconRunner {
  constructor(lcd: string, public publishedAt: string) {
    super(lcd, reconFactsFor(`${lcd.slice(0, 7)}-01`, lcd), `${lcd.slice(0, 7)}-01`);
  }
  override async query<T = Record<string, unknown>>(sql: string, params?: Record<string, unknown>): Promise<T[]> {
    if (sql.includes('REF_SKU_MASTER')) return population().map((p) => ({ nm_id: p.nmId, product_name_short: p.name })) as T[];
    if (sql.includes('V_UNITKA_COGS_CANONICAL')) {
      return [{ nm_id: NEW_NM, internal_sku: 'EVT-SET', day: { value: addDaysIso(this.lcd, -1) }, cogs_interval_count: 1, canonical_cogs: '426.735',
        snapshot_published_at: { value: this.publishedAt }, snapshot_run_id: 'run-cogs-g10' }] as T[];
    }
    return super.query<T>(sql, params);
  }
}

/** Книга, прожитая от сентября 2026 до месяца `last` включительно (каждый месяц — каноническим генератором). */
function bookThrough(last: MonthKey, committedIso: string): BoundaryBook {
  let sections: Snapshot[] = [{ ...sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT), anchorCol: WIDTH_SEPT }];
  let rows = 768; let width = WIDTH_SEPT;
  for (let k: MonthKey = { year: 2026, month: 10 }; k.year < last.year || (k.year === last.year && k.month <= last.month); k = nextMonth(k)) {
    const pred = sections[sections.length - 1]!; const pg = pred.geometry;
    const colA: CellValue[] = Array(rows).fill(null); for (const s of sections) colA[s.geometry.topRow - 1] = s.grid[0]![0]!;
    const p = planMonthPrep({
      target: k, meta: { sheetId: SHEET_ID, rowCount: rows, columnCount: width, anchorCol: width }, columnA: colA, predecessor: pred, existing: null,
      population: population(), cogs: cogsSnapshot({ [NEW_NM]: 426.735 }), structure: septemberStructure(rows, width),
      rowFormats: septemberRowFormats(width, [pg.topRow, pg.headerRow, pg.firstDailyRow, pg.lastDailyRow, pg.mtdRow, pg.spacerRow]),
    });
    if (p.status !== 'PLAN_CREATE') throw new Error(`цепочка ${formatMonthKey(k)}: ${p.status} ${p.code}`);
    const ins = p.insertColumns;
    if (ins) { sections = sections.map((s) => ({ ...insertColumnsInto(s, ins.at, ins.count), anchorCol: width + ins.count })); width += ins.count; }
    sections.push({ ...applyPlan(p, width), anchorCol: width });
    rows = p.geometry!.spacerRow;
  }
  return new BoundaryBook(sections, rows, width, width, committedIso);
}

/** Тот же план, что построит цикл: те же чтения книги и BigQuery, тот же генератор. */
async function canonicalPlan(book: BoundaryBook, runner: BoundaryRunner, target: MonthKey, now: Date): Promise<MonthPrepPlan> {
  const bq = new UnitkaBq(runner, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS');
  const pred = book.sections[book.sections.length - 1]!; const pg = pred.geometry;
  const colA: CellValue[] = Array(book.rowCount).fill(null); for (const s of book.sections) colA[s.geometry.topRow - 1] = s.grid[0]![0]!;
  return planMonthPrep({
    target, meta: { sheetId: SHEET_ID, rowCount: book.rowCount, columnCount: book.columnCount, locale: 'en_US', anchorCol: book.anchorCol }, columnA: colA,
    predecessor: pred, existing: null, population: await bq.activeSkus(), cogs: classifyCogsSnapshot(await bq.cogsCanonical(), now),
    structure: septemberStructure(book.rowCount, book.columnCount),
    rowFormats: septemberRowFormats(book.columnCount, [pg.topRow, pg.headerRow, pg.firstDailyRow, pg.mtdRow, pg.spacerRow]),
  });
}

let seq = 0;
async function runDaily(book: BoundaryBook, runner: BoundaryRunner, now: Date, lines: LogLine[] = []): Promise<LogLine[]> {
  const ctx: LoaderContext = { config: cfg(), logger: recordingLogger(lines), logicalPeriod: 'p', targetDate: 'p', runId: `run-b-${++seq}` };
  const deps: UnitkaDeps = { makeRunner: () => runner, makeSheets: () => book, now: () => now };
  await unitkaLoader(ctx, deps);
  return lines;
}
const columnA = (b: BoundaryBook): CellValue[] => { const a: CellValue[] = Array(b.rowCount).fill(null); for (const s of b.sections) a[s.geometry.topRow - 1] = s.grid[0]![0]!; return a; };
const lifecycle = (lines: LogLine[], ev: string) => lines.filter((l) => l.event === 'unitka_lifecycle' && l.fields.lifecycle_event === ev);
const lastCommit = (r: BoundaryRunner) => JSON.parse(String(r.journal.at(-1)!.qaJson)).lifecycle as { committed_before: string; candidate: string; commit: { code: string } };

/** Строки, которых касаются запросы данного типа (0-based индексы → 1-based строки). */
function rowsTouched(req: StructureRequest[], kind: 'repeatCell' | 'updateCells'): Set<number> {
  const out = new Set<number>();
  for (const r of req) {
    const body = (r as Record<string, { range?: { startRowIndex: number; endRowIndex: number }; start?: { rowIndex: number }; rows?: unknown[] }>)[kind];
    if (!body) continue;
    if (body.range) for (let i = body.range.startRowIndex; i < body.range.endRowIndex; i++) out.add(i + 1);
    if (body.start && body.rows) for (let i = 0; i < body.rows.length; i++) out.add(body.start.rowIndex + i + 1);
  }
  return out;
}

const BOUNDARIES: Array<{ committed: string; candidate: string; days: number; topRow: number; newSku: boolean }> = [
  { committed: '2026-09-30', candidate: '2026-10-01', days: 31, topRow: 769, newSku: true },
  { committed: '2026-10-31', candidate: '2026-11-01', days: 30, topRow: 804, newSku: false },
  { committed: '2026-12-31', candidate: '2027-01-01', days: 31, topRow: 873, newSku: false },
  { committed: '2027-02-28', candidate: '2027-03-01', days: 31, topRow: 940, newSku: false },
  { committed: '2028-02-29', candidate: '2028-03-01', days: 31, topRow: 1354, newSku: false },
];

describe('Gate 10 §6 · граница месяца через боевой цикл WB: создать один раз → записать → проверить → коммит LCD', () => {
  for (const b of BOUNDARIES) {
    const target: MonthKey = { year: Number(b.candidate.slice(0, 4)), month: Number(b.candidate.slice(5, 7)) };
    const tk = formatMonthKey(target);
    const pred: MonthKey = { year: Number(b.committed.slice(0, 4)), month: Number(b.committed.slice(5, 7)) };
    const now = new Date(`${addDaysIso(b.candidate, 1)}T07:00:00Z`);
    const fresh = (): { book: BoundaryBook; runner: BoundaryRunner } => ({
      book: bookThrough(pred, b.committed), runner: new BoundaryRunner(b.candidate, `${addDaysIso(b.candidate, 1)}T06:50:02Z`),
    });

    it(`${b.committed} → ${b.candidate}: ${tk} создан один раз (${b.days} дн., строки ${b.topRow}..${b.topRow + b.days + 3}), LCD после цикла, повтор — 0 мутаций`, async () => {
      const { book, runner } = fresh();
      expect(locateSection(columnA(book), target).status, 'до прогона секции нет').toBe('MISSING');
      const rowsBefore = book.rowCount; const colsBefore = book.columnCount;
      book.expected = await canonicalPlan(book, runner, target, now);
      expect(book.expected.status).toBe('PLAN_CREATE');

      const lines = await runDaily(book, runner, now);

      // создан ровно один раз и ровно каноническим генератором
      expect(book.structureCalls).toHaveLength(1);
      expect(book.requestsMatchCanonical).toEqual([true]);
      expect(book.sections.filter((s) => s.geometry.monthKey === tk)).toHaveLength(1);
      expect(lifecycle(lines, 'NEW_MONTH_CREATED').map((l) => l.fields)).toEqual([expect.objectContaining({ target: tk, days: b.days, top_row: b.topRow })]);
      // геометрия: ровно days + 4 строки (заголовок, шапка, дни, MTD, разделитель); колонки — только при новом SKU
      const sec = book.section(tk); const g = sec.geometry;
      expect(g).toMatchObject(geometryAt(target, b.topRow));
      expect([g.daysInMonth, g.firstDailyRow, g.lastDailyRow, g.mtdRow, g.spacerRow]).toEqual([b.days, b.topRow + 2, b.topRow + 1 + b.days, b.topRow + 2 + b.days, b.topRow + 3 + b.days]);
      expect(book.rowCount - rowsBefore).toBe(b.days + 4);
      expect(book.columnCount - colsBefore).toBe(b.newSku ? 23 : 0);
      // контракт секции (формулы дней и MTD, шапка, блоки) — тем же валидатором, что и боевой цикл
      const v = validateSection(sec);
      expect([...v.sectionIssues, ...v.driftIssues]).toEqual([]);
      expect(v.blocks).toHaveLength(25);
      // формат и значения: каждый день новой секции получил и оформление, и содержимое, строки прошлых месяцев — нет
      const req = book.structureCalls[0]!;
      const fmt = rowsTouched(req, 'repeatCell'), cells = rowsTouched(req, 'updateCells');
      for (let r = g.firstDailyRow; r <= g.lastDailyRow; r++) { expect(fmt.has(r), `формат строки ${r}`).toBe(true); expect(cells.has(r), `ячейки строки ${r}`).toBe(true); }
      expect([...cells].every((r) => r >= g.topRow && r <= g.spacerRow), 'содержимое — только в новой секции').toBe(true);

      // порядок: месяц → данные → LCD; LCD — кандидат, ровно один коммит
      expect(book.order).toEqual(['structure', 'data', 'lcd']);
      expect(book.lcdSerial).toBe(isoToSerial(b.candidate));
      expect(lastCommit(runner)).toMatchObject({ committed_before: b.committed, candidate: b.candidate, commit: { code: 'LCD_COMMITTED' } });
      expect(lifecycle(lines, 'LCD_COMMITTED')).toHaveLength(1);
      expect(runner.journal.at(-1)!.qaStatus).toBe('PASS');
      // сводка первого дня = Σ блоков (эмулятор формул); MTD прибыли = прибыль первого дня; день 2 — будущий
      const d1 = dayRowOf(g, 0);
      for (const [sumCol, off] of SUMMARY_TO_OFFSET) {
        const want = v.blocks.reduce((t, blk) => t + (Number(book.get(d1, blk.start + off)) || 0), 0);
        expect(book.get(d1, sumCol)).toBeCloseTo(want, 6);
      }
      expect(book.get(g.mtdRow, SUMMARY.profit)).toBeCloseTo(Number(book.get(d1, SUMMARY.profit)), 6);
      expect(book.get(d1, v.blocks[24]!.start + OFFSET.orders)).not.toBe('');
      expect(book.get(dayRowOf(g, 1), v.blocks[0]!.start + OFFSET.orders)).toBe('');

      // повтор: ни структуры, ни данных, ни LCD
      const again = await runDaily(book, runner, now);
      expect(book.structureCalls).toHaveLength(1);
      expect(book.order).toEqual(['structure', 'data', 'lcd']);
      expect(lastCommit(runner).commit.code).toBe('LCD_NOT_ADVANCED');
      expect(lifecycle(again, 'NEW_MONTH_CREATED')).toHaveLength(0);
      expect(book.sections.filter((s) => s.geometry.monthKey === tk)).toHaveLength(1);
    });

    it(`${b.committed} → ${b.candidate}: сбой записи данных ПОСЛЕ создания ${tk} → LCD прежний; повтор не создаёт месяц второй раз и коммитит один раз`, async () => {
      const { book, runner } = fresh();
      book.expected = await canonicalPlan(book, runner, target, now);
      book.failNextWrite = new Error('Sheets API 503 на записи данных');
      await expect(runDaily(book, runner, now)).rejects.toBeDefined();
      expect(book.structureCalls).toHaveLength(1);
      expect(book.lcdSerial, 'LCD не сдвинулся').toBe(isoToSerial(b.committed));
      expect(book.order).toEqual(['structure']);

      await runDaily(book, runner, now);
      expect(book.structureCalls, 'DUPLICATE_MONTH_SECTIONS = 0').toHaveLength(1);
      expect(book.sections.filter((s) => s.geometry.monthKey === tk)).toHaveLength(1);
      expect(book.order).toEqual(['structure', 'data', 'lcd']);
      expect(book.lcdSerial).toBe(isoToSerial(b.candidate));
      expect(lastCommit(runner).commit.code).toBe('LCD_COMMITTED');
    });
  }

  it('предшественники построены тем же генератором: високосный 2028 и невисокосный 2027 февраль', () => {
    expect(bookThrough({ year: 2027, month: 2 }, '2027-02-28').section('2027-02').geometry.daysInMonth).toBe(28);
    expect(bookThrough({ year: 2028, month: 2 }, '2028-02-29').section('2028-02').geometry.daysInMonth).toBe(29);
  });
});
