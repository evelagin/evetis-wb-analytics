import { describe, it, expect } from 'vitest';
import { d1Moscow } from '../src/loaders/mart/targetDate.js';

describe('d1Moscow — D-1 Europe/Moscow (блокер #3)', () => {
  it('утро МСК: 03.08 05:00Z (08:00 МСК) → D-1 = 2026-08-02', () => {
    expect(d1Moscow(new Date('2026-08-03T05:00:00Z'))).toBe('2026-08-02');
  });
  it('пересечение суток по МСК: 02.08 22:00Z (03.08 01:00 МСК) → D-1 = 2026-08-02', () => {
    expect(d1Moscow(new Date('2026-08-02T22:00:00Z'))).toBe('2026-08-02');
  });
  it('переход месяца: 01.08 00:30 МСК (31.07 21:30Z) → D-1 = 2026-07-31', () => {
    expect(d1Moscow(new Date('2026-07-31T21:30:00Z'))).toBe('2026-07-31');
  });
});
