/**
 * Загрузчик `cutover` — координатор переключения писателей WB (этап C, OWNER ACK 11.11.2026).
 * Тот же образ wb-loader, Job ingest-cutover-prod, `args = ["cutover"]`. Плоскость управления: без lease
 * и без строки LOADER_RUNS (registry.ts controlPlane) — конкурентность решает CAS в таблице владения.
 *
 * env:
 *   CUTOVER_ACTION  tick | status | fence | activate | rollback (по умолчанию tick — так зовёт Scheduler);
 *   CUTOVER_STREAM  поток; пусто — все потоки (только tick/status: команды требуют явный поток);
 *   CUTOVER_AUTO_STREAMS  потоки через запятую, для которых tick сам ставит fence (иначе — колонка
 *                   auto_cutover_enabled; по умолчанию ни один);
 *   CUTOVER_INCLUDE_STOCKS=1  вести и остатки (M1) — только после приёмки координатора; по умолчанию M1 не трогается;
 *   CUTOVER_LEGACY_DRAIN_MIN  дренаж Apps Script, мин (не меньше 7; для Workspace-аккаунта — 31);
 *   WRITER_OWNERSHIP_DATASET  датасет таблиц владения (по умолчанию wb_ops).
 *
 * Итог: переход в STOP / UNKNOWN_OUTCOME / ROLLBACK_REQUIRED, нарушение инварианта или отказ хранилища →
 * LoaderError → exit 1 (алерт по jsonPayload.code). Остальное — exit 0.
 */
import { LoaderError } from '../../errors.js';
import { BqClient } from '../../bq/client.js';
import type { LoaderContext, LoaderResult } from '../types.js';
import { DEFAULT_OWNERSHIP_DATASET, STREAMS, isStream, type Stream } from '../../ownership/streams.js';
import { ACTIONS, step, type Action, type StepResult } from './machine.js';
import { LEGACY_DRAIN_MIN } from './profiles.js';
import { BqCutoverStore, type CutoverStore } from './store.js';

export const CUTOVER_CONFIG = 'CUTOVER_CONFIG';
export const CUTOVER_ATTENTION = 'CUTOVER_ATTENTION';
export const CUTOVER_STORE_ERROR = 'CUTOVER_STORE_ERROR';

export interface CutoverRuntime {
  env: Record<string, string | undefined>;
  makeStore: (ctx: LoaderContext, opsDataset: string) => CutoverStore;
  now: () => number;
}

export const defaultCutoverRuntime: CutoverRuntime = {
  env: process.env,
  makeStore: (ctx, opsDataset) =>
    new BqCutoverStore(new BqClient(ctx.config.projectId, ctx.config.bqLocation), opsDataset, ctx.config.rawDataset),
  now: () => Date.now(),
};

export interface CutoverEnv {
  action: Action;
  streams: Stream[];
  autoStreams: Set<Stream>;
  legacyDrainMin: number;
  opsDataset: string;
  includeStocks: boolean;
}

export function parseCutoverEnv(env: Record<string, string | undefined>): CutoverEnv {
  const action = (env.CUTOVER_ACTION ?? '').trim() || 'tick';
  if (!(ACTIONS as readonly string[]).includes(action)) {
    throw new LoaderError(`CUTOVER_ACTION '${action}' — допустимы ${ACTIONS.join(', ')}`, CUTOVER_CONFIG);
  }
  const rawStream = (env.CUTOVER_STREAM ?? '').trim();
  if (rawStream && !isStream(rawStream)) throw new LoaderError(`CUTOVER_STREAM '${rawStream}' — допустимы ${STREAMS.join(', ')}`, CUTOVER_CONFIG);
  if (!rawStream && action !== 'tick' && action !== 'status') {
    throw new LoaderError(`CUTOVER_ACTION=${action} требует явный CUTOVER_STREAM`, CUTOVER_CONFIG);
  }
  const autoStreams = new Set<Stream>();
  for (const s of (env.CUTOVER_AUTO_STREAMS ?? '').split(',').map((x) => x.trim()).filter(Boolean)) {
    if (!isStream(s)) throw new LoaderError(`CUTOVER_AUTO_STREAMS: неизвестный поток '${s}'`, CUTOVER_CONFIG);
    autoStreams.add(s);
  }
  const drainRaw = (env.CUTOVER_LEGACY_DRAIN_MIN ?? '').trim();
  const drain = drainRaw ? Number(drainRaw) : LEGACY_DRAIN_MIN;
  if (!Number.isInteger(drain) || drain < LEGACY_DRAIN_MIN || drain > 120) {
    throw new LoaderError(`CUTOVER_LEGACY_DRAIN_MIN '${drainRaw}' — целое ${LEGACY_DRAIN_MIN}..120`, CUTOVER_CONFIG);
  }
  return {
    action: action as Action,
    streams: rawStream ? [rawStream as Stream] : [...STREAMS],
    autoStreams,
    legacyDrainMin: drain,
    opsDataset: (env.WRITER_OWNERSHIP_DATASET ?? '').trim() || DEFAULT_OWNERSHIP_DATASET,
    includeStocks: (env.CUTOVER_INCLUDE_STOCKS ?? '').trim() === '1',
  };
}

export function makeCutoverLoader(rt: CutoverRuntime = defaultCutoverRuntime) {
  return async function cutoverLoader(ctx: LoaderContext): Promise<LoaderResult> {
    const { logger, config } = ctx;
    const ce = parseCutoverEnv(rt.env);
    const store = rt.makeStore(ctx, ce.opsDataset);
    const results: StepResult[] = [];
    const failures: string[] = [];
    for (const stream of ce.streams) {
      let r: StepResult;
      try {
        r = await step(stream, ce.action, {
          store, nowMs: rt.now(), runId: ctx.runId, changedBy: `cutover:${ce.action}:${config.executionId || 'local'}`,
          autoFromEnv: ce.autoStreams.has(stream), legacyDrainMin: ce.legacyDrainMin, includeUncoordinated: ce.includeStocks,
        });
      } catch (e) {
        failures.push(`${stream}: ${e instanceof Error ? e.message : String(e)}`);
        logger.error('cutover_step_failed', { code: CUTOVER_STORE_ERROR, stream, action: ce.action, error: e instanceof Error ? e.message : String(e) });
        continue;
      }
      results.push(r);
      const rec = {
        stream, action: ce.action, outcome: r.outcome, from: r.from, to: r.to ?? null, owner: r.owner ?? null, epoch: r.epoch ?? null,
        reason: r.reason, evidence: r.evidence,
      };
      if (r.attention) logger.error('cutover_attention', { code: r.attention, ...rec });
      else if (r.outcome === 'TRANSITION') logger.info('cutover_transition', rec);
      else if (r.outcome === 'CAS_CONFLICT' || r.outcome === 'REFUSED') logger.warn('cutover_step', rec);
      else logger.info('cutover_step', rec);
    }
    const attention = results.filter((r) => r.attention && (r.outcome === 'TRANSITION' || r.outcome === 'REFUSED' || r.outcome === 'MISSING_ROW'));
    const refusedCommand = ce.action !== 'tick' && ce.action !== 'status' && results.some((r) => r.outcome === 'REFUSED');
    if (failures.length) throw new LoaderError(`cutover: отказ хранилища — ${failures.join('; ')}`, CUTOVER_STORE_ERROR);
    if (attention.length) {
      throw new LoaderError(`cutover: требуется решение — ${attention.map((r) => `${r.stream} ${r.from}→${r.to ?? '—'} (${r.attention}): ${r.reason}`).join('; ')}`, CUTOVER_ATTENTION);
    }
    if (refusedCommand) throw new LoaderError(`cutover ${ce.action}: команда отклонена — ${results.map((r) => r.reason).join('; ')}`, CUTOVER_CONFIG);
    return { rowsFetched: results.length, rowsLoaded: results.filter((r) => r.outcome === 'TRANSITION').length };
  };
}

export const cutoverLoader = makeCutoverLoader();
