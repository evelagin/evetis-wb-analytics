/**
 * PHASE C (OWNER ACK 09.10.2026) — загрузчик `unitka-store-pnl`: P&L магазина WB.
 *
 * Прогон: лист Юнитки ТОЛЬКО ЧТЕНИЕМ (Sheets scope readonly) → снимок компонент SKU в
 * wb_ops.UNITKA_SKU_COMPONENTS_DAILY (атомарно) → чтение wb_mart.V_WB_STORE_PNL_MONTHLY →
 * план вкладки «WB Магазин P&L» в журнал (отпечаток, строки, состояния). Лист не пишется:
 * в этом загрузчике нет пути записи в Sheets. Engine Юнитки не затрагивается.
 *
 * Новые операции финотчёта WB (вне утверждённой карты) не роняют прогон: их видно в журнале
 * (store_pnl_new_operations, warn) и в состоянии месяца PENDING_CLASSIFICATION.
 */
import { randomUUID } from 'node:crypto';
import type { LoaderContext, LoaderResult } from '../../types.js';
import { LoaderError } from '../../../errors.js';
import { SheetsRest, type SheetsGateway } from '../sheets.js';
import { colA1, NAMED, serialToIso, type CellValue } from '../model.js';
import { quoteSheet } from '../section.js';
import { parseUnitkaComponents } from './parse.js';
import { StorePnlBq } from './bq.js';
import { buildTabPlan } from './tabplan.js';

export interface StorePnlDeps {
  makeSheets: (ctx: LoaderContext) => Pick<SheetsGateway, 'readSheetMeta' | 'readValues'>;
  makeBq: (ctx: LoaderContext) => Pick<StorePnlBq, 'appendSnapshot' | 'readPnl' | 'readNewOperations'>;
  now: () => Date;
}

export const defaultStorePnlDeps: StorePnlDeps = {
  // readonly = true: токен со scope spreadsheets.readonly — запись в книгу невозможна даже ошибкой.
  makeSheets: (ctx) => new SheetsRest(ctx.config.unitkaSpreadsheetId, true),
  makeBq: (ctx) => new StorePnlBq(ctx.config.projectId, ctx.config.bqLocation, ctx.config.unitkaOpsDataset, ctx.config.unitkaMartDataset),
  now: () => new Date(),
};

/** 2026-10-09T07:40:01.234Z → 20261009T074001234Z: сортируется как время. */
export function snapshotIdOf(d: Date): string {
  return d.toISOString().replace(/[-:.]/g, '');
}

function single(v: CellValue[][] | undefined, name: string): CellValue {
  const x = v?.[0]?.[0];
  if (x === undefined || x === null || x === '') throw new LoaderError(`именованный диапазон ${name} пуст`, 'STORE_PNL_SHEET_ERROR');
  return x;
}

export async function unitkaStorePnlLoader(ctx: LoaderContext, deps: StorePnlDeps = defaultStorePnlDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  if (config.environment !== 'prod') {
    throw new LoaderError('unitka-store-pnl пишет снимок в wb_ops и запускается только в prod', 'STORE_PNL_PROD_ONLY');
  }
  // Изоляция маркетплейсов: только домен WB.
  if (config.rawDataset !== 'wb_raw' || config.unitkaOpsDataset !== 'wb_ops' || config.unitkaMartDataset !== 'wb_mart') {
    throw new LoaderError(`датасеты ${config.rawDataset}/${config.unitkaOpsDataset}/${config.unitkaMartDataset}: P&L магазина WB живёт только в wb_*`, 'STORE_PNL_CONFIG');
  }
  const sheets = deps.makeSheets(ctx);
  const bq = deps.makeBq(ctx);
  const name = config.unitkaSheetName;

  const meta = await sheets.readSheetMeta(name, false);
  const [grid, lcdV, revV] = await sheets.readValues([
    `${quoteSheet(name)}!A1:${colA1(meta.columnCount)}${meta.rowCount}`, NAMED.LCD, NAMED.REVERSE,
  ]);
  const lcdRaw = single(lcdV, NAMED.LCD);
  const lcd = typeof lcdRaw === 'number' ? serialToIso(lcdRaw) : String(lcdRaw).trim();
  const rev = Number(single(revV, NAMED.REVERSE));
  const parsed = parseUnitkaComponents(grid ?? [], lcd, rev);

  const now = deps.now();
  const snap = { snapshotId: snapshotIdOf(now), runId: ctx.runId || `store_pnl_${randomUUID()}`, snapshotAt: now.toISOString(),
    lcd, imageDigest: config.imageDigest, gitSha: config.gitSha };
  logger.info('store_pnl_sheet_read', { lcd, reverseLegRate: rev, sections: parsed.sections, rows: parsed.rows.length,
    months: Object.fromEntries(Object.entries(parsed.months).map(([m, t]) => [m, { ...t, profit: Math.round(t.profit * 100) / 100,
      modelGapAbs: Math.round(t.modelGapAbs * 100) / 100 }])) });

  await bq.appendSnapshot(parsed.rows, snap, `store_pnl_${snap.snapshotId}_${randomUUID().replace(/-/g, '').slice(0, 8)}`);
  logger.info('store_pnl_snapshot_appended', { snapshotId: snap.snapshotId, rows: parsed.rows.length, lcd });

  const pnl = await bq.readPnl();
  const plan = buildTabPlan(pnl);
  if (plan.rowIdentityMaxAbs > 0.02) {
    throw new LoaderError(`строка плана не складывается: расхождение ${plan.rowIdentityMaxAbs} ₽`, 'STORE_PNL_IDENTITY');
  }
  const newOps = await bq.readNewOperations();
  if (newOps.length) logger.warn('store_pnl_new_operations', { operations: newOps.map((o) => ({ op: o.supplier_oper_name, treatment: o.treatment, rows: Number(o.rows_n) })) });
  logger.info('store_pnl_tab_plan', { tab: plan.tab, range: plan.range, fingerprint: plan.fingerprint, header: plan.header, rows: plan.rows,
    snapshotId: snap.snapshotId, write: 'DISABLED' });
  return { rowsFetched: parsed.rows.length, rowsLoaded: parsed.rows.length };
}
