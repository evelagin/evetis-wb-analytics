/**
 * Конфигурация и ПРОВЕРКА КОНТУРА рекламного загрузчика — до обращения к WB API и до любой записи
 * (по образцу cloud/src/loaders/stocks/targets.ts, PR #416).
 *
 *   shadow → пишет ТОЛЬКО в `<таблица>__CR`;
 *   prod   → пишет ровно в боевые таблицы Apps Script (RAW_WB_ADV_*), и ни в какие другие.
 * Ни в одном контуре загрузчик не пишет во вью: `V_ADV_*` только читаются (scope Ads-2 и телеметрия).
 * 🔴 Загрузчик НЕ создаёт и НЕ пересоздаёт вью. Аналога wbAdsBqCreateViews() здесь нет и не будет:
 *    та функция пересобрала бы V_ADV_COSTS дедупом по всей таблице и откатила бы cutover на
 *    V_ADV_COSTS_SNAPSHOT (CLAUDE.md, docs/ops/STAGE_B_BOOTSTRAP_RUNBOOK.md §3).
 *
 * Конфиг читается из окружения здесь, а не в общем config.ts: параллельные этапы миграции (M2, M4)
 * правят config.ts, а рекламе не нужно ни одного общего поля сверх Config.
 */
import type { Config } from '../../config.js';
import { LoaderError } from '../../errors.js';
import { WB_ADS_API_HOST, WB_ADS_FULLSTATS_PAUSE_MS } from './constants.js';

/** Боевые таблицы — те же, что пишет Apps Script (Wbadsbigquery.gs:46-60, allowlist). */
export const ADS_PROD_TABLES = {
  campaigns: 'RAW_WB_ADV_CAMPAIGNS',
  campaignStats: 'RAW_WB_ADV_CAMPAIGN_STATS',
  boosterStats: 'RAW_WB_ADV_BOOSTER_STATS',
  costs: 'RAW_WB_ADV_COSTS',
  costsRuns: 'RAW_WB_ADV_COSTS_RUNS',
  queryBids: 'RAW_WB_ADV_QUERY_BIDS',
  queryBidsRuns: 'RAW_WB_ADV_QUERY_BIDS_RUNS',
  queryStats: 'RAW_WB_ADV_QUERY_STATS',
  queryStatsRuns: 'RAW_WB_ADV_QUERY_STATS_RUNS',
} as const;

export type AdsTableKey = keyof typeof ADS_PROD_TABLES;

/** Имя переменной окружения на каждую целевую таблицу. */
export const ADS_TABLE_ENV: Record<AdsTableKey, string> = {
  campaigns: 'ADS_CAMPAIGNS_TABLE',
  campaignStats: 'ADS_CAMPAIGN_STATS_TABLE',
  boosterStats: 'ADS_BOOSTER_STATS_TABLE',
  costs: 'ADS_COSTS_TABLE',
  costsRuns: 'ADS_COSTS_RUNS_TABLE',
  queryBids: 'ADS_QUERY_BIDS_TABLE',
  queryBidsRuns: 'ADS_QUERY_BIDS_RUNS_TABLE',
  queryStats: 'ADS_QUERY_STATS_TABLE',
  queryStatsRuns: 'ADS_QUERY_STATS_RUNS_TABLE',
};

export const SHADOW_SUFFIX = '__CR';

/** Вью, которые загрузчик ЧИТАЕТ (как Apps Script): scope и телеметрия Ads-2. Никогда не пишутся. */
export const ADS_READ_VIEWS = {
  qstatsScope: 'V_ADV_CAMPAIGN_STATS',
  qstatsCosts: 'V_ADV_COSTS',
} as const;

export interface AdsConfig {
  host: string;
  secret: string;
  tables: Record<AdsTableKey, string>;
  qstatsScopeView: string;
  qstatsCostsView: string;
  /** Бюджет fullstats от старта прогона, мс. Apps Script был ограничен стеной 6 мин; здесь её нет. */
  fullstatsBudgetMs: number;
  /** Ads-3/Ads-2: собственные потолки — как в эталоне (90 c / 120 c). */
  qbidsBudgetMs: number;
  qstatsBudgetMs: number;
  /** Пауза между вызовами fullstats (лимит WB 3/мин); в тестах — 0. */
  fullstatsPauseMs: number;
  /** Недельный аудит расходов: начало окна, суток назад (эталон 45; по умолчанию 46 — см. runbook). */
  auditFromDays: number;
  /** prod-аудит исполняется только при ADS_COSTS_AUDIT_ENABLED=1 (после ACK ADS-UTC-EDGE). */
  auditEnabled: boolean;
  enabledQueryBids: boolean;
  enabledQueryStats: boolean;
}

type Env = Record<string, string | undefined>;

function str(env: Env, name: string, fallback: string): string {
  const v = (env[name] ?? '').trim();
  return v || fallback;
}

function int(env: Env, name: string, fallback: number, min: number, max: number): number {
  const raw = (env[name] ?? '').trim();
  if (!raw) return fallback;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new LoaderError(`${name} должно быть целым в [${min}, ${max}], получено '${raw}'`, 'ADS_CONFIG');
  }
  return n;
}

function flag(env: Env, name: string, fallback: boolean): boolean {
  const raw = (env[name] ?? '').trim();
  if (!raw) return fallback;
  if (raw === '1') return true;
  if (raw === '0') return false;
  throw new LoaderError(`${name} должно быть '0' или '1', получено '${raw}'`, 'ADS_CONFIG');
}

/**
 * Значения по умолчанию — ТЕНЕВЫЕ (`__CR`): пустое окружение не может направить запись в прод.
 * В prod каждое имя обязано быть задано явно и совпасть с боевым (assertAdsTargets).
 */
export function loadAdsConfig(env: Env = process.env): AdsConfig {
  const tables = {} as Record<AdsTableKey, string>;
  for (const k of Object.keys(ADS_PROD_TABLES) as AdsTableKey[]) {
    tables[k] = str(env, ADS_TABLE_ENV[k], `${ADS_PROD_TABLES[k]}${SHADOW_SUFFIX}`);
  }
  return {
    host: str(env, 'WB_ADS_HOST', WB_ADS_API_HOST),
    secret: str(env, 'WB_ADS_SECRET', 'WB_TOKEN_ADS'),
    tables,
    qstatsScopeView: str(env, 'ADS_QSTATS_SCOPE_VIEW', ADS_READ_VIEWS.qstatsScope),
    qstatsCostsView: str(env, 'ADS_QSTATS_COSTS_VIEW', ADS_READ_VIEWS.qstatsCosts),
    fullstatsBudgetMs: int(env, 'ADS_FULLSTATS_BUDGET_MS', 900_000, 60_000, 1_500_000),
    qbidsBudgetMs: int(env, 'ADS_QBIDS_BUDGET_MS', 90_000, 10_000, 600_000),
    qstatsBudgetMs: int(env, 'ADS_QSTATS_BUDGET_MS', 120_000, 10_000, 600_000),
    fullstatsPauseMs: WB_ADS_FULLSTATS_PAUSE_MS,
    auditFromDays: int(env, 'ADS_COSTS_AUDIT_FROM_DAYS', 46, 9, 120),
    auditEnabled: flag(env, 'ADS_COSTS_AUDIT_ENABLED', false),
    enabledQueryBids: flag(env, 'ADS_QUERY_BIDS', true),
    enabledQueryStats: flag(env, 'ADS_QUERY_STATS', true),
  };
}

/**
 * Fail-closed проверка контура. Вызывается ПЕРВОЙ строкой обработчика: при нарушении ни WB API,
 * ни Secret Manager, ни BigQuery не тронуты. `keys` — таблицы, которые пишет конкретный загрузчик.
 */
export function assertAdsTargets(environment: Config['environment'], a: AdsConfig, keys: readonly AdsTableKey[]): void {
  const bad: string[] = [];
  for (const k of keys) {
    const t = a.tables[k];
    if (environment === 'shadow') {
      if (t !== `${ADS_PROD_TABLES[k]}${SHADOW_SUFFIX}`) bad.push(`${ADS_TABLE_ENV[k]}=${t} (ожидалось ${ADS_PROD_TABLES[k]}${SHADOW_SUFFIX})`);
    } else if (t !== ADS_PROD_TABLES[k]) {
      bad.push(`${ADS_TABLE_ENV[k]}=${t} (ожидалось ${ADS_PROD_TABLES[k]})`);
    }
  }
  // Читаемые вью не могут совпасть ни с одной целевой таблицей, а цель — не может быть вью.
  const targets = new Set(keys.map((k) => a.tables[k]));
  for (const v of [a.qstatsScopeView, a.qstatsCostsView]) {
    if (targets.has(v)) bad.push(`вью ${v} указана как цель записи`);
  }
  for (const k of keys) if (/^V_/i.test(a.tables[k])) bad.push(`${ADS_TABLE_ENV[k]}=${a.tables[k]}: запись во вью запрещена`);
  if (bad.length) {
    throw new LoaderError(
      `${environment === 'shadow' ? 'shadow пишет только в __CR' : 'prod пишет только в боевые RAW_WB_ADV_*'}: ${bad.join('; ')}`,
      'ADS_TARGET_MISMATCH',
    );
  }
}
