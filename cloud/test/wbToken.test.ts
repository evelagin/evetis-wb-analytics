import { describe, expect, it, vi } from 'vitest';
import {
  WB_TOKEN_EXPIRING_SOON, assertWbTokenUsable, isWbAuthStatus, redactTokenLike, tokenExpiryMs, wbAuthError, wbTokenState,
} from '../src/http/wbToken.js';
import { classifyFailure } from '../src/failure.js';
import type { Logger } from '../src/logging.js';

const NOW = Date.parse('2026-11-10T12:00:00Z');
const b64url = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url');
/** Синтетический JWT: подпись — мусор, проверяется только чтение exp. */
const jwt = (payload: unknown) => `${b64url({ alg: 'ES256', typ: 'JWT' })}.${b64url(payload)}.c2lnbmF0dXJlLXN5bnRoZXRpYw`;
const expIn = (days: number) => jwt({ exp: Math.floor((NOW + days * 86_400_000) / 1000), id: 'synthetic' });

function fakeLogger() {
  const warn = vi.fn();
  return { logger: { warn, info: vi.fn(), error: vi.fn(), debug: vi.fn() } as unknown as Logger, warn };
}

describe('tokenExpiryMs', () => {
  it('читает exp из JWT, в т.ч. с префиксом Bearer и пробелами', () => {
    const t = jwt({ exp: 1_790_000_000 });
    expect(tokenExpiryMs(t)).toBe(1_790_000_000_000);
    expect(tokenExpiryMs(`  Bearer ${t}\n`)).toBe(1_790_000_000_000);
  });
  it('не JWT, битый base64, без exp, exp<=0 → null', () => {
    expect(tokenExpiryMs('opaque-token')).toBeNull();
    expect(tokenExpiryMs('a.!!!.c')).toBeNull();
    expect(tokenExpiryMs(jwt({ id: 1 }))).toBeNull();
    expect(tokenExpiryMs(jwt({ exp: 0 }))).toBeNull();
    expect(tokenExpiryMs(jwt({ exp: 'soon' }))).toBeNull();
  });
});

describe('wbTokenState', () => {
  it('EXPIRED / EXPIRING_SOON / OK / NO_EXPIRY', () => {
    expect(wbTokenState(expIn(-1), NOW).kind).toBe('EXPIRED');
    expect(wbTokenState(expIn(0), NOW).kind).toBe('EXPIRED');
    expect(wbTokenState(expIn(5), NOW)).toMatchObject({ kind: 'EXPIRING_SOON', daysLeft: 5 });
    expect(wbTokenState(expIn(13.5), NOW).kind).toBe('EXPIRING_SOON');
    expect(wbTokenState(expIn(14), NOW).kind).toBe('OK');
    expect(wbTokenState(expIn(30), NOW, 45).kind).toBe('EXPIRING_SOON');
    expect(wbTokenState('opaque', NOW).kind).toBe('NO_EXPIRY');
  });
});

describe('assertWbTokenUsable', () => {
  it('истёкший токен — детерминированный <PREFIX>_TOKEN_EXPIRED без значения токена', () => {
    const t = expIn(-10);
    let err: unknown;
    try { assertWbTokenUsable(t, 'WB_TOKEN_STATISTICS', { codePrefix: 'WB_STATS', nowMs: NOW }); } catch (e) { err = e; }
    expect(classifyFailure(err)).toEqual({ code: 'WB_STATS_TOKEN_EXPIRED', category: 'DETERMINISTIC' });
    const msg = (err as Error).message;
    expect(msg).toContain('WB_TOKEN_STATISTICS');
    expect(msg).toContain('2026-10-31');
    expect(msg).not.toContain(t.split('.')[1]!);
  });
  it('пустой секрет — <PREFIX>_AUTH', () => {
    expect(() => assertWbTokenUsable('  \n', 'WB_TOKEN_ADS', { codePrefix: 'WB_ADS', nowMs: NOW }))
      .toThrow(expect.objectContaining({ code: 'WB_ADS_AUTH' }));
  });
  it('скоро истекает — warn с кодом WB_TOKEN_EXPIRING_SOON, без значения токена', () => {
    const { logger, warn } = fakeLogger();
    const t = expIn(3);
    const st = assertWbTokenUsable(t, 'WB_TOKEN_FINANCE', { codePrefix: 'WB_FINANCE', nowMs: NOW, logger });
    expect(st.kind).toBe('EXPIRING_SOON');
    expect(warn).toHaveBeenCalledTimes(1);
    const [msg, ctx] = warn.mock.calls[0]!;
    expect(msg).toBe('wb_token_expiring');
    expect(ctx).toMatchObject({ code: WB_TOKEN_EXPIRING_SOON, secret: 'WB_TOKEN_FINANCE', daysLeft: 3 });
    expect(JSON.stringify(ctx)).not.toContain(t.split('.')[1]!);
  });
  it('годный токен и токен без exp — без предупреждения', () => {
    const { logger, warn } = fakeLogger();
    expect(assertWbTokenUsable(expIn(60), 'S', { codePrefix: 'P', nowMs: NOW, logger }).kind).toBe('OK');
    expect(assertWbTokenUsable('opaque', 'S', { codePrefix: 'P', nowMs: NOW, logger }).kind).toBe('NO_EXPIRY');
    expect(warn).not.toHaveBeenCalled();
  });
});

describe('wbAuthError', () => {
  it('401 и 403 — детерминированный <PREFIX>_AUTH с разной подсказкой', () => {
    expect(isWbAuthStatus(401) && isWbAuthStatus(403) && !isWbAuthStatus(429) && !isWbAuthStatus(500)).toBe(true);
    const e401 = wbAuthError('WB_SUPPLIES', 'WB_TOKEN_SUPPLIES', 401, 'supplies', '{"title":"unauthorized","detail":"token is expired"}');
    const e403 = wbAuthError('WB_SUPPLIES', 'WB_TOKEN_SUPPLIES', 403, 'supplies');
    expect(classifyFailure(e401)).toEqual({ code: 'WB_SUPPLIES_AUTH', category: 'DETERMINISTIC' });
    expect(e401.message).toContain('истёк или отозван');
    expect(e401.message).toContain('token is expired');
    expect(e403.message).toContain('нет категории доступа');
  });
  it('токеноподобное из тела ответа вырезается, тело обрезается', () => {
    const t = expIn(1);
    const e = wbAuthError('WB_ADS', 'WB_TOKEN_ADS', 401, 'adv', `bad token ${t} Bearer ${t} ${'x'.repeat(500)}`);
    expect(e.message).not.toContain(t);
    expect(e.message).not.toContain(t.split('.')[1]!);
    expect(e.message.length).toBeLessThan(500);
    expect(redactTokenLike(`Authorization: Bearer abc.def`)).toBe('Authorization: Bearer [скрыто]');
  });
});
