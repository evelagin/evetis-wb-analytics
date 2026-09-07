import { describe, it, expect } from 'vitest';
import { PricesBq, isAlreadyExists, type BqLike } from '../src/loaders/prices/bq.js';
import type { RawPriceRow } from '../src/loaders/prices/normalize.js';

function row(nm: number): RawPriceRow {
  return {
    observed_at: '2026-09-07T10:20:05.000Z', observation_bucket: '2026-09-07T10:20',
    observation_id: 'WBPX_prod_202609071020', environment: 'prod', run_id: 'r1',
    nm_id: nm, internal_sku: 'EVT-X', vendor_code: 'v', size_id: 1, tech_size_name: '0',
    seller_list_price: 1600, seller_discount_pct: 60, seller_effective_price: 640,
    wb_club_discount_pct: 0, wb_club_price: 640, currency_code: 'RUB', editable_size_price: false,
    raw_item_json: '{}', source_endpoint: 'GET /api/v2/list/goods/filter',
    source_payload_hash: 'h', ingested_at: '2026-09-07T10:20:06.000Z',
  };
}

/** Фейковый BQ: считает load-джобы и умеет изображать конфликт jobId. */
function fakeBq(opts: { conflictOnJobIds?: Set<string> } = {}) {
  const loads: Array<{ jobId: string; table: string }> = [];
  const queries: string[] = [];
  const bq: BqLike = {
    async query(o) { queries.push(o.query); return [[{ n: 25 }]]; },
    dataset() {
      return {
        table(tableId: string) {
          return {
            async load(_src: string, md: { jobId?: string }) {
              const jobId = String(md.jobId);
              if (opts.conflictOnJobIds?.has(jobId)) {
                const e = new Error('Already Exists: Job') as Error & { code?: number };
                e.code = 409;
                throw e;
              }
              loads.push({ jobId, table: tableId });
              return {};
            },
          };
        },
      };
    },
  };
  return { bq, loads, queries };
}

describe('appendRaw: append-only и идемпотентность', () => {
  it('обычная запись → LOADED, одна load-джоба', async () => {
    const f = fakeBq();
    const bq = new PricesBq('p', 'EU', 'wb_raw', f.bq);
    const out = await bq.appendRaw('RAW_WB_PRICES', [row(1), row(2)], 'job-a');
    expect(out).toBe('LOADED');
    expect(f.loads).toEqual([{ jobId: 'job-a', table: 'RAW_WB_PRICES' }]);
  });

  it('повтор того же окна (тот же jobId) → REUSED, дубля НЕТ', async () => {
    const f = fakeBq({ conflictOnJobIds: new Set(['job-a']) });
    const bq = new PricesBq('p', 'EU', 'wb_raw', f.bq);
    const out = await bq.appendRaw('RAW_WB_PRICES', [row(1)], 'job-a');
    expect(out).toBe('REUSED');
    expect(f.loads).toHaveLength(0);
  });

  it('следующее окно (другой jobId) пишется, даже если данные идентичны', async () => {
    // Это и есть требование «неизменившаяся цена всё равно сохраняется».
    const f = fakeBq({ conflictOnJobIds: new Set(['job-a']) });
    const bq = new PricesBq('p', 'EU', 'wb_raw', f.bq);
    expect(await bq.appendRaw('RAW_WB_PRICES', [row(1)], 'job-a')).toBe('REUSED');
    expect(await bq.appendRaw('RAW_WB_PRICES', [row(1)], 'job-b')).toBe('LOADED');
    expect(f.loads.map((l) => l.jobId)).toEqual(['job-b']);
  });

  it('ошибка, не связанная с дублем, пробрасывается', async () => {
    const bq = new PricesBq('p', 'EU', 'wb_raw', {
      async query() { return [[]]; },
      dataset() { return { table() { return { async load() { throw new Error('quota exceeded'); } }; } }; },
    });
    await expect(bq.appendRaw('RAW_WB_PRICES', [row(1)], 'job-x')).rejects.toThrow(/quota/);
  });

  it('пустой набор строк не создаёт load-джобу', async () => {
    const f = fakeBq();
    const bq = new PricesBq('p', 'EU', 'wb_raw', f.bq);
    expect(await bq.appendRaw('RAW_WB_PRICES', [], 'job-empty')).toBe('LOADED');
    expect(f.loads).toHaveLength(0);
  });
});

describe('isAlreadyExists: распознавание конфликта jobId', () => {
  it('код 409 и текстовое «already exists»', () => {
    const e = new Error('x') as Error & { code?: number };
    e.code = 409;
    expect(isAlreadyExists(e)).toBe(true);
    expect(isAlreadyExists(new Error('Already Exists: Job foo'))).toBe(true);
  });
  it('прочие ошибки — не конфликт', () => {
    expect(isAlreadyExists(new Error('permission denied'))).toBe(false);
  });
});

describe('appendRaw: запись идёт только в переданную таблицу', () => {
  it('не пишет ни в какую другую таблицу', async () => {
    const f = fakeBq();
    const bq = new PricesBq('p', 'EU', 'wb_raw', f.bq);
    await bq.appendRaw('RAW_WB_PRICES', [row(1)], 'job-a');
    expect(new Set(f.loads.map((l) => l.table))).toEqual(new Set(['RAW_WB_PRICES']));
  });
});
