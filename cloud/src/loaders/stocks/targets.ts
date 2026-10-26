/**
 * Mig1b-0: проверка целевых таблиц загрузчика остатков ДО любой записи.
 *
 * Дизайн rev2 (блокер 3): «неверный env физически не может писать в прод». Права SA
 * разделяют контуры, но env prod-Job'а живёт вне Terraform (ignore_changes, им владеет
 * деплой) — и на момент Mig1b он пуст: без STOCKS_* загрузчик взял бы значения по
 * умолчанию, т.е. теневые `__CR`. Поэтому контур проверяется явно и fail-closed:
 *   shadow → только `__CR`-таблицы;
 *   prod   → ровно боевые таблицы Apps Script и обязательно T5 (prod повторяет
 *            wbStocksSnapshotCore_ целиком, без T5 потерялась бы детализация складов).
 * Нарушение — LoaderError до manifestStart: ни RAW, ни manifest не тронуты.
 */
import type { Config } from '../../config.js';
import { LoaderError } from '../../errors.js';

/** Боевые таблицы остатков — те же, что пишет Apps Script (WbStocksBigQuery.gs:36-38). */
export const WB_STOCKS_PROD_TARGETS = {
  raw: 'RAW_WB_STOCKS',
  snapshot: 'WB_STOCKS_SNAPSHOTS',
  t5: 'RAW_WB_STOCKS_T5',
} as const;

const SHADOW_SUFFIX = '__CR';

type TargetConfig = Pick<Config, 'environment' | 'stocksRawTable' | 'stocksSnapshotTable' | 'stocksT5Table' | 'stocksT5Mode'>;

export function assertStocksTargets(c: TargetConfig): void {
  const tables: Array<[string, string]> = [
    ['STOCKS_RAW_TABLE', c.stocksRawTable],
    ['STOCKS_SNAPSHOT_TABLE', c.stocksSnapshotTable],
  ];
  if (c.environment === 'prod' || c.stocksT5Mode === 'on') tables.push(['STOCKS_T5_TABLE', c.stocksT5Table]);

  if (c.environment === 'shadow') {
    const bad = tables.filter(([, t]) => !t.endsWith(SHADOW_SUFFIX));
    if (bad.length) {
      throw new LoaderError(
        `shadow пишет только в ${SHADOW_SUFFIX}: ${bad.map(([k, t]) => `${k}=${t}`).join(', ')}`,
        'STOCKS_TARGET_MISMATCH',
      );
    }
    return;
  }

  const expected: Record<string, string> = {
    STOCKS_RAW_TABLE: WB_STOCKS_PROD_TARGETS.raw,
    STOCKS_SNAPSHOT_TABLE: WB_STOCKS_PROD_TARGETS.snapshot,
    STOCKS_T5_TABLE: WB_STOCKS_PROD_TARGETS.t5,
  };
  const bad = tables.filter(([k, t]) => t !== expected[k]);
  if (bad.length) {
    throw new LoaderError(
      `prod пишет только в боевые таблицы: ${bad.map(([k, t]) => `${k}=${t} (ожидалось ${expected[k]})`).join(', ')}`,
      'STOCKS_TARGET_MISMATCH',
    );
  }
  if (c.stocksT5Mode !== 'on') {
    throw new LoaderError('prod требует STOCKS_T5_MODE=on: без T5 пропала бы детализация складов', 'STOCKS_TARGET_MISMATCH');
  }
}
