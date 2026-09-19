/**
 * UNITKA CALENDAR V2 — дописывание нового SKU в УЖЕ СОЗДАННЫЙ текущий месяц (final polish, F4).
 *
 * Контракт (только добавление в конец):
 *   • порядок блоков месяца неизменен; существующие блоки не перестраиваются, не сдвигаются и не удаляются
 *     (в т.ч. блок SKU, выбывшего из справочника, — он остаётся на месте);
 *   • новый SKU дописывается ОДИН раз в конец цепочки: колонки вставляются перед хвостом книги (как при подготовке
 *     месяца), блок получает все даты месяца; дни до появления SKU остаются пустыми/нулевыми по фактам;
 *   • ручной ввод (СПП, блогеры), факты и формулы существующих блоков не трогаются: значения пишутся сериями только
 *     в новые колонки и в те формулы сводки, чей диапазон действительно расширился;
 *   • квалификация — не «есть в справочнике», а коммерческая значимость в этом месяце: остаток > 0 ИЛИ заказы > 0
 *     по V_UNITKA_DAILY_FACT (тот же слой, что читает Engine) на закрытую дату ≤ LAST_CLOSED_DATE;
 *   • несколько новых SKU сразу — по возрастанию nm_id (как при подготовке месяца);
 *   • повтор — NO_CHANGE; любой непонятый инвариант — BLOCKED без записи, авто-починки нет.
 *
 * ЧИСТЫЙ модуль: вход — снимок секции и структура листа, выход — план того же вида, что у подготовки месяца
 * (исполняется тем же toStructureRequests одним batchUpdate).
 */
import type { CellValue } from './model.js';
import { OFFSET, SUMMARY, colA1 } from './model.js';
import {
  BLOCK_BODY_WIDTH, BLOCK_WIDTH, chainGaps, formatMonthKey, geometryAt, insertGeometry, isLastBlock, blockLastColumn, layoutOf, locateSection,
  monthKeyOf, nextFreeSlot, sameMonth, slotStart, tailStartOf, dayRowOf, type MonthKey, type MonthLayout, type BlockSlot,
} from './calendar.js';
import {
  blockDayFormulas, blockProjectionFormulas, blockMtdFormulas, summaryDayFormulas, summaryMtdFormulas, normFormula, formulaStyleOf, fromLocaleFormula,
  type FormulaStyle,
} from './formulas.js';
import { cellAt, formulaAt, isFormula, validateSection, type Snapshot } from './plan.js';
import type { FactRow } from './bq.js';
import type { CogsSnapshot } from './integrity.js';
import type { ConditionalFormatRule, RawCellFormat, SheetMeta, SheetStructure, StructureRequest } from './sheets.js';
import { isoToSerial, addDaysIso } from './model.js';
import {
  BLOCK_TITLE_MERGE_WIDTH, GENERATED_STOCK_PROJECTION, cogsLiteral, extractPredecessorParams, resolveCanonicalCogs, assertPlanConfined, valueRunRequests,
  type CellWrite, type CellValueWrite, type GridRect, type MonthPrepPlan, type NoteWrite, type PlannedBlock, type PopulationSku, type CogsProvenance,
} from './monthprep.js';
import { cfInsertTrims, planDimensions, tailGroupDetach, groupInsertTrims, type WidthUpgrade } from './monthprep_struct.js';
import { buildConditionalFormats, cfRequests, borderSpec, bordersJson, rowKindOf, SUMMARY_LAST_COLUMN } from './visual.js';

/* ───────────────────────── квалификация ───────────────────────── */

export interface MidMonthCandidate { nmId: number; name: string | null; qualifiedBy: 'STOCK' | 'ORDERS'; firstDay: string }

/**
 * MID_MONTH_SKU_QUALIFICATION: активный SKU WB из справочника (популяция Unitka) без блока в секции месяца, у которого
 * в V_UNITKA_DAILY_FACT за ЭТОТ месяц на дату ≤ LAST_CLOSED_DATE есть остаток > 0 ИЛИ заказы > 0. Только справочник,
 * показы, корзины, цена — не основание. Порядок — по возрастанию nm_id.
 */
export function qualifyMidMonth(a: {
  population: readonly PopulationSku[]; represented: ReadonlySet<number>; facts: readonly FactRow[]; target: MonthKey; lcd: string;
}): { qualified: MidMonthCandidate[]; waiting: number[] } {
  const qualified: MidMonthCandidate[] = [];
  const waiting: number[] = [];
  for (const p of [...a.population].sort((x, y) => x.nmId - y.nmId)) {
    if (a.represented.has(p.nmId)) continue;
    let stockDay: string | null = null, orderDay: string | null = null;
    for (const f of a.facts) {
      if (f.nmId !== p.nmId || f.date > a.lcd || !sameMonth(monthKeyOf(f.date), a.target)) continue;
      if ((f.stock ?? 0) > 0 && (stockDay === null || f.date < stockDay)) stockDay = f.date;
      if ((f.orders ?? 0) > 0 && (orderDay === null || f.date < orderDay)) orderDay = f.date;
    }
    if (stockDay === null && orderDay === null) { waiting.push(p.nmId); continue; }
    const byStock = stockDay !== null && (orderDay === null || stockDay <= orderDay);
    qualified.push({ nmId: p.nmId, name: p.name, qualifiedBy: byStock ? 'STOCK' : 'ORDERS', firstDay: (byStock ? stockDay : orderDay)! });
  }
  return { qualified, waiting };
}

/* ───────────────────────── план ───────────────────────── */

export interface AppendInputs {
  target: MonthKey;
  meta: SheetMeta;
  columnA: readonly CellValue[];
  /** Секция целевого месяца (значения и формулы). */
  existing: Snapshot | null;
  population: readonly PopulationSku[];
  cogs: CogsSnapshot;
  structure: SheetStructure | null;
  /** userEnteredFormat строк-шаблонов САМОЙ секции (заголовок, шапка, первый день, MTD, план). */
  rowFormats: Map<number, RawCellFormat[]> | null;
  /** V_UNITKA_DAILY_FACT (месяц LAST_CLOSED_DATE); null — слой не прочитан → дописывания нет. */
  facts: readonly FactRow[] | null;
  lcd: string;
}

export interface MonthAppendPlan extends MonthPrepPlan {
  candidates: MidMonthCandidate[];
  /** Активные SKU без блока, ещё не набравшие основания (нет остатка и заказов в этом месяце). */
  waiting: number[];
  layoutBefore: MonthLayout | null;
}

function base(target: string, status: MonthPrepPlan['status'], code: string | null, reasons: string[]): MonthAppendPlan {
  return {
    status, code, target, reasons, geometry: null, layout: null, predecessor: null, blocks: [], retiredNmIds: [], unmappedActive: [],
    appendRows: 0, insertColumns: null, cfTrims: [], anchorCol: 0, groupRequests: [], tailGroupDetachedColumn: null, chainGaps: [], merges: [], formatCopies: [], conditionalFormats: null,
    dimensionRequests: [], widthUpgrades: [], rowHeights: null, formulaStyle: 'COMMA', sheetId: 0, cells: [], notes: [], manualBlankCells: 0, cogsProvenance: [],
    templateFormats: null, resetInheritedFormats: null, cfStartIndex: 0, candidates: [], waiting: [], layoutBefore: null,
  };
}

/** Отпечаток правила УФ: вид, формула/точки шкалы, диапазоны (порядок диапазонов API не гарантирует). */
function cfFingerprint(r: ConditionalFormatRule): string {
  const ranges = r.ranges.map((x) => `${x.startRowIndex}:${x.endRowIndex}:${x.startColumnIndex}:${x.endColumnIndex}`).sort().join(',');
  const f = r.booleanRule ? `B|${r.booleanRule.condition.type}|${(r.booleanRule.condition.values ?? []).map((v) => v.userEnteredValue ?? '').join('¦')}` : 'G';
  return `${f}|${ranges}`;
}

/** Индексы правил УФ, целиком лежащих в строках дней секции. */
export function sectionCfIndexes(rules: readonly ConditionalFormatRule[], layout: { firstDailyRow: number; lastDailyRow: number }): number[] {
  const out: number[] = [];
  rules.forEach((r, i) => {
    if (r.ranges.length && r.ranges.every((x) => (x.startRowIndex ?? 0) >= layout.firstDailyRow - 1 && (x.endRowIndex ?? Infinity) <= layout.lastDailyRow)) out.push(i);
  });
  return out;
}

export function planMonthAppend(inp: AppendInputs): MonthAppendPlan {
  const tk = formatMonthKey(inp.target);
  let style: FormulaStyle;
  try { style = formulaStyleOf(inp.meta.locale); } catch (e) {
    return base(tk, 'BLOCKED', 'UNSUPPORTED_LOCALE', [e instanceof Error ? e.message : String(e)]);
  }
  const loc = locateSection(inp.columnA, inp.target);
  if (loc.status !== 'FOUND' || !inp.existing) return base(tk, 'BLOCKED', 'MONTH_SECTION_MISSING', ['дописывать некуда: секции месяца нет — её создаёт подготовка месяца']);
  const g = geometryAt(inp.target, loc.topRow);
  // Дописывание — только в текущий месяц LAST_CLOSED_DATE: слой фактов покрывает именно его.
  if (!sameMonth(monthKeyOf(inp.lcd), inp.target)) {
    return { ...base(tk, 'NO_CHANGE', 'APPEND_NOT_CURRENT_MONTH', [`месяц LAST_CLOSED_DATE ${inp.lcd.slice(0, 7)} ≠ ${tk}: основания (остаток/заказы) читаются только за текущий месяц`]), geometry: g };
  }
  const v = validateSection(inp.existing);
  if (v.sectionIssues.length || v.driftIssues.length || v.blocks.length === 0) {
    return { ...base(tk, 'BLOCKED', 'MONTH_SECTION_INVALID', [...v.sectionIssues, ...v.driftIssues, ...(v.blocks.length ? [] : ['в секции нет блоков'])].slice(0, 20)), geometry: g, layout: v.layout };
  }
  const gaps = chainGaps(v.blocks);
  if (gaps.length || v.blocks[0]!.slot !== 0) {
    return { ...base(tk, 'BLOCKED', 'CHAIN_GAP', [`цепочка блоков секции не сплошная (слоты ${gaps.join(', ') || 'начинается не с 0'}) — дописывать в конец небезопасно`]), geometry: g, layout: v.layout };
  }
  const seen = new Set<number>();
  for (const p of inp.population) {
    if (seen.has(p.nmId)) return base(tk, 'BLOCKED', 'DUPLICATE_SKU', [`nm_id ${p.nmId} дважды в популяции`]);
    seen.add(p.nmId);
  }
  const represented = new Set(v.blocks.map((b) => b.nmId));
  const unmapped = inp.population.map((p) => p.nmId).filter((n) => !represented.has(n)).sort((a, b) => a - b);
  const noChange = (code: string | null, reasons: string[], waiting: number[] = unmapped): MonthAppendPlan =>
    ({ ...base(tk, 'NO_CHANGE', code, reasons), geometry: g, layout: v.layout, layoutBefore: v.layout, unmappedActive: unmapped, waiting });
  if (unmapped.length === 0) return noChange(null, ['все активные SKU уже представлены блоками месяца']);
  if (!inp.facts) return noChange('APPEND_FACTS_UNAVAILABLE', ['V_UNITKA_DAILY_FACT не прочитан — основания для дописывания неизвестны, запись не планируется']);
  const q = qualifyMidMonth({ population: inp.population, represented, facts: inp.facts, target: inp.target, lcd: inp.lcd });
  if (q.qualified.length === 0) return noChange(null, [`активные SKU без блока есть (${unmapped.join(', ')}), но ни у одного нет остатка или заказов в ${tk} на дату ≤ ${inp.lcd}`], q.waiting);

  // Секция обязана быть последней в листе: строки под ней принадлежали бы следующему месяцу с другой раскладкой.
  if (inp.meta.rowCount !== g.spacerRow) {
    return { ...base(tk, 'BLOCKED', 'LATER_SECTION_EXISTS', [`под секцией ${tk} (до строки ${g.spacerRow}) есть строки до ${inp.meta.rowCount}: следующий месяц уже создан — дописывание требует решения владельца`]), geometry: g, layout: v.layout };
  }
  if (!inp.structure) return { ...base(tk, 'BLOCKED', 'STRUCTURE_UNAVAILABLE', ['структура листа (УФ, размеры, группы) не прочитана']), geometry: g, layout: v.layout };
  const tplRows = [g.topRow, g.headerRow, g.firstDailyRow, g.mtdRow, g.spacerRow];
  if (!inp.rowFormats || tplRows.some((r) => !inp.rowFormats!.has(r))) {
    return { ...base(tk, 'BLOCKED', 'TEMPLATE_FORMATS_UNAVAILABLE', [`форматы строк секции ${tplRows.join(', ')} не прочитаны`]), geometry: g, layout: v.layout };
  }
  // Построители обязаны воспроизводить формулы секции (в т.ч. сводки) — иначе её расширение «не понято».
  const extracted = extractPredecessorParams(inp.existing, v.layout, style);
  if (extracted.mismatches.length) {
    return { ...base(tk, 'BLOCKED', 'TEMPLATE_MISMATCH', [`построители формул не воспроизводят секцию ${tk}: ${extracted.mismatches.slice(0, 20).join(', ')}`]), geometry: g, layout: v.layout };
  }
  const last = v.blocks[v.blocks.length - 1]!;
  const ig = insertGeometry(last.slot, tailStartOf(inp.meta.anchorCol), q.qualified.length);
  if ('error' in ig) {
    return { ...base(tk, 'BLOCKED', ig.error, [`хвост книги начинается в ${colA1(tailStartOf(inp.meta.anchorCol))} (якорь ${colA1(inp.meta.anchorCol)}), цепочка кончается в ${colA1(last.start + BLOCK_BODY_WIDTH - 1)} — раскладка хвоста не распознана`]), geometry: g, layout: v.layout };
  }

  // Правила УФ секции обязаны быть теми, что построил генератор для нынешней раскладки: они заменяются целиком.
  const rules = inp.structure.conditionalFormats;
  const own = sectionCfIndexes(rules, g);
  const expectCf = buildConditionalFormats(v.layout, inp.meta.sheetId, style, inp.meta.anchorCol).rules.map(cfFingerprint).sort();
  const haveCf = own.map((i) => cfFingerprint(rules[i]!)).sort();
  if (expectCf.length !== haveCf.length || expectCf.some((x, i) => x !== haveCf[i])) {
    return { ...base(tk, 'BLOCKED', 'CF_SECTION_DRIFT', [`правила УФ секции (${haveCf.length}) не совпадают с контрактом генератора для ${v.blocks.length} блоков (${expectCf.length}) — заменять их вслепую нельзя`]), geometry: g, layout: v.layout };
  }
  // Новые блоки — в конец, по возрастанию nm_id; COGS — только из канона.
  const kept: PlannedBlock[] = v.blocks.map((b) => ({ ...b, origin: 'CARRIED' as const, params: { ...extracted.params.get(b.nmId)! }, cogsProvenance: null }));
  const fresh: PlannedBlock[] = [];
  const provenance: CogsProvenance[] = [];
  let slot = last.slot;
  const tmpl = kept[kept.length - 1]!.params;
  for (const c of q.qualified) {
    slot = nextFreeSlot(slot);
    const cg = resolveCanonicalCogs(inp.cogs, c.nmId);
    if ('error' in cg) return { ...base(tk, 'BLOCKED', cg.error, [`новый блок ${c.nmId}: канон COGS — ${cg.error}; 0 не подставляется`]), geometry: g, layout: v.layout };
    provenance.push(cg);
    const start = slotStart(slot);
    fresh.push({
      index: kept.length + fresh.length, slot, start, nmId: c.nmId, title: `${c.nmId} ${c.name ?? ''}`.trim(), origin: 'NEW',
      params: { start, cogsTerm: cogsLiteral(cg.cogs), overhead: tmpl.overhead, stockProjection: GENERATED_STOCK_PROJECTION, storageProjection: 'none' },
      cogsProvenance: cg,
    });
  }
  const blocks = [...kept, ...fresh];
  const slim = (b: BlockSlot): BlockSlot => ({ index: b.index, slot: b.slot, start: b.start, nmId: b.nmId, title: b.title });
  const layout = layoutOf(g, blocks.map(slim));
  const lastSlot = blocks[blocks.length - 1]!.slot;
  const oldEnd = v.layout.lastBlockColumn;                     // последняя метрика прежнего последнего блока
  const anchorAfter = inp.meta.anchorCol + ig.count;

  const cells: CellWrite[] = [];
  const notes: NoteWrite[] = [];
  const fml = (row: number, col: number, text: string): void => { cells.push({ row, col, value: { kind: 'formula', text } }); };
  const str = (row: number, col: number, value: string): void => { cells.push({ row, col, value: { kind: 'string', value } }); };
  const fAt = (r: number, c: number): CellValue => { const x = formulaAt(inp.existing!, r, c); return typeof x === 'string' && x.startsWith('=') ? fromLocaleFormula(x, style) : x; };
  const headerOf = (col: number): string | null => { const x = cellAt(inp.existing!, g.headerRow, col); return typeof x === 'string' && x !== '' ? x : null; };

  // 1) Новые блоки: заголовок, шапка (как у прежнего последнего блока), даты, формулы, MTD.
  for (const b of fresh) {
    str(g.topRow, b.start, b.title);
    for (let o = 0; o < BLOCK_WIDTH; o++) {
      if (b.start + o > blockLastColumn(layout, b)) continue;
      const h = headerOf(last.start + o) ?? headerOf(v.blocks[0]!.start + o);
      if (h) str(g.headerRow, b.start + o, h);
    }
    for (const [off, f] of blockMtdFormulas(b.start, g)) fml(g.mtdRow, b.start + off, f);
  }
  // 2) Прежний последний блок перестал быть последним — у него появляется колонка-разделитель (день недели).
  const separators = blocks.filter((b) => !isLastBlock(layout, b) && (b.origin === 'NEW' || (b.slot === last.slot && !isFormula(formulaAt(inp.existing!, g.lastDailyRow, b.start + OFFSET.weekday)))));
  for (const b of separators.filter((x) => x.origin === 'CARRIED')) {
    const h = headerOf(v.blocks[0]!.start + OFFSET.weekday);
    if (h) str(g.headerRow, b.start + OFFSET.weekday, h);
  }
  for (let i = 0; i < g.daysInMonth; i++) {
    const row = dayRowOf(g, i);
    const serial = isoToSerial(addDaysIso(g.monthStart, i));
    for (const b of fresh) {
      cells.push({ row, col: b.start + OFFSET.date, value: { kind: 'number', value: serial } });
      for (const [off, f] of blockDayFormulas(b.params, row)) if (b.start + off <= blockLastColumn(layout, b)) fml(row, b.start + off, f);
      for (const [off, f] of blockProjectionFormulas(b.params, row, i === 0)) fml(row, b.start + off, f);
    }
    for (const b of separators.filter((x) => x.origin === 'CARRIED')) fml(row, b.start + OFFSET.weekday, blockDayFormulas(b.params, row).get(OFFSET.weekday)!);
    // 3) Сводка: только формулы, чей диапазон действительно расширился.
    for (const [col, f] of summaryDayFormulas(row, lastSlot, blocks[0]!.start)) if (normFormula(fAt(row, col)) !== normFormula(f)) fml(row, col, f);
  }
  for (const [col, f] of summaryMtdFormulas(g, lastSlot)) if (normFormula(fAt(g.mtdRow, col)) !== normFormula(f)) fml(g.mtdRow, col, f);
  // Подписи сводки «N SKU» — по числу представленных блоков.
  for (let c = 1; c <= SUMMARY_LAST_COLUMN; c++) {
    const h = headerOf(c);
    const next = h?.replace(/\b\d+ SKU\b/, `${blocks.length} SKU`);
    if (h && next && next !== h) str(g.headerRow, c, next);
  }
  for (const b of fresh) {
    const p = b.cogsProvenance!;
    notes.push({ row: g.firstDailyRow, col: b.start + OFFSET.unitProfit, note: `Себестоимость ${p.cogs} ₽ подставлена при дописывании SKU в ${g.monthKey} из канона ${p.source}: значение на ${p.day}, публикация ${p.publishedAt ?? 'н/д'}, run ${p.runId ?? 'н/д'}.` });
  }

  // Форматы новых колонок — из строк-шаблонов самой секции: тело ← прежний последний блок, разделитель ← образец.
  const sample = v.blocks.length > 1 ? v.blocks[0]!.start + OFFSET.weekday : SUMMARY_LAST_COLUMN;
  const rowPairs: Array<[number, number, number]> = [
    [g.topRow, g.topRow, g.topRow], [g.headerRow, g.headerRow, g.headerRow], [g.firstDailyRow, g.firstDailyRow, g.firstDailyRow],
    [g.firstDailyRow, g.firstDailyRow + 1, g.lastDailyRow], [g.mtdRow, g.mtdRow, g.mtdRow], [g.spacerRow, g.spacerRow, g.spacerRow],
  ];
  const formatCopies: Array<{ source: GridRect; dest: GridRect }> = [];
  for (const [src, d1, d2] of rowPairs) {
    const copy = (s1: number, s2: number, t1: number): void => { formatCopies.push({ source: { r1: src, r2: src, c1: s1, c2: s2 }, dest: { r1: d1, r2: d2, c1: t1, c2: t1 + (s2 - s1) } }); };
    for (const b of separators) copy(sample, sample, b.start + OFFSET.weekday);
    for (const b of fresh) copy(last.start, last.start + BLOCK_BODY_WIDTH - 1, b.start);
  }
  const merges: GridRect[] = fresh.map((b) => ({ r1: g.topRow, r2: g.topRow, c1: b.start, c2: b.start + BLOCK_TITLE_MERGE_WIDTH - 1 }));

  // УФ: правила секции заменяются целиком (идиомы зависят от последней колонки и от колонки якорей).
  const { trims, unsafe } = cfInsertTrims(rules, ig.at, ig.count);
  if (unsafe.length) return { ...base(tk, 'BLOCKED', 'CF_TRIM_UNSAFE', unsafe), geometry: g, layout: v.layout };
  const ownSet = new Set(own);
  const dim = planDimensions({
    structure: inp.structure, prev: g, next: g, layout, inserted: { at: ig.at, count: ig.count }, templateSlot: last.slot,
    templateFormats: inp.rowFormats, headerTexts: new Map(), sheetId: inp.meta.sheetId, rows: false,
  });
  const tplGroups = inp.structure.columnGroups.filter((gr) => gr.startIndex >= slotStart(last.slot) - 1 && gr.endIndex <= slotStart(last.slot) + BLOCK_WIDTH - 1);
  const detach = tplGroups.length ? tailGroupDetach(inp.structure.columnGroups, tailStartOf(inp.meta.anchorCol), ig.count, inp.meta.sheetId) : null;
  const plan: MonthAppendPlan = {
    ...base(tk, 'PLAN_APPEND', null, [`дописываются SKU: ${q.qualified.map((c) => `${c.nmId} (${c.qualifiedBy === 'STOCK' ? 'остаток' : 'заказы'} с ${c.firstDay})`).join(', ')}`]),
    geometry: g, layout, layoutBefore: v.layout, blocks, unmappedActive: unmapped, candidates: q.qualified, waiting: q.waiting,
    insertColumns: { at: ig.at, count: ig.count }, anchorCol: anchorAfter, chainGaps: chainGaps(blocks), merges, formatCopies,
    templateFormats: inp.rowFormats, resetInheritedFormats: { r1: 1, r2: g.topRow - 1, c1: ig.at + 1, c2: ig.at + ig.count },
    cfTrims: trims.filter((t) => !ownSet.has(t.index)), cfDeleteIndexes: [...own].sort((a, b) => b - a),
    conditionalFormats: buildConditionalFormats(layout, inp.meta.sheetId, style, anchorAfter), cfStartIndex: rules.length - own.length,
    dimensionRequests: dim.requests, widthUpgrades: dim.widthUpgrades, rowHeights: null,
    tailGroupDetachedColumn: detach?.column ?? null,
    groupRequests: [...groupInsertTrims(inp.structure.columnGroups, ig.at, ig.count, inp.meta.sheetId), ...(detach ? [detach.request] : []), ...fresh.flatMap((b) => tplGroups.map((gr) => ({ addDimensionGroup: { range: { sheetId: inp.meta.sheetId, dimension: 'COLUMNS', startIndex: gr.startIndex + (b.start - last.start), endIndex: gr.endIndex + (b.start - last.start) } } }) as StructureRequest))],
    formulaStyle: style, sheetId: inp.meta.sheetId, cells, notes, manualBlankCells: fresh.length * g.daysInMonth * 2, cogsProvenance: provenance,
    formatFromColumn: oldEnd, bordersOnlyColumns: [oldEnd], valueMode: 'runs',
  };
  assertAppendConfined(plan, v.layout);
  assertPlanConfined(plan);
  return plan;
}

/**
 * Предохранитель дописывания: значения — только сводка (формулы C..K и подписи шапки) и колонки правее прежней
 * последней метрики; ни одной записи в тело существующих блоков (факты, формулы, ручные СПП и блогеры).
 */
export function assertAppendConfined(plan: MonthAppendPlan, before: MonthLayout): void {
  const g = plan.geometry!;
  const oldEnd = before.lastBlockColumn;
  const bad: string[] = [];
  for (const c of plan.cells) {
    if (c.col > oldEnd) continue;
    const summaryFormula = c.col >= SUMMARY.bloggers && c.col <= SUMMARY.drr && c.value.kind === 'formula' && c.row >= g.firstDailyRow && c.row <= g.mtdRow;
    const summaryLabel = c.col <= SUMMARY_LAST_COLUMN && c.row === g.headerRow && c.value.kind === 'string';
    if (!summaryFormula && !summaryLabel) bad.push(`${colA1(c.col)}${c.row}`);
  }
  if (plan.formatCopies.some((f) => f.dest.c1 <= oldEnd)) bad.push('формат существующих колонок');
  if ((plan.formatFromColumn ?? 1) < oldEnd) bad.push('полоса форматов левее прежней последней метрики');
  if (plan.blocks.slice(0, before.blocks.length).some((b, i) => b.nmId !== before.blocks[i]!.nmId || b.slot !== before.blocks[i]!.slot)) bad.push('порядок существующих блоков изменён');
  if (bad.length) throw new RangeError(`план дописывания SKU задевает существующие блоки: ${bad.slice(0, 10).join(', ')}`);
}

/* ───────────────────────── откат дописывания ───────────────────────── */

export interface AppendRollbackInputs {
  target: MonthKey;
  meta: SheetMeta;
  columnA: readonly CellValue[];
  /** Секция ПОСЛЕ дописывания. */
  existing: Snapshot | null;
  structure: SheetStructure | null;
  /** Дописанные SKU — обязаны быть последними блоками секции, в этом порядке (из журнала плана дописывания). */
  removeNmIds: readonly number[];
  /** Вставленные дописыванием колонки (1-based, включительно) — из журнала плана (insert_columns). */
  insertedColumns: [number, number];
  /** Вставленные колонки пусты во всех строках выше секции. */
  insertedColumnsEmptyAbove: boolean;
  /**
   * Колонка-разделитель прежнего последнего блока существовала ДО дописывания (в ней есть данные выше секции — книга до
   * Calendar V2). Только тогда вставка могла начаться через колонку после последней метрики.
   */
  separatorPreexisted?: boolean;
  restoreWidths?: readonly WidthUpgrade[];
}
export interface AppendRollbackPlan { status: 'PLAN_ROLLBACK' | 'BLOCKED'; code: string | null; reasons: string[]; requests: StructureRequest[]; anchorCol: number; blocksAfter: number }

/**
 * Откат дописывания SKU: удалить правила УФ секции → удалить вставленные колонки (группы внутри них исчезают, якоря и
 * ссылки возвращаются) → вернуть сводке диапазоны и подписи прежнего числа блоков (Sheets при удалении колонок сам
 * «ужимает» диапазоны — текст формул пишется канонический) → границы прежней последней метрики по контракту → правила
 * УФ для прежней раскладки. Итог проверяется сверкой со снимком до дописывания. Любое расхождение с ожидаемым — отказ.
 */
export function planAppendRollback(inp: AppendRollbackInputs): AppendRollbackPlan {
  const refuse = (code: string, reason: string): AppendRollbackPlan => ({ status: 'BLOCKED', code, reasons: [reason], requests: [], anchorCol: inp.meta.anchorCol, blocksAfter: 0 });
  let style: FormulaStyle;
  try { style = formulaStyleOf(inp.meta.locale); } catch (e) { return refuse('UNSUPPORTED_LOCALE', e instanceof Error ? e.message : String(e)); }
  const loc = locateSection(inp.columnA, inp.target);
  if (loc.status !== 'FOUND' || !inp.existing || !inp.structure) return refuse('MONTH_SECTION_MISSING', 'секция месяца или структура листа не прочитаны');
  const g = geometryAt(inp.target, loc.topRow);
  const v = validateSection(inp.existing);
  if (v.sectionIssues.length || v.driftIssues.length) return refuse('MONTH_SECTION_INVALID', [...v.sectionIssues, ...v.driftIssues].slice(0, 8).join(' | '));
  const n = inp.removeNmIds.length;
  const tail = v.blocks.slice(-n).map((b) => b.nmId);
  if (n === 0 || n >= v.blocks.length || tail.join() !== inp.removeNmIds.join()) return refuse('APPEND_ROLLBACK_MISMATCH', `последние блоки секции ${tail.join(', ')} ≠ дописанным ${inp.removeNmIds.join(', ')}`);
  const keep = v.blocks.slice(0, -n);
  const keepLast = keep[keep.length - 1]!;
  const keepEnd = keepLast.start + BLOCK_BODY_WIDTH - 1;
  const [c1, c2] = inp.insertedColumns;
  if (c2 !== v.layout.lastBlockColumn || (c1 !== keepEnd + 1 && c1 !== keepEnd + 2)) return refuse('APPEND_ROLLBACK_MISMATCH', `вставленные колонки ${colA1(c1)}..${colA1(c2)} не совпадают с концом цепочки (${colA1(keepEnd + 1)}..${colA1(v.layout.lastBlockColumn)})`);
  if (c1 === keepEnd + 2 && inp.separatorPreexisted !== true) return refuse('APPEND_ROLLBACK_MISMATCH', `вставка с ${colA1(c1)} возможна, только если колонка ${colA1(keepEnd + 1)} существовала до дописывания — подтверждения нет`);
  if (!inp.insertedColumnsEmptyAbove) return refuse('APPEND_ROLLBACK_UNSAFE', 'вставленные колонки непусты выше секции — удалять их небезопасно');
  const rules = inp.structure.conditionalFormats;
  const own = sectionCfIndexes(rules, g);
  const want = buildConditionalFormats(v.layout, inp.meta.sheetId, style, inp.meta.anchorCol).rules.map(cfFingerprint).sort();
  const have = own.map((i) => cfFingerprint(rules[i]!)).sort();
  if (want.length !== have.length || want.some((x, i) => x !== have[i])) return refuse('CF_SECTION_DRIFT', 'правила УФ секции не совпадают с контрактом генератора — откат вслепую невозможен');

  const sheetId = inp.meta.sheetId;
  const count = c2 - c1 + 1;
  const anchorAfter = inp.meta.anchorCol - count;
  const layout = layoutOf(g, keep.map((b) => ({ index: b.index, slot: b.slot, start: b.start, nmId: b.nmId, title: b.title })));
  const req: StructureRequest[] = [];
  for (const i of [...own].sort((a, b) => b - a)) req.push({ deleteConditionalFormatRule: { sheetId, index: i } });
  req.push({ deleteDimension: { range: { sheetId, dimension: 'COLUMNS', startIndex: c1 - 1, endIndex: c2 } } });
  // Колонка-разделитель прежнего последнего блока существовала до дописывания (книга до Calendar V2) — очистить её в секции.
  if (c1 === keepEnd + 2) {
    req.push({ repeatCell: { range: { sheetId, startRowIndex: g.topRow - 1, endRowIndex: g.spacerRow, startColumnIndex: keepEnd, endColumnIndex: keepEnd + 1 }, cell: {}, fields: 'userEnteredValue,userEnteredFormat' } });
  }
  const byRow = new Map<number, Map<number, CellValueWrite>>();
  const put = (row: number, col: number, value: CellValueWrite): void => { (byRow.get(row) ?? byRow.set(row, new Map()).get(row)!).set(col, value); };
  for (let i = 0; i < g.daysInMonth; i++) for (const [col, f] of summaryDayFormulas(dayRowOf(g, i), keepLast.slot, keep[0]!.start)) put(dayRowOf(g, i), col, { kind: 'formula', text: f });
  for (const [col, f] of summaryMtdFormulas(g, keepLast.slot)) put(g.mtdRow, col, { kind: 'formula', text: f });
  for (let c = 1; c <= SUMMARY_LAST_COLUMN; c++) {
    const h = cellAt(inp.existing, g.headerRow, c);
    if (typeof h === 'string' && /\b\d+ SKU\b/.test(h)) put(g.headerRow, c, { kind: 'string', value: h.replace(/\b\d+ SKU\b/, `${keep.length} SKU`) });
  }
  for (const [row, cols] of [...byRow.entries()].sort((a, b) => a[0] - b[0])) req.push(...valueRunRequests(row, cols, style, sheetId));
  const bands: Array<[number, number]> = [[g.topRow, g.topRow], [g.headerRow, g.headerRow], [g.firstDailyRow, g.firstDailyRow], [g.firstDailyRow + 1, g.lastDailyRow], [g.mtdRow, g.mtdRow], [g.spacerRow, g.spacerRow]];
  for (const [r1, r2] of bands) {
    const spec = borderSpec(rowKindOf(g, r1)!, keepEnd, layout)!;
    req.push({ repeatCell: { range: { sheetId, startRowIndex: r1 - 1, endRowIndex: r2, startColumnIndex: keepEnd - 1, endColumnIndex: keepEnd }, cell: { userEnteredFormat: { borders: bordersJson(spec) } }, fields: 'userEnteredFormat.borders' } });
  }
  req.push(...cfRequests(buildConditionalFormats(layout, sheetId, style, anchorAfter), sheetId, rules.length - own.length));
  for (const u of inp.restoreWidths ?? []) req.push({ updateDimensionProperties: { range: { sheetId, dimension: 'COLUMNS', startIndex: u.col - 1, endIndex: u.col }, properties: { pixelSize: u.from }, fields: 'pixelSize' } });
  return { status: 'PLAN_ROLLBACK', code: null, reasons: [`удаляются блоки ${inp.removeNmIds.join(', ')}, колонки ${colA1(c1)}..${colA1(c2)}`], requests: req, anchorCol: anchorAfter, blocksAfter: keep.length };
}
