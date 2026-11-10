/**
 * M5: параметры REF Sync из окружения и проверка контура ДО чтения листа и ДО любой записи.
 *
 *   shadow → все четыре таблицы — `<боевая>__CR`;
 *   prod   → ровно боевые REF_SKU_MASTER_DATA / REF_ACTIVE_VERSION / REF_SYNC_RUNS / REF_SYNC_TABLE_LOG;
 *   оба    → датасет wb_raw; книга — только «Evetis аналитика 2.0» (id из
 *            docs/apps_script/PROJECT_SEPARATION_2026-09-16.md: контейнер Apps Script-проекта
 *            evetis-wb-analitics, к которому привязан runRefSync через getActiveSpreadsheet()).
 */
import type { Config } from '../../config.js';
import { LoaderError } from '../../errors.js';

/** Книга-источник SKU_MASTER: «Evetis аналитика 2.0». */
export const REF_SOURCE_SPREADSHEET_ID = '1MLOCJEX0UR0Gj9gUcDbTBfPrtAhqQc2ctcsoaepSTiU';
export const REF_SOURCE_SHEET = 'SKU_MASTER';
export const REF_DATASET = 'wb_raw';
export const SHADOW_SUFFIX = '__CR';

export const REF_PROD_TABLES = {
  data: 'REF_SKU_MASTER_DATA',
  active: 'REF_ACTIVE_VERSION',
  runs: 'REF_SYNC_RUNS',
  tlog: 'REF_SYNC_TABLE_LOG',
} as const;
type Key = keyof typeof REF_PROD_TABLES;

export interface RefEnv {
  spreadsheetId: string;
  sheet: string;
  tables: Record<Key, string>;
}

const ENV_NAMES: Record<Key, string> = {
  data: 'REF_DATA_TABLE', active: 'REF_ACTIVE_TABLE', runs: 'REF_RUNS_TABLE', tlog: 'REF_TLOG_TABLE',
};

export function refEnv(env: NodeJS.ProcessEnv): RefEnv {
  const v = (k: string, d: string) => {
    const s = (env[k] ?? '').trim();
    return s === '' ? d : s;
  };
  const tables = {} as Record<Key, string>;
  for (const k of Object.keys(REF_PROD_TABLES) as Key[]) tables[k] = v(ENV_NAMES[k], REF_PROD_TABLES[k] + SHADOW_SUFFIX);
  return { spreadsheetId: v('REF_SPREADSHEET_ID', REF_SOURCE_SPREADSHEET_ID), sheet: v('REF_SHEET_NAME', REF_SOURCE_SHEET), tables };
}

export function assertRefTargets(c: Pick<Config, 'environment' | 'rawDataset'>, e: RefEnv): void {
  const fail = (m: string): never => { throw new LoaderError(m, 'REF_TARGET_MISMATCH'); };
  if (c.rawDataset !== REF_DATASET) fail(`справочник — только датасет ${REF_DATASET}, получено BQ_RAW_DATASET=${c.rawDataset}`);
  if (e.spreadsheetId !== REF_SOURCE_SPREADSHEET_ID || e.sheet !== REF_SOURCE_SHEET) {
    fail(`источник — только книга ${REF_SOURCE_SPREADSHEET_ID}, лист ${REF_SOURCE_SHEET}; получено ${e.spreadsheetId} / ${e.sheet}`);
  }
  const want = (k: Key) => (c.environment === 'shadow' ? REF_PROD_TABLES[k] + SHADOW_SUFFIX : REF_PROD_TABLES[k]);
  const bad = (Object.keys(REF_PROD_TABLES) as Key[]).filter((k) => e.tables[k] !== want(k));
  if (bad.length) {
    fail(`${c.environment} пишет только в ${c.environment === 'shadow' ? SHADOW_SUFFIX : 'боевые таблицы'}: ` +
      bad.map((k) => `${ENV_NAMES[k]}=${e.tables[k]} (ожидалось ${want(k)})`).join(', '));
  }
}
