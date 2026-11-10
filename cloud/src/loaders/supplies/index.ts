/**
 * M5: суточный загрузчик поставок WB → RAW_WB_SUPPLIES(+__CR) + RAW_WB_SUPPLIES_GOODS(+__CR).
 * Эталон поведения — runWbSuppliesDaily (apps-script/ingestion/Wbsuppliesrawloader.gs),
 * проект — docs/wb_supplies_raw_design.md. Журнал прогона — LOADER_RUNS (пишет cli.ts).
 *
 * Порядок (каждый шаг — только после успеха предыдущего):
 *   1. проверка контура (до токена, WB и BigQuery);
 *   2. токен из Secret Manager; истёкший по claim exp — отказ ДО WB;
 *   3. список → watermark из целевой шапки → выбор объектов (§7.3);
 *   4. по объекту: карточка и состав (пауза 900 мс), порциями по 40 объектов:
 *        LOAD goods порции → LOAD header порции (commit-marker).
 *      Шапка пишется ПОСЛЕДНЕЙ: пока её нет, V_WB_SUPPLIES_GOODS_CURRENT позиции прогона не видит.
 *   5. бюджет 20 минут: при упоре собранное коммитится, прогон — ERROR SUPPLIES_PARTIAL.
 */
import type { LoaderContext, LoaderResult } from '../types.js';
import { LoaderError } from '../../errors.js';
import { SecretsClient } from '../../secrets.js';
import type { Logger } from '../../logging.js';
import { assertSuppliesTargets, suppliesEnv, type SuppliesEnv } from './targets.js';
import { WbSuppliesApi, tokenExpiryMs, type SuppliesApi } from './wbApi.js';
import { SuppliesBq, suppliesJobId } from './bq.js';
import {
  dailyRunId, duplicateBarcodes, goodsRow, headerRow, selectTodo, toId,
  type GoodsRow, type HeaderRow, type KnownState, type TodoItem,
} from './normalize.js';
import {
  WB_SUPPLIES_COMMIT_CHUNK, WB_SUPPLIES_HTTP_BACKOFF_BASE_MS, WB_SUPPLIES_HTTP_BACKOFF_CAP_MS,
  WB_SUPPLIES_HTTP_MAX_RETRIES, WB_SUPPLIES_PAUSE_MS, WB_SUPPLIES_TIME_BUDGET_MS, WB_TOKEN_EXPIRY_WARN_DAYS,
} from './constants.js';

/** Минимум, который нужен ядру от BigQuery (фейк в тестах). */
export interface SuppliesStore {
  knownUpdated(headerTable: string): Promise<Map<string, KnownState>>;
  load(table: string, rows: object[], jobId: string): Promise<'LOADED' | 'ALREADY_LOADED'>;
}

export interface SuppliesDeps {
  env: NodeJS.ProcessEnv;
  readToken: (secret: string) => Promise<string>;
  makeApi: (host: string, token: string, logger: Logger) => SuppliesApi;
  makeStore: () => SuppliesStore;
  nowMs: () => number;
  sleep: (ms: number) => Promise<void>;
  pauseMs: number;
  budgetMs: number;
  chunk: number;
}

export function defaultSuppliesDeps(ctx: LoaderContext): SuppliesDeps {
  const { config } = ctx;
  return {
    env: process.env,
    readToken: (s) => new SecretsClient(config.projectId).access(s),
    makeApi: (host, token, logger) => new WbSuppliesApi(host, token, {
      timeoutMs: config.wbHttpTimeoutMs, maxRetries: WB_SUPPLIES_HTTP_MAX_RETRIES,
      backoffBaseMs: WB_SUPPLIES_HTTP_BACKOFF_BASE_MS, backoffCapMs: WB_SUPPLIES_HTTP_BACKOFF_CAP_MS,
    }, logger),
    makeStore: () => new SuppliesBq(config.projectId, config.bqLocation, config.rawDataset),
    nowMs: () => Date.now(),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    pauseMs: WB_SUPPLIES_PAUSE_MS,
    budgetMs: WB_SUPPLIES_TIME_BUDGET_MS,
    chunk: WB_SUPPLIES_COMMIT_CHUNK,
  };
}

export async function suppliesLoader(ctx: LoaderContext, deps: SuppliesDeps = defaultSuppliesDeps(ctx)): Promise<LoaderResult> {
  const { config, logger } = ctx;
  const e: SuppliesEnv = suppliesEnv(deps.env);
  // 1. Контур — до любой записи и до обращения к WB API.
  assertSuppliesTargets(config, e);

  const t0 = deps.nowMs();
  const runId = dailyRunId(t0);
  const snapshotTs = new Date(t0).toISOString();

  // 2. Токен. Значение не логируется; в журнал — только имя секрета и срок.
  const token = (await deps.readToken(e.secret)).trim();
  if (!token) throw new LoaderError(`секрет ${e.secret} пуст`, 'WB_SUPPLIES_AUTH');
  const exp = tokenExpiryMs(token);
  if (exp !== null) {
    const daysLeft = Math.floor((exp - t0) / 86_400_000);
    if (exp <= t0) throw new LoaderError(`токен ${e.secret} истёк ${new Date(exp).toISOString()} — нужна новая версия секрета`, 'WB_SUPPLIES_TOKEN_EXPIRED');
    if (daysLeft < WB_TOKEN_EXPIRY_WARN_DAYS) {
      logger.warn('wb_token_expiring', { code: 'WB_TOKEN_EXPIRING_SOON', secret: e.secret, daysLeft, expiresAt: new Date(exp).toISOString() });
    }
  }

  const api = deps.makeApi(e.host, token, logger);
  const store = deps.makeStore();

  // 3. Список и watermark (из целевой таблицы контура).
  const list = await api.list();
  const known = await store.knownUpdated(e.headerTable);
  const sel = selectTodo(list, known, t0);
  if (sel.skippedNoKey) logger.warn('supplies_no_key', { code: 'SUPPLIES_NO_KEY', skipped: sel.skippedNoKey });
  if (sel.duplicateKeys.length) logger.warn('supplies_duplicate_entity_key', { code: 'SUPPLIES_DUP_KEY', keys: sel.duplicateKeys });
  logger.info('supplies_plan', {
    runId, listed: list.length, known: known.size, todo: sel.todo.length,
    reasons: countBy(sel.todo.map((t) => t.why)), headerTable: e.headerTable, goodsTable: e.goodsTable,
  });

  // 4. Сбор и запись порциями.
  let committed = 0, goodsTotal = 0, noCard = 0, chunkNo = 0, stopped = false;
  let pendH: HeaderRow[] = [], pendG: GoodsRow[] = [];
  const commit = async () => {
    if (!pendH.length) return;
    chunkNo++;
    // goods — первыми, шапка — commit-marker.
    if (pendG.length) await store.load(e.goodsTable, pendG, suppliesJobId(config.environment, runId, e.goodsTable, chunkNo));
    await store.load(e.headerTable, pendH, suppliesJobId(config.environment, runId, e.headerTable, chunkNo));
    committed += pendH.length;
    goodsTotal += pendG.length;
    pendH = []; pendG = [];
  };

  let fetchFailure: unknown = null;
  try {
    for (const item of sel.todo) {
      if (deps.nowMs() - t0 > deps.budgetMs) { stopped = true; break; }
      let obj: { header: HeaderRow; goods: GoodsRow[] };
      try {
        obj = await fetchObject(api, item, runId, snapshotTs, deps);
      } catch (fe) {
        fetchFailure = fe; // объект не собран целиком — в запись не идёт
        break;
      }
      const dupBarcodes = duplicateBarcodes(obj.goods);
      if (dupBarcodes.length) logger.warn('supplies_invariant_i10', { code: 'SUPPLIES_I10_BARCODE_DUP', entityKey: item.ek, barcodes: dupBarcodes.length });
      if (!obj.header.card_read_ok && obj.header.supply_id !== null) noCard++;
      pendH.push(obj.header);
      pendG.push(...obj.goods);
      if (pendH.length >= deps.chunk) await commit();
    }
    // Уже собранные ЦЕЛИКОМ объекты коммитятся и при отказе WB на следующем: каждый из них —
    // законченный снимок, а несобранный объект остаётся на прежнем committed-прогоне.
    await commit();
  } finally {
    logger.info('supplies_written', { runId, objects: committed, goodsRows: goodsTotal, cardUnreadable: noCard, chunks: chunkNo, todo: sel.todo.length });
  }
  if (fetchFailure !== null) throw fetchFailure;
  if (stopped) {
    throw new LoaderError(`бюджет времени исчерпан: записано ${committed} из ${sel.todo.length}; остальные — в следующем прогоне`, 'SUPPLIES_PARTIAL');
  }
  return { rowsFetched: list.length, rowsLoaded: committed + goodsTotal };
}

async function fetchObject(api: SuppliesApi, item: TodoItem, runId: string, snapshotTs: string, deps: SuppliesDeps): Promise<{ header: HeaderRow; goods: GoodsRow[] }> {
  const supplyId = toId(item.o.supplyID);
  let card: Record<string, unknown> | null = null;
  let goods: Record<string, unknown>[] = [];
  // Заявка без supplyID: ни карточки, ни состава у WB нет — шапка с нулём позиций (§5.2).
  if (supplyId !== null) {
    card = await api.card(supplyId); await deps.sleep(deps.pauseMs);
    goods = await api.goods(supplyId); await deps.sleep(deps.pauseMs);
  }
  return {
    header: headerRow(item.o, card, item.ek, runId, snapshotTs),
    goods: goods.map((g) => goodsRow(g, item.o, item.ek, runId, snapshotTs)),
  };
}

function countBy(xs: string[]): Record<string, number> {
  const m: Record<string, number> = {};
  for (const x of xs) m[x] = (m[x] ?? 0) + 1;
  return m;
}
