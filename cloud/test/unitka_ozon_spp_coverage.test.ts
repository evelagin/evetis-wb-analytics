import { describe, expect, it } from 'vitest';
import { ozonBlockMtdFormulas } from '../src/loaders/unitka/ozon/formulas.js';
import { OZON_OFFSET as OFF } from '../src/loaders/unitka/ozon/offsets.js';
import { sppMonthCoverage, sppCoverageNote } from '../src/loaders/unitka/ozon/monthplan.js';
import { ozonMonthSpec, type OzonMonthComposition } from '../src/loaders/unitka/ozon/month.js';

/**
 * Аудит 2026-10-06: СПП месяца = 1 − Σ(ед.×цена с СПП)/Σ(ед.×цена) давала завышение вплоть до ложных 100 %,
 * потому что сутки без финансовой пары добавляли цену в знаменатель и ноль в числитель (26 ячеек).
 * Тест исполняет СГЕНЕРИРОВАННУЮ формулу строки MTD маленьким векторным интерпретатором — не пересказ формулы.
 */

const FIRST = 607, LAST = 609, MTD = 610, START = 12; // блок начинается в L
const LCD = 46300; // серийная дата «закрыто»

function colNum(s: string): number { return [...s].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0); }

/** Векторный интерпретатор ровно того подмножества, которое генерирует ozonBlockMtdFormulas. */
function evalMtd(formula: string, cell: (row: number, col: number) => number | string): number | string {
  const toks = formula.slice(1).match(/"[^"]*"|\$?[A-Z]+\$?\d+:\$?[A-Z]+\$?\d+|\$?[A-Z]+\$?\d+|[A-Z_][A-Z_0-9]*|\d+(?:\.\d+)?|<>|<=|>=|[(),;*+\-/<>=&]/g)!;
  let i = 0;
  type V = number | string | Array<number | string>;
  const range = (t: string): Array<number | string> => {
    const [a, b] = t.replaceAll('$', '').split(':') as [string, string];
    const ma = /^([A-Z]+)(\d+)$/.exec(a)!, mb = /^([A-Z]+)(\d+)$/.exec(b)!;
    const c = colNum(ma[1]!); const out: Array<number | string> = [];
    for (let r = Number(ma[2]); r <= Number(mb[2]); r++) out.push(cell(r, c));
    if (colNum(mb[1]!) !== c) throw Error('2D range');
    return out;
  };
  const num = (v: number | string): number => (typeof v === 'number' ? v : 0);
  const zip = (x: V, y: V, f: (a: number | string, b: number | string) => number | string): V => {
    if (Array.isArray(x) || Array.isArray(y)) {
      const n = Array.isArray(x) ? x.length : (y as unknown[]).length;
      return Array.from({ length: n }, (_, k) => f(Array.isArray(x) ? x[k]! : x, Array.isArray(y) ? y[k]! : y));
    }
    return f(x, y);
  };
  const prim = (): V => {
    const t = toks[i++]!;
    if (t === '(') { const v = cmp(); if (toks[i++] !== ')') throw Error(')'); return v; }
    if (t.startsWith('"')) return t.slice(1, -1);
    if (/^\d/.test(t)) return Number(t);
    if (t.includes(':')) return range(t);
    if (t === 'OZON_LAST_CLOSED_DATE') return LCD;
    if (toks[i] === '(') {
      i++; const args: V[] = []; do { args.push(cmp()); } while ((toks[i] === ',' || toks[i] === ';') && ++i > 0);
      if (toks[i++] !== ')') throw Error('fn)');
      if (t === 'SUMPRODUCT') { const a = args[0]!; return Array.isArray(a) ? a.reduce<number>((s, v) => s + num(v), 0) : num(a); }
      throw Error(`fn ${t}`);
    }
    const m = /^\$?([A-Z]+)\$?(\d+)$/.exec(t)!; return cell(Number(m[2]), colNum(m[1]!));
  };
  const mul = (): V => { let a = prim(); while (toks[i] === '*' || toks[i] === '/') { const op = toks[i++]; const b = prim();
    a = zip(a, b, (x, y) => { if (op === '/') { if (num(y) === 0) throw Error('DIV0'); return num(x) / num(y); } return num(x) * num(y); }); } return a; };
  const add = (): V => { let a = mul(); while (toks[i] === '+' || toks[i] === '-') { const op = toks[i++]; const b = mul();
    a = zip(a, b, (x, y) => (op === '+' ? num(x) + num(y) : num(x) - num(y))); } return a; };
  const cmp = (): V => { let a = add(); while (['<=', '<>', '=', '<', '>', '>='].includes(toks[i]!)) { const op = toks[i++]; const b = add();
    a = zip(a, b, (x, y) => Number(op === '<=' ? num(x) <= num(y) : op === '<>' ? x !== y : op === '=' ? x === y
      : op === '<' ? num(x) < num(y) : op === '>' ? num(x) > num(y) : num(x) >= num(y))); } return a; };
  // =IFERROR(expr,"")
  if (toks[i] !== 'IFERROR') throw Error('IFERROR expected');
  i += 2;
  try { const v = cmp(); if (Array.isArray(v)) throw Error('vector'); return v; } catch { return ''; }
}

/** Лист из трёх суток: [заказы, отмены, цена, цена с СПП ("" если СПП нет)]. */
function sheet(days: Array<[number, number, number, number | string]>) {
  return (row: number, col: number): number | string => {
    const i = row - FIRST; const d = days[i];
    const off = col - START;
    if (off === OFF.date) return i >= 0 && i < days.length ? LCD - 5 + i : '';
    if (!d) return '';
    if (off === OFF.orders) return d[0]; if (off === OFF.cancels) return d[1];
    if (off === OFF.price) return d[2]; if (off === OFF.priceSpp) return d[3];
    return '';
  };
}
const SPP = ozonBlockMtdFormulas(START, { firstDailyRow: FIRST, lastDailyRow: LAST, mtdRow: MTD }, 'OZON_LAST_CLOSED_DATE').get(OFF.spp)!;

describe('СПП месяца Ozon: только по единицам с доказанной ценой покупателя', () => {
  it('частичное покрытие: значение по покрытой части (30 %), а не завышенные 53,3 %', () => {
    const v = evalMtd(SPP, sheet([[2, 0, 1000, 700], [1, 0, 1000, ''], [0, 0, 1000, '']]));
    expect(v).toBeCloseTo(30, 8);
  });
  it('нет покрытия: пусто, а не ложные 100 %', () => {
    expect(evalMtd(SPP, sheet([[2, 0, 1000, ''], [1, 0, 1000, ''], [0, 0, 0, '']]))).toBe('');
  });
  it('полное покрытие: взвешенная по единицам СПП (как раньше)', () => {
    const v = evalMtd(SPP, sheet([[2, 0, 1000, 700], [1, 0, 2000, 1000], [0, 0, 0, '']]));
    expect(v).toBeCloseTo((1 - (2 * 700 + 1000) / (2 * 1000 + 2000)) * 100, 8);
  });
  it('отменённые единицы не участвуют (вес = заказы − отмены)', () => {
    const v = evalMtd(SPP, sheet([[2, 2, 1000, ''], [1, 0, 1000, 600], [0, 0, 0, '']]));
    expect(v).toBeCloseTo(40, 8);
  });
});

describe('Заметка о покрытии СПП месяца', () => {
  const spec = ozonMonthSpec(2026, 9, 605, 30, ['A']);
  const comp = (cells: Record<string, object>): OzonMonthComposition => ({ cells, provenance: [], partialEconomics: [] } as unknown as OzonMonthComposition);
  const r = (day: number) => spec.firstRow + day - 1;
  it('частично: число единиц и доля; дни до окна берутся из живого листа', () => {
    const c = comp({ [`A|${r(20)}`]: { orders: 2, cancel: 0, price: 1000, spp: 30 }, [`A|${r(21)}`]: { orders: 1, cancel: 0, price: 1000 } });
    const live = { '2026-09-05|A': { units: 3, priced: true, spp: true }, '2026-09-06|A': { units: 2, priced: true, spp: false } };
    const cov = sppMonthCoverage(spec, c, '2026-09-30', 20, 'A', live);
    expect(cov).toEqual({ units: 8, covered: 5 });
    expect(sppCoverageNote(cov)).toMatch(/^ЧАСТИЧНО: СПП месяца рассчитана по 5 из 8 ед\. \(62\.5 %\)/);
  });
  it('полное покрытие → заметка снимается; нет единиц → снимается', () => {
    expect(sppCoverageNote({ units: 4, covered: 4 })).toBe('');
    expect(sppCoverageNote({ units: 0, covered: 0 })).toBe('');
  });
  it('нет покрытия → объяснение пустой ячейки', () => {
    expect(sppCoverageNote({ units: 3, covered: 0 })).toMatch(/не рассчитана/);
  });
  it('сутки после LCD не считаются', () => {
    const c = comp({ [`A|${r(29)}`]: { orders: 1, cancel: 0, price: 1000 }, [`A|${r(30)}`]: { orders: 5, cancel: 0, price: 1000, spp: 10 } });
    expect(sppMonthCoverage(spec, c, '2026-09-29', 1, 'A')).toEqual({ units: 1, covered: 0 });
  });
});
