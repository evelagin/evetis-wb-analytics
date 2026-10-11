/**
 * WB STORE P&L AUTO-PUBLISH — публикация плана P&L магазина во вкладку «WB Магазин P&L».
 *
 * F-18 (OWNER ACK 2026-10-09, узкое исключение; источник расширен OWNER ACK 2026-10-11 H0): автоматическая запись
 * разрешена ТОЛЬКО в эту вкладку (sheetId из UNITKA_STORE_PNL_SHEET_ID) и только представлением
 * wb_mart.V_WB_STORE_PNL_PRESENTATION (история 2026-05..07 + Phase C с 2026-08, одна власть на месяц).
 * BigQuery — источник истины; вкладка — представление. Модуль чистый: гейт, решение, запросы. I/O — в index.ts.
 *
 * Защита от затирания ручных правок ЗНАЧЕНИЙ: после публикации отпечаток содержимого A1:Q (sha256)
 * лежит в developer metadata вкладки. Писать можно, только если текущее содержимое равно последнему
 * опубликованному, либо явно принятому владельцем UNITKA_STORE_PNL_ADOPT_SHA (первое подключение или
 * восстановление: владелец проверил вкладку и подтверждает ровно это содержимое). Иначе — отказ
 * STORE_PNL_TAB_EDITED. Содержимое уже равно плану → NOOP.
 *
 * Атомарность: значения (updateCells), форматы строк данных и отпечаток — ОДИН spreadsheets.batchUpdate:
 * либо легло всё, либо ничего (иначе отпечаток мог бы разойтись со значениями навсегда).
 * Что перезаписывается: значения A1:R{n+1} (заголовок — теми же значениями); форматы строк данных A:R сначала
 * сбрасываются (число, фон, цвет текста — чтобы смена схемы не оставила чужих форматов), затем пишутся формат чисел
 * B:O/R, процент P и фон Q; заметки 18 ячеек заголовка — системный контракт (ручные заметки A1:R1 перезаписываются).
 * Всё вне A:R не трогается. Каждый запрос ограничен sheetId вкладки (assertOnlyTab).
 */
import { createHash } from 'node:crypto';
import type { CellValue } from '../model.js';
import { STATE_LABEL, TAB_COLS, TAB_HEADER, TAB_NOTES, toNum, type TabPlan } from './tabplan.js';
import type { PnlRow } from './bq.js';

export const PUBLISH_META_KEY = 'evetis.store_pnl.published_sha';
/** Отпечаток КОНТРАКТА представления (заголовки + заметки + число колонок) — в metadata рядом с отпечатком значений. */
export const CONTRACT_META_KEY = 'evetis.store_pnl.contract_sha';
export { TAB_COLS };

/** sha256 контракта: изменение заголовка, заметки или ширины = новая схема, требует управляемой миграции. */
export function contractSha(): string {
  return createHash('sha256').update(JSON.stringify({ cols: TAB_COLS, header: TAB_HEADER, notes: TAB_NOTES })).digest('hex');
}

/**
 * Миграция схемы (A:Q → A:R и любые будущие) — только управляемо (OWNER ACK 11.10): если отпечаток контракта во вкладке
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
        || !(b(p.account_invoice_window_closed) || (n('minimum_payment_invoices') > 0 && n('utilization_invoices') > 0));
      if (open) out.push(`${m}: «${st}» при открытых условиях`);
    }
    if (Math.abs(n('double_count_residual_rub')) >= 0.01) out.push(`${m}: тождество формы ${n('double_count_residual_rub')}`);
  }
  return out;
}

export type PublishDecision =
  | { action: 'NOOP'; sha: string; setMeta: boolean }
  | { action: 'WRITE'; sha: string; previousSha: string; clearRows: number; adopted: boolean }
  | { action: 'REFUSE'; code: 'STORE_PNL_TAB_EDITED' | 'STORE_PNL_TAB_UNTRACKED'; message: string };

export function decidePublish(input: { current: readonly (readonly CellValue[])[]; publishedSha?: string; adoptSha?: string; plan: TabPlan }): PublishDecision {
  const current = normalizeGrid(input.current);
  const want = normalizeGrid([input.plan.header, ...input.plan.rows]);
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

/** Фон колонки «Финансовое состояние» (утверждён OWNER ACK 09.10). */
export const STATE_BG: Readonly<Record<string, string>> = {
  'Справочно: наследие, известные дефекты': 'EFEFEF',
  'Ждём счета WB': 'FFF2CC',
  'Есть неразобранные операции': 'F4CCCC',
  'Есть неизвестные расходы': 'F4CCCC',
  'Закрыт': 'D9EAD3',
  'Закрыт, когорта дозревает': 'D9EAD3',
};

const cellOf = (v: CellValue): Record<string, unknown> =>
  typeof v === 'number' ? { userEnteredValue: { numberValue: v } } : v === '' || v === null ? {} : { userEnteredValue: { stringValue: String(v) } };

function metaRequest(sheetId: number, sha: string, metaId?: number): Record<string, unknown> {
  return metaId === undefined
    ? { createDeveloperMetadata: { developerMetadata: { metadataKey: PUBLISH_META_KEY, metadataValue: sha, location: { sheetId }, visibility: 'DOCUMENT' } } }
    : { updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: metaId, metadataKey: PUBLISH_META_KEY, metadataLocation: { sheetId } } }],
        developerMetadata: { metadataValue: sha }, fields: 'metadataValue' } };
}

/** Один атомарный batchUpdate: [расширение сетки] + значения + очистка + форматы строк данных + заметки + отпечатки. */
export function publishRequests(plan: TabPlan, sheetId: number, gridRows: number, d: Extract<PublishDecision, { action: 'WRITE' }>, metaId?: number,
  contractMetaId?: number, gridCols: number = TAB_COLS): Record<string, unknown>[] {
  const n = plan.rows.length;
  const grid: CellValue[][] = [[...plan.header], ...plan.rows.map((r) => [...r])];
  if (grid.some((r) => r.length !== TAB_COLS)) throw new Error(`план не ${TAB_COLS} колонок`);
  for (let i = 0; i < d.clearRows; i++) grid.push(Array<CellValue>(TAB_COLS).fill(''));
  const R = (r0: number, r1: number, c0: number, c1: number): Record<string, number> =>
    ({ sheetId, startRowIndex: r0, endRowIndex: r1, startColumnIndex: c0, endColumnIndex: c1 });
  const requests: Record<string, unknown>[] = [];
  if (grid.length > gridRows) requests.push({ appendDimension: { sheetId, dimension: 'ROWS', length: grid.length - gridRows + 10 } });
  if (gridCols < TAB_COLS) requests.push({ appendDimension: { sheetId, dimension: 'COLUMNS', length: TAB_COLS - gridCols } });
  requests.push(
    { updateCells: { range: R(0, grid.length, 0, TAB_COLS), rows: grid.map((r) => ({ values: r.map(cellOf) })), fields: 'userEnteredValue' } },
    // Сброс форматов строк данных (включая очищаемые): после смены схемы прежние фон/цвет/формат колонок не переезжают.
    { repeatCell: { range: R(1, grid.length, 0, TAB_COLS), cell: { userEnteredFormat: {} }, fields: 'userEnteredFormat(numberFormat,backgroundColor,textFormat.foregroundColor)' } },
    // A..R: 0 месяц · 1–14 деньги (выручка … доходы, чистая прибыль) · 15 маржа · 16 состояние · 17 мост сроков
    { repeatCell: { range: R(1, n + 1, 1, 15), cell: { userEnteredFormat: { numberFormat: { type: 'NUMBER', pattern: '#,##0.00' } } }, fields: 'userEnteredFormat.numberFormat' } },
    { repeatCell: { range: R(1, n + 1, 15, 16), cell: { userEnteredFormat: { numberFormat: { type: 'PERCENT', pattern: '0.00%' } } }, fields: 'userEnteredFormat.numberFormat' } },
    { repeatCell: { range: R(1, n + 1, 17, 18), cell: { userEnteredFormat: { numberFormat: { type: 'NUMBER', pattern: '#,##0.00' }, textFormat: { foregroundColor: rgb('666666') } } }, fields: 'userEnteredFormat(numberFormat,textFormat.foregroundColor)' } },
    ...plan.rows.map((row, i) => ({ repeatCell: { range: R(i + 1, i + 2, 16, 17), cell: { userEnteredFormat: { backgroundColor: rgb(STATE_BG[String(row[16])] ?? 'F4CCCC') } }, fields: 'userEnteredFormat.backgroundColor' } })),
    // Системные заметки заголовка (контракт): пишутся каждой записью, текст — из TAB_COLUMNS.
    { updateCells: { range: R(0, 1, 0, TAB_COLS), rows: [{ values: plan.notes.map((note) => ({ note })) }], fields: 'note' } },
    metaRequest(sheetId, d.sha, metaId),
    contractMetaRequest(sheetId, contractMetaId),
  );
  assertOnlyTab(sheetId, requests, metaId, contractMetaId);
  return requests;
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
