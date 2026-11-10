/**
 * M2: проверка контура записи ДО секрета, WB API и любой записи в RAW (по образцу
 * stocks/targets.ts из Mig1b-0). Env prod-Job'а Terraform не реконсилирует (ignore_changes),
 * поэтому контур проверяется в коде, fail-closed:
 *   shadow → только `__CR`-таблицы;
 *   prod   → ровно боевые таблицы Apps Script и датасет wb_raw.
 * Нарушение — LoaderError: lease уже взят, но ни RAW, ни WB не тронуты.
 */
import type { Config } from '../../config.js';
import { LoaderError } from '../../errors.js';

/** Боевые таблицы — те же, что пишет Apps Script (WbOrdersBigQuery.gs:34, WbSalesReturnsBigQuery.gs:43). */
export const STATS_PROD_TARGETS = {
  orders: 'RAW_WB_ORDERS',
  sales: 'RAW_WB_SALES_RETURNS',
} as const;
export const STATS_PROD_DATASET = 'wb_raw';
export const STATS_TARGET_MISMATCH = 'STATS_TARGET_MISMATCH';

const SHADOW_SUFFIX = '__CR';

type TargetConfig = Pick<Config, 'environment' | 'rawDataset' | 'ordersRawTable' | 'salesRawTable'>;

/** Проверяет таблицу одного потока; возвращает её имя для записи. */
export function assertStatsTarget(c: TargetConfig, stream: keyof typeof STATS_PROD_TARGETS): string {
  const [envName, table] = stream === 'orders'
    ? ['ORDERS_RAW_TABLE', c.ordersRawTable]
    : ['SALES_RAW_TABLE', c.salesRawTable];
  if (c.environment === 'shadow') {
    if (!table.endsWith(SHADOW_SUFFIX)) {
      throw new LoaderError(`shadow пишет только в ${SHADOW_SUFFIX}: ${envName}=${table}`, STATS_TARGET_MISMATCH);
    }
    return table;
  }
  const expected = STATS_PROD_TARGETS[stream];
  if (table !== expected) {
    throw new LoaderError(`prod пишет только в боевую таблицу: ${envName}=${table} (ожидалось ${expected})`, STATS_TARGET_MISMATCH);
  }
  if (c.rawDataset !== STATS_PROD_DATASET) {
    throw new LoaderError(`prod пишет только в ${STATS_PROD_DATASET}: BQ_RAW_DATASET=${c.rawDataset}`, STATS_TARGET_MISMATCH);
  }
  return table;
}
