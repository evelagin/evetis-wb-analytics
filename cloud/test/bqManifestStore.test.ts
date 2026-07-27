import { describe, it, expect } from 'vitest';
import { BqManifestStore, type ManifestKey } from '../src/bq/runManifest.js';
import type { BqClient } from '../src/bq/client.js';

/**
 * Регрессия fix/mig1-bq-typed-null-params.
 *
 * Баг: BqManifestStore.finalize() слал в BigQuery null-параметры БЕЗ явных типов.
 *  - COMPLETE-ветка (cli.ts): errorCode/errorMessage не задаются → null.
 *  - ERROR-ветка (cli.ts): rowsFetched/rowsLoaded не задаются → null.
 * Node-клиент BigQuery не выводит тип для null → query() падает, строка LOADER_RUNS
 * остаётся STARTED, guard 30 мин отдаёт ALREADY_RUNNING, а ретрай Cloud Run
 * «зеленеет», ничего не загрузив.
 *
 * Тесты проверяют КОНТРАКТ: finalize передаёт третьим аргументом types, и у
 * КАЖДОГО параметра (в т.ч. null) есть объявленный тип.
 */

interface QueryCall {
  sql: string;
  params?: Record<string, unknown>;
  types?: Record<string, string>;
}

/** Шпион вместо BqClient: фиксирует (sql, params, types) каждого query(). */
class SpyBq {
  projectId = 'proj';
  location = 'EU';
  calls: QueryCall[] = [];
  async query(
    sql: string,
    params?: Record<string, unknown>,
    types?: Record<string, string>,
  ): Promise<unknown[]> {
    this.calls.push({ sql, params, types });
    return [];
  }
  last(): QueryCall {
    return this.calls[this.calls.length - 1]!;
  }
}

function makeStore() {
  const spy = new SpyBq();
  const store = new BqManifestStore(spy as unknown as BqClient, 'wb_raw', 'LOADER_RUNS');
  return { spy, store };
}

const KEY: ManifestKey = {
  environment: 'shadow',
  loaderName: 'wb-stocks',
  logicalPeriod: '2026-07-27',
};

describe('BqManifestStore.finalize — типизированные null-параметры', () => {
  it('COMPLETE: errorCode/errorMessage = null получают тип STRING', async () => {
    const { spy, store } = makeStore();
    await store.finalize(KEY, 'run1', { status: 'COMPLETE', rowsFetched: 158, rowsLoaded: 158 });
    const c = spy.last();
    expect(c.params).toMatchObject({
      status: 'COMPLETE',
      errorCode: null,
      errorMessage: null,
      rowsFetched: 158,
      rowsLoaded: 158,
    });
    expect(c.types).toBeDefined();
    expect(c.types!.errorCode).toBe('STRING');
    expect(c.types!.errorMessage).toBe('STRING');
    expect(c.types!.rowsFetched).toBe('INT64');
    expect(c.types!.rowsLoaded).toBe('INT64');
  });

  it('ERROR: rowsFetched/rowsLoaded = null получают тип INT64', async () => {
    const { spy, store } = makeStore();
    await store.finalize(KEY, 'run1', {
      status: 'ERROR',
      errorCode: 'WB_T6_HTTP',
      errorMessage: 'T6 HTTP 401: unauthorized',
    });
    const c = spy.last();
    expect(c.params).toMatchObject({
      status: 'ERROR',
      errorCode: 'WB_T6_HTTP',
      errorMessage: 'T6 HTTP 401: unauthorized',
      rowsFetched: null,
      rowsLoaded: null,
    });
    expect(c.types!.rowsFetched).toBe('INT64');
    expect(c.types!.rowsLoaded).toBe('INT64');
    expect(c.types!.errorCode).toBe('STRING');
    expect(c.types!.errorMessage).toBe('STRING');
  });

  it('инвариант: у КАЖДОГО параметра finalize есть объявленный тип (нет нетипизированных null)', async () => {
    const { spy, store } = makeStore();
    await store.finalize(KEY, 'run1', { status: 'COMPLETE', rowsFetched: 1, rowsLoaded: 1 });
    const c = spy.last();
    const paramKeys = Object.keys(c.params ?? {});
    expect(paramKeys.length).toBeGreaterThan(0);
    for (const k of paramKeys) {
      expect(c.types, `нет типа для параметра @${k}`).toHaveProperty(k);
    }
  });
});
