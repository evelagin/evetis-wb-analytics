/**
 * M4 · Проверка контура ДО любой записи и ДО обращения к WB API (по образцу stocks/targets.ts, PR #416).
 *
 * «Неверный env физически не может писать в прод»: env prod-Job'а живёт вне Terraform
 * (ignore_changes, им владеет деплой), а значения по умолчанию в config.ts — теневые `__CR`.
 * Поэтому контур проверяется явно и fail-closed:
 *   shadow → только `__CR`-таблицы, и у каждой базовое имя = своя боевая таблица (не чужая);
 *   prod   → ровно боевые таблицы Apps Script и cutover строго 2026-07-13 (от него зависят
 *            календарь недель и V_WB_FINANCE_CANONICAL).
 * Нарушение — LoaderError: ни журнал, ни манифест, ни RAW не тронуты, токен не читался.
 */
import type { Config } from '../../config.js';
import { LoaderError } from '../../errors.js';
import { FIN_PROD_CUTOVER } from './constants.js';
import { isCalendarDate, weekStart } from './parse.js';
import type { FinanceTables } from './store.js';

/** Боевые таблицы — те же, что пишет Apps Script (Wbfinancedaily.gs:118-121, WbBigQuery.gs:31). */
export const WB_FINANCE_PROD_TARGETS: FinanceTables = {
  raw: 'RAW_WB_FINANCE',
  runs: 'FINANCE_LOADER_RUNS',
  loads: 'FINANCE_REPORT_LOADS',
  weekStatus: 'FINANCE_WEEK_STATUS',
  weekRecon: 'FINANCE_WEEK_RECON',
};

const SHADOW_SUFFIX = '__CR';

export type FinanceTargetConfig = Pick<Config, 'environment' | 'financeTables' | 'financeCutover'>;

const ENV_NAMES: Record<keyof FinanceTables, string> = {
  raw: 'FINANCE_RAW_TABLE',
  runs: 'FINANCE_RUNS_TABLE',
  loads: 'FINANCE_LOADS_TABLE',
  weekStatus: 'FINANCE_WEEK_STATUS_TABLE',
  weekRecon: 'FINANCE_WEEK_RECON_TABLE',
};

export function assertFinanceTargets(c: FinanceTargetConfig): void {
  const keys = Object.keys(WB_FINANCE_PROD_TARGETS) as Array<keyof FinanceTables>;
  const cut = c.financeCutover;
  if (!isCalendarDate(cut) || weekStart(cut) !== cut) {
    throw new LoaderError(`FINANCE_CUTOVER=${cut}: нужна дата-понедельник YYYY-MM-DD`, 'FINANCE_TARGET_MISMATCH');
  }

  if (c.environment === 'shadow') {
    const bad = keys.filter((k) => c.financeTables[k] !== `${WB_FINANCE_PROD_TARGETS[k]}${SHADOW_SUFFIX}`);
    if (bad.length) {
      throw new LoaderError(
        `shadow пишет только в свои ${SHADOW_SUFFIX}: ${bad.map((k) => `${ENV_NAMES[k]}=${c.financeTables[k]} (ожидалось ${WB_FINANCE_PROD_TARGETS[k]}${SHADOW_SUFFIX})`).join(', ')}`,
        'FINANCE_TARGET_MISMATCH',
      );
    }
    return;
  }

  const bad = keys.filter((k) => c.financeTables[k] !== WB_FINANCE_PROD_TARGETS[k]);
  if (bad.length) {
    throw new LoaderError(
      `prod пишет только в боевые таблицы: ${bad.map((k) => `${ENV_NAMES[k]}=${c.financeTables[k]} (ожидалось ${WB_FINANCE_PROD_TARGETS[k]})`).join(', ')}`,
      'FINANCE_TARGET_MISMATCH',
    );
  }
  if (cut !== FIN_PROD_CUTOVER) {
    throw new LoaderError(`prod требует FINANCE_CUTOVER=${FIN_PROD_CUTOVER}, получено ${cut}`, 'FINANCE_TARGET_MISMATCH');
  }
}
