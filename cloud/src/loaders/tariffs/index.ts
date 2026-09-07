/**
 * Загрузчик тарифов WB (PR-2). READ-ONLY.
 *
 * Четыре GET-эндпоинта категории «Тарифы». Мутирующих методов нет.
 * Каденс — сутки: ставки WB меняются реже раза в месяц, и опрос чаще
 * тратил бы лимит без единицы новой информации.
 *
 * Отказ одного вида тарифа не отменяет остальные (изоляция), но попадает
 * в kinds_failed и снижает здоровье наблюдения. Пустой ответ — отказ,
 * а не «тарифов нет».
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import { wbFetch } from '../../http/wbHttp.js';
import { normalizeTariffRows, type TariffKind, type TariffRow } from './normalize.js';
import { TariffsBq } from './bq.js';

interface KindSpec {
  kind: TariffKind;
  path: (date: string) => string;
  /** Достаёт массив строк тарифа и даты действия из тела ответа. */
  extract: (body: Record<string, unknown>) => { rows: Array<Record<string, unknown>>; next?: unknown; tillMax?: unknown };
}

/**
 * Формы ответов у эндпоинтов разные: commission отдаёт `report[]` на верхнем
 * уровне, складские — `response.data.warehouseList[]` с датами действия рядом.
 */
export const KINDS: readonly KindSpec[] = [
  {
    kind: 'COMMISSION',
    path: () => '/api/v1/tariffs/commission',
    extract: (b) => ({ rows: (b.report as Array<Record<string, unknown>>) ?? [] }),
  },
  {
    kind: 'BOX',
    path: (d) => `/api/v1/tariffs/box?date=${d}`,
    extract: (b) => {
      const data = ((b.response as Record<string, unknown>)?.data ?? {}) as Record<string, unknown>;
      return { rows: (data.warehouseList as Array<Record<string, unknown>>) ?? [], next: data.dtNextBox, tillMax: data.dtTillMax };
    },
  },
  {
    kind: 'RETURN',
    path: (d) => `/api/v1/tariffs/return?date=${d}`,
    extract: (b) => {
      const data = ((b.response as Record<string, unknown>)?.data ?? {}) as Record<string, unknown>;
      return { rows: (data.warehouseList as Array<Record<string, unknown>>) ?? [], tillMax: data.dtTillMax };
    },
  },
  {
    kind: 'PALLET',
    path: (d) => `/api/v1/tariffs/pallet?date=${d}`,
    extract: (b) => {
      const data = ((b.response as Record<string, unknown>)?.data ?? {}) as Record<string, unknown>;
      return { rows: (data.warehouseList as Array<Record<string, unknown>>) ?? [], next: data.dtNextPallet, tillMax: data.dtTillMax };
    },
  },
];

export function tariffsObservationId(environment: string, date: string): string {
  return `WBTF_${environment}_${date.replace(/-/g, '')}`;
}

export function tariffsLoadJobId(environment: string, date: string): string {
  return `wbtariffs_${environment}_${date.replace(/-/g, '')}`;
}

export async function tariffsLoader(ctx: LoaderContext): Promise<LoaderResult> {
  const { config, logger, logicalPeriod, runId } = ctx;
  const date = logicalPeriod; // YYYY-MM-DD (UTC-сутки из registry)
  const observationId = tariffsObservationId(config.environment, date);
  const startedAtIso = new Date().toISOString();

  const bq = new TariffsBq(config.projectId, config.bqLocation, config.rawDataset);
  await bq.observationStart(config.tariffsObservationsTable, {
    observationId, date, environment: config.environment, runId, startedAtIso,
  });

  try {
    const token = await new SecretsClient(config.projectId).access(config.wbPricesSecret);
    const rows: TariffRow[] = [];
    const ok: string[] = [];
    const failed: string[] = [];
    let lastStatus = 0;
    const observedAtIso = new Date().toISOString();

    for (const spec of KINDS) {
      const url = `${config.wbTariffsHost}${spec.path(date)}`;
      const res = await wbFetch(url, { method: 'GET', headers: { Authorization: token } },
        { timeoutMs: config.wbHttpTimeoutMs, maxRetries: 3 }, logger);
      lastStatus = res.status;
      if (!res.ok) {
        // Изоляция отказов: один вид тарифа не роняет остальные.
        failed.push(`${spec.kind}:${res.status}`);
        logger.warn('wb_tariff_kind_failed', { kind: spec.kind, status: res.status });
        continue;
      }
      let body: Record<string, unknown>;
      try {
        body = JSON.parse(res.body) as Record<string, unknown>;
      } catch {
        failed.push(`${spec.kind}:BAD_JSON`);
        continue;
      }
      const { rows: raw, next, tillMax } = spec.extract(body);
      if (!Array.isArray(raw) || raw.length === 0) {
        failed.push(`${spec.kind}:EMPTY`);
        logger.warn('wb_tariff_kind_empty', { kind: spec.kind });
        continue;
      }
      rows.push(...normalizeTariffRows(spec.kind, raw, `GET ${spec.path(date)}`,
        { observedAtIso, observationDate: date, observationId, environment: config.environment, runId },
        { next, tillMax }));
      ok.push(spec.kind);
    }

    if (ok.length === 0) {
      throw new LoaderError('Ни один вид тарифа не получен', 'WB_TARIFF_ALL_FAILED');
    }
    // Комиссия — единственный вид, без которого экономика не считается вовсе.
    if (!ok.includes('COMMISSION')) {
      throw new LoaderError('Не получен тариф комиссии — экономика без него недействительна', 'WB_TARIFF_NO_COMMISSION');
    }

    const outcome = await bq.appendRaw(config.tariffsRawTable, rows, tariffsLoadJobId(config.environment, date));
    const written = await bq.countObservationRows(config.tariffsRawTable, observationId);
    if (written === 0) throw new LoaderError('После append нет строк снимка тарифов', 'WB_TARIFF_POSTCOUNT_EMPTY');

    const unparsed = rows.filter((r) => !r.is_parsed).length;
    await bq.observationFinalize(config.tariffsObservationsTable, {
      observationId, status: outcome === 'REUSED' ? 'REUSED' : 'COMPLETE', observedAtIso,
      kindsRequested: KINDS.map((k) => k.kind).join(','),
      kindsOk: ok.join(','), kindsFailed: failed.join(',') || null,
      rowsWritten: written, httpStatus: lastStatus,
      schemaStatus: failed.length > 0 ? 'DRIFT_MISSING_FIELDS' : 'OK',
      schemaUnknownFields: null, errorCode: null, errorMessage: null,
    });

    logger.info('wb_tariffs_complete', {
      observationId, date, outcome, kindsOk: ok, kindsFailed: failed,
      rowsFetched: rows.length, rowsWritten: written, unparsedValues: unparsed,
    });
    return { rowsFetched: rows.length, rowsLoaded: written };
  } catch (e) {
    const err = e instanceof LoaderError ? e : new LoaderError(e instanceof Error ? e.message : String(e), 'WB_TARIFF_ERROR');
    try {
      await bq.observationFinalize(config.tariffsObservationsTable, {
        observationId, status: 'ERROR', observedAtIso: null, kindsRequested: KINDS.map((k) => k.kind).join(','),
        kindsOk: null, kindsFailed: null, rowsWritten: null, httpStatus: null,
        schemaStatus: null, schemaUnknownFields: null,
        errorCode: err.code, errorMessage: err.message.slice(0, 900),
      });
    } catch (e2) {
      logger.error('wb_tariffs_manifest_finalize_failed', { error: e2 instanceof Error ? e2.message : String(e2) });
    }
    throw err;
  }
}
