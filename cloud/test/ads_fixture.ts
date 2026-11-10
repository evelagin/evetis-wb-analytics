/**
 * Синтетический «WB Advert API» для тестов рекламы. Идентификаторы, суммы и названия выдуманы
 * (репозиторий публичный): advertId 1001…1005, nmId 50001…50004, суммы — круглые.
 * Даты строятся от логического периода, чтобы окна загрузчика попадали в данные.
 *
 * Один и тот же роутер кормит и боевой Apps Script (в node:vm), и Cloud Run-загрузчик.
 */
import { addDays } from '../src/loaders/ads/time.js';

export interface WbReply { status: number; body: string; headers?: Record<string, string> }
export type WbRoute = (method: string, url: string, body: string | undefined) => WbReply;

export interface FixtureOpts {
  /** fullstats отвечает 500 на любой запрос, где есть этот advertId (проверка 50 → 10 → 1). */
  fullstatsFailId?: number;
  /** upd возвращает пустой массив. */
  updEmpty?: boolean;
  /** upd: первые N ответов — 429 (с Retry-After: 1). */
  upd429?: number;
  /** get-bids отвечает 500. */
  bidsFail?: boolean;
  /** normquery/stats: вернуть на один пакет меньше, чем пар (нарушение контракта мощности). */
  qstatsDropPack?: boolean;
  /** count отвечает 500. */
  countFail?: boolean;
}

const json = (status: number, v: unknown, headers?: Record<string, string>): WbReply => ({ status, body: JSON.stringify(v), headers });

export const COUNT = {
  adverts: [
    { type: 8, status: 9, count: 2, advert_list: [{ advertId: 1001, changeTime: '2026-11-01T10:00:00.000+03:00' }, { advertId: 1002 }] },
    { type: 8, status: 11, count: 1, advert_list: [{ advertId: 1003 }] },
    // 1001 повторно в группе «завершена»: кратность в fullstats сохраняется, приоритет статуса — 9.
    { type: 8, status: 7, count: 2, advert_list: [{ advertId: 1004 }, { advertId: 1001 }] },
    { type: 9, status: 4, count: 1, advertList: [{ advertID: 1005 }] },
  ],
  all: 6,
};

export const DETAILS: Record<number, unknown> = {
  1001: {
    id: 1001, bid_type: 'manual', status: 9, type: 8, name: 'Кампания A',
    settings: { payment_type: 'cpm', name: 'Кампания A' },
    nm_settings: [{ nm_id: 50001, bids_kopecks: { search: 25000 } }, { nm_id: 50002 }],
    timestamps: { created: '2026-09-01T10:00:00+03:00', updated: '2026-11-01T10:00:00+03:00' },
  },
  1002: { id: 1002, bid_type: 'manual', status: 9, settings: { payment_type: 'cpc' }, nm_settings: [{ nm_id: 50003 }] },
  1003: { id: 1003, bid_type: 'manual', status: 11, settings: { payment_type: 'cpm' }, nm_settings: [{ nm_id: 50003 }] },
  1004: { id: 1004, bid_type: 'unified', status: 7, settings: { payment_type: 'cpm' }, nm_settings: [{ nmId: 50004 }] },
  1005: { id: 1005, bid_type: 'manual', status: 4, settings: { payment_type: 'cpm' }, nm_settings: [{ nm_id: 50004 }] },
};

export function makeWbRouter(lp: string, o: FixtureOpts = {}) {
  let upd429Left = o.upd429 ?? 0;
  const calls: string[] = [];
  const route: WbRoute = (method, url, body) => {
    const u = new URL(url);
    calls.push(`${method.toUpperCase()} ${u.pathname}`);
    if (u.pathname === '/adv/v1/promotion/count') return o.countFail ? json(500, { error: 'synthetic' }) : json(200, COUNT);
    if (u.pathname === '/api/advert/v2/adverts') {
      const ids = (u.searchParams.get('ids') ?? '').split(',').map(Number);
      return json(200, { adverts: ids.map((id) => DETAILS[id]).filter(Boolean) });
    }
    if (u.pathname === '/adv/v3/fullstats') {
      const ids = (u.searchParams.get('ids') ?? '').split(',').map(Number);
      if (o.fullstatsFailId !== undefined && ids.includes(o.fullstatsFailId)) return json(500, { error: 'synthetic 500' });
      const out = ids.filter((id) => id === 1001 || id === 1003).map((id) => ({
        advertId: id,
        days: [
          { date: `${lp}T00:00:00Z`, apps: [{ appType: 1, nm: [
            { nmId: id === 1001 ? 50001 : 50003, name: 'Товар', views: 100, clicks: 7, ctr: 7, cpc: 10.5, cr: 14.29,
              atbs: 2, orders: 1, canceled: 0, shks: 1, sum: 73.5, sum_price: 990 },
          ] }, { appType: 32, nm: [{ nmId: id === 1001 ? 50002 : 50003, views: 3, clicks: 0, sum: 0, sumPrice: 0 }] }] },
          { date: `${addDays(lp, -1)}T00:00:00Z`, apps: [{ appType: 1, nm: [{ nmId: id === 1001 ? 50001 : 50003, views: 50, clicks: 2, sum: 21 }] }] },
        ],
        boosterStats: [{ date: `${lp}T00:00:00Z`, nm: id === 1001 ? 50001 : 50003, avg_position: 12 }],
      }));
      return json(200, out);
    }
    if (u.pathname === '/adv/v1/upd') {
      if (upd429Left > 0) { upd429Left--; return json(429, { error: 'too many' }, { 'Retry-After': '1' }); }
      if (o.updEmpty) return json(200, []);
      const to = u.searchParams.get('to') ?? lp;
      const from = u.searchParams.get('from') ?? lp;
      return json(200, [
        { updNum: 0, updTime: `${to}T10:15:00+03:00`, updSum: 150, advertId: 1001, campName: 'Кампания A', advertType: 8, paymentType: 'Баланс', advertStatus: 9 },
        { updNum: 7, updTime: `${from}T05:00:00+03:00`, updSum: 40.5, advertId: 1003, campName: 'Кампания C', advertType: 8, paymentType: 'Счёт', advertStatus: 11 },
        // «хвост» UTC-окна: строка следующих суток (rows_out_of_window)
        { updNum: 0, updTime: `${addDays(to, 1)}T01:30:00+03:00`, updSum: 5, advertId: 1001, campName: 'Кампания A', advertType: 8, paymentType: 'Баланс', advertStatus: 9 },
      ]);
    }
    if (u.pathname === '/adv/v0/normquery/get-bids') {
      if (o.bidsFail) return json(500, { error: 'synthetic' });
      const items = (JSON.parse(body ?? '{}').items ?? []) as Array<{ advert_id: number; nm_id: number }>;
      return json(200, { bids: items.map((p) => ({ advert_id: p.advert_id, nm_id: p.nm_id, norm_query: 'крем для лица', bid: 250, bid_kopecks: 25000, currency: 'RUB' })) });
    }
    if (u.pathname === '/adv/v0/normquery/stats') {
      const req = JSON.parse(body ?? '{}') as { from: string; items: Array<{ advert_id: number; nm_id: number }> };
      let packs = req.items.map((p) => ({
        advert_id: p.advert_id, nm_id: p.nm_id,
        stats: p.nm_id === 50001
          ? [{ norm_query: 'крем', views: 40, clicks: 3, ctr: 7.5, cpc: 9, cpm: 300, avg_pos: 4.2, atbs: 1, orders: 0, shks: 0, spend: 27, currency: 'RUB' },
             { norm_query: 'крем увлажняющий', clicks: 1, cpc: 12, avg_pos: 8, atbs: 0, orders: 0, spend: 12 }]
          : [],
      }));
      if (o.qstatsDropPack) packs = packs.slice(1);
      return json(200, { stats: packs });
    }
    return json(404, { error: `синтетический роутер: нет маршрута ${u.pathname}` });
  };
  return { route, calls };
}

/** Scope Ads-2 (как будто V_ADV_CAMPAIGN_STATS) и телеметрия расхода по суткам. */
export function scopeFor(day: string, lp: string): Array<{ advertId: number; nmId: number; spend: number }> {
  if (day === lp) return [{ advertId: 1001, nmId: 50001, spend: 73.5 }, { advertId: 1003, nmId: 50003, spend: 73.5 }];
  if (day === addDays(lp, -1)) return [{ advertId: 1001, nmId: 50001, spend: 21 }];
  return [];
}
export const COSTS_BY_DAY = (lp: string): Record<string, number> => ({ [lp]: 150, [addDays(lp, -1)]: 40.5 });
