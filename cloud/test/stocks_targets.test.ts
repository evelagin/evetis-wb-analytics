import { describe, it, expect } from 'vitest';
import { assertStocksTargets, WB_STOCKS_PROD_TARGETS } from '../src/loaders/stocks/targets.js';
import { loadConfig } from '../src/config.js';

/**
 * Mig1b-0 — контур ↔ целевые таблицы, fail-closed ДО любой записи.
 * Главный сценарий: prod-Job получил args=["stocks"], а боевых STOCKS_* в env нет —
 * без проверки он взял бы теневые значения по умолчанию.
 */
const base = { ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw' };
const PROD_ENV = {
  STOCKS_RAW_TABLE: 'RAW_WB_STOCKS', STOCKS_SNAPSHOT_TABLE: 'WB_STOCKS_SNAPSHOTS',
  STOCKS_T5_TABLE: 'RAW_WB_STOCKS_T5', STOCKS_T5_MODE: 'on',
};
const err = (env: Record<string, string>) => {
  try { assertStocksTargets(loadConfig({ ...base, ...env })); return null; } catch (e) { return e as { code?: string; message: string }; }
};

describe('assertStocksTargets — prod', () => {
  it('боевой env проходит', () => {
    expect(err(PROD_ENV)).toBeNull();
  });
  it('пустой env prod (значения по умолчанию = __CR) — STOCKS_TARGET_MISMATCH', () => {
    const e = err({});
    expect(e?.code).toBe('STOCKS_TARGET_MISMATCH');
    expect(e?.message).toContain('STOCKS_RAW_TABLE=RAW_WB_STOCKS__CR');
  });
  it('любая теневая или чужая таблица в prod — отказ', () => {
    expect(err({ ...PROD_ENV, STOCKS_T5_TABLE: 'RAW_WB_STOCKS_T5__CR' })?.code).toBe('STOCKS_TARGET_MISMATCH');
    expect(err({ ...PROD_ENV, STOCKS_SNAPSHOT_TABLE: 'WB_STOCKS_SNAPSHOTS_BAK_20260816' })?.code).toBe('STOCKS_TARGET_MISMATCH');
  });
  it('prod без T5 — отказ (prod повторяет Apps Script целиком)', () => {
    const e = err({ ...PROD_ENV, STOCKS_T5_MODE: 'off' });
    expect(e?.code).toBe('STOCKS_TARGET_MISMATCH');
    expect(e?.message).toContain('STOCKS_T5_MODE=on');
  });
  it('боевые имена совпадают с таблицами Apps Script', () => {
    expect(WB_STOCKS_PROD_TARGETS).toEqual({ raw: 'RAW_WB_STOCKS', snapshot: 'WB_STOCKS_SNAPSHOTS', t5: 'RAW_WB_STOCKS_T5' });
  });
});

describe('assertStocksTargets — shadow', () => {
  const sh = (env: Record<string, string>) => err({ ENVIRONMENT: 'shadow', ...env });
  it('значения по умолчанию (__CR) проходят, с T5 и без', () => {
    expect(sh({})).toBeNull();
    expect(sh({ STOCKS_T5_MODE: 'on' })).toBeNull();
  });
  it('shadow с боевой таблицей — отказ', () => {
    expect(sh({ STOCKS_RAW_TABLE: 'RAW_WB_STOCKS' })?.code).toBe('STOCKS_TARGET_MISMATCH');
    expect(sh({ STOCKS_T5_MODE: 'on', STOCKS_T5_TABLE: 'RAW_WB_STOCKS_T5' })?.code).toBe('STOCKS_TARGET_MISMATCH');
  });
});
