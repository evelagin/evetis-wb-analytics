import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractT6Array, validateT6, normalizeT6, type NormalizeCtx } from '../src/loaders/stocks/normalize.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, 'fixtures', 'wb_stocks_t6.json'), 'utf8'));

const ctx: NormalizeCtx = {
  snapshotId: 'STOCK_SNAP_TEST',
  snapshotTsIso: '2026-07-27T06:23:00.000Z',
  snapshotDate: '2026-07-27',
  loadId: 'STOCK_LOAD_TEST',
  sourceApi: 'WB_API_ANALYTICS_STOCKS',
  skuByNm: new Map<number, string>([[101, 'SKU-A']]), // 999 намеренно не сопоставлен
};

describe('extractT6Array', () => {
  it('достаёт data.items', () => {
    const arr = extractT6Array(fixture);
    expect(Array.isArray(arr)).toBe(true);
    expect(arr!.length).toBe(3);
  });
  it('поддерживает плоский массив и data[]', () => {
    expect(extractT6Array([{ a: 1 }])!.length).toBe(1);
    expect(extractT6Array({ data: [{ a: 1 }] })!.length).toBe(1);
    expect(extractT6Array({ foo: 1 })).toBeNull();
  });
});

describe('validateT6', () => {
  it('валидный пакет → ok, distinct=rows', () => {
    const v = validateT6(extractT6Array(fixture));
    expect(v.ok).toBe(true);
    expect(v.distinctKeys).toBe(3);
    expect(v.duplicateKeys).toBe(0);
  });
  it('пустой массив → ERROR (не нулевой остаток)', () => {
    expect(validateT6([]).ok).toBe(false);
  });
  it('дубль ключа nm|chrt|wh → ERROR', () => {
    const dupData = [
      { nmId: 1, chrtId: 1, warehouseId: 1, quantity: 0, inWayToClient: 0, inWayFromClient: 0 },
      { nmId: 1, chrtId: 1, warehouseId: 1, quantity: 0, inWayToClient: 0, inWayFromClient: 0 },
    ];
    const v = validateT6(dupData);
    expect(v.ok).toBe(false);
    expect(v.error).toMatch(/дубли ключа/);
  });
  it('отрицательный quantity → ERROR', () => {
    expect(validateT6([{ nmId: 1, chrtId: 1, warehouseId: 1, quantity: -1, inWayToClient: 0, inWayFromClient: 0 }]).ok).toBe(false);
  });
});

describe('normalizeT6 (replay/fixture parity 1:1)', () => {
  const { rows, metrics } = normalizeT6(extractT6Array(fixture)!, ctx);

  it('строки: количество и ключевые поля', () => {
    expect(rows.length).toBe(3);
    const r0 = rows[0]!;
    expect(r0.nm_id).toBe(101);
    expect(r0.warehouse_id).toBe(10);
    expect(r0.quantity).toBe(5);
    expect(r0.is_aggregate_warehouse).toBe(false);
    expect(r0.internal_sku).toBe('SKU-A');
    expect(r0.sku_match_status).toBe('matched');
    expect(r0._snapshot_date).toBe('2026-07-27');
    expect(r0.source_api).toBe('WB_API_ANALYTICS_STOCKS');
  });

  it('агрегатный склад (warehouseId=0 / «Остальные») помечен', () => {
    const agg = rows.find((r) => r.warehouse_id === 0)!;
    expect(agg.is_aggregate_warehouse).toBe(true);
  });

  it('unmapped SKU → not_found, попал в unmatched', () => {
    const unm = rows.find((r) => r.nm_id === 999)!;
    expect(unm.sku_match_status).toBe('not_found');
    expect(unm.internal_sku).toBe('');
    expect(metrics.unmatched_nm_ids).toContain('999');
  });

  it('метрики точны', () => {
    expect(metrics).toMatchObject({
      expected_rows: 3,
      unique_nm_ids: 2,
      warehouses_count: 2,
      qty_positive_rows: 3,
      qty_zero_rows: 0,
      aggregate_warehouse_rows: 1,
      sum_quantity_all_t6: 10,
      sum_quantity_physical_t6: 7, // 5 + 2, агрегатные 3 исключены
      distinct_keys: 3,
      duplicate_keys: 0,
    });
    expect(metrics.unmatched_nm_ids.sort()).toEqual(['999']);
  });
});
