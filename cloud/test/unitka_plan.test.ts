import { describe, it, expect } from 'vitest';
import { buildPlan, preflight, toWriteRanges, type PlanInputs } from '../src/loaders/unitka/plan.js';
import { GRID, OFFSET, colA1, serialToIso, isoToSerial, findBlocks, factEqual, dayRow } from '../src/loaders/unitka/model.js';
import { unitkaSlot } from '../src/loaders/unitka/slot.js';
import { LoaderError } from '../src/errors.js';
import { snapshot, facts, logistics, commission, lcdRow, LCD, NM_IDS } from './unitka_fixture.js';

const inputs = (over: Partial<PlanInputs> = {}): PlanInputs => ({
  snapshot: snapshot(), lcd: lcdRow(), facts: facts(), logistics: logistics(), commission: commission(),
  minN: 10, maxLagDays: 2, ...over,
});

const codeOf = (fn: () => unknown): string => {
  try { fn(); } catch (e) { return e instanceof LoaderError ? e.code : 'NOT_LOADER_ERROR'; }
  return 'NO_THROW';
};

describe('model', () => {
  it('колонки и даты как в Apps Script', () => {
    expect(colA1(1)).toBe('A');
    expect(colA1(13)).toBe('M');
    expect(colA1(588)).toBe('VP');
    expect(colA1(600)).toBe('WB');
    expect(serialToIso(isoToSerial('2026-09-10'))).toBe('2026-09-10');
    expect(isoToSerial('1899-12-31')).toBe(1);
    expect(dayRow(0)).toBe(737);
    expect(dayRow(29)).toBe(766);
  });
  it('факт: пусто ≠ 0, допуск 0.005', () => {
    expect(factEqual('', null)).toBe(true);
    expect(factEqual(0, null)).toBe(false);
    expect(factEqual('', 0)).toBe(false);
    expect(factEqual(12.344, 12.34)).toBe(true);
    expect(factEqual(12.35, 12.34)).toBe(false);
  });
  it('блоки находятся по nmID в строке 735', () => {
    const b = findBlocks(snapshot().grid[0]!);
    expect(b).toHaveLength(24);
    expect(b[0]!.start).toBe(13);
    expect(b[23]!.start).toBe(13 + 23 * 24);
    expect(b.map((x) => x.nmId)).toEqual(NM_IDS);
  });
  it('слот — час МСК', () => {
    expect(unitkaSlot(new Date('2026-09-12T07:00:00Z'))).toBe('2026-09-12T10');
    expect(unitkaSlot(new Date('2026-09-12T21:30:00Z'))).toBe('2026-09-13T00');
  });
});

describe('preflight', () => {
  it('эталонный лист проходит', () => {
    const r = preflight(snapshot(), LCD);
    expect(r.issues).toEqual([]);
    expect(r.blocks).toHaveLength(24);
    expect(r.monthMismatch).toBe(false);
  });
  it('пропавшая формула — STRUCTURE_DRIFT', () => {
    const snap = snapshot({ mutate: (s) => { s.formulas[3]![GRID.B0 - 1 + OFFSET.unitProfit] = 5; } });
    expect(preflight(snap, LCD).issues.join()).toMatch(/расчётных ячеек без формулы/);
    expect(codeOf(() => buildPlan(inputs({ snapshot: snap })))).toBe('STRUCTURE_DRIFT');
  });
  it('формула-наследие в факт-ячейке — не дрейф, а инвентарь; в закрытом дне заменяется значением', () => {
    // закрытый день: значение формулы совпадает с BQ, но ячейка всё равно попадает в план
    const snap = snapshot({
      direct: (nm) => (nm === NM_IDS[0] ? 65.98 : 60.55), commission: (nm) => (nm === NM_IDS[0] ? 0.441234 : 0.45578),
      mutate: (s) => {
        s.formulas[0]![GRID.B0 - 1 + OFFSET.storage] = '=T737*0.15';
        s.formulas[15]![GRID.B0 - 1 + OFFSET.stock] = '=T751-Q752+S751';
        s.grid[dayRow(15) - GRID.TOP]![GRID.B0 - 1 + OFFSET.stock] = 480; // проекция остатка в будущем дне
      },
    });
    const pre = preflight(snap, LCD);
    expect(pre.issues).toEqual([]);
    expect(pre.legacy).toEqual({ closed: 1, future: 1, byKey: { storage: 1, stock: 1 } });
    const p = buildPlan(inputs({ snapshot: snap }));           // FUTURE_LEAKAGE не срабатывает
    expect(p.legacyReplaced).toBe(1);
    expect(p.cells).toHaveLength(1);
    expect(p.cells[0]).toMatchObject({ row: 737, col: GRID.B0 + OFFSET.storage, kind: 'fact', want: 4.56 });
  });
  it('23 блока — BLOCKS 23/24', () => {
    const snap = snapshot({ mutate: (s) => { s.grid[0]![GRID.B0 + 23 * GRID.BW - 1] = 'без артикула'; } });
    expect(preflight(snap, LCD).issues[0]).toMatch(/BLOCKS = 23\/24/);
  });
  it('октябрьский LCD на сентябрьском листе — MONTH_ROLLOVER_REQUIRED', () => {
    expect(preflight(snapshot(), '2026-10-01').monthMismatch).toBe(true);
    expect(codeOf(() => buildPlan(inputs({ lcd: lcdRow('2026-10-01', '2026-10-02') })))).toBe('MONTH_ROLLOVER_REQUIRED');
  });
});

describe('buildPlan', () => {
  it('лист == BQ → пустой план (идемпотентность), ожидание полное', () => {
    const p = buildPlan(inputs({
      snapshot: snapshot({ direct: (nm) => (nm === NM_IDS[0] ? 65.98 : 60.55), commission: (nm) => (nm === NM_IDS[0] ? 0.441234 : 0.45578) }),
    }));
    expect(p.cells).toEqual([]);
    expect(p.closedDays).toBe(10);
    // 24 × 10 × 9 факт + 24 × 30 × 2 ставки + reverse + 2 LCD
    expect(p.expected).toHaveLength(24 * 10 * 9 + 24 * 30 * 2 + 3);
    expect(p.invariant).toEqual({ shipments: 597, sales: 545, refusals: 52, pass: true });
    expect(p.gaps.stock).toBe(10);
    expect(p.gaps.storage).toBe(24);
  });
  it('пустой лист → план = все факт-ячейки закрытых дней + ставки + LCD', () => {
    const p = buildPlan(inputs({ snapshot: snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }) }));
    const facts = p.cells.filter((c) => c.kind === 'fact');
    // 24×10×9 минус ячейки, которые и так должны быть пустыми (GAP): stock у SKU#3 (10) + storage 10.09 (24)
    expect(facts).toHaveLength(24 * 10 * 9 - 10 - 24);
    expect(p.cells.filter((c) => c.kind === 'logistics')).toHaveLength(30);   // только SKU #1 (65.98 ≠ 60.55)
    expect(p.cells.filter((c) => c.kind === 'commission')).toHaveLength(30);  // только SKU #1
    expect(p.cells.filter((c) => c.kind === 'lcd')).toHaveLength(2);
    const lcd = p.cells.find((c) => c.namedRange === 'LAST_CLOSED_DATE')!;
    expect(lcd.want).toBe(isoToSerial(LCD));
  });
  it('своя ставка при n >= 10, иначе магазин; округление 2 и 6 знаков', () => {
    const p = buildPlan(inputs());
    const r0 = p.rates.find((r) => r.nmId === NM_IDS[0])!;
    const r1 = p.rates.find((r) => r.nmId === NM_IDS[1])!;
    const r5 = p.rates.find((r) => r.nmId === NM_IDS[5])!;
    expect(r0).toMatchObject({ direct: 65.98, directSource: 'own', commission: 0.441234, commissionSource: 'own' });
    expect(r1).toMatchObject({ direct: 60.55, directSource: 'store', commission: 0.45578, commissionSource: 'store' });
    expect(r5).toMatchObject({ direct: 60.55, directSource: 'store' });
    expect(p.reverseRate).toBe(32.5256);
  });
  it('пересчёт задним числом: одна изменившаяся ячейка → план из одной ячейки', () => {
    const fx = facts();
    const row = fx.find((r) => r.nmId === NM_IDS[4] && r.date === '2026-09-07')!;
    row.orders = (row.orders ?? 0) + 5;
    const p = buildPlan(inputs({
      facts: fx,
      snapshot: snapshot({ direct: (nm) => (nm === NM_IDS[0] ? 65.98 : 60.55), commission: (nm) => (nm === NM_IDS[0] ? 0.441234 : 0.45578) }),
    }));
    expect(p.cells).toHaveLength(1);
    expect(p.cells[0]).toMatchObject({ row: dayRow(6), col: GRID.B0 + 4 * GRID.BW + OFFSET.orders, kind: 'fact', want: row.orders });
  });
  it('инвариант n ≠ sales + ref — INVARIANT_FAIL до записи', () => {
    expect(codeOf(() => buildPlan(inputs({ logistics: logistics({ sales: 540 }) })))).toBe('INVARIANT_FAIL');
  });
  it('дубль nmID×дата — DUP_KEY', () => {
    const fx = facts();
    fx.push({ ...fx[0]! });
    expect(codeOf(() => buildPlan(inputs({ facts: fx })))).toBe('DUP_KEY');
  });
  it('LCD в BQ раньше, чем в книге — LCD_REGRESSION', () => {
    expect(codeOf(() => buildPlan(inputs({ snapshot: snapshot({ lcdInSheet: '2026-09-11' }) })))).toBe('LCD_REGRESSION');
  });
  it('LCD отстаёт от D-1 больше допуска — SOURCE_STALE', () => {
    expect(codeOf(() => buildPlan(inputs({ lcd: lcdRow(LCD, '2026-09-13') })))).toBe('SOURCE_STALE');
    expect(codeOf(() => buildPlan(inputs({ lcd: lcdRow(LCD, '2026-09-12') })))).toBe('NO_THROW');
  });
  it('факт за датой > LCD в книге — FUTURE_LEAKAGE', () => {
    const snap = snapshot({ mutate: (s) => { s.grid[dayRow(12) - GRID.TOP]![GRID.B0 - 1 + OFFSET.views] = 7; } });
    expect(codeOf(() => buildPlan(inputs({ snapshot: snap })))).toBe('FUTURE_LEAKAGE');
  });
  it('у блока нет строк в подготовленном слое — BLOCK_MISSING', () => {
    const fx = facts().filter((r) => r.nmId !== NM_IDS[7]);
    expect(codeOf(() => buildPlan(inputs({ facts: fx })))).toBe('BLOCK_MISSING');
  });
});

describe('toWriteRanges', () => {
  it('группирует вертикальные отрезки, именованный диапазон отдельно, пусто → ""', () => {
    const p = buildPlan(inputs({ snapshot: snapshot({ applyFacts: false, lcdInSheet: '2026-09-01' }) }));
    const ranges = toWriteRanges(p.cells, 'WB_Юнит_2025');
    expect(ranges.some((r) => r.range === 'LAST_CLOSED_DATE')).toBe(true);
    const col = colA1(GRID.B0 + OFFSET.views);
    const r = ranges.find((x) => x.range === `'WB_Юнит_2025'!${col}737:${col}746`)!;
    expect(r.values).toHaveLength(10);
    const total = ranges.reduce((a, x) => a + x.values.length, 0);
    expect(total).toBe(p.cells.length);
    // stock у SKU#3 должен быть очищен пустой строкой, если в листе стоит число
    const p2 = buildPlan(inputs({ snapshot: snapshot({ mutate: (s) => { s.grid[dayRow(0) - GRID.TOP]![GRID.B0 + 2 * GRID.BW - 1 + OFFSET.stock] = 42; } }) }));
    const w = toWriteRanges(p2.cells.filter((c) => c.kind === 'fact'), 'S');
    expect(w[0]!.values).toEqual([['']]);
  });
});
