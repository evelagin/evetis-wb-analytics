/**
 * Статическая проверка SQL V_UNITKA_LOGISTICS_RATES (sql/unitka/engine_v1_views.sql) — хвост доказанного возврата.
 * Офлайн парсера GoogleSQL нет — проверяется текст. Живые данные проверены read-only запросами с подстановкой LCD:
 * паритет на срезе LCD 19.10 (22/22 строки), бэктест LCD 21.07–24.08 и прогноз LCD 02.11–18.11.
 * Граница, которая не должна тихо съехать: единственное плечо хвоста — ОБРАТНОЕ (в пул reverse, вне популяции прямых),
 * shipments и refusals сохраняют смысл, return_tail считается независимо по «Возврат» без «Продажи».
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const code = (s: string): string => s.split('\n').map((l) => { const i = l.indexOf('--'); return i >= 0 ? l.slice(0, i) : l; }).join('\n');
const sql = code(readFileSync(join(root, 'sql/unitka/engine_v1_views.sql'), 'utf8'));
const start = sql.indexOf('wb_mart.V_UNITKA_LOGISTICS_RATES` AS');
const body = sql.slice(start, sql.indexOf('CREATE OR REPLACE VIEW', start + 10));
const flat = body.replace(/\s+/g, ' ');

describe('V_UNITKA_LOGISTICS_RATES: хвост доказанного возврата', () => {
  it('хвост определён независимо: одно плечо, без «Продажи», с «Возвратом» на том же срезе b', () => {
    expect(flat).toContain("(NOT srx AND legs = 1 AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid IN (SELECT srid FROM b WHERE son = 'Возврат')) AS tail");
    expect(flat).toContain("WHERE legs = 1 AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid IN (SELECT srid FROM b WHERE son = 'Возврат') AND srid NOT IN (SELECT srid FROM sr)");
  });
  it('плечо хвоста — обратное: вне популяции прямых, в пуле обратных', () => {
    expect(flat).toContain('SUM(IF(tail OR sret, 0, fwd)) AS fwd');
    expect(flat).toContain('SUM(rev) + SUM(IF(tail, fwd, 0)) AS rev');
    expect(flat).toContain("SUM(IF(son = 'Доставка' AND NOT tail AND NOT sret, fwd, 0)) AS dlvfwd");
  });
  it('знаменатели: прямая — отправки без хвостов, обратная — отказы плюс хвосты (SKU и магазин)', () => {
    expect(flat).toContain('ROUND(SAFE_DIVIDE(fwd, NULLIF(n - nt - ns, 0)), 4) AS direct_rate');
    expect(flat).toContain('ROUND(SAFE_DIVIDE(rev, NULLIF(ref + nt, 0)), 4) AS reverse_rate');
    expect(flat).toContain('ROUND(SAFE_DIVIDE(SUM(fwd), NULLIF(SUM(n) - SUM(nt) - SUM(ns), 0)), 4)');
    expect(flat).toContain('ROUND(SAFE_DIVIDE(SUM(rev), NULLIF(SUM(ref) + SUM(nt), 0)), 4)');
  });
  it('у SKU return_tail = число хвостов (порог своей ставки в Engine — shipments − return_tail)', () => {
    expect(flat).toContain('CAST(NULL AS INT64) AS sale_refusal_overlap, nt AS return_tail, ns AS seller_return, ROUND(srsum, 2) AS seller_return_sum, IFNULL(xu.svc_rows, 0) AS service_rows');
    expect(flat).toContain('FROM a LEFT JOIN xu USING (nm) UNION ALL');
  });
  it('shipments и refusals сохраняют смысл (все srid с плечом; два и более плеч) — инвариант V1 не меняется', () => {
    expect(flat).toContain('COUNT(*) AS n,');
    expect(flat).toContain('COUNTIF(legs >= 2 AND NOT srx) AS ref');
    expect(flat).toMatch(/\(SELECT n FROM ov\), \(SELECT n FROM rt\), \(SELECT n FROM sx\), ROUND\(SUM\(srsum\), 2\), \(SELECT svc_rows FROM xt\), ROUND\(\(SELECT svc_sum FROM xt\), 2\), \(SELECT unk_srids FROM xt\), ROUND\(\(SELECT unk_sum FROM xt\), 2\) FROM a;?\s*$/);
    // UNKNOWN-строки логистики (23.10.2026): прибавляются к shipments строки магазина — fail-closed инварианта
    expect(flat).toContain('SELECT 0, SUM(n) + (SELECT unk_srids FROM xt), ROUND(SAFE_DIVIDE(SUM(fwd), NULLIF(SUM(n) - SUM(nt) - SUM(ns), 0)), 4)');
  });
});

describe('V_UNITKA_LOGISTICS_RATES: возврат неопознанного товара продавцу (06.11.2026)', () => {
  it('класс seller_return расширен ровно одним точным значением bonusTypeName — без LIKE, префиксов и регистра', () => {
    const sr = /sr AS \(SELECT DISTINCT srid FROM b WHERE son IN \('Логистика', 'Доставка'\) AND bonus IN \(((?:'[^']*'(?:, )?)+)\)\)/.exec(flat)!;
    expect([...sr[1]!.matchAll(/'([^']*)'/g)].map((m) => m[1])).toEqual(['Возврат брака (К продавцу)', 'Возврат неопознанного товара (К продавцу)']);
    expect(flat).not.toMatch(/bonus\s+(NOT\s+)?LIKE|LOWER\(bonus|STARTS_WITH\(bonus|REGEXP_\w+\(bonus/i);
    expect(flat.match(/\bsr AS \(/g)).toHaveLength(1);                         // одна точка правды класса
  });
});

describe('V_UNITKA_LOGISTICS_RATES: возврат брака продавцу (27.10.2026)', () => {
  it('класс определён строго по bonusTypeName плеча, без допусков', () => {
    expect(flat).toContain("JSON_VALUE(raw_json, '$.bonusTypeName') AS bonus");
    expect(flat).toContain("sr AS (SELECT DISTINCT srid FROM b WHERE son IN ('Логистика', 'Доставка') AND bonus IN ('Возврат брака (К продавцу)', 'Возврат неопознанного товара (К продавцу)'))");
    expect(flat).toContain("(srx AND legs = 1 AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid NOT IN (SELECT srid FROM b WHERE son = 'Возврат')) AS sret");
    expect(flat).toContain("WHERE legs = 1 AND srid IN (SELECT srid FROM sr) AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid NOT IN (SELECT srid FROM b WHERE son = 'Возврат')");
  });
  it('srid с плечом «К продавцу» исключён из sales, refusals, пересечения и хвоста — другая форма не попадёт ни в один класс (fail-closed)', () => {
    expect(flat).toContain("sale AS (SELECT COUNT(DISTINCT srid) AS n FROM b WHERE son = 'Продажа' AND srid NOT IN (SELECT srid FROM sr) AND srid NOT IN (SELECT srid FROM lg WHERE cls = 'UNKNOWN'))");
    expect(flat).toContain('COUNTIF(legs >= 2 AND NOT srx) AS ref');
    expect(flat).toContain("WHERE legs >= 2 AND srid IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid NOT IN (SELECT srid FROM sr)");
    expect(flat).toContain('(NOT srx AND legs = 1');
  });
  it('плечо не в ставках, но и не теряется: отдельная сумма seller_return_sum (fwd + rev + seller_return_sum = все плечи окна)', () => {
    expect(flat).toContain('SUM(IF(sret, fwd, 0)) AS srsum');
    expect(flat).toContain('SUM(IF(tail OR sret, 0, fwd)) AS fwd');
  });
});

describe('V_UNITKA_LOGISTICS_RATES: плечо = поездка (23.10.2026, экспортное сопровождение)', () => {
  it('плечами считаются только строки с одной поездкой; услуга экспорта — отдельный класс; иное — UNKNOWN', () => {
    expect(flat).toContain("WHEN da IS NOT NULL AND ra IS NOT NULL AND da >= 0 AND ra >= 0 AND da + ra = 1 THEN 'LEG'");
    expect(flat).toContain("WHEN da = 0 AND ra = 0 AND bonus = 'Услуга сопровождения экспортных операций' AND srid IN (SELECT srid FROM b WHERE son = 'Продажа') THEN 'SERVICE'");
    expect(flat).toContain("ELSE 'UNKNOWN' END AS cls");
    expect(flat).toContain("ROW_NUMBER() OVER (PARTITION BY srid ORDER BY amt DESC) AS rn, COUNT(*) OVER (PARTITION BY srid) AS legs FROM lg WHERE cls = 'LEG'");
  });
  it('суммы вне плеч видны: service_* и unclassified_* у SKU и магазина', () => {
    for (const c of ['service_rows', 'service_sum', 'unclassified_srids', 'unclassified_sum']) expect(flat).toContain(`AS ${c}`);
  });
});
