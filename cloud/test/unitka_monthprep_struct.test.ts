/**
 * UNITKA CALENDAR V2 (Phase 2C) — локаль формул, размеры, откат (УФ — unitka_visual.test.ts).
 * Эталоны локали — строки, прочитанные Sheets API из тестовой копии книги (ru_RU) 19.09.2026.
 */
import { describe, it, expect } from 'vitest';
import { formulaStyleOf, fromLocaleFormula, toLocaleFormula } from '../src/loaders/unitka/formulas.js';
import { cfInsertTrims, tailGroupDetach, groupInsertTrims, planDimensions, blockColumnWidth, summaryColumnWidth, heightForFont, estimateWrappedLines, planMonthRollback, shiftColumnRefs } from '../src/loaders/unitka/monthprep_struct.js';
import { layoutOf, slotStart } from '../src/loaders/unitka/calendar.js';
import { OFFSET, SUMMARY } from '../src/loaders/unitka/model.js';
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

describe('контракт размеров (F3): ширины по смыслу колонки, высоты по типу строки и кеглю', () => {
  type Dim = { range: { dimension: string; startIndex: number; endIndex: number }; properties: { pixelSize?: number; hiddenByUser?: boolean }; fields: string };
  const dims = (p: { requests: Array<Record<string, unknown>> }): Dim[] => p.requests.map((r) => r.updateDimensionProperties as Dim);
  const lay = (n: number, top = 769, key = { year: 2026, month: 10 }) => layoutOf(geometryAt(key, top), Array.from({ length: n }, (_, i) => ({ index: i, slot: i, start: slotStart(i), nmId: 100 + i, title: `${100 + i}` })));
  const headers = new Map<number, string>([[3, 'Блогеры + самовыкупы '], [13, 'Дата'], [13 + OFFSET.adsOut, 'Внешняя реклама (затраты на блогеров)']]);
  const planFor = (n: number, inserted: { at: number; count: number } | null, st = septemberStructure()) =>
    planDimensions({ structure: st, prev: SEPT, next: geometryAt({ year: 2026, month: 10 }, 769), layout: lay(n), inserted, templateSlot: 23, templateFormats: septemberRowFormats(), headerTexts: headers, sheetId: SHEET_ID, rows: true });

  it('ширина — по СМЫСЛУ колонки: итоги в деньгах шире (104/100), разделитель узкий (56), остальное 84; сводка — как в живой книге', () => {
    expect(blockColumnWidth(OFFSET.profitAll)).toBe(104);
    expect(blockColumnWidth(OFFSET.adsIn)).toBe(100);
    expect(blockColumnWidth(OFFSET.weekday)).toBe(56);
    for (const o of [OFFSET.date, OFFSET.bloggers, OFFSET.views, OFFSET.orders, OFFSET.stock, OFFSET.price, OFFSET.spp, OFFSET.commission, OFFSET.unitProfit]) expect(blockColumnWidth(o)).toBe(84);
    expect([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map(summaryColumnWidth)).toEqual([73, 73, 92, 102, 82, 85, 85, 85, 104, 100, 78, 73]);
    expect(summaryColumnWidth(SUMMARY.profit)).toBe(blockColumnWidth(OFFSET.profitAll));     // один смысл — одна ширина
  });
  it('высота по кеглю: 20 пт → 40, 16 пт → 33, 10 пт → 22; перенос шапки оценивается по словам', () => {
    expect([20, 16, 12, 10].map(heightForFont)).toEqual([40, 33, 26, 22]);
    expect(estimateWrappedLines('Дата', 84, 12)).toBe(1);
    expect(estimateWrappedLines('Внешняя реклама (затраты на блогеров)', 84, 12)).toBe(5);
    expect(estimateWrappedLines('Оборачиваемость (дн)', 84, 12)).toBe(3);
    expect(estimateWrappedLines('', 84, 12)).toBe(0);
  });
  it('вставленные колонки (блок 25, VQ..WM): ширина = контракт, скрытие = как у блока-шаблона (16–22), колонки-сироты нет', () => {
    const cols = dims(planFor(25, { at: 588, count: 23 })).filter((d) => d.range.dimension === 'COLUMNS' && d.range.startIndex >= 588);
    expect(cols.map((c) => c.range.startIndex + 1)).toEqual(Array.from({ length: 23 }, (_, o) => 589 + o));
    expect(cols.every((c) => c.fields === 'pixelSize,hiddenByUser')).toBe(true);
    expect(cols.map((c) => c.properties.pixelSize)).toEqual(Array.from({ length: 23 }, (_, o) => blockColumnWidth(o)));
    expect(cols.filter((c) => c.properties.hiddenByUser).map((c) => c.range.startIndex + 1 - 589)).toEqual([16, 17, 18, 19, 20, 21, 22]);
  });
  it('вставка «разделитель + тело» (книга после Calendar V2, блок 26): разделитель WN виден и узок, тело — по контракту', () => {
    const st = septemberStructure(803, 623);
    const p = planDimensions({ structure: st, prev: geometryAt({ year: 2026, month: 10 }, 769), next: geometryAt({ year: 2026, month: 11 }, 804), layout: lay(26, 804, { year: 2026, month: 11 }), inserted: { at: 611, count: 24 }, templateSlot: 24, templateFormats: null, headerTexts: headers, sheetId: SHEET_ID, rows: false });
    const cols = dims(p).filter((d) => d.range.startIndex >= 611);
    expect(cols[0]).toMatchObject({ range: { startIndex: 611, endIndex: 612 }, properties: { pixelSize: 56, hiddenByUser: false } });
    expect(cols.slice(1).map((c) => c.properties.pixelSize)).toEqual(Array.from({ length: 23 }, (_, o) => blockColumnWidth(o)));
    expect(dims(p).some((d) => d.range.dimension === 'ROWS')).toBe(false);
  });
  it('существующие колонки — ТОЛЬКО расширение до контракта: поле pixelSize, скрытие не трогается; уже широкие — без запроса', () => {
    const p = planFor(25, { at: 588, count: 23 });
    const up = dims(p).filter((d) => d.range.dimension === 'COLUMNS' && d.range.startIndex < 588);
    expect(up.every((d) => d.fields === 'pixelSize' && d.properties.hiddenByUser === undefined)).toBe(true);
    // фикстура: даты 80 px (< 84), «доходность (общая)» 100 px (< 104); остальное 100 px — не уже контракта.
    expect(p.widthUpgrades.filter((u) => u.col >= 13)).toHaveLength(24 * 2);
    expect(new Set(p.widthUpgrades.filter((u) => u.col >= 13).map((u) => (u.col - 13) % 24))).toEqual(new Set([OFFSET.date, OFFSET.profitAll]));
    expect(p.widthUpgrades.filter((u) => u.col < 13)).toEqual([{ col: SUMMARY.views, from: 100, to: 102 }, { col: SUMMARY.profit, from: 100, to: 104 }]);
    expect(p.widthUpgrades.every((u) => u.to > u.from)).toBe(true);
    expect(up.map((d) => d.range.startIndex + 1).sort((a, b) => a - b)).toEqual(p.widthUpgrades.map((u) => u.col).sort((a, b) => a - b));
    // повтор на уже расширенной книге — ни одного запроса к существующим колонкам.
    const wide = septemberStructure(); wide.columnMetadata.forEach((c) => { c.pixelSize = 120; });
    expect(planFor(25, { at: 588, count: 23 }, wide).widthUpgrades).toEqual([]);
  });
  it('строки секции: заголовок 40 (кегль 20), шапка ≥ шаблона и вмещает перенос, дни как в шаблоне, MTD 33 (кегль 16), план как в шаблоне', () => {
    const p = planFor(25, { at: 588, count: 23 });
    const rows = dims(p).filter((d) => d.range.dimension === 'ROWS').map((d) => [d.range.startIndex + 1, d.range.endIndex, d.properties.pixelSize]);
    expect(rows).toEqual([[769, 769, 40], [770, 770, p.rowHeights.header], [771, 771, 18], [772, 801, 18], [802, 802, 33], [803, 803, 18]]);
    expect(p.rowHeights.header).toBeGreaterThanOrEqual(5 * 20 + 8);       // 5 строк переноса «Внешняя реклама (затраты на блогеров)»
    expect(p.rowHeights.header).toBeLessThanOrEqual(120);
    expect(p.rowHeights).toMatchObject({ title: 40, dayFirst: 18, day: 18, mtd: 33, plan: 18 });
  });
  it.each([[2027, 2, 28], [2028, 2, 29], [2026, 11, 30], [2026, 10, 31]])('месяц %i-%i (%i дн.): те же высоты по типу строки, полоса дней до последнего дня — без координат октября', (y, m, days) => {
    const next = geometryAt({ year: y, month: m }, 1000);
    const p = planDimensions({ structure: septemberStructure(1000 + days + 3, 600), prev: SEPT, next, layout: layoutOf(next, lay(24).blocks), inserted: null, templateSlot: 23, templateFormats: septemberRowFormats(), headerTexts: headers, sheetId: SHEET_ID, rows: true });
    const rows = dims(p).filter((d) => d.range.dimension === 'ROWS').map((d) => [d.range.startIndex + 1, d.range.endIndex, d.properties.pixelSize]);
    expect(rows.map((r) => r[2])).toEqual([40, p.rowHeights.header, 18, 18, 33, 18]);
    expect(rows[3]).toEqual([1003, 1000 + days + 1, 18]);
    expect(rows[4]![0]).toBe(1000 + days + 2);
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

describe('группы колонок у конца цепочки: Sheets сливает стоящие вплотную группы одной глубины', () => {
  const groups = septemberStructure().columnGroups;          // у хвоста группа с его первой колонки: VQ..VZ (индексы 588..598)
  it('фикстура как живая книга: группа хвоста начинается с первой колонки хвоста', () => {
    expect(groups.some((g) => g.startIndex === 588 && g.endIndex === 598)).toBe(true);
  });
  it('у последнего блока нет разделителя → его группа (16..22) встала бы вплотную к группе хвоста: первая колонка хвоста выводится из группы', () => {
    expect(tailGroupDetach(groups, 589, 23, SHEET_ID)).toEqual({ column: 612, request: { deleteDimensionGroup: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 611, endIndex: 612 } } } });
    expect(tailGroupDetach(groups, 589, 47, SHEET_ID)!.column).toBe(636);
  });
  it('хвост уже отделён (группа начинается со второй колонки хвоста) или группы нет — ничего не делаем', () => {
    const detached = groups.map((g) => (g.startIndex === 588 ? { ...g, startIndex: 589 } : g));
    expect(tailGroupDetach(detached, 589, 24, SHEET_ID)).toBeNull();
    expect(tailGroupDetach([], 589, 23, SHEET_ID)).toBeNull();
    expect(tailGroupDetach(groups, 589, 0, SHEET_ID)).toBeNull();
  });
  it('откат возвращает колонку в группу хвоста: соседние группы сольются в исходную', () => {
    const p = planMonthRollback({ geometry: OCT, rowCount: 803, sheetId: SHEET_ID, rules: [], insertedColumns: [589, 611], newColumnsEmptyAbove: true, regroupTailColumn: 589, restoreWidths: [{ col: 23, from: 84, to: 104 }] });
    expect(p.requests.map((r) => Object.keys(r)[0])).toEqual(['deleteDimension', 'deleteDimension', 'updateDimensionProperties', 'addDimensionGroup']);
    expect(p.requests[2]).toEqual({ updateDimensionProperties: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 22, endIndex: 23 }, properties: { pixelSize: 84 }, fields: 'pixelSize' } });
    expect(p.requests[3]).toEqual({ addDimensionGroup: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 588, endIndex: 589 } } });
  });
});

describe('вставка колонок и группы: Sheets расширяет группу, кончающуюся ровно на колонке перед вставкой', () => {
  const g = (startIndex: number, endIndex: number) => ({ startIndex, endIndex, depth: 1 });
  it('группа блока 25 (605..611) кончается на точке вставки 611 → вставленные колонки выводятся из неё одним запросом на всю вставку', () => {
    expect(groupInsertTrims([g(580, 587), g(604, 611), g(612, 621)], 611, 24, SHEET_ID)).toEqual([{ deleteDimensionGroup: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 611, endIndex: 635 } } }]);
  });
  it('книга до Calendar V2: между группой блока 24 (…587) и вставкой (588) стоит колонка-разделитель → ничего не расширится, запросов нет', () => {
    expect(groupInsertTrims([g(580, 587), g(588, 598)], 588, 23, SHEET_ID)).toEqual([]);
    expect(groupInsertTrims([], 588, 23, SHEET_ID)).toEqual([]);
    expect(groupInsertTrims([g(604, 611)], 611, 0, SHEET_ID)).toEqual([]);
  });
  it('две вложенные группы, кончающиеся на точке вставки, — два снятия (по одному на уровень глубины)', () => {
    expect(groupInsertTrims([g(600, 611), g(604, 611)], 611, 24, SHEET_ID)).toHaveLength(2);
  });
});
