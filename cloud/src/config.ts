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
  // ── UNITKA ENGINE v1 (Stage E1) — читает ТОЛЬКО wb_mart.V_UNITKA_*, пишет только факт-ячейки ──
  unitkaSpreadsheetId: string;
  unitkaSheetName: string;
  unitkaMartDataset: string;
  unitkaOpsDataset: string;
  unitkaRunsTable: string;
  /** Порог владельца: своя ставка SKU при n >= 10, иначе fallback магазина. */
  unitkaMinN: number;
  /** SOURCE_STALE, если LAST_CLOSED_DATE отстаёт от D-1 МСК больше чем на столько суток. */
  unitkaMaxLagDays: number;
  /**
   * Запись в книгу разрешена ТОЛЬКО при ENVIRONMENT=prod И UNITKA_WRITE_ENABLED=1.
   * Shadow-Job физически получает readonly-scope Sheets и писать не может.
   */
  unitkaWriteEnabled: boolean;
  // ── UNITKA INTEGRITY GUARD V1 (Phase 1C1) — все параметры разбираются МЯГКО: опечатка ──
  // ── не должна ронять loadConfig и вместе с ним зрелый факт-писатель Unitka.        ──
  /**
   * off | observe | enforce. По умолчанию off: образ с Guard без явного включения ведёт себя
   * как раньше. Неизвестное значение → off (+ предупреждение в логе прогона).
   * observe — оценить, журналировать, НИКОГДА не менять исход прогона.
   * enforce — зарезервирован (решение владельца); DATA_ERROR не бросает и там.
   */
  unitkaIntegrityMode: 'off' | 'observe' | 'enforce';
  /** Сырое значение UNITKA_INTEGRITY_MODE, если оно было нераспознано (для предупреждения). */
  unitkaIntegrityModeInvalid: string | null;
  /** Срок D−1 для отчёта хранения, "HH:MM" МСК. Нераспознанное → '12:15'. */
  unitkaStorageDueMsk: string;
  /**
   * Бюджет времени Guard на прогон, мс (по умолчанию 90 000; допустимо 5 000–300 000, иначе 90 000).
   * Передаётся в BigQuery как серверный jobTimeoutMs: зависший запрос Guard отменяет сам BigQuery,
   * а прогон успевает записать журнал до таймаута Job'а (600 с).
   */
  unitkaIntegrityBudgetMs: number;
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

/** Integrity Guard: мягкий разбор. Никогда не бросает ConfigError. */
function integrityConfig(env: Env): Pick<Config, 'unitkaIntegrityMode' | 'unitkaIntegrityModeInvalid' | 'unitkaStorageDueMsk' | 'unitkaIntegrityBudgetMs'> {
  const raw = (env.UNITKA_INTEGRITY_MODE ?? '').trim().toLowerCase();
  const known = raw === 'off' || raw === 'observe' || raw === 'enforce';
  const due = (env.UNITKA_STORAGE_DUE_MSK ?? '').trim();
  const budgetRaw = Number((env.UNITKA_INTEGRITY_BUDGET_MS ?? '').trim());
  const budget = Number.isInteger(budgetRaw) && budgetRaw >= 5_000 && budgetRaw <= 300_000 ? budgetRaw : 90_000;
  return {
    unitkaIntegrityMode: known ? (raw as 'off' | 'observe' | 'enforce') : 'off',
    unitkaIntegrityModeInvalid: raw === '' || known ? null : raw,
    unitkaStorageDueMsk: /^([01]\d|2[0-3]):[0-5]\d$/.test(due) ? due : '12:15',
    unitkaIntegrityBudgetMs: budget,
  };
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
    unitkaSpreadsheetId: opt(env, 'UNITKA_SPREADSHEET_ID', '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg'),
    unitkaSheetName: opt(env, 'UNITKA_SHEET_NAME', 'WB_Юнит_2025'),
    unitkaMartDataset: opt(env, 'UNITKA_MART_DATASET', 'wb_mart'),
    unitkaOpsDataset: opt(env, 'UNITKA_OPS_DATASET', 'wb_ops'),
    unitkaRunsTable: opt(env, 'UNITKA_RUNS_TABLE', 'UNITKA_ENGINE_RUNS'),
    unitkaMinN: intOpt(env, 'UNITKA_MIN_N', 10),
    unitkaMaxLagDays: intOpt(env, 'UNITKA_MAX_LAG_DAYS', 2),
    unitkaWriteEnabled: opt(env, 'UNITKA_WRITE_ENABLED', '0') === '1',
    ...integrityConfig(env),
  };
}
