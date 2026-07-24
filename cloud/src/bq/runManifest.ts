/**
 * Execution-guard (PR-Mig0) — persistent run-manifest в BigQuery.
 *
 * REQUIRED FIX 1 (аудит): логический ключ включает ОКРУЖЕНИЕ:
 *     PRIMARY LOGICAL KEY = environment × loader_name × logical_period
 * Иначе shadow, записав COMPLETE за день, заблокировал бы prod (OK_NO_NEW).
 *
 * Чистая функция `decideRun` тестируется на in-memory store; `BqManifestStore`
 * — это BQ-проводка. Атомарность STARTED реализуется MERGE (см. insertStarted).
 */
import { BqClient } from './client.js';
import type { GuardAction, ManifestStatus } from '../errors.js';

export interface ManifestKey {
  environment: string;
  loaderName: string;
  logicalPeriod: string;
}

export interface ManifestRecord extends ManifestKey {
  runId: string;
  executionId: string;
  imageDigest: string;
  gitSha: string;
  status: ManifestStatus;
  attemptCount: number;
  startedAt: string;
  completedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  rowsFetched?: number | null;
  rowsLoaded?: number | null;
}

export interface ManifestStore {
  getCurrent(key: ManifestKey): Promise<ManifestRecord | null>;
  insertStarted(rec: ManifestRecord): Promise<void>;
  finalize(
    key: ManifestKey,
    runId: string,
    patch: { status: ManifestStatus } & Partial<ManifestRecord>,
  ): Promise<void>;
}

export interface DecisionInput {
  current: ManifestRecord | null;
  nowMs: number;
  staleStartedMs: number;
}

export interface Decision {
  action: GuardAction;
  reason: string;
}

/**
 * Чистое решение guard'а по текущей строке манифеста.
 * COMPLETE → OK_NO_NEW; свежий STARTED → ALREADY_RUNNING; устаревший STARTED →
 * RECOVER (перезапуск разрешён); ERROR или отсутствие → PROCEED.
 */
export function decideRun(input: DecisionInput): Decision {
  const { current, nowMs, staleStartedMs } = input;
  if (!current) return { action: 'PROCEED', reason: 'no_prior_run' };
  if (current.status === 'COMPLETE') return { action: 'OK_NO_NEW', reason: 'already_complete' };
  if (current.status === 'ERROR') return { action: 'PROCEED', reason: 'retry_after_error' };
  // STARTED
  const age = nowMs - Date.parse(current.startedAt);
  if (Number.isFinite(age) && age < staleStartedMs) {
    return { action: 'ALREADY_RUNNING', reason: `started_${Math.round(age / 1000)}s_ago` };
  }
  return { action: 'RECOVER', reason: 'stale_started' };
}

export const DEFAULT_STALE_STARTED_MS = 30 * 60 * 1000;

const FQN = (projectId: string, dataset: string, table: string): string =>
  `\`${projectId}.${dataset}.${table}\``;

export class BqManifestStore implements ManifestStore {
  constructor(
    private readonly bq: BqClient,
    private readonly dataset: string,
    private readonly table: string,
  ) {}

  async getCurrent(key: ManifestKey): Promise<ManifestRecord | null> {
    const rows = await this.bq.query<Record<string, unknown>>(
      `SELECT * FROM ${FQN(this.bq.projectId, this.dataset, this.table)}
       WHERE environment=@environment AND loader_name=@loaderName AND logical_period=@logicalPeriod
       ORDER BY started_at DESC LIMIT 1`,
      key as unknown as Record<string, unknown>,
    );
    const r = rows[0];
    if (!r) return null;
    return {
      environment: String(r.environment),
      loaderName: String(r.loader_name),
      logicalPeriod: String(r.logical_period),
      runId: String(r.run_id),
      executionId: String(r.execution_id ?? ''),
      imageDigest: String(r.image_digest ?? ''),
      gitSha: String(r.git_sha ?? ''),
      status: r.status as ManifestStatus,
      attemptCount: Number(r.attempt_count ?? 0),
      startedAt: typeof r.started_at === 'string' ? r.started_at : new Date().toISOString(),
    };
  }

  /**
   * Атомарная вставка STARTED: MERGE, который создаёт строку только если для
   * ключа нет активного STARTED. (В foundation — контракт; строгую гонку
   * добьём в PR-Mig1 вместе с портом остатков.)
   */
  async insertStarted(rec: ManifestRecord): Promise<void> {
    await this.bq.query(
      `MERGE ${FQN(this.bq.projectId, this.dataset, this.table)} T
       USING (SELECT
         @environment AS environment, @loaderName AS loader_name, @logicalPeriod AS logical_period,
         @runId AS run_id, @executionId AS execution_id, @imageDigest AS image_digest,
         @gitSha AS git_sha, @startedAt AS started_at) S
       ON T.environment=S.environment AND T.loader_name=S.loader_name
          AND T.logical_period=S.logical_period AND T.status='STARTED'
       WHEN NOT MATCHED THEN INSERT
         (environment, loader_name, logical_period, run_id, execution_id, image_digest,
          git_sha, status, attempt_count, started_at)
       VALUES
         (S.environment, S.loader_name, S.logical_period, S.run_id, S.execution_id, S.image_digest,
          S.git_sha, 'STARTED', 1, TIMESTAMP(S.started_at))`,
      {
        environment: rec.environment,
        loaderName: rec.loaderName,
        logicalPeriod: rec.logicalPeriod,
        runId: rec.runId,
        executionId: rec.executionId,
        imageDigest: rec.imageDigest,
        gitSha: rec.gitSha,
        startedAt: rec.startedAt,
      },
    );
  }

  async finalize(
    key: ManifestKey,
    runId: string,
    patch: { status: ManifestStatus } & Partial<ManifestRecord>,
  ): Promise<void> {
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
