/** Контракт загрузчика (PR-Mig0). Реальные загрузчики появятся в PR-Mig1+. */
import type { Config } from '../config.js';
import type { Logger } from '../logging.js';

export interface LoaderContext {
  config: Config;
  logger: Logger;
  logicalPeriod: string;
  /**
   * PR-Mart3b: сквозной id прогона (Cloud Run → LOADER_RUNS → FACT → MART → MART_RUNS).
   * Для generic-загрузчиков совпадает с run_id, вычисленным в cli.
   */
  runId: string;
  /**
   * PR-Mart3b: целевая дата прогона (YYYY-MM-DD). Для витрины — D-1 Europe/Moscow.
   * ИНВАРИАНТ: `targetDate === logicalPeriod` (cli/entry-point выставляют их одинаковыми;
   * handler НЕ пересчитывает текущую дату самостоятельно).
   *
   * PR-1: формат периода задаёт загрузчик (см. registry.ts). У наблюдателя цен период —
   * 20-минутное окно UTC, поэтому и targetDate у него окно, а не дата. Инвариант
   * равенства сохраняется; наблюдатель цен targetDate не использует.
   */
  targetDate: string;
}

export interface LoaderResult {
  rowsFetched: number;
  rowsLoaded: number;
}

export type LoaderHandler = (ctx: LoaderContext) => Promise<LoaderResult>;
