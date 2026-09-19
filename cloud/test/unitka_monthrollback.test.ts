/**
 * UNITKA CALENDAR V2 — исполняемый откат создания месяца: манифест (до записи), отказы fail-closed, запросы, сверка.
 * Книга — синтетическая (unitka_calendar_fixture.ts): сентябрь 24 блока → октябрь 25 блоков (вставка 23 колонок).
 * Точность восстановления на живой книге доказывается репетицией на копии; здесь — контракт планировщика и загрузчика.
 */
import { describe, it, expect } from 'vitest';
import { planMonthPrep, toStructureRequests, type PrepInputs, type MonthPrepPlan } from '../src/loaders/unitka/monthprep.js';
import {
  buildRollbackManifest, encodeManifest, parseManifest, planManifestRollback, verifyRollback, manualCellsIn, canonicalJson,
  ROLLBACK_MANIFEST_KIND, type RollbackManifest, type RollbackLive,
} from '../src/loaders/unitka/monthrollback.js';
import { unitkaMonthRollbackLoader, monthRollbackWriteAllowed } from '../src/loaders/unitka/rollback.js';
import { OFFSET, type CellValue } from '../src/loaders/unitka/model.js';
import { slotStart, type MonthKey } from '../src/loaders/unitka/calendar.js';
import type { SheetsGateway, SheetMeta, SheetStructure, StructureRequest, FormatGrid } from '../src/loaders/unitka/sheets.js';
import type { UnitkaDeps } from '../src/loaders/unitka/index.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';
import { loadConfig, type Config } from '../src/config.js';
import { LOADERS } from '../src/loaders/registry.js';
import { sectionFromSpec, septemberSpec, applyPlan, cogsSnapshot, septemberStructure, septemberRowFormats, SEPT_NMS, NEW_NM, WIDTH_SEPT, SHEET_ID } from './unitka_calendar_fixture.js';

const OCT: MonthKey = { year: 2026, month: 10 };
const BOOK = 'BOOK-ID', SHEET = 'WB_Юнит_2025';
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

interface Created { plan: MonthPrepPlan; manifest: RollbackManifest; before: SheetStructure; live: RollbackLive }

/** Книга до создания → план → манифест → живая книга «после создания» (как её оставил бы batchUpdate). */
function created(extra: number[] = [NEW_NM]): Created {
  const sept = sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT);
  const before = septemberStructure(768, WIDTH_SEPT);
  const colA: CellValue[] = Array(768).fill(null); colA[734] = 'Сентябрь 2026';
  const meta: SheetMeta = { sheetId: SHEET_ID, rowCount: 768, columnCount: WIDTH_SEPT, anchorCol: WIDTH_SEPT };
  const inp: PrepInputs = {
    target: OCT, meta, columnA: colA, predecessor: sept, existing: null,
    population: [...SEPT_NMS, ...extra].map((nmId) => ({ nmId, name: `Товар ${nmId}` })), cogs: cogsSnapshot({ [NEW_NM]: 426.735 }),
    structure: before, rowFormats: septemberRowFormats(),
  };
  const plan = planMonthPrep(inp);
  if (plan.status !== 'PLAN_CREATE') throw new Error(`фикстура: ${plan.status} ${plan.code}`);
  const manifest = buildRollbackManifest({ plan, meta, structure: before, spreadsheetId: BOOK, sheetName: SHEET, requests: toStructureRequests(plan, SHEET_ID).length, engine: 'test-engine', gitSha: 'sha-1', now: new Date('2026-09-28T07:00:00Z') });
  const ins = plan.insertColumns?.count ?? 0;
  const width = WIDTH_SEPT + ins;
  const after = septemberStructure(803, width);
  after.conditionalFormats = [...clone(before.conditionalFormats), ...plan.conditionalFormats!.rules];
  for (const u of plan.widthUpgrades) after.columnMetadata[u.col - 1]!.pixelSize = u.to;
  after.columnGroups = ins
    ? [...before.columnGroups.filter((g) => g.startIndex !== WIDTH_SEPT - 12), { startIndex: slotStart(24) + 15, endIndex: slotStart(24) + 22, depth: 1 }, { startIndex: width - 11, endIndex: width - 2, depth: 1 }]
    : clone(before.columnGroups);
  const colA2: CellValue[] = Array(803).fill(null); colA2[734] = 'Сентябрь 2026'; colA2[768] = 'Октябрь 2026';
  const live: RollbackLive = {
    spreadsheetId: BOOK, sheetName: SHEET, meta: { sheetId: SHEET_ID, rowCount: 803, columnCount: width, anchorCol: width },
    columnA: colA2, structure: after, sectionGrid: applyPlan(plan, width).grid, insertedEmptyAbove: true,
  };
  return { plan, manifest, before, live };
}
const mutLive = (c: Created, f: (l: RollbackLive) => void): RollbackLive => {
  const l: RollbackLive = { ...c.live, meta: { ...c.live.meta }, columnA: [...c.live.columnA], structure: clone(c.live.structure), sectionGrid: c.live.sectionGrid!.map((r) => [...r]) };
  f(l); return l;
};

describe('манифест отката — строится ДО записи из плана и состояния листа', () => {
  const c = created();
  it('описывает ровно мутации подготовки месяца: строки, вставленные колонки, УФ, ширины, группы, якорь', () => {
    const m = c.manifest;
    expect([m.kind, m.version, m.spreadsheetId, m.sheetId, m.sheetName, m.target]).toEqual([ROLLBACK_MANIFEST_KIND, 1, BOOK, SHEET_ID, SHEET, '2026-10']);
    expect(m.before).toMatchObject({ rowCount: 768, columnCount: 600, anchorCol: 600, cfRules: c.before.conditionalFormats.length, merges: 0 });
    expect(m.before.columnGroups).toHaveLength(c.before.columnGroups.length);
    expect(m.created).toMatchObject({
      title: 'Октябрь 2026', topRow: 769, headerRow: 770, firstDailyRow: 771, lastDailyRow: 801, mtdRow: 802, spacerRow: 803, days: 31,
      appendRows: 35, insertedColumns: { at: 588, count: 23 }, rowCountAfter: 803, columnCountAfter: 623, anchorColAfter: 623,
      cfRulesAdded: c.plan.conditionalFormats!.rules.length, mergesCreated: 26, notesCreated: 1, validationsCreated: 0,
      tailGroupDetachedColumn: 612, requests: toStructureRequests(c.plan, SHEET_ID).length,
    });
    expect([m.created.cells, m.created.mtdStockFamily]).toEqual([c.plan.cells.length, { native: 25 }]);
    expect(m.created.cellsDigest).toMatch(/^[0-9a-f]{64}$/);
    expect(m.created.blocks).toHaveLength(25);
    expect(m.created.blocks[24]).toEqual({ slot: 24, nmId: NEW_NM, start: 589, origin: 'NEW' });
    expect(m.created.widthUpgrades).toEqual(c.plan.widthUpgrades);
    expect(m.created.widthUpgrades.length).toBeGreaterThan(0);
    expect(m.created.cfRulesReissued).toEqual(c.plan.cfTrims.map((t) => t.index));
    expect(m.created.groupsAdded).toEqual([{ startIndex: 604, endIndex: 611 }]);
    expect(m.created.groupsTrimmed).toEqual([{ startIndex: 611, endIndex: 612 }]);
    expect(m.created.rowHeights).toEqual(c.plan.rowHeights);
  });
  it('отпечаток: не зависит от времени и сборки, зависит от плана и состояния листа', () => {
    const again = created().manifest;
    expect(again.digest).toBe(c.manifest.digest);
    expect(c.manifest.digest).toMatch(/^[0-9a-f]{64}$/);
    const other = { ...c.manifest, engine: 'x', gitSha: 'y', generatedAt: '2030-01-01T00:00:00.000Z' };
    expect(parseManifest(canonicalJson(other))).toMatchObject({ digest: c.manifest.digest });
    const st = clone(c.before); st.columnMetadata[5]!.pixelSize = 333;
    const meta: SheetMeta = { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, anchorCol: 600 };
    const m2 = buildRollbackManifest({ plan: c.plan, meta, structure: st, spreadsheetId: BOOK, sheetName: SHEET, requests: 1, engine: 'e', gitSha: 'g', now: new Date() });
    expect(m2.digest).not.toBe(c.manifest.digest);
    // содержимое плана входит в отпечаток: другая формула — другой отпечаток; заметка о происхождении COGS — нет
    const build = (plan: MonthPrepPlan): string => buildRollbackManifest({ plan, meta, structure: c.before, spreadsheetId: BOOK, sheetName: SHEET, requests: c.manifest.created.requests, engine: 'e', gitSha: 'g', now: new Date() }).digest;
    expect(build(c.plan)).toBe(c.manifest.digest);
    const cells = c.plan.cells.map((x, i) => (i === 500 ? { ...x, value: { kind: 'number' as const, value: 1 } } : x));
    expect(build({ ...c.plan, cells })).not.toBe(c.manifest.digest);
    expect(build({ ...c.plan, notes: c.plan.notes.map((n) => ({ ...n, note: `${n.note} (другая публикация канона)` })) })).toBe(c.manifest.digest);
  });
  it('только для PLAN_CREATE', () => {
    expect(() => buildRollbackManifest({ plan: { ...c.plan, status: 'NO_CHANGE' }, meta: c.live.meta, structure: c.before, spreadsheetId: BOOK, sheetName: SHEET, requests: 0, engine: 'e', gitSha: 'g', now: new Date() })).toThrow(/PLAN_CREATE/);
  });
  it('передача: base64url без запятых и «=»; разбор восстанавливает манифест; подмена, обрезка, чужой вид — отказ', () => {
    const b64 = encodeManifest(c.manifest);
    expect(b64).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(b64.length).toBeLessThan(24_000);
    expect(parseManifest(b64)).toEqual(c.manifest);
    expect(parseManifest(JSON.stringify(c.manifest))).toEqual(c.manifest);
    expect(parseManifest('')).toMatchObject({ error: expect.stringContaining('не передан') });
    expect(parseManifest(b64.slice(0, b64.length - 40))).toHaveProperty('error');
    expect(parseManifest('{"kind":"other","version":1}')).toHaveProperty('error');
    const tampered = clone(c.manifest); tampered.created.insertedColumns = { at: 500, count: 23 };
    expect(parseManifest(JSON.stringify(tampered))).toMatchObject({ error: expect.stringContaining('отпечаток') });
    const widths = clone(c.manifest); widths.created.widthUpgrades[0]!.from = 1;
    expect(parseManifest(JSON.stringify(widths))).toMatchObject({ error: expect.stringContaining('отпечаток') });
  });
});

describe('план отката — ровно обратные действия, только в созданной секции', () => {
  const c = created();
  const plan = planManifestRollback(c.manifest, c.live);
  it('PLAN_ROLLBACK: правила УФ секции (по убыванию индекса) → вставленные колонки → строки секции → ширины → группа хвоста', () => {
    expect([plan.status, plan.code]).toEqual(['PLAN_ROLLBACK', null]);
    const kinds = plan.requests.map((r) => Object.keys(r)[0]);
    const nCf = c.manifest.created.cfRulesAdded, nW = c.manifest.created.widthUpgrades.length;
    expect(kinds).toEqual([...Array(nCf).fill('deleteConditionalFormatRule'), 'deleteDimension', 'deleteDimension', ...Array(nW).fill('updateDimensionProperties'), 'addDimensionGroup']);
    const cfIdx = plan.requests.slice(0, nCf).map((r) => (r.deleteConditionalFormatRule as { index: number }).index);
    expect(cfIdx).toEqual([...cfIdx].sort((a, b) => b - a));
    expect(Math.min(...cfIdx)).toBe(c.manifest.before.cfRules);                    // ни одного правила прошлых месяцев
    expect(plan.requests[nCf]).toEqual({ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 588, endIndex: 611 } } });
    expect(plan.requests[nCf + 1]).toEqual({ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'ROWS', startIndex: 768, endIndex: 803 } } });
    expect(plan.requests[plan.requests.length - 1]).toEqual({ addDimensionGroup: { range: { sheetId: SHEET_ID, dimension: 'COLUMNS', startIndex: 588, endIndex: 589 } } });
    expect([plan.deletedRows, plan.deletedColumns, plan.regroupTailColumn]).toEqual([[769, 803], [589, 611], 589]);
    expect(plan.restoredWidths).toEqual(c.manifest.created.widthUpgrades);
    expect(plan.skippedWidths).toEqual([]);
  });
  it('ширину, изменённую вручную после создания, откат не трогает (отчёт); уже сгруппированную колонку хвоста не группирует повторно', () => {
    const u = c.manifest.created.widthUpgrades[3]!;
    const p = planManifestRollback(c.manifest, mutLive(c, (l) => { l.structure.columnMetadata[u.col - 1]!.pixelSize = 150; l.structure.columnGroups.push({ startIndex: 611, endIndex: 612, depth: 1 }); }));
    expect(p.status).toBe('PLAN_ROLLBACK');
    expect(p.skippedWidths).toEqual([{ ...u, live: 150 }]);
    expect(p.restoredWidths).toHaveLength(c.manifest.created.widthUpgrades.length - 1);
    expect(p.regroupTailColumn).toBeNull();
    expect(p.requests.some((r) => 'addDimensionGroup' in r)).toBe(false);
  });
  it('месяц без новых SKU: колонок не вставляли — удаляются только правила УФ и строки секции', () => {
    const c0 = created([]);
    expect(c0.manifest.created.insertedColumns).toBeNull();
    const p = planManifestRollback(c0.manifest, c0.live);
    expect(p.status).toBe('PLAN_ROLLBACK');
    expect(p.requests.filter((r) => 'deleteDimension' in r)).toEqual([{ deleteDimension: { range: { sheetId: SHEET_ID, dimension: 'ROWS', startIndex: 768, endIndex: 803 } } }]);
    expect([p.deletedColumns, p.regroupTailColumn]).toEqual([null, null]);
  });
});

describe('откат fail-closed: любое несовпадение книги с манифестом — отказ с кодом и НОЛЬ запросов', () => {
  const c = created();
  const cases: Array<[string, string, (l: RollbackLive) => void]> = [
    ['другая книга', 'ROLLBACK_MANIFEST_FOREIGN', (l) => { l.spreadsheetId = 'OTHER'; }],
    ['другой лист', 'ROLLBACK_MANIFEST_FOREIGN', (l) => { l.meta.sheetId = 1; }],
    ['секции месяца нет (откат уже выполнен)', 'ROLLBACK_SECTION_MISSING', (l) => { (l.columnA as CellValue[])[768] = null; }],
    ['заголовок месяца дважды', 'ROLLBACK_SECTION_AMBIGUOUS', (l) => { (l.columnA as CellValue[])[700] = 'Октябрь 2026'; }],
    ['секция в другой строке', 'ROLLBACK_SECTION_MISMATCH', (l) => { const a = l.columnA as CellValue[]; a[768] = null; a[770] = 'Октябрь 2026'; }],
    ['уже есть следующий месяц', 'ROLLBACK_LATER_MONTH_EXISTS', (l) => { (l.columnA as CellValue[]).push(...Array(33).fill(null)); (l.columnA as CellValue[])[803] = 'Ноябрь 2026'; l.meta.rowCount = 836; }],
    ['после секции есть строки', 'ROLLBACK_LATER_MONTH_EXISTS', (l) => { l.meta.rowCount = 810; }],
    ['строк меньше, чем после создания', 'ROLLBACK_GEOMETRY_CHANGED', (l) => { l.meta.rowCount = 802; }],
    ['после создания дописан SKU', 'ROLLBACK_APPEND_DETECTED', (l) => { (l.sectionGrid as CellValue[][])[0]![slotStart(25) - 1] = '900000011 Фикстура'; l.meta.columnCount = 647; l.meta.anchorCol = 647; }],
    ['блок заголовка подменён', 'ROLLBACK_SECTION_MISMATCH', (l) => { (l.sectionGrid as CellValue[][])[0]![slotStart(24) - 1] = '111111111 Другой'; }],
    ['блок заголовка стёрт', 'ROLLBACK_SECTION_MISMATCH', (l) => { (l.sectionGrid as CellValue[][])[0]![slotStart(24) - 1] = ''; }],
    ['колонок не столько, сколько после создания', 'ROLLBACK_GEOMETRY_CHANGED', (l) => { l.meta.columnCount = 624; }],
    ['якорь сдвинут', 'ROLLBACK_ANCHOR_MOVED', (l) => { l.meta.anchorCol = 622; }],
    ['правило УФ секции удалено', 'ROLLBACK_CF_AMBIGUOUS', (l) => { l.structure.conditionalFormats.pop(); }],
    ['в секцию добавлено правило УФ', 'ROLLBACK_CF_AMBIGUOUS', (l) => { l.structure.conditionalFormats.push(clone(l.structure.conditionalFormats[l.structure.conditionalFormats.length - 1]!)); }],
    ['правило прошлого месяца удалено', 'ROLLBACK_CF_AMBIGUOUS', (l) => { l.structure.conditionalFormats.shift(); }],
    ['значения секции не прочитаны', 'ROLLBACK_EVIDENCE_UNAVAILABLE', (l) => { l.sectionGrid = null; }],
    ['вставленные колонки выше секции не прочитаны', 'ROLLBACK_EVIDENCE_UNAVAILABLE', (l) => { l.insertedEmptyAbove = null; }],
    ['вставленные колонки непусты выше секции', 'ROLLBACK_INSERTED_COLUMNS_NOT_EMPTY', (l) => { l.insertedEmptyAbove = false; }],
    ['владелец ввёл СПП (в т.ч. 0)', 'ROLLBACK_MANUAL_DATA_PRESENT', (l) => { (l.sectionGrid as CellValue[][])[771 - 769]![slotStart(0) + OFFSET.spp - 1] = 0; }],
    ['владелец ввёл блогеров', 'ROLLBACK_MANUAL_DATA_PRESENT', (l) => { (l.sectionGrid as CellValue[][])[780 - 769]![slotStart(24) + OFFSET.bloggers - 1] = 3; }],
  ];
  it.each(cases)('%s → %s', (_n, code, mutate) => {
    const p = planManifestRollback(c.manifest, mutLive(c, mutate));
    expect([p.status, p.code]).toEqual(['REFUSED', code]);
    expect(p.requests).toEqual([]);
    expect(p.reasons.length).toBeGreaterThan(0);
  });
  it('факты Engine в секции откату не мешают (восстановимы из BigQuery); ручные ячейки перечисляются адресами', () => {
    const l = mutLive(c, (x) => { (x.sectionGrid as CellValue[][])[771 - 769]![slotStart(0) + OFFSET.orders - 1] = 12; });
    expect(planManifestRollback(c.manifest, l).status).toBe('PLAN_ROLLBACK');
    const g = c.live.sectionGrid!.map((r) => [...r]); g[772 - 769]![slotStart(1) + OFFSET.spp - 1] = 25;
    expect(manualCellsIn(g, c.manifest)).toEqual(['AZ772']);
  });
});

describe('сверка после отката: структура листа против состояния «до создания» из манифеста', () => {
  const c = created();
  const beforeMeta: SheetMeta = { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, anchorCol: 600 };
  it('точное восстановление → exact', () => {
    expect(verifyRollback(c.manifest, { meta: beforeMeta, structure: clone(c.before) })).toEqual({ exact: true, structural: [], viewState: [] });
  });
  it('структурные расхождения: сетка, якорь, УФ, объединения, ширины, высоты, диапазоны групп', () => {
    const st = clone(c.before);
    st.conditionalFormats.pop(); st.merges.push({ startRowIndex: 1, endRowIndex: 2, startColumnIndex: 1, endColumnIndex: 3 });
    st.columnMetadata[10]!.pixelSize = 1; st.rowMetadata[10]!.pixelSize = 1; st.columnGroups.pop();
    const v = verifyRollback(c.manifest, { meta: { ...beforeMeta, rowCount: 769, columnCount: 601, anchorCol: 601 }, structure: st });
    expect(v.exact).toBe(false);
    expect(v.structural).toHaveLength(8);
    expect(v.viewState).toEqual([]);
  });
  it('только состояние вида (скрытая колонка, флаг «свёрнуто») — отдельно, не структурная ошибка', () => {
    const st = clone(c.before);
    st.columnMetadata[20]!.hiddenByUser = true; st.columnGroups[0] = { ...st.columnGroups[0]!, collapsed: true };
    const v = verifyRollback(c.manifest, { meta: beforeMeta, structure: st });
    expect(v.structural).toEqual([]);
    expect(v.viewState).toHaveLength(2);
    expect(v.exact).toBe(false);
  });
});

/* ───────────────────────── загрузчик unitka-month-rollback ───────────────────────── */

interface LogLine { level: string; event: string; fields: Record<string, unknown> }
function recordingLogger(lines: LogLine[]): Logger {
  const mk = (): Logger => ({
    info: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'info', event, fields }); },
    warn: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'warn', event, fields }); },
    error: (event: string, fields: Record<string, unknown> = {}) => { lines.push({ level: 'error', event, fields }); },
    child: () => mk(),
  } as unknown as Logger);
  return mk();
}
const cfgOf = (environment: 'shadow' | 'prod', env: Record<string, string> = {}): Config =>
  loadConfig({ ENVIRONMENT: environment, GCP_PROJECT_ID: 'proj', BQ_RAW_DATASET: 'wb_raw', UNITKA_SPREADSHEET_ID: BOOK, GIT_SHA: 'test', ...env });
const ctxOf = (config: Config, lines: LogLine[] = []): LoaderContext => ({ config, logger: recordingLogger(lines), logicalPeriod: '2026-10-01T10', targetDate: '2026-10-01T10', runId: 'run-rollback' });

/** Книга «после создания»; исполнение отката переводит её в состояние «до» (или в заданное — для проверки сверки). */
class RollbackSheets implements SheetsGateway {
  writes: StructureRequest[][] = [];
  reads: string[] = [];
  rolledBack = false;
  afterStructure: SheetStructure;
  constructor(private readonly c: Created, private readonly readonlyScope: boolean, private readonly above: CellValue[][] = []) { this.afterStructure = clone(c.before); }
  async readSheetMeta(): Promise<SheetMeta> { return this.rolledBack ? { sheetId: SHEET_ID, rowCount: 768, columnCount: 600, anchorCol: 600 } : { ...this.c.live.meta }; }
  async readSheetStructure(): Promise<SheetStructure> { return this.rolledBack ? this.afterStructure : this.c.live.structure; }
  async readRowFormats(): Promise<Map<number, Array<Record<string, unknown> | null>>> { throw new Error('не ожидалось'); }
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    this.reads.push(...ranges);
    return ranges.map((r) => {
      if (/!A1:A\d+$/.test(r)) return this.c.live.columnA.map((v) => [v]);
      if (/!A769:WY802$/.test(r)) return this.c.live.sectionGrid!.map((row) => [...row]);
      throw new Error(`неожиданный диапазон ${r}`);
    });
  }
  async readFormulas(range: string): Promise<CellValue[][]> { this.reads.push(range); if (!/!VQ1:WM768$/.test(range)) throw new Error(`неожиданный диапазон ${range}`); return this.above; }
  async readFormats(): Promise<FormatGrid> { throw new Error('не ожидалось'); }
  async batchWrite(): Promise<number> { throw new Error('не ожидалось'); }
  async formatWrite(): Promise<number> { throw new Error('не ожидалось'); }
  async structureWrite(requests: StructureRequest[]): Promise<number> {
    if (this.readonlyScope) throw new Error('STRUCTURE_WRITE_FORBIDDEN');
    this.writes.push(requests); this.rolledBack = true; return requests.length;
  }
}
const depsOf = (sheets: RollbackSheets, made: boolean[] = []): UnitkaDeps => ({
  makeRunner: () => { throw new Error('откат не читает BigQuery'); }, makeSheets: (_c, ro) => { made.push(ro); return sheets; }, now: () => new Date('2026-10-01T07:00:00Z'),
});

describe('загрузчик unitka-month-rollback: план по умолчанию, исполнение — только явно', () => {
  const c = created();
  const env = (extra: Record<string, string> = {}): Record<string, string> => ({ UNITKA_MONTH_ROLLBACK_MANIFEST: encodeManifest(c.manifest), UNITKA_MONTH_PREP_TARGET: '2026-10', ...extra });

  it('зарегистрирован, не prodOnly (план доступен), гейт записи: prod + флаг + без DRY_RUN', () => {
    expect(LOADERS['unitka-month-rollback']).toBeDefined();
    expect(LOADERS['unitka-month-rollback']!.prodOnly).toBeUndefined();
    expect(monthRollbackWriteAllowed({ environment: 'prod', unitkaMonthRollbackWrite: true })).toBe(true);
    expect(monthRollbackWriteAllowed({ environment: 'prod', unitkaMonthRollbackWrite: true, unitkaDryRun: true })).toBe(false);
    expect(monthRollbackWriteAllowed({ environment: 'shadow', unitkaMonthRollbackWrite: true })).toBe(false);
    expect(monthRollbackWriteAllowed({ environment: 'prod' })).toBe(false);
    expect(cfgOf('prod', { UNITKA_MONTH_PREP_WRITE: '1' }).unitkaMonthRollbackWrite).toBe(false);   // флаг создания откат не разрешает
  });
  it('PLAN (prod без флага; shadow с флагом; prod с флагом и DRY_RUN=1): шлюз readonly, 0 записей, план в журнале', async () => {
    for (const config of [cfgOf('prod', env()), cfgOf('shadow', env({ UNITKA_MONTH_ROLLBACK_WRITE: '1' })), cfgOf('prod', env({ UNITKA_MONTH_ROLLBACK_WRITE: '1', DRY_RUN: '1' }))]) {
      const sheets = new RollbackSheets(c, true); const made: boolean[] = []; const lines: LogLine[] = [];
      const res = await unitkaMonthRollbackLoader(ctxOf(config, lines), depsOf(sheets, made));
      expect([made, sheets.writes.length, res.rowsLoaded]).toEqual([[true], 0, 0]);
      expect(lines.find((l) => l.event === 'unitka_month_rollback_plan')?.fields).toMatchObject({ status: 'PLAN_ROLLBACK', manifest_digest: c.manifest.digest, delete_rows: [769, 803], delete_columns: [589, 611] });
      expect(lines.some((l) => l.event === 'unitka_month_rollback_dry')).toBe(true);
    }
  });
  it('WRITE: ровно один batchUpdate, затем сверка структуры с состоянием «до создания»', async () => {
    const sheets = new RollbackSheets(c, false); const made: boolean[] = []; const lines: LogLine[] = [];
    const res = await unitkaMonthRollbackLoader(ctxOf(cfgOf('prod', env({ UNITKA_MONTH_ROLLBACK_WRITE: '1' })), lines), depsOf(sheets, made));
    expect(made).toEqual([false]);
    expect(sheets.writes).toHaveLength(1);
    expect(sheets.writes[0]).toEqual(planManifestRollback(c.manifest, c.live).requests);
    expect(res.rowsLoaded).toBe(sheets.writes[0]!.length);
    expect(lines.find((l) => l.event === 'unitka_month_rollback_verified')?.fields).toMatchObject({ exact: true, structural: [], view_state: [] });
    expect(sheets.reads.some((r) => r.endsWith('!VQ1:WM768'))).toBe(true);           // вставленные колонки выше секции проверены
  });
  it('после отката структура не та → ROLLBACK_VERIFY_FAILED; расхождение только вида — не ошибка', async () => {
    const bad = new RollbackSheets(c, false); bad.afterStructure.conditionalFormats.pop();
    await expect(unitkaMonthRollbackLoader(ctxOf(cfgOf('prod', env({ UNITKA_MONTH_ROLLBACK_WRITE: '1' }))), depsOf(bad))).rejects.toMatchObject({ code: 'ROLLBACK_VERIFY_FAILED' });
    const view = new RollbackSheets(c, false); view.afterStructure.columnGroups[0] = { ...view.afterStructure.columnGroups[0]!, collapsed: true };
    const lines: LogLine[] = [];
    await unitkaMonthRollbackLoader(ctxOf(cfgOf('prod', env({ UNITKA_MONTH_ROLLBACK_WRITE: '1' })), lines), depsOf(view));
    expect(lines.find((l) => l.event === 'unitka_month_rollback_verified')?.fields).toMatchObject({ exact: false, structural: [] });
  });
  it('отказы до любой записи: нет манифеста, битый манифест, месяц не предъявлен или не тот, книга не совпала', async () => {
    const run = (e: Record<string, string>, sheets = new RollbackSheets(c, false)) => unitkaMonthRollbackLoader(ctxOf(cfgOf('prod', { UNITKA_MONTH_ROLLBACK_WRITE: '1', ...e })), depsOf(sheets)).then(() => sheets, (err: unknown) => ({ err, sheets }));
    for (const [e, code] of [
      [{ UNITKA_MONTH_PREP_TARGET: '2026-10' }, 'ROLLBACK_MANIFEST_INVALID'],
      [{ UNITKA_MONTH_ROLLBACK_MANIFEST: 'AAAA', UNITKA_MONTH_PREP_TARGET: '2026-10' }, 'ROLLBACK_MANIFEST_INVALID'],
      [{ UNITKA_MONTH_ROLLBACK_MANIFEST: encodeManifest(c.manifest) }, 'ROLLBACK_TARGET_REQUIRED'],
      [{ UNITKA_MONTH_ROLLBACK_MANIFEST: encodeManifest(c.manifest), UNITKA_MONTH_PREP_TARGET: '2026-11' }, 'ROLLBACK_TARGET_REQUIRED'],
    ] as Array<[Record<string, string>, string]>) {
      const r = await run(e) as { err: { code: string }; sheets: RollbackSheets };
      expect(r.err.code).toBe(code);
      expect(r.sheets.writes).toHaveLength(0);
    }
    const dirty = new RollbackSheets(c, false, [['', ''], ['', 'чужое значение']]);
    const r = await run(env(), dirty) as { err: { code: string }; sheets: RollbackSheets };
    expect(r.err.code).toBe('ROLLBACK_INSERTED_COLUMNS_NOT_EMPTY');
    expect(dirty.writes).toHaveLength(0);
    // книга другая, чем в манифесте
    const foreign = await unitkaMonthRollbackLoader(ctxOf(loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', UNITKA_SPREADSHEET_ID: 'OTHER', UNITKA_MONTH_ROLLBACK_WRITE: '1', ...env() })), depsOf(new RollbackSheets(c, false))).catch((e: unknown) => e);
    expect(foreign).toMatchObject({ code: 'ROLLBACK_MANIFEST_FOREIGN' });
  });
  it('статически: откат не читает BigQuery, суточный Engine отката не импортирует', async () => {
    const { readFileSync } = await import('node:fs');
    const src = (f: string): string => readFileSync(new URL(`../src/loaders/unitka/${f}`, import.meta.url), 'utf8');
    expect(src('rollback.ts')).not.toMatch(/UnitkaBq|makeRunner\(|from '\.\/bq\.js'/);
    expect(src('monthrollback.ts')).not.toMatch(/from '\.\/bq\.js'|SheetsRest|structureWrite\(/);   // чистый модуль: ни BigQuery, ни HTTP
    expect(src('index.ts')).not.toMatch(/rollback/i);
  });
});
