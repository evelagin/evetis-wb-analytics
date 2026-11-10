/**
 * M5: REF Sync — версионная публикация REF_SKU_MASTER из листа SKU_MASTER в BigQuery.
 * Эталон — runRefSync / refSyncCore_ (apps-script/ingestion/Refsync.gs). Модель all-or-nothing:
 *   *_DATA (строки версии под ref_run_id, append-only) + REF_ACTIVE_VERSION (единый указатель) +
 *   вью REF_SKU_MASTER = только активная версия.
 * Провал валидации → указатель не трогается, потребители видят прежнюю версию (fail-closed).
 *
 * Журналы: собственные REF_SYNC_RUNS / REF_SYNC_TABLE_LOG — как в Apps Script (их читают
 * sql/health/dro1_02_probe.sql и sql/mart/dashboard_layer_v1.sql), плюс LOADER_RUNS из cli.ts.
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { assertRefTargets, refEnv, type RefEnv } from './targets.js';
import { buildSkuMaster, refRunId, SkuMasterShapeError, validationErrors } from './normalize.js';
import { RefBq, type RefStore } from './bq.js';
import { SheetsReadonly, type SheetReader } from './sheet.js';

export const REF_VIEW_NAME = 'REF_SKU_MASTER';

export interface RefDeps {
  env: NodeJS.ProcessEnv;
  makeStore: (e: RefEnv) => RefStore;
  makeReader: () => SheetReader;
  nowMs: () => number;
  hex8: () => string;
}

export function defaultRefDeps(ctx: LoaderContext): RefDeps {
  const { config } = ctx;
  return {
    env: process.env,
    makeStore: (e) => new RefBq(config.projectId, config.bqLocation, config.rawDataset, e.tables),
    makeReader: () => new SheetsReadonly(),
    nowMs: () => Date.now(),
    hex8: () => randomUUID().replace(/-/g, '').substring(0, 8),
  };
}

export async function refSyncLoader(ctx: LoaderContext, deps: RefDeps = defaultRefDeps(ctx)): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const e = refEnv(deps.env);
  assertRefTargets(config, e); // до чтения листа и до любой записи

  const now = deps.nowMs();
  const iso = new Date(now).toISOString();
  const runId = refRunId(now, deps.hex8());
  const store = deps.makeStore(e);

  const activeBefore = await store.activeBefore();
  await store.insertRun(runId, iso, activeBefore);
  // Указатель после прогона: меняется только успешной активацией. Отказ ПОСЛЕ неё (журнал) не
  // должен записать в REF_SYNC_RUNS прежнюю версию как текущую — в Apps Script так и было бы.
  let activeNow = activeBefore;

  try {
    // build → stage → validate
    const grid = await deps.makeReader().readGrid(e.spreadsheetId, e.sheet);
    let built;
    try {
      built = buildSkuMaster(grid, runId, iso);
    } catch (se) {
      if (se instanceof SkuMasterShapeError) throw new LoaderError(se.message, 'REF_SHEET_SHAPE');
      throw se;
    }
    await store.loadData(runId, built.rows);
    const staged = built.rows.length;
    const m = await store.validate(runId);
    const errors = validationErrors(m, staged);

    if (errors.length) {
      await store.tableLog(runId, { name: REF_VIEW_NAME, src: built.source, norm: staged, staged, published: 0, valErr: errors.length, status: 'ERROR' });
      await store.finalizeRun(runId, 'ERROR', activeBefore, `Валидация: ${errors.join('; ')}`);
      // fail-closed: указатель НЕ трогаем, потребители остаются на прежней версии
      throw new LoaderError(`Валидация REF_SKU_MASTER: ${errors.join('; ')}`, 'REF_VALIDATION');
    }

    await store.activate(runId); // одна DML — переключение
    activeNow = runId;
    await store.tableLog(runId, { name: REF_VIEW_NAME, src: built.source, norm: staged, staged, published: staged, valErr: 0, status: 'COMPLETE' });
    await store.finalizeRun(runId, 'COMPLETE', runId, '');
    try {
      await store.prune();
    } catch (pe) {
      // Retention — уборка, а не публикация: версия уже активна и журнал закрыт COMPLETE.
      logger.warn('ref_prune_failed', { code: 'REF_PRUNE_FAILED', error: pe instanceof Error ? pe.message : String(pe) });
    }

    logger.info('ref_sync_complete', {
      refRunId: runId, activeBefore, activeAfter: runId, rows: m.n, nm: m.n_nm, single: m.n_single, bundle: m.n_bundle,
    });
    return { rowsFetched: built.source, rowsLoaded: staged };
  } catch (err) {
    if (!(err instanceof LoaderError && err.code === 'REF_VALIDATION')) {
      try {
        await store.finalizeRun(runId, 'ERROR', activeNow, `Исключение: ${err instanceof Error ? err.message : String(err)}`);
      } catch (fe) {
        logger.error('ref_finalize_error_failed', { code: 'REF_FINALIZE_FAILED', error: fe instanceof Error ? fe.message : String(fe) });
      }
    }
    throw err;
  }
}
