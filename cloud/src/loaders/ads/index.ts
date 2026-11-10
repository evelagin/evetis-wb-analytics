/**
 * M3 — реклама WB в Cloud Run. Три загрузчика:
 *
 *   `ads`              ← критичная часть runWbAdsDaily (Wbadsdaily.gs): campaigns → costs (D−14…D−1) →
 *                        fullstats (D−7…D−1). Журнал — LOADER_RUNS, loader_name='ads', logical_period = D−1
 *                        МСК: ту же роль играет INGEST_RUNS 'ads'; freshness-гейт витрины
 *                        (cloud/src/loaders/mart/bq.ts) видит строку через V_INGEST_HEARTBEAT (только prod).
 *                        Catch-up = несколько окон Scheduler: lease cli пропускает COMPLETE
 *                        (SKIP_COMPLETE), свежий STARTED (SKIP_RUNNING) и повторяет ERROR/зависший.
 *   `ads-query`        ← необязательная часть runWbAdsDaily: Ads-3 (снимок ставок) и Ads-2 (query stats
 *                        D−7…D−1). В эталоне она шла ПОСЛЕ ingestFinalizeByStatus_ (Wbadsdaily.gs:212-231),
 *                        чтобы таймаут на хвосте не оставил heartbeat 'ads' незакрытым. Здесь та же гарантия
 *                        дана отдельным загрузчиком со своим loader_name и Job'ом: журнал 'ads' закрывается,
 *                        не дожидаясь хвоста. Стартует, только когда последняя попытка 'ads' за тот же D−1
 *                        в том же контуре терминальна (COMPLETE или ERROR — эталон гонял хвост в обоих).
 *   `ads-costs-audit`  ← loadWbAdsCostsAudit (WbAdsRawLoader.gs:405), ни разу не запускался в Apps Script.
 *                        ADSAUDIT_-ран расходов D−46…D−8 окнами по 30 суток.
 *
 * У каждого обработчика дедлайн (AdsConfig.criticalDeadlineMs / queryDeadlineMs): после него новые
 * HTTP-вызовы не начинаются, источник отдаёт свой статус, и исход пишется в журнал до таймаута Job'а.
 *
 * Итог `ads` (wbAdsOverallStatus_): OK/STALE → handler возвращается → cli пишет COMPLETE;
 * PARTIAL/ERROR → LoaderError('ADS_PARTIAL'|'ADS_ERROR') → ERROR с тем же кодом, что ingestFinalizeByStatus_.
 *
 * 🔴 Реклама здесь — сырьё для АТРИБУЦИИ (fullstats → MART_SKU_DAILY.ad_spend) и для БИЛЛИНГА
 *    (upd → V_ADV_COSTS_SNAPSHOT → FACT_ADS_COSTS_DAILY, FIN CONTRACT V2). Загрузчик их не смешивает
 *    и не пересчитывает: пишет оба RAW как есть, дальше — вью и витрина.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { AdsHttp } from './http.js';
import { AdsBq } from './bq.js';
import { loadAdsConfig, assertAdsTargets, type AdsConfig, type AdsTableKey } from './targets.js';
import { loadCampaigns, loadCosts, loadFullstats, loadQueryBids, loadQueryStats, withDeadline, type AdsDeps } from './sources.js';
import { overallStatus, isJournalSuccess, isStale, type SourceResult } from './status.js';
import { addDays, daysOf, fullstatsRange, mskCompact, mskDate, mskNow, rangeBack } from './time.js';
import {
  WB_ADS_COSTS_OPERATIONAL_DAYS, WB_ADS_COSTS_AUDIT_TO_DAYS, WB_ADS_COSTS_AUDIT_WINDOW_DAYS,
  WB_ADS_DAILY_STALE_DAYS, WB_ADS_QSTATS_WINDOW_DAYS,
} from './constants.js';

/** Таблицы, которые пишет каждый загрузчик, — проверяются ВСЕ до первого вызова WB. */
export const ADS_DAILY_KEYS: readonly AdsTableKey[] = ['campaigns', 'campaignStats', 'boosterStats', 'costs', 'costsRuns'];
export const ADS_QUERY_KEYS: readonly AdsTableKey[] = ['queryBids', 'queryBidsRuns', 'queryStats', 'queryStatsRuns'];
export const ADS_AUDIT_KEYS: readonly AdsTableKey[] = ['costs', 'costsRuns'];

/** Внешние зависимости обработчика (тесты подменяют все). */
export interface AdsRuntime {
  env: Record<string, string | undefined>;
  getToken: (projectId: string, secret: string) => Promise<string>;
  makeDeps: (ctx: LoaderContext, cfg: AdsConfig, token: string, runId: string) => AdsDeps;
  now: () => Date;
}

export const defaultRuntime: AdsRuntime = {
  env: process.env,
  getToken: (projectId, secret) => new SecretsClient(projectId).access(secret),
  makeDeps: (ctx, cfg, token, runId) => {
    const c = ctx.config;
    const bq = new AdsBq(c.projectId, c.bqLocation, c.rawDataset, c.environment, runId);
    return {
      http: new AdsHttp(token, { timeoutMs: c.wbHttpTimeoutMs, logger: ctx.logger }),
      sink: bq,
      reader: bq,
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      nowMs: () => Date.now(),
      nowTs: () => mskNow(new Date()),
      logger: ctx.logger,
      cfg,
    };
  },
  now: () => new Date(),
};

/**
 * run_id RAW — формат Apps Script (`ADSRAW_yyyyMMdd_HHmmss_<суффикс>`), чтобы детекторы
 * (`run_id LIKE 'ADSRAW_%'`, sql/ops/step5a_detectors.sql) и исключения по префиксу в
 * V_ADV_COSTS_UNION_PREBOOTSTRAP работали без правок. Суффикс `CR…` отличает строки Cloud Run.
 */
export function adsRunId(prefix: 'ADSRAW_' | 'ADSAUDIT_', now: Date, executionId: string, rnd: () => number = Math.random, tag = ''): string {
  const tail = executionId.replace(/[^A-Za-z0-9]/g, '').slice(-5) || Math.floor(rnd() * 0xffff).toString(16).padStart(4, '0');
  return `${prefix}${mskCompact(now)}_CR${tag}${tail}`;
}

/** Зависимости с дедлайном обработчика: HTTP после него не начинается (withDeadline). */
function bounded(d: AdsDeps, deadlineMs: number): AdsDeps {
  return { ...d, deadlineMs, http: withDeadline(d.http, d.nowMs, deadlineMs) };
}

async function token(rt: AdsRuntime, projectId: string, cfg: AdsConfig): Promise<string> {
  try {
    return await rt.getToken(projectId, cfg.secret);
  } catch (e) {
    // Эквивалент BLOCKED всех источников в Apps Script (нет токена) → итог ERROR.
    throw new LoaderError(`Нет токена WB Promotion (${cfg.secret}): ${e instanceof Error ? e.message : String(e)}`, 'ADS_TOKEN_UNAVAILABLE', { cause: e });
  }
}

export function makeAdsLoader(rt: AdsRuntime = defaultRuntime) {
  return async function adsLoader(ctx: LoaderContext): Promise<LoaderResult> {
    const { config, logger, logicalPeriod } = ctx;
    const cfg = loadAdsConfig(rt.env);
    assertAdsTargets(config.environment, cfg, ADS_DAILY_KEYS); // ДО WB, Secret Manager и BigQuery

    const fsRange = fullstatsRange(logicalPeriod);
    const todayMsk = addDays(logicalPeriod, 1);
    const costsRange = rangeBack(todayMsk, WB_ADS_COSTS_OPERATIONAL_DAYS, 1);

    if (rt.env.DRY_RUN === '1') { // ровно как cli.ts:159
      logger.info('ads_dry_run', { logicalPeriod, fullstats: fsRange, costs: costsRange, tables: cfg.tables });
      return { rowsFetched: 0, rowsLoaded: 0 };
    }

    const tok = await token(rt, config.projectId, cfg);
    const startedAt = rt.now();
    const runId = adsRunId('ADSRAW_', startedAt, config.executionId);
    const d0 = rt.makeDeps(ctx, cfg, tok, runId);
    const t0 = d0.nowMs();
    const d = bounded(d0, t0 + cfg.criticalDeadlineMs);
    logger.info('ads_run_start', { runId, logicalPeriod, fullstats: fsRange, costs: costsRange, deadlineMs: cfg.criticalDeadlineMs });

    let results: SourceResult[];
    let overall: ReturnType<typeof overallStatus>;
    let note: string;
    let total: number;
    try {
      results = [];
      results.push(await loadCampaigns(d, runId));                                // DIM, дёшево
      results.push(await loadCosts(d, runId, costsRange.from, costsRange.to));    // своё окно D−14…D−1
      results.push(await loadFullstats(d, runId, fsRange.from, fsRange.to, t0));  // тяжёлый — последним

      let maxDate = '?';
      let stale: boolean | null = null;
      try {
        maxDate = await d.reader.maxStatsDate(cfg.tables.campaignStats);
        stale = isStale(mskDate(rt.now()), maxDate, WB_ADS_DAILY_STALE_DAYS);
      } catch (e) {
        logger.warn('ads_freshness_unavailable', { error: e instanceof Error ? e.message : String(e) });
      }
      overall = overallStatus(results, stale);
      total = results.reduce((a, x) => a + (x.rows || 0), 0);
      const summary = results.map((x) => `${x.source}=${x.status}(${x.rows})`).join(' | ');
      note = `${summary} | fullstats_max=${maxDate || ''} | ${((d.nowMs() - t0) / 1000).toFixed(1)}c`;
      logger.info('ads_run_critical_done', { runId, overall, note, rows: total });
    } catch (e) {
      throw new LoaderError(`ADS_EXCEPTION: ${e instanceof Error ? e.message : String(e)}`, 'ADS_EXCEPTION', { cause: e });
    }

    // Ads-3/Ads-2 здесь НЕ вызываются: это загрузчик `ads-query` (свой Job и loader_name), поэтому
    // журнал 'ads' закрывается сразу после критичной части — как ingestFinalizeByStatus_ до хвоста в эталоне.
    if (!isJournalSuccess(overall)) throw new LoaderError(note, `ADS_${overall}`);
    logger.info('ads_run_done', { runId, overall, rows: total, durationMs: d.nowMs() - t0 });
    return { rowsFetched: total, rowsLoaded: total };
  };
}

/**
 * `ads-query` — Ads-3 и Ads-2. Наружу статусы источников не выносит (как в эталоне, где они не входили в
 * heartbeat): COMPLETE, если хвост отработал без исключения, даже при FAILED/PARTIAL отдельного источника —
 * повторять раз за разом невосстановимый снимок ставок незачем, а Ads-2 добирается следующими сутками.
 * ERROR — только «рано» (ADS_QUERY_NOT_READY: 'ads' за D−1 ещё не завершён) и сбой инфраструктуры:
 * тогда следующее окно Scheduler повторит.
 */
export function makeAdsQueryLoader(rt: AdsRuntime = defaultRuntime) {
  return async function adsQueryLoader(ctx: LoaderContext): Promise<LoaderResult> {
    const { config, logger, logicalPeriod } = ctx;
    const cfg = loadAdsConfig(rt.env);
    assertAdsTargets(config.environment, cfg, ADS_QUERY_KEYS);
    const qsDays = daysOf(addDays(logicalPeriod, -(WB_ADS_QSTATS_WINDOW_DAYS - 1)), logicalPeriod);
    if (rt.env.DRY_RUN === '1') { // ровно как cli.ts:159
      logger.info('ads_query_dry_run', { logicalPeriod, qstatsDays: qsDays, tables: cfg.tables });
      return { rowsFetched: 0, rowsLoaded: 0 };
    }
    // Порядок эталона: хвост — ПОСЛЕ критичной части того же D−1 (scope Ads-2 читает свежий fullstats).
    const probe = rt.makeDeps(ctx, cfg, '', 'ADS_QUERY_PROBE');
    const latest = await probe.reader.adsAttempt(config.manifestTable, config.environment, logicalPeriod);
    if (latest !== 'COMPLETE' && latest !== 'ERROR') {
      throw new LoaderError(`'ads' за ${logicalPeriod} ещё не завершён (последняя попытка: ${latest ?? 'нет'}) — хвост рано`, 'ADS_QUERY_NOT_READY');
    }
    const tok = await token(rt, config.projectId, cfg);
    const runId = adsRunId('ADSRAW_', rt.now(), config.executionId, Math.random, 'Q');
    const d0 = rt.makeDeps(ctx, cfg, tok, runId);
    const d = bounded(d0, d0.nowMs() + cfg.queryDeadlineMs);
    logger.info('ads_query_start', { runId, logicalPeriod, adsAttempt: latest, deadlineMs: cfg.queryDeadlineMs });
    let rows = 0;
    // Ads-3 раньше Ads-2: снимок ставок невосстановим, срез статистики восстановим.
    if (cfg.enabledQueryBids) rows += (await loadQueryBids(d, runId)).rows;
    if (cfg.enabledQueryStats) rows += (await loadQueryStats(d, runId, qsDays)).rows;
    return { rowsFetched: rows, rowsLoaded: rows };
  };
}

export function makeAdsCostsAuditLoader(rt: AdsRuntime = defaultRuntime) {
  return async function adsCostsAuditLoader(ctx: LoaderContext): Promise<LoaderResult> {
    const { config, logger, logicalPeriod } = ctx;
    const cfg = loadAdsConfig(rt.env);
    assertAdsTargets(config.environment, cfg, ADS_AUDIT_KEYS);
    // Под снапшотом v1 краевое прочтение аудита (первый день каждого рана) становится каноническим и
    // ЗАНИЖАЕТ дату в P&L (docs/ops/ADS_COSTS_UTC_EDGE_2026-10-21.md). В prod — только после ACK v2.
    if (config.environment === 'prod' && !cfg.auditEnabled) {
      throw new LoaderError('Аудит расходов в prod выключен: нужен ADS_COSTS_AUDIT_ENABLED=1 после применения snapshot v2 (ACK ADS-UTC-EDGE)', 'ADS_AUDIT_DISABLED');
    }
    const r = rangeBack(logicalPeriod, cfg.auditFromDays, WB_ADS_COSTS_AUDIT_TO_DAYS);
    if (rt.env.DRY_RUN === '1') { // ровно как cli.ts:159
      logger.info('ads_audit_dry_run', { logicalPeriod, range: r, tables: { costs: cfg.tables.costs, runs: cfg.tables.costsRuns } });
      return { rowsFetched: 0, rowsLoaded: 0 };
    }
    const tok = await token(rt, config.projectId, cfg);
    const runId = adsRunId('ADSAUDIT_', rt.now(), config.executionId);
    const d0 = rt.makeDeps(ctx, cfg, tok, runId);
    const d = bounded(d0, d0.nowMs() + cfg.queryDeadlineMs); // timeout Job'а аудита 900 c
    logger.info('ads_audit_start', { runId, range: r, windowDays: WB_ADS_COSTS_AUDIT_WINDOW_DAYS });
    const res = await loadCosts(d, runId, r.from, r.to, WB_ADS_COSTS_AUDIT_WINDOW_DAYS);
    if (res.status !== 'OK') throw new LoaderError(`аудит расходов ${r.from}…${r.to}: ${res.status}`, `ADS_AUDIT_${res.status}`);
    return { rowsFetched: res.rows, rowsLoaded: res.rows };
  };
}

export const adsLoader = makeAdsLoader();
export const adsQueryLoader = makeAdsQueryLoader();
export const adsCostsAuditLoader = makeAdsCostsAuditLoader();
