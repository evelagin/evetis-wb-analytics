/**
 * UNITKA FINANCIAL INTEGRITY V1 — статическая проверка SQL слоя сверки (sql/unitka/reconcile_v1.sql).
 * Офлайн парсера GoogleSQL нет — проверяются тексты исходников (как в unitka_integrity_sql.test.ts). Живые данные
 * слоя проверены read-only запросами с подстановкой вью (доку §11); здесь — границы, которые не должны тихо съехать:
 * окно 35 дней и нижняя граница, условие fallback цены, запрет чужих источников цены, граница доступа, откат.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { RECONCILE_WINDOW_DAYS, RECONCILIATION_EPOCH } from '../src/loaders/unitka/reconcile.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p: string): string => readFileSync(join(root, p), 'utf8');
const code = (s: string): string => s.split('\n').map((l) => { const i = l.indexOf('--'); return i >= 0 ? l.slice(0, i) : l; }).join('\n');
function viewBodies(sql: string): Map<string, string> {
  const out = new Map<string, string>();
  const re = /CREATE OR REPLACE VIEW `[^`]+\.(\w+)` AS\n([\s\S]*?)(?=\nCREATE |$)/g;
  for (const m of code(sql).matchAll(re)) out.set(m[1]!, m[2]!);
  return out;
}
const objectsOf = (body: string): string[] => [...body.matchAll(/`project-fa311fc0-4d87-4781-986\.(\w+\.\w+)`/g)].map((m) => m[1]!);
const NEW_VIEWS = ['V_UNITKA_INTEGRITY_STATUS', 'V_UNITKA_RECON_COGS_CANONICAL', 'V_UNITKA_RECON_FACT', 'V_UNITKA_RECON_INTEGRITY', 'V_UNITKA_RECON_WINDOW'];

const sql = read('sql/unitka/reconcile_v1.sql');
const views = viewBodies(sql);

describe('слой сверки: состав и граница доступа', () => {
  it('ровно пять новых вью; существующие вью Unitka не пересоздаются', () => {
    expect([...views.keys()].sort()).toEqual(NEW_VIEWS);
    expect(code(sql)).not.toMatch(/CREATE OR REPLACE VIEW `[^`]+\.(V_UNITKA_DAILY_FACT|V_UNITKA_INTEGRITY|V_UNITKA_COGS_CANONICAL|V_UNITKA_LAST_CLOSED_DATE)`/);
  });
  it('только SELECT-вью: ни таблиц, ни DML, ни процедур', () => {
    expect(code(sql)).not.toMatch(/\b(CREATE\s+(OR\s+REPLACE\s+)?(TABLE|PROCEDURE|FUNCTION)|INSERT\s+INTO|UPDATE\s+`|DELETE\s+FROM|MERGE\s+|TRUNCATE|DROP\s+)/i);
  });
  it.each(NEW_VIEWS)('%s: evetis_ref и Ozon не читаются; только wb_raw / wb_mart / wb_ops', (name) => {
    const body = views.get(name)!;
    expect(body).not.toMatch(/evetis_ref|ozon_/i);
    expect(objectsOf(body).every((o) => /^(wb_raw|wb_mart|wb_ops)\./.test(o))).toBe(true);
  });
  it('wb_ops читает только вью наблюдаемости (две новые таблицы); вью фактов — нет', () => {
    for (const name of NEW_VIEWS.filter((n) => n !== 'V_UNITKA_INTEGRITY_STATUS')) expect(objectsOf(views.get(name)!).some((o) => o.startsWith('wb_ops.'))).toBe(false);
    expect(new Set(objectsOf(views.get('V_UNITKA_INTEGRITY_STATUS')!).filter((o) => o.startsWith('wb_ops.')))).toEqual(new Set(['wb_ops.UNITKA_INTEGRITY_ISSUES', 'wb_ops.UNITKA_REPAIR_LEDGER']));
  });
  it('канон COGS — физическая копия UNITKA_COGS_EFFECTIVE, не вью evetis_ref', () => {
    const body = views.get('V_UNITKA_RECON_COGS_CANONICAL')!;
    expect(body).toContain('wb_mart.UNITKA_COGS_EFFECTIVE');
    expect(body).not.toMatch(/V_PRODUCT_COGS_EFFECTIVE/);
  });
});

describe('окно и ЭПОХА СВЕРКИ: явный параметр домена, заданный один раз, тот же, что в коде Engine', () => {
  const w = views.get('V_UNITKA_RECON_WINDOW')!;
  const flat = w.replace(/\s+/g, ' ');
  it('эпоха и длина окна заданы один раз в CTE cfg и совпадают с reconcile.ts', () => {
    expect([RECONCILE_WINDOW_DAYS, RECONCILIATION_EPOCH]).toEqual([35, '2026-09-01']);
    expect(flat).toContain(`WITH cfg AS ( SELECT DATE '${RECONCILIATION_EPOCH}' AS reconciliation_epoch, ${RECONCILE_WINDOW_DAYS} AS window_days )`);
    // литерал эпохи — ровно один раз во всём коде слоя (комментарии не в счёт): не «дата, зашитая в логику запросов»
    expect(code(sql).match(new RegExp(`'${RECONCILIATION_EPOCH}'`, 'g'))).toHaveLength(1);
  });
  it('начало окна = max(начало скользящих 35 дней, эпоха); конец = LCD; скользящее начало отдаётся для прозрачности', () => {
    expect(flat).toContain('GREATEST(DATE_SUB(l.last_closed_date, INTERVAL cfg.window_days - 1 DAY), cfg.reconciliation_epoch) AS window_from');
    expect(flat).toMatch(/l\.last_closed_date AS window_to/);
    expect(flat).toMatch(/cfg\.reconciliation_epoch, DATE_SUB\(l\.last_closed_date, INTERVAL cfg\.window_days - 1 DAY\) AS rolling_window_from/);
    expect(flat).not.toMatch(/LEAST\(/);
  });
  it('границы окна остальные вью берут из V_UNITKA_RECON_WINDOW, а не из своих констант', () => {
    for (const name of ['V_UNITKA_RECON_FACT', 'V_UNITKA_RECON_INTEGRITY', 'V_UNITKA_RECON_COGS_CANONICAL']) {
      const body = views.get(name)!;
      expect(body).not.toMatch(/INTERVAL\s+3[0-9]\s+DAY/);
      expect(body).not.toContain(RECONCILIATION_EPOCH);
      expect(body).toMatch(/V_UNITKA_RECON_WINDOW|V_UNITKA_RECON_FACT/);
    }
  });
  it('Engine читает эпоху из вью тем же именем колонки', () => {
    expect(read('cloud/src/loaders/unitka/bq.ts')).toContain('window_days, reconciliation_epoch FROM');
  });
});

describe('отмена дня заказа — только диагностика расхождения счётчиков', () => {
  const f = views.get('V_UNITKA_RECON_FACT')!.replace(/\s+/g, ' ');
  it('считается по Orders API (cancel_dt = order_date) и отдаётся в обе вью', () => {
    expect(f).toContain('SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) = order_date, quantity, 0)) AS same_day_canc');
    expect(f).toMatch(/IFNULL\(o\.same_day_canc, 0\) AS same_day_cancel_qty/);
    expect(views.get('V_UNITKA_RECON_INTEGRITY')!).toMatch(/f\.same_day_cancel_qty/);
  });
  it('в Q, S и цену листа не входит: формулы этих колонок прежние', () => {
    expect(f).toContain('COALESCE(f.forders, bf.forders, o.gross, 0) AS orders');
    expect(f).toContain('COALESCE(bf.canc, o.canc, 0) AS cancels');
    expect(f.match(/same_day_canc\b/g)).toHaveLength(2);                                            // определение + вывод, больше нигде
  });
});

describe('цена: основной источник, доказанный fallback, запрет выдуманных цен', () => {
  const f = views.get('V_UNITKA_RECON_FACT')!;
  const flat = f.replace(/\s+/g, ' ');
  const final = flat.slice(flat.lastIndexOf('SELECT nm_id'));                                     // итоговый SELECT вью
  it('Orders API — первый в CASE и для цены, и для происхождения', () => {
    const price = final.match(/CASE (WHEN .*?) END AS price,/)![1]!;
    const source = final.match(/, CASE (WHEN orders_api_price[^,]*?) END AS price_source/)![1]!;
    expect(price).toMatch(/^WHEN orders_api_price IS NOT NULL THEN ROUND\(orders_api_price, 2\) WHEN funnel_fallback_ok THEN ROUND\(SAFE_DIVIDE\(funnel_orders_sum, funnel_orders\), 2\)$/);
    expect(source).toMatch(/^WHEN orders_api_price IS NOT NULL THEN 'ORDERS_API' WHEN funnel_fallback_ok THEN 'FUNNEL_FALLBACK'$/);
  });
  it('fallback: Orders API пуст И счётчик Unitka = воронка И счётчики согласны И сумма > 0 — все условия через AND', () => {
    const cond = flat.match(/\((x\.orders_api_price IS NULL .*?)\) AS funnel_fallback_ok/)![1]!;
    expect(cond.split(' AND ')).toEqual([
      'x.orders_api_price IS NULL', 'x.fact_order_qty = 0', "x.orders_source = 'FUNNEL_API'",
      'IFNULL(x.funnel_orders, 0) > 0', 'x.funnel_orders = x.orders', 'IFNULL(x.funnel_orders_sum, 0) > 0',
    ]);
    expect(cond).not.toMatch(/\bOR\b/);
  });
  it('сумма воронки делится ровно в одном месте — под funnel_fallback_ok', () => {
    // второе деление в файле — средневзвешенная цена Orders API (SUM(price·qty) / SUM(qty)), к воронке не относится
    expect([...flat.matchAll(/SAFE_DIVIDE\(([^,]+),/g)].map((m) => m[1])).toEqual(['SUM(price_with_disc * quantity)', 'funnel_orders_sum']);
    expect(flat).not.toMatch(/(funnel_orders_sum|fsum|orders_sum_rub)\s*\//);
  });
  it('нет соседних дней, цены карточки, средних и чужих SKU: ни оконных функций по цене, ни наблюдателя цен в факте', () => {
    expect(f).not.toMatch(/\b(LAG|LEAD|LAST_VALUE|FIRST_VALUE)\s*\(/i);
    expect(f).not.toMatch(/PRICE_OBSERV|V_WB_PRICE|RAW_WB_PRICES|card_price|AVG\s*\(/i);
  });
  it('наблюдаемая цена в вью целостности — только диагностика, в цену не попадает', () => {
    const i = views.get('V_UNITKA_RECON_INTEGRITY')!;
    expect(i).toMatch(/observed_price_diagnostic/);
    expect(i).not.toMatch(/COALESCE\([^)]*observed_price/i);
  });
  it('колонки V_UNITKA_DAILY_FACT сохранены теми же именами и порядком; новые — в хвосте', () => {
    const select = final;
    const names = ['nm_id', 'date_msk', 'views', 'opens', 'carts', 'orders', 'cancels', 'stock', 'ads_in', 'price', 'storage', 'orders_source', 'cancels_source', 'price_source', 'fact_order_qty', 'funnel_orders', 'funnel_orders_sum', 'sku_active', 'month_key', 'storage_observed_at', 'same_day_cancel_qty'];
    let at = -1;
    for (const n of names) { const next = select.search(new RegExp(`(\\bAS ${n}\\b|[ ,]${n}(,| FROM))`)); expect(next, n).toBeGreaterThan(at); at = next; }
  });
});

describe('контракт отмен: S = только отмены СЛЕДУЮЩИХ дней (доказан на официальном экспорте WB 01–19.09.2026)', () => {
  const LATER = "SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) > order_date, quantity, 0)) AS canc";
  const SAME  = "SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) = order_date, quantity, 0)) AS same_day_canc";
  it.each(['sql/unitka/reconcile_v1.sql', 'sql/unitka/engine_v1_views.sql'])('%s: cancels берёт СТРОГО отмены следующих дней', (file) => {
    const body = code(read(file));
    expect(body).toContain(LATER);
    // отмена дня заказа НИКОГДА не попадает в поле cancels: нет ни безусловного счёта отмен, ни счёта "=" в canc
    expect(body).not.toMatch(/SUM\(IF\(is_cancel,\s*quantity,\s*0\)\)\s*AS canc\b/);
    expect(body).not.toMatch(/=\s*order_date, quantity, 0\)\)\s*AS canc\b/);
    expect(body).toContain('COALESCE(bf.canc, o.canc, 0)');          // XLSX-бэкфилл (сама воронка) остаётся приоритетным источником
  });
  it('слой сверки отдаёт отмену дня заказа отдельной ДИАГНОСТИЧЕСКОЙ колонкой, не смешивая её с cancels', () => {
    const f = views.get('V_UNITKA_RECON_FACT')!;
    expect(code(f)).toContain(SAME);
    expect(code(f)).toMatch(/IFNULL\(o\.same_day_canc, 0\)\s+AS same_day_cancel_qty/);
    const flat = code(f).replace(/\s+/g, ' ');
    expect(flat).toContain('COALESCE(bf.canc, o.canc, 0) AS cancels');
    expect(flat).not.toMatch(/same_day_canc[^,]*AS cancels/);
  });
});

describe('откат', () => {
  const rb = code(read('sql/unitka/reconcile_v1_rollback.sql'));
  it('только DROP VIEW IF EXISTS пяти новых вью, в порядке зависимостей (сначала потребители)', () => {
    const stmts = rb.split(';').map((s) => s.trim()).filter(Boolean);
    expect(stmts.every((s) => /^DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986\.wb_mart\.\w+`$/.test(s))).toBe(true);
    const dropped = stmts.map((s) => s.match(/\.(\w+)`$/)![1]!);
    expect([...dropped].sort()).toEqual(NEW_VIEWS);
    expect(dropped.indexOf('V_UNITKA_RECON_WINDOW')).toBe(dropped.length - 1);
    expect(dropped.indexOf('V_UNITKA_RECON_INTEGRITY')).toBeLessThan(dropped.indexOf('V_UNITKA_RECON_FACT'));
  });
});

describe('активация: режим сверки не включается доставкой', () => {
  it('ни один workflow и ни один Terraform-файл не задают UNITKA_RECONCILE_MODE (по умолчанию в коде — off)', () => {
    for (const f of ['.github/workflows/deploy-shadow.yml', '.github/workflows/deploy-prod.yml', '.github/workflows/infra.yml', 'infra/terraform/unitka_engine.tf']) {
      expect(read(f).split('\n').filter((l) => !l.trim().startsWith('#')).join('\n'), f).not.toMatch(/UNITKA_RECONCILE_MODE\s*=/);
    }
  });
  it('Terraform: две таблицы wb_ops под deletion_protection; запись — потаблично и только prod', () => {
    const tf = read('infra/terraform/unitka_engine.tf');
    for (const t of ['UNITKA_REPAIR_LEDGER', 'UNITKA_INTEGRITY_ISSUES']) expect(tf).toContain(`table_id            = "${t}"`);
    const iam = [...tf.matchAll(/resource "google_bigquery_table_iam_member" "(unitka_repair_ledger_write|unitka_integrity_issues_write)" \{([\s\S]*?)\n\}/g)];
    expect(iam).toHaveLength(2);
    for (const m of iam) { expect(m[2]).toContain('google_service_account.loaders_prod.email'); expect(m[2]).not.toMatch(/loaders_shadow|for_each/); expect(m[2]).toContain('roles/bigquery.dataEditor'); }
    expect(tf).not.toMatch(/google_bigquery_dataset_iam_member" "unitka_(repair|integrity_issues)/);
  });
  it('INSERT журнала и снимка: без SAFE.DATE / SAFE.TIMESTAMP — BigQuery отвергает «SAFE with function date» (найдено проверкой на живом BigQuery)', () => {
    const bq = read('cloud/src/loaders/unitka/bq.ts');
    expect(bq).not.toMatch(/SAFE\.(DATE|TIMESTAMP|DATETIME)\s*\(/);
    expect(bq).toContain("SAFE_CAST(JSON_VALUE(x, '$.businessDate') AS DATE)");
  });
  it('колонки таблиц = колонкам INSERT в bq.ts', () => {
    const tf = read('infra/terraform/unitka_engine.tf'); const bq = read('cloud/src/loaders/unitka/bq.ts');
    const cols = (table: string): string[] => { const at = tf.indexOf(`table_id            = "${table}"`); const block = tf.slice(at, tf.indexOf('\n  ])', at)); return [...block.matchAll(/\{ name = "(\w+)"/g)].map((m) => m[1]!); };
    // первый ключ SELECT сразу после списка колонок различает два INSERT: журнал — repairId, снимок issue — runId
    const ins = (first: string): string[] => { const m = bq.match(new RegExp(`INSERT INTO \\$\\{t\\}\\s*\\(([^)]*?)\\)\\s*SELECT JSON_VALUE\\(x, '\\$\\.${first}'\\)`)); return m![1]!.split(',').map((x) => x.trim()); };
    expect(cols('UNITKA_REPAIR_LEDGER')).toEqual(ins('repairId'));
    expect(cols('UNITKA_INTEGRITY_ISSUES')).toEqual(ins('runId'));
  });
});
