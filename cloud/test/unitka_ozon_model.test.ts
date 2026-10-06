import { describe, expect, it } from 'vitest';
import { composeMonth, ozonMonthSpec, provenOrderReferencePrice, serialOf,
  type OzonFactRow, type OzonMonthSpec } from '../src/loaders/unitka/ozon/month.js';
import { buildOzonPlan, sectionFormulas, type OzonSectionInput } from '../src/loaders/unitka/ozon/monthplan.js';
import { layoutOf } from '../src/loaders/unitka/ozon/requests.js';
import { parseLiveLayout } from '../src/loaders/unitka/ozon/loader.js';
import { ozonSourceCompletenessIssues, ozonWrittenReader, verifyOzonModel } from '../src/loaders/unitka/ozon/model_qa.js';
import { ozonRewriteWindow, ozonDeepWindow, ozonFromDayFor } from '../src/loaders/unitka/ozon/window.js';
import { ozonMonthFactsSql } from '../src/loaders/unitka/ozon/bq.js';
import { OZON_ROLE_CLASS } from '../src/loaders/unitka/ozon/presentation.js';
import { readbackAndVerify } from '../src/loaders/unitka/ozon/ozon_lifecycle.js';
import type { SheetsGateway } from '../src/loaders/unitka/sheets.js';

const SKU = '305101272';
const SEP = ozonMonthSpec(2026, 9, 570, 31, ['fixture-a', 'fixture-b', 'fixture-c', SKU]);
const TARGET: OzonFactRow = { d: '2026-09-20', offer_id: SKU, gross_qty: 1, cancelled_qty: 0,
  realized_qty: 1, expected_realized_qty: 1, revenue: 0, provisional_revenue_rub: 0,
  order_reference_price: 1287, order_reference_qty: 1, order_reference_covered_qty: 1,
  cogs_amt: 130.81, provisional_cogs_rub: 130.81, cogs_missing_qty: 0,
  logistics: 105, logistics_actual: 105, logistics_state: 'ACTUAL',
  commission: 669.24, commission_actual: 0, commission_estimated_rub: 669.24,
  commission_state: 'ESTIMATED', commission_estimate_method: 'COMMISSION_POLICY_TARIFF',
  commission_missing_qty: 1, commission_not_applicable_qty: 0,
  buyout_revenue_unproven_qty: 1, economics_completeness: 'PROVISIONAL_PARTIAL',
  ads_spend: 336.64, impr: 1192, clicks: 28, buyer_amt: null, seller_amt: null };

function prepared(facts: OzonFactRow[] = [TARGET], spec = SEP, fromDay = 1,
  cart: Record<string, number> = {}, bloggers: Record<string, number> = {}, externalAds: Record<string, number | string> = {},
  lcd = `${spec.key}-${String(spec.days).padStart(2, '0')}`) {
  const sheetInputs = { bloggers, externalAds };
  const comp = composeMonth(spec, facts, {}, lcd, fromDay, cart, sheetInputs);
  const sec: OzonSectionInput = { spec, facts, stock: {}, cart, sheetInputs, fromDay,
    formulas: sectionFormulas(spec, comp, 'SEMICOLON', 'OZON_LAST_CLOSED_DATE'),
    refTitle: [], refHeader: [], refAnchor: {} };
  const plan = buildOzonPlan({ sheetId: 1, sheetName: 'OZON_Юнит_2025', sections: [sec],
    allSections: [layoutOf(spec)], grid: { current: { rows: 800, columns: 600 },
      growth: { widenBlockAt: [], insertColumnsBefore: 590, insertColumnCount: 0, appendColumnCount: 0, appendRowCount: 0 } },
    lcd, lcdMirror: null, lcdRef: '$VB$2', blocks: spec.blocks.length });
  return { plan, sec, comp, lcd, read: ozonWrittenReader(plan.values) };
}

/** Небольшой независимый interpreter фактически собранных дневных формул.
 * Не вызывает расчётные функции production для вычисления результата.
 */
function evaluate(read: (r: number, c: number) => unknown, lcd: string, cell: string): unknown {
  const cache = new Map<string, unknown>(), num = (v: unknown) => typeof v === 'number' ? v : 0;
  type Ast = { value?: unknown; ref?: string; fn?: string; args?: Ast[]; op?: string; a?: Ast; b?: Ast };
  function parse(f: string): Ast {
    const t = f.slice(1).match(/"[^"]*"|\$?[A-Z]+\$?\d+|[A-Z_][A-Z_0-9]*|\d+(?:[.,]\d+)?|[();+\-*/%=<>]/g)!;
    let i = 0;
    const primary = (): Ast => {
      const v = t[i++]!; let a: Ast;
      if (v === '(') { a = cmp(); if (t[i++] !== ')') throw Error('close'); }
      else if (v.startsWith('"')) a = { value: v.slice(1, -1) };
      else if (/^\d/.test(v)) a = { value: Number(v.replace(',', '.')) };
      else if (t[i] === '(') { i++; const args: Ast[] = [];
        do { args.push(cmp()); } while (t[i] === ';' && (++i > 0));
        if (t[i++] !== ')') throw Error('function close'); a = { fn: v, args };
      } else a = { ref: v.replaceAll('$', '') };
      while (t[i] === '%') { i++; a = { op: '/', a, b: { value: 100 } }; } return a;
    };
    const mul = (): Ast => { let a = primary(); while (['*', '/'].includes(t[i]!)) { const op = t[i++]!; a = { op, a, b: primary() }; } return a; };
    const add = (): Ast => { let a = mul(); while (['+', '-'].includes(t[i]!)) { const op = t[i++]!; a = { op, a, b: mul() }; } return a; };
    const cmp = (): Ast => { let a = add(); while (['=', '>', '<'].includes(t[i]!)) { const op = t[i++]!; a = { op, a, b: add() }; } return a; };
    const a = cmp(); if (i !== t.length) throw Error('unused tokens'); return a;
  }
  function get(k: string): unknown {
    if (k === 'OZON_LAST_CLOSED_DATE') return serialOf(lcd);
    if (cache.has(k)) return cache.get(k);
    const m = /^([A-Z]+)(\d+)$/.exec(k)!;
    const col = [...m[1]!].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
    const v = read(Number(m[2]), col) ?? '';
    const result = typeof v === 'string' && v.startsWith('=') ? ev(parse(v)) : v;
    cache.set(k, result); return result;
  }
  function ev(a: Ast): unknown {
    if ('value' in a) return a.value; if (a.ref) return get(a.ref);
    if (a.fn) { const x = a.args!;
      if (a.fn === 'IF') return ev(x[0]!) ? ev(x[1]!) : ev(x[2]!);
      if (a.fn === 'IFERROR') { try { return ev(x[0]!); } catch { return ev(x[1]!); } }
      if (a.fn === 'N') return num(ev(x[0]!));
      if (a.fn === 'OR') return x.some((b) => !!ev(b)); throw Error(a.fn);
    }
    const x = ev(a.a!), y = ev(a.b!);
    switch (a.op) { case '+': return num(x) + num(y); case '-': return num(x) - num(y);
      case '*': return num(x) * num(y); case '/': if (num(y) === 0) throw Error('DIV0'); return num(x) / num(y);
      case '=': return x === y; case '>': return num(x) > num(y); default: throw Error(String(a.op)); }
  }
  return get(cell);
}

describe('Ozon: proven calculator model, posting 92767357-0024-1', () => {
  it('1287 price with unproven revenue/SPP includes the dated estimate and proven costs', () => {
    const p = prepared();
    expect(p.read(626, 101)).toBe(1287); // CW
    expect(p.read(626, 102)).toBe(''); // CX
    expect(p.read(626, 104)).toBe(.52); // CZ: ESTIMATED, not NOT_APPLICABLE
    expect(p.read(626, 106)).toBe(105); // DB
    expect(p.read(626, 110)).toContain('-130,81'); // DF
    expect(evaluate(p.read, p.lcd, 'CY626')).toBe('');
    expect(evaluate(p.read, p.lcd, 'CV626')).toBe('');
    expect(evaluate(p.read, p.lcd, 'DA626')).toBeCloseTo(617.76, 6);
    expect(evaluate(p.read, p.lcd, 'DE626')).toBeCloseTo(25.74, 6);
    expect(evaluate(p.read, p.lcd, 'DF626')).toBeCloseTo(356.21, 6);
    expect(evaluate(p.read, p.lcd, 'CS626')).toBeCloseTo(19.57, 6);
    expect(evaluate(p.read, p.lcd, 'CS626')).not.toBe(924.62);
    expect(p.comp.totals.revenue).toBe(0);
    expect(TARGET.revenue).toBe(0); expect(TARGET.buyer_amt).toBeNull();
    expect(TARGET.economics_completeness).toBe('PROVISIONAL_PARTIAL');
    expect(p.comp.totals.comm).toBe(669.24);
    expect(p.comp.provenance).toContainEqual(expect.objectContaining({ component: 'COMMISSION',
      state: 'ESTIMATED', method: 'COMMISSION_POLICY_TARIFF', estimatedRub: 669.24, actualRub: 0 }));
    expect(JSON.stringify(p.plan.presentation)).toContain('не окончательная прибыль');
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
  });

  it.each(['complete finance', 'in transit', 'documented CIS'])('%s keeps existing economic numbers', (kind) => {
    const row: OzonFactRow = { ...TARGET, buyout_revenue_unproven_qty: 0,
      economics_completeness: kind === 'in transit' ? 'PROVISIONAL_COMPLETE' : 'ACTUAL',
      revenue: kind === 'documented CIS' ? 558.88 : kind === 'in transit' ? 0 : 1287,
      provisional_revenue_rub: kind === 'documented CIS' ? 558.88 : 1287,
      realized_qty: kind === 'in transit' ? 0 : 1, in_transit_qty: kind === 'in transit' ? 1 : 0,
      commission: kind === 'documented CIS' ? 0 : 669.24,
      commission_actual: kind === 'in transit' || kind === 'documented CIS' ? 0 : 669.24,
      commission_estimated_rub: kind === 'in transit' ? 669.24 : 0,
      commission_state: kind === 'documented CIS' ? 'NOT_APPLICABLE' : kind === 'in transit' ? 'ESTIMATED' : 'ACTUAL',
      commission_estimate_method: kind === 'in transit' ? 'COMMISSION_POLICY_TARIFF' : null,
      commission_missing_qty: kind === 'in transit' ? 1 : 0,
      commission_not_applicable_qty: kind === 'documented CIS' ? 1 : 0,
      buyer_amt: kind === 'in transit' ? null : 333.33, seller_amt: kind === 'in transit' ? null : 1287 };
    const p = prepared([row]);
    expect(p.read(626, 101)).toBe(row.provisional_revenue_rub);
    expect(evaluate(p.read, p.lcd, 'CS626')).toBeCloseTo(row.provisional_revenue_rub! - row.commission!
      - 105 - 130.81 - 336.64 - row.provisional_revenue_rub! * .02, 5);
    expect(p.read(626, 102)).toBe(kind === 'in transit' ? '' : Math.round((1 - 333.33 / 1287) * 100e6) / 1e6);
  });

  it.each(['documented buyout', 'ordinary actual finance'] as const)('historical refresh replaces estimate with %s without double counting', (kind) => {
    const first = prepared();
    const buyout = kind === 'documented buyout';
    const stronger: OzonFactRow = { ...TARGET, revenue: buyout ? 558.88 : 1287,
      provisional_revenue_rub: buyout ? 558.88 : 1287, buyout_revenue_unproven_qty: 0,
      economics_completeness: 'ACTUAL', commission: buyout ? 0 : 579.15,
      commission_actual: buyout ? 0 : 579.15, commission_estimated_rub: 0,
      commission_state: buyout ? 'NOT_APPLICABLE' : 'ACTUAL', commission_estimate_method: null,
      commission_missing_qty: 0, commission_not_applicable_qty: buyout ? 1 : 0 };
    // The same historical day/SKU is rebuilt from current canonical sources, never appended.
    const later = prepared([stronger], SEP, ozonFromDayFor(SEP.key, ozonRewriteWindow('2026-10-03')));
    const cells = new Map<string, unknown>([['626:104', first.read(626, 104)]]);
    cells.set('626:104', later.read(626, 104));
    const commissionNote = (p: ReturnType<typeof prepared>, initial = '') => {
      let note = initial;
      for (const req of p.plan.presentation) {
        const r = req.repeatCell as { range?: { startRowIndex: number; endRowIndex: number;
          startColumnIndex: number; endColumnIndex: number }; cell?: { note?: string }; fields?: string } | undefined;
        if (r?.fields === 'note' && r.range && r.range.startRowIndex <= 625 && r.range.endRowIndex > 625
          && r.range.startColumnIndex <= 103 && r.range.endColumnIndex > 103 && r.cell?.note !== undefined) note = r.cell.note;
      }
      return note;
    };
    const priorNote = commissionNote(first);
    expect(priorNote).toContain('ОЦЕНКА');
    expect(priorNote).toContain('документ о выкупе');
    expect(priorNote).not.toContain('заказ ещё в пути');
    expect(commissionNote(later, priorNote)).toBe('');
    expect(cells.get('626:104')).toBe(buyout ? 0 : .45);
    expect(later.read(626, 101)).toBe(stronger.provisional_revenue_rub);
    expect(later.comp.totals.comm).toBe(stronger.commission);
    expect(later.comp.provenance.filter((p) => p.component === 'COMMISSION')).toEqual([]);
    expect(later.comp.partialEconomics).toEqual([]);
    expect(JSON.stringify(later.plan.presentation)).not.toContain('COMMISSION_POLICY_TARIFF');
    const expected = stronger.provisional_revenue_rub! - stronger.commission! - 105 - 130.81
      - stronger.provisional_revenue_rub! * .02 - 336.64;
    expect(evaluate(later.read, later.lcd, 'CS626')).toBeCloseTo(expected, 6);
    expect(later.comp.totals.canonical).toBeCloseTo(expected, 6);
    expect(verifyOzonModel({ sections: [later.sec], lcd: later.lcd, read: later.read, mode: 'PLAN' })).toEqual([]);
  });

  it.each([null, 0, -1, NaN, Infinity])('invalid reference %s fails closed', (price) => {
    const p = prepared([{ ...TARGET, order_reference_price: price }]);
    expect(p.read(626, 101)).toBe('');
  });
  it.each([0, 2, null])('incomplete/mismatched price coverage %s fails closed', (qty) => {
    expect(provenOrderReferencePrice({ ...TARGET, order_reference_covered_qty: qty }, 1)).toBeUndefined();
  });
  it('known logistics/COGS survive even when both revenue and proven reference price are absent', () => {
    const p = prepared([{ ...TARGET, order_reference_price: null }]);
    expect(p.read(626, 101)).toBe('');
    expect(p.read(626, 106)).toBe(105);
    expect(p.read(626, 110)).toContain('-130,81');
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
  });
  it('weighted source price is used without changing recognized revenue', () => {
    const row = { ...TARGET, gross_qty: 3, realized_qty: 3, expected_realized_qty: 3,
      order_reference_qty: 3, order_reference_covered_qty: 3, order_reference_price: (1000 + 2 * 1300) / 3 };
    expect(prepared([row]).read(626, 101)).toBe(1200);
    expect(row.revenue).toBe(0);
  });

  it.each(['45 days', '120 days', 'rollover'])('%s preserves manual blank/0/positive and external output without writing them', (mode) => {
    const spec: OzonMonthSpec = mode === 'rollover' ? ozonMonthSpec(2026, 10, 605, 30, [SKU]) : SEP;
    const window = mode === '120 days' ? ozonDeepWindow('2026-10-03') : ozonRewriteWindow('2026-10-03');
    const zero = `${spec.key}-20|${SKU}`, positive = `${spec.key}-21|${SKU}`;
    const p = prepared([], spec, ozonFromDayFor(spec.key, window), {}, { [zero]: 0, [positive]: 2 }, { [positive]: -412 });
    const base = spec.anchor[SKU]!;
    for (const day of [19, 20, 21]) for (const off of [1, 12]) expect(p.read(spec.firstRow + day - 1, base + off)).toBeUndefined();
    expect(p.comp.cells[`${SKU}|${spec.firstRow + 19}`]).toMatchObject({ bloggers: 0 });
    expect(p.comp.cells[`${SKU}|${spec.firstRow + 20}`]).toMatchObject({ bloggers: 2, externalAds: -412 });
    expect(p.comp.cells[`${SKU}|${spec.firstRow + 18}`]).not.toHaveProperty('bloggers');
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
  });

  it('live layout preserves explicit zero and positive owner inputs by date x SKU', () => {
    const g: (string | number)[][] = Array.from({ length: 34 }, () => Array(40).fill(''));
    g[0]![0] = 'Сентябрь 2026'; g[0]![11] = SKU; g[1]![11] = 'Дата';
    for (let i = 0; i < 3; i++) g[i + 2]![11] = serialOf(`2026-09-0${i + 1}`);
    g[3]![12] = 0; g[4]![12] = 2; g[4]![23] = -412;
    const inputs = parseLiveLayout(g, 37, (s) => s === SKU ? s : null);
    expect(inputs.bloggers).toEqual({ [`2026-09-02|${SKU}`]: 0, [`2026-09-03|${SKU}`]: 2 });
    expect(inputs.externalAds).toEqual({ [`2026-09-03|${SKU}`]: -412 });
  });
  it.each(['45 days', '120 days'])('%s actual rollover keeps prior and future owner cells byte-for-byte', (mode) => {
    const lcd = '2026-10-03';
    const w = mode === '120 days' ? ozonDeepWindow(lcd) : ozonRewriteWindow(lcd);
    const october = ozonMonthSpec(2026, 10, SEP.titleRow, SEP.days, SEP.blocks);
    for (const spec of [SEP, october]) {
      const inputs = { [`${spec.key}-20|${SKU}`]: 0, [`${spec.key}-21|${SKU}`]: 2 };
      const external = { [`${spec.key}-21|${SKU}`]: '=unproven_existing_formula' };
      const p = prepared([], spec, ozonFromDayFor(spec.key, w), {}, inputs, external, lcd);
      const base = spec.anchor[SKU]!;
      const before = new Map<string, number | string>([
        [`${spec.firstRow + 18}:${base + 1}`, ''],
        [`${spec.firstRow + 19}:${base + 1}`, 0],
        [`${spec.firstRow + 20}:${base + 1}`, 2],
        [`${spec.firstRow + 20}:${base + 12}`, external[`${spec.key}-21|${SKU}`]!],
      ]);
      const after = new Map(before);
      for (const [k] of before) {
        const [row, col] = k.split(':').map(Number) as [number, number];
        const written = p.read(row, col); if (written !== undefined) after.set(k, written as number | string);
      }
      expect(after).toEqual(before);
      expect(verifyOzonModel({ sections: [p.sec], lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
    }
  });

  it.each([0, 9])('cart observation %s survives the full planner', (cart) => {
    const p = prepared([TARGET], SEP, 1, { [`2026-09-20|${SKU}`]: cart });
    expect(p.read(626, 92)).toBe(cart);
  });
  it('external output is CALC and no new daily formula/value is published', () => {
    expect(OZON_ROLE_CLASS.EXTERNAL_ADS).toBe('CALC');
    expect(prepared().read(626, 99)).toBeUndefined();
  });

  it.each([
    ['ORDER_PRICE_OMITTED_OR_CHANGED', 101, ''], ['KNOWN_LOGISTICS_OMITTED', 106, ''],
    ['COMMISSION_OMITTED_OR_CHANGED', 104, 0],
    ['KNOWN_COGS_OMITTED', 110, '=IF($CI626>OZON_LAST_CLOSED_DATE;"";IF(N(DA626)=0;"";DA626-N(DB626)-N(DE626)-0))'],
    ['BLOGGERS_OVERWRITTEN', 88, ''], ['CART_OBSERVATION_LOST', 92, ''],
  ] as const)('independent source QA detects %s', (code, col, wrong) => {
    const p = prepared([TARGET], SEP, 1, { [`2026-09-20|${SKU}`]: 9 }, { [`2026-09-20|${SKU}`]: 2 });
    const read = (r: number, c: number) => r === 626 && c === col ? wrong : p.read(r, c);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read, mode: 'PLAN' }).map((i) => i.code)).toContain(code);
  });
  it('independent source QA rejects ACTUAL with unproven revenue', () => {
    const p = prepared([{ ...TARGET, economics_completeness: 'ACTUAL' }]);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })[0]!.code).toBe('ACTUAL_WITH_UNPROVEN_REVENUE');
  });
  it('structural fallback alone cannot prove NOT_APPLICABLE', () => {
    const p = prepared([{ ...TARGET, commission: 0, commission_estimated_rub: 0,
      commission_state: 'NOT_APPLICABLE', commission_missing_qty: 0, commission_not_applicable_qty: 1 }]);
    expect(p.read(626, 104)).toBe('');
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' }).map((i) => i.code))
      .toContain('UNPROVEN_BUYOUT_COMMISSION_NOT_APPLICABLE');
  });
  it('readback detects overwritten owner input independently of planned value', () => {
    const p = prepared([TARGET], SEP, 1, {}, { [`2026-09-20|${SKU}`]: 2 });
    const read = (r: number, c: number) => c === 88 ? '' : p.read(r, c);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read, mode: 'READBACK' }).map((i) => i.code)).toContain('BLOGGERS_OVERWRITTEN');
  });
  it('independent monetary readback rejects the historical false +924.62 result', () => {
    const p = prepared();
    const effectiveRead = (r: number, c: number) => r === 626 && c === 97 ? 924.62 : p.read(r, c);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'READBACK',
      effectiveRead, effectiveThrough: p.lcd }).map((i) => i.code)).toContain('SOURCE_ECONOMICS_MISMATCH');
  });
  it('correct target source-based monetary readback passes', () => {
    const p = prepared();
    const effectiveRead = (r: number, c: number) => r === 626 && c === 97 ? 19.57
      : r === 626 && c === 110 ? 356.21 : p.read(r, c);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'READBACK',
      effectiveRead, effectiveThrough: p.lcd })).toEqual([]);
  });
  it.each([false, true])('actual readback bridge exposes source QA, corrupted=%s', async (corrupt) => {
    const p = prepared();
    const grid = (effective: boolean) => Array.from({ length: SEP.mtdRow - SEP.titleRow + 1 }, (_, i) =>
      Array.from({ length: SEP.ncols }, (_, j) => {
        const r = SEP.titleRow + i, c = j + 1;
        if (r === 626 && c === 97 && effective) return corrupt ? 924.62 : 19.57;
        if (r === 626 && c === 110 && effective) return 356.21;
        return p.read(r, c) ?? '';
      }));
    const sheets = { readValues: async () => [grid(true)], readFormulas: async () => grid(false) } as unknown as SheetsGateway;
    const checks = await readbackAndVerify({ sheets, sheetName: 'OZON_Юнит_2025', written: new Map(),
      sections: [{ ...layoutOf(SEP), blockCount: SEP.blocks.length }], tailFirst: SEP.ncols + 1,
      summaryUpTo: p.lcd, model: { sections: [p.sec], lcd: p.lcd } });
    const model = checks.find((c) => c.name === 'SOURCE_MODEL_QA')!;
    expect(model.pass).toBe(!corrupt);
    if (corrupt) expect(model.sample.join(' ')).toContain('SOURCE_ECONOMICS_MISMATCH');
  });
  it('PRE_COMMIT checks candidate inputs without requiring formulas hidden by the old LCD to evaluate', async () => {
    const p = prepared([TARGET], SEP, 1, {}, {}, {}, '2026-09-20');
    const grid = (effective: boolean) => Array.from({ length: SEP.mtdRow - SEP.titleRow + 1 }, (_, i) =>
      Array.from({ length: SEP.ncols }, (_, j) => {
        const value = p.read(SEP.titleRow + i, j + 1) ?? '';
        return effective && typeof value === 'string' && value.startsWith('=') ? '' : value;
      }));
    const sheets = { readValues: async () => [grid(true)], readFormulas: async () => grid(false) } as unknown as SheetsGateway;
    const checks = await readbackAndVerify({ sheets, sheetName: 'OZON_Юнит_2025', written: new Map(),
      sections: [{ ...layoutOf(SEP), blockCount: SEP.blocks.length }], tailFirst: SEP.ncols + 1,
      summaryUpTo: '2026-09-19', model: { sections: [p.sec], lcd: p.lcd } });
    expect(checks.find((c) => c.name === 'SOURCE_MODEL_QA')!.pass).toBe(true);
  });
  it('query exposes separate proven price and never assigns it to revenue or buyer_amt', () => {
    const sql = ozonMonthFactsSql({ project: 'project-x', from: '2026-09-20', to: '2026-09-20' });
    expect(sql).toContain('SUM(p.price_rub * p.quantity)');
    expect(sql).toContain('op.units = f.expected_realized_qty');
    expect(sql).toContain('f.seller_base_revenue_rub revenue');
    expect(sql).toContain('b.buyer_amt, b.seller_amt');
    expect(sql).not.toContain('cart_adds');
  });
});

type UnitEvidence = { posting_number: string; quantity: number; reference_unit_rub: number | null;
  finance_unit_rub?: number | null; documented_buyout_present?: boolean; documented_buyout_unit_rub?: number | null;
  status?: string };
function populationFact(units: UnitEvidence[]): OzonFactRow {
  const witness = units.map((u) => ({ marketplace_sku: '1991772098', status: 'delivered', finance_unit_rub: null,
    documented_buyout_present: false, documented_buyout_unit_rub: null, ...u,
    basis_source: u.documented_buyout_present ? 'DOCUMENTED_BUYOUT' : u.finance_unit_rub !== undefined
      && u.finance_unit_rub !== null ? 'ACTUAL_FINANCE' : 'REFERENCE' }));
  const actual = witness.filter((u) => u.basis_source !== 'REFERENCE'), provisional = witness.filter((u) => u.basis_source === 'REFERENCE');
  const qty = (us: typeof witness) => us.reduce((n, u) => n + u.quantity, 0);
  const actualBasis = actual.reduce((n, u) => n + (u.documented_buyout_present ? u.documented_buyout_unit_rub! : u.finance_unit_rub!) * u.quantity, 0);
  const referenceBasis = provisional.reduce((n, u) => n + (u.reference_unit_rub ?? 0) * u.quantity, 0);
  const covered = qty(provisional.filter((u) => u.reference_unit_rub !== null && u.reference_unit_rub > 0));
  const actualCommission = actual.reduce((n, u) => n + (u.documented_buyout_present ? 0 : u.finance_unit_rub! * .52 * u.quantity), 0);
  const unproven = qty(provisional.filter((u) => u.status === 'delivered'));
  const q = qty(witness), transit = qty(provisional.filter((u) => u.status !== 'delivered'));
  return { ...TARGET, gross_qty: q, realized_qty: q - transit, in_transit_qty: transit, expected_realized_qty: q,
    revenue: actualBasis, provisional_revenue_rub: actualBasis + (transit ? referenceBasis : 0),
    order_reference_price: witness.every((u) => u.reference_unit_rub !== null && u.reference_unit_rub > 0)
      ? witness.reduce((n, u) => n + u.reference_unit_rub! * u.quantity, 0) / q : null,
    order_reference_qty: q, order_reference_covered_qty: qty(witness.filter((u) => u.reference_unit_rub !== null && u.reference_unit_rub > 0)),
    operational_basis_version: 1, operational_expected_qty: q, operational_actual_qty: qty(actual),
    operational_provisional_qty: qty(provisional), operational_reference_covered_qty: covered,
    operational_actual_basis_rub: actualBasis, operational_reference_basis_rub: referenceBasis,
    operational_basis_rub: covered === qty(provisional) ? actualBasis + referenceBasis : null,
    operational_basis_units_json: JSON.stringify(witness),
    commission: actualCommission + referenceBasis * .52, commission_actual: actualCommission,
    commission_estimated_rub: referenceBasis * .52,
    commission_state: provisional.length ? 'ESTIMATED' : actualCommission ? 'ACTUAL' : 'NOT_APPLICABLE',
    commission_estimate_method: provisional.length ? 'COMMISSION_POLICY_TARIFF' : null,
    commission_missing_qty: qty(provisional), commission_not_applicable_qty: qty(actual.filter((u) => u.documented_buyout_present)),
    buyout_revenue_unproven_qty: unproven,
    economics_completeness: unproven ? 'PROVISIONAL_PARTIAL' : provisional.length ? 'PROVISIONAL_COMPLETE' : 'ACTUAL',
    logistics: q * 105, logistics_actual: q * 105, cogs_amt: q * 130.81, provisional_cogs_rub: q * 130.81 };
}

describe('Ozon completeness: actual requires the entire operational population to be actual', () => {
  const ordinary = { posting_number: 'actual', quantity: 1, reference_unit_rub: 1287, finance_unit_rub: 1287 };
  const provisional = { posting_number: 'transit', quantity: 1, reference_unit_rub: 1287, status: 'delivering' };
  const buyout = { posting_number: 'documented', quantity: 1, reference_unit_rub: 998,
    documented_buyout_present: true, documented_buyout_unit_rub: 558.88 };

  it.each([
    ['documented buyout only', [buyout], 'ACTUAL'],
    ['ordinary actual only', [ordinary], 'ACTUAL'],
    ['ordinary provisional with complete estimates', [provisional], 'PROVISIONAL_COMPLETE'],
    ['documented buyout and ordinary provisional', [buyout, provisional], 'PROVISIONAL_COMPLETE'],
    ['ordinary actual and ordinary provisional', [ordinary, provisional], 'PROVISIONAL_COMPLETE'],
    ['unproven delivered revenue', [{ ...provisional, status: 'delivered' }], 'PROVISIONAL_PARTIAL'],
  ] as const)('%s preserves economics and the allowed completeness state', (_name, units, state) => {
    const f = populationFact([...units]), p = prepared([f]);
    expect(f.economics_completeness).toBe(state);
    expect(ozonSourceCompletenessIssues([f])).toEqual([]);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
    if (state !== 'ACTUAL') expect(ozonSourceCompletenessIssues([{ ...f, economics_completeness: 'ACTUAL' }])
      .map((i) => i.code)).toContain('ACTUAL_WITH_PROVISIONAL_COMPONENTS');
  });

  it('exact review fixture rejects old ACTUAL in source/plan/readback QA; PROVISIONAL_COMPLETE passes', () => {
    const f: OzonFactRow = { ...populationFact([buyout, provisional]), logistics: 204, logistics_actual: 105,
      logistics_estimated_rub: 99, logistics_state: 'ESTIMATED' };
    const p = prepared([f]), old = prepared([{ ...f, economics_completeness: 'ACTUAL' }]);
    expect(f.operational_actual_qty).toBe(1); expect(f.operational_provisional_qty).toBe(1);
    expect(f.operational_basis_rub).toBeCloseTo(1845.88, 8);
    expect(f.revenue).toBe(558.88); expect(f.commission).toBe(669.24);
    expect(f.economics_completeness).toBe('PROVISIONAL_COMPLETE');
    expect(ozonSourceCompletenessIssues(old.sec.facts).map((i) => i.code)).toContain('ACTUAL_WITH_PROVISIONAL_COMPONENTS');
    for (const mode of ['PLAN', 'READBACK'] as const) {
      expect(verifyOzonModel({ sections: [old.sec], lcd: old.lcd, read: old.read, mode })
        .map((i) => i.code)).toContain('ACTUAL_WITH_PROVISIONAL_COMPONENTS');
      expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode })).toEqual([]);
    }
    // Only completeness/provenance changes: every calculator input and formula is preserved.
    expect(old.plan.values).toEqual(p.plan.values);
    expect(p.read(626, 101)).toBe(922.94);
    expect(evaluate(p.read, p.lcd, 'DF626')).toBeCloseTo(337.0512, 6);
    // Existing eight-decimal commission-rate rounding is unchanged.
    expect(evaluate(p.read, p.lcd, 'CS626')).toBeCloseTo(337.4624, 5);
  });

  it.each([
    ['provisional population', { operational_provisional_qty: 1 }, 'ACTUAL_WITH_PROVISIONAL_COMPONENTS'],
    ['in transit population', { in_transit_qty: 1 }, 'ACTUAL_WITH_PROVISIONAL_COMPONENTS'],
    ['estimated commission state', { commission_state: 'ESTIMATED' }, 'ACTUAL_WITH_PROVISIONAL_COMPONENTS'],
    ['estimated logistics state', { logistics_state: 'ESTIMATED' }, 'ACTUAL_WITH_PROVISIONAL_COMPONENTS'],
    ['estimated commission amount despite state', { commission_estimated_rub: 1 }, 'ACTUAL_WITH_PROVISIONAL_COMPONENTS'],
    ['estimated logistics amount despite state', { logistics_estimated_rub: 1 }, 'ACTUAL_WITH_PROVISIONAL_COMPONENTS'],
    ['missing actual COGS', { cogs_missing_qty: 1 }, 'ACTUAL_WITH_MISSING_MATERIAL_EVIDENCE'],
    ['missing provisional COGS', { provisional_cogs_missing_qty: 1 }, 'ACTUAL_WITH_MISSING_MATERIAL_EVIDENCE'],
    ['missing commission', { commission_missing_qty: 1 }, 'ACTUAL_WITH_MISSING_MATERIAL_EVIDENCE'],
    ['unknown commission', { commission_state: 'UNKNOWN' }, 'ACTUAL_WITH_MISSING_MATERIAL_EVIDENCE'],
    ['unknown logistics', { logistics_state: 'UNKNOWN' }, 'ACTUAL_WITH_MISSING_MATERIAL_EVIDENCE'],
  ] as const)('independent QA rejects false ACTUAL with %s', (_name, bad, code) => {
    const f = { ...populationFact([ordinary]), ...bad };
    expect(ozonSourceCompletenessIssues([f]).map((i) => i.code)).toContain(code);
  });

  it('historical stronger evidence removes the last provisional unit and allows ACTUAL', () => {
    const first = populationFact([buyout, provisional]);
    const later = populationFact([buyout, { ...provisional, status: 'delivered', finance_unit_rub: 1287 }]);
    expect(first.economics_completeness).toBe('PROVISIONAL_COMPLETE');
    expect(later.economics_completeness).toBe('ACTUAL');
    expect(later.operational_actual_qty).toBe(2); expect(later.operational_provisional_qty).toBe(0);
    expect(later.commission_estimated_rub).toBe(0); expect(later.commission_actual).toBe(669.24);
    expect(later.operational_basis_rub).toBe(first.operational_basis_rub);
    const p = prepared([later]);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
  });
});

describe('Ozon mixed SKU-day: disjoint stronger and provisional populations', () => {
  const a = (id: string, price: number, quantity = 1, reference = price): UnitEvidence =>
    ({ posting_number: id, quantity, reference_unit_rub: reference, finance_unit_rub: price });
  const p = (id: string, price: number | null, quantity = 1): UnitEvidence =>
    ({ posting_number: id, quantity, reference_unit_rub: price });
  it.each([
    ['100% actual, differing reference ignored', [a('A', 1287, 1, 2000)], 1287, 'ACTUAL'],
    ['100% structural provisional', [p('B', 1287)], 1287, 'PROVISIONAL_PARTIAL'],
    ['exact review mixed fixture', [a('A', 1287), p('B', 1287)], 1287, 'PROVISIONAL_PARTIAL'],
    ['multiple actual and multiple provisional', [a('A', 1100, 2, 900), a('B', 1400), p('C', 1300, 3), p('D', 1000)], 1214.285714, 'PROVISIONAL_PARTIAL'],
    ['different prices correctly quantity weighted', [a('A', 1500, 1, 9999), p('B', 1000, 2), p('C', 1300)], 1200, 'PROVISIONAL_PARTIAL'],
    ['ordinary in-transit reference', [{ ...p('B', 1287), status: 'delivering' }], 1287, 'PROVISIONAL_COMPLETE'],
  ] as const)('%s', (_name, units, price, state) => {
    const f = populationFact([...units]), r = prepared([f]);
    expect(r.read(626, 101)).toBe(price);
    expect(r.read(626, 104)).toBe(.52);
    expect(f.economics_completeness).toBe(state);
    expect(f.operational_actual_qty! + f.operational_provisional_qty!).toBe(f.expected_realized_qty);
    expect(f.operational_basis_rub).toBe(f.operational_actual_basis_rub! + f.operational_reference_basis_rub!);
    const unit = price - price * .52 - 105 - 130.81 - price * .02;
    expect(evaluate(r.read, r.lcd, 'DF626')).toBeCloseTo(unit, 8);
    expect(evaluate(r.read, r.lcd, 'CS626')).toBeCloseTo(f.expected_realized_qty! * unit - 336.64, 8);
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' })).toEqual([]);
    expect(f.revenue).toBe(f.operational_actual_basis_rub);
    expect(f.buyer_amt).toBeNull(); expect(r.read(626, 102)).toBe('');
  });
  it('exact counterexample is 356.21 per unit and 375.78 total; old behavior fails independent QA', () => {
    const f = populationFact([a('A', 1287), p('B', 1287)]), r = prepared([f]);
    expect(evaluate(r.read, r.lcd, 'DA626')).toBeCloseTo(617.76, 8);
    expect(evaluate(r.read, r.lcd, 'DF626')).toBeCloseTo(356.21, 8);
    expect(evaluate(r.read, r.lcd, 'CS626')).toBeCloseTo(375.78, 8);
    const old = (row: number, col: number) => row === 626 && col === 101 ? 643.5
      : row === 626 && col === 104 ? 1.04 : r.read(row, col);
    const oldEffective = (row: number, col: number) => row === 626 && col === 110 ? -274.42
      : row === 626 && col === 97 ? -885.48 : old(row, col);
    const codes = verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: old, mode: 'READBACK',
      effectiveRead: oldEffective, effectiveThrough: r.lcd }).map((i) => i.code);
    expect(codes).toContain('ORDER_PRICE_OMITTED_OR_CHANGED');
    expect(codes).toContain('COMMISSION_OMITTED_OR_CHANGED');
    expect(codes).toContain('SOURCE_ECONOMICS_MISMATCH');
    const effectiveRead = (row: number, col: number) => row === 626 && col === 110 ? 356.21
      : row === 626 && col === 97 ? 375.78 : r.read(row, col);
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'READBACK',
      effectiveRead, effectiveThrough: r.lcd })).toEqual([]);
  });
  it('documented buyout proceeds replace reference and finance for that unit only', () => {
    const f = populationFact([{ ...a('doc', 1287, 1, 998), documented_buyout_present: true, documented_buyout_unit_rub: 558.88 }, a('sale', 1287)]);
    const r = prepared([f]);
    expect(r.read(626, 101)).toBe(922.94);
    expect(f.commission).toBe(669.24);
    expect(f.operational_actual_basis_rub).toBe(1845.88);
    expect(f.operational_provisional_qty).toBe(0);
    expect(f.commission_not_applicable_qty).toBe(1);
    expect(r.read(626, 104)).toBe(Math.round(669.24 / 1845.88 * 1e8) / 1e8);
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' })).toEqual([]);
  });
  it('later actual evidence replaces remaining reference population and its estimate', () => {
    const first = prepared([populationFact([a('A', 1287), p('B', 1287)])]);
    const f = populationFact([a('A', 1287), a('B', 1200, 1, 1287)]), later = prepared([f]);
    expect(first.read(626, 101)).toBe(1287);
    expect(later.read(626, 101)).toBe(1243.5);
    expect(f.operational_actual_qty).toBe(2); expect(f.operational_provisional_qty).toBe(0);
    expect(f.operational_basis_rub).toBe(2487);
    expect(f.commission_estimated_rub).toBe(0);
    expect(later.comp.provenance).toEqual([]);
    expect(f.economics_completeness).toBe('ACTUAL');
    expect(verifyOzonModel({ sections: [later.sec], lcd: later.lcd, read: later.read, mode: 'PLAN' })).toEqual([]);
  });
  it.each([null, 0, -1])('incomplete/invalid provisional reference %s fails closed despite positive actual revenue', (price) => {
    const f = populationFact([a('A', 1287), p('B', price)]), r = prepared([f]);
    expect(f.revenue).toBe(1287);
    expect(r.read(626, 101)).toBe(''); expect(r.read(626, 104)).toBe('');
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' }).map((i) => i.code))
      .toContain('OPERATIONAL_REFERENCE_INCOMPLETE');
  });
  it('100% actual does not require reference coverage for already covered units', () => {
    const f = populationFact([{ ...a('A', 1287), reference_unit_rub: null }]), r = prepared([f]);
    expect(r.read(626, 101)).toBe(1287);
    expect(f.order_reference_price).toBeNull();
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' })).toEqual([]);
  });
  it.each([
    [a('A', 1287.5), p('B', 1300.25)],
    [{ ...a('doc', 1287, 1, 998), documented_buyout_present: true, documented_buyout_unit_rub: 558.88 }, p('B', 1287)],
  ])('independent QA accepts BigQuery NUMERIC decimal-string witness amounts', (...units) => {
    const f = populationFact(units);
    const witness = JSON.parse(f.operational_basis_units_json!);
    for (const u of witness) for (const field of ['reference_unit_rub', 'finance_unit_rub', 'documented_buyout_unit_rub']) {
      if (typeof u[field] === 'number') u[field] = String(u[field]);
    }
    f.operational_basis_units_json = JSON.stringify(witness);
    const r = prepared([f]);
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' })).toEqual([]);
  });
  it.each(['', 'not-a-price', 'Infinity', false, {}])('invalid witness reference %s fails independent coverage QA', (value) => {
    const f = populationFact([a('A', 1287), p('B', 1287)]);
    const witness = JSON.parse(f.operational_basis_units_json!);
    witness[1].reference_unit_rub = value;
    f.operational_basis_units_json = JSON.stringify(witness);
    const r = prepared([f]);
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' }).map((i) => i.code))
      .toContain('OPERATIONAL_REFERENCE_INCOMPLETE');
  });
  it.each([
    ['OPERATIONAL_ACTUAL_QTY_MISMATCH', { operational_actual_qty: 0 }],
    ['OPERATIONAL_PROVISIONAL_QTY_MISMATCH', { operational_provisional_qty: 0 }],
    ['OPERATIONAL_EXPECTED_QTY_MISMATCH', { operational_expected_qty: 1 }],
    ['OPERATIONAL_EXPECTED_QTY_MISMATCH', { expected_realized_qty: 1 }],
    ['OPERATIONAL_REFERENCE_COVERAGE_MISMATCH', { operational_reference_covered_qty: 0 }],
    ['OPERATIONAL_ACTUAL_BASIS_MISMATCH', { operational_actual_basis_rub: 2000 }],
    ['OPERATIONAL_REFERENCE_BASIS_MISMATCH', { operational_reference_basis_rub: 0 }],
    ['OPERATIONAL_ECONOMIC_BASIS_MISMATCH', { operational_basis_rub: 1287 }],
  ] as const)('independent source population invariant detects %s', (code, change) => {
    const r = prepared([{ ...populationFact([a('A', 1287), p('B', 1287)]), ...change }]);
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' }).map((i) => i.code)).toContain(code);
  });
  it('independent witness reconciliation rejects overlapping posting/SKU populations', () => {
    const r = prepared([populationFact([a('same', 1287), p('same', 1287)])]);
    expect(verifyOzonModel({ sections: [r.sec], lcd: r.lcd, read: r.read, mode: 'PLAN' }).map((i) => i.code))
      .toContain('OPERATIONAL_POPULATION_OVERLAP');
  });
});
