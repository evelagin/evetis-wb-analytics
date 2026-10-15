/**
 * D4 — публикация плана во вкладку «Ozon Магазин P&L». Модуль чистый: гейты, решение, запросы. I/O — index.ts.
 *
 * F-18 (узкое исключение — предложено, активно только по OWNER ACK первой записи): автоматическая запись ТОЛЬКО в
 * эту вкладку (sheetId из OZON_STORE_PNL_SHEET_ID) и только представлением ozon_mart.V_OZON_STORE_PNL_MONTHLY.
 * Каждый запрос ограничен sheetId вкладки (assertOnlyTab — тот же контроль, что у «WB Магазин P&L»).
 *
 * Гейты СТРОЖЕ WB (OWNER ACK 15.10 §4): любая неразобранная операция, нарушение тождества, неизвестное состояние,
 * «сверено» при открытых условиях, несошедшиеся деньги RAW → леджер или несвежие источники — публикация
 * БЛОКИРУЕТСЯ целиком (вкладка остаётся на прошлой опубликованной версии, алерт по коду).
 *
 * Защита ручных правок: отпечаток содержимого A:Z (sha256) — в developer metadata вкладки; писать можно, только
 * если текущее содержимое равно последнему опубликованному или явно принятому владельцем OZON_STORE_PNL_ADOPT_SHA.
 * Содержимое уже равно плану → NOOP. Значения, форматы, заметки и отпечатки — один атомарный batchUpdate.
 */
import { createHash } from 'node:crypto';
import type { CellValue } from '../model.js';
import { assertOnlyTab, contentSha, normalizeGrid } from '../storepnl/publish.js';
import { COL, STATE_LABEL, CLOSURE_LABEL, TAB_COLS, TAB_HEADER, TAB_NOTES, identityResidual, toNum, toStr, type PnlRow, type TabPlan } from './tabplan.js';
import type { GateInputs } from './bq.js';

export { assertOnlyTab, contentSha, normalizeGrid, TAB_COLS };
export const PUBLISH_META_KEY = 'evetis.ozon_store_pnl.published_sha';
export const CONTRACT_META_KEY = 'evetis.ozon_store_pnl.contract_sha';

/** sha256 контракта: изменение заголовка, заметки или ширины = новая схема, только управляемой миграцией. */
export function contractSha(): string {
  return createHash('sha256').update(JSON.stringify({ cols: TAB_COLS, header: TAB_HEADER, notes: TAB_NOTES })).digest('hex');
}

export function decideContract(input: { publishedContract?: string; allowMigrationTo?: string; firstPublication: boolean }):
  { ok: true; migrate: boolean } | { ok: false; message: string } {
  const want = contractSha();
  if (input.publishedContract === want) return { ok: true, migrate: false };
  // Первая публикация в пустую вкладку (нет ни отпечатка значений, ни контракта) ставит контракт без миграции.
  if (input.publishedContract === undefined && input.firstPublication) return { ok: true, migrate: false };
  if (input.allowMigrationTo === want) return { ok: true, migrate: true };
  return { ok: false, message: `контракт вкладки ${input.publishedContract ?? '(нет)'} ≠ коду ${want}; миграция только с OZON_STORE_PNL_MIGRATE_CONTRACT=${want} (OWNER ACK)` };
}

export interface GateResult { source: string[]; model: string[] }

/**
 * Гейт публикации. source — готовность источников (свежесть загрузок Ozon); model — финансовая целостность
 * представления. Любой непустой список — публикации нет. Известные WARN QA (Q16 мост к L4, Q20 жизненный цикл
 * отправлений) гейтом не являются: они раскрыты в состоянии и причине месяца.
 */
export function publishGate(pnl: readonly PnlRow[], g: GateInputs): GateResult {
  const source: string[] = [];
  const model: string[] = [];
  if (pnl.length === 0) model.push('представление P&L пусто — публикация стёрла бы вкладку');
  const months: string[] = [];
  for (const p of pnl) {
    const m = toStr(p.pnl_month).slice(0, 7);
    months.push(m);
    const st = toStr(p.financial_state);
    const cl = toStr(p.month_closure_status);
    const n = (k: string): number => toNum(p[k]);
    if (!STATE_LABEL[st]) model.push(`${m}: неизвестное состояние ${st}`);
    if (!CLOSURE_LABEL[cl]) model.push(`${m}: неизвестное закрытие ${cl}`);
    if (st === 'PENDING_CLASSIFICATION') model.push(`${m}: есть неразобранные операции (PENDING_CLASSIFICATION)`);
    const res = identityResidual(p);
    if (res > 0.02) model.push(`${m}: тождество D3 нарушено на ${Math.round(res * 100) / 100} ₽`);
    if (st === 'FINANCIAL_COMPLETE' && (cl !== 'CLOSED' || n('provisional_open_rub') !== 0 || n('provisional_units') > 0
      || toStr(p.known_source_defects) !== '')) model.push(`${m}: «${st}» при открытых условиях`);
    if (st === 'FINANCIAL_COMPLETE_WITH_TIMING_BRIDGE' && cl === 'CLOSED') model.push(`${m}: «${st}» у закрытого месяца`);
    const fr = toStr(p.source_freshness_state);
    if (fr !== 'FRESH') source.push(`${m}: источники не свежие (${fr || 'нет статуса'})`);
  }
  for (let i = 1; i < months.length; i++) {
    const [y0, m0] = months[i - 1]!.split('-').map(Number), [y1, m1] = months[i]!.split('-').map(Number);
    if ((y1! - y0!) * 12 + (m1! - m0!) !== 1) model.push(`месяцы не подряд: ${months[i - 1]} → ${months[i]}`);
  }
  if (g.new_operation_rows > 0) model.push(`неразобранных начислений Ozon: ${g.new_operation_rows} (V_OZON_FINANCE_NEW_OPERATIONS)`);
  if (g.max_abs_conservation_residual_rub > 0.005) model.push(`деньги RAW ≠ леджер: ${g.max_abs_conservation_residual_rub} ₽`);
  if (g.unconsumed_rub > 0.005) model.push(`деньги без колонки P&L: ${g.unconsumed_rub} ₽`);
  if (g.unlinked_posting_rub > 0.005) model.push(`деньги без отправления: ${g.unlinked_posting_rub} ₽`);
  return { source: [...new Set(source)].slice(0, 5), model };
}

export type PublishDecision =
  | { action: 'NOOP'; sha: string; setMeta: boolean }
  | { action: 'WRITE'; sha: string; previousSha: string; clearRows: number; adopted: boolean; first: boolean }
  | { action: 'REFUSE'; code: 'OZON_STORE_PNL_TAB_EDITED' | 'OZON_STORE_PNL_TAB_UNTRACKED'; message: string };

/**
 * Первая запись: вкладка пуста (ни значений, ни отпечатка) — разрешена без ADOPT. Вкладка с содержимым без отпечатка —
 * только по ADOPT владельца (так случайная чужая вкладка с тем же названием не будет затёрта).
 */
export function decidePublish(input: { current: readonly (readonly CellValue[])[]; publishedSha?: string; adoptSha?: string; plan: TabPlan }): PublishDecision {
  const current = normalizeGrid(input.current);
  const want = normalizeGrid([input.plan.header, ...input.plan.rows]);
  const cur = contentSha(current);
  const wantSha = contentSha(want);
  if (cur === wantSha) return { action: 'NOOP', sha: wantSha, setMeta: input.publishedSha !== wantSha };
  const clearRows = Math.max(0, current.length - want.length);
  if (current.length === 0 && input.publishedSha === undefined) return { action: 'WRITE', sha: wantSha, previousSha: cur, clearRows: 0, adopted: false, first: true };
  if (input.publishedSha !== undefined && cur === input.publishedSha) return { action: 'WRITE', sha: wantSha, previousSha: cur, clearRows, adopted: false, first: false };
  if (input.adoptSha !== undefined && cur === input.adoptSha) return { action: 'WRITE', sha: wantSha, previousSha: cur, clearRows, adopted: true, first: false };
  if (input.publishedSha === undefined) {
    return { action: 'REFUSE', code: 'OZON_STORE_PNL_TAB_UNTRACKED', message: `во вкладке есть содержимое без отпечатка публикации и не задан OZON_STORE_PNL_ADOPT_SHA (текущее ${cur})` };
  }
  return { action: 'REFUSE', code: 'OZON_STORE_PNL_TAB_EDITED', message: `вкладку правили вручную: содержимое ${cur} ≠ опубликованному ${input.publishedSha}; запись остановлена. Восстановление: владелец проверяет вкладку и задаёт OZON_STORE_PNL_ADOPT_SHA=${cur}` };
}

const rgb = (h: string): { red: number; green: number; blue: number } =>
  ({ red: parseInt(h.slice(0, 2), 16) / 255, green: parseInt(h.slice(2, 4), 16) / 255, blue: parseInt(h.slice(4, 6), 16) / 255 });

/** Фон «Финансового состояния»: окончательное — зелёный; ожидание — жёлтый; недоказанное и неполное — красный. */
export const STATE_BG: Readonly<Record<string, string>> = {
  'Деньги Ozon сверены': 'D9EAD3',
  'Сверены, ждём поздних начислений': 'FFF2CC',
  'Ждём начислений Ozon — предварительно': 'FFF2CC',
  'Не доказан — нужна сверка': 'FCE5CD',
  'Не хватает денег источника': 'F4CCCC',
  'Есть неразобранные операции': 'F4CCCC',
};

const cellOf = (v: CellValue): Record<string, unknown> =>
  typeof v === 'number' ? { userEnteredValue: { numberValue: v } } : v === '' || v === null ? {} : { userEnteredValue: { stringValue: String(v) } };

function metaRequest(sheetId: number, key: string, value: string, metaId?: number): Record<string, unknown> {
  return metaId === undefined
    ? { createDeveloperMetadata: { developerMetadata: { metadataKey: key, metadataValue: value, location: { sheetId }, visibility: 'DOCUMENT' } } }
    : { updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: metaId, metadataKey: key, metadataLocation: { sheetId } } }],
        developerMetadata: { metadataValue: value }, fields: 'metadataValue' } };
}

/** Один атомарный batchUpdate: [расширение сетки] + значения + очистка + форматы + заметки + отпечатки. */
export function publishRequests(plan: TabPlan, sheetId: number, gridRows: number, d: Extract<PublishDecision, { action: 'WRITE' }>, metaId?: number,
  contractMetaId?: number, gridCols: number = TAB_COLS): Record<string, unknown>[] {
  const n = plan.rows.length;
  const grid: CellValue[][] = [[...plan.header], ...plan.rows.map((r) => [...r])];
  if (grid.some((r) => r.length !== TAB_COLS)) throw new Error(`план не ${TAB_COLS} колонок`);
  for (let i = 0; i < d.clearRows; i++) grid.push(Array<CellValue>(TAB_COLS).fill(''));
  const R = (r0: number, r1: number, c0: number, c1: number): Record<string, number> =>
    ({ sheetId, startRowIndex: r0, endRowIndex: r1, startColumnIndex: c0, endColumnIndex: c1 });
  const money = { numberFormat: { type: 'NUMBER', pattern: '#,##0.00' } };
  const requests: Record<string, unknown>[] = [];
  if (grid.length > gridRows) requests.push({ appendDimension: { sheetId, dimension: 'ROWS', length: grid.length - gridRows + 10 } });
  if (gridCols < TAB_COLS) requests.push({ appendDimension: { sheetId, dimension: 'COLUMNS', length: TAB_COLS - gridCols } });
  requests.push(
    { updateCells: { range: R(0, grid.length, 0, TAB_COLS), rows: grid.map((r) => ({ values: r.map(cellOf) })), fields: 'userEnteredValue' } },
    { repeatCell: { range: R(1, grid.length, 0, TAB_COLS), cell: { userEnteredFormat: {} }, fields: 'userEnteredFormat(numberFormat,backgroundColor,textFormat.foregroundColor,textFormat.italic)' } },
    // B..T — деньги; S (оценка X) — курсив оранжевым: предварительное значение не должно выглядеть фактом.
    { repeatCell: { range: R(1, n + 1, COL.FIRST_MONEY, COL.NET + 1), cell: { userEnteredFormat: money }, fields: 'userEnteredFormat.numberFormat' } },
    { repeatCell: { range: R(1, n + 1, COL.X, COL.X + 1), cell: { userEnteredFormat: { ...money, textFormat: { foregroundColor: rgb('B45F06'), italic: true } } }, fields: 'userEnteredFormat(numberFormat,textFormat.foregroundColor,textFormat.italic)' } },
    { repeatCell: { range: R(1, n + 1, COL.MARGIN, COL.MARGIN + 1), cell: { userEnteredFormat: { numberFormat: { type: 'PERCENT', pattern: '0.00%' } } }, fields: 'userEnteredFormat.numberFormat' } },
    { repeatCell: { range: R(1, n + 1, COL.NET_WITHOUT_X, COL.NET_WITHOUT_X + 1), cell: { userEnteredFormat: { ...money, textFormat: { foregroundColor: rgb('666666') } } }, fields: 'userEnteredFormat(numberFormat,textFormat.foregroundColor)' } },
    ...plan.rows.map((row, i) => ({ repeatCell: { range: R(i + 1, i + 2, COL.STATE, COL.STATE + 1), cell: { userEnteredFormat: { backgroundColor: rgb(STATE_BG[String(row[COL.STATE])] ?? 'F4CCCC') } }, fields: 'userEnteredFormat.backgroundColor' } })),
    { updateCells: { range: R(0, 1, 0, TAB_COLS), rows: [{ values: plan.notes.map((note) => ({ note })) }], fields: 'note' } },
    metaRequest(sheetId, PUBLISH_META_KEY, d.sha, metaId),
    metaRequest(sheetId, CONTRACT_META_KEY, contractSha(), contractMetaId),
  );
  assertOnlyTab(sheetId, requests, metaId, contractMetaId);
  return requests;
}

/** NOOP без отпечатка: дописать только metadata значений. */
export function metaOnly(sheetId: number, sha: string, metaId?: number): Record<string, unknown>[] {
  const r = [metaRequest(sheetId, PUBLISH_META_KEY, sha, metaId)];
  assertOnlyTab(sheetId, r, metaId);
  return r;
}

/**
 * Отпечаток ОСТАЛЬНЫХ вкладок книги (F-18): sheetId, название, индекс, размеры сетки — до и после записи обязаны
 * совпасть. Записи вне вкладки исключены assertOnlyTab; этот отпечаток доказывает это на живой книге.
 */
export function otherTabsFingerprint(sheets: ReadonlyArray<{ sheetId: number; title: string; index: number; rowCount: number; columnCount: number }>,
  ownSheetId: number): string {
  const others = sheets.filter((s) => s.sheetId !== ownSheetId).map((s) => [s.sheetId, s.title, s.index, s.rowCount, s.columnCount])
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  return createHash('sha256').update(JSON.stringify(others)).digest('hex');
}
