/**
 * UNITKA CALENDAR V2 (Phase 2B) — планировщик подготовки месяца. ЧИСТЫЙ модуль: ни Sheets, ни BigQuery.
 *
 * ПЛАН отделён от ИСПОЛНЕНИЯ: planMonthPrep() строит желаемую секцию месяца по предыдущему месяцу
 * (снимок листа) + популяции активных SKU + канону COGS; toStructureRequests() переводит план в
 * запросы spreadsheets.batchUpdate. Исполняет их только загрузчик unitka-month-prep в явном режиме
 * записи (prep.ts); суточный Engine этого модуля не импортирует.
 *
 * Идемпотентность:
 *   месяца нет, предыдущий валиден          → PLAN_CREATE;
 *   месяц есть и проходит контракт секции   → NO_CHANGE (0 запросов, ручные СПП/блогеры не трогаются);
 *   месяц есть, но неполон / не по контракту → MONTH_SECTION_PARTIAL / MONTH_SECTION_INVALID, 0 запросов,
 *                                             авто-починки нет;
 *   прочие препятствия                       → BLOCKED с кодом (COGS, предыдущий месяц, шаблон…).
 *
 * Популяция (V1): слоты предыдущего месяца сохраняются; выбывший SKU оставляет слот пустым; новые
 * активные SKU — по возрастанию nm_id в слоты СРАЗУ за последним занятым: для них ВСТАВЛЯЮТСЯ колонки
 * (insertDimension) перед хвостом книги (расчёты VQ..VZ, подписи WA, якоря WB736..739), который
 * сдвигается вправо вместе с именованным диапазоном REVERSE_LEG_RATE и ссылками формул/УФ (это делает
 * Sheets). Цепочка блоков сплошная (visual hardening 2). После принятия месяца раскладка заморожена.
 */
import {
  BLOCK_WIDTH, geometryAt, layoutOf, locateSection, nextFreeSlot, previousMonth, formatMonthKey,
  slotStart, dayRowOf, chainGaps,
  type MonthGeometry, type MonthKey, type MonthLayout, type BlockSlot,
} from './calendar.js';
import { OFFSET, SUMMARY, colA1, isoToSerial, addDaysIso, type CellValue } from './model.js';
import { cellAt, formulaAt, isFormula, validateSection, MTD_LABEL, type Snapshot } from './plan.js';
import { parseCogsTerm, type CogsSnapshot } from './integrity.js';
import {
  blockDayFormulas, blockProjectionFormulas, blockMtdFormulas, summaryDayFormulas, summaryMtdFormulas, normFormula,
  formulaStyleOf, fromLocaleFormula, toLocaleFormula,
  type BlockFormulaParams, type FormulaStyle,
} from './formulas.js';
import type { RawCellFormat, SheetMeta, SheetStructure, StructureRequest } from './sheets.js';
import { dimensionRequests, cfInsertTrims, type CfTrim } from './monthprep_struct.js';
import { buildConditionalFormats, cfRequests, columnKind, neutralizeDayFormat, borderSpec, bordersJson, rowKindOf, SUMMARY_LAST_COLUMN, type VisualCf } from './visual.js';

export type PrepStatus = 'PLAN_CREATE' | 'NO_CHANGE' | 'MONTH_SECTION_PARTIAL' | 'MONTH_SECTION_INVALID' | 'BLOCKED';

/** Ширина объединения заголовка блока (live сентябрь: все 24 блока — 7 колонок) и заголовка месяца A:K. */
export const BLOCK_TITLE_MERGE_WIDTH = 7;
export const MONTH_TITLE_MERGE_LAST_COL = SUMMARY.drr + 1; // L — сводка A..L одним контуром

export interface PopulationSku { nmId: number; name: string | null }

export interface CogsProvenance {
  nmId: number;
  cogs: number;
  day: string;
  source: string;
  publishedAt: string | null;
  runId: string | null;
}

export interface PlannedBlock extends BlockSlot {
  origin: 'CARRIED' | 'NEW';
  params: BlockFormulaParams;
  cogsProvenance: CogsProvenance | null;
}

/** Прямоугольник листа, 1-based, включительно. */
export interface GridRect { r1: number; r2: number; c1: number; c2: number }

export type CellValueWrite =
  | { kind: 'formula'; text: string }
  | { kind: 'number'; value: number }
  | { kind: 'string'; value: string };

export interface CellWrite { row: number; col: number; value: CellValueWrite }
export interface NoteWrite { row: number; col: number; note: string }

export interface MonthPrepPlan {
  status: PrepStatus;
  code: string | null;
  target: string;
  reasons: string[];
  geometry: MonthGeometry | null;
  layout: MonthLayout | null;
  predecessor: { monthKey: string; topRow: number; blocks: number } | null;
  blocks: PlannedBlock[];
  retiredNmIds: number[];
  /** NO_CHANGE: активные SKU без блока в уже принятом месяце (раскладка заморожена — только отчёт). */
  unmappedActive: number[];
  appendRows: number;
  /** Вставка колонок нового блока/блоков сразу за последним блоком прошлого месяца (0-based index = колонка перед вставкой). */
  insertColumns: { at: number; count: number } | null;
  /** Правила УФ прошлых месяцев, которые вставка колонок расширила бы на новый блок, — переиздаются без расширения. */
  cfTrims: CfTrim[];
  /** Колонка якорей книги ПОСЛЕ вставки (в формулах УФ и для Engine). */
  anchorCol: number;
  /** Группы колонок (expand/collapse аналитики) новых блоков — как у блока-шаблона. */
  groupRequests: StructureRequest[];
  /** Слоты-дыры внутри цепочки (только отчёт; контракт — пусто). */
  chainGaps: number[];
  merges: GridRect[];
  /**
   * Карта форматов «строка/колонки прошлого месяца → строки/колонки новой секции». Phase 2C: исполняется
   * ЯВНОЙ записью userEnteredFormat (copyPaste PASTE_FORMAT на живом листе копирует и УФ — запрещено).
   */
  formatCopies: Array<{ source: GridRect; dest: GridRect }>;
  /** Форматы строк-шаблонов прошлого месяца (строка → колонки 1..N), прочитанные из листа. */
  templateFormats: Map<number, RawCellFormat[]> | null;
  /** Сброс форматов, унаследованных добавленными колонками в старых строках (appendDimension копирует формат WB). */
  resetInheritedFormats: GridRect | null;
  /** УФ новой секции — из визуального контракта (visual.ts), не копия правил прошлого месяца. */
  conditionalFormats: VisualCf | null;
  /** Индекс, с которого добавляются правила УФ (после существующих правил листа). */
  cfStartIndex: number;
  /** Ширины/скрытие колонок новых блоков и высоты строк новой секции (Phase 2C). */
  dimensionRequests: StructureRequest[];
  /** Синтаксис формул книги (локаль): в запросах формулы переводятся из канонической формы. */
  formulaStyle: FormulaStyle;
  sheetId: number;
  cells: CellWrite[];
  notes: NoteWrite[];
  /** Ручные ячейки (СПП, блогеры) новых дней — намеренно пустые. */
  manualBlankCells: number;
  cogsProvenance: CogsProvenance[];
}

export interface PrepInputs {
  target: MonthKey;
  meta: SheetMeta;
  columnA: readonly CellValue[];
  /** Секция предыдущего месяца (если найдена и помещается в сетку). */
  predecessor: Snapshot | null;
  /** Секция целевого месяца (если заголовок найден и секция помещается в сетку). */
  existing: Snapshot | null;
  population: readonly PopulationSku[];
  cogs: CogsSnapshot;
  /** Структура листа (УФ, размеры) — для переноса УФ и размеров. Без неё PLAN_CREATE не строится. */
  structure?: SheetStructure | null;
  /** userEnteredFormat строк-шаблонов прошлого месяца (строка → колонки 1..N). Без них PLAN_CREATE не строится. */
  rowFormats?: Map<number, RawCellFormat[]> | null;
}

/** Строки прошлого месяца, чьи форматы нужны для новой секции (дни — только первый день, эталон E3). */
export function templateRowsOf(g: MonthGeometry): number[] {
  return [g.topRow, g.headerRow, g.firstDailyRow, g.mtdRow, g.spacerRow];
}

const COGS_SOURCE = 'wb_mart.V_UNITKA_COGS_CANONICAL (копия wb_mart.UNITKA_COGS_EFFECTIVE)';

function emptyPlan(target: string, status: PrepStatus, code: string | null, reasons: string[]): MonthPrepPlan {
  return {
    status, code, target, reasons, geometry: null, layout: null, predecessor: null, blocks: [], retiredNmIds: [], unmappedActive: [],
    appendRows: 0, insertColumns: null, cfTrims: [], anchorCol: 0, groupRequests: [], chainGaps: [], merges: [], formatCopies: [], conditionalFormats: null, dimensionRequests: [],
    formulaStyle: 'COMMA', sheetId: 0, cells: [], notes: [], manualBlankCells: 0, cogsProvenance: [],
    templateFormats: null, resetInheritedFormats: null, cfStartIndex: 0,
  };
}

/* ───────────────────────── параметры блока из прошлого месяца ───────────────────────── */

/** Константа внешней рекламы из формулы Y: «…*(1-AD)+<k>-(…». */
function overheadOf(y: string, cogsTerm: string): string | null {
  const f = normFormula(y);
  const at = f.indexOf(`-(${normFormula(cogsTerm)}+`);
  if (at < 0) return null;
  const m = /\)\+(\d+(?:\.\d+)?)-\(/.exec(f.slice(at));
  return m ? m[1]! : null;
}

/**
 * Проекция остатка в СОЗДАВАЕМОМ месяце — у каждого блока с защитой LAST_CLOSED_DATE, как у блока 1 живой книги
 * (решение владельца, final visual polish). В сентябре блоки 2..24 держат форму без защиты: в свежем месяце
 * «пусто − пусто + пусто» показывает 0 в каждом будущем дне. С защитой будущий день пуст, а закрытый считает то же
 * самое (настоящий ноль остаётся числом 0). Форма прошлого месяца по-прежнему распознаётся (иначе TEMPLATE_MISMATCH),
 * но в новый месяц не переносится; от момента подготовки (проекция последнего дня уже заменена фактом) не зависит.
 */
export const GENERATED_STOCK_PROJECTION = 'guarded' as const;

function projectionStyle(f: CellValue, wantGuarded: string, wantPlain: string): 'guarded' | 'plain' | 'none' | null {
  if (!isFormula(f)) return 'none';
  const n = normFormula(f);
  if (n === normFormula(wantGuarded)) return 'guarded';
  if (n === normFormula(wantPlain)) return 'plain';
  return null;
}

/**
 * Параметры каждого блока прошлого месяца + доказательство, что построители воспроизводят его
 * формулы: последняя строка дня, строка MTD, сводка. Любое расхождение — TEMPLATE_MISMATCH.
 */
export function extractPredecessorParams(pred: Snapshot, layout: MonthLayout, style: FormulaStyle = 'COMMA'): { params: Map<number, BlockFormulaParams>; mismatches: string[] } {
  const params = new Map<number, BlockFormulaParams>();
  const mismatches: string[] = [];
  const g = pred.geometry;
  const row = g.lastDailyRow;
  const lastSlot = layout.blocks.reduce((m, b) => Math.max(m, b.slot), 0);
  // Формулы листа — в локали книги; сравнение и разбор — в канонической форме.
  const fAt = (r: number, c: number): CellValue => { const v = formulaAt(pred, r, c); return typeof v === 'string' && v.startsWith('=') ? fromLocaleFormula(v, style) : v; };
  const check = (r: number, c: number, want: string): void => {
    const got = fAt(r, c) ?? cellAt(pred, r, c);
    if (normFormula(got) !== normFormula(want)) mismatches.push(`${colA1(c)}${r}`);
  };
  for (const b of layout.blocks) {
    const t = parseCogsTerm(fAt(row, b.start + OFFSET.unitProfit), b, row);
    if (t.kind === 'unrecognised') { mismatches.push(`${colA1(b.start + OFFSET.unitProfit)}${row} COGS`); continue; }
    const cogsTerm = t.text; // «240», «159.3» или «$R$45» — ровно как в формуле прошлого месяца
    const overhead = overheadOf(String(fAt(row, b.start + OFFSET.adsOut) ?? ''), cogsTerm);
    if (overhead === null) { mismatches.push(`${colA1(b.start + OFFSET.adsOut)}${row} Y`); continue; }
    const base: BlockFormulaParams = { start: b.start, cogsTerm, overhead, stockProjection: 'none', storageProjection: 'none' };
    const guardedStock = blockProjectionFormulas({ ...base, stockProjection: 'guarded' }, row, false).get(OFFSET.stock)!;
    const plainStock = blockProjectionFormulas({ ...base, stockProjection: 'plain' }, row, false).get(OFFSET.stock)!;
    const st = projectionStyle(fAt(row, b.start + OFFSET.stock), guardedStock, plainStock);
    const storageF = blockProjectionFormulas({ ...base, storageProjection: 'guarded' }, row, false).get(OFFSET.storage)!;
    const sg = projectionStyle(fAt(row, b.start + OFFSET.storage), storageF, '<нет формы plain для хранения>');
    if (st === null) mismatches.push(`${colA1(b.start + OFFSET.stock)}${row} проекция остатка`);
    if (sg === null || sg === 'plain') mismatches.push(`${colA1(b.start + OFFSET.storage)}${row} проекция хранения`);
    const p: BlockFormulaParams = { ...base, stockProjection: st ?? 'none', storageProjection: sg === 'guarded' ? 'guarded' : 'none' };
    for (const [off, f] of blockDayFormulas(p, row)) check(row, b.start + off, f);
    for (const [off, f] of blockMtdFormulas(b.start, g)) check(g.mtdRow, b.start + off, f);
    params.set(b.nmId, p);
  }
  const first = layout.blocks[0];
  if (first) for (const [col, f] of summaryDayFormulas(row, lastSlot, first.start)) check(row, col, f);
  for (const [col, f] of summaryMtdFormulas(g, lastSlot)) check(g.mtdRow, col, f);
  return { params, mismatches };
}

/* ───────────────────────── COGS нового блока ───────────────────────── */

/** Канон COGS для НОВОГО блока: последний день копии с положительным значением. Никакого 0 по умолчанию. */
export function resolveCanonicalCogs(cogs: CogsSnapshot, nmId: number): CogsProvenance | { error: string } {
  if (cogs.state !== 'AVAILABLE') return { error: `COGS_${cogs.state}` };
  let best: { day: string; v: number } | null = null;
  for (const r of cogs.rows) {
    if (r.nmId !== nmId || r.canonicalCogs === null || !(r.canonicalCogs > 0)) continue;
    if (!best || r.day > best.day) best = { day: r.day, v: r.canonicalCogs };
  }
  if (!best) return { error: 'COGS_MISSING' };
  return { nmId, cogs: best.v, day: best.day, source: COGS_SOURCE, publishedAt: cogs.publishedAt, runId: cogs.runId };
}

/** Число как литерал формулы: без экспоненты и без локальной запятой. */
export function cogsLiteral(v: number): string {
  if (!Number.isFinite(v) || v <= 0) throw new RangeError(`COGS ${v}`);
  const s = String(v);
  if (/e/i.test(s)) throw new RangeError(`COGS ${v}: экспоненциальная запись`);
  return s;
}

/* ───────────────────────── план ───────────────────────── */

export function planMonthPrep(inp: PrepInputs): MonthPrepPlan {
  const tk = formatMonthKey(inp.target);
  let style: FormulaStyle;
  try { style = formulaStyleOf(inp.meta.locale); } catch (e) {
    return emptyPlan(tk, 'BLOCKED', 'UNSUPPORTED_LOCALE', [e instanceof Error ? e.message : String(e)]);
  }
  const loc = locateSection(inp.columnA, inp.target);

  // Месяц уже есть → только проверка, никакой записи.
  if (loc.status === 'AMBIGUOUS') return emptyPlan(tk, 'MONTH_SECTION_INVALID', 'MONTH_SECTION_AMBIGUOUS', [`заголовок месяца в строках ${loc.rows.join(', ')}`]);
  if (loc.status === 'FOUND') {
    const g = geometryAt(inp.target, loc.topRow);
    if (!inp.existing || g.spacerRow > inp.meta.rowCount) {
      return { ...emptyPlan(tk, 'MONTH_SECTION_PARTIAL', 'MONTH_SECTION_PARTIAL', [`секция A${g.topRow} требует строк до ${g.spacerRow}, в листе ${inp.meta.rowCount}`]), geometry: g };
    }
    const v = validateSection(inp.existing);
    const headerMissing = String(cellAt(inp.existing, g.headerRow, SUMMARY.date) ?? '').trim() === '';
    if (v.blocks.length === 0 || headerMissing) {
      return { ...emptyPlan(tk, 'MONTH_SECTION_PARTIAL', 'MONTH_SECTION_PARTIAL', [...v.sectionIssues, ...v.driftIssues].slice(0, 20)), geometry: g, layout: v.layout };
    }
    if (v.sectionIssues.length || v.driftIssues.length) {
      return { ...emptyPlan(tk, 'MONTH_SECTION_INVALID', 'MONTH_SECTION_INVALID', [...v.sectionIssues, ...v.driftIssues].slice(0, 20)), geometry: g, layout: v.layout };
    }
    const mapped = new Set(v.blocks.map((b) => b.nmId));
    return {
      ...emptyPlan(tk, 'NO_CHANGE', null, ['секция месяца уже существует и проходит контракт — раскладка заморожена']),
      geometry: g, layout: v.layout, unmappedActive: inp.population.map((p) => p.nmId).filter((n) => !mapped.has(n)).sort((a, b) => a - b),
    };
  }

  // Месяца нет → нужен валидный предыдущий месяц в самом конце листа.
  const pk = previousMonth(inp.target);
  const ploc = locateSection(inp.columnA, pk);
  if (ploc.status !== 'FOUND' || !inp.predecessor) {
    return emptyPlan(tk, 'BLOCKED', 'PREDECESSOR_SECTION_MISSING', [`секция предыдущего месяца ${formatMonthKey(pk)}: ${ploc.status === 'AMBIGUOUS' ? 'неоднозначна' : 'не найдена или не прочитана'}`]);
  }
  const pred = inp.predecessor;
  const pg = pred.geometry;
  if (pg.topRow !== ploc.topRow) return emptyPlan(tk, 'BLOCKED', 'PREDECESSOR_SECTION_MISSING', ['снимок предыдущего месяца не соответствует заголовку']);
  const pv = validateSection(pred);
  if (pv.sectionIssues.length || pv.driftIssues.length) {
    return emptyPlan(tk, 'BLOCKED', 'PREDECESSOR_INVALID', [...pv.sectionIssues, ...pv.driftIssues].slice(0, 20));
  }
  if (inp.meta.rowCount !== pg.spacerRow) {
    return emptyPlan(tk, 'BLOCKED', 'SHEET_TAIL_NOT_EMPTY', [`лист кончается строкой ${inp.meta.rowCount}, а разделитель ${pg.monthKey} — ${pg.spacerRow}: после предыдущего месяца есть строки`]);
  }

  // Популяция.
  const reasons: string[] = [];
  const seen = new Set<number>();
  for (const p of inp.population) {
    if (seen.has(p.nmId)) return emptyPlan(tk, 'BLOCKED', 'DUPLICATE_SKU', [`nm_id ${p.nmId} дважды в популяции`]);
    seen.add(p.nmId);
  }
  if (seen.size === 0) return emptyPlan(tk, 'BLOCKED', 'EMPTY_POPULATION', ['нет активных SKU WB']);

  const extracted = extractPredecessorParams(pred, pv.layout, style);
  if (extracted.mismatches.length) {
    return emptyPlan(tk, 'BLOCKED', 'TEMPLATE_MISMATCH', [`построители формул не воспроизводят ${pg.monthKey}: ${extracted.mismatches.slice(0, 20).join(', ')}`]);
  }

  const blocks: PlannedBlock[] = [];
  const retired: number[] = [];
  for (const b of pv.blocks) {
    if (!seen.has(b.nmId)) { retired.push(b.nmId); continue; }
    blocks.push({ ...b, index: blocks.length, origin: 'CARRIED', params: { ...extracted.params.get(b.nmId)!, stockProjection: GENERATED_STOCK_PROJECTION }, cogsProvenance: null });
  }
  const predNm = new Set(pv.blocks.map((b) => b.nmId));
  const fresh = inp.population.filter((p) => !predNm.has(p.nmId)).sort((a, b) => a.nmId - b.nmId);
  let slot = pv.blocks.reduce((m, b) => Math.max(m, b.slot), -1);
  const provenance: CogsProvenance[] = [];
  for (const p of fresh) {
    slot = nextFreeSlot(slot);
    const c = resolveCanonicalCogs(inp.cogs, p.nmId);
    if ('error' in c) return emptyPlan(tk, 'BLOCKED', c.error, [`новый блок ${p.nmId}: канон COGS — ${c.error}; 0 не подставляется, нужна публикация канона или решение владельца`]);
    provenance.push(c);
    const start = slotStart(slot);
    const tmpl = blocks[blocks.length - 1]?.params ?? [...extracted.params.values()][0]!;
    blocks.push({
      index: blocks.length, slot, start, nmId: p.nmId, title: `${p.nmId} ${p.name ?? ''}`.trim(), origin: 'NEW',
      // Шаблон — последний перенесённый блок (сентябрь: блок 24 — константа 10, без проекции хранения).
      // Единственное новое — COGS из канона. Проекция остатка — с защитой LCD, как у всех блоков месяца.
      params: { start, cogsTerm: cogsLiteral(c.cogs), overhead: tmpl.overhead, stockProjection: GENERATED_STOCK_PROJECTION, storageProjection: 'none' },
      cogsProvenance: c,
    });
  }
  blocks.sort((a, b) => a.slot - b.slot).forEach((b, i) => { b.index = i; });
  if (blocks.length === 0) return emptyPlan(tk, 'BLOCKED', 'EMPTY_POPULATION', ['ни одного блока для нового месяца']);
  // Цепочка блоков обязана быть сплошной (visual hardening 2): выбывший SKU посреди цепочки оставил бы дыру —
  // месяц не готовится, решение владельца (вернуть SKU в реестр или выводить его блок явно).
  const gaps = chainGaps(blocks);
  if (gaps.length) return emptyPlan(tk, 'BLOCKED', 'CHAIN_GAP', [`выбывшие SKU ${retired.join(', ')} оставляют дыру в цепочке блоков (слоты ${gaps.join(', ')}) — сплошная цепочка нарушена; нужно решение владельца`]);
  if (retired.length) reasons.push(`выбывшие SKU (последние слоты остаются пустыми): ${retired.join(', ')}`);

  const g = geometryAt(inp.target, pg.nextTopRow);
  const layout = layoutOf(g, blocks.map(({ index, slot: s, start, nmId, title }) => ({ index, slot: s, start, nmId, title })));
  const lastSlot = blocks[blocks.length - 1]!.slot;
  const cells: CellWrite[] = [];
  const notes: NoteWrite[] = [];
  const put = (row: number, col: number, value: CellValueWrite): void => { cells.push({ row, col, value }); };
  const fml = (row: number, col: number, text: string): void => put(row, col, { kind: 'formula', text });

  // Заголовок месяца и блоков.
  put(g.topRow, 1, { kind: 'string', value: g.title });
  for (const b of blocks) put(g.topRow, b.start, { kind: 'string', value: b.title });
  // Шапка: сводка — из прошлого месяца («N SKU» пересчитано), блок — из того же слота (новый — из первого блока).
  for (let c = 1; c <= SUMMARY_LAST_COLUMN; c++) {
    const v = cellAt(pred, pg.headerRow, c);
    if (typeof v === 'string' && v !== '') put(g.headerRow, c, { kind: 'string', value: v.replace(/\b\d+ SKU\b/, `${blocks.length} SKU`) });
  }
  const predBySlot = new Map(pv.blocks.map((b) => [b.slot, b]));
  const firstPred = pv.blocks[0]!;
  for (const b of blocks) {
    const src = predBySlot.get(b.slot) ?? firstPred;
    for (let o = 0; o < BLOCK_WIDTH; o++) {
      const v = cellAt(pred, pg.headerRow, src.start + o);
      if (typeof v === 'string' && v !== '') put(g.headerRow, b.start + o, { kind: 'string', value: v });
    }
  }
  // Дни.
  let manualBlank = 0;
  for (let i = 0; i < g.daysInMonth; i++) {
    const row = dayRowOf(g, i);
    const serial = isoToSerial(addDaysIso(g.monthStart, i));
    put(row, SUMMARY.date, { kind: 'number', value: serial });
    for (const [col, f] of summaryDayFormulas(row, lastSlot, blocks[0]!.start)) fml(row, col, f);
    for (const b of blocks) {
      put(row, b.start + OFFSET.date, { kind: 'number', value: serial });
      for (const [off, f] of blockDayFormulas(b.params, row)) fml(row, b.start + off, f);
      for (const [off, f] of blockProjectionFormulas(b.params, row, i === 0)) fml(row, b.start + off, f);
      manualBlank += 2; // СПП и блогеры — пустые, ввод владельца
    }
  }
  // MTD.
  for (const [col, f] of summaryMtdFormulas(g, lastSlot)) fml(g.mtdRow, col, f);
  put(g.mtdRow, blocks[0]!.start + OFFSET.date, { kind: 'string', value: MTD_LABEL });
  for (const b of blocks) for (const [off, f] of blockMtdFormulas(b.start, g)) fml(g.mtdRow, b.start + off, f);
  // Происхождение COGS нового блока — заметка на AI первого дня (видна в листе, по-русски).
  for (const b of blocks) {
    const p = b.cogsProvenance;
    if (!p) continue;
    notes.push({
      row: g.firstDailyRow, col: b.start + OFFSET.unitProfit,
      note: `Себестоимость ${p.cogs} ₽ подставлена при подготовке месяца ${g.monthKey} из канона ${p.source}: `
        + `значение на ${p.day}, публикация ${p.publishedAt ?? 'н/д'}, run ${p.runId ?? 'н/д'}.`,
    });
  }

  // Форматы: копия строк прошлого месяца по типу строки. ВСЕ дни — из первого дня прошлого месяца
  // (эталон контракта формата закрытого дня, E3): статический формат всех строк дней одинаков по
  // построению, и контракт Engine выполняется без перекраски; будущий вид даёт нейтрализация (visual.ts)
  // и правила УФ, а не другая строка-шаблон (в живом сентябре строки 737 и 766 отличались в мелочах).
  const predLast = pv.layout.lastBlockColumn;
  const rowPairs: Array<[number, number, number]> = [
    [pg.topRow, g.topRow, g.topRow], [pg.headerRow, g.headerRow, g.headerRow],
    // Дни — две полосы одного шаблона (первый день прошлого месяца): у первого дня нет верхней линии (над ним линия
    // шапки), у дней 2..N она есть — полоса форматов не может объединять строки разного типа границ.
    [pg.firstDailyRow, g.firstDailyRow, g.firstDailyRow], [pg.firstDailyRow, g.firstDailyRow + 1, g.lastDailyRow],
    [pg.mtdRow, g.mtdRow, g.mtdRow], [pg.spacerRow, g.spacerRow, g.spacerRow],
  ];
  const formatCopies: Array<{ source: GridRect; dest: GridRect }> = [];
  const lastPred = pv.blocks[pv.blocks.length - 1]!;
  for (const [src, d1, d2] of rowPairs) {
    formatCopies.push({ source: { r1: src, r2: src, c1: 1, c2: predLast }, dest: { r1: d1, r2: d2, c1: 1, c2: predLast } });
    // L — часть сводки: заголовок, шапка, MTD и план берут вид колонки A (день недели сводки); границы — контракт.
    if (d1 < g.firstDailyRow || d1 > g.lastDailyRow) formatCopies.push({ source: { r1: src, r2: src, c1: SUMMARY.weekday, c2: SUMMARY.weekday }, dest: { r1: d1, r2: d2, c1: SUMMARY_LAST_COLUMN, c2: SUMMARY_LAST_COLUMN } });
    for (const b of blocks.filter((x) => x.origin === 'NEW')) {
      formatCopies.push({ source: { r1: src, r2: src, c1: lastPred.start, c2: lastPred.start + BLOCK_WIDTH - 1 }, dest: { r1: d1, r2: d2, c1: b.start, c2: b.start + BLOCK_WIDTH - 1 } });
    }
  }
  // Геометрия вставки: новые блоки идут сплошь за последним блоком прошлого месяца; колонки вставляются
  // перед хвостом книги, якоря сдвигаются на ширину вставки.
  const insertAt = pv.layout.lastBlockColumn;                       // 0-based index вставки = колонка VP (588)
  const insertCount = blocks.filter((b) => b.origin === 'NEW').length * BLOCK_WIDTH;
  const anchorAfter = inp.meta.anchorCol > insertAt ? inp.meta.anchorCol + insertCount : inp.meta.anchorCol;
  const merges: GridRect[] = [{ r1: g.topRow, r2: g.topRow, c1: 1, c2: MONTH_TITLE_MERGE_LAST_COL }];
  for (const b of blocks) merges.push({ r1: g.topRow, r2: g.topRow, c1: b.start, c2: b.start + BLOCK_TITLE_MERGE_WIDTH - 1 });

  if (!inp.structure) return emptyPlan(tk, 'BLOCKED', 'STRUCTURE_UNAVAILABLE', ['структура листа (УФ, размеры) не прочитана — секцию без УФ и размеров не создаём']);
  const needRows = templateRowsOf(pg);
  if (!inp.rowFormats || needRows.some((r) => !inp.rowFormats!.has(r))) {
    return emptyPlan(tk, 'BLOCKED', 'TEMPLATE_FORMATS_UNAVAILABLE', [`форматы строк-шаблонов ${needRows.join(', ')} не прочитаны`]);
  }
  const plan: MonthPrepPlan = {
    status: 'PLAN_CREATE', code: null, target: tk, reasons, geometry: g, layout,
    predecessor: { monthKey: pg.monthKey, topRow: pg.topRow, blocks: pv.blocks.length },
    blocks, retiredNmIds: retired, unmappedActive: [],
    appendRows: g.spacerRow - inp.meta.rowCount,
    insertColumns: insertCount > 0 ? { at: insertAt, count: insertCount } : null, cfTrims: [],
    anchorCol: anchorAfter, groupRequests: [], chainGaps: chainGaps(blocks),
    merges, formatCopies,
    conditionalFormats: null, dimensionRequests: [], formulaStyle: style, sheetId: inp.meta.sheetId, cfStartIndex: 0,
    templateFormats: inp.rowFormats ?? null,
    // Вставленные колонки наследуют формат колонки перед ними (inheritFromBefore) во ВСЕХ старых строках — сброс.
    resetInheritedFormats: insertCount > 0 ? { r1: 1, r2: inp.meta.rowCount, c1: insertAt + 1, c2: insertAt + insertCount } : null,
    cells, notes, manualBlankCells: manualBlank, cogsProvenance: provenance,
  };
  const newSlots = blocks.filter((b) => b.origin === 'NEW').map((b) => b.slot);
  const predSlots = pv.blocks.map((b) => b.slot);
  if (insertCount > 0) {
    const { trims, unsafe } = cfInsertTrims(inp.structure.conditionalFormats, insertAt, insertCount);
    if (unsafe.length) return emptyPlan(tk, 'BLOCKED', 'CF_TRIM_UNSAFE', unsafe);
    plan.cfTrims = trims;
  }
  plan.conditionalFormats = buildConditionalFormats(layout, inp.meta.sheetId, style, anchorAfter);
  plan.cfStartIndex = inp.structure.conditionalFormats.length;
  plan.dimensionRequests = dimensionRequests(inp.structure, pg, g, Math.max(...predSlots), newSlots, inp.meta.sheetId);
  // Группы колонок новых блоков — как у блока-шаблона (последний блок прошлого месяца): те же смещения.
  const tpl = Math.max(...predSlots);
  const tplGroups = inp.structure.columnGroups.filter((gr) => gr.startIndex >= slotStart(tpl) - 1 && gr.endIndex <= slotStart(tpl) + BLOCK_WIDTH - 1);
  for (const s of newSlots) for (const gr of tplGroups) {
    const delta = slotStart(s) - slotStart(tpl);
    plan.groupRequests.push({ addDimensionGroup: { range: { sheetId: inp.meta.sheetId, dimension: 'COLUMNS', startIndex: gr.startIndex + delta, endIndex: gr.endIndex + delta } } });
  }
  assertPlanConfined(plan);
  return plan;
}

/**
 * Предохранитель: всё, что пишет план, — не выше строки заголовка нового месяца. Прошлые месяцы
 * (в том числе якоря книги WB736..WB739) план не адресует никогда.
 */
export function assertPlanConfined(plan: MonthPrepPlan): void {
  const top = plan.geometry?.topRow;
  if (top === undefined) return;
  const ins = plan.insertColumns;
  const inInserted = (c: number): boolean => !!ins && c >= ins.at + 1 && c <= ins.at + ins.count;
  const bad = [
    ...plan.cells.filter((c) => c.row < top).map((c) => `${colA1(c.col)}${c.row}`),
    ...plan.notes.filter((c) => c.row < top).map((c) => `note ${colA1(c.col)}${c.row}`),
    ...plan.merges.filter((m) => m.r1 < top).map((m) => `merge ${m.r1}`),
    ...plan.formatCopies.filter((f) => f.dest.r1 < top).map((f) => `format ${f.dest.r1}`),
    // Сброс унаследованных форматов — только во вставленных колонках.
    ...(plan.resetInheritedFormats && !(inInserted(plan.resetInheritedFormats.c1) && inInserted(plan.resetInheritedFormats.c2)) ? ['сброс форматов вне вставленных колонок'] : []),
    ...(plan.conditionalFormats?.rules ?? []).flatMap((r) => r.ranges)
      .filter((x) => (x.startRowIndex ?? 0) < top - 1).map((x) => `УФ ${(x.startRowIndex ?? 0) + 1}`),
    ...plan.dimensionRequests.map((r) => (r.updateDimensionProperties as { range: { dimension: string; startIndex: number } }).range)
      .filter((d) => (d.dimension === 'ROWS' ? d.startIndex < top - 1 : !inInserted(d.startIndex + 1)))
      .map((d) => `размер ${d.dimension} ${d.startIndex + 1}`),
    ...plan.groupRequests.map((r) => (r.addDimensionGroup as { range: { startIndex: number; endIndex: number } }).range)
      .filter((d) => !inInserted(d.startIndex + 1) || !inInserted(d.endIndex)).map((d) => `группа ${d.startIndex + 1}`),
    // Переиздание правил УФ прошлых месяцев: только существующие правила и ни одного диапазона во вставленных колонках.
    ...plan.cfTrims.filter((t) => !ins || t.index >= plan.cfStartIndex || t.rule.ranges.some((x) => (x.startColumnIndex ?? 0) < ins.at + ins.count && (x.endColumnIndex ?? Infinity) > ins.at))
      .map((t) => `переиздание УФ ${t.index} задевает вставленные колонки`),
    ...(plan.chainGaps.length ? [`дыры в цепочке блоков: слоты ${plan.chainGaps.join(', ')}`] : []),
  ];
  if (bad.length) {
    throw new RangeError(`план подготовки месяца выходит за пределы новой секции: ${bad.slice(0, 10).join(', ')}`);
  }
}

/* ───────────────────────── план → запросы batchUpdate ───────────────────────── */

const gridRange = (sheetId: number, r: GridRect): Record<string, number> => ({
  sheetId, startRowIndex: r.r1 - 1, endRowIndex: r.r2, startColumnIndex: r.c1 - 1, endColumnIndex: r.c2,
});

function cellData(v: CellValueWrite | undefined, style: FormulaStyle): Record<string, unknown> {
  if (!v) return {};
  if (v.kind === 'formula') return { userEnteredValue: { formulaValue: toLocaleFormula(v.text, style) } };
  if (v.kind === 'number') return { userEnteredValue: { numberValue: v.value } };
  return { userEnteredValue: { stringValue: v.value } };
}

/**
 * Запросы одного spreadsheets.batchUpdate: вставить колонки новых блоков → переиздать правила УФ, которые вставка
 * расширила бы на новый блок → добавить строки → сброс
 * унаследованных форматов во вставленных колонках старых строк → форматы (шаблон + контракт границ) →
 * значения и формулы (синтаксис локали книги) → заметки COGS → объединения → УФ (визуальный контракт) →
 * размеры → группы колонок. copyPaste не используется.
 * Только для PLAN_CREATE; иначе — пустой список.
 */
export function toStructureRequests(plan: MonthPrepPlan, sheetId: number): StructureRequest[] {
  if (plan.status !== 'PLAN_CREATE' || !plan.geometry) return [];
  assertPlanConfined(plan);
  const req: StructureRequest[] = [];
  // Колонки новых блоков — вставка сразу за последним блоком (хвост книги сдвигается вправо);
  // свойства наследуются от колонки перед вставкой (VP), затем задаются явно (dimensionRequests).
  if (plan.insertColumns) {
    req.push({ insertDimension: { range: { sheetId, dimension: 'COLUMNS', startIndex: plan.insertColumns.at, endIndex: plan.insertColumns.at + plan.insertColumns.count }, inheritFromBefore: true } });
    for (const t of plan.cfTrims) req.push({ updateConditionalFormatRule: { sheetId, index: t.index, rule: t.rule } });
  }
  if (plan.appendRows > 0) req.push({ appendDimension: { sheetId, dimension: 'ROWS', length: plan.appendRows } });
  // Сброс форматов, унаследованных вставленными колонками в старых строках.
  if (plan.resetInheritedFormats) {
    req.push({ repeatCell: { range: gridRange(sheetId, plan.resetInheritedFormats), cell: {}, fields: 'userEnteredFormat' } });
  }
  // ФОРМАТЫ новой секции — явный userEnteredFormat строк-шаблонов (без УФ) + границы по контракту
  // (borderSpec), repeatCell сериями одинаковых форматов. Колонки вне карты — сброс.
  const g = plan.geometry;
  const W = plan.layout!.lastBlockColumn;
  const covered = new Map<string, RawCellFormat>();
  for (const f of plan.formatCopies) {
    const src = plan.templateFormats?.get(f.source.r1);
    if (!src) throw new RangeError(`нет формата строки-шаблона ${f.source.r1}`);
    const dayBand = f.dest.r1 >= g.firstDailyRow && f.dest.r2 <= g.lastDailyRow;
    for (let c = f.dest.c1; c <= f.dest.c2; c++) {
      const raw = src[f.source.c1 - 1 + (c - f.dest.c1)] ?? null;
      covered.set(`${f.dest.r1}|${f.dest.r2}|${c}`, dayBand ? neutralizeDayFormat(raw, columnKind(c, plan.layout!)) : raw);
    }
  }
  const bands = [...new Set(plan.formatCopies.map((f) => `${f.dest.r1}|${f.dest.r2}`))].map((k) => k.split('|').map(Number) as [number, number]);
  for (const [r1, r2] of bands.sort((a, b) => a[0] - b[0])) {
    const kind = rowKindOf(g, r1);
    // Полоса форматов — строки ОДНОГО типа: иначе границы всей полосы были бы взяты по её первой строке.
    for (let r = r1 + 1; r <= r2; r++) if (rowKindOf(g, r) !== kind) throw new RangeError(`полоса форматов ${r1}..${r2} объединяет строки разного типа (${kind} / ${rowKindOf(g, r)})`);
    let run: { c1: number; key: string; fmt: RawCellFormat } | null = null;
    const flush = (cEnd: number): void => {
      if (!run) return;
      const range = gridRange(sheetId, { r1, r2, c1: run.c1, c2: cEnd });
      req.push({ repeatCell: { range, cell: run.fmt ? { userEnteredFormat: run.fmt } : {}, fields: 'userEnteredFormat' } });
      run = null;
    };
    for (let c = 1; c <= W; c++) {
      const base = covered.has(`${r1}|${r2}|${c}`) ? covered.get(`${r1}|${r2}|${c}`)! : null;
      const spec = kind ? borderSpec(kind, c, plan.layout!) : null;
      const fmt: RawCellFormat = spec ? { ...(base ?? {}), borders: bordersJson(spec) } : base;
      const key = JSON.stringify(fmt);
      if (run && run.key === key) continue;
      flush(c - 1);
      run = { c1: c, key, fmt };
    }
    flush(W);
  }
  // Значения и формулы — updateCells по строкам (поле userEnteredValue: форматы не трогает).
  const byRow = new Map<number, Map<number, CellValueWrite>>();
  for (const c of plan.cells) (byRow.get(c.row) ?? byRow.set(c.row, new Map()).get(c.row)!).set(c.col, c.value);
  for (const [row, cols] of [...byRow.entries()].sort((a, b) => a[0] - b[0])) {
    const max = Math.max(...cols.keys());
    const values = Array.from({ length: max }, (_, i) => cellData(cols.get(i + 1), plan.formulaStyle));
    req.push({ updateCells: { start: { sheetId, rowIndex: row - 1, columnIndex: 0 }, rows: [{ values }], fields: 'userEnteredValue' } });
  }
  for (const n of plan.notes) {
    req.push({ updateCells: { start: { sheetId, rowIndex: n.row - 1, columnIndex: n.col - 1 }, rows: [{ values: [{ note: n.note }] }], fields: 'note' } });
  }
  for (const m of plan.merges) req.push({ mergeCells: { range: gridRange(sheetId, m), mergeType: 'MERGE_ALL' } });
  if (plan.conditionalFormats) req.push(...cfRequests(plan.conditionalFormats, sheetId, plan.cfStartIndex));
  req.push(...plan.dimensionRequests);
  req.push(...plan.groupRequests);
  return req;
}

/** Сколько ручных ячеек (СПП, блогеры) план бы записал — контракт: 0. */
export function manualCellsWritten(plan: MonthPrepPlan): number {
  const manual = new Set<number>([OFFSET.spp, OFFSET.bloggers]);
  return plan.cells.filter((c) => plan.blocks.some((b) => c.col - b.start >= 0 && c.col - b.start < BLOCK_WIDTH && manual.has(c.col - b.start) && c.row >= (plan.geometry?.firstDailyRow ?? 0) && c.row <= (plan.geometry?.lastDailyRow ?? 0))).length;
}
