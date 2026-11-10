/**
 * M5: параметры загрузчика поставок из окружения и проверка контура ДО любой записи и ДО
 * обращения к WB API (по образцу stocks/targets.ts, PR #416).
 *
 * Параметры читаются здесь, а не в общем config.ts: так поток не конфликтует с параллельными
 * переносами (M2–M4), а значения по умолчанию — теневые `__CR`.
 *
 *   shadow → обе таблицы обязаны оканчиваться на `__CR`;
 *   prod   → ровно RAW_WB_SUPPLIES и RAW_WB_SUPPLIES_GOODS;
 *   оба    → датасет wb_raw (чужой датасет — отказ).
 * Нарушение — LoaderError SUPPLIES_TARGET_MISMATCH: ни RAW, ни токен, ни WB не тронуты.
 */
import type { Config } from '../../config.js';
import { LoaderError } from '../../errors.js';
import { SHADOW_SUFFIX, WB_SUPPLIES_HOST, WB_SUPPLIES_PROD_TABLES, WB_SUPPLIES_SECRET } from './constants.js';

export interface SuppliesEnv {
  host: string;
  secret: string;
  headerTable: string;
  goodsTable: string;
}

export function suppliesEnv(env: NodeJS.ProcessEnv): SuppliesEnv {
  const v = (k: string, d: string) => {
    const s = (env[k] ?? '').trim();
    return s === '' ? d : s;
  };
  return {
    host: v('WB_SUPPLIES_HOST', WB_SUPPLIES_HOST),
    secret: v('WB_SUPPLIES_SECRET', WB_SUPPLIES_SECRET),
    headerTable: v('SUPPLIES_HEADER_TABLE', WB_SUPPLIES_PROD_TABLES.header + SHADOW_SUFFIX),
    goodsTable: v('SUPPLIES_GOODS_TABLE', WB_SUPPLIES_PROD_TABLES.goods + SHADOW_SUFFIX),
  };
}

export const SUPPLIES_DATASET = 'wb_raw';

export function assertSuppliesTargets(c: Pick<Config, 'environment' | 'rawDataset'>, e: SuppliesEnv): void {
  const fail = (m: string): never => { throw new LoaderError(m, 'SUPPLIES_TARGET_MISMATCH'); };
  if (c.rawDataset !== SUPPLIES_DATASET) fail(`датасет поставок — только ${SUPPLIES_DATASET}, получено BQ_RAW_DATASET=${c.rawDataset}`);
  const tables: Array<[string, string, string]> = [
    ['SUPPLIES_HEADER_TABLE', e.headerTable, WB_SUPPLIES_PROD_TABLES.header],
    ['SUPPLIES_GOODS_TABLE', e.goodsTable, WB_SUPPLIES_PROD_TABLES.goods],
  ];
  if (c.environment === 'shadow') {
    const bad = tables.filter(([, t, prod]) => t !== prod + SHADOW_SUFFIX);
    if (bad.length) fail(`shadow пишет только в ${SHADOW_SUFFIX}: ${bad.map(([k, t]) => `${k}=${t}`).join(', ')}`);
    return;
  }
  const bad = tables.filter(([, t, prod]) => t !== prod);
  if (bad.length) fail(`prod пишет только в боевые таблицы: ${bad.map(([k, t, p]) => `${k}=${t} (ожидалось ${p})`).join(', ')}`);
}
