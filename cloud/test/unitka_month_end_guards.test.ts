/**
 * UNITKA Engine 2.3.0 — защиты конца месяца (аудит 06.10.2026, Phase 3):
 *   A. закрытие незакрытых дней прошлого месяца до коммита LCD (инцидент 02.10: 30.09 остался пустым, LCD ушёл в 01.10);
 *   B. FACT_NOT_ON_SHEET — пустые факт-ячейки прошлых секций (раньше ловилась только цена);
 *   C. SKU_WITHOUT_BLOCK по деньгам (909951444: 61 открытие, 0 ₽ — больше не DATA_ERROR);
 *   D. режим controlled (реализован, не включён): политика записи прошлых месяцев;
 *   E. алерты INTEGRITY_DATA_ERROR и RECON_RESIDUAL_PERSISTENT.
 * Книга и BigQuery — в памяти (фикстуры сверки), ни Sheets, ни BigQuery.
 */
import { describe, it, expect } from 'vitest';
import { unitkaLoader, type UnitkaDeps } from '../src/loaders/unitka/index.js';
import {
  unclosedPreviousMonthDays, planMonthEndClose, controlledWritePolicy, sectionCellsReadback, buildSectionRepairPlan, reconcileWindow, repairRecords,
  CONTROLLED_MAX_CORRECTIONS, restrictedCorrectionRefusal,
} from '../src/loaders/unitka/reconcile.js';
import { factCellRules, coverageRules, moneyActivityFrom, type IntegrityFactsRow } from '../src/loaders/unitka/integrity.js';
import { OFFSET, FACT_KEYS, isoToSerial, type Block } from '../src/loaders/unitka/model.js';
import { dayRowOf, slotStart } from '../src/loaders/unitka/calendar.js';
import type { PlannedCell } from '../src/loaders/unitka/plan.js';
import type { FactRow } from '../src/loaders/unitka/bq.js';
import { loadConfig, type Config } from '../src/config.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';
import { SEPT_NMS, NEW_NM } from './unitka_calendar_fixture.js';
import { MemoryBook, ReconRunner, buildBook, reconFactsFor } from './unitka_reconcile_fixture.js';

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
async function run(book: MemoryBook, runner: ReconRunner, mode: string, extra: Record<string, string> = {}): Promise<{ rowsLoaded: number; lines: LogLine[] }> {
  const lines: LogLine[] = [];
  const ctx: LoaderContext = { config: cfg(mode, extra), logger: recordingLogger(lines), logicalPeriod: 'p', targetDate: 'p', runId: `meg-${++seq}` };
  const deps: UnitkaDeps = { makeRunner: () => runner, makeSheets: () => book, now: () => new Date('2026-10-03T07:00:00Z') };
  try {
    const res = await unitkaLoader(ctx, deps);
    return { rowsLoaded: res.rowsLoaded, lines };
  } catch (e) {
    (e as { lines?: LogLine[] }).lines = lines;
    throw e;
  }
}
const SEPT_NM = SEPT_NMS[5]!;                                   // блок 6 сентября (слот 5)
const col = (slot: number, off: number): number => slotStart(slot) + off;
const septRow = (book: MemoryBook, iso: string): number => dayRowOf(book.section('2026-09').geometry, Number(iso.slice(8)) - 1);
const octRow = (book: MemoryBook, iso: string): number => dayRowOf(book.section('2026-10').geometry, Number(iso.slice(8)) - 1);
const patch = (facts: FactRow[], nm: number, date: string, p: Partial<FactRow>): FactRow[] => facts.map((f) => (f.nmId === nm && f.date === date ? { ...f, ...p } : f));
const qaOf = (runner: ReconRunner, i = -1): Record<string, unknown> => JSON.parse(String(runner.journal.at(i)!.qaJson)) as Record<string, unknown>;
const events = (lines: LogLine[], event: string): LogLine[] => lines.filter((l) => l.event === event);

/**
 * Состояние 02.10.2026 до прогона kf6rr: сентябрь прожит до 29.09 включительно (факты записаны, LCD = 29.09),
 * источники уже дают 30.09 и 01.10 — кандидат LCD 01.10 в ДРУГОМ месяце. Суточная вью фактов (как в production)
 * отдаёт только месяц канонического LCD — октябрь.
 */
async function at2909(over: (f: FactRow) => Partial<FactRow> | null = () => null): Promise<{ book: MemoryBook; runner: ReconRunner }> {
  const book = buildBook('2026-08-31');
  const runner = new ReconRunner('2026-09-29', reconFactsFor('2026-09-01', '2026-09-29', over), '2026-09-01');
  await run(book, runner, 'write');
  expect(book.lcdSerial).toBe(isoToSerial('2026-09-29'));
  runner.lcd = '2026-10-01'; runner.facts = reconFactsFor('2026-09-01', '2026-10-01', over);
  runner.journal = []; runner.ledger = []; book.batchWrites = [];
  return { book, runner };
}

/** Книга с сентябрём и 01–02.10, LCD 02.10 (как seeded в unitka_reconcile.test.ts). */
async function seeded(): Promise<{ book: MemoryBook; runner: ReconRunner }> {
  const book = buildBook('2026-08-31');
  const runner = new ReconRunner('2026-09-30', reconFactsFor('2026-09-01', '2026-09-30'), '2026-09-01');
  await run(book, runner, 'write');
  runner.lcd = '2026-10-02'; runner.facts = reconFactsFor('2026-09-01', '2026-10-02');
  await run(book, runner, 'write');
  runner.journal = []; runner.ledger = []; book.batchWrites = [];
  return { book, runner };
}

/* ───────────────────────── A. закрытие конца месяца ───────────────────────── */

describe('A. закрытие конца месяца: кандидат LCD не перешагивает незакрытые дни прошлого месяца', () => {
  it('незакрытые дни: bookLcd < d < 1-е число месяца кандидата', () => {
    expect(unclosedPreviousMonthDays('2026-09-29', '2026-10-01')).toEqual(['2026-09-30']);
    expect(unclosedPreviousMonthDays('2026-09-30', '2026-10-01')).toEqual([]);              // обычный переход месяца
    expect(unclosedPreviousMonthDays('2026-10-01', '2026-10-05')).toEqual([]);              // внутри месяца — нечего закрывать
    expect(unclosedPreviousMonthDays('2026-09-28', '2026-10-03')).toEqual(['2026-09-29', '2026-09-30']);
    expect(unclosedPreviousMonthDays('2026-10-30', '2026-12-01')).toHaveLength(31);          // 31.10 + 30 дней ноября
  });

  it('D пусто → поведение прежнее: ни события, ни ключа month_end_close, даже в off', async () => {
    expect(planMonthEndClose({ bookLcd: '2026-09-30', lcd: '2026-10-02', mode: 'off', sections: [] })).toEqual({ days: [], cells: [], formatCells: [], parts: [] });
    const { book, runner } = await seeded();
    runner.lcd = '2026-10-03'; runner.facts = reconFactsFor('2026-09-01', '2026-10-03');
    const { lines } = await run(book, runner, 'observe');
    expect(events(lines, 'unitka_month_end_close')).toHaveLength(0);
    expect(qaOf(runner).month_end_close).toBeUndefined();
    expect(book.lcdSerial).toBe(isoToSerial('2026-10-03'));
  });

  it('D = {30.09}, observe, полное покрытие: строка 30.09 пишется в той же записи, перечитывание до и сводка после коммита — PASS', async () => {
    const { book, runner } = await at2909();
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 3 });          // поздняя поправка — observe её НЕ пишет
    const row30 = septRow(book, '2026-09-30');
    expect(book.get(row30, col(5, OFFSET.opens))).toBe('');
    const { lines } = await run(book, runner, 'observe');
    // строка 30.09 заполнена у всех 24 блоков сентября; 17.09 не тронут (остаток observe)
    expect(book.get(row30, col(5, OFFSET.opens))).toBe(40);
    for (let slot = 0; slot < 24; slot++) expect(book.get(row30, col(slot, OFFSET.views)), `слот ${slot}`).toBe(100 + slot);
    expect(book.get(septRow(book, '2026-09-17'), col(5, OFFSET.cancels))).toBe(0);
    expect(book.get(octRow(book, '2026-10-01'), col(5, OFFSET.opens))).toBe(11);
    expect(book.lcdSerial).toBe(isoToSerial('2026-10-01'));
    // одна запись данных (сентябрь 30.09 + октябрь 01.10) + коммит LCD
    expect(book.batchWrites).toHaveLength(2);
    const ev = events(lines, 'unitka_month_end_close')[0]!.fields as { days: string[]; cells: number; write: boolean };
    expect([ev.days, ev.write]).toEqual([['2026-09-30'], true]);
    expect(ev.cells).toBeGreaterThan(24 * 7);
    const qa = qaOf(runner) as { month_end_close: { days: string[]; cells: number }; checks: Array<{ name: string; pass: boolean }> };
    expect(qa.month_end_close).toMatchObject({ days: ['2026-09-30'], cells: ev.cells });
    expect(qa.checks.find((c) => c.name === 'MONTH_END_CLOSE_READBACK')).toMatchObject({ pass: true });
    const post = events(lines, 'unitka_qa').find((l) => l.fields.phase === 'POST_COMMIT')!.fields as { pass: boolean; checks: string[] };
    expect(post.pass).toBe(true);
    expect(post.checks).toEqual(expect.arrayContaining(['MONTH_END_CLOSE_READBACK:PASS', 'MONTH_END_CLOSE_SUMMARY:PASS']));
    expect(runner.journal.at(-1)).toMatchObject({ qaStatus: 'PASS', errorCode: null });
    expect(runner.ledger).toHaveLength(0);                                               // первое заполнение — не ремонт; observe журнал не ведёт
  });

  it('SHADOW (UNITKA_WRITE_ENABLED=0): план закрытия только в журнал, книга не трогается', async () => {
    const { book, runner } = await at2909();
    const { lines } = await run(book, runner, 'observe', { UNITKA_WRITE_ENABLED: '0' });
    expect(book.batchWrites).toHaveLength(0);
    expect(book.get(septRow(book, '2026-09-30'), col(5, OFFSET.opens))).toBe('');
    expect((events(lines, 'unitka_month_end_close')[0]!.fields as { write: boolean }).write).toBe(false);
    expect(qaOf(runner).month_end_close).toMatchObject({ days: ['2026-09-30'] });
  });

  it('блок сентября без полного покрытия слоя сверки → MONTH_END_UNCLOSED ДО любой записи, LCD стоит', async () => {
    const { book, runner } = await at2909();
    runner.facts = runner.facts.filter((f) => !(f.nmId === SEPT_NM && f.date === '2026-09-30'));
    await expect(run(book, runner, 'observe')).rejects.toMatchObject({ code: 'MONTH_END_UNCLOSED' });
    expect([book.batchWrites.length, book.lcdSerial]).toEqual([0, isoToSerial('2026-09-29')]);
    expect(runner.journal.at(-1)).toMatchObject({ errorCode: 'MONTH_END_UNCLOSED', qaStatus: 'FAIL' });
  });

  it('секция сентября не сверялась (сломан контракт) → MONTH_END_UNCLOSED с кодом отказа секции в сообщении', async () => {
    const { book, runner } = await at2909();
    book.section('2026-09').grid[1]![1] = 'не дата';
    const err = await run(book, runner, 'observe').catch((e: unknown) => e) as { code: string; message: string };
    expect(err.code).toBe('MONTH_END_UNCLOSED');
    expect(err.message).toContain('RECON_SECTION_INVALID');
    expect(book.batchWrites).toHaveLength(0);
  });

  it('off при незакрытых днях → MONTH_END_UNCLOSED (раньше LCD молча перешагивал)', async () => {
    const { book, runner } = await at2909();
    await expect(run(book, runner, 'off')).rejects.toMatchObject({ code: 'MONTH_END_UNCLOSED' });
    expect(book.batchWrites).toHaveLength(0);
  });

  it('write: ячейки конца месяца уже в сверке — без дублей по ячейке', async () => {
    const { book, runner } = await at2909();
    await run(book, runner, 'write');
    const keys = book.batchWrites[0]!.flatMap((w) => {
      const m = /!([A-Z]+)(\d+):[A-Z]+\d+$/.exec(w.range)!;
      return w.values.map((_, i) => `${m[1]}${Number(m[2]) + i}`);
    });
    expect(new Set(keys).size).toBe(keys.length);
    expect(runner.journal.at(-1)).toMatchObject({ qaStatus: 'PASS', cellsPlanned: keys.length });
    expect(book.get(septRow(book, '2026-09-30'), col(5, OFFSET.opens))).toBe(40);
  });

  it('потерянная запись (лист подтвердил, но не применил) → MONTH_END_CLOSE_READBACK провален, коммит не начинается', async () => {
    const { book, runner } = await at2909();
    book.dropNextWrite = true;
    await expect(run(book, runner, 'observe')).rejects.toMatchObject({ code: expect.any(String) });
    expect(book.lcdSerial).toBe(isoToSerial('2026-09-29'));
    const qa = qaOf(runner) as { checks: Array<{ name: string; pass: boolean }> };
    expect(qa.checks.find((c) => c.name === 'MONTH_END_CLOSE_READBACK')).toMatchObject({ pass: false });
  });

  it('чистая функция перечитывания: расхождение и ошибка формулы в строке дня — провал', async () => {
    const { book, runner } = await at2909();
    await run(book, runner, 'observe');
    const snap = book.section('2026-09');
    const plan = buildSectionRepairPlan({ snapshot: snap, facts: runner.facts, window: reconcileWindow('2026-10-01'), bookLcd: '2026-10-01', lcd: '2026-10-01' });
    const cells = plan.expected.filter((e) => e.date === '2026-09-30');
    expect(sectionCellsReadback('MONTH_END_CLOSE', [{ after: snap, plan, cells, summaryDays: ['2026-09-30'] }], { summary: true }).map((c) => c.pass)).toEqual([true, true]);
    book.set(septRow(book, '2026-09-30'), col(5, OFFSET.opens), 41);
    const bad = sectionCellsReadback('MONTH_END_CLOSE', [{ after: snap, plan, cells }]);
    expect(bad).toMatchObject([{ name: 'MONTH_END_CLOSE_READBACK', pass: false, count: 1 }]);
    book.set(septRow(book, '2026-09-30'), col(5, OFFSET.opens), 40);
    book.set(septRow(book, '2026-09-30'), col(5, OFFSET.profitAll), '#REF!');
    expect(sectionCellsReadback('MONTH_END_CLOSE', [{ after: snap, plan, cells }])[0]!.sample[0]).toContain('#REF!');
  });
});

/* ───────────────────────── B. FACT_NOT_ON_SHEET ───────────────────────── */

describe('B. FACT_NOT_ON_SHEET: пустые факт-ячейки прошлых секций', () => {
  const blank = (): string => '';
  const e = (key: string, want: number | null, date = '2026-09-30', row = 10, c = 3) => ({ row, col: c, want, kind: 'fact', nmId: 111, key, date, source: 'SRC' });
  const base = { lcd: '2026-10-01', monthStart: '2026-10-01', cellAt: blank };
  it('деньги (orders, cancels, adsIn, storage) — ERROR, фин. недействительна, ремонт доступен; счётчики и остаток — WARNING', () => {
    const out = factCellRules({ ...base, expected: ['orders', 'cancels', 'adsIn', 'storage', 'views', 'opens', 'carts', 'stock'].map((k, i) => e(k, 5, '2026-09-30', 10, 3 + i)) });
    expect(out.map((i) => [i.field, i.severity, i.financialInvalid, i.repairAvailable ?? false])).toEqual([
      ['orders', 'ERROR', true, true], ['cancels', 'ERROR', true, true], ['adsIn', 'ERROR', true, true], ['storage', 'ERROR', true, true],
      ['views', 'WARNING', false, false], ['opens', 'WARNING', false, false], ['carts', 'WARNING', false, false], ['stock', 'WARNING', false, false],
    ]);
    expect(out.every((i) => i.code === 'FACT_NOT_ON_SHEET' && i.nmId === 111 && i.day === '2026-09-30')).toBe(true);
  });
  it('цена исключена (её ведёт PRICE_NOT_ON_SHEET); пусто в источнике, заполненная ячейка, месяц LCD, день > LCD — не issue', () => {
    expect(factCellRules({ ...base, expected: [e('price', 1000)] })).toEqual([]);
    expect(factCellRules({ ...base, expected: [e('orders', null)] })).toEqual([]);
    expect(factCellRules({ ...base, cellAt: () => 0, expected: [e('orders', 5)] })).toEqual([]);   // 0 — заполнено
    expect(factCellRules({ ...base, expected: [e('orders', 5, '2026-10-01')] })).toEqual([]);      // месяц LCD — правила месяца LCD
    expect(factCellRules({ ...base, lcd: '2026-09-29', monthStart: '2026-09-01', expected: [e('orders', 5, '2026-09-30')] })).toEqual([]);
  });
  it('ячейку пишет этот прогон (pendingWrite) — не issue; денежный ноль — WARNING (прибыль та же)', () => {
    expect(factCellRules({ ...base, pendingWrite: (r, c) => r === 10 && c === 3, expected: [e('orders', 5)] })).toEqual([]);
    expect(factCellRules({ ...base, expected: [e('storage', 0)] })).toMatchObject([{ severity: 'WARNING', financialInvalid: false }]);
  });
  it('в прогоне: observe, пустая реклама 17.09 при источнике 5,5 → ERROR FACT_NOT_ON_SHEET и алерт INTEGRITY_DATA_ERROR', async () => {
    const { book, runner } = await seeded();
    book.set(septRow(book, '2026-09-17'), col(5, OFFSET.adsIn), '');
    const { lines } = await run(book, runner, 'observe', { UNITKA_INTEGRITY_MODE: 'observe' });
    const integ = events(lines, 'unitka_integrity').at(-1)!.fields as { integrity_status: string; issue_codes: Record<string, number>; error_keys: string[] };
    expect(integ.integrity_status).toBe('DATA_ERROR');
    expect(integ.issue_codes.FACT_NOT_ON_SHEET).toBe(1);
    expect(integ.error_keys).toContain(`2026-09-17/${SEPT_NM}/FACT_NOT_ON_SHEET`);
    const alert = events(lines, 'unitka_integrity_data_error');
    expect(alert).toHaveLength(1);
    expect(alert[0]).toMatchObject({ level: 'error', fields: { code: 'INTEGRITY_DATA_ERROR', data_error: 1 } });
  });
});

/* ───────────────────────── C. SKU_WITHOUT_BLOCK по деньгам ───────────────────────── */

describe('C. SKU_WITHOUT_BLOCK: тяжесть по деньгам', () => {
  const row = (nmId: number, day = '2026-09-10'): IntegrityFactsRow => ({
    marketplace: 'WB', nmId, internalSku: null, productName: 'Товар', day, lastClosedDate: '2026-10-01', ordersUnitka: 0, cancelsUnitka: 0,
    ordersSource: 'FUNNEL_API', factualOrderPrice: null, ordersFunnel: 0, factOrderRows: null, factOrderQty: null, observedPriceDiagnostic: null,
    observedPriceAt: null, storageValue: 0, storageDateCovered: true, priceState: 'MISSING_NO_ACTIVITY', divergenceClass: 'EXACT', skuActive: true,
  });
  const blocks: Block[] = [{ index: 0, slot: 0, start: 13, nmId: 1 } as Block];
  it('денег нет → WARNING, не блокирует, no_money_activity; деньги есть или неизвестно → ERROR (fail-safe)', () => {
    expect(coverageRules([row(NEW_NM)], blocks, '2026-10-01', () => false)).toMatchObject([{ code: 'SKU_WITHOUT_BLOCK', severity: 'WARNING', blocking: false, diagnosticValue: 'no_money_activity; Товар' }]);
    expect(coverageRules([row(NEW_NM)], blocks, '2026-10-01', () => true)).toMatchObject([{ severity: 'ERROR', blocking: true }]);
    expect(coverageRules([row(NEW_NM)], blocks, '2026-10-01')).toMatchObject([{ severity: 'ERROR', blocking: true }]);
  });
  it('moneyActivityFrom: только открытия — нет денег; хранение/реклама/отмены/заказы — есть; нет строк — неизвестно (true); вне дней — не считается', () => {
    const f = (nmId: number, p: Partial<FactRow> = {}, date = '2026-09-10'): FactRow => ({ nmId, date, views: 1, opens: 61, carts: 0, orders: 0, cancels: 0, stock: 0, adsIn: 0, price: null, storage: 0, ordersSource: 'FUNNEL_API', cancelsSource: 'X', ...p });
    const act = moneyActivityFrom([f(1), f(2, { storage: 0.4 }), f(3, { adsIn: 2 }), f(4, { cancels: 1 }), f(5, { orders: 1, price: 900 }), f(6, { storage: 9 }, '2026-08-31')], '2026-09-01', '2026-09-30');
    expect([1, 2, 3, 4, 5, 6, 7].map(act)).toEqual([false, true, true, true, true, true, true]);
  });
  const integrityRow = (day: string): Record<string, unknown> => ({
    marketplace: 'WB', nm_id: NEW_NM, internal_sku: 's', product_name: 'p', day, last_closed_date: '2026-10-02', orders_unitka: 0, cancels_unitka: 0, orders_source: 'FUNNEL_API',
    orders_funnel: 0, fact_order_rows: null, fact_order_qty: null, observed_price_diagnostic: null, observed_price_at: null, storage_value: 0, storage_date_covered: true,
    divergence_class: 'EXACT', factual_order_price: null, price_source: null, price_state: 'MISSING_NO_ACTIVITY', funnel_orders_sum: 0, same_day_cancel_qty: 0, stock_date_covered: true, sku_active: true,
  });
  it('в прогоне: 909951444 в сентябре без денег → WARNING, integrity не DATA_ERROR, алерта нет; с хранением → DATA_ERROR и алерт', async () => {
    const zero = (f: FactRow): Partial<FactRow> | null => (f.nmId === NEW_NM && f.date < '2026-10-01' ? { orders: 0, cancels: 0, adsIn: 0, storage: 0, price: null, priceSource: null } : null);
    const { book, runner } = await seeded();
    runner.facts = runner.facts.map((f) => ({ ...f, ...(zero(f) ?? {}) }));
    runner.integrityRows = [integrityRow('2026-09-10')];
    const quiet = await run(book, runner, 'observe', { UNITKA_INTEGRITY_MODE: 'observe' });
    const s1 = events(quiet.lines, 'unitka_integrity').at(-1)!.fields as { integrity_status: string; issue_codes: Record<string, number> };
    expect(s1.issue_codes.SKU_WITHOUT_BLOCK).toBe(1);
    expect(s1.integrity_status).not.toBe('DATA_ERROR');
    expect(events(quiet.lines, 'unitka_integrity_data_error')).toHaveLength(0);
    runner.facts = patch(runner.facts, NEW_NM, '2026-09-12', { storage: 0.35 });
    const loud = await run(book, runner, 'observe', { UNITKA_INTEGRITY_MODE: 'observe' });
    expect((events(loud.lines, 'unitka_integrity').at(-1)!.fields as { integrity_status: string }).integrity_status).toBe('DATA_ERROR');
    expect(events(loud.lines, 'unitka_integrity_data_error')[0]!.fields).toMatchObject({ code: 'INTEGRITY_DATA_ERROR', error_keys: [`-/${NEW_NM}/SKU_WITHOUT_BLOCK`] });
  });
});

/* ───────────────────────── D2. controlled · restricted (Phase 1B, решение владельца 07.10.2026) ───────────────────────── */

describe('D2. controlled restricted: без владельца — только первые заполнения и рост S ≤ Q', () => {
  const cell = (key: string, before: number | string, want: number | null, i = 0, date = '2026-09-17'): PlannedCell => ({
    row: 900 + i, col: 30, want, kind: 'fact', nmId: 1, key, date, source: 'SRC', before, changeType: 'LATE_SOURCE_CORRECTION', reason: 'r',
  });
  const q = (n: number) => () => n;
  const ctx = { lcdMonthStart: '2026-10-01', contractCells: 6480 };
  it('по умолчанию охват restricted; full — только явным UNITKA_CONTROLLED_SCOPE=full; опечатка = restricted', () => {
    expect(cfg('controlled').unitkaControlledScope).toBe('restricted');
    expect(cfg('controlled', { UNITKA_CONTROLLED_SCOPE: ' FULL ' }).unitkaControlledScope).toBe('full');
    expect(cfg('controlled', { UNITKA_CONTROLLED_SCOPE: 'ful' }).unitkaControlledScope).toBe('restricted');
    expect(controlledWritePolicy([], ctx).counts.scope).toBe('restricted');
  });
  it('рост S в пределах Q — пишется; первое заполнение — пишется', () => {
    const r = controlledWritePolicy([cell('cancels', 1, 2, 0), cell('storage', '', 3.5, 1)], { ...ctx, ordersOf: q(12) });
    expect(r.apply.map((c) => c.row)).toEqual([901, 900]);
    expect(r.counts).toMatchObject({ applied: 2, refused: 0, restricted_refused: 0 });
  });
  it.each([
    ['цена', cell('price', 1090, 1089.68), 'RECON_RESTRICTED_REQUIRES_ACK'],
    ['откат цены', cell('price', 660, 650), 'RECON_RESTRICTED_REQUIRES_ACK'],
    ['хранение (поправка, не первое заполнение)', cell('storage', 3, 4), 'RECON_RESTRICTED_REQUIRES_ACK'],
    ['заказы', cell('orders', 3, 4), 'RECON_RESTRICTED_REQUIRES_ACK'],
    ['уменьшение S', cell('cancels', 2, 1), 'RECON_CANCELS_DECREASE_REQUIRES_ACK'],
    ['S выше Q', cell('cancels', 1, 13), 'RECON_CANCELS_EXCEED_ORDERS'],
    ['S дробный', cell('cancels', 1, 1.5), 'RECON_RESTRICTED_REQUIRES_ACK'],
  ])('не пишется без владельца: %s', (_n, c, code) => {
    const r = controlledWritePolicy([c], { ...ctx, ordersOf: q(12) });
    expect(r.apply).toEqual([]);
    expect(r.refused.map((x) => x.code)).toEqual([code]);
  });
  it('без Q источника рост S не пишется (граница S ≤ Q не проверяема)', () => {
    expect(restrictedCorrectionRefusal(cell('cancels', 1, 2), () => null)).toBe('RECON_CANCELS_EXCEED_ORDERS');
    expect(restrictedCorrectionRefusal(cell('cancels', 1, 2))).toBe('RECON_CANCELS_EXCEED_ORDERS');
  });
  it('отзыв источника — никогда; вне прошлых месяцев — вне рамок; потолок действует на рост S', () => {
    const r = controlledWritePolicy([cell('cancels', 2, null, 0), cell('cancels', 1, 2, 1, '2026-10-02')], { ...ctx, ordersOf: q(12) });
    expect(r.refused.map((x) => x.code)).toEqual(['RECON_WITHDRAWAL_REQUIRES_ACK', 'RECON_OUT_OF_SCOPE']);
    const many = Array.from({ length: 51 }, (_, i) => cell('cancels', 1, 2, 10 + i));
    expect(controlledWritePolicy(many, { ...ctx, ordersOf: q(12) }).counts).toMatchObject({ applied: 0, cap_exceeded: true });
  });
  it('в прогоне: рост S и первое заполнение записаны с журналом; правка цены — отказ с кодом, ячейка не тронута', async () => {
    const { book, runner } = await seeded();
    const r17 = septRow(book, '2026-09-17');
    book.set(r17, col(5, OFFSET.storage), '');                                                    // первое заполнение
    const before = book.get(r17, col(5, OFFSET.price));
    runner.facts = patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 2, orders: 5, price: Number(before) + 1 });
    const { lines } = await run(book, runner, 'controlled');
    expect(book.get(r17, col(5, OFFSET.storage))).toBe(1.25);
    expect(book.get(r17, col(5, OFFSET.cancels))).toBe(2);
    expect(book.get(r17, col(5, OFFSET.price))).toBe(before);                                     // цена — только владельцем
    expect(runner.ledger.map((x) => [x.field, x.status]).sort()).toEqual([['cancels', 'REPAIRED'], ['storage', 'REPAIRED']]);
    expect(events(lines, 'unitka_controlled_refused').map((e) => (e.fields as { code: string }).code)).toContain('RECON_RESTRICTED_REQUIRES_ACK');
    expect(events(lines, 'unitka_controlled_policy')[0]!.fields).toMatchObject({ scope: 'restricted' });
  });
});

/* ───────────────────────── D. режим controlled ───────────────────────── */

describe('D. controlled: политика записи прошлых месяцев (реализован, не включён)', () => {
  const cell = (before: number | string, want: number | null, date = '2026-09-17', i = 0): PlannedCell => ({
    row: 800 + i, col: 30, want, kind: 'fact', nmId: 1, key: 'cancels', date, source: 'SRC', before,
    changeType: date > '2026-09-29' ? 'FACT_CHANGE' : 'LATE_SOURCE_CORRECTION', reason: 'r',
  });
  // Охват full — прежняя политика (разовый прогон владельца). restricted (по умолчанию) — отдельный блок D2 ниже.
  const ctx = { lcdMonthStart: '2026-10-01', contractCells: 6480, scope: 'full' as const };
  it('первое заполнение — пишется; отзыв источника — НИКОГДА (RECON_WITHDRAWAL_REQUIRES_ACK); поправка под потолком — пишется', () => {
    const r = controlledWritePolicy([cell('', 3, '2026-09-17', 0), cell(2, null, '2026-09-17', 1), cell(2, 3, '2026-09-17', 2)], ctx);
    expect(r.apply.map((c) => c.row)).toEqual([800, 802]);
    expect(r.refused.map((x) => [x.cell.row, x.code])).toEqual([[801, 'RECON_WITHDRAWAL_REQUIRES_ACK']]);
    expect(r.counts).toMatchObject({ first_fills: 1, corrections: 1, withdrawals: 1, applied: 2, refused: 1, correction_cap: 50, cap_exceeded: false });
  });
  it('потолок 50: ровно 50 поправок — пишутся; 51 — не пишется НИ ОДНА, первые заполнения всё равно пишутся', () => {
    const corr = (n: number): PlannedCell[] => Array.from({ length: n }, (_, i) => cell(1, 2, '2026-09-17', 10 + i));
    expect(controlledWritePolicy(corr(CONTROLLED_MAX_CORRECTIONS), ctx).counts).toMatchObject({ applied: 50, cap_exceeded: false });
    const r = controlledWritePolicy([cell('', 5, '2026-09-30', 0), ...corr(51)], ctx);
    expect(r.apply.map((c) => c.row)).toEqual([800]);
    expect(r.refused).toHaveLength(51);
    expect(new Set(r.refused.map((x) => x.code))).toEqual(new Set(['RECON_CORRECTION_CAP_EXCEEDED']));
  });
  it('потолок 2 % контракта: контракт 1000 → не больше 20 поправок', () => {
    const corr = (n: number): PlannedCell[] => Array.from({ length: n }, (_, i) => cell(1, 2, '2026-09-17', i));
    expect(controlledWritePolicy(corr(20), { ...ctx, contractCells: 1000 }).counts).toMatchObject({ correction_cap: 20, applied: 20, cap_exceeded: false });
    expect(controlledWritePolicy(corr(21), { ...ctx, contractCells: 1000 }).counts).toMatchObject({ applied: 0, refused: 21, cap_exceeded: true });
  });
  it('смесь + вне рамок: ячейка месяца LCD — RECON_OUT_OF_SCOPE', () => {
    const r = controlledWritePolicy([cell('', 1, '2026-10-01', 0), cell('', 1, '2026-09-30', 1), cell(4, null, '2026-09-20', 2), cell(4, 5, '2026-09-20', 3)], ctx);
    expect(r.apply.map((c) => c.row)).toEqual([801, 803]);
    expect(r.refused.map((x) => x.code)).toEqual(['RECON_OUT_OF_SCOPE', 'RECON_WITHDRAWAL_REQUIRES_ACK']);
  });
  it('журнал ремонта: includeFirstFills добавляет FACT_CHANGE-первые заполнения (LATE_FIRST_FILL); без флага — прежний состав', () => {
    const c = cell('', 4, '2026-09-30', 0);
    const base = { runId: 'r', environment: 'prod', engineVersion: 'v', gitSha: 'g', detectedAt: 't', repairedAt: null, status: 'PLANNED_NOT_WRITTEN' as const, factOf: () => undefined };
    expect(repairRecords([c], base)).toEqual([]);
    expect(repairRecords([c], { ...base, includeFirstFills: true })).toMatchObject([{ businessDate: '2026-09-30', reason: 'LATE_FIRST_FILL', newValue: '4' }]);
  });

  it('конфиг: controlled принимается, опечатка = off + предупреждение; по умолчанию off', () => {
    expect(cfg('controlled').unitkaReconcileMode).toBe('controlled');
    expect(cfg(' CONTROLLED ').unitkaReconcileMode).toBe('controlled');
    expect(cfg('controled')).toMatchObject({ unitkaReconcileMode: 'off', unitkaReconcileModeInvalid: 'controled' });
    expect(cfg('').unitkaReconcileMode).toBe('off');
  });

  it('в прогоне: первое заполнение и поправка записаны с журналом REPAIRED, отзыв не записан и даёт ошибку с кодом', async () => {
    const { book, runner } = await seeded();
    const r17 = septRow(book, '2026-09-17'), r18 = septRow(book, '2026-09-18');
    book.set(r17, col(5, OFFSET.storage), '');                                                    // первое заполнение
    runner.facts = patch(patch(runner.facts, SEPT_NM, '2026-09-17', { cancels: 2 }), SEPT_NM, '2026-09-18', { views: null });
    const { lines } = await run(book, runner, 'controlled', { UNITKA_CONTROLLED_SCOPE: 'full' });
    expect(book.get(r17, col(5, OFFSET.storage))).toBe(1.25);
    expect(book.get(r17, col(5, OFFSET.cancels))).toBe(2);
    expect(book.get(r18, col(5, OFFSET.views))).toBe(105);                                       // отзыв не применён
    expect(runner.ledger.map((x) => [x.field, x.reason, x.status])).toEqual(expect.arrayContaining([['storage', 'LATE_FIRST_FILL', 'REPAIRED'], ['cancels', 'SOURCE_REVISED', 'REPAIRED']]));
    expect(runner.ledger).toHaveLength(2);
    expect(events(lines, 'unitka_controlled_refused')).toMatchObject([{ level: 'error', fields: { code: 'RECON_WITHDRAWAL_REQUIRES_ACK', count: 1 } }]);
    const qa = qaOf(runner) as { reconcile: { repairs_residual: number; controlled: { applied: number } }; checks: Array<{ name: string; pass: boolean }> };
    expect(qa.reconcile).toMatchObject({ repairs_residual: 1, controlled: { applied: 2 } });
    expect(qa.checks.filter((c) => c.name.startsWith('RECON_CONTROLLED_'))).toMatchObject([{ name: 'RECON_CONTROLLED_READBACK', pass: true }, { name: 'RECON_CONTROLLED_SUMMARY', pass: true }]);
    expect(runner.journal.at(-1)).toMatchObject({ qaStatus: 'PASS', errorCode: null });
  });

  it('controlled + конец месяца: строка 30.09 пишется и попадает в журнал как LATE_FIRST_FILL', async () => {
    const { book, runner } = await at2909();
    await run(book, runner, 'controlled');
    expect(book.get(septRow(book, '2026-09-30'), col(5, OFFSET.opens))).toBe(40);
    expect(book.lcdSerial).toBe(isoToSerial('2026-10-01'));
    expect(runner.ledger.length).toBeGreaterThan(0);
    expect(runner.ledger.every((x) => x.businessDate === '2026-09-30' && x.reason !== 'SOURCE_REVISED' && x.status === 'REPAIRED')).toBe(true);
  });
});

/* ───────────────────────── E. алерт остатка сверки ───────────────────────── */

describe('E. RECON_RESIDUAL_PERSISTENT: остаток сверки второй прогон подряд', () => {
  const withResidual = async (): Promise<{ book: MemoryBook; runner: ReconRunner }> => {
    const s = await seeded();
    s.runner.facts = patch(s.runner.facts, SEPT_NM, '2026-09-17', { cancels: 1 });
    return s;
  };
  it('observe: остаток > 0 и у предыдущего прогона > 0 → ошибка с кодом; repairs_residual в qa_json', async () => {
    const { book, runner } = await withResidual();
    runner.previousResidual = 8;
    const { lines } = await run(book, runner, 'observe');
    expect(events(lines, 'unitka_recon_residual_persistent')).toMatchObject([{ level: 'error', fields: { code: 'RECON_RESIDUAL_PERSISTENT', residual: 1, previous_residual: 8 } }]);
    expect((qaOf(runner) as { reconcile: { repairs_residual: number } }).reconcile.repairs_residual).toBe(1);
    expect(runner.queries.some((q) => q.includes('UNITKA_ENGINE_RUNS') && q.includes('repairs_residual') && q.includes("environment = 'prod'"))).toBe(true);
  });
  it('предыдущий прогон без остатка (0 или нет строки) — молчит', async () => {
    const { book, runner } = await withResidual();
    runner.previousResidual = 0;
    expect(events((await run(book, runner, 'observe')).lines, 'unitka_recon_residual_persistent')).toHaveLength(0);
    runner.previousResidual = null;
    expect(events((await run(book, runner, 'observe')).lines, 'unitka_recon_residual_persistent')).toHaveLength(0);
  });
  it('сбой чтения журнала прогонов — только предупреждение, прогон проходит', async () => {
    const { book, runner } = await withResidual();
    runner.previousResidual = 'fail';
    const { lines } = await run(book, runner, 'observe');
    expect(events(lines, 'unitka_recon_residual_check_failed')).toMatchObject([{ level: 'warn' }]);
    expect(runner.journal.at(-1)).toMatchObject({ qaStatus: 'PASS', errorCode: null });
  });
  it('поздняя поправка месяца LCD, которую observe записывает сам, остатком НЕ считается (ревью #259)', async () => {
    const book = buildBook('2026-08-31');
    const runner = new ReconRunner('2026-09-30', reconFactsFor('2026-09-01', '2026-09-30'), '2026-09-01');
    await run(book, runner, 'write');
    runner.lcd = '2026-10-02'; runner.facts = reconFactsFor('2026-09-01', '2026-10-02');
    await run(book, runner, 'write');
    runner.lcd = '2026-10-03'; runner.facts = patch(reconFactsFor('2026-09-01', '2026-10-03'), SEPT_NMS[5]!, '2026-10-01', { cancels: 3 });
    runner.previousResidual = 5;
    const { lines } = await run(book, runner, 'observe');
    const g = book.section('2026-10').geometry;
    expect(book.get(dayRowOf(g, 0), slotStart(5) + OFFSET.cancels)).toBe(3);                    // записано этим прогоном
    expect((qaOf(runner) as { reconcile: { repairs_residual: number } }).reconcile.repairs_residual).toBe(0);
    expect(events(lines, 'unitka_recon_residual_persistent')).toHaveLength(0);
  });
  it('write и нулевой остаток — журнал прогонов не читается', async () => {
    const { book, runner } = await withResidual();
    runner.previousResidual = 8;
    await run(book, runner, 'write');
    await run(book, runner, 'observe');                                                           // поправка уже записана → остаток 0
    expect(runner.queries.some((q) => q.includes('repairs_residual'))).toBe(false);
  });
});

/* ───────────────────────── контрольные значения покрытия ───────────────────────── */

describe('контракт дня: 9 факт-ключей на блок', () => {
  it('FACT_KEYS — ровно 9 автоматических ключей (перечитывание конца месяца считает их на каждый блок)', () => {
    expect(FACT_KEYS).toHaveLength(9);
  });
});
