/**
 * M5: чтение листа SKU_MASTER. ТОЛЬКО scope spreadsheets.readonly и ТОЛЬКО values.get одного
 * листа: записывать в книгу этот модуль не умеет ни в shadow, ни в prod.
 * Доступ к книге даёт владелец: runtime-SA REF Sync — «Читатель» книги (см. runbook M5).
 */
import { GoogleAuth } from 'google-auth-library';
import { LoaderError } from '../../errors.js';
import type { Cell } from './normalize.js';

export const SHEETS_SCOPE_RO = 'https://www.googleapis.com/auth/spreadsheets.readonly';
const API = 'https://sheets.googleapis.com/v4/spreadsheets';

export interface SheetReader {
  readGrid(spreadsheetId: string, sheet: string): Promise<Cell[][]>;
}

export class SheetsReadonly implements SheetReader {
  private readonly auth = new GoogleAuth({ scopes: [SHEETS_SCOPE_RO] });

  async readGrid(spreadsheetId: string, sheet: string): Promise<Cell[][]> {
    const range = `'${sheet.replace(/'/g, "''")}'`;
    // UNFORMATTED_VALUE — числа и флажки типизированы, как getValues(); даты строкой (см. runbook, отличия).
    const url = `${API}/${spreadsheetId}/values/${encodeURIComponent(range)}?majorDimension=ROWS&valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=FORMATTED_STRING`;
    try {
      const client = await this.auth.getClient();
      const res = await client.request<{ values?: Cell[][] }>({ url, method: 'GET', timeout: 120_000 });
      return res.data.values ?? [];
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      throw new LoaderError(`Sheets API GET ${sheet}: ${msg}`, 'SHEETS_API');
    }
  }
}
