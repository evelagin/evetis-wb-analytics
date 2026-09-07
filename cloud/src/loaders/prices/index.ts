/**
 * Наблюдатель цен WB (PR-1). READ-ONLY.
 *
 * Что делает: раз в окно наблюдения снимает текущее состояние цен ПРОДАВЦА
 * и дописывает его в append-only историю. Ничего не рекомендует, ничего не меняет.
 *
 * Чего НЕ делает и не должен начать делать без отдельного Stage:
 *   - не вызывает мутирующие методы WB (их нет в коде вовсе);
 *   - не выводит целевую цену;
 *   - не трогает акции, WB Club, рекламу и карточки.
 *
 * Уровни идемпотентности:
 *   1. cli/LOADER_RUNS — повтор того же окна после COMPLETE не запускает handler;
 *   2. детерминированный jobId load-джобы — повтор окна после ERROR не задваивает строки;
 *   3. манифест WB_PRICES_OBSERVATIONS — одна строка на окно, повтор виден как REUSED.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { fetchGoodsPrices } from './wbApi.js';
import { auditSchema, computeCoverage, normalizeGoods } from './normalize.js';
import { PricesBq } from './bq.js';
import { pricesObservationId, pricesLoadJobId } from './bucket.js';

export async function pricesLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, logicalPeriod, runId } = ctx;
  const bucket = logicalPeriod;                      // 20-минутное окно UTC из registry
  const observationId = pricesObservationId(config.environment, bucket);
  const startedAtIso = new Date().toISOString();

  const bq = new PricesBq(config.projectId, config.bqLocation, config.rawDataset);
  await bq.observationStart(config.pricesObservationsTable, {
    observationId, bucket, environment: config.environment, runId, startedAtIso,
  });

  try {
    const token = await new SecretsClient(config.projectId).access(config.wbPricesSecret);

    const fetched = await fetchGoodsPrices(
      config.wbPricesHost, token,
      { timeoutMs: config.wbHttpTimeoutMs, maxRetries: 3 },
      logger,
    );
    // Момент наблюдения фиксируем ПОСЛЕ успешного ответа: это время состояния
    // маркетплейса, а не время старта прогона.
    const observedAtIso = new Date().toISOString();

    const audit = auditSchema(fetched.items);
    if (audit.status === 'DRIFT_MISSING_FIELDS') {
      throw new LoaderError(
        `В ответе WB отсутствуют обязательные поля: ${audit.missingRequired.join(', ')}`,
        'WB_PRICES_SCHEMA_MISSING',
      );
    }
    if (audit.status === 'DRIFT_NEW_FIELDS') {
      // Не роняем прогон: WB регулярно добавляет поля. Но дрейф обязан быть виден.
      logger.warn('wb_prices_schema_drift', { unknownFields: audit.unknownFields });
    }

    const expectedMap = await bq.loadExpectedSku(config.refSkuTable);
    const observedNm = new Set<number>(
      fetched.items.map((i) => Number(i.nmID)).filter((n) => Number.isFinite(n)),
    );
    const coverage = computeCoverage(new Set(expectedMap.keys()), observedNm);

    if (coverage.observed === 0) {
      // Пустой снимок — это отказ, а не «все товары исчезли».
      throw new LoaderError('WB вернул 0 товаров — снимок не состоялся', 'WB_PRICES_EMPTY');
    }
    if (coverage.missing > 0 || coverage.unexpected > 0) {
      // Неполное покрытие не отменяет наблюдение остальных товаров, но обязано
      // отличаться операционно от полного снимка — см. V_WB_PRICES_OBSERVER_HEALTH.
      logger.warn('wb_prices_coverage_degraded', {
        expected: coverage.expected, observed: coverage.observed,
        missing: coverage.missingNmIds, unexpected: coverage.unexpectedNmIds,
        coveragePct: coverage.coveragePct,
      });
    }

    const rows = normalizeGoods(fetched.items, {
      observedAtIso, observationBucket: bucket, observationId,
      environment: config.environment, runId, skuByNm: expectedMap,
    });

    const jobId = pricesLoadJobId(config.environment, bucket, config.pricesRawTable);
    const outcome = await bq.appendRaw(config.pricesRawTable, rows, jobId);
    const written = await bq.countObservationRows(config.pricesRawTable, observationId);

    if (written === 0) {
      throw new LoaderError('После append в RAW_WB_PRICES нет строк снимка', 'WB_PRICES_POSTCOUNT_EMPTY');
    }

    await bq.observationFinalize(config.pricesObservationsTable, {
      observationId,
      status: outcome === 'REUSED' ? 'REUSED' : 'COMPLETE',
      observedAtIso,
      httpStatus: fetched.httpStatus,
      httpAttempts: fetched.attempts,
      expected: coverage.expected,
      observed: coverage.observed,
      missing: coverage.missing,
      unexpected: coverage.unexpected,
      coveragePct: coverage.coveragePct,
      missingNmIds: coverage.missingNmIds.join(',') || null,
      unexpectedNmIds: coverage.unexpectedNmIds.join(',') || null,
      rowsWritten: written,
      schemaStatus: audit.status,
      schemaUnknownFields: audit.unknownFields.join(',') || null,
      errorCode: null,
      errorMessage: null,
    });

    logger.info('wb_prices_complete', {
      observationId, bucket, outcome, pages: fetched.pages, httpAttempts: fetched.attempts,
      rowsFetched: rows.length, rowsWritten: written,
      coveragePct: coverage.coveragePct, schemaStatus: audit.status,
    });
    return { rowsFetched: rows.length, rowsLoaded: written };
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'WB_PRICES_ERROR');
    try {
      await bq.observationFinalize(config.pricesObservationsTable, {
        observationId, status: 'ERROR', observedAtIso: null,
        httpStatus: null, httpAttempts: null,
        expected: null, observed: null, missing: null, unexpected: null, coveragePct: null,
        missingNmIds: null, unexpectedNmIds: null, rowsWritten: null,
        schemaStatus: null, schemaUnknownFields: null,
        errorCode: err.code, errorMessage: err.message.slice(0, 900),
      });
    } catch (e2) {
      logger.error('wb_prices_manifest_finalize_failed', { error: e2 instanceof Error ? e2.message : String(e2) });
    }
    throw err;
  }
}
