/** Контракт загрузчика (PR-Mig0). Реальные загрузчики появятся в PR-Mig1+. */
import type { Config } from '../config.js';
import type { Logger } from '../logging.js';

export interface LoaderContext {
  config: Config;
  logger: Logger;
  logicalPeriod: string;
}

export interface LoaderResult {
  rowsFetched: number;
  rowsLoaded: number;
}

export type LoaderHandler = (ctx: LoaderContext) => Promise<LoaderResult>;
