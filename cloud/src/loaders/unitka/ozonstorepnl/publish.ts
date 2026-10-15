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
 * Защита ручных правок: отпечаток содержимого A:AE (sha256) — в developer metadata вкладки; писать можно, только
 * если текущее содержимое равно последнему опубликованному или явно принятому владельцем OZON_STORE_PNL_ADOPT_SHA.
 * Содержимое уже равно плану → NOOP. Значения, форматы, заметки и отпечатки — один атомарный batchUpdate.
 */
import { createHash } from 'node:crypto';
import type { CellValue } from '../model.js';
import { assertOnlyTab, contentSha, normalizeGrid } from '../storepnl/publish.js';
import { BAND_ROW, BANDS, COL, CLOSURE_LABEL, DETAIL_GROUPS, FIRST_DATA_ROW, FROZEN_ROWS, HEADER_ROW, KPI_HEAD_ROW, KPI_ROWS, KPI_STATE_SPAN, LAYOUT_VERSION,
  STATE_LABEL, SUBTITLE_ROW, TAB_COLS, TAB_COLUMNS, TAB_HEADER, TAB_NOTES, TITLE_ROW, identityResidual, toNum, toStr, type PnlRow, type TabPlan } from './tabplan.js';
import type { GateInputs, QaResult } from './bq.js';

export { assertOnlyTab, contentSha, normalizeGrid, TAB_COLS };
export const PUBLISH_META_KEY = 'evetis.ozon_store_pnl.published_sha';
export const CONTRACT_META_KEY = 'evetis.ozon_store_pnl.contract_sha';

/** sha256 контракта: изменение заголовка, заметки или раскладки (LAYOUT_VERSION) = новая схема, только управляемой миграцией. */
export function contractSha(): string {
  return createHash('sha256').update(JSON.stringify({ cols: TAB_COLS, header: TAB_HEADER, notes: TAB_NOTES, layout: LAYOUT_VERSION })).digest('hex');
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
export function publishGate(pnl: readonly PnlRow[], g: GateInputs, qa?: readonly QaResult[]): GateResult {
  const source: string[] = [];
  const model: string[] = [];
  // Канонический QA D3 (sql/ozon/qa_ozon_store_pnl_v1.sql): любой FAIL, ошибка запроса или незнакомый статус — блок.
  // WARN (Q16 мост к L4, Q20 жизненный цикл) — раскрыт и публикацию не останавливает. Без результатов QA — блок.
  if (qa === undefined || qa.length === 0) model.push('QA D3 не выполнен');
  for (const r of qa ?? []) if (r.status !== 'PASS' && r.status !== 'WARN') model.push(`QA ${r.id}: ${r.status} — ${r.detail.slice(0, 200)}`);
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
  const want = normalizeGrid(input.plan.grid);
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

/**
 * Условное форматирование «Состояния месяца» (и сводки в «Итого»): первое сработавшее правило. Красное — первым, чтобы
 * сводка года с дефектом не выглядела жёлтой или зелёной: известные дефекты и неполные данные — красный; недоказанное —
 * оранжевый; ожидание — жёлтый; сверено — зелёный. Подстроки — из подписей tabplan.ts (TEXT_CONTAINS без учёта регистра).
 */
export const STATE_RULES: ReadonlyArray<{ contains: string; bg: string }> = [
  { contains: 'D1', bg: 'F4CCCC' },
  { contains: 'M2', bg: 'F4CCCC' },
  { contains: 'Неполные', bg: 'F4CCCC' },
  { contains: 'неразобранные', bg: 'F4CCCC' },
  { contains: 'Не доказан', bg: 'FCE5CD' },
  { contains: 'ждём', bg: 'FFF2CC' },
  { contains: 'сверен', bg: 'D9EAD3' },
];
/** updateSheetProperties: ровно эти поля gridProperties — размеры сетки, название, индекс и видимость не меняются. */
export const SHEET_PROPERTY_FIELDS = 'gridProperties(frozenRowCount,frozenColumnCount,rowGroupControlAfter,columnGroupControlAfter)';
const RED = 'C5221F', GREEN = '137333', RED_BG = 'FCE8E6', GREEN_BG = 'E6F4EA';
const MONEY = { type: 'NUMBER', pattern: '#,##0;-#,##0;0' };
const MONEY_KPI = { type: 'NUMBER', pattern: '#,##0" ₽";-#,##0" ₽";0" ₽"' };
const PERCENT = { type: 'PERCENT', pattern: '0.0%' };

const cellOf = (v: CellValue): Record<string, unknown> =>
  typeof v === 'number' ? { userEnteredValue: { numberValue: v } } : v === '' || v === null ? {} : { userEnteredValue: { stringValue: String(v) } };

function metaRequest(sheetId: number, key: string, value: string, metaId?: number): Record<string, unknown> {
  return metaId === undefined
    ? { createDeveloperMetadata: { developerMetadata: { metadataKey: key, metadataValue: value, location: { sheetId }, visibility: 'DOCUMENT' } } }
    : { updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: metaId, metadataKey: key, metadataLocation: { sheetId } } }],
        developerMetadata: { metadataValue: value }, fields: 'metadataValue' } };
}

/** Текущая структура вкладки, которую запись снимает перед раскладкой заново (идемпотентность). */
export interface TabStructure {
  conditionalFormats: number;
  merges: boolean;
  columnGroups: ReadonlyArray<{ startIndex: number; endIndex: number; depth: number }>;
  rowGroups: ReadonlyArray<{ startIndex: number; endIndex: number; depth: number }>;
}
export const EMPTY_STRUCTURE: TabStructure = { conditionalFormats: 0, merges: false, columnGroups: [], rowGroups: [] };

/**
 * Один атомарный batchUpdate: [расширение сетки] + снятие прежней раскладки (УФ, объединения, группы, форматы, заметки)
 * + значения + раскладка (закрепление, ширины, высоты, полосы, группы, форматы, УФ) + заметки заголовка + отпечатки.
 * Каждый запрос ограничен sheetId вкладки (assertOwnTab). Название, индекс и видимость вкладки не меняются.
 */
export function publishRequests(plan: TabPlan, sheetId: number, gridRows: number, d: Extract<PublishDecision, { action: 'WRITE' }>, metaId?: number,
  contractMetaId?: number, gridCols: number = TAB_COLS, current: TabStructure = EMPTY_STRUCTURE): Record<string, unknown>[] {
  const grid: CellValue[][] = plan.grid.map((r) => [...r]);
  if (grid.some((r) => r.length !== TAB_COLS)) throw new Error(`план не ${TAB_COLS} колонок`);
  if (plan.kinds.length !== grid.length) throw new Error('типы строк не совпадают с сеткой');
  for (let i = 0; i < d.clearRows; i++) grid.push(Array<CellValue>(TAB_COLS).fill(''));
  const rows = grid.length;
  const cols = Math.max(TAB_COLS, gridCols);
  const R = (r0: number, r1: number, c0: number, c1: number): Record<string, number> =>
    ({ sheetId, startRowIndex: r0, endRowIndex: r1, startColumnIndex: c0, endColumnIndex: c1 });
  const fmt = (r0: number, r1: number, c0: number, c1: number, f: Record<string, unknown>, fields: string): Record<string, unknown> =>
    ({ repeatCell: { range: R(r0, r1, c0, c1), cell: { userEnteredFormat: f }, fields: `userEnteredFormat(${fields})` } });
  const dim = (dimension: 'ROWS' | 'COLUMNS', start: number, end: number): Record<string, unknown> => ({ sheetId, dimension, startIndex: start, endIndex: end });
  const requests: Record<string, unknown>[] = [];

  // 1. Сетка и снятие прежней раскладки.
  if (rows + 10 > gridRows) requests.push({ appendDimension: { sheetId, dimension: 'ROWS', length: rows + 10 - gridRows } });
  if (gridCols < TAB_COLS) requests.push({ appendDimension: { sheetId, dimension: 'COLUMNS', length: TAB_COLS - gridCols } });
  for (let i = 0; i < current.conditionalFormats; i++) requests.push({ deleteConditionalFormatRule: { sheetId, index: 0 } });
  for (const g of [...current.columnGroups].sort((a, b) => b.depth - a.depth)) requests.push({ deleteDimensionGroup: { range: dim('COLUMNS', g.startIndex, g.endIndex) } });
  for (const g of [...current.rowGroups].sort((a, b) => b.depth - a.depth)) requests.push({ deleteDimensionGroup: { range: dim('ROWS', g.startIndex, g.endIndex) } });
  if (current.merges) requests.push({ unmergeCells: { range: R(0, Math.max(rows, gridRows), 0, cols) } });
  requests.push(
    { repeatCell: { range: R(0, rows, 0, cols), cell: {}, fields: 'userEnteredFormat' } },
    { updateCells: { range: R(0, rows, 0, cols), rows: Array.from({ length: rows }, () => ({ values: Array.from({ length: cols }, () => ({})) })), fields: 'note' } },
  );

  // 2. Значения.
  requests.push({ updateCells: { range: R(0, rows, 0, TAB_COLS), rows: grid.map((r) => ({ values: r.map(cellOf) })), fields: 'userEnteredValue' } });

  // 3. Закрепление, ширины, высоты.
  requests.push({ updateSheetProperties: { properties: { sheetId, gridProperties: { frozenRowCount: FROZEN_ROWS, frozenColumnCount: 1,
    rowGroupControlAfter: true, columnGroupControlAfter: false } }, fields: SHEET_PROPERTY_FIELDS } });
  TAB_COLUMNS.forEach((c, i) => requests.push({ updateDimensionProperties: { range: dim('COLUMNS', i, i + 1), properties: { pixelSize: c.width }, fields: 'pixelSize' } }));
  const heights: Array<[number, number]> = [[TITLE_ROW, 34], [SUBTITLE_ROW, 20], [KPI_HEAD_ROW, 34], [KPI_ROWS[0], 30], [KPI_ROWS[1], 30], [BAND_ROW, 22], [HEADER_ROW, 64]];
  for (const [r, px] of heights) requests.push({ updateDimensionProperties: { range: dim('ROWS', r, r + 1), properties: { pixelSize: px }, fields: 'pixelSize' } });

  // 4. Шапка, показатели, полоса групп, заголовок.
  const all = 'numberFormat,backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy';
  requests.push(
    fmt(TITLE_ROW, TITLE_ROW + 1, 0, 1, { textFormat: { bold: true, fontSize: 15, foregroundColor: rgb('1F3864') }, verticalAlignment: 'MIDDLE', wrapStrategy: 'OVERFLOW_CELL' }, all),
    fmt(SUBTITLE_ROW, SUBTITLE_ROW + 1, 0, 1, { textFormat: { italic: true, fontSize: 9, foregroundColor: rgb('666666') }, wrapStrategy: 'OVERFLOW_CELL' }, all),
    fmt(KPI_HEAD_ROW, KPI_HEAD_ROW + 1, 0, TAB_COLS, { backgroundColor: rgb('F3F3F3'), textFormat: { bold: true, fontSize: 9, foregroundColor: rgb('595959') },
      verticalAlignment: 'MIDDLE', horizontalAlignment: 'CENTER', wrapStrategy: 'WRAP' }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, 0, TAB_COLS, { textFormat: { bold: true, fontSize: 12 }, verticalAlignment: 'MIDDLE', numberFormat: MONEY_KPI }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, 0, 1, { textFormat: { bold: true, fontSize: 10 }, verticalAlignment: 'MIDDLE' }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, COL.ADS_SUM, COL.ADS_SUM + 1, { textFormat: { bold: true, fontSize: 12 }, verticalAlignment: 'MIDDLE', numberFormat: PERCENT }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, COL.CABINET_SUM, COL.CABINET_SUM + 1, { textFormat: { bold: true, fontSize: 12, italic: true, foregroundColor: rgb('B45F06') },
      verticalAlignment: 'MIDDLE', numberFormat: MONEY_KPI }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1], { textFormat: { fontSize: 9, foregroundColor: rgb('595959') }, verticalAlignment: 'MIDDLE', wrapStrategy: 'WRAP' }, all),
    { mergeCells: { range: R(KPI_HEAD_ROW, KPI_HEAD_ROW + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1]), mergeType: 'MERGE_ALL' } },
    { mergeCells: { range: R(KPI_ROWS[0], KPI_ROWS[0] + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1]), mergeType: 'MERGE_ALL' } },
    { mergeCells: { range: R(KPI_ROWS[1], KPI_ROWS[1] + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1]), mergeType: 'MERGE_ALL' } },
  );
  for (const b of BANDS) {
    requests.push(fmt(BAND_ROW, BAND_ROW + 1, b.start, b.end, { backgroundColor: rgb(b.color), textFormat: { bold: true, fontSize: 10, foregroundColor: rgb('1F3864') },
      horizontalAlignment: 'CENTER', verticalAlignment: 'MIDDLE' }, all));
    if (b.end - b.start > 1) requests.push({ mergeCells: { range: R(BAND_ROW, BAND_ROW + 1, b.start, b.end), mergeType: 'MERGE_ALL' } });
  }
  requests.push(fmt(HEADER_ROW, HEADER_ROW + 1, 0, TAB_COLS, { backgroundColor: rgb('F3F3F3'), textFormat: { bold: true, fontSize: 9 },
    horizontalAlignment: 'CENTER', verticalAlignment: 'MIDDLE', wrapStrategy: 'WRAP' }, all));

  // 5. Строки месяцев и итогов: деньги в рублях (значение ячейки — до копеек), «всего» — жирным, подробности — серым.
  const body0 = FIRST_DATA_ROW, body1 = plan.grid.length;
  if (body1 > body0) {
    requests.push(fmt(body0, body1, 0, TAB_COLS, { verticalAlignment: 'MIDDLE', textFormat: { fontSize: 10 } }, 'verticalAlignment,textFormat.fontSize'));
    TAB_COLUMNS.forEach((c, i) => {
      if (c.kind === 'money' || c.kind === 'sum') {
        const tf = c.kind === 'sum' ? { bold: true } : c.detail ? { foregroundColor: rgb('595959') } : {};
        requests.push(fmt(body0, body1, i, i + 1, { numberFormat: MONEY, textFormat: { fontSize: 10, ...tf } }, 'numberFormat,textFormat'));
      }
      if (c.kind === 'margin') requests.push(fmt(body0, body1, i, i + 1, { numberFormat: PERCENT }, 'numberFormat'));
    });
    requests.push(
      fmt(body0, body1, COL.X, COL.X + 1, { numberFormat: MONEY, textFormat: { fontSize: 10, italic: true, foregroundColor: rgb('B45F06') } }, 'numberFormat,textFormat'),
      fmt(body0, body1, COL.NET, COL.NET + 1, { numberFormat: MONEY, textFormat: { fontSize: 10, bold: true } }, 'numberFormat,textFormat'),
      fmt(body0, body1, COL.STATE, COL.STATE + 1, { wrapStrategy: 'WRAP', textFormat: { fontSize: 9 } }, 'wrapStrategy,textFormat'),
      fmt(body0, body1, COL.CLOSURE, COL.FRESHNESS + 1, { textFormat: { fontSize: 9, foregroundColor: rgb('595959') } }, 'textFormat'),
      fmt(body0, body1, COL.REASON, COL.REASON + 1, { wrapStrategy: 'WRAP', textFormat: { fontSize: 9, foregroundColor: rgb('595959') } }, 'wrapStrategy,textFormat'),
    );
    plan.kinds.forEach((k, r) => {
      if (k !== 'total') return;
      requests.push(fmt(r, r + 1, 0, TAB_COLS, { backgroundColor: rgb('E8EAED') }, 'backgroundColor'),
        fmt(r, r + 1, 0, COL.STATE, { textFormat: { bold: true, fontSize: 10 } }, 'textFormat.bold'));
    });
  }

  // 6. Условное форматирование: результат и маржа (плюс зелёный, минус красный), состояние месяца — цветом.
  const resultRanges = [R(body0, body1, COL.NET, COL.NET + 1), R(KPI_ROWS[0], KPI_ROWS[1] + 1, COL.LOGISTICS_SUM, COL.LOGISTICS_SUM + 1)];
  const signRanges = [R(body0, body1, COL.MARGIN, COL.NET_WITHOUT_X + 1), R(KPI_ROWS[0], KPI_ROWS[1] + 1, COL.ADS_SUM, COL.ADS_SUM + 1)];
  const rule = (ranges: unknown[], type: string, value: string, format: Record<string, unknown>): Record<string, unknown> =>
    ({ addConditionalFormatRule: { index: 0, rule: { ranges, booleanRule: { condition: { type, values: [{ userEnteredValue: value }] }, format } } } });
  const cf: Record<string, unknown>[] = [
    rule(resultRanges, 'NUMBER_LESS', '0', { backgroundColor: rgb(RED_BG), textFormat: { foregroundColor: rgb(RED) } }),
    rule(resultRanges, 'NUMBER_GREATER', '0', { backgroundColor: rgb(GREEN_BG), textFormat: { foregroundColor: rgb(GREEN) } }),
    rule(signRanges, 'NUMBER_LESS', '0', { textFormat: { foregroundColor: rgb(RED) } }),
    rule(signRanges, 'NUMBER_GREATER', '0', { textFormat: { foregroundColor: rgb(GREEN) } }),
    ...STATE_RULES.map((s) => rule([R(body0, body1, COL.STATE, COL.STATE + 1)], 'TEXT_CONTAINS', s.contains, { backgroundColor: rgb(s.bg) })),
  ];
  // index: 0 у каждого — значит последнее добавленное окажется первым: добавляем в обратном порядке приоритета.
  requests.push(...cf.reverse());

  // 7. Группы: подробные колонки свёрнуты; месяцы каждого года — группа строк над строкой «Итого». Удалённая свёрнутая
  // группа могла оставить строки или колонки скрытыми — сначала показываем всё, затем сворачиваем только подробности.
  requests.push(
    { updateDimensionProperties: { range: dim('ROWS', 0, rows), properties: { hiddenByUser: false }, fields: 'hiddenByUser' } },
    { updateDimensionProperties: { range: dim('COLUMNS', 0, TAB_COLS), properties: { hiddenByUser: false }, fields: 'hiddenByUser' } },
  );
  for (const [a, b] of DETAIL_GROUPS) {
    requests.push({ addDimensionGroup: { range: dim('COLUMNS', a, b) } },
      { updateDimensionGroup: { dimensionGroup: { range: dim('COLUMNS', a, b), depth: 1, collapsed: true }, fields: 'collapsed' } });
  }
  for (const g of plan.yearGroups) requests.push({ addDimensionGroup: { range: dim('ROWS', g.start, g.end) } });

  // 8. Заметки заголовка (контракт) и отпечатки.
  requests.push(
    { updateCells: { range: R(HEADER_ROW, HEADER_ROW + 1, 0, TAB_COLS), rows: [{ values: plan.notes.map((note) => ({ note })) }], fields: 'note' } },
    metaRequest(sheetId, PUBLISH_META_KEY, d.sha, metaId),
    metaRequest(sheetId, CONTRACT_META_KEY, contractSha(), contractMetaId),
  );
  assertOwnTab(sheetId, requests, metaId, contractMetaId);
  return requests;
}

/**
 * F-18: каждый запрос — только про эту вкладку. Запросы содержимого проверяет общий assertOnlyTab (без изменений);
 * запросы раскладки — здесь. Свойства вкладки меняются только в gridProperties (закрепление, кнопки групп): название,
 * индекс и видимость — нет (иначе сдвинулись бы индексы остальных вкладок).
 */
export function assertOwnTab(sheetId: number, requests: readonly Record<string, unknown>[], metaId?: number, contractMetaId?: number): void {
  const shared: Record<string, unknown>[] = [];
  for (const r of requests) {
    const keys = Object.keys(r);
    if (keys.length !== 1) throw new Error(`запрос вне вкладки: ${keys.join(',')}`);
    const kind = keys[0]!;
    const b = r[kind] as Record<string, unknown>;
    const rangeSheet = (x: unknown): number | undefined => (x as { sheetId?: number } | undefined)?.sheetId;
    let ok: boolean | undefined;
    if (kind === 'updateSheetProperties') {
      const fields = String(b.fields ?? '');
      ok = rangeSheet(b.properties) === sheetId && fields === SHEET_PROPERTY_FIELDS
        && Object.keys((b.properties ?? {}) as object).every((k) => k === 'sheetId' || k === 'gridProperties');
    } else if (kind === 'updateDimensionProperties' || kind === 'mergeCells' || kind === 'unmergeCells' || kind === 'addDimensionGroup' || kind === 'deleteDimensionGroup') {
      ok = rangeSheet(b.range) === sheetId;
    } else if (kind === 'updateDimensionGroup') {
      ok = rangeSheet((b.dimensionGroup as { range?: unknown } | undefined)?.range) === sheetId;
    } else if (kind === 'deleteConditionalFormatRule') {
      ok = b.sheetId === sheetId;
    } else if (kind === 'addConditionalFormatRule') {
      const ranges = ((b.rule as { ranges?: unknown[] } | undefined)?.ranges ?? []);
      ok = ranges.length > 0 && ranges.every((x) => rangeSheet(x) === sheetId);
    }
    if (ok === undefined) shared.push(r);
    else if (!ok) throw new Error(`запрос вне вкладки: ${JSON.stringify(r).slice(0, 200)}`);
  }
  assertOnlyTab(sheetId, shared, metaId, contractMetaId);
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
