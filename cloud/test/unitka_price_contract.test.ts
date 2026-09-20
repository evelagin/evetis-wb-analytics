/**
 * UNITKA — КОНТРАКТ ЦЕНЫ: один на суточный слой и слой сверки.
 *
 * Зачем: пока подстановка цены из воронки жила только в V_UNITKA_RECON_FACT, суточный писатель видел в
 * V_UNITKA_DAILY_FACT NULL и планировал СТЕРЕТЬ отремонтированную цену (production-наблюдение
 * unitka-engine-prod-sj7z6 от 20.09.2026: пять ячеек «806→»). Два разных контракта цены в двух слоях
 * разрушают данные, поэтому выражения обязаны совпадать ДОСЛОВНО.
 *
 * Две части:
 *  1. Статически (идёт всегда, в CI тоже): текст `funnel_fallback_ok`, `price` и `price_source`
 *     извлекается из ОБОИХ файлов и сравнивается побайтово; проверяются шесть условий допуска,
 *     единственное деление суммы воронки, отсутствие широкого COALESCE и порядок колонок.
 *  2. Состязательные сценарии на живом BigQuery (только при UNITKA_LIVE_BQ=1; в CI пропускаются):
 *     извлечённые выражения исполняются как обычный SELECT над синтетическим массивом строк. Ни одной
 *     строки DML, ни одного обращения к настоящим таблицам — это проверяется для каждого запроса и офлайн.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p: string): string => readFileSync(join(root, p), 'utf8');
const stripComments = (s: string): string => s.split('\n').map((l) => { const i = l.indexOf('--'); return i >= 0 ? l.slice(0, i) : l; }).join('\n');
const P = 'project-fa311fc0-4d87-4781-986';

const ENGINE = 'sql/unitka/engine_v1_views.sql';
const RECON = 'sql/unitka/reconcile_v1.sql';
const FILES = [ENGINE, RECON] as const;

/** Три выражения контракта цены, извлечённые из файла ровно так, как они там записаны. */
function priceContract(file: string): { gate: string; price: string; source: string } {
  const sql = stripComments(read(file));
  const gate = sql.match(/\(x\.orders_api_price IS NULL[\s\S]*?\) AS funnel_fallback_ok/)?.[0];
  const price = sql.match(/CASE\n {4}WHEN orders_api_price IS NOT NULL THEN ROUND\(orders_api_price, 2\)[\s\S]*?\n {2}END/)?.[0];
  const source = sql.match(/CASE\n {4}WHEN orders_api_price IS NOT NULL THEN 'ORDERS_API'[\s\S]*?\n {2}END/)?.[0];
  expect(gate, `${file}: не найден funnel_fallback_ok`).toBeTruthy();
  expect(price, `${file}: не найдено выражение price`).toBeTruthy();
  expect(source, `${file}: не найдено выражение price_source`).toBeTruthy();
  return { gate: gate!, price: price!, source: source! };
}

const engine = priceContract(ENGINE);
const recon = priceContract(RECON);

/** Тело именно той вью, которая разрешает цену: в файле движка рядом живут и другие вью. */
const PRICE_VIEW: Record<string, string> = { [ENGINE]: 'V_UNITKA_DAILY_FACT', [RECON]: 'V_UNITKA_RECON_FACT' };
function priceViewBody(file: string): string {
  const sql = stripComments(read(file));
  const at = sql.indexOf(`CREATE OR REPLACE VIEW \`${P}.wb_mart.${PRICE_VIEW[file]}\` AS`);
  expect(at, `${file}: вью ${PRICE_VIEW[file]} не найдена`).toBeGreaterThan(-1);
  const next = sql.indexOf('CREATE OR REPLACE VIEW', at + 10);
  return next < 0 ? sql.slice(at) : sql.slice(at, next);
}

/* ───────────────────────── 1. статически ───────────────────────── */

describe('контракт цены: одно определение на два слоя, дословно', () => {
  it('условие допуска fallback побайтово совпадает в суточном слое и в слое сверки', () => {
    expect(engine.gate).toBe(recon.gate);
  });
  it('выражения price и price_source побайтово совпадают в обоих слоях', () => {
    expect(engine.price).toBe(recon.price);
    expect(engine.source).toBe(recon.source);
  });
  it.each(FILES)('%s: шесть условий допуска, только через AND, приоритет у Orders API', (file) => {
    const { gate, price, source } = priceContract(file);
    const cond = gate.replace(/\s+/g, ' ').match(/\((x\.orders_api_price IS NULL .*?)\) AS funnel_fallback_ok/)![1]!;
    expect(cond.split(' AND ')).toEqual([
      'x.orders_api_price IS NULL',            // 1. Orders API не дал цену
      'x.fact_order_qty = 0',                  // 2. и НИ ОДНОГО заказа — расхождение счётчиков не принимается
      "x.orders_source = 'FUNNEL_API'",        // 3. счётчик Unitka взят из воронки (не XLSX, не Orders API)
      'IFNULL(x.funnel_orders, 0) > 0',        // 4. в день без заказов цена не выдумывается
      'x.funnel_orders = x.orders',            // 5. делитель — ровно тот счётчик, что стоит в листе
      'IFNULL(x.funnel_orders_sum, 0) > 0',    // 6. сумма положительна: ни NULL, ни 0, ни отрицательная
    ]);
    expect(cond).not.toMatch(/\bOR\b/);
    expect(price.replace(/\s+/g, ' ')).toContain('WHEN orders_api_price IS NOT NULL THEN ROUND(orders_api_price, 2)');
    expect(source.replace(/\s+/g, ' ')).toContain("WHEN orders_api_price IS NOT NULL THEN 'ORDERS_API'");
  });
  it('широкого COALESCE по цене нет ни в одном слое: неразрешённый случай остаётся NULL', () => {
    for (const file of FILES) {
      const sql = stripComments(read(file));
      expect(sql, file).not.toMatch(/COALESCE\([^)]*(orders_api_price|funnel_orders_sum|fsum)/i);
      expect(sql, file).not.toMatch(/IFNULL\(\s*(x\.)?orders_api_price/i);
      // у CASE нет ELSE: не разрешили — значит NULL, а не выдуманная цена
      expect(priceContract(file).price).not.toMatch(/\bELSE\b/);
      expect(priceContract(file).source).not.toMatch(/\bELSE\b/);
    }
  });
  it('сумма воронки делится ровно один раз в каждом слое — под funnel_fallback_ok', () => {
    for (const file of FILES) {
      const flat = priceViewBody(file).replace(/\s+/g, ' ');
      expect([...flat.matchAll(/SAFE_DIVIDE\(([^,]+),/g)].map((m) => m[1]), file)
        .toEqual(['SUM(price_with_disc * quantity)', 'funnel_orders_sum']);
      expect(flat, file).not.toMatch(/(funnel_orders_sum|fsum|orders_sum_rub)\s*\//);   // «на глаз» делить нельзя
    }
  });
});

describe('суточный слой: провенанс и неизменные границы', () => {
  const sql = stripComments(read(ENGINE));
  const at = sql.indexOf(`CREATE OR REPLACE VIEW \`${P}.wb_mart.V_UNITKA_DAILY_FACT\` AS`);
  const daily = sql.slice(at, sql.indexOf('CREATE OR REPLACE VIEW', at + 10));

  it('происхождение цены различимо: ORDERS_API / FUNNEL_FALLBACK / NULL', () => {
    expect(daily).toMatch(/END\s+AS price_source/);
    expect(daily).toContain("'FUNNEL_FALLBACK'");
    expect(daily).toContain("'ORDERS_API'");
  });
  it('колонки сохранены теми же именами и порядком, price_source добавлен в хвост', () => {
    const flat = daily.replace(/\s+/g, ' ');
    const final = flat.slice(flat.lastIndexOf('SELECT nm_id'));
    const names = ['nm_id', 'date_msk', 'views', 'opens', 'carts', 'orders', 'cancels', 'stock', 'ads_in', 'price', 'storage', 'orders_source', 'cancels_source', 'price_source'];
    let prev = -1;
    for (const n of names) { const next = final.search(new RegExp(`(\\bAS ${n}\\b|[ ,]${n}(,| FROM))`)); expect(next, n).toBeGreaterThan(prev); prev = next; }
  });
  it('окно суточного слоя не тронуто: текущий месяц до LCD, то есть никогда раньше эпохи сверки 2026-09-01', () => {
    expect(daily).toContain('DATE_TRUNC(last_closed_date, MONTH)');
    expect(daily).not.toMatch(/V_UNITKA_RECON_WINDOW|reconciliation_epoch/);           // суточный слой не зависит от окна сверки
  });
  it('fsum берётся только из воронки и только как делимое подстановки', () => {
    expect(daily).toContain('MAX(orders_sum_rub) AS fsum');
    expect(daily).toMatch(/f\.fsum\s+AS funnel_orders_sum/);
  });
  it('PR не трогает контракт отмен, COGS, СПП и блогеров', () => {
    expect(daily).toContain('SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) > order_date, quantity, 0)) AS canc');
    expect(daily).toContain('COALESCE(bf.canc, o.canc, 0)');
    expect(daily).not.toMatch(/COGS|R\$?45|spp|blogger/i);
  });
});

/* ───────────── 2. состязательные сценарии на живом BigQuery ───────────── */

interface Row {
  name: string;
  orders_api_price: number | null;
  fact_order_qty: number;
  orders_source: string;
  funnel_orders: number | null;
  orders: number;
  funnel_orders_sum: number | null;
  price: number | null;
  price_source: string | null;
  why: string;
}

const lit = (v: number | null): string => (v === null ? 'NULL' : String(v));

/** Состязательная таблица: каждая строка бьёт ровно в одно условие допуска. */
const ROWS: Row[] = [
  { name: 'known_806', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 1, orders: 1, funnel_orders_sum: 806, price: 806, price_source: 'FUNNEL_FALLBACK', why: '08.09/305101361 и 10.09/305101361 — доказанный случай' },
  { name: 'known_1120', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 1, orders: 1, funnel_orders_sum: 1120, price: 1120, price_source: 'FUNNEL_FALLBACK', why: '09.09 и 17.09 / 930334396' },
  { name: 'known_1274', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 1, orders: 1, funnel_orders_sum: 1274, price: 1274, price_source: 'FUNNEL_FALLBACK', why: '19.09/952068582' },
  { name: 'fact_orders_wins', orders_api_price: 700, fact_order_qty: 1, orders_source: 'FUNNEL_API', funnel_orders: 1, orders: 1, funnel_orders_sum: 9999, price: 700, price_source: 'ORDERS_API', why: 'есть цена Orders API — воронка не используется даже при странной сумме' },
  { name: 'late_data_keeps_orders_api', orders_api_price: 740, fact_order_qty: 1, orders_source: 'FUNNEL_API', funnel_orders: 5, orders: 5, funnel_orders_sum: 3700, price: 740, price_source: 'ORDERS_API', why: 'Orders API отстаёт от воронки — цена НЕ пересчитывается, семантика LATE_DATA сохранена' },
  { name: 'orders_api_ahead', orders_api_price: null, fact_order_qty: 3, orders_source: 'FUNNEL_API', funnel_orders: 2, orders: 2, funnel_orders_sum: 1332, price: null, price_source: null, why: 'Orders API впереди воронки — подстановка запрещена (fact_order_qty ≠ 0)' },
  { name: 'count_mismatch', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 2, orders: 1, funnel_orders_sum: 2240, price: null, price_source: null, why: 'делитель не равен счётчику листа' },
  { name: 'zero_orders', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 0, orders: 0, funnel_orders_sum: 0, price: null, price_source: null, why: 'день без заказов — цена не выдумывается' },
  { name: 'sum_null', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 1, orders: 1, funnel_orders_sum: null, price: null, price_source: null, why: 'orders_sum_rub NULL — подстановки нет' },
  { name: 'sum_zero', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 1, orders: 1, funnel_orders_sum: 0, price: null, price_source: null, why: 'нулевая сумма — цена 0 недопустима' },
  { name: 'sum_negative', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 1, orders: 1, funnel_orders_sum: -100, price: null, price_source: null, why: 'отрицательная сумма — отвергается' },
  { name: 'xlsx_backfill', orders_api_price: null, fact_order_qty: 0, orders_source: 'XLSX_BACKFILL', funnel_orders: 1, orders: 1, funnel_orders_sum: 806, price: null, price_source: null, why: 'исторический бэкфилл не даёт подстановку' },
  { name: 'orders_api_source', orders_api_price: null, fact_order_qty: 0, orders_source: 'ORDERS_API', funnel_orders: 1, orders: 1, funnel_orders_sum: 806, price: null, price_source: null, why: 'счётчик не из воронки — подстановки нет' },
  { name: 'funnel_orders_null', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: null, orders: 0, funnel_orders_sum: 806, price: null, price_source: null, why: 'нет счётчика воронки — делить не на что' },
  { name: 'rounding_two_places', orders_api_price: null, fact_order_qty: 0, orders_source: 'FUNNEL_API', funnel_orders: 3, orders: 3, funnel_orders_sum: 1000, price: 333.33, price_source: 'FUNNEL_FALLBACK', why: 'граница округления: 1000/3 → ровно два знака' },
];

/** Тело запроса: синтетический массив x-строк + ИЗВЛЕЧЁННЫЕ из файла выражения контракта. */
function contractQuery(file: string): string {
  const { gate, price, source } = priceContract(file);
  const structs = ROWS.map((r) => `STRUCT('${r.name}' AS name, ${lit(r.orders_api_price)} AS orders_api_price, `
    + `${r.fact_order_qty} AS fact_order_qty, '${r.orders_source}' AS orders_source, `
    + `CAST(${lit(r.funnel_orders)} AS INT64) AS funnel_orders, ${r.orders} AS orders, `
    + `CAST(${lit(r.funnel_orders_sum)} AS FLOAT64) AS funnel_orders_sum)`).join(',\n    ');
  return `WITH x AS (\n  SELECT * FROM UNNEST([\n    ${structs}\n  ])\n),\n`
    + `p AS (\n  SELECT x.*, ${gate}\n  FROM x\n)\n`
    + `SELECT name, ${price} AS price, ${source} AS price_source FROM p ORDER BY name`;
}

describe('контракт цены на живом BigQuery: состязательные случаи', () => {
  it.each(FILES)('%s: запрос синтетический — ни DML, ни настоящих таблиц', (file) => {
    const q = contractQuery(file);
    expect(q).not.toMatch(/\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b/i);
    expect(q).not.toMatch(/wb_ops|wb_mart|wb_raw|FACT_ORDERS|V_WB_FUNNEL_DAILY/);
  });

  const live = process.env.UNITKA_LIVE_BQ === '1';
  it.runIf(live).each(FILES)('%s: все %i состязательных строк разрешаются по контракту', async (file) => {
    const { BqClient } = await import('../src/bq/client.js');
    const rows = await new BqClient(P, 'EU').query<Record<string, unknown>>(contractQuery(file));
    expect(rows).toHaveLength(ROWS.length);
    const num = (v: unknown): number | null => (v === null || v === undefined ? null : Number(String(v)));
    const got = new Map(rows.map((r) => [String(r.name), { price: num(r.price), price_source: r.price_source ?? null }]));
    for (const r of ROWS) {
      expect(got.get(r.name), `${r.name}: ${r.why}`).toEqual({ price: r.price, price_source: r.price_source });
    }
  }, 60_000);
});
