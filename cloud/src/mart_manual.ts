/**
 * Dedicated ручной прогон витрины `mart` (PR-Mart3b-1). ЕДИНСТВЕННЫЙ путь запуска mart
 * в PR-Mart3b-1: в generic CLI mart намеренно НЕ зарегистрирован (блокер #3), его wiring
 * (+D-1, +LOADER_RUNS run-lease) переносится в PR-Mart3b-2.
 *
 * Строит ctx (targetDate = D-1 МСК, runId) и зовёт martLoader напрямую, В ОБХОД generic run-lease.
 *
 * 🔒 Fail-closed guards (аудит REV4 #1, см. loaders/mart/manualGuard.ts):
 *   - ТОЛЬКО ENVIRONMENT=prod (процедуры публикуют production wb_mart; отдельного shadow-датасета нет);
 *   - --target-date обязан быть РЕАЛЬНОЙ календарной датой И строго D-1 Europe/Moscow
 *     (исторический backfill — отдельный будущий entry-point со своим контрактом, не этот).
 *
 * Использование:
 *   npm run build && npm run mart:manual                      # target = D-1 автоматически
 *   npm run build && npm run mart:manual -- --target-date=<D-1>  # явный D-1 (иная дата отклоняется)
 *   (env: GCP_PROJECT_ID, BQ_RAW_DATASET, ENVIRONMENT=prod, GIT_SHA=<sha>, [IMAGE_DIGEST])
 */
import { randomUUID } from 'node:crypto';
import { loadConfig } from './config.js';
import { Logger, parseLevel } from './logging.js';
import { EXIT_OK, EXIT_ERROR } from './errors.js';
import { martLoader } from './loaders/mart/index.js';
import { d1Moscow } from './loaders/mart/targetDate.js';
import { assertManualRunAllowed } from './loaders/mart/manualGuard.js';

function parseTargetDate(argv: string[]): string {
  const arg = argv.find((a) => a.startsWith('--target-date='));
  return arg ? arg.slice('--target-date='.length).trim() : d1Moscow();
}

async function main(): Promise<number> {
  const targetDate = parseTargetDate(process.argv);
  const config = loadConfig({ ...process.env, LOADER_NAME: 'mart' });
  // Fail-closed guards ДО вызова loader (аудит REV4 #1): только prod, только реальная дата D-1.
  // Процедуры публикуют production wb_mart — shadow/историческая дата здесь запрещены.
  try {
    assertManualRunAllowed(config.environment, targetDate);
  } catch (e) {
    // eslint-disable-next-line no-console
    console.error(e instanceof Error ? e.message : String(e));
    return EXIT_ERROR;
  }
  const logger = new Logger(
    { loader: 'mart', environment: config.environment, gitSha: config.gitSha, entry: 'mart_manual' },
    parseLevel(config.logLevel),
  );
  const runId = `${config.environment}:mart:${targetDate}:${config.gitSha}:manual-${randomUUID()}`;
  logger.info('mart_manual_start', { targetDate, runId });
  try {
    const res = await martLoader({ config, logger, logicalPeriod: targetDate, runId, targetDate });
    logger.info('mart_manual_done', { rowsLoaded: res.rowsLoaded });
    return EXIT_OK;
  } catch (e) {
    logger.error('mart_manual_failed', { message: e instanceof Error ? e.message : String(e) });
    return EXIT_ERROR;
  }
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    // eslint-disable-next-line no-console
    console.error(JSON.stringify({ severity: 'ERROR', message: 'fatal', error: e instanceof Error ? e.message : String(e) }));
    process.exit(EXIT_ERROR);
  });
