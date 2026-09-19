/**
 * UNITKA CALENDAR V2 (Phase 2C) — локаль формул, размеры, откат (УФ — unitka_visual.test.ts).
 * Эталоны локали — строки, прочитанные Sheets API из тестовой копии книги (ru_RU) 19.09.2026.
 */
import { describe, it, expect } from 'vitest';
import { formulaStyleOf, fromLocaleFormula, toLocaleFormula } from '../src/loaders/unitka/formulas.js';
import { cfInsertTrims, dimensionRequests, planMonthRollback, shiftColumnRefs } from '../src/loaders/unitka/monthprep_struct.js';
import { planMonthPrep, toStructureRequests } from '../src/loaders/unitka/monthprep.js';
import { geometryAt } from '../src/loaders/unitka/calendar.js';
import type { CellValue } from '../src/loaders/unitka/model.js';
import type { Snapshot } from '../src/loaders/unitka/plan.js';
import type { ConditionalFormatRule } from '../src/loaders/unitka/sheets.js';
import { SEPT_LIVE_FORMULAS as LIVE } from './unitka_sept_live_formulas.js';
import {
  sectionFromSpec, septemberSpec, septemberStructure, septemberCfRules, septemberRowFormats, cogsSnapshot, SEPT_NMS, NEW_NM, SHEET_ID, WIDTH_SEPT,
} from './unitka_calendar_fixture.js';

const SEPT = geometryAt({ year: 2026, month: 9 }, 735);
const OCT = geometryAt({ year: 2026, month: 10 }, 769);

/** Живые строки API (ru_RU) из тестовой копии: C737, Y766, AG766, AF767. */
const API_RU: Record<string, string> = {
  C737: '=IF($B737>LAST_CLOSED_DATE;"";SUM(FILTER(N737:UT737;MOD(COLUMN(N737:UT737)-COLUMN(N737);24)=0)))',
  Y766: '=IF($M766>LAST_CLOSED_DATE;"";-($R$45+AA766*(1-AD766)+100-(AA766*(1-AD766)-AF766))*N766)',
  AG766: '=IF($M766>LAST_CLOSED_DATE;"";T766*0,15)',
  AF767: '=SUMPRODUCT(($M$737:$M$766<=LAST_CLOSED_DATE)*AF737:AF766*Q737:Q766)+SUMIF($M$737:$M$766;"<="&LAST_CLOSED_DATE;S737:S766)*REVERSE_LEG_RATE',
};

describe('локаль формул: ru_RU ↔ каноническая форма', () => {
  it('стиль по локали; неизвестная локаль — отказ', () => {
    expect(formulaStyleOf('ru_RU')).toBe('SEMICOLON');
    expect(formulaStyleOf('en_US')).toBe('COMMA');
    expect(formulaStyleOf(undefined)).toBe('COMMA');
    expect(() => formulaStyleOf('de_DE')).toThrow();
  });
  it.each(Object.keys(API_RU))('%s: строка API (ru) ↔ эталон сентября (en), в обе стороны', (a1) => {
    expect(fromLocaleFormula(API_RU[a1]!, 'SEMICOLON')).toBe(LIVE[a1]);
    expect(toLocaleFormula(LIVE[a1]!, 'SEMICOLON')).toBe(API_RU[a1]);
  });
  it('все эталонные формулы: туда-обратно без потерь; строковые литералы не трогаются', () => {
    for (const f of Object.values(LIVE)) expect(fromLocaleFormula(toLocaleFormula(f, 'SEMICOLON'), 'SEMICOLON')).toBe(f);
    expect(toLocaleFormula('=CHOOSE(2,"пн,вт","a;b")', 'SEMICOLON')).toBe('=CHOOSE(2;"пн,вт";"a;b")');
    expect(toLocaleFormula('=A1-426.735', 'SEMICOLON')).toBe('=A1-426,735');
  });
});

describe('размеры колонок и строк', () => {
  const req = dimensionRequests(septemberStructure(), SEPT, OCT, 23, [25], SHEET_ID).map((r) => r.updateDimensionProperties as { range: { dimension: string; startIndex: number; endIndex: number }; properties: { pixelSize?: number; hiddenByUser: boolean } });
  it('блок 25: ширина и скрытие по смещениям шаблона (16–22 скрыты), не целиком скрыт; резерв не копируется', () => {
    const cols = req.filter((r) => r.range.dimension === 'COLUMNS');
    expect(cols).toHaveLength(24);
    expect(cols.map((c) => c.range.startIndex + 1)).toEqual(Array.from({ length: 24 }, (_, o) => 613 + o));
    expect(cols.filter((c) => c.properties.hiddenByUser).map((c) => c.range.startIndex + 1 - 613)).toEqual([16, 17, 18, 19, 20, 21, 22]);
    expect(cols[0]!.properties.pixelSize).toBe(80);
    expect(cols.some((c) => c.range.startIndex + 1 >= 589 && c.range.startIndex + 1 <= 612)).toBe(false);
  });
  it('строки 769..803 — высоты тех же типов строк сентября', () => {
    const rows = req.filter((r) => r.range.dimension === 'ROWS').map((r) => [r.range.startIndex + 1, r.range.endIndex, r.properties.pixelSize]);
    expect(rows).toEqual([[769, 769, 30], [770, 770, 18], [771, 771, 18], [772, 801, 18], [802, 802, 18], [803, 803, 18]]);
  });
});

describe('откат созданного месяца', () => {
  const rules = [...septemberCfRules(), { ranges: [{ sheetId: SHEET_ID, startRowIndex: 770, endRowIndex: 801, startColumnIndex: 12, endColumnIndex: 13 }], gradientRule: {} } as ConditionalFormatRule];
  it('удаляет только УФ новой секции, вставленные колонки 589..612 и строки 769..803 — в этом порядке', () => {
    const p = planMonthRollback({ geometry: OCT, rowCount: 803, sheetId: SHEET_ID, rules, insertedColumns: [589, 612], newColumnsEmptyAbove: true });
    expect(p.refused).toBeNull();
    expect(p.deletedCfRules).toBe(1);
    expect(p.requests[0]).toEqual({ deleteConditionalFormatRule: { sheetId: SHEET_ID, index: rules.length - 1 } });
    expect(p.requests[1]).toEqual({ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 588, endIndex: 612 } } });
    expect(p.requests[2]).toEqual({ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'ROWS', startIndex: 768, endIndex: 803 } } });
  });
  it('новые колонки непусты выше секции — отказ', () => {
    expect(planMonthRollback({ geometry: OCT, rowCount: 803, sheetId: SHEET_ID, rules, insertedColumns: [589, 612], newColumnsEmptyAbove: false }).refused).toMatch(/непусты/);
  });
  it('правило «через границу» не удаляется: его диапазон в новой секции обрезает удаление строк (живой лист, Phase 2C)', () => {
    const straddle = [{ ranges: [{ startRowIndex: 736, endRowIndex: 766 }, { startRowIndex: 770, endRowIndex: 801 }] } as ConditionalFormatRule];
    const p = planMonthRollback({ geometry: OCT, rowCount: 803, sheetId: SHEET_ID, rules: straddle, insertedColumns: [589, 612], newColumnsEmptyAbove: true });
    expect(p.refused).toBeNull();
    expect(p.deletedCfRules).toBe(0);
    expect(p.requests.map((r) => Object.keys(r)[0])).toEqual(['deleteDimension', 'deleteDimension']);
  });
});

describe('планировщик в локали ru_RU (как живая книга)', () => {
  const toRu = (s: Snapshot): Snapshot => ({ ...s, formulas: s.formulas.map((r) => r.map((v: CellValue) => (typeof v === 'string' && v.startsWith('=') ? toLocaleFormula(v, 'SEMICOLON') : v))) });
  const sept = toRu(sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT));
  const colA: CellValue[] = Array(768).fill(null); colA[734] = 'Сентябрь 2026';
  const base = {
    target: { year: 2026, month: 10 }, columnA: colA, predecessor: sept, existing: null,
    population: [...SEPT_NMS, NEW_NM].map((n) => ({ nmId: n, name: `Товар ${n}` })), cogs: cogsSnapshot({ [NEW_NM]: 426.735 }), structure: septemberStructure(), rowFormats: septemberRowFormats(),
  };
  it('формулы листа в «;»-форме распознаются (нет TEMPLATE_MISMATCH); в запросах — формулы в «;»-форме', () => {
    const p = planMonthPrep({ ...base, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'ru_RU', anchorCol: 600 } });
    expect(p.status).toBe('PLAN_CREATE');
    expect(p.formulaStyle).toBe('SEMICOLON');
    const req = JSON.stringify(toStructureRequests(p, SHEET_ID));
    expect(req).toContain('WI771-WJ771-WL771-426,735');
    expect(req).toContain('SUM(FILTER(N771:VR771;MOD(COLUMN(N771:VR771)-COLUMN(N771);24)=0))');
    expect(req).not.toContain('LAST_CLOSED_DATE,');
  });
  it('та же «;»-книга, прочитанная как en_US, — TEMPLATE_MISMATCH (локаль обязательна)', () => {
    expect(planMonthPrep({ ...base, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'en_US', anchorCol: 600 } }).code).toBe('TEMPLATE_MISMATCH');
  });
  it('неподдержанная локаль — BLOCKED UNSUPPORTED_LOCALE; без структуры листа — STRUCTURE_UNAVAILABLE', () => {
    expect(planMonthPrep({ ...base, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'de_DE', anchorCol: 600 } }).code).toBe('UNSUPPORTED_LOCALE');
    expect(planMonthPrep({ ...base, structure: null, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'ru_RU', anchorCol: 600 } }).code).toBe('STRUCTURE_UNAVAILABLE');
  });
});

describe('вставка колонок и УФ прошлых месяцев: Sheets расширяет диапазоны, кончающиеся на колонке перед вставкой', () => {
  it('shiftColumnRefs: сдвигаются только ссылки правее точки вставки — как это делает сам Sheets', () => {
    expect(shiftColumnRefs('=$US737>$WB$736', 588, 24)).toBe('=$US737>$WZ$736');
    expect(shiftColumnRefs('=WEEKDAY(US737;2)>5', 588, 24)).toBe('=WEEKDAY(US737;2)>5');
    expect(shiftColumnRefs('=AND($B737<=$WB$736;WB737<>"")', 588, 24)).toBe('=AND($B737<=$WZ$736;WZ737<>"")');
    expect(shiftColumnRefs('=VP737+VQ737', 588, 24)).toBe('=VP737+WO737');                 // VP=588 стоит, VQ=589 едет
    expect(shiftColumnRefs('=SUM($WA:$WB)+SUM(A:B)', 588, 24)).toBe('=SUM($WY:$WZ)+SUM(A:B)');
    expect(shiftColumnRefs('=IF(A1="WB736";LOG10(WB1);LAST_CLOSED_DATE)', 588, 24)).toBe('=IF(A1="WB736";LOG10(WZ1);LAST_CLOSED_DATE)');
    expect(shiftColumnRefs('50', 588, 24)).toBe('50');
  });
  it('shiftColumnRefs: ссылка на другой лист — отказ (ничего не угадываем)', () => {
    expect(() => shiftColumnRefs("='Склад'!WB1>0", 588, 24)).toThrow();
  });
  it('cfInsertTrims: переиздаются ровно правила с диапазоном до VP; диапазоны не расширены, порядок сохранён, ссылки сдвинуты', () => {
    const rules = septemberCfRules();
    const { trims, unsafe } = cfInsertTrims(rules, 588, 24);
    expect(unsafe).toEqual([]);
    const expectIdx = rules.map((r, i) => (r.ranges.some((x) => x.endColumnIndex === 588 && (x.startColumnIndex ?? 0) < 588) ? i : -1)).filter((i) => i >= 0);
    expect(expectIdx).toHaveLength(2);
    expect(trims.map((t) => t.index)).toEqual(expectIdx);
    for (const t of trims) {
      expect(t.rule.ranges).toEqual(rules[t.index]!.ranges);                                 // ни одного диапазона во вставке
      expect(t.rule.ranges.some((x) => (x.endColumnIndex ?? 0) > 588 && (x.startColumnIndex ?? 0) < 612)).toBe(false);
    }
    const f = trims.map((t) => t.rule.booleanRule!.condition.values![0]!.userEnteredValue);
    expect(f).toEqual(['=$US737>$WZ$736', '=WEEKDAY(US737;2)>5']);
    expect(rules[expectIdx[0]!]!.booleanRule!.condition.values![0]!.userEnteredValue).toBe('=$US737>$WB$736'); // вход не мутирован
  });
  it('cfInsertTrims: диапазон правее вставки едет вместе с хвостом; через вставку — расширяется (это делает и Sheets)', () => {
    const r: ConditionalFormatRule = { ranges: [{ startColumnIndex: 587, endColumnIndex: 588 }, { startColumnIndex: 598, endColumnIndex: 600 }, { startColumnIndex: 10, endColumnIndex: 600 }], gradientRule: { midpoint: { type: 'NUMBER', value: '=$WB$736' } } };
    const { trims } = cfInsertTrims([r], 588, 24);
    expect(trims[0]!.rule.ranges).toEqual([{ startColumnIndex: 587, endColumnIndex: 588 }, { startColumnIndex: 622, endColumnIndex: 624 }, { startColumnIndex: 10, endColumnIndex: 624 }]);
    expect((trims[0]!.rule.gradientRule as { midpoint: { value: string } }).midpoint.value).toBe('=$WZ$736');
  });
  it('cfInsertTrims: нет правил до VP — нечего переиздавать; небезопасная формула — в unsafe', () => {
    expect(cfInsertTrims(septemberCfRules(), 600, 24).trims).toEqual([]);
    const bad: ConditionalFormatRule = { ranges: [{ startColumnIndex: 587, endColumnIndex: 588 }], booleanRule: { condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: "='Склад'!WB1>0" }] } } };
    expect(cfInsertTrims([bad], 588, 24).unsafe).toHaveLength(1);
  });
});
