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
 */
import { GoogleAuth } from 'google-auth-library';
import { LoaderError } from '../../errors.js';
import type { CellValue } from './model.js';

export interface WriteRange {
  range: string;          // A1 с именем листа
  values: CellValue[][];  // '' очищает ячейку (как setValue('') в Apps Script)
}

export interface SheetsGateway {
  /** Значения нескольких диапазонов (UNFORMATTED_VALUE, даты серийными числами). */
  readValues(ranges: string[]): Promise<CellValue[][][]>;
  /** Формулы одного диапазона (valueRenderOption=FORMULA): строки '=…' либо значения. */
  readFormulas(range: string): Promise<CellValue[][]>;
  /** Один values.batchUpdate; возвращает totalUpdatedCells по ответу API. */
  batchWrite(data: WriteRange[]): Promise<number>;
}

const API = 'https://sheets.googleapis.com/v4/spreadsheets';
export const SCOPE_RO = 'https://www.googleapis.com/auth/spreadsheets.readonly';
export const SCOPE_RW = 'https://www.googleapis.com/auth/spreadsheets';

interface ValueRangeResp { range?: string; values?: CellValue[][] }
interface BatchGetResp { valueRanges?: ValueRangeResp[] }
interface BatchUpdateResp { totalUpdatedCells?: number }

export class SheetsRest implements SheetsGateway {
  private readonly auth: GoogleAuth;

  constructor(
    private readonly spreadsheetId: string,
    readonly: boolean,
  ) {
    this.auth = new GoogleAuth({ scopes: [readonly ? SCOPE_RO : SCOPE_RW] });
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

  async batchWrite(data: WriteRange[]): Promise<number> {
    if (data.length === 0) return 0;
    const url = `${API}/${this.spreadsheetId}/values:batchUpdate`;
    const body = {
      valueInputOption: 'RAW',
      includeValuesInResponse: false,
      data: data.map((d) => ({ range: d.range, majorDimension: 'ROWS', values: d.values })),
    };
    const res = await this.request<BatchUpdateResp>('POST', url, body);
    return Number(res.totalUpdatedCells ?? 0);
  }
}
