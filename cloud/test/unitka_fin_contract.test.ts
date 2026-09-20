/**
 * UNITKA FINANCIAL INTEGRITY V1 — контракт финансовой полноты SKU-дня (правила Guard, чистые функции).
 * Решения владельца: заказ без цены — DATA_ERROR и фин. недействительность; COGS листа ≠ канону на дату при
 * активности — ERROR (не WARNING); расхождение счётчиков — LATE_DATA до срока зрелости Orders API, цена при этом
 * делением суммы воронки НЕ пересчитывается; СПП — MANUAL_REQUIRED; законное отсутствие источника — NOT_AVAILABLE.
 * Состояния не схлопываются в один класс.
 */
import { describe, it, expect } from 'vitest';
import {
  evaluateIntegrity, summarize, classifyCogsSnapshot, priceRules, priceCellRules, cogsRules, divergenceRules, stockRules, sppRules, sectionDays,
  amountConfirmedByFunnel, contractState, aggregateStatus, ORDERS_API_MATURITY_DAYS, FUNNEL_MATURITY_DAYS,
  type IntegrityFactsRow, type CogsCanonicalRow, type CogsSnapshot, type IntegrityInputs, type IntegrityIssue,
} from '../src/loaders/unitka/integrity.js';
import { financialValidity } from '../src/loaders/unitka/reconcile.js';
import { OFFSET, addDaysIso, colA1, type Block, type CellValue } from '../src/loaders/unitka/model.js';
import { slotStart } from '../src/loaders/unitka/calendar.js';

const NOW = new Date('2026-10-03T07:00:00Z');            // 03.10, 10:00 МСК
const LCD = '2026-10-02';
const NM = { A: 930334396, B: 252442517, NEW: 909951444 };
const blocks: Block[] = [NM.A, NM.B].map((nm, i) => ({ index: i, slot: i, start: slotStart(i), nmId: nm, title: `${nm}` }));

function row(nmId: number, day: string, over: Partial<IntegrityFactsRow> = {}): IntegrityFactsRow {
  return {
    marketplace: 'WB', nmId, internalSku: `SKU-${nmId}`, productName: `Товар ${nmId}`, day, lastClosedDate: LCD,
    ordersUnitka: 0, cancelsUnitka: 0, ordersSource: 'FUNNEL_API', factualOrderPrice: null, ordersFunnel: 0, factOrderRows: null, factOrderQty: null,
    observedPriceDiagnostic: null, observedPriceAt: null, storageValue: 0, storageDateCovered: true, priceState: 'MISSING_NO_ACTIVITY', divergenceClass: 'EXACT',
    priceSource: null, funnelOrdersSum: 0, stockDateCovered: true, skuActive: true, ...over,
  };
}
const withOrders = (nm: number, day: string, q: number, price: number | null, over: Partial<IntegrityFactsRow> = {}): IntegrityFactsRow =>
  row(nm, day, { ordersUnitka: q, ordersFunnel: q, factOrderQty: q, factOrderRows: q, factualOrderPrice: price, priceSource: price === null ? null : 'ORDERS_API', funnelOrdersSum: price === null ? null : q * price, priceState: price === null ? 'MISSING_WITH_ACTIVITY' : 'PRESENT', ...over });
const fresh = (rows: CogsCanonicalRow[]): CogsSnapshot => classifyCogsSnapshot({ rows, publishedAt: '2026-10-03T06:50:00Z', runId: 'pub' }, NOW);
const canon = (nm: number, from: string, to: string, cogs: number | null): CogsCanonicalRow[] => {
  const out: CogsCanonicalRow[] = [];
  for (let d = from; d <= to; d = addDaysIso(d, 1)) out.push({ nmId: nm, internalSku: `SKU-${nm}`, day: d, cogsIntervalCount: cogs === null ? 0 : 1, canonicalCogs: cogs });
  return out;
};
/** Сентябрьская секция (первый день — строка 737): формула AI блока с заданным COGS-термом по дням. */
function inputs(facts: IntegrityFactsRow[], cogs: CogsCanonicalRow[], term: (nm: number, day: string) => string, over: Partial<IntegrityInputs> = {}): IntegrityInputs {
  const ai = (b: Block, r: number, t: string): string => `=IF($${colA1(b.start)}${r}>LAST_CLOSED_DATE,"",${colA1(b.start + OFFSET.priceMinusComm)}${r}-${colA1(b.start + OFFSET.logistics)}${r}-${colA1(b.start + OFFSET.tax)}${r}-${t})`;
  return {
    facts, cogs: fresh(cogs), blocks, lcd: LCD, monthStart: '2026-09-01', firstDailyRow: 737,
    cellAt: (_r, c): CellValue => ((c - 13) % 24 === OFFSET.spp ? 20 : null),
    formulaAt: (r, c) => { const b = blocks.find((x) => c === x.start + OFFSET.unitProfit); return b ? ai(b, r, term(b.nmId, addDaysIso('2026-09-01', r - 737))) : null; },
    refValues: { R45: 240 }, now: NOW, storageDueMinutes: 735, fromDay: '2026-09-01', toDay: '2026-09-30', ...over,
  };
}
const septemberQuiet = (): IntegrityFactsRow[] => [NM.A, NM.B].flatMap((nm) => Array.from({ length: 30 }, (_, i) => row(nm, addDaysIso('2026-09-01', i))));
const replace = (facts: IntegrityFactsRow[], r: IntegrityFactsRow): IntegrityFactsRow[] => facts.map((f) => (f.nmId === r.nmId && f.day === r.day ? r : f));
const allCanon = (a = 406.65, b = 231.38): CogsCanonicalRow[] => [...canon(NM.A, '2026-09-01', '2026-09-30', a), ...canon(NM.B, '2026-09-01', '2026-09-30', b)];
const okTerm = (nm: number): string => (nm === NM.A ? '406.65' : '231.38');
const codes = (issues: IntegrityIssue[]): string[] => issues.filter((i) => i.severity !== 'INFO').map((i) => `${i.code}:${i.severity}`).sort();

describe('цена и действительность строки', () => {
  it('заказ есть, цены нет → DATA_ERROR, строка фин. недействительна, статус не PASS', () => {
    const facts = replace(septemberQuiet(), withOrders(NM.A, '2026-09-17', 1, null, { divergenceClass: 'ONLY_FUNNEL', factOrderQty: null, factOrderRows: null }));
    const issues = evaluateIntegrity(inputs(facts, allCanon(), okTerm));
    const p = issues.filter((i) => i.code === 'PRICE_MISSING_WITH_ORDERS');
    expect(p).toMatchObject([{ day: '2026-09-17', nmId: NM.A, severity: 'ERROR', financialInvalid: true }]);
    // зрелое расхождение ONLY_FUNNEL без цены денег не подтверждает — второй, независимый признак той же недействительной строки
    expect(financialValidity(issues).get(`${NM.A}|2026-09-17`)).toEqual({ valid: false, codes: ['PRICE_MISSING_WITH_ORDERS', 'ORDERS_SOURCE_DIVERGENCE'] });
    const s = summarize(issues, 'observe', 'POST_WRITE', NOW, fresh(allCanon()));
    expect([s.status, s.states.DATA_ERROR, s.financially_invalid_rows, s.price_provenance.UNRESOLVED, s.oldest_unresolved_data_error]).toEqual(['DATA_ERROR', 2, 1, 1, '2026-09-17']);
  });
  it('доказанный fallback воронки → строка действительна; происхождение видно (INFO), расхождения денег нет', () => {
    const facts = replace(septemberQuiet(), withOrders(NM.A, '2026-09-17', 1, 1120, { priceSource: 'FUNNEL_FALLBACK', divergenceClass: 'ONLY_FUNNEL', factOrderQty: null, factOrderRows: null, funnelOrdersSum: 1120 }));
    const issues = evaluateIntegrity(inputs(facts, allCanon(), okTerm));
    expect(issues.filter((i) => i.code === 'PRICE_FUNNEL_FALLBACK')).toMatchObject([{ severity: 'INFO', financialInvalid: false }]);
    expect(issues.find((i) => i.code === 'PRICE_FUNNEL_FALLBACK')!.sourceValue).toContain('provenance=FUNNEL_FALLBACK');
    expect(codes(issues)).toEqual([]);
    expect(financialValidity(issues).get(`${NM.A}|2026-09-17`)!.valid).toBe(true);
    const s = summarize(issues, 'observe', 'POST_WRITE', NOW, fresh(allCanon()));
    expect([s.status, s.price_provenance.FUNNEL_FALLBACK]).toEqual(['PASS', 1]);
  });
  it('цена есть в источнике, но не в ячейке листа (режим observe, отказ секции) → PRICE_NOT_ON_SHEET: действительность определяет ЛИСТ', () => {
    const facts = replace(septemberQuiet(), withOrders(NM.A, '2026-09-17', 1, 1120, { priceSource: 'FUNNEL_FALLBACK', divergenceClass: 'ONLY_FUNNEL', factOrderQty: null, funnelOrdersSum: 1120 }));
    const priceCol = blocks[0]!.start + OFFSET.price;
    const blank = inputs(facts, allCanon(), okTerm, { checkSheetCells: true });                       // ячейка цены пуста
    const issues = evaluateIntegrity(blank);
    expect(issues.filter((i) => i.code === 'PRICE_NOT_ON_SHEET')).toMatchObject([{ day: '2026-09-17', nmId: NM.A, severity: 'ERROR', financialInvalid: true, source: `SHEET:${colA1(priceCol)}753` }]);
    const s = summarize(issues, 'observe', 'POST_WRITE', NOW, fresh(allCanon()));
    expect([s.status, s.financially_invalid_rows, s.price_provenance]).toEqual(['DATA_ERROR', 1, { FUNNEL_FALLBACK: 1, UNRESOLVED: 0, NOT_ON_SHEET: 1 }]);
    // режим observe обязан говорить именно это: ремонт доступен, а лист пока недействителен
    const cellIssue = issues.find((i) => i.code === 'PRICE_NOT_ON_SHEET')!;
    expect([cellIssue.repairAvailable, cellIssue.financialInvalid, cellIssue.diagnosticValue]).toEqual([true, true, 'repair_available=true; sheet_financial_valid=false']);
    expect(s.repair_available_sku_days).toBe(1);
    // после записи сверки ячейка заполнена → правило молчит; до записи (PRE_WRITE / SHADOW) правило выключено
    const filled = { ...blank, cellAt: (r: number, c: number): CellValue => (r === 753 && c === priceCol ? 1120 : blank.cellAt(r, c)) };
    expect(priceCellRules(filled)).toEqual([]);
    expect(summarize(evaluateIntegrity(filled), 'observe', 'POST_WRITE', NOW, fresh(allCanon()))).toMatchObject({ status: 'PASS', repair_available_sku_days: 0, financially_invalid_rows: 0 });
    // до записи (PRE_WRITE / SHADOW): ячейка, которую прогон сам запишет, дефектом не считается; остальные — считаются
    expect(priceCellRules({ ...blank, pendingWrite: (r, c) => r === 753 && c === priceCol })).toEqual([]);
    expect(priceCellRules({ ...blank, pendingWrite: () => false })).toHaveLength(1);
    expect(evaluateIntegrity({ ...blank, checkSheetCells: false }).filter((i) => i.code === 'PRICE_NOT_ON_SHEET')).toEqual([]);   // по умолчанию — Guard V1
  });
  it('нулевая цена при заказах → PRICE_ZERO_WITH_ORDERS, ERROR', () => {
    const issues = priceRules([withOrders(NM.A, '2026-09-17', 2, 0)], new Set([NM.A]), LCD);
    expect(issues).toMatchObject([{ code: 'PRICE_ZERO_WITH_ORDERS', severity: 'ERROR', financialInvalid: true }]);
  });
});

describe('расхождение счётчиков (решение владельца 20.09 №2): возраст ничего не доказывает; статус, а не повод пересчитать цену', () => {
  const young = '2026-09-25', old = '2026-09-10';        // возраст на 03.10: 8 и 23 дня; срок зрелости Orders API — 14
  const behind = (day: string, over: Partial<IntegrityFactsRow> = {}): IntegrityFactsRow =>
    withOrders(NM.A, day, 2, 806, { factOrderQty: 1, factOrderRows: 1, divergenceClass: 'FUNNEL_GT_FACT', funnelOrdersSum: 1612, sameDayCancelQty: 0, ...over });
  it('пороги зрелости — из замеров: Orders API 14 дней (p99 = 13,9 дня на 907 заказах), воронка 0 (0 изменений на 1 575 значениях)', () => {
    expect([ORDERS_API_MATURITY_DAYS, FUNNEL_MATURITY_DAYS]).toEqual([14, 0]);
  });
  it('Orders API отстаёт, в окне запаздывания → LATE_DATA; цена остаётся ценой Orders API и не пересчитывается', () => {
    const r = behind(young);
    const d = divergenceRules([r], LCD, NOW);
    expect(d).toMatchObject([{ code: 'ORDERS_SOURCE_DIVERGENCE', severity: 'EXPECTED_DELAY', financialInvalid: false, blocking: false }]);
    expect(contractState(d[0]!.severity)).toBe('LATE_DATA');
    expect(d[0]!.sourceValue).toContain('verdict=LATE_DATA');
    expect(priceRules([r], new Set([NM.A]), LCD)).toEqual([]);                                  // цена есть и не трогается
  });
  it('в окне запаздывания, но деньги НЕ подтверждены → состояние LATE_DATA, а действительность отдельно: financial_valid = false', () => {
    const d = divergenceRules([behind(young, { funnelOrdersSum: 1500 })], LCD, NOW);
    expect(d).toMatchObject([{ severity: 'EXPECTED_DELAY', financialInvalid: true, blocking: false }]);
    const s = summarize(d, 'observe', 'POST_WRITE', NOW, fresh(allCanon()));
    expect([s.status, s.states.LATE_DATA, s.states.DATA_ERROR, s.financially_invalid_rows]).toEqual(['PASS', 1, 0, 1]);
  });
  it('срок истёк, финансово значимое расхождение не разрешено → DATA_ERROR, financial_valid = false (НЕ WARNING по возрасту)', () => {
    const r = behind(old, { funnelOrdersSum: 1500 });                                           // второй заказ шёл по другой цене
    expect(amountConfirmedByFunnel(r)).toBe(false);
    const d = divergenceRules([r], LCD, NOW);
    expect(d).toMatchObject([{ severity: 'ERROR', financialInvalid: true, blocking: true }]);
    expect(d[0]!.sourceValue).toContain('verdict=AMOUNT_NOT_CONFIRMED');
    expect(summarize(d, 'observe', 'POST_WRITE', NOW, fresh(allCanon())).status).toBe('DATA_ERROR');
    // цена при этом не выдумывается: правило цены молчит, в строке остаётся цена Orders API
    expect(priceRules([r], new Set([NM.A]), LCD)).toEqual([]);
  });
  it('срок истёк, но доказано, что на результат не влияет (2 × 806 = 1612 = сумма воронки) → WARNING, строка действительна', () => {
    const r = behind(old);
    expect(amountConfirmedByFunnel(r)).toBe(true);
    const d = divergenceRules([r], LCD, NOW);
    expect(d).toMatchObject([{ severity: 'WARNING', financialInvalid: false }]);
    expect(d[0]!.sourceValue).toContain('verdict=AMOUNT_CONFIRMED');
  });
  it('WARNING даёт только доказательство, а не возраст: та же строка без суммы воронки или без цены → DATA_ERROR', () => {
    expect(divergenceRules([behind(old, { funnelOrdersSum: null })], LCD, NOW)).toMatchObject([{ severity: 'ERROR', financialInvalid: true }]);
    expect(divergenceRules([behind(old, { factualOrderPrice: null, priceSource: null })], LCD, NOW)).toMatchObject([{ severity: 'ERROR', financialInvalid: true }]);
    expect(divergenceRules([behind('2026-09-01')], LCD, NOW)).toMatchObject([{ severity: 'WARNING' }]);   // возраст 32 дня: всё равно нужно доказательство — оно есть
  });
  it('Orders API опережает воронку, разница объяснена отменой ДНЯ ЗАКАЗА → INFO, строка достоверна (контракт отмен 20.09.2026)', () => {
    // Доказано на официальном экспорте воронки WB 01–19.09.2026: заказ, отменённый в день заказа, воронка не
    // считает НИ в «Заказали товаров», НИ в «Отменили, шт» (5 из 5 случаев). Q его не содержит, и S (только отмены
    // следующих дней) его не вычитает — двойного учёта больше нет, строка финансово достоверна.
    const sameDay = withOrders(NM.A, young, 2, 666, { factOrderQty: 3, factOrderRows: 3, cancelsUnitka: 0, divergenceClass: 'FACT_GT_FUNNEL', funnelOrdersSum: 1332, sameDayCancelQty: 1 });
    const onlyFact = row(NM.A, '2026-10-01', { factOrderQty: 1, factOrderRows: 1, cancelsUnitka: 0, factualOrderPrice: 800, priceSource: 'ORDERS_API', priceState: 'PRESENT', divergenceClass: 'ONLY_FACT', funnelOrdersSum: 0, sameDayCancelQty: 1 });
    const d = divergenceRules([sameDay, onlyFact], LCD, NOW);
    expect(d.map((i) => [i.severity, i.financialInvalid])).toEqual([['INFO', false], ['INFO', false]]);
    expect(d.map((i) => /verdict=(\w+)/.exec(i.sourceValue!)![1])).toEqual(['SAME_DAY_CANCEL_EXCLUDED_BY_FUNNEL', 'SAME_DAY_CANCEL_EXCLUDED_BY_FUNNEL']);
    expect(d[0]!.dependentFields).toEqual(['Q', 'S']);
    expect(summarize(d, 'observe', 'POST_WRITE', NOW, fresh(allCanon()))).toMatchObject({ status: 'PASS', financially_invalid_rows: 0 });
  });
  it('Orders API опережает воронку, но отменами дня заказа разница НЕ объясняется → DATA_ERROR', () => {
    const unexplained = withOrders(NM.A, old, 5, 740, { factOrderQty: 6, factOrderRows: 6, divergenceClass: 'FACT_GT_FUNNEL', funnelOrdersSum: 3700, sameDayCancelQty: 0 });
    const partly = withOrders(NM.A, old, 5, 740, { factOrderQty: 7, factOrderRows: 7, divergenceClass: 'FACT_GT_FUNNEL', funnelOrdersSum: 3700, sameDayCancelQty: 1 });  // 7 − 1 ≠ 5
    const d = divergenceRules([unexplained, partly], LCD, NOW);
    expect(d.map((i) => [i.severity, i.financialInvalid])).toEqual([['ERROR', true], ['ERROR', true]]);
    expect(d.map((i) => /verdict=(\w+)/.exec(i.sourceValue!)![1])).toEqual(['ORDERS_API_AHEAD_UNEXPLAINED', 'ORDERS_API_AHEAD_UNEXPLAINED']);
  });
  it('ONLY_FUNNEL с доказанным fallback — не расхождение денег (INFO); без разрешённой цены — ошибку даёт правило цены', () => {
    const fb = withOrders(NM.A, old, 1, 1120, { priceSource: 'FUNNEL_FALLBACK', divergenceClass: 'ONLY_FUNNEL', factOrderQty: null, factOrderRows: null, funnelOrdersSum: 1120 });
    expect(divergenceRules([fb], LCD, NOW)).toMatchObject([{ severity: 'INFO', financialInvalid: false }]);
  });
  it('старая вью без суммы воронки (режим off) — прежнее поведение Guard V1 (INFO / WARNING), ERROR не возникает', () => {
    const legacy = { ...behind(old) };
    delete (legacy as Partial<IntegrityFactsRow>).funnelOrdersSum; delete (legacy as Partial<IntegrityFactsRow>).sameDayCancelQty;
    expect(divergenceRules([legacy], LCD, NOW)).toMatchObject([{ severity: 'INFO' }]);
  });
});

describe('COGS: лист против канона на дату', () => {
  const active = (day: string): IntegrityFactsRow => withOrders(NM.B, day, 3, 640);
  it('совпадение с каноном (в пределах округления до копейки) — нет issue', () => {
    const issues = cogsRules(inputs(replace(septemberQuiet(), active('2026-09-17')), allCanon(406.651629), okTerm));
    expect(issues).toEqual([]);
  });
  it('расхождение при заказах → ERROR, фин. недействительны именно дни с активностью (240 ↔ 231,38 через $R$45)', () => {
    const facts = replace(replace(septemberQuiet(), active('2026-09-16')), active('2026-09-17'));
    const issues = cogsRules(inputs(facts, allCanon(), (nm) => (nm === NM.B ? '$R$45' : '406.65')));
    expect(issues).toMatchObject([{ code: 'COGS_SOURCE_MISMATCH', nmId: NM.B, severity: 'ERROR', blocking: true, financialInvalid: true, invalidDays: ['2026-09-16', '2026-09-17'] }]);
    const v = financialValidity(issues);
    expect([v.get(`${NM.B}|2026-09-16`)!.valid, v.get(`${NM.B}|2026-09-17`)!.valid, v.has(`${NM.B}|2026-09-18`)]).toEqual([false, false, false]);
    expect(summarize(issues, 'observe', 'POST_WRITE', NOW, fresh(allCanon())).financially_invalid_rows).toBe(2);
  });
  it('расхождение без заказов в дни расхождения — латентно, WARNING', () => {
    const issues = cogsRules(inputs(septemberQuiet(), allCanon(), (nm) => (nm === NM.B ? '$R$45' : '406.65')));
    expect(issues).toMatchObject([{ code: 'COGS_SOURCE_MISMATCH', severity: 'WARNING', financialInvalid: false }]);
    expect(issues[0]!.invalidDays).toBeUndefined();
  });
  it('канона на дату нет, заказы есть → COGS_ZERO_OR_MISSING, ERROR', () => {
    const facts = replace(septemberQuiet(), active('2026-09-17'));
    const issues = cogsRules(inputs(facts, [...canon(NM.A, '2026-09-01', '2026-09-30', 406.65), ...canon(NM.B, '2026-09-01', '2026-09-30', null)], okTerm));
    expect(issues).toMatchObject([{ code: 'COGS_ZERO_OR_MISSING', nmId: NM.B, severity: 'ERROR', financialInvalid: true, invalidDays: ['2026-09-17'] }]);
  });
  it('смена интервала канона посреди месяца: литерал листа верен до 10.09 и неверен с 11.09 — недействительны только поздние дни', () => {
    const facts = replace(replace(septemberQuiet(), active('2026-09-05')), active('2026-09-20'));
    const cogs = [...canon(NM.A, '2026-09-01', '2026-09-30', 406.65), ...canon(NM.B, '2026-09-01', '2026-09-10', 231.38), ...canon(NM.B, '2026-09-11', '2026-09-30', 250)];
    const issues = cogsRules(inputs(facts, cogs, okTerm));
    expect(issues).toMatchObject([{ code: 'COGS_SOURCE_MISMATCH', severity: 'ERROR', invalidDays: ['2026-09-20'] }]);
    // лист, где терм меняется в тот же день, что и канон, — чист (дата-эффективность по строкам)
    expect(cogsRules(inputs(facts, cogs, (nm, day) => (nm === NM.A ? '406.65' : day <= '2026-09-10' ? '231.38' : '250')))).toEqual([]);
  });
  it('будущий SKU: новый блок с литералом канона сверяется так же, как старые', () => {
    const b: Block = { index: 0, slot: 24, start: slotStart(24), nmId: NM.NEW, title: `${NM.NEW}` };
    const facts = Array.from({ length: 2 }, (_, i) => withOrders(NM.NEW, addDaysIso('2026-10-01', i), 1, 1500));
    const inp: IntegrityInputs = {
      ...inputs(facts, canon(NM.NEW, '2026-10-01', '2026-10-02', 426.735), () => ''), blocks: [b], monthStart: '2026-10-01', firstDailyRow: 771, fromDay: undefined, toDay: undefined,
      formulaAt: (r, c) => (c === b.start + OFFSET.unitProfit ? `=IF($${colA1(b.start)}${r}>LAST_CLOSED_DATE,"",${colA1(b.start + OFFSET.priceMinusComm)}${r}-${colA1(b.start + OFFSET.logistics)}${r}-${colA1(b.start + OFFSET.tax)}${r}-426.735)` : null),
    };
    expect(cogsRules(inp)).toEqual([]);
  });
});

describe('состояния не схлопываются: MANUAL_REQUIRED, LATE_DATA, NOT_AVAILABLE — отдельно, и сами по себе не «ломают» систему', () => {
  it('СПП не введена → MANUAL_REQUIRED (не дефект источника); 0 — заполнено', () => {
    const facts = replace(septemberQuiet(), withOrders(NM.A, '2026-09-17', 1, 1120));
    const inp = inputs(facts, allCanon(), okTerm, { cellAt: () => null });
    expect(sppRules(inp)).toMatchObject([{ code: 'SPP_MISSING', severity: 'MANUAL_REQUIRED', financialInvalid: false, day: '2026-09-17' }]);
    expect(sppRules({ ...inp, cellAt: () => 0 })).toEqual([]);
  });
  it('нет снимка остатков за дату → NOT_AVAILABLE, одна issue на дату, статус прогона не поднимает', () => {
    const facts = septemberQuiet().map((f) => (f.day === '2026-09-03' ? { ...f, stockDateCovered: false } : f));
    const s = stockRules(facts, LCD);
    expect(s).toMatchObject([{ code: 'STOCK_SNAPSHOT_MISSING', day: '2026-09-03', nmId: null, severity: 'NOT_AVAILABLE', financialInvalid: false }]);
    expect(aggregateStatus(s)).toBe('PASS');
  });
  it('сводка: каждое состояние своим счётчиком; NOT_AVAILABLE и MANUAL_REQUIRED не дают DATA_ERROR', () => {
    let facts = septemberQuiet().map((f) => (f.day === '2026-09-03' ? { ...f, stockDateCovered: false } : f));
    facts = replace(facts, withOrders(NM.A, '2026-09-17', 1, 1120));
    facts = replace(facts, withOrders(NM.A, '2026-09-25', 2, 806, { factOrderQty: 1, divergenceClass: 'FUNNEL_GT_FACT', funnelOrdersSum: 1612 }));
    const issues = evaluateIntegrity(inputs(facts, allCanon(), okTerm, { cellAt: () => null }));
    const s = summarize(issues, 'observe', 'POST_WRITE', NOW, fresh(allCanon()));
    expect(s.states).toMatchObject({ DATA_ERROR: 0, LATE_DATA: 1, MANUAL_REQUIRED: 2, NOT_AVAILABLE: 1 });
    expect([s.status, s.financially_invalid_rows, s.affected_sku_days, s.oldest_unresolved, s.oldest_unresolved_data_error]).toEqual(['MANUAL_REQUIRED', 0, 2, '2026-09-17', null]);
  });
  it('фин. недействительная строка не может дать PASS', () => {
    const facts = replace(septemberQuiet(), withOrders(NM.A, '2026-09-17', 1, null, { divergenceClass: 'ONLY_FUNNEL', factOrderQty: null }));
    const issues = evaluateIntegrity(inputs(facts, allCanon(), okTerm));
    expect(['PASS', 'PASS_WITH_WARNINGS', 'MANUAL_REQUIRED']).not.toContain(aggregateStatus(issues));
  });
});

describe('оценка секции за дни окна', () => {
  it('прошлый месяц: дни [from, конец месяца]; месяц LCD: [начало месяца, LCD]; по умолчанию — поведение Guard V1', () => {
    expect(sectionDays({ monthStart: '2026-09-01', lcd: '2026-10-06', fromDay: '2026-09-02', toDay: '2026-09-30' }).map((d) => d.i)).toEqual(Array.from({ length: 29 }, (_, i) => i + 1));
    expect(sectionDays({ monthStart: '2026-10-01', lcd: '2026-10-06' }).map((d) => d.day)).toEqual(['2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05', '2026-10-06']);
    expect(sectionDays({ monthStart: '2026-09-01', lcd: '2026-09-17' })).toHaveLength(17);
  });
  it('СПП и COGS прошлой секции читаются только в её строках (не за пределами месяца)', () => {
    const rowsRead: number[] = [];
    const facts = replace(septemberQuiet(), withOrders(NM.A, '2026-09-30', 1, 1120));
    const inp = inputs(facts, allCanon(), okTerm, { cellAt: (r) => { rowsRead.push(r); return null; } });
    sppRules(inp);
    expect(Math.max(...rowsRead)).toBe(766);                                                    // 30.09 — последняя строка дня сентября
  });
});
