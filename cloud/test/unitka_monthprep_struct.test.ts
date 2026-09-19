/**
 * UNITKA CALENDAR V2 (Phase 2C) — локаль формул, перенос УФ, размеры, откат.
 * Эталоны локали — строки, прочитанные Sheets API из тестовой копии книги (ru_RU) 19.09.2026.
 */
import { describe, it, expect } from 'vitest';
import { formulaStyleOf, fromLocaleFormula, toLocaleFormula } from '../src/loaders/unitka/formulas.js';
import { carryConditionalFormats, dimensionRequests, planMonthRollback, remapCfFormula } from '../src/loaders/unitka/monthprep_struct.js';
import { planMonthPrep, toStructureRequests } from '../src/loaders/unitka/monthprep.js';
import { geometryAt, slotStart } from '../src/loaders/unitka/calendar.js';
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

describe('перенос условного форматирования', () => {
  const rules = septemberCfRules();
  const carry = carryConditionalFormats(rules, SEPT, OCT, Array.from({ length: 24 }, (_, i) => i), [25], SHEET_ID, rules.length);
  const added = carry.requests.map((r) => (r.addConditionalFormatRule as { rule: ConditionalFormatRule; index: number }));
  const formulaOf = (r: ConditionalFormatRule) => r.booleanRule?.condition.values?.[0]?.userEnteredValue;

  it('правила добавляются в конец, по порядку; все диапазоны — строки 771..801', () => {
    expect(added.map((a) => a.index)).toEqual(added.map((_, i) => rules.length + i));
    for (const a of added) for (const g of a.rule.ranges) expect([g.startRowIndex, g.endRowIndex]).toEqual([770, 801]);
  });
  it('per-block «будущий день»: 24 переносятся как есть + клон для блока 25 ($WO771>$WB$736, якорь не сдвигается)', () => {
    const fut = added.filter((a) => /^=\$[A-Z]+771>\$WB\$736$/.test(formulaOf(a.rule) ?? ''));
    expect(fut).toHaveLength(25);
    expect(formulaOf(fut[0]!.rule)).toBe('=$M771>$WB$736');
    const clone = fut.find((a) => formulaOf(a.rule) === '=$WO771>$WB$736')!;
    expect(clone.rule.ranges).toEqual([{ sheetId: SHEET_ID, startRowIndex: 770, endRowIndex: 801, startColumnIndex: slotStart(25) - 1, endColumnIndex: slotStart(25) + 5 }]);
  });
  it('правило на все блоки: блок 25 получает диапазон того же смещения; формула — только сдвиг строк', () => {
    const wk = added.find((a) => formulaOf(a.rule) === '=WEEKDAY(US771;2)>5')!;
    expect(wk.rule.ranges).toHaveLength(25);
    expect(wk.rule.ranges[24]).toMatchObject({ startColumnIndex: slotStart(25) + 23 - 1, endColumnIndex: slotStart(25) + 23 });
    const mx = added.find((a) => (formulaOf(a.rule) ?? '').includes('MAX('))!;
    expect(formulaOf(mx.rule)).toBe('=AND(UX771<>"";UX771>0,75*MAX(UX$771:UX$801))');
    expect(mx.rule.ranges.some((g) => g.startColumnIndex === slotStart(25) + 5 - 1)).toBe(true);
    expect(mx.rule.ranges.some((g) => g.startColumnIndex === 6)).toBe(true); // сводка G осталась
  });
  it('наследие: «август+сентябрь» — только сентябрьская часть; шапка 735..736 и частичный диапазон — не переносятся', () => {
    const cross = added.filter((a) => a.rule.gradientRule && a.rule.ranges.length === 1 && a.rule.ranges[0]!.startColumnIndex === slotStart(0) + 9 - 1);
    expect(cross).toHaveLength(1);
    expect(carry.skipped.map((x) => x.reason)).toEqual(expect.arrayContaining([
      expect.stringMatching(/шапка\/прошлые месяцы/), expect.stringMatching(/частичный диапазон/),
    ]));
    expect(carry.skipped).toHaveLength(2);
  });
  it('per-block шкала: 23 переноса + клон на блок 25; ничего не попадает в резервный слот', () => {
    const grad10 = added.filter((a) => a.rule.gradientRule && a.rule.ranges.every((g) => ((g.startColumnIndex ?? 0) + 1 - 13) % 24 === 10));
    expect(grad10).toHaveLength(24);
    for (const a of added) for (const g of a.rule.ranges) {
      const c = (g.startColumnIndex ?? 0) + 1;
      expect(c >= slotStart(24) && c < slotStart(25)).toBe(false);
    }
    expect(carry).toMatchObject({ carried: rules.length - 2, cloned: 2, extendedToNewBlocks: 2 });
  });
  it('ссылка на строку вне секции (кроме якорей WB736..739) — правило не переносится', () => {
    expect(remapCfFormula('=A700>0', SEPT, OCT)).toBeNull();
    expect(remapCfFormula('=$M737>$WB$736', SEPT, OCT)).toBe('=$M771>$WB$736');
    expect(remapCfFormula('=AND(M737<=TODAY();D737>0;F737=0)', SEPT, OCT)).toBe('=AND(M771<=TODAY();D771>0;F771=0)');
    expect(remapCfFormula('=LOG10(A737)', SEPT, OCT)).toBe('=LOG10(A771)');
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
  it('удаляет только УФ новой секции, колонки 601..636 и строки 769..803 — в этом порядке', () => {
    const p = planMonthRollback({ geometry: OCT, rowCount: 803, columnCount: 636, preColumnCount: 600, sheetId: SHEET_ID, rules, newColumnsEmptyAbove: true });
    expect(p.refused).toBeNull();
    expect(p.deletedCfRules).toBe(1);
    expect(p.requests[0]).toEqual({ deleteConditionalFormatRule: { sheetId: SHEET_ID, index: rules.length - 1 } });
    expect(p.requests[1]).toEqual({ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 600, endIndex: 636 } } });
    expect(p.requests[2]).toEqual({ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'ROWS', startIndex: 768, endIndex: 803 } } });
  });
  it('новые колонки непусты выше секции — отказ', () => {
    expect(planMonthRollback({ geometry: OCT, rowCount: 803, columnCount: 636, preColumnCount: 600, sheetId: SHEET_ID, rules, newColumnsEmptyAbove: false }).refused).toMatch(/непусты/);
  });
  it('правило «через границу» не удаляется: его диапазон в новой секции обрезает удаление строк (живой лист, Phase 2C)', () => {
    const straddle = [{ ranges: [{ startRowIndex: 736, endRowIndex: 766 }, { startRowIndex: 770, endRowIndex: 801 }] } as ConditionalFormatRule];
    const p = planMonthRollback({ geometry: OCT, rowCount: 803, columnCount: 636, preColumnCount: 600, sheetId: SHEET_ID, rules: straddle, newColumnsEmptyAbove: true });
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
    const p = planMonthPrep({ ...base, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'ru_RU' } });
    expect(p.status).toBe('PLAN_CREATE');
    expect(p.formulaStyle).toBe('SEMICOLON');
    const req = JSON.stringify(toStructureRequests(p, SHEET_ID));
    expect(req).toContain('XG771-XH771-XJ771-426,735');
    expect(req).toContain('SUM(FILTER(N771:WP771;MOD(COLUMN(N771:WP771)-COLUMN(N771);24)=0))');
    expect(req).not.toContain('LAST_CLOSED_DATE,');
  });
  it('та же «;»-книга, прочитанная как en_US, — TEMPLATE_MISMATCH (локаль обязательна)', () => {
    expect(planMonthPrep({ ...base, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'en_US' } }).code).toBe('TEMPLATE_MISMATCH');
  });
  it('неподдержанная локаль — BLOCKED UNSUPPORTED_LOCALE; без структуры листа — STRUCTURE_UNAVAILABLE', () => {
    expect(planMonthPrep({ ...base, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'de_DE' } }).code).toBe('UNSUPPORTED_LOCALE');
    expect(planMonthPrep({ ...base, structure: null, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, locale: 'ru_RU' } }).code).toBe('STRUCTURE_UNAVAILABLE');
  });
});
