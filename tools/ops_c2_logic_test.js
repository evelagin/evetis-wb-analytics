#!/usr/bin/env node
/**
 * EVETIS OPERATIONS · C2 — локальные тесты чистой логики Apps Script (без Google).
 *
 * Загружает apps-script/evetis_operations/{OpsConfig,OpsCore,OpsInstall}.gs в изолированный контекст и проверяет:
 * проверку выборок (FAIL CLOSED), хеш снимка, отпечаток, контракт раскладки, установку поверх C1.2,
 * машину состояний решения владельца (сценарии тестов 2, 3, 11, 12, 13) и отрисовку.
 *
 * Запуск:  node tools/ops_c2_logic_test.js <каталог фикстур>
 * Фикстуры: python3 tools/ops_c2_fixtures.py <каталог> <экспорт книги C1.2 .xlsx>
 */
'use strict';
const vm = require('vm');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const FIX = process.argv[2] || process.env.OPS_C2_FIXTURES;
if (!FIX) {
  console.error('Укажите каталог фикстур: node tools/ops_c2_logic_test.js <каталог>');
  process.exit(2);
}
const SRC = path.join(__dirname, '..', 'apps-script', 'evetis_operations');

function formatDate(d, tz, pattern) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false
  }).formatToParts(d).map((x) => [x.type, x.value]));
  return pattern.replace('yyyy', p.year).replace('MM', p.month).replace('dd', p.day)
    .replace('HH', p.hour === '24' ? '00' : p.hour).replace('mm', p.minute).replace('ss', p.second);
}

function parseDate(s, tz, fmt) {
  if (typeof tz !== 'string' || !tz) throw new Error('Invalid argument: timeZone. Should be of type: String');
  return { s, tz, fmt };
}

const ctx = { console, Utilities: { formatDate, parseDate } };
vm.createContext(ctx);
for (const f of ['OpsConfig.gs', 'OpsCore.gs', 'OpsInstall.gs', 'OpsSheetIO.gs']) {
  vm.runInContext(fs.readFileSync(path.join(SRC, f), 'utf8'), ctx, { filename: f });
}
const G = ctx;

let passed = 0;
let failed = 0;
function t(name, fn) {
  try {
    fn();
    passed++;
    console.log('PASS ' + name);
  } catch (e) {
    failed++;
    console.log('FAIL ' + name + '\n     → ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n       ') : e));
  }
}
function eq(a, b, msg) {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error((msg || 'не равно') + ': ' + JSON.stringify(a) + ' ≠ ' + JSON.stringify(b));
}
function ok(c, msg) {
  if (!c) throw new Error(msg || 'условие не выполнено');
}
function throwsCode(fn, code) {
  try {
    fn();
  } catch (e) {
    if (e.opsCode === code) return e;
    throw new Error('ожидалась ошибка ' + code + ', получена ' + (e.opsCode || '') + ' ' + e.message);
  }
  throw new Error('ожидалась ошибка ' + code + ', ошибки нет');
}
const clone = (x) => JSON.parse(JSON.stringify(x));

function loadPayload() {
  const views = {};
  for (const name of Object.keys(G.OPS_VIEWS)) {
    const raw = JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));
    const fields = raw.schema.fields.map((f) => ({ name: f.name, type: f.type }));
    views[name] = { fields, rows: G.opsTypeRows_(fields, raw.rows) };
  }
  return { fetchedAtMs: Date.parse('2026-09-14T07:00:00Z'), views };
}
const P0 = loadPayload();
const plan0 = P0.views.V_OPS_SHEET_PLAN.rows;
const ship = (b) => plan0.filter((r) => r.sheet_block === b).sort((x, y) => x.block_order - y.block_order);
const statusOk = {
  status: 'OK', message: '', dataAsOfMs: P0.fetchedAtMs, lastSuccessMs: P0.fetchedAtMs, lastCheckMs: P0.fetchedAtMs,
  tail: G.opsStatusLineTail_(P0), freshness: 'x', nextAuto: 'каждый час', checkResult: 'данные обновлены'
};

// ───────────── проверка выборок ─────────────
t('выборки контракта проходят проверку', () => {
  const v = G.opsValidatePayload_(clone(P0));
  ok(Array.isArray(v.warnings), 'warnings');
  eq(v.rowsReceived, Object.values(P0.views).reduce((s, x) => s + x.rows.length, 0), 'rowsReceived');
});
t('нет колонки → SCHEMA', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_PLAN.fields = p.views.V_OPS_SHEET_PLAN.fields.filter((f) => f.name !== 'rec_fingerprint');
  throwsCode(() => G.opsValidatePayload_(p), 'SCHEMA');
});
t('повтор канонического ключа → DUP_KEY', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_PLAN.rows.push(clone(p.views.V_OPS_SHEET_PLAN.rows[0]));
  throwsCode(() => G.opsValidatePayload_(p), 'DUP_KEY');
});
t('ключ не канонический → SCHEMA', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_PLAN.rows[0].business_key = 'SUPPLY_SHIP|WB|' + p.views.V_OPS_SHEET_PLAN.rows[0].product_name;
  throwsCode(() => G.opsValidatePayload_(p), 'SCHEMA');
});
t('выборки не сходятся → RECONCILE', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_PICK.rows.forEach((r) => { r.t_total_physical_demand += 1; });
  throwsCode(() => G.opsValidatePayload_(p), 'RECONCILE');
});
t('выборки за разные дни → INCONSISTENT_DAY', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_BOM.rows.forEach((r) => { r.contract_today = '2099-01-01'; });
  throwsCode(() => G.opsValidatePayload_(p), 'INCONSISTENT_DAY');
});
t('пустая META → ROWCOUNT', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_META.rows = [];
  throwsCode(() => G.opsValidatePayload_(p), 'ROWCOUNT');
});
t('устаревший источник → предупреждение, не ошибка', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_META.rows[0].wb_stock_stale = true;
  eq(G.opsValidatePayload_(p).warnings, ['остатки WB']);
});

// ───────────── хеш и отпечаток ─────────────
t('хеш снимка: стабилен, не зависит от computed_at, меняется от данных', () => {
  const a = G.opsHashInput_(clone(P0));
  const p1 = clone(P0);
  p1.views.V_OPS_SHEET_PLAN.rows.forEach((r) => { r.computed_at = 1; });
  eq(G.opsHashInput_(p1), a, 'computed_at влияет на хеш');
  const p2 = clone(P0);
  p2.views.V_OPS_SHEET_PLAN.rows[0].rec_final += 1;
  ok(G.opsHashInput_(p2) !== a, 'изменение данных не меняет хеш');
});
t('отпечаток SQL = SHA-256 сути решения (формула тестов Apps Script)', () => {
  for (const r of plan0) {
    const s = [r.business_key, r.shipping_method || '-', r.status_code || '-', r.rule || '-', r.rec_final, r.rec_physical_units, r.gates || ''].join('|');
    eq(crypto.createHash('sha256').update(s, 'utf8').digest('hex').slice(0, 16), r.rec_fingerprint, r.business_key);
  }
});

// ───────────── установка поверх C1.2 и контракт раскладки ─────────────
const colA = JSON.parse(fs.readFileSync(path.join(FIX, 'live_colA.json'), 'utf8'));
function installedMarkers(name) {
  let m = G.opsDiscoverC12_(name, colA[name]);
  if (name === '04_SETTINGS') {
    const ins = ['@S'].concat(G.OPS_STATUS_KEYS.map((k) => '@R:REFRESH_STATUS:' + k), ['@S']);
    m = m.slice(0, 3).concat(ins, m.slice(3));
  }
  return m;
}
const EXPECT_C12 = {
  '01_SUPPLY_PLAN': { WB_SHIP: 15, OZON_SHIP: 11, HOLD: 7, TASK_MOVE: 9, TASK_BUILD: 10, TASK_COMP: 9, USEND_TOP: 6, USEND_TASK: 6 },
  '02_SHIPMENTS': { SHIPMENTS: 9 },
  '03_FF_STOCK': { FF_STOCK: 11 },
  '04_SETTINGS': { CHANNELS: 3, CONFIG: 6, LOGISTICS: 45, RULES: 7, FRESHNESS: 6, REFRESH_STATUS: 5 },
  '05_РАСЧЁТ': { CALC_WB: 25, CALC_OZON: 20, CALC_BUNDLES: 14, CALC_BOM: 24, CALC_PICK: 11 }
};
for (const name of Object.keys(EXPECT_C12)) {
  t('установка C1.2 → маркеры и контракт: ' + name, () => {
    const m = installedMarkers(name);
    const model = G.opsParseLayout_(m, G.OPS_LAYOUT[name], {});
    for (const [id, n] of Object.entries(EXPECT_C12[name])) {
      const b = model.blocks[id];
      eq(b.lastRow - b.firstRow + 1, n, id);
    }
    eq(m[m.length - 1], '@END');
  });
}
t('установка: изменённая шапка C1.2 → LAYOUT, без записи', () => {
  const a = clone(colA['01_SUPPLY_PLAN']);
  a[11] = 'Товар!';
  throwsCode(() => G.opsDiscoverC12_('01_SUPPLY_PLAN', a), 'LAYOUT');
});
t('установка: лишняя строка в конце → LAYOUT', () => {
  const a = clone(colA['02_SHIPMENTS']).concat(['x']);
  throwsCode(() => G.opsDiscoverC12_('02_SHIPMENTS', a), 'LAYOUT');
});
t('установка: пустая строка внутри блока → LAYOUT', () => {
  const a = clone(colA['01_SUPPLY_PLAN']);
  a[14] = '';
  throwsCode(() => G.opsDiscoverC12_('01_SUPPLY_PLAN', a), 'LAYOUT');
});

const spec01 = G.OPS_LAYOUT['01_SUPPLY_PLAN'];
t('раскладка: нет @END → LAYOUT', () => {
  throwsCode(() => G.opsParseLayout_(installedMarkers('01_SUPPLY_PLAN').slice(0, -1), spec01, {}), 'LAYOUT');
});
t('раскладка: другая версия → LAYOUT', () => {
  const m = installedMarkers('01_SUPPLY_PLAN');
  m[0] = '@L:C2L0';
  throwsCode(() => G.opsParseLayout_(m, spec01, {}), 'LAYOUT');
});
t('раскладка: строка без маркера → LAYOUT; при восстановлении — строка блока', () => {
  const m = installedMarkers('01_SUPPLY_PLAN');
  const model = G.opsParseLayout_(m, spec01, {});
  const at = model.blocks.WB_SHIP.lastRow;   // индекс следующей строки в массиве = lastRow
  m.splice(at, 0, '');
  throwsCode(() => G.opsParseLayout_(m, spec01, {}), 'LAYOUT');
  const rec = G.opsParseLayout_(m, spec01, { recovery: true });
  eq(rec.blocks.WB_SHIP.lastRow - rec.blocks.WB_SHIP.firstRow + 1, 16, 'принятая строка');
});
t('раскладка: повтор ключа строки → LAYOUT', () => {
  const m = installedMarkers('01_SUPPLY_PLAN');
  const b = G.opsParseLayout_(m, spec01, {}).blocks.HOLD;
  m[b.firstRow - 1] = '@R:HOLD:K1';
  m[b.firstRow] = '@R:HOLD:K1';
  throwsCode(() => G.opsParseLayout_(m, spec01, {}), 'LAYOUT');
});
t('раскладка: «Нет позиций» вместе со строками → LAYOUT', () => {
  const m = installedMarkers('01_SUPPLY_PLAN');
  const b = G.opsParseLayout_(m, spec01, {}).blocks.HOLD;
  m[b.firstRow - 1] = '@E:HOLD';
  throwsCode(() => G.opsParseLayout_(m, spec01, {}), 'LAYOUT');
});
t('раскладка: фиксированный блок с чужим ключом → LAYOUT', () => {
  const m = installedMarkers('01_SUPPLY_PLAN');
  const b = G.opsParseLayout_(m, spec01, {}).blocks.USEND_TOP;
  m[b.firstRow - 1] = '@R:USEND_TOP:OTHER';
  throwsCode(() => G.opsParseLayout_(m, spec01, {}), 'LAYOUT');
});

// ───────────── отрисовка ─────────────
const R = G.opsRender_(clone(P0), {}, statusOk);

/** Разметка, которую оставит запись: строки блоков заменены на строки отрисовки. */
function markersAfterWrite(name, render) {
  const spec = name === 'DATA' ? G.opsDataLayout_() : G.OPS_LAYOUT[name];
  let m = name === 'DATA' ? dataSkeleton() : installedMarkers(name);
  const model = G.opsParseLayout_(m, spec, {});
  const blocks = spec.blocks.filter((b) => b.kind === 'rows').sort((a, b) => model.blocks[b.id].firstRow - model.blocks[a.id].firstRow);
  for (const b of blocks) {
    const blk = model.blocks[b.id];
    const rows = render[b.id].rows;
    const marks = rows.length ? rows.map((x) => G.opsMarker_('R', b.id, x.key)) : [G.opsMarker_('E', b.id, null)];
    m = m.slice(0, blk.firstRow - 1).concat(marks, m.slice(blk.lastRow));
  }
  return { markers: m, model: G.opsParseLayout_(m, spec, {}) };
}
function dataSkeleton() {
  const m = ['@L:' + G.OPS.LAYOUT_VERSION, '@S'];
  Object.keys(G.OPS_VIEWS).forEach((v) => m.push('@B:D_' + v, '@H:D_' + v, '@E:D_' + v, '@S', '@S'));
  m.push('@END');
  return m;
}

t('отрисовка 01: блоки отгрузки, порядок, Usend из контракта', () => {
  const r = R['01_SUPPLY_PLAN'];
  const wb = ship('WB_SHIP');
  eq(r.WB_SHIP.rows.length, wb.length, 'строк WB');
  eq(r.OZON_SHIP.rows.length, ship('OZON_SHIP').length, 'строк Ozon');
  eq(r.HOLD.rows.length, ship('HOLD').length, 'строк HOLD');
  eq(r.WB_SHIP.rows[0].cells[1], wb[0].product_name);
  eq(r.WB_SHIP.rows[0].cells[2], wb[0].rec_final);
  eq(r.WB_SHIP.rows[0].key, wb[0].business_key + '#' + wb[0].rec_fingerprint);
  eq(r.WB_SHIP.rows[0].owner.value, '', 'без решения владельца');
  eq(r.USEND_TOP.rows.map((x) => x.key), G.OPS_USEND_KEYS);
  eq(r.USEND_TOP.rows[2].cells[2], plan0[0].wb_ship_positions);
  eq(r.USEND_TOP.rows[0].cells[3], P0.views.V_OPS_SHEET_PICK.rows[0].t_pick_units);
  eq(Object.keys(r.HOLD.rows[0].cells), ['1', '2', '3', '5'], 'ячейки HOLD по объединениям');
  ok(!/07\.09|ТОЛЬКО ЧТЕНИЕ/.test(JSON.stringify(r)), 'снимочные тексты C1.2 не должны появляться');
  ok(r.STATUS_LINE.value.text.indexOf('evetis_ops на ') === 0, 'строка статуса');
});
t('отрисовка: ячейки только в сгенерированных колонках (кроме колонки владельца)', () => {
  for (const name of Object.keys(G.OPS_LAYOUT)) {
    for (const b of G.OPS_LAYOUT[name].blocks) {
      const r = R[name][b.id];
      if (!r.rows) continue;
      const allowed = new Set();
      b.segments.forEach(([c0, c1]) => { for (let c = c0; c <= c1; c++) allowed.add(String(c)); });
      for (const row of r.rows) {
        for (const c of Object.keys(row.cells)) ok(allowed.has(c), name + '/' + b.id + ': колонка ' + c + ' вне сегментов');
      }
      if (r.total) for (const c of Object.keys(r.total.cells)) ok(allowed.has(c), name + '/' + b.id + ' итог: колонка ' + c);
    }
  }
});
t('отрисовка 02–05 и DATA', () => {
  eq(R['02_SHIPMENTS'].SHIPMENTS.rows.length, P0.views.V_OPS_SHEET_SHIPMENTS.rows.length);
  ok(R['02_SHIPMENTS'].SHIPMENTS.rows[0].cells[15].date, 'дата — значение даты');
  eq(R['03_FF_STOCK'].FF_STOCK.total.cells[11], P0.views.V_OPS_SHEET_FF_STOCK.rows[0].t_total_physical);
  const wbCalc = plan0.filter((x) => x.channel === 'WB');
  eq(R['05_РАСЧЁТ'].CALC_WB.rows.length, wbCalc.length);
  eq(R['05_РАСЧЁТ'].CALC_WB.total.cells[19], wbCalc[0].ch_rec_final);
  const meta = P0.views.V_OPS_SHEET_META.rows[0];
  ok(R['04_SETTINGS'].RULES.rows[3].cells[1].indexOf('WB ' + meta.wb_overstock_limit_days + ' дней (' + meta.wb_target_cover_days + ' + ' +
    meta.wb_safety_stock_days + ' + ' + meta.overstock_tolerance_days + ')') > 0, 'правило 4 из параметров');
  eq(Object.keys(R.DATA).length, Object.keys(G.OPS_VIEWS).length, 'блоков DATA');
  eq(R.DATA.D_V_OPS_SHEET_PLAN.header.length, P0.views.V_OPS_SHEET_PLAN.fields.length);
  eq(Math.min(...Object.keys(R.DATA.D_V_OPS_SHEET_PLAN.rows[0].cells).map(Number)), 2, 'DATA с колонки B');
});
for (const name of ['01_SUPPLY_PLAN', '02_SHIPMENTS', '03_FF_STOCK', '04_SETTINGS', '05_РАСЧЁТ', 'DATA']) {
  t('после записи раскладка остаётся в контракте: ' + name, () => {
    const res = markersAfterWrite(name, R[name]);
    const spec = name === 'DATA' ? G.opsDataLayout_() : G.OPS_LAYOUT[name];
    for (const b of spec.blocks.filter((x) => x.kind === 'rows')) {
      const blk = res.model.blocks[b.id];
      eq(blk.empty ? 0 : blk.lastRow - blk.firstRow + 1, R[name][b.id].rows.length, b.id);
    }
  });
}
t('пустой блок → одна строка «Нет позиций» и контракт цел', () => {
  const p = clone(P0);
  p.views.V_OPS_SHEET_SHIPMENTS.rows = [];
  const r = G.opsRender_(p, {}, statusOk);
  const res = markersAfterWrite('02_SHIPMENTS', r['02_SHIPMENTS']);
  ok(res.model.blocks.SHIPMENTS.empty, 'блок пуст');
  eq(r['02_SHIPMENTS'].SHIPMENTS.total.cells[6], 0, 'итог пустого блока');
});

// ───────────── решение владельца ─────────────
const NOW = '2026-09-14 10:00:00';
const A = ship('WB_SHIP')[0];
function planWith(fn) {
  const p = clone(plan0);
  fn(p.find((r) => r.business_key === A.business_key), p);
  return p;
}
function approvedState() {
  const st = G.opsReconcileState_([], plan0, NOW);
  ok(st.rows.length === ship('WB_SHIP').length + ship('OZON_SHIP').length, 'строки состояния для строк отгрузки');
  const s = st.byKey[A.business_key];
  const res = G.opsApplyOwnerEdit_(s, 777, A.rec_fingerprint, NOW);
  eq(res.events.map((e) => e.event), ['APPROVED']);
  return st.rows;
}
t('Т2: решение сохраняется при той же рекомендации', () => {
  const st = G.opsReconcileState_(approvedState(), plan0, NOW);
  eq(st.byKey[A.business_key].approval_status, 'APPROVED');
  eq(st.events.length, 0);
  eq(G.opsOwnerDisplay_(st.byKey[A.business_key]).value, 777);
});
t('Т11: другое название товара при том же ключе — решение живо', () => {
  const st = G.opsReconcileState_(approvedState(), planWith((r) => { r.product_name = 'Новое имя'; }), NOW);
  eq(st.byKey[A.business_key].approval_status, 'APPROVED');
});
t('Т3: рекомендация изменилась — ПЕРЕСОГЛАСОВАТЬ, прежнее в previous_*', () => {
  const st = G.opsReconcileState_(approvedState(), planWith((r) => { r.rec_fingerprint = 'ffffffffffffffff'; }), NOW);
  const s = st.byKey[A.business_key];
  eq([s.approval_status, s.previous_approved_qty, s.previous_approved_fingerprint, s.approved_fingerprint, s.invalidation_reason],
    ['REAPPROVAL_REQUIRED', 777, A.rec_fingerprint, '', 'FINGERPRINT_CHANGED']);
  eq(G.opsOwnerDisplay_(s).value, 'ПЕРЕСОГЛ. 777');
  eq(G.opsOwnerDisplay_(s).size, 9);
  ok(G.opsOwnerDisplay_(s).note.indexOf('ПЕРЕСОГЛАСОВАТЬ · ранее: 777') === 0, 'полный текст в заметке');
  eq(G.opsOwnerDisplay_(s).align, 'left');
  eq(st.events.map((e) => e.event), ['INVALIDATED']);
});
t('Т13: 100 → 120 → 100 — старое решение не оживает; новое подтверждение работает', () => {
  const s1 = G.opsReconcileState_(approvedState(), planWith((r) => { r.rec_fingerprint = 'ffffffffffffffff'; }), NOW);
  const s2 = G.opsReconcileState_(s1.rows, plan0, NOW);
  eq(s2.byKey[A.business_key].approval_status, 'REAPPROVAL_REQUIRED');
  eq(s2.byKey[A.business_key].current_fingerprint, A.rec_fingerprint);
  const res = G.opsApplyOwnerEdit_(s2.byKey[A.business_key], 100, A.rec_fingerprint, NOW);
  eq(res.events.map((e) => e.event), ['REAPPROVED']);
  const s3 = G.opsReconcileState_(s2.rows, plan0, NOW);
  eq([s3.byKey[A.business_key].approval_status, s3.byKey[A.business_key].approved_qty], ['APPROVED', 100]);
});
t('Т12: строка ушла из отгрузки и вернулась — нужно новое подтверждение', () => {
  const away = G.opsReconcileState_(approvedState(), planWith((r) => { r.sheet_block = 'HOLD'; r.rec_final = 0; }), NOW);
  eq([away.byKey[A.business_key].approval_status, away.byKey[A.business_key].invalidation_reason], ['INACTIVE', 'LEFT_SHIP_BLOCK']);
  const back = G.opsReconcileState_(away.rows, plan0, NOW);
  eq(back.byKey[A.business_key].approval_status, 'REAPPROVAL_REQUIRED');
  eq(back.events.map((e) => e.event), ['RETURNED']);
});
t('ключ исчез из выборки — решение неактивно (KEY_ABSENT)', () => {
  const st = G.opsReconcileState_(approvedState(), plan0.filter((r) => r.business_key !== A.business_key), NOW);
  eq([st.byKey[A.business_key].approval_status, st.byKey[A.business_key].invalidation_reason, st.byKey[A.business_key].current_block],
    ['INACTIVE', 'KEY_ABSENT', 'ABSENT']);
});
t('правка по устаревшей строке — решение сразу требует пересогласования', () => {
  const s = G.opsNewStateRow_(A.business_key);
  s.current_fingerprint = 'aaaaaaaaaaaaaaaa';
  const res = G.opsApplyOwnerEdit_(s, 5, A.rec_fingerprint, NOW);
  eq(res.events.map((e) => e.event), ['APPROVED', 'INVALIDATED']);
  eq(s.approval_status, 'REAPPROVAL_REQUIRED');
});
t('владелец очистил решение — NONE, прежнее значение сохранено', () => {
  const rows = approvedState();
  const s = rows.find((r) => r.business_key === A.business_key);
  const res = G.opsApplyOwnerEdit_(s, '', A.rec_fingerprint, NOW);
  eq([s.approval_status, s.previous_approved_qty, s.invalidation_reason, res.events[0].event], ['NONE', 777, 'CLEARED_BY_OWNER', 'CLEARED']);
});
t('ввод владельца: целое ≥ 0; текст пересогласования игнорируется', () => {
  eq([G.opsParseQty_(620), G.opsParseQty_('1 200'), G.opsParseQty_('15,0'), G.opsParseQty_(0)], [620, 1200, 15, 0]);
  eq([G.opsParseQty_('abc'), G.opsParseQty_(-1), G.opsParseQty_(12.5), G.opsParseQty_('5 шт')], [null, null, null, null]);
  const s = G.opsNewStateRow_('K');
  ok(G.opsApplyOwnerEdit_(s, 'ПЕРЕСОГЛ. · 5', 'x', NOW).ignored, 'игнор');
  ok(G.opsApplyOwnerEdit_(s, 'abc', 'x', NOW).invalid, 'неверный ввод');
  eq(s.approval_status, 'NONE');
});
t('маркер строки отгрузки: ключ и отпечаток разделяются по последнему #', () => {
  eq(G.opsSplitShipRowKey_('SUPPLY_SHIP|WB|EVT-X#0123456789abcdef'),
    { businessKey: 'SUPPLY_SHIP|WB|EVT-X', fingerprint: '0123456789abcdef' });
  const tok = G.opsParseMarker_('@R:WB_SHIP:SUPPLY_SHIP|WB|EVT-X#0123456789abcdef', 13);
  eq([tok.type, tok.id, tok.key], ['R', 'WB_SHIP', 'SUPPLY_SHIP|WB|EVT-X#0123456789abcdef']);
});
t('отрисовка показывает решение владельца по ключу, а не по номеру строки', () => {
  const st = G.opsReconcileState_(approvedState(), plan0, NOW);
  const p = clone(P0);
  const rows = p.views.V_OPS_SHEET_PLAN.rows.filter((r) => r.sheet_block === 'WB_SHIP');
  const maxOrder = Math.max(...rows.map((r) => r.block_order));
  rows.find((r) => r.business_key === A.business_key).block_order = maxOrder + 1;   // строка уехала в конец блока
  const r = G.opsRender_(p, st.byKey, statusOk)['01_SUPPLY_PLAN'].WB_SHIP.rows;
  eq(r[r.length - 1].owner.value, 777, 'решение переехало вместе со строкой');
  eq(r[0].owner.value, '', 'на прежнем месте решения нет');
});

// ───────────── значения для setValues ─────────────
t('даты: пустой часовой пояс книги (импорт .xlsx) → часовой пояс скрипта', () => {
  eq(G.opsSheetValue_({ date: '2026-09-16' }, '').tz, 'Europe/Moscow');
  eq(G.opsSheetValue_({ date: '2026-09-16' }, null).tz, 'Europe/Moscow');
  eq(G.opsSheetValue_({ date: '2026-09-16' }, 'Asia/Tokyo').tz, 'Asia/Tokyo');
});
t('текст, похожий на число, дату, формулу или логическое, остаётся текстом', () => {
  eq([G.opsSheetValue_('2026-09-16', 'x'), G.opsSheetValue_('14.09.2026 06:30', 'x'), G.opsSheetValue_('false', 'x'),
    G.opsSheetValue_('=1+1', 'x'), G.opsSheetValue_('2000065337049', 'x'), G.opsSheetValue_('30', 'x')],
  ["'2026-09-16", "'14.09.2026 06:30", "'false", "'=1+1", "'2000065337049", "'30"]);
  eq([G.opsSheetValue_('Крем Руки', 'x'), G.opsSheetValue_('SET_2026-09-09', 'x'), G.opsSheetValue_(620, 'x'),
    G.opsSheetValue_(null, 'x'), G.opsSheetValue_('—', 'x')], ['Крем Руки', 'SET_2026-09-09', 620, '', '—']);
});

console.log(`\n${passed} PASS / ${failed} FAIL`);
process.exit(failed ? 1 : 0);
