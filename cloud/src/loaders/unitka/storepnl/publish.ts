/**
 * WB STORE P&L AUTO-PUBLISH — публикация плана P&L магазина во вкладку «WB Магазин P&L».
 *
 * F-18 (OWNER ACK 2026-10-09, узкое исключение; источник расширен OWNER ACK 2026-10-11 H0): автоматическая запись
 * разрешена ТОЛЬКО в эту вкладку (sheetId из UNITKA_STORE_PNL_SHEET_ID) и только представлением
 * wb_mart.V_WB_STORE_PNL_PRESENTATION (история 2026-05..07 + Phase C с 2026-08, одна власть на месяц).
 * BigQuery — источник истины; вкладка — представление. Модуль чистый: гейт, решение, запросы. I/O — в index.ts.
 *
 * Защита от затирания ручных правок ЗНАЧЕНИЙ: после публикации отпечаток содержимого A1:Z (sha256)
 * лежит в developer metadata вкладки. Писать можно, только если текущее содержимое равно последнему
 * опубликованному, либо явно принятому владельцем UNITKA_STORE_PNL_ADOPT_SHA (первое подключение или
 * восстановление: владелец проверил вкладку и подтверждает ровно это содержимое). Иначе — отказ
 * STORE_PNL_TAB_EDITED. Содержимое уже равно плану → NOOP.
 *
 * Атомарность: снятие прежней раскладки, значения, раскладка (закрепление, ширины, полосы групп, группы колонок и строк,
 * форматы, условное форматирование), заметки заголовка (строка 7, системный контракт) и отпечатки — ОДИН
 * spreadsheets.batchUpdate: либо легло всё, либо ничего. Каждый запрос ограничен sheetId вкладки (assertOwnTab →
 * assertOnlyTab); название, индекс и видимость вкладки не меняются.
 */
import { createHash } from 'node:crypto';
import type { CellValue } from '../model.js';
import { BANDS, COL, DETAIL_GROUPS, FIRST_DATA_ROW, FROZEN_ROWS, HEADER_ROW, KPI_HEAD_ROW, KPI_ROWS, KPI_STATE_SPAN, LAYOUT_VERSION,
  STATE_LABEL, SUBTITLE_ROW, TAB_COLS, TAB_COLUMNS, TAB_HEADER, TAB_NOTES, TITLE_ROW, toNum, type TabPlan } from './tabplan.js';
import type { PnlRow } from './bq.js';

export const PUBLISH_META_KEY = 'evetis.store_pnl.published_sha';
/** Отпечаток КОНТРАКТА представления (заголовки + заметки + число колонок) — в metadata рядом с отпечатком значений. */
export const CONTRACT_META_KEY = 'evetis.store_pnl.contract_sha';
export { TAB_COLS };

/** sha256 контракта: изменение заголовка, заметки или ширины = новая схема, требует управляемой миграции. */
export function contractSha(): string {
  return createHash('sha256').update(JSON.stringify({ cols: TAB_COLS, header: TAB_HEADER, notes: TAB_NOTES, layout: LAYOUT_VERSION })).digest('hex');
}

/**
 * Миграция схемы (A:Q → A:U и любые будущие) — только управляемо (OWNER ACK 11.10): если отпечаток контракта во вкладке
 * не равен коду, публикация отказывает (STORE_PNL_CONTRACT_MIGRATION_REQUIRED), пока владелец не разрешит ровно этот
 * переход через UNITKA_STORE_PNL_MIGRATE_CONTRACT = contractSha() нового кода. Первый плановый прогон новой схему не мигрирует.
 */
export function decideContract(input: { publishedContract?: string; allowMigrationTo?: string }): { ok: true; migrate: boolean } | { ok: false; message: string } {
  const want = contractSha();
  if (input.publishedContract === want) return { ok: true, migrate: false };
  if (input.allowMigrationTo === want) return { ok: true, migrate: true };
  return { ok: false, message: `контракт вкладки ${input.publishedContract ?? '(нет — схема A:Q)'} ≠ коду ${want}; миграция только с UNITKA_STORE_PNL_MIGRATE_CONTRACT=${want} (OWNER ACK)` };
}

export function contentSha(values: readonly (readonly CellValue[])[]): string {
  return createHash('sha256').update(JSON.stringify(values)).digest('hex');
}

/** Пустые хвосты строк и пустые строки в конце Sheets API не отдаёт — приводим план к той же форме. */
export function normalizeGrid(values: readonly (readonly CellValue[])[]): CellValue[][] {
  const rows = values.map((r) => {
    const x = [...r];
    while (x.length && (x[x.length - 1] === '' || x[x.length - 1] === null || x[x.length - 1] === undefined)) x.pop();
    return x;
  });
  while (rows.length && rows[rows.length - 1]!.length === 0) rows.pop();
  return rows;
}

/**
 * Гейт публикации (ревью #303, P1): вкладка не публикуется, если сломана МОДЕЛЬ — строка не складывается,
 * тождество формы нарушено, состояние неизвестно или «закрыт» при открытых условиях (= QA STATE_CONSISTENCY,
 * FORMULA_IDENTITY, ROW_IDENTITY). Проблемы ДАННЫХ (новая операция, непрочитанные деньги, неклассифицированное)
 * публикуются: месяц честно показывается красным состоянием — это и есть сигнал владельцу.
 */
export function publishGate(pnl: readonly PnlRow[]): string[] {
  const out: string[] = [];
  const b = (v: unknown): boolean => v === true || v === 'true';
  for (const p of pnl) {
    const m = String(p.month ?? '');
    const st = String(p.financial_state ?? '');
    const n = (k: string): number => toNum(p[k]);
    if (!STATE_LABEL[st]) out.push(`${m}: неизвестное состояние ${st}`);
    // Наследие — только август 2026 (Phase C, дефекты листа не чинятся); исторический слой (2026-05..07) — канонический.
    const sm = String(p.service_month && typeof p.service_month === 'object' && 'value' in p.service_month ? (p.service_month as { value: unknown }).value : p.service_month);
    const legacy = sm >= '2026-08-01' && sm < '2026-09-01';
    if (legacy !== (st === 'LEGACY_PARTIAL_KNOWN_DEFECTS')) out.push(`${m}: состояние наследия не совпадает с месяцем`);
    const authority = String(p.authority ?? '');
    if (authority !== (sm < '2026-08-01' ? 'HISTORICAL_CANONICAL' : 'CURRENT_PHASE_C')) out.push(`${m}: власть месяца «${authority}»`);
    if (st.startsWith('FINANCIAL_COMPLETE')) {
      const open = n('pending_rows') > 0 || n('unknown_rub') > 0.5 || n('unconsumed_finance_rub') > 0.5 || n('deduction_source_gap_rub') > 0.5
        || n('orphan_finance_base_rub') !== 0 || n('orphan_finance_logistics_rub') !== 0 || !b(p.sku_month_closed) || !b(p.month_finance_final)
        || n('ads_days') === 0 || n('ads_final_days') !== n('ads_days')
        || n('inventory_loss_cogs_missing_qty') > 0 || n('inventory_loss_double_count_risk_qty') > 0
        || n('loss_compensation_unreconciled_rows') > 0
        || !(b(p.account_invoice_window_closed) || (n('minimum_payment_invoices') > 0 && n('utilization_invoices') > 0));
      if (open) out.push(`${m}: «${st}» при открытых условиях`);
    }
    if (Math.abs(n('double_count_residual_rub')) >= 0.01) out.push(`${m}: тождество формы ${n('double_count_residual_rub')}`);
  }
  // Доказательства утраты товара (закрытый wb_secure, агрегат V_WB_INVENTORY_LOSS_STATUS) обязаны быть загружены ровно одним импортом:
  // пустая таблица дала бы списание 0 без единого флага (FIRE/LOST INVENTORY, OWNER DECISION 11.10).
  if (pnl.length === 0) out.push('представление P&L пусто — публикация стёрла бы вкладку');
  const ev = pnl[0];
  if (ev && (toNum(ev.inventory_loss_evidence_rows) <= 0 || toNum(ev.inventory_loss_evidence_runs) !== 1)) {
    out.push(`доказательства утраты товара не загружены или загружены не одним импортом (строк ${toNum(ev.inventory_loss_evidence_rows)}, импортов ${toNum(ev.inventory_loss_evidence_runs)})`);
  }
  // Документ компенсации, которого нет в финотчёте или он там не один раз, — сверка сломана (не только «месяц не закрыт»).
  if (ev && toNum(ev.loss_compensation_documents_not_once) > 0) {
    out.push(`документы компенсации за утрату не сверяются с финотчётом (не ровно одна строка: ${toNum(ev.loss_compensation_documents_not_once)})`);
  }
  return out;
}

export type PublishDecision =
  | { action: 'NOOP'; sha: string; setMeta: boolean }
  | { action: 'WRITE'; sha: string; previousSha: string; clearRows: number; adopted: boolean }
  | { action: 'REFUSE'; code: 'STORE_PNL_TAB_EDITED' | 'STORE_PNL_TAB_UNTRACKED'; message: string };

export function decidePublish(input: { current: readonly (readonly CellValue[])[]; publishedSha?: string; adoptSha?: string; plan: TabPlan }): PublishDecision {
  const current = normalizeGrid(input.current);
  const want = normalizeGrid(input.plan.grid);
  const cur = contentSha(current);
  const wantSha = contentSha(want);
  if (cur === wantSha) return { action: 'NOOP', sha: wantSha, setMeta: input.publishedSha !== wantSha };
  const clearRows = Math.max(0, current.length - want.length);
  if (input.publishedSha !== undefined && cur === input.publishedSha) return { action: 'WRITE', sha: wantSha, previousSha: cur, clearRows, adopted: false };
  if (input.adoptSha !== undefined && cur === input.adoptSha) return { action: 'WRITE', sha: wantSha, previousSha: cur, clearRows, adopted: true };
  if (input.publishedSha === undefined && input.adoptSha === undefined) {
    return { action: 'REFUSE', code: 'STORE_PNL_TAB_UNTRACKED', message: `у вкладки нет отпечатка публикации и не задан UNITKA_STORE_PNL_ADOPT_SHA (текущее содержимое ${cur})` };
  }
  return { action: 'REFUSE', code: 'STORE_PNL_TAB_EDITED', message: `вкладку правили вручную: содержимое ${cur} ≠ опубликованному ${input.publishedSha ?? '—'}; запись остановлена. Восстановление: владелец проверяет вкладку и задаёт UNITKA_STORE_PNL_ADOPT_SHA=${cur}` };
}

const rgb = (h: string): { red: number; green: number; blue: number } =>
  ({ red: parseInt(h.slice(0, 2), 16) / 255, green: parseInt(h.slice(2, 4), 16) / 255, blue: parseInt(h.slice(4, 6), 16) / 255 });

/**
 * Условное форматирование «Финансового состояния» (и сводки в «Итого»): первое сработавшее правило. Красное — первым,
 * чтобы сводка года с проблемой не выглядела зелёной: неразобранные операции и неизвестные расходы — красный; ожидание
 * счёта — жёлтый; наследие — серый; закрыт — зелёный. Подстроки — из подписей tabplan.ts (TEXT_CONTAINS без учёта регистра).
 */
export const STATE_RULES: ReadonlyArray<{ contains: string; bg: string }> = [
  { contains: 'неразобранные', bg: 'F4CCCC' },
  { contains: 'неизвестные', bg: 'F4CCCC' },
  { contains: 'ждём', bg: 'FFF2CC' },
  { contains: 'наследие', bg: 'EFEFEF' },
  { contains: 'закрыт', bg: 'D9EAD3' },
];
/** updateSheetProperties: ровно эти поля gridProperties — размеры сетки, название, индекс и видимость не меняются. */
export const SHEET_PROPERTY_FIELDS = 'gridProperties(frozenRowCount,frozenColumnCount,rowGroupControlAfter,columnGroupControlAfter)';
const RED = 'C5221F', GREEN = '137333', RED_BG = 'FCE8E6', GREEN_BG = 'E6F4EA';
const MONEY = { type: 'NUMBER', pattern: '#,##0;-#,##0;0' };
const MONEY_KPI = { type: 'NUMBER', pattern: '#,##0" ₽";-#,##0" ₽";0" ₽"' };
const PERCENT = { type: 'PERCENT', pattern: '0.0%' };

const cellOf = (v: CellValue): Record<string, unknown> =>
  typeof v === 'number' ? { userEnteredValue: { numberValue: v } } : v === '' || v === null ? {} : { userEnteredValue: { stringValue: String(v) } };

function metaRequest(sheetId: number, sha: string, metaId?: number): Record<string, unknown> {
  return metaId === undefined
    ? { createDeveloperMetadata: { developerMetadata: { metadataKey: PUBLISH_META_KEY, metadataValue: sha, location: { sheetId }, visibility: 'DOCUMENT' } } }
    : { updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: metaId, metadataKey: PUBLISH_META_KEY, metadataLocation: { sheetId } } }],
        developerMetadata: { metadataValue: sha }, fields: 'metadataValue' } };
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
  if (rows > gridRows) requests.push({ appendDimension: { sheetId, dimension: 'ROWS', length: rows - gridRows + 10 } });
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
  const heights: Array<[number, number]> = [[TITLE_ROW, 34], [SUBTITLE_ROW, 20], [KPI_HEAD_ROW, 34], [KPI_ROWS[0], 30], [KPI_ROWS[1], 30], [HEADER_ROW - 1, 22], [HEADER_ROW, 64]];
  for (const [r, px] of heights) requests.push({ updateDimensionProperties: { range: dim('ROWS', r, r + 1), properties: { pixelSize: px }, fields: 'pixelSize' } });

  // 4. Шапка, показатели, полоса групп, заголовок.
  const all = 'numberFormat,backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy';
  requests.push(
    fmt(TITLE_ROW, TITLE_ROW + 1, 0, 1, { textFormat: { bold: true, fontSize: 15, foregroundColor: rgb('4A148C') }, verticalAlignment: 'MIDDLE', wrapStrategy: 'OVERFLOW_CELL' }, all),
    fmt(SUBTITLE_ROW, SUBTITLE_ROW + 1, 0, 1, { textFormat: { italic: true, fontSize: 9, foregroundColor: rgb('666666') }, wrapStrategy: 'OVERFLOW_CELL' }, all),
    fmt(KPI_HEAD_ROW, KPI_HEAD_ROW + 1, 0, TAB_COLS, { backgroundColor: rgb('F3F3F3'), textFormat: { bold: true, fontSize: 9, foregroundColor: rgb('595959') },
      verticalAlignment: 'MIDDLE', horizontalAlignment: 'CENTER', wrapStrategy: 'WRAP' }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, 0, TAB_COLS, { textFormat: { bold: true, fontSize: 12 }, verticalAlignment: 'MIDDLE', numberFormat: MONEY_KPI }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, 0, 1, { textFormat: { bold: true, fontSize: 10 }, verticalAlignment: 'MIDDLE' }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, COL.MARGIN, COL.MARGIN + 1, { textFormat: { bold: true, fontSize: 12 }, verticalAlignment: 'MIDDLE', numberFormat: PERCENT }, all),
    fmt(KPI_ROWS[0], KPI_ROWS[1] + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1], { textFormat: { fontSize: 9, foregroundColor: rgb('595959') }, verticalAlignment: 'MIDDLE', wrapStrategy: 'WRAP' }, all),
    { mergeCells: { range: R(KPI_HEAD_ROW, KPI_HEAD_ROW + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1]), mergeType: 'MERGE_ALL' } },
    { mergeCells: { range: R(KPI_ROWS[0], KPI_ROWS[0] + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1]), mergeType: 'MERGE_ALL' } },
    { mergeCells: { range: R(KPI_ROWS[1], KPI_ROWS[1] + 1, KPI_STATE_SPAN[0], KPI_STATE_SPAN[1]), mergeType: 'MERGE_ALL' } },
  );
  for (const b of BANDS) {
    requests.push(fmt(HEADER_ROW - 1, HEADER_ROW, b.start, b.end, { backgroundColor: rgb(b.color), textFormat: { bold: true, fontSize: 10, foregroundColor: rgb('1F3864') },
      horizontalAlignment: 'CENTER', verticalAlignment: 'MIDDLE' }, all));
    if (b.end - b.start > 1) requests.push({ mergeCells: { range: R(HEADER_ROW - 1, HEADER_ROW, b.start, b.end), mergeType: 'MERGE_ALL' } });
  }
  requests.push(fmt(HEADER_ROW, HEADER_ROW + 1, 0, TAB_COLS, { backgroundColor: rgb('F3F3F3'), textFormat: { bold: true, fontSize: 9 },
    horizontalAlignment: 'CENTER', verticalAlignment: 'MIDDLE', wrapStrategy: 'WRAP' }, all));

  // 5. Месяцы и итоги: деньги в рублях (значение ячейки — до копеек), «всего» — жирным, подробности и справочные — серым.
  const body0 = FIRST_DATA_ROW, body1 = plan.grid.length;
  if (body1 > body0) {
    requests.push(fmt(body0, body1, 0, TAB_COLS, { verticalAlignment: 'MIDDLE', textFormat: { fontSize: 10 } }, 'verticalAlignment,textFormat.fontSize'));
    TAB_COLUMNS.forEach((c, i) => {
      if (c.kind === 'money' || c.kind === 'sum') {
        const tf = c.kind === 'sum' ? { bold: true } : c.detail || c.ref ? { foregroundColor: rgb('595959') } : {};
        requests.push(fmt(body0, body1, i, i + 1, { numberFormat: MONEY, textFormat: { fontSize: 10, ...tf } }, 'numberFormat,textFormat'));
      }
      if (c.kind === 'margin') requests.push(fmt(body0, body1, i, i + 1, { numberFormat: PERCENT }, 'numberFormat'));
    });
    requests.push(
      fmt(body0, body1, COL.NET, COL.NET + 1, { numberFormat: MONEY, textFormat: { fontSize: 10, bold: true } }, 'numberFormat,textFormat'),
      fmt(body0, body1, COL.STATE, COL.STATE + 1, { wrapStrategy: 'WRAP', textFormat: { fontSize: 9 } }, 'wrapStrategy,textFormat'),
      fmt(body0, body1, COL.REASON, COL.REASON + 1, { wrapStrategy: 'WRAP', textFormat: { fontSize: 9, foregroundColor: rgb('595959') } }, 'wrapStrategy,textFormat'),
    );
    plan.kinds.forEach((k, r) => {
      if (k !== 'total') return;
      requests.push(fmt(r, r + 1, 0, TAB_COLS, { backgroundColor: rgb('E8EAED') }, 'backgroundColor'),
        fmt(r, r + 1, 0, COL.STATE, { textFormat: { bold: true, fontSize: 10 } }, 'textFormat.bold'));
    });
  }

  // 6. Условное форматирование: прибыль (фон), маржа и справочные результаты (цвет текста), состояние месяца — цветом.
  const resultRanges = [R(body0, body1, COL.NET, COL.NET + 1), R(KPI_ROWS[0], KPI_ROWS[1] + 1, COL.NET, COL.NET + 1)];
  const signRanges = [R(body0, body1, COL.MARGIN, COL.NET_COMPARABLE + 1), R(KPI_ROWS[0], KPI_ROWS[1] + 1, COL.MARGIN, COL.NET_COMPARABLE + 1)];
  const rule = (ranges: unknown[], type: string, value: string, format: Record<string, unknown>): Record<string, unknown> =>
    ({ addConditionalFormatRule: { index: 0, rule: { ranges, booleanRule: { condition: { type, values: [{ userEnteredValue: value }] }, format } } } });
  const cf: Record<string, unknown>[] = [
    rule(resultRanges, 'NUMBER_LESS', '0', { backgroundColor: rgb(RED_BG), textFormat: { foregroundColor: rgb(RED) } }),
    rule(resultRanges, 'NUMBER_GREATER', '0', { backgroundColor: rgb(GREEN_BG), textFormat: { foregroundColor: rgb(GREEN) } }),
    rule(signRanges, 'NUMBER_LESS', '0', { textFormat: { foregroundColor: rgb(RED) } }),
    rule(signRanges, 'NUMBER_GREATER', '0', { textFormat: { foregroundColor: rgb(GREEN) } }),
    ...STATE_RULES.map((s) => rule([R(body0, body1, COL.STATE, COL.STATE + 1)], 'TEXT_CONTAINS', s.contains, { backgroundColor: rgb(s.bg) })),
  ];
  // index: 0 у каждого — последнее добавленное станет первым: добавляем в обратном порядке приоритета.
  requests.push(...cf.reverse());

  // 7. Группы: сначала показать всё (удалённая свёрнутая группа могла оставить скрытые строки/колонки), затем подробные
  // колонки — свёрнутые группы, месяцы каждого года — развёрнутая группа строк над «Итого» (состояние задаётся явно).
  requests.push(
    { updateDimensionProperties: { range: dim('ROWS', 0, rows), properties: { hiddenByUser: false }, fields: 'hiddenByUser' } },
    { updateDimensionProperties: { range: dim('COLUMNS', 0, TAB_COLS), properties: { hiddenByUser: false }, fields: 'hiddenByUser' } },
  );
  for (const [a, b] of DETAIL_GROUPS) {
    requests.push({ addDimensionGroup: { range: dim('COLUMNS', a, b) } },
      { updateDimensionGroup: { dimensionGroup: { range: dim('COLUMNS', a, b), depth: 1, collapsed: true }, fields: 'collapsed' } });
  }
  for (const g of plan.yearGroups) {
    requests.push({ addDimensionGroup: { range: dim('ROWS', g.start, g.end) } },
      { updateDimensionGroup: { dimensionGroup: { range: dim('ROWS', g.start, g.end), depth: 1, collapsed: false }, fields: 'collapsed' } });
  }

  // 8. Заметки заголовка (контракт) и отпечатки.
  requests.push(
    { updateCells: { range: R(HEADER_ROW, HEADER_ROW + 1, 0, TAB_COLS), rows: [{ values: plan.notes.map((note) => ({ note })) }], fields: 'note' } },
    metaRequest(sheetId, d.sha, metaId),
    contractMetaRequest(sheetId, contractMetaId),
  );
  assertOwnTab(sheetId, requests, metaId, contractMetaId);
  return requests;
}

/**
 * F-18: каждый запрос — только про эту вкладку. Запросы содержимого проверяет assertOnlyTab (без изменений, его же
 * использует Ozon Store P&L); запросы раскладки — здесь. Свойства вкладки — только поля SHEET_PROPERTY_FIELDS.
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
      ok = rangeSheet(b.properties) === sheetId && b.fields === SHEET_PROPERTY_FIELDS
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

function contractMetaRequest(sheetId: number, contractMetaId?: number): Record<string, unknown> {
  return contractMetaId === undefined
    ? { createDeveloperMetadata: { developerMetadata: { metadataKey: CONTRACT_META_KEY, metadataValue: contractSha(), location: { sheetId }, visibility: 'DOCUMENT' } } }
    : { updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: contractMetaId, metadataKey: CONTRACT_META_KEY, metadataLocation: { sheetId } } }],
        developerMetadata: { metadataValue: contractSha() }, fields: 'metadataValue' } };
}

/** NOOP без отпечатка: дописать только metadata. */
export function metaOnly(sheetId: number, sha: string, metaId?: number): Record<string, unknown>[] {
  const r = [metaRequest(sheetId, sha, metaId)];
  assertOnlyTab(sheetId, r, metaId);
  return r;
}

/** Разрешённые типы запросов, и каждый — только про sheetId вкладки. Всё остальное — отказ. */
export function assertOnlyTab(sheetId: number, requests: readonly Record<string, unknown>[], metaId?: number, contractMetaId?: number): void {
  for (const r of requests) {
    const keys = Object.keys(r);
    if (keys.length !== 1) throw new Error(`запрос вне вкладки: ${keys.join(',')}`);
    const kind = keys[0]!;
    const body = r[kind] as {
      range?: { sheetId?: number }; sheetId?: number; developerMetadata?: { location?: { sheetId?: number } };
      dataFilters?: Array<{ developerMetadataLookup?: { metadataId?: number; metadataLocation?: { sheetId?: number } } }>;
    };
    let ok = false;
    if (kind === 'updateCells' || kind === 'repeatCell') ok = body.range?.sheetId === sheetId;
    else if (kind === 'appendDimension') ok = body.sheetId === sheetId;
    else if (kind === 'createDeveloperMetadata') ok = body.developerMetadata?.location?.sheetId === sheetId;
    else if (kind === 'updateDeveloperMetadata') {
      const f = body.dataFilters ?? [];
      ok = f.length > 0 && f.every((x) => {
        const id = x.developerMetadataLookup?.metadataId;
        return id !== undefined && (id === metaId || id === contractMetaId) && x.developerMetadataLookup?.metadataLocation?.sheetId === sheetId;
      });
    }
    if (!ok) throw new Error(`запрос вне вкладки: ${JSON.stringify(r).slice(0, 200)}`);
  }
}

/**
 * Отпечаток ОСТАЛЬНЫХ вкладок книги (F-18): sheetId, название, индекс, размеры сетки — до и после записи обязаны
 * совпасть (как у «Ozon Магазин P&L»). Записи вне вкладки исключены assertOwnTab; отпечаток доказывает это на живой книге.
 */
export function otherTabsFingerprint(sheets: ReadonlyArray<{ sheetId: number; title: string; index: number; rowCount: number; columnCount: number }>,
  ownSheetId: number): string {
  const others = sheets.filter((s) => s.sheetId !== ownSheetId).map((s) => [s.sheetId, s.title, s.index, s.rowCount, s.columnCount])
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  return createHash('sha256').update(JSON.stringify(others)).digest('hex');
}
