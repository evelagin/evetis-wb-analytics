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
    expect(q).toContain("control_status=IF(status='COMPLETE', control_status, 'REUSED')");
    expect(q).toContain('written_rows=@written');
    // НЕ трогает исходные метрики:
    expect(q).not.toContain('expected_rows');
    expect(q).not.toContain('unique_nm_ids');
    expect(q).not.toContain('sum_quantity');
  });
});

describe('PR-Mig1c: manifestFinalize с T5 и без', () => {
  const metrics = {
    expected_rows: 3, unique_nm_ids: 2, warehouses_count: 1, qty_positive_rows: 3, qty_zero_rows: 0,
    aggregate_warehouse_rows: 0, anonymized_warehouse_rows: 3, sum_quantity_all_t6: 14, sum_quantity_physical_t6: 14,
    distinct_keys: 3, duplicate_keys: 0, unmatched_nm_ids: ['999'],
  };

  it('без T5 (STOCKS_T5_MODE=off) — прежний набор колонок: новые колонки не упоминаются', async () => {
    const { fake, sb } = bq();
    await sb.manifestFinalize('WB_STOCKS_SNAPSHOTS__CR', 'S', 'COMPLETE', metrics, 3, 'NOT_RUN', '');
    const q = fake.last();
    expect(q).toContain('control_delta=NULL');
    for (const col of ['t5_control_sum', 't6_comparable', 'availability_status', 'anonymized_warehouse_rows', 't5_rows_written']) {
      expect(q).not.toContain(col);
    }
    expect(fake.queries.at(-1)!.params!.control).toBe('NOT_RUN');
  });

  it('с T5 — все колонки контроля, NULL-параметры типизированы, статус берётся из контроля', async () => {
    const calls: Array<{ types?: Record<string, string>; params?: Record<string, unknown> }> = [];
    const fake2 = new FakeBq();
    const orig = fake2.query.bind(fake2);
    fake2.query = async (o) => { calls.push({ types: (o as { types?: Record<string, string> }).types, params: o.params }); return orig(o); };
    const sb = new StocksBq('proj', 'EU', 'wb_raw', fake2);
    await sb.manifestFinalize('WB_STOCKS_SNAPSHOTS__CR', 'S', 'COMPLETE', metrics, 3, 'NOT_RUN', '', {
      control: {
        t5_control_sum: null, t5_wb_rf_sum: null, t5_named_sum: null, control_status: 'T5_UNAVAILABLE', control_delta: null,
        t6_comparable: 14, availability_gap: null, availability_gap_prev: null, availability_gap_delta: null,
        availability_ratio: null, availability_status: 'T5_UNAVAILABLE',
      },
      anonymizedWarehouseRows: 3,
      t5RowsWritten: 0,
    });
    const q = fake2.last();
    for (const col of ['control_delta=@cdelta', 'anonymized_warehouse_rows=@anon', 't5_control_sum=@t5sum', 't5_wb_rf_sum=@t5agg',
      't5_named_sum=@t5named', 't5_rows_written=@t5rows', 't6_comparable=@t6cmp', 'availability_gap=@agap',
      'availability_gap_prev=@agapprev', 'availability_gap_delta=@agapdelta', 'availability_ratio=@aratio', 'availability_status=@astatus']) {
      expect(q).toContain(col);
    }
    expect(q).not.toContain('control_delta=NULL');
    const c = calls.at(-1)!;
    expect(c.params!.control).toBe('T5_UNAVAILABLE');
    expect(c.types).toMatchObject({ t5sum: 'INT64', aratio: 'FLOAT64', astatus: 'STRING', agapdelta: 'INT64' });
    expect(c.params).toMatchObject({ anon: 3, t5rows: 0, t6cmp: 14, astatus: 'T5_UNAVAILABLE' });
    // каждый NULL-параметр обязан иметь тип, иначе клиент BigQuery откажет
    for (const [k, v] of Object.entries(c.params!)) if (v === null) expect(c.types![k], k).toBeDefined();
  });

  it('с T5: каждое значение контроля уходит в СВОЮ колонку (защита от перестановки параметров)', async () => {
    const calls: Array<{ query: string; params?: Record<string, unknown> }> = [];
    const fake3 = new FakeBq();
    fake3.query = async (o) => { calls.push({ query: o.query, params: o.params }); return [[]]; };
    const sb = new StocksBq('proj', 'EU', 'wb_raw', fake3);
    const control = {
      t5_control_sum: 101, t5_wb_rf_sum: 102, t5_named_sum: 103, control_status: 'MISMATCH', control_delta: 104,
      t6_comparable: 105, availability_gap: 106, availability_gap_prev: 107, availability_gap_delta: 108,
      availability_ratio: 0.109, availability_status: 'DEGRADED',
    };
    await sb.manifestFinalize('T', 'S', 'COMPLETE', metrics, 3, 'NOT_RUN', '', { control, anonymizedWarehouseRows: 110, t5RowsWritten: 111 });
    const { query, params } = calls.at(-1)!;
    const valueOf = (col: string) => {
      const m = query.match(new RegExp(`\\b${col}=@(\\w+)`));
      expect(m, col).not.toBeNull();
      return params![m![1]!];
    };
    expect({
      t5_control_sum: valueOf('t5_control_sum'), t5_wb_rf_sum: valueOf('t5_wb_rf_sum'), t5_named_sum: valueOf('t5_named_sum'),
      control_status: valueOf('control_status'), control_delta: valueOf('control_delta'), t6_comparable: valueOf('t6_comparable'),
      availability_gap: valueOf('availability_gap'), availability_gap_prev: valueOf('availability_gap_prev'),
      availability_gap_delta: valueOf('availability_gap_delta'), availability_ratio: valueOf('availability_ratio'),
      availability_status: valueOf('availability_status'),
    }).toEqual(control);
    expect(valueOf('anonymized_warehouse_rows')).toBe(110);
    expect(valueOf('t5_rows_written')).toBe(111);
  });

  it('prevAvailabilityGap читает ту же manifest-таблицу и только COMPLETE с непустым gap', async () => {
    const { fake, sb } = bq();
    expect(await sb.prevAvailabilityGap('WB_STOCKS_SNAPSHOTS__CR')).toBeNull();
    const q = fake.last();
    expect(q).toContain('`proj.wb_raw.WB_STOCKS_SNAPSHOTS__CR`');
    expect(q).toContain("status='COMPLETE' AND availability_gap IS NOT NULL");
    expect(q).toContain('ORDER BY period_to DESC, completed_at DESC, snapshot_id DESC LIMIT 1');
  });
});
