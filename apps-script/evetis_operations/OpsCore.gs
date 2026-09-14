/**
 * EVETIS OPERATIONS · C2 — чистая логика без SpreadsheetApp и BigQuery.
 * Проверяется локально: node tools/ops_c2_logic_test.js
 *
 * Здесь НЕТ расчёта поставок: только типы значений, проверка и сверка выборок контракта, разбор маркеров
 * раскладки, машина состояний решения владельца и раскладка уже посчитанных значений по ячейкам.
 */

function OpsError_(code, message, details) {
  var e = new Error(code + ': ' + message);
  e.opsCode = code;
  e.details = details || null;
  return e;
}

function opsCheck_(ok, code, message) {
  if (!ok) throw OpsError_(code, message);
}

function opsN_(x) {
  return (x === null || x === undefined || x === '') ? 0 : Number(x);
}

function opsFirst_(rows) {
  return rows && rows.length ? rows[0] : {};
}

// ───────────────────────────── типы значений BigQuery ─────────────────────────────

function opsTypeValue_(type, v) {
  if (v === null || v === undefined) return null;
  switch (type) {
    case 'INTEGER': case 'INT64': case 'FLOAT': case 'FLOAT64': case 'NUMERIC': case 'BIGNUMERIC':
      return Number(v);
    case 'BOOLEAN': case 'BOOL':
      return v === true || v === 'true';
    case 'TIMESTAMP':
      return Math.round(Number(v) * 1000);   // миллисекунды эпохи
    default:
      return String(v);                      // STRING, DATE ('YYYY-MM-DD'), DATETIME
  }
}

function opsTypeRows_(fields, rawRows) {
  return (rawRows || []).map(function (row) {
    var rec = {};
    for (var i = 0; i < fields.length; i++) {
      var cell = row.f[i];
      rec[fields[i].name] = opsTypeValue_(fields[i].type, cell ? cell.v : null);
    }
    return rec;
  });
}

function opsFieldTypes_(view) {
  var t = {};
  view.fields.forEach(function (f) { t[f.name] = f.type; });
  return t;
}

// ───────────────────────────── форматирование текста ─────────────────────────────

function opsFmtMsk_(ms, pattern) {
  if (ms === null || ms === undefined || ms === '') return '';
  return Utilities.formatDate(new Date(Number(ms)), OPS.TZ, pattern || 'dd.MM.yyyy HH:mm');
}

function opsFmtDate_(iso) {
  if (!iso) return '';
  var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
  return m ? m[3] + '.' + m[2] + '.' + m[1] : String(iso);
}

function opsFmtNum_(x) {
  if (x === null || x === undefined || x === '') return '';
  var n = Number(x);
  return Math.round(n) === n ? String(n) : String(Math.round(n * 10) / 10);
}

function opsFmtThousands_(x) {
  return String(Math.round(opsN_(x))).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

// ───────────────────────────── проверка выборок (FAIL CLOSED) ─────────────────────────────

function opsAssertUnique_(label, rows, col) {
  var seen = {};
  rows.forEach(function (r) {
    var k = r[col];
    if (k === null || k === undefined || k === '') throw OpsError_('DUP_KEY', label + ': пустой ' + col);
    if (seen[k]) throw OpsError_('DUP_KEY', label + ': повтор ' + col + ' = ' + k);
    seen[k] = true;
  });
}

function opsSum_(rows, pred, col) {
  var s = 0;
  rows.forEach(function (r) { if (pred(r)) s += opsN_(r[col]); });
  return s;
}

/** Проверяет полный набор выборок до первой записи. Возвращает { warnings: [...] }, иначе бросает OpsError_. */
function opsValidatePayload_(payload) {
  var v = payload.views;
  Object.keys(OPS_VIEWS).forEach(function (name) {
    var view = v[name];
    if (!view) throw OpsError_('SCHEMA', 'нет выборки ' + name);
    var types = opsFieldTypes_(view);
    var missing = OPS_VIEWS[name].filter(function (c) { return !(c in types); });
    if (missing.length) throw OpsError_('SCHEMA', name + ': нет колонок ' + missing.join(', '));
    var lim = OPS_ROW_LIMITS[name], n = view.rows.length;
    if (n < lim[0] || n > lim[1]) throw OpsError_('ROWCOUNT', name + ': строк ' + n + ', допустимо ' + lim[0] + '…' + lim[1]);
    opsAssertUnique_(name, view.rows, name === 'V_OPS_SHEET_PLAN' ? 'business_key' : 'row_key');
    if (name !== 'V_OPS_SHEET_PLAN' && name !== 'V_OPS_SHEET_META') opsAssertUnique_(name, view.rows, 'sheet_order');
  });

  var plan = v.V_OPS_SHEET_PLAN.rows;
  var blocks = { WB_SHIP: [], OZON_SHIP: [], HOLD: [] };
  plan.forEach(function (r) {
    opsCheck_(/^[0-9a-f]{16}$/.test(r.rec_fingerprint || ''), 'SCHEMA', 'неверный отпечаток у ' + r.business_key);
    opsCheck_(r.business_key === 'SUPPLY_SHIP|' + r.channel + '|' + r.card_sku, 'SCHEMA', 'ключ не канонический: ' + r.business_key);
    if (r.sheet_block !== null) {
      opsCheck_(r.sheet_block in blocks, 'SCHEMA', 'неизвестный блок ' + r.sheet_block);
      blocks[r.sheet_block].push(r);
    }
    opsCheck_(opsN_(r.rec_final) >= 0 && opsN_(r.rec_physical_units) >= 0, 'RECONCILE', 'отрицательная рекомендация ' + r.business_key);
    opsCheck_(r.ch_arrival_date === r.ch_arrival_date_max && r.ch_next_arrival_date === r.ch_next_arrival_date_max,
      'RECONCILE', 'разные даты прибытия в канале ' + r.channel);
  });
  Object.keys(blocks).forEach(function (b) { opsAssertUnique_('V_OPS_SHEET_PLAN/' + b, blocks[b], 'block_order'); });
  var p0 = plan[0];
  opsCheck_(p0.wb_ship_rows === blocks.WB_SHIP.length && p0.ozon_ship_rows === blocks.OZON_SHIP.length &&
    p0.hold_rows === blocks.HOLD.length, 'RECONCILE', 'счётчики блоков плана не равны числу строк');

  // все выборки — за один день (запросы могут попасть по разные стороны полуночи)
  var days = {};
  Object.keys(OPS_VIEWS).forEach(function (name) {
    v[name].rows.forEach(function (r) { if (r.contract_today) days[r.contract_today] = true; });
  });
  plan.forEach(function (r) { days[r.today] = true; });
  opsCheck_(Object.keys(days).length === 1, 'INCONSISTENT_DAY', Object.keys(days).join(' / '));

  // сверка между представлениями: те же тождества, что в инварианте I24
  var pick = opsFirst_(v.V_OPS_SHEET_PICK.rows), task = opsFirst_(v.V_OPS_SHEET_FF_TASK.rows);
  var bund = opsFirst_(v.V_OPS_SHEET_BUNDLES.rows), bom = opsFirst_(v.V_OPS_SHEET_BOM.rows);
  var ff = opsFirst_(v.V_OPS_SHEET_FF_STOCK.rows);
  var all = function () { return true; };
  var solo = function (ch) { return function (r) { return r.channel === ch && !r.is_bundle; }; };
  var sets = function (ch) { return function (r) { return r.channel === ch && r.is_bundle; }; };
  var pairs = [
    ['R1 физ. единицы плана = спрос снятия', opsSum_(plan, all, 'rec_physical_units'), opsN_(pick.t_total_physical_demand)],
    ['R2 соло WB', opsSum_(plan, solo('WB'), 'rec_final'), opsN_(pick.t_solo_wb_need)],
    ['R2 соло Ozon', opsSum_(plan, solo('OZON'), 'rec_final'), opsN_(pick.t_solo_ozon_need)],
    ['R3 снять под новые', opsN_(task.t_move_pallet_to_shelf_new), opsN_(pick.t_to_pick_from_pallet)],
    ['R3 снять под резерв', opsN_(task.t_move_pallet_to_shelf_reserved), opsN_(pick.t_reserved_on_pallet_to_pick)],
    ['R3 ТЗ = спрос снятия', opsN_(task.t_total_physical_units), opsN_(pick.t_total_physical_demand)],
    ['R4 BOM = сборка', opsN_(bom.t_units_total), opsN_(bund.t_assemble_physical_units)],
    ['R5 наборы WB', opsN_(bund.t_ship_wb), opsSum_(plan, sets('WB'), 'rec_final')],
    ['R5 наборы Ozon', opsN_(bund.t_ship_ozon), opsSum_(plan, sets('OZON'), 'rec_final')],
    ['R6 резервы ФФ', opsN_(ff.t_reserved_wb) + opsN_(ff.t_reserved_ozon), opsN_(task.t_reserved_units_already)]
  ];
  pairs.forEach(function (p) {
    opsCheck_(p[1] === p[2], 'RECONCILE', p[0] + ': ' + p[1] + ' ≠ ' + p[2]);
  });

  var meta = v.V_OPS_SHEET_META.rows[0], warnings = [];
  if (meta.wb_stock_stale) warnings.push('остатки WB');
  if (meta.ozon_stock_stale) warnings.push('остатки Ozon');
  if (meta.ozon_orders_stale) warnings.push('поставки Ozon');
  if (meta.plan_missing) warnings.push('нет активного плана продаж');
  return { warnings: warnings, rowsReceived: Object.keys(OPS_VIEWS).reduce(function (s, n) { return s + v[n].rows.length; }, 0) };
}

/** Строка для хеша снимка: без колонок, которые меняются при каждом запросе. */
function opsHashInput_(payload) {
  var out = {};
  Object.keys(OPS_VIEWS).forEach(function (name) {
    var view = payload.views[name];
    var cols = view.fields.map(function (f) { return f.name; }).filter(function (c) { return OPS_VOLATILE_COLUMNS.indexOf(c) < 0; });
    out[name] = view.rows.map(function (r) { return cols.map(function (c) { return r[c]; }); });
  });
  return JSON.stringify(out);
}

// ───────────────────────────── контракт раскладки: разбор маркеров ─────────────────────────────

function opsParseMarker_(text, row) {
  var m = String(text === null || text === undefined ? '' : text).trim();
  if (m === '') return { row: row, type: '', id: '', key: null };
  if (m === '@END') return { row: row, type: 'END', id: '', key: null };
  var mm = /^@(L|S|V|B|H|R|E|T)(?::([^:]*))?(?::([\s\S]*))?$/.exec(m);
  if (!mm) throw OpsError_('LAYOUT', 'непонятный маркер «' + m + '» в строке ' + row);
  return { row: row, type: mm[1], id: mm[2] || '', key: mm[3] === undefined ? null : mm[3] };
}

function opsMarker_(type, id, key) {
  return '@' + type + (id ? ':' + id : '') + (key !== null && key !== undefined ? ':' + key : '');
}

/**
 * Разбирает маркеры системной колонки листа по спецификации. markers[0] — строка 1.
 * opts.recovery: после прерванной записи строки без маркера сразу за строками блока признаются строками этого блока.
 */
function opsParseLayout_(markers, spec, opts) {
  opts = opts || {};
  var tokens = [];
  for (var i = 0; i < markers.length; i++) {
    var t = opsParseMarker_(markers[i], i + 1);
    if (t.type === 'END') break;
    tokens.push(t);
  }
  if (i >= markers.length) throw OpsError_('LAYOUT', 'нет маркера @END');
  var endRow = i + 1;
  opsCheck_(tokens.length > 0 && tokens[0].type === 'L', 'LAYOUT', 'нет маркера раскладки в строке 1');
  opsCheck_(tokens[0].id === OPS.LAYOUT_VERSION, 'LAYOUT', 'версия раскладки ' + tokens[0].id + ' вместо ' + OPS.LAYOUT_VERSION);

  var pos = 1, model = {};
  function skipStatic() { while (pos < tokens.length && tokens[pos].type === 'S') pos++; }
  spec.blocks.forEach(function (b) {
    skipStatic();
    var blk = { id: b.id, kind: b.kind };
    function expect(type) {
      var t = tokens[pos];
      if (!t || t.type !== type || t.id !== b.id) {
        throw OpsError_('LAYOUT', 'блок ' + b.id + ': ожидался маркер @' + type + ' в строке ' + (t ? t.row : endRow) +
          (t ? ', найден «' + opsMarker_(t.type, t.id, t.key) + '»' : ''));
      }
      pos++;
      return t;
    }
    if (b.kind === 'value') {
      blk.valueRow = expect('V').row;
    } else {
      if (b.band) blk.bandRow = expect('B').row;
      if (b.header) blk.headerRow = expect('H').row;
      var rows = [];
      while (pos < tokens.length) {
        var t = tokens[pos];
        if ((t.type === 'R' || t.type === 'E') && t.id === b.id) { rows.push(t); pos++; continue; }
        if (t.type === '' && opts.recovery && b.kind === 'rows' && rows.length) {
          rows.push({ row: t.row, type: 'R', id: b.id, key: '', adopted: true }); pos++; continue;
        }
        break;
      }
      opsCheck_(rows.length > 0, 'LAYOUT', 'блок ' + b.id + ': нет строк данных');
      var empties = rows.filter(function (r) { return r.type === 'E'; }).length;
      opsCheck_(empties === 0 || rows.length === 1 || opts.recovery, 'LAYOUT', 'блок ' + b.id + ': «Нет позиций» вместе со строками');
      if (b.kind === 'fixed') {
        opsCheck_(rows.length === b.keys.length, 'LAYOUT', 'блок ' + b.id + ': строк ' + rows.length + ' вместо ' + b.keys.length);
        rows.forEach(function (r, k) {
          opsCheck_(r.type === 'R' && r.key === b.keys[k], 'LAYOUT', 'блок ' + b.id + ': строка ' + r.row + ' не ' + b.keys[k]);
        });
      } else {
        var seen = {};
        rows.forEach(function (r) {
          if (!r.key) return;
          opsCheck_(!seen[r.key], 'LAYOUT', 'блок ' + b.id + ': повтор ключа строки ' + r.key);
          seen[r.key] = true;
        });
      }
      blk.rows = rows;
      blk.firstRow = rows[0].row;
      blk.lastRow = rows[rows.length - 1].row;
      blk.empty = rows.length === 1 && rows[0].type === 'E';
      if (b.total) blk.totalRow = expect('T').row;
    }
    model[b.id] = blk;
  });
  skipStatic();
  if (pos !== tokens.length) {
    var extra = tokens[pos];
    throw OpsError_('LAYOUT', 'строка ' + extra.row + ' вне контракта раскладки («' + opsMarker_(extra.type, extra.id, extra.key) + '»)');
  }
  return { blocks: model, endRow: endRow };
}

// ───────────────────────────── решение владельца: машина состояний ─────────────────────────────

function opsIsShipBlock_(block) {
  return block === 'WB_SHIP' || block === 'OZON_SHIP';
}

function opsNewStateRow_(key) {
  var s = {};
  OPS_STATE_COLS.forEach(function (c) { s[c] = ''; });
  s.business_key = key;
  s.approval_status = 'NONE';
  return s;
}

function opsEvent_(at, key, event, qty, fp, reason, source) {
  return { event_at: at, business_key: key, event: event, qty: qty === undefined ? '' : qty, fingerprint: fp || '',
    reason: reason || '', source: source };
}

function opsInvalidate_(s, reason, now) {
  s.previous_approved_qty = s.approved_qty;
  s.previous_approved_fingerprint = s.approved_fingerprint;
  s.previous_approved_at = s.approved_at;
  s.approved_qty = '';
  s.approved_fingerprint = '';
  s.approved_at = '';
  s.invalidated_at = now;
  s.invalidation_reason = reason;
}

/**
 * Сверяет решения владельца с новой выборкой плана. Решение живёт, пока строка в блоке отгрузки
 * и отпечаток рекомендации равен одобренному. Иначе — недействительно навсегда: прежнее значение
 * уходит в previous_*, строка требует нового подтверждения. Старое одобрение само не оживает (T12, T13).
 */
function opsReconcileState_(stateRows, planRows, now) {
  var order = [], byKey = {}, plan = {}, events = [];
  stateRows.forEach(function (r) {
    var c = {};
    OPS_STATE_COLS.forEach(function (k) { c[k] = r[k] === undefined || r[k] === null ? '' : r[k]; });
    byKey[c.business_key] = c;
    order.push(c.business_key);
  });
  planRows.forEach(function (p) {
    plan[p.business_key] = p;
    if (opsIsShipBlock_(p.sheet_block) && !byKey[p.business_key]) {
      byKey[p.business_key] = opsNewStateRow_(p.business_key);
      order.push(p.business_key);
    }
  });
  order.forEach(function (k) {
    var s = byKey[k], p = plan[k] || null, inShip = !!p && opsIsShipBlock_(p.sheet_block);
    s.current_fingerprint = p ? p.rec_fingerprint : '';
    s.current_block = p ? (p.sheet_block || 'NONE') : 'ABSENT';
    s.current_rec_final = p ? opsN_(p.rec_final) : '';
    if (p) s.last_seen_at = now;
    switch (s.approval_status) {
      case 'APPROVED':
        if (inShip && s.approved_fingerprint === p.rec_fingerprint) break;
        var reason = inShip ? 'FINGERPRINT_CHANGED' : (p ? 'LEFT_SHIP_BLOCK' : 'KEY_ABSENT');
        opsInvalidate_(s, reason, now);
        s.approval_status = inShip ? 'REAPPROVAL_REQUIRED' : 'INACTIVE';
        events.push(opsEvent_(now, k, 'INVALIDATED', s.previous_approved_qty, s.previous_approved_fingerprint, reason, 'REFRESH'));
        break;
      case 'REAPPROVAL_REQUIRED':
        if (!inShip) {
          s.approval_status = 'INACTIVE';
          events.push(opsEvent_(now, k, 'INACTIVE', '', s.current_fingerprint, p ? 'LEFT_SHIP_BLOCK' : 'KEY_ABSENT', 'REFRESH'));
        }
        break;
      case 'INACTIVE':
        if (inShip) {
          s.approval_status = 'REAPPROVAL_REQUIRED';
          events.push(opsEvent_(now, k, 'RETURNED', '', p.rec_fingerprint, 'RETURNED_TO_SHIP_BLOCK', 'REFRESH'));
        }
        break;
      default:
        s.approval_status = 'NONE';
    }
  });
  return { rows: order.map(function (k) { return byKey[k]; }), byKey: byKey, events: events };
}

/** Количество из ячейки владельца: целое ≥ 0 или null. */
function opsParseQty_(raw) {
  if (typeof raw === 'number') return (isFinite(raw) && raw >= 0 && Math.floor(raw) === raw) ? raw : null;
  var t = String(raw).replace(/[\s ]/g, '').replace(',', '.');
  if (!/^\d+(\.0+)?$/.test(t)) return null;
  return Number(t);
}

/**
 * Правка «Одобрено владельцем». shownFingerprint — отпечаток из маркера строки, которую владелец видел.
 * Возвращает { state, events, ignored?, invalid? }. state мутируется.
 */
function opsApplyOwnerEdit_(s, raw, shownFingerprint, now) {
  var events = [], key = s.business_key;
  var text = raw === null || raw === undefined ? '' : String(raw).trim();
  if (text.indexOf('ПЕРЕСОГЛ') === 0) return { state: s, events: events, ignored: true };
  if (text === '') {
    if (s.approval_status === 'APPROVED') {
      opsInvalidate_(s, 'CLEARED_BY_OWNER', now);
      s.approval_status = 'NONE';
      events.push(opsEvent_(now, key, 'CLEARED', s.previous_approved_qty, s.previous_approved_fingerprint, 'CLEARED_BY_OWNER', 'ONEDIT'));
    } else if (s.approval_status === 'REAPPROVAL_REQUIRED' || s.approval_status === 'INACTIVE') {
      s.approval_status = 'NONE';
      events.push(opsEvent_(now, key, 'CLEARED', '', shownFingerprint, 'REAPPROVAL_DISMISSED_BY_OWNER', 'ONEDIT'));
    } else {
      return { state: s, events: events, ignored: true };
    }
    return { state: s, events: events };
  }
  var n = opsParseQty_(raw);
  if (n === null) return { state: s, events: events, invalid: true };
  var again = s.approval_status === 'REAPPROVAL_REQUIRED' || s.approval_status === 'INACTIVE';
  s.approved_qty = n;
  s.approved_fingerprint = shownFingerprint;
  s.approved_at = now;
  s.approval_status = 'APPROVED';
  events.push(opsEvent_(now, key, again ? 'REAPPROVED' : 'APPROVED', n, shownFingerprint, '', 'ONEDIT'));
  if (s.current_fingerprint && shownFingerprint !== s.current_fingerprint) {
    // между показом строки и записью решения обновление уже сменило рекомендацию
    opsInvalidate_(s, 'FINGERPRINT_CHANGED', now);
    s.approval_status = 'REAPPROVAL_REQUIRED';
    events.push(opsEvent_(now, key, 'INVALIDATED', n, shownFingerprint, 'FINGERPRINT_CHANGED', 'ONEDIT'));
  }
  return { state: s, events: events };
}

/** Что показать в «Одобрено владельцем». */
function opsOwnerDisplay_(s) {
  if (s && s.approval_status === 'APPROVED') {
    return { value: Number(s.approved_qty), fg: OPS_COLOR.DARK, bold: false, align: 'right', size: 10, note: '' };
  }
  if (s && s.approval_status === 'REAPPROVAL_REQUIRED') {
    return { value: OPS_TEXT.REAPPROVAL_SHORT + s.previous_approved_qty, fg: OPS_COLOR.RED, bold: true, align: 'left', size: 9,
      note: OPS_TEXT.REAPPROVAL_NOTE_PREFIX + s.previous_approved_qty +
        (s.previous_approved_at ? ' (одобрено ' + s.previous_approved_at + ')' : '') +
        '. Рекомендация изменилась или строка уходила из отгрузки после вашего решения. ' +
        'Введите количество заново — это станет решением по текущей рекомендации.' };
  }
  return { value: '', fg: OPS_COLOR.DARK, bold: false, align: 'right', size: 10, note: '' };
}

function opsShipRowKey_(p) {
  return p.business_key + '#' + p.rec_fingerprint;
}

function opsSplitShipRowKey_(key) {
  var i = String(key || '').lastIndexOf('#');
  return i < 0 ? { businessKey: '', fingerprint: '' } : { businessKey: key.slice(0, i), fingerprint: key.slice(i + 1) };
}

// ───────────────────────────── отрисовка: значения по ячейкам ─────────────────────────────

/** Значение ячейки: дата-поле → {date}, остальное как есть. */
function opsCellValue_(type, value) {
  if (value === null || value === undefined) return null;
  if (type === 'DATE') return { date: value };
  return value;
}

function opsRowsFor_(rows, types, cols, keyFn, extra) {
  return rows.map(function (r) {
    var cells = {};
    Object.keys(cols).forEach(function (c) { cells[c] = opsCellValue_(types[cols[c]], r[cols[c]]); });
    var out = { key: keyFn(r), cells: cells };
    if (extra) extra(r, out);
    return out;
  });
}

function opsTotalFor_(first, cols) {
  var cells = { 1: 'ИТОГО' };
  Object.keys(cols).forEach(function (c) { cells[c] = opsN_(first[cols[c]]); });
  return { cells: cells };
}

function opsSortBy_(rows, col) {
  return rows.slice().sort(function (a, b) { return opsN_(a[col]) - opsN_(b[col]); });
}

function opsBoldKeyStyle_(on) {
  return { bg: on ? OPS_COLOR.BLUE_BG : OPS_COLOR.WHITE, fg: on ? OPS_COLOR.BLUE : OPS_COLOR.MUTED, bold: !!on };
}

function opsStatusStyle_(code) {
  var st = OPS_ST[code || ''] || [OPS_COLOR.WHITE, OPS_COLOR.DARK, false];
  return { bg: st[0], fg: st[1], bold: st[2] };
}

function opsCoverText_(meta) {
  return meta.wb_target_cover_days === meta.ozon_target_cover_days ? String(meta.wb_target_cover_days)
    : 'WB ' + meta.wb_target_cover_days + ' / Ozon ' + meta.ozon_target_cover_days;
}

function opsArrivals_(plan) {
  var a = { WB: '', OZON: '' }, n = { WB: '', OZON: '' };
  plan.forEach(function (r) {
    if (r.channel in a && !a[r.channel]) { a[r.channel] = r.ch_arrival_date || ''; n[r.channel] = r.ch_next_arrival_date || ''; }
  });
  return { arrival: a, next: n };
}

/** Хвост строки статуса (всё после времени и статуса) — берётся из снимка, который сейчас на экране. */
function opsStatusLineTail_(payload) {
  var meta = payload.views.V_OPS_SHEET_META.rows[0], arr = opsArrivals_(payload.views.V_OPS_SHEET_PLAN.rows);
  return 'отгрузка с ФФ ' + meta.wb_ship_date + ' → прибытие WB ' + arr.arrival.WB + ' / Ozon ' + arr.arrival.OZON +
    ' · следующая поставка ' + meta.wb_next_ship_date + ' → ' + arr.next.WB + ' / ' + arr.next.OZON +
    ' · покрытие ' + opsCoverText_(meta) + ' дн считается ПОСЛЕ прибытия · подробный расчёт на листе 05_РАСЧЁТ.';
}

/**
 * status: { status: OK|WARNING|ERROR, message, dataAsOfMs, lastSuccessMs, lastCheckMs, tail, nextAuto, freshness }
 * Возвращает { text, fg } для строки 2 листа 01.
 */
function opsStatusLine_(status) {
  var data = status.dataAsOfMs ? opsFmtMsk_(status.dataAsOfMs) : '—';
  if (status.status === 'ERROR') {
    return { text: 'ОШИБКА ОБНОВЛЕНИЯ ' + opsFmtMsk_(status.lastCheckMs) + ': ' + status.message + ' · на экране данные на ' +
      data + ' МСК · ' + status.tail, fg: OPS_COLOR.RED };
  }
  if (status.status === 'WARNING') {
    return { text: 'evetis_ops на ' + data + ' МСК · ВНИМАНИЕ: устарели ' + status.message + ' · ' + status.tail, fg: OPS_COLOR.AMBER };
  }
  return { text: 'evetis_ops на ' + data + ' МСК · проверено ' + opsFmtMsk_(status.lastCheckMs) + ' · ' + status.tail, fg: OPS_COLOR.GREY };
}

function opsFreshnessText_(meta) {
  var mark = function (stale) { return stale ? ' (устарело)' : ''; };
  return 'остатки WB ' + opsFmtDate_(meta.wb_stock_date) + mark(meta.wb_stock_stale) +
    ' · остатки Ozon ' + opsFmtDate_(meta.ozon_stock_date) + mark(meta.ozon_stock_stale) +
    ' · поставки Ozon ' + opsFmtMsk_(meta.ozon_orders_at) + mark(meta.ozon_orders_stale) +
    ' · план ' + (meta.plan_version || 'нет');
}

/** Блок «ОБНОВЛЕНИЕ ДАННЫХ» на 04_SETTINGS. */
function opsRenderStatusRows_(status) {
  var L = OPS_TEXT.STATUS_LABELS, word;
  if (status.status === 'ERROR') word = 'ОШИБКА: ' + status.message + ' · на экране данные на ' + opsFmtMsk_(status.dataAsOfMs) + ' МСК';
  else if (status.status === 'WARNING') word = 'ВНИМАНИЕ: устарели ' + status.message;
  else word = 'OK';
  var st = status.status === 'ERROR' ? { bg: OPS_COLOR.RED_BG, fg: OPS_COLOR.RED, bold: true }
    : status.status === 'WARNING' ? { bg: OPS_COLOR.AMBER_BG, fg: OPS_COLOR.AMBER, bold: true }
      : { bg: OPS_COLOR.GREEN_BG, fg: OPS_COLOR.GREEN, bold: true };
  var vals = {
    LAST_SUCCESS: status.lastSuccessMs ? opsFmtMsk_(status.lastSuccessMs) + ' МСК' : 'ещё не было',
    LAST_CHECK: opsFmtMsk_(status.lastCheckMs) + ' МСК · ' + (status.checkResult || ''),
    STATUS: word,
    FRESHNESS: status.freshness || '',
    NEXT_AUTO: status.nextAuto || ''
  };
  return OPS_STATUS_KEYS.map(function (k) {
    return { key: k, cells: { 1: L[k], 3: vals[k] },
      styles: { 3: k === 'STATUS' ? st : { bg: OPS_COLOR.WHITE, fg: OPS_COLOR.DARK, bold: true } } };
  });
}

function opsUsendRows_(v) {
  var plan = v.V_OPS_SHEET_PLAN.rows, p0 = plan[0], arr = opsArrivals_(plan).arrival;
  var pick = opsFirst_(v.V_OPS_SHEET_PICK.rows), bund = opsFirst_(v.V_OPS_SHEET_BUNDLES.rows);
  var task = opsFirst_(v.V_OPS_SHEET_FF_TASK.rows), L = OPS_TEXT.USEND_LABELS;
  var data = {
    PICK: ['', opsN_(pick.t_pick_units), opsN_(pick.t_to_pick_from_pallet) + ' под новые отгрузки + ' +
      opsN_(pick.t_reserved_on_pallet_to_pick) + ' под уже зарезервированные поставки'],
    BUILD: [opsN_(bund.t_to_assemble_now), opsN_(bund.t_assemble_physical_units),
      'позиции = наборы, физические единицы = флаконы в них; в т.ч. ' + opsN_(bund.t_to_assemble_reserved) + ' наборов под резерв отгрузок'],
    SHIP_WB: [p0.wb_ship_positions, p0.wb_ship_physical, p0.wb_ship_rows + ' позиций · прибытие ' + arr.WB],
    SHIP_OZON: [p0.ozon_ship_positions, p0.ozon_ship_physical, p0.ozon_ship_rows + ' позиций · прибытие ' + arr.OZON],
    RESERVED: ['', opsN_(task.t_reserved_units_already), 'из них ' + opsN_(task.t_reserved_units_on_pallet) +
      ' лежит на паллетах — их тоже нужно снять'],
    FREE_AFTER: ['', opsN_(task.t_free_ff_after_operation), 'после снятия, сборки и отгрузки']
  };
  return OPS_USEND_KEYS.map(function (k) {
    return { key: k, cells: { 1: L[k], 2: data[k][0], 3: data[k][1], 4: data[k][2] } };
  });
}

function opsRulesRows_(meta) {
  var tol = meta.overstock_tolerance_days;
  var lines = [
    '1. Целевое покрытие ' + opsCoverText_(meta) + ' дней — ПОСЛЕ прибытия. Сборка ФФ (' + meta.ff_prep_working_days +
      ' рабочих дня) и срок канала учитываются отдельно и видны в листе.',
    '2. Округление вверх до операционной кратности. Потребность меньше кратности → ЖДАТЬ, если страховой запас доживёт ' +
      'до следующей возможной поставки (цикл ' + meta.cadence_days + ' дней); иначе отгружается один минимум.',
    '3. Ozon при низком спросе: уменьшенная кратность (по SKU — колонка «Низкий спрос» в таблице логистики ниже) — ' +
      'только если потребность меньше обычной кратности и не меньше уменьшенной.',
    '4. ПРОВЕРИТЬ, если после округления покрытие выше: WB ' + meta.wb_overstock_limit_days + ' дней (' + meta.wb_target_cover_days +
      ' + ' + meta.wb_safety_stock_days + ' + ' + tol + '), Ozon ' + meta.ozon_overstock_limit_days + ' дней (' +
      meta.ozon_target_cover_days + ' + ' + meta.ozon_safety_stock_days + ' + ' + tol + '); или если запас не распродаётся ' +
      'минимум за ' + meta.expiry_margin_days + ' дней до срока годности. Количество при этом не уменьшается — решает владелец.',
    '5. Позиция запаса: остаток площадки + подтверждённое входящее + приёмка. Каждая единица считается один раз.',
    '6. Ozon: ON_OZON, IN_ACCEPTANCE и RESERVED_OZON_ON_FF — раздельно. Приёмка никогда не прибавляется к остатку площадки. ' +
      'Если неизвестное принятое количество может изменить решение «отгрузить ↔ ждать» — статус ПРОВЕРИТЬ.',
    '7. ФФ — один общий пул: WB и Ozon не могут претендовать на одну и ту же единицу.'
  ];
  return OPS_RULE_KEYS.map(function (k, i) { return { key: k, cells: { 1: lines[i] } }; });
}

function opsFreshRows_(meta) {
  var L = OPS_TEXT.FRESH_LABELS, mark = function (stale) { return stale ? ' — устарело' : ''; };
  var vals = {
    WB_STOCK: opsFmtDate_(meta.wb_stock_date) + mark(meta.wb_stock_stale),
    OZON_STOCK: opsFmtDate_(meta.ozon_stock_date) + mark(meta.ozon_stock_stale),
    OZON_ORDERS: opsFmtMsk_(meta.ozon_orders_at) + mark(meta.ozon_orders_stale),
    PLAN_VERSION: meta.plan_version || 'нет активного плана',
    LEDGER_LAST: opsFmtMsk_(meta.ledger_last_at),
    WRITEBACK: meta.writeback_enabled
  };
  return OPS_FRESH_KEYS.map(function (k) { return { key: k, cells: { 1: L[k], 3: vals[k] } }; });
}

/**
 * Полная отрисовка всех листов из проверенного снимка и состояния решений.
 * Возвращает { '<лист>': { '<блок>': { band?, value?, rows?, total? } } }.
 */
function opsRender_(payload, stateByKey, status) {
  var v = payload.views, C = OPS_COLS;
  var plan = v.V_OPS_SHEET_PLAN.rows, meta = v.V_OPS_SHEET_META.rows[0], p0 = plan[0];
  var tPlan = opsFieldTypes_(v.V_OPS_SHEET_PLAN);
  var byBlock = function (b) { return opsSortBy_(plan.filter(function (r) { return r.sheet_block === b; }), 'block_order'); };
  var byChannel = function (ch) { return opsSortBy_(plan.filter(function (r) { return r.channel === ch; }), 'calc_order'); };
  var rowsOf = function (name, pred) {
    var rows = v[name].rows.filter(pred || function () { return true; });
    return opsSortBy_(rows, 'sheet_order');
  };
  var keyRow = function (r) { return r.row_key; };
  var keyBiz = function (r) { return r.business_key; };
  var line = opsStatusLine_(status);
  var shipExtra = function (r, out) {
    out.key = opsShipRowKey_(r);
    out.owner = opsOwnerDisplay_(stateByKey[r.business_key]);
    out.styles = { 2: opsBoldKeyStyle_(opsN_(r.rec_final) > 0), 7: opsStatusStyle_(r.risk_label) };
  };
  var calcExtra = function (r, out) {
    out.rowFg = r.status_code === 'NO PLAN' ? OPS_COLOR.MUTED : OPS_COLOR.DARK;
    out.styles = { 19: opsBoldKeyStyle_(opsN_(r.rec_final) > 0), 23: opsStatusStyle_(r.status_code) };
  };
  var arr = opsArrivals_(plan).arrival;
  var calcBand = function (ch, name, prefix) {
    var rows = byChannel(ch), f = opsFirst_(rows);
    return name + '   ·   отгрузка ' + meta[prefix + 'ship_date'] + ' → прибытие ' + arr[ch] +
      '   ·   покрытие ' + meta[prefix + 'target_cover_days'] + ' дн + страховой ' + meta[prefix + 'safety_stock_days'] +
      ' дн   ·   срок канала ' + meta[prefix + 'lead_time_days'] + ' дн   ·   к отгрузке ' + opsN_(f.ch_rec_final) +
      ' позиций / ' + opsN_(f.ch_rec_physical_units) + ' физ. ед.';
  };
  var ship = v.V_OPS_SHEET_SHIPMENTS, ship0 = opsFirst_(ship.rows);
  var ff0 = opsFirst_(v.V_OPS_SHEET_FF_STOCK.rows);
  var R = {};

  R[OPS_SHEET.PLAN] = {
    STATUS_LINE: { value: line },
    USEND_TOP: { rows: opsUsendRows_(v) },
    WB_SHIP: {
      band: { text: 'ОТГРУЗИТЬ WB СЕЙЧАС   ·   ' + p0.wb_ship_positions + ' позиций / ' + p0.wb_ship_physical + ' физ. единиц',
        note: 'Лимит одной поставки WB (' + meta.wb_shipping_method + '): ' + opsFmtNum_(meta.wb_max_lot_weight_kg) + ' кг / ' +
          opsFmtNum_(meta.wb_max_lot_units) + ' ед. / ' + opsFmtNum_(meta.wb_max_lot_volume_l) + ' л.' },
      rows: opsRowsFor_(byBlock('WB_SHIP'), tPlan, C.SHIP_TOP, keyBiz, shipExtra)
    },
    OZON_SHIP: {
      band: { text: 'ОТГРУЗИТЬ OZON СЕЙЧАС   ·   ' + p0.ozon_ship_positions + ' позиций / ' + p0.ozon_ship_physical + ' физ. единиц' },
      rows: opsRowsFor_(byBlock('OZON_SHIP'), tPlan, C.SHIP_TOP, keyBiz, shipExtra)
    },
    HOLD: {
      band: { text: 'ПРОВЕРИТЬ / НЕ ОТГРУЖАТЬ   ·   ' + p0.hold_rows + ' строк' },
      rows: opsRowsFor_(byBlock('HOLD'), tPlan, C.HOLD, keyBiz)
    },
    USEND_TASK: { rows: opsUsendRows_(v) },
    TASK_MOVE: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_FF_TASK', function (r) { return r.is_move_row; }),
        opsFieldTypes_(v.V_OPS_SHEET_FF_TASK), C.TASK_MOVE, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_FF_TASK.rows), C.TASK_MOVE_T)
    },
    TASK_BUILD: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_BUNDLES', function (r) { return r.is_build_row; }),
        opsFieldTypes_(v.V_OPS_SHEET_BUNDLES), C.TASK_BUILD, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_BUNDLES.rows), C.TASK_BUILD_T)
    },
    TASK_COMP: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_FF_TASK', function (r) { return r.is_comp_row; }),
        opsFieldTypes_(v.V_OPS_SHEET_FF_TASK), C.TASK_COMP, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_FF_TASK.rows), C.TASK_COMP_T)
    }
  };

  R[OPS_SHEET.SHIP] = {
    SHIP_SUBTITLE: { value: { text: 'Данные на ' + opsFmtMsk_(status.dataAsOfMs) + ' (МСК). Один заказ — один этап: резерв на ФФ, ' +
      'приёмка, в пути или принято. Единица не может быть одновременно в двух этапах.' } },
    SHIPMENTS: {
      band: { text: 'ПОСТАВКИ   ·   OZON ' + opsN_(ship0.ozon_rows) + '   ·   WB ' + opsN_(ship0.wb_rows) },
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_SHIPMENTS'), opsFieldTypes_(ship), C.SHIPMENTS, keyRow),
      total: opsTotalFor_(ship0, C.SHIPMENTS_T)
    },
    SHIP_WB_LINE: { value: { text: opsN_(ship0.wb_rows) === 0
      ? 'Поставок WB в журнале evetis_ops нет; черновики в кабинете поставками не считаются.'
      : 'Поставок WB в журнале evetis_ops: ' + opsN_(ship0.wb_rows) + '.' } }
  };
  R[OPS_SHEET.SHIP].SHIPMENTS.total.cells[1] = 'ИТОГО';

  R[OPS_SHEET.FF] = {
    FF_SUBTITLE: { value: { text: 'Источник правды — журнал evetis_ops на ' + opsFmtMsk_(status.dataAsOfMs) +
      ' (МСК). Старое число Control Tower как физическую правду не использовать.' } },
    FF_TOTAL_LINE: { value: { text: 'ИТОГО НА ФФ: ' + opsFmtThousands_(ff0.t_total_physical) + ' единиц' } },
    FF_STOCK: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_FF_STOCK'), opsFieldTypes_(v.V_OPS_SHEET_FF_STOCK), C.FF_STOCK, keyRow),
      total: opsTotalFor_(ff0, C.FF_STOCK_T)
    }
  };

  R[OPS_SHEET.SETTINGS] = {
    REFRESH_STATUS: { rows: opsRenderStatusRows_(status) },
    RULES: { rows: opsRulesRows_(meta) },
    CHANNELS: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_CHANNELS'), opsFieldTypes_(v.V_OPS_SHEET_CHANNELS), C.CHANNELS, keyRow) },
    CONFIG: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_CONFIG'), opsFieldTypes_(v.V_OPS_SHEET_CONFIG), C.CONFIG, keyRow) },
    LOGISTICS: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_LOGISTICS'), opsFieldTypes_(v.V_OPS_SHEET_LOGISTICS), C.LOGISTICS, keyRow) },
    FRESHNESS: { rows: opsFreshRows_(meta) }
  };

  var calcTotal = function (ch) { return opsTotalFor_(opsFirst_(byChannel(ch)), C.CALC_PLAN_T); };
  R[OPS_SHEET.CALC] = {
    CALC_WB: { band: { text: calcBand('WB', 'WILDBERRIES', 'wb_') },
      rows: opsRowsFor_(byChannel('WB'), tPlan, C.CALC_PLAN, keyBiz, calcExtra), total: calcTotal('WB') },
    CALC_OZON: { band: { text: calcBand('OZON', 'OZON', 'ozon_') },
      rows: opsRowsFor_(byChannel('OZON'), tPlan, C.CALC_PLAN, keyBiz, calcExtra), total: calcTotal('OZON') },
    CALC_BUNDLES: {
      rows: opsRowsFor_(rowsOf('V_OPS_SHEET_BUNDLES'), opsFieldTypes_(v.V_OPS_SHEET_BUNDLES), C.CALC_BUNDLES, keyRow,
        function (r, out) { out.styles = { 13: opsBoldKeyStyle_(opsN_(r.to_assemble_now) > 0) }; }),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_BUNDLES.rows), C.CALC_BUNDLES_T)
    },
    CALC_BOM: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_BOM'), opsFieldTypes_(v.V_OPS_SHEET_BOM), C.CALC_BOM, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_BOM.rows), C.CALC_BOM_T) },
    CALC_PICK: { rows: opsRowsFor_(rowsOf('V_OPS_SHEET_PICK'), opsFieldTypes_(v.V_OPS_SHEET_PICK), C.CALC_PICK, keyRow),
      total: opsTotalFor_(opsFirst_(v.V_OPS_SHEET_PICK.rows), C.CALC_PICK_T) }
  };

  R[OPS_SHEET.DATA] = opsRenderData_(payload);
  return R;
}

/** DATA: сырые строки контракта, как пришли из BigQuery (TIMESTAMP — МСК, BOOL — True/False). */
function opsRenderData_(payload) {
  var out = {};
  Object.keys(OPS_VIEWS).forEach(function (name) {
    var view = payload.views[name], fields = view.fields;
    var keyCol = name === 'V_OPS_SHEET_PLAN' ? 'business_key' : 'row_key';
    var rows = view.rows.slice().sort(function (a, b) { return String(a[keyCol]) < String(b[keyCol]) ? -1 : 1; });
    out['D_' + name] = {
      band: { text: name },
      header: fields.map(function (f) { return f.name; }),
      rows: rows.map(function (r) {
        var cells = {};
        fields.forEach(function (f, i) {
          var x = r[f.name];
          if (x !== null && f.type === 'TIMESTAMP') x = opsFmtMsk_(x, 'yyyy-MM-dd HH:mm');
          else if (x !== null && (f.type === 'BOOLEAN' || f.type === 'BOOL')) x = x ? 'True' : 'False';
          cells[i + 2] = x;   // колонка A — служебная, данные с B
        });
        return { key: String(r[keyCol]), cells: cells };
      })
    };
  });
  return out;
}
