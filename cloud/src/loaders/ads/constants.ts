/**
 * M3 (перенос рекламы WB из Apps Script). Константы — ДОСЛОВНО из эталона Apps Script:
 *   apps-script/ingestion/WbAdsProbe.gs      — хост, батчи, паузы, ретраи;
 *   apps-script/ingestion/WbAdsRawLoader.gs  — окна, колонки RAW, бюджет;
 *   apps-script/ingestion/WbAdsQueryBids.gs  — Ads-3;
 *   apps-script/ingestion/Wbadsquerystats.gs — Ads-2.
 * Любое расхождение с эталоном — отдельной строкой в docs/ops/MIG_ADS_RUNBOOK.md.
 */

export const WB_ADS_API_HOST = 'https://advert-api.wildberries.ru';

/** ids ≤ 50 за запрос; статистика только по статусам 7/9/11 (WbAdsProbe.gs:62-63). */
export const WB_ADS_IDS_BATCH = 50;
export const WB_ADS_STATS_STATUSES = [7, 9, 11] as const;
/** Порядок fullstats: 9 активна → 11 пауза → 7 завершена (WbAdsRawLoader.gs:819). */
export const WB_ADS_FULLSTATS_STATUS_ORDER = [9, 11, 7] as const;

/** Ретраи (WbAdsProbe.gs:72-73 + дефолты wbFetchWithRetry_, Utils.gs:157). */
export const WB_ADS_MAX_RETRY = 3;           // до 4 попыток
export const WB_ADS_RETRY_BASE_MS = 20_000;  // 20 c, 40 c, 60 c
export const WB_ADS_RETRY_MAX_DELAY_MS = 60_000;
export const WB_ADS_RETRY_CODES = [429, 500, 502, 503, 504] as const;

/** Паузы между запросами — не меньше лимитов WB (WbAdsProbe.gs:76-78). */
export const WB_ADS_FULLSTATS_PAUSE_MS = 21_000; // fullstats: 3 запроса/мин
export const WB_ADS_UPD_PAUSE_MS = 1_200;        // upd: 1 запрос/с
export const WB_ADS_ADVERTS_PAUSE_MS = 1_200;    // пачки /api/advert/v2/adverts
/** Один ответ WB (WbAdsRawLoader.gs:174) — резерв перед стартом очередной операции. */
export const WB_ADS_REQUEST_RESERVE_MS = 30_000;

/** Лимит периода WB для fullstats и upd — 31 день на запрос. */
export const WB_ADV_RAW_MAX_DAYS = 31;
/** Обрезка raw_json (WbAdsRawLoader.gs:197). */
export const WB_ADV_RAW_JSON_MAX = 45_000;

/** Окна расходов (WbAdsRawLoader.gs:113-117). Действует Фаза B: D−14 … D−1. */
export const WB_ADS_COSTS_OPERATIONAL_DAYS = 14;
export const WB_ADS_COSTS_AUDIT_FROM_DAYS = 45;
export const WB_ADS_COSTS_AUDIT_TO_DAYS = 8;
export const WB_ADS_COSTS_AUDIT_WINDOW_DAYS = 30;

/** Окно fullstats: вчера−6 … вчера (wbAdsLast7Range_, WbAdsProbe.gs:685). */
export const WB_ADS_FULLSTATS_WINDOW_DAYS = 7;
/** fullstats за вчера — норма; старше N дней = STALE (Wbadsdaily.gs:31). */
export const WB_ADS_DAILY_STALE_DAYS = 2;

/** Ads-3 (WbAdsQueryBids.gs:64-67). */
export const WB_ADS_QBIDS_METHOD = 'adv/v0/normquery/get-bids';
export const WB_ADS_QBIDS_MAX_ITEMS = 100;
export const WB_ADS_QBIDS_PAUSE_MS = 250;
export const WB_ADS_QBIDS_BUDGET_MS = 90_000;

/** Ads-2 (Wbadsquerystats.gs:71-79). */
export const WB_ADS_QSTATS_METHOD = 'adv/v0/normquery/stats';
export const WB_ADS_QSTATS_MAX_ITEMS = 100;
export const WB_ADS_QSTATS_PAUSE_MS = 6_500;
export const WB_ADS_QSTATS_BUDGET_MS = 120_000;
export const WB_ADS_QSTATS_WINDOW_DAYS = 7;
export const WB_ADS_QSTATS_SUSPICIOUS_CAPS = [100, 200, 500, 1000];

/** Колонки RAW — порядок и имена как в живых таблицах wb_raw (tables.get, 10.11.2026). */
export const CAMPAIGNS_COLUMNS = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'advertId', 'type', 'status', 'payment_type', 'name', 'campName',
  'create_time', 'change_time', 'start_time', 'end_time',
  'nm_ids', 'nm_count', 'raw_json',
] as const;

export const CAMPAIGN_STATS_COLUMNS = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'advertId', 'date', 'appType', 'nmId', 'name',
  'views', 'clicks', 'ctr', 'cpc', 'cr', 'atbs', 'orders', 'canceled', 'shks',
  'sum', 'sum_price', 'source_level', 'raw_json',
] as const;

export const BOOSTER_STATS_COLUMNS = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'advertId', 'date', 'nmId', 'avg_position', 'raw_json',
] as const;

export const COSTS_COLUMNS = [
  'load_ts', 'run_id', 'period_from', 'period_to', 'source_method', 'processed_status',
  'updTime', 'updDate', 'updNum', 'updSum', 'advertId',
  'campName', 'advertType', 'paymentType', 'advertStatus', 'raw_json',
  'window_index',
] as const;

export const COSTS_RUNS_COLUMNS = [
  'load_ts', 'run_id', 'source_method', 'window_index', 'window_completed_at',
  'period_from', 'period_to', 'requested_days',
  'http_status', 'http_success', 'returned_rows', 'returned_days',
  'returned_min_date', 'returned_max_date', 'rows_out_of_window',
  'duration_ms', 'status', 'error_message',
] as const;

export const QUERY_BIDS_COLUMNS = [
  'load_ts', 'run_id', 'source_method', 'processed_status',
  'snapshot_ts', 'snapshot_date',
  'advert_id', 'nm_id', 'norm_query',
  'bid', 'bid_kopecks', 'currency',
  'payment_type', 'bid_type', 'campaign_status',
  'raw_json',
] as const;

export const QUERY_BIDS_RUNS_COLUMNS = [
  'load_ts', 'run_id', 'source_method',
  'snapshot_ts', 'snapshot_date',
  'requested_pairs', 'expected_batches', 'requested_batches',
  'http_status', 'http_success',
  'returned_bid_rows', 'distinct_pairs_with_bids',
  'duration_ms', 'status', 'error_message',
] as const;

export const QUERY_STATS_COLUMNS = [
  'load_ts', 'run_id', 'source_method', 'processed_status',
  'period_from', 'period_to',
  'advert_id', 'nm_id', 'norm_query',
  'views', 'clicks', 'ctr', 'cpc', 'cpm', 'avg_pos',
  'atbs', 'orders', 'shks', 'spend', 'currency',
  'raw_json',
] as const;

export const QUERY_STATS_RUNS_COLUMNS = [
  'load_ts', 'run_id', 'source_method',
  'period_from', 'period_to',
  'requested_pairs', 'expected_batches', 'requested_batches',
  'returned_packs', 'http_status', 'http_success',
  'returned_rows', 'distinct_pairs_with_rows',
  'max_keys_per_pair', 'rows_with_views',
  'scope_pairs', 'scope_spend_rub', 'day_spend_costs_rub',
  'duration_ms', 'status', 'error_message',
] as const;
