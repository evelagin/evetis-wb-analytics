/**
 * WB STORE P&L AUTO-PUBLISH — публикация плана P&L магазина во вкладку «WB Магазин P&L».
 *
 * BigQuery (wb_mart.V_WB_STORE_PNL_MONTHLY) — источник истины; вкладка — только представление.
 * Модуль чистый: решение «писать / ничего / отказ» и запросы записи. I/O — в index.ts.
 *
 * Защита от затирания ручных правок: после каждой публикации отпечаток записанного содержимого
 * (sha256 значений A1:Q) кладётся в developer metadata вкладки. Следующий прогон пишет, только если
 * текущее содержимое вкладки равно последнему опубликованному (или, при первом подключении, отпечатку,
 * утверждённому владельцем, — ADOPT). Иначе отказ STORE_PNL_TAB_EDITED: кто-то правил вкладку руками.
 * Идемпотентность: содержимое уже равно плану → NOOP (ничего не пишется).
 *
 * Все запросы ограничены sheetId вкладки — запись в любой другой лист книги невозможна по построению
 * (assertOnlyTab). Заголовок и заметки строки 1 не переписываются, форматы — только строк данных.
 */
import { createHash } from 'node:crypto';
import type { CellValue } from '../model.js';
import type { TabPlan } from './tabplan.js';

export const PUBLISH_META_KEY = 'evetis.store_pnl.published_sha';
/** Читаемая ширина вкладки: строк с запасом, чтобы увидеть и чужие строки ниже плана. */
export const TAB_READ_ROWS = 200;

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

export type PublishDecision =
  | { action: 'NOOP'; sha: string; setMeta: boolean }
  | { action: 'WRITE'; sha: string; previousSha: string; clearRows: number }
  | { action: 'REFUSE'; code: 'STORE_PNL_TAB_EDITED' | 'STORE_PNL_TAB_UNTRACKED'; message: string };

export function decidePublish(input: { current: readonly (readonly CellValue[])[]; publishedSha?: string; adoptSha?: string; plan: TabPlan }): PublishDecision {
  const current = normalizeGrid(input.current);
  const want = normalizeGrid([input.plan.header, ...input.plan.rows]);
  const cur = contentSha(current);
  const wantSha = contentSha(want);
  if (cur === wantSha) return { action: 'NOOP', sha: wantSha, setMeta: input.publishedSha !== wantSha };
  const baseline = input.publishedSha ?? input.adoptSha;
  if (!baseline) {
    return { action: 'REFUSE', code: 'STORE_PNL_TAB_UNTRACKED', message: `у вкладки нет отпечатка публикации и не задан UNITKA_STORE_PNL_ADOPT_SHA (текущее содержимое ${cur})` };
  }
  if (cur !== baseline) {
    return { action: 'REFUSE', code: 'STORE_PNL_TAB_EDITED', message: `вкладку правили вручную: содержимое ${cur} ≠ опубликованному ${baseline}; запись остановлена, нужна проверка владельца` };
  }
  return { action: 'WRITE', sha: wantSha, previousSha: cur, clearRows: Math.max(0, current.length - want.length) };
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

export interface PublishWrite {
  values: { range: string; values: CellValue[][] }[];
  requests: Record<string, unknown>[];
}

/** Значения A1:Q{n+1} (RAW) + очистка лишних строк + форматы строк данных + отпечаток в metadata. */
export function publishWrite(plan: TabPlan, sheetId: number, d: Extract<PublishDecision, { action: 'WRITE' }>, metaId?: number): PublishWrite {
  const n = plan.rows.length;
  const q = `'${plan.tab}'`;
  const values: PublishWrite['values'] = [{ range: `${q}!A1:Q${n + 1}`, values: [[...plan.header], ...plan.rows.map((r) => [...r])] }];
  if (d.clearRows > 0) {
    values.push({ range: `${q}!A${n + 2}:Q${n + 1 + d.clearRows}`, values: Array.from({ length: d.clearRows }, () => Array<CellValue>(17).fill('')) });
  }
  const R = (r0: number, r1: number, c0: number, c1: number): Record<string, number> =>
    ({ sheetId, startRowIndex: r0, endRowIndex: r1, startColumnIndex: c0, endColumnIndex: c1 });
  const requests: Record<string, unknown>[] = [
    { repeatCell: { range: R(1, n + 1, 1, 14), cell: { userEnteredFormat: { numberFormat: { type: 'NUMBER', pattern: '#,##0.00' } } }, fields: 'userEnteredFormat.numberFormat' } },
    { repeatCell: { range: R(1, n + 1, 14, 15), cell: { userEnteredFormat: { numberFormat: { type: 'PERCENT', pattern: '0.00%' } } }, fields: 'userEnteredFormat.numberFormat' } },
    { repeatCell: { range: R(1, n + 1, 16, 17), cell: { userEnteredFormat: { numberFormat: { type: 'NUMBER', pattern: '#,##0.00' }, textFormat: { foregroundColor: rgb('666666') } } }, fields: 'userEnteredFormat(numberFormat,textFormat.foregroundColor)' } },
    ...plan.rows.map((row, i) => ({ repeatCell: { range: R(i + 1, i + 2, 15, 16), cell: { userEnteredFormat: { backgroundColor: rgb(STATE_BG[String(row[15])] ?? 'F4CCCC') } }, fields: 'userEnteredFormat.backgroundColor' } })),
    metaId === undefined
      ? { createDeveloperMetadata: { developerMetadata: { metadataKey: PUBLISH_META_KEY, metadataValue: d.sha, location: { sheetId }, visibility: 'DOCUMENT' } } }
      : { updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: metaId } }], developerMetadata: { metadataValue: d.sha }, fields: 'metadataValue' } },
  ];
  assertOnlyTab(plan.tab, sheetId, values, requests, metaId);
  return { values, requests };
}

/** Запись metadata без записи значений (NOOP с отсутствующим отпечатком). */
export function metaOnly(sheetId: number, sha: string, metaId?: number): Record<string, unknown>[] {
  return [metaId === undefined
    ? { createDeveloperMetadata: { developerMetadata: { metadataKey: PUBLISH_META_KEY, metadataValue: sha, location: { sheetId }, visibility: 'DOCUMENT' } } }
    : { updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: metaId } }], developerMetadata: { metadataValue: sha }, fields: 'metadataValue' } }];
}

/** Ни один диапазон и ни один запрос не выходит за вкладку P&L. */
export function assertOnlyTab(tab: string, sheetId: number, values: PublishWrite['values'], requests: Record<string, unknown>[], metaId?: number): void {
  for (const v of values) if (!v.range.startsWith(`'${tab}'!`)) throw new Error(`диапазон вне вкладки: ${v.range}`);
  for (const r of requests) {
    const body = Object.values(r)[0] as { range?: { sheetId?: number }; developerMetadata?: { location?: { sheetId?: number } }; dataFilters?: Array<{ developerMetadataLookup?: { metadataId?: number } }> };
    const sid = body.range?.sheetId ?? body.developerMetadata?.location?.sheetId;
    const lookup = body.dataFilters?.[0]?.developerMetadataLookup?.metadataId;
    const ok = sid !== undefined ? sid === sheetId : lookup !== undefined && lookup === metaId;
    if (!ok) throw new Error(`запрос вне вкладки: ${JSON.stringify(r).slice(0, 200)}`);
  }
}
