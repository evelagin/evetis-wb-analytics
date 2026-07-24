import { describe, it, expect } from 'vitest';
import {
  decideRun,
  DEFAULT_STALE_STARTED_MS,
  type ManifestKey,
  type ManifestRecord,
  type ManifestStore,
} from '../src/bq/runManifest.js';

/** In-memory реализация ManifestStore для тестов (без BigQuery). */
class MemStore implements ManifestStore {
  rows: ManifestRecord[] = [];
  private k(r: ManifestKey) {
    return `${r.environment}|${r.loaderName}|${r.logicalPeriod}`;
  }
  async getCurrent(key: ManifestKey): Promise<ManifestRecord | null> {
    const match = this.rows.filter((r) => this.k(r) === this.k(key));
    return match.length ? match[match.length - 1]! : null;
  }
  async insertStarted(rec: ManifestRecord): Promise<void> {
    const active = this.rows.find((r) => this.k(r) === this.k(rec) && r.status === 'STARTED');
    if (active) return; // MERGE WHEN NOT MATCHED — не вставляем второй STARTED
    this.rows.push({ ...rec });
  }
  async finalize(key: ManifestKey, runId: string, patch: Partial<ManifestRecord> & { status: ManifestRecord['status'] }) {
    const r = this.rows.find((x) => this.k(x) === this.k(key) && x.runId === runId);
    if (r) Object.assign(r, patch);
  }
}

function rec(env: string, period = '2026-07-24', status: ManifestRecord['status'] = 'STARTED', startedAt?: string): ManifestRecord {
  return {
    environment: env,
    loaderName: 'wb-stocks',
    logicalPeriod: period,
    runId: `${env}:wb-stocks:${period}`,
    executionId: 'exec1',
    imageDigest: 'sha256:abc',
    gitSha: 'deadbeef',
    status,
    attemptCount: 1,
    startedAt: startedAt ?? new Date().toISOString(),
  };
}

describe('decideRun', () => {
  const now = Date.parse('2026-07-24T12:00:00Z');
  it('нет строки → PROCEED', () => {
    expect(decideRun({ current: null, nowMs: now, staleStartedMs: DEFAULT_STALE_STARTED_MS }).action).toBe('PROCEED');
  });
  it('COMPLETE → OK_NO_NEW', () => {
    expect(decideRun({ current: rec('prod', '2026-07-24', 'COMPLETE'), nowMs: now, staleStartedMs: DEFAULT_STALE_STARTED_MS }).action).toBe('OK_NO_NEW');
  });
  it('свежий STARTED → ALREADY_RUNNING', () => {
    const started = new Date(now - 60_000).toISOString();
    expect(decideRun({ current: rec('prod', '2026-07-24', 'STARTED', started), nowMs: now, staleStartedMs: DEFAULT_STALE_STARTED_MS }).action).toBe('ALREADY_RUNNING');
  });
  it('устаревший STARTED → RECOVER', () => {
    const started = new Date(now - 60 * 60_000).toISOString();
    expect(decideRun({ current: rec('prod', '2026-07-24', 'STARTED', started), nowMs: now, staleStartedMs: DEFAULT_STALE_STARTED_MS }).action).toBe('RECOVER');
  });
  it('ERROR → PROCEED (повтор)', () => {
    expect(decideRun({ current: rec('prod', '2026-07-24', 'ERROR'), nowMs: now, staleStartedMs: DEFAULT_STALE_STARTED_MS }).action).toBe('PROCEED');
  });
});

describe('REQUIRED FIX 1 — окружение в ключе', () => {
  it('shadow COMPLETE НЕ блокирует prod за тот же период', async () => {
    const store = new MemStore();
    await store.insertStarted(rec('shadow', '2026-07-24', 'STARTED'));
    await store.finalize({ environment: 'shadow', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24' }, 'shadow:wb-stocks:2026-07-24', { status: 'COMPLETE' });

    const prodCurrent = await store.getCurrent({ environment: 'prod', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24' });
    expect(prodCurrent).toBeNull();
    const decision = decideRun({ current: prodCurrent, nowMs: Date.parse('2026-07-24T12:00:00Z'), staleStartedMs: DEFAULT_STALE_STARTED_MS });
    expect(decision.action).toBe('PROCEED');
  });
});
