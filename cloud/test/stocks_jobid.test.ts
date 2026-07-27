import { describe, it, expect } from 'vitest';
import { stocksLoadJobId } from '../src/loaders/stocks/jobId.js';

describe('stocksLoadJobId (детерминизм load job)', () => {
  it('одинаковые env+period+table → одинаковый jobId', () => {
    const a = stocksLoadJobId('shadow', '2026-07-27', 'RAW_WB_STOCKS__CR');
    const b = stocksLoadJobId('shadow', '2026-07-27', 'RAW_WB_STOCKS__CR');
    expect(a).toBe(b);
    expect(a).toBe('stocks_shadow_20260727_raw_wb_stocks_cr');
    expect(a).not.toContain('undefined');
  });
  it('разный environment → разный jobId', () => {
    expect(stocksLoadJobId('shadow', '2026-07-27', 'RAW_WB_STOCKS__CR'))
      .not.toBe(stocksLoadJobId('prod', '2026-07-27', 'RAW_WB_STOCKS__CR'));
  });
  it('разный logicalPeriod → разный jobId', () => {
    expect(stocksLoadJobId('shadow', '2026-07-27', 'RAW_WB_STOCKS__CR'))
      .not.toBe(stocksLoadJobId('shadow', '2026-07-28', 'RAW_WB_STOCKS__CR'));
  });
  it('разная targetTable → разный jobId', () => {
    expect(stocksLoadJobId('shadow', '2026-07-27', 'RAW_WB_STOCKS__CR'))
      .not.toBe(stocksLoadJobId('shadow', '2026-07-27', 'RAW_WB_STOCKS'));
  });
});
