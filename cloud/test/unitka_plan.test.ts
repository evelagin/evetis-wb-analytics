import { describe, it, expect } from 'vitest';
import { buildPlan, preflight, toWriteRanges, diffRows, formatContract, toFormatWrites, formatRows, type PlanInputs } from '../src/loaders/unitka/plan.js';
import { OFFSET, colA1, serialToIso, isoToSerial, findBlocks, factEqual } from '../src/loaders/unitka/model.js';
import { unitkaSlot } from '../src/loaders/unitka/slot.js';
import { LoaderError } from '../src/errors.js';
import { snapshot, facts, logistics, commission, lcdRow, LCD, NM_IDS, refFormat, DIM, GRID, dayRow } from './unitka_fixture.js';

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
    expect(r.sectionIssues).toEqual([]);
    expect(r.blocks).toHaveLength(24);
    expect(r.layout).toMatchObject({ monthKey: '2026-09', topRow: 735, firstDailyRow: 737, lastDailyRow: 766, mtdRow: 767, daysInMonth: 30, lastBlockColumn: 587 });   // терминальная колонка — последняя метрика блока 24 (VO)
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
    expect(p.stockProjectionCells).toBe(1);
    expect(p.cells).toHaveLength(1);
    expect(p.cells[0]).toMatchObject({ row: 737, col: GRID.B0 + OFFSET.storage, kind: 'fact', want: 4.56, changeType: 'NO_CHANGE' });
  });
  it('фактическое хранение в будущем дне — FUTURE_LEAKAGE даже из формулы (KEEP только для остатка)', () => {
    const snap = snapshot({ mutate: (s) => {
      s.formulas[15]![GRID.B0 - 1 + OFFSET.storage] = '=T752*0.15';
      s.grid[dayRow(15) - GRID.TOP]![GRID.B0 - 1 + OFFSET.storage] = 12.3;
    } });
    expect(codeOf(() => buildPlan(inputs({ snapshot: snap })))).toBe('FUTURE_LEAKAGE');
  });
  it('стёртый nmID при живой шапке блока — MONTH_SECTION_INVALID (не «выбывший SKU»)', () => {
    const snap = snapshot({ mutate: (s) => { s.grid[0]![GRID.B0 + 23 * GRID.BW - 1] = 'без артикула'; } });
    expect(preflight(snap, LCD).sectionIssues.join()).toMatch(/слот 23 \(US\): шапка блока есть, nmID в строке 735 нет/);
    expect(codeOf(() => buildPlan(inputs({ snapshot: snap })))).toBe('MONTH_SECTION_INVALID');
  });
  it('октябрьский LCD на сентябрьской секции — MONTH_SECTION_INVALID (MONTH_ROLLOVER_REQUIRED выведен из пути)', () => {
    expect(preflight(snapshot(), '2026-10-01').sectionIssues[0]).toMatch(/месяц LAST_CLOSED_DATE 2026-10 ≠ месяцу секции 2026-09/);
    expect(codeOf(() => buildPlan(inputs({ lcd: lcdRow('2026-10-01', '2026-10-02') })))).toBe('MONTH_SECTION_INVALID');
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
    expect(p.cells[0]).toMatchObject({ row: dayRow(6), col: GRID.B0 + 4 * GRID.BW + OFFSET.orders, kind: 'fact', want: row.orders, changeType: 'LATE_SOURCE_CORRECTION', source: 'FUNNEL_API' });
    expect(p.byChangeType).toEqual({ FACT_CHANGE: 0, LATE_SOURCE_CORRECTION: 1, MODEL_PARAMETER_REFRESH: 0, LCD_ADVANCE: 0, NO_CHANGE: 0 });
  });
  it('классификация: новый день — FACT_CHANGE, ставки — MODEL_PARAMETER_REFRESH, LCD — LCD_ADVANCE', () => {
    // книга закрыта на 09.09, BQ — на 10.09: факт 10.09 = FACT_CHANGE; комиссия SKU#1 отличается; LCD двигается
    const p = buildPlan(inputs({ snapshot: snapshot({ lcdInSheet: '2026-09-09', direct: (nm) => (nm === NM_IDS[0] ? 65.98 : 60.55) }) }));
    expect(p.bookLcd).toBe('2026-09-09');
    expect(p.byChangeType.FACT_CHANGE).toBe(24 * 9 - 1 - 24); // день 10.09: минус GAP stock SKU#3 и storage у всех
    expect(p.byChangeType.MODEL_PARAMETER_REFRESH).toBe(30);   // комиссия SKU#1
    expect(p.byChangeType.LCD_ADVANCE).toBe(2);
    expect(p.byChangeType.LATE_SOURCE_CORRECTION).toBe(0);
    const rows = diffRows(p);
    expect(rows.find((r) => r.CELL === 'LAST_CLOSED_DATE')).toMatchObject({ NEW: LCD, CHANGE_TYPE: 'LCD_ADVANCE' });
    expect(rows.filter((r) => r.CHANGE_TYPE === 'FACT_CHANGE').every((r) => r.DATE === LCD)).toBe(true);
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

describe('formatContract', () => {
  it('эталон = строка 737; будущие строки не рассматриваются; идемпотентно на одинаковом листе', () => {
    const snap = snapshot();
    const p = buildPlan(inputs({ snapshot: snap }));
    expect(p.formatCells).toEqual([]);
    // 9 закрытых строк после эталонной × 11 колонок × 24 блока минус GAP-ячейки (stock SKU#3: 9, storage 10.09: 24)
    expect(p.formatContractCells).toBe(24 * 11 * 9 - 9 - 24);
  });
  it('Master 12.09: «будущий» вид с 10.09 при LCD 11.09 → 2 дня × 3 колонки × 24 блока = 144 ячейки, 11 колонок сверены', () => {
    const snap = snapshot({ futureStyleFrom: 9 });
    const p = buildPlan(inputs({ snapshot: snap, lcd: lcdRow('2026-09-11', '2026-09-12'), facts: facts('2026-09-11') }));
    // 2 дня × 3 колонки × 24 блока = 144, минус GAP хранения 10.09 (фикстура: storage 10.09 = null у всех 24) = 120
    expect(p.formatCells).toHaveLength(120);
    expect(new Set(p.formatCells.map((c) => c.key))).toEqual(new Set(['logistics', 'commission', 'storage']));
    expect(new Set(p.formatCells.map((c) => c.date))).toEqual(new Set(['2026-09-10', '2026-09-11']));
    expect(p.formatCells.every((c) => c.row <= dayRow(10))).toBe(true);
    const c0 = p.formatCells.find((c) => c.key === 'logistics')!;
    expect(c0.before.fg).toEqual(DIM);
    expect(c0.want).toEqual(refFormat('logistics'));
    const rows = formatRows(p);
    expect(rows.find((r) => r.CELL === `${colA1(c0.col)}${c0.row}`)).toMatchObject({ CHANGE_TYPE: 'FORMAT_CHANGE', METRIC: 'logistics', SOURCE: `эталон ${colA1(c0.col)}737` });
    expect(rows).toHaveLength(120);
    // группировка: ставки — 2 смежные строки в один repeatCell (48), хранение — только 11.09 (24) → 72 запроса
    const w = toFormatWrites(p.formatCells);
    expect(w).toHaveLength(72);
    const wl = w.find((x) => x.startCol === c0.col - 1)!;
    expect(wl).toMatchObject({ startRow: dayRow(9) - 1, endRow: dayRow(10), endCol: c0.col }); // строки 746..747 → 0-based [745, 747)
  });
  it('будущие строки со «взрослым» форматом не трогаются, закрытые с «будущим» — приводятся', () => {
    const snap = snapshot({ futureStyleFrom: 20 });
    const p = buildPlan(inputs({ snapshot: snap }));
    const fc = formatContract(snap, p.blocks, 10, '2026-09-01', p.expected);
    expect(fc.cells).toEqual([]); // будущее (индекс ≥ 20) вне контракта
  });
  it('GAP-ячейка (want = null) вне контракта: оранжевая разметка пропуска остатков 03.09 не трогается', () => {
    const snap = snapshot({ mutate: (s) => {
      // SKU#3: остатков нет весь месяц (GAP) — Master пометил их оранжевым; формат ≠ эталону
      for (let i = 1; i < 10; i++) s.formats[i]![GRID.B0 + 2 * GRID.BW - 1 + OFFSET.stock] = { bg: { red: 0.988, green: 0.898, blue: 0.804 }, fg: null, numberFormat: null };
    } });
    const p = buildPlan(inputs({ snapshot: snap }));
    expect(p.formatCells).toEqual([]);
  });
});
