/**
 * EVETIS OWNER CONTROL TOWER — Phase 1.1
 * Owner action workflow: one-click status change from Metabase without the BigQuery console.
 *
 * HOW IT WORKS
 *   Metabase table «Что делать сегодня» shows link columns (✅ Готово / ▶ В работе / ✖ Отменить).
 *   Each link points to this web app:  <webapp>/exec?id=CT-0002&status=DONE[&note=...]
 *   doGet() validates the parameters and calls the ONLY sanctioned write path —
 *   `evetis_ref.sp_ct_action_update(action_id, status, note, changed_by)` — via the BigQuery
 *   advanced service under the OWNER's Google account (Execute as: Me). The procedure writes
 *   CT_ACTION_STATUS_LOG (audit trail) and status_updated_at / completed_at.
 *   The response page confirms the change and offers undo (→ OPEN) and the other statuses.
 *
 * DEPLOYMENT (one-time, owner)
 *   1. Apps Script editor of the EVETIS project → add file CtOwnerActions.gs with this code.
 *   2. Deploy → New deployment → Web app → Execute as: Me · Who has access: Only myself → Deploy.
 *   3. Copy the /exec URL into BigQuery:
 *        UPDATE `evetis_ref.CT_CONFIG` SET config_value = '<url>', updated_at = CURRENT_TIMESTAMP()
 *        WHERE config_key = 'action_webapp_url';
 *      (or ask Claude to do it). Metabase link columns appear automatically (V_CT_ACTION_QUEUE).
 *   4. First click will ask for the BigQuery consent screen once.
 *
 * SAFETY
 *   - No marketplace writes. Only CT_* tables via the procedure.
 *   - Only whitelisted statuses; action_id must match ^CT-\d{4}$.
 *   - Access: Only myself (owner's Google account). A logged-out browser gets Google's login page.
 *   - GET applies the change immediately (one click) — the page always shows an undo link.
 */

var CT_PROJECT_ID = 'project-fa311fc0-4d87-4781-986';
var CT_ALLOWED_STATUSES = ['OPEN', 'IN_PROGRESS', 'DONE', 'CANCELLED'];
var CT_METABASE_HOME = 'http://localhost:3000/dashboard/5';

function doGet(e) {
  var p = (e && e.parameter) || {};
  var id = String(p.id || '').trim().toUpperCase();
  var status = String(p.status || '').trim().toUpperCase();
  var note = String(p.note || '').trim().slice(0, 300);

  if (!/^CT-\d{4}$/.test(id)) {
    return ctPage_('Некорректный идентификатор действия', 'Ожидается CT-0001 … CT-9999, получено: ' + ctEsc_(id || '—'), null, null);
  }
  var current = ctReadAction_(id);
  if (!current) {
    return ctPage_('Действие не найдено', ctEsc_(id) + ' отсутствует в очереди Control Tower.', null, null);
  }
  if (!status) {
    // No status → show the action card with buttons (safe landing page).
    return ctPage_('Действие ' + id, ctActionHtml_(current), id, current.status);
  }
  if (CT_ALLOWED_STATUSES.indexOf(status) < 0) {
    return ctPage_('Недопустимый статус', 'Разрешены: ' + CT_ALLOWED_STATUSES.join(', ') + '. Получено: ' + ctEsc_(status), id, current.status);
  }
  if (current.status === status) {
    return ctPage_('Без изменений', ctEsc_(id) + ' уже в статусе <b>' + ctStatusRu_(status) + '</b>.<br>' + ctActionHtml_(current), id, current.status);
  }

  var sql = 'CALL `' + CT_PROJECT_ID + '.evetis_ref.sp_ct_action_update`(@id, @status, @note, @who)';
  var req = {
    query: sql,
    useLegacySql: false,
    queryParameters: [
      ctParam_('id', id), ctParam_('status', status), ctParam_('note', note), ctParam_('who', 'owner:webapp')
    ]
  };
  try {
    BigQuery.Jobs.query(req, CT_PROJECT_ID);
  } catch (err) {
    return ctPage_('Ошибка записи', 'BigQuery отклонил изменение: ' + ctEsc_(String(err && err.message || err)), id, current.status);
  }
  var after = ctReadAction_(id) || current;
  var title = (status === 'DONE' ? '✅ Готово' : status === 'IN_PROGRESS' ? '▶ В работе' : status === 'CANCELLED' ? '✖ Отменено' : '↩ Снова открыто') + ' · ' + id;
  var body = '<p>Статус изменён: <b>' + ctStatusRu_(current.status) + '</b> → <b>' + ctStatusRu_(status) + '</b>' +
             (note ? ' · заметка сохранена' : '') + '.</p>' + ctActionHtml_(after) +
             '<p class="muted">Действие исчезнет из активной очереди Owner Home после обновления карточек (≈ 1 мин, кнопка «Обновить» в Metabase). История остаётся в разделе «Детали».</p>';
  return ctPage_(title, body, id, status);
}

/** Read the current row for a page (read-only). */
function ctReadAction_(id) {
  var sql = 'SELECT action_id, status, priority, what_to_do, executor_ru, deadline_ru, effect_ru, why_short, owner_note ' +
            'FROM `' + CT_PROJECT_ID + '.wb_mart.V_CT_ACTION_QUEUE` WHERE action_id = @id';
  var res = BigQuery.Jobs.query({ query: sql, useLegacySql: false, queryParameters: [ctParam_('id', id)] }, CT_PROJECT_ID);
  if (!res.rows || !res.rows.length) return null;
  var f = res.rows[0].f;
  return { action_id: f[0].v, status: f[1].v, priority: f[2].v, what_to_do: f[3].v, executor_ru: f[4].v,
           deadline_ru: f[5].v, effect_ru: f[6].v, why_short: f[7].v, owner_note: f[8].v };
}

function ctParam_(name, value) {
  return { name: name, parameterType: { type: 'STRING' }, parameterValue: { value: value } };
}

function ctStatusRu_(s) {
  return { OPEN: 'Открыто', IN_PROGRESS: 'В работе', DONE: 'Готово', CANCELLED: 'Отменено', SUPERSEDED: 'Снято правилом' }[s] || s;
}

function ctEsc_(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function ctActionHtml_(a) {
  return '<div class="card">' +
    '<div class="pri">' + ctEsc_(a.priority) + ' · ' + ctStatusRu_(a.status) + '</div>' +
    '<div class="what">' + ctEsc_(a.what_to_do) + '</div>' +
    '<div class="meta">→ ' + ctEsc_(a.executor_ru) + ' · срок: ' + ctEsc_(a.deadline_ru) + (a.effect_ru ? ' · эффект: ' + ctEsc_(a.effect_ru) : '') + '</div>' +
    (a.why_short ? '<div class="why">Почему: ' + ctEsc_(a.why_short) + '</div>' : '') +
    (a.owner_note ? '<div class="why">Заметки: ' + ctEsc_(a.owner_note) + '</div>' : '') +
    '</div>';
}

function ctPage_(title, bodyHtml, id, currentStatus) {
  var base = ScriptApp.getService().getUrl();
  var btns = '';
  if (id) {
    var mk = function (st, label) {
      if (st === currentStatus) return '';
      return '<a class="btn" href="' + base + '?id=' + encodeURIComponent(id) + '&status=' + st + '">' + label + '</a>';
    };
    btns = '<div class="btns">' + mk('DONE', '✅ Готово') + mk('IN_PROGRESS', '▶ В работе') + mk('CANCELLED', '✖ Отменить') + mk('OPEN', '↩ Вернуть в открытые') + '</div>' +
      '<form method="get" action="' + base + '" class="noteform"><input type="hidden" name="id" value="' + ctEsc_(id) + '">' +
      '<input type="hidden" name="status" value="' + (currentStatus === 'DONE' ? 'DONE' : 'IN_PROGRESS') + '">' +
      '<input name="note" placeholder="Заметка (необязательно)" maxlength="300"><button type="submit">Сохранить заметку</button></form>';
  }
  var html = '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>' + ctEsc_(title) + '</title><style>' +
    'body{font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:#0f1b26;color:#e8eef4;margin:0;padding:28px}' +
    'h1{font-size:22px;margin:0 0 14px}.card{background:#172633;border-radius:10px;padding:14px 16px;margin:12px 0}' +
    '.pri{font-size:12px;letter-spacing:.04em;color:#9fb3c8;text-transform:uppercase}.what{font-size:18px;font-weight:600;margin:4px 0}' +
    '.meta,.why{color:#c6d3df;font-size:14px;margin-top:4px}.muted{color:#8aa0b5;font-size:13px}' +
    '.btns{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.btn{background:#2f6fed;color:#fff;text-decoration:none;padding:9px 14px;border-radius:8px;font-weight:600}' +
    '.noteform{display:flex;gap:8px;margin:8px 0 18px}.noteform input{flex:1;padding:8px 10px;border-radius:8px;border:1px solid #33475a;background:#0f1b26;color:#e8eef4}' +
    '.noteform button{padding:8px 12px;border-radius:8px;border:0;background:#33475a;color:#fff}a.home{color:#8fc1ff}' +
    '</style></head><body><h1>' + ctEsc_(title) + '</h1>' + bodyHtml + btns +
    '<p><a class="home" href="' + CT_METABASE_HOME + '">← Вернуться в EVETIS OWNER HOME</a></p></body></html>';
  return HtmlService.createHtmlOutput(html).setTitle(title).setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

/** Manual smoke test from the editor: reads one action, changes nothing. */
function ctSmokeTest() {
  var a = ctReadAction_('CT-0001');
  Logger.log(JSON.stringify(a));
}
