/**
 * M4 · Сценарии тика финансового загрузчика (порт finDailyRun_) — офлайн: синтетический WB Finance API,
 * модель хранилища в памяти, поддельные часы. Проверяются: discovery DAILY/WEEKLY, загрузка, идемпотентность
 * повтора, финализация недель и RECON, «пропущено (final week)», неизменяемость COMPLETE, orphan guard,
 * KEY_CONFLICT, бюджет → PARTIAL, stale-recovery, потолок попыток, reportType=3, отказ хранилища.
 */
import { describe, it, expect } from 'vitest';
import { FinanceApi } from '../src/loaders/finance/wbApi.js';
import { runFinanceTick, runBackfillRegister, type TickDeps } from '../src/loaders/finance/run.js';
import { FakeClock, SynthFinanceApi, MemFinanceStore, synthRow, silentLogger, type SynthReport } from './finance_fixture.js';

// Вторник 10.11.2026 07:30 МСК. Теневой cutover — понедельник 02.11 (календарь недель: только 02.11).
const T0 = Date.UTC(2026, 10, 10, 4, 30);
const CUTOVER = '2026-11-02';

const ids = (prefix: number, n: number) => Array.from({ length: n }, (_, i) => `93${String(prefix).padStart(4, '0')}${String(i).padStart(12, '0')}`);
function rep(reportId: string, period: 'daily' | 'weekly', rt: number, from: string, to: string, rrds: string[]): SynthReport {
  return { reportId, reportType: rt, period, dateFrom: from, dateTo: to, rows: rrds.map((r, i) => synthRow(r, i, from)) };
}

const D1_SUN = rep('900000000000000201', 'daily', 1, '2026-11-08', '2026-11-08', ids(1, 3));
const D1_MON = rep('900000000000000202', 'daily', 1, '2026-11-09', '2026-11-09', ids(2, 2));
const D2_MON = rep('900000000000000203', 'daily', 2, '2026-11-09', '2026-11-09', ids(3, 1));
// Недельные за 02–08.11: type 1 содержит rrdId суточного 08.11 + свои; type 2 — свои.
const W1 = rep('900000000000000301', 'weekly', 1, '2026-11-02', '2026-11-08', [...ids(1, 3), ...ids(4, 2)]);
const W2 = rep('900000000000000302', 'weekly', 2, '2026-11-02', '2026-11-08', ids(5, 2));

function world(reports: SynthReport[], opts: { budgetMs?: number; t?: number } = {}) {
  const clock = new FakeClock(opts.t ?? T0);
  const wb = new SynthFinanceApi(reports, clock);
  const store = new MemFinanceStore(clock);
  let n = 0;
  const tick = async (over: Partial<TickDeps> = {}) => {
    const api = new FinanceApi({ host: 'https://finance.example.invalid', token: 't', t0: clock.now(), budgetMs: opts.budgetMs ?? 1_200_000, timeoutMs: 1000 }, clock, wb.fetch, silentLogger);
    n++;
    return runFinanceTick({ store, api, clock, logger: silentLogger, runId: `FINDAILY_TEST_${n}`, triggerType: 'AUTO', cutover: CUTOVER, ...over });
  };
  return { clock, wb, store, tick };
}

describe('тик: DAILY discovery и загрузка', () => {
  it('первый тик: календарь недель, 3 суточных отчёта открыты и загружены, журнал OK', async () => {
    const w = world([D1_SUN, D1_MON, D2_MON]);
    const r = await w.tick();
    expect(r.status).toBe('OK');
    expect(r.counters).toMatchObject({ discovered: 3, loaded: 3, errors: 0, requests: 5 }); // list daily + list weekly(204) + 3 detailed
    expect(r.rowsLoaded).toBe(6);
    expect(w.store.raw.length).toBe(6);
    expect([...w.store.weeks.keys()].sort()).toEqual(['2026-11-02|1', '2026-11-02|2']);
    expect(w.store.runs.get('FINDAILY_TEST_1')).toMatchObject({ status: 'OK', trigger_type: 'AUTO' });
    const l = w.store.loads.get(D1_SUN.reportId)!;
    expect(l).toMatchObject({ status: 'COMPLETE', report_period: 'DAILY', report_type: 1, attempt_count: 1, rows_fetched: 3, rows_loaded: 3 });
    expect(l.det!.det_forpay).toBe(l.list.list_forpay); // Σ detailed = Σ list на синтетике
    // daily list запрошен за сегодня−3…сегодня (МСК)
    expect(JSON.parse(w.wb.calls[0]!.body)).toMatchObject({ dateFrom: '2026-11-07', dateTo: '2026-11-10', period: 'daily', limit: 1000, offset: 0 });
    // строки RAW помечены своим прогоном и слоем
    expect(new Set(w.store.raw.map((x) => x.run_id))).toEqual(new Set(['FINDAILY_TEST_1']));
    expect(new Set(w.store.raw.map((x) => x.report_period))).toEqual(new Set(['DAILY']));
  });

  it('повтор (тот же набор отчётов) не дублирует RAW и не трогает COMPLETE → OK_NO_NEW', async () => {
    const w = world([D1_SUN, D1_MON, D2_MON]);
    await w.tick();
    const before = JSON.stringify(w.store.raw);
    const r2 = await w.tick();
    expect(r2.status).toBe('OK_NO_NEW');
    expect(r2.counters).toMatchObject({ discovered: 0, loaded: 0, errors: 0 });
    expect(JSON.stringify(w.store.raw)).toBe(before);
    expect(w.store.loads.get(D1_SUN.reportId)!.attempt_count).toBe(1);
  });

  it('пустой ответ list (204) → OK_NO_NEW, ничего не записано', async () => {
    const w = world([]);
    const r = await w.tick();
    expect(r.status).toBe('OK_NO_NEW');
    expect(w.store.raw.length).toBe(0);
    expect(w.store.loads.size).toBe(0);
  });
});

describe('тик: WEEKLY, финализация недели и RECON', () => {
  it('недельный отчёт загружен, неделя финализирована (оба типа), RECON посчитан', async () => {
    const w = world([D1_SUN, D1_MON, D2_MON]);
    await w.tick();
    w.wb.reports.push(W1, W2);
    w.clock.t += 5 * 3600_000; // 12:30 МСК
    const r = await w.tick();
    expect(r.status).toBe('OK');
    expect(r.counters.loaded).toBe(2);
    expect(r.finalized).toBe(2);
    expect(w.store.weeks.get('2026-11-02|1')).toMatchObject({ weekly_final: true, weekly_report_id: W1.reportId, daily_days_loaded: 1 });
    expect(w.store.weeks.get('2026-11-02|2')).toMatchObject({ weekly_final: true, weekly_report_id: W2.reportId, daily_days_loaded: 0 });
    // type 1: Σ daily (только 08.11) ≠ Σ weekly → WARN; метрики без движения → OK
    expect(w.store.recon.get('2026-11-02|1|forPaySum')!.recon_status).toBe('WARN');
    expect(w.store.recon.get('2026-11-02|1|penaltySum')!.recon_status).toBe('OK');
    expect(w.store.recon.size).toBe(14);
    // DAILY и WEEKLY лежат в RAW двумя слоями (законная двухслойность), но под разными report_id
    const w1rows = w.store.raw.filter((x) => x.report_id === W1.reportId);
    expect(w1rows.every((x) => x.report_period === 'WEEKLY')).toBe(true);
  });

  it('daily финализированной недели не регистрируется («пропущено (final week)»)', async () => {
    const w = world([W1, W2]);
    await w.tick(); // недели финализированы
    const LATE = rep('900000000000000204', 'daily', 1, '2026-11-07', '2026-11-07', ids(6, 1)); // неделя 02.11 уже FINAL
    w.wb.reports.push(LATE);
    const r = await w.tick();
    expect(w.store.loads.has(LATE.reportId)).toBe(false);
    expect(r.status).toBe('OK_NO_NEW');
  });

  it('weekly list не запрашивается, когда все недели финальны', async () => {
    const w = world([W1, W2]);
    await w.tick();
    const calls = w.wb.calls.length;
    await w.tick();
    const lists = w.wb.calls.slice(calls).map((c) => JSON.parse(c.body).period);
    expect(lists).toEqual(['daily']);
  });
});

describe('тик: fail-closed', () => {
  it('COMPLETE неизменяем: list с другими метриками → IMMUTABLE_ANOMALY, ERROR прогона, манифест не тронут', async () => {
    const w = world([D1_SUN]);
    await w.tick();
    const stored = { ...w.store.loads.get(D1_SUN.reportId)!.list };
    w.wb.reports[0] = { ...D1_SUN, rows: [...D1_SUN.rows, synthRow('9399990000000000001', 99, '2026-11-08')] };
    const r = await w.tick();
    expect(r.status).toBe('ERROR');
    expect(r.errorMessage).toContain('IMMUTABLE_ANOMALY');
    expect(w.store.loads.get(D1_SUN.reportId)!.list).toEqual(stored);
    expect(w.store.runs.get('FINDAILY_TEST_2')!.status).toBe('ERROR');
  });

  it('orphan guard: строки отчёта от чужого прогона → ERROR отчёта, append не выполняется', async () => {
    const w = world([D1_SUN]);
    w.store.raw.push({ report_id: D1_SUN.reportId, run_id: 'FINDAILY_GHOST', rrd_id: '1', _rr_date: '2026-11-08' });
    const r = await w.tick();
    expect(r.status).toBe('ERROR'); // errors>0 && loaded=0
    const l = w.store.loads.get(D1_SUN.reportId)!;
    expect(l.status).toBe('ERROR');
    expect(l.error_message).toContain('ORPHANED_RAW_REPORT');
    expect(w.store.raw.length).toBe(1);
    // detailed даже не запрашивался
    expect(w.wb.calls.some((c) => c.path.includes('/detailed/'))).toBe(false);
  });

  it('orphan guard шаг 2: строки отчёта вне периода манифеста (или с NULL-партицией) → ERROR', async () => {
    const w = world([D1_SUN]);
    w.store.raw.push({ report_id: D1_SUN.reportId, run_id: 'FINDAILY_GHOST', rrd_id: '1' }); // без _rr_date
    await w.tick();
    expect(w.store.loads.get(D1_SUN.reportId)!.error_message).toContain('ORPHANED_RAW_OUTSIDE_MANIFEST_PERIOD');
  });

  it('KEY_CONFLICT: дубль rrdId в detailed → ERROR, в RAW ничего', async () => {
    const dup = { ...D1_SUN, rows: [synthRow('9300000000000000001', 0, '2026-11-08'), synthRow('9300000000000000001', 1, '2026-11-08')] };
    const w = world([dup]);
    await w.tick();
    expect(w.store.loads.get(D1_SUN.reportId)!.error_message).toContain('KEY_CONFLICT: пустых rrdId=0, дублей=1');
    expect(w.store.raw.length).toBe(0);
  });

  it('reportType=3 в list → ошибка discovery, отчёты этого list не регистрируются', async () => {
    const w = world([]);
    w.wb.listOverride = (p) => (p === 'daily' ? [{ reportId: '900000000000000205', reportType: 3, dateFrom: '2026-11-09', dateTo: '2026-11-09' }] : []);
    const r = await w.tick();
    expect(r.status).toBe('ERROR');
    expect(r.errorMessage).toContain('неподдерживаемый reportType=3');
    expect(w.store.loads.size).toBe(0);
  });

  it('отказ хранилища посреди тика → журнал закрыт как ERROR (best-effort), исключение не вылетает', async () => {
    const w = world([D1_SUN]);
    w.store.failOn.orphanPruned = new Error('boom'); // guard ловит → отчёт ERROR
    const r = await w.tick();
    expect(w.store.loads.get(D1_SUN.reportId)!.status).toBe('ERROR');
    expect(r.status).toBe('ERROR');
    w.store.failOn = { runOpen: new Error('bq down') };
    const r2 = await w.tick();
    expect(r2.status).toBe('ERROR');
    expect(r2.errorMessage).toBe('bq down');
  });

  it('потолок попыток: ERROR-отчёт повторяется до 5 попыток, затем выпадает из очереди', async () => {
    const w = world([D1_SUN]);
    w.wb.overrides.push((path) => (path.includes('/detailed/') ? { status: 400, body: 'bad' } : undefined));
    for (let i = 0; i < 7; i++) { await w.tick(); w.clock.t += 3600_000; }
    const l = w.store.loads.get(D1_SUN.reportId)!;
    expect(l.attempt_count).toBe(5);
    expect(l.status).toBe('ERROR');
    expect(await w.store.queue(5)).toEqual([]);
  });
});

describe('тик: бюджет, очередь, recovery', () => {
  it('бюджет кончился → PARTIAL с хвостом; следующий тик дочитывает очередь ДО новых list', async () => {
    // 3 запроса помещаются: list daily, list weekly, 1 detailed (бюджет 4 мин, шаг пейсера 61 с, маржа 30 с)
    const w = world([D1_SUN, D1_MON, D2_MON], { budgetMs: 240_000 });
    const r1 = await w.tick();
    expect(r1.status).toBe('PARTIAL');
    expect(r1.queueLeft).toBeGreaterThan(0);
    const left = [...w.store.loads.values()].filter((l) => l.status === 'DISCOVERED').length;
    expect(left).toBe(r1.queueLeft);
    w.clock.t += 5 * 3600_000;
    const calls = w.wb.calls.length;
    const r2 = await w.tick();
    expect(w.wb.calls[calls]!.path).toContain('/detailed/'); // очередь — раньше discovery
    expect(r2.counters.loaded).toBeGreaterThan(0);
  });

  it('stale STARTED (> 120 мин) возвращается в DISCOVERED без инкремента попыток и догружается', async () => {
    const w = world([D1_SUN]);
    await w.store.mergeDiscovered({ reportId: D1_SUN.reportId, period: 'DAILY', reportType: 1, dateFrom: '2026-11-08', dateTo: '2026-11-08',
      metrics: { list_forpay: 0, list_retail: 0, list_delivery: 0, list_storage: 0, list_acceptance: 0, list_deduction: 0, list_penalty: 0 }, metricsJson: '{}' }, 'R0');
    await w.store.mergeStarted(D1_SUN.reportId, 'R_DEAD', 5);
    w.clock.t += 121 * 60_000;
    const r = await w.tick();
    const l = w.store.loads.get(D1_SUN.reportId)!;
    expect(l.status).toBe('COMPLETE');
    expect(l.attempt_count).toBe(2); // 1 (зависший) + 1 (этот тик)
    expect(r.counters.loaded).toBe(1);
  });

  it('повтор load-job внутри прогона (RESUME): строки этого прогона уже в RAW → append пропущен, COMPLETE', async () => {
    const w = world([D1_SUN]);
    // имитация: прогон уже записал строки и упал до COMPLETE, его же повтор (тот же run_id) видит свои строки
    const first = await w.tick();
    expect(first.status).toBe('OK');
    const l = w.store.loads.get(D1_SUN.reportId)!;
    l.status = 'ERROR'; l.processing_run_id = null;
    const r = await w.tick({ runId: 'FINDAILY_TEST_1' });
    expect(w.store.raw.length).toBe(3);
    expect(w.store.loads.get(D1_SUN.reportId)!.status).toBe('COMPLETE');
    expect(r.counters.loaded).toBe(1);
  });
});

describe('finance-backfill: регистрация WEEKLY без загрузки', () => {
  it('регистрирует недельные отчёты периода (DISCOVERED), RAW не трогает; журнал BACKFILL', async () => {
    const clock = new FakeClock(T0);
    const wb = new SynthFinanceApi([W1, W2], clock);
    const store = new MemFinanceStore(clock);
    const api = new FinanceApi({ host: 'https://finance.example.invalid', token: 't', t0: T0, budgetMs: 1_200_000, timeoutMs: 1000 }, clock, wb.fetch, silentLogger);
    const r = await runBackfillRegister({ store, api, clock, logger: silentLogger, runId: 'FINDAILY_BF', triggerType: 'BACKFILL', cutover: CUTOVER },
      '2026-11-02', '2026-11-08');
    expect(r.errorMessage).toBe('');
    expect(r.status).toBe('OK');
    expect(r.counters.discovered).toBe(2);
    expect([...store.loads.values()].map((l) => l.status)).toEqual(['DISCOVERED', 'DISCOVERED']);
    expect(store.raw.length).toBe(0);
    expect(store.runs.get('FINDAILY_BF')).toMatchObject({ status: 'OK', trigger_type: 'BACKFILL' });
    expect(wb.calls.length).toBe(1);
  });
});
