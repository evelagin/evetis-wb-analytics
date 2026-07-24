import { describe, it, expect } from 'vitest';
import { isRetryableStatus, computeBackoffMs } from '../src/http/wbHttp.js';

describe('isRetryableStatus', () => {
  it('429 и 5xx — повторяемы', () => {
    expect(isRetryableStatus(429)).toBe(true);
    expect(isRetryableStatus(500)).toBe(true);
    expect(isRetryableStatus(503)).toBe(true);
  });
  it('2xx/4xx (кроме 429) — не повторяемы', () => {
    expect(isRetryableStatus(200)).toBe(false);
    expect(isRetryableStatus(400)).toBe(false);
    expect(isRetryableStatus(404)).toBe(false);
  });
});

describe('computeBackoffMs', () => {
  it('экспонента с верхним лимитом', () => {
    expect(computeBackoffMs(1, 1000, 21000)).toBe(1000);
    expect(computeBackoffMs(2, 1000, 21000)).toBe(2000);
    expect(computeBackoffMs(3, 1000, 21000)).toBe(4000);
    expect(computeBackoffMs(10, 1000, 21000)).toBe(21000);
  });
});
