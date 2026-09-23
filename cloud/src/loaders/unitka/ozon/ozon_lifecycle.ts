/**
 * OZON — AUTO-LCD, ГЕОМЕТРИЯ ИЗ ЛИСТА И ПРОВЕРКА ПУБЛИКАЦИИ (Gate 10).
 *
 * ТРИ ВЕЩИ, КОТОРЫХ У OZON-ПИСАТЕЛЯ ДО GATE 10 НЕ БЫЛО:
 *   1. собственного LCD — лист Ozon жил на LAST_CLOSED_DATE платформы WB, поэтому успех WB
 *      23.09 закрыл 22.09 и у Ozon, хотя писатель Ozon в тот день не запускался;
 *   2. проверки после записи — писатель писал структуру, значения и оформление и сразу
 *      возвращал результат; перечитывания не было вовсе, а значит, не на что было опереть
 *      атомарный коммит;
 *   3. ёмкости из листа — число слотов, граница хвоста и число правил УФ были КОНСТАНТАМИ
 *      окружения (22, 562, 434). Любая вставка колонок делала их ложью.
 *
 * ПЕРЕКЛЮЧАТЕЛЬ ДО МИГРАЦИИ. Пока в production нет OZON_LAST_CLOSED_DATE, писатель обязан вести
 * себя ПО-ПРЕЖНЕМУ: читать LCD из B2 и никогда его не писать (B2 принадлежит WB). Режим задаётся
 * адресом авторитета (OZON_UNITKA_LCD_CELL): «ZZ_CONFIG!B2» / «LAST_CLOSED_DATE» — LEGACY,
 * «OZON_LAST_CLOSED_DATE» — собственный цикл. Код можно выкатить до миграции: LEGACY не коммитит.
 */
import { LoaderError } from '../../../errors.js';
import type { SheetsGateway } from '../sheets.js';
import type { CellValue as SheetCell } from '../model.js';
import { OZON_GEOMETRY, ozonSlotStart, OZON_SUMMARY_TO_OFFSET } from './contract.js';
import { OZON_LEGACY_LCD_NAME, OZON_OWN_LCD_NAME } from './formulas.js';
import { cfAllRequests, futureDayRequests, columnName, type SectionLayout } from './requests.js';
import { markedSlots } from './capacity.js';
import { dateCellToIso } from '../config_sheet.js';
import { addDaysIso, type LcdCell } from '../lcd.js';

/* ─────────────────────────── авторитет LCD ─────────────────────────── */

export type OzonAuthority =
  | { readonly kind: 'LEGACY'; readonly read: string; readonly lcdName: string }
  | { readonly kind: 'OWN'; readonly read: string; readonly lcdName: string };

export function resolveOzonAuthority(cell: string): OzonAuthority {
  const c = cell.trim();
  if (c === 'ZZ_CONFIG!B2' || c === OZON_LEGACY_LCD_NAME) return { kind: 'LEGACY', read: c, lcdName: OZON_LEGACY_LCD_NAME };
  if (c === OZON_OWN_LCD_NAME) return { kind: 'OWN', read: c, lcdName: OZON_OWN_LCD_NAME };
  throw new LoaderError(`OZON_UNITKA_LCD_CELL = «${cell}»: ожидается ${OZON_OWN_LCD_NAME} (собственный цикл) или ZZ_CONFIG!B2 (до миграции)`, 'OZON_LCD_AUTHORITY_INVALID');
}

/** Авторитет LCD Ozon — именованный диапазон. Зеркало VA2 — формула «=OZON_LAST_CLOSED_DATE», его не пишем. */
export class OzonLcdCell implements LcdCell {
  constructor(private readonly sheets: SheetsGateway) {}
  async read(): Promise<string | null> {
    const [grid] = await this.sheets.readValues([OZON_OWN_LCD_NAME]);
    return dateCellToIso(grid?.[0]?.[0]);
  }
  async write(iso: string): Promise<void> {
    const serial = Math.round((Date.parse(`${iso}T00:00:00Z`) - Date.UTC(1899, 11, 30)) / 86_400_000);
    await this.sheets.batchWrite([{ range: OZON_OWN_LCD_NAME, values: [[serial]] }]);
  }
}

/** D-1 по МСК: сегодняшний день закрытым не бывает. */
export function d1MoscowOf(now: Date): string {
  return addDaysIso(new Date(now.getTime() + 3 * 3600_000).toISOString().slice(0, 10), -1);
}

/**
 * Сущности, чьё ОКНО покрывает сутки (source_from..source_to). prices — СНИМОК (все прогоны —
 * точечное окно): снимок за прошлый день не догрузить, и per-day покрытие по нему остановило
 * бы Ozon навсегда после одного пропуска. prices остаётся в прежнем барьере свежести.
 */
export const OZON_COVERAGE_ENTITIES: readonly string[] = ['finance_accrual', 'fbo_postings', 'ads_sku_daily'];

/* ─────────────────────────── геометрия из листа ─────────────────────────── */

export interface OzonGeometry {
  /** Слотов с шапкой «Дата» в самой длинной секции — размеченные. */
  readonly markedSlots: number;
  /** Пустых зарезервированных слотов сразу за размеченными: колонки вставлены, блок ещё не активирован. */
  readonly reservedSlots: number;
  readonly physicalSlots: number;
  /** Первая колонка хвоста владельца — ВЫВЕДЕНА из листа. */
  readonly tailFirst: number;
  /** Абсолютная ссылка на зеркало LCD для УФ ($VA$2) — из положения OZON_LCD_MIRROR. */
  readonly lcdRef: string | null;
  readonly notes: readonly string[];
}

const W = OZON_GEOMETRY.BLOCK_WIDTH;

/** Пуст ли прямоугольник колонок c1..c2 во всех строках сетки (значения, не формат). */
function columnsEmpty(grid: readonly (readonly SheetCell[])[], c1: number, c2: number): boolean {
  for (const row of grid) for (let c = c1; c <= c2; c++) {
    const v = row[c - 1];
    if (v !== undefined && v !== null && String(v) !== '') return false;
  }
  return true;
}
function anyContentFrom(grid: readonly (readonly SheetCell[])[], c1: number): boolean {
  for (const row of grid) for (let c = c1; c <= row.length; c++) {
    const v = row[c - 1];
    if (v !== undefined && v !== null && String(v) !== '') return true;
  }
  return false;
}

/**
 * Геометрия — из самого листа. Размеченные слоты — по шапке «Дата» на старте каждого слота.
 * Затем ЗАРЕЗЕРВИРОВАННЫЕ: пустые на всю высоту листа полосы шириной в блок сразу за размеченными,
 * за которыми дальше есть содержимое (хвост). Так повтор после частичного сбоя (колонки уже
 * вставлены, блок ещё не активирован) узнаёт расширенную геометрию и НЕ вставляет колонки снова.
 * В штатном состоянии первая же полоса за блоками — хвост (таблица владельца и зеркало LCD лежат
 * в первых 25 колонках за блоками), поэтому зарезервированных слотов 0.
 */
export function deriveOzonGeometry(a: {
  grid: readonly (readonly SheetCell[])[];
  columnCount: number;
  mirror?: { row: number; col: number } | null;
  envTailFirst?: number;
}): OzonGeometry {
  const notes: string[] = [];
  let marked = 0;
  for (let r = 1; r <= a.grid.length; r++) {
    const row = a.grid[r - 1] ?? [];
    if (String(row[OZON_GEOMETRY.BLOCK_FIRST_COLUMN - 1] ?? '').trim() !== 'Дата') continue;
    marked = Math.max(marked, markedSlotsOf(row, a.columnCount));
  }
  if (marked < 1) throw new LoaderError('в листе Ozon не найдено ни одной шапки блока «Дата»', 'OZON_UNITKA_LAYOUT');
  let reserved = 0;
  for (;;) {
    const c1 = ozonSlotStart(marked + reserved), c2 = c1 + W - 1;
    if (c2 > a.columnCount) break;                          // лист кончился — не резерв
    if (!columnsEmpty(a.grid, c1, c2)) break;               // здесь содержимое — это хвост
    if (!anyContentFrom(a.grid, c2 + 1)) break;             // пусто и дальше — хвоста нет, резерва нет
    reserved++;
    if (reserved > 16) throw new LoaderError('больше 16 пустых слотов подряд за блоками — раскладка не опознана', 'NO_SAFE_EXPANSION_PATH');
  }
  const physical = marked + reserved;
  const tailFirst = ozonSlotStart(physical);
  if (reserved) notes.push(`зарезервировано ${reserved} пустых слотов: колонки вставлены прошлым прогоном, блок ещё не активирован`);
  if (a.envTailFirst !== undefined && a.envTailFirst !== tailFirst) {
    notes.push(`OZON_UNITKA_TAIL_FIRST_COLUMN=${a.envTailFirst} устарел: лист даёт ${tailFirst} (авторитет — лист)`);
  }
  let lcdRef: string | null = null;
  if (a.mirror) {
    if (a.mirror.col < tailFirst) {
      throw new LoaderError(`зеркало LCD (колонка ${a.mirror.col}) оказалось левее хвоста (${tailFirst}) — раскладка не опознана`, 'NO_SAFE_EXPANSION_PATH');
    }
    lcdRef = `$${columnName(a.mirror.col)}$${a.mirror.row}`;
  }
  return { markedSlots: marked, reservedSlots: reserved, physicalSlots: physical, tailFirst, lcdRef, notes };
}

function markedSlotsOf(headerRow: readonly SheetCell[], columnCount: number): number {
  const n = markedSlots(headerRow);
  return Math.min(n, Math.floor((columnCount - OZON_GEOMETRY.BLOCK_FIRST_COLUMN + 1) / W));
}

/* ─────────────────────────── правила УФ ─────────────────────────── */

/**
 * Сколько правил УФ генерирует движок для раскладки. До Gate 10 это была константа окружения 434:
 * верная, пока число блоков 22. Генератор ставит правило на СЕМЬЮ, а не на ячейку, поэтому новый
 * МЕСЯЦ добавляет диапазоны, а не правила (число стабильно), но 23-й БЛОК добавляет правила. С
 * константой 434 прогон после расширения удалял бы лишь часть своих правил, а остаток копился бы
 * каждый день. Счёт — тем же генератором, что пишет правила: расхождение исключено построением.
 */
export function engineCfRuleCount(sheetId: number, sections: readonly SectionLayout[], lcdRef: string): number {
  return cfAllRequests(sheetId, sections, lcdRef).length + futureDayRequests(sheetId, sections, lcdRef).length;
}

/**
 * Сколько правил УФ движка стоит В ЛИСТЕ СЕЙЧАС. Движок добавляет свои правила в начало списка
 * (index 0), поэтому его правила — ПРЕФИКС, и все они лежат левее хвоста владельца. Счёт по листу,
 * а не по генератору, потому что лист мог остаться в промежуточном состоянии: фаза УФ уходит
 * пачками по 400 запросов, и сбой между пачками оставляет не то число правил, которое вычислил бы
 * генератор. Удаление по фактическому префиксу самовосстанавливается и не копит правила.
 * Правила, задевающие хвост (правила владельца), не считаются никогда.
 */
export function engineCfPrefix(
  rules: ReadonlyArray<{ ranges?: ReadonlyArray<{ endColumnIndex?: number; startColumnIndex?: number }> }>, tailFirst: number,
): number {
  let n = 0;
  for (const r of rules) {
    const ranges = r.ranges ?? [];
    if (!ranges.length || !ranges.every((g) => (g.endColumnIndex ?? Number.MAX_SAFE_INTEGER) <= tailFirst - 1)) break;
    n++;
  }
  return n;
}

/* ─────────────────────────── проверка публикации ─────────────────────────── */

export interface WrittenRange { readonly range: string; readonly values: ReadonlyArray<ReadonlyArray<unknown>> }

const A1 = /^(?:'(?:[^']|'')*'|[^!]+)!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$/;
function colOf(s: string): number { let n = 0; for (const ch of s) n = n * 26 + ch.charCodeAt(0) - 64; return n; }

/** Ячейки, записанные планом: «строка:колонка» → значение (формула — строка с «=»). */
export function writtenCells(ranges: readonly WrittenRange[]): Map<string, unknown> {
  const out = new Map<string, unknown>();
  for (const r of ranges) {
    const m = A1.exec(r.range);
    if (!m) throw new LoaderError(`диапазон записи не распознан: ${r.range}`, 'OZON_UNITKA_READBACK');
    const r1 = Number(m[2]), c1 = colOf(m[1]!);
    r.values.forEach((row, i) => row.forEach((v, j) => out.set(`${r1 + i}:${c1 + j}`, v)));
  }
  return out;
}

const ERR = /^#(REF!|ERROR!|VALUE!|N\/A|NAME\?|DIV\/0!|NUM!|NULL!)/;

export interface PublicationCheck { readonly name: string; readonly pass: boolean; readonly count: number; readonly sample: readonly string[] }

/**
 * Проверка публикации Ozon: перечитанный лист против того, что прогон записал.
 *   READBACK_VALUES    — каждое записанное значение-литерал лежит в листе (числа с допуском 1e-6);
 *   READBACK_FORMULAS  — каждая записанная формула лежит в листе ФОРМУЛОЙ;
 *   FORMULA_ERRORS     — ни одна записанная ячейка не вычислилась в ошибку;
 *   SUMMARY_RECONCILIATION — сводка дня = Σ блоков секции по дням ≤ `summaryUpTo`. До коммита это
 *     прежний LCD книги: формулы сводки более поздних дней честно пусты, пока LCD не закоммичен.
 */
export function verifyPublication(a: {
  written: ReadonlyMap<string, unknown>;
  values: (row: number, col: number) => unknown;
  formulas: (row: number, col: number) => unknown;
  sections: ReadonlyArray<{ readonly firstRow: number; readonly lastRow: number; readonly blockCount: number }>;
  dateAt: (row: number) => string | null;
  summaryUpTo: string;
}): PublicationCheck[] {
  const lit: string[] = [], frm: string[] = [], err: string[] = [], sum: string[] = [];
  for (const [k, want] of a.written) {
    const [r, c] = k.split(':').map(Number) as [number, number];
    const got = a.values(r, c);
    if (typeof got === 'string' && ERR.test(got)) { err.push(`${columnName(c)}${r} ${got}`); continue; }
    if (typeof want === 'string' && want.startsWith('=')) {
      const f = a.formulas(r, c);
      if (!(typeof f === 'string' && f.startsWith('='))) frm.push(`${columnName(c)}${r} формула не легла: ${String(f)}`);
      continue;
    }
    const empty = (v: unknown) => v === undefined || v === null || v === '';
    if (empty(want) && empty(got)) continue;
    if (typeof want === 'number' && typeof got === 'number' && Math.abs(want - got) <= 1e-6) continue;
    if (String(want) === String(got)) continue;
    lit.push(`${columnName(c)}${r} записано[${String(want)}] лист[${String(got)}]`);
  }
  for (const s of a.sections) {
    for (let r = s.firstRow; r <= s.lastRow; r++) {
      const d = a.dateAt(r);
      if (!d || d > a.summaryUpTo) continue;
      for (const [sumCol, off, name] of OZON_SUMMARY_TO_OFFSET) {
        let total = 0, seen = 0;
        for (let b = 0; b < s.blockCount; b++) {
          const v = a.values(r, ozonSlotStart(b) + off);
          if (typeof v === 'number') { total += v; seen++; }
        }
        const sv = a.values(r, sumCol);
        const ok = seen === 0 ? (sv === '' || sv === null || sv === undefined) : (typeof sv === 'number' && Math.abs(sv - total) <= 0.01);
        if (!ok) sum.push(`${name} ${columnName(sumCol)}${r} сводка[${String(sv)}] Σблоков[${seen ? Math.round(total * 100) / 100 : 'пусто'}]`);
      }
    }
  }
  const chk = (name: string, bad: string[]): PublicationCheck => ({ name, pass: bad.length === 0, count: bad.length, sample: bad.slice(0, 10) });
  return [chk('READBACK_VALUES', lit), chk('READBACK_FORMULAS', frm), chk('FORMULA_ERRORS', err), chk('SUMMARY_RECONCILIATION', sum)];
}

/* ═══════════════════════════ ввод-вывод цикла ═══════════════════════════ */

import type { Logger } from '../../../logging.js';
import type { SheetMeta } from '../sheets.js';
import { decideCandidate, coveredByRunWindows, type CandidateDecision, type LcdMode } from '../lcd.js';
import { readLifecycleConfig, type LifecycleConfig } from '../config_sheet.js';
import { logLifecycle } from '../lifecycle_log.js';
import { planExpansion, isRefusal } from './capacity.js';

export interface OzonCandidate {
  readonly mode: LcdMode;
  readonly committed: string;
  readonly candidate: string;
  readonly decision: CandidateDecision;
  readonly d1Msk: string;
  readonly lifecycle: LifecycleConfig['state'];
}

/**
 * Кандидат LCD Ozon: смежность вперёд от ЗАКОММИЧЕННОГО по окнам успешных прогонов
 * OZON_INGESTION_RUNS (source_from..source_to), а не по наличию заказов: сутки без заказов, но
 * с покрытием загрузкой — законно закрытые сутки. Нужна только история позже закоммиченного.
 */
export async function resolveOzonCandidate(a: {
  sheets: SheetsGateway; projectId: string; log: Logger; now: Date;
  query: <T>(sql: string) => Promise<T[]>;
}): Promise<OzonCandidate> {
  const life = await readLifecycleConfig(a.sheets);
  if (life.state === 'DRIFT' || life.state === 'INVALID') {
    throw new LoaderError(`блок жизненного цикла ZZ_CONFIG: ${life.issues.slice(0, 4).join(' | ')}`, 'LIFECYCLE_CONFIG_INVALID');
  }
  const cfg = life.state === 'PRESENT' ? life.ozon : { mode: 'AUTO' as const, manualLcd: undefined, manualRaw: null };
  const committed = await new OzonLcdCell(a.sheets).read();
  if (!committed) throw new LoaderError(`${OZON_OWN_LCD_NAME} в книге пуст или не дата`, 'LCD_BOOK_INVALID');
  if (!/^[A-Za-z0-9-]+$/.test(a.projectId)) throw new LoaderError('идентификатор проекта', 'OZON_UNITKA_CONFIG');
  const runs = await a.query<{ entity: string; source_from: string | null; source_to: string | null; completed_msk: string | null }>(
    `SELECT entity, source_from, source_to, CAST(DATE(completed_at, 'Europe/Moscow') AS STRING) AS completed_msk
     FROM \`${a.projectId}.ozon_raw.OZON_INGESTION_RUNS\`
     WHERE status = 'OK' AND entity IN (${OZON_COVERAGE_ENTITIES.map((e) => `'${e}'`).join(', ')})
       AND source_to > '${committed}'`);
  const d1Msk = d1MoscowOf(a.now);
  const decision = decideCandidate({
    platform: 'OZON', mode: cfg.mode, manualLcd: cfg.manualLcd, committed, d1Msk,
    covered: coveredByRunWindows(runs.map((r) => ({ entity: r.entity, from: String(r.source_from ?? ''),
      to: completedWindowEnd(String(r.source_to ?? ''), r.completed_msk) })), OZON_COVERAGE_ENTITIES),
  });
  if (decision.code === 'MANUAL_LCD_INVALID' || decision.code === 'MANUAL_LCD_REGRESSION') {
    logLifecycle(a.log, 'OZON', 'MANUAL_OVERRIDE_ACTIVE', { outcome: 'REJECTED', code: decision.code, raw: cfg.manualRaw, notes: decision.notes }, 'error');
    throw new LoaderError(`OZON_MANUAL_LCD отвергнута: ${decision.notes.join('; ')}`, decision.code);
  }
  logLifecycle(a.log, 'OZON', cfg.mode === 'MANUAL' ? 'MANUAL_OVERRIDE_ACTIVE' : 'LCD_CANDIDATE', {
    mode: cfg.mode, committed, candidate: decision.candidate, d1_msk: d1Msk, will_advance: decision.willAdvance,
    gap_at: decision.gapAt, lifecycle_block: life.state, notes: decision.notes,
  }, cfg.mode === 'MANUAL' ? 'warn' : 'info');
  return { mode: cfg.mode, committed, candidate: decision.candidate, decision, d1Msk, lifecycle: life.state };
}

/**
 * Конец окна, за который прогон РЕАЛЬНО ручается. source_to — это дата ЗАПУСКА: прогон, завершённый
 * 23.09 в 06:32 МСК, пишет source_to = 2026-09-23 — день, который едва начался (замер prod 24.09).
 * Ручаться прогон может только за сутки, закончившиеся до его завершения: min(source_to, дата
 * завершения по МСК − 1). Без этого ночной или догоняющий прогон закрыл бы неполные сутки.
 */
export function completedWindowEnd(sourceTo: string, completedMsk: string | null): string {
  if (!completedMsk || !/^\d{4}-\d{2}-\d{2}$/.test(completedMsk)) return '';     // нет даты завершения — не ручается
  const lastFull = addDaysIso(completedMsk, -1);
  return sourceTo < lastFull ? sourceTo : lastFull;
}

/* ─────────────────────────── ёмкость: реальная вставка ─────────────────────────── */

export interface ExpansionOutcome {
  readonly geometry: OzonGeometry;
  readonly meta: SheetMeta;
  readonly grid: SheetCell[][];
  readonly insertedColumns: number;
  readonly tailCellsVerified: number;
}

const q = (name: string) => `'${name.replace(/'/g, "''")}'`;

async function readGrid(sheets: SheetsGateway, name: string, rows: number, cols: number): Promise<SheetCell[][]> {
  const [g] = await sheets.readValues([`${q(name)}!A1:${columnName(cols)}${rows}`]);
  return (g ?? []) as SheetCell[][];
}

function rect(grid: readonly (readonly SheetCell[])[], c1: number, c2: number, rows: number): string[][] {
  const out: string[][] = [];
  for (let r = 0; r < rows; r++) {
    const row = grid[r] ?? [];
    const line: string[] = [];
    for (let c = c1; c <= c2; c++) line.push(JSON.stringify(row[c - 1] ?? ''));
    out.push(line);
  }
  return out;
}

/**
 * Вставка слотов ПЕРЕД хвостом владельца — со снимком и доказательством после:
 *   1. план (capacity.planExpansion) — либо безопасный план, либо отказ БЕЗ мутаций;
 *   2. снимок хвоста: все значения колонок tailFirst..последней;
 *   3. insertDimension (одна атомарная структурная запись);
 *   4. доказательства: ширина выросла ровно на вставленное; хвост ПОСЛЕ сдвига побайтно равен
 *      снимку ДО; вставленные колонки пусты; зеркало LCD сдвинулось ровно на вставленное;
 *   5. геометрия ЗАНОВО выводится ИЗ ЛИСТА и обязана совпасть с планом. Дальше прогон работает
 *      только с ней — старое положение хвоста (562) после этого не используется нигде.
 */
export async function expandOzonCapacity(a: {
  sheets: SheetsGateway; sheetName: string; meta: SheetMeta; geometry: OzonGeometry;
  blocksNeeded: number; log: Logger; envTailFirst?: number;
}): Promise<ExpansionOutcome> {
  const { sheets, sheetName: name, meta, geometry: geo } = a;
  const plan = planExpansion({ slotsNow: geo.physicalSlots, blocksNeeded: a.blocksNeeded, tailFirstLive: geo.tailFirst, sheetColumnCount: meta.columnCount });
  if (isRefusal(plan)) {
    logLifecycle(a.log, 'OZON', 'NO_SAFE_EXPANSION_PATH', { code: 'NO_SAFE_EXPANSION_PATH', why: plan.why, blocks_needed: a.blocksNeeded, slots: geo.physicalSlots }, 'error');
    throw new LoaderError(`расширение листа Ozon невозможно доказать безопасным: ${plan.why}`, 'NO_SAFE_EXPANSION_PATH');
  }
  const gridBefore = await readGrid(sheets, name, meta.rowCount, meta.columnCount);
  if (plan.insertCount === 0) return { geometry: geo, meta, grid: gridBefore, insertedColumns: 0, tailCellsVerified: 0 };

  const tailBefore = rect(gridBefore, geo.tailFirst, meta.columnCount, meta.rowCount);
  const mirrorBefore = meta.namedRanges?.OZON_LCD_MIRROR ?? null;
  await sheets.structureWrite([{ insertDimension: {
    range: { sheetId: meta.sheetId, dimension: 'COLUMNS', startIndex: plan.insertAt, endIndex: plan.insertAt + plan.insertCount },
    inheritFromBefore: true } }]);

  const meta2 = await sheets.readSheetMeta(name, false);
  const fail = (why: string): never => {
    logLifecycle(a.log, 'OZON', 'NO_SAFE_EXPANSION_PATH', { code: 'CAPACITY_EXPANSION_VERIFY_FAILED', why, inserted: plan.insertCount }, 'error');
    throw new LoaderError(`проверка после вставки колонок: ${why}`, 'CAPACITY_EXPANSION_VERIFY_FAILED');
  };
  if (meta2.columnCount !== meta.columnCount + plan.insertCount) fail(`ширина листа ${meta2.columnCount} вместо ${meta.columnCount + plan.insertCount}`);
  const grid2 = await readGrid(sheets, name, meta2.rowCount, meta2.columnCount);
  const tailAfter = rect(grid2, geo.tailFirst + plan.insertCount, meta2.columnCount, meta.rowCount);
  if (JSON.stringify(tailAfter) !== JSON.stringify(tailBefore)) fail('хвост владельца после сдвига не совпал со снимком до вставки');
  // Пустота проверяется по САМИМ значениям. Первая версия сравнивала дважды сериализованную строку
  // и на пустых колонках давала ложное «не пусты» (репетиция 24.09: 0 непустых ячеек из 16 250).
  if (!columnsEmpty(grid2, plan.insertAt + 1, plan.insertAt + plan.insertCount)) fail('вставленные колонки не пусты');
  const mirror2 = meta2.namedRanges?.OZON_LCD_MIRROR ?? null;
  if (mirrorBefore && (!mirror2 || mirror2.col !== mirrorBefore.col + plan.insertCount || mirror2.row !== mirrorBefore.row)) {
    fail(`зеркало LCD ${JSON.stringify(mirrorBefore)} → ${JSON.stringify(mirror2)}, ожидался сдвиг на ${plan.insertCount}`);
  }
  const geo2 = deriveOzonGeometry({ grid: grid2, columnCount: meta2.columnCount, mirror: mirror2, envTailFirst: a.envTailFirst });
  if (geo2.physicalSlots !== plan.slotsAfter || geo2.tailFirst !== plan.tailFirstAfter) {
    fail(`геометрия из листа ${geo2.physicalSlots} слотов / хвост ${geo2.tailFirst} ≠ плану ${plan.slotsAfter} / ${plan.tailFirstAfter}`);
  }
  const tailCells = tailBefore.reduce((n, r) => n + r.filter((c) => c !== '""').length, 0);
  logLifecycle(a.log, 'OZON', 'CAPACITY_EXPANDED', {
    slots_before: plan.slotsBefore, slots_after: plan.slotsAfter, inserted_columns: plan.insertCount, insert_at: plan.insertAt,
    tail_before: plan.tailFirstBefore, tail_after: geo2.tailFirst, tail_cells_verified: tailCells, lcd_ref: geo2.lcdRef,
  });
  return { geometry: geo2, meta: meta2, grid: grid2, insertedColumns: plan.insertCount, tailCellsVerified: tailCells };
}

/* ─────────────────────────── перечитывание опубликованного ─────────────────────────── */

export async function readbackAndVerify(a: {
  sheets: SheetsGateway; sheetName: string; written: ReadonlyMap<string, unknown>;
  sections: ReadonlyArray<{ readonly titleRow: number; readonly firstRow: number; readonly lastRow: number; readonly mtdRow: number; readonly blockCount: number }>;
  tailFirst: number; summaryUpTo: string;
}): Promise<PublicationCheck[]> {
  if (!a.sections.length) return [];
  const top = Math.min(...a.sections.map((s) => s.titleRow));
  const bottom = Math.max(...a.sections.map((s) => s.mtdRow));
  const lastCol = a.tailFirst - 1;
  const box = `${q(a.sheetName)}!A${top}:${columnName(lastCol)}${bottom}`;
  const [vals] = await a.sheets.readValues([box]);
  const frm = await a.sheets.readFormulas(box);
  const at = (g: readonly (readonly unknown[])[] | undefined) => (r: number, c: number): unknown =>
    (r < top || r > bottom || c > lastCol) ? undefined : ((g?.[r - top] ?? [])[c - 1] ?? '');
  const values = at(vals), formulas = at(frm);
  // вне прямоугольника проверки ничего быть не должно: запись в хвост владельца — нарушение
  const outside: string[] = [];
  const inBox = new Map<string, unknown>();
  for (const [k, v] of a.written) {
    const [r, c] = k.split(':').map(Number) as [number, number];
    if (c >= a.tailFirst) outside.push(`${columnName(c)}${r}`);
    else if (r >= top && r <= bottom) inBox.set(k, v);
  }
  const EPOCH = Date.UTC(1899, 11, 30);
  const dateAt = (r: number): string | null => {
    const v = values(r, 2);
    return typeof v === 'number' ? new Date(EPOCH + v * 86_400_000).toISOString().slice(0, 10) : null;
  };
  const checks = verifyPublication({ written: inBox, values, formulas, sections: a.sections, dateAt, summaryUpTo: a.summaryUpTo });
  return [...checks, { name: 'NO_WRITE_INTO_OWNER_TAIL', pass: outside.length === 0, count: outside.length, sample: outside.slice(0, 10) }];
}
