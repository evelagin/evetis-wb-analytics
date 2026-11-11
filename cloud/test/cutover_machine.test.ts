import { describe, it, expect, vi, afterEach } from 'vitest';
import { step, OWNER_OF_STATE, ACTIONS, CUTOVER_STOP, CUTOVER_UNKNOWN_OUTCOME, CUTOVER_ROLLBACK_REQUIRED, CUTOVER_INVARIANT, type Action, type StepDeps } from '../src/loaders/cutover/machine.js';
import { evaluateQualification } from '../src/loaders/cutover/qualification.js';
import { fenceWindowCheck, mskDate } from '../src/loaders/cutover/window.js';
import { PROFILES, LEGACY_DRAIN_MIN, CLOUD_DRAIN_MARGIN_MIN, type StreamProfile } from '../src/loaders/cutover/profiles.js';
import { STATES, BqCutoverStore, type State } from '../src/loaders/cutover/store.js';
import { makeCutoverLoader, parseCutoverEnv, CUTOVER_ATTENTION, CUTOVER_CONFIG } from '../src/loaders/cutover/index.js';
import { loadConfig } from '../src/config.js';
import { Logger } from '../src/logging.js';
import type { Owner, Stream } from '../src/ownership/streams.js';
import { MemCutoverStore, seedRow, T0, MIN, H, DAY } from './cutover_fixture.js';

/**
 * Этап C — машина состояний координатора cutover на хранилище в памяти (та же CAS-семантика, что у BigQuery).
 * Время задаётся явно: T0 — понедельник 16.11.2026 10:00 МСК (окно остатков свободно: 06:30 ± запуски).
 */

afterEach(() => vi.restoreAllMocks());

const STOCKS_GROUPS = ['STOCKS_T6', 'STOCKS_T5', 'STOCKS_DIGEST'];
const deps = (store: MemCutoverStore, nowMs: number, o: Partial<StepDeps> = {}): StepDeps =>
  ({ store, nowMs, runId: 'run-1', changedBy: 'test', autoFromEnv: false, includeUncoordinated: true, ...o });
const tick = (stream: Stream, store: MemCutoverStore, nowMs: number, o: Partial<StepDeps> = {}) => step(stream, 'tick', deps(store, nowMs, o));
const act = (action: Action, stream: Stream, store: MemCutoverStore, nowMs: number, o: Partial<StepDeps> = {}) => step(stream, action, deps(store, nowMs, o));

/** Порядок владельцев по журналу переходов: смена владельца — только через NONE. */
function assertNoDirectHandover(store: MemCutoverStore, stream: Stream, start: Owner = 'APPS_SCRIPT'): Owner[] {
  const owners: Owner[] = [start];
  for (const e of store.log.filter((l) => l.stream === stream)) {
    const o = OWNER_OF_STATE[e.toState] ?? store.rows.get(stream)!.owner;
    owners.push(o);
  }
  for (let i = 1; i < owners.length; i++) {
    const a = owners[i - 1], b = owners[i];
    expect(a === 'APPS_SCRIPT' && b === 'CLOUD_RUN').toBe(false);
    expect(a === 'CLOUD_RUN' && b === 'APPS_SCRIPT').toBe(false);
  }
  return owners;
}

function qualifiedStocks(o: Parameters<typeof seedRow>[1] = {}) {
  const store = new MemCutoverStore(seedRow('stocks', { state: 'QUALIFIED', revision: 2, ...o }));
  store.passDays('stocks', STOCKS_GROUPS, '2026-11-15', 5);
  store.watermark.stocks = '2026-11-16';
  return store;
}

function fencedStocks(fencedAt = T0) {
  return new MemCutoverStore(seedRow('stocks', { owner: 'NONE', state: 'LEGACY_FENCED', epoch: 2, revision: 3, fencedAtMs: fencedAt }));
}

describe('основной путь остатков: LEGACY_ACTIVE → … → COMPLETED, по одному шагу за tick', () => {
  it('все переходы, epoch растёт только при смене владельца, журнал на каждый переход', async () => {
    const store = new MemCutoverStore(seedRow('stocks'));
    store.passDays('stocks', STOCKS_GROUPS, '2026-11-15', 5);
    store.watermark.stocks = '2026-11-16';
    store.legacy.push({ stream: 'stocks', journal: 'WB_STOCKS_SNAPSHOTS', id: 'STOCK_SNAP_20261116_063012_ab12', startMs: T0 - 3.5 * H, status: 'COMPLETE', shared: false });

    const r1 = await tick('stocks', store, T0);
    expect(r1).toMatchObject({ outcome: 'TRANSITION', from: 'LEGACY_ACTIVE', to: 'SHADOW_VALIDATING', owner: 'APPS_SCRIPT', epoch: 1 });
    const r2 = await tick('stocks', store, T0);
    expect(r2).toMatchObject({ outcome: 'TRANSITION', to: 'QUALIFIED', epoch: 1 });
    const r3 = await tick('stocks', store, T0);
    expect(r3).toMatchObject({ outcome: 'NOOP', from: 'QUALIFIED' });
    expect(r3.reason).toMatch(/auto_cutover_enabled = false/);

    const r4 = await tick('stocks', store, T0, { autoFromEnv: true });
    expect(r4).toMatchObject({ outcome: 'TRANSITION', to: 'LEGACY_FENCED', owner: 'NONE', epoch: 2 });
    expect(store.rows.get('stocks')!.fencedAtMs).toBe(T0);

    const r5 = await tick('stocks', store, T0 + 5 * MIN);
    expect(r5).toMatchObject({ outcome: 'NOOP', from: 'LEGACY_FENCED' });
    expect(r5.reason).toMatch(/дренаж APPS_SCRIPT: прошло 5 из 7 мин/);

    const r6 = await tick('stocks', store, T0 + LEGACY_DRAIN_MIN * MIN);
    expect(r6).toMatchObject({ outcome: 'TRANSITION', to: 'CLOUD_ACTIVE', owner: 'CLOUD_RUN', epoch: 3 });
    expect(store.rows.get('stocks')).toMatchObject({ legacyWatermark: '2026-11-16', activeSinceMs: T0 + 7 * MIN });

    expect(await tick('stocks', store, T0 + H)).toMatchObject({ outcome: 'NOOP', reason: 'ждём первый COMPLETE Cloud Run' });

    const day = (n: number) => T0 + n * DAY - 3.5 * H; // 06:30 МСК следующих суток
    store.cloud.push({ stream: 'stocks', id: 'c1', loader: 'stocks', startMs: day(1), status: 'COMPLETE', logicalPeriod: '2026-11-17', errorCode: '' });
    expect(await tick('stocks', store, day(1) + H)).toMatchObject({ outcome: 'TRANSITION', to: 'MONITORING', epoch: 3 });

    store.cloud.push({ stream: 'stocks', id: 'c2', loader: 'stocks', startMs: day(2), status: 'COMPLETE', logicalPeriod: '2026-11-18', errorCode: '' });
    expect((await tick('stocks', store, day(2) + H)).reason).toMatch(/stocks 2\/3/);
    store.cloud.push({ stream: 'stocks', id: 'c3', loader: 'stocks', startMs: day(3), status: 'COMPLETE', logicalPeriod: '2026-11-19', errorCode: '' });
    expect((await tick('stocks', store, day(3) + H)).reason).toMatch(/downstream mart/);
    store.downstream.mart = true;
    store.watermark.stocks = '2026-11-19';
    expect(await tick('stocks', store, day(3) + 2 * H)).toMatchObject({ outcome: 'TRANSITION', to: 'COMPLETED', owner: 'CLOUD_RUN', epoch: 3 });
    expect(await tick('stocks', store, day(4))).toMatchObject({ outcome: 'NOOP', reason: 'COMPLETED' });

    expect(store.log.map((l) => `${l.fromState}>${l.toState}@${l.epoch}`)).toEqual([
      'LEGACY_ACTIVE>SHADOW_VALIDATING@1', 'SHADOW_VALIDATING>QUALIFIED@1', 'QUALIFIED>LEGACY_FENCED@2',
      'LEGACY_FENCED>CLOUD_ACTIVE@3', 'CLOUD_ACTIVE>MONITORING@3', 'MONITORING>COMPLETED@3',
    ]);
    expect(store.log.map((l) => l.transitionId)).toEqual(['stocks:r1', 'stocks:r2', 'stocks:r3', 'stocks:r4', 'stocks:r5', 'stocks:r6']);
    expect(store.log[3]).toMatchObject({ runId: 'run-1', sourceWatermark: '2026-11-16' });
    expect(store.log[3]!.evidence).toMatchObject({ action: 'tick', detail: { drained: 'APPS_SCRIPT', legacyWatermark: '2026-11-16' } });
    expect(store.rows.get('stocks')!.cloudWatermark).toBe('2026-11-19');
    expect(assertNoDirectHandover(store, 'stocks')).toEqual(['APPS_SCRIPT', 'APPS_SCRIPT', 'APPS_SCRIPT', 'NONE', 'CLOUD_RUN', 'CLOUD_RUN', 'CLOUD_RUN']);
  });

  it('колонка auto_cutover_enabled = true разрешает fence без env', async () => {
    const store = qualifiedStocks({ autoCutoverEnabled: true });
    expect(await tick('stocks', store, T0)).toMatchObject({ outcome: 'TRANSITION', to: 'LEGACY_FENCED' });
  });
});

describe('квалификация', () => {
  it('нет вердиктов — LEGACY_ACTIVE стоит; FAIL → BLOCKED; новый чистый день → снова SHADOW_VALIDATING', async () => {
    const store = new MemCutoverStore(seedRow('stocks'));
    expect(await tick('stocks', store, T0)).toMatchObject({ outcome: 'NOOP' });
    store.passDays('stocks', STOCKS_GROUPS, '2026-11-14', 4);
    store.qual.push({ stream: 'stocks', businessDate: '2026-11-15', checkGroup: 'STOCKS_T6', verdict: 'FAIL', imageDigest: 'sha256:aa', atMs: T0 - H });
    store.qual.push({ stream: 'stocks', businessDate: '2026-11-15', checkGroup: 'STOCKS_T5', verdict: 'PASS', imageDigest: 'sha256:aa', atMs: T0 - H });
    expect(await tick('stocks', store, T0)).toMatchObject({ to: 'SHADOW_VALIDATING' });
    expect(await tick('stocks', store, T0)).toMatchObject({ to: 'BLOCKED', owner: 'APPS_SCRIPT', epoch: 1 });
    expect(await tick('stocks', store, T0)).toMatchObject({ outcome: 'NOOP' });
    store.passDays('stocks', ['STOCKS_T6', 'STOCKS_T5'], '2026-11-16', 1);
    expect(await tick('stocks', store, T0 + DAY)).toMatchObject({ to: 'SHADOW_VALIDATING' });
    // Дыра FAIL 15.11 обрывает счёт: подряд только 16.11 → ждём, не QUALIFIED.
    expect((await tick('stocks', store, T0 + DAY)).reason).toMatch(/1 из 5/);
  });

  it('QUALIFIED теряет квалификацию: новый FAIL → BLOCKED, устаревание → SHADOW_VALIDATING', async () => {
    const a = qualifiedStocks();
    a.qual.push({ stream: 'stocks', businessDate: '2026-11-16', checkGroup: 'STOCKS_T5', verdict: 'FAIL', imageDigest: 'sha256:aa', atMs: T0 });
    expect(await tick('stocks', a, T0, { autoFromEnv: true })).toMatchObject({ to: 'BLOCKED' });
    const b = qualifiedStocks();
    expect(await tick('stocks', b, T0 + 5 * DAY, { autoFromEnv: true })).toMatchObject({ to: 'SHADOW_VALIDATING' });
  });

  it('остатки: разные образы в засчитанных сутках → FAIL; нужна квалификация digest того же образа', () => {
    const req = PROFILES.stocks.qualification;
    const rows = (digest: (i: number) => string, groups = STOCKS_GROUPS) => Array.from({ length: 5 }, (_, i) => {
      const d = new Date(Date.parse('2026-11-15') - i * DAY).toISOString().slice(0, 10);
      return groups.map((g) => ({ businessDate: d, checkGroup: g, verdict: 'PASS', imageDigest: digest(i), atMs: 1 }));
    }).flat();
    expect(evaluateQualification(rows(() => 'sha256:aa'), req, '2026-11-16').status).toBe('PASS');
    expect(evaluateQualification(rows((i) => (i === 3 ? 'sha256:bb' : 'sha256:aa')), req, '2026-11-16').status).toBe('FAIL');
    expect(evaluateQualification(rows(() => 'sha256:aa', ['STOCKS_T6', 'STOCKS_T5']), req, '2026-11-16')).toMatchObject({ status: 'PENDING' });
  });

  it('финансы: обязателен WEEKLY понедельника; FAIL денег = необъяснённое расхождение → отказ; DRIFT_EXPLAINED допустим', () => {
    const req = PROFILES.finance.qualification;
    const base = (end: string, n: number, money = 'PASS') => Array.from({ length: n }, (_, i) => {
      const d = new Date(Date.parse(end) - i * DAY).toISOString().slice(0, 10);
      return [
        { businessDate: d, checkGroup: 'FINANCE_DAILY', verdict: 'PASS', imageDigest: '', atMs: 1 },
        { businessDate: d, checkGroup: 'FINANCE_MONEY', verdict: i === 1 ? money : 'PASS', imageDigest: '', atMs: 1 },
      ];
    }).flat();
    // 11.11 (ср) … 15.11 (вс): понедельника нет.
    const noMonday = base('2026-11-15', 5);
    expect(evaluateQualification(noMonday, req, '2026-11-16')).toMatchObject({ status: 'PENDING' });
    expect(evaluateQualification(noMonday, req, '2026-11-16').reason).toMatch(/FINANCE_WEEKLY/);
    const weeklySunday = [...noMonday, { businessDate: '2026-11-15', checkGroup: 'FINANCE_WEEKLY', verdict: 'PASS', imageDigest: '', atMs: 1 }];
    expect(evaluateQualification(weeklySunday, req, '2026-11-16').status).toBe('PENDING');
    const withMonday = [...base('2026-11-16', 5), { businessDate: '2026-11-16', checkGroup: 'FINANCE_WEEKLY', verdict: 'PASS', imageDigest: '', atMs: 1 }];
    expect(evaluateQualification(withMonday, req, '2026-11-16').status).toBe('PASS');
    const explained = [...base('2026-11-16', 5, 'DRIFT_EXPLAINED'), { businessDate: '2026-11-16', checkGroup: 'FINANCE_WEEKLY', verdict: 'PASS', imageDigest: '', atMs: 1 }];
    expect(evaluateQualification(explained, req, '2026-11-16').status).toBe('PASS');
    const unexplained = [...base('2026-11-16', 5, 'FAIL'), { businessDate: '2026-11-16', checkGroup: 'FINANCE_WEEKLY', verdict: 'PASS', imageDigest: '', atMs: 1 }];
    expect(evaluateQualification(unexplained, req, '2026-11-16').status).not.toBe('PASS');
    const weeklyFail = [...base('2026-11-16', 5), { businessDate: '2026-11-16', checkGroup: 'FINANCE_WEEKLY', verdict: 'FAIL', imageDigest: '', atMs: 1 }];
    expect(evaluateQualification(weeklyFail, req, '2026-11-16').status).toBe('FAIL');
  });

  it('реклама: недельный аудит обязателен; статистика: ночная сверка — daily-группа', () => {
    expect(PROFILES.ads.qualification.groups.find((g) => g.group === 'ADS_COSTS_AUDIT')).toMatchObject({ cadence: 'weekly' });
    expect(PROFILES.statistics.qualification.groups.find((g) => g.group === 'STATS_SALES_RECONCILE')).toMatchObject({ cadence: 'daily' });
    for (const s of ['statistics', 'ads'] as const) expect(PROFILES[s].qualification.minComparableDays).toBe(5);
  });

  it('последний вердикт по (дата, группа) заменяет прежний; свежий день без части вердиктов не обрывает счёт', () => {
    const req = PROFILES.supplies.qualification;
    const rows = Array.from({ length: 5 }, (_, i) => ({ businessDate: new Date(Date.parse('2026-11-15') - i * DAY).toISOString().slice(0, 10), checkGroup: 'SUPPLIES', verdict: 'PASS', imageDigest: '', atMs: 2 }));
    rows.push({ businessDate: '2026-11-15', checkGroup: 'SUPPLIES', verdict: 'FAIL', imageDigest: '', atMs: 1 }); // старее — не в счёт
    expect(evaluateQualification(rows, req, '2026-11-16').status).toBe('PASS');
    rows.push({ businessDate: '2026-11-15', checkGroup: 'SUPPLIES', verdict: 'FAIL', imageDigest: '', atMs: 3 });
    expect(evaluateQualification(rows, req, '2026-11-16').status).toBe('FAIL');
    const stats = PROFILES.statistics.qualification;
    const s = Array.from({ length: 5 }, (_, i) => ['STATS_ORDERS', 'STATS_SALES', 'STATS_SALES_RECONCILE'].map((g) =>
      ({ businessDate: new Date(Date.parse('2026-11-14') - i * DAY).toISOString().slice(0, 10), checkGroup: g, verdict: 'PASS', imageDigest: '', atMs: 1 }))).flat();
    s.push({ businessDate: '2026-11-15', checkGroup: 'STATS_ORDERS', verdict: 'PASS', imageDigest: '', atMs: 1 });
    expect(evaluateQualification(s, stats, '2026-11-16').status).toBe('PASS');
  });
});

describe('окно fence', () => {
  it('занятое окно: tick и явная команда fence ждут; свободное — fence без auto', async () => {
    const busy = Date.parse('2026-11-16T03:28:00Z'); // 06:28 МСК — снимок остатков
    const a = qualifiedStocks();
    const r = await tick('stocks', a, busy, { autoFromEnv: true });
    expect(r.outcome).toBe('NOOP');
    expect(r.reason).toMatch(/окно занято: .*runWbStocksSnapshot@06:30/);
    expect(await act('fence', 'stocks', a, busy)).toMatchObject({ outcome: 'NOOP' });
    expect(await act('fence', 'stocks', a, T0)).toMatchObject({ outcome: 'TRANSITION', to: 'LEGACY_FENCED', owner: 'NONE' });
    expect(a.log[0]!.reason).toMatch(/^команда fence/);
  });

  it('окно учитывает запуски обоих писателей, дренаж и запас', () => {
    const at = (hhmm: string) => Date.parse(`2026-11-16T${hhmm}:00+03:00`);
    const busyAhead = LEGACY_DRAIN_MIN + 5;
    expect(fenceWindowCheck(at('06:05'), PROFILES.stocks.schedules, busyAhead).allowed).toBe(false); // AS 06:30 −15
    expect(fenceWindowCheck(at('06:55'), PROFILES.stocks.schedules, busyAhead).allowed).toBe(false); // CR 06:30 занят до 07:01
    expect(fenceWindowCheck(at('07:10'), PROFILES.stocks.schedules, busyAhead).allowed).toBe(true);
    expect(fenceWindowCheck(at('07:30'), PROFILES.stocks.schedules, busyAhead).allowed).toBe(true);
    // Финансы 07:30/12:30/18:30 у обоих писателей.
    expect(fenceWindowCheck(at('12:20'), PROFILES.finance.schedules, busyAhead).conflicts.join()).toMatch(/runWbFinanceDaily|wb-finance-prod/);
    expect(fenceWindowCheck(at('15:00'), PROFILES.finance.schedules, busyAhead).allowed).toBe(true);
    // Аудит рекламы — только воскресенье.
    expect(fenceWindowCheck(at('04:25'), PROFILES.ads.schedules, busyAhead).allowed).toBe(true); // пн
    expect(fenceWindowCheck(Date.parse('2026-11-15T04:25:00+03:00'), PROFILES.ads.schedules, busyAhead).allowed).toBe(false); // вс
    // Продажи — каждый час в :22 ±15: свободны только хвосты часов.
    expect(fenceWindowCheck(at('10:30'), PROFILES.statistics.schedules, busyAhead).allowed).toBe(false);
    expect(fenceWindowCheck(at('10:44'), PROFILES.statistics.schedules, busyAhead).allowed).toBe(true);
    // Поставки: минута Apps Script неизвестна — занят весь час.
    expect(fenceWindowCheck(at('05:50'), PROFILES.supplies.schedules, busyAhead).allowed).toBe(false);
  });
});

describe('дренаж Apps Script', () => {
  it('старт Apps Script после fence → STOP (gate не соблюдён), владелец остаётся NONE', async () => {
    const store = fencedStocks();
    store.legacy.push({ stream: 'stocks', journal: 'WB_STOCKS_SNAPSHOTS', id: 'STOCK_SNAP_x', startMs: T0 + 2 * MIN, status: 'COMPLETE', shared: false });
    const r = await tick('stocks', store, T0 + 8 * MIN);
    expect(r).toMatchObject({ outcome: 'TRANSITION', to: 'STOP', owner: 'NONE', attention: CUTOVER_STOP, epoch: 2 });
    expect(await tick('stocks', store, T0 + 20 * MIN)).toMatchObject({ outcome: 'NOOP' });
    expect(store.log).toHaveLength(1);
  });

  it('старт Cloud Run после fence при owner = NONE — тоже STOP', async () => {
    const store = fencedStocks();
    store.cloud.push({ stream: 'stocks', id: 'c', loader: 'stocks', startMs: T0 + MIN, status: 'COMPLETE', logicalPeriod: '2026-11-16', errorCode: '' });
    expect(await tick('stocks', store, T0 + 8 * MIN)).toMatchObject({ to: 'STOP', attention: CUTOVER_STOP });
  });

  it('прогон Apps Script, начатый до fence, без исхода → UNKNOWN_OUTCOME; tick не повторяет; activate после разбора', async () => {
    const store = fencedStocks();
    const open = { stream: 'stocks' as Stream, journal: 'WB_STOCKS_SNAPSHOTS', id: 'STOCK_SNAP_y', startMs: T0 - 3 * MIN, status: 'STARTED', shared: false };
    store.legacy.push(open);
    store.watermark.stocks = '2026-11-16';
    expect(await tick('stocks', store, T0 + 8 * MIN)).toMatchObject({ to: 'UNKNOWN_OUTCOME', owner: 'NONE', attention: CUTOVER_UNKNOWN_OUTCOME });
    for (let i = 0; i < 3; i++) {
      const r = await tick('stocks', store, T0 + (20 + i) * MIN);
      expect(r).toMatchObject({ outcome: 'NOOP', from: 'UNKNOWN_OUTCOME' });
    }
    expect(store.casCalls).toBe(1); // ни одной попытки перехода из UNKNOWN_OUTCOME по tick
    // activate без разбора — по-прежнему без исхода: остаётся UNKNOWN_OUTCOME, без перехода.
    expect(await act('activate', 'stocks', store, T0 + 30 * MIN)).toMatchObject({ outcome: 'NOOP' });
    // Владелец разобрал: прогон закрыт ERROR в журнале → activate доказывает дренаж и отдаёт Cloud Run.
    open.status = 'ERROR';
    expect(await act('activate', 'stocks', store, T0 + 40 * MIN)).toMatchObject({ outcome: 'TRANSITION', to: 'CLOUD_ACTIVE', owner: 'CLOUD_RUN', epoch: 3 });
  });

  it('пустой legacy watermark → STOP: Cloud Run не с чего начинать', async () => {
    const store = fencedStocks();
    expect(await tick('stocks', store, T0 + 8 * MIN)).toMatchObject({ to: 'STOP', attention: CUTOVER_STOP });
  });

  it('CUTOVER_LEGACY_DRAIN_MIN удлиняет дренаж (Workspace: лимит 30 мин)', async () => {
    const store = fencedStocks();
    store.watermark.stocks = '2026-11-16';
    expect(await tick('stocks', store, T0 + 20 * MIN, { legacyDrainMin: 31 })).toMatchObject({ outcome: 'NOOP' });
    expect(await tick('stocks', store, T0 + 31 * MIN, { legacyDrainMin: 31 })).toMatchObject({ to: 'CLOUD_ACTIVE' });
  });
});

describe('идемпотентность и гонка', () => {
  it('гонка двух tick: один переход, второй OK_ALREADY; журнал — одна строка, epoch вырос один раз', async () => {
    const store = fencedStocks();
    store.watermark.stocks = '2026-11-16';
    store.armRace(2);
    const [a, b] = await Promise.all([tick('stocks', store, T0 + 8 * MIN), tick('stocks', store, T0 + 8 * MIN)]);
    const outcomes = [a.outcome, b.outcome].sort();
    expect(outcomes).toEqual(['OK_ALREADY', 'TRANSITION']);
    expect(store.log).toHaveLength(1);
    expect(store.rows.get('stocks')).toMatchObject({ state: 'CLOUD_ACTIVE', owner: 'CLOUD_RUN', epoch: 3, revision: 4 });
  });

  it('гонка fence и rollback: проигравший CAS с другим целевым состоянием → CAS_CONFLICT, без записи', async () => {
    const store = qualifiedStocks();
    store.armRace(2);
    const [f, r] = await Promise.all([act('fence', 'stocks', store, T0), act('activate', 'stocks', store, T0)]);
    expect(f.outcome).toBe('TRANSITION');
    expect(r.outcome).toBe('REFUSED'); // activate из QUALIFIED недопустим вовсе
    const s2 = fencedStocks();
    s2.watermark.stocks = '2026-11-16';
    s2.armRace(2);
    const [x, y] = await Promise.all([act('activate', 'stocks', s2, T0 + 8 * MIN), act('rollback', 'stocks', s2, T0 + 8 * MIN)]);
    expect([x.outcome, y.outcome].sort()).toEqual(['CAS_CONFLICT', 'TRANSITION']);
    expect(s2.log).toHaveLength(1);
  });

  it('повтор той же команды — OK_ALREADY без CAS', async () => {
    const store = qualifiedStocks();
    expect(await act('fence', 'stocks', store, T0)).toMatchObject({ outcome: 'TRANSITION' });
    const calls = store.casCalls;
    expect(await act('fence', 'stocks', store, T0 + MIN)).toMatchObject({ outcome: 'OK_ALREADY' });
    expect(store.casCalls).toBe(calls);
    const m = new MemCutoverStore(seedRow('stocks', { owner: 'CLOUD_RUN', state: 'MONITORING', epoch: 3, revision: 5, fencedAtMs: T0, activeSinceMs: T0 }));
    expect(await act('activate', 'stocks', m, T0)).toMatchObject({ outcome: 'OK_ALREADY' });
    const l = new MemCutoverStore(seedRow('stocks'));
    expect(await act('rollback', 'stocks', l, T0)).toMatchObject({ outcome: 'OK_ALREADY' });
  });

  it('status ничего не меняет', async () => {
    const store = qualifiedStocks();
    const r = await act('status', 'stocks', store, T0, { autoFromEnv: true });
    expect(r).toMatchObject({ outcome: 'NOOP', to: 'LEGACY_FENCED' });
    expect(r.reason).toMatch(/^status: tick выполнил бы QUALIFIED → LEGACY_FENCED/);
    expect(store.casCalls).toBe(0);
  });
});

describe('откат', () => {
  const monitoring = () => {
    const s = new MemCutoverStore(seedRow('stocks', { owner: 'CLOUD_RUN', state: 'MONITORING', epoch: 3, revision: 5, fencedAtMs: T0 - DAY, activeSinceMs: T0 - DAY, legacyWatermark: '2026-11-15' }));
    s.watermark.stocks = '2026-11-16';
    return s;
  };

  it('CLOUD_RUN → NONE (дренаж Cloud Run: timeout Job + запас) → APPS_SCRIPT; двух писателей нет', async () => {
    const store = monitoring();
    expect(await act('rollback', 'stocks', store, T0)).toMatchObject({ outcome: 'TRANSITION', to: 'CLOUD_FENCED', owner: 'NONE', epoch: 4 });
    const wait = (PROFILES.stocks.cloudJobTimeoutMin + CLOUD_DRAIN_MARGIN_MIN) * MIN;
    expect((await tick('stocks', store, T0 + wait - MIN)).reason).toMatch(/дренаж CLOUD_RUN/);
    // Прогон Cloud Run, начатый до fence, завершился (ERROR WRITER_EPOCH_CHANGED — это исход).
    store.cloud.push({ stream: 'stocks', id: 'c', loader: 'stocks', startMs: T0 - 2 * MIN, status: 'ERROR', logicalPeriod: '2026-11-16', errorCode: 'WRITER_EPOCH_CHANGED' });
    expect(await tick('stocks', store, T0 + wait)).toMatchObject({ outcome: 'TRANSITION', to: 'LEGACY_ACTIVE', owner: 'APPS_SCRIPT', epoch: 5 });
    expect(store.rows.get('stocks')!.cloudWatermark).toBe('2026-11-16');
    expect(assertNoDirectHandover(store, 'stocks', 'CLOUD_RUN')).toEqual(['CLOUD_RUN', 'NONE', 'APPS_SCRIPT']);
  });

  it('Cloud Run STARTED без исхода (убит по timeout) → UNKNOWN_OUTCOME; rollback после разбора', async () => {
    const store = monitoring();
    await act('rollback', 'stocks', store, T0);
    const run = { stream: 'stocks' as Stream, id: 'c', loader: 'stocks', startMs: T0 - 2 * MIN, status: 'STARTED', logicalPeriod: '2026-11-16', errorCode: '' };
    store.cloud.push(run);
    const later = T0 + 40 * MIN;
    expect(await tick('stocks', store, later)).toMatchObject({ to: 'UNKNOWN_OUTCOME', attention: CUTOVER_UNKNOWN_OUTCOME });
    expect(await tick('stocks', store, later + MIN)).toMatchObject({ outcome: 'NOOP' });
    run.status = 'ERROR';
    expect(await act('rollback', 'stocks', store, later + 2 * MIN)).toMatchObject({ to: 'LEGACY_ACTIVE', owner: 'APPS_SCRIPT' });
    assertNoDirectHandover(store, 'stocks', 'CLOUD_RUN');
  });

  it('rollback до активации (LEGACY_FENCED) возвращает Apps Script сразу', async () => {
    const store = fencedStocks();
    expect(await act('rollback', 'stocks', store, T0 + MIN)).toMatchObject({ to: 'LEGACY_ACTIVE', owner: 'APPS_SCRIPT', epoch: 3 });
  });

  it('из ROLLBACK_REQUIRED: rollback или activate (мониторинг заново)', async () => {
    const a = new MemCutoverStore(seedRow('stocks', { owner: 'CLOUD_RUN', state: 'ROLLBACK_REQUIRED', epoch: 3, revision: 6, fencedAtMs: T0 - DAY, activeSinceMs: T0 - DAY }));
    expect(await tick('stocks', a, T0)).toMatchObject({ outcome: 'NOOP' });
    expect(await act('activate', 'stocks', a, T0)).toMatchObject({ to: 'MONITORING', epoch: 3 });
    expect(a.rows.get('stocks')!.activeSinceMs).toBe(T0);
    const b = new MemCutoverStore(seedRow('stocks', { owner: 'CLOUD_RUN', state: 'ROLLBACK_REQUIRED', epoch: 3, revision: 6, fencedAtMs: T0 - DAY, activeSinceMs: T0 - DAY }));
    expect(await act('rollback', 'stocks', b, T0)).toMatchObject({ to: 'CLOUD_FENCED', owner: 'NONE', epoch: 4 });
  });

  it('исчерпывающе: ни одно действие ни из одного состояния не передаёт владение AS ↔ CR напрямую', async () => {
    for (const state of STATES) {
      for (const action of ACTIONS) {
        const owner = OWNER_OF_STATE[state] ?? 'NONE';
        const store = new MemCutoverStore(seedRow('stocks', { owner, state, epoch: 5, revision: 9, fencedAtMs: T0 - DAY, activeSinceMs: T0 - DAY, legacyWatermark: '2026-11-15', autoCutoverEnabled: true }));
        store.passDays('stocks', STOCKS_GROUPS, '2026-11-15', 5);
        store.watermark.stocks = '2026-11-16';
        store.downstream.mart = true;
        for (let d = 0; d < 3; d++) store.cloud.push({ stream: 'stocks', id: `c${d}`, loader: 'stocks', startMs: T0 - DAY + d * H, status: 'COMPLETE', logicalPeriod: '2026-11-16', errorCode: '' });
        const r = await act(action, 'stocks', store, T0);
        if (r.outcome === 'TRANSITION') {
          expect(owner === 'APPS_SCRIPT' && r.owner === 'CLOUD_RUN', `${state}/${action}`).toBe(false);
          expect(owner === 'CLOUD_RUN' && r.owner === 'APPS_SCRIPT', `${state}/${action}`).toBe(false);
          expect(r.epoch, `${state}/${action}`).toBe(r.owner === owner ? 5 : 6);
          expect(OWNER_OF_STATE[r.to!] === null || OWNER_OF_STATE[r.to!] === r.owner, `${state}/${action} → ${r.to}`).toBe(true);
        }
      }
    }
  });
});

describe('мониторинг после активации', () => {
  const active = (stream: Stream, o: Parameters<typeof seedRow>[1] = {}) =>
    new MemCutoverStore(seedRow(stream, { owner: 'CLOUD_RUN', state: 'CLOUD_ACTIVE', epoch: 3, revision: 4, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, ...o }));

  it('ERROR/PARTIAL Cloud Run → ROLLBACK_REQUIRED (владелец CLOUD_RUN, данные идут)', async () => {
    const s = active('stocks', { legacyWatermark: '2026-11-16' });
    s.cloud.push({ stream: 'stocks', id: 'e', loader: 'stocks', startMs: T0 + H, status: 'ERROR', logicalPeriod: '2026-11-16', errorCode: 'STOCKS_T5_FAILED' });
    expect(await tick('stocks', s, T0 + 2 * H)).toMatchObject({ to: 'ROLLBACK_REQUIRED', owner: 'CLOUD_RUN', attention: CUTOVER_ROLLBACK_REQUIRED, epoch: 3 });
  });

  it('Apps Script пишет при владельце CLOUD_RUN → STOP и отсечение Cloud Run (NONE)', async () => {
    const s = active('ads', { legacyWatermark: '2026-11-15' });
    s.legacy.push({ stream: 'ads', journal: 'INGEST_RUNS', id: 'ADS_x', startMs: T0 + H, status: 'COMPLETE', shared: false });
    expect(await tick('ads', s, T0 + 2 * H)).toMatchObject({ to: 'STOP', owner: 'NONE', epoch: 4, attention: CUTOVER_STOP });
    expect(s.rows.get('ads')!.fencedAtMs).toBe(T0 + 2 * H);
  });

  it('общие RAW (одинаковый формат run_id у обоих писателей) не считаются Apps Script после активации', async () => {
    const s = active('supplies', { legacyWatermark: '2026-11-15' });
    s.legacy.push({ stream: 'supplies', journal: 'RAW_WB_SUPPLIES', id: 'DL_20261116_052900', startMs: T0 + H, status: 'LOADED', shared: true });
    expect(await tick('supplies', s, T0 + 2 * H)).toMatchObject({ outcome: 'NOOP', reason: 'ждём первый COMPLETE Cloud Run' });
  });

  it('нет первого COMPLETE за срок → ROLLBACK_REQUIRED (писатель не работает)', async () => {
    const s = active('statistics', { legacyWatermark: '{"orders":"2026-11-16 09:00:00","sales":"2026-11-16T09:00:00"}' });
    expect(await tick('statistics', s, T0 + 7 * H)).toMatchObject({ outcome: 'NOOP' });
    expect(await tick('statistics', s, T0 + 9 * H)).toMatchObject({ to: 'ROLLBACK_REQUIRED' });
  });

  it('остатки: два COMPLETE-снимка за сутки после fence → ROLLBACK_REQUIRED', async () => {
    const s = active('stocks', { legacyWatermark: '2026-11-16' });
    s.dups = 1;
    expect(await tick('stocks', s, T0 + H)).toMatchObject({ to: 'ROLLBACK_REQUIRED' });
  });

  it('финансы: COMPLETED только с недельным циклом понедельника', async () => {
    const s = new MemCutoverStore(seedRow('finance', { owner: 'CLOUD_RUN', state: 'MONITORING', epoch: 3, revision: 5, fencedAtMs: T0 - 7 * DAY, activeSinceMs: Date.parse('2026-11-11T04:00:00Z'), legacyWatermark: '2026-11-10' }));
    s.downstream.mart = true;
    const run = (iso: string, id: string) => ({ stream: 'finance' as Stream, id, loader: 'finance', startMs: Date.parse(iso), status: 'COMPLETE', logicalPeriod: '', errorCode: '' });
    s.cloud.push(run('2026-11-11T04:30:00Z', 'a'), run('2026-11-11T09:30:00Z', 'b'), run('2026-11-12T04:30:00Z', 'c'),
      run('2026-11-13T04:30:00Z', 'd'), run('2026-11-14T04:30:00Z', 'e'), run('2026-11-15T04:30:00Z', 'f'));
    expect((await tick('finance', s, Date.parse('2026-11-13T10:00:00Z'))).reason).toMatch(/недельный цикл finance/);
    s.cloud.push(run('2026-11-16T04:30:00Z', 'mon')); // пн 07:30 МСК
    expect(await tick('finance', s, Date.parse('2026-11-16T06:00:00Z'))).toMatchObject({ to: 'COMPLETED' });
  });

  it('REF: COMPLETED после того, как указатель REF_ACTIVE_VERSION переключён версией Cloud Run', async () => {
    const s = new MemCutoverStore(seedRow('ref', { owner: 'CLOUD_RUN', state: 'MONITORING', epoch: 3, revision: 5, fencedAtMs: T0 - 4 * DAY, activeSinceMs: T0 - 4 * DAY }));
    for (let i = 0; i < 3; i++) s.cloud.push({ stream: 'ref', id: `r${i}`, loader: 'ref-sync', startMs: T0 - (3 - i) * DAY, status: 'COMPLETE', logicalPeriod: '', errorCode: '' });
    expect((await tick('ref', s, T0)).reason).toMatch(/downstream ref_active/);
    s.downstream.ref_active = true;
    expect(await tick('ref', s, T0)).toMatchObject({ to: 'COMPLETED' });
  });
});

describe('первый прогон Cloud Run начинается с legacy watermark', () => {
  const LEGACY = '{"orders":"2026-11-16 09:31:05","sales":"2026-11-16T09:22:40"}';
  const activated = (cur: string) => {
    const s = new MemCutoverStore(seedRow('statistics', { owner: 'CLOUD_RUN', state: 'CLOUD_ACTIVE', epoch: 3, revision: 4, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, legacyWatermark: LEGACY }));
    s.watermark.statistics = cur;
    s.seenBefore = () => LEGACY; // до первого прогона в таблице ровно legacy
    s.cloud.push({ stream: 'statistics', id: 'o1', loader: 'orders', startMs: T0 + 2 * H, status: 'COMPLETE', logicalPeriod: '2026-11-15', errorCode: '' });
    return s;
  };

  it('активация фиксирует legacy watermark = текущая отметка таблицы после доказанного дренажа', async () => {
    const s = new MemCutoverStore(seedRow('statistics', { owner: 'NONE', state: 'LEGACY_FENCED', epoch: 2, revision: 3, fencedAtMs: T0 }));
    s.watermark.statistics = LEGACY;
    s.legacy.push({ stream: 'statistics', journal: 'INGEST_RUNS', id: 'r', startMs: T0 - 4 * MIN, status: 'COMPLETE', shared: false });
    s.legacy.push({ stream: 'statistics', journal: 'RAW_WB_ORDERS', id: 'ORD_INC_20261116_093105', startMs: T0 - 4 * MIN, status: 'LOADED', shared: true });
    expect(await tick('statistics', s, T0 + 8 * MIN)).toMatchObject({ to: 'CLOUD_ACTIVE' });
    expect(s.rows.get('statistics')!.legacyWatermark).toBe(LEGACY);
    expect(s.log[0]!.sourceWatermark).toBe(LEGACY);
  });

  it('отметка не откатилась и Cloud Run не писал строк старше неё → MONITORING', async () => {
    const s = activated('{"orders":"2026-11-16 15:41:00","sales":"2026-11-16T09:22:40"}');
    expect(await tick('statistics', s, T0 + 3 * H)).toMatchObject({ to: 'MONITORING' });
  });

  it('строки Cloud Run старше legacy watermark (начал не с него) → ROLLBACK_REQUIRED', async () => {
    const s = activated('{"orders":"2026-11-16 15:41:00","sales":"2026-11-16T15:22:00"}');
    s.violations = 12;
    const r = await tick('statistics', s, T0 + 3 * H);
    expect(r).toMatchObject({ to: 'ROLLBACK_REQUIRED', attention: CUTOVER_ROLLBACK_REQUIRED });
    expect(r.reason).toMatch(/старше legacy watermark/);
  });

  it('отметка таблицы ниже legacy → ROLLBACK_REQUIRED', async () => {
    const s = activated('{"orders":"2026-11-16 09:00:00","sales":"2026-11-16T09:22:40"}');
    expect((await tick('statistics', s, T0 + 3 * H)).reason).toMatch(/откатился/);
  });

  it('потоки с окном: разрыв больше перекрытия → ROLLBACK_REQUIRED; в пределах → MONITORING', async () => {
    const mk = (period: string) => {
      const s = new MemCutoverStore(seedRow('ads', { owner: 'CLOUD_RUN', state: 'CLOUD_ACTIVE', epoch: 3, revision: 4, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, legacyWatermark: '2026-11-08' }));
      s.cloud.push({ stream: 'ads', id: 'a', loader: 'ads', startMs: T0 + H, status: 'COMPLETE', logicalPeriod: period, errorCode: '' });
      return s;
    };
    expect(await tick('ads', mk('2026-11-15'), T0 + 2 * H)).toMatchObject({ to: 'MONITORING' });
    expect(await tick('ads', mk('2026-11-16'), T0 + 2 * H)).toMatchObject({ to: 'ROLLBACK_REQUIRED' });
  });

  it('SQL отметки statistics — те же выражения, что у загрузчика M2 (statistics/bq.ts)', () => {
    const sql = new BqCutoverStore({ projectId: 'p', query: async () => [] } as never, 'wb_ops', 'wb_raw').watermarkSql('statistics');
    expect(sql).toContain("COALESCE(MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'ORD_INC_'), last_change_date, NULL)), MAX(last_change_date))");
    expect(sql).toContain("MAX(IF(STARTS_WITH(IFNULL(load_id, ''), 'SALE_RECON_'), NULL, last_change_date))");
    expect(sql).toContain("source_api = 'WB_API_ORDERS' AND last_change_date <> ''");
  });
});

describe('инварианты строки', () => {
  it('нет строки → MISSING_ROW; состояние не совпадает с владельцем → REFUSED (CUTOVER_INVARIANT)', async () => {
    const empty = new MemCutoverStore();
    expect(await tick('ref', empty, T0)).toMatchObject({ outcome: 'MISSING_ROW', attention: CUTOVER_INVARIANT });
    const bad = new MemCutoverStore(seedRow('ref', { owner: 'CLOUD_RUN', state: 'LEGACY_ACTIVE' }));
    expect(await tick('ref', bad, T0)).toMatchObject({ outcome: 'REFUSED', attention: CUTOVER_INVARIANT });
    expect(bad.casCalls).toBe(0);
  });

  it('у каждого состояния, кроме STOP, ровно один допустимый владелец', () => {
    for (const s of STATES) expect(s === 'STOP' ? OWNER_OF_STATE[s] === null : OWNER_OF_STATE[s] !== null).toBe(true);
  });
});

describe('загрузчик cutover', () => {
  const ctx = () => ({
    config: loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw' }),
    logger: new Logger({}, 'error'), logicalPeriod: '2026-11-16T10', runId: 'r', targetDate: '2026-11-16T10',
  });

  it('env: действие, поток, auto-потоки, дренаж', () => {
    expect(parseCutoverEnv({})).toMatchObject({ action: 'tick', streams: ['stocks', 'statistics', 'ads', 'finance', 'supplies', 'ref'], legacyDrainMin: 7, opsDataset: 'wb_ops' });
    expect(() => parseCutoverEnv({ CUTOVER_ACTION: 'fence' })).toThrow(/явный CUTOVER_STREAM/);
    expect(() => parseCutoverEnv({ CUTOVER_ACTION: 'drop' })).toThrow(/CUTOVER_ACTION/);
    expect(() => parseCutoverEnv({ CUTOVER_STREAM: 'orders' })).toThrow(/CUTOVER_STREAM/);
    expect(() => parseCutoverEnv({ CUTOVER_LEGACY_DRAIN_MIN: '5' })).toThrow(/7\.\.120/);
    expect([...parseCutoverEnv({ CUTOVER_AUTO_STREAMS: 'stocks, ads' }).autoStreams]).toEqual(['stocks', 'ads']);
    try { parseCutoverEnv({ CUTOVER_AUTO_STREAMS: 'all' }); } catch (e) { expect((e as { code: string }).code).toBe(CUTOVER_CONFIG); }
  });

  it('tick по всем потокам: переход в STOP → CUTOVER_ATTENTION (exit 1 в cli); прочие потоки отработаны', async () => {
    const store = fencedStocks();
    store.rows.set('ref', seedRow('ref'));
    store.legacy.push({ stream: 'stocks', journal: 'WB_STOCKS_SNAPSHOTS', id: 'x', startMs: T0 + MIN, status: 'COMPLETE', shared: false });
    const loader = makeCutoverLoader({ env: { CUTOVER_ACTION: 'tick', CUTOVER_INCLUDE_STOCKS: '1' }, makeStore: () => store, now: () => T0 + 8 * MIN });
    await expect(loader(ctx())).rejects.toMatchObject({ code: CUTOVER_ATTENTION });
    expect(store.rows.get('stocks')!.state).toBe('STOP');
  });

  it('команда rollback без потока отклоняется; отклонённая команда — CUTOVER_CONFIG', async () => {
    const store = new MemCutoverStore(seedRow('ref'));
    const bad = makeCutoverLoader({ env: { CUTOVER_ACTION: 'rollback' }, makeStore: () => store, now: () => T0 });
    await expect(bad(ctx())).rejects.toMatchObject({ code: CUTOVER_CONFIG });
    const refused = makeCutoverLoader({ env: { CUTOVER_ACTION: 'fence', CUTOVER_STREAM: 'ref' }, makeStore: () => store, now: () => T0 });
    await expect(refused(ctx())).rejects.toMatchObject({ code: CUTOVER_CONFIG });
  });

  it('tick без событий — exit 0 (штатно), MISSING_ROW — внимание', async () => {
    const store = new MemCutoverStore(...(['stocks', 'statistics', 'ads', 'finance', 'supplies', 'ref'] as Stream[]).map((s) => seedRow(s)));
    const ok = makeCutoverLoader({ env: {}, makeStore: () => store, now: () => T0 });
    await expect(ok(ctx())).resolves.toMatchObject({ rowsFetched: 6, rowsLoaded: 0 });
    store.rows.delete('ads');
    await expect(ok(ctx())).rejects.toMatchObject({ code: CUTOVER_ATTENTION });
  });

  it('mskDate — сутки МСК', () => {
    expect(mskDate(Date.parse('2026-11-15T21:30:00Z'))).toBe('2026-11-16');
  });

  it('профили: дренаж Cloud Run не короче timeout Job потока; Apps Script — не меньше 7 мин', () => {
    expect(LEGACY_DRAIN_MIN).toBeGreaterThanOrEqual(7);
    const timeouts: Record<Stream, number> = { stocks: 30, statistics: 60, ads: 40, finance: 30, supplies: 30, ref: 10 };
    for (const [s, t] of Object.entries(timeouts) as Array<[Stream, number]>) {
      const p: StreamProfile = PROFILES[s];
      expect(p.cloudJobTimeoutMin, s).toBeGreaterThanOrEqual(t);
      const crMax = Math.max(...p.schedules.filter((e) => e.writer === 'CLOUD_RUN').map((e) => e.maxRunMin));
      expect(p.cloudJobTimeoutMin, s).toBeGreaterThanOrEqual(crMax);
    }
  });

  it('все состояния машины известны таблице', () => {
    const known: State[] = ['LEGACY_ACTIVE', 'SHADOW_VALIDATING', 'QUALIFIED', 'LEGACY_FENCED', 'CLOUD_ACTIVE', 'MONITORING', 'COMPLETED', 'BLOCKED', 'STOP', 'ROLLBACK_REQUIRED', 'UNKNOWN_OUTCOME', 'CLOUD_FENCED'];
    expect([...STATES].sort()).toEqual([...known].sort());
  });
});

describe('дополнения владельца: задания BigQuery, потерянный ACK, повтор cutover, покрытие, M1', () => {
  const job = (o: Partial<import('../src/loaders/cutover/store.js').PendingJob> & { createdMs: number }) =>
    ({ stream: 'stocks' as Stream, jobId: 'job_1', user: 'owner@example.com', state: 'RUNNING', table: 'RAW_WB_STOCKS', statementType: 'LOAD', ...o });

  it('дренаж: незавершённое задание BigQuery старого писателя → ждём; DONE → активация', async () => {
    const store = fencedStocks();
    store.watermark.stocks = '2026-11-16';
    const j = job({ createdMs: T0 - MIN });
    store.jobs.push(j);
    const r = await tick('stocks', store, T0 + 8 * MIN);
    expect(r).toMatchObject({ outcome: 'NOOP' });
    expect(r.reason).toMatch(/незавершённых заданий BigQuery/);
    j.state = 'DONE';
    expect(await tick('stocks', store, T0 + 9 * MIN)).toMatchObject({ to: 'CLOUD_ACTIVE' });
  });

  it('дренаж: задание в таблицы потока, созданное после fence → STOP (любой пользователь)', async () => {
    const store = fencedStocks();
    // Создано позже, чем мог жить любой прогон, начатый до fence (Cloud Run: timeout 30 + 5 мин).
    store.jobs.push(job({ createdMs: T0 + 36 * MIN, user: 'sa-loaders-prod@p.iam.gserviceaccount.com' }));
    expect(await tick('stocks', store, T0 + 37 * MIN)).toMatchObject({ to: 'STOP', attention: CUTOVER_STOP });
    // Задание прогона «в полёте» (gate пройден до fence) — ждём, а не STOP.
    const s2 = fencedStocks();
    s2.jobs.push(job({ createdMs: T0 + MIN }));
    expect((await tick('stocks', s2, T0 + 8 * MIN)).reason).toMatch(/незавершённых заданий BigQuery/);
  });

  it('откат тоже ждёт задания BigQuery Cloud Run', async () => {
    const store = new MemCutoverStore(seedRow('stocks', { owner: 'NONE', state: 'CLOUD_FENCED', epoch: 4, revision: 7, fencedAtMs: T0 }));
    store.jobs.push(job({ createdMs: T0 - 2 * MIN, statementType: 'SCRIPT', table: '' }));
    expect((await tick('stocks', store, T0 + 40 * MIN)).reason).toMatch(/незавершённых заданий BigQuery/);
  });

  it('потерянный ACK: CAS применился, ответ потерян → OK_ALREADY, второго перехода нет', async () => {
    const store = qualifiedStocks();
    store.loseNextCasAck = true;
    const r = await act('fence', 'stocks', store, T0);
    expect(r).toMatchObject({ outcome: 'OK_ALREADY', to: 'LEGACY_FENCED', epoch: 2 });
    expect(r.reason).toMatch(/ответ CAS потерян/);
    expect(store.log).toHaveLength(1);
    expect(await act('fence', 'stocks', store, T0 + MIN)).toMatchObject({ outcome: 'OK_ALREADY' });
    expect(store.log).toHaveLength(1);
    expect(store.rows.get('stocks')).toMatchObject({ epoch: 2, revision: 3 });
  });

  it('потерянный ответ без применения → отказ хранилища (повтор — следующим tick)', async () => {
    const store = qualifiedStocks();
    store.cas = async () => { throw new Error('socket hang up'); };
    await expect(act('fence', 'stocks', store, T0)).rejects.toThrow(/socket hang up/);
    expect(store.rows.get('stocks')!.state).toBe('QUALIFIED');
  });

  it('повтор cutover после отката: поколения растут монотонно, двух писателей нет', async () => {
    const store = qualifiedStocks();
    let t = T0;
    const go = async (a: Action, dt = MIN) => { t += dt; return act(a, 'stocks', store, t, { autoFromEnv: true }); };
    await go('fence', 0);
    await go('tick', 8 * MIN); // CLOUD_ACTIVE
    await go('rollback');      // CLOUD_FENCED
    await go('tick', 40 * MIN); // LEGACY_ACTIVE
    expect(store.rows.get('stocks')).toMatchObject({ state: 'LEGACY_ACTIVE', owner: 'APPS_SCRIPT', epoch: 5 });
    t = Date.parse('2026-11-17T07:00:00Z');
    store.passDays('stocks', STOCKS_GROUPS, '2026-11-16', 5);
    await go('tick', 0); // SHADOW_VALIDATING
    await go('tick');    // QUALIFIED
    await go('tick');    // LEGACY_FENCED
    expect(store.rows.get('stocks')).toMatchObject({ state: 'LEGACY_FENCED', epoch: 6 });
    await go('tick', 8 * MIN);
    expect(store.rows.get('stocks')).toMatchObject({ state: 'CLOUD_ACTIVE', owner: 'CLOUD_RUN', epoch: 7 });
    const epochs = store.log.map((l) => l.epoch);
    expect(epochs).toEqual([...epochs].sort((a, b) => a - b));
    assertNoDirectHandover(store, 'stocks');
  });

  it('MONITORING: первый прогон statistics прочитал не legacy (кто-то писал/удалял) → ROLLBACK_REQUIRED', async () => {
    const LEGACY = '{"orders":"2026-11-16 09:31:05","sales":"2026-11-16T09:22:40"}';
    const s = new MemCutoverStore(seedRow('statistics', { owner: 'CLOUD_RUN', state: 'MONITORING', epoch: 3, revision: 5, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, legacyWatermark: LEGACY }));
    s.watermark.statistics = '{"orders":"2026-11-18 09:00:00","sales":"2026-11-18T09:00:00"}';
    s.seenBefore = () => '{"orders":"2026-11-16 12:00:00","sales":"2026-11-16T09:22:40"}';
    s.downstream.mart = true;
    for (let d = 0; d < 3; d++) {
      for (const l of ['orders', 'sales', 'sales-reconcile']) {
        s.cloud.push({ stream: 'statistics', id: `${l}${d}`, loader: l, startMs: T0 + (d + 1) * 6 * H, status: 'COMPLETE', logicalPeriod: '', errorCode: '' });
      }
    }
    const r = await tick('statistics', s, T0 + 3 * DAY);
    expect(r).toMatchObject({ to: 'ROLLBACK_REQUIRED', attention: CUTOVER_ROLLBACK_REQUIRED });
    expect(r.reason).toMatch(/интервал от legacy не покрыт/);
    // Тот же поток, первый прогон прочитал legacy, но первая сверка стартовала через 9 суток — окно 7 не накрывает.
    const s2 = new MemCutoverStore(seedRow('statistics', { owner: 'CLOUD_RUN', state: 'MONITORING', epoch: 3, revision: 5, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, legacyWatermark: LEGACY }));
    s2.watermark.statistics = s.watermark.statistics;
    s2.seenBefore = () => LEGACY;
    s2.downstream.mart = true;
    s2.cloud.push(...s.cloud.filter((x) => x.loader !== 'sales-reconcile'));
    for (let d = 0; d < 3; d++) s2.cloud.push({ stream: 'statistics', id: `rc${d}`, loader: 'sales-reconcile', startMs: T0 + (9 + d) * DAY, status: 'COMPLETE', logicalPeriod: '', errorCode: '' });
    s2.cloud.sort((a, b) => a.startMs - b.startMs);
    expect((await tick('statistics', s2, T0 + 12 * DAY)).reason).toMatch(/не накрыла legacy watermark продаж/);
    // Всё сходится → COMPLETED (владелец вернул мониторинг командой activate).
    expect(await act('activate', 'statistics', s2, T0)).toMatchObject({ to: 'MONITORING' });
    s2.cloud = s2.cloud.filter((x) => x.loader !== 'sales-reconcile');
    for (let d = 0; d < 3; d++) s2.cloud.push({ stream: 'statistics', id: `rk${d}`, loader: 'sales-reconcile', startMs: T0 + (d + 1) * DAY, status: 'COMPLETE', logicalPeriod: '', errorCode: '' });
    s2.cloud.sort((a, b) => a.startMs - b.startMs);
    expect(await tick('statistics', s2, T0 + 12 * DAY)).toMatchObject({ to: 'COMPLETED' });
  });

  it('MONITORING: пропущенные сутки остатков (разрыв > 1) → ROLLBACK_REQUIRED', async () => {
    const s = new MemCutoverStore(seedRow('stocks', { owner: 'CLOUD_RUN', state: 'MONITORING', epoch: 3, revision: 5, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, legacyWatermark: '2026-11-16' }));
    for (const [i, d] of ['2026-11-17', '2026-11-19'].entries()) s.cloud.push({ stream: 'stocks', id: `c${i}`, loader: 'stocks', startMs: T0 + (i + 1) * DAY, status: 'COMPLETE', logicalPeriod: d, errorCode: '' });
    const r = await tick('stocks', s, T0 + 3 * DAY);
    expect(r).toMatchObject({ to: 'ROLLBACK_REQUIRED' });
    expect(r.reason).toMatch(/разрыв 2 сут между 2026-11-17 и периодом Cloud Run 2026-11-19/);
  });

  it('M1 (остатки) координатор по умолчанию не ведёт: tick — NOOP без чтения, команды — REFUSED', async () => {
    const store = qualifiedStocks({ autoCutoverEnabled: true });
    const t = await step('stocks', 'tick', { store, nowMs: T0, runId: 'r', changedBy: 't', autoFromEnv: true });
    expect(t).toMatchObject({ outcome: 'NOOP', from: null });
    expect(await step('stocks', 'fence', { store, nowMs: T0, runId: 'r', changedBy: 't', autoFromEnv: true })).toMatchObject({ outcome: 'REFUSED' });
    expect(store.casCalls).toBe(0);
    expect(PROFILES.stocks.coordinated).toBe(false);
    for (const s of ['statistics', 'ads', 'finance', 'supplies', 'ref'] as const) expect(PROFILES[s].coordinated).toBe(true);
  });

  it('SQL дренажа: JOBS_BY_PROJECT региона, state ≠ DONE, по таблицам потока (а не по пользователю)', () => {
    const st = new BqCutoverStore({ projectId: 'p', location: 'EU', query: async () => [] } as never, 'wb_ops', 'wb_raw');
    const ads = st.jobsSql('ads');
    expect(ads).toContain('`region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT');
    expect(ads).toContain("state != 'DONE'");
    expect(ads).toContain("destination_table.dataset_id = 'wb_raw'");
    expect(ads).toContain("'RAW_WB_ADV_COSTS_RUNS'");
    expect(ads).not.toContain('STARTS_WITH');
    expect(ads).not.toContain('RAW_WB_ADV_SEARCH_CLUSTERS');
    expect(ads).toContain("statement_type = 'SCRIPT'");
    expect(ads).toContain("job_type IN ('LOAD', 'COPY') OR statement_type IN ('INSERT', 'UPDATE', 'DELETE', 'MERGE'");
    expect(ads).not.toMatch(/user_email\s*(=|IN)/);
    // INGEST_RUNS общий у statistics и ads — дренаж одного потока не ловит работу другого.
    expect(st.exclusiveTables('ads')).not.toContain('INGEST_RUNS');
    expect(st.exclusiveTables('statistics')).toEqual(['RAW_WB_ORDERS', 'RAW_WB_SALES_RETURNS']);
    expect(st.jobsSql('finance')).toContain("'FINANCE_REPORT_LOADS'");
    // Регулярка SCRIPT: только DML в таблицу потока; shadow …__CR и чтение — не совпадают.
    const re = new RegExp(/r'\(\?i\)(.*)'\)\)\)$/s.exec(st.jobsSql('statistics'))![1]!.replace(/\\\\/g, '\\'), 'i');
    expect(re.test('MERGE `p.wb_raw.RAW_WB_ORDERS` T USING x')).toBe(true);
    expect(re.test('INSERT INTO `p.wb_raw.RAW_WB_SALES_RETURNS` (a) VALUES (1)')).toBe(true);
    expect(re.test('INSERT INTO `p.wb_raw.RAW_WB_ORDERS__CR` (a) VALUES (1)')).toBe(false);
    expect(re.test('SELECT * FROM `p.wb_raw.RAW_WB_ORDERS`')).toBe(false);
    expect(st.gateEvidenceSql('ads')).toContain("r'WRITER_GATE[:]AS[:]ads[:]v1'");
    expect(st.gateEvidenceSql('ads')).not.toContain('WRITER_GATE:AS');
  });
});

describe('ревью #426: resolve, мёртвые прогоны, запас gate→журнал, писатель без gate, доказательство gate', () => {
  it('STARTED, начатый задолго до fence (убит лимитом) — мёртвый, не UNKNOWN_OUTCOME', async () => {
    const store = fencedStocks();
    store.watermark.stocks = '2026-11-16';
    store.legacy.push({ stream: 'stocks', journal: 'WB_STOCKS_SNAPSHOTS', id: 'old', startMs: T0 - 3 * H, status: 'STARTED', shared: false });
    const r = await tick('stocks', store, T0 + 8 * MIN);
    expect(r).toMatchObject({ to: 'CLOUD_ACTIVE' });
    expect(store.log[0]!.evidence).toMatchObject({ detail: { deadStartedBeforeFence: [expect.objectContaining({ id: 'old' })] } });
  });

  it('прогон, прошедший gate до fence и записавший журнал сразу после (запас) — не нарушение; позже запаса — STOP', async () => {
    const ok = new MemCutoverStore(seedRow('stocks', { owner: 'NONE', state: 'CLOUD_FENCED', epoch: 4, revision: 7, fencedAtMs: T0 }));
    ok.cloud.push({ stream: 'stocks', id: 'inflight', loader: 'stocks', startMs: T0 + 90_000, status: 'ERROR', logicalPeriod: '2026-11-16', errorCode: 'WRITER_EPOCH_CHANGED' });
    expect(await tick('stocks', ok, T0 + 40 * MIN)).toMatchObject({ to: 'LEGACY_ACTIVE' });
    const bad = new MemCutoverStore(seedRow('stocks', { owner: 'NONE', state: 'CLOUD_FENCED', epoch: 4, revision: 7, fencedAtMs: T0 }));
    bad.cloud.push({ stream: 'stocks', id: 'late', loader: 'stocks', startMs: T0 + 4 * MIN, status: 'COMPLETE', logicalPeriod: '2026-11-16', errorCode: '' });
    expect(await tick('stocks', bad, T0 + 40 * MIN)).toMatchObject({ to: 'STOP', attention: CUTOVER_STOP });
  });

  it('resolve: выход из STOP/UNKNOWN_OUTCOME с причиной оператора и новым отсчётом дренажа', async () => {
    const store = fencedStocks();
    store.watermark.stocks = '2026-11-16';
    store.legacy.push({ stream: 'stocks', journal: 'WB_STOCKS_SNAPSHOTS', id: 'rogue', startMs: T0 + 5 * MIN, status: 'COMPLETE', shared: false });
    expect(await tick('stocks', store, T0 + 8 * MIN)).toMatchObject({ to: 'STOP' });
    expect(await act('resolve', 'stocks', store, T0 + H)).toMatchObject({ outcome: 'REFUSED' }); // без причины
    expect(await act('resolve', 'stocks', store, T0 + H, { operatorReason: 'разобрано: ручной запуск из редактора, данные сверены' })).toMatchObject({ outcome: 'REFUSED' }); // без цели
    const r = await act('resolve', 'stocks', store, T0 + H, { operatorReason: 'разобрано: ручной запуск из редактора, данные сверены', resolveTo: 'LEGACY_FENCED' });
    expect(r).toMatchObject({ outcome: 'TRANSITION', to: 'LEGACY_FENCED', owner: 'NONE', epoch: 2 });
    expect(store.rows.get('stocks')).toMatchObject({ fencedAtMs: T0 + H, checksSinceMs: T0 + H });
    expect(store.log.at(-1)!.reason).toBe('resolve: разобрано: ручной запуск из редактора, данные сверены');
    expect(await tick('stocks', store, T0 + H + 8 * MIN)).toMatchObject({ to: 'CLOUD_ACTIVE', epoch: 3 });
  });

  it('владелец не CLOUD_RUN, а prod-прогон Cloud Run есть → BLOCKED (писатель не под gate); снять — resolve', async () => {
    const store = qualifiedStocks({ checksSinceMs: T0 - DAY });
    store.cloud.push({ stream: 'stocks', id: 'old-image', loader: 'stocks', startMs: T0 - H, status: 'COMPLETE', logicalPeriod: '2026-11-16', errorCode: '' });
    const r = await tick('stocks', store, T0, { autoFromEnv: true });
    expect(r).toMatchObject({ to: 'BLOCKED', owner: 'APPS_SCRIPT', attention: 'CUTOVER_WRITER_NOT_GATED' });
    expect(await tick('stocks', store, T0 + 15 * MIN)).toMatchObject({ outcome: 'NOOP', from: 'BLOCKED' });
    expect(await act('resolve', 'stocks', store, T0 + 2 * H, { operatorReason: 'образ без gate заменён, прогон признан' })).toMatchObject({ to: 'SHADOW_VALIDATING' });
    expect(await tick('stocks', store, T0 + 2 * H + MIN)).toMatchObject({ to: 'QUALIFIED' });
  });

  it('перед fence: нет чтений строки владения Apps Script или Cloud Run за 24 ч → fence не ставится', async () => {
    for (const ev of [{ appsScript: 0, cloudRun: 5 }, { appsScript: 5, cloudRun: 0 }]) {
      const store = qualifiedStocks();
      store.evidence = ev;
      const r = await act('fence', 'stocks', store, T0);
      expect(r.outcome).toBe('NOOP');
      expect(r.reason).toMatch(/нет доказательства gate/);
    }
  });

  it('временный ERROR, закрытый COMPLETE повтором того же периода, — не ROLLBACK_REQUIRED', async () => {
    const s = new MemCutoverStore(seedRow('ads', { owner: 'CLOUD_RUN', state: 'CLOUD_ACTIVE', epoch: 3, revision: 4, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, legacyWatermark: '2026-11-15' }));
    s.cloud.push({ stream: 'ads', id: 'e', loader: 'ads', startMs: T0 + H, status: 'ERROR', logicalPeriod: '2026-11-16', errorCode: 'TRANSIENT_INFRA' });
    s.cloud.push({ stream: 'ads', id: 'c', loader: 'ads', startMs: T0 + 2 * H, status: 'COMPLETE', logicalPeriod: '2026-11-16', errorCode: '' });
    expect(await tick('ads', s, T0 + 3 * H)).toMatchObject({ to: 'MONITORING' });
  });

  it('проверки состояний Apps Script — раз в час на плановом tick (экономия BigQuery)', async () => {
    const store = new MemCutoverStore(seedRow('stocks'));
    store.passDays('stocks', STOCKS_GROUPS, '2026-11-15', 5);
    expect(await tick('stocks', store, T0, { hourlyChecks: false })).toMatchObject({ outcome: 'NOOP' });
    expect(await tick('stocks', store, T0, { hourlyChecks: true })).toMatchObject({ to: 'SHADOW_VALIDATING' });
  });

  it('граница «до первого прогона» — начало секунды старта (load_id несёт секунды)', async () => {
    const LEGACY = '{"orders":"2026-11-16 09:31:05","sales":"2026-11-16T09:22:40"}';
    const s = new MemCutoverStore(seedRow('statistics', { owner: 'CLOUD_RUN', state: 'CLOUD_ACTIVE', epoch: 3, revision: 4, fencedAtMs: T0 - 10 * MIN, activeSinceMs: T0, legacyWatermark: LEGACY }));
    s.watermark.statistics = '{"orders":"2026-11-16 15:41:00","sales":"2026-11-16T09:22:40"}';
    const seen: number[] = [];
    s.seenBefore = (b) => { seen.push(b); return LEGACY; };
    s.cloud.push({ stream: 'statistics', id: 'o1', loader: 'orders', startMs: T0 + 2 * H + 700, status: 'COMPLETE', logicalPeriod: '2026-11-15', errorCode: '' });
    expect(await tick('statistics', s, T0 + 3 * H)).toMatchObject({ to: 'MONITORING' });
    expect(seen).toEqual([T0 + 2 * H]);
  });
});
