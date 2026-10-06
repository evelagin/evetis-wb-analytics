import { describe, expect, it } from 'vitest';
import { ozonBlockDayFormulas, ozonBlockMtdFormulas, ozonSummaryDrrDayFormula, ozonSummaryDrrMtdFormula } from '../src/loaders/unitka/ozon/formulas.js';
import { OZON_OFFSET as OFF } from '../src/loaders/unitka/ozon/offsets.js';
import { OZON_SUMMARY } from '../src/loaders/unitka/ozon/contract.js';
import { composeMonth, ozonMonthSpec, serialOf, type OzonFactRow } from '../src/loaders/unitka/ozon/month.js';
import {
  buildOzonPlan, sectionFormulas, ozonDrrBases, drrShareNote,
  type OzonSectionInput, type SppDayEvidence, type SppEstimate,
} from '../src/loaders/unitka/ozon/monthplan.js';
import { layoutOf } from '../src/loaders/unitka/ozon/requests.js';
import { ozonWrittenReader } from '../src/loaders/unitka/ozon/model_qa.js';
import { ozonSppEstimateSql } from '../src/loaders/unitka/ozon/bq.js';
import { toLocaleFormula } from '../src/loaders/unitka/formulas.js';

/**
 * Phase 6 (2026-10-06): ДРР с провенансом. Знаменатель — фактическая цена покупателя там, где она
 * доказана, и ОЦЕНЁННАЯ (СПП E1m5) там, где финансовой пары ещё нет. Тесты исполняют СГЕНЕРИРОВАННЫЕ
 * формулы небольшим векторным интерпретатором (FILTER/MOD/COLUMN/SUMPRODUCT/SUMIF) — не пересказ формул.
 */

/* ── интерпретатор подмножества Google Sheets, в котором собраны формулы Ozon (локаль ru_RU) ── */
class Err { constructor(readonly code: string) {} }
type S = number | string | boolean | Err;
type M = S[][];
type V = S | M;
type Ast = { k: 'v'; v: S } | { k: 'ref'; t: string } | { k: 'name'; t: string }
  | { k: 'fn'; f: string; a: Ast[] } | { k: 'op'; op: string; a: Ast; b: Ast } | { k: 'neg'; a: Ast } | { k: 'pct'; a: Ast };

const colNum = (s: string): number => [...s].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
const isM = (v: V): v is M => Array.isArray(v);
const num = (v: S): number | Err => v instanceof Err ? v : typeof v === 'number' ? v : typeof v === 'boolean' ? (v ? 1 : 0)
  : v === '' ? 0 : Number.isFinite(Number(v)) ? Number(v) : new Err('VALUE');
function zip(a: V, b: V, f: (x: S, y: S) => S): V {
  if (!isM(a) && !isM(b)) return f(a, b);
  const A = isM(a) ? a : [[a]], B = isM(b) ? b : [[b]];
  const R = Math.max(A.length, B.length), C = Math.max(A[0]!.length, B[0]!.length);
  const at = (X: M, r: number, c: number): S => X[X.length === 1 ? 0 : r]![X[0]!.length === 1 ? 0 : c]!;
  return Array.from({ length: R }, (_, r) => Array.from({ length: C }, (_, c) => f(at(A, r, c), at(B, r, c))));
}
const map = (a: V, f: (x: S) => S): V => isM(a) ? a.map((r) => r.map(f)) : f(a);
const cells = (a: V): S[] => isM(a) ? a.flat() : [a];
const truthy = (v: S): boolean => typeof v === 'boolean' ? v : typeof v === 'number' ? v !== 0 : false;
function cmp(op: string, x: S, y: S): S {
  if (x instanceof Err) return x; if (y instanceof Err) return y;
  const nx = typeof x === 'number' ? x : x === '' && typeof y === 'number' ? 0 : x;
  const ny = typeof y === 'number' ? y : y === '' && typeof x === 'number' ? 0 : y;
  if (op === '=') return x === y; if (op === '<>') return x !== y;
  const lt = typeof nx === typeof ny ? nx < ny : typeof nx === 'number';     // число < текст
  const eq = nx === ny;
  return op === '<' ? lt : op === '<=' ? lt || eq : op === '>' ? !lt && !eq : !lt;
}

function parse(f: string): Ast {
  const t = f.slice(1).match(/"[^"]*"|\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?|[A-Z_][A-Z_0-9]*|\d+(?:,\d+)?|<>|<=|>=|[();+\-*/%=<>&]/g)!;
  let i = 0;
  const primary = (): Ast => {
    const v = t[i++]!;
    let a: Ast;
    if (v === '(') { a = concat(); if (t[i++] !== ')') throw Error('close'); }
    else if (v === '-') return { k: 'neg', a: primary() };
    else if (v.startsWith('"')) a = { k: 'v', v: v.slice(1, -1) };
    else if (/^\d/.test(v)) a = { k: 'v', v: Number(v.replace(',', '.')) };
    else if (t[i] === '(') {
      i++; const args: Ast[] = [];
      if (t[i] !== ')') do { args.push(concat()); } while (t[i] === ';' && ++i > 0);
      if (t[i++] !== ')') throw Error(`fn close ${v}`);
      a = { k: 'fn', f: v, a: args };
    } else if (/^\$?[A-Z]+\$?\d+/.test(v)) a = { k: 'ref', t: v.replaceAll('$', '') };
    else a = { k: 'name', t: v };
    while (t[i] === '%') { i++; a = { k: 'pct', a }; }
    return a;
  };
  const bin = (next: () => Ast, ops: string[]) => (): Ast => {
    let a = next(); while (ops.includes(t[i]!)) { const op = t[i++]!; a = { k: 'op', op, a, b: next() }; } return a;
  };
  const mul = bin(primary, ['*', '/']); const add = bin(mul, ['+', '-']);
  const concat = bin(bin(add, ['&']), ['=', '<>', '<', '>', '<=', '>=']);
  const a = concat(); if (i !== t.length) throw Error(`лишние токены в ${f}`); return a;
}

function sheetEval(read: (r: number, c: number) => unknown, lcd: string) {
  const cache = new Map<string, S>();
  const cell = (r: number, c: number): S => {
    const k = `${r}:${c}`; if (cache.has(k)) return cache.get(k)!;
    const v = read(r, c);
    const out: S = typeof v === 'string' && v.startsWith('=') ? (() => { const x = ev(parse(v)); return isM(x) ? new Err('ARR') : x; })()
      : typeof v === 'number' || typeof v === 'string' ? v : '';
    cache.set(k, out); return out;
  };
  const rng = (t: string): M => {
    const [a, b] = t.split(':') as [string, string];
    const ma = /^([A-Z]+)(\d+)$/.exec(a)!, mb = /^([A-Z]+)(\d+)$/.exec(b)!;
    const out: M = [];
    for (let r = Number(ma[2]); r <= Number(mb[2]); r++) {
      const row: S[] = []; for (let c = colNum(ma[1]!); c <= colNum(mb[1]!); c++) row.push(cell(r, c)); out.push(row);
    }
    return out;
  };
  function ev(a: Ast): V {
    switch (a.k) {
      case 'v': return a.v;
      case 'name': if (a.t === 'OZON_LAST_CLOSED_DATE') return serialOf(lcd); throw Error(a.t);
      case 'ref': { if (a.t.includes(':')) return rng(a.t); const m = /^([A-Z]+)(\d+)$/.exec(a.t)!; return cell(Number(m[2]), colNum(m[1]!)); }
      case 'neg': return map(ev(a.a), (x) => { const n = num(x); return n instanceof Err ? n : -n; });
      case 'pct': return map(ev(a.a), (x) => { const n = num(x); return n instanceof Err ? n : n / 100; });
      case 'op': {
        const x = ev(a.a), y = ev(a.b);
        if (['=', '<>', '<', '>', '<=', '>='].includes(a.op)) return zip(x, y, (p, q) => cmp(a.op, p, q));
        if (a.op === '&') return zip(x, y, (p, q) => `${p}${q}`);
        return zip(x, y, (p, q) => {
          const n = num(p), m = num(q);
          if (n instanceof Err) return n; if (m instanceof Err) return m;
          return a.op === '+' ? n + m : a.op === '-' ? n - m : a.op === '*' ? n * m : m === 0 ? new Err('DIV0') : n / m;
        });
      }
      case 'fn': {
        const A = a.a;
        if (a.f === 'IF') { const c = ev(A[0]!); if (isM(c) || c instanceof Err) return isM(c) ? new Err('ARR') : c; return truthy(c) ? ev(A[1]!) : ev(A[2]!); }
        if (a.f === 'IFERROR') { const x = ev(A[0]!); const fb = () => ev(A[1]!) as S; return isM(x) ? map(x, (s) => s instanceof Err ? fb() : s) : x instanceof Err ? fb() : x; }
        if (a.f === 'COLUMN') {
          const r = (A[0] as { t: string }).t; const [p, q] = r.split(':');
          const c1 = colNum(/^([A-Z]+)/.exec(p!)![1]!), c2 = q ? colNum(/^([A-Z]+)/.exec(q)![1]!) : c1;
          return q ? [Array.from({ length: c2 - c1 + 1 }, (_, k) => c1 + k)] : c1;
        }
        const x = A.map(ev);
        switch (a.f) {
          case 'N': { const v = x[0]!; return isM(v) ? new Err('ARR') : typeof v === 'number' ? v : 0; }
          case 'OR': return x.some((v) => cells(v).some(truthy));
          case 'MOD': return zip(x[0]!, x[1]!, (p, q) => { const n = num(p), m = num(q); return n instanceof Err ? n : m instanceof Err ? m : ((n % m) + m) % m; });
          case 'SUMPRODUCT': { let s = 0; for (const v of cells(x[0]!)) { const n = num(v); if (n instanceof Err) return n; s += n; } return s; }
          case 'SUM': { let s = 0; for (const v of x.flatMap(cells)) { if (v instanceof Err) return v; if (typeof v === 'number') s += v; } return s; }
          case 'COUNT': return x.flatMap(cells).filter((v) => typeof v === 'number').length;
          case 'FILTER': {
            const src = x[0] as M, cond = x[1]!; if (!isM(cond)) throw Error('FILTER');
            let out: M;
            if (cond.length === 1 && src.length >= 1 && cond[0]!.length === src[0]!.length) out = src.map((r) => r.filter((_, c) => truthy(cond[0]![c]!)));
            else out = src.filter((_, r) => truthy(cond[r]![0]!));
            return out.length && out[0]!.length ? out : new Err('N/A');
          }
          case 'SUMIF': {
            const crit = String(x[1]); const m = /^(<=|>=|<>|<|>|=)?(.*)$/.exec(crit)!;
            const op = m[1] ?? '=', val = Number(m[2]);
            const keys = cells(x[0]!), vals = cells(x[2]!); let s = 0;
            keys.forEach((k, j) => { if (typeof k === 'number' && truthy(cmp(op, k, val)) && typeof vals[j] === 'number') s += vals[j] as number; });
            return s;
          }
          default: throw Error(`fn ${a.f}`);
        }
      }
    }
  }
  return (ref: string): S => { const m = /^([A-Z]+)(\d+)$/.exec(ref)!; return cell(Number(m[2]), colNum(m[1]!)); };
}

const LCDN = 'OZON_LAST_CLOSED_DATE';
const loc = (f: string) => toLocaleFormula(f, 'SEMICOLON');

/* ── дневная ДРР SKU ─────────────────────────────────────────────────────────────── */
describe('Phase 6: дневная ДРР SKU', () => {
  const ROW = 607;
  const day = (est?: string) => loc(ozonBlockDayFormulas({ start: 12, cogsTerm: '0', ...(est ? { sppEstimateTerm: est } : {}) }, ROW, LCDN).get(OFF.drr)!);
  // W — реклама, P — заказы, M — блогеры, Z — цена, AB — цена с СПП (блок в L)
  const run = (f: string, v: Record<string, number | string>) =>
    sheetEval((r, c) => {
      const k = `${String.fromCharCode(64 + Math.floor((c - 1) / 26)).replace('@', '')}${String.fromCharCode(65 + ((c - 1) % 26))}${r}`;
      return k === `Y${ROW}` ? f : v[k] ?? (c === 12 ? serialOf('2026-09-01') : '');
    }, '2026-09-30')(`Y${ROW}`);

  it('фактическая СПП есть → формула побайтно прежняя', () => {
    expect(day()).toBe(loc('=IF($L607>OZON_LAST_CLOSED_DATE,"",IFERROR(W607/((P607-N(M607))*AB607),""))'));
  });
  it('СПП нет, есть оценка → литерал оценки в ru_RU, факт старше оценки', () => {
    const f = day('61.41965');
    expect(f).toBe('=IF($L607>OZON_LAST_CLOSED_DATE;"";IFERROR(W607/((P607-N(M607))*IF(AB607<>"";AB607;Z607-Z607*61,41965%));""))');
    // знаменатель — цена покупателя ПО ОЦЕНКЕ, не цена продавца: 150 / (2 × 1000 × 0,4)
    expect(run(f, { W607: 150, P607: 3, M607: 1, Z607: 1000, AB607: '' }) as number).toBeCloseTo(150 / (2 * 1000 * (1 - 0.6141965)), 10);
    // пришла фактическая «цена с СПП» — берётся она, оценка уже не участвует
    expect(run(f, { W607: 150, P607: 3, M607: 1, Z607: 1000, AB607: 420 }) as number).toBeCloseTo(150 / (2 * 420), 10);
  });
  it('нет оценки и нет СПП → пусто; нулевой знаменатель → пусто', () => {
    expect(run(day(), { W607: 150, P607: 3, Z607: 1000, AB607: '' })).toBe('');
    expect(run(day('60'), { W607: 150, P607: 1, M607: 1, Z607: 1000, AB607: '' })).toBe('');
  });
});

/* ── секция из двух блоков: сутки до окна, окно, факт + оценка ───────────────────── */
const SPEC = ozonMonthSpec(2026, 9, 570, 31, ['A', 'B']);
const LCD = '2026-09-05', FROM = 3;
const R = (d: number) => SPEC.firstRow + d - 1;
const iso = (d: number) => `2026-09-${String(d).padStart(2, '0')}`;
const fact = (d: number, offer: string, q: number, revenue: number, ads: number, buyer?: number): OzonFactRow => ({
  d: iso(d), offer_id: offer, gross_qty: q, cancelled_qty: 0, realized_qty: q, expected_realized_qty: q,
  revenue, commission: 0, logistics: 0, impr: 100, clicks: 5, ads_spend: ads,
  buyer_amt: buyer ?? null, seller_amt: buyer === undefined ? null : revenue });
// окно: A3 факт (СПП 60 %), A4 оценка 55 % (SKU), A5 без оценки; B3 оценка 50 % (магазин), B4 факт (СПП 60 %)
const FACTS: OzonFactRow[] = [
  fact(3, 'A', 2, 2000, 100, 800), fact(4, 'A', 3, 3300, 150), fact(5, 'A', 1, 900, 10),
  fact(3, 'B', 1, 500, 50), fact(4, 'B', 2, 1200, 30, 480),
];
const ESTIMATE: Record<string, SppEstimate> = {
  [`${iso(2)}|A`]: { pct: 60, level: 'SKU' }, [`${iso(4)}|A`]: { pct: 55, level: 'SKU' },
  [`${iso(3)}|B`]: { pct: 50, level: 'SHOP' }, [`${iso(1)}|A`]: { pct: 99, level: 'SKU' },   // у A1 факт: оценка не используется
};
// сутки до окна — из живого листа: A1 факт (цена с СПП 400), A2 без СПП
const EVIDENCE: Record<string, SppDayEvidence> = {
  [`${iso(1)}|A`]: { units: 2, priced: true, spp: true, orders: 2, price: 1000, buyer: 400 },
  [`${iso(2)}|A`]: { units: 1, priced: true, spp: false, orders: 1, price: 1000 },
};
const PRE_ADS: Record<string, number> = { [`1|A`]: 40, [`2|A`]: 20 };
const BLOGGERS: Record<string, number> = { [`${iso(4)}|A`]: 1 };

function section(estimate: Record<string, SppEstimate> = ESTIMATE, facts = FACTS) {
  const sheetInputs = { bloggers: BLOGGERS, externalAds: {} };
  const comp = composeMonth(SPEC, facts, {}, LCD, FROM, {}, sheetInputs);
  const sec: OzonSectionInput = { spec: SPEC, facts, stock: {}, cart: {}, sheetInputs, fromDay: FROM,
    sppEvidence: EVIDENCE, sppEstimate: estimate,
    formulas: sectionFormulas(SPEC, comp, 'SEMICOLON', LCDN, { lcd: LCD, from: FROM, estimate, evidence: EVIDENCE, bloggers: BLOGGERS }),
    refTitle: [], refHeader: [], refAnchor: {} };
  const plan = buildOzonPlan({ sheetId: 1, sheetName: 'OZON_Юнит_2025', sections: [sec], allSections: [layoutOf(SPEC)],
    grid: { current: { rows: 800, columns: 600 },
      growth: { widenBlockAt: [], insertColumnsBefore: 590, insertColumnCount: 0, appendColumnCount: 0, appendRowCount: 0 } },
    lcd: LCD, lcdMirror: null, lcdRef: '$VB$2', blocks: 2 });
  const written = ozonWrittenReader(plan.values);
  // лист = записанное планом + то, что уже лежит в листе в сутках до окна
  const read = (r: number, c: number): unknown => {
    const w = written(r, c); if (w !== undefined) return w;
    const d = r - SPEC.firstRow + 1; if (d < 1 || d > SPEC.days) return '';
    if (c === 2) return serialOf(iso(d));
    for (const o of SPEC.blocks) {
      const s = SPEC.anchor[o]!, e = EVIDENCE[`${iso(d)}|${o}`];
      if (c === s + OFF.date) return serialOf(iso(d));
      if (c === s + OFF.bloggers) return BLOGGERS[`${iso(d)}|${o}`] ?? '';   // ручной ввод: план его не пишет
      if (!e) continue;
      if (c === s + OFF.orders) return e.orders; if (c === s + OFF.price) return e.price;
      if (c === s + OFF.priceSpp) return e.buyer ?? ''; if (c === s + OFF.adsIn) return PRE_ADS[`${d}|${o}`] ?? '';
    }
    if (c === 10) return Object.entries(PRE_ADS).filter(([k]) => k.startsWith(`${d}|`)).reduce((n, [, v]) => n + v, 0) || '';
    return '';
  };
  return { plan, sec, comp, read, ev: sheetEval(read, LCD) };
}
const A1 = (c: number, r: number) => `${c > 26 ? String.fromCharCode(64 + Math.floor((c - 1) / 26)) : ''}${String.fromCharCode(65 + ((c - 1) % 26))}${r}`;
const a = SPEC.anchor['A']!, b = SPEC.anchor['B']!;

describe('Phase 6: базы ДРР секции', () => {
  it('оценка — только без факта СПП, с ценой и оценкой; сутки до окна — из живого листа', () => {
    const { comp } = section();
    const x = ozonDrrBases(SPEC, comp, LCD, FROM, ESTIMATE, EVIDENCE, BLOGGERS);
    const est = Object.fromEntries(x.estimated.map((e) => [`${e.day}|${e.offerId}`, e.basis]));
    expect(est['2|A']).toBeCloseTo(1 * 1000 * 0.4, 8);
    expect(est['4|A']).toBeCloseTo((3 - 1) * 1100 * 0.45, 8);       // блогеры вычитаются, как в формуле
    expect(est['3|B']).toBeCloseTo(1 * 500 * 0.5, 8);
    expect(est['1|A']).toBeUndefined();                              // факт старше оценки
    expect(est['5|A']).toBeUndefined();                              // оценки нет → базы нет, а не цена продавца
    expect(x.actual.map((e) => `${e.day}|${e.offerId}`).sort()).toEqual(['1|A', '3|A', '4|B']);
  });
});

describe('Phase 6: ДРР SKU и магазина по сгенерированным формулам', () => {
  const { ev } = section();
  const estA = 400 + 990, actA = 2 * 400 + 2 * 400, estB = 250, actB = 2 * 240;
  it('дневная ДРР SKU: факт без изменений, оценка — по оценённой цене покупателя', () => {
    expect(ev(A1(a + OFF.drr, R(3))) as number).toBeCloseTo(100 / (2 * 400), 10);
    expect(ev(A1(a + OFF.drr, R(4))) as number).toBeCloseTo(150 / 990, 10);
    expect(ev(A1(a + OFF.drr, R(5)))).toBe('');                       // нет ни факта, ни оценки
    expect(ev(A1(b + OFF.drr, R(3))) as number).toBeCloseTo(50 / 250, 10);
  });
  it('MTD SKU: Σ реклама / (Σ фактическая база + Σ оценённая), сутки до окна включены', () => {
    expect(ev(A1(a + OFF.drr, SPEC.mtdRow)) as number).toBeCloseTo((40 + 20 + 100 + 150 + 10) / (actA + estA), 10);
    expect(ev(A1(b + OFF.drr, SPEC.mtdRow)) as number).toBeCloseTo((50 + 30) / (actB + estB), 10);
  });
  it('K дня = Σ J / Σ баз блоков (агрегат, не среднее процентов SKU)', () => {
    const k4 = ev(A1(OZON_SUMMARY.drr, R(4))) as number;
    expect(k4).toBeCloseTo((150 + 30) / (990 + 2 * 240), 10);
    const avg = ((ev(A1(a + OFF.drr, R(4))) as number) + (ev(A1(b + OFF.drr, R(4))) as number)) / 2;
    expect(Math.abs(k4 - avg)).toBeGreaterThan(1e-3);
    expect(ev(A1(OZON_SUMMARY.drr, R(3))) as number).toBeCloseTo((100 + 50) / (800 + 250), 10);
    // сутки ДО окна тоже получили K: база из листа + оценка суток
    expect(ev(A1(OZON_SUMMARY.drr, R(2))) as number).toBeCloseTo(20 / 400, 10);
    expect(ev(A1(OZON_SUMMARY.drr, R(1))) as number).toBeCloseTo(40 / 800, 10);
    expect(ev(A1(OZON_SUMMARY.drr, R(6)))).toBe('');                   // после LCD пусто
  });
  it('K месяца = Σ рекламы магазина / Σ всех баз', () => {
    expect(ev(A1(OZON_SUMMARY.drr, SPEC.mtdRow)) as number)
      .toBeCloseTo((40 + 20 + 100 + 150 + 10 + 50 + 30) / (actA + estA + actB + estB), 10);
  });
});

describe('Phase 6: без оценки формулы прежние', () => {
  it('нет ни одной оценённой базы → ДРР блоков побайтно как до Phase 6', () => {
    const { sec } = section({});
    const geom = { firstDailyRow: SPEC.firstRow, lastDailyRow: SPEC.lastRow, mtdRow: SPEC.mtdRow };
    expect(sec.formulas.mtd[`${SPEC.mtdRow}:${a + OFF.drr}`]).toBe(loc(ozonBlockMtdFormulas(a, geom, LCDN).get(OFF.drr)!));
    expect(sec.formulas.day[`${R(4)}:${a + OFF.drr}`]).toBe(loc(ozonBlockDayFormulas({ start: a, cogsTerm: '0' }, R(4), LCDN).get(OFF.drr)!));
    expect(sec.formulas.day[`${R(4)}:${OZON_SUMMARY.drr}`]).toBe(loc(ozonSummaryDrrDayFormula(R(4), 2, LCDN)));
    expect(sec.formulas.mtd[`${SPEC.mtdRow}:${OZON_SUMMARY.drr}`]).toBe(loc(ozonSummaryDrrMtdFormula(geom, 2)));
    expect(sec.formulas.day[`${R(4)}:${OZON_SUMMARY.drr}`]).not.toMatch(/\)\+\d/);
  });
  it('форма K повторяет живую WB K740: FILTER по шагу блока и IFERROR(…;0) внутри SUMPRODUCT', () => {
    expect(ozonSummaryDrrDayFormula(740, 2, LCDN, '123.45')).toBe(
      '=IF($B740>OZON_LAST_CLOSED_DATE,"",IFERROR(J740/(SUMPRODUCT(IFERROR(('
      + 'FILTER($P$740:$AO$740,MOD(COLUMN($P$740:$AO$740)-COLUMN($P$740),25)=0)'
      + '-FILTER($M$740:$AL$740,MOD(COLUMN($M$740:$AL$740)-COLUMN($M$740),25)=0))'
      + '*FILTER($AB$740:$BA$740,MOD(COLUMN($AB$740:$BA$740)-COLUMN($AB$740),25)=0),0))+123.45),""))');
  });
});

describe('Phase 6: заметки провенанса ДРР', () => {
  type Req = { repeatCell?: { range: { startRowIndex: number; endRowIndex: number; startColumnIndex: number }; cell: { note?: string } } };
  const notesAt = (plan: ReturnType<typeof section>['plan'], row: number, col: number): string[] =>
    (plan.presentation as Req[]).filter((q) => q.repeatCell?.cell.note !== undefined
      && q.repeatCell.range.startColumnIndex === col - 1
      && q.repeatCell.range.startRowIndex <= row - 1 && q.repeatCell.range.endRowIndex >= row).map((q) => q.repeatCell!.cell.note!);
  it('оценённые сутки получают ESTIMATED с уровнем; область сначала очищается', () => {
    const { plan } = section();
    expect(notesAt(plan, R(4), a + OFF.drr)).toEqual(['', expect.stringMatching(/^ESTIMATED: цена покупателя оценена по СПП 55\.0 % \(SKU 30 дн\)/)]);
    expect(notesAt(plan, R(3), b + OFF.drr)[1]).toMatch(/\(магазин 30 дн\)/);
    expect(notesAt(plan, R(3), a + OFF.drr)).toEqual(['']);            // факт — заметки нет
    expect(notesAt(plan, SPEC.mtdRow, a + OFF.drr)[1]).toBe(drrShareNote(400 + 990, 1600));
    expect(drrShareNote(400 + 990, 1600)).toMatch(/^ESTIMATED: 46\.5 % базы — оценка СПП/);
    expect(notesAt(plan, R(4), OZON_SUMMARY.drr)[1]).toBe(drrShareNote(990, 480));
    expect(notesAt(plan, SPEC.mtdRow, OZON_SUMMARY.drr)[1]).toMatch(/^ESTIMATED: /);
  });
  it('пришёл факт → заметка снимается (остаётся только очистка)', () => {
    const facts = FACTS.map((f) => f.d === iso(4) && f.offer_id === 'A' ? { ...f, buyer_amt: 1320, seller_amt: 3300 }
      : f.d === iso(3) && f.offer_id === 'B' ? { ...f, buyer_amt: 200, seller_amt: 500 } : f);
    const { plan } = section({ [`${iso(2)}|A`]: { pct: 60, level: 'SKU' } }, facts);
    expect(notesAt(plan, R(4), a + OFF.drr)).toEqual(['']);
    expect(notesAt(plan, R(4), OZON_SUMMARY.drr)).toEqual(['']);
    expect(notesAt(plan, SPEC.mtdRow, b + OFF.drr)).toEqual(['']);
    // A2 до окна всё ещё по оценке → итог A и K месяца помечены
    expect(notesAt(plan, SPEC.mtdRow, a + OFF.drr)[1]).toMatch(/^ESTIMATED: /);
  });
});

describe('Phase 6: SQL оценки СПП (E1m5)', () => {
  const sql = ozonSppEstimateSql({ project: 'p-1', from: '2026-09-01', to: '2026-10-05' });
  it('окно 30 суток до d−1, финансовая пара известна к d−1, ≥ 5 ед. SKU, иначе магазин', () => {
    expect(sql).toContain('BETWEEN DATE_SUB(days.d, INTERVAL 30 DAY) AND DATE_SUB(days.d, INTERVAL 1 DAY)');
    expect(sql).toContain('o.known <= DATE_SUB(days.d, INTERVAL 1 DAY)');
    expect(sql).toContain('MIN(event_date) known');
    expect(sql).toContain("p.status = 'delivered'");
    expect(sql).toContain('buyer_paid_price_rub IS NOT NULL');
    expect(sql).toMatch(/IF\(sw\.units >= 5, 'SKU', 'SHOP'\)/);
    expect(sql).toContain('sh.units >= 1');
    expect(sql).toContain("GENERATE_DATE_ARRAY(DATE '2026-09-01', DATE '2026-10-05')");
  });
  it('даты и проект валидируются', () => {
    expect(() => ozonSppEstimateSql({ project: 'p', from: '2026-9-1', to: '2026-10-05' })).toThrow();
    expect(() => ozonSppEstimateSql({ project: 'p;drop', from: '2026-09-01', to: '2026-10-05' })).toThrow();
  });
});
