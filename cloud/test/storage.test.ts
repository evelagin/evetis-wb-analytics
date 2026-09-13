import { describe, it, expect } from 'vitest';
import { storageWindow } from '../src/loaders/storage/window.js';
import { normalizePaidStorage } from '../src/loaders/storage/normalize.js';

describe('UNITKA E4 paid storage', () => {
  it('8-day overlap ending D-1=2026-09-12 includes required 10-12 Sep backfill', () => {
    expect(storageWindow('2026-09-12', 8)).toEqual({
      startDate: '2026-09-05',
      endDate: '2026-09-12',
    });
  });

  it('rejects overlap outside WB supported 1..8 days', () => {
    expect(() => storageWindow('2026-09-12', 0)).toThrow();
    expect(() => storageWindow('2026-09-12', 9)).toThrow();
  });

  it('normalizes only requested dated SKU rows and preserves storage amount', () => {
    const src = [
      {
        date: '2026-09-10', nmId: 111, chrtId: 222, barcode: '4601', warehouse: 'Коледино',
        officeId: 507, warehouseCoef: 1, logWarehouseCoef: 1, subject: 'Крем', brand: 'EVETIS',
        vendorCode: 'SKU-1', volume: 0.3, calcType: 'короб', warehousePrice: 12.34,
        barcodesCount: 2, palletPlaceCode: 0, palletCount: 0, loyaltyDiscount: 0,
        tariffFixDate: '', tariffLowerDate: '',
      },
      { date: '2026-09-11T00:00:00', nmId: '111', warehousePrice: '5.66' },
      { date: '2026-09-09', nmId: 111, warehousePrice: 999 },
      { date: '2026-09-12', nmId: null, warehousePrice: 999 },
    ];

    const out = normalizePaidStorage(src, {
      observationId: 'WBPS_1',
      runId: 'run-1',
      observedAtIso: '2026-09-13T10:00:00.000Z',
      startDate: '2026-09-10',
      endDate: '2026-09-12',
    });

    expect(out.rows).toHaveLength(2);
    expect(out.rejected).toBe(2);
    expect(out.days).toEqual(['2026-09-10', '2026-09-11']);
    expect(out.nmIds).toEqual([111]);
    expect(out.storageRub).toBeCloseTo(18, 8);
    expect(out.rows[0]).toMatchObject({ date_msk: '2026-09-10', nm_id: 111, warehouse_price: 12.34 });
    expect(JSON.parse(out.rows[0].raw_row_json)).toMatchObject({ vendorCode: 'SKU-1' });
  });
});
