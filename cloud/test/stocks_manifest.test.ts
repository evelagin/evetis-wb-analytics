import { describe, it, expect } from 'vitest';
import { StocksBq, type BqLike } from '../src/loaders/stocks/bq.js';

/** Фейковый BQ-клиент: записывает выполненные query для проверки SQL-контракта. */
class FakeBq implements BqLike {
  public queries: Array<{ query: string; params?: Record<string, unknown> }> = [];
  async query(o: { query: string; params?: Record<string, unknown>; location?: string }): Promise<[unknown[]]> {
    this.queries.push({ query: o.query, params: o.params });
    return [[]];
  }
  dataset() {
    return { table: () => ({ load: async () => ({}) }) };
  }
  job() {
    return { get: async () => [{ metadata: { status: { state: 'DONE' } } }] as [{ metadata?: unknown }] };
  }
  last(): string { return this.queries[this.queries.length - 1]!.query; }
}

function bq() {
  const fake = new FakeBq();
  return { fake, sb: new StocksBq('proj', 'EU', 'wb_raw', fake) };
}

describe('manifest SQL-контракт (защита исходного COMPLETE)', () => {
  it('manifestStart НЕ сбрасывает существующий COMPLETE', async () => {
    const { fake, sb } = bq();
    await sb.manifestStart('WB_STOCKS_SNAPSHOTS__CR', 'STOCK_SNAP_shadow_20260727', '2026-07-27T06:00:00Z', '2026-07-27', '2026-07-27');
    const q = fake.last();
    expect(q).toContain("WHEN MATCHED AND T.status != 'COMPLETE' THEN");
    expect(q).toContain('WHEN NOT MATCHED THEN INSERT');
  });

  it('manifestFinalize(ERROR) трогает только STARTED (COMPLETE не затирается)', async () => {
    const { fake, sb } = bq();
    await sb.manifestFinalize('WB_STOCKS_SNAPSHOTS__CR', 'STOCK_SNAP_shadow_20260727', 'ERROR', null, 0, 'NOT_RUN', 'fetch failed');
    const q = fake.last();
    expect(q).toContain("status='STARTED'");
  });

  it('manifestMarkReused допускает COMPLETE и НЕ перезаписывает исходные метрики', async () => {
    const { fake, sb } = bq();
    await sb.manifestMarkReused('WB_STOCKS_SNAPSHOTS__CR', 'STOCK_SNAP_shadow_20260727', 100);
    const q = fake.last();
    expect(q).toContain("status IN ('STARTED', 'COMPLETE')");
    expect(q).toContain("control_status='REUSED'");
    expect(q).toContain('written_rows=@written');
    // НЕ трогает исходные метрики:
    expect(q).not.toContain('expected_rows');
    expect(q).not.toContain('unique_nm_ids');
    expect(q).not.toContain('sum_quantity');
  });
});
