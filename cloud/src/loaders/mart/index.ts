/**
 * Loader `mart` (PR-Mart3b-1) — оркестратор витрины MART_SKU_DAILY.
 *
 * Поток (fail-closed):
 *   1) freshness-gate LATEST-ATTEMPT по V_INGEST_HEARTBEAT (РОВНО orders/sales/ads за target_date) —
 *      именно heartbeat, а не MAX(FACT_ADS.date), доказывает ПОКРЫТИЕ рекламы (блокер #4);
 *   2) при OK: CALL sp_bootstrap_facts(runId) → CALL sp_build_mart_sku_daily(targetDate, NULL, runId);
 *   3) снимок опубликованной витрины, ПРИВЯЗАННЫЙ к run_id/target_date (блокер #2);
 *   4) ОДНА терминальная запись в MART_RUNS (COMPLETE|ERROR), идемпотентно (блокер #1).
 *
 * Контракты аудита:
 *   - инвариант ctx.logicalPeriod === ctx.targetDate;
 *   - freshness-результат — ровно уникальные orders/sales/ads;
 *   - публикация витрины принадлежит именно этому run_id и target_date (иначе fail-closed);
 *   - MART_RUNS: одна строка на run_id (MERGE+read-back), запись не маскирует исходную ошибку;
 *   - run-lease (LOADER_RUNS) здесь НЕ трогаем (в PR-Mart3b-1 mart ходит только через mart_manual.ts).
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { MartBq, REQUIRED_LOADERS, type FreshnessRow, type MartRunRecord } from './bq.js';

interface Step {
  step: string;
  status: string;
  duration_ms: number;
}

/** Классификация error_code для MART_RUNS: код LoaderError → дефолт MART_ERROR. */
export function classifyMartError(e: unknown): string {
  if (e instanceof LoaderError && e.code && e.code !== 'LOADER_ERROR') return e.code;
  return 'MART_ERROR';
}

export async function martLoader(ctx: LoaderContext, injectedBq?: MartBq): Promise<LoaderResult> {
  const { config, logger, targetDate, runId } = ctx;
  const bq = injectedBq ?? new MartBq(config.projectId, config.bqLocation);
  const startedAtIso = new Date().toISOString();
  const startMs = Date.now();
  const steps: Step[] = [];
  let freshness: FreshnessRow[] | null = null;
  // Текущий шаг для честного failed-step в steps_json (аудит REV4 #2):
  // OK пишется только ПОСЛЕ всех проверок шага; при ошибке в catch пишется ЭТОТ шаг с кодом ошибки.
  let currentStep = 'ctx_invariant';
  let stepStartMs = startMs;

  try {
    // 0) инвариант контекста — ВНУТРИ try, чтобы рассинхрон тоже дал терминальную строку
    //    MART_RUNS ERROR (иначе LOADER_RUNS=ERROR, а MART_RUNS отсутствует — нарушение контракта журнала).
    if (ctx.logicalPeriod !== targetDate) {
      throw new LoaderError(
        `CTX_INVARIANT: logicalPeriod(${ctx.logicalPeriod}) !== targetDate(${targetDate})`,
        'CTX_INVARIANT',
      );
    }

    // 1) freshness-gate. ВАЖНО (аудит REV4 #2): статус OK попадает в steps ТОЛЬКО после
    //    успешных проверок shape И coverage — иначе при отказе гейта steps_json лгал бы
    //    «freshness_gate: OK» рядом с error_code=FRESHNESS_GATE.
    currentStep = 'freshness_gate';
    stepStartMs = Date.now();
    freshness = await bq.checkFreshness(targetDate);
    // форма результата: РОВНО уникальные orders/sales/ads (иначе вью/данные аномальны → fail-closed).
    const got = [...new Set(freshness.map((r) => r.loader))].sort();
    const want = [...REQUIRED_LOADERS].sort();
    if (freshness.length !== want.length || got.length !== want.length || got.join(',') !== want.join(',')) {
      throw new LoaderError(
        `FRESHNESS_SHAPE: ожидались ровно уникальные ${want.join('/')}, получено [${freshness.map((r) => r.loader).join(', ')}]`,
        'FRESHNESS_SHAPE',
      );
    }
    const failed = freshness.filter((r) => !r.covers_target).map((r) => r.loader);
    if (failed.length > 0) {
      throw new LoaderError(
        `FRESHNESS_GATE: источники не COMPLETE за ${targetDate}: ${failed.join(', ')}`,
        'FRESHNESS_GATE',
      );
    }
    steps.push({ step: 'freshness_gate', status: 'OK', duration_ms: Date.now() - stepStartMs });

    // 2a) FACT
    currentStep = 'sp_bootstrap_facts';
    stepStartMs = Date.now();
    await bq.callBootstrapFacts(runId);
    steps.push({ step: 'sp_bootstrap_facts', status: 'OK', duration_ms: Date.now() - stepStartMs });

    // 2b) MART
    currentStep = 'sp_build_mart_sku_daily';
    stepStartMs = Date.now();
    await bq.callBuildMart(targetDate, runId);
    steps.push({ step: 'sp_build_mart_sku_daily', status: 'OK', duration_ms: Date.now() - stepStartMs });

    // 3) снимок витрины, ПРИВЯЗАННЫЙ к текущему прогону (блокер #2)
    currentStep = 'mart_snapshot';
    stepStartMs = Date.now();
    const snap = await bq.readMartSnapshot(runId, targetDate);
    if (snap.martRows === 0) {
      throw new LoaderError('MART_EMPTY: витрина пуста после publish', 'MART_EMPTY');
    }
    if (snap.wrongRun > 0 || snap.distinctRun !== 1) {
      throw new LoaderError(
        `MART_RUN_MISMATCH: строки витрины не принадлежат run_id (wrongRun=${snap.wrongRun}, distinctRun=${snap.distinctRun})`,
        'MART_RUN_MISMATCH',
      );
    }
    if (snap.wrongDate > 0 || snap.distinctDate !== 1) {
      throw new LoaderError(
        `MART_DATE_MISMATCH: build_as_of_date витрины != target_date (wrongDate=${snap.wrongDate}, distinctDate=${snap.distinctDate})`,
        'MART_DATE_MISMATCH',
      );
    }

    // snapshot доказан → закрываем его шаг OK (аудит REV5: иначе сбой ЗАПИСИ COMPLETE ниже
    // ложно маркировался бы как mart_snapshot=MART_ERROR при успешном snapshot).
    steps.push({ step: 'mart_snapshot', status: 'OK', duration_ms: Date.now() - stepStartMs });

    // 4) терминальная запись COMPLETE — СОБСТВЕННЫЙ шаг журнала.
    currentStep = 'mart_runs_complete';
    stepStartMs = Date.now();
    await bq.writeMartRun(record('COMPLETE', {
      config, targetDate, runId, startedAtIso, startMs, freshness,
      adsActivityLagged: snap.adsActivityLagged, adsActivityMaxDate: snap.adsActivityMaxDate,
      steps, martRows: snap.martRows, errorCode: null, errorMessage: null,
    }));

    logger.info('mart_complete', {
      targetDate, runId, martRows: snap.martRows,
      adsActivityLagged: snap.adsActivityLagged, adsActivityMaxDate: snap.adsActivityMaxDate,
    });
    return { rowsFetched: snap.martRows, rowsLoaded: snap.martRows };
  } catch (e) {
    const code = classifyMartError(e);
    const message = e instanceof Error ? e.message : String(e);
    // Честный operational log (аудит REV4 #2): пишем КОНКРЕТНЫЙ упавший шаг с кодом ошибки,
    // а не общий 'error'. OK для этого шага в steps отсутствует (он ставится только после проверок).
    steps.push({ step: currentStep, status: code, duration_ms: Date.now() - stepStartMs });
    // Запись ERROR НЕ должна маскировать исходную ошибку.
    try {
      await bq.writeMartRun(record('ERROR', {
        config, targetDate, runId, startedAtIso, startMs, freshness,
        adsActivityLagged: null, adsActivityMaxDate: null,
        steps, martRows: null, errorCode: code, errorMessage: message,
      }));
    } catch (e2) {
      logger.error('mart_runs_write_failed', {
        message: e2 instanceof Error ? e2.message : String(e2),
        original_code: code, original_message: message,
      });
    }
    logger.error('mart_failed', { code, message });
    throw e instanceof LoaderError ? e : new LoaderError(message, code);
  }
}

/** Сборка строки MART_RUNS (единая точка, чтобы COMPLETE/ERROR были симметричны). */
function record(
  status: 'COMPLETE' | 'ERROR',
  a: {
    config: LoaderContext['config'];
    targetDate: string;
    runId: string;
    startedAtIso: string;
    startMs: number;
    freshness: FreshnessRow[] | null;
    adsActivityLagged: boolean | null;
    adsActivityMaxDate: string | null;
    steps: Step[];
    martRows: number | null;
    errorCode: string | null;
    errorMessage: string | null;
  },
): MartRunRecord {
  return {
    runId: a.runId,
    environment: a.config.environment,
    targetDate: a.targetDate,
    status,
    startedAtIso: a.startedAtIso,
    durationMs: Date.now() - a.startMs,
    freshnessJson: a.freshness ? JSON.stringify(a.freshness) : null,
    adsActivityLagged: a.adsActivityLagged,
    adsActivityMaxDate: a.adsActivityMaxDate,
    stepsJson: JSON.stringify(a.steps),
    martRows: a.martRows,
    gitSha: a.config.gitSha,
    imageDigest: a.config.imageDigest,
    errorCode: a.errorCode,
    errorMessage: a.errorMessage,
  };
}
