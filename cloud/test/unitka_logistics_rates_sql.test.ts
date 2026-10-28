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
    expect(flat).toContain('CAST(NULL AS INT64) AS sale_refusal_overlap, nt AS return_tail, ns AS seller_return, ROUND(srsum, 2) AS seller_return_sum FROM a UNION ALL');
  });
  it('shipments и refusals сохраняют смысл (все srid с плечом; два и более плеч) — инвариант V1 не меняется', () => {
    expect(flat).toContain('COUNT(*) AS n,');
    expect(flat).toContain('COUNTIF(legs >= 2 AND NOT srx) AS ref');
    expect(flat).toMatch(/\(SELECT n FROM ov\), \(SELECT n FROM rt\), \(SELECT n FROM sx\), ROUND\(SUM\(srsum\), 2\) FROM a;?\s*$/);
  });
});

describe('V_UNITKA_LOGISTICS_RATES: возврат брака продавцу (27.10.2026)', () => {
  it('класс определён строго по bonusTypeName плеча, без допусков', () => {
    expect(flat).toContain("JSON_VALUE(raw_json, '$.bonusTypeName') AS bonus");
    expect(flat).toContain("sr AS (SELECT DISTINCT srid FROM b WHERE son IN ('Логистика', 'Доставка') AND bonus = 'Возврат брака (К продавцу)')");
    expect(flat).toContain("(srx AND legs = 1 AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid NOT IN (SELECT srid FROM b WHERE son = 'Возврат')) AS sret");
    expect(flat).toContain("WHERE legs = 1 AND srid IN (SELECT srid FROM sr) AND srid NOT IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid NOT IN (SELECT srid FROM b WHERE son = 'Возврат')");
  });
  it('srid с плечом «К продавцу» исключён из sales, refusals, пересечения и хвоста — другая форма не попадёт ни в один класс (fail-closed)', () => {
    expect(flat).toContain("sale AS (SELECT COUNT(DISTINCT srid) AS n FROM b WHERE son = 'Продажа' AND srid NOT IN (SELECT srid FROM sr))");
    expect(flat).toContain('COUNTIF(legs >= 2 AND NOT srx) AS ref');
    expect(flat).toContain("WHERE legs >= 2 AND srid IN (SELECT srid FROM b WHERE son = 'Продажа') AND srid NOT IN (SELECT srid FROM sr)");
    expect(flat).toContain('(NOT srx AND legs = 1');
  });
  it('плечо не в ставках, но и не теряется: отдельная сумма seller_return_sum (fwd + rev + seller_return_sum = все плечи окна)', () => {
    expect(flat).toContain('SUM(IF(sret, fwd, 0)) AS srsum');
    expect(flat).toContain('SUM(IF(tail OR sret, 0, fwd)) AS fwd');
  });
});
