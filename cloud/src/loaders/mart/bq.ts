/**
 * BQ-проводка loader `mart` (PR-Mart3b-1). Всё в датасете `wb_mart`, кроме
 * freshness-gate (читает `wb_raw.V_INGEST_HEARTBEAT`). Не создаёт таблиц (их DDL —
 * sql/mart3/pr_mart3b_mart_runs.sql); операции: SELECT / CALL / MERGE (запись MART_RUNS —
 * mutating DML, НЕ append-only INSERT → нужен updateData на wb_mart.MART_RUNS, см. DDL/IAM).
 *
 * Инъекция QueryRunner делает слой тестируемым без реального BigQuery.
 */
import { BqClient } from '../../bq/client.js';
import { LoaderError } from '../../errors.js';

/** Минимальный контракт исполнителя запросов (реализуется BqClient; в тестах — фейк). */
export interface QueryRunner {
  readonly projectId: string;
  query<T = Record<string, unknown>>(
    query: string,
    params?: Record<string, unknown>,
    types?: Record<string, string>,
  ): Promise<T[]>;
}

/** Обязательные для гейта загрузчики (детерминированный logical_period). */
export const REQUIRED_LOADERS = ['orders', 'sales', 'ads'] as const;

export interface FreshnessRow {
  loader: string;
  covers_target: boolean;
  last_complete_at: string | null;
}

export interface MartSnapshot {
  martRows: number;
  distinctRun: number;   // COUNT(DISTINCT mart_run_id)
  distinctDate: number;  // COUNT(DISTINCT build_as_of_date)
  wrongRun: number;      // строк с mart_run_id != @run_id
  wrongDate: number;     // строк с build_as_of_date != @target_date
  adsActivityLagged: boolean | null;
  adsActivityMaxDate: string | null; // YYYY-MM-DD (диагностика активности)
}

export interface MartRunRecord {
  runId: string;
  environment: string;
  targetDate: string; // YYYY-MM-DD
  status: 'COMPLETE' | 'ERROR';
  startedAtIso: string;
  durationMs: number;
  freshnessJson: string | null;
  adsActivityLagged: boolean | null;
  adsActivityMaxDate: string | null;
  stepsJson: string;
  martRows: number | null;
  gitSha: string;
  imageDigest: string;
  errorCode: string | null;
  errorMessage: string | null;
}

export class MartBq {
  private readonly runner: QueryRunner;
  constructor(projectId: string, location: string, runner?: QueryRunner) {
    this.runner = runner ?? new BqClient(projectId, location);
  }

  private mart(table: string): string {
    return `\`${this.runner.projectId}.wb_mart.${table}\``;
  }
  private raw(obj: string): string {
    return `\`${this.runner.projectId}.wb_raw.${obj}\``;
  }

  /**
   * Freshness-gate LATEST-ATTEMPT (контракт REV5 мастер-дизайна). Список обязательных
   * загрузчиков — константа (безопасно инлайнится), параметр только @target_date.
   * Отсутствующий загрузчик → covers_target=FALSE (LEFT JOIN, fail-closed).
   */
  async checkFreshness(targetDate: string): Promise<FreshnessRow[]> {
    const sql = `
WITH required AS (SELECT l AS loader_name FROM UNNEST(['orders','sales','ads']) AS l),
     latest AS (
       SELECT loader_name, status, completed_at,
              ROW_NUMBER() OVER (PARTITION BY loader_name
                                 ORDER BY started_at DESC, run_id DESC) AS rn
       FROM ${this.raw('V_INGEST_HEARTBEAT')}
       WHERE loader_name IN ('orders','sales','ads')
         AND logical_period = @target_date
         AND started_at    >= TIMESTAMP(DATE_ADD(@target_date, INTERVAL 1 DAY), 'Europe/Moscow')
     )
SELECT r.loader_name AS loader,
       COALESCE(l.status = 'COMPLETE' AND l.completed_at IS NOT NULL, FALSE) AS covers_target,
       CAST(l.completed_at AS STRING) AS last_complete_at
FROM required r
LEFT JOIN (SELECT * FROM latest WHERE rn = 1) l USING (loader_name)
ORDER BY r.loader_name`;
    const rows = await this.runner.query<{ loader: unknown; covers_target: unknown; last_complete_at: unknown }>(
      sql,
      { target_date: targetDate },
      { target_date: 'DATE' },
    );
    return rows.map((r) => ({
      loader: String(r.loader),
      covers_target: r.covers_target === true,
      last_complete_at: r.last_complete_at == null ? null : String(r.last_complete_at),
    }));
  }

  /** Шаг FACT: пересборка фактов под сквозной run_id. */
  async callBootstrapFacts(runId: string): Promise<void> {
    await this.runner.query(
      `CALL ${this.mart('sp_bootstrap_facts')}(@run_id)`,
      { run_id: runId },
      { run_id: 'STRING' },
    );
  }

  /** Шаг MART: сборка витрины. NULL — литерал (global_start по умолчанию). */
  async callBuildMart(targetDate: string, runId: string): Promise<void> {
    await this.runner.query(
      `CALL ${this.mart('sp_build_mart_sku_daily')}(@target_date, NULL, @run_id)`,
      { target_date: targetDate, run_id: runId },
      { target_date: 'DATE', run_id: 'STRING' },
    );
  }

  /**
   * Снимок опубликованной витрины, ПРИВЯЗАННЫЙ к текущему прогону (блокер #2):
   * возвращает число строк, distinct run/date и число строк с ЧУЖИМ run_id/date —
   * чтобы handler доказал, что публикация принадлежит именно @run_id и @target_date.
   */
  async readMartSnapshot(runId: string, targetDate: string): Promise<MartSnapshot> {
    const rows = await this.runner.query<Record<string, unknown>>(
      `SELECT COUNT(*)                                            AS c,
              COUNT(DISTINCT mart_run_id)                         AS d_run,
              COUNT(DISTINCT build_as_of_date)                    AS d_date,
              COUNTIF(mart_run_id     IS DISTINCT FROM @run_id)   AS wrong_run,
              COUNTIF(build_as_of_date IS DISTINCT FROM @target_date) AS wrong_date,
              ANY_VALUE(ads_activity_lagged)                      AS al,
              CAST(ANY_VALUE(ads_activity_max_date) AS STRING)    AS abmd
       FROM ${this.mart('MART_SKU_DAILY')}`,
      { run_id: runId, target_date: targetDate },
      { run_id: 'STRING', target_date: 'DATE' },
    );
    const r = rows[0] ?? {};
    return {
      martRows: Number(r.c ?? 0),
      distinctRun: Number(r.d_run ?? 0),
      distinctDate: Number(r.d_date ?? 0),
      wrongRun: Number(r.wrong_run ?? 0),
      wrongDate: Number(r.wrong_date ?? 0),
      adsActivityLagged: r.al == null ? null : r.al === true,
      adsActivityMaxDate: r.abmd == null ? null : String(r.abmd),
    };
  }

  /**
   * ОДНА терминальная строка на run_id — ИДЕМПОТЕНТНО и с защитой от cross-state/коллизии id (блокер #1):
   *   1) MERGE ON run_id WHEN NOT MATCHED THEN INSERT — повтор того же run_id НЕ плодит дубль и НЕ
   *      перезаписывает уже записанную строку;
   *   2) read-back по run_id сверяет НЕ только статус, но и ИДЕНТИЧНОСТЬ записи (environment, target_date,
   *      git_sha). Иначе fail-closed:
   *      MART_RUNS_DUP (не 1 строка) / MART_RUNS_CONFLICT (иной терминальный статус) /
   *      MART_RUNS_IDENTITY_CONFLICT (тот же run_id, но другая дата/среда/версия кода — НЕ идемпотентный повтор).
   */
  async writeMartRun(rec: MartRunRecord): Promise<void> {
    await this.runner.query(
      `MERGE ${this.mart('MART_RUNS')} T
       USING (SELECT @run_id AS run_id) S ON T.run_id = S.run_id
       WHEN NOT MATCHED THEN INSERT
        (run_id, environment, target_date, status, started_at, completed_at, duration_ms,
         freshness_json, ads_activity_lagged, ads_activity_max_date, steps_json, mart_rows,
         git_sha, image_digest, error_code, error_message)
       VALUES
        (@run_id, @environment, @target_date, @status, TIMESTAMP(@started_at), CURRENT_TIMESTAMP(), @duration_ms,
         @freshness_json, @ads_activity_lagged, @ads_activity_max_date, @steps_json, @mart_rows,
         @git_sha, @image_digest, @error_code, @error_message)`,
      {
        run_id: rec.runId,
        environment: rec.environment,
        target_date: rec.targetDate,
        status: rec.status,
        started_at: rec.startedAtIso,
        duration_ms: rec.durationMs,
        freshness_json: rec.freshnessJson,
        ads_activity_lagged: rec.adsActivityLagged,
        ads_activity_max_date: rec.adsActivityMaxDate,
        steps_json: rec.stepsJson,
        mart_rows: rec.martRows,
        git_sha: rec.gitSha,
        image_digest: rec.imageDigest,
        error_code: rec.errorCode,
        error_message: rec.errorMessage,
      },
      {
        run_id: 'STRING',
        environment: 'STRING',
        target_date: 'DATE',
        status: 'STRING',
        started_at: 'STRING',
        duration_ms: 'INT64',
        freshness_json: 'STRING',
        ads_activity_lagged: 'BOOL',
        ads_activity_max_date: 'DATE',
        steps_json: 'STRING',
        mart_rows: 'INT64',
        git_sha: 'STRING',
        image_digest: 'STRING',
        error_code: 'STRING',
        error_message: 'STRING',
      },
    );

    // read-back: одна строка + сверка статуса И идентичности (environment/target_date/git_sha).
    const back = await this.runner.query<{ c: unknown; status: unknown; environment: unknown; target_date: unknown; git_sha: unknown }>(
      `SELECT COUNT(*) AS c,
              ANY_VALUE(status)                      AS status,
              ANY_VALUE(environment)                 AS environment,
              CAST(ANY_VALUE(target_date) AS STRING) AS target_date,
              ANY_VALUE(git_sha)                     AS git_sha
       FROM ${this.mart('MART_RUNS')} WHERE run_id = @run_id`,
      { run_id: rec.runId },
      { run_id: 'STRING' },
    );
    const row: { c?: unknown; status?: unknown; environment?: unknown; target_date?: unknown; git_sha?: unknown } = back[0] ?? {};
    const c = Number(row.c ?? 0);
    if (c !== 1) {
      throw new LoaderError(`MART_RUNS_DUP: ожидалась 1 строка на run_id=${rec.runId}, получено ${c}`, 'MART_RUNS_DUP');
    }
    const persisted = row.status == null ? null : String(row.status);
    if (persisted !== rec.status) {
      throw new LoaderError(
        `MART_RUNS_CONFLICT: терминальный статус run_id=${rec.runId} в таблице '${persisted}', ожидалось '${rec.status}'`,
        'MART_RUNS_CONFLICT',
      );
    }
    const env = row.environment == null ? null : String(row.environment);
    const td = row.target_date == null ? null : String(row.target_date);
    const gs = row.git_sha == null ? null : String(row.git_sha);
    if (env !== rec.environment || td !== rec.targetDate || gs !== rec.gitSha) {
      throw new LoaderError(
        `MART_RUNS_IDENTITY_CONFLICT: run_id=${rec.runId} уже записан с иной идентичностью ` +
          `(env '${env}' vs '${rec.environment}', date '${td}' vs '${rec.targetDate}', git '${gs}' vs '${rec.gitSha}')`,
        'MART_RUNS_IDENTITY_CONFLICT',
      );
    }
  }
}
