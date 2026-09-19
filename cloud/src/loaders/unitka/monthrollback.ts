/**
 * UNITKA CALENDAR V2 — исполняемый откат СОЗДАНИЯ месяца. ЧИСТЫЙ модуль: ни Sheets, ни BigQuery.
 *
 * Не «восстановление книги», а ровно обратные действия к тому, что сделала подготовка месяца (monthprep.ts):
 *   удалить правила УФ новой секции → удалить вставленные колонки новых блоков (хвост книги, якоря и именованный диапазон
 *   REVERSE_LEG_RATE возвращаются на место сами) → удалить строки секции → вернуть ширины расширенных колонок →
 *   вернуть первую колонку хвоста в его группу. Объединения, заметки, форматы, размеры и группы новых блоков лежат
 *   внутри удаляемых строк/колонок и исчезают вместе с ними; проверок данных подготовка месяца не создаёт.
 *
 * МАНИФЕСТ строится ДО записи из плана и состояния книги до записи (prep.ts пишет его в журнал; запись месяца без
 * предъявленного отпечатка манифеста отказывает). Откат исполняется только по манифесту и только если живая книга
 * совпадает с записанным в нём состоянием «после создания»: иначе — отказ с кодом, ни одной мутации (fail-closed).
 * После отката структура листа сверяется с отпечатками «до создания» из манифеста.
 */
import { createHash } from 'node:crypto';
import { formatMonthKey, locateSection, nextMonth, parseMonthKey, geometryAt, dayRowOf, tailStartOf } from './calendar.js';
import { OFFSET, colA1, findBlocks, isEmpty, type CellValue } from './model.js';
import type { MonthPrepPlan } from './monthprep.js';
import { planMonthRollback, type RowHeights, type WidthUpgrade } from './monthprep_struct.js';
import type { ColumnGroup, SheetMeta, SheetStructure, StructureRequest } from './sheets.js';

export const ROLLBACK_MANIFEST_VERSION = 1;
export const ROLLBACK_MANIFEST_KIND = 'unitka-calendar-v2/month-create';

/** JSON с отсортированными ключами: один и тот же объект → одна и та же строка (для отпечатков). */
export function canonicalJson(v: unknown): string {
  if (v === null || typeof v !== 'object') return JSON.stringify(v) ?? 'null';
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(',')}]`;
  const o = v as Record<string, unknown>;
  return `{${Object.keys(o).filter((k) => o[k] !== undefined).sort().map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`).join(',')}}`;
}
const sha = (v: unknown): string => createHash('sha256').update(canonicalJson(v)).digest('hex');

export interface GroupState { startIndex: number; endIndex: number; depth: number; collapsed: boolean }
const groupsOf = (gs: readonly ColumnGroup[] | undefined): GroupState[] =>
  [...(gs ?? [])].map((g) => ({ startIndex: g.startIndex, endIndex: g.endIndex, depth: g.depth, collapsed: g.collapsed === true }))
    .sort((a, b) => a.startIndex - b.startIndex || a.endIndex - b.endIndex || a.depth - b.depth);

/** Отпечатки структуры листа: то, что подготовка месяца меняет или могла бы задеть. Значения ячеек сюда не входят. */
export interface StructureFingerprint {
  conditionalFormats: string;
  merges: string;
  columnWidths: string;
  rowHeights: string;
  /** Состояние вида (скрытые колонки/строки) — отдельно: его меняет и владелец, сворачивая группы. */
  columnHidden: string;
  rowHidden: string;
}
export function fingerprintOf(st: SheetStructure): StructureFingerprint {
  return {
    conditionalFormats: sha(st.conditionalFormats),
    merges: sha([...st.merges].map((m) => canonicalJson(m)).sort()),
    columnWidths: sha(st.columnMetadata.map((c) => c.pixelSize ?? null)),
    rowHeights: sha(st.rowMetadata.map((r) => r.pixelSize ?? null)),
    columnHidden: sha(st.columnMetadata.map((c) => c.hiddenByUser === true)),
    rowHidden: sha(st.rowMetadata.map((r) => r.hiddenByUser === true)),
  };
}

export interface ManifestBlock { slot: number; nmId: number; start: number; origin: 'CARRIED' | 'NEW' }

export interface RollbackManifest {
  version: number;
  kind: string;
  spreadsheetId: string;
  sheetId: number;
  sheetName: string;
  /** Созданный месяц, YYYY-MM. */
  target: string;
  /** Состояние листа ДО создания месяца — к нему возвращает откат. */
  before: {
    rowCount: number; columnCount: number; anchorCol: number;
    cfRules: number; merges: number;
    columnGroups: GroupState[]; rowGroups: GroupState[];
    fingerprint: StructureFingerprint;
  };
  /** Что именно создаёт подготовка месяца — по этому откат узнаёт «свою» секцию и отказывает, если книга иная. */
  created: {
    title: string;
    topRow: number; headerRow: number; firstDailyRow: number; lastDailyRow: number; mtdRow: number; spacerRow: number; days: number;
    blocks: ManifestBlock[];
    /** Семейства формулы остатка MTD созданных блоков (production: native) — для просмотра плана. */
    mtdStockFamily: Record<string, number>;
    /** Сколько ячеек (значения и формулы) пишет план и их отпечаток: запись обязана совпасть с просмотренным планом и по содержимому. */
    cells: number;
    cellsDigest: string;
    appendRows: number;
    insertedColumns: { at: number; count: number } | null;
    rowCountAfter: number; columnCountAfter: number; anchorColAfter: number;
    cfRulesAdded: number; cfRulesReissued: number[];
    mergesCreated: number; notesCreated: number; validationsCreated: number;
    widthUpgrades: WidthUpgrade[]; rowHeights: RowHeights | null;
    tailGroupDetachedColumn: number | null;
    groupsAdded: Array<{ startIndex: number; endIndex: number }>;
    groupsTrimmed: Array<{ startIndex: number; endIndex: number }>;
    requests: number;
  };
  /** sha256 канонического JSON полей version..created. Поля ниже в отпечаток не входят. */
  digest: string;
  engine: string;
  gitSha: string;
  generatedAt: string;
}

const digestOf = (m: Pick<RollbackManifest, 'version' | 'kind' | 'spreadsheetId' | 'sheetId' | 'sheetName' | 'target' | 'before' | 'created'>): string =>
  sha({ version: m.version, kind: m.kind, spreadsheetId: m.spreadsheetId, sheetId: m.sheetId, sheetName: m.sheetName, target: m.target, before: m.before, created: m.created });

const groupRange = (r: StructureRequest, key: 'addDimensionGroup' | 'deleteDimensionGroup'): { startIndex: number; endIndex: number } | null => {
  const x = (r[key] as { range?: { startIndex?: number; endIndex?: number } } | undefined)?.range;
  return x && x.startIndex !== undefined && x.endIndex !== undefined ? { startIndex: x.startIndex, endIndex: x.endIndex } : null;
};

/** Манифест отката — из плана PLAN_CREATE и состояния листа ДО записи. */
export function buildRollbackManifest(a: {
  plan: MonthPrepPlan; meta: SheetMeta; structure: SheetStructure; spreadsheetId: string; sheetName: string;
  requests: number; engine: string; gitSha: string; now: Date;
}): RollbackManifest {
  const { plan, meta, structure: st } = a;
  const g = plan.geometry;
  if (plan.status !== 'PLAN_CREATE' || !g) throw new RangeError(`манифест отката строится только для PLAN_CREATE, получено ${plan.status}`);
  const ins = plan.insertColumns;
  const body = {
    version: ROLLBACK_MANIFEST_VERSION, kind: ROLLBACK_MANIFEST_KIND,
    spreadsheetId: a.spreadsheetId, sheetId: meta.sheetId, sheetName: a.sheetName, target: plan.target,
    before: {
      rowCount: meta.rowCount, columnCount: meta.columnCount, anchorCol: meta.anchorCol,
      cfRules: st.conditionalFormats.length, merges: st.merges.length,
      columnGroups: groupsOf(st.columnGroups), rowGroups: groupsOf(st.rowGroups),
      fingerprint: fingerprintOf(st),
    },
    created: {
      title: g.title, topRow: g.topRow, headerRow: g.headerRow, firstDailyRow: g.firstDailyRow, lastDailyRow: g.lastDailyRow, mtdRow: g.mtdRow, spacerRow: g.spacerRow, days: g.daysInMonth,
      blocks: plan.blocks.map((b) => ({ slot: b.slot, nmId: b.nmId, start: b.start, origin: b.origin })),
      mtdStockFamily: plan.blocks.reduce<Record<string, number>>((m, b) => { m[b.params.mtdStockFamily] = (m[b.params.mtdStockFamily] ?? 0) + 1; return m; }, {}),
      // Заметки о происхождении COGS в отпечаток не входят: в них время публикации канона, оно меняется ежечасно.
      cells: plan.cells.length, cellsDigest: sha(plan.cells.map((x) => [x.row, x.col, x.value])),
      appendRows: plan.appendRows,
      insertedColumns: ins ? { at: ins.at, count: ins.count } : null,
      rowCountAfter: meta.rowCount + plan.appendRows, columnCountAfter: meta.columnCount + (ins?.count ?? 0), anchorColAfter: plan.anchorCol,
      cfRulesAdded: plan.conditionalFormats?.rules.length ?? 0, cfRulesReissued: plan.cfTrims.map((t) => t.index),
      mergesCreated: plan.merges.length, notesCreated: plan.notes.length, validationsCreated: 0,
      widthUpgrades: plan.widthUpgrades.map((u) => ({ col: u.col, from: u.from, to: u.to })), rowHeights: plan.rowHeights,
      tailGroupDetachedColumn: plan.tailGroupDetachedColumn,
      groupsAdded: plan.groupRequests.map((r) => groupRange(r, 'addDimensionGroup')).filter((x): x is { startIndex: number; endIndex: number } => x !== null),
      groupsTrimmed: plan.groupRequests.map((r) => groupRange(r, 'deleteDimensionGroup')).filter((x): x is { startIndex: number; endIndex: number } => x !== null),
      requests: a.requests,
    },
  };
  return { ...body, digest: digestOf(body), engine: a.engine, gitSha: a.gitSha, generatedAt: a.now.toISOString() };
}

/** Манифест для передачи в переменной окружения: base64url канонического JSON (без запятых, «=» и «/»). */
export function encodeManifest(m: RollbackManifest): string {
  return Buffer.from(canonicalJson(m), 'utf8').toString('base64url');
}

/** Разбор манифеста (JSON или base64url). Любая неполнота, чужой вид/версия или несовпадение отпечатка — отказ. */
export function parseManifest(raw: string): RollbackManifest | { error: string } {
  const text = raw.trim();
  if (!text) return { error: 'манифест отката не передан' };
  let obj: unknown;
  try { obj = JSON.parse(text.startsWith('{') ? text : Buffer.from(text, 'base64url').toString('utf8')); } catch { return { error: 'манифест отката не разбирается как JSON' }; }
  const m = obj as Partial<RollbackManifest> | null;
  if (!m || typeof m !== 'object') return { error: 'манифест отката — не объект' };
  if (m.kind !== ROLLBACK_MANIFEST_KIND || m.version !== ROLLBACK_MANIFEST_VERSION) return { error: `вид/версия манифеста: ${String(m.kind)} v${String(m.version)}` };
  const b = m.before, c = m.created;
  const ints = (...xs: unknown[]): boolean => xs.every((x) => Number.isInteger(x) && (x as number) >= 0);
  if (typeof m.spreadsheetId !== 'string' || typeof m.sheetName !== 'string' || typeof m.target !== 'string' || !ints(m.sheetId) || !b || !c) return { error: 'в манифесте нет обязательных полей' };
  if (!ints(b.rowCount, b.columnCount, b.anchorCol, b.cfRules, b.merges) || !Array.isArray(b.columnGroups) || !Array.isArray(b.rowGroups) || !b.fingerprint) return { error: 'манифест: состояние «до» неполно' };
  if (!ints(c.topRow, c.headerRow, c.firstDailyRow, c.lastDailyRow, c.mtdRow, c.spacerRow, c.days, c.appendRows, c.rowCountAfter, c.columnCountAfter, c.anchorColAfter, c.cfRulesAdded)
    || typeof c.title !== 'string' || typeof c.cellsDigest !== 'string' || !ints(c.cells) || !Array.isArray(c.blocks) || !Array.isArray(c.widthUpgrades) || !Array.isArray(c.cfRulesReissued)) return { error: 'манифест: описание созданной секции неполно' };
  if (c.insertedColumns !== null && !ints(c.insertedColumns?.at, c.insertedColumns?.count)) return { error: 'манифест: вставленные колонки заданы неверно' };
  try { parseMonthKey(m.target); } catch { return { error: `манифест: месяц «${m.target}»` }; }
  const full = m as RollbackManifest;
  if (digestOf(full) !== full.digest) return { error: 'отпечаток манифеста не совпадает с содержимым — манифест изменён или обрезан' };
  // Внутренняя согласованность: геометрия манифеста = геометрия календаря для этого месяца.
  const g = geometryAt(parseMonthKey(full.target), c.topRow);
  if (g.title !== c.title || g.spacerRow !== c.spacerRow || g.mtdRow !== c.mtdRow || g.daysInMonth !== c.days || c.rowCountAfter !== c.spacerRow
    || c.rowCountAfter !== b.rowCount + c.appendRows || c.columnCountAfter !== b.columnCount + (c.insertedColumns?.count ?? 0)
    || c.anchorColAfter !== b.anchorCol + (c.insertedColumns?.count ?? 0)) return { error: 'манифест внутренне несогласован (геометрия месяца, сетка или якорь)' };
  return full;
}

/* ───────────────────────── план отката по манифесту ───────────────────────── */

export type RollbackRefusal =
  | 'ROLLBACK_MANIFEST_FOREIGN' | 'ROLLBACK_SECTION_MISSING' | 'ROLLBACK_SECTION_AMBIGUOUS' | 'ROLLBACK_SECTION_MISMATCH'
  | 'ROLLBACK_LATER_MONTH_EXISTS' | 'ROLLBACK_APPEND_DETECTED' | 'ROLLBACK_GEOMETRY_CHANGED' | 'ROLLBACK_ANCHOR_MOVED'
  | 'ROLLBACK_CF_AMBIGUOUS' | 'ROLLBACK_EVIDENCE_UNAVAILABLE' | 'ROLLBACK_INSERTED_COLUMNS_NOT_EMPTY' | 'ROLLBACK_MANUAL_DATA_PRESENT';

/** Живое состояние книги, прочитанное перед откатом (только чтение). */
export interface RollbackLive {
  spreadsheetId: string;
  sheetName: string;
  meta: SheetMeta;
  columnA: readonly CellValue[];
  structure: SheetStructure;
  /** Значения секции: строки topRow..mtdRow × 1..columnCount; null — секция не прочитана (не найдена / не помещается). */
  sectionGrid: readonly (readonly CellValue[])[] | null;
  /** Вставленные колонки выше секции пусты (значения И формулы); null — не прочитано; вставки не было — true. */
  insertedEmptyAbove: boolean | null;
}

export interface ManifestRollbackPlan {
  status: 'PLAN_ROLLBACK' | 'REFUSED';
  code: RollbackRefusal | null;
  reasons: string[];
  requests: StructureRequest[];
  deletedCfRules: number;
  deletedRows: [number, number] | null;
  deletedColumns: [number, number] | null;
  restoredWidths: WidthUpgrade[];
  /** Колонки, чью ширину после создания месяца меняли вручную: откат их не трогает (только отчёт). */
  skippedWidths: Array<WidthUpgrade & { live: number | null }>;
  regroupTailColumn: number | null;
}

const refuse = (code: RollbackRefusal, ...reasons: string[]): ManifestRollbackPlan => ({
  status: 'REFUSED', code, reasons, requests: [], deletedCfRules: 0, deletedRows: null, deletedColumns: null, restoredWidths: [], skippedWidths: [], regroupTailColumn: null,
});

/** Непустые ручные ячейки (СПП, блогеры) в днях созданной секции — откат их стёр бы безвозвратно. */
export function manualCellsIn(grid: readonly (readonly CellValue[])[], m: RollbackManifest): string[] {
  const out: string[] = [];
  const g = geometryAt(parseMonthKey(m.target), m.created.topRow);
  for (let i = 0; i < g.daysInMonth; i++) {
    const row = dayRowOf(g, i);
    for (const b of m.created.blocks) for (const off of [OFFSET.spp, OFFSET.bloggers]) {
      const v = grid[row - g.topRow]?.[b.start + off - 1];
      if (!isEmpty(v ?? null)) out.push(`${colA1(b.start + off)}${row}`);
    }
  }
  return out;
}

/**
 * План отката. Мутации строятся ТОЛЬКО если живая книга — ровно та, что описана в манифесте как «после создания»:
 * та же книга и лист; секция месяца на своём месте и последняя в листе; следующего месяца нет; сетка и колонка якорей
 * как после создания; блоки заголовка — ровно блоки манифеста (лишний блок = дописывание SKU после создания);
 * правил УФ секции ровно столько, сколько создано; вставленные колонки пусты выше секции; ручного ввода в секции нет.
 */
export function planManifestRollback(m: RollbackManifest, live: RollbackLive): ManifestRollbackPlan {
  const c = m.created;
  if (m.spreadsheetId !== live.spreadsheetId || m.sheetId !== live.meta.sheetId || m.sheetName !== live.sheetName) {
    return refuse('ROLLBACK_MANIFEST_FOREIGN', `манифест выписан для книги ${m.spreadsheetId} / листа ${m.sheetId} «${m.sheetName}», а откат запущен на ${live.spreadsheetId} / ${live.meta.sheetId} «${live.sheetName}»`);
  }
  const key = parseMonthKey(m.target);
  const loc = locateSection(live.columnA, key);
  if (loc.status === 'MISSING') return refuse('ROLLBACK_SECTION_MISSING', `заголовок «${c.title}» в колонке A не найден — откатывать нечего (или откат уже выполнен)`);
  if (loc.status === 'AMBIGUOUS') return refuse('ROLLBACK_SECTION_AMBIGUOUS', `заголовок «${c.title}» в строках ${loc.rows.join(', ')}`);
  if (loc.topRow !== c.topRow) return refuse('ROLLBACK_SECTION_MISMATCH', `секция ${m.target} в строке ${loc.topRow}, манифест — ${c.topRow}`);
  const later = locateSection(live.columnA, nextMonth(key));
  if (later.status !== 'MISSING') return refuse('ROLLBACK_LATER_MONTH_EXISTS', `в книге уже есть секция ${formatMonthKey(nextMonth(key))} — откат ${m.target} сдвинул бы её`);
  if (live.meta.rowCount > c.rowCountAfter) return refuse('ROLLBACK_LATER_MONTH_EXISTS', `после секции ${m.target} есть строки ${c.rowCountAfter + 1}..${live.meta.rowCount}`);
  if (live.meta.rowCount < c.rowCountAfter) return refuse('ROLLBACK_GEOMETRY_CHANGED', `в листе ${live.meta.rowCount} строк, после создания было ${c.rowCountAfter}`);
  if (!live.sectionGrid) return refuse('ROLLBACK_EVIDENCE_UNAVAILABLE', 'значения секции не прочитаны');
  const blocks = findBlocks(live.sectionGrid[0] ?? [], live.meta.columnCount);
  const same = (n: number): boolean => c.blocks.slice(0, n).every((b, i) => blocks[i]?.nmId === b.nmId && blocks[i]?.start === b.start && blocks[i]?.slot === b.slot);
  if (blocks.length > c.blocks.length && same(c.blocks.length)) {
    return refuse('ROLLBACK_APPEND_DETECTED', `в секции ${blocks.length} блоков, создано было ${c.blocks.length}: после создания дописан SKU ${blocks.slice(c.blocks.length).map((b) => b.nmId).join(', ')} — сначала откат дописывания`);
  }
  if (blocks.length !== c.blocks.length || !same(c.blocks.length)) {
    return refuse('ROLLBACK_SECTION_MISMATCH', `блоки заголовка секции (${blocks.map((b) => `${b.nmId}@${colA1(b.start)}`).slice(0, 30).join(' ')}) не совпадают с манифестом`);
  }
  if (live.meta.columnCount !== c.columnCountAfter) return refuse('ROLLBACK_GEOMETRY_CHANGED', `в листе ${live.meta.columnCount} колонок, после создания было ${c.columnCountAfter}`);
  if (live.meta.anchorCol !== c.anchorColAfter) return refuse('ROLLBACK_ANCHOR_MOVED', `колонка якорей ${colA1(live.meta.anchorCol)}, после создания была ${colA1(c.anchorColAfter)}`);
  const rules = live.structure.conditionalFormats;
  const inSection = rules.filter((r) => r.ranges.length > 0 && r.ranges.every((x) => (x.startRowIndex ?? 0) >= c.topRow - 1)).length;
  if (inSection !== c.cfRulesAdded || rules.length !== m.before.cfRules + c.cfRulesAdded) {
    return refuse('ROLLBACK_CF_AMBIGUOUS', `правил УФ в секции ${inSection} (создано ${c.cfRulesAdded}), всего ${rules.length} (до создания ${m.before.cfRules}) — правила меняли после создания месяца`);
  }
  if (c.insertedColumns && live.insertedEmptyAbove === null) return refuse('ROLLBACK_EVIDENCE_UNAVAILABLE', 'вставленные колонки выше секции не прочитаны');
  if (c.insertedColumns && live.insertedEmptyAbove !== true) {
    return refuse('ROLLBACK_INSERTED_COLUMNS_NOT_EMPTY', `колонки ${colA1(c.insertedColumns.at + 1)}..${colA1(c.insertedColumns.at + c.insertedColumns.count)} непусты выше строки ${c.topRow} — их удаление стёрло бы чужие данные`);
  }
  const manual = manualCellsIn(live.sectionGrid, m);
  if (manual.length) return refuse('ROLLBACK_MANUAL_DATA_PRESENT', `в секции есть ручной ввод (СПП / блогеры): ${manual.slice(0, 12).join(', ')}${manual.length > 12 ? ` … всего ${manual.length}` : ''} — откат стёр бы его; очистите ячейки осознанно`);

  // Ширины: возвращаем только те, что с момента создания не меняли (живая ширина = записанной при создании).
  const width = (col: number): number | null => live.structure.columnMetadata[col - 1]?.pixelSize ?? null;
  const restoredWidths = c.widthUpgrades.filter((u) => width(u.col) === u.to);
  const skippedWidths = c.widthUpgrades.filter((u) => width(u.col) !== u.to).map((u) => ({ ...u, live: width(u.col) }));
  // Первая колонка хвоста выведена из группы хвоста при создании — вернуть, если она по-прежнему вне групп.
  const detached = c.tailGroupDetachedColumn;
  const stillDetached = detached !== null && !live.structure.columnGroups.some((gr) => gr.startIndex <= detached - 1 && gr.endIndex >= detached);
  const regroupTailColumn = detached !== null && stillDetached && c.insertedColumns ? detached - c.insertedColumns.count : null;
  if (regroupTailColumn !== null && regroupTailColumn !== tailStartOf(m.before.anchorCol)) {
    return refuse('ROLLBACK_GEOMETRY_CHANGED', `выведенная из группы колонка ${colA1(detached!)} не соответствует первой колонке хвоста ${colA1(tailStartOf(m.before.anchorCol))} до создания`);
  }
  const base = planMonthRollback({
    geometry: geometryAt(key, c.topRow), rowCount: live.meta.rowCount, sheetId: live.meta.sheetId, rules,
    insertedColumns: c.insertedColumns ? [c.insertedColumns.at + 1, c.insertedColumns.at + c.insertedColumns.count] : null,
    newColumnsEmptyAbove: true, restoreWidths: restoredWidths, regroupTailColumn,
  });
  if (base.refused || base.deletedCfRules !== c.cfRulesAdded) return refuse('ROLLBACK_SECTION_MISMATCH', base.refused ?? 'число удаляемых правил УФ не равно созданному');
  return {
    status: 'PLAN_ROLLBACK', code: null, reasons: [], requests: base.requests, deletedCfRules: base.deletedCfRules,
    deletedRows: base.deletedRows, deletedColumns: base.deletedColumns, restoredWidths, skippedWidths, regroupTailColumn,
  };
}

/* ───────────────────────── сверка после отката ───────────────────────── */

export interface RollbackVerification {
  /** Структура листа совпала с состоянием «до создания» из манифеста. */
  exact: boolean;
  /** Расхождения структуры: сетка, якорь, УФ, объединения, размеры, диапазоны групп. */
  structural: string[];
  /** Расхождения только состояния вида: скрытые колонки/строки, флаг «свёрнуто» у групп. */
  viewState: string[];
}

export function verifyRollback(m: RollbackManifest, after: { meta: SheetMeta; structure: SheetStructure }): RollbackVerification {
  const b = m.before;
  const structural: string[] = [];
  const viewState: string[] = [];
  if (after.meta.rowCount !== b.rowCount) structural.push(`строк ${after.meta.rowCount}, было ${b.rowCount}`);
  if (after.meta.columnCount !== b.columnCount) structural.push(`колонок ${after.meta.columnCount}, было ${b.columnCount}`);
  if (after.meta.anchorCol !== b.anchorCol) structural.push(`якорь ${colA1(after.meta.anchorCol)}, был ${colA1(b.anchorCol)}`);
  const fp = fingerprintOf(after.structure);
  const names: Array<[keyof StructureFingerprint, string, string[]]> = [
    ['conditionalFormats', 'правила УФ', structural], ['merges', 'объединения', structural], ['columnWidths', 'ширины колонок', structural],
    ['rowHeights', 'высоты строк', structural], ['columnHidden', 'скрытые колонки', viewState], ['rowHidden', 'скрытые строки', viewState],
  ];
  for (const [k, label, bucket] of names) if (fp[k] !== b.fingerprint[k]) bucket.push(`${label}: отпечаток отличается от состояния до создания`);
  const cmp = (label: string, was: readonly GroupState[], now: readonly GroupState[]): void => {
    const ranges = (gs: readonly GroupState[]): string => canonicalJson(gs.map((g) => [g.startIndex, g.endIndex, g.depth]));
    if (ranges(was) !== ranges(now)) { structural.push(`${label}: диапазоны отличаются (${now.length} сейчас, ${was.length} было)`); return; }
    const flips = now.filter((g, i) => g.collapsed !== was[i]!.collapsed).map((g) => `${g.startIndex + 1}..${g.endIndex}`);
    if (flips.length) viewState.push(`${label}: флаг «свёрнуто» отличается у ${flips.join(', ')}`);
  };
  cmp('группы колонок', b.columnGroups, groupsOf(after.structure.columnGroups));
  cmp('группы строк', b.rowGroups, groupsOf(after.structure.rowGroups));
  return { exact: structural.length === 0 && viewState.length === 0, structural, viewState };
}
