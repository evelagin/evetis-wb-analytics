/**
 * Phase 1A (07.10.2026) — доказанные отказы вне Orders API.
 * Заказ есть в воронке (Q), Orders API его не отдал, в финотчёте у srid два плеча логистики без продажи и возврата →
 * отказ входит в S. Проверяются: SQL-контракт слоя (refusals_v1.sql и обе вью фактов), форма строки (bq.ts),
 * вердикты Guard (правило 9A) и заметки S (только свои, чужие не трогаются).
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { UnitkaBq, CANCELS_SOURCE_WITH_REFUSALS, type FactRow } from '../src/loaders/unitka/bq.js';
import { divergenceRules, REFUSAL_MATURITY_DAYS, type IntegrityFactsRow } from '../src/loaders/unitka/integrity.js';
import { REFUSAL_NOTE_MARKER, refusalNote, planRefusalNotes, verifyRefusalNotes, noteRequests, noteKey } from '../src/loaders/unitka/refusal_notes.js';
import type { ExpectedCell } from '../src/loaders/unitka/plan.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p: string): string => readFileSync(join(root, p), 'utf8');
const code = (s: string): string => s.split('\n').map((l) => { const i = l.indexOf('--'); return i >= 0 ? l.slice(0, i) : l; }).join('\n');
const flat = (s: string): string => code(s).replace(/\s+/g, ' ');
function viewBodies(sql: string): Map<string, string> {
  const out = new Map<string, string>();
  const re = /CREATE OR REPLACE VIEW `[^`]+\.(\w+)` AS\n([\s\S]*?)(?=\nCREATE |$)/g;
  for (const m of code(sql).matchAll(re)) out.set(m[1]!, m[2]!);
  return out;
}

describe('SQL: слой доказательств отказов (refusals_v1.sql)', () => {
  const sql = read('sql/unitka/refusals_v1.sql');
  const v = viewBodies(sql);
  it('ровно две вью, только SELECT, только wb_raw / wb_mart (изоляция маркетплейсов)', () => {
    expect([...v.keys()].sort()).toEqual(['V_UNITKA_REFUSAL_DAILY', 'V_UNITKA_REFUSAL_EVIDENCE']);
    expect(code(sql)).not.toMatch(/\b(CREATE\s+(OR\s+REPLACE\s+)?(TABLE|PROCEDURE|FUNCTION)|INSERT\s+INTO|UPDATE\s+`|DELETE\s+FROM|MERGE\s+|TRUNCATE|DROP\s+)/i);
    for (const body of v.values()) {
      expect(body).not.toMatch(/evetis_ref|ozon_/i);
      for (const m of body.matchAll(/`project-fa311fc0-4d87-4781-986\.(\w+)\./g)) expect(['wb_raw', 'wb_mart']).toContain(m[1]);
    }
  });
  it('дата отказа — сутки МСК из UTC order_dt финотчёта (не UTC-дата)', () => {
    expect(flat(v.get('V_UNITKA_REFUSAL_EVIDENCE')!)).toContain("DATE(MIN(SAFE.TIMESTAMP(order_dt)), 'Europe/Moscow') AS order_date_msk");
  });
  it('класс отказа: нет в Orders API, нет продажи/возврата, ≥ 2 ненулевых плеча, оба плеча — окончательный слой', () => {
    const e = flat(v.get('V_UNITKA_REFUSAL_EVIDENCE')!);
    const order = ["THEN 'IN_ORDERS_API'", "THEN 'SOLD_NOT_IN_ORDERS_API'", "THEN 'PROVEN_REFUSAL'", "THEN 'REFUSAL_PENDING_FINAL'"].map((s) => e.indexOf(s));
    expect(order.every((i) => i > 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);                 // приоритет: Orders API → продажа → отказ
    expect(e).toContain("WHEN s.sale_rows > 0 OR s.return_rows > 0 THEN 'SOLD_NOT_IN_ORDERS_API'");
    expect(e).toContain("WHEN s.logistics_legs >= 2 AND s.legs_final THEN 'PROVEN_REFUSAL'");
    expect(e).toContain("COUNTIF(op IN ('Логистика', 'Доставка') AND lg <> 0) AS logistics_legs");
    expect(e).toContain('MAX(NULLIF(nm, 0)) AS nm_id');                        // строка финотчёта с nm = 0 не теряет SKU
  });
  it('сутки × SKU: в S — не больше избытка воронки; срок зрелости = константе Guard; начало — первый день воронки API', () => {
    const d = flat(v.get('V_UNITKA_REFUSAL_DAILY')!);
    expect(d).toContain(`SELECT DATE '2026-09-04' AS funnel_api_from, ${REFUSAL_MATURITY_DAYS} AS maturity_days`);
    expect(REFUSAL_MATURITY_DAYS).toBe(46);
    expect(d).toContain('LEAST(proven, excess) AS counted');
    expect(d).toContain('GREATEST(fu.funnel_orders - (IFNULL(api.gross, 0) - IFNULL(api.same_day, 0)), 0) AS excess');
    expect(d).toContain("THEN 'MATURE_BUT_UNPROVEN'");
    expect(d).toContain("ELSE 'STILL_OPEN'");
  });
});

describe('SQL: обе вью фактов прибавляют к S только доказанные отказы и только на днях воронки', () => {
  const FORMULA = 'COALESCE(bf.canc, o.canc, 0) + IF(f.forders IS NOT NULL AND bf.canc IS NULL, IFNULL(rf.refusal_counted_qty, 0), 0) AS cancels';
  it.each([['sql/unitka/engine_v1_views.sql', 'V_UNITKA_DAILY_FACT'], ['sql/unitka/reconcile_v1.sql', 'V_UNITKA_RECON_FACT']])('%s %s', (file, name) => {
    const body = flat(viewBodies(read(file)).get(name)!);
    expect(body).toContain(FORMULA);
    expect(body).toContain('wb_mart.V_UNITKA_REFUSAL_DAILY');
    expect(body).toContain(`WHEN f.forders IS NOT NULL AND IFNULL(rf.refusal_counted_qty, 0) > 0 THEN '${CANCELS_SOURCE_WITH_REFUSALS}'`);
    expect(body.match(/rf\.refusal_counted_qty/g)!.length).toBe(3);   // S, источник, колонка провенанса — больше нигде
  });
  it('вью целостности отдают доказательство Guard-у', () => {
    for (const [file, name] of [['sql/unitka/integrity_v1.sql', 'V_UNITKA_INTEGRITY'], ['sql/unitka/reconcile_v1.sql', 'V_UNITKA_RECON_INTEGRITY']] as const) {
      const body = flat(viewBodies(read(file)).get(name)!);
      for (const c of ['refusal_counted_qty', 'refusal_evidence_status', 'funnel_excess_qty', 'sold_not_in_api_qty', 'unexplained_qty', 'refusal_finance_age_days', 'refusal_proven_srids']) expect(body, `${name}.${c}`).toContain(c);
    }
  });
});

/* ── форма строки (bq.ts): fail-closed до любой записи ── */
const runnerOf = (rows: Record<string, unknown>[]) => ({ projectId: 'p', query: async () => rows } as never);
const row = (o: Record<string, unknown>): Record<string, unknown> => ({
  nm_id: 1, date_msk: { value: '2026-09-08' }, views: 1, opens: 1, carts: 1, orders: 1, cancels: 1, stock: 1, ads_in: 0, price: 500, storage: 1,
  orders_source: 'FUNNEL_API', cancels_source: CANCELS_SOURCE_WITH_REFUSALS, refusal_counted_qty: 1, refusal_evidence_status: 'EXPLAINED',
  refusal_proven_srids: 'ebQ.r8fe', refusal_proven_logistics_rub: 73.6, ...o,
});
describe('bq.ts: контракт строки факта с отказами', () => {
  const facts = (o: Record<string, unknown>) => new UnitkaBq(runnerOf([row(o)]), 'wb_mart', 'wb_ops', 'RUNS').facts();
  it('доказанный отказ читается вместе с провенансом', async () => {
    const [f] = await facts({});
    expect(f).toMatchObject({ cancels: 1, refusalCountedQty: 1, refusalProvenSrids: 'ebQ.r8fe', refusalProvenLogisticsRub: 73.6 });
  });
  it.each([
    ['отказ без своего источника', { cancels_source: 'PROXY_FACT_ORDERS' }],
    ['источник отказа без отказа', { refusal_counted_qty: 0 }],
    ['отказ не на дне воронки', { orders_source: 'ORDERS_API' }],
    ['S меньше отказов', { cancels: 0 }],
    ['S больше Q', { orders: 1, cancels: 2, refusal_counted_qty: 2 }],
    ['неизвестный источник', { cancels_source: 'X', refusal_counted_qty: 0 }],
    ['неизвестный статус', { refusal_evidence_status: 'GUESS' }],
  ])('отвергается: %s', async (_n, o) => {
    await expect(facts(o)).rejects.toMatchObject({ code: 'BQ_SHAPE' });
  });
});

/* ── Guard, правило 9A ── */
const ir = (o: Partial<IntegrityFactsRow>): IntegrityFactsRow => ({
  marketplace: 'WB', nmId: 305101361, internalSku: null, productName: null, day: '2026-09-08', lastClosedDate: '2026-10-06',
  ordersUnitka: 1, cancelsUnitka: 1, ordersSource: 'FUNNEL_API', factualOrderPrice: 500, ordersFunnel: 1, factOrderRows: 0, factOrderQty: null,
  observedPriceDiagnostic: null, observedPriceAt: null, storageValue: 1, storageDateCovered: true, priceState: 'PRESENT', divergenceClass: 'ONLY_FUNNEL',
  priceSource: 'FUNNEL_FALLBACK', funnelOrdersSum: 500, sameDayCancelQty: 0, stockDateCovered: true, skuActive: true,
  refusalCountedQty: 1, refusalEvidenceStatus: 'EXPLAINED', funnelExcessQty: 1, soldNotInApiQty: 0, unexplainedQty: 0, refusalFinanceAgeDays: 26,
  refusalProvenSrids: 'ebQ.r8fe', ...o,
});
const verdictOf = (o: Partial<IntegrityFactsRow>) => {
  const [i] = divergenceRules([ir(o)], '2026-10-06', new Date('2026-10-07T08:00:00Z'));
  return { severity: i!.severity, invalid: i!.financialInvalid, value: i!.sourceValue, message: i!.message };
};
describe('Guard 9A: избыток воронки классифицируется финотчётом, а не суммой воронки', () => {
  it('доказанный отказ → INFO, строка достоверна (отказ уже в S)', () => {
    expect(verdictOf({})).toMatchObject({ severity: 'INFO', invalid: false });
    expect(verdictOf({}).value).toContain('verdict=PROVEN_REFUSAL_APPLIED');
  });
  it('продажа вне Orders API → INFO SOLD_NOT_IN_ORDERS_API', () => {
    expect(verdictOf({ refusalCountedQty: 0, soldNotInApiQty: 1, cancelsUnitka: 0 }).value).toContain('verdict=SOLD_NOT_IN_ORDERS_API');
  });
  it('нет финансового следа, моложе срока зрелости → EXPECTED_DELAY, результат предварительный', () => {
    expect(verdictOf({ refusalEvidenceStatus: 'STILL_OPEN', refusalCountedQty: 0, unexplainedQty: 1, refusalFinanceAgeDays: 10 })).toMatchObject({ severity: 'EXPECTED_DELAY', invalid: true });
  });
  it('нет следа старше срока зрелости → WARNING, строка недостоверна, но НЕ отмена и НЕ ошибка', () => {
    const v = verdictOf({ refusalEvidenceStatus: 'MATURE_BUT_UNPROVEN', refusalCountedQty: 0, unexplainedQty: 1, refusalFinanceAgeDays: 50 });
    expect(v).toMatchObject({ severity: 'WARNING', invalid: true });
    expect(v.value).toContain('verdict=MATURE_BUT_UNPROVEN');
  });
  it('старый вердикт «сумма воронки подтверждает деньги» для избытка с доказательством больше не выносится', () => {
    for (const st of ['EXPLAINED', 'STILL_OPEN', 'MATURE_BUT_UNPROVEN'] as const) {
      expect(verdictOf({ refusalEvidenceStatus: st, divergenceClass: 'FUNNEL_GT_FACT', factOrderQty: 3, ordersFunnel: 4, priceSource: 'ORDERS_API' }).value).not.toContain('AMOUNT_CONFIRMED');
    }
  });
  it('избыток при EXACT (отмены дня заказа в Orders API) тоже классифицируется, а не пропускается', () => {
    const v = verdictOf({ divergenceClass: 'EXACT', ordersFunnel: 3, factOrderQty: 3, sameDayCancelQty: 1, funnelExcessQty: 1 });
    expect(v.value).toContain('verdict=PROVEN_REFUSAL_APPLIED');
  });
  it('без доказательства старше 14 дней и с неподтверждённой суммой воронки — прежний ERROR правила 9', () => {
    const v = verdictOf({ refusalEvidenceStatus: 'STILL_OPEN', refusalCountedQty: 0, unexplainedQty: 1, divergenceClass: 'FUNNEL_GT_FACT',
      ordersFunnel: 4, factOrderQty: 3, ordersUnitka: 4, funnelOrdersSum: 9999, priceSource: 'ORDERS_API', day: '2026-09-17' });
    expect(v).toMatchObject({ severity: 'ERROR', invalid: true });
    expect(v.value).toContain('verdict=AMOUNT_NOT_CONFIRMED');
  });
  it('без доказательства, но сумма воронки подтверждена — STILL_OPEN (не «на результат не влияет»)', () => {
    const v = verdictOf({ refusalEvidenceStatus: 'STILL_OPEN', refusalCountedQty: 0, unexplainedQty: 1, divergenceClass: 'FUNNEL_GT_FACT',
      ordersFunnel: 4, factOrderQty: 3, ordersUnitka: 4, funnelOrdersSum: 2000, priceSource: 'ORDERS_API', day: '2026-09-17' });
    expect(v).toMatchObject({ severity: 'EXPECTED_DELAY', invalid: true });
  });
  it('без доказательства (старая вью) поведение прежнее', () => {
    const v = verdictOf({ refusalEvidenceStatus: null, divergenceClass: 'FUNNEL_GT_FACT', factOrderQty: 3, ordersFunnel: 4, priceSource: 'ORDERS_API', ordersUnitka: 4, funnelOrdersSum: 2000 });
    expect(v.value).toContain('verdict=AMOUNT_CONFIRMED');
  });
});

/* ── заметки S ── */
const fact = (o: Partial<FactRow>): FactRow => ({
  nmId: 1, date: '2026-09-08', views: 1, opens: 1, carts: 1, orders: 2, cancels: 1, stock: 1, adsIn: 0, price: 500, storage: 1,
  ordersSource: 'FUNNEL_API', cancelsSource: CANCELS_SOURCE_WITH_REFUSALS, refusalCountedQty: 1, refusalProvenSrids: 'a,b', refusalProvenLogisticsRub: 73.6, ...o,
});
const sCell = (row: number, date = '2026-09-08'): ExpectedCell => ({ row, col: 19, want: 1, kind: 'fact', nmId: 1, key: 'cancels', date });
describe('заметки S: только свои, чужие не трогаются, идемпотентно', () => {
  it('текст: метка, число, srid, логистика финотчёта, источник', () => {
    const t = refusalNote(fact({}));
    expect(t.startsWith(`${REFUSAL_NOTE_MARKER}: 1 шт. из 1 в S`)).toBe(true);
    expect(t).toContain('srid: a, b');
    expect(t).toContain('73,60 ₽');
    expect(t).toContain('V_UNITKA_REFUSAL_DAILY');
    expect(refusalNote(fact({ refusalCountedQty: 0 }))).toBe('');
  });
  it('ставит заметку на ячейку с отказом, снимает только свою, чужую не стирает и не перезаписывает', () => {
    const facts = new Map([['2026-09-08', fact({})], ['2026-09-09', fact({ date: '2026-09-09', refusalCountedQty: 0, cancelsSource: 'PROXY_FACT_ORDERS' })],
      ['2026-09-10', fact({ date: '2026-09-10', refusalCountedQty: 0, cancelsSource: 'PROXY_FACT_ORDERS' })], ['2026-09-11', fact({ date: '2026-09-11' })]]);
    const current = new Map([[noteKey(11, 19), `${REFUSAL_NOTE_MARKER}: устарело`], [noteKey(12, 19), 'заметка владельца'], [noteKey(13, 19), 'заметка владельца']]);
    const cells = [sCell(10, '2026-09-08'), sCell(11, '2026-09-09'), sCell(12, '2026-09-10'), sCell(13, '2026-09-11')];
    const { notes, conflicts } = planRefusalNotes(cells, (_n, d) => facts.get(d), current);
    expect(notes.map((n) => [n.row, n.note === '' ? 'CLEAR' : 'SET'])).toEqual([[10, 'SET'], [11, 'CLEAR']]);
    expect(conflicts.map((c) => c.row)).toEqual([13]);                       // отказ есть, но заметка владельца — не трогаем
    // повтор при уже записанном — ничего
    const after = new Map(current); for (const n of notes) after.set(noteKey(n.row, n.col), n.note);
    expect(planRefusalNotes(cells, (_n, d) => facts.get(d), new Map([...after].filter(([, v]) => v !== ''))).notes).toEqual([]);
    expect(verifyRefusalNotes(notes, new Map([...after].filter(([, v]) => v !== ''))).pass).toBe(true);
    expect(verifyRefusalNotes(notes, new Map()).pass).toBe(false);
  });
  it('пересекающиеся цели (месяц LCD + записанные S прошлых месяцев) дают одну заметку на ячейку', () => {
    const f = fact({});
    const { notes } = planRefusalNotes([sCell(10), sCell(10), sCell(10)], () => f, new Map());
    expect(notes).toHaveLength(1);
  });
  it('запрос: repeatCell только поля note, одна ячейка', () => {
    const [r] = noteRequests(7, [{ row: 10, col: 19, nmId: 1, date: '2026-09-08', note: 'x', before: '' }]);
    expect(r).toEqual({ repeatCell: { range: { sheetId: 7, startRowIndex: 9, endRowIndex: 10, startColumnIndex: 18, endColumnIndex: 19 }, cell: { note: 'x' }, fields: 'note' } });
  });
});
