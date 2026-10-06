const { load, makeClock } = require('./harness');
const path = require('path');
// STEP 5A: по умолчанию тесты гонят исходники репозитория (apps-script/ingestion/).
// Второй аргумент позволяет указать другой каталог (например выгрузку production)
// для воспроизведения base-регрессии.
const dir = process.argv[2] ? path.resolve(process.argv[2]) : path.join(__dirname, '..', '..', 'apps-script', 'ingestion');
const variant = path.basename(dir);
const results = []; const t = (name, fn) => { try { fn(); results.push(['PASS', name]); } catch (e) { results.push(['FAIL', name, e.message.split('\n')[0]]); } };
const assert = (c, m) => { if (!c) throw new Error(m); };
const need = (ctx, fn) => assert(typeof ctx[fn] === 'function', 'нет функции ' + fn + ' (дефект не исправлен)');
const T0 = '2026-09-15T02:07:23Z'; // 05:07:23 MSK

function env(clock) { return load(dir, ['IngestRunLog.gs', 'WbAdsRawLoader.gs', 'Wbadsdaily.gs'], clock); }
function seed(e, clock, rows) { rows.forEach(r => e.table.push(Object.assign({ source: 'apps_script', completed_at: null }, r))); }
const reap = (e, extra) => e.ctx.ingestReapStaleRuns_ ? e.ctx.ingestReapStaleRuns_('ads', Object.assign({ activationTs: '2026-09-16T00:00:00Z', reaperRunId: 'R1' }, extra)) : null;

// 1–5 state machine
t('01 stale STARTED → ERROR/STALE_RUN_TIMEOUT с provenance', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'S', loader_name: 'ads', status: 'STARTED', started_at: Date.parse('2026-09-17T02:07:00Z') }]);
  const r = reap(e); const row = e.table[0];
  assert(r.status === 'REAPED' && r.reaped === 1, JSON.stringify(r));
  assert(row.status === 'ERROR' && row.error_code === 'STALE_RUN_TIMEOUT', row.status);
  assert(/reaper_run_id=R1/.test(row.error_message) && /threshold_min=15/.test(row.error_message), 'нет provenance');
});
t('02 COMPLETE неизменяем reaper-ом', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'C', loader_name: 'ads', status: 'COMPLETE', started_at: Date.parse('2026-09-17T02:00:00Z'), completed_at: 1 }]);
  reap(e); assert(e.table[0].status === 'COMPLETE' && e.table[0].completed_at === 1, 'COMPLETE изменён');
});
t('03 ERROR неизменяем reaper-ом', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'E', loader_name: 'ads', status: 'ERROR', error_code: 'ADS_TIMEOUT', started_at: Date.parse('2026-09-17T02:00:00Z'), completed_at: 1 }]);
  reap(e); assert(e.table[0].error_code === 'ADS_TIMEOUT', 'ERROR переписан');
});
t('04 свежий STARTED (<15 мин) не закрывается', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'F', loader_name: 'ads', status: 'STARTED', started_at: Date.parse('2026-09-17T02:46:00Z') }]);
  const r = reap(e); assert(e.table[0].status === 'STARTED' && r.reaped === 0, 'живой run закрыт');
});
t('05 повтор reaper идемпотентен (affected=0, NOTHING_TO_REAP)', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'S', loader_name: 'ads', status: 'STARTED', started_at: Date.parse('2026-09-17T02:07:00Z') }]);
  reap(e); const msg = e.table[0].error_message; const r2 = reap(e);
  assert(r2.status === 'NOTHING_TO_REAP' && e.table[0].error_message === msg, JSON.stringify(r2));
});
t('05b порог < 15 мин отвергается; нет activation → reaper выключен', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'S', loader_name: 'ads', status: 'STARTED', started_at: Date.parse('2026-09-17T02:07:00Z') }]);
  assert(reap(e, { thresholdMin: 5 }).status === 'FAILED', 'порог 5 мин принят');
  assert(e.ctx.ingestReapStaleRuns_('ads', {}).status === 'DISABLED_NO_ACTIVATION_TS', 'без activation reaper работает');
  assert(e.table[0].status === 'STARTED', 'строка тронута');
});
t('05c исторические STARTED до activation не трогаются (без отдельного approval)', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'INS_ADS_20260911050728_8e7cf2d0', loader_name: 'ads', status: 'STARTED', started_at: Date.parse('2026-09-11T02:07:29Z') }]);
  reap(e); assert(e.table[0].status === 'STARTED', 'исторический STARTED закрыт');
});
t('05d Cloud Run источник не трогается', () => {
  const c = makeClock('2026-09-17T03:00:00Z'), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  seed(e, c, [{ run_id: 'X', loader_name: 'ads', status: 'STARTED', source: 'cloud_run', started_at: Date.parse('2026-09-17T02:07:00Z') }]);
  reap(e); assert(e.table[0].status === 'STARTED', 'cloud_run закрыт');
});

// 6–7: hard kill → retry → COMPLETE; gate model stays fail-closed
function gateCovers(table, loader, targetDate) { // модель LATEST-ATTEMPT из wb-mart (cloud/src/loaders/mart/bq.ts), см. память
  const rows = table.filter(r => r.loader_name === loader).sort((a,b) => b.started_at - a.started_at || (b.run_id > a.run_id ? 1 : -1));
  const l = rows[0]; if (!l) return false;
  const nextDayMsk = Date.parse(targetDate + 'T00:00:00+03:00') + 864e5;
  return l.status === 'COMPLETE' && l.completed_at != null && l.started_at >= nextDayMsk;
}
t('06+07 kill после RAW → gate закрыт; reaper → всё ещё закрыт; retry → COMPLETE → gate открыт', () => {
  const c = makeClock(T0), e = env(c); need(e.ctx, 'ingestReapStaleRuns_');
  const id = e.ctx.ingestRunStart_('ads', '2026-09-14', 'SCHEDULED'); // hard kill: финализации нет
  assert(!gateCovers(e.table, 'ads', '2026-09-14'), 'gate открыт на STARTED');
  c.advance(20 * 60000); reap(e, { activationTs: '2026-09-15T00:00:00Z' });
  assert(e.table.find(r => r.run_id === id).status === 'ERROR', 'не reaped');
  assert(!gateCovers(e.table, 'ads', '2026-09-14'), 'reaper открыл gate (fail-open!)');
  const id2 = e.ctx.ingestRunStart_('ads', '2026-09-14', 'CATCHUP'); c.advance(230000);
  e.ctx.ingestFinalizeByStatus_(id2, 'ads', 'OK', 631, 631, '');
  assert(gateCovers(e.table, 'ads', '2026-09-14'), 'retry не открыл gate');
  assert(e.ctx.ingestRunError_(id2, 'X', 'y') === false && e.table.find(r => r.run_id === id2).status === 'COMPLETE', 'COMPLETE перезаписан');
});
t('06b catch-up decision: COMPLETE→skip, свежий STARTED→skip, ERROR/stale→run', () => {
  const c = makeClock(T0), e = env(c); need(e.ctx, 'wbAdsCatchUpDecision_');
  const d = e.ctx.wbAdsCatchUpDecision_;
  assert(d(null, 15) === 'RUN' && d({ status: 'COMPLETE', age_min: 999 }, 15) === 'SKIP_COMPLETE', 'a');
  assert(d({ status: 'STARTED', age_min: 3 }, 15) === 'SKIP_RUNNING' && d({ status: 'STARTED', age_min: 70 }, 15) === 'RUN' && d({ status: 'ERROR', age_min: 1 }, 15) === 'RUN', 'b');
});

// 8: D-14 costs actually requested by orchestrator
t('08 orchestrator запрашивает costs D−14…D−1', () => {
  const c = makeClock(T0), e = env(c); const calls = {};
  e.ctx.wbAdsBqSinkOn_ = () => true; e.ctx.wbAdsRawWriteStatus_ = () => {};
  e.ctx.loadWbAdsCampaignsRaw = () => ({ source: 'c', status: 'OK', rows: 1 });
  e.ctx.loadWbAdsCostsRaw = (f, to) => { calls.costs = [f, to]; return { source: 'k', status: 'OK', rows: 1 }; };
  e.ctx.loadWbAdsFullstatsRaw = (f, to) => { calls.fs = [f, to]; return { source: 'f', status: 'OK', rows: 1 }; };
  e.ctx.loadWbAdsQueryBidsRaw = () => ({ status: 'OK' }); e.ctx.loadWbAdsQueryStatsRaw = () => ({ status: 'OK' });
  const res = (e.ctx.runWbAdsDailyCore_ || e.ctx.runWbAdsDaily)('SCHEDULED');
  assert(calls.costs && calls.costs[0] === '2026-09-01' && calls.costs[1] === '2026-09-14', 'costs окно ' + JSON.stringify(calls.costs));
  assert(calls.fs[0] === '2026-09-08', 'fullstats окно изменилось ' + JSON.stringify(calls.fs));
});
t('08b ручной запуск помечается MANUAL, триггер — SCHEDULED', () => {
  const c = makeClock(T0), e = env(c);
  Object.assign(e.ctx, { wbAdsBqSinkOn_: () => true, wbAdsRawWriteStatus_: () => {}, loadWbAdsCampaignsRaw: () => ({ source:'c', status:'OK', rows:1 }),
    loadWbAdsCostsRaw: () => ({ source:'k', status:'OK', rows:1 }), loadWbAdsFullstatsRaw: () => ({ source:'f', status:'OK', rows:1 }) });
  e.ctx.runWbAdsDaily(); e.ctx.runWbAdsDaily({ triggerUid: '123' });
  const tt = e.table.map(r => r.trigger_type);
  assert(tt[0] === 'MANUAL' && tt[1] === 'SCHEDULED', 'trigger_type=' + tt);
});

// 10–11: deadline accounts for pause + request reserve; slow WB → no op guaranteed to cross wall
function simulateFullstats(ctx, clock, perCallMs, nIds) {
  ctx.WB_ADS_RAW_RUN_T0_ = clock.now();
  ctx.wbAdsHttp_ = () => { clock.advance(perCallMs); return { ok: true, code: 200, json: [] }; };
  ctx.wbAdsRawWriteStatus_ = () => {};
  const ids = Array.from({ length: nIds }, (_, i) => i + 1);
  clock.advance(36000); // campaigns + costs, как в проде (~36 с)
  const deadline = ctx.wbAdsRawDeadline_();
  const out = ctx.wbAdsFullstatsCollect_('tok', ids, '2026-09-08', '2026-09-14', 'r', deadline);
  return { elapsed: clock.now() - ctx.WB_ADS_RAW_RUN_T0_, calls: out.httpCalls, stopped: out.stopped };
}
t('10 нормальный WB (3 с/запрос, 431 id): все 9 вызовов, без остановки', () => {
  const c = makeClock(T0), e = load(dir, ['WbAdsRawLoader.gs'], c); const r = simulateFullstats(e.ctx, c, 3000, 431);
  assert(r.calls === 9 && !r.stopped, JSON.stringify(r));
});
t('11 медленный WB (25 с/запрос): последняя операция НЕ стартует так, что пробьёт 360 с с резервом финализации', () => {
  const c = makeClock(T0), e = load(dir, ['WbAdsRawLoader.gs'], c); const r = simulateFullstats(e.ctx, c, 25000, 431);
  const reserve = e.ctx.WB_ADS_FINALIZE_RESERVE_MS_;
  assert(typeof reserve === 'number' && reserve > 0, 'нет WB_ADS_FINALIZE_RESERVE_MS_ (дефект не исправлен)');
  assert(r.elapsed + reserve <= e.ctx.WB_ADS_HARD_WALL_MS_, 'критичный путь заканчивается на ' + (r.elapsed/1000) + ' с, финализации нет места (' + JSON.stringify(r) + ')');
  assert(r.stopped, 'остановка не зафиксирована → статус не станет PARTIAL');
});
t('11b хвост (query_stats) не стартует у стены прогона', () => {
  const c = makeClock(T0), e = load(dir, ['WbAdsRawLoader.gs', 'Wbadsquerystats.gs'], c);
  e.ctx.WB_ADS_RAW_RUN_T0_ = c.now(); c.advance(300000);
  assert(e.ctx.wbAdsQsOutOfBudget_({ t0: c.now() - 1000 }) === true, 'хвост стартует на 300 с');
});

// 12–13: дефекты, найденные на PRE-PR REVIEW
t('12 установщик catch-up идемпотентен из ЛЮБОГО состояния (0/1/2/3 → ровно 2)', () => {
  const c = makeClock(T0);
  for (const start of [0, 1, 2, 3]) {
    const e = load(dir, ['Wbadsdaily.gs'], c);
    need(e.ctx, 'wbAdsInstallCatchUpTriggers');
    for (let i = 0; i < start; i++) e.triggers.push({ getHandlerFunction: () => 'runWbAdsDailyCatchUp' });
    e.triggers.push({ getHandlerFunction: () => 'runWbAdsDaily' });          // чужой триггер
    e.ctx.wbAdsInstallCatchUpTriggers();
    const mine = e.triggers.filter(x => x.getHandlerFunction() === 'runWbAdsDailyCatchUp');
    assert(mine.length === 2, 'старт ' + start + ' → ' + mine.length + ' catch-up триггеров');
    assert(e.triggers.some(x => x.getHandlerFunction() === 'runWbAdsDaily'), 'снесён чужой триггер');
    const hours = mine.map(x => x.hour).sort();
    assert(hours[0] === 6 && hours[1] === 8, 'часы ' + JSON.stringify(hours));
    assert(mine.every(x => x.tz === 'Europe/Moscow'), 'пояс не задан явно');
  }
  // повторный вызов подряд тоже даёт 2, а не 4
  const e2 = load(dir, ['Wbadsdaily.gs'], c);
  e2.ctx.wbAdsInstallCatchUpTriggers(); e2.ctx.wbAdsInstallCatchUpTriggers();
  assert(e2.triggers.length === 2, 'два вызова подряд → ' + e2.triggers.length);
});
t('13 catch-up не дублирует прогон: COMPLETE, случившийся пока ждали lock, отменяет запуск', () => {
  const c = makeClock('2026-09-15T06:15:00Z'), e = env(c);
  need(e.ctx, 'wbAdsCatchUpDecision_');
  const lp = e.ctx.wbAdsLast7Range_().to;
  // на момент решения — старый ERROR, значит catch-up решает RUN
  seed(e, c, [{ run_id: 'OLD', loader_name: 'ads', logical_period: lp, status: 'ERROR',
                started_at: Date.parse('2026-09-15T02:07:00Z'), completed_at: 1 }]);
  const before = e.table.length;
  // конкурент закрылся COMPLETE, пока catch-up ждал ScriptLock
  const race = () => { e.table.push({ run_id: 'WINNER', loader_name: 'ads', logical_period: lp, status: 'COMPLETE',
      source: 'apps_script', started_at: c.now() - 60000, completed_at: c.now() }); return 'SKIP_COMPLETE'; };
  const res = e.ctx.runWbAdsDailyCore_('CATCHUP', race);
  assert(res && res.status === 'SKIP_COMPLETE' && res.recheck === true, JSON.stringify(res));
  assert(e.table.length === before + 1, 'catch-up открыл свою строку журнала вопреки отмене');
  assert(!e.table.some(x => x.trigger_type === 'CATCHUP'), 'создан конкурирующий CATCHUP-run');
});


// 14–16: порядок fullstats (инцидент 01.10.2026 — бюджет кончился на завершённых кампаниях)
// Население как в проде 01.10: 411 завершённых (7) идут у WB первыми, затем 19 на паузе (11) и 1 активная (9).
function incidentPopulation() {
  const ids = [], st = {};
  for (let i = 0; i < 411; i++) { const id = 19722775 + i * 43000; ids.push(id); st[id] = 7; }
  for (let i = 0; i < 19; i++) { const id = 40000000 + i; ids.push(id); st[id] = 11; }
  ids.push(40500000); st[40500000] = 9;
  return { ids, st };
}
t('14 порядок 9→11→7: тот же набор и кратность, завершённые не исключены, внутри группы по возрастанию', () => {
  const c = makeClock(T0), e = load(dir, ['WbAdsRawLoader.gs'], c);
  need(e.ctx, 'wbAdsOrderFullstatsIds_');
  const ids = [7007, 9002, 11005, 7001, 9001, 11001, 555, 7001], st = { 7007: 7, 7001: 7, 9002: 9, 9001: 9, 11005: 11, 11001: 11 };
  const out = e.ctx.wbAdsOrderFullstatsIds_(ids, st);
  assert(JSON.stringify(out) === JSON.stringify([9001, 9002, 11001, 11005, 7001, 7001, 7007, 555]), 'порядок ' + JSON.stringify(out));
  assert(JSON.stringify(ids) === JSON.stringify([7007, 9002, 11005, 7001, 9001, 11001, 555, 7001]), 'вход изменён на месте');
  const shuffled = ids.slice().reverse();
  assert(JSON.stringify(e.ctx.wbAdsOrderFullstatsIds_(shuffled, st)) === JSON.stringify(out), 'порядок зависит от порядка WB');
  const p = incidentPopulation(), o = e.ctx.wbAdsOrderFullstatsIds_(p.ids, p.st);
  assert(o.length === 431 && JSON.stringify(o.slice().sort()) === JSON.stringify(p.ids.slice().sort()), 'набор изменился');
  assert(p.st[o[0]] === 9 && o.slice(1, 20).every(id => p.st[id] === 11) && o.slice(20).every(id => p.st[id] === 7), 'группы');
});
function replay(ordered) {
  const c = makeClock(T0), e = load(dir, ['WbAdsRawLoader.gs'], c), p = incidentPopulation();
  e.ctx.WB_ADS_RAW_RUN_T0_ = c.now();
  e.ctx.wbAdsRawWriteStatus_ = () => {};
  // как 01.10: ~2,6 с на вызов, стата есть только у кампаний 9/11
  e.ctx.wbAdsHttp_ = (m, url) => { c.advance(2600);
    const ids = url.split('ids=')[1].split('&')[0].split(',').map(Number);
    return { ok: true, code: 200, json: ids.filter(id => p.st[id] !== 7).map(id => ({ advertId: id, days: [] })) }; };
  c.advance(100000); // медленный WB до fullstats (01.10: ~92–103 с)
  const list = ordered ? e.ctx.wbAdsOrderFullstatsIds_(p.ids, p.st) : p.ids;
  const out = e.ctx.wbAdsFullstatsCollect_('tok', list, '2026-09-24', '2026-09-30', 'r', e.ctx.wbAdsRawDeadline_());
  const got = new Set(out.collected.map(x => x.advertId));
  return { out, p, live: p.ids.filter(id => p.st[id] !== 7), got };
}
t('15 повтор 01.10: в порядке WB активная/паузные пропущены (воспроизведение), с порядком 9→11→7 — собраны', () => {
  const base = replay(false);
  assert(base.out.stopped && base.live.every(id => base.out.skipped.includes(id)), 'база не воспроизводит инцидент: ' + JSON.stringify({ stopped: base.out.stopped, skipped: base.out.skipped.length }));
  const fix = replay(true);
  assert(fix.live.every(id => fix.got.has(id)), 'активные/паузные не собраны: ' + fix.live.filter(id => !fix.got.has(id)).length);
  assert(!fix.live.some(id => fix.out.skipped.includes(id)), 'активные/паузные в skipped');
  assert(fix.out.stopped === base.out.stopped, 'бюджет и разбиение не должны меняться — меняется только порядок');
  assert(fix.out.skipped.every(id => fix.p.st[id] === 7), 'пропущены не только завершённые');
});
t('16 PARTIAL остаётся PARTIAL; campaigns_sampled = реально отправленные id', () => {
  const c = makeClock(T0), e = load(dir, ['WbAdsRawLoader.gs'], c), p = incidentPopulation(), statuses = [];
  e.ctx.WB_ADS_RAW_RUN_T0_ = c.now();
  Object.assign(e.ctx, {
    SpreadsheetApp: { getActiveSpreadsheet: () => ({}) },
    wbAdsResolveRunId_: (r) => r,
    wbAdsRawNormalizeRange_: (f, t2) => ({ from: f, to: t2 }),
    wbAdsSplitPeriod_: (f, t2) => [{ from: f, to: t2 }],
    getWbAdsToken_: () => ({ token: 't' }),
    wbAdsFetchCampaigns_: () => ({ advertIds: p.ids, statsAdvertIds: p.ids.slice(), statusById: p.st, countHttp: 200 }),
    wbAdvRawEnsureSheet_: () => ({}), wbAdvRawAppendRows_: (sh, rows) => rows.length,
    wbAdvFlattenFullstats_: (col) => ({ statRows: col.map(() => [1]), boosterRows: [] }),
    wbAdvCampaignStatNoStatsRow_: () => [0],
    wbAdsRawWriteStatus_: (rid, src, f, t2, o) => statuses.push(o),
    wbAdsHttp_: () => { c.advance(2600); return { ok: true, code: 200, json: [] }; },
  });
  c.advance(100000);
  const r = e.ctx.loadWbAdsFullstatsRaw('2026-09-24', '2026-09-30', 'RID');
  const last = statuses[statuses.length - 1];
  assert(r.status === 'PARTIAL', 'статус ' + r.status);
  assert(last.status === 'PARTIAL' && last.campaigns_found === 431, JSON.stringify(last));
  assert(last.campaigns_sampled < 431 && last.campaigns_sampled > 0, 'campaigns_sampled=' + last.campaigns_sampled + ' (должно быть число реально отправленных)');
  const partialRow = statuses.find(s => /тайм-бюджету/.test(s.error_message || ''));
  assert(partialRow && partialRow.campaigns_sampled === last.campaigns_sampled, 'строки PARTIAL расходятся');
});

results.forEach(r => console.log(r.join('  ')));
const f = results.filter(r => r[0] === 'FAIL').length;
console.log(`\n${variant}: ${results.length - f}/${results.length} PASS`);
process.exitCode = 0;
