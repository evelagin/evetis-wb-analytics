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
  };
}
