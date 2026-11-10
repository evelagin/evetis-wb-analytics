/**
 * M4 · Тик финансового загрузчика — порт finDailyRun_ / finDailyLoadReport_ /
 * finDailyRegisterReports_ / finDailyFinalizeWeeks_ / wbFinDeepBackfillRegister (Wbfinancedaily.gs).
 *
 * Чистая оркестрация: хранилище (FinanceStore), Finance API и часы инъектируются — весь сценарий
 * проверяется офлайн. Порядок шагов тот же, что в Apps Script:
 *   1 stale-recovery → 2 календарь недель → PK-assert → 3 очередь → 4 discovery daily/weekly →
 *   PK-assert → 5 свежеоткрытое → 6 финализация недель + RECON → 7 статус прогона.
 */
import type { Logger } from '../../logging.js';
import { classifyFailure } from '../../failure.js';
import {
  FIN_LOOKBACK_DAYS, FIN_STALE_STARTED_MIN, FIN_MAX_ATTEMPTS, FIN_METRIC_TOL, FIN_LOAD_BATCH, FIN_LIST_METRICS,
} from './constants.js';
import { isDigits, weekStart, todayMsk, finNum, r2, financeLoadJobId } from './parse.js';
import {
  checkDetailedKeys, detailedSums, mapDetailedRow, runStatusOf,
  type ListReport, type ReportPeriod, type RunCounters, type RunStatus,
} from './normalize.js';
import type { FinanceStore, QueueItem, ReconRow, WeeklyCandidate } from './store.js';
import type { FinanceApi, Clock } from './wbApi.js';

export type FinanceApiPort = Pick<FinanceApi, 'canRequest' | 'list' | 'detailed' | 'requests'>;

export interface TickDeps {
  store: FinanceStore;
  api: FinanceApiPort;
  clock: Clock;
  logger: Logger;
  runId: string;
  triggerType: string;
  cutover: string;
}

export interface TickResult {
  status: RunStatus;
  runId: string;
  counters: RunCounters;
  queueLeft: number;
  finalized: number;
  anomalies: string[];
  errorMessage: string;
  rowsFetched: number;
  rowsLoaded: number;
}

type LoadOutcome = { status: 'COMPLETE'; fetched: number; loaded: number } | { status: 'ERROR' | 'SKIPPED' };

/** Регистрация отчётов list (дизайн §3.3/§4). */
export async function registerReports(
  store: FinanceStore,
  runId: string,
  reports: ListReport[],
  period: ReportPeriod,
  finalWeeks: Set<string>,
): Promise<{ registered: number; skippedFinal: number; anomalies: string[] }> {
  const res = { registered: 0, skippedFinal: 0, anomalies: [] as string[] };
  const candidates: ListReport[] = [];
  for (const rep of reports) {
    // daily финализированной (неделя × тип) недели НЕ регистрируется
    if (period === 'DAILY' && finalWeeks.has(`${weekStart(rep.dateFrom)}|${rep.reportType}`)) {
      res.skippedFinal++;
      continue;
    }
    candidates.push(rep);
  }
  if (!candidates.length) return res;

  const manifest = await store.manifestByIds(candidates.map((r) => r.reportId));
  for (const rep of candidates) {
    const ex = manifest.get(rep.reportId);
    if (ex && ex.status === 'COMPLETE') {
      // COMPLETE неизменяем: только сверка list-метрик, расхождение — аномалия прогона
      for (const [, col] of FIN_LIST_METRICS) {
        const stored = ex.metrics[col];
        const fresh = rep.metrics[col];
        if (Math.abs(stored - fresh) > FIN_METRIC_TOL) res.anomalies.push(`${rep.reportId}:${col} stored=${stored} fresh=${fresh}`);
      }
      continue;
    }
    await store.mergeDiscovered({
      reportId: rep.reportId, period, reportType: rep.reportType,
      dateFrom: rep.dateFrom, dateTo: rep.dateTo, metrics: rep.metrics, metricsJson: rep.metricsJson,
    }, runId);
    if (!ex) res.registered++;
  }
  return res;
}

/** DISCOVERED/ERROR → STARTED → detailed(rrdId=0) → RAW load-job → post-load → COMPLETE|ERROR. */
export async function loadReport(d: TickDeps, item: QueueItem): Promise<LoadOutcome> {
  const { store, api, logger, runId } = d;
  const rid = String(item.report_id);
  const log = logger.child({ reportPeriod: item.report_period, reportType: item.report_type, dateFrom: item.date_from });
  if (!isDigits(rid)) { log.error('finance_report_bad_id'); return { status: 'SKIPPED' }; }

  if (!(await store.mergeStarted(rid, runId, FIN_MAX_ATTEMPTS))) {
    log.info('finance_report_not_captured');
    return { status: 'SKIPPED' };
  }
  const fail = async (code: string, msg: string): Promise<LoadOutcome> => {
    await store.mergeError(rid, runId, msg);
    log.error('finance_report_error', { code, error: msg.substring(0, 300) });
    return { status: 'ERROR' };
  };

  // A5a · pre-load orphan guard — ДО запроса detailed и ДО любого load-job'а.
  // Как в Apps Script: любой отказ guard'а (включая сбой самого запроса) переводит отчёт в ERROR.
  let resume: number | null = null;
  let guardMsg = '';
  try {
    const p1 = await store.orphanPruned(rid, runId, item.date_from, item.date_to);
    if (p1.otherRun > 0) {
      guardMsg = `ORPHANED_RAW_REPORT: report_id=${rid}, this_run=${p1.thisRun}, other_run=${p1.otherRun}, run_id=${runId}, `
        + `period=${item.date_from}..${item.date_to}. Строки прошлого прогона остались в RAW без владельца в манифесте. `
        + 'Автоматическое усыновление вне PR-A — требуется ручное восстановление.';
    } else if (p1.thisRun > 0) {
      resume = p1.thisRun; // повторный заход внутри ТОГО ЖЕ прогона: строки уже наши
    } else {
      const p2 = await store.orphanFull(rid, item.date_from, item.date_to);
      if (p2.full > 0) {
        guardMsg = `ORPHANED_RAW_OUTSIDE_MANIFEST_PERIOD: report_id=${rid}, full=${p2.full}, pruned=0, `
          + `outside=${p2.outside}, null_rr_date=${p2.nullRr}, run_count=${p2.runCount}, period=${item.date_from}..${item.date_to}. `
          + 'Строки отчёта есть в RAW вне периода манифеста — partition-инвариант нарушен.';
      }
    }
  } catch (e) {
    guardMsg = e instanceof Error ? e.message : String(e);
  }
  if (guardMsg) return fail('ORPHAN_GUARD', guardMsg);

  const fetched = await api.detailed(rid);
  if (!fetched.ok) return fail('DETAILED_FAILED', fetched.error);
  const rows = fetched.rows;

  const k = checkDetailedKeys(rows);
  if (k.empty > 0 || k.dup > 0) return fail('KEY_CONFLICT', `KEY_CONFLICT: пустых rrdId=${k.empty}, дублей=${k.dup}`);

  const ctx = {
    runId, loadedAt: new Date(d.clock.now()).toISOString(),
    reportId: rid, reportPeriod: item.report_period,
    reportType: Number(item.report_type) || 0,
    dateFrom: item.date_from, dateTo: item.date_to,
  };
  const objs = rows.map((r) => mapDetailedRow(r, ctx));
  const det = detailedSums(rows);

  let loaded = 0;
  if (resume !== null) {
    loaded = resume;
    log.info('finance_report_resume', { persisted: resume });
  } else {
    try {
      let batch = 0;
      for (let b = 0; b < objs.length; b += FIN_LOAD_BATCH) {
        loaded += await store.loadRaw(objs.slice(b, b + FIN_LOAD_BATCH), financeLoadJobId(rid, runId, batch));
        batch++;
      }
    } catch (e) {
      return fail('LOAD_JOB', `LOAD_JOB: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  // Post-load: фактически сохранённое этим прогоном
  const pl = await store.postLoadThisRun(rid, runId);
  if (!(pl.count === pl.distinct && pl.count === rows.length && pl.count === loaded)) {
    return fail('POSTLOAD_MISMATCH', `POSTLOAD_MISMATCH: persisted=${pl.count} distinct=${pl.distinct} fetched=${rows.length} loaded=${loaded}`);
  }
  // A5c · post-load без привязки к прогону: межпрогонное задвоение сразу после загрузки
  const cr = await store.postLoadCrossRun(rid, item.date_from, item.date_to);
  if (!(cr.count === cr.distinct && cr.count === rows.length)) {
    return fail('POSTLOAD_CROSSRUN_MISMATCH', `POSTLOAD_CROSSRUN_MISMATCH: report_rows=${cr.count} distinct_rrd=${cr.distinct} `
      + `fetched=${rows.length} this_run=${pl.count}. В RAW по этому report_id больше строк, чем загрузил текущий прогон.`);
  }

  // Строки уже в RAW: транзиентный отказ BigQuery на COMPLETE повторяется один раз (иначе отчёт остался бы STARTED
  // со строками и через 120 мин ушёл бы в ORPHANED_RAW_REPORT). MERGE идемпотентен: ловит только STARTED своего прогона.
  let completed: boolean;
  try {
    completed = await store.mergeComplete(rid, runId, rows.length, loaded, det);
  } catch (e) {
    if (classifyFailure(e).category !== 'TRANSIENT' && !/concurrent update|serialize access/i.test(String(e))) throw e;
    log.warn('finance_complete_retry', { error: e instanceof Error ? e.message : String(e) });
    await d.clock.sleep(5_000);
    completed = await store.mergeComplete(rid, runId, rows.length, loaded, det);
  }
  if (!completed) {
    let diag = '';
    try { diag = await store.manifestDiag(rid); } catch (e) { diag = `диагностика недоступна: ${String(e)}`; }
    log.error('finance_report_error', { code: 'COMPLETE_MERGE_NOT_APPLIED', diag });
    try { await store.mergeError(rid, runId, `COMPLETE_MERGE_NOT_APPLIED; ${diag}`); } catch (e) {
      log.error('finance_report_error_not_applied', { error: String(e) });
    }
    return { status: 'ERROR' };
  }
  log.info('finance_report_complete', { rows: rows.length, pages: fetched.pages });
  return { status: 'COMPLETE', fetched: rows.length, loaded };
}

/** RECON + weekly_final одной недели (RECON — до weekly_final). */
async function finalizeOne(store: FinanceStore, wk: WeeklyCandidate, logger: Logger, event: string): Promise<void> {
  const ds = await store.dailySums(wk.report_type, wk.week_start);
  const rows: ReconRow[] = FIN_LIST_METRICS.map(([name, col]) => ({
    metric: name,
    sum_daily: r2(finNum(ds.metrics[col])),
    sum_weekly: r2(finNum(wk.metrics[col])),
  }));
  await store.mergeRecon(wk.week_start, wk.report_type, rows, FIN_METRIC_TOL);
  await store.markWeekFinal(wk.week_start, wk.report_type, wk.report_id, ds.days_loaded);
  logger.info(event, { weekStart: wk.week_start, reportType: wk.report_type, dailyDays: ds.days_loaded });
}

/** Финализация недель + RECON (только SQL по манифесту — бюджета API не ест). Возвращает финализированные недели. */
export async function finalizeWeeks(store: FinanceStore, cutover: string, logger: Logger): Promise<WeeklyCandidate[]> {
  const done: WeeklyCandidate[] = [];
  for (const wk of await store.finalizeCandidates(cutover)) {
    await finalizeOne(store, wk, logger, 'finance_week_finalized');
    done.push(wk);
  }
  return done;
}

/** Основной тик. Не бросает: отказ любой стадии закрывает журнал как ERROR (как finDailyRun_). */
export async function runFinanceTick(d: TickDeps): Promise<TickResult> {
  const { store, api, clock, logger, runId } = d;
  const counters: RunCounters = { discovered: 0, loaded: 0, errors: 0, requests: 0 };
  let anomalies: string[] = [];
  let queueLeft = 0;
  let runErr = '';
  let finalized = 0;
  let rowsFetched = 0;
  let rowsLoaded = 0;
  let opened = false;
  const t0 = clock.now();

  /** Какие слои дошли до COMPLETE в текущем цикле (для решения о финализации и пересчёте RECON). */
  const completedIn = { WEEKLY: 0, DAILY: 0 };
  const loadOne = async (item: QueueItem): Promise<void> => {
    const res = await loadReport(d, item);
    if (res.status === 'COMPLETE') {
      counters.loaded++;
      rowsFetched += res.fetched;
      rowsLoaded += res.loaded;
      completedIn[item.report_period]++;
    } else if (res.status === 'ERROR') counters.errors++;
  };

  try {
    await store.runOpen(runId, d.triggerType, new Date(t0).toISOString());
    opened = true;
    logger.info('finance_run_open', { financeRunId: runId, triggerType: d.triggerType });

    // 1. Stale-recovery (attempt_count НЕ инкрементится)
    const recovered = await store.staleRecovery(FIN_STALE_STARTED_MIN);
    if (recovered) logger.warn('finance_stale_recovered', { recovered });

    // 2. Календарные строки недель: от cutover до последней завершённой недели, типы 1 и 2
    const lastCompleted = weekStart(todayMsk(clock.now(), -7));
    if (lastCompleted >= d.cutover) await store.ensureWeekRows(d.cutover, lastCompleted);

    await store.assertManifestPk('до discovery');

    // Снимок состояния недель ДО очереди. В Apps Script его читают после шага 3, но там до шага 6 неделя не
    // финализируется, поэтому «до» и «после» очереди совпадают. Снимок нужен, чтобы ранняя финализация (ниже)
    // не изменила ни регистрацию суточных («пропущено (final week)»), ни решение о weekly list.
    const finalWeeks = await store.finalWeeks();
    const needWeekly = await store.hasOpenWeeks();

    // 3. Очередь — до новых list-запросов
    const queue = await store.queue(FIN_MAX_ATTEMPTS);
    logger.info('finance_queue', { size: queue.length });
    for (let i = 0; i < queue.length; i++) {
      if (!api.canRequest()) { queueLeft = queue.length - i; break; }
      await loadOne(queue[i]!);
    }

    // ОТЛИЧИЕ: если в очереди дошёл до COMPLETE недельный отчёт, неделя финализируется сразу после цикла очереди,
    // а не после discovery и свежих отчётов. Пока WEEKLY COMPLETE, а неделя не финальна, V_WB_FINANCE_CANONICAL
    // держит DAILY и WEEKLY строки одних rrd_id (ASSERT витрины #91). Окно кончается вместе с циклом очереди и
    // не включает discovery и шаг 5 — то есть не длиннее, чем у Apps Script. Итог RECON совпадает с Apps Script:
    // недели, финализированные здесь, пересчитываются после шага 5, если там загружались суточные.
    const earlyFinal = completedIn.WEEKLY > 0 ? await finalizeWeeks(store, d.cutover, logger) : [];
    finalized += earlyFinal.length;
    completedIn.WEEKLY = 0;
    completedIn.DAILY = 0;

    // 4. Discovery — при остатке бюджета
    if (api.canRequest()) {
      const dl = await api.list('daily', todayMsk(clock.now(), -FIN_LOOKBACK_DAYS), todayMsk(clock.now(), 0));
      if (!dl.ok) {
        counters.errors++;
        runErr = dl.error;
        logger.error('finance_list_failed', { period: 'daily', error: dl.error });
      } else {
        const reg = await registerReports(store, runId, dl.reports, 'DAILY', finalWeeks);
        counters.discovered += reg.registered;
        anomalies = anomalies.concat(reg.anomalies);
        logger.info('finance_discovery', { period: 'daily', reports: dl.reports.length, registered: reg.registered, skippedFinal: reg.skippedFinal });
      }
    } else queueLeft = Math.max(queueLeft, 1);

    if (needWeekly && api.canRequest()) {
      const wl = await api.list('weekly', d.cutover, todayMsk(clock.now(), 0));
      if (!wl.ok) {
        counters.errors++;
        runErr = runErr || wl.error;
        logger.error('finance_list_failed', { period: 'weekly', error: wl.error });
      } else {
        const regW = await registerReports(store, runId, wl.reports, 'WEEKLY', new Set());
        counters.discovered += regW.registered;
        anomalies = anomalies.concat(regW.anomalies);
        logger.info('finance_discovery', { period: 'weekly', reports: wl.reports.length, registered: regW.registered });
      }
    }

    await store.assertManifestPk('после discovery');

    // 5. Свежеоткрытое — в этом же прогоне при остатке бюджета
    if (counters.discovered > 0) {
      const fresh = await store.discovered();
      for (let j = 0; j < fresh.length; j++) {
        if (!api.canRequest()) { queueLeft += fresh.length - j; break; }
        await loadOne(fresh[j]!);
      }
    }

    // 6. Финализация недель + RECON (как в Apps Script — всегда, после свежих отчётов)
    finalized += (await finalizeWeeks(store, d.cutover, logger)).length;
    // Суточные, зарегистрированные по снимку и загруженные в шаге 5, входят в RECON недели так же, как в Apps Script.
    if (earlyFinal.length && completedIn.DAILY > 0) {
      for (const wk of earlyFinal) await finalizeOne(store, wk, logger, 'finance_week_recon_refreshed');
    }

    // 7. Аномалии неизменяемого COMPLETE — ERROR прогона (манифест не тронут)
    if (anomalies.length) {
      runErr = `IMMUTABLE_ANOMALY: ${anomalies.join('; ').substring(0, 700)}`;
      logger.error('finance_immutable_anomaly', { count: anomalies.length });
    }
    counters.requests = api.requests;
    const status = runStatusOf(counters, anomalies.length, queueLeft);
    await store.runClose(runId, status, counters, runErr);
    logger.info('finance_run_close', {
      financeRunId: runId, status, ...counters, queueLeft, finalized, elapsedMs: clock.now() - t0,
    });
    return { status, runId, counters, queueLeft, finalized, anomalies, errorMessage: runErr, rowsFetched, rowsLoaded };
  } catch (e) {
    const em = e instanceof Error ? e.message : String(e);
    counters.requests = api.requests;
    logger.error('finance_run_exception', { financeRunId: runId, error: em });
    if (opened) {
      try { await store.runClose(runId, 'ERROR', counters, em); } catch { /* best-effort, как в Apps Script */ }
    }
    return { status: 'ERROR', runId, counters, queueLeft, finalized, anomalies, errorMessage: em, rowsFetched, rowsLoaded };
  }
}

/**
 * wbFinDeepBackfillRegister — регистрирует WEEKLY-отчёты периода (DISCOVERED), НЕ грузит.
 * Загрузка — обычными тиками `finance`. Для недель < cutover действует приоритет canonical.
 */
export async function runBackfillRegister(d: TickDeps, from: string, to: string): Promise<TickResult> {
  const { store, api, clock, logger, runId } = d;
  const counters: RunCounters = { discovered: 0, loaded: 0, errors: 0, requests: 0 };
  await store.runOpen(runId, 'BACKFILL', new Date(clock.now()).toISOString());
  const base = { runId, queueLeft: 0, finalized: 0, rowsFetched: 0, rowsLoaded: 0 };
  try {
    const wl = await api.list('weekly', from, to);
    if (!wl.ok) {
      await store.runClose(runId, 'ERROR', { ...counters, errors: 1, requests: api.requests }, wl.error);
      return { ...base, status: 'ERROR', counters: { ...counters, errors: 1, requests: api.requests }, anomalies: [], errorMessage: wl.error };
    }
    const reg = await registerReports(store, runId, wl.reports, 'WEEKLY', new Set());
    const status: RunStatus = reg.anomalies.length ? 'ERROR' : 'OK';
    const c = { discovered: reg.registered, loaded: 0, errors: reg.anomalies.length ? 1 : 0, requests: api.requests };
    await store.runClose(runId, status, c, reg.anomalies.join('; '));
    logger.info('finance_backfill_registered', { found: wl.reports.length, registered: reg.registered, status });
    return { ...base, status, counters: c, anomalies: reg.anomalies, errorMessage: reg.anomalies.join('; ') };
  } catch (e) {
    const em = e instanceof Error ? e.message : String(e);
    try { await store.runClose(runId, 'ERROR', { ...counters, requests: api.requests }, em); } catch { /* best-effort */ }
    return { ...base, status: 'ERROR', counters, anomalies: [], errorMessage: em };
  }
}
