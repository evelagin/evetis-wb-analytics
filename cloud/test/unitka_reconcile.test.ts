/**
 * UNITKA FINANCIAL INTEGRITY V1 — самовосстанавливающая сверка: окно 35 дней через границы месяцев, цена с
 * происхождением, защита ручных полей, зависимые расчёты, журнал ремонта, режимы off / observe / write.
 * Книга — в памяти: сентябрь-наследие (24 блока) + октябрь Calendar V2 (25 блоков) [+ ноябрь V2]. Координат месяцев
 * в коде сверки нет — тесты ходят по тем же секциям, что нашёл бы Engine.
 */
import { createHash } from 'node:crypto';
import { describe, it, expect } from 'vitest';
import { unitkaLoader, ENGINE_VERSION, type UnitkaDeps } from '../src/loaders/unitka/index.js';
import {
  reconcileWindow, windowMonths, sectionWindowDays, buildSectionRepairPlan, assertFactCellsOnly, repairRecords, issueRecords,
  financialValidity, assertNotBeforeEpoch, RECONCILE_WINDOW_DAYS, RECONCILIATION_EPOCH,
} from '../src/loaders/unitka/reconcile.js';
import { OFFSET, SUMMARY, colA1 } from '../src/loaders/unitka/model.js';
import { isoToSerial as isoToSerialG10 } from '../src/loaders/unitka/model.js';
import { dayRowOf, slotStart } from '../src/loaders/unitka/calendar.js';
import { blockDayFormulas, type BlockFormulaParams } from '../src/loaders/unitka/formulas.js';
import { UnitkaBq, type FactRow } from '../src/loaders/unitka/bq.js';
import type { PlannedCell } from '../src/loaders/unitka/plan.js';
import { LoaderError } from '../src/errors.js';
import { loadConfig, type Config } from '../src/config.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';
import { SEPT_NMS } from './unitka_calendar_fixture.js';
import { MemoryBook, ReconRunner, buildBook, reconFactsFor, REV_RATE } from './unitka_reconcile_fixture.js';

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
const cfg = (mode: string, extra: Record<string, string> = {}): Config =>
  loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'proj', BQ_RAW_DATASET: 'wb_raw', UNITKA_WRITE_ENABLED: '1', GIT_SHA: 'sha-test', UNITKA_RECONCILE_MODE: mode, ...extra });
let seq = 0;
async function run(book: MemoryBook, runner: ReconRunner, mode = 'write', extra: Record<string, string> = {}, lines: LogLine[] = []): Promise<{ rowsLoaded: number; lines: LogLine[] }> {
  const config = cfg(mode, extra);
  const ctx: LoaderContext = { config, logger: recordingLogger(lines), logicalPeriod: 'p', targetDate: 'p', runId: `run-${++seq}` };
  const deps: UnitkaDeps = { makeRunner: () => runner, makeSheets: () => book, now: () => new Date('2026-10-03T07:00:00Z') };
  const res = await unitkaLoader(ctx, deps);
  return { rowsLoaded: res.rowsLoaded, lines };
}
const SEPT_NM = SEPT_NMS[5]!;                    // блок 6 сентября (слот 5)
const blockCol = (slot: number, off: number): number => slotStart(slot) + off;
const septRow = (book: MemoryBook, iso: string): number => dayRowOf(book.section('2026-09').geometry, Number(iso.slice(8)) - 1);
const octRow = (book: MemoryBook, iso: string): number => dayRowOf(book.section('2026-10').geometry, Number(iso.slice(8)) - 1);
const patch = (facts: FactRow[], nm: number, date: string, p: Partial<FactRow>): FactRow[] => facts.map((f) => (f.nmId === nm && f.date === date ? { ...f, ...p } : f));

/**
 * Книга как в жизни: сентябрь прожит с LCD в сентябре (факты И ставки сентября записаны), затем LCD ушёл в октябрь
 * (01–02.10). Оба прохода — факты новых дней, не ремонт: журнал пуст.
 */
async function seeded(over: (f: FactRow) => Partial<FactRow> | null = () => null): Promise<{ book: MemoryBook; runner: ReconRunner }> {
  const book = buildBook('2026-08-31');
  const runner = new ReconRunner('2026-09-30', reconFactsFor('2026-09-01', '2026-09-30', over), '2026-09-01');
  await run(book, runner);
  runner.lcd = '2026-10-02'; runner.facts = reconFactsFor('2026-09-01', '2026-10-02', over);
  await run(book, runner);
  expect(runner.ledger).toHaveLength(0);          // оба прохода — факты новых дней, а не ремонт
  runner.journal = []; book.batchWrites = [];
  return { book, runner };
}

/* ───────────────────────── окно ───────────────────────── */

describe('окно сверки: 35 календарных дней через границы месяцев, нижняя граница — первый месяц под Engine', () => {
  it('константы решения владельца', () => {
    expect([RECONCILE_WINDOW_DAYS, RECONCILIATION_EPOCH, ENGINE_VERSION]).toEqual([35, '2026-09-01', 'unitka-engine/2.3.0']);
  });
  it('LCD 18.09 → окно с 01.09 (не с 15.08: август вела не Engine — сверка стёрла бы чужие значения)', () => {
    expect(reconcileWindow('2026-09-18')).toEqual({ from: '2026-09-01', to: '2026-09-18', days: 35, epoch: '2026-09-01', rollingFrom: '2026-08-15' });
    expect(windowMonths(reconcileWindow('2026-09-18')).map((k) => `${k.year}-${k.month}`)).toEqual(['2026-9']);
  });
  it('граница 35-го дня: LCD 05.10 включает 01.09, LCD 06.10 — уже нет (день 36 заморожен)', () => {
    expect(reconcileWindow('2026-10-05').from).toBe('2026-09-01');
    expect(reconcileWindow('2026-10-06').from).toBe('2026-09-02');
    expect(sectionWindowDays({ year: 2026, month: 9 }, reconcileWindow('2026-10-06'))).toMatchObject({ fromDay: '2026-09-02', toDay: '2026-09-30' });
    expect(sectionWindowDays({ year: 2026, month: 9 }, reconcileWindow('2026-11-05'))).toBeNull(); // сентябрь вне окна — заморожен
  });
  it('окно может пересечь три месяца (февраль 28 дней): 01.03.2027 → январь, февраль, март', () => {
    const w = reconcileWindow('2027-03-01');
    expect(w.from).toBe('2027-01-26');
    expect(windowMonths(w).map((k) => k.month)).toEqual([1, 2, 3]);
    expect(sectionWindowDays({ year: 2027, month: 2 }, w)!.dayIndexes).toHaveLength(28);
  });
  it('окно короче месяца недопустимо (месяц LCD обязан входить целиком); LCD раньше эпохи — отказ', () => {
    expect(() => reconcileWindow('2026-10-31', 5)).toThrow(/меньше месяца/);
    expect(() => reconcileWindow('2026-08-31')).toThrow(/раньше эпохи/);
    expect(reconcileWindow('2026-10-31', 31).from).toBe('2026-10-01');
  });
});

/* ───────────────────────── эпоха сверки ───────────────────────── */

const daysBetweenIso = (a: string, b: string): number => Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);

describe('ЭПОХА СВЕРКИ 2026-09-01: начало окна = max(скользящие 35 дней, эпоха); август и раньше не меняются никогда', () => {
  const sha = (snap: { grid: unknown; formulas: unknown }): string => createHash('sha256').update(JSON.stringify([snap.grid, snap.formulas])).digest('hex');
  it('31.08.2026 исключено, 01.09.2026 включено — пока эпоха связывает окно (LCD 01.09 … 05.10)', () => {
    for (const lcd of ['2026-09-01', '2026-09-04', '2026-09-18', '2026-09-30', '2026-10-02', '2026-10-05']) {
      const w = reconcileWindow(lcd);
      expect([w.from, w.epoch], lcd).toEqual(['2026-09-01', RECONCILIATION_EPOCH]);
      expect(w.rollingFrom < RECONCILIATION_EPOCH || lcd === '2026-10-05', lcd).toBe(true);        // скользящее начало ушло бы в август
      expect(sectionWindowDays({ year: 2026, month: 8 }, w), lcd).toBeNull();                       // ни одного дня августа
      expect(windowMonths(w).some((k) => k.year === 2026 && k.month <= 8), lcd).toBe(false);
      expect(sectionWindowDays({ year: 2026, month: 9 }, w)!.fromDay, lcd).toBe('2026-09-01');      // 01.09 — первый сверяемый день
    }
  });
  it('будущие окна ведут себя обычно: эпоха перестаёт связывать с LCD 06.10, дальше — чистые скользящие 35 дней', () => {
    expect(reconcileWindow('2026-10-06')).toMatchObject({ from: '2026-09-02', rollingFrom: '2026-09-02' });
    expect(reconcileWindow('2026-12-15')).toMatchObject({ from: '2026-11-11', rollingFrom: '2026-11-11', epoch: '2026-09-01' });
    expect(reconcileWindow('2027-03-01')).toMatchObject({ from: '2027-01-26', rollingFrom: '2027-01-26' });
    for (const lcd of ['2026-10-06', '2026-12-15', '2027-03-01', '2028-02-29']) { const w = reconcileWindow(lcd); expect(daysBetweenIso(w.from, w.to) + 1, lcd).toBe(35); }
  });
  it('сентябрь сверяется из октября (LCD 02.10), а секция августа остаётся побайтно прежней', async () => {
    const book = buildBook('2026-08-31', { august: true });
    const runner = new ReconRunner('2026-09-30', reconFactsFor('2026-09-01', '2026-09-30'), '2026-09-01');
    await run(book, runner);                                                                       // сентябрь заполнен
    const august = sha(book.section('2026-08'));
    runner.lcd = '2026-10-02'; runner.facts = patch(reconFactsFor('2026-09-01', '2026-10-02'), SEPT_NM, '2026-09-17', { cancels: 2 });
    await run(book, runner);                                                                       // LCD в октябре, ремонт в сентябре
    expect(book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.cancels))).toBe(2);
    expect(runner.ledger.at(-1)).toMatchObject({ monthKey: '2026-09', businessDate: '2026-09-17', field: 'cancels', status: 'REPAIRED' });
    expect(sha(book.section('2026-08'))).toBe(august);
    const touched = book.batchWrites.flat().map((w) => Number(/!\D+(\d+):/.exec(w.range)?.[1] ?? 0)).filter((r) => r > 0);
    expect(Math.min(...touched)).toBeGreaterThanOrEqual(735);                                       // ни одной записи выше заголовка сентября
  });
  it('источник отдал строки раньше эпохи (ошибка вью) → RECON_BEFORE_EPOCH до любой записи; август не тронут', async () => {
    const book = buildBook('2026-08-31', { august: true });
    const august = sha(book.section('2026-08'));
    const runner = new ReconRunner('2026-09-18', reconFactsFor('2026-08-29', '2026-09-18'), '2026-09-01');
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'RECON_BEFORE_EPOCH' });
    expect([book.batchWrites.length, runner.ledger.length, sha(book.section('2026-08'))]).toEqual([0, 0, august]);
  });
  it('вью с другой эпохой или окном, уходящим в август → RECON_WINDOW_INCONSISTENT до любой записи', async () => {
    const book = buildBook('2026-08-31', { august: true });
    const august = sha(book.section('2026-08'));
    const runner = new ReconRunner('2026-09-18', reconFactsFor('2026-09-01', '2026-09-18'), '2026-09-01');
    runner.windowOverride = { from: '2026-08-15', to: '2026-09-18', epoch: '2026-08-01' };
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'RECON_WINDOW_INCONSISTENT' });
    runner.windowOverride = { from: '2026-09-01', to: '2026-09-18', epoch: '2026-08-01' };          // окно верное, эпоха чужая — тоже отказ
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'RECON_WINDOW_INCONSISTENT' });
    expect([book.batchWrites.length, sha(book.section('2026-08'))]).toEqual([0, august]);
  });
  it('вторая линия защиты — чистая функция: дата раньше эпохи в плане или источнике = отказ', () => {
    expect(() => assertNotBeforeEpoch(['2026-09-01', '2026-10-02'], 'x')).not.toThrow();
    expect(() => assertNotBeforeEpoch(['2026-09-01', '2026-08-31'], 'x')).toThrow(/2026-08-31/);
    try { assertNotBeforeEpoch(['2026-08-31'], 'x'); } catch (e) { expect((e as LoaderError).code).toBe('RECON_BEFORE_EPOCH'); }
  });
});

/* ───────────────────────── режимы ───────────────────────── */

describe('режимы: off = поведение 2.0.0, observe = только журнал, write = сверка окна', () => {
  it('off (по умолчанию): слой сверки не читается, прошлый месяц не трогается, журнал ремонта не используется', async () => {
    const book = buildBook('2026-08-31');
    const runner = new ReconRunner('2026-10-02', reconFactsFor('2026-09-01', '2026-10-02'), '2026-09-01');
    // 2.3.0: книга не закрыла сентябрь, кандидат в октябре — off закрыть сентябрь не может → отказ ДО записи (не тихий перескок)
    await expect(run(book, runner, 'off')).rejects.toMatchObject({ code: 'MONTH_END_UNCLOSED' });
    expect([book.batchWrites.length, book.lcdSerial]).toEqual([0, isoToSerialG10('2026-08-31')]);
    book.lcdSerial = isoToSerialG10('2026-09-30');                                                 // сентябрь в книге закрыт
    await run(book, runner, 'off');
    expect(runner.queries.some((q) => /RECON|REPAIR_LEDGER|INTEGRITY_ISSUES/.test(q))).toBe(false);
    expect(book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.orders))).toBe('');       // сентябрь не тронут
    expect(book.get(octRow(book, '2026-10-02'), blockCol(5, OFFSET.orders))).not.toBe('');     // месяц LCD записан
    expect(cfg('').unitkaReconcileMode).toBe('off');
    expect(cfg('wirte')).toMatchObject({ unitkaReconcileMode: 'off', unitkaReconcileModeInvalid: 'wirte' });   // опечатка = off
  });
  it('observe: пишет как off; план сверки и будущие записи журнала — только в лог', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 1 });
    const before = book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.cancels));
    const { lines } = await run(book, runner, 'observe');
    expect(book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.cancels))).toBe(before);
    expect(runner.ledger).toHaveLength(0);
    const p = lines.find((l) => l.event === 'unitka_reconcile_plan')!.fields as { mode: string; repairs_planned: number; sections: Array<{ month: string; cells_planned: number }> };
    expect([p.mode, p.repairs_planned, p.sections.map((s) => [s.month, s.cells_planned])]).toEqual(['observe', 1, [['2026-09', 1]]]);
  });
  it('observe + Guard: repair_available = true и sheet_financial_valid = false, пока ремонт не записан; write снимает оба', async () => {
    const { book, runner } = await seeded();
    const nm = SEPT_NMS[18]!, day = '2026-09-17';
    const gap = (over: Record<string, unknown>) => ({ marketplace: 'WB', nm_id: nm, internal_sku: 's', product_name: 'p', day, last_closed_date: '2026-10-02', orders_unitka: 1, cancels_unitka: 0, orders_source: 'FUNNEL_API',
      orders_funnel: 1, fact_order_rows: null, fact_order_qty: null, observed_price_diagnostic: null, observed_price_at: null, storage_value: 0, storage_date_covered: true, divergence_class: 'ONLY_FUNNEL',
      factual_order_price: 1120, price_source: 'FUNNEL_FALLBACK', price_state: 'PRESENT', funnel_orders_sum: 1120, same_day_cancel_qty: 0, stock_date_covered: true, sku_active: true, ...over });
    book.set(septRow(book, day), blockCol(18, OFFSET.price), '');                                   // лист: заказ без цены
    book.set(septRow(book, day), blockCol(18, OFFSET.orders), 1);
    runner.facts = patch(runner.facts, nm, day, { orders: 1, price: 1120, priceSource: 'FUNNEL_FALLBACK', factOrderQty: 0, funnelOrders: 1, funnelOrdersSum: 1120 });
    runner.integrityRows = [gap({})]; runner.cogsRows = [];
    const obs = await run(book, runner, 'observe', { UNITKA_INTEGRITY_MODE: 'observe' });
    const seen = obs.lines.filter((l) => l.event === 'unitka_integrity').at(-1)!.fields as { repair_available_sku_days: number; financially_invalid_rows: number; price_provenance: { NOT_ON_SHEET: number } };
    expect([seen.repair_available_sku_days, seen.financially_invalid_rows, seen.price_provenance.NOT_ON_SHEET]).toEqual([1, 1, 1]);
    expect(book.get(septRow(book, day), blockCol(18, OFFSET.price))).toBe('');                     // observe ничего не записал
    const wr = await run(book, runner, 'write', { UNITKA_INTEGRITY_MODE: 'observe' });
    const after = wr.lines.filter((l) => l.event === 'unitka_integrity').at(-1)!.fields as { repair_available_sku_days: number; financially_invalid_rows: number };
    expect(book.get(septRow(book, day), blockCol(18, OFFSET.price))).toBe(1120);
    expect([after.repair_available_sku_days, after.financially_invalid_rows]).toEqual([0, 0]);
  });
  it('окно во вью ≠ окну в коде → RECON_WINDOW_INCONSISTENT до любой записи', async () => {
    const { book, runner } = await seeded();
    runner.windowOverride = { from: '2026-08-29', to: '2026-10-02', epoch: '2026-08-01' };
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'RECON_WINDOW_INCONSISTENT' });
    expect(book.batchWrites).toHaveLength(0);
  });
});

/* ───────────────────────── граница месяца ───────────────────────── */

describe('сверка через границу месяца: сентябрь-наследие + октябрь Calendar V2', () => {
  it('первый проход заполняет окно: сентябрь (24 блока, VP — разделитель) и октябрь (25 блоков) — одной записью', async () => {
    const book = buildBook('2026-08-31');
    const runner = new ReconRunner('2026-10-02', reconFactsFor('2026-09-01', '2026-10-02'), '2026-09-01');
    await run(book, runner);
    // Gate 10: данные обоих месяцев — ОДНИМ values.batchUpdate; LCD — отдельной записью после проверки.
    // Догон через границу месяца (книга на 31.08): сводка сентября сверена уже ПОСЛЕ коммита.
    expect(book.batchWrites).toHaveLength(2);
    expect(book.batchWrites[0]!.map((w) => w.range)).not.toContain('LAST_CLOSED_DATE');
    expect(book.batchWrites[1]!.map((w) => w.range)).toContain('LAST_CLOSED_DATE');
    expect(book.lcdSerial).toBe(isoToSerialG10('2026-10-02'));
    expect(book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.opens))).toBe(27);
    expect(book.get(octRow(book, '2026-10-02'), blockCol(24, OFFSET.opens))).toBe(12);          // блок 25 (909951444) — только в октябре
    expect(book.get(septRow(book, '2026-09-17'), blockCol(24, OFFSET.opens))).toBe('');         // в сентябре блока нет — не пишем
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS', engineVersion: 'unitka-engine/2.3.0' });
  });
  it('LCD в октябре: источник пересмотрел 17.09 → поправка в сентябрьской секции, одна ячейка, одна запись журнала', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 2 });
    const { rowsLoaded } = await run(book, runner);
    expect(rowsLoaded).toBe(1);
    expect(book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.cancels))).toBe(2);
    expect(runner.ledger).toHaveLength(1);
    expect(runner.ledger[0]).toMatchObject({ businessDate: '2026-09-17', nmId: SEPT_NM, field: 'cancels', oldValue: '0', newValue: '2', reason: 'SOURCE_REVISED', status: 'REPAIRED', monthKey: '2026-09', source: 'PROXY_FACT_ORDERS' });
    expect(runner.ledger[0]!.cellA1).toBe(`${colA1(blockCol(5, OFFSET.cancels))}${septRow(book, '2026-09-17')}`);
    expect(runner.ledger[0]!.sourceAsOf).toBe('2026-09-18 04:01:00+00');
  });
  it('35-й день входит, 36-й заморожен: LCD 06.10 — поправка 01.09 не планируется, 02.09 — планируется', async () => {
    const { book, runner } = await seeded();
    runner.lcd = '2026-10-06'; runner.windowFrom = '2026-09-02';
    runner.facts = patch(patch(reconFactsFor('2026-09-01', '2026-10-06'), SEPT_NM, '2026-09-01', { views: 9999 }), SEPT_NM, '2026-09-02', { views: 8888 }).filter((f) => f.date >= '2026-09-02' || f.date === '2026-09-01');
    await run(book, runner);
    expect(book.get(septRow(book, '2026-09-01'), blockCol(5, OFFSET.views))).toBe(105);        // заморожено
    expect(book.get(septRow(book, '2026-09-02'), blockCol(5, OFFSET.views))).toBe(8888);
    expect(runner.ledger.map((r) => r.businessDate)).toEqual(['2026-09-02']);
  });
  it('будущий месяц Calendar V2: LCD в ноябре → сверяется октябрь (V2), сентябрь вне окна', async () => {
    const book = buildBook('2026-08-31', { november: true });
    book.lcdSerial = isoToSerialG10('2026-10-06');            // 2.3.0: книга закрыта по 06.10 — иначе кандидат перешагнул бы незакрытые дни вне окна
    const runner = new ReconRunner('2026-11-10', reconFactsFor('2026-10-07', '2026-11-10'), '2026-10-07');
    const { lines } = await run(book, runner);
    const p = lines.find((l) => l.event === 'unitka_reconcile_plan')!.fields as { sections: Array<{ month: string; from: string; to: string; blocks: number }>; refused: unknown[] };
    expect(p.sections).toMatchObject([{ month: '2026-10', from: '2026-10-07', to: '2026-10-31', blocks: 25 }]);
    expect(p.refused).toEqual([]);
    expect(book.get(octRow(book, '2026-10-07'), blockCol(24, OFFSET.opens))).toBe(17);
    expect(book.get(octRow(book, '2026-10-06'), blockCol(24, OFFSET.opens))).toBe('');          // вне окна
    expect(book.get(septRow(book, '2026-09-30'), blockCol(5, OFFSET.opens))).toBe('');          // сентябрь заморожен
  });
  it('прошлая секция не проходит контракт → отказ с кодом только для неё; месяц LCD пишется как обычно', async () => {
    // Сентябрь в книге закрыт (seeded): незакрытых дней прошлого месяца нет — отказ секции касается только сверки.
    // Если бы книга не закрыла сентябрь, тот же отказ дал бы MONTH_END_UNCLOSED (unitka_month_end_guards.test.ts).
    const { book, runner } = await seeded();
    const sept = book.section('2026-09');
    sept.grid[1]![SUMMARY.date - 1] = 'не дата';                                                 // сломана шапка сентября
    const before = book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.orders));
    runner.lcd = '2026-10-03'; runner.facts = patch(reconFactsFor('2026-09-01', '2026-10-03'), SEPT_NM, '2026-09-17', { orders: 7 });
    const { lines } = await run(book, runner);
    expect(lines.find((l) => l.event === 'unitka_reconcile_section_refused')!.fields).toMatchObject({ month: '2026-09', code: 'RECON_SECTION_INVALID' });
    expect(book.get(octRow(book, '2026-10-03'), blockCol(5, OFFSET.orders))).not.toBe('');
    expect(book.get(septRow(book, '2026-09-17'), blockCol(5, OFFSET.orders))).toBe(before);       // поправка в сломанной секции не пишется
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS' });
  });
  it('секции прошлого месяца нет в книге → RECON_SECTION_MISSING, прогон не падает', async () => {
    const { book, runner } = await seeded();
    book.sections = book.sections.filter((s) => s.geometry.monthKey !== '2026-09');
    runner.lcd = '2026-10-03'; runner.facts = reconFactsFor('2026-09-01', '2026-10-03');
    const { lines } = await run(book, runner);
    expect(lines.find((l) => l.event === 'unitka_reconcile_section_refused')!.fields).toMatchObject({ code: 'RECON_SECTION_MISSING' });
  });
  it('блок прошлого месяца без строк источника за часть дней не сверяется вовсе (и не стирается)', async () => {
    const { book } = await seeded();
    const snap = book.section('2026-09');
    const facts = reconFactsFor('2026-09-01', '2026-10-02').filter((f) => !(f.nmId === SEPT_NM && f.date === '2026-09-10'));
    const p = buildSectionRepairPlan({ snapshot: snap, facts, window: reconcileWindow('2026-10-02'), bookLcd: '2026-10-02', lcd: '2026-10-02' });
    expect(p.skippedBlocks).toEqual([{ nmId: SEPT_NM, reason: 'в слое сверки 29 дней из 30' }]);
    expect(p.expected.some((e) => e.nmId === SEPT_NM)).toBe(false);
    expect(p.cells).toHaveLength(0);
  });
});

/* ───────────────────────── цена ───────────────────────── */

describe('цена: основной источник Orders API, доказанный fallback — сумма заказов той же строки воронки', () => {
  const control = { nm: SEPT_NMS[18]!, slot: 18, date: '2026-09-17' };    // 930334396 в живой книге — слот 18
  const COGS_SLOT18 = 250.25;                                              // литерал COGS блока в формулах фикстуры
  const missing = (f: FactRow): Partial<FactRow> | null =>
    (f.nmId === control.nm && f.date === control.date ? { orders: 1, funnelOrders: 1, funnelOrdersSum: 1120, factOrderQty: 0, price: null, priceSource: null } : null);

  it('цена Orders API есть → пишется она, происхождение FACT_ORDERS', async () => {
    const { book } = await seeded();
    const row = septRow(book, '2026-09-17');                                                     // у блока 6 в этот день 1 заказ
    expect(book.get(row, blockCol(5, OFFSET.orders))).toBe(1);
    expect(book.get(row, blockCol(5, OFFSET.price))).toBe(1005);
  });
  it('контроль: заказ есть, цены нет нигде → ячейка пуста, прибыль строки = −(логистика + COGS) — финансово неверно', async () => {
    const { book } = await seeded(missing);
    const row = septRow(book, control.date);
    expect(book.get(row, blockCol(control.slot, OFFSET.price))).toBe('');
    expect(book.get(row, blockCol(control.slot, OFFSET.profitAll))).toBeCloseTo(-(60.5 + COGS_SLOT18) - 5.5 - 1.25, 6);
  });
  it('Orders API пуст, воронка совместима → FUNNEL_FALLBACK: цена 1120, зависимые поля пересчитаны, одна запись журнала, повтор — NO_CHANGE', async () => {
    const { book, runner } = await seeded(missing);
    const row = septRow(book, control.date);
    const col = blockCol(control.slot, OFFSET.price);
    const profitBefore = book.get(row, blockCol(control.slot, OFFSET.profitAll)) as number;
    const mtdBefore = book.get(book.section('2026-09').geometry.mtdRow, SUMMARY.profit) as number;
    runner.facts = patch(runner.facts, control.nm, control.date, { price: 1120, priceSource: 'FUNNEL_FALLBACK' });
    const first = await run(book, runner);
    expect(first.rowsLoaded).toBe(1);
    expect(book.get(row, col)).toBe(1120);
    const ai = 1120 - 1120 * 0.45 - 60.5 - 1120 * 0.02 - COGS_SLOT18;
    expect(book.get(row, blockCol(control.slot, OFFSET.unitProfit))).toBeCloseTo(ai, 6);
    const profitAfter = book.get(row, blockCol(control.slot, OFFSET.profitAll)) as number;
    expect(profitAfter).toBeCloseTo(ai - 5.5 - 1.25, 6);
    expect(profitAfter - profitBefore).toBeCloseTo(1120 * (1 - 0.45 - 0.02), 6);                 // цена · (1 − комиссия − налог)
    expect((book.get(book.section('2026-09').geometry.mtdRow, SUMMARY.profit) as number) - mtdBefore).toBeCloseTo(profitAfter - profitBefore, 6);
    expect(runner.ledger).toHaveLength(1);
    expect(runner.ledger[0]).toMatchObject({
      businessDate: control.date, nmId: control.nm, field: 'price', oldValue: null, newValue: '1120', reason: 'PRICE_FUNNEL_FALLBACK', status: 'REPAIRED',
      source: 'V_WB_FUNNEL_DAILY.orders_sum_rub (FUNNEL_FALLBACK)', sourceAsOf: '2026-09-18 06:31:00+00', cellA1: `${colA1(col)}${row}`,
    });
    const second = await run(book, runner);
    expect(second.rowsLoaded).toBe(0);
    expect(runner.ledger).toHaveLength(1);                                                        // идемпотентно: ни ячеек, ни записей
  });
  it('поздний приход Orders API заменяет fallback: 1120 → 1119.92 (SOURCE_REVISED, источник FACT_ORDERS); равное значение — без записи', async () => {
    const { book, runner } = await seeded((f) => (missing(f) ? { ...missing(f)!, price: 1120, priceSource: 'FUNNEL_FALLBACK' } : null));
    const row = septRow(book, control.date), col = blockCol(control.slot, OFFSET.price);
    expect(book.get(row, col)).toBe(1120);
    runner.facts = patch(runner.facts, control.nm, control.date, { price: 1120, priceSource: 'ORDERS_API', factOrderQty: 1 });
    expect((await run(book, runner)).rowsLoaded).toBe(0);                                         // то же значение — ничего не пишем
    expect(runner.ledger).toHaveLength(0);
    runner.facts = patch(runner.facts, control.nm, control.date, { price: 1119.92 });
    await run(book, runner);
    expect(book.get(row, col)).toBe(1119.92);
    expect(runner.ledger).toMatchObject([{ field: 'price', oldValue: '1120', newValue: '1119.92', reason: 'SOURCE_REVISED', source: 'FACT_ORDERS' }]);
  });
  it('слой обязан отдавать цену вместе с происхождением: рассогласование и неизвестное происхождение — BQ_SHAPE (fail-closed)', async () => {
    const mk = (row: Record<string, unknown>): UnitkaBq => new UnitkaBq({ projectId: 'p', query: async () => [row] } as never, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS');
    const base = { nm_id: 1, date_msk: '2026-09-17', orders: 1, orders_source: 'FUNNEL_API', cancels_source: 'PROXY_FACT_ORDERS' };
    await expect(mk({ ...base, price: 1120, price_source: null }).reconFacts()).rejects.toMatchObject({ code: 'BQ_SHAPE' });
    await expect(mk({ ...base, price: null, price_source: 'FUNNEL_FALLBACK' }).reconFacts()).rejects.toMatchObject({ code: 'BQ_SHAPE' });
    await expect(mk({ ...base, price: 1120, price_source: 'PREVIOUS_DAY' }).reconFacts()).rejects.toMatchObject({ code: 'BQ_SHAPE' });
    const ok = { ...base, price: 1120, price_source: 'FUNNEL_FALLBACK', fact_order_qty: 0, funnel_orders: 1, funnel_orders_sum: 1120 };
    await expect(mk(ok).reconFacts()).resolves.toMatchObject([{ price: 1120, priceSource: 'FUNNEL_FALLBACK' }]);
  });
  it('fallback воронки ЗАПРЕЩЁН при несогласных счётчиках — вторая линия защиты в коде (вью уже не отдаёт такие строки)', async () => {
    const mk = (row: Record<string, unknown>): UnitkaBq => new UnitkaBq({ projectId: 'p', query: async () => [row] } as never, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS');
    const ok = { nm_id: 1, date_msk: '2026-09-17', orders: 2, orders_source: 'FUNNEL_API', cancels_source: 'PROXY_FACT_ORDERS', price: 750, price_source: 'FUNNEL_FALLBACK', fact_order_qty: 0, funnel_orders: 2, funnel_orders_sum: 1500 };
    await expect(mk(ok).reconFacts()).resolves.toHaveLength(1);
    const bad: Array<[string, Record<string, unknown>]> = [
      ['Orders API дал строку, счётчики 1 ≠ 2 — сумма воронки делиться не может', { fact_order_qty: 1 }],
      ['счётчик Unitka ≠ воронке', { orders: 3 }],
      ['счётчик Unitka взят не из воронки', { orders_source: 'ORDERS_API' }],
      ['суммы воронки нет', { funnel_orders_sum: null }],
      ['сумма воронки нулевая', { funnel_orders_sum: 0 }],
      ['заказов в воронке нет', { funnel_orders: 0, orders: 0 }],
      ['цена не равна сумме той же строки воронки', { price: 806 }],
    ];
    for (const [why, over] of bad) await expect(mk({ ...ok, ...over }).reconFacts(), why).rejects.toMatchObject({ code: 'BQ_SHAPE' });
  });
  it('Engine: строка с fallback вне условий → отказ до любой записи', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, control.nm, control.date, { price: 560, priceSource: 'FUNNEL_FALLBACK', factOrderQty: 1, funnelOrders: 2, funnelOrdersSum: 1120, orders: 2 });
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'BQ_SHAPE' });
    expect([book.batchWrites.length, runner.ledger.length]).toEqual([0, 0]);
  });
});

/* ───────────────────────── ручные поля ───────────────────────── */

describe('ручные поля защищены: СПП и блогеры не адресуются ни в одной секции', () => {
  it('введённые СПП (в т.ч. 0) и блогеры переживают историческую сверку; план не содержит их колонок', async () => {
    const { book, runner } = await seeded();
    const row = septRow(book, '2026-09-17');
    book.set(row, blockCol(5, OFFSET.spp), 27); book.set(row, blockCol(6, OFFSET.spp), 0); book.set(row, blockCol(5, OFFSET.bloggers), 2);
    book.set(octRow(book, '2026-10-01'), blockCol(5, OFFSET.spp), 31);
    runner.facts = runner.facts.map((f) => (f.date === '2026-09-17' || f.date === '2026-10-01' ? { ...f, views: (f.views ?? 0) + 1000, cancels: 1 } : f));
    await run(book, runner);
    expect([book.get(row, blockCol(5, OFFSET.spp)), book.get(row, blockCol(6, OFFSET.spp)), book.get(row, blockCol(5, OFFSET.bloggers)), book.get(octRow(book, '2026-10-01'), blockCol(5, OFFSET.spp))]).toEqual([27, 0, 2, 31]);
    const written = book.batchWrites.flat().map((w) => w.range);
    const manualCols = new Set([...Array(25).keys()].flatMap((s) => [colA1(blockCol(s, OFFSET.spp)), colA1(blockCol(s, OFFSET.bloggers))]));
    expect(written.some((r) => manualCols.has(/!([A-Z]+)\d/.exec(r)?.[1] ?? ''))).toBe(false);
    expect(runner.ledger.every((r) => !['spp', 'bloggers'].includes(String(r.field)))).toBe(true);
  });
  it('предохранитель: ячейка вне автоматических факт-колонок в плане сверки — отказ до записи', async () => {
    const { book } = await seeded();
    const s = book.section('2026-09');
    const blocks = [{ index: 0, slot: 0, start: 13, nmId: SEPT_NMS[0]!, title: 't' }];
    const cell = (col: number, row = s.geometry.firstDailyRow): PlannedCell => ({ row, col, want: 1, kind: 'fact', before: '', changeType: 'LATE_SOURCE_CORRECTION', reason: '' });
    expect(() => assertFactCellsOnly([cell(13 + OFFSET.orders)], blocks, s.geometry)).not.toThrow();
    for (const bad of [cell(13 + OFFSET.spp), cell(13 + OFFSET.bloggers), cell(13 + OFFSET.unitProfit), cell(SUMMARY.profit), cell(13 + OFFSET.orders, s.geometry.mtdRow), { ...cell(13 + OFFSET.commission), kind: 'commission' as const }]) {
      expect(() => assertFactCellsOnly([bad], blocks, s.geometry)).toThrow(LoaderError);
    }
  });
  it('ставки прошлого месяца не трогаются (решение владельца: поведение прежнее)', async () => {
    const { book, runner } = await seeded();
    const row = septRow(book, '2026-09-17');
    book.set(row, blockCol(5, OFFSET.commission), 0.41); book.set(row, blockCol(5, OFFSET.logistics), 55.55);   // «застывшие» сентябрьские ставки
    await run(book, runner);
    expect([book.get(row, blockCol(5, OFFSET.commission)), book.get(row, blockCol(5, OFFSET.logistics))]).toEqual([0.41, 55.55]);
    expect(book.get(octRow(book, '2026-10-01'), blockCol(5, OFFSET.commission))).toBe(0.45);      // месяц LCD — как раньше
    expect(book.batchWrites.flat().some((w) => new RegExp(`!${colA1(blockCol(5, OFFSET.commission))}7[3-6]\\d`).test(w.range))).toBe(false);
  });
});

/* ───────────────────────── зависимости ───────────────────────── */

describe('граф зависимостей: исправленный факт даёт согласованную строку (зависимые поля — формулы листа)', () => {
  const P: BlockFormulaParams = { start: 13, cogsTerm: '231.38', overhead: '100', stockProjection: 'guarded', storageProjection: 'none', mtdStockFamily: 'native' };
  const f = blockDayFormulas(P, 771);
  const refs = (off: number): string[] => [...(f.get(off) ?? '').matchAll(/\$?([A-Z]{1,3})771/g)].map((m) => m[1]!);
  const L = (off: number): string => colA1(13 + off);

  it('цена → цена с СПП, цена − комиссия, налог, внешняя реклама; → доходность 1 шт → доходность общая → на 1 шт; ДРР', () => {
    for (const off of [OFFSET.priceSpp, OFFSET.priceMinusComm, OFFSET.tax, OFFSET.adsOut]) expect(refs(off)).toContain(L(OFFSET.price));
    expect(refs(OFFSET.unitProfit)).toEqual(expect.arrayContaining([L(OFFSET.priceMinusComm), L(OFFSET.logistics), L(OFFSET.tax)]));
    expect(refs(OFFSET.profitAll)).toEqual(expect.arrayContaining([L(OFFSET.orders), L(OFFSET.unitProfit), L(OFFSET.adsIn), L(OFFSET.storage), L(OFFSET.cancels), L(OFFSET.logistics), L(OFFSET.adsOut)]));
    expect(refs(OFFSET.profit1)).toEqual(expect.arrayContaining([L(OFFSET.profitAll), L(OFFSET.orders)]));
    expect(refs(OFFSET.drr)).toEqual(expect.arrayContaining([L(OFFSET.adsIn), L(OFFSET.orders), L(OFFSET.bloggers), L(OFFSET.priceSpp)]));
  });
  it('COGS — терм внутри формул AI и Y (не факт-ячейка): Engine его не пишет, ручная СПП входит только в AC → ДРР', () => {
    expect(f.get(OFFSET.unitProfit)).toContain('-231.38)');
    expect(f.get(OFFSET.adsOut)).toContain('-(231.38+');
    expect(refs(OFFSET.priceSpp)).toContain(L(OFFSET.spp));
    expect([OFFSET.unitProfit, OFFSET.profitAll, OFFSET.tax].some((o) => refs(o).includes(L(OFFSET.spp)))).toBe(false); // СПП не влияет на прибыль
  });
  it.each([
    ['хранение пришло поздно', { storage: 25.5 }, (d: number) => expect(d).toBeCloseTo(-(25.5 - 1.25), 6)],
    ['поздняя отмена', { cancels: 1 }, (d: number, ai: number) => expect(d).toBeCloseTo(-(ai + 60.5 + REV_RATE), 6)],
    ['реклама пересчитана', { adsIn: 105.5 }, (d: number) => expect(d).toBeCloseTo(-100, 6)],
  ] as Array<[string, Partial<FactRow>, (delta: number, ai: number) => void]>)('%s → доходность общая и сводка пересчитываются согласованно', async (_n, change, check) => {
    const { book, runner } = await seeded();
    const date = '2026-09-17', row = septRow(book, date);                                        // у блока 6 в этот день 1 заказ
    const before = book.get(row, blockCol(5, OFFSET.profitAll)) as number;
    const sumBefore = book.get(row, SUMMARY.profit) as number;
    const ai = book.get(row, blockCol(5, OFFSET.unitProfit)) as number;
    runner.facts = patch(runner.facts, SEPT_NM, date, change);
    await run(book, runner);
    const after = book.get(row, blockCol(5, OFFSET.profitAll)) as number;
    check(after - before, ai);
    expect((book.get(row, SUMMARY.profit) as number) - sumBefore).toBeCloseTo(after - before, 6);
    expect(runner.ledger).toHaveLength(1);
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS' });                                // QA прошлой секции: лист = источник, сводка = Σ блоков
  });
  it('ставки месяца LCD изменились → пересчёт цены − комиссия и доходности в месяце LCD; в журнал ремонта ставки не идут', async () => {
    const { book } = await seeded();
    const row = octRow(book, '2026-10-02');
    const q = book.get(row, blockCol(5, OFFSET.orders)) as number, aa = book.get(row, blockCol(5, OFFSET.price)) as number;
    expect(q).toBeGreaterThan(0);
    expect(book.get(row, blockCol(5, OFFSET.priceMinusComm))).toBeCloseTo(aa * (1 - 0.45), 6);
  });
});

/* ───────────────────────── журнал ремонта ───────────────────────── */

describe('журнал ремонта: одна фактическая поправка — одна запись; без изменений — без записей', () => {
  it('NO_CHANGE → 0 ячеек, 0 записей; факты нового дня и ставки ремонтом не являются', async () => {
    const { book, runner } = await seeded();
    expect((await run(book, runner)).rowsLoaded).toBe(0);
    expect(runner.ledger).toHaveLength(0);
    runner.lcd = '2026-10-03'; runner.facts = reconFactsFor('2026-09-01', '2026-10-03');
    await run(book, runner);                                                                       // новый закрытый день 03.10
    expect(book.get(octRow(book, '2026-10-03'), blockCol(5, OFFSET.opens))).toBe(13);
    expect(runner.ledger).toHaveLength(0);
  });
  it('сбой записи в лист → ремонт НЕ объявлен состоявшимся: WRITE_FAILED, ни одной REPAIRED; повтор чинит и пишет REPAIRED', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 3 });
    book.failNextWrite = new LoaderError('Sheets API 503', 'SHEETS_API');
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'SHEETS_API' });
    expect(runner.ledger).toMatchObject([{ field: 'cancels', newValue: '3', status: 'WRITE_FAILED', repairedAt: null }]);
    expect(runner.ledger.filter((r) => r.status === 'REPAIRED')).toHaveLength(0);
    expect(runner.journal.at(-1)).toMatchObject({ qaStatus: 'FAIL', errorCode: 'SHEETS_API', cellsWritten: 0 });
    expect(runner.issues).toHaveLength(0);                                                          // снимок состояния «после ремонта» не пишется
    // повтор после сбоя: расхождение никуда не делось → тот же ремонт выполняется и только теперь становится REPAIRED
    await run(book, runner);
    expect(runner.ledger.map((r) => r.status)).toEqual(['WRITE_FAILED', 'REPAIRED']);
    expect(runner.ledger[1]).toMatchObject({ field: 'cancels', oldValue: String(runner.ledger[0]!.oldValue), newValue: '3' });
    expect(typeof runner.ledger[1]!.repairedAt).toBe('string');
    // идемпотентность успешного ремонта: третий прогон — NO_CHANGE, журнал не растёт
    const writes = book.batchWrites.length;
    await run(book, runner);
    expect([book.batchWrites.length, runner.ledger.length]).toEqual([writes, 2]);
  });
  it('лист подтвердил запись, но значения не применил («потерянная запись») → QA FAIL, APPLIED_UNVERIFIED, не REPAIRED', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 3 });
    book.dropNextWrite = true;
    await expect(run(book, runner)).rejects.toMatchObject({ code: expect.stringMatching(/MISMATCH|PARTIAL_WRITE/) });
    expect(runner.ledger.map((r) => r.status)).toEqual(['APPLIED_UNVERIFIED']);
    expect(runner.ledger[0]!.repairedAt).toBeNull();
    await run(book, runner);                                                                       // повтор: теперь запись применяется и проверяется
    expect(runner.ledger.map((r) => r.status)).toEqual(['APPLIED_UNVERIFIED', 'REPAIRED']);
  });
  it('запись прошла, перечитывание упало → APPLIED_UNVERIFIED: происхождение не теряется, успех не объявляется', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 3 });
    book.failReadAfterNextWrite(new LoaderError('Sheets API 500 on read-back', 'SHEETS_API'));
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'SHEETS_API' });
    expect(runner.ledger).toMatchObject([{ field: 'cancels', newValue: '3', status: 'APPLIED_UNVERIFIED', repairedAt: null }]);
    // значения уже в листе: следующий прогон расхождения не видит и REPAIRED задним числом не выдумывает
    await run(book, runner);
    expect(runner.ledger.map((r) => r.status)).toEqual(['APPLIED_UNVERIFIED']);
  });
  it('сбой журнала при неподтверждённой попытке исход прогона не маскирует', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 3 });
    book.failNextWrite = new LoaderError('Sheets API 503', 'SHEETS_API'); runner.ledgerInsertFails = true;
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'SHEETS_API' });                  // не LEDGER_WRITE_FAILED
    expect(runner.ledger).toHaveLength(0);
  });
  it('журнал недоступен → LEDGER_UNAVAILABLE до любой записи: ремонт без происхождения не выполняется', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 3 });
    runner.ledgerAvailable = false;
    await expect(run(book, runner)).rejects.toMatchObject({ code: 'LEDGER_UNAVAILABLE' });
    expect(book.batchWrites).toHaveLength(0);
  });
  it('журнал отказал ПОСЛЕ записи → LEDGER_WRITE_FAILED, поправки остаются в логе прогона', async () => {
    const { book, runner } = await seeded();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 3 });
    runner.ledgerInsertFails = true;
    const lines: LogLine[] = [];
    await expect(run(book, runner, 'write', {}, lines)).rejects.toMatchObject({ code: 'LEDGER_WRITE_FAILED' });
    expect((lines.find((l) => l.event === 'unitka_repairs')!.fields as { count: number }).count).toBe(1);
  });
  it('записи ремонта: причины LATE_FIRST_FILL / SOURCE_WITHDRAWN / SOURCE_REVISED; NO_CHANGE и FACT_CHANGE не пишутся', () => {
    const mk = (over: Partial<PlannedCell>): PlannedCell => ({ row: 753, col: 20, want: 5, kind: 'fact', nmId: 1, key: 'storage', date: '2026-09-17', source: 'RAW_WB_PAID_STORAGE', before: '', changeType: 'LATE_SOURCE_CORRECTION', reason: '', ...over });
    const recs = repairRecords([mk({}), mk({ before: 5, want: null, col: 21 }), mk({ before: 4, want: 5, col: 22 }), mk({ changeType: 'NO_CHANGE', col: 23 }), mk({ changeType: 'FACT_CHANGE', col: 24 }), mk({ kind: 'commission', changeType: 'MODEL_PARAMETER_REFRESH', col: 25 })],
      { runId: 'r', environment: 'prod', engineVersion: 'v', gitSha: 'g', detectedAt: 't', repairedAt: null, status: 'PLANNED_NOT_WRITTEN', factOf: () => undefined });
    expect(recs.map((r) => [r.cellA1, r.reason, r.oldValue, r.newValue])).toEqual([['T753', 'LATE_FIRST_FILL', null, '5'], ['U753', 'SOURCE_WITHDRAWN', '5', null], ['V753', 'SOURCE_REVISED', '4', '5']]);
    expect(new Set(recs.map((r) => r.repairId)).size).toBe(3);
  });
});

/* ───────────────────────── снимок issue / действительность ───────────────────────── */

describe('наблюдаемость: состояния не схлопываются, действительность строки считается по дням', () => {
  const issue = (over: Record<string, unknown>) => ({ marketplace: 'WB' as const, nmId: 1, day: '2026-09-17', field: 'price', code: 'PRICE_MISSING_WITH_ORDERS' as const, severity: 'ERROR' as const,
    blocking: true, financialInvalid: true, source: 's', sourceValue: null, diagnosticValue: null, dependentFields: [], message: 'm', ...over });
  it('INFO в снимок не идёт (кроме происхождения цены); issue уровня SKU разворачивается по недействительным дням', () => {
    const recs = issueRecords([
      issue({}), issue({ code: 'PRICE_MISSING_NO_ORDERS', severity: 'INFO', financialInvalid: false }), issue({ code: 'PRICE_FUNNEL_FALLBACK', severity: 'INFO', financialInvalid: false }),
      issue({ code: 'COGS_SOURCE_MISMATCH', day: null, invalidDays: ['2026-09-16', '2026-09-17'] }), issue({ code: 'SPP_MISSING', severity: 'MANUAL_REQUIRED', financialInvalid: false }),
      issue({ code: 'STOCK_SNAPSHOT_MISSING', severity: 'NOT_AVAILABLE', nmId: null, financialInvalid: false }), issue({ code: 'ORDERS_SOURCE_DIVERGENCE', severity: 'EXPECTED_DELAY', financialInvalid: false }),
    ] as never, { runId: 'r', environment: 'prod', evaluatedAt: 't', phase: 'POST_WRITE' });
    // маркер — ПОСЛЕДНЕЙ строкой: «снимок записан целиком»; несёт режим сверки и число строк
    expect(recs.at(-1)).toMatchObject({ issueKey: null, code: 'RUN_MARKER', state: 'RUN', businessDate: null, nmId: null, sourceValue: 'reconcile_mode=write; issue_rows=7' });
    expect(recs.slice(0, -1).map((r) => [r.issueKey, r.state, r.financialValid])).toEqual([
      ['2026-09-17|1|PRICE_MISSING_WITH_ORDERS', 'DATA_ERROR', false], ['2026-09-17|1|PRICE_FUNNEL_FALLBACK', 'INFO', true],
      ['2026-09-16|1|COGS_SOURCE_MISMATCH', 'DATA_ERROR', false], ['2026-09-17|1|COGS_SOURCE_MISMATCH', 'DATA_ERROR', false],
      ['2026-09-17|1|SPP_MISSING', 'MANUAL_REQUIRED', true], ['2026-09-17|-|STOCK_SNAPSHOT_MISSING', 'NOT_AVAILABLE', true], ['2026-09-17|1|ORDERS_SOURCE_DIVERGENCE', 'LATE_DATA', true],
    ]);
  });
  it('financialValidity: строка недействительна, если её покрывает хоть одна фин. недействительная issue', () => {
    const v = financialValidity([issue({}), issue({ code: 'SPP_MISSING', severity: 'MANUAL_REQUIRED', financialInvalid: false, day: '2026-09-18' }), issue({ code: 'COGS_SOURCE_MISMATCH', day: null, invalidDays: ['2026-09-19'] })] as never);
    expect([v.get('1|2026-09-17'), v.get('1|2026-09-18'), v.get('1|2026-09-19')]).toEqual([{ valid: false, codes: ['PRICE_MISSING_WITH_ORDERS'] }, { valid: true, codes: [] }, { valid: false, codes: ['COGS_SOURCE_MISMATCH'] }]);
  });
  it('write + Guard observe: снимок issue пишется после прогона; сбой снимка прогон не роняет', async () => {
    const { book, runner } = await seeded();
    runner.integrityRows = []; runner.cogsRows = [];
    await run(book, runner, 'write', { UNITKA_INTEGRITY_MODE: 'observe' });
    expect(runner.journal[0]).toMatchObject({ qaStatus: 'PASS' });
    expect(runner.issues.filter((i) => i.code === 'RUN_MARKER')).toHaveLength(1);                   // маркер — даже когда issue нет
    expect(runner.queries.some((q) => q.includes('V_UNITKA_RECON_INTEGRITY'))).toBe(true);        // Guard читает слой сверки, а не старую вью
  });
});

/* ───────────────────────── снимок наблюдаемости по режимам ───────────────────────── */

describe('снимок issue — состояние наблюдаемости: observe пишет ТОЛЬКО его; write — как раньше; off — ничего', () => {
  const GUARD = { UNITKA_INTEGRITY_MODE: 'observe' };
  const nm = SEPT_NMS[18]!, day = '2026-09-17';
  const gapRow = (over: Record<string, unknown> = {}): Record<string, unknown> => ({ marketplace: 'WB', nm_id: nm, internal_sku: 's', product_name: 'p', day, last_closed_date: '2026-10-02', orders_unitka: 1, cancels_unitka: 0,
    orders_source: 'FUNNEL_API', orders_funnel: 1, fact_order_rows: null, fact_order_qty: null, observed_price_diagnostic: null, observed_price_at: null, storage_value: 0, storage_date_covered: true,
    divergence_class: 'ONLY_FUNNEL', factual_order_price: 1120, price_source: 'FUNNEL_FALLBACK', price_state: 'PRESENT', funnel_orders_sum: 1120, same_day_cancel_qty: 0, stock_date_covered: true, sku_active: true, ...over });
  /** Книга, где суточная работа уже сделана, а ремонт сверки ждёт: заказ 17.09 без цены в листе, источник цену знает. */
  async function pendingRepair(): Promise<{ book: MemoryBook; runner: ReconRunner }> {
    const { book, runner } = await seeded();
    book.set(septRow(book, day), blockCol(18, OFFSET.price), ''); book.set(septRow(book, day), blockCol(18, OFFSET.orders), 1);
    runner.facts = patch(runner.facts, nm, day, { orders: 1, price: 1120, priceSource: 'FUNNEL_FALLBACK', factOrderQty: 0, funnelOrders: 1, funnelOrdersSum: 1120 });
    runner.integrityRows = [gapRow()]; runner.cogsRows = [];
    return { book, runner };
  }
  const markerOf = (runner: ReconRunner): Record<string, unknown> | undefined => runner.issues.find((r) => r.code === 'RUN_MARKER');

  it('observe: сохраняет снимок issue с DATA_ERROR и маркером; 0 ячеек листа, 0 записей журнала ремонта, журнал даже не опрашивается', async () => {
    const { book, runner } = await pendingRepair();
    const priceCell = (): unknown => book.get(septRow(book, day), blockCol(18, OFFSET.price));
    runner.queries = [];                                                                          // подготовка книги шла в режиме write — её запросы не в счёт
    const { lines } = await run(book, runner, 'observe', GUARD);
    expect(book.batchWrites).toHaveLength(0);                                                     // ни одной ячейки: суточный план пуст, ремонт observe не пишет
    expect(priceCell()).toBe('');
    expect(runner.ledger).toHaveLength(0);                                                        // ни REPAIRED, ни какой-либо другой записи ремонта
    expect(runner.queries.some((q) => q.includes('UNITKA_REPAIR_LEDGER'))).toBe(false);
    expect(runner.issues.at(-1)).toMatchObject({ code: 'RUN_MARKER', issueKey: null, environment: 'prod', sourceValue: `reconcile_mode=observe; issue_rows=${runner.issues.length - 1}` });
    expect(runner.issues.filter((r) => r.code === 'RUN_MARKER')).toHaveLength(1);
    expect(runner.issues.find((r) => r.code === 'PRICE_NOT_ON_SHEET')).toMatchObject({ state: 'DATA_ERROR', financialValid: false, businessDate: day, nmId: nm, diagnosticValue: 'repair_available=true; sheet_financial_valid=false' });
    expect(new Set(runner.issues.map((r) => r.runId)).size).toBe(1);                              // снимок одного прогона
    expect(lines.find((l) => l.event === 'unitka_issue_snapshot')!.fields).toMatchObject({ reconcile_mode: 'observe', rows: runner.issues.length });
    expect(JSON.parse(String(runner.journal.at(-1)!.qaJson)).reconcile).toMatchObject({ mode: 'observe', repairs_planned: 1, repairs_recorded: 0, issue_snapshot: 'PERSISTED' });
  });
  it('observe пишет в лист ровно то же, что off (суточная работа), — ремонт сверки не добавляет ни ячейки', async () => {
    const writes = async (mode: string): Promise<string> => {
      const book = buildBook('2026-08-31'); const runner = new ReconRunner('2026-09-30', reconFactsFor('2026-09-01', '2026-09-30'), '2026-09-01');
      await run(book, runner, 'off');
      runner.lcd = '2026-10-02'; runner.facts = patch(reconFactsFor('2026-09-01', '2026-10-02'), SEPT_NM, '2026-09-17', { cancels: 3 });   // поздняя поправка сентября + новые дни октября
      book.batchWrites = []; await run(book, runner, mode, GUARD);
      return JSON.stringify(book.batchWrites);
    };
    const off = await writes('off'), observe = await writes('observe'), write = await writes('write');
    expect(observe).toBe(off);
    expect(write).not.toBe(off);                                                                  // write добавляет поправку прошлого месяца
  });
  it('observe без DATA_ERROR: снимок и маркер всё равно пишутся — «чисто» отличимо от «оценки не было»', async () => {
    const { book, runner } = await seeded();
    runner.integrityRows = []; runner.cogsRows = [];
    await run(book, runner, 'observe', GUARD);
    expect(markerOf(runner)).toMatchObject({ sourceValue: `reconcile_mode=observe; issue_rows=${runner.issues.length - 1}` });
    expect(runner.issues.filter((r) => r.state === 'DATA_ERROR')).toHaveLength(0);
    // прогон вообще без issue оставляет ровно одну строку — маркер
    expect(issueRecords([], { runId: 'r', environment: 'prod', evaluatedAt: 't', phase: 'POST_WRITE', reconcileMode: 'observe' })).toMatchObject([{ issueKey: null, code: 'RUN_MARKER', sourceValue: 'reconcile_mode=observe; issue_rows=0' }]);
  });
  it('write: поведение прежнее — ремонт, REPAIRED в журнале, затем снимок с маркером режима write', async () => {
    const { book, runner } = await pendingRepair();
    await run(book, runner, 'write', GUARD);
    expect(book.get(septRow(book, day), blockCol(18, OFFSET.price))).toBe(1120);
    expect(runner.ledger).toMatchObject([{ field: 'price', newValue: '1120', status: 'REPAIRED', reason: 'PRICE_FUNNEL_FALLBACK' }]);
    expect(markerOf(runner)).toMatchObject({ sourceValue: `reconcile_mode=write; issue_rows=${runner.issues.length - 1}` });
    expect(runner.issues.some((r) => r.code === 'PRICE_NOT_ON_SHEET')).toBe(false);               // цена в листе — строка действительна
  });
  it('off: ни снимка, ни журнала ремонта, ни чтения слоя сверки — даже при включённом Guard', async () => {
    const { book, runner } = await pendingRepair();
    runner.queries = [];
    await run(book, runner, 'off', GUARD);
    expect([runner.issues.length, runner.ledger.length]).toEqual([0, 0]);
    expect(runner.queries.some((q) => /RECON|REPAIR_LEDGER|INTEGRITY_ISSUES/.test(q))).toBe(false);
    expect(JSON.parse(String(runner.journal.at(-1)!.qaJson)).reconcile).toBeUndefined();
  });
  it('observe без Guard (UNITKA_INTEGRITY_MODE=off): оценки нет → снимка нет, явное предупреждение; статус останется NO_RUN_YET', async () => {
    const { book, runner } = await pendingRepair();
    const { lines } = await run(book, runner, 'observe');
    expect(runner.issues).toHaveLength(0);
    expect(lines.some((l) => l.event === 'unitka_issue_snapshot_skipped' && l.level === 'warn')).toBe(true);
    expect(JSON.parse(String(runner.journal.at(-1)!.qaJson)).reconcile.issue_snapshot).toBe('SKIPPED_GUARD_OFF');
  });
  it('SHADOW не пишет снимок никогда (прав у sa-loaders-shadow нет и не нужно)', async () => {
    const { runner } = await pendingRepair();
    const book = buildBook('2026-10-02', { readonly: true });
    const config = loadConfig({ ENVIRONMENT: 'shadow', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', LOADER_NAME: 'unitka', UNITKA_SPREADSHEET_ID: 'x', UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', UNITKA_INTEGRITY_MODE: 'observe' });
    const ctx: LoaderContext = { config, logger: recordingLogger([]), logicalPeriod: 'p', targetDate: 'p', runId: 'shadow-1' };
    await unitkaLoader(ctx, { makeRunner: () => runner, makeSheets: () => book, now: () => new Date('2026-10-03T07:00:00Z') }).catch(() => undefined);
    expect([runner.issues.length, runner.ledger.length, book.batchWrites.length]).toEqual([0, 0, 0]);
    expect(runner.queries.some((q) => q.includes('INSERT INTO') && q.includes('UNITKA_INTEGRITY_ISSUES'))).toBe(false);
  });
  it('сбой записи снимка прогон не роняет и маркера не оставляет: оценка считается незавершённой', async () => {
    const { book, runner } = await pendingRepair();
    runner.issuesFailAfter = 0;
    const { lines } = await run(book, runner, 'observe', GUARD);
    expect(runner.journal.at(-1)).toMatchObject({ qaStatus: 'PASS', errorCode: null });
    expect(markerOf(runner)).toBeUndefined();
    expect(lines.some((l) => l.event === 'unitka_issue_snapshot_failed')).toBe(true);
    expect(JSON.parse(String(runner.journal.at(-1)!.qaJson)).reconcile.issue_snapshot).toBe('FAILED');
  });
  it('insertIssues: маркер — последним, ОТДЕЛЬНЫМ оператором; сбой пачки issue = маркера нет (снимок не выглядит завершённым)', async () => {
    const mk = (n: number) => issueRecords(Array.from({ length: n }, (_, i) => ({ marketplace: 'WB' as const, nmId: 1, day: `2026-09-${String((i % 28) + 1).padStart(2, '0')}`, field: 'spp', code: 'SPP_MISSING' as const,
      severity: 'MANUAL_REQUIRED' as const, blocking: false, financialInvalid: false, source: `s${i}`, sourceValue: null, diagnosticValue: null, dependentFields: [], message: 'm' })) as never,
      { runId: 'r', environment: 'prod', evaluatedAt: '2026-10-03T07:00:00Z', phase: 'POST_WRITE', reconcileMode: 'observe' });
    const ok = new ReconRunner('2026-10-02', [], '2026-09-01');
    await new UnitkaBq(ok, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS').insertIssues(mk(450));
    expect(ok.issueStatements).toEqual([200, 200, 50, 1]);                                        // три пачки issue и отдельно маркер
    expect(ok.issues.at(-1)).toMatchObject({ code: 'RUN_MARKER', sourceValue: 'reconcile_mode=observe; issue_rows=450' });
    const broken = new ReconRunner('2026-10-02', [], '2026-09-01'); broken.issuesFailAfter = 2;
    await expect(new UnitkaBq(broken, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS').insertIssues(mk(450))).rejects.toThrow(/issues insert failed/);
    expect([broken.issues.length, broken.issues.some((r) => r.code === 'RUN_MARKER')]).toEqual([400, false]);
  });
});

/* ───────────────────────── матрица способностей (Rollout Gate 4.1) ───────────────────────── */

/**
 * Три способности прогона независимы:
 *   SHEET_BUSINESS_WRITE — обычная суточная запись в книгу (prod И UNITKA_WRITE_ENABLED=1);
 *   OBSERVABILITY_WRITE  — снимок issue в wb_ops (prod И режим сверки ≠ off) — НЕ зависит от права записи в книгу;
 *   REPAIR_EXECUTION     — исторические поправки и журнал ремонта (SHEET_BUSINESS_WRITE И режим write).
 * Дефект Gate 4: контролируемый production-observe с UNITKA_WRITE_ENABLED=0 уходил в ветку SHADOW,
 * оценивал целостность, но до снимка не доходил — таблица оставалась пустой, статус NO_RUN_YET.
 */
describe('матрица способностей: запись в книгу, снимок наблюдаемости и исполнение ремонта разделены', () => {
  const GUARD = { UNITKA_INTEGRITY_MODE: 'observe' };
  const nm = SEPT_NMS[18]!, day = '2026-09-17';
  const gapRow = (): Record<string, unknown> => ({ marketplace: 'WB', nm_id: nm, internal_sku: 's', product_name: 'p', day, last_closed_date: '2026-10-02', orders_unitka: 1, cancels_unitka: 0,
    orders_source: 'FUNNEL_API', orders_funnel: 1, fact_order_rows: null, fact_order_qty: null, observed_price_diagnostic: null, observed_price_at: null, storage_value: 0, storage_date_covered: true,
    divergence_class: 'ONLY_FUNNEL', factual_order_price: 1120, price_source: 'FUNNEL_FALLBACK', price_state: 'PRESENT', funnel_orders_sum: 1120, same_day_cancel_qty: 0, stock_date_covered: true, sku_active: true });
  /** Книга после суточной работы, где ремонт цены сверкой ещё не записан: 17.09 заказ есть, цены в листе нет. */
  async function pending(): Promise<{ book: MemoryBook; runner: ReconRunner }> {
    const { book, runner } = await seeded();
    book.set(septRow(book, day), blockCol(18, OFFSET.price), ''); book.set(septRow(book, day), blockCol(18, OFFSET.orders), 1);
    runner.facts = patch(runner.facts, nm, day, { orders: 1, price: 1120, priceSource: 'FUNNEL_FALLBACK', factOrderQty: 0, funnelOrders: 1, funnelOrdersSum: 1120 });
    runner.integrityRows = [gapRow()]; runner.cogsRows = [];
    runner.queries = []; book.batchWrites = []; runner.journal = [];
    return { book, runner };
  }
  /** Прогон в произвольной среде: ENVIRONMENT задаётся явно (канонический сигнал config.environment). */
  async function runIn(env: 'prod' | 'shadow', book: MemoryBook, runner: ReconRunner, over: Record<string, string>): Promise<LogLine[]> {
    const lines: LogLine[] = [];
    const config = loadConfig({ ENVIRONMENT: env, GCP_PROJECT_ID: 'proj', BQ_RAW_DATASET: 'wb_raw', GIT_SHA: 'sha-test', LOADER_NAME: 'unitka', UNITKA_SPREADSHEET_ID: 'x', ...over });
    const ctx: LoaderContext = { config, logger: recordingLogger(lines), logicalPeriod: 'p', targetDate: 'p', runId: `cap-${++seq}` };
    await unitkaLoader(ctx, { makeRunner: () => runner, makeSheets: () => book, now: () => new Date('2026-10-03T07:00:00Z') }, );
    return lines;
  }
  const snapshotOf = (runner: ReconRunner): string => JSON.parse(String(runner.journal.at(-1)!.qaJson)).reconcile?.issue_snapshot ?? 'ABSENT';
  const marker = (runner: ReconRunner): Record<string, unknown> | undefined => runner.issues.find((r) => r.code === 'RUN_MARKER');

  it('A. prod + write_enabled=0 + observe: снимок пишется, маркер последним, 0 записей в лист, 0 ремонтов, 0 записей журнала ремонта', async () => {
    const { book, runner } = await pending();
    const priceBefore = book.get(septRow(book, day), blockCol(18, OFFSET.price));
    const lines = await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    // снимок наблюдаемости сохранён, маркер — последней строкой
    expect(runner.issues.length).toBeGreaterThan(1);
    expect(runner.issues.at(-1)).toMatchObject({ code: 'RUN_MARKER', issueKey: null, environment: 'prod', sourceValue: `reconcile_mode=observe; issue_rows=${runner.issues.length - 1}` });
    expect(runner.issueStatements.at(-1)).toBe(1);                                                // маркер — отдельным оператором
    expect(runner.issues.filter((r) => r.code === 'RUN_MARKER')).toHaveLength(1);
    expect(runner.issues.find((r) => r.code === 'PRICE_NOT_ON_SHEET')).toMatchObject({ state: 'DATA_ERROR', financialValid: false });
    expect(new Set(runner.issues.map((r) => r.runId)).size).toBe(1);
    expect(snapshotOf(runner)).toBe('PERSISTED');
    // ничего не записано и не отремонтировано
    expect(book.batchWrites).toHaveLength(0);
    expect(book.get(septRow(book, day), blockCol(18, OFFSET.price))).toBe(priceBefore);
    expect(runner.ledger).toHaveLength(0);
    expect(runner.queries.some((q) => q.includes('UNITKA_REPAIR_LEDGER'))).toBe(false);            // журнал ремонта даже не опрашивается
    expect(runner.journal.at(-1)).toMatchObject({ mode: 'SHADOW', cellsWritten: 0 });
    expect(lines.some((l) => l.event === 'unitka_issue_snapshot')).toBe(true);
  });
  it('B. настоящая shadow-среда + observe: снимок подавляется НАМЕРЕННО (не отказом IAM); 0 записей везде', async () => {
    const { book, runner } = await pending();
    const lines = await runIn('shadow', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    expect([runner.issues.length, runner.ledger.length, book.batchWrites.length]).toEqual([0, 0, 0]);
    expect(marker(runner)).toBeUndefined();
    expect(runner.queries.some((q) => q.includes('INSERT INTO') && q.includes('UNITKA_INTEGRITY_ISSUES'))).toBe(false);
    expect(snapshotOf(runner)).toBe('SKIPPED_NOT_PRODUCTION');
    expect(lines.find((l) => l.event === 'unitka_issue_snapshot_skipped')!.fields).toMatchObject({ reconcile_mode: 'observe' });
    // Guard при этом оценку выполняет — наблюдаемость shadow остаётся в логе и журнале прогона
    expect(JSON.parse(String(runner.journal.at(-1)!.qaJson)).integrity.status).toBeDefined();
  });
  it('C. prod + write_enabled=1 + observe: суточный писатель работает как раньше, ремонтов 0, снимок пишется', async () => {
    const { book, runner } = await pending();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-10-02', { cancels: 4 });                     // обычная суточная поправка закрытого дня месяца LCD (октябрь)
    const lines = await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '1', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    expect(book.batchWrites.length).toBeGreaterThan(0);                                            // лист пишется
    expect(book.get(septRow(book, day), blockCol(18, OFFSET.price))).toBe('');                     // но ремонт сверки — нет
    expect(runner.ledger).toHaveLength(0);
    expect(snapshotOf(runner)).toBe('PERSISTED');
    expect(marker(runner)).toMatchObject({ sourceValue: `reconcile_mode=observe; issue_rows=${runner.issues.length - 1}` });
    expect(runner.journal.at(-1)).toMatchObject({ mode: 'WRITE' });
    expect(lines.some((l) => l.event === 'unitka_repairs')).toBe(false);
  });
  it('D. prod + режимы off: поведение прежнее — слой сверки не читается, снимка нет', async () => {
    const { book, runner } = await pending();
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '1' });
    expect([runner.issues.length, runner.ledger.length]).toEqual([0, 0]);
    expect(runner.queries.some((q) => /RECON|REPAIR_LEDGER|INTEGRITY_ISSUES/.test(q))).toBe(false);
    expect(JSON.parse(String(runner.journal.at(-1)!.qaJson)).reconcile).toBeUndefined();
    expect(snapshotOf(runner)).toBe('ABSENT');
  });
  it('E. сбой вставки issue → маркер НЕ пишется: завершённой оценки нет', async () => {
    const { book, runner } = await pending();
    runner.issuesFailAfter = 0;
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    expect(marker(runner)).toBeUndefined();
    expect(snapshotOf(runner)).toBe('FAILED');
    expect(runner.journal.at(-1)).toMatchObject({ qaStatus: 'SHADOW_MATCH', errorCode: null });    // прогон не падает
    expect(book.batchWrites).toHaveLength(0);
  });
  it('F. сбой вставки МАРКЕРА → строки issue есть, но завершённой оценки нет', async () => {
    const { book, runner } = await pending();
    const probe = await pending();                                                                 // сколько операторов issue даст этот снимок
    await runIn('prod', probe.book, probe.runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    const issueBatches = probe.runner.issueStatements.length - 1;
    runner.issuesFailAfter = issueBatches;                                                          // падает ровно на маркере
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    expect(runner.issues.length).toBeGreaterThan(0);
    expect(marker(runner)).toBeUndefined();
    expect(snapshotOf(runner)).toBe('FAILED');
  });
  it('G. прогон без DATA_ERROR: маркер всё равно пишется — «чисто» отличимо от «оценки не было»', async () => {
    const { book, runner } = await pending();
    runner.integrityRows = []; runner.cogsRows = [];
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    expect(runner.issues.filter((r) => r.state === 'DATA_ERROR')).toHaveLength(0);
    expect(marker(runner)).toMatchObject({ code: 'RUN_MARKER', state: 'RUN' });
    expect(snapshotOf(runner)).toBe('PERSISTED');
    expect(issueRecords([], { runId: 'r', environment: 'prod', evaluatedAt: 't', phase: 'POST_WRITE', reconcileMode: 'observe' })).toMatchObject([{ code: 'RUN_MARKER', sourceValue: 'reconcile_mode=observe; issue_rows=0' }]);
  });
  it('H. повтор контролируемого observe идемпотентен: лист не меняется, каждый прогон даёт свой снимок и свой маркер', async () => {
    const { book, runner } = await pending();
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    const firstRun = String(runner.issues.at(-1)!.runId), firstCount = runner.issues.length;
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'observe', ...GUARD });
    const markers = runner.issues.filter((r) => r.code === 'RUN_MARKER');
    expect(markers).toHaveLength(2);
    expect(new Set(markers.map((m) => m.runId)).size).toBe(2);                                     // разные прогоны не смешиваются
    expect(runner.issues.length).toBe(firstCount * 2);
    expect(String(runner.issues.at(-1)!.runId)).not.toBe(firstRun);
    expect([book.batchWrites.length, runner.ledger.length]).toEqual([0, 0]);
  });
  it('исполнение ремонта требует И права записи в книгу, И режима write: контролируемый observe его не получает', async () => {
    const { book, runner } = await pending();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-16', { cancels: 4 });
    // write + запись отключена → ремонт не исполняется, журнал ремонта не опрашивается
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '0', UNITKA_RECONCILE_MODE: 'write', ...GUARD });
    expect([runner.ledger.length, book.batchWrites.length]).toEqual([0, 0]);
    expect(runner.queries.some((q) => q.includes('UNITKA_REPAIR_LEDGER'))).toBe(false);
    // та же книга и те же источники с включённой записью → ремонт исполняется и попадает в журнал
    runner.queries = [];
    await runIn('prod', book, runner, { UNITKA_WRITE_ENABLED: '1', UNITKA_RECONCILE_MODE: 'write', ...GUARD });
    // два ремонта: цена 17.09 (её книга потеряла в pending()) и отмены 16.09
    expect(new Set(runner.ledger.map((r) => r.status))).toEqual(new Set(['REPAIRED']));
    expect(runner.ledger.map((r) => r.field).sort()).toEqual(['cancels', 'price']);
    expect(book.batchWrites.length).toBeGreaterThan(0);
  });
});

/* ───────────────────────── статически ───────────────────────── */

describe('статически: никаких подстановок цены и никакого литерала 231.38 в коде', () => {
  it('в исходниках Engine нет литерала себестоимости и запрещённых источников цены', async () => {
    const { readFileSync, readdirSync } = await import('node:fs');
    const dir = new URL('../src/loaders/unitka/', import.meta.url);
    for (const f of readdirSync(dir).filter((x) => x.endsWith('.ts'))) {
      const src = readFileSync(new URL(f, dir), 'utf8').replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '');
      expect(src, f).not.toMatch(/231[.,]38/);
      expect(src, f).not.toMatch(/V_WB_PRICES_CURRENT|seller_effective_price\s*\*|previousDayPrice|nextDayPrice|mtdAveragePrice/);
    }
  });
});
