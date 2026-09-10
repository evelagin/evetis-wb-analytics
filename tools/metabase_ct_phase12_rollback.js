/*
 * EVETIS OWNER CONTROL TOWER — Phase 1.2 · Metabase rollback (→ Phase 1.1)
 * ---------------------------------------------------------------------
 * Run in the browser console on http://localhost:3000 (owner session) AFTER pasting
 * tools/metabase_ct_phase11_build.js (it defines window.ctBuild with the Phase 1.1 catalogue).
 *
 *   await ctRollback12.all()
 *
 * What it does:
 *   1. matches every Phase 1.1 card by the key stored in the card description
 *      («EVETIS Control Tower Phase 1.x · <key>») — names changed in Phase 1.2, so name lookup is unsafe;
 *   2. PUTs the Phase 1.1 definition (name, SQL, visualization) back into the same card id;
 *   3. re-lays out dashboards 5 / 6 / ДЕТАЛИ / DAILY BRIEF with the Phase 1.1 layouts (ctBuild.layouts);
 *   4. archives cards that exist only in Phase 1.2 (brief_* sections, period_summary,
 *      sp_top_sku_table, channels_month).
 * Nothing is deleted; archived cards can be restored from the Metabase trash.
 */
(function () {
  const COLL = 9, DB = 2;
  const mb = async (path, method = 'GET', body) => {
    const r = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const t = await r.text(); try { return JSON.parse(t); } catch (e) { return { status: r.status, text: t.slice(0, 300) }; }
  };
  // Template tags of Phase 1.1 (the Phase 1.1 builder does not export them)
  const TAGS11 = { marketplace: { name: 'marketplace', 'display-name': 'Канал', type: 'text', required: false }, sales_mode: { name: 'sales_mode', 'display-name': 'Соло/набор', type: 'text', required: false }, sku: { name: 'sku', 'display-name': 'SKU', type: 'text', required: false }, product_line: { name: 'product_line', 'display-name': 'Линия', type: 'text', required: false } };
  const ONLY_12 = ['brief_yesterday', 'brief_month', 'brief_deviations', 'brief_today', 'brief_decisions', 'brief_data', 'period_summary', 'sp_top_sku_table', 'channels_month'];
  async function all() {
    if (!window.ctBuild || !window.ctBuild.CARDS) throw new Error('Сначала вставьте tools/metabase_ct_phase11_build.js');
    const B = window.ctBuild;
    const existing = await mb('/collection/' + COLL + '/items?models=card');
    const byKey = {};
    for (const c of existing.data || []) { const m = /Phase 1\.\d · (\w+)$/.exec(c.description || ''); if (m) byKey[m[1]] = c.id; }
    // 1–2. Phase 1.1 definitions back into the same ids
    for (const c of B.CARDS) {
      const id = c.id || byKey[c.key];
      const payload = { name: c.name, display: c.display, collection_id: COLL, visualization_settings: c.vs || {}, dataset_query: { database: DB, type: 'native', native: { query: c.sql, 'template-tags': c.tags ? TAGS11 : {} } }, description: 'EVETIS Control Tower Phase 1.1 · ' + c.key };
      const r = id ? await mb('/card/' + id, 'PUT', payload) : await mb('/card', 'POST', { ...payload, type: 'question' });
      if (!r.id) throw new Error('card ' + c.key + ': ' + JSON.stringify(r).slice(0, 200));
      B.IDS[c.key] = r.id;
    }
    // 3. Phase 1.1 layouts (uses B.IDS)
    const lay = await B.layouts();
    // 4. archive Phase 1.2-only cards
    const archived = [];
    for (const k of ONLY_12) if (byKey[k]) { await mb('/card/' + byKey[k], 'PUT', { archived: true }); archived.push(k + '=' + byKey[k]); }
    return { layouts: lay, archived };
  }
  window.ctRollback12 = { all };
})();
