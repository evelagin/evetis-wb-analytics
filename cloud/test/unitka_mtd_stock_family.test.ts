/**
 * UNITKA CALENDAR V2 — остаток MTD: узкий СЕМАНТИЧЕСКИЙ контракт одной ячейки (смещение 7, строка MTD).
 *
 * Production-книга хранит родную форму  =IFERROR(INDEX(<остаток дней>;MATCH(LAST_CLOSED_DATE;<даты дней>;0));"").
 * Копия книги (Drive) отдаёт ту же формулу в обёртке ARRAY_CONSTRAIN(ARRAYFORMULA(…); 1; 1). Обе формы значат одно:
 * остаток на LAST_CLOSED_DATE в диапазонах СВОЕГО блока и СВОЕГО месяца, пусто — если даты нет. Всё остальное — отказ.
 * Генератор переносит семейство месяца-источника: production не зависит от артефакта копии.
 */
import { describe, it, expect } from 'vitest';
import { OFFSET, colA1 } from '../src/loaders/unitka/model.js';
import { geometryAt, slotStart, type MonthKey } from '../src/loaders/unitka/calendar.js';
import { blockMtdFormulas, mtdStockFormula, recogniseMtdStock, fromLocaleFormula, toLocaleFormula, type MtdStockFamily } from '../src/loaders/unitka/formulas.js';
import { planMonthPrep, type PrepInputs } from '../src/loaders/unitka/monthprep.js';
import type { CellValue } from '../src/loaders/unitka/model.js';
import type { Snapshot } from '../src/loaders/unitka/plan.js';
import { sectionFromSpec, septemberSpec, cogsSnapshot, septemberStructure, septemberRowFormats, NEW_NM, SEPT_NMS, WIDTH_SEPT } from './unitka_calendar_fixture.js';

const SEPT = geometryAt({ year: 2026, month: 9 }, 735);
const OCT = geometryAt({ year: 2026, month: 10 }, 769);
const B1 = slotStart(0), B2 = slotStart(1), B24 = slotStart(23), B25 = slotStart(24);

/** Тексты — ровно как их отдаёт Sheets API (локаль ru_RU): ORIGINAL (снимок Gate F 19.09.2026) и копия книги. */
const PROD_T767 = '=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"")';
const PROD_AR767 = '=IFERROR(INDEX(AR737:AR766;MATCH(LAST_CLOSED_DATE;$AK$737:$AK$766;0));"")';
const PROD_UZ767 = '=IFERROR(INDEX(UZ737:UZ766;MATCH(LAST_CLOSED_DATE;$US$737:$US$766;0));"")';
const TEST_T767 = '=ARRAY_CONSTRAIN(ARRAYFORMULA(IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"")); 1; 1)';
const en = (f: string): string => fromLocaleFormula(f, 'SEMICOLON');
const rec = (f: CellValue, start = B1, g = SEPT) => recogniseMtdStock(typeof f === 'string' ? en(f) : f, start, g);
const issueOf = (f: CellValue, start = B1, g = SEPT): string | null => { const r = rec(f, start, g); return 'issue' in r ? r.issue : null; };

describe('остаток MTD · распознавание семейства (только доказанные формы)', () => {
  it('1 · родная production-форма INDEX/MATCH → native (блоки 1, 2 и 24 живой книги)', () => {
    expect(rec(PROD_T767)).toEqual({ family: 'native' });
    expect(rec(PROD_AR767, B2)).toEqual({ family: 'native' });
    expect(rec(PROD_UZ767, B24)).toEqual({ family: 'native' });
  });
  it('2 · обёртка копии книги ARRAY_CONSTRAIN(ARRAYFORMULA(…); 1; 1) → wrapped, ядро то же самое', () => {
    expect(rec(TEST_T767)).toEqual({ family: 'wrapped' });
    expect(en(TEST_T767).replace('=ARRAY_CONSTRAIN(ARRAYFORMULA(', '=').replace('), 1, 1)', '')).toBe(en(PROD_T767));
  });
  it('регистр и пробелы не значимы; построитель и распознаватель согласованы в обе стороны', () => {
    expect(rec(' =iferror( index(T737:T766; match(LAST_CLOSED_DATE; $M$737:$M$766; 0)); "") ')).toEqual({ family: 'native' });
    for (const fam of ['native', 'wrapped'] as MtdStockFamily[]) {
      expect(recogniseMtdStock(mtdStockFormula(B1, SEPT, fam), B1, SEPT)).toEqual({ family: fam });
      expect(blockMtdFormulas(B1, SEPT, fam).get(OFFSET.stock)).toBe(mtdStockFormula(B1, SEPT, fam));
    }
    expect(toLocaleFormula(mtdStockFormula(B1, SEPT, 'native'), 'SEMICOLON')).toBe(PROD_T767);
    expect(toLocaleFormula(mtdStockFormula(B1, SEPT, 'wrapped'), 'SEMICOLON')).toBe(TEST_T767);
  });
  it('3 · другая колонка остатка (U вместо T) → MTD_STOCK_WRONG_STOCK_RANGE', () => {
    expect(issueOf('=IFERROR(INDEX(U737:U766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"")')).toBe('MTD_STOCK_WRONG_STOCK_RANGE');
    expect(issueOf('=ARRAY_CONSTRAIN(ARRAYFORMULA(IFERROR(INDEX(Q737:Q766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"")); 1; 1)')).toBe('MTD_STOCK_WRONG_STOCK_RANGE');
  });
  it('4 · другая колонка дат (N вместо M; относительная вместо абсолютной) → MTD_STOCK_WRONG_DATE_RANGE', () => {
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$N$737:$N$766;0));"")')).toBe('MTD_STOCK_WRONG_DATE_RANGE');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$B$737:$B$766;0));"")')).toBe('MTD_STOCK_WRONG_DATE_RANGE');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;M737:M766;0));"")')).toBe('MTD_STOCK_WRONG_DATE_RANGE');
  });
  it('5 · диапазоны ДРУГОГО блока SKU (формула блока 2 в блоке 1 и наоборот) → отказ с указанием на чужой блок', () => {
    const r = rec(PROD_AR767, B1);
    expect(r).toMatchObject({ issue: 'MTD_STOCK_WRONG_STOCK_RANGE' });
    expect('detail' in r && r.detail).toMatch(/друг\S* блок/);
    expect(issueOf(PROD_T767, B2)).toBe('MTD_STOCK_WRONG_STOCK_RANGE');
    // остаток свой, даты чужого блока
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$AK$737:$AK$766;0));"")')).toBe('MTD_STOCK_WRONG_DATE_RANGE');
  });
  it('6 · другой интервал строк: короче месяца, длиннее месяца, август (702..732), расходящиеся интервалы', () => {
    expect(issueOf('=IFERROR(INDEX(T737:T765;MATCH(LAST_CLOSED_DATE;$M$737:$M$765;0));"")')).toBe('MTD_STOCK_WRONG_STOCK_RANGE');
    expect(issueOf('=IFERROR(INDEX(T737:T767;MATCH(LAST_CLOSED_DATE;$M$737:$M$767;0));"")')).toBe('MTD_STOCK_WRONG_STOCK_RANGE');
    expect(issueOf('=IFERROR(INDEX(T702:T732;MATCH(LAST_CLOSED_DATE;$M$702:$M$732;0));"")')).toBe('MTD_STOCK_WRONG_STOCK_RANGE');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$736:$M$766;0));"")')).toBe('MTD_STOCK_WRONG_DATE_RANGE');
    // сентябрьская формула в октябрьской секции — чужой месяц
    expect(issueOf(PROD_T767, B1, OCT)).toBe('MTD_STOCK_WRONG_STOCK_RANGE');
  });
  it('7 · другой именованный диапазон или ячейка вместо LAST_CLOSED_DATE → MTD_STOCK_WRONG_NAMED_RANGE', () => {
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DAY;$M$737:$M$766;0));"")')).toBe('MTD_STOCK_WRONG_NAMED_RANGE');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH($WB$736;$M$737:$M$766;0));"")')).toBe('MTD_STOCK_WRONG_NAMED_RANGE');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(REVERSE_LEG_RATE;$M$737:$M$766;0));"")')).toBe('MTD_STOCK_WRONG_NAMED_RANGE');
  });
  it('8 · другая подстановка при отсутствии даты (0, «-», отсутствует) и неточный поиск → отказ', () => {
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));0)')).toBe('MTD_STOCK_WRONG_FALLBACK');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"-")')).toBe('MTD_STOCK_WRONG_FALLBACK');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));" ")')).toBe('MTD_STOCK_WRONG_FALLBACK'); // пробел — не пусто
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0)))')).toBe('MTD_STOCK_MALFORMED');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;1));"")')).toBe('MTD_STOCK_WRONG_MATCH_TYPE');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766));"")')).toBe('MTD_STOCK_MALFORMED');
  });
  it('9 · неизвестная или битая обёртка, многоячеечный вывод, не формула → отказ', () => {
    const core = 'IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"")';
    expect(issueOf(`=ARRAYFORMULA(${core})`)).toBe('MTD_STOCK_UNKNOWN_WRAPPER');
    expect(issueOf(`=ARRAY_CONSTRAIN(${core}; 1; 1)`)).toBe('MTD_STOCK_UNKNOWN_WRAPPER');
    expect(issueOf(`=SORT(ARRAYFORMULA(${core}))`)).toBe('MTD_STOCK_UNKNOWN_WRAPPER');
    expect(issueOf(`=IF(TRUE;${core};"")`)).toBe('MTD_STOCK_UNKNOWN_WRAPPER');
    expect(issueOf(`=ARRAY_CONSTRAIN(ARRAYFORMULA(${core}); 2; 1)`)).toBe('MTD_STOCK_MULTI_CELL_OUTPUT');
    expect(issueOf(`=ARRAY_CONSTRAIN(ARRAYFORMULA(${core}); 1; 3)`)).toBe('MTD_STOCK_MULTI_CELL_OUTPUT');
    expect(issueOf(`=ARRAY_CONSTRAIN(ARRAYFORMULA(${core}); 1)`)).toBe('MTD_STOCK_UNKNOWN_WRAPPER');
    expect(issueOf(`=ARRAY_CONSTRAIN(ARRAYFORMULA(${core}; 1; 1)`)).toBe('MTD_STOCK_MALFORMED');
    expect(issueOf(`=ARRAY_CONSTRAIN(ARRAYFORMULA(ARRAY_CONSTRAIN(ARRAYFORMULA(${core}); 1; 1)); 1; 1)`)).toBe('MTD_STOCK_MALFORMED');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"")+0')).toBe('MTD_STOCK_MALFORMED');
    expect(issueOf('=IFERROR(INDEX(T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0);1);"")')).toBe('MTD_STOCK_MALFORMED');
    expect(issueOf("=IFERROR(INDEX('Другой лист'!T737:T766;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));\"\")")).toBe('MTD_STOCK_MALFORMED');
    expect(issueOf('=IFERROR(INDEX(T737:T;MATCH(LAST_CLOSED_DATE;$M$737:$M$766;0));"")')).toBe('MTD_STOCK_MALFORMED');
    expect(issueOf(15)).toBe('MTD_STOCK_NOT_FORMULA');
    expect(issueOf('')).toBe('MTD_STOCK_NOT_FORMULA');
    expect(issueOf(null)).toBe('MTD_STOCK_NOT_FORMULA');
  });
  it('10 · после сдвига колонок и строк формы остаются распознаваемыми: блок 25 (VQ..), октябрь, февраль 2027', () => {
    const vx = colA1(B25 + OFFSET.stock), vq = colA1(B25);
    expect([vq, vx]).toEqual(['VQ', 'VX']);
    expect(rec(`=IFERROR(INDEX(VX771:VX801;MATCH(LAST_CLOSED_DATE;$VQ$771:$VQ$801;0));"")`, B25, OCT)).toEqual({ family: 'native' });
    expect(rec(`=ARRAY_CONSTRAIN(ARRAYFORMULA(IFERROR(INDEX(VX771:VX801;MATCH(LAST_CLOSED_DATE;$VQ$771:$VQ$801;0));"")); 1; 1)`, B25, OCT)).toEqual({ family: 'wrapped' });
    const feb = geometryAt({ year: 2027, month: 2 }, 908);
    for (const fam of ['native', 'wrapped'] as MtdStockFamily[]) for (const s of [0, 7, 23, 24, 40]) {
      expect(recogniseMtdStock(mtdStockFormula(slotStart(s), feb, fam), slotStart(s), feb)).toEqual({ family: fam });
      // та же формула, но проверенная против соседнего блока — отказ
      expect(recogniseMtdStock(mtdStockFormula(slotStart(s), feb, fam), slotStart(s + 1), feb)).toMatchObject({ issue: 'MTD_STOCK_WRONG_STOCK_RANGE' });
    }
  });
});

/* ───────────────────────── генератор: семейство месяца-источника ───────────────────────── */

const OCT_KEY: MonthKey = { year: 2026, month: 10 };
function septemberInputs(families: (slot: number) => MtdStockFamily, mutate?: (s: Snapshot) => void): PrepInputs {
  const spec = septemberSpec().map((b) => ({ ...b, params: { ...b.params, mtdStockFamily: families(b.slot) } }));
  const sept = sectionFromSpec({ year: 2026, month: 9 }, 735, spec, WIDTH_SEPT);
  mutate?.(sept);
  const columnA: CellValue[] = Array(768).fill(null);
  columnA[734] = sept.grid[0]![0]!;
  return {
    target: OCT_KEY, meta: { sheetId: 739487431, rowCount: 768, columnCount: WIDTH_SEPT, anchorCol: WIDTH_SEPT }, columnA,
    predecessor: sept, existing: null,
    population: [...SEPT_NMS, NEW_NM].map((nmId) => ({ nmId, name: `Товар ${nmId}` })),
    cogs: cogsSnapshot({ [NEW_NM]: 426.735 }), structure: septemberStructure(768, WIDTH_SEPT),
    rowFormats: septemberRowFormats(WIDTH_SEPT, [735, 736, 737, 766, 767, 768]),
  };
}
const mtdStockCells = (inp: PrepInputs): string[] => {
  const plan = planMonthPrep(inp);
  expect(plan.status).toBe('PLAN_CREATE');
  return plan.blocks.map((b) => { const v = plan.cells.find((c) => c.row === 802 && c.col === b.start + OFFSET.stock)!.value; return v.kind === 'formula' ? v.text : ''; });
};

describe('остаток MTD · генератор переносит семейство месяца-источника', () => {
  it('production (24 × native) → октябрь: 25 × native со своими диапазонами 771..801; обёртки копии нет нигде', () => {
    const plan = planMonthPrep(septemberInputs(() => 'native'));
    expect(plan.status).toBe('PLAN_CREATE');
    expect(plan.blocks.map((b) => b.params.mtdStockFamily)).toEqual(Array(25).fill('native'));
    const cells = mtdStockCells(septemberInputs(() => 'native'));
    expect(cells).toHaveLength(25);
    cells.forEach((f, i) => expect(f).toBe(mtdStockFormula(slotStart(i), OCT, 'native')));
    expect(cells[0]).toBe('=IFERROR(INDEX(T771:T801,MATCH(LAST_CLOSED_DATE,$M$771:$M$801,0)),"")');
    expect(cells[24]).toBe('=IFERROR(INDEX(VX771:VX801,MATCH(LAST_CLOSED_DATE,$VQ$771:$VQ$801,0)),"")');
    expect(plan.cells.some((c) => c.value.kind === 'formula' && /ARRAY_CONSTRAIN|ARRAYFORMULA/.test(c.value.text))).toBe(false);
    expect(plan.cells.some((c) => c.value.kind === 'formula' && c.row === 802 && /7[3-6]\d/.test(c.value.text))).toBe(false); // ни одной ссылки на сентябрь
  });
  it('копия книги (24 × wrapped) → октябрь: 25 × wrapped — форма копии сохраняется в копии', () => {
    const cells = mtdStockCells(septemberInputs(() => 'wrapped'));
    cells.forEach((f, i) => expect(f).toBe(mtdStockFormula(slotStart(i), OCT, 'wrapped')));
    expect(cells[24]).toBe('=ARRAY_CONSTRAIN(ARRAYFORMULA(IFERROR(INDEX(VX771:VX801,MATCH(LAST_CLOSED_DATE,$VQ$771:$VQ$801,0)),"")), 1, 1)');
  });
  it('смешанный месяц: каждый перенесённый блок сохраняет СВОЁ семейство; новый блок — семейство блока-шаблона (последнего)', () => {
    const fam = (slot: number): MtdStockFamily => (slot === 2 || slot === 23 ? 'wrapped' : 'native');
    const plan = planMonthPrep(septemberInputs(fam));
    expect(plan.status).toBe('PLAN_CREATE');
    expect(plan.blocks.map((b) => b.params.mtdStockFamily)).toEqual([...Array.from({ length: 24 }, (_, s) => fam(s)), 'wrapped']);
  });
  it.each([
    ['чужая колонка остатка', (s: Snapshot) => { s.formulas[767 - 737]![B2 + OFFSET.stock - 1] = '=IFERROR(INDEX(AS737:AS766,MATCH(LAST_CLOSED_DATE,$AK$737:$AK$766,0)),"")'; }, 'MTD_STOCK_WRONG_STOCK_RANGE'],
    ['диапазоны августа', (s: Snapshot) => { s.formulas[767 - 737]![B2 + OFFSET.stock - 1] = '=IFERROR(INDEX(AR702:AR732,MATCH(LAST_CLOSED_DATE,$AK$702:$AK$732,0)),"")'; }, 'MTD_STOCK_WRONG_STOCK_RANGE'],
    ['неизвестная обёртка', (s: Snapshot) => { s.formulas[767 - 737]![B2 + OFFSET.stock - 1] = '=ARRAYFORMULA(IFERROR(INDEX(AR737:AR766,MATCH(LAST_CLOSED_DATE,$AK$737:$AK$766,0)),""))'; }, 'MTD_STOCK_UNKNOWN_WRAPPER'],
    ['значение вместо формулы', (s: Snapshot) => { s.formulas[767 - 737]![B2 + OFFSET.stock - 1] = 120; }, 'MTD_STOCK_NOT_FORMULA'],
  ])('одна ячейка вне контракта (%s) → BLOCKED TEMPLATE_MISMATCH с адресом и кодом, месяц не создаётся', (_n, mutate, code) => {
    const plan = planMonthPrep(septemberInputs(() => 'native', mutate));
    expect([plan.status, plan.code]).toEqual(['BLOCKED', 'TEMPLATE_MISMATCH']);
    expect(plan.reasons.join(' ')).toContain('AR767');
    expect(plan.reasons.join(' ')).toContain(code);
    expect(plan.cells).toHaveLength(0);
  });
});
