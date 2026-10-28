/**
 * Версия источника финансов для guard витрины (WB finance source-aware mart refresh, 2026-10-12).
 *
 * Зачем. Guard LOADER_RUNS держит ключ (environment, loader, logical_period=D-1). Витрина собирается
 * в 04:00 UTC, а финансы WB приходят позже (DAILY ~04:28, WEEKLY по понедельникам ~09:26 UTC).
 * Раньше COMPLETE за D-1 блокировал все следующие слоты суток, и FACT_FINANCE отставал на сутки.
 * Теперь COMPLETE блокирует слот, только если собран на ТОЙ ЖЕ версии финансов.
 *
 * Что считается версией. Отпечаток СОДЕРЖИМОГО ровно того входа, из которого sp_bootstrap_facts
 * строит FACT_FINANCE: wb_raw.V_WB_FINANCE_SEMANTIC, ВСЕ колонки каждой строки.
 *   1) row_digest  = SHA256(TO_JSON_STRING(строка)). Строка сериализуется целиком, в порядке
 *      колонок, объявленном вью; имена полей входят в JSON, NULL пишется как null, NUMERIC — числом
 *      по значению, DATE — 'YYYY-MM-DD'. Целая строка, а не ручной список колонок: колонку нельзя
 *      «забыть», а новая колонка вью меняет отпечаток всех строк (одна лишняя безопасная пересборка).
 *      Исключённых колонок НЕТ: load_id/loaded_at/run_id стабильны (у RAW_WB_FINANCE 0 ключей
 *      report_id#rrd_id с повторной загрузкой, 12.10.2026), CURRENT_TIMESTAMP во вью нет.
 *   2) part_digest = SHA256(конкатенация row_digest, отсортированных по значению) по report_id.
 *      Сортировка даёт независимость от порядка строк; дубль строки входит в список дважды.
 *   3) итог = 'fin:v2:' || строк || ':' || SHA256(строки «report_id=строк:part_digest», по report_id).
 *      report_id сериализуется через TO_JSON_STRING: NULL и '' различимы и не выпадают из STRING_AGG.
 * Никакого XOR: XOR двух одинаковых строк даёт 0, и дубль/пара изменений гасили бы друг друга.
 *
 * Размер: одна партиция ≤ 9 029 строк × 64 hex ≈ 0,58 МБ, верхний уровень 224 × ~80 Б (12.10.2026).
 * Цена: ~0,36 ГБ billed и ~3 с на зонд; при неизменных таблицах — кэш BigQuery, 0 Б.
 *
 * Остатки (stk:v1, 28.10.2026). Версия источника витрины = 'fin:v2:…|stk:v1:…'. Инцидент: снимок остатков
 * 28.10 восстановлен в 17:33 UTC, но слоты сборки шли с guard_skip (финансы не менялись), и
 * FACT_STOCKS_SNAPSHOT ждал следующих суток. stk:v1 — отпечаток ровно того входа, из которого
 * sp_bootstrap_facts строит FACT_STOCKS_SNAPSHOT: COMPLETE-манифесты WB_STOCKS_SNAPSHOTS, по одному на
 * period_to (последний по started_at), строка выбора целиком. RAW остатков дописывается только под новый
 * snapshot_id, поэтому выбор снимков + written_rows + сумма определяют вход. Смена формата версии →
 * одна безопасная пересборка при первом запуске.
 */
import { BqClient } from '../../bq/client.js';
import type { QueryRunner } from './bq.js';

/** Версия функции отпечатка. Смена функции → смена префикса → одна безопасная пересборка. */
export const FINANCE_WATERMARK_VERSION = 'fin:v2';

/** Код ERROR-события, когда версию источника узнать не удалось (сборка всё равно идёт). */
export const MART_SOURCE_WATERMARK_PROBE_FAILED = 'MART_SOURCE_WATERMARK_PROBE_FAILED';

/** Код WARNING-события: версия после сборки ≠ версии до неё → COMPLETE без версии, следующий слот пересоберёт. */
export const MART_SOURCE_CHANGED_DURING_BUILD = 'MART_SOURCE_CHANGED_DURING_BUILD';

/** Серверный таймаут зонда: зависший зонд не должен съесть таймаут Job'а (1800 с). */
export const FINANCE_WATERMARK_TIMEOUT_MS = 120_000;

const WATERMARK_RE = /^fin:v2:\d+:[0-9a-f]{64}$/;

/** Версия функции отпечатка остатков. */
export const STOCKS_WATERMARK_VERSION = 'stk:v1';

const STOCKS_WATERMARK_RE = /^stk:v1:\d+:[0-9a-f]{64}$/;

/** SQL отпечатка. Только SELECT; объект источника — тот же, что читает sp_bootstrap_facts. */
export function financeWatermarkSql(projectId: string): string {
  return financeWatermarkSqlFrom(`\`${projectId}.wb_raw.V_WB_FINANCE_SEMANTIC\``);
}

/**
 * Тот же SQL над произвольным источником строк (имя таблицы/CTE или `(подзапрос)`). Нужен для read-only
 * прогона алгоритма в BigQuery на синтетике (tools/mart_watermark_bq_check.py); шаблон скрипта сверяется
 * с этой функцией тестом, чтобы проверка и прод не разошлись.
 */
export function financeWatermarkSqlFrom(source: string): string {
  return `
WITH row_digests AS (
  SELECT TO_JSON_STRING(s.report_id) AS part_key,
         TO_HEX(SHA256(TO_JSON_STRING(s))) AS row_digest
  FROM ${source} s
),
parts AS (
  SELECT part_key,
         COUNT(*) AS part_rows,
         TO_HEX(SHA256(STRING_AGG(row_digest, '' ORDER BY row_digest))) AS part_digest
  FROM row_digests
  GROUP BY part_key
)
SELECT CONCAT('${FINANCE_WATERMARK_VERSION}:', CAST(IFNULL(SUM(part_rows), 0) AS STRING), ':',
              TO_HEX(SHA256(IFNULL(STRING_AGG(
                CONCAT(part_key, '=', CAST(part_rows AS STRING), ':', part_digest), '\\n' ORDER BY part_key), '')))
       ) AS watermark
FROM parts`;
}

/**
 * SQL отпечатка остатков. Выбор снимка — дословно как в sp_bootstrap_facts (FACT_STOCKS_SNAPSHOT__BUILD,
 * sql/mart/pr_mart1_facts.sql); тест сверяет фрагмент с файлом процедуры. Только SELECT.
 */
export function stocksWatermarkSql(projectId: string): string {
  return `
WITH pick AS (
  SELECT period_to, snapshot_id, written_rows, sum_quantity_all_t6
  FROM \`${projectId}.wb_raw.WB_STOCKS_SNAPSHOTS\`
  WHERE status = 'COMPLETE' AND period_to IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1
)
SELECT CONCAT('${STOCKS_WATERMARK_VERSION}:', CAST(COUNT(*) AS STRING), ':',
              TO_HEX(SHA256(IFNULL(STRING_AGG(TO_JSON_STRING(p), '\\n' ORDER BY p.period_to), '')))
       ) AS watermark
FROM pick p`;
}

async function probeOne(runner: QueryRunner, sql: string, version: string, re: RegExp): Promise<string> {
  const rows = await runner.query<{ watermark?: unknown }>(sql, {}, undefined, { jobTimeoutMs: FINANCE_WATERMARK_TIMEOUT_MS });
  if (rows.length !== 1) {
    throw new Error(`${version}: ожидалась 1 строка отпечатка, получено ${rows.length}`);
  }
  const w = rows[0]!.watermark;
  if (typeof w !== 'string' || !re.test(w)) {
    throw new Error(`${version}: неожиданная форма отпечатка '${String(w)}'`);
  }
  return w;
}

/** Версия остатков (stk:v1). Отказ — исключение, как у финансов. */
export function stocksWatermark(runner: QueryRunner): Promise<string> {
  return probeOne(runner, stocksWatermarkSql(runner.projectId), STOCKS_WATERMARK_VERSION, STOCKS_WATERMARK_RE);
}

/** Версия источника витрины: финансы и остатки. Отказ любого зонда — отказ целиком (сборка не пропускается). */
export async function martWatermark(runner: QueryRunner): Promise<string> {
  return `${await financeWatermark(runner)}|${await stocksWatermark(runner)}`;
}

/**
 * Версия финансов. Бросает при любой ошибке, включая неожиданную форму ответа: вызывающий (cli)
 * трактует отказ как «версия неизвестна» и НЕ пропускает сборку.
 */
export function financeWatermark(runner: QueryRunner): Promise<string> {
  return probeOne(runner, financeWatermarkSql(runner.projectId), FINANCE_WATERMARK_VERSION, WATERMARK_RE);
}

/** Проводка для реестра: реальный BqClient в location конфигурации. */
export function martSourceWatermark(config: { projectId: string; bqLocation: string }): Promise<string> {
  return martWatermark(new BqClient(config.projectId, config.bqLocation));
}
