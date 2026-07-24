/**
 * Execution-guard (PR-Mig0) — атомарный persistent run-manifest в BigQuery.
 *
 * Аудит-фиксы:
 *  - №3 атомарность: захват lock'а через BQ multi-statement TRANSACTION
 *    (snapshot isolation + конфликт-детект). acquire() ВОЗВРАЩАЕТ результат;
 *    loader выполняется ТОЛЬКО при acquired=true.
 *  - №4 recovery: устаревший STARTED не считается активным → вставляется НОВАЯ
 *    строка STARTED (свой run_id), finalize() находит её по run_id.
 *  - REQUIRED FIX 1 (прошлый аудит): логический ключ включает environment.
 *
 * Чистые помощники (isActive/classifyReason) тестируются отдельно; BqManifestStore
 * — BQ-проводка. MemManifestStore (в тестах) моделирует ту же семантику.
 */
import { BqClient } from './client.js';
import { normalizeBqTimestamp } from './bqTime.js';
import type { ManifestStatus } from '../errors.js';

export const DEFAULT_STALE_STARTED_MS = 30 * 60 * 1000;

export interface ManifestKey {
  environment: string;
  loaderName: string;
  logicalPeriod: string;
}

export interface ManifestRow extends ManifestKey {
  runId: string;
  status: ManifestStatus;
  startedAt: string;
}

export interface AcquireParams extends ManifestKey {
  runId: string;
  executionId: string;
  imageDigest: string;
  gitSha: string;
  nowMs: number;
  staleMs: number;
}

export type AcquireResult =
  | { acquired: true; runId: string; recovered: boolean }
  | { acquired: false; reason: 'ALREADY_RUNNING' | 'COMPLETE' };

export interface FinalizePatch {
  status: ManifestStatus;
  errorCode?: string | null;
  errorMessage?: string | null;
  rowsFetched?: number | null;
  rowsLoaded?: number | null;
}

export interface ManifestStore {
  acquire(params: AcquireParams): Promise<AcquireResult>;
  finalize(key: ManifestKey, runId: string, patch: FinalizePatch): Promise<void>;
}

/** Активна ли строка для целей блокировки: COMPLETE или свежий STARTED. */
export function isActive(row: ManifestRow, nowMs: number, staleMs: number): boolean {
  if (row.status === 'COMPLETE') return true;
  if (row.status === 'STARTED') {
    const t = Date.parse(row.startedAt);
    return Number.isFinite(t) && t > nowMs - staleMs;
  }
  return false; // ERROR / прочее — не блокирует
}

/** Причина отказа по последней строке (когда есть активная). */
export function classifyReason(latest: ManifestRow | null): 'ALREADY_RUNNING' | 'COMPLETE' {
  return latest?.status === 'COMPLETE' ? 'COMPLETE' : 'ALREADY_RUNNING';
}

const FQN = (projectId: string, dataset: string, table: string): string =>
  `\`${projectId}.${dataset}.${table}\``;

export class BqManifestStore implements ManifestStore {
  constructor(
    private readonly bq: BqClient,
    private readonly dataset: string,
    private readonly table: string,
  ) {}

  /**
   * Атомарный захват в транзакции. Возвращает active (кол-во активных строк ДО
   * вставки) и cur_status (статус последней строки). При конфликте транзакции
   * (гонка) BigQuery прерывает один из execution — трактуем как ALREADY_RUNNING.
   */
  async acquire(p: AcquireParams): Promise<AcquireResult> {
    const staleIso = new Date(p.nowMs - p.staleMs).toISOString();
    const t = FQN(this.bq.projectId, this.dataset, this.table);
    const sql = `
BEGIN
  DECLARE active INT64 DEFAULT 0;
  DECLARE cur_status STRING DEFAULT NULL;
  BEGIN TRANSACTION;
  SET active = (
    SELECT COUNT(*) FROM ${t}
    WHERE environment=@environment AND loader_name=@loaderName AND logical_period=@logicalPeriod
      AND (status='COMPLETE' OR (status='STARTED' AND started_at > TIMESTAMP(@staleIso)))
  );
  SET cur_status = (
    SELECT status FROM ${t}
    WHERE environment=@environment AND loader_name=@loaderName AND logical_period=@logicalPeriod
    ORDER BY started_at DESC LIMIT 1
  );
  IF active = 0 THEN
    INSERT INTO ${t}
      (environment, loader_name, logical_period, run_id, execution_id, image_digest,
       git_sha, status, attempt_count, started_at)
    VALUES
      (@environment, @loaderName, @logicalPeriod, @runId, @executionId, @imageDigest,
       @gitSha, 'STARTED', 1, CURRENT_TIMESTAMP());
  END IF;
  COMMIT TRANSACTION;
  SELECT active AS active, cur_status AS cur_status;
END`;
    let rows: Array<{ active?: unknown; cur_status?: unknown }>;
    try {
      rows = await this.bq.query(sql, {
        environment: p.environment,
        loaderName: p.loaderName,
        logicalPeriod: p.logicalPeriod,
        runId: p.runId,
        executionId: p.executionId,
        imageDigest: p.imageDigest,
        gitSha: p.gitSha,
        staleIso,
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // Конфликт транзакции = кто-то другой захватил параллельно.
      if (/conflict|abort|serializ|concurrent/i.test(msg)) {
        return { acquired: false, reason: 'ALREADY_RUNNING' };
      }
      throw e;
    }
    const r = rows[0] ?? {};
    const active = Number(r.active ?? 0);
    const curStatus = r.cur_status == null ? null : String(r.cur_status);
    if (active === 0) {
      return { acquired: true, runId: p.runId, recovered: curStatus !== null };
    }
    return { acquired: false, reason: curStatus === 'COMPLETE' ? 'COMPLETE' : 'ALREADY_RUNNING' };
  }

  async finalize(key: ManifestKey, runId: string, patch: FinalizePatch): Promise<void> {
    await this.bq.query(
      `UPDATE ${FQN(this.bq.projectId, this.dataset, this.table)}
       SET status=@status, completed_at=CURRENT_TIMESTAMP(),
           error_code=@errorCode, error_message=@errorMessage,
           rows_fetched=@rowsFetched, rows_loaded=@rowsLoaded
       WHERE environment=@environment AND loader_name=@loaderName
         AND logical_period=@logicalPeriod AND run_id=@runId`,
      {
        status: patch.status,
        errorCode: patch.errorCode ?? null,
        errorMessage: patch.errorMessage ?? null,
        rowsFetched: patch.rowsFetched ?? null,
        rowsLoaded: patch.rowsLoaded ?? null,
        environment: key.environment,
        loaderName: key.loaderName,
        logicalPeriod: key.logicalPeriod,
        runId,
      },
    );
  }
}

// normalizeBqTimestamp реэкспортируем для потребителей манифеста.
export { normalizeBqTimestamp };
