/**
 * Машина состояний cutover потока (этап C). Один вызов step() — не больше ОДНОГО перехода.
 *
 * Основной путь (владелец в скобках):
 *   LEGACY_ACTIVE(AS) → SHADOW_VALIDATING(AS) → QUALIFIED(AS) → LEGACY_FENCED(NONE) → CLOUD_ACTIVE(CR)
 *   → MONITORING(CR) → COMPLETED(CR)
 * Откат: CLOUD_*(CR) → CLOUD_FENCED(NONE) → LEGACY_ACTIVE(AS).
 * Отказы: BLOCKED(AS), STOP(владелец прежний), ROLLBACK_REQUIRED(CR), UNKNOWN_OUTCOME(NONE).
 *
 * Инварианты (тесты cutover_machine.test.ts):
 *   • владелец меняется только через NONE: APPS_SCRIPT ↔ CLOUD_RUN напрямую не бывает — двух писателей нет;
 *   • каждая смена владельца увеличивает epoch; каждый переход — revision; CAS по (epoch, state, revision);
 *   • NONE → CLOUD_RUN только после доказанного дренажа Apps Script (≥ LEGACY_DRAIN_MIN, нет стартов после
 *     fence, нет «STARTED без исхода»); NONE → APPS_SCRIPT при откате — после дренажа Cloud Run
 *     (timeout Job'а + запас);
 *   • UNKNOWN_OUTCOME и STOP tick не трогает: слепого повтора нет, решает человек (activate/rollback);
 *   • QUALIFIED → LEGACY_FENCED tick делает только при auto_cutover_enabled и в свободном окне.
 */
import type { Owner, Stream } from '../../ownership/streams.js';
import {
  CLOUD_DRAIN_MARGIN_MIN, DRAIN_LOOKBACK_H, FENCE_GUARD_MIN, LEGACY_DRAIN_MIN, PROFILES, type StreamProfile,
} from './profiles.js';
import { evaluateQualification, type QualEvaluation } from './qualification.js';
import { fenceWindowCheck, mskDate, mskIsoDow } from './window.js';
import { parseStatsWatermark, type CloudRun, type CutoverStore, type OwnershipRow, type State, type WriteJob } from './store.js';

export const ACTIONS = ['tick', 'status', 'fence', 'activate', 'rollback', 'resolve'] as const;
export type Action = (typeof ACTIONS)[number];

/** Коды внимания: переход в состояние, которое требует человека, — exit 1 и алерт. */
export const CUTOVER_STOP = 'CUTOVER_STOP';
export const CUTOVER_UNKNOWN_OUTCOME = 'CUTOVER_UNKNOWN_OUTCOME';
export const CUTOVER_ROLLBACK_REQUIRED = 'CUTOVER_ROLLBACK_REQUIRED';
export const CUTOVER_INVARIANT = 'CUTOVER_INVARIANT';
/** Prod-прогон Cloud Run при владельце не CLOUD_RUN — писатель не под gate (не тот образ / выключен gate). */
export const CUTOVER_WRITER_NOT_GATED = 'CUTOVER_WRITER_NOT_GATED';

/** Владелец, который обязан стоять в состоянии (STOP — любой). */
export const OWNER_OF_STATE: Readonly<Record<State, Owner | null>> = Object.freeze({
  LEGACY_ACTIVE: 'APPS_SCRIPT',
  SHADOW_VALIDATING: 'APPS_SCRIPT',
  QUALIFIED: 'APPS_SCRIPT',
  BLOCKED: 'APPS_SCRIPT',
  LEGACY_FENCED: 'NONE',
  CLOUD_FENCED: 'NONE',
  UNKNOWN_OUTCOME: 'NONE',
  CLOUD_ACTIVE: 'CLOUD_RUN',
  MONITORING: 'CLOUD_RUN',
  COMPLETED: 'CLOUD_RUN',
  ROLLBACK_REQUIRED: 'CLOUD_RUN',
  STOP: null,
});

export interface StepDeps {
  store: CutoverStore;
  nowMs: number;
  /** Идентификатор исполнения координатора (журнал переходов). */
  runId: string;
  changedBy: string;
  /** Флаг потока из env (CUTOVER_AUTO_STREAMS); колонка auto_cutover_enabled учитывается дополнительно. */
  autoFromEnv: boolean;
  /** Дренаж Apps Script, мин (не меньше LEGACY_DRAIN_MIN). */
  legacyDrainMin?: number;
  profile?: StreamProfile;
  /** Вести и потоки с coordinated = false (остатки M1) — только после их отдельного ACK (CUTOVER_INCLUDE_STOCKS=1). */
  includeUncoordinated?: boolean;
  /** resolve: причина оператора (обязательна, пишется в журнал) и состояние, в которое вернуть поток. */
  operatorReason?: string;
  resolveTo?: State;
  /**
   * Тяжёлые проверки в состояниях Apps Script (квалификация, «Cloud Run без права») — не на каждом tick.
   * Загрузчик ставит true раз в час; по умолчанию (команды, тесты) — true.
   */
  hourlyChecks?: boolean;
  /**
   * Кто есть кто в INFORMATION_SCHEMA.JOBS: учётная запись владельца, от которой работает Apps Script
   * (CUTOVER_APPS_SCRIPT_USER), и prod-SA Cloud Run по потокам (CUTOVER_STREAM_PROD_SAS). Не задано —
   * fence не ставится (доказательство невозможно), а чужие записи считаются нарушением.
   */
  identities?: Identities;
}

export interface Identities {
  appsScriptUser: string;
  streamSa: Partial<Record<Stream, string>>;
}

export type Outcome = 'TRANSITION' | 'OK_ALREADY' | 'NOOP' | 'REFUSED' | 'CAS_CONFLICT' | 'MISSING_ROW';

export interface StepResult {
  stream: Stream;
  action: Action;
  outcome: Outcome;
  from: State | null;
  to?: State;
  owner?: Owner;
  epoch?: number;
  reason: string;
  attention?: string;
  evidence?: unknown;
}

type Transition = {
  kind: 'transition';
  to: State;
  owner: Owner;
  reason: string;
  evidence?: unknown;
  attention?: string;
  legacyWatermark?: string | null;
  cloudWatermark?: string | null;
  setFencedAt?: boolean;
  setActiveSince?: boolean;
  setChecksSince?: boolean;
};
type Plan =
  | Transition
  | { kind: 'noop' | 'refuse' | 'ok_already'; reason: string; evidence?: unknown; attention?: string };

const MIN = 60_000;
const H = 3_600_000;
const noop = (reason: string, evidence?: unknown, attention?: string): Plan => ({ kind: 'noop', reason, evidence, attention });
const refuse = (reason: string): Plan => ({ kind: 'refuse', reason });
const okAlready = (reason: string): Plan => ({ kind: 'ok_already', reason });

// ── доказательства ───────────────────────────────────────────────────────────────────────────

async function qualify(store: CutoverStore, p: StreamProfile, nowMs: number): Promise<QualEvaluation> {
  const since = mskDate(nowMs - 21 * 24 * H);
  return evaluateQualification(await store.qualification(p.stream, since), p.qualification, mskDate(nowMs));
}

type Drain =
  | { kind: 'WAIT'; reason: string }
  | { kind: 'VIOLATION'; reason: string; evidence: unknown }
  | { kind: 'UNKNOWN'; reason: string; evidence: unknown }
  | { kind: 'DRAINED'; evidence: unknown };

/**
 * Сколько может пройти между прохождением gate и первой записью журнала прогона: прогон, прошедший gate за
 * мгновение до fence, пишет строку журнала уже после него. Это не нарушение, а прогон «в полёте».
 *   Apps Script: gate → ingestRunStart_/manifest — секунды; Cloud Run: gate → зонд → acquire с одним повтором 30 с.
 */
export const GATE_TO_JOURNAL_GRACE_MS = { APPS_SCRIPT: 60_000, CLOUD_RUN: 180_000 } as const;

/** Верхняя граница жизни прогона писателя (мин): после неё STARTED — мёртвый прогон, а не «в полёте». */
function writerLifeMin(p: StreamProfile, w: 'APPS_SCRIPT' | 'CLOUD_RUN', legacyDrainMin: number): number {
  return w === 'APPS_SCRIPT' ? legacyDrainMin : p.cloudJobTimeoutMin + CLOUD_DRAIN_MARGIN_MIN;
}

/**
 * Дренаж писателя `drained` после fence в fencedAtMs. Для любого направления:
 *   — ни один писатель не стартовал после fence + запас gate→журнал (оба отсечены: owner = NONE) → иначе STOP;
 *   — незавершённые ЗАПИСЫВАЮЩИЕ задания BigQuery в таблицы потока: созданные позже, чем мог жить прогон,
 *     начатый до fence, → STOP; прочие → ждём DONE;
 *   — «без исхода» (STARTED/RUNNING) у дренируемого писателя считаются только прогоны, которые МОГЛИ быть живы
 *     в момент fence (старт не раньше fence − время жизни). Более старые STARTED — мёртвые прогоны (убиты
 *     лимитом исполнения до fence): они в доказательствах, но cutover не блокируют. Живые-на-fence без исхода
 *     → UNKNOWN_OUTCOME.
 */
/** Журналы, свою строку которых прогон Apps Script «в полёте» закрывает после fence (WriterGate.gs). */
const JOURNAL_TABLES = new Set(['INGEST_RUNS', 'FINANCE_LOADER_RUNS', 'WB_STOCKS_SNAPSHOTS', 'REF_SYNC_RUNS']);

/** Чья это запись: Apps Script (учётная запись владельца), prod-SA потока или посторонний. */
function writerOf(j: WriteJob, p: StreamProfile, ids: Identities | undefined): 'APPS_SCRIPT' | 'CLOUD_RUN' | 'OTHER' {
  const u = j.user.toLowerCase();
  if (ids?.appsScriptUser && u === ids.appsScriptUser.toLowerCase()) return 'APPS_SCRIPT';
  const sa = ids?.streamSa[p.stream];
  if (sa && u === sa.toLowerCase()) return 'CLOUD_RUN';
  return 'OTHER';
}

/**
 * Запись после fence допустима только как хвост прогона дренируемого писателя, начатого до fence:
 *   Apps Script — до fence + запас gate→журнал; позже — только UPDATE/MERGE своей строки журнала в пределах
 *   времени жизни (остальное его перечитка перед записью не пропускает);
 *   Cloud Run — в пределах времени жизни прогона (перечитки перед каждой записью у загрузчиков потоков нет).
 * Запись второго (уже отсечённого) писателя или постороннего — нарушение.
 */
function allowedAfterFence(j: WriteJob, p: StreamProfile, drained: 'APPS_SCRIPT' | 'CLOUD_RUN', fenced: number,
  legacyDrainMin: number, ids: Identities | undefined): boolean {
  const w = writerOf(j, p, ids);
  if (w !== drained) return false;
  const life = fenced + writerLifeMin(p, drained, legacyDrainMin) * MIN;
  if (drained === 'CLOUD_RUN') return j.createdMs <= life;
  if (j.createdMs <= fenced + GATE_TO_JOURNAL_GRACE_MS.APPS_SCRIPT) return true;
  return j.createdMs <= life && (j.statementType === 'UPDATE' || j.statementType === 'MERGE') && JOURNAL_TABLES.has(j.table);
}

async function proveDrain(store: CutoverStore, p: StreamProfile, row: OwnershipRow, drained: 'APPS_SCRIPT' | 'CLOUD_RUN',
  nowMs: number, legacyDrainMin: number, ids?: Identities): Promise<Drain> {
  const fenced = row.fencedAtMs;
  if (fenced === null) return { kind: 'VIOLATION', reason: 'нет fenced_at у строки без владельца', evidence: {} };
  const waitMin = writerLifeMin(p, drained, legacyDrainMin);
  if (nowMs - fenced < waitMin * MIN) {
    return { kind: 'WAIT', reason: `дренаж ${drained}: прошло ${Math.floor((nowMs - fenced) / MIN)} из ${waitMin} мин` };
  }
  const from = fenced - DRAIN_LOOKBACK_H * H;
  const legacy = await store.legacyRuns(p.stream, from, true);
  const cloud = await store.cloudRuns(p.stream, from);
  const asLimit = fenced + GATE_TO_JOURNAL_GRACE_MS.APPS_SCRIPT;
  const crLimit = fenced + GATE_TO_JOURNAL_GRACE_MS.CLOUD_RUN;
  const afterFence = [
    ...legacy.filter((r) => r.startMs > asLimit).map((r) => ({ writer: 'APPS_SCRIPT', journal: r.journal, id: r.id, startMs: r.startMs })),
    ...cloud.filter((r) => r.startMs > crLimit).map((r) => ({ writer: 'CLOUD_RUN', journal: 'LOADER_RUNS', id: r.id, startMs: r.startMs })),
  ];
  if (afterFence.length > 0) {
    return { kind: 'VIOLATION', reason: `после fence стартовало ${afterFence.length} прогонов — gate не соблюдён`, evidence: { afterFence: afterFence.slice(0, 20) } };
  }
  // Задания BigQuery в таблицы потока — и ВЫПОЛНЕННЫЕ (запись длится секунды, замер 7 суток: ≤ 21 с, p99 ≤ 6 с).
  const jobs = await store.writeJobs(p.stream, from);
  const bad = jobs.filter((j) => j.createdMs > fenced && !allowedAfterFence(j, p, drained, fenced, legacyDrainMin, ids));
  if (bad.length > 0) {
    return { kind: 'VIOLATION', reason: `${bad.length} записывающих заданий BigQuery после fence не объясняются прогоном «в полёте» — gate не соблюдён`, evidence: { jobsAfterFence: bad.slice(0, 20) } };
  }
  const pending = jobs.filter((j) => j.state !== 'DONE');
  if (pending.length > 0) {
    return { kind: 'WAIT', reason: `дренаж ${drained}: ${pending.length} незавершённых заданий BigQuery в таблицы потока (${pending.slice(0, 3).map((j) => `${j.jobId}→${j.table || j.statementType}`).join(', ')})` };
  }
  const lifeFrom = fenced - writerLifeMin(p, drained, legacyDrainMin) * MIN;
  const isOpen = (status: string) => status === 'STARTED' || status === 'RUNNING';
  const openAll = drained === 'APPS_SCRIPT'
    ? legacy.filter((r) => isOpen(r.status)).map((r) => ({ journal: r.journal, id: r.id, startMs: r.startMs }))
    : cloud.filter((r) => isOpen(r.status)).map((r) => ({ journal: 'LOADER_RUNS', id: r.id, startMs: r.startMs }));
  const open = openAll.filter((r) => r.startMs >= lifeFrom);
  const dead = openAll.filter((r) => r.startMs < lifeFrom);
  if (open.length > 0) {
    return { kind: 'UNKNOWN', reason: `${drained}: ${open.length} прогонов, живых в момент fence, без исхода — повтора вслепую нет`, evidence: { open: open.slice(0, 20), dead: dead.slice(0, 20) } };
  }
  return {
    kind: 'DRAINED',
    evidence: {
      drained, fencedAt: new Date(fenced).toISOString(), waitedMin: Math.floor((nowMs - fenced) / MIN),
      legacyRunsChecked: legacy.length, cloudRunsChecked: cloud.length, pendingBigQueryJobs: 0,
      deadStartedBeforeFence: dead.slice(0, 20),
    },
  };
}

/** NONE → CLOUD_RUN: дренаж Apps Script доказан, legacy watermark зафиксирован. */
async function planActivate(store: CutoverStore, p: StreamProfile, row: OwnershipRow, d: StepDeps, why: string): Promise<Plan> {
  const drain = await proveDrain(store, p, row, 'APPS_SCRIPT', d.nowMs, d.legacyDrainMin ?? LEGACY_DRAIN_MIN, d.identities);
  if (drain.kind === 'WAIT') return noop(drain.reason);
  if (drain.kind === 'VIOLATION') {
    if (row.state === 'STOP') return noop(`STOP: ${drain.reason}`, drain.evidence);
    return { kind: 'transition', to: 'STOP', owner: 'NONE', reason: drain.reason, evidence: drain.evidence, attention: CUTOVER_STOP };
  }
  if (drain.kind === 'UNKNOWN') {
    if (row.state === 'UNKNOWN_OUTCOME') return noop(`UNKNOWN_OUTCOME: ${drain.reason}`, drain.evidence);
    return { kind: 'transition', to: 'UNKNOWN_OUTCOME', owner: 'NONE', reason: drain.reason, evidence: drain.evidence, attention: CUTOVER_UNKNOWN_OUTCOME };
  }
  const wm = await store.currentWatermark(p.stream);
  if (wm === null && p.continuity.kind !== 'state') {
    return { kind: 'transition', to: 'STOP', owner: 'NONE', reason: 'legacy watermark не определён — Cloud Run не с чего начинать', evidence: drain.evidence, attention: CUTOVER_STOP };
  }
  return {
    kind: 'transition', to: 'CLOUD_ACTIVE', owner: 'CLOUD_RUN', reason: `${why}: дренаж Apps Script доказан, legacy watermark ${wm ?? '—'}`,
    evidence: { ...(drain.evidence as object), legacyWatermark: wm }, legacyWatermark: wm, setActiveSince: true,
  };
}

/** NONE → APPS_SCRIPT при откате: дренаж Cloud Run доказан. */
async function planReturnToLegacy(store: CutoverStore, p: StreamProfile, row: OwnershipRow, d: StepDeps, why: string): Promise<Plan> {
  const drain = await proveDrain(store, p, row, 'CLOUD_RUN', d.nowMs, d.legacyDrainMin ?? LEGACY_DRAIN_MIN, d.identities);
  if (drain.kind === 'WAIT') return noop(drain.reason);
  if (drain.kind === 'VIOLATION') {
    if (row.state === 'STOP') return noop(`STOP: ${drain.reason}`, drain.evidence);
    return { kind: 'transition', to: 'STOP', owner: 'NONE', reason: drain.reason, evidence: drain.evidence, attention: CUTOVER_STOP };
  }
  if (drain.kind === 'UNKNOWN') {
    if (row.state === 'UNKNOWN_OUTCOME') return noop(`UNKNOWN_OUTCOME: ${drain.reason}`, drain.evidence);
    return { kind: 'transition', to: 'UNKNOWN_OUTCOME', owner: 'NONE', reason: drain.reason, evidence: drain.evidence, attention: CUTOVER_UNKNOWN_OUTCOME };
  }
  const wm = await store.currentWatermark(p.stream);
  return {
    kind: 'transition', to: 'LEGACY_ACTIVE', owner: 'APPS_SCRIPT', reason: `${why}: дренаж Cloud Run доказан, владелец возвращён Apps Script`,
    evidence: { ...(drain.evidence as object), cloudWatermark: wm }, cloudWatermark: wm,
  };
}

const dateDiffDays = (a: string, b: string): number =>
  Math.round((Date.parse(`${b.slice(0, 10)}T00:00:00Z`) - Date.parse(`${a.slice(0, 10)}T00:00:00Z`)) / (24 * H));

/**
 * Покрытие после активации — ПРОВЕРКА на данных, а не утверждение: Cloud Run продолжил ровно с legacy
 * watermark и не пропустил событий. Зовётся на CLOUD_ACTIVE → MONITORING и на каждом tick MONITORING.
 *   table_watermark (заказы/продажи):
 *     • отметка таблицы не откатилась ниже legacy;
 *     • Cloud Run не писал строк инкремента старше legacy (не перечитывал «до» отметки);
 *     • состояние, которое прочитал ПЕРВЫЙ прогон Cloud Run (MAX строк инкремента со стартом раньше него),
 *       равно legacy watermark — между дренажом и первым прогоном никто не писал и ничего не исчезло;
 *     • первая ночная сверка Cloud Run (окно reconcileDays) накрыла legacy watermark продаж.
 *   period_gap: ни одного разрыва больше maxGapDays в цепочке legacy → периоды COMPLETE Cloud Run.
 */
async function coverageProblem(store: CutoverStore, p: StreamProfile, row: OwnershipRow, runs: readonly CloudRun[]): Promise<string | null> {
  const c = p.continuity;
  if (c.kind === 'state') return null;
  const legacy = row.legacyWatermark;
  if (!legacy) return 'нет legacy watermark';
  if (c.kind === 'table_watermark') {
    const l = parseStatsWatermark(legacy);
    const cur = await store.currentWatermark(p.stream);
    const n = cur ? parseStatsWatermark(cur) : null;
    if (!l || !n) return `watermark не разобран: legacy=${legacy} current=${cur}`;
    if (n.orders < l.orders || n.sales < l.sales) return `watermark откатился: legacy=${legacy} current=${cur}`;
    const older = await store.continuityViolations(p.stream, legacy, row.activeSinceMs ?? 0);
    if (older > 0) return `Cloud Run записал ${older} строк старше legacy watermark — первый прогон начал не с него`;
    const inc = runs.filter((r) => r.loader === 'orders' || r.loader === 'sales');
    const first = inc[0];
    if (first) {
      // load_id несёт секунды (МСК), started_at — миллисекунды: граница — начало секунды старта.
      const seenRaw = await store.statsWatermarkBefore(Math.floor(first.startMs / 1000) * 1000);
      const seen = seenRaw ? parseStatsWatermark(seenRaw) : null;
      if (!seen || seen.orders !== l.orders || seen.sales !== l.sales) {
        return `первый прогон Cloud Run (${first.loader} ${new Date(first.startMs).toISOString()}) прочитал отметку ${seenRaw}, а legacy ${legacy} — интервал от legacy не покрыт`;
      }
    }
    const recon = runs.find((r) => r.loader === c.reconcileLoader && r.status === 'COMPLETE');
    if (recon) {
      const windowFrom = mskDate(recon.startMs - c.reconcileDays * 24 * H);
      if (windowFrom > l.sales.slice(0, 10)) {
        return `первая сверка ${c.reconcileLoader} (окно с ${windowFrom}) не накрыла legacy watermark продаж ${l.sales}`;
      }
    }
    return null;
  }
  const primary = Object.keys(p.monitoring.cycles)[0] ?? '';
  const dates = [...new Set(runs.filter((r) => r.loader === primary && r.status === 'COMPLETE').map((r) => (r.logicalPeriod || mskDate(r.startMs)).slice(0, 10)))].sort();
  let prev = legacy.slice(0, 10);
  for (const d of dates) {
    const gap = dateDiffDays(prev, d);
    if (gap > c.maxGapDays) return `разрыв ${gap} сут между ${prev} и периодом Cloud Run ${d} > ${c.maxGapDays} (legacy ${legacy})`;
    if (d > prev) prev = d;
  }
  return null;
}

interface Health {
  runs: CloudRun[];
  stop?: { reason: string; evidence: unknown };
  rollback?: { reason: string; evidence: unknown };
  completes: Record<string, number>;
  mondayCompletes: number;
  firstPeriod: string | null;
  anyComplete: boolean;
}

/** Здоровье после активации: Apps Script не пишет, у Cloud Run нет ERROR/PARTIAL, нет дублей, писатель жив. */
async function cloudHealth(store: CutoverStore, p: StreamProfile, row: OwnershipRow, nowMs: number, d: StepDeps): Promise<Health> {
  const since = row.activeSinceMs ?? row.changedAtMs;
  const fenced = row.fencedAtMs ?? since;
  const legacyAfter = (await store.legacyRuns(p.stream, fenced, false)).filter((r) => r.startMs > fenced && !r.shared);
  const runs = await store.cloudRuns(p.stream, since);
  const completes: Record<string, number> = {};
  for (const l of p.cloudLoaders) completes[l] = 0;
  let mondayCompletes = 0;
  const primary = Object.keys(p.monitoring.cycles)[0] ?? p.cloudLoaders[0] ?? '';
  let firstPeriod: string | null = null;
  for (const r of runs) {
    if (r.status !== 'COMPLETE') continue;
    completes[r.loader] = (completes[r.loader] ?? 0) + 1;
    if (p.monitoring.mondayCycleLoader === r.loader && mskIsoDow(r.startMs) === 1) mondayCompletes++;
    if (r.loader === primary && firstPeriod === null) firstPeriod = r.logicalPeriod;
  }
  const h: Health = { runs, completes, mondayCompletes, firstPeriod, anyComplete: (completes[primary] ?? 0) > 0 };
  if (legacyAfter.length > 0) {
    h.stop = { reason: `Apps Script пишет при владельце CLOUD_RUN: ${legacyAfter.length} прогонов — два писателя`, evidence: { legacyAfter: legacyAfter.slice(0, 20) } };
    return h;
  }
  // Любая запись в таблицы потока не от prod-SA потока — второй писатель (Apps Script пишет от учётной записи
  // человека — различимо). Тяжёлый запрос JOBS — раз в час и в моменты решения (переходы).
  if (d.hourlyChecks !== false) {
    const from = Math.max(row.activeSinceMs ?? row.changedAtMs, nowMs - GATE_EVIDENCE_H * H);
    const foreign = (await store.writeJobs(p.stream, from)).filter((j) => writerOf(j, p, d.identities) !== 'CLOUD_RUN');
    if (foreign.length > 0) {
      h.stop = { reason: `запись в таблицы потока не от prod-SA Cloud Run: ${foreign.length} заданий — второй писатель`, evidence: { foreignWrites: foreign.slice(0, 20) } };
      return h;
    }
  }
  // ERROR, закрытый более поздним COMPLETE того же загрузчика: того же периода — всегда; любого более позднего
  // периода — только у потоков, где следующий прогон дочитывает пропущенное (errorsSupersededByLaterPeriod).
  const errors = runs.filter((r) => r.status === 'ERROR' && !runs.some((c) => c.loader === r.loader && c.status === 'COMPLETE'
    && c.startMs > r.startMs && (c.logicalPeriod === r.logicalPeriod || p.errorsSupersededByLaterPeriod)));
  // STARTED без исхода дольше времени жизни Job'а — прогон умер без исхода: это отказ, а не «ещё идёт».
  const lifeMs = (p.cloudJobTimeoutMin + CLOUD_DRAIN_MARGIN_MIN) * MIN;
  const dead = runs.filter((r) => r.status === 'STARTED' && r.startMs < nowMs - lifeMs);
  if (dead.length > 0) {
    h.rollback = { reason: `прогоны Cloud Run без исхода дольше timeout Job'а: ${dead.length}`, evidence: { deadStarted: dead.slice(0, 20) } };
    return h;
  }
  if (errors.length > 0) {
    h.rollback = { reason: `ERROR/PARTIAL Cloud Run после активации: ${errors.length}`, evidence: { errors: errors.slice(0, 20) } };
    return h;
  }
  const dup = await store.duplicates(p.stream, fenced);
  if (dup > 0) {
    h.rollback = { reason: `дубли после fence: ${dup}`, evidence: { duplicates: dup } };
    return h;
  }
  if (!h.anyComplete && nowMs - since > p.monitoring.firstCycleDeadlineH * H) {
    h.rollback = { reason: `нет ни одного COMPLETE ${primary} за ${p.monitoring.firstCycleDeadlineH} ч после активации — писатель не работает`, evidence: { runs: runs.length } };
  }
  return h;
}

// ── план по действию ──────────────────────────────────────────────────────────────────────────

/** Prod-прогоны Cloud Run при владельце не CLOUD_RUN (после окна проверок + запаса gate) — писатель не под gate. */
async function ungatedCloudRuns(store: CutoverStore, p: StreamProfile, row: OwnershipRow): Promise<CloudRun[]> {
  const since = (row.checksSinceMs ?? row.changedAtMs) + GATE_TO_JOURNAL_GRACE_MS.CLOUD_RUN;
  return (await store.cloudRuns(p.stream, since)).filter((r) => r.startMs > since);
}

/** Окно доказательства «писатели под gate» перед fence: ежедневные точки входа — 26 ч, недельные — 8 суток. */
export const GATE_EVIDENCE_H = 26;
export const GATE_EVIDENCE_WEEKLY_H = 8 * 24;

/**
 * Доказательство по КАЖДОЙ плановой точке входа (profiles.schedules): запрос gate этой точки входа выполнен
 * в своём окне и от своего пользователя — Apps Script от учётной записи владельца, Cloud Run от prod-SA потока.
 * Ручной wbWriterGateStatus, перечитка перед записью и запросы других пользователей не засчитываются.
 */
export async function gateProof(store: CutoverStore, p: StreamProfile, nowMs: number, ids: Identities | undefined): Promise<{ missing: string[]; seen: unknown }> {
  const sa = ids?.streamSa[p.stream];
  if (!ids?.appsScriptUser || !sa) {
    return { missing: [`не заданы учётные записи писателей (CUTOVER_APPS_SCRIPT_USER / CUTOVER_STREAM_PROD_SAS для ${p.stream})`], seen: [] };
  }
  const ev = await store.gateEvidence(p.stream, nowMs - GATE_EVIDENCE_WEEKLY_H * H);
  const missing: string[] = [];
  const done = new Set<string>();
  for (const e of p.schedules) {
    const entry = e.writer === 'APPS_SCRIPT' ? e.label : (e.loader ?? '');
    const key = `${e.writer}:${entry}`;
    if (done.has(key)) continue;
    done.add(key);
    const user = (e.writer === 'APPS_SCRIPT' ? ids.appsScriptUser : sa).toLowerCase();
    const windowMs = (e.dow ? GATE_EVIDENCE_WEEKLY_H : GATE_EVIDENCE_H) * H;
    const ok = ev.some((x) => x.writer === e.writer && x.entry === entry && x.user.toLowerCase() === user && x.lastMs >= nowMs - windowMs);
    if (!ok) missing.push(`${e.writer === 'APPS_SCRIPT' ? 'Apps Script' : 'Cloud Run'} ${entry} за ${e.dow ? '8 сут' : `${GATE_EVIDENCE_H} ч`}`);
  }
  return { missing, seen: ev };
}

async function planTick(row: OwnershipRow, d: StepDeps, p: StreamProfile, explicitFence: boolean): Promise<Plan> {
  const { store, nowMs } = d;
  const asOwned = row.state === 'LEGACY_ACTIVE' || row.state === 'SHADOW_VALIDATING' || row.state === 'QUALIFIED' || row.state === 'BLOCKED';
  if (asOwned) {
    if (d.hourlyChecks === false && row.state !== 'QUALIFIED') return noop('проверки состояний Apps Script — раз в час (экономия BigQuery)');
    const ungated = await ungatedCloudRuns(store, p, row);
    if (ungated.length > 0) {
      const ev = { cloudRunsWithoutRight: ungated.slice(0, 20) };
      const why = `Cloud Run пишет при владельце ${row.owner}: ${ungated.length} prod-прогонов — писатель не под gate`;
      return row.state === 'BLOCKED'
        ? noop(`BLOCKED: ${why} (снять — команда resolve после разбора)`, ev)
        : { kind: 'transition', to: 'BLOCKED', owner: 'APPS_SCRIPT', reason: why, evidence: ev, attention: CUTOVER_WRITER_NOT_GATED };
    }
  }
  switch (row.state) {
    case 'LEGACY_ACTIVE': {
      const q = await qualify(store, p, nowMs);
      if (q.newestDate === null) return noop('вердиктов квалификации нет — shadow ещё не сверяется');
      return { kind: 'transition', to: 'SHADOW_VALIDATING', owner: 'APPS_SCRIPT', reason: `появились вердикты квалификации (последний ${q.newestDate})`, evidence: q };
    }
    case 'SHADOW_VALIDATING':
    case 'BLOCKED': {
      const q = await qualify(store, p, nowMs);
      if (q.status === 'PASS') return { kind: 'transition', to: 'QUALIFIED', owner: 'APPS_SCRIPT', reason: q.reason, evidence: q };
      if (q.status === 'FAIL') {
        return row.state === 'BLOCKED' ? noop(`BLOCKED: ${q.reason}`, q) : { kind: 'transition', to: 'BLOCKED', owner: 'APPS_SCRIPT', reason: q.reason, evidence: q };
      }
      return row.state === 'BLOCKED'
        ? { kind: 'transition', to: 'SHADOW_VALIDATING', owner: 'APPS_SCRIPT', reason: `отказ снят: ${q.reason}`, evidence: q }
        : noop(q.reason, q);
    }
    case 'QUALIFIED': {
      const q = await qualify(store, p, nowMs);
      if (q.status === 'FAIL') return { kind: 'transition', to: 'BLOCKED', owner: 'APPS_SCRIPT', reason: q.reason, evidence: q };
      if (q.status === 'PENDING') return { kind: 'transition', to: 'SHADOW_VALIDATING', owner: 'APPS_SCRIPT', reason: `квалификация утрачена: ${q.reason}`, evidence: q };
      if (!explicitFence && !(d.autoFromEnv || row.autoCutoverEnabled)) return noop('auto_cutover_enabled = false — fence только командой fence');
      const w = fenceWindowCheck(nowMs, p.schedules, (d.legacyDrainMin ?? LEGACY_DRAIN_MIN) + FENCE_GUARD_MIN);
      if (!w.allowed) return noop(`окно занято: ${w.conflicts.join(', ')}`, w);
      // Оба писателя под gate: за 24 ч и Apps Script (учётная запись владельца), и Cloud Run (SA) читали строку
      // этого потока. Иначе не обновлённый файл Apps Script или образ без gate = тихий второй писатель.
      const g = await gateProof(store, p, nowMs, d.identities);
      if (g.missing.length) return noop(`нет доказательства gate: ${g.missing.join('; ')} — fence не ставим`, g);
      return {
        kind: 'transition', to: 'LEGACY_FENCED', owner: 'NONE', setFencedAt: true,
        reason: `${explicitFence ? 'команда fence' : 'auto'}: квалификация PASS, окно свободно, оба писателя под gate — Apps Script отсечён`,
        evidence: { qualification: q, window: w, gateEvidence: g },
      };
    }
    case 'LEGACY_FENCED':
      return planActivate(store, p, row, d, 'tick');
    case 'CLOUD_FENCED':
      return planReturnToLegacy(store, p, row, d, 'tick');
    case 'CLOUD_ACTIVE':
    case 'MONITORING': {
      const h = await cloudHealth(store, p, row, nowMs, d);
      // Apps Script пишет при владельце CLOUD_RUN — gate Apps Script не соблюдён. Отсекаем и Cloud Run (NONE):
      // писатель остаётся максимум один (тот, что нарушает gate), решение — за человеком.
      if (h.stop) return { kind: 'transition', to: 'STOP', owner: 'NONE', setFencedAt: true, reason: h.stop.reason, evidence: h.stop.evidence, attention: CUTOVER_STOP };
      if (h.rollback) return { kind: 'transition', to: 'ROLLBACK_REQUIRED', owner: 'CLOUD_RUN', reason: h.rollback.reason, evidence: h.rollback.evidence, attention: CUTOVER_ROLLBACK_REQUIRED };
      if (row.state === 'CLOUD_ACTIVE' && !h.anyComplete) return noop('ждём первый COMPLETE Cloud Run', h);
      // Покрытие. period_gap — только LOADER_RUNS (дёшево) — на каждом tick. table_watermark читает RAW
      // заказов/продаж — только в момент решения (активация первого цикла, затем перед COMPLETED), чтобы
      // плановый tick (раз в 15 мин) не сканировал RAW (экономия BigQuery, #414).
      const cheap = p.continuity.kind !== 'table_watermark';
      const missing = Object.entries(p.monitoring.cycles).filter(([l, n]) => (h.completes[l] ?? 0) < n).map(([l, n]) => `${l} ${h.completes[l] ?? 0}/${n}`);
      if (p.monitoring.mondayCycleLoader && h.mondayCompletes < 1) missing.push(`недельный цикл ${p.monitoring.mondayCycleLoader} (понедельник) 0/1`);
      if (cheap || row.state === 'CLOUD_ACTIVE' || missing.length === 0) {
        const problem = await coverageProblem(store, p, row, h.runs);
        if (problem) return { kind: 'transition', to: 'ROLLBACK_REQUIRED', owner: 'CLOUD_RUN', reason: `покрытие: ${problem}`, evidence: h, attention: CUTOVER_ROLLBACK_REQUIRED };
      }
      if (row.state === 'CLOUD_ACTIVE') {
        return { kind: 'transition', to: 'MONITORING', owner: 'CLOUD_RUN', reason: `первый цикл Cloud Run начат с legacy watermark (${row.legacyWatermark ?? '—'})`, evidence: h };
      }
      if (missing.length) return noop(`мониторинг: ${missing.join(', ')}`, h);
      if (p.monitoring.downstream) {
        const alive = await store.downstreamAlive(p.monitoring.downstream, row.activeSinceMs ?? row.changedAtMs);
        if (!alive) return noop(`мониторинг: downstream ${p.monitoring.downstream} ещё не обновился после активации`, h);
      }
      const wm = await store.currentWatermark(p.stream);
      return { kind: 'transition', to: 'COMPLETED', owner: 'CLOUD_RUN', reason: 'циклы Cloud Run штатные, покрытие от legacy watermark подтверждено, дублей нет, downstream жив', evidence: h, cloudWatermark: wm };
    }
    case 'UNKNOWN_OUTCOME':
      return noop('UNKNOWN_OUTCOME: tick не повторяет — после разбора команда resolve (CUTOVER_REASON, CUTOVER_RESOLVE_TO), activate или rollback');
    case 'STOP':
      return noop('STOP: tick не продолжает — после разбора команда resolve (CUTOVER_REASON, CUTOVER_RESOLVE_TO), activate или rollback');
    case 'ROLLBACK_REQUIRED':
      return noop('ROLLBACK_REQUIRED: ждём решения — rollback или activate (повторный мониторинг)');
    case 'COMPLETED':
      return noop('COMPLETED');
  }
}

async function plan(action: Action, row: OwnershipRow, d: StepDeps, p: StreamProfile): Promise<Plan> {
  const s = row.state;
  switch (action) {
    case 'tick':
    case 'status':
      return planTick(row, d, p, false);
    case 'fence':
      if (s === 'QUALIFIED') return planTick(row, d, p, true);
      if (['LEGACY_FENCED', 'CLOUD_ACTIVE', 'MONITORING', 'COMPLETED'].includes(s)) return okAlready(`уже ${s}`);
      return refuse(`fence допустим только из QUALIFIED, сейчас ${s}`);
    case 'activate':
      if (s === 'LEGACY_FENCED') return planActivate(d.store, p, row, d, 'команда activate');
      if ((s === 'UNKNOWN_OUTCOME' || s === 'STOP') && row.owner === 'NONE') {
        const r = await planActivate(d.store, p, row, d, `команда activate из ${s}`);
        return r.kind === 'transition' && r.to !== 'CLOUD_ACTIVE' ? noop(`${s}: ${r.reason}`, r.evidence) : r;
      }
      if (s === 'ROLLBACK_REQUIRED') {
        return { kind: 'transition', to: 'MONITORING', owner: 'CLOUD_RUN', setActiveSince: true, reason: 'команда activate: мониторинг перезапущен с текущего момента' };
      }
      if (['CLOUD_ACTIVE', 'MONITORING', 'COMPLETED'].includes(s)) return okAlready(`уже ${s}`);
      return refuse(`activate недопустим из ${s}`);
    case 'resolve': {
      const why = (d.operatorReason ?? '').trim();
      if (why.length < 10) return refuse('resolve требует причину оператора (CUTOVER_REASON, не короче 10 символов)');
      if ((s === 'STOP' || s === 'UNKNOWN_OUTCOME') && row.owner === 'NONE') {
        if (d.resolveTo !== 'LEGACY_FENCED' && d.resolveTo !== 'CLOUD_FENCED') {
          return refuse('resolve из STOP/UNKNOWN_OUTCOME требует CUTOVER_RESOLVE_TO = LEGACY_FENCED (дренаж к Cloud Run) или CLOUD_FENCED (к Apps Script)');
        }
        // Новый отсчёт дренажа и проверок: нарушения до этого момента оператор разобрал; дальше доказательство заново.
        return { kind: 'transition', to: d.resolveTo, owner: 'NONE', setFencedAt: true, setChecksSince: true, reason: `resolve: ${why}` };
      }
      if (s === 'BLOCKED') {
        return { kind: 'transition', to: 'SHADOW_VALIDATING', owner: 'APPS_SCRIPT', setChecksSince: true, reason: `resolve: ${why}` };
      }
      return refuse(`resolve допустим из STOP/UNKNOWN_OUTCOME (owner NONE) и BLOCKED, сейчас ${s}`);
    }
    case 'rollback':
      if (row.owner === 'CLOUD_RUN') {
        return { kind: 'transition', to: 'CLOUD_FENCED', owner: 'NONE', setFencedAt: true, reason: `команда rollback из ${s}: Cloud Run отсечён, дренаж` };
      }
      if (s === 'CLOUD_FENCED') return planReturnToLegacy(d.store, p, row, d, 'команда rollback');
      if (s === 'LEGACY_FENCED') {
        // Cloud Run владельцем с момента fence не был: Apps Script возвращается сразу.
        return { kind: 'transition', to: 'LEGACY_ACTIVE', owner: 'APPS_SCRIPT', reason: 'команда rollback до активации: владелец возвращён Apps Script' };
      }
      if ((s === 'UNKNOWN_OUTCOME' || s === 'STOP') && row.owner === 'NONE') {
        const r = await planReturnToLegacy(d.store, p, row, d, `команда rollback из ${s}`);
        return r.kind === 'transition' && r.to !== 'LEGACY_ACTIVE' ? noop(`${s}: ${r.reason}`, r.evidence) : r;
      }
      if (row.owner === 'APPS_SCRIPT') return okAlready(`владелец уже APPS_SCRIPT (${s})`);
      return refuse(`rollback недопустим из ${s}`);
  }
}

// ── применение ────────────────────────────────────────────────────────────────────────────────

export async function step(stream: Stream, action: Action, d: StepDeps): Promise<StepResult> {
  const p = d.profile ?? PROFILES[stream];
  if (!p.coordinated && !d.includeUncoordinated) {
    const reason = `поток ${stream} координатором не ведётся (M1: утверждённая процедура MIG1B); включение — CUTOVER_INCLUDE_STOCKS=1 после ACK`;
    return { stream, action, outcome: action === 'tick' || action === 'status' ? 'NOOP' : 'REFUSED', from: null, reason };
  }
  const row = await d.store.read(stream);
  if (!row) return { stream, action, outcome: 'MISSING_ROW', from: null, reason: `нет строки потока ${stream}`, attention: CUTOVER_INVARIANT };
  const expectedOwner = OWNER_OF_STATE[row.state];
  if (expectedOwner !== null && expectedOwner !== row.owner) {
    return { stream, action, outcome: 'REFUSED', from: row.state, reason: `нарушен инвариант: состояние ${row.state} при владельце ${row.owner}`, attention: CUTOVER_INVARIANT };
  }
  const planned = await plan(action, row, d, p);
  const base = { stream, action, from: row.state, owner: row.owner, epoch: row.epoch };
  if (planned.kind !== 'transition') {
    if (planned.kind === 'noop') return { ...base, outcome: 'NOOP', reason: planned.reason, evidence: planned.evidence, attention: planned.attention };
    if (planned.kind === 'refuse') return { ...base, outcome: 'REFUSED', reason: planned.reason };
    return { ...base, outcome: 'OK_ALREADY', reason: planned.reason };
  }
  const pl: Transition = planned;
  if (action === 'status') {
    return { ...base, outcome: 'NOOP', to: pl.to, reason: `status: tick выполнил бы ${row.state} → ${pl.to}: ${pl.reason}`, evidence: pl.evidence };
  }
  // Защита инварианта «двух писателей не бывает»: владелец меняется только через NONE.
  if ((row.owner === 'APPS_SCRIPT' && pl.owner === 'CLOUD_RUN') || (row.owner === 'CLOUD_RUN' && pl.owner === 'APPS_SCRIPT')) {
    return { ...base, outcome: 'REFUSED', reason: `запрещённый переход владельца ${row.owner} → ${pl.owner}`, attention: CUTOVER_INVARIANT };
  }
  const ownerChanged = pl.owner !== row.owner;
  const epoch = ownerChanged ? row.epoch + 1 : row.epoch;
  const revision = row.revision + 1;
  let ok: boolean;
  let lostAck = false;
  try {
    ok = await d.store.cas(
    { stream, epoch: row.epoch, state: row.state, revision: row.revision },
    {
      owner: pl.owner, state: pl.to, epoch, revision,
      legacyWatermark: pl.legacyWatermark !== undefined ? pl.legacyWatermark : row.legacyWatermark,
      cloudWatermark: pl.cloudWatermark !== undefined ? pl.cloudWatermark : row.cloudWatermark,
      fencedAtMs: pl.setFencedAt ? d.nowMs : row.fencedAtMs,
      activeSinceMs: pl.setActiveSince ? d.nowMs : row.activeSinceMs,
      checksSinceMs: ownerChanged || pl.setChecksSince ? d.nowMs : row.checksSinceMs,
      changedBy: d.changedBy, reason: pl.reason,
    },
    {
      transitionId: `${stream}:r${revision}`, stream, fromState: row.state, toState: pl.to, epoch, runId: d.runId,
      sourceWatermark: pl.legacyWatermark ?? pl.cloudWatermark ?? row.legacyWatermark ?? null,
      reason: pl.reason, evidence: { action, ...(pl.evidence !== undefined ? { detail: pl.evidence } : {}) },
    },
    );
  } catch (e) {
    // Ответ CAS потерян (сеть/таймаут), а транзакция могла примениться. Решаем по строке: revision ровно
    // наша и состояние целевое → переход применён ЭТИМ вызовом; иначе — отказ хранилища (повтор — tick).
    const after = await d.store.read(stream);
    if (after && after.revision === revision && after.state === pl.to && after.owner === pl.owner && after.epoch === epoch) {
      ok = false;
      lostAck = true;
    } else {
      throw e;
    }
  }
  if (lostAck) {
    return { stream, action, outcome: 'OK_ALREADY', from: row.state, to: pl.to, owner: pl.owner, epoch, reason: `ответ CAS потерян, переход ${row.state} → ${pl.to} уже применён (revision ${revision}) — повторно не выполняется`, attention: pl.attention };
  }
  if (ok) {
    return { ...base, outcome: 'TRANSITION', to: pl.to, owner: pl.owner, epoch, reason: pl.reason, attention: pl.attention, evidence: pl.evidence };
  }
  // CAS не совпал: другой tick/команда успели раньше. Целевое состояние уже достигнуто — OK_ALREADY.
  const now = await d.store.read(stream);
  if (now && now.state === pl.to && now.owner === pl.owner) {
    return { stream, action, outcome: 'OK_ALREADY', from: row.state, to: now.state, owner: now.owner, epoch: now.epoch, reason: `переход ${row.state} → ${pl.to} уже выполнен другим исполнением` };
  }
  return { ...base, outcome: 'CAS_CONFLICT', reason: `CAS не совпал: строка изменилась (${now?.state ?? 'нет строки'}), шаг не применён — следующий tick переоценит` };
}
