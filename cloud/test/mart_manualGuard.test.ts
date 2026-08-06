import { describe, it, expect } from 'vitest';
import { assertManualRunAllowed, isCanonicalDate } from '../src/loaders/mart/manualGuard.js';

// 2026-08-03 08:00 МСК → D-1 = 2026-08-02
const NOW = new Date('2026-08-03T05:00:00Z');
const D1 = '2026-08-02';

describe('isCanonicalDate', () => {
  it('реальная дата — ок', () => expect(isCanonicalDate('2026-08-02')).toBe(true));
  it('2026-02-31 — отклоняется (regex прошёл бы)', () => expect(isCanonicalDate('2026-02-31')).toBe(false));
  it('2026-13-01 — отклоняется', () => expect(isCanonicalDate('2026-13-01')).toBe(false));
  it('не-дата по форме — отклоняется', () => expect(isCanonicalDate('2026-8-2')).toBe(false));
});

describe('assertManualRunAllowed (аудит REV4 #1 — fail-closed guards ручного прогона)', () => {
  it('ENVIRONMENT=shadow → MART_MANUAL_ENV (loader не должен вызываться)', () => {
    expect(() => assertManualRunAllowed('shadow', D1, NOW)).toThrow(/MART_MANUAL_ENV/);
  });
  it('targetDate != D-1 (историческая дата) → MART_MANUAL_TARGET', () => {
    expect(() => assertManualRunAllowed('prod', '2026-06-01', NOW)).toThrow(/MART_MANUAL_TARGET/);
  });
  it('targetDate = сегодня (не D-1) → MART_MANUAL_TARGET', () => {
    expect(() => assertManualRunAllowed('prod', '2026-08-03', NOW)).toThrow(/MART_MANUAL_TARGET/);
  });
  it('несуществующая календарная дата 2026-02-31 → MART_MANUAL_DATE', () => {
    expect(() => assertManualRunAllowed('prod', '2026-02-31', NOW)).toThrow(/MART_MANUAL_DATE/);
  });
  it('prod + корректный D-1 → допускается', () => {
    expect(() => assertManualRunAllowed('prod', D1, NOW)).not.toThrow();
  });
});
