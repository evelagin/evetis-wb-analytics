import { describe, it, expect } from 'vitest';
import { evaluatePostLoad, planFinalize } from '../src/loaders/stocks/postLoad.js';
import type { StockMetrics } from '../src/loaders/stocks/normalize.js';
import { stocksDataSnapshotId } from '../src/loaders/stocks/jobId.js';

describe('stocksDataSnapshotId (детерминизм набора данных)', () => {
  it('стабилен для env+period', () => {
    expect(stocksDataSnapshotId('shadow', '2026-07-27')).toBe('STOCK_SNAP_shadow_20260727');
    expect(stocksDataSnapshotId('shadow', '2026-07-27')).toBe(stocksDataSnapshotId('shadow', '2026-07-27'));
  });
  it('различается по env и period', () => {
    expect(stocksDataSnapshotId('shadow', '2026-07-27')).not.toBe(stocksDataSnapshotId('prod', '2026-07-27'));
    expect(stocksDataSnapshotId('shadow', '2026-07-27')).not.toBe(stocksDataSnapshotId('shadow', '2026-07-28'));
  });
});

describe('evaluatePostLoad', () => {
  it('LOADED + count==distinct==expected → COMPLETE (не reused)', () => {
    expect(evaluatePostLoad('LOADED', 150, 150, 150)).toEqual({ ok: true, reused: false });
  });
  it('LOADED + count!=expected → ERROR POSTCOUNT', () => {
    const d = evaluatePostLoad('LOADED', 149, 149, 150);
    expect(d).toMatchObject({ ok: false, code: 'STOCKS_POSTCOUNT' });
  });
  it('ALREADY_LOADED + строки на месте → COMPLETE (reused), даже если re-fetch дал другой expected', () => {
    // ключевой сценарий аудита: повтор с новым fetch (expected=148), но уже загружено 150
    expect(evaluatePostLoad('ALREADY_LOADED', 150, 150, 148)).toEqual({ ok: true, reused: true });
  });
  it('ALREADY_LOADED + 0 строк → ERROR (ложный success недопустим)', () => {
    const d = evaluatePostLoad('ALREADY_LOADED', 0, 0, 150);
    expect(d).toMatchObject({ ok: false, code: 'STOCKS_REUSE_EMPTY' });
  });
  it('count!=distinct → ERROR (дубли), в любом режиме', () => {
    expect(evaluatePostLoad('LOADED', 150, 149, 150)).toMatchObject({ ok: false, code: 'STOCKS_POSTCOUNT_DUP' });
    expect(evaluatePostLoad('ALREADY_LOADED', 150, 149, 150)).toMatchObject({ ok: false, code: 'STOCKS_POSTCOUNT_DUP' });
  });
});

const M: StockMetrics = {
  expected_rows: 120, unique_nm_ids: 40, warehouses_count: 5, qty_positive_rows: 100,
  qty_zero_rows: 20, aggregate_warehouse_rows: 3, sum_quantity_all_t6: 999,
  sum_quantity_physical_t6: 900, distinct_keys: 120, duplicate_keys: 0, unmatched_nm_ids: [],
};

describe('planFinalize (целостность manifest при REUSED)', () => {
  it('reused=true → markReused, метрики нового fetch НЕ передаются (пишется только written)', () => {
    // сценарий аудита: первая загрузка written=100, повторный fetch expected=120
    const plan = planFinalize(true, M, 100);
    expect(plan).toEqual({ kind: 'markReused', written: 100 });
    // в плане нет поля metrics → expected_rows/суммы исходной загрузки не перезапишутся
    expect('metrics' in plan).toBe(false);
  });
  it('reused=false → finalize с метриками текущей загрузки', () => {
    const plan = planFinalize(false, M, 120);
    expect(plan.kind).toBe('finalize');
    if (plan.kind === 'finalize') {
      expect(plan.metrics).toBe(M);
      expect(plan.written).toBe(120);
      expect(plan.controlStatus).toBe('NOT_RUN');
    }
  });
});
