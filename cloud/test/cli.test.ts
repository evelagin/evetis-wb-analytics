import { describe, it, expect } from 'vitest';
import { runCli, type CliDeps } from '../src/cli.js';
import { EXIT_OK, EXIT_ERROR } from '../src/errors.js';
import type { ManifestStore } from '../src/bq/runManifest.js';

/**
 * PR-Mart3b-2 REV2 (аудит, блокер #4): DRY_RUN=1 для prodOnly-загрузчика (mart) НЕ должен
 * ни брать lease (acquire), ни исполнять handler — иначе «сухой» прогон реально пересобрал бы
 * production wb_mart в обход lease. Инъекция CliDeps позволяет доказать это без облака.
 */

function baseEnv(overrides: Record<string, string>): NodeJS.ProcessEnv {
  return { GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', LOG_LEVEL: 'error', GIT_SHA: 'testsha', ...overrides };
}

function spies() {
  const calls = { makeStore: 0, runHandler: 0, acquire: 0, finalize: 0 };
  const store: ManifestStore = {
    acquire: async () => {
      calls.acquire++;
      return { acquired: true, runId: 'r', recovered: false };
    },
    finalize: async () => {
      calls.finalize++;
    },
  };
  const deps: CliDeps = {
    makeStore: () => {
      calls.makeStore++;
      return store;
    },
    runHandler: async () => {
      calls.runHandler++;
      return { rowsFetched: 1, rowsLoaded: 1 };
    },
    nowMs: () => 0,
  };
  return { calls, deps };
}

const argv = (loader: string) => ['node', 'cli.js', loader];

describe('runCli: DRY_RUN + prodOnly (mart) — блокер #4', () => {
  it('DRY_RUN=1 mart: НЕ зовёт acquire и НЕ зовёт handler; EXIT_OK (production BQ не тронут)', async () => {
    const { calls, deps } = spies();
    const code = await runCli(argv('mart'), baseEnv({ ENVIRONMENT: 'prod', DRY_RUN: '1' }), deps);
    expect(code).toBe(EXIT_OK);
    expect(calls.makeStore).toBe(0); // до acquire дело не доходит
    expect(calls.acquire).toBe(0);
    expect(calls.runHandler).toBe(0); // martLoader не вызван
  });

  it('DRY_RUN=1 stocks: handler исполняется (каркас), но lease НЕ берётся', async () => {
    const { calls, deps } = spies();
    const code = await runCli(argv('stocks'), baseEnv({ ENVIRONMENT: 'prod', DRY_RUN: '1' }), deps);
    expect(code).toBe(EXIT_OK);
    expect(calls.runHandler).toBe(1);
    expect(calls.makeStore).toBe(0);
    expect(calls.acquire).toBe(0);
  });
});

describe('runCli: prodOnly guard до lease', () => {
  it('mart + ENVIRONMENT=shadow (без DRY_RUN): EXIT_ERROR; ни store, ни handler', async () => {
    const { calls, deps } = spies();
    const code = await runCli(argv('mart'), baseEnv({ ENVIRONMENT: 'shadow' }), deps);
    expect(code).toBe(EXIT_ERROR);
    expect(calls.makeStore).toBe(0);
    expect(calls.runHandler).toBe(0);
  });

  it('unknown loader: EXIT_ERROR; store не создаётся', async () => {
    const { calls, deps } = spies();
    const code = await runCli(argv('nope'), baseEnv({ ENVIRONMENT: 'prod' }), deps);
    expect(code).toBe(EXIT_ERROR);
    expect(calls.makeStore).toBe(0);
  });
});

describe('runCli: обычный путь (acquire→handler→finalize)', () => {
  it('stocks prod: acquire+handler+finalize; EXIT_OK', async () => {
    const { calls, deps } = spies();
    const code = await runCli(argv('stocks'), baseEnv({ ENVIRONMENT: 'prod' }), deps);
    expect(code).toBe(EXIT_OK);
    expect(calls.acquire).toBe(1);
    expect(calls.runHandler).toBe(1);
    expect(calls.finalize).toBe(1);
  });
});
