/**
 * UNITKA WB — загрузчик `unitka-cancel-restore`: адресное восстановление зрелых суток по классу отмен
 * (sql/unitka/cancel_class_v1.sql; OWNER ACK «CANCEL RECONCILIATION» 02.11.2026).
 *
 * Что меняется в строке суток SKU — ровно три ячейки:
 *   S «Отменили товаров» — итоговый S класса (отмены в Q: с логистикой и без; отмена вне Q в S не входит);
 *   W «Доходность (общая)» — формула строки с поправкой «+V*(AF+REVERSE_LEG_RATE)», если в S есть отмены без логистики;
 *   заметка S — раздел КЛАСС ОТМЕНЫ: причина, даты, srid, источник (раздел об отказах вне Orders API сохраняется).
 * Пример (сверка октября 2026): 18.10/535580776 S 0 → 1 и V = 1 — ΔW = −AI (плечи не списываются);
 * 24.10/252442517 S 1 → 0 — ΔW = +(AI + AF + REV).
 *
 * Почему отдельный загрузчик: зрелость (финотчёт окончателен ≥ 46 суток) всегда старше окна сверки (35 суток),
 * суточный писатель и сверка этих суток не достигают и не перезапишут восстановленное. Уменьшение исторического S
 * без OWNER ACK запрещено — поэтому запись только так:
 *   ПЛАН (по умолчанию): BigQuery (только чтение) + лист (readonly-шлюз) → манифест с отпечатком в журнал.
 *   ЗАПИСЬ: ENVIRONMENT=prod И UNITKA_CANCEL_RESTORE_WRITE=1 И без DRY_RUN И UNITKA_CANCEL_RESTORE_DIGEST = отпечатку
 *   манифеста, пересчитанного из живого листа в момент записи (пишется ровно просмотренный план).
 *   ОТКАТ: UNITKA_CANCEL_RESTORE_DIRECTION=rollback + UNITKA_CANCEL_RESTORE_MANIFEST (+ отпечаток при записи).
 * Каждое поле ячейки (S, формула W, заметка) обязано быть «до» или «после» манифеста; иное — DRIFT, отказ ДО записи.
 * Аномалии класса (ANOMALY_*) входят в отпечаток манифеста; ЗАПИСЬ при аномалии, не принятой владельцем поимённо
 * (UNITKA_CANCEL_RESTORE_ACCEPT_ANOMALIES = «nmID|дата|статус,…»), — отказ CANCEL_RESTORE_ANOMALY до первой записи.
 * АТОМАРНОСТЬ (аудит PR #394): все поля всех суток — ОДИН spreadsheets.batchUpdate из updateCells (S: значение и заметка,
 * W: формула в локали книги, как пишет месяц monthprep); Sheets применяет пакет целиком или не применяет вовсе, поэтому
 * лист и снимок Store P&L никогда не видят S без W или W без S. Запросы ограничены sheetId манифеста и ячейками S/W
 * его суток (assertConfined). Ошибка или таймаут записи — исход неизвестен: только перечитывание и диагноз состояния
 * (ВСЁ ДО / ВСЁ ПОСЛЕ / СМЕШАНО), повтора нет (CANCEL_RESTORE_UNKNOWN_OUTCOME). Перечитывание сверяет поля и значение W
 * с формулой строки (Q·AI − X − AG − S·(AI+AF+REV) + Y + V·(AF+REV)) по живым ячейкам.
 */
import { createHash } from 'node:crypto';
import { gzipSync, gunzipSync } from 'node:zlib';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { UnitkaBq, type CancelClassRow } from './bq.js';
import { OFFSET, NAMED, colA1, findBlocks, isoToSerial, asNumber, type CellValue, type Block } from './model.js';
import { geometryAt, locateSection, monthKeyOf, dayIndexOf, dayRowOf, formatMonthKey, type MonthGeometry } from './calendar.js';
import { profitAllFormula, profitAllVoidQty, toLocaleFormula, formulaStyleOf, type FormulaStyle } from './formulas.js';
import { CANCEL_NOTE_MARKER, isEngineNote, joinSNote, noteKey } from './refusal_notes.js';
import { canonicalJson } from './monthrollback.js';
import { RECONCILIATION_EPOCH } from './reconcile.js';
import { quoteSheet, readColumnA } from './section.js';
import { defaultUnitkaDeps, ENGINE_VERSION, type UnitkaDeps } from './index.js';
import type { SheetsGateway, StructureRequest } from './sheets.js';

export const CANCEL_RESTORE_VERSION = `${ENGINE_VERSION}+cancel-restore`;
export const CANCEL_MANIFEST_KIND = 'unitka-cancel/class-restore';
export const CANCEL_MANIFEST_VERSION = 1;
/** Первый день счётчика Q из воронки API — раньше класс отмен не определён (cancel_class_v1.sql, cfg.funnel_api_from). */
export const CANCEL_CLASS_FROM = '2026-09-04';

export function cancelRestoreWriteAllowed(config: { environment: string; unitkaCancelRestoreWrite?: boolean; unitkaDryRun?: boolean }): boolean {
  return config.environment === 'prod' && config.unitkaCancelRestoreWrite === true && config.unitkaDryRun !== true;
}

/* ───────────────────────────── заметка ───────────────────────────── */

const pairs = (srids: readonly string[], dates: readonly string[]): string =>
  srids.map((s, i) => `${s}${dates[i] ? ` (отмена ${dates[i]})` : ''}`).join(', ') || '—';

/**
 * Раздел заметки S о классе отмены суток. Только факты строки класса: дата отмены, дата финотчёта, srid, источник.
 * Даты прогона в тексте нет — отпечаток манифеста от дня исполнения не зависит.
 */
export function cancelNoteText(r: CancelClassRow): string {
  const why: string[] = [];
  if (r.nologInQ > 0) why.push(`${r.nologInQ} шт. без логистики остались в воронке (Q): входят в S, плечи не списываются — поправка W +${r.nologInQ}·(AF+REVERSE_LEG_RATE)`);
  if (r.nologAbsentQty > 0) why.push(`${r.nologAbsentQty} шт. без логистики в воронке нет (мест в Q не осталось): в S не входят`);
  if (r.logInQ > 0) why.push(`${r.logInQ} шт. с логистикой в воронке: в S, как любая отмена`);
  return [
    `${CANCEL_NOTE_MARKER} ${r.date}: S ${r.legacyCancels} → ${r.finalCancels}`,
    ...why,
    `Воронка Q = ${r.funnelOrders}; заказов Orders API без отмены ${r.keptQty}; доказанных вне Orders API ${r.refusalCountedQty + r.soldNotInApiQty}; мест для отмен ${r.room}.`,
    `Отмены Orders API: с логистикой ${r.cancelLogQty}, без логистики ${r.cancelNologQty}.`,
    `Логистики нет: финотчёт окончателен непрерывно с ${r.date} по ${r.matureOn} (46 суток; недели FINANCE_WEEK_STATUS, отчёты FINANCE_REPORT_LOADS), последний окончательный отчёт — по ${r.financeFinalThrough}.`,
    `srid без логистики: ${pairs(r.nologSrids, r.nologCancelDates)}`,
    ...(r.logSrids.length ? [`srid с логистикой: ${r.logSrids.join(', ')}`] : []),
    'Источник: wb_mart.V_UNITKA_CANCEL_CLASS_DAILY (sql/unitka/cancel_class_v1.sql); запись — unitka-cancel-restore по OWNER ACK.',
  ].join('\n');
}

/** Раздел заметки об отказах вне Orders API (всё до раздела класса отмен) — сохраняется при восстановлении. */
function refusalSectionOf(note: string): string {
  const i = note.indexOf(CANCEL_NOTE_MARKER);
  return (i < 0 ? note : note.slice(0, i)).trimEnd();
}

/* ───────────────────────────── манифест ───────────────────────────── */

export interface CancelFieldState { s: number; wFormula: string; note: string }
export interface CancelManifestCell {
  date: string; nmId: number; month: string; row: number; sCol: number; wCol: number; sA1: string; wA1: string; blockStart: number;
  legacyCancels: number; finalCancels: number; voidQty: number;
  before: CancelFieldState; after: CancelFieldState;
}
export interface CancelManifest {
  kind: typeof CANCEL_MANIFEST_KIND; version: number;
  spreadsheetId: string; sheetId: number; sheetName: string; style: FormulaStyle;
  financeFinalThrough: string;
  cells: CancelManifestCell[];
  /** Аномалии класса на момент плана (ключ nmID|дата|статус): просмотренный отпечаток фиксирует и их. */
  anomalies: string[];
  digest: string;
}

const sha = (v: unknown): string => createHash('sha256').update(canonicalJson(v)).digest('hex');

export function buildCancelManifest(cells: readonly CancelManifestCell[], a: { spreadsheetId: string; sheetId: number; sheetName: string; style: FormulaStyle; financeFinalThrough: string; anomalies?: readonly string[] }): CancelManifest {
  const body: Omit<CancelManifest, 'digest'> = { kind: CANCEL_MANIFEST_KIND, version: CANCEL_MANIFEST_VERSION, ...a, anomalies: [...(a.anomalies ?? [])].sort(), cells: [...cells] };
  return { ...body, digest: sha(body) };
}

/** gzip + base64url с префиксом «gz.» — манифест помещается в переменную окружения Job. */
export function encodeCancelManifest(m: CancelManifest): string {
  return `gz.${gzipSync(Buffer.from(canonicalJson(m), 'utf8')).toString('base64url')}`;
}

export function parseCancelManifest(raw: string): CancelManifest | { error: string } {
  const text = raw.trim();
  if (!text) return { error: 'манифест восстановления отмен не передан' };
  let obj: unknown;
  try {
    obj = JSON.parse(text.startsWith('{') ? text : gunzipSync(Buffer.from(text.replace(/^gz\./, ''), 'base64url')).toString('utf8'));
  } catch { return { error: 'манифест восстановления отмен не разбирается' }; }
  const m = obj as Partial<CancelManifest> | null;
  if (!m || m.kind !== CANCEL_MANIFEST_KIND || m.version !== CANCEL_MANIFEST_VERSION || !Array.isArray(m.cells) || !Array.isArray(m.anomalies) || typeof m.digest !== 'string') {
    return { error: 'вид, версия или поля манифеста восстановления отмен' };
  }
  const { digest, ...body } = m as CancelManifest;
  if (sha(body) !== digest) return { error: 'отпечаток манифеста не совпадает с содержимым — изменён или обрезан' };
  for (const c of m.cells) {
    if (c.sCol !== c.blockStart + OFFSET.cancels || c.wCol !== c.blockStart + OFFSET.profitAll || c.date < CANCEL_CLASS_FROM) {
      return { error: `манифест: ${c.sA1}/${c.wA1} ${c.date} вне контракта (S и W одного блока, не раньше ${CANCEL_CLASS_FROM})` };
    }
  }
  return m as CancelManifest;
}

/* ───────────────────────────── чтение листа ───────────────────────────── */

/** Сутки одного блока, как они стоят в листе: всё, что нужно плану и перечитыванию. */
export interface LiveRow {
  nmId: number; date: string; month: string; row: number; blockStart: number;
  dateSerial: CellValue; s: CellValue; wFormula: CellValue; w: CellValue; note: string;
  q: CellValue; x: CellValue; ag: CellValue; y: CellValue; ai: CellValue; af: CellValue;
}

/** Секции месяцев с нужными сутками: значения и формулы заголовок..MTD, заметки S. Только чтение. */
export async function readLiveRows(
  sheets: SheetsGateway, sheetName: string, rowCount: number, width: number, keys: ReadonlyArray<{ nmId: number; date: string }>,
): Promise<{ rows: Map<string, LiveRow>; missing: string[] }> {
  const q = quoteSheet(sheetName);
  const columnA = await readColumnA(sheets, sheetName, rowCount);
  const rows = new Map<string, LiveRow>();
  const missing: string[] = [];
  const byMonth = new Map<string, Array<{ nmId: number; date: string }>>();
  for (const k of keys) (byMonth.get(k.date.slice(0, 7)) ?? byMonth.set(k.date.slice(0, 7), []).get(k.date.slice(0, 7))!).push(k);
  for (const [month, list] of byMonth) {
    const key = monthKeyOf(`${month}-01`);
    const loc = locateSection(columnA, key);
    if (loc.status !== 'FOUND') { for (const k of list) missing.push(`${k.nmId} ${k.date}: секция ${month} — ${loc.status}`); continue; }
    const g: MonthGeometry = geometryAt(key, loc.topRow);
    const range = `${q}!A${g.topRow}:${colA1(width)}${g.mtdRow}`;
    const [[values], formulas] = await Promise.all([sheets.readValues([range]), sheets.readFormulas(range)]);
    const at = (grid: readonly (readonly CellValue[])[] | undefined, r: number, c: number): CellValue => grid?.[r - g.topRow]?.[c - 1] ?? '';
    const blocks: Block[] = findBlocks(values?.[0] ?? [], width);
    const found: LiveRow[] = [];
    for (const k of list) {
      const b = blocks.find((x) => x.nmId === k.nmId);
      if (!b) { missing.push(`${k.nmId} ${k.date}: блока nmID в секции ${month} нет`); continue; }
      const row = dayRowOf(g, dayIndexOf(key, k.date));
      const v = (off: number): CellValue => at(values, row, b.start + off);
      found.push({
        nmId: k.nmId, date: k.date, month: formatMonthKey(key), row, blockStart: b.start, note: '',
        dateSerial: v(OFFSET.date), s: v(OFFSET.cancels), wFormula: at(formulas, row, b.start + OFFSET.profitAll), w: v(OFFSET.profitAll),
        q: v(OFFSET.orders), x: v(OFFSET.adsIn), ag: v(OFFSET.storage), y: v(OFFSET.adsOut), ai: v(OFFSET.unitProfit), af: v(OFFSET.logistics),
      });
    }
    if (found.length && !sheets.readNotes) throw new LoaderError('шлюз Sheets не читает заметки — заметку S сверить нельзя', 'CANCEL_RESTORE_NOTES_UNSUPPORTED');
    const notes = found.length ? await sheets.readNotes!(sheetName, found.map((f) => ({ row: f.row, col: f.blockStart + OFFSET.cancels }))) : new Map<string, string>();
    for (const f of found) rows.set(`${f.nmId}|${f.date}`, { ...f, note: notes.get(noteKey(f.row, f.blockStart + OFFSET.cancels)) ?? '' });
  }
  return { rows, missing };
}

/* ───────────────────────────── план ───────────────────────────── */

type FieldState = 'BEFORE' | 'AFTER' | 'DRIFT';
const numEq = (v: CellValue, want: number): boolean => { const n = asNumber(v); return Number.isFinite(n) && Math.abs(n - want) < 0.005; };

export interface CancelRestorePlan {
  cells: CancelManifestCell[];
  /** Ячейки, все поля которых уже «после» (повтор после записи). */
  alreadyApplied: number;
  /** Отказы плана — запись невозможна, пока список не пуст. */
  refusals: string[];
  /** Аномалии класса: лист по ним не меняется; описание для журнала. */
  anomalies: string[];
  /** Ключи аномалий nmID|дата|статус — в отпечатке; запись требует поимённого принятия каждой. */
  anomalyKeys: string[];
  /** Ожидаемое изменение W по суткам при текущей REVERSE_LEG_RATE — только журнал (в отпечаток не входит). */
  expectedDeltaW: Array<{ date: string; nmId: number; deltaW: number }>;
}

/**
 * План восстановления. rows — строки V_UNITKA_CANCEL_CLASS_DAILY (FINAL с изменением и ANOMALY_*); live — сутки листа;
 * windowFrom — начало окна сверки: сутки окна писатель ещё ведёт, восстанавливать их нельзя (отказ, а не пропуск).
 */
export function planCancelRestore(a: {
  rows: readonly CancelClassRow[]; live: ReadonlyMap<string, LiveRow>; missing: readonly string[];
  windowFrom: string; reverseLegRate: number; style: FormulaStyle;
}): CancelRestorePlan {
  const cells: CancelManifestCell[] = [];
  const refusals = [...a.missing];
  const anomalies: string[] = [];
  const anomalyKeys: string[] = [];
  const expectedDeltaW: CancelRestorePlan['expectedDeltaW'] = [];
  let alreadyApplied = 0;
  for (const r of a.rows) {
    const where = `${r.nmId} ${r.date}`;
    if (r.status.startsWith('ANOMALY')) {
      anomalies.push(`${where}: ${r.status} (Q ${r.funnelOrders}, без отмены ${r.keptQty}, с логистикой ${r.cancelLogQty}, мест ${r.room})`);
      anomalyKeys.push(anomalyKey(r));
      continue;
    }
    if (r.status !== 'FINAL' || !r.sheetChangeRequired) continue;
    if (r.date < CANCEL_CLASS_FROM || r.date < RECONCILIATION_EPOCH) { refusals.push(`${where}: раньше ${CANCEL_CLASS_FROM}`); continue; }
    if (r.date >= a.windowFrom) { refusals.push(`${where}: внутри окна сверки (с ${a.windowFrom}) — сутки ещё ведёт писатель`); continue; }
    const l = a.live.get(`${r.nmId}|${r.date}`);
    if (!l) { if (!a.missing.some((m) => m.startsWith(where))) refusals.push(`${where}: сутки не найдены в листе`); continue; }
    if (!numEq(l.dateSerial, isoToSerial(r.date))) { refusals.push(`${where}: дата блока в строке ${l.row} ≠ ${r.date}`); continue; }
    const sA1 = `${colA1(l.blockStart + OFFSET.cancels)}${l.row}`, wA1 = `${colA1(l.blockStart + OFFSET.profitAll)}${l.row}`;
    const liveV = profitAllVoidQty(l.wFormula, l.blockStart, l.row, a.style);
    if (liveV === null) { refusals.push(`${where}: формула W ${wA1} не каноническая — правлена вручную? (${String(l.wFormula)})`); continue; }
    if (!isEngineNote(l.note)) { refusals.push(`${where}: на ${sA1} заметка владельца — не перезаписывается`); continue; }
    const cancelPart = cancelNoteText(r);
    const before: CancelFieldState = { s: asNumber(l.s), wFormula: String(l.wFormula), note: l.note };
    const after: CancelFieldState = {
      s: r.finalCancels,
      wFormula: toLocaleFormula(profitAllFormula(l.blockStart, l.row, r.voidQty), a.style),
      note: joinSNote(refusalSectionOf(l.note), cancelPart),
    };
    const sState: FieldState = numEq(l.s, r.finalCancels) ? 'AFTER' : numEq(l.s, r.legacyCancels) ? 'BEFORE' : 'DRIFT';
    const wState: FieldState = liveV === r.voidQty ? 'AFTER' : liveV === 0 ? 'BEFORE' : 'DRIFT';
    if (sState === 'DRIFT') { refusals.push(`${where}: S ${sA1} = ${String(l.s)}, ожидалось ${r.legacyCancels} (до) или ${r.finalCancels} (после)`); continue; }
    if (wState === 'DRIFT') { refusals.push(`${where}: поправка W ${wA1} = ${liveV}, ожидалось 0 (до) или ${r.voidQty} (после)`); continue; }
    // «до» фиксирует ИСХОДНОЕ состояние: поле, уже стоящее «после», в манифесте отката возвращается к прежнему S / канону W.
    const orig: CancelFieldState = {
      s: sState === 'AFTER' ? r.legacyCancels : before.s,
      wFormula: wState === 'AFTER' ? toLocaleFormula(profitAllFormula(l.blockStart, l.row, 0), a.style) : before.wFormula,
      note: l.note === after.note ? refusalSectionOf(l.note) : before.note,
    };
    if (sState === 'AFTER' && wState === 'AFTER' && l.note === after.note) alreadyApplied++;
    cells.push({
      date: r.date, nmId: r.nmId, month: l.month, row: l.row, sCol: l.blockStart + OFFSET.cancels, wCol: l.blockStart + OFFSET.profitAll,
      sA1, wA1, blockStart: l.blockStart, legacyCancels: r.legacyCancels, finalCancels: r.finalCancels, voidQty: r.voidQty, before: orig, after,
    });
    const ai = asNumber(l.ai), af = asNumber(l.af);
    expectedDeltaW.push({ date: r.date, nmId: r.nmId,
      deltaW: Math.round((-(r.finalCancels - r.legacyCancels) * (ai + af + a.reverseLegRate) + r.voidQty * (af + a.reverseLegRate)) * 100) / 100 });
  }
  return { cells, alreadyApplied, refusals, anomalies, anomalyKeys, expectedDeltaW };
}

/** Ключ аномалии для поимённого принятия владельцем. */
export const anomalyKey = (r: Pick<CancelClassRow, 'nmId' | 'date' | 'status'>): string => `${r.nmId}|${r.date}|${r.status}`;

/** Аномалии, не принятые владельцем поимённо: любая — запись запрещена. */
export function unacceptedAnomalies(keys: readonly string[], accepted: readonly string[]): string[] {
  const ok = new Set(accepted.map((x) => x.trim()).filter(Boolean));
  return keys.filter((k) => !ok.has(k));
}

/* ───────────────────────────── запись и перечитывание ───────────────────────────── */

/** Модель W строки по живым ячейкам — то же, что считает формула листа с поправкой. */
export function modelW(l: Pick<LiveRow, 'q' | 'x' | 'ag' | 'y' | 'ai' | 'af' | 's'>, voidQty: number, rev: number): number {
  const n = (v: CellValue): number => { const x = asNumber(v); return Number.isFinite(x) ? x : 0; };
  return n(l.q) * n(l.ai) - n(l.x) - n(l.ag) - n(l.s) * (n(l.ai) + n(l.af) + rev) + n(l.y) + voidQty * (n(l.af) + rev);
}

/** Сверка ячеек с целевым состоянием; пустой список — всё совпало. */
export function verifyCancelCells(cells: readonly CancelManifestCell[], live: ReadonlyMap<string, LiveRow>, target: 'after' | 'before', rev: number, style: FormulaStyle): string[] {
  const bad: string[] = [];
  for (const c of cells) {
    const l = live.get(`${c.nmId}|${c.date}`);
    const want = c[target];
    const v = target === 'after' ? c.voidQty : 0;
    if (!l) { bad.push(`${c.date} ${c.nmId}: сутки не найдены`); continue; }
    if (!numEq(l.s, want.s)) bad.push(`${c.sA1}: S ${String(l.s)} ≠ ${want.s}`);
    if (profitAllVoidQty(l.wFormula, c.blockStart, c.row, style) !== v) bad.push(`${c.wA1}: формула W не с поправкой ${v}`);
    if (l.note !== want.note) bad.push(`${c.sA1}: заметка не совпала`);
    const w = asNumber(l.w), m = modelW(l, v, rev);
    if (!Number.isFinite(w) || Math.abs(w - m) > 0.01) bad.push(`${c.wA1}: W ${String(l.w)} ≠ модели ${m.toFixed(2)}`);
  }
  return bad;
}

const cellRange = (sheetId: number, row: number, col: number): Record<string, number> =>
  ({ sheetId, startRowIndex: row - 1, endRowIndex: row, startColumnIndex: col - 1, endColumnIndex: col });

/**
 * Запросы ОДНОГО batchUpdate для полей, отличающихся от цели: S — значение и заметка одной ячейкой (updateCells,
 * fields userEnteredValue,note — формат ячейки не трогается), W — формула в локали книги (fields userEnteredValue).
 */
export function cancelWriteRequests(sheetId: number, cells: readonly CancelManifestCell[], live: ReadonlyMap<string, LiveRow>, target: 'after' | 'before'): StructureRequest[] {
  const out: StructureRequest[] = [];
  for (const c of cells) {
    const l = live.get(`${c.nmId}|${c.date}`);
    if (!l) throw new LoaderError(`${c.date} ${c.nmId}: сутки не найдены перед записью`, 'CANCEL_RESTORE_DRIFT');
    const want = c[target];
    if (!numEq(l.s, want.s) || l.note !== want.note) {
      out.push({ updateCells: { range: cellRange(sheetId, c.row, c.sCol), rows: [{ values: [{ userEnteredValue: { numberValue: want.s }, note: want.note }] }], fields: 'userEnteredValue,note' } });
    }
    if (String(l.wFormula) !== want.wFormula) {
      out.push({ updateCells: { range: cellRange(sheetId, c.row, c.wCol), rows: [{ values: [{ userEnteredValue: { formulaValue: want.wFormula } }] }], fields: 'userEnteredValue' } });
    }
  }
  return out;
}

/** Каждый запрос — updateCells одной ячейки S или W суток манифеста на листе манифеста; иное — отказ без записи. */
export function assertConfined(requests: readonly StructureRequest[], sheetId: number, cells: readonly CancelManifestCell[]): void {
  const allowed = new Set(cells.flatMap((c) => [`${c.row}|${c.sCol}`, `${c.row}|${c.wCol}`]));
  for (const q of requests) {
    const u = (q as { updateCells?: { range?: Record<string, number> } }).updateCells;
    const r = u?.range;
    const ok = Object.keys(q).length === 1 && r !== undefined && r.sheetId === sheetId && r.endRowIndex === r.startRowIndex! + 1
      && r.endColumnIndex === r.startColumnIndex! + 1 && allowed.has(`${r.startRowIndex! + 1}|${r.startColumnIndex! + 1}`);
    if (!ok) throw new LoaderError(`запрос вне ячеек манифеста: ${JSON.stringify(q).slice(0, 200)}`, 'CANCEL_RESTORE_NOT_CONFINED');
  }
}

/** Состояние суток после неизвестного исхода записи: все поля «до», все «после» или смешано (диагноз, без повтора). */
export function outcomeState(cells: readonly CancelManifestCell[], live: ReadonlyMap<string, LiveRow>, style: FormulaStyle): 'ALL_BEFORE' | 'ALL_AFTER' | 'MIXED' {
  let before = 0, after = 0;
  for (const c of cells) {
    const l = live.get(`${c.nmId}|${c.date}`);
    if (!l) return 'MIXED';
    const v = profitAllVoidQty(l.wFormula, c.blockStart, c.row, style);
    if (numEq(l.s, c.after.s) && v === c.voidQty && l.note === c.after.note) after++;
    else if (numEq(l.s, c.before.s) && v === 0 && l.note === c.before.note) before++;
  }
  return after === cells.length ? 'ALL_AFTER' : before === cells.length ? 'ALL_BEFORE' : 'MIXED';
}

/**
 * Одна атомарная запись. Ошибка API — исход неизвестен: перечитать, назвать состояние, НЕ повторять.
 * Возвращает число запросов пакета.
 */
async function writeAtomic(
  sheets: SheetsGateway, sheetName: string, meta: { sheetId: number; rowCount: number; columnCount: number }, cells: readonly CancelManifestCell[],
  live: ReadonlyMap<string, LiveRow>, target: 'after' | 'before', style: FormulaStyle, log: LoaderContext['logger'],
): Promise<number> {
  const requests = cancelWriteRequests(meta.sheetId, cells, live, target);
  assertConfined(requests, meta.sheetId, cells);
  if (!requests.length) return 0;
  try {
    return await sheets.structureWrite(requests);
  } catch (e) {
    const state = outcomeState(cells, (await readLiveRows(sheets, sheetName, meta.rowCount, meta.columnCount, cells)).rows, style);
    log.error('unitka_cancel_restore_unknown_outcome', { target, requests: requests.length, state, error: e instanceof Error ? e.message : String(e) });
    throw new LoaderError(`исход записи неизвестен (${state}): только перечитывание и решение владельца, повтора нет`, 'CANCEL_RESTORE_UNKNOWN_OUTCOME');
  }
}

/** Состояние поля относительно манифеста при откате: каждое поле «до» или «после», иначе DRIFT. */
export function rollbackDrift(cells: readonly CancelManifestCell[], live: ReadonlyMap<string, LiveRow>, style: FormulaStyle): string[] {
  const out: string[] = [];
  for (const c of cells) {
    const l = live.get(`${c.nmId}|${c.date}`);
    if (!l) { out.push(`${c.date} ${c.nmId}: сутки не найдены`); continue; }
    if (!numEq(l.s, c.after.s) && !numEq(l.s, c.before.s)) out.push(`${c.sA1}: S ${String(l.s)} — ни до (${c.before.s}), ни после (${c.after.s})`);
    const v = profitAllVoidQty(l.wFormula, c.blockStart, c.row, style);
    if (v !== c.voidQty && v !== 0) out.push(`${c.wA1}: формула W изменена после восстановления`);
    if (l.note !== c.after.note && l.note !== c.before.note) out.push(`${c.sA1}: заметка изменена после восстановления`);
  }
  return out;
}

/* ───────────────────────────── загрузчик ───────────────────────────── */

export async function unitkaCancelRestoreLoader(ctx: LoaderContext, deps: UnitkaDeps = defaultUnitkaDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const write = cancelRestoreWriteAllowed(config);
  const direction = config.unitkaCancelRestoreDirection ?? 'apply';
  const log = logger.child({ engine: CANCEL_RESTORE_VERSION, mode: write ? 'WRITE' : 'PLAN', direction });
  if (config.unitkaCancelRestoreDirectionInvalid) log.warn('unitka_cancel_restore_direction_invalid', { value: config.unitkaCancelRestoreDirectionInvalid, applied: direction });

  const sheets = deps.makeSheets(ctx, !write);
  const sheetName = config.unitkaSheetName;
  const meta = await sheets.readSheetMeta(sheetName);
  const style = formulaStyleOf(meta.locale);
  const [revV] = await sheets.readValues([NAMED.REVERSE]);
  const rev = asNumber(revV?.[0]?.[0] ?? null);
  if (!Number.isFinite(rev) || rev <= 0) throw new LoaderError(`${NAMED.REVERSE} = ${String(revV?.[0]?.[0])}`, 'CANCEL_RESTORE_SHEET');
  const book = { spreadsheetId: config.unitkaSpreadsheetId, sheetId: meta.sheetId, sheetName };

  if (direction === 'rollback') {
    const m = parseCancelManifest(config.unitkaCancelRestoreManifest ?? '');
    if ('error' in m) throw new LoaderError(`UNITKA_CANCEL_RESTORE_MANIFEST: ${m.error}`, 'CANCEL_RESTORE_MANIFEST_INVALID');
    if (m.spreadsheetId !== book.spreadsheetId || m.sheetName !== sheetName || m.sheetId !== meta.sheetId || m.style !== style) {
      throw new LoaderError(`манифест для ${m.spreadsheetId}/${m.sheetName}#${m.sheetId} (${m.style}), загрузчик — ${book.spreadsheetId}/${sheetName}#${meta.sheetId} (${style})`, 'CANCEL_RESTORE_WRONG_BOOK');
    }
    if (write && config.unitkaCancelRestoreDigest !== m.digest) throw new LoaderError(`UNITKA_CANCEL_RESTORE_DIGEST ≠ отпечатку манифеста ${m.digest}`, 'CANCEL_RESTORE_DIGEST_MISMATCH');
    const { rows: live, missing } = await readLiveRows(sheets, sheetName, meta.rowCount, meta.columnCount, m.cells);
    const drift = [...missing, ...rollbackDrift(m.cells, live, style)];
    log.info('unitka_cancel_restore_rollback_plan', { manifest_digest: m.digest, cells: m.cells.length, drift: drift.length, drift_sample: drift.slice(0, 10) });
    if (drift.length) throw new LoaderError(`${drift.length} полей изменены после восстановления — откат не перетирает чужие правки`, 'CANCEL_RESTORE_DRIFT');
    if (!write) return { rowsFetched: m.cells.length, rowsLoaded: 0 };
    const written = await writeAtomic(sheets, sheetName, meta, m.cells, live, 'before', style, log);
    const after = (await readLiveRows(sheets, sheetName, meta.rowCount, meta.columnCount, m.cells)).rows;
    const bad = verifyCancelCells(m.cells, after, 'before', rev, style);
    log.info('unitka_cancel_restore_rollback_done', { manifest_digest: m.digest, cells: m.cells.length, written, verify_failed: bad.length, sample: bad.slice(0, 10) });
    if (bad.length) throw new LoaderError(`после отката ${bad.length} расхождений: ${bad.slice(0, 5).join('; ')}`, 'CANCEL_RESTORE_VERIFY_FAILED');
    return { rowsFetched: m.cells.length, rowsLoaded: written };
  }

  const bq = new UnitkaBq(deps.makeRunner(ctx), config.unitkaMartDataset, config.unitkaOpsDataset, config.unitkaRunsTable);
  const [rows, window] = await Promise.all([bq.cancelClassRows(), bq.reconWindow()]);
  const keys = rows.filter((r) => r.status === 'FINAL' && r.sheetChangeRequired);
  const { rows: live, missing } = await readLiveRows(sheets, sheetName, meta.rowCount, meta.columnCount, keys);
  const plan = planCancelRestore({ rows, live, missing, windowFrom: window.from, reverseLegRate: rev, style });
  const fft = rows.reduce((m, r) => (r.financeFinalThrough > m ? r.financeFinalThrough : m), '');
  const manifest = buildCancelManifest(plan.cells, { ...book, style, financeFinalThrough: fft, anomalies: plan.anomalyKeys });
  const unaccepted = unacceptedAnomalies(plan.anomalyKeys, config.unitkaCancelRestoreAcceptedAnomalies ?? []);
  log.info('unitka_cancel_restore_plan', {
    manifest_digest: manifest.digest, manifest_b64: encodeCancelManifest(manifest), cells: plan.cells.length, already_applied: plan.alreadyApplied,
    finance_final_through: fft, window_from: window.from, reverse_leg_rate: rev,
    rows: plan.cells.map((c) => `${c.date} ${c.nmId} ${c.sA1}: S ${c.before.s} → ${c.after.s}, V ${c.voidQty}`),
    expected_delta_w: plan.expectedDeltaW, expected_delta_w_total: Math.round(plan.expectedDeltaW.reduce((s, x) => s + x.deltaW, 0) * 100) / 100,
    refusals: plan.refusals, anomalies: plan.anomalies, anomaly_keys: plan.anomalyKeys, anomalies_unaccepted: unaccepted,
  });
  if (plan.anomalies.length) log.warn('unitka_cancel_class_anomalies', { anomalies: plan.anomalies });
  if (plan.refusals.length) throw new LoaderError(`план восстановления отмен отказан: ${plan.refusals.slice(0, 5).join(' | ')}`, 'CANCEL_RESTORE_DRIFT');
  if (!write) return { rowsFetched: rows.length, rowsLoaded: 0 };
  // Аномалия, не принятая владельцем поимённо, останавливает ЗАПИСЬ до первой записи (контракт выпуска).
  if (unaccepted.length) {
    throw new LoaderError(`аномалии класса отмен не приняты владельцем: ${unaccepted.join(', ')} — запись остановлена`, 'CANCEL_RESTORE_ANOMALY');
  }
  if (config.unitkaCancelRestoreDigest !== manifest.digest) {
    throw new LoaderError(`UNITKA_CANCEL_RESTORE_DIGEST «${config.unitkaCancelRestoreDigest ?? ''}» ≠ отпечатку плана ${manifest.digest}: лист или источник изменились после просмотра`, 'CANCEL_RESTORE_DIGEST_MISMATCH');
  }
  const written = await writeAtomic(sheets, sheetName, meta, plan.cells, live, 'after', style, log);
  const after = (await readLiveRows(sheets, sheetName, meta.rowCount, meta.columnCount, plan.cells)).rows;
  const bad = verifyCancelCells(plan.cells, after, 'after', rev, style);
  log.info('unitka_cancel_restore_done', { manifest_digest: manifest.digest, cells: plan.cells.length, written, verify_failed: bad.length, sample: bad.slice(0, 10) });
  if (bad.length) throw new LoaderError(`после восстановления ${bad.length} расхождений: ${bad.slice(0, 5).join('; ')}`, 'CANCEL_RESTORE_VERIFY_FAILED');
  return { rowsFetched: rows.length, rowsLoaded: written };
}
