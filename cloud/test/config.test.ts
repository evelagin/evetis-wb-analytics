import { describe, it, expect } from 'vitest';
import { loadConfig } from '../src/config.js';

const base = {
  GCP_PROJECT_ID: 'project-x',
  BQ_RAW_DATASET: 'wb_raw',
  ENVIRONMENT: 'shadow',
};

describe('loadConfig', () => {
  it('валидный shadow-конфиг', () => {
    const c = loadConfig(base);
    expect(c.environment).toBe('shadow');
    expect(c.bqLocation).toBe('EU');
    expect(c.manifestTable).toBe('LOADER_RUNS');
  });
  it('неверный ENVIRONMENT → ошибка', () => {
    expect(() => loadConfig({ ...base, ENVIRONMENT: 'staging' })).toThrow();
  });
  it('отсутствие обязательной переменной → ошибка', () => {
    expect(() => loadConfig({ ENVIRONMENT: 'prod' })).toThrow();
  });
});
