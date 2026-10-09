/**
 * PHASE C (OWNER ACK 09.10.2026) — загрузчик `unitka-store-pnl`: P&L магазина WB.
 *
 * Прогон: лист Юнитки ТОЛЬКО ЧТЕНИЕМ (Sheets scope readonly) → снимок компонент SKU в
 * wb_ops.UNITKA_SKU_COMPONENTS_DAILY (атомарно) → чтение wb_mart.V_WB_STORE_PNL_MONTHLY →
 * план вкладки «WB Магазин P&L» в журнал (отпечаток, строки, состояния). Engine Юнитки не затрагивается.
 *
 * AUTO-PUBLISH (по умолчанию ВЫКЛЮЧЕН): при UNITKA_STORE_PNL_PUBLISH=1 план публикуется ТОЛЬКО во вкладку
 * с sheetId = UNITKA_STORE_PNL_SHEET_ID (publish.ts: идемпотентно, ручные правки → отказ, metadata-отпечаток).
 * Без флага шлюз Sheets остаётся readonly (scope spreadsheets.readonly) — запись невозможна.
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
import { decidePublish, publishRequests, publishGate, metaOnly, PUBLISH_META_KEY, TAB_COLS } from './publish.js';
import { STORE_PNL_FROM } from './parse.js';
import type { NewOperationRow } from './bq.js';

export interface PublishEnv { enabled: boolean; sheetId?: number; adoptSha?: string }

export function publishEnv(env: NodeJS.ProcessEnv): PublishEnv {
  const enabled = (env.UNITKA_STORE_PNL_PUBLISH ?? '0').trim() === '1';
  const sid = (env.UNITKA_STORE_PNL_SHEET_ID ?? '').trim();
  const adopt = (env.UNITKA_STORE_PNL_ADOPT_SHA ?? '').trim();
  if (enabled && !/^\d+$/.test(sid)) throw new LoaderError('UNITKA_STORE_PNL_PUBLISH=1 требует UNITKA_STORE_PNL_SHEET_ID', 'STORE_PNL_CONFIG');
  if (adopt && !/^[0-9a-f]{64}$/.test(adopt)) throw new LoaderError('UNITKA_STORE_PNL_ADOPT_SHA: нужен sha256 (64 hex)', 'STORE_PNL_CONFIG');
  return { enabled, sheetId: sid ? Number(sid) : undefined, adoptSha: adopt || undefined };
}

/**
 * Предупреждать только об операциях активного горизонта: новые операции (вне карты) — всегда,
 * PENDING — если встречались с начала окна P&L. Исторические PENDING 2024–2025 — шум, не сигнал;
 * состояние месяца по-прежнему считает их в окне месяца (вью), детектор не ослаблен.
 */
export function activeNewOperations(rows: readonly NewOperationRow[], from: string = STORE_PNL_FROM): NewOperationRow[] {
  const d = (v: unknown): string => String(v && typeof v === 'object' && 'value' in v ? (v as { value: unknown }).value : v ?? '');
  return rows.filter((o) => o.treatment === 'NEW_FINANCE_OPERATION' || d(o.last_seen) >= from);
}

export interface StorePnlDeps {
  makeSheets: (ctx: LoaderContext, readonly: boolean) => Pick<SheetsGateway, 'readSheetMeta' | 'readValues' | 'structureWrite'>
    & Pick<SheetsRest, 'readSheetMetadata'>;
  makeBq: (ctx: LoaderContext) => Pick<StorePnlBq, 'appendSnapshot' | 'readPnl' | 'readNewOperations'>;
  now: () => Date;
}

export const defaultStorePnlDeps: StorePnlDeps = {
  // readonly = true: токен со scope spreadsheets.readonly — запись в книгу невозможна даже ошибкой.
  makeSheets: (ctx, readonly) => new SheetsRest(ctx.config.unitkaSpreadsheetId, readonly),
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
  const pub = publishEnv(process.env);
  const sheets = deps.makeSheets(ctx, !pub.enabled);
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
  const newOps = activeNewOperations(await bq.readNewOperations());
  if (newOps.length) logger.warn('store_pnl_new_operations', { operations: newOps.map((o) => ({ op: o.supplier_oper_name, treatment: o.treatment, rows: Number(o.rows_n) })) });
  logger.info('store_pnl_tab_plan', { tab: plan.tab, range: plan.range, fingerprint: plan.fingerprint, header: plan.header, rows: plan.rows,
    snapshotId: snap.snapshotId, write: pub.enabled ? 'ENABLED' : 'DISABLED' });

  if (pub.enabled) {
    const gate = publishGate(pnl);
    if (gate.length) throw new LoaderError(`публикация остановлена гейтом модели: ${gate.join('; ')}`, 'STORE_PNL_PUBLISH_GATE');
    const sheetId = pub.sheetId!;
    const readTab = async (): Promise<{ title: string; rowCount: number; metas: Array<{ id: number; key: string; value: string }>; values: CellValue[][] }> => {
      const tab = await sheets.readSheetMetadata(sheetId);
      if (!tab || tab.title !== plan.tab) {
        throw new LoaderError(`вкладка sheetId=${sheetId} ${tab ? `называется «${tab.title}»` : 'не найдена'}, ожидалась «${plan.tab}»`, 'STORE_PNL_TAB_MISSING');
      }
      const metas = tab.metadata.filter((m) => m.key === PUBLISH_META_KEY);
      if (metas.length > 1) throw new LoaderError(`${metas.length} отпечатков публикации у вкладки`, 'STORE_PNL_TAB_EDITED');
      // Вся сетка вкладки в колонках A:Q (чтение за пределом сетки Sheets обрезает сам).
      const [values] = await sheets.readValues([`'${plan.tab}'!A1:${colA1(TAB_COLS)}${Math.max(tab.rowCount, 1)}`]);
      return { title: tab.title, rowCount: tab.rowCount, metas, values: values ?? [] };
    };
    const t = await readTab();
    const d = decidePublish({ current: t.values, publishedSha: t.metas[0]?.value, adoptSha: pub.adoptSha, plan });
    if (d.action === 'REFUSE') throw new LoaderError(d.message, d.code);
    if (d.action === 'NOOP') {
      if (d.setMeta) await sheets.structureWrite(metaOnly(sheetId, d.sha, t.metas[0]?.id));
      logger.info('store_pnl_tab_published', { action: 'NOOP', sheetId, sha: d.sha, fingerprint: plan.fingerprint, metaUpdated: d.setMeta });
    } else {
      await sheets.structureWrite(publishRequests(plan, sheetId, t.rowCount, d, t.metas[0]?.id));
      logger.info('store_pnl_tab_published', { action: 'WRITE', sheetId, sha: d.sha, previousSha: d.previousSha, adopted: d.adopted,
        fingerprint: plan.fingerprint, rows: plan.rows.length + 1, clearedRows: d.clearRows });
    }
    // Перечитывание: на вкладке ровно план, и ровно один отпечаток, равный ему.
    const after = await readTab();
    const check = decidePublish({ current: after.values, publishedSha: after.metas[0]?.value, plan });
    if (check.action !== 'NOOP' || check.setMeta) {
      throw new LoaderError('перечитанная вкладка или её отпечаток не равны опубликованному плану', 'STORE_PNL_PUBLISH_READBACK');
    }
  } else {
    const gate = publishGate(pnl);
    if (gate.length) logger.warn('store_pnl_publish_gate', { failures: gate });
  }
  return { rowsFetched: parsed.rows.length, rowsLoaded: parsed.rows.length };
}
