/**
 * Регрессия на боевой отказ 26.08.2026 (shadow rollout REJECTED_BY_GATE).
 *
 * PR #121 перевёл ключ грейна в normalize.ts с warehouse_id на warehouse_code,
 * но зеркальный ключ в пост-проверке bq.ts остался на warehouse_id. После
 * обезличивания складов warehouse_id = NULL, а в BigQuery CONCAT с NULL-аргументом
 * возвращает NULL и COUNT(DISTINCT NULL) = 0. Контроль получал count=23 distinct=0
 * и падал с STOCKS_POSTCOUNT_DUP, хотя все 23 строки уникальны.
 *
 * Тест воспроизводит именно эту семантику, а не форму запроса.
 */
import { describe, it, expect } from 'vitest';
import { StocksBq, STOCKS_GRAIN_KEY_SQL, type BqLike } from '../src/loaders/stocks/bq.js';
import { evaluatePostLoad } from '../src/loaders/stocks/postLoad.js';

type Row = { nm_id: number; chrt_id: number; warehouse_id: number | null; warehouse_code: string | null };

/** 23 обезличенные строки — ровно та форма, что WB отдаёт с 15.08.2026. */
const anonymizedRows: Row[] = Array.from({ length: 23 }, (_, i) => ({
  nm_id: 100 + i,
  chrt_id: 500 + i,
  warehouse_id: null,
  warehouse_code: '-999999',
}));

/**
 * Мини-эмулятор BigQuery для выражения ключа. Поддерживает CONCAT из частей вида
 * 'литерал' | CAST(col AS STRING) | IFNULL(col,'литерал').
 * Главное свойство, которое воспроизводится: NULL в любой части → результат NULL.
 */
function evalKey(expr: string, row: Row): string | null {
  const inner = expr.replace(/^CONCAT\(/, '').replace(/\)$/, '');
  const parts: string[] = [];
  let depth = 0, buf = '';
  for (const ch of inner) {
    if (ch === '(') depth++;
    if (ch === ')') depth--;
    if (ch === ',' && depth === 0) { parts.push(buf.trim()); buf = ''; continue; }
    buf += ch;
  }
  parts.push(buf.trim());

  const out: string[] = [];
  for (const p of parts) {
    let m = /^'(.*)'$/.exec(p);
    if (m) { out.push(m[1]); continue; }
    m = /^CAST\((\w+) AS STRING\)$/.exec(p);
    if (m) {
      const v = (row as unknown as Record<string, unknown>)[m[1]];
      if (v === null || v === undefined) return null;   // ← NULL-пропагация CONCAT
      out.push(String(v));
      continue;
    }
    m = /^IFNULL\((\w+),'(.*)'\)$/.exec(p);
    if (m) {
      const v = (row as unknown as Record<string, unknown>)[m[1]];
      out.push(v === null || v === undefined ? m[2] : String(v));
      continue;
    }
    throw new Error(`эмулятор не знает части ключа: ${p}`);
  }
  return out.join('');
}

function fakeBq(rows: Row[], capture?: { sql?: string }): BqLike {
  return {
    async query(opts) {
      capture && (capture.sql = opts.query);
      const m = /COUNT\(DISTINCT ([\s\S]+?)\) AS d/.exec(opts.query);
      if (!m) throw new Error('в запросе нет ключа COUNT(DISTINCT …)');
      const keys = rows.map((r) => evalKey(m[1].trim(), r));
      const distinct = new Set(keys.filter((k) => k !== null)).size; // COUNT(DISTINCT) игнорирует NULL
      return [[{ c: rows.length, d: distinct }]] as [unknown[]];
    },
    dataset: () => ({ table: () => ({ load: async () => undefined }) }),
    job: () => ({ get: async () => [{}] as [{ metadata?: unknown }] }),
  };
}

describe('ключ пост-проверки снимка остатков', () => {
  it('ключ построен на warehouse_code, а не на nullable warehouse_id', () => {
    expect(STOCKS_GRAIN_KEY_SQL).toContain('warehouse_code');
    expect(STOCKS_GRAIN_KEY_SQL).not.toContain('warehouse_id');
  });

  it('обезличенные строки: COUNT(*) == COUNT(DISTINCT key)', async () => {
    const cap: { sql?: string } = {};
    const bq = new StocksBq('p', 'EU', 'wb_raw', fakeBq(anonymizedRows, cap));
    const { count, distinct } = await bq.snapshotCounts('RAW_WB_STOCKS__CR', 'SNAP');
    expect(count).toBe(23);
    expect(distinct).toBe(23);
    expect(cap.sql).toContain('warehouse_code');
  });

  it('пост-проверка НЕ поднимает STOCKS_POSTCOUNT_DUP на обезличенном снимке', async () => {
    const bq = new StocksBq('p', 'EU', 'wb_raw', fakeBq(anonymizedRows));
    const { count, distinct } = await bq.snapshotCounts('RAW_WB_STOCKS__CR', 'SNAP');
    const decision = evaluatePostLoad('LOADED', count, distinct, 23);
    expect(decision.ok).toBe(true);
  });

  it('НЕГАТИВНЫЙ КОНТРОЛЬ: старый ключ на warehouse_id воспроизводит боевой отказ', async () => {
    const legacyKey =
      "CONCAT(CAST(nm_id AS STRING),'|',CAST(chrt_id AS STRING),'|',CAST(warehouse_id AS STRING))";
    const keys = anonymizedRows.map((r) => evalKey(legacyKey, r));
    const distinct = new Set(keys.filter((k) => k !== null)).size;
    expect(distinct).toBe(0); // ровно то, что видел прод: count=23 distinct=0
    const decision = evaluatePostLoad('LOADED', 23, distinct, 23);
    expect(decision.ok).toBe(false);
    expect(decision.ok === false && decision.code).toBe('STOCKS_POSTCOUNT_DUP');
  });

  it('поимённые склады (warehouse_id заполнен) продолжают работать', async () => {
    const named: Row[] = [
      { nm_id: 1, chrt_id: 1, warehouse_id: 10, warehouse_code: '10' },
      { nm_id: 1, chrt_id: 1, warehouse_id: 0, warehouse_code: '0' },
      { nm_id: 2, chrt_id: 5, warehouse_id: 10, warehouse_code: '10' },
    ];
    const bq = new StocksBq('p', 'EU', 'wb_raw', fakeBq(named));
    const { count, distinct } = await bq.snapshotCounts('RAW_WB_STOCKS__CR', 'SNAP');
    expect(count).toBe(3);
    expect(distinct).toBe(3);
  });
});
