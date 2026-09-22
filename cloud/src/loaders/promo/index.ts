/**
 * Наблюдатель акций WB (PR-PROMO-1). READ-ONLY.
 *
 * Что делает: четыре раза в сутки снимает состояние КАЛЕНДАРЯ АКЦИЙ WB и
 * дописывает его в append-only историю. Ничего не рекомендует, ничего не меняет,
 * ничего не считает.
 *
 * Чего НЕ делает и не должен начать делать без отдельного Stage:
 *   - не вызывает мутирующие методы WB (их нет в коде вовсе);
 *   - не выводит участие товаров из агрегатных счётчиков акции;
 *   - не считает вклад, маржу, требуемый рост и любую другую экономику;
 *   - не трогает цены, скидки и рекламу.
 *
 * 🔴 Главное ограничение источника. Для акций type='auto' метод
 * /calendar/promotions/nomenclatures неприменим по официальной документации WB
 * и отвечает 422. Значит поимённый состав автоакции НЕ наблюдаем. Наблюдатель
 * не пытается его восстановить: агрегат inPromoActionTotal сохраняется как факт
 * уровня акции, а поле sku_level_data_available честно говорит «нет».
 *
 * Уровни идемпотентности:
 *   1. cli/LOADER_RUNS — повтор того же слота после COMPLETE не запускает handler;
 *   2. детерминированный jobId load-джобы — повтор слота после ERROR не задваивает строки;
 *   3. манифест WB_PROMO_OBSERVATIONS — одна строка на слот, повтор виден как REUSED.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import {
  fetchPromotionDetails,
  fetchPromotionNomenclatures,
  fetchPromotions,
  type RawPromotion,
} from './wbApi.js';
import {
  auditNomenclatureSchema,
  auditSchema,
  buildCalendarRows,
  buildNomenclatureRows,
  buildRangingRows,
  isAutoPromotion,
  int,
  type NomenclatureRow,
  type PromoRowContext,
} from './normalize.js';
import { PromoBq } from './bq.js';
import { promoLoadJobId, promoObservationId } from './slot.js';
import {
  WB_PROMO_DETAILS_PATH,
  WB_PROMO_LIST_PATH,
  WB_PROMO_NOMENCLATURES_PATH,
  WB_PROMO_WINDOW_BACK_DAYS,
  WB_PROMO_WINDOW_FORWARD_DAYS,
  type NomenclatureStatus,
} from './constants.js';

const DAY_MS = 24 * 3600 * 1000;

export async function promoLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, logicalPeriod, runId } = ctx;
  const slot = logicalPeriod;
  const observationId = promoObservationId('WB', config.environment, slot);
  const startedAt = new Date();
  const windowFromIso = new Date(startedAt.getTime() - WB_PROMO_WINDOW_BACK_DAYS * DAY_MS).toISOString();
  const windowToIso = new Date(startedAt.getTime() + WB_PROMO_WINDOW_FORWARD_DAYS * DAY_MS).toISOString();

  const bq = new PromoBq(config.projectId, config.bqLocation, config.rawDataset);
  await bq.observationStart(config.promoObservationsTable, {
    observationId,
    bucket: slot,
    environment: config.environment,
    runId,
    startedAtIso: startedAt.toISOString(),
    windowFromIso,
    windowToIso,
  });

  try {
    const token = await new SecretsClient(config.projectId).access(config.wbPromoSecret);
    const httpOpts = { timeoutMs: config.wbHttpTimeoutMs, maxRetries: 3 };

    const list = await fetchPromotions(config.wbPromoHost, token, windowFromIso, windowToIso, httpOpts, logger);
    // Момент наблюдения фиксируем ПОСЛЕ успешного ответа: это время состояния
    // маркетплейса, а не время старта прогона.
    const observedAtIso = new Date().toISOString();

    const ids = list.promotions.map((p) => int(p.id)).filter((n): n is number => n !== null);
    const details = await fetchPromotionDetails(config.wbPromoHost, token, ids, httpOpts, logger);

    const audit = auditSchema(list.promotions, details.details);
    if (audit.status === 'DRIFT_MISSING_FIELDS') {
      throw new LoaderError(
        `В ответе WB отсутствуют обязательные поля: ${audit.missingRequired.join(', ')}`,
        'WB_PROMO_SCHEMA_MISSING',
      );
    }
    if (audit.status === 'DRIFT_NEW_FIELDS') {
      // Не роняем прогон: WB регулярно добавляет поля. Но дрейф обязан быть виден.
      logger.warn('wb_promo_schema_drift', { unknownFields: audit.unknownFields });
    }

    const detailsById = new Map<number, RawPromotion>();
    for (const d of details.details) {
      const id = int(d.id);
      if (id !== null) detailsById.set(id, d);
    }

    const rowCtx: PromoRowContext = {
      observedAtIso,
      observationBucket: slot,
      observationId,
      environment: config.environment,
      runId,
      sourceEndpoint: `GET ${WB_PROMO_LIST_PATH} + GET ${WB_PROMO_DETAILS_PATH}`,
    };

    // ── Состав акции: ТОЛЬКО для не-автоакций ────────────────────────────────
    // Для автоакций запрос не делается вовсе: он заведомо вернёт 422, а тратить
    // на это лимит категории (10 запросов / 6 с на всю категорию) бессмысленно.
    const nomenclatureStatusById = new Map<number, NomenclatureStatus>();
    const nomenclatureRows: NomenclatureRow[] = [];
    let nomenclatureAttempts = 0;
    let capabilityGaps = 0;
    let unknownNomenclatureFields: string[] = [];

    const regular = list.promotions.filter((p) => !isAutoPromotion(p));
    const skuByNm = regular.length > 0 ? await bq.loadSkuByNm(config.refSkuTable) : new Map<number, string>();

    for (const p of list.promotions) {
      const id = int(p.id);
      if (id === null) continue;
      if (isAutoPromotion(p)) {
        nomenclatureStatusById.set(id, 'SKIPPED_AUTO_PROMOTION');
        capabilityGaps += 1;
        continue;
      }
      let status: NomenclatureStatus = 'EMPTY';
      for (const inAction of [true, false]) {
        const out = await fetchPromotionNomenclatures(config.wbPromoHost, token, id, inAction, httpOpts, logger);
        nomenclatureAttempts += 1;
        if (out.kind === 'UNSUPPORTED_422') {
          status = 'UNSUPPORTED_422';
          capabilityGaps += 1;
          break;
        }
        if (out.kind === 'FETCHED') {
          status = 'FETCHED';
          unknownNomenclatureFields = [
            ...new Set([...unknownNomenclatureFields, ...auditNomenclatureSchema(out.items)]),
          ].sort();
          nomenclatureRows.push(
            ...buildNomenclatureRows(id, inAction, out.items, skuByNm, {
              ...rowCtx,
              sourceEndpoint: `GET ${WB_PROMO_NOMENCLATURES_PATH}`,
            }),
          );
        }
      }
      nomenclatureStatusById.set(id, status);
    }

    const calendarRows = buildCalendarRows({ list: list.promotions, detailsById, nomenclatureStatusById }, rowCtx);
    const rangingRows = buildRangingRows(details.details, {
      ...rowCtx,
      sourceEndpoint: `GET ${WB_PROMO_DETAILS_PATH}`,
    });

    if (calendarRows.length === 0) {
      // Пустой календарь возможен: акции — целиком дело площадки, а не наш
      // ассортимент. Это НЕ отказ, но обязано быть видно в телеметрии.
      logger.warn('wb_promo_empty_calendar', { windowFromIso, windowToIso });
    }

    const appends: Array<[string, number]> = [
      [
        await bq.appendRows(
          config.promoCalendarTable,
          calendarRows,
          promoLoadJobId('WB', config.environment, slot, config.promoCalendarTable),
        ),
        calendarRows.length,
      ],
      [
        await bq.appendRows(
          config.promoRangingTable,
          rangingRows,
          promoLoadJobId('WB', config.environment, slot, config.promoRangingTable),
        ),
        rangingRows.length,
      ],
      [
        await bq.appendRows(
          config.promoNomenclatureTable,
          nomenclatureRows,
          promoLoadJobId('WB', config.environment, slot, config.promoNomenclatureTable),
        ),
        nomenclatureRows.length,
      ],
    ];
    // REUSED считаем ТОЛЬКО по таблицам, в которые реально были строки: пустая
    // запись возвращает LOADED, не обращаясь к BigQuery, и её исход ничего не
    // говорит о повторе слота. Без этой оговорки снимок, где состав акций пуст
    // (а сегодня он пуст всегда — автоакции его не отдают), навсегда оставался бы
    // COMPLETE даже при повторном прогоне того же слота.
    const meaningful = appends.filter(([, n]) => n > 0);
    const reused = meaningful.length > 0 && meaningful.every(([o]) => o === 'REUSED');

    const written = await bq.countObservationRows(config.promoCalendarTable, observationId);
    if (calendarRows.length > 0 && written === 0) {
      throw new LoaderError('После append в RAW_WB_PROMO_CALENDAR нет строк снимка', 'WB_PROMO_POSTCOUNT_EMPTY');
    }

    const mappedNm = nomenclatureRows.filter((r) => r.internal_sku !== null).length;
    const unmappedNm = nomenclatureRows.length - mappedNm;
    const detailsEmpty = calendarRows.filter((r) => !r.details_available).length;
    const autoCount = calendarRows.filter((r) => r.is_auto_promotion === true).length;

    await bq.observationFinalize(config.promoObservationsTable, {
      observationId,
      status: reused ? 'REUSED' : 'COMPLETE',
      observedAtIso,
      httpStatus: list.httpStatus,
      httpAttempts: list.attempts + details.attempts + nomenclatureAttempts,
      promotionsListed: list.promotions.length,
      promotionsDetailed: details.details.length,
      detailsEmpty,
      autoPromotions: autoCount,
      regularPromotions: calendarRows.length - autoCount,
      rangingRows: rangingRows.length,
      nomenclatureRows: nomenclatureRows.length,
      nomenclatureAttempts,
      capabilityGapCount: capabilityGaps,
      unmappedNmIds: unmappedNm,
      mappingCoveragePct:
        nomenclatureRows.length === 0 ? null : Math.round((mappedNm / nomenclatureRows.length) * 10000) / 100,
      rowsWritten: calendarRows.length + rangingRows.length + nomenclatureRows.length,
      schemaStatus: audit.status,
      schemaUnknownFields:
        [...audit.unknownFields, ...unknownNomenclatureFields].join(',') || null,
      errorCode: null,
      errorMessage: null,
    });

    logger.info('wb_promo_complete', {
      observationId,
      slot,
      reused,
      promotions: calendarRows.length,
      autoPromotions: autoCount,
      regularPromotions: calendarRows.length - autoCount,
      rangingRows: rangingRows.length,
      nomenclatureRows: nomenclatureRows.length,
      capabilityGaps,
      schemaStatus: audit.status,
    });

    const total = calendarRows.length + rangingRows.length + nomenclatureRows.length;
    return { rowsFetched: total, rowsLoaded: total };
  } catch (e) {
    const err =
      e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'WB_PROMO_ERROR');
    try {
      await bq.observationFinalize(config.promoObservationsTable, {
        observationId,
        status: 'ERROR',
        observedAtIso: null,
        httpStatus: null,
        httpAttempts: null,
        promotionsListed: null,
        promotionsDetailed: null,
        detailsEmpty: null,
        autoPromotions: null,
        regularPromotions: null,
        rangingRows: null,
        nomenclatureRows: null,
        nomenclatureAttempts: null,
        capabilityGapCount: null,
        unmappedNmIds: null,
        mappingCoveragePct: null,
        rowsWritten: null,
        schemaStatus: null,
        schemaUnknownFields: null,
        errorCode: err.code,
        errorMessage: err.message.slice(0, 900),
      });
    } catch (e2) {
      logger.error('wb_promo_manifest_finalize_failed', {
        error: e2 instanceof Error ? e2.message : String(e2),
      });
    }
    throw err;
  }
}
