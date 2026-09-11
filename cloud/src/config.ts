/**
 * Конфигурация загрузчика из окружения (PR-Mig0).
 * Разделение: секреты — в Secret Manager (см. secrets.ts), НЕ здесь;
 * несекретные параметры — env; runtime-состояние — в BQ (см. bq/runManifest.ts).
 */
import { ConfigError } from './errors.js';

export type Environment = 'shadow' | 'prod';

export interface Config {
  projectId: string;
  bqLocation: string;
  rawDataset: string;
  manifestTable: string;
  environment: Environment;
  loaderName: string;
  logLevel: string;
  wbHttpTimeoutMs: number;
  lookbackDays: number;
  sinkMode: string;
  /** Метаданные образа/коммита — прокидываются при деплое, пишутся в манифест. */
  imageDigest: string;
  gitSha: string;
  /** Идентификаторы конкретного запуска Cloud Run Job (если доступны). */
  executionId: string;
  // ── stocks loader (mart-прогону НЕ нужны: он не ходит в WB API и не читает WB-секрет) ──
  wbAnalyticsHost: string;
  wbAnalyticsSecret: string;
  stocksRawTable: string;
  stocksSnapshotTable: string;
  refSkuTable: string;
  // ── prices observer (PR-1) ────────────────────────────────────────────────
  /** Хост категории «Цены и скидки». Отличается от аналитического — не переиспользовать. */
  wbPricesHost: string;
  /** Имя секрета с токеном категории «Цены и скидки». Значение читает ТОЛЬКО runtime SA. */
  wbPricesSecret: string;
  pricesRawTable: string;
  pricesObservationsTable: string;
  // ── tariffs loader (PR-2) — тот же секрет, что у наблюдателя цен ──────────
  wbTariffsHost: string;
  tariffsRawTable: string;
  tariffsObservationsTable: string;
  // ── funnel loader (UNITKA 2.0 R2) — воронка продаж, тот же аналитический хост и секрет ──
  /**
   * Окно перезабора в сутках. Не 1: WB досчитывает воронку задним числом,
   * и однодневное окно навсегда зафиксировало бы первую (неполную) версию дня.
   * Не больше 7: без подписки «Джем» WB отдаёт максимум последнюю неделю (R7).
   */
  funnelLookbackDays: number;
  funnelRawTable: string;
  funnelObservationsTable: string;
}

type Env = Record<string, string | undefined>;

function req(env: Env, name: string): string {
  const v = (env[name] ?? '').trim();
  if (!v) throw new ConfigError(`Отсутствует обязательная переменная окружения: ${name}`);
  return v;
}

function opt(env: Env, name: string, fallback: string): string {
  const v = (env[name] ?? '').trim();
  return v || fallback;
}

function intOpt(env: Env, name: string, fallback: number): number {
  const raw = (env[name] ?? '').trim();
  if (!raw) return fallback;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 0) throw new ConfigError(`${name} должно быть неотрицательным целым, получено '${raw}'`);
  return n;
}

export function loadConfig(env: Env = process.env): Config {
  const environment = (env.ENVIRONMENT ?? '').trim();
  if (environment !== 'shadow' && environment !== 'prod') {
    throw new ConfigError(`ENVIRONMENT должно быть 'shadow' или 'prod', получено '${environment}'`);
  }
  return {
    projectId: req(env, 'GCP_PROJECT_ID'),
    bqLocation: opt(env, 'BQ_LOCATION', 'EU'),
    rawDataset: req(env, 'BQ_RAW_DATASET'),
    manifestTable: opt(env, 'BQ_MANIFEST_TABLE', 'LOADER_RUNS'),
    environment,
    loaderName: opt(env, 'LOADER_NAME', ''),
    logLevel: opt(env, 'LOG_LEVEL', 'info'),
    wbHttpTimeoutMs: intOpt(env, 'WB_HTTP_TIMEOUT_MS', 60000),
    lookbackDays: intOpt(env, 'LOOKBACK_DAYS', 1),
    sinkMode: opt(env, 'SINK_MODE', 'on'),
    imageDigest: opt(env, 'IMAGE_DIGEST', 'unknown'),
    gitSha: opt(env, 'GIT_SHA', 'unknown'),
    executionId: opt(env, 'CLOUD_RUN_EXECUTION', ''),
    wbAnalyticsHost: opt(env, 'WB_ANALYTICS_HOST', 'https://seller-analytics-api.wildberries.ru'),
    wbAnalyticsSecret: opt(env, 'WB_ANALYTICS_SECRET', 'WB_TOKEN_ANALYTICS'),
    stocksRawTable: opt(env, 'STOCKS_RAW_TABLE', 'RAW_WB_STOCKS__CR'),
    stocksSnapshotTable: opt(env, 'STOCKS_SNAPSHOT_TABLE', 'WB_STOCKS_SNAPSHOTS__CR'),
    refSkuTable: opt(env, 'REF_SKU_TABLE', 'REF_SKU_MASTER'),
    wbPricesHost: opt(env, 'WB_PRICES_HOST', 'https://discounts-prices-api.wildberries.ru'),
    wbPricesSecret: opt(env, 'WB_PRICES_SECRET', 'WB_PRICES_READ_TOKEN'),
    pricesRawTable: opt(env, 'PRICES_RAW_TABLE', 'RAW_WB_PRICES'),
    pricesObservationsTable: opt(env, 'PRICES_OBSERVATIONS_TABLE', 'WB_PRICES_OBSERVATIONS'),
    wbTariffsHost: opt(env, 'WB_TARIFFS_HOST', 'https://common-api.wildberries.ru'),
    tariffsRawTable: opt(env, 'TARIFFS_RAW_TABLE', 'RAW_WB_TARIFFS'),
    tariffsObservationsTable: opt(env, 'TARIFFS_OBSERVATIONS_TABLE', 'WB_TARIFF_OBSERVATIONS'),
    funnelLookbackDays: intOpt(env, 'FUNNEL_LOOKBACK_DAYS', 7),
    funnelRawTable: opt(env, 'FUNNEL_RAW_TABLE', 'RAW_WB_FUNNEL_DAILY'),
    funnelObservationsTable: opt(env, 'FUNNEL_OBSERVATIONS_TABLE', 'WB_FUNNEL_OBSERVATIONS'),
  };
}
