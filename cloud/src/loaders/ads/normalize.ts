/**
 * Нормализация ответов WB Advert API → строки RAW. ПОРТ БЕЗ ИЗМЕНЕНИЯ СЕМАНТИКИ:
 *   wbAdvCampaignRow_, wbAdvCollectNmIds_, wbAdvFlattenFullstats_, wbAdvCampaignStatNoStatsRow_,
 *   wbAdvCostRow_, wbAdsCostsWriteRunLog_ (WbAdsRawLoader.gs), wbAdsQsFlatten_ (Wbadsquerystats.gs),
 *   строки снимка ставок (WbAdsQueryBids.gs), wbAdvBqAppendRows_ (Wbadsbigquery.gs:174).
 *
 * Разбор защитный («defensive по ключам»), как в эталоне: у WB встречаются и snake_case, и camelCase,
 * и разные обёртки. Поэтому здесь сознательно нетипизированный JSON — порядок проверок ключей и
 * семантика `||` / `!= null` перенесены дословно; именно они определяют содержимое колонок.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
import { WB_ADV_RAW_JSON_MAX } from './constants.js';

export type RawRow = Record<string, unknown>;
export type BqRow = Record<string, string>;

/** Безопасный JSON.stringify с обрезкой под лимит ячейки (wbAdvRawJson_). */
export function rawJson(obj: unknown): string {
  try {
    const s = JSON.stringify(obj);
    if (s == null) return '';
    return s.length > WB_ADV_RAW_JSON_MAX ? s.substring(0, WB_ADV_RAW_JSON_MAX) : s;
  } catch {
    return '';
  }
}

/**
 * Строка для load-job (wbAdvBqAppendRows_): '' / null / undefined НЕ пишутся (→ NULL в BigQuery),
 * всё прочее — String(v). Неизвестная колонка — ошибка программы, а не молчаливый пропуск
 * (Apps Script грузит с ignoreUnknownValues=true; здесь контракт строже — fail-closed).
 */
export function toBqRow(obj: RawRow, columns: readonly string[]): BqRow {
  const allowed = new Set(columns);
  const out: BqRow = {};
  for (const k of Object.keys(obj)) {
    if (!allowed.has(k)) throw new Error(`toBqRow: колонка '${k}' отсутствует в схеме RAW`);
    const v = obj[k];
    if (v === '' || v === null || v === undefined) continue;
    out[k] = typeof v === 'string' ? v : String(v);
  }
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// promotion/count → список id
// ─────────────────────────────────────────────────────────────────────────

/** Группы ответа /adv/v1/promotion/count (обе формы обёртки). */
export function countGroups(json: any): any[] {
  return (json && (json.adverts || (json.data && json.data.adverts))) || [];
}

export interface CampaignIndex {
  ids: number[];
  meta: Record<number, { type: unknown; status: unknown; changeTime: unknown }>;
}

/** loadWbAdsCampaignsRaw, шаг 1: уникальные id + type/status/changeTime по группам. */
export function campaignIndexFromCount(json: any): CampaignIndex {
  const groups = countGroups(json);
  const ids: number[] = [];
  const meta: CampaignIndex['meta'] = {};
  for (const grp of groups) {
    const gType = grp.type, gStatus = grp.status;
    const list = grp.advert_list || grp.advertList || [];
    for (const it of list) {
      const id = it.advertId || it.advertID || it.id;
      if (id == null) continue;
      const idNum = Number(id);
      if (!meta[idNum]) {
        meta[idNum] = { type: gType, status: gStatus, changeTime: it.changeTime || it.change_time || '' };
        ids.push(idNum);
      }
    }
  }
  return { ids, meta };
}

export interface StatsIndex {
  advertIds: number[];
  statsAdvertIds: number[];
  statusById: Record<number, number>;
}

/**
 * wbAdsFetchCampaigns_ (WbAdsProbe.gs:438) — набор id для fullstats.
 * 🔴 Кратность сохраняется: id, пришедший в двух группах, попадает в список дважды (как в эталоне).
 * statusById — более приоритетный из статусов (9 раньше 11 раньше 7).
 */
export function statsIndexFromCount(json: any): StatsIndex {
  const out: StatsIndex = { advertIds: [], statsAdvertIds: [], statusById: {} };
  const order = [9, 11, 7];
  for (const grp of countGroups(json)) {
    const st = grp.status;
    const list = grp.advert_list || grp.advertList || [];
    for (const it of list) {
      const id = it.advertId || it.advertID || it.id;
      if (id == null) continue;
      const idNum = Number(id);
      out.advertIds.push(idNum);
      if ([7, 9, 11].indexOf(Number(st)) !== -1) {
        out.statsAdvertIds.push(idNum);
        const prev = out.statusById[idNum];
        if (prev === undefined || order.indexOf(Number(st)) < order.indexOf(prev)) out.statusById[idNum] = Number(st);
      }
    }
  }
  return out;
}

/** wbAdsOrderFullstatsIds_: 9 → 11 → 7, внутри группы по возрастанию; ничего не отбрасывает. */
export function orderFullstatsIds(ids: number[], statusById: Record<number, number>): number[] {
  const order = [9, 11, 7];
  const rank = (id: number): number => {
    const r = order.indexOf(Number(statusById[id]));
    return r === -1 ? order.length : r;
  };
  return ids.slice(0).sort((a, b) => (rank(a) - rank(b)) || (Number(a) - Number(b)));
}

/** Список кампаний из ответа /api/advert/v2/adverts (обе формы обёртки). */
export function advertsList(json: any): any[] {
  if (!json) return [];
  return Array.isArray(json) ? json : ((json.adverts) || (json.data && json.data.adverts) || []);
}

// ─────────────────────────────────────────────────────────────────────────
// RAW_WB_ADV_CAMPAIGNS
// ─────────────────────────────────────────────────────────────────────────

/** Defensive deep-scan уникальных nmId (wbAdvCollectNmIds_): порядок обхода = порядок эталона. */
export function collectNmIds(obj: any): number[] {
  const out: number[] = [];
  const seen: Record<number, 1> = {};
  const budget = { n: 0 };
  const add = (v: any): void => {
    if (v == null || typeof v === 'object') return;
    const num = Number(v);
    if (!num || seen[num]) return;
    seen[num] = 1;
    out.push(num);
  };
  const walk = (node: any): void => {
    if (node == null || typeof node !== 'object' || budget.n > 5000) return;
    budget.n++;
    if (node.nm_id != null) add(node.nm_id);
    if (node.nmId != null) add(node.nmId);
    if (node.nmID != null) add(node.nmID);
    for (const k in node) {
      if (!Object.prototype.hasOwnProperty.call(node, k)) continue;
      const v = node[k];
      if (k === 'nm' && (typeof v === 'number' || typeof v === 'string')) add(v);
      if (v && typeof v === 'object') walk(v);
    }
  };
  walk(obj);
  return out;
}

/** Один row кампании (wbAdvCampaignRow_). */
export function campaignRow(rid: string, loadTs: string, advertId: number, meta: any, detail: any): RawRow {
  const d = detail || {};
  const m = meta || {};
  const nmIds = detail ? collectNmIds(detail) : [];
  return {
    load_ts: loadTs, run_id: rid, period_from: '', period_to: '',
    source_method: 'promotion/count+adverts/v2',
    processed_status: detail ? 'raw' : 'count_only',
    advertId,
    type: (d.type != null) ? d.type : (m.type != null ? m.type : ''),
    status: (d.status != null) ? d.status : (m.status != null ? m.status : ''),
    payment_type: (d.paymentType != null) ? d.paymentType : (d.payment_type != null ? d.payment_type : ''),
    name: (d.name != null) ? d.name : '',
    campName: (d.campName != null) ? d.campName : (d.name != null ? d.name : ''),
    create_time: (d.createTime != null) ? d.createTime : (d.create_time != null ? d.create_time : ''),
    change_time: (d.changeTime != null) ? d.changeTime : (d.change_time != null ? d.change_time : (m.changeTime || '')),
    start_time: (d.startTime != null) ? d.startTime : (d.start_time != null ? d.start_time : ''),
    end_time: (d.endTime != null) ? d.endTime : (d.end_time != null ? d.end_time : ''),
    nm_ids: nmIds.join(','), nm_count: nmIds.length,
    raw_json: rawJson(detail || { advertId, count_meta: m }),
  };
}

/** id кампании в элементе ответа adverts v2 (порядок ключей — как в loadWbAdsCampaignsRaw). */
export function advertDetailId(a: any): unknown {
  return a.advertId || a.id || a.advertID;
}

// ─────────────────────────────────────────────────────────────────────────
// fullstats → RAW_WB_ADV_CAMPAIGN_STATS + RAW_WB_ADV_BOOSTER_STATS
// ─────────────────────────────────────────────────────────────────────────

/** Кампании из ответа fullstats (массив или {data}). */
export function fullstatsCampaigns(json: any): any[] {
  return Array.isArray(json) ? json : ((json && json.data) || []);
}

export function flattenFullstats(camps: any[], rid: string, from: string, to: string, now: () => string):
  { statRows: RawRow[]; boosterRows: RawRow[] } {
  const statRows: RawRow[] = [];
  const boosterRows: RawRow[] = [];
  for (const c of camps) {
    const camp = c || {};
    const advertId = camp.advertId != null ? camp.advertId : camp.advertID;
    for (const dd of camp.days || []) {
      const day = dd || {};
      const date = day.date != null ? day.date : (day.dt || '');
      for (const aa of day.apps || []) {
        const app = aa || {};
        const appType = (app.appType != null) ? app.appType : (app.appName != null ? app.appName : '');
        for (const nn of app.nm || app.nms || []) {
          const nm = nn || {};
          statRows.push({
            load_ts: now(), run_id: rid, period_from: from, period_to: to,
            source_method: 'adv/v3/fullstats', processed_status: 'raw',
            advertId, date, appType,
            nmId: (nm.nmId != null) ? nm.nmId : (nm.nm != null ? nm.nm : ''),
            name: (nm.name != null) ? nm.name : '',
            views: nm.views, clicks: nm.clicks, ctr: nm.ctr, cpc: nm.cpc, cr: nm.cr,
            atbs: nm.atbs, orders: nm.orders, canceled: nm.canceled, shks: nm.shks,
            sum: nm.sum, sum_price: (nm.sum_price != null) ? nm.sum_price : nm.sumPrice,
            source_level: 'nm', raw_json: rawJson(nm),
          });
        }
      }
    }
    for (const bb of camp.boosterStats || camp.booster_stats || []) {
      const bs = bb || {};
      boosterRows.push({
        load_ts: now(), run_id: rid, period_from: from, period_to: to,
        source_method: 'adv/v3/fullstats', processed_status: 'raw',
        advertId, date: bs.date != null ? bs.date : '',
        nmId: (bs.nm != null) ? bs.nm : (bs.nmId != null ? bs.nmId : ''),
        avg_position: (bs.avg_position != null) ? bs.avg_position
          : (bs.position != null ? bs.position : (bs.avgPosition != null ? bs.avgPosition : '')),
        raw_json: rawJson(bs),
      });
    }
  }
  return { statRows, boosterRows };
}

/** Маркерная строка no_stats (wbAdvCampaignStatNoStatsRow_). */
export function noStatsRow(rid: string, loadTs: string, advertId: number, from: string, to: string): RawRow {
  return {
    load_ts: loadTs, run_id: rid, period_from: from, period_to: to,
    source_method: 'adv/v3/fullstats', processed_status: 'no_stats',
    advertId, date: '', appType: '', nmId: '', name: '',
    views: '', clicks: '', ctr: '', cpc: '', cr: '', atbs: '', orders: '',
    canceled: '', shks: '', sum: '', sum_price: '', source_level: '', raw_json: '',
  };
}

// ─────────────────────────────────────────────────────────────────────────
// /adv/v1/upd → RAW_WB_ADV_COSTS + RAW_WB_ADV_COSTS_RUNS
// ─────────────────────────────────────────────────────────────────────────

/** Строки ответа upd (массив или {data}). */
export function updItems(json: any): any[] {
  return Array.isArray(json) ? json : ((json && json.data) || []);
}

/** Один row расхода (wbAdvCostRow_). window_index — вторая половина ключа снапшота (run_id, window_index). */
export function costRow(rid: string, loadTs: string, u0: any, from: string, to: string, windowIndex: number): RawRow {
  const u = u0 || {};
  const updTime = (u.updTime != null) ? u.updTime : '';
  return {
    window_index: String(windowIndex),
    load_ts: loadTs, run_id: rid, period_from: from, period_to: to,
    source_method: 'adv/v1/upd', processed_status: 'raw',
    updTime,
    updDate: updTime ? String(updTime).substring(0, 10) : '',
    updNum: (u.updNum != null) ? u.updNum : '',
    updSum: (u.updSum != null) ? u.updSum : '',
    advertId: (u.advertId != null) ? u.advertId : '',
    campName: (u.campName != null) ? u.campName : '',
    advertType: (u.advertType != null) ? u.advertType : '',
    paymentType: (u.paymentType != null) ? u.paymentType : '',
    advertStatus: (u.advertStatus != null) ? u.advertStatus : '',
    raw_json: rawJson(u),
  };
}

export interface CostsRunLogInput {
  rid: string;
  windowIndex: number;
  win: { from: string; to: string };
  httpCode: number | '';
  httpOk: boolean;
  rows: RawRow[];
  durationMs: number;
  status: 'OK' | 'ERROR';
  errorMessage: string;
  completedAt: string;
  requestedDays: number;
}

/**
 * Строка журнала окна = COMMIT-MARKER (wbAdsCostsWriteRunLog_). http_success — строчные 'true'/'false':
 * V_ADV_COSTS_SNAPSHOT берёт окно только при status='OK' AND http_success='true'.
 */
export function costsRunLogRow(o: CostsRunLogInput): RawRow {
  let minD = '', maxD = '', outOfWindow = 0, dayCnt = 0;
  const days: Record<string, true> = {};
  for (const r of o.rows) {
    const d = String(r.updDate || '');
    if (!d) continue;
    if (!days[d]) { days[d] = true; dayCnt++; }
    if (!minD || d < minD) minD = d;
    if (!maxD || d > maxD) maxD = d;
    if (d < o.win.from || d > o.win.to) outOfWindow++;
  }
  return {
    load_ts: o.completedAt,
    run_id: o.rid,
    source_method: 'adv/v1/upd',
    window_index: String(o.windowIndex),
    window_completed_at: o.completedAt,
    period_from: o.win.from,
    period_to: o.win.to,
    requested_days: String(o.requestedDays),
    http_status: String(o.httpCode || ''),
    http_success: o.httpOk ? 'true' : 'false',
    returned_rows: String(o.rows.length),
    returned_days: String(dayCnt),
    returned_min_date: minD,
    returned_max_date: maxD,
    rows_out_of_window: String(outOfWindow),
    duration_ms: String(o.durationMs),
    status: o.status,
    error_message: o.errorMessage || '',
  };
}

// ─────────────────────────────────────────────────────────────────────────
// Ads-3: get-bids → RAW_WB_ADV_QUERY_BIDS
// ─────────────────────────────────────────────────────────────────────────

export interface PairCtx { payment_type: unknown; bid_type: unknown; status: unknown }

export interface CpmManualPairs {
  pairs: Array<{ advertId: number; nmId: number }>;
  ctx: Record<string, PairCtx>;
}

/** Плоский список advertId из count — уникальных (wbAdsCollectCpmManualPairs_, шаг 1). */
export function uniqueIdsFromCount(json: any): number[] {
  const ids: number[] = [];
  const seenId: Record<string, 1> = {};
  for (const grp of countGroups(json)) {
    const list = (grp && (grp.advert_list || grp.advertList)) || [];
    for (const it of list) {
      const id = it && (it.advertId || it.advertID || it.id);
      if (id != null && !seenId[id]) { seenId[id] = 1; ids.push(Number(id)); }
    }
  }
  return ids;
}

/** Пары CPM+manual из одной пачки adverts v2 (накопительно в acc, дедуп по 'advert|nm'). */
export function addCpmManualPairs(adverts: any[], acc: CpmManualPairs, seenPair: Record<string, 1>): void {
  for (const a of adverts) {
    const adv = a || {};
    const advertId = adv.id || adv.advertId || adv.advertID;
    if (advertId == null) continue;
    const payType = (adv.settings && adv.settings.payment_type) || adv.payment_type || '';
    const bidType = adv.bid_type || '';
    if (String(payType).toLowerCase() !== 'cpm') continue;
    if (String(bidType).toLowerCase() !== 'manual') continue;
    for (const s of adv.nm_settings || []) {
      const nmId = s && (s.nm_id || s.nmId);
      if (nmId == null) continue;
      const key = `${advertId}|${nmId}`;
      if (seenPair[key]) continue;
      seenPair[key] = 1;
      acc.pairs.push({ advertId: Number(advertId), nmId: Number(nmId) });
      acc.ctx[key] = { payment_type: payType, bid_type: bidType, status: adv.status };
    }
  }
}

/** Строки снимка ставок из ответа одной пачки; pairsWithBids пополняется ключами 'advert|nm'. */
export function bidRows(json: any, rid: string, snapshotTs: string, ctx: Record<string, PairCtx>,
  pairsWithBids: Record<string, 1>): RawRow[] {
  const bids = (json && (json.bids || json.items)) || [];
  const rows: RawRow[] = [];
  const snapshotDate = snapshotTs.substring(0, 10);
  for (const b of bids) {
    const it = b || {};
    const key = `${it.advert_id}|${it.nm_id}`;
    const c: any = ctx[key] || {};
    pairsWithBids[key] = 1;
    rows.push({
      load_ts: snapshotTs, run_id: rid, source_method: 'adv/v0/normquery/get-bids', processed_status: 'raw',
      snapshot_ts: snapshotTs, snapshot_date: snapshotDate,
      advert_id: it.advert_id, nm_id: it.nm_id, norm_query: it.norm_query,
      bid: it.bid, bid_kopecks: it.bid_kopecks, currency: it.currency,
      payment_type: c.payment_type || '', bid_type: c.bid_type || '',
      campaign_status: (c.status != null ? c.status : ''),
      raw_json: rawJson(it),
    });
  }
  return rows;
}

// ─────────────────────────────────────────────────────────────────────────
// Ads-2: normquery/stats → RAW_WB_ADV_QUERY_STATS
// ─────────────────────────────────────────────────────────────────────────

export interface QsFlat {
  rows: RawRow[];
  packs: number;
  pairsWithRows: string[];
  maxKeys: number;
  rowsWithViews: number;
}

/** wbAdsQsFlatten_. Контекст кампании НЕ пишется: строка описывает прошлый период. */
export function flattenQueryStats(json: any, rid: string, day: string, nowTs: string): QsFlat {
  const out: QsFlat = { rows: [], packs: 0, pairsWithRows: [], maxKeys: 0, rowsWithViews: 0 };
  const arr = (json && (json.stats || json.data)) || [];
  for (const p of arr) {
    const pack = p || {};
    out.packs++;
    const advertId = (pack.advert_id != null) ? pack.advert_id : pack.advertId;
    const nmId = (pack.nm_id != null) ? pack.nm_id : pack.nmId;
    const inner = pack.stats || pack.items || [];
    if (inner.length) out.pairsWithRows.push(`${advertId}|${nmId}`);
    if (inner.length > out.maxKeys) out.maxKeys = inner.length;
    for (const c of inner) {
      const cl = c || {};
      if (cl.views != null) out.rowsWithViews++;
      out.rows.push({
        load_ts: nowTs, run_id: rid,
        source_method: 'adv/v0/normquery/stats', processed_status: 'raw',
        period_from: day, period_to: day,
        advert_id: advertId, nm_id: nmId,
        norm_query: (cl.norm_query != null) ? cl.norm_query : (cl.normQuery != null ? cl.normQuery : ''),
        views: cl.views, clicks: cl.clicks, ctr: cl.ctr, cpc: cl.cpc, cpm: cl.cpm,
        avg_pos: (cl.avg_pos != null) ? cl.avg_pos : cl.avgPos,
        atbs: cl.atbs, orders: cl.orders, shks: cl.shks,
        spend: cl.spend, currency: cl.currency,
        raw_json: rawJson(cl),
      });
    }
  }
  return out;
}

/** wbAdsQsRound2_. */
export function round2(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? Math.round(n * 100) / 100 : 0;
}

export function chunk<T>(arr: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}
