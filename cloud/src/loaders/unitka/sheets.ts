/**
 * UNITKA ENGINE v1 — шлюз Google Sheets. Единственное место в облачном коде, которое
 * говорит с книгой. REST v4 напрямую через google-auth-library (ADC сервисного аккаунта
 * Cloud Run) — без тяжёлого пакета googleapis.
 *
 * SHADOW получает scope `spreadsheets.readonly` и физически не может писать;
 * PROD — `spreadsheets`. Права на книгу выдаёт владелец (Читатель / Редактор).
 *
 * Чтение: UNFORMATTED_VALUE + SERIAL_NUMBER — числа как числа, даты как серийные числа,
 * ошибки формул как строки `#REF!`… Запись: valueInputOption=RAW, одним batchUpdate.
 *
 * Структурная запись (Phase 2B, подготовка месяца): `structureWrite` — ТОЛЬКО из загрузчика
 * `unitka-month-prep` в явном режиме записи. Суточный Engine её не вызывает никогда (тест).
 * На readonly-шлюзе (shadow) метод отказывает ДО любого HTTP-запроса.
 */
import { GoogleAuth } from 'google-auth-library';
import { LoaderError } from '../../errors.js';
import { colA1, type CellValue, type CellFormat } from './model.js';

export interface WriteRange {
  range: string;          // A1 с именем листа
  values: CellValue[][];  // '' очищает ячейку (как setValue('') в Apps Script)
}

/** Статические форматы прямоугольника (userEnteredFormat, БЕЗ условного форматирования). */
export interface FormatGrid {
  sheetId: number;
  /** [row][col] относительно левого верхнего угла запрошенного диапазона; отсутствие = EMPTY_FORMAT. */
  rows: CellFormat[][];
}

/** Одна операция repeatCell: прямоугольник (0-based, конец исключительно) → формат. */
export interface FormatWrite {
  startRow: number;
  endRow: number;
  startCol: number;
  endCol: number;
  format: CellFormat;
}

/** Свойства листа (сетка): для поиска секции и планирования добавления строк/колонок. */
export interface SheetMeta {
  sheetId: number;
  rowCount: number;
  columnCount: number;
  /** Локаль книги (spreadsheets.properties.locale): от неё зависит синтаксис формул в API (Phase 2C). */
  locale?: string;
  /** Колонка якорей книги (1-based) — из именованного диапазона REVERSE_LEG_RATE на этом листе. */
  anchorCol: number;
  /**
   * Именованные диапазоны ЭТОГО листа (Gate 10): левый верхний угол, 1-based. Нужны, чтобы вывести
   * положение зеркала LCD Ozon из листа, а не из переменной окружения: вставка колонок его сдвигает.
   */
  namedRanges?: Readonly<Record<string, { row: number; col: number }>>;
}
/** Группа колонок (expand/collapse) как в REST v4 (индексы 0-based, конец исключительно). */
export interface ColumnGroup { startIndex: number; endIndex: number; depth: number; collapsed?: boolean }

/** Правило условного форматирования как в REST v4 (ranges + booleanRule|gradientRule). */
export interface ConditionalFormatRule {
  ranges: Array<{ sheetId?: number; startRowIndex?: number; endRowIndex?: number; startColumnIndex?: number; endColumnIndex?: number }>;
  booleanRule?: { condition: { type: string; values?: Array<{ userEnteredValue?: string; relativeDate?: string }> }; format?: Record<string, unknown> };
  gradientRule?: Record<string, unknown>;
}
export interface DimensionProps { pixelSize?: number; hiddenByUser?: boolean }
/** userEnteredFormat ячейки как в REST v4 (null — формата нет). */
export type RawCellFormat = Record<string, unknown> | null;
/** Структура листа для подготовки месяца: УФ (весь лист, по порядку), размеры колонок и строк 1..N. */
export interface SheetStructure {
  conditionalFormats: ConditionalFormatRule[];
  columnMetadata: DimensionProps[];  // [0] = колонка A
  rowMetadata: DimensionProps[];     // [0] = строка 1
  merges: Array<{ startRowIndex?: number; endRowIndex?: number; startColumnIndex?: number; endColumnIndex?: number }>;
  columnGroups: ColumnGroup[];
  /** Группы строк — только для манифеста отката и сверки после него (подготовка месяца их не меняет). */
  rowGroups?: ColumnGroup[];
}

/** Запрос spreadsheets.batchUpdate (appendDimension, mergeCells, updateCells, copyPaste…) — как в REST v4. */
export type StructureRequest = Record<string, unknown>;

/**
 * Как Sheets толкует записываемое значение.
 *
 * `RAW` — строка кладётся строкой. Для WB это верно: движок пишет ТОЛЬКО величины, а формулы
 * там расставлены один раз при создании месяца (`monthprep`, updateCells/formulaValue).
 *
 * `USER_ENTERED` — строка, начинающаяся с `=`, становится формулой. Ozon-Юнитка пишет формулы
 * ВМЕСТЕ со значениями, и под RAW они легли в лист ТЕКСТОМ: «цена минус комиссия», «налог» и
 * «доходность 1 шт» показывали пустоту, потому что в ячейке лежала не формула, а её текст.
 * Числа при этом уходят JSON-числами и разбору по локали не подлежат — прочитать «88.09»
 * как текст в ru_RU невозможно.
 */
export type ValueInputOption = 'RAW' | 'USER_ENTERED';

/**
 * Тело values.batchUpdate. Вынесено отдельной функцией ровно потому, что режим записи —
 * не деталь транспорта: под RAW формула становится текстом, и лист молча показывает пустоту.
 */
export function batchWriteBody(data: WriteRange[], inputOption: ValueInputOption): {
  valueInputOption: ValueInputOption; includeValuesInResponse: boolean;
  data: Array<{ range: string; majorDimension: 'ROWS'; values: CellValue[][] }>;
} {
  return {
    valueInputOption: inputOption,
    includeValuesInResponse: false,
    data: data.map((d) => ({ range: d.range, majorDimension: 'ROWS' as const, values: d.values })),
  };
}

export interface SheetsGateway {
  /**
   * Свойства сетки листа по имени (spreadsheets.get, только properties) и локаль книги.
   *
   * `requireAnchor` — якорь тарифов WB (`REVERSE_LEG_RATE`, строка 737). У листа Ozon его нет
   * и быть не должно: тарифы Ozon приходят из BigQuery, а не из ячейки книги. Поэтому для
   * Ozon якорь не требуется, и `anchorCol` возвращается нулём.
   */
  readSheetMeta(sheetName: string, requireAnchor?: boolean): Promise<SheetMeta>;
  /** Структура листа (УФ, объединения, размеры строк/колонок) — только чтение, для подготовки месяца. */
  readSheetStructure(sheetName: string, rowCount: number, columnCount: number): Promise<SheetStructure>;
  /**
   * Статические форматы (userEnteredFormat) целых строк-шаблонов 1..lastColumn — только чтение. Phase 2C:
   * формат новой секции пишется явно (copyPaste PASTE_FORMAT на живом листе копирует и УФ — нельзя).
   */
  readRowFormats(sheetName: string, rows: readonly number[], lastColumn: number): Promise<Map<number, RawCellFormat[]>>;
  /** Значения нескольких диапазонов (UNFORMATTED_VALUE, даты серийными числами). */
  readValues(ranges: string[]): Promise<CellValue[][][]>;
  /** Формулы одного диапазона (valueRenderOption=FORMULA): строки '=…' либо значения. */
  readFormulas(range: string): Promise<CellValue[][]>;
  /** Статические форматы одного диапазона (spreadsheets.get + includeGridData). */
  readFormats(range: string): Promise<FormatGrid>;
  /** Один values.batchUpdate; возвращает totalUpdatedCells по ответу API. */
  batchWrite(data: WriteRange[], inputOption?: ValueInputOption): Promise<number>;
  /**
   * SPP-3: values.batchClear — очищает ТОЛЬКО значения, формат ячейки сохраняется. Запись '' через batchUpdate
   * стирает numberFormat (замер на тестовой книге 28.09), поэтому очистка идёт только этим методом.
   * Необязательный: шлюзы, которым очистка не нужна, его не реализуют.
   */
  batchClear?(ranges: string[]): Promise<number>;
  /**
   * Phase 1A: заметки ячеек (spreadsheets.get, fields=note) — ключ `row|col`, пустые не возвращаются.
   * Необязательный: шлюзы без заметок его не реализуют, и движок тогда заметок не пишет.
   */
  readNotes?(sheetName: string, cells: ReadonlyArray<{ row: number; col: number }>): Promise<Map<string, string>>;
  /** Один spreadsheets.batchUpdate из repeatCell; возвращает число применённых запросов. */
  formatWrite(sheetId: number, writes: FormatWrite[]): Promise<number>;
  /**
   * Структурный spreadsheets.batchUpdate подготовки месяца (атомарно на стороне API).
   * Только загрузчик unitka-month-prep в режиме записи; readonly-шлюз отказывает без HTTP.
   */
  structureWrite(requests: StructureRequest[]): Promise<number>;
}

const API = 'https://sheets.googleapis.com/v4/spreadsheets';
export const SCOPE_RO = 'https://www.googleapis.com/auth/spreadsheets.readonly';
export const SCOPE_RW = 'https://www.googleapis.com/auth/spreadsheets';

interface ValueRangeResp { range?: string; values?: CellValue[][] }
interface BatchGetResp { valueRanges?: ValueRangeResp[] }
interface BatchUpdateResp { totalUpdatedCells?: number }
interface GridDataResp {
  sheets?: Array<{
    properties?: { sheetId?: number };
    data?: Array<{ rowData?: Array<{ values?: Array<{ userEnteredFormat?: {
      backgroundColor?: Record<string, number>;
      textFormat?: { foregroundColor?: Record<string, number> };
      numberFormat?: { type?: string; pattern?: string };
    } }> }> }>;
  }>;
}
interface SpreadsheetBatchUpdateResp { replies?: unknown[] }

const FORMAT_FIELDS = 'userEnteredFormat(backgroundColor,textFormat.foregroundColor,numberFormat)';

export function normalizeFormat(f: { backgroundColor?: Record<string, number>; textFormat?: { foregroundColor?: Record<string, number> }; numberFormat?: { type?: string; pattern?: string } } | undefined): CellFormat {
  if (!f) return { bg: null, fg: null, numberFormat: null };
  const col = (c: Record<string, number> | undefined) => (c ? { red: c.red ?? 0, green: c.green ?? 0, blue: c.blue ?? 0 } : null);
  return {
    bg: col(f.backgroundColor),
    fg: col(f.textFormat?.foregroundColor),
    numberFormat: f.numberFormat ? { type: f.numberFormat.type, pattern: f.numberFormat.pattern } : null,
  };
}

/**
 * Колонка якорей книги: именованный диапазон REVERSE_LEG_RATE — одна ячейка в строке 737 листа Unitka.
 * Sheets сдвигает именованный диапазон при вставке колонок, поэтому Engine всегда читает актуальную колонку.
 * Fail-closed: нет диапазона, не тот лист, не одна ячейка или не строка 737 — ошибка до любой записи.
 */
export function resolveAnchorCol(named: ReadonlyArray<{ name?: string; range?: { sheetId?: number; startRowIndex?: number; endRowIndex?: number; startColumnIndex?: number; endColumnIndex?: number } }>, sheetId: number): number {
  const nr = named.find((n) => n.name === 'REVERSE_LEG_RATE');
  const r = nr?.range;
  if (!r || r.sheetId !== sheetId) throw new LoaderError('именованный диапазон REVERSE_LEG_RATE не найден на листе Unitka', 'ANCHOR_UNRESOLVED');
  const rows = (r.endRowIndex ?? 0) - (r.startRowIndex ?? 0), cols = (r.endColumnIndex ?? 0) - (r.startColumnIndex ?? 0);
  if (rows !== 1 || cols !== 1 || (r.startRowIndex ?? 0) + 1 !== 737) {
    throw new LoaderError(`REVERSE_LEG_RATE должен быть одной ячейкой в строке 737, получено строки ${(r.startRowIndex ?? 0) + 1}..${r.endRowIndex ?? 0} × ${cols} кол.`, 'ANCHOR_UNRESOLVED');
  }
  return (r.startColumnIndex ?? 0) + 1;
}

export class SheetsRest implements SheetsGateway {
  private readonly auth: GoogleAuth;

  constructor(
    private readonly spreadsheetId: string,
    private readonly readonly: boolean,
    /**
     * Внедрённая аутентификация — только для репетиции Gate 10 (сервисный аккаунт репетиции на
     * тестовой книге). В production не передаётся: остаётся ADC сервисного аккаунта Cloud Run.
     */
    auth?: GoogleAuth,
  ) {
    this.auth = auth ?? new GoogleAuth({ scopes: [readonly ? SCOPE_RO : SCOPE_RW] });
  }

  async readSheetMeta(sheetName: string, requireAnchor = true): Promise<SheetMeta> {
    const fields = 'properties(locale),namedRanges,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))';
    const url = `${API}/${this.spreadsheetId}?fields=${encodeURIComponent(fields)}`;
    const data = await this.request<{ properties?: { locale?: string }; namedRanges?: Array<{ name?: string; range?: { sheetId?: number; startRowIndex?: number; endRowIndex?: number; startColumnIndex?: number; endColumnIndex?: number } }>; sheets?: Array<{ properties?: { sheetId?: number; title?: string; gridProperties?: { rowCount?: number; columnCount?: number } } }> }>('GET', url);
    const p = (data.sheets ?? []).map((x) => x.properties).find((x) => x?.title === sheetName);
    if (!p || p.sheetId === undefined || !p.gridProperties?.rowCount || !p.gridProperties.columnCount) {
      throw new LoaderError(`лист «${sheetName}» не найден или без свойств сетки`, 'SHEETS_API');
    }
    const named: Record<string, { row: number; col: number }> = {};
    for (const n of data.namedRanges ?? []) {
      if (!n.name || n.range?.sheetId !== p.sheetId) continue;
      named[n.name] = { row: (n.range.startRowIndex ?? 0) + 1, col: (n.range.startColumnIndex ?? 0) + 1 };
    }
    return { sheetId: p.sheetId, rowCount: p.gridProperties.rowCount, columnCount: p.gridProperties.columnCount, locale: data.properties?.locale,
             anchorCol: requireAnchor ? resolveAnchorCol(data.namedRanges ?? [], p.sheetId) : 0, namedRanges: named };
  }

  async readSheetStructure(sheetName: string, rowCount: number, columnCount: number): Promise<SheetStructure> {
    const q = `'${sheetName.replace(/'/g, "''")}'`;
    const range = `${q}!A1:${colA1(columnCount)}${rowCount}`;
    const fields = 'sheets(properties(title),merges,conditionalFormats,columnGroups,rowGroups,data(rowMetadata(pixelSize,hiddenByUser),columnMetadata(pixelSize,hiddenByUser)))';
    const url = `${API}/${this.spreadsheetId}?ranges=${encodeURIComponent(range)}&fields=${encodeURIComponent(fields)}`;
    type ApiGroup = { range?: { startIndex?: number; endIndex?: number }; depth?: number; collapsed?: boolean };
    const group = (g: ApiGroup): ColumnGroup => ({ startIndex: g.range?.startIndex ?? 0, endIndex: g.range?.endIndex ?? 0, depth: g.depth ?? 1, ...(g.collapsed ? { collapsed: true } : {}) });
    const data = await this.request<{ sheets?: Array<{ merges?: SheetStructure['merges']; conditionalFormats?: ConditionalFormatRule[]; columnGroups?: ApiGroup[]; rowGroups?: ApiGroup[]; data?: Array<{ rowMetadata?: DimensionProps[]; columnMetadata?: DimensionProps[] }> }> }>('GET', url);
    const sh = data.sheets?.[0];
    if (!sh) throw new LoaderError(`структура листа «${sheetName}» не получена`, 'SHEETS_API');
    return {
      conditionalFormats: sh.conditionalFormats ?? [], merges: sh.merges ?? [],
      rowMetadata: sh.data?.[0]?.rowMetadata ?? [], columnMetadata: sh.data?.[0]?.columnMetadata ?? [],
      columnGroups: (sh.columnGroups ?? []).map(group), rowGroups: (sh.rowGroups ?? []).map(group),
    };
  }

  async readRowFormats(sheetName: string, rows: readonly number[], lastColumn: number): Promise<Map<number, RawCellFormat[]>> {
    const q = `'${sheetName.replace(/'/g, "''")}'`;
    const ranges = rows.map((r) => `ranges=${encodeURIComponent(`${q}!A${r}:${colA1(lastColumn)}${r}`)}`).join('&');
    const url = `${API}/${this.spreadsheetId}?${ranges}&fields=${encodeURIComponent('sheets(data(rowData(values(userEnteredFormat))))')}`;
    const data = await this.request<{ sheets?: Array<{ data?: Array<{ rowData?: Array<{ values?: Array<{ userEnteredFormat?: Record<string, unknown> }> }> }> }> }>('GET', url);
    const blocks = data.sheets?.[0]?.data ?? [];
    if (blocks.length !== rows.length) throw new LoaderError(`форматы строк: получено ${blocks.length} диапазонов из ${rows.length}`, 'SHEETS_API');
    const out = new Map<number, RawCellFormat[]>();
    rows.forEach((r, i) => {
      const vals = blocks[i]?.rowData?.[0]?.values ?? [];
      out.set(r, Array.from({ length: lastColumn }, (_, c) => vals[c]?.userEnteredFormat ?? null));
    });
    return out;
  }

  async readNotes(sheetName: string, cells: ReadonlyArray<{ row: number; col: number }>): Promise<Map<string, string>> {
    const out = new Map<string, string>();
    const byCol = new Map<number, { lo: number; hi: number }>();
    for (const c of cells) {
      const r = byCol.get(c.col);
      byCol.set(c.col, r ? { lo: Math.min(r.lo, c.row), hi: Math.max(r.hi, c.row) } : { lo: c.row, hi: c.row });
    }
    if (byCol.size === 0) return out;
    const q = `'${sheetName.replace(/'/g, "''")}'`;
    const ranges = [...byCol].map(([col, r]) => `ranges=${encodeURIComponent(`${q}!${colA1(col)}${r.lo}:${colA1(col)}${r.hi}`)}`).join('&');
    const url = `${API}/${this.spreadsheetId}?${ranges}&fields=${encodeURIComponent('sheets(data(startRow,startColumn,rowData(values(note))))')}`;
    const data = await this.request<{ sheets?: Array<{ data?: Array<{ startRow?: number; startColumn?: number; rowData?: Array<{ values?: Array<{ note?: string }> }> }> }> }>('GET', url);
    const blocks = data.sheets?.[0]?.data ?? [];
    if (blocks.length !== byCol.size) throw new LoaderError(`заметки: получено ${blocks.length} диапазонов из ${byCol.size}`, 'SHEETS_API');
    for (const b of blocks) {
      (b.rowData ?? []).forEach((rd, i) => (rd.values ?? []).forEach((v, j) => {
        if (v.note) out.set(`${(b.startRow ?? 0) + i + 1}|${(b.startColumn ?? 0) + j + 1}`, v.note);
      }));
    }
    return out;
  }

  async structureWrite(requests: StructureRequest[]): Promise<number> {
    if (this.readonly) throw new LoaderError('структурная запись на readonly-шлюзе запрещена', 'STRUCTURE_WRITE_FORBIDDEN');
    if (requests.length === 0) return 0;
    const url = `${API}/${this.spreadsheetId}:batchUpdate`;
    const res = await this.request<SpreadsheetBatchUpdateResp>('POST', url, { requests });
    return (res.replies ?? requests).length;
  }

  private async request<T>(method: 'GET' | 'POST', url: string, body?: unknown): Promise<T> {
    const client = await this.auth.getClient();
    try {
      const res = await client.request<T>({ url, method, data: body, timeout: 120_000 });
      return res.data;
    } catch (e) {
      // Ошибку Sheets API НЕ маскируем и НЕ ретраим здесь: fail-closed, следующий прогон повторит.
      const msg = e instanceof Error ? e.message : String(e);
      throw new LoaderError(`Sheets API ${method} ${url.replace(API, '')}: ${msg}`, 'SHEETS_API');
    }
  }

  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    const q = ranges.map((r) => `ranges=${encodeURIComponent(r)}`).join('&');
    const url = `${API}/${this.spreadsheetId}/values:batchGet?${q}&valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=SERIAL_NUMBER`;
    const data = await this.request<BatchGetResp>('GET', url);
    const out = data.valueRanges ?? [];
    if (out.length !== ranges.length) {
      throw new LoaderError(`Sheets API вернул ${out.length} диапазонов вместо ${ranges.length}`, 'SHEETS_API');
    }
    return out.map((v) => v.values ?? []);
  }

  async readFormulas(range: string): Promise<CellValue[][]> {
    const url = `${API}/${this.spreadsheetId}/values/${encodeURIComponent(range)}?valueRenderOption=FORMULA&dateTimeRenderOption=SERIAL_NUMBER`;
    const data = await this.request<ValueRangeResp>('GET', url);
    return data.values ?? [];
  }

  async readFormats(range: string): Promise<FormatGrid> {
    const url = `${API}/${this.spreadsheetId}?ranges=${encodeURIComponent(range)}&includeGridData=true&fields=${encodeURIComponent(`sheets(properties(sheetId),data(rowData(values(${FORMAT_FIELDS}))))`)}`;
    const data = await this.request<GridDataResp>('GET', url);
    const sheet = data.sheets?.[0];
    const sheetId = sheet?.properties?.sheetId;
    if (sheet === undefined || sheetId === undefined) throw new LoaderError('Sheets API не вернул лист для диапазона форматов', 'SHEETS_API');
    const rows = (sheet.data?.[0]?.rowData ?? []).map((r) => (r.values ?? []).map((v) => normalizeFormat(v.userEnteredFormat)));
    return { sheetId, rows };
  }

  async formatWrite(sheetId: number, writes: FormatWrite[]): Promise<number> {
    if (writes.length === 0) return 0;
    const url = `${API}/${this.spreadsheetId}:batchUpdate`;
    const requests = writes.map((w) => ({
      repeatCell: {
        range: { sheetId, startRowIndex: w.startRow, endRowIndex: w.endRow, startColumnIndex: w.startCol, endColumnIndex: w.endCol },
        cell: { userEnteredFormat: {
          ...(w.format.bg ? { backgroundColor: w.format.bg } : {}),
          ...(w.format.fg ? { textFormat: { foregroundColor: w.format.fg } } : {}),
          ...(w.format.numberFormat ? { numberFormat: w.format.numberFormat } : {}),
        } },
        fields: FORMAT_FIELDS,
      },
    }));
    const res = await this.request<SpreadsheetBatchUpdateResp>('POST', url, { requests });
    return (res.replies ?? requests).length;
  }

  async batchClear(ranges: string[]): Promise<number> {
    if (ranges.length === 0) return 0;
    if (this.readonly) throw new LoaderError('очистка на readonly-шлюзе запрещена', 'SHEETS_API');
    const url = `${API}/${this.spreadsheetId}/values:batchClear`;
    const res = await this.request<{ clearedRanges?: string[] }>('POST', url, { ranges });
    return (res.clearedRanges ?? []).length;
  }

  async batchWrite(data: WriteRange[], inputOption: ValueInputOption = 'RAW'): Promise<number> {
    if (data.length === 0) return 0;
    const url = `${API}/${this.spreadsheetId}/values:batchUpdate`;
    const body = batchWriteBody(data, inputOption);
    const res = await this.request<BatchUpdateResp>('POST', url, body);
    return Number(res.totalUpdatedCells ?? 0);
  }
}
