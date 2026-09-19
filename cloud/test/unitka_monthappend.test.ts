/**
 * UNITKA CALENDAR V2 (final polish, F4) — дописывание нового SKU в уже созданный текущий месяц.
 * Книга — синтетическая: сентябрь (24 блока, как живой) → октябрь, созданный Calendar V2 (25 блоков, VQ..WM).
 * Никаких реальных SKU и фактов: nm_id 9000000xx — структурные фикстуры.
 */
import { describe, it, expect } from 'vitest';
import { planMonthPrep, toStructureRequests, type PrepInputs } from '../src/loaders/unitka/monthprep.js';
import { planMonthAppend, planAppendRollback, qualifyMidMonth, sectionCfIndexes, type AppendInputs } from '../src/loaders/unitka/monthappend.js';
import { buildConditionalFormats } from '../src/loaders/unitka/visual.js';
import { geometryAt, slotStart, type MonthKey } from '../src/loaders/unitka/calendar.js';
import { OFFSET, SUMMARY, colA1, addDaysIso, isoToSerial, type CellValue } from '../src/loaders/unitka/model.js';
import { buildPlan, cellAt, formulaAt, validateSection, type Snapshot } from '../src/loaders/unitka/plan.js';
import type { FactRow } from '../src/loaders/unitka/bq.js';
import type { SheetStructure } from '../src/loaders/unitka/sheets.js';
import {
  sectionFromSpec, septemberSpec, applyPlan, applyCells, insertColumnsInto, deleteColumnsFrom, cogsSnapshot, septemberStructure, septemberRowFormats,
  SEPT_NMS, NEW_NM, WIDTH_SEPT, SHEET_ID,
} from './unitka_calendar_fixture.js';
import { logistics, commission } from './unitka_fixture.js';

const OCT: MonthKey = { year: 2026, month: 10 };
const G = geometryAt(OCT, 769);
const FIX_A = 900000011, FIX_B = 900000007;            // структурные фикстуры, не реальные SKU
const LCD = '2026-10-12';
const pop = (extra: number[] = [], drop: number[] = []) => [...SEPT_NMS, NEW_NM, ...extra].filter((n) => !drop.includes(n)).map((nmId) => ({ nmId, name: `Товар ${nmId}` }));
const cogs = cogsSnapshot({ [NEW_NM]: 426.735, [FIX_A]: 111.5, [FIX_B]: 222.25 });

/** Октябрь, созданный генератором из сентября (25 блоков, 23 вставленные колонки, якорь WY = 623). */
function octoberBook(): { section: Snapshot; structure: SheetStructure; width: number } {
  const sept = sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT);
  const colA: CellValue[] = Array(768).fill(null); colA[734] = 'Сентябрь 2026';
  const inp: PrepInputs = {
    target: OCT, meta: { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, anchorCol: 600 }, columnA: colA, predecessor: sept, existing: null,
    population: pop(), cogs, structure: septemberStructure(), rowFormats: septemberRowFormats(),
  };
  const plan = planMonthPrep(inp);
  if (plan.status !== 'PLAN_CREATE') throw new Error(`фикстура: ${plan.status} ${plan.code}`);
  const width = 600 + plan.insertColumns!.count;
  const st = septemberStructure(803, width);
  st.conditionalFormats = [...st.conditionalFormats, ...plan.conditionalFormats!.rules];
  // книга после подготовки месяца: у блока 25 своя группа, первая колонка хвоста (WN) выведена из группы хвоста.
  st.columnGroups = [...st.columnGroups.filter((x) => x.startIndex !== width - 12), { startIndex: 589 + 16 - 1, endIndex: 589 + 22, depth: 1 }, { startIndex: width - 11, endIndex: width - 2, depth: 1 }];
  return { section: { ...applyPlan(plan, width), anchorCol: width }, structure: st, width };
}
const columnA = (): CellValue[] => { const a: CellValue[] = Array(803).fill(null); a[734] = 'Сентябрь 2026'; a[768] = 'Октябрь 2026'; return a; };

/** Плотный слой фактов (как V_UNITKA_DAILY_FACT): активный SKU × каждый закрытый день; у фикстур — нули до активации. */
function facts(nms: readonly number[], lcd: string, act: Record<number, { stockFrom?: string; orderOn?: string }> = {}): FactRow[] {
  const out: FactRow[] = [];
  for (const nm of nms) for (let d = '2026-10-01'; d <= lcd; d = addDaysIso(d, 1)) {
    const a = act[nm];
    const fixture = nm === FIX_A || nm === FIX_B;
    out.push({
      nmId: nm, date: d, views: fixture ? 0 : 100, opens: fixture ? 0 : 10, carts: fixture ? 0 : 3, cancels: 0, adsIn: fixture ? 0 : 5.5, price: fixture ? null : 990.5, storage: fixture ? null : 1.25,
      orders: fixture ? (a?.orderOn === d ? 1 : 0) : 2, stock: fixture ? (a?.stockFrom ? (d >= a.stockFrom ? 40 : 0) : null) : 50, ordersSource: 'SYNTH', cancelsSource: 'SYNTH',
    });
  }
  return out;
}
function inputs(over: Partial<AppendInputs> = {}, book = octoberBook()): AppendInputs {
  return {
    target: OCT, meta: { sheetId: SHEET_ID, rowCount: 803, columnCount: book.width, anchorCol: book.width }, columnA: columnA(), existing: book.section,
    population: pop([FIX_A]), cogs, structure: book.structure, rowFormats: septemberRowFormats(book.width, [769, 770, 771, 802, 803]),
    facts: facts([...SEPT_NMS, NEW_NM, FIX_A], LCD, { [FIX_A]: { stockFrom: '2026-10-09' } }), lcd: LCD, ...over,
  };
}

describe('квалификация SKU для дописывания в текущий месяц', () => {
  const rep = new Set([...SEPT_NMS, NEW_NM]);
  const q = (act: Record<number, { stockFrom?: string; orderOn?: string }>, extra = [FIX_A, FIX_B]) =>
    qualifyMidMonth({ population: pop(extra), represented: rep, facts: facts([...SEPT_NMS, NEW_NM, ...extra], LCD, act), target: OCT, lcd: LCD });
  it('только справочник — без остатка, заказов и активности — НЕ основание: SKU ждёт', () => {
    expect(q({})).toEqual({ qualified: [], waiting: [FIX_B, FIX_A] });
  });
  it('появился остаток → основание STOCK с датой; первый заказ при отсутствии снимка остатков → основание ORDERS', () => {
    const r = q({ [FIX_A]: { stockFrom: '2026-10-09' }, [FIX_B]: { orderOn: '2026-10-11' } });
    expect(r.qualified).toEqual([{ nmId: FIX_B, name: `Товар ${FIX_B}`, qualifiedBy: 'ORDERS', firstDay: '2026-10-11' }, { nmId: FIX_A, name: `Товар ${FIX_A}`, qualifiedBy: 'STOCK', firstDay: '2026-10-09' }]);
    expect(r.waiting).toEqual([]);
  });
  it('уже представленный блоком SKU и факты позже LAST_CLOSED_DATE / другого месяца не считаются', () => {
    expect(qualifyMidMonth({ population: pop(), represented: rep, facts: facts([...SEPT_NMS, NEW_NM], LCD), target: OCT, lcd: LCD }).qualified).toEqual([]);
    const late = facts([FIX_A], '2026-10-20', { [FIX_A]: { stockFrom: '2026-10-15' } });
    expect(qualifyMidMonth({ population: pop([FIX_A]), represented: rep, facts: late, target: OCT, lcd: LCD }).waiting).toEqual([FIX_A]);
    expect(qualifyMidMonth({ population: pop([FIX_A]), represented: rep, facts: late, target: { year: 2026, month: 11 }, lcd: '2026-11-05' }).waiting).toEqual([FIX_A]);
  });
  it('два новых SKU в один день — порядок по возрастанию nm_id', () => {
    expect(q({ [FIX_A]: { stockFrom: '2026-10-05' }, [FIX_B]: { stockFrom: '2026-10-05' } }).qualified.map((c) => c.nmId)).toEqual([FIX_B, FIX_A]);
  });
});

describe('план дописывания: только добавление в конец, существующее не трогается', () => {
  const book = octoberBook();
  const plan = planMonthAppend(inputs({}, book));
  it('SKU с остатком → PLAN_APPEND: блок 26 сразу за блоком 25; вставка «разделитель + тело» = 24 колонки; якорь едет', () => {
    expect([plan.status, plan.code]).toEqual(['PLAN_APPEND', null]);
    expect(plan.candidates).toEqual([{ nmId: FIX_A, name: `Товар ${FIX_A}`, qualifiedBy: 'STOCK', firstDay: '2026-10-09' }]);
    expect(plan.blocks).toHaveLength(26);
    expect(plan.blocks.slice(0, 25).map((b) => [b.slot, b.nmId])).toEqual([...SEPT_NMS, NEW_NM].map((n, i) => [i, n]));   // порядок неизменен
    expect([plan.blocks[25]!.slot, colA1(plan.blocks[25]!.start), plan.blocks[25]!.origin]).toEqual([25, 'WO', 'NEW']);
    expect(plan.insertColumns).toEqual({ at: 611, count: 24 });
    expect(plan.anchorCol).toBe(623 + 24);
    expect(plan.layout!.lastBlockColumn).toBe(slotStart(25) + 22);
    expect(plan.appendRows).toBe(0);
  });
  it('новый блок: все 31 дата месяца, формулы, MTD, COGS из канона с заметкой; СПП и блогеры пусты', () => {
    const b = plan.blocks[25]!;
    const at = (row: number, off: number) => plan.cells.find((c) => c.row === row && c.col === b.start + off)?.value;
    for (let i = 0; i < 31; i++) expect(at(771 + i, OFFSET.date)?.kind).toBe('number');
    expect((at(771, OFFSET.unitProfit) as { text: string }).text).toContain('-111.5)');
    expect((at(772, OFFSET.stock) as { text: string }).text).toBe(`=IF($${colA1(b.start)}772>LAST_CLOSED_DATE,"",${colA1(b.start + 7)}771-${colA1(b.start + 4)}772+${colA1(b.start + 6)}771)`);
    expect(at(802, OFFSET.orders)?.kind).toBe('formula');
    expect(at(771, OFFSET.spp)).toBeUndefined();
    expect(at(771, OFFSET.bloggers)).toBeUndefined();
    expect(at(771, OFFSET.weekday)).toBeUndefined();                          // новый последний блок — без разделителя
    expect(plan.notes).toHaveLength(1);
    expect(plan.notes[0]!.note).toMatch(/111\.5.*дописывании SKU.*V_UNITKA_COGS_CANONICAL/);
  });
  it('прежний последний блок (25) получает колонку-разделитель WN: формула дня недели на каждый день', () => {
    for (let i = 0; i < 31; i++) expect((plan.cells.find((c) => c.row === 771 + i && c.col === 612)!.value as { text: string }).text).toBe(`=IF($VQ${771 + i}="","",CHOOSE(WEEKDAY($VQ${771 + i},2),"пн","вт","ср","чт","пт","сб","вс"))`);
  });
  it('сводка расширяется ровно на новый блок: C..K каждого дня и ДРР MTD; суммы SUM(C:C) не переписываются; «26 SKU»', () => {
    const c771 = plan.cells.find((c) => c.row === 771 && c.col === SUMMARY.bloggers)!.value as { text: string };
    expect(c771.text).toBe(`=IF($B771>LAST_CLOSED_DATE,"",SUM(FILTER(N771:${colA1(slotStart(25) + 1)}771,MOD(COLUMN(N771:${colA1(slotStart(25) + 1)}771)-COLUMN(N771),24)=0)))`);
    const summaryDay = plan.cells.filter((c) => c.col <= 12 && c.row >= 771 && c.row <= 801);
    expect(new Set(summaryDay.map((c) => c.col))).toEqual(new Set([3, 4, 5, 6, 7, 8, 9, 10, 11]));
    expect(summaryDay).toHaveLength(31 * 9);
    expect(plan.cells.filter((c) => c.col <= 12 && c.row === 802).map((c) => c.col)).toEqual([SUMMARY.drr]);   // SUM(C771:C801) не зависит от числа блоков
    expect(plan.cells.filter((c) => c.row === 770 && c.col <= 12).map((c) => (c.value as { value: string }).value)).toEqual(['Заказы 26 SKU', 'Положили в корзину 26 SKU']);
  });
  it('ни одной записи в тело существующих блоков: факты, формулы, ручные СПП и блогеры не адресуются — ни ячейками, ни запросами', () => {
    const oldEnd = 611;
    expect(plan.cells.filter((c) => c.col >= 13 && c.col <= oldEnd)).toEqual([]);
    const req = toStructureRequests(plan, SHEET_ID) as Array<Record<string, { start?: { rowIndex: number; columnIndex: number }; rows?: Array<{ values: unknown[] }>; range?: Record<string, number>; fields?: string }>>;
    for (const r of req) {
      const u = r.updateCells;
      if (!u?.start || u.fields !== 'userEnteredValue') continue;
      const first = u.start.columnIndex + 1, lastCol = first + u.rows![0]!.values.length - 1;
      expect(lastCol <= 12 || first > oldEnd, `значения ${colA1(first)}..${colA1(lastCol)} строки ${u.start.rowIndex + 1}`).toBe(true);
      expect(u.rows![0]!.values.every((x) => Object.keys(x as object).length > 0)).toBe(true);   // ни одной «пустой» записи
    }
    // форматы: полный формат — только правее прежней последней метрики; сама она (WM) — ТОЛЬКО границы.
    for (const r of req) {
      const rc = r.repeatCell;
      if (!rc?.range || rc.range.startRowIndex! < 768) continue;
      if (rc.fields === 'userEnteredFormat') expect(rc.range.startColumnIndex!).toBeGreaterThanOrEqual(oldEnd);
      else { expect(rc.fields).toBe('userEnteredFormat.borders'); expect([rc.range.startColumnIndex, rc.range.endColumnIndex]).toEqual([oldEnd - 1, oldEnd]); }
    }
    expect(JSON.stringify(req)).not.toMatch(/copyPaste|PASTE_|"deleteDimension"|appendDimension/);
  });
  it('запросы: вставка → замена правил УФ секции (старые 108 удаляются по убыванию, новые 111 с новым якорем) → размеры → группа', () => {
    const req = toStructureRequests(plan, SHEET_ID);
    const kinds = req.map((r) => Object.keys(r)[0]);
    expect(kinds[0]).toBe('insertDimension');
    expect(req[0]).toEqual({ insertDimension: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 611, endIndex: 635 }, inheritFromBefore: true } });
    const del = req.filter((r) => 'deleteConditionalFormatRule' in r).map((r) => (r.deleteConditionalFormatRule as { index: number }).index);
    expect(del).toHaveLength(108);
    expect(del).toEqual([...del].sort((a, b) => b - a));
    expect(Math.min(...del)).toBe(book.structure.conditionalFormats.length - 108);
    const add = req.filter((r) => 'addConditionalFormatRule' in r);
    expect(add).toHaveLength(111);
    expect((add[0]!.addConditionalFormatRule as { index: number }).index).toBe(book.structure.conditionalFormats.length - 108);
    expect(JSON.stringify(add)).toContain(`$${colA1(647)}$736`);
    expect(JSON.stringify(add)).not.toContain('$WY$736');
    expect(kinds.indexOf('deleteConditionalFormatRule')).toBeLessThan(kinds.indexOf('addConditionalFormatRule'));
    expect(plan.tailGroupDetachedColumn).toBeNull();                              // книга после Calendar V2: хвост уже отделён от группы
    // группа блока 25 кончается ровно на точке вставки — Sheets растянул бы её на новый блок: сначала вывод вставки из неё.
    expect(plan.groupRequests).toEqual([{ deleteDimensionGroup: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 611, endIndex: 635 } } }, { addDimensionGroup: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: slotStart(25) + 16 - 1, endIndex: slotStart(25) + 22 } } }]);
    const cols = plan.dimensionRequests.map((r) => (r.updateDimensionProperties as { range: { startIndex: number; dimension: string } }).range);
    expect(cols.every((d) => d.dimension === 'COLUMNS')).toBe(true);                       // строки месяца не трогаются
    expect(cols.filter((d) => d.startIndex >= 611).map((d) => d.startIndex + 1)).toEqual(Array.from({ length: 24 }, (_, i) => 612 + i));
  });
  it('после дописывания: секция проходит контракт, 26 блоков, повтор — NO_CHANGE; Engine читает все 26 блоков', () => {
    const after = applyCells(insertColumnsInto(book.section, 611, 24), plan.cells);
    const v = validateSection(after);
    expect([...v.sectionIssues, ...v.driftIssues]).toEqual([]);
    expect(v.blocks.map((b) => b.nmId)).toEqual([...SEPT_NMS, NEW_NM, FIX_A]);
    const st2 = { ...book.structure, conditionalFormats: [...book.structure.conditionalFormats.slice(0, -108), ...plan.conditionalFormats!.rules] };
    const again = planMonthAppend(inputs({ existing: after, structure: st2, meta: { sheetId: SHEET_ID, rowCount: 803, columnCount: 647, anchorCol: 647 }, rowFormats: septemberRowFormats(647, [769, 770, 771, 802, 803]) }));
    expect([again.status, again.code, again.cells.length]).toEqual(['NO_CHANGE', null, 0]);
    expect(toStructureRequests(again, SHEET_ID)).toEqual([]);
    const nms = [...SEPT_NMS, NEW_NM, FIX_A];
    const ep = buildPlan({ snapshot: { ...after, mirrorLcd: isoToSerial('2026-10-11'), namedLcd: isoToSerial('2026-10-11') }, lcd: { lastClosedDate: LCD, d1Msk: addDaysIso(LCD, 1) }, facts: facts(nms, LCD, { [FIX_A]: { stockFrom: '2026-10-09' } }), logistics: logistics(), commission: commission(), minN: 10, maxLagDays: 2 });
    const b26 = ep.cells.filter((c) => c.kind === 'fact' && c.col >= slotStart(25));
    expect(new Set(b26.map((c) => c.row)).size).toBe(12);                                  // 01..12.10: строки нового блока пишутся, в т.ч. дни до активации
    expect(b26.find((c) => c.row === 771 && c.col === slotStart(25) + OFFSET.stock)!.want).toBe(0);
    expect(b26.find((c) => c.row === 779 && c.col === slotStart(25) + OFFSET.stock)!.want).toBe(40);
  });
});

describe('дописывание: NO_CHANGE и отказы (fail-closed, 0 запросов)', () => {
  const none = (p: { status: string; code: string | null; cells: unknown[] }, status: string, code: string | null) => { expect([p.status, p.code, p.cells.length]).toEqual([status, code, 0]); };
  it('все активные SKU уже представлены → NO_CHANGE; SKU только в справочнике → NO_CHANGE и список ожидающих', () => {
    none(planMonthAppend(inputs({ population: pop() })), 'NO_CHANGE', null);
    const p = planMonthAppend(inputs({ facts: facts([...SEPT_NMS, NEW_NM, FIX_A], LCD) }));
    none(p, 'NO_CHANGE', null);
    expect(p.waiting).toEqual([FIX_A]);
    expect(p.unmappedActive).toEqual([FIX_A]);
  });
  it('первый заказ без снимка остатков → дописывается (основание ORDERS)', () => {
    const p = planMonthAppend(inputs({ facts: facts([...SEPT_NMS, NEW_NM, FIX_A], LCD, { [FIX_A]: { orderOn: '2026-10-10' } }) }));
    expect([p.status, p.candidates[0]?.qualifiedBy, p.candidates[0]?.firstDay]).toEqual(['PLAN_APPEND', 'ORDERS', '2026-10-10']);
  });
  it('два новых SKU в один прогон — по возрастанию nm_id: слоты 25 и 26, вставка 48 колонок', () => {
    const p = planMonthAppend(inputs({ population: pop([FIX_A, FIX_B]), facts: facts([...SEPT_NMS, NEW_NM, FIX_A, FIX_B], LCD, { [FIX_A]: { stockFrom: '2026-10-03' }, [FIX_B]: { orderOn: '2026-10-12' } }) }));
    expect(p.blocks.slice(25).map((b) => [b.nmId, b.slot])).toEqual([[FIX_B, 25], [FIX_A, 26]]);
    expect(p.insertColumns).toEqual({ at: 611, count: 48 });
    expect(p.cells.some((c) => c.row === 771 && c.col === slotStart(25) + 23)).toBe(true);     // между новыми блоками разделитель есть
    expect(p.cells.some((c) => c.col > slotStart(26) + 22)).toBe(false);                       // за последним — ничего
  });
  it('SKU выбыл из справочника, но блок в месяце есть → блок остаётся на месте, порядок прежний, дописывание идёт в конец', () => {
    const gone = SEPT_NMS[5]!;
    const p = planMonthAppend(inputs({ population: pop([FIX_A], [gone]) }));
    expect(p.status).toBe('PLAN_APPEND');
    expect(p.blocks.map((b) => b.nmId)).toEqual([...SEPT_NMS, NEW_NM, FIX_A]);
    expect(p.blocks[5]).toMatchObject({ nmId: gone, slot: 5, origin: 'CARRIED' });
  });
  it('не текущий месяц LCD → NO_CHANGE; слой фактов не прочитан → NO_CHANGE APPEND_FACTS_UNAVAILABLE', () => {
    none(planMonthAppend(inputs({ lcd: '2026-11-02' })), 'NO_CHANGE', 'APPEND_NOT_CURRENT_MONTH');
    none(planMonthAppend(inputs({ facts: null })), 'NO_CHANGE', 'APPEND_FACTS_UNAVAILABLE');
  });
  it('дыра в цепочке блоков → BLOCKED CHAIN_GAP', () => {
    const book = octoberBook();
    // блок слота 10 исчез из секции целиком (нет ни заголовка, ни шапки): цепочка 0..9, 11..24 — с дырой.
    const wipe = Array.from({ length: 24 }, (_, o) => [769, 770].map((row) => ({ row, col: slotStart(10) + o, value: { kind: 'string' as const, value: '' } }))).flat();
    none(planMonthAppend(inputs({ existing: applyCells(book.section, wipe) }, book)), 'BLOCKED', 'CHAIN_GAP');
  });
  it('следующий месяц уже создан (под секцией есть строки) → BLOCKED LATER_SECTION_EXISTS', () => {
    none(planMonthAppend(inputs({ meta: { sheetId: SHEET_ID, rowCount: 838, columnCount: 623, anchorCol: 623 } })), 'BLOCKED', 'LATER_SECTION_EXISTS');
  });
  it('хвост книги не там, где его ждёт цепочка → BLOCKED TAIL_GEOMETRY_UNKNOWN', () => {
    none(planMonthAppend(inputs({ meta: { sheetId: SHEET_ID, rowCount: 803, columnCount: 640, anchorCol: 640 } })), 'BLOCKED', 'TAIL_GEOMETRY_UNKNOWN');
  });
  it('формула секции не воспроизводится построителем → BLOCKED TEMPLATE_MISMATCH (расширение сводки «не понято»)', () => {
    const book = octoberBook();
    const s = applyCells(book.section, [{ row: 801, col: SUMMARY.orders, value: { kind: 'formula', text: '=SUM(Q801:VU801)' } }]);
    none(planMonthAppend(inputs({ existing: s }, book)), 'BLOCKED', 'TEMPLATE_MISMATCH');
  });
  it('правила УФ секции не те, что строит генератор → BLOCKED CF_SECTION_DRIFT', () => {
    const book = octoberBook();
    const st = { ...book.structure, conditionalFormats: book.structure.conditionalFormats.slice(0, -1) };
    none(planMonthAppend(inputs({ structure: st }, book)), 'BLOCKED', 'CF_SECTION_DRIFT');
  });
  it('нет канона COGS нового SKU → BLOCKED COGS_MISSING; дубль nm_id → DUPLICATE_SKU; нет структуры/форматов → отказ', () => {
    none(planMonthAppend(inputs({ cogs: cogsSnapshot({ [NEW_NM]: 426.735 }) })), 'BLOCKED', 'COGS_MISSING');
    none(planMonthAppend(inputs({ population: [...pop([FIX_A]), { nmId: FIX_A, name: 'x' }] })), 'BLOCKED', 'DUPLICATE_SKU');
    none(planMonthAppend(inputs({ structure: null })), 'BLOCKED', 'STRUCTURE_UNAVAILABLE');
    none(planMonthAppend(inputs({ rowFormats: null })), 'BLOCKED', 'TEMPLATE_FORMATS_UNAVAILABLE');
  });
  it('секция месяца не найдена → BLOCKED MONTH_SECTION_MISSING (месяц создаёт подготовка, не дописывание)', () => {
    none(planMonthAppend(inputs({ existing: null })), 'BLOCKED', 'MONTH_SECTION_MISSING');
  });
});

describe('откат дописывания: секция и сводка возвращаются к прежнему виду', () => {
  const book = octoberBook();
  const plan = planMonthAppend(inputs({}, book));
  const after = applyCells(insertColumnsInto(book.section, 611, 24), plan.cells);
  const stAfter: SheetStructure = { ...book.structure, conditionalFormats: [...book.structure.conditionalFormats.slice(0, -108), ...plan.conditionalFormats!.rules] };
  const rbInputs = { target: OCT, meta: { sheetId: SHEET_ID, rowCount: 803, columnCount: 647, anchorCol: 647 }, columnA: columnA(), existing: after, structure: stAfter, removeNmIds: [FIX_A], insertedColumns: [612, 635] as [number, number], insertedColumnsEmptyAbove: true };
  it('запросы: удалить УФ секции → удалить колонки 612..635 → сводка и подписи 25 SKU → границы WM → УФ прежней раскладки с прежним якорем', () => {
    const rb = planAppendRollback(rbInputs);
    expect([rb.status, rb.code, rb.anchorCol, rb.blocksAfter]).toEqual(['PLAN_ROLLBACK', null, 623, 25]);
    const kinds = rb.requests.map((r) => Object.keys(r)[0]);
    expect(kinds.filter((k) => k === 'deleteConditionalFormatRule')).toHaveLength(111);
    expect(rb.requests[111]).toEqual({ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 611, endIndex: 635 } } });
    expect(kinds.filter((k) => k === 'addConditionalFormatRule')).toHaveLength(108);
    const want = buildConditionalFormats(validateSection(book.section).layout, SHEET_ID, 'COMMA', 623).rules;
    expect(rb.requests.filter((r) => 'addConditionalFormatRule' in r).map((r) => (r.addConditionalFormatRule as { rule: unknown }).rule)).toEqual(want);
    expect(sectionCfIndexes(stAfter.conditionalFormats, G)).toHaveLength(111);
  });
  it('исполнение отката в памяти возвращает секцию ячейка в ячейку (значения и формулы)', () => {
    const rb = planAppendRollback(rbInputs);
    let s = deleteColumnsFrom(after, 612, 635);
    const cells = rb.requests.filter((r) => 'updateCells' in r).flatMap((r) => {
      const u = r.updateCells as { start: { rowIndex: number; columnIndex: number }; rows: Array<{ values: Array<{ userEnteredValue?: { formulaValue?: string; stringValue?: string } }> }> };
      return u.rows[0]!.values.map((x, i) => ({ row: u.start.rowIndex + 1, col: u.start.columnIndex + 1 + i, value: x.userEnteredValue!.formulaValue !== undefined ? { kind: 'formula' as const, text: x.userEnteredValue!.formulaValue } : { kind: 'string' as const, value: x.userEnteredValue!.stringValue! } }));
    });
    s = applyCells(s, cells);
    expect(s.grid).toEqual(book.section.grid);
    expect(s.formulas).toEqual(book.section.formulas);
    expect(cellAt(s, 770, SUMMARY.orders)).toBe('Заказы 25 SKU');
    expect(formulaAt(s, 771, SUMMARY.bloggers)).toBe(formulaAt(book.section, 771, SUMMARY.bloggers));
  });
  it('отказы: не те блоки в конце, не те колонки, колонки непусты выше секции, дрейф УФ', () => {
    expect(planAppendRollback({ ...rbInputs, removeNmIds: [NEW_NM] }).code).toBe('APPEND_ROLLBACK_MISMATCH');
    expect(planAppendRollback({ ...rbInputs, insertedColumns: [613, 635] }).code).toBe('APPEND_ROLLBACK_MISMATCH');
    expect(planAppendRollback({ ...rbInputs, insertedColumnsEmptyAbove: false }).code).toBe('APPEND_ROLLBACK_UNSAFE');
    expect(planAppendRollback({ ...rbInputs, structure: { ...stAfter, conditionalFormats: stAfter.conditionalFormats.slice(0, -2) } }).code).toBe('CF_SECTION_DRIFT');
    expect(planAppendRollback({ ...rbInputs, removeNmIds: [] }).requests).toEqual([]);
  });
});
