/**
 * M4 · Взаимоисключение тика `finance` и регистрации `finance-backfill` (эквивалент общего ScriptLock Apps Script:
 * wbFinDeepBackfillRegister брал тот же lock, что и runWbFinanceDaily).
 *
 * Lease cli ключуется (environment, loader_name, logical_period), поэтому два режима с разными именами
 * сами по себе друг друга не видят: параллельно они делили бы лимит Finance API (429) и писали бы DML в один
 * манифест. Общий ключ не годится: COMPLETE регистрации занял бы часовой слот тика.
 *
 * Схема «два флага»: каждый режим СНАЧАЛА пишет свою строку STARTED (acquire в cli, до handler), ПОТОМ ищет
 * свежую STARTED-строку соседа. Из двух одновременных прогонов хотя бы один проверяет позже, чем оба записались,
 * и видит соседа; оба отказаться могут, оба пройти — нет (fail-closed). Свежесть — stale-порог lease (30 мин):
 * сосед, упавший без finalize, перестаёт блокировать тогда же, когда cli перестаёт считать его активным.
 */
import { BqClient } from '../../bq/client.js';
import { DEFAULT_STALE_STARTED_MS } from '../../bq/runManifest.js';
import type { LoaderContext } from '../types.js';
import { FINANCE_LOADER_NAME, FINANCE_BACKFILL_LOADER_NAME } from './constants.js';

/** Число свежих STARTED-строк соседнего режима в LOADER_RUNS того же окружения. */
export type PeerProbe = (ctx: LoaderContext, peerLoader: string) => Promise<number>;

export function peerOf(mode: 'tick' | 'backfill'): string {
  return mode === 'tick' ? FINANCE_BACKFILL_LOADER_NAME : FINANCE_LOADER_NAME;
}

/** SELECT по LOADER_RUNS (партиция started_at отсекается: граница — константное выражение). */
export function peerActiveSql(projectId: string, dataset: string, table: string): string {
  const minutes = Math.round(DEFAULT_STALE_STARTED_MS / 60_000);
  return `SELECT COUNT(*) AS n FROM \`${projectId}.${dataset}.${table}\`
WHERE environment = @env AND loader_name = @peer AND status = 'STARTED'
  AND started_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${minutes} MINUTE)`;
}

export const bqPeerProbe: PeerProbe = async (ctx, peer) => {
  const c = ctx.config;
  const rows = await new BqClient(c.projectId, c.bqLocation).query<{ n: unknown }>(
    peerActiveSql(c.projectId, c.rawDataset, c.manifestTable),
    { env: c.environment, peer },
    { env: 'STRING', peer: 'STRING' },
  );
  return Number(rows[0]?.n ?? 0);
};
