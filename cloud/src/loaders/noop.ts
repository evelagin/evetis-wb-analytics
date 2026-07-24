/**
 * No-op загрузчик (PR-Mig0): доказывает каркас (config → guard → manifest →
 * exit code) БЕЗ обращений к WB API и БЕЗ записи производственных данных.
 * Реальный `wb-stocks` придёт в PR-Mig1.
 */
import type { LoaderContext, LoaderResult } from './types.js';

export async function noopLoader(ctx: LoaderContext): Promise<LoaderResult> {
  ctx.logger.info('noop_loader_run', {
    note: 'foundation no-op: бизнес-логики нет',
    logicalPeriod: ctx.logicalPeriod,
  });
  return { rowsFetched: 0, rowsLoaded: 0 };
}
