/**
 * D4 (OWNER ACK 15.10.2026) — загрузчик `ozon-store-pnl`: публикация управленческого P&L магазина Ozon во вкладку
 * «Ozon Магазин P&L» книги Юнитки.
 *
 * Прогон: ozon_mart.V_OZON_STORE_PNL_MONTHLY + ворота состояния + детектор и покрытие + канонический QA D3 Q01–Q23 —
 * ТОЛЬКО чтение → план вкладки (отпечаток, строки, состояния) в журнал → гейты (источники, модель, QA). Без
 * OZON_STORE_PNL_PUBLISH=1 Sheets не открывается вовсе: прогон — только план и гейты.
 *
 * С флагом: вкладка sheetId = OZON_STORE_PNL_SHEET_ID (название обязано совпасть), отпечаток остальных вкладок книги
 * до записи, решение NOOP / WRITE / REFUSE (ручные правки → отказ), манифест отката (прежнее содержимое) в журнал,
 * один атомарный batchUpdate, перечитывание: значения = план, отпечаток = план, контракт = код, заметки = контракт,
 * отпечаток остальных вкладок не изменился. Любое расхождение — ошибка с кодом (алерт).
 *
 * Не читает и не пишет листы Юнитки, WB P&L и любые другие вкладки; в BigQuery не пишет (кроме аренды guard'а cli).
 */
import type { LoaderContext, LoaderResult } from '../../types.js';
import { LoaderError } from '../../../errors.js';
import { SheetsRest } from '../sheets.js';
import { colA1, type CellValue } from '../model.js';
import { OzonStorePnlBq, type GateInputs } from './bq.js';
import { buildTabPlan, HEADER_ROW, TAB_NOTES, toStr, type PnlRow } from './tabplan.js';
import { contractSha, CONTRACT_META_KEY, decideContract, decidePublish, metaOnly, normalizeGrid, otherTabsFingerprint,
  publishGate, publishRequests, PUBLISH_META_KEY, TAB_COLS, type TabStructure } from './publish.js';

export interface PublishEnv { enabled: boolean; sheetId?: number; adoptSha?: string; migrateContract?: string }

export function publishEnv(env: NodeJS.ProcessEnv): PublishEnv {
  const enabled = (env.OZON_STORE_PNL_PUBLISH ?? '0').trim() === '1';
  const sid = (env.OZON_STORE_PNL_SHEET_ID ?? '').trim();
  const adopt = (env.OZON_STORE_PNL_ADOPT_SHA ?? '').trim();
  const mig = (env.OZON_STORE_PNL_MIGRATE_CONTRACT ?? '').trim();
  if (enabled && !/^\d+$/.test(sid)) throw new LoaderError('OZON_STORE_PNL_PUBLISH=1 требует OZON_STORE_PNL_SHEET_ID', 'OZON_STORE_PNL_CONFIG');
  if (adopt && !/^[0-9a-f]{64}$/.test(adopt)) throw new LoaderError('OZON_STORE_PNL_ADOPT_SHA: нужен sha256 (64 hex)', 'OZON_STORE_PNL_CONFIG');
  if (mig && !/^[0-9a-f]{64}$/.test(mig)) throw new LoaderError('OZON_STORE_PNL_MIGRATE_CONTRACT: нужен sha256 (64 hex)', 'OZON_STORE_PNL_CONFIG');
  return { enabled, sheetId: sid ? Number(sid) : undefined, adoptSha: adopt || undefined, migrateContract: mig || undefined };
}

type SheetsLike = Pick<SheetsRest, 'readSheetMetadata' | 'readValues' | 'readNotes' | 'structureWrite' | 'readSheetsList' | 'readSheetStructure'>;
export interface OzonStorePnlDeps {
  makeSheets: (ctx: LoaderContext, readonly: boolean) => SheetsLike;
  makeBq: (ctx: LoaderContext) => Pick<OzonStorePnlBq, 'readPnl' | 'readGateInputs' | 'readStates' | 'runQa'>;
}

export const defaultOzonStorePnlDeps: OzonStorePnlDeps = {
  // readonly = true: токен со scope spreadsheets.readonly — запись в книгу невозможна даже ошибкой.
  makeSheets: (ctx, readonly) => new SheetsRest(ctx.config.unitkaSpreadsheetId, readonly),
  makeBq: (ctx) => new OzonStorePnlBq(ctx.config.projectId, ctx.config.bqLocation),
};

/** Сводка состояний месяцев для журнала: сколько месяцев в каком состоянии. */
export function stateSummary(pnl: readonly PnlRow[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const p of pnl) out[toStr(p.financial_state)] = (out[toStr(p.financial_state)] ?? 0) + 1;
  return out;
}

export async function ozonStorePnlLoader(ctx: LoaderContext, deps: OzonStorePnlDeps = defaultOzonStorePnlDeps): Promise<LoaderResult> {
  const { config, logger } = ctx;
  if (config.environment !== 'prod') {
    throw new LoaderError('ozon-store-pnl читает production ozon_mart и запускается только в prod', 'OZON_STORE_PNL_PROD_ONLY');
  }
  const pub = publishEnv(process.env);
  const bq = deps.makeBq(ctx);
  const pnl = await bq.readPnl();
  const gi: GateInputs = await bq.readGateInputs();
  const states = await bq.readStates();
  const qa = await bq.runQa();
  const plan = buildTabPlan(pnl, states, true);
  const gate = publishGate(pnl, gi, qa);
  logger.info('ozon_store_pnl_qa', { results: qa.map((r) => ({ id: r.id, status: r.status })),
    warn: qa.filter((r) => r.status === 'WARN').map((r) => ({ id: r.id, detail: r.detail })) });
  logger.info('ozon_store_pnl_tab_plan', { tab: plan.tab, range: plan.range, fingerprint: plan.fingerprint, rows: plan.rows.length, gridRows: plan.grid.length,
    states: stateSummary(pnl), contract: contractSha(), gateInputs: gi, sourceGate: gate.source, modelGate: gate.model,
    write: pub.enabled ? 'ENABLED' : 'DISABLED' });

  if (!pub.enabled) {
    if (gate.source.length || gate.model.length) logger.warn('ozon_store_pnl_publish_gate', { source: gate.source, model: gate.model });
    return { rowsFetched: pnl.length, rowsLoaded: 0 };
  }
  if (gate.model.length) throw new LoaderError(`публикация заблокирована гейтом модели: ${gate.model.join('; ')}`, 'OZON_STORE_PNL_PUBLISH_GATE');
  if (gate.source.length) throw new LoaderError(`публикация отложена — источники не готовы: ${gate.source.join('; ')}`, 'OZON_STORE_PNL_SOURCE_NOT_READY');

  const sheetId = pub.sheetId!;
  const sheets = deps.makeSheets(ctx, false);
  type Meta = { id: number; key: string; value: string };
  const readTab = async (): Promise<{ rowCount: number; columnCount: number; metas: Meta[]; contracts: Meta[]; values: CellValue[][] }> => {
    const tab = await sheets.readSheetMetadata(sheetId);
    if (!tab || tab.title !== plan.tab) {
      throw new LoaderError(`вкладка sheetId=${sheetId} ${tab ? `называется «${tab.title}»` : 'не найдена'}, ожидалась «${plan.tab}»`, 'OZON_STORE_PNL_TAB_MISSING');
    }
    if (!(tab.rowCount > 0 && tab.columnCount > 0)) {
      throw new LoaderError(`сетка вкладки «${plan.tab}» не прочитана (${tab.rowCount}×${tab.columnCount})`, 'OZON_STORE_PNL_TAB_MISSING');
    }
    const metas = tab.metadata.filter((m) => m.key === PUBLISH_META_KEY);
    const contracts = tab.metadata.filter((m) => m.key === CONTRACT_META_KEY);
    if (metas.length > 1 || contracts.length > 1) {
      throw new LoaderError(`у вкладки ${metas.length} отпечатков публикации и ${contracts.length} контракта`, 'OZON_STORE_PNL_TAB_EDITED');
    }
    const [values] = await sheets.readValues([`'${plan.tab}'!A1:${colA1(TAB_COLS)}${Math.max(tab.rowCount, 1)}`]);
    return { rowCount: tab.rowCount, columnCount: tab.columnCount, metas, contracts, values: values ?? [] };
  };
  // Заметки строки заголовка — системные; читать можно только внутри сетки (за её пределами Sheets API отказывает).
  const headerNotesOff = async (gridCols: number, gridRows: number): Promise<number> => {
    const row = HEADER_ROW + 1;
    // Строки заголовка ещё нет в сетке (новая маленькая вкладка) — заметок нет, нужна запись.
    if (gridRows < row) return 0;
    const inGrid = Math.min(TAB_NOTES.length, gridCols);
    const notes = inGrid > 0 ? await sheets.readNotes(plan.tab, TAB_NOTES.slice(0, inGrid).map((_, i) => ({ row, col: i + 1 }))) : new Map<string, string>();
    return TAB_NOTES.findIndex((n, i) => i >= inGrid || notes.get(`${row}|${i + 1}`) !== n);
  };

  const othersBefore = otherTabsFingerprint(await sheets.readSheetsList(), sheetId);
  const t = await readTab();
  let d = decidePublish({ current: t.values, publishedSha: t.metas[0]?.value, adoptSha: pub.adoptSha, plan });
  if (d.action === 'REFUSE') throw new LoaderError(d.message, d.code);
  const c = decideContract({ publishedContract: t.contracts[0]?.value, allowMigrationTo: pub.migrateContract,
    firstPublication: d.action === 'WRITE' && d.first });
  if (!c.ok) throw new LoaderError(c.message, 'OZON_STORE_PNL_CONTRACT_MIGRATION_REQUIRED');
  const notesOff = await headerNotesOff(t.columnCount, t.rowCount);
  // NOOP по значениям не должен оставить миграцию без отпечатка контракта или заметки не по контракту.
  if (d.action === 'NOOP' && (c.migrate || notesOff >= 0 || t.contracts[0]?.value !== contractSha())) {
    d = { action: 'WRITE', sha: d.sha, previousSha: d.sha, clearRows: 0, adopted: false, first: false };
  }
  if (d.action === 'NOOP') {
    if (d.setMeta) await sheets.structureWrite(metaOnly(sheetId, d.sha, t.metas[0]?.id));
    logger.info('ozon_store_pnl_tab_published', { action: 'NOOP', sheetId, sha: d.sha, fingerprint: plan.fingerprint, metaUpdated: d.setMeta });
  } else {
    // Манифест отката: прежнее содержимое вкладки и его отпечаток — до записи.
    logger.info('ozon_store_pnl_rollback_manifest', { sheetId, previousSha: d.previousSha, previousContract: t.contracts[0]?.value ?? null,
      previousRows: normalizeGrid(t.values).length, previousValues: normalizeGrid(t.values) });
    // Прежняя раскладка (УФ, объединения, группы) снимается в том же batchUpdate — запись идемпотентна.
    const st = await sheets.readSheetStructure(plan.tab, t.rowCount, t.columnCount);
    const current: TabStructure = { conditionalFormats: st.conditionalFormats.length, merges: st.merges.length > 0,
      columnGroups: st.columnGroups, rowGroups: st.rowGroups ?? [] };
    await sheets.structureWrite(publishRequests(plan, sheetId, t.rowCount, d, t.metas[0]?.id, t.contracts[0]?.id, t.columnCount, current));
    logger.info('ozon_store_pnl_tab_published', { action: 'WRITE', sheetId, sha: d.sha, previousSha: d.previousSha, adopted: d.adopted,
      first: d.first, fingerprint: plan.fingerprint, rows: plan.grid.length, clearedRows: d.clearRows, contract: contractSha(), migrated: c.migrate });
  }
  // Перечитывание: ровно план, ровно один отпечаток, контракт кода, заметки контракта, остальные вкладки не тронуты.
  const after = await readTab();
  const check = decidePublish({ current: after.values, publishedSha: after.metas[0]?.value, plan });
  if (check.action !== 'NOOP' || check.setMeta || after.contracts[0]?.value !== contractSha()) {
    throw new LoaderError('перечитанная вкладка, её отпечаток или контракт не равны опубликованному плану', 'OZON_STORE_PNL_PUBLISH_READBACK');
  }
  const bad = await headerNotesOff(after.columnCount, after.rowCount);
  if (bad >= 0) throw new LoaderError(`заметка заголовка ${colA1(bad + 1)}${HEADER_ROW + 1} не равна контракту`, 'OZON_STORE_PNL_PUBLISH_READBACK');
  const othersAfter = otherTabsFingerprint(await sheets.readSheetsList(), sheetId);
  if (othersAfter !== othersBefore) throw new LoaderError('отпечаток остальных вкладок книги изменился во время публикации', 'OZON_STORE_PNL_OTHER_TABS_CHANGED');
  logger.info('ozon_store_pnl_readback', { sheetId, sha: check.sha, otherTabs: othersAfter });
  return { rowsFetched: pnl.length, rowsLoaded: d.action === 'WRITE' ? plan.rows.length : 0 };
}
