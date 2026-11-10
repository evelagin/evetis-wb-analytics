/**
 * Пять источников рекламы — порт per-source загрузчиков Apps Script с той же семантикой статусов:
 *   loadCampaigns   ← loadWbAdsCampaignsRaw   (WbAdsRawLoader.gs:462)
 *   loadFullstats   ← loadWbAdsFullstatsRaw   (WbAdsRawLoader.gs:552) + resilient 50 → 10 → 1
 *   loadCosts       ← loadWbAdsCostsRaw       (WbAdsRawLoader.gs:657) + commit-marker окна
 *   loadQueryBids   ← loadWbAdsQueryBidsRaw   (WbAdsQueryBids.gs:82)
 *   loadQueryStats  ← loadWbAdsQueryStatsRaw  (Wbadsquerystats.gs:94)
 * Каждый источник ловит СВОИ ошибки и возвращает статус, как в эталоне: один упавший advertId,
 * окно или пачка не роняют прогон. Строки WB_ADS_STATUS (лист Google) заменены структурным логом.
 * Исключение — отказ авторизации `WB_ADS_AUTH` (401/403, http.ts): его источник НЕ глотает (rethrowAuth),
 * прогон падает с этим кодом — токен один на все методы, следующий источник получил бы тот же отказ.
 *
 * 🔴 Ни одного write-вызова в WB: GET count/adverts/fullstats/upd и POST get-bids/normquery/stats —
 *    чтение (POST только потому, что список пар передаётся телом).
 */
import type { Logger } from '../../logging.js';
import type { AdsHttpResponse, SleepFn } from './http.js';
import { clip, isAdsAuthError } from './http.js';
import type { AdsSink, AdsReader } from './bq.js';
import type { AdsConfig } from './targets.js';
import type { SourceResult } from './status.js';
import {
  WB_ADS_IDS_BATCH, WB_ADS_ADVERTS_PAUSE_MS, WB_ADS_UPD_PAUSE_MS, WB_ADS_REQUEST_RESERVE_MS, WB_ADV_RAW_MAX_DAYS,
  WB_ADS_QBIDS_MAX_ITEMS, WB_ADS_QBIDS_PAUSE_MS, WB_ADS_QBIDS_METHOD,
  WB_ADS_QSTATS_MAX_ITEMS, WB_ADS_QSTATS_PAUSE_MS, WB_ADS_QSTATS_METHOD, WB_ADS_QSTATS_SUSPICIOUS_CAPS,
  CAMPAIGNS_COLUMNS, CAMPAIGN_STATS_COLUMNS, BOOSTER_STATS_COLUMNS, COSTS_COLUMNS, COSTS_RUNS_COLUMNS,
  QUERY_BIDS_COLUMNS, QUERY_BIDS_RUNS_COLUMNS, QUERY_STATS_COLUMNS, QUERY_STATS_RUNS_COLUMNS,
} from './constants.js';
import {
  campaignIndexFromCount, statsIndexFromCount, orderFullstatsIds, advertsList, advertDetailId, campaignRow,
  fullstatsCampaigns, flattenFullstats, noStatsRow, updItems, costRow, costsRunLogRow,
  uniqueIdsFromCount, addCpmManualPairs, bidRows, flattenQueryStats, round2, chunk,
  type RawRow, type CpmManualPairs,
} from './normalize.js';
import { splitPeriod, dayCount } from './time.js';

export interface HttpLike {
  request(method: 'get' | 'post', url: string, payload?: unknown, notAfterMs?: number): Promise<AdsHttpResponse>;
}

export interface AdsDeps {
  http: HttpLike;
  sink: AdsSink;
  reader: AdsReader;
  sleep: SleepFn;
  nowMs: () => number;
  /** 'yyyy-MM-dd HH:mm:ss' МСК (wbAdsNow_). */
  nowTs: () => string;
  logger: Logger;
  cfg: AdsConfig;
  /** Абсолютный дедлайн обработчика (мс эпохи): новые HTTP-вызовы после него не начинаются. */
  deadlineMs?: number;
}

export const ADS_DEADLINE = 'ADS_DEADLINE';

/**
 * Обёртка HTTP с дедлайном: вызов, который уже не успевает уложиться (сейчас + резерв одного ответа
 * > дедлайна), НЕ начинается — бросается ошибка ADS_DEADLINE. Источник ловит её, как любую ошибку
 * WB, и возвращает свой статус (FAILED/PARTIAL), поэтому исход прогона фиксируется в журнале
 * до таймаута Job'а, а не обрывается убийством execution.
 */
export function withDeadline(http: HttpLike, nowMs: () => number, deadlineMs: number): HttpLike {
  return {
    request: (method, url, payload) => {
      if (nowMs() + WB_ADS_REQUEST_RESERVE_MS > deadlineMs) {
        return Promise.reject(Object.assign(new Error(`дедлайн прогона: ${method.toUpperCase()} ${url.split('?')[0]} не начат`), { code: ADS_DEADLINE }));
      }
      return http.request(method, url, payload, deadlineMs); // повторы тоже не переживут дедлайн
    },
  };
}

const errMsg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/** Отказ авторизации пробрасывается мимо статусов источника (см. шапку). */
function rethrowAuth(e: unknown): void {
  if (isAdsAuthError(e)) throw e;
}

// ═══════════════════════════════════════
// CAMPAIGNS
// ═══════════════════════════════════════

export async function loadCampaigns(d: AdsDeps, rid: string): Promise<SourceResult> {
  const src = 'raw_campaigns';
  try {
    const cResp = await d.http.request('get', `${d.cfg.host}/adv/v1/promotion/count`);
    if (!cResp.ok) {
      d.logger.error('ads_source', { source: src, status: 'FAILED', http: cResp.code, error: `count HTTP ${cResp.code}: ${clip(cResp.body)}` });
      return { source: src, status: 'FAILED', rows: 0 };
    }
    const idx = campaignIndexFromCount(cResp.json);
    const detailMap: Record<number, unknown> = {};
    let detailHttp: number | '' = '';
    const chunks = chunk(idx.ids, WB_ADS_IDS_BATCH);
    for (let ch = 0; ch < chunks.length; ch++) {
      if (ch > 0) await d.sleep(WB_ADS_ADVERTS_PAUSE_MS);
      const aResp = await d.http.request('get', `${d.cfg.host}/api/advert/v2/adverts?ids=${chunks[ch]!.join(',')}`);
      detailHttp = aResp.code;
      if (!aResp.ok || !aResp.json) continue; // пачка не доехала → строки count_only
      for (const adv of advertsList(aResp.json)) {
        const advId = advertDetailId(adv);
        if (advId != null) detailMap[Number(advId)] = adv;
      }
    }
    const rows: RawRow[] = idx.ids.map((id) => campaignRow(rid, d.nowTs(), id, idx.meta[id], detailMap[id] ?? null));
    const written = await d.sink.append(d.cfg.tables.campaigns, CAMPAIGNS_COLUMNS, rows);
    d.logger.info('ads_source', {
      source: src, status: 'OK', rows: written, http: `${cResp.code}/${detailHttp}`,
      campaigns: idx.ids.length, withDetail: Object.keys(detailMap).length,
    });
    return { source: src, status: 'OK', rows: written };
  } catch (e) {
    rethrowAuth(e);
    d.logger.error('ads_source', { source: src, status: 'FAILED', error: `Исключение: ${errMsg(e)}` });
    return { source: src, status: 'FAILED', rows: 0 };
  }
}

// ═══════════════════════════════════════
// FULLSTATS (resilient 50 → 10 → single)
// ═══════════════════════════════════════

interface FsCtx {
  collected: unknown[];
  noStats: Record<number, true>;
  failures: Array<{ advertId: number; code: number }>;
  skipped: number[];
  stopped: boolean;
  httpCalls: number;
  lastCode: number | '';
  deadline: number;
}

export async function loadFullstats(d: AdsDeps, rid: string, from: string, to: string, t0: number): Promise<SourceResult> {
  const src = 'raw_fullstats';
  try {
    // Свой count, как wbAdsFetchCampaigns_. Лишний «sample»-вызов adverts эталона не повторяется:
    // для fullstats его результат не использовался (см. runbook, отличие D4).
    const cResp = await d.http.request('get', `${d.cfg.host}/adv/v1/promotion/count`);
    const c = cResp.ok ? statsIndexFromCount(cResp.json) : { advertIds: [], statsAdvertIds: [], statusById: {} };
    const statsIds = orderFullstatsIds(c.statsAdvertIds, c.statusById);
    if (!statsIds.length) {
      d.logger.warn('ads_source', {
        source: src, status: 'SKIPPED', http: cResp.code, campaigns: c.advertIds.length,
        error: cResp.ok ? 'Нет кампаний в статусах 7/9/11 — fullstats не запрашивался' : `count HTTP ${cResp.code}: ${clip(cResp.body)}`,
      });
      return { source: src, status: 'SKIPPED', rows: 0 };
    }

    const deadline = Math.min(t0 + d.cfg.fullstatsBudgetMs, d.deadlineMs ?? Number.POSITIVE_INFINITY);
    const windows = splitPeriod(from, to, WB_ADV_RAW_MAX_DAYS);
    let totalStat = 0, totalBoost = 0, totalNoStats = 0, totalSkipped = 0, totalFail = 0;
    let lastCode: number | '' = '';
    let windowsDone = 0;

    for (const w of windows) {
      const ctx: FsCtx = { collected: [], noStats: {}, failures: [], skipped: [], stopped: false, httpCalls: 0, lastCode: '', deadline };
      for (const batch of chunk(statsIds, WB_ADS_IDS_BATCH)) await fullstatsTryLevel(d, batch, w.from, w.to, ctx);
      lastCode = ctx.lastCode;

      const flat = flattenFullstats(ctx.collected, rid, w.from, w.to, d.nowTs);
      totalStat += await d.sink.append(d.cfg.tables.campaignStats, CAMPAIGN_STATS_COLUMNS, flat.statRows);
      totalBoost += await d.sink.append(d.cfg.tables.boosterStats, BOOSTER_STATS_COLUMNS, flat.boosterRows);
      // no_stats — маркерные строки (HTTP 200, но без статистики). Порядок — как for-in эталона.
      const noStatsRows = Object.keys(ctx.noStats).map((nid) => noStatsRow(rid, d.nowTs(), Number(nid), w.from, w.to));
      totalNoStats += await d.sink.append(d.cfg.tables.campaignStats, CAMPAIGN_STATS_COLUMNS, noStatsRows);

      totalFail += ctx.failures.length;
      totalSkipped += ctx.skipped.length;
      windowsDone++;
      if (ctx.stopped) {
        d.logger.warn('ads_fullstats_stopped', {
          window: `${w.from}…${w.to}`, skipped: ctx.skipped.length, skippedSample: ctx.skipped.slice(0, 20).join(','),
          reason: 'тайм-бюджет fullstats',
        });
        break;
      }
    }

    const st = (totalFail > 0 || totalSkipped > 0) ? 'PARTIAL' : 'OK';
    d.logger.info('ads_source', {
      source: src, status: st, rows: totalStat, http: lastCode, campaigns: statsIds.length,
      boosterRows: totalBoost, noStats: totalNoStats, failedSingle: totalFail, skipped: totalSkipped,
      windows: `${windowsDone}/${windows.length}`, order: '9,11,7',
    });
    return { source: src, status: st, rows: totalStat };
  } catch (e) {
    rethrowAuth(e);
    d.logger.error('ads_source', { source: src, status: 'FAILED', error: `Исключение: ${errMsg(e)}` });
    return { source: src, status: 'FAILED', rows: 0 };
  }
}

/** wbAdsFullstatsTryLevel_: при ошибке дробит 50 → 10 → single; упавший single фиксируется и не роняет загрузку. */
async function fullstatsTryLevel(d: AdsDeps, ids: number[], from: string, to: string, ctx: FsCtx): Promise<void> {
  if (!ids.length) return;
  if (ctx.stopped) { ctx.skipped.push(...ids); return; }
  const pauseNeeded = ctx.httpCalls > 0 ? d.cfg.fullstatsPauseMs : 0;
  // Операция не начинается, если пауза + один ответ WB уже не помещаются в бюджет (STEP 5A).
  const now = d.nowMs();
  if (now >= ctx.deadline || now + pauseNeeded + WB_ADS_REQUEST_RESERVE_MS > ctx.deadline) {
    ctx.stopped = true;
    ctx.skipped.push(...ids);
    return;
  }
  if (ctx.httpCalls > 0) await d.sleep(d.cfg.fullstatsPauseMs);
  ctx.httpCalls++;

  const resp = await d.http.request('get', `${d.cfg.host}/adv/v3/fullstats?ids=${ids.join(',')}&beginDate=${from}&endDate=${to}`);
  ctx.lastCode = resp.code;

  if (resp.ok) {
    const camps = fullstatsCampaigns(resp.json) as Array<{ advertId?: unknown; advertID?: unknown }>;
    if (camps.length) {
      const present: Record<number, true> = {};
      for (const cmp of camps) {
        const aid = cmp.advertId || cmp.advertID; // null-элемент роняет источник в FAILED, как в эталоне
        if (aid != null) present[Number(aid)] = true;
        ctx.collected.push(cmp);
      }
      for (const id of ids) if (!present[Number(id)]) ctx.noStats[Number(id)] = true;
    } else {
      for (const id of ids) ctx.noStats[Number(id)] = true; // HTTP 200, пусто → no_stats (не ошибка)
    }
    return;
  }

  if (ids.length > 10) {
    for (const c10 of chunk(ids, 10)) await fullstatsTryLevel(d, c10, from, to, ctx);
  } else if (ids.length > 1) {
    for (const one of ids) await fullstatsTryLevel(d, [one], from, to, ctx);
  } else {
    ctx.failures.push({ advertId: ids[0]!, code: resp.code });
    d.logger.warn('ads_fullstats_single_failed', { advertId: ids[0], http: resp.code, error: clip(resp.body) });
  }
}

// ═══════════════════════════════════════
// COSTS (/adv/v1/upd) — snapshot-aware producer
// ═══════════════════════════════════════

/**
 * На КАЖДОЕ окно — строка RAW_WB_ADV_COSTS_RUNS (включая пустой успешный ответ и ошибку).
 * Порядок внутри окна: сначала данные, ПОТОМ строка журнала — commit-marker. Ран, упавший между
 * ними, оставит строки, невидимые для V_ADV_COSTS_SNAPSHOT (v1 и v2 одинаково).
 */
export async function loadCosts(d: AdsDeps, rid: string, from: string, to: string, maxDays = WB_ADV_RAW_MAX_DAYS): Promise<SourceResult> {
  const src = 'raw_costs';
  try {
    const windows = splitPeriod(from, to, maxDays > 0 ? maxDays : WB_ADV_RAW_MAX_DAYS);
    let total = 0, failed = 0, journalFailed = 0, emptyOk = 0;
    let lastCode: number | '' = '';
    for (let w = 0; w < windows.length; w++) {
      const win = windows[w]!;
      if (w > 0) await d.sleep(WB_ADS_UPD_PAUSE_MS);
      const tW = d.nowMs();
      const resp = await d.http.request('get', `${d.cfg.host}/adv/v1/upd?from=${win.from}&to=${win.to}`);
      lastCode = resp.code;
      if (!resp.ok) {
        failed++;
        journalFailed += await writeCostsRunLog(d, rid, w, win, resp, [], d.nowMs() - tW, 'ERROR', `upd HTTP ${resp.code}: ${clip(resp.body)}`);
        d.logger.warn('ads_costs_window_failed', { window: `${win.from}…${win.to}`, index: w, http: resp.code });
        continue;
      }
      const rows = updItems(resp.json).map((u) => costRow(rid, d.nowTs(), u, win.from, win.to, w));
      total += await d.sink.append(d.cfg.tables.costs, COSTS_COLUMNS, rows); // данные — ДО маркера
      if (!rows.length) emptyOk++;
      journalFailed += await writeCostsRunLog(d, rid, w, win, resp, rows, d.nowMs() - tW, 'OK', '');
    }
    // Незаписанный маркер — не мелочь: окно без него не попадёт в canonical → прогон не OK.
    const st = (failed > 0 || journalFailed > 0) ? 'PARTIAL' : 'OK';
    d.logger.info('ads_source', {
      source: src, status: st, rows: total, http: lastCode, period: `${from}…${to}`,
      windows: windows.length, failedWindows: failed, emptySuccessWindows: emptyOk, runlogFailed: journalFailed,
    });
    return { source: src, status: st, rows: total, windows: windows.length, empty_success_windows: emptyOk,
      failed_windows: failed, runlog_failed: journalFailed };
  } catch (e) {
    rethrowAuth(e);
    d.logger.error('ads_source', { source: src, status: 'FAILED', error: `Исключение: ${errMsg(e)}` });
    return { source: src, status: 'FAILED', rows: 0 };
  }
}

/** Возвращает 0 при успехе записи маркера, 1 при неудаче (окно становится неавторитетным — fail-closed). */
async function writeCostsRunLog(d: AdsDeps, rid: string, windowIndex: number, win: { from: string; to: string },
  resp: AdsHttpResponse, rows: RawRow[], durationMs: number, status: 'OK' | 'ERROR', errorMessage: string): Promise<number> {
  const row = costsRunLogRow({
    rid, windowIndex, win, httpCode: resp.code, httpOk: resp.ok, rows, durationMs, status, errorMessage,
    completedAt: d.nowTs(), requestedDays: dayCount(win.from, win.to),
  });
  try {
    await d.sink.append(d.cfg.tables.costsRuns, COSTS_RUNS_COLUMNS, [row]);
    d.logger.info('ads_costs_window', {
      window: `${win.from}…${win.to}`, index: windowIndex, status, rows: row.returned_rows,
      days: row.returned_days, outOfWindow: row.rows_out_of_window, durationMs,
    });
    return 0;
  } catch (e) {
    d.logger.error('ads_costs_runlog_failed', { window: `${win.from}…${win.to}`, index: windowIndex, error: errMsg(e) });
    return 1;
  }
}

// ═══════════════════════════════════════
// Ads-3: снимок ставок по кластерам (CPM + manual)
// ═══════════════════════════════════════

interface PairsCollected extends CpmManualPairs {
  countHttp: number | '';
  countOk: boolean;
  idsFound: number;
  advBatchesTotal: number;
  advBatchesOk: number;
}

async function collectCpmManualPairs(d: AdsDeps): Promise<PairsCollected> {
  const out: PairsCollected = { pairs: [], ctx: {}, countHttp: '', countOk: false, idsFound: 0, advBatchesTotal: 0, advBatchesOk: 0 };
  const cResp = await d.http.request('get', `${d.cfg.host}/adv/v1/promotion/count`);
  out.countHttp = cResp.code;
  if (!cResp.ok || !cResp.json) return out; // countOk=false — СБОЙ, не пустота
  out.countOk = true;
  const ids = uniqueIdsFromCount(cResp.json);
  out.idsFound = ids.length;
  if (!ids.length) return out;
  const seenPair: Record<string, 1> = {};
  const chunks = chunk(ids, WB_ADS_IDS_BATCH);
  out.advBatchesTotal = chunks.length;
  for (let ch = 0; ch < chunks.length; ch++) {
    if (ch > 0) await d.sleep(WB_ADS_ADVERTS_PAUSE_MS);
    const resp = await d.http.request('get', `${d.cfg.host}/api/advert/v2/adverts?ids=${chunks[ch]!.join(',')}`);
    if (!resp.ok || !resp.json) continue;
    out.advBatchesOk++;
    addCpmManualPairs(advertsList(resp.json), out, seenPair);
  }
  return out;
}

/** Не бросает (кроме WB_ADS_AUTH): снимок ставок не влияет на итог прогона и журнал 'ads'. */
export async function loadQueryBids(d: AdsDeps, rid: string): Promise<SourceResult> {
  const src = 'raw_query_bids';
  const t0 = d.nowMs();
  const snapshotTs = d.nowTs();
  const log: Record<string, unknown> = {
    load_ts: snapshotTs, run_id: rid, source_method: WB_ADS_QBIDS_METHOD,
    snapshot_ts: snapshotTs, snapshot_date: snapshotTs.substring(0, 10),
    requested_pairs: 0, expected_batches: 0, requested_batches: 0,
    http_status: '', http_success: 'FALSE',
    returned_bid_rows: 0, distinct_pairs_with_bids: 0,
    duration_ms: 0, status: 'ERROR', error_message: '',
  };
  try {
    const collected = await collectCpmManualPairs(d);
    log.requested_pairs = collected.pairs.length;
    if (!collected.pairs.length) {
      const { advBatchesTotal: tot, advBatchesOk: ok } = collected;
      if (!collected.countOk) {
        log.status = 'FAILED';
        log.error_message = `Справочник не доехал: /promotion/count HTTP ${collected.countHttp || 'нет ответа'}`;
      } else if (tot > 0 && ok === 0) {
        log.status = 'FAILED';
        log.error_message = `Справочник не доехал: /adverts не отдал ни одной из ${tot} пачек (${collected.idsFound} кампаний в count)`;
      } else if (ok < tot) {
        log.status = 'FAILED';
        log.error_message = `Справочник неполон (${ok}/${tot} пачек) и пар нет — пустой scope НЕ доказан`;
      } else {
        log.status = 'EMPTY';
        log.error_message = `Валидный пустой scope: среди ${collected.idsFound} кампаний нет ни одной CPM+manual`;
      }
      log.http_status = '';
      log.http_success = 'FALSE';
      return await finishQueryBids(d, src, log, t0, 0);
    }

    const scopeIncomplete = collected.advBatchesOk < collected.advBatchesTotal;
    if (scopeIncomplete) log.error_message = `Справочник неполон: ${collected.advBatchesOk}/${collected.advBatchesTotal} пачек /adverts`;

    const chunks = chunk(collected.pairs, WB_ADS_QBIDS_MAX_ITEMS);
    log.expected_batches = chunks.length;
    let written = 0, failed = 0, anyOk = false;
    let lastCode: number | '' = '';
    const pairsWithBids: Record<string, 1> = {};
    let requested = 0;
    for (let ch = 0; ch < chunks.length; ch++) {
      if (d.nowMs() - t0 > d.cfg.qbidsBudgetMs) {
        log.error_message = `Тайм-бюджет исчерпан на пачке ${ch + 1} из ${chunks.length}`;
        break;
      }
      if (ch > 0) await d.sleep(WB_ADS_QBIDS_PAUSE_MS);
      requested++;
      log.requested_batches = requested;
      const items = chunks[ch]!.map((p) => ({ advert_id: p.advertId, nm_id: p.nmId }));
      const resp = await d.http.request('post', `${d.cfg.host}/adv/v0/normquery/get-bids`, { items });
      lastCode = resp.code;
      if (!resp.ok) {
        failed++;
        log.error_message = `${log.error_message ? `${String(log.error_message)}; ` : ''}пачка ${ch + 1} HTTP ${resp.code}`;
        continue;
      }
      anyOk = true;
      const rows = bidRows(resp.json, rid, snapshotTs, collected.ctx, pairsWithBids);
      if (rows.length) written += await d.sink.append(d.cfg.tables.queryBids, QUERY_BIDS_COLUMNS, rows);
    }
    log.http_status = String(lastCode);
    log.http_success = anyOk ? 'TRUE' : 'FALSE';
    log.returned_bid_rows = written;
    log.distinct_pairs_with_bids = Object.keys(pairsWithBids).length;
    const incomplete = requested < chunks.length;
    if (!anyOk) log.status = 'FAILED';
    else if (failed > 0 || incomplete || scopeIncomplete) log.status = 'PARTIAL';
    else log.status = 'OK'; // в т.ч. при returned_bid_rows = 0
    return await finishQueryBids(d, src, log, t0, written);
  } catch (e) {
    rethrowAuth(e);
    log.status = 'FAILED';
    log.error_message = `Исключение: ${errMsg(e)}`;
    try { return await finishQueryBids(d, src, log, t0, 0); } catch { return { source: src, status: 'FAILED', rows: 0, pairs: 0 }; }
  }
}

/** Run-log пишется ВСЕГДА; его потеря сама по себе делает снимок FAILED. */
async function finishQueryBids(d: AdsDeps, src: string, log: Record<string, unknown>, t0: number, rows: number): Promise<SourceResult> {
  log.duration_ms = d.nowMs() - t0;
  try {
    await d.sink.append(d.cfg.tables.queryBidsRuns, QUERY_BIDS_RUNS_COLUMNS, [log]);
  } catch (e) {
    log.error_message = `${log.error_message ? `${String(log.error_message)}; ` : ''}RUN-LOG НЕ ЗАПИСАН (исходный статус ${String(log.status)})`;
    log.status = 'FAILED';
    d.logger.error('ads_query_bids_runlog_failed', { error: errMsg(e) });
  }
  d.logger.info('ads_source', {
    source: src, status: log.status, pairs: log.requested_pairs, batches: `${String(log.requested_batches)}/${String(log.expected_batches)}`,
    rows: log.returned_bid_rows, pairsWithBids: log.distinct_pairs_with_bids, durationMs: log.duration_ms, note: log.error_message,
  });
  return { source: src, status: String(log.status), rows, pairs: Number(log.requested_pairs) };
}

// ═══════════════════════════════════════
// Ads-2: суточные срезы query-level статистики
// ═══════════════════════════════════════

interface QsState { t0: number; httpCalls: number; wallStopLogged?: boolean }

function qsOutOfBudget(d: AdsDeps, st: QsState): boolean {
  return d.nowMs() - st.t0 > d.cfg.qstatsBudgetMs;
}

const appendMsg = (existing: unknown, msg: string): string => (existing ? `${String(existing)}; ${msg}` : msg);

/** Не бросает (кроме WB_ADS_AUTH). days — явный список суток; обход от свежих к старым (Stage 3A, 19.08.2026). */
export async function loadQueryStats(d: AdsDeps, rid: string, days: string[]): Promise<SourceResult & { days: number; days_ok: number }> {
  const src = 'raw_query_stats';
  const st: QsState = { t0: d.nowMs(), httpCalls: 0 };
  if (!days.length) return { source: src, status: 'EMPTY', rows: 0, days: 0, days_ok: 0 };

  // Телеметрия покрытия одним запросом на окно; недоступность не влияет на статусы.
  let costsByDay: Record<string, number> = {};
  try {
    costsByDay = await d.reader.costsByDay(d.cfg.qstatsCostsView, days[0]!, days[days.length - 1]!);
  } catch (e) {
    d.logger.warn('ads_query_stats_costs_telemetry_unavailable', { error: errMsg(e) });
  }

  const ordered = days.slice().sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
  let totalRows = 0, daysOk = 0, daysPartial = 0, daysFailed = 0, daysEmpty = 0;
  for (let i = 0; i < ordered.length; i++) {
    const day = ordered[i]!;
    let res: { status: string; rows: number };
    try {
      res = await queryStatsDay(d, rid, day, costsByDay[day], st);
    } catch (e) {
      rethrowAuth(e);
      res = { status: 'FAILED', rows: 0 };
      d.logger.error('ads_query_stats_day_exception', { day, error: errMsg(e) });
    }
    totalRows += res.rows;
    if (res.status === 'OK') daysOk++;
    else if (res.status === 'PARTIAL') daysPartial++;
    else if (res.status === 'EMPTY') daysEmpty++;
    else daysFailed++;
    if (qsOutOfBudget(d, st)) {
      const left = ordered.length - i - 1;
      if (left > 0) {
        // Необработанные сутки НЕ получают строку run-log: попытки не было.
        d.logger.warn('ads_query_stats_budget', { notProcessed: left });
        daysFailed += left;
      }
      break;
    }
  }
  let overall = daysFailed > 0 ? 'PARTIAL' : (daysOk > 0 ? 'OK' : (daysEmpty > 0 ? 'EMPTY' : 'PARTIAL'));
  if (daysOk === 0 && daysPartial === 0 && daysEmpty === 0) overall = 'FAILED';
  d.logger.info('ads_source', {
    source: src, status: overall, days: days.length, ok: daysOk, partial: daysPartial, empty: daysEmpty,
    failed: daysFailed, rows: totalRows, http: st.httpCalls, durationMs: d.nowMs() - st.t0,
  });
  return { source: src, status: overall, rows: totalRows, days: days.length, days_ok: daysOk };
}

/** Одни сутки = один срез = одна строка run-log (wbAdsQsProcessDay_). */
async function queryStatsDay(d: AdsDeps, rid: string, day: string, dayCostsRub: number | undefined, st: QsState):
  Promise<{ status: string; rows: number }> {
  const tDay = d.nowMs();
  const nowTs = d.nowTs();
  const log: Record<string, unknown> = {
    load_ts: nowTs, run_id: rid, source_method: WB_ADS_QSTATS_METHOD,
    period_from: day, period_to: day,
    requested_pairs: 0, expected_batches: 0, requested_batches: 0,
    returned_packs: 0, http_status: '', http_success: 'FALSE',
    returned_rows: 0, distinct_pairs_with_rows: 0,
    max_keys_per_pair: 0, rows_with_views: 0,
    scope_pairs: 0, scope_spend_rub: '',
    day_spend_costs_rub: (dayCostsRub != null ? dayCostsRub : ''),
    duration_ms: 0, status: 'FAILED', error_message: '',
  };

  let scope: { pairs: Array<{ advertId: number; nmId: number }>; spendRub: number };
  try {
    scope = await d.reader.scopePairsForDay(d.cfg.qstatsScopeView, day);
  } catch (e) {
    // Недоступность BigQuery — FAILED, а не «пустой scope»: ноль пар значит противоположное.
    log.status = 'FAILED';
    log.error_message = `Scope не получен из BigQuery: ${errMsg(e)}`;
    return finishQueryStatsDay(d, log, tDay, 0);
  }
  log.scope_pairs = scope.pairs.length;
  log.requested_pairs = scope.pairs.length;
  log.scope_spend_rub = round2(scope.spendRub);
  if (!scope.pairs.length) {
    log.status = 'EMPTY';
    log.error_message = `Валидный пустой scope: за ${day} нет ни одной пары advert×nm в V_ADV_CAMPAIGN_STATS`;
    return finishQueryStatsDay(d, log, tDay, 0);
  }

  const chunks = chunk(scope.pairs, WB_ADS_QSTATS_MAX_ITEMS);
  log.expected_batches = chunks.length;
  let written = 0, failed = 0, anyOk = false, maxKeys = 0, rowsWithViews = 0, requested = 0, packs = 0;
  let lastCode: number | '' = '';
  const pairsWithRows: Record<string, 1> = {};
  for (let ch = 0; ch < chunks.length; ch++) {
    if (qsOutOfBudget(d, st)) {
      log.error_message = appendMsg(log.error_message, `тайм-бюджет исчерпан на пачке ${ch + 1} из ${chunks.length}`);
      break;
    }
    if (st.httpCalls > 0) await d.sleep(WB_ADS_QSTATS_PAUSE_MS);
    st.httpCalls++;
    requested++;
    const items = chunks[ch]!.map((p) => ({ advert_id: p.advertId, nm_id: p.nmId }));
    const resp = await d.http.request('post', `${d.cfg.host}/adv/v0/normquery/stats`, { from: day, to: day, items });
    lastCode = resp.code;
    if (!resp.ok) {
      failed++;
      log.error_message = appendMsg(log.error_message, `пачка ${ch + 1} HTTP ${resp.code}`);
      continue;
    }
    anyOk = true;
    const flat = flattenQueryStats(resp.json, rid, day, nowTs);
    packs += flat.packs;
    if (flat.maxKeys > maxKeys) maxKeys = flat.maxKeys;
    rowsWithViews += flat.rowsWithViews;
    for (const k of flat.pairsWithRows) pairsWithRows[k] = 1;
    if (flat.rows.length) written += await d.sink.append(d.cfg.tables.queryStats, QUERY_STATS_COLUMNS, flat.rows);
  }
  log.requested_batches = requested;
  log.returned_packs = packs;
  log.http_status = String(lastCode);
  log.http_success = anyOk ? 'TRUE' : 'FALSE';
  log.returned_rows = written;
  log.distinct_pairs_with_rows = Object.keys(pairsWithRows).length;
  log.max_keys_per_pair = maxKeys;
  log.rows_with_views = rowsWithViews;

  const batchesIncomplete = requested < chunks.length;
  const packsMismatch = packs !== scope.pairs.length; // СТРОГОЕ равенство: и недостача, и избыток — PARTIAL
  if (!anyOk) {
    log.status = 'FAILED';
    log.error_message = appendMsg(log.error_message, 'ни одна пачка не вернула HTTP 200');
  } else if (failed > 0 || batchesIncomplete || packsMismatch) {
    log.status = 'PARTIAL';
    if (packsMismatch) {
      log.error_message = appendMsg(log.error_message,
        `нарушен контракт мощности: пакетов ${packs} при ${scope.pairs.length} запрошенных парах${packs > scope.pairs.length ? ' (ИЗБЫТОК)' : ' (недостача)'}`);
    }
  } else {
    log.status = 'OK'; // в том числе при returned_rows = 0
  }
  if (maxKeys > 0 && WB_ADS_QSTATS_SUSPICIOUS_CAPS.indexOf(maxKeys) >= 0) {
    d.logger.warn('ads_query_stats_suspicious_cap', { day, maxKeys });
  }
  return finishQueryStatsDay(d, log, tDay, written);
}

async function finishQueryStatsDay(d: AdsDeps, log: Record<string, unknown>, tDay: number, rows: number): Promise<{ status: string; rows: number }> {
  log.duration_ms = d.nowMs() - tDay;
  try {
    await d.sink.append(d.cfg.tables.queryStatsRuns, QUERY_STATS_RUNS_COLUMNS, [log]);
  } catch (e) {
    log.error_message = appendMsg(log.error_message, `RUN-LOG НЕ ЗАПИСАН (исходный статус ${String(log.status)})`);
    log.status = 'FAILED';
    d.logger.error('ads_query_stats_runlog_failed', { day: log.period_from, error: errMsg(e) });
  }
  d.logger.info('ads_query_stats_day', {
    day: log.period_from, status: log.status, pairs: log.requested_pairs,
    batches: `${String(log.requested_batches)}/${String(log.expected_batches)}`, packs: log.returned_packs,
    rows: log.returned_rows, durationMs: log.duration_ms, note: log.error_message,
  });
  return { status: String(log.status), rows };
}
