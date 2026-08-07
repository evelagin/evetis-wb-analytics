import { describe, it, expect } from 'vitest';
import {
  isActive,
  classifyReason,
  normalizeBqTimestamp,
  DEFAULT_STALE_STARTED_MS,
  type AcquireParams,
  type AcquireResult,
  type FinalizePatch,
  type ManifestKey,
  type ManifestRow,
  type ManifestStore,
} from '../src/bq/runManifest.js';

/**
 * In-memory реализация с той же семантикой, что и BQ-транзакция: acquire атомарен
 * относительно последовательных вызовов (моделирует взаимное исключение).
 */
class MemManifestStore implements ManifestStore {
  rows: ManifestRow[] = [];
  private k(r: ManifestKey) {
    return `${r.environment}|${r.loaderName}|${r.logicalPeriod}`;
  }
  async acquire(p: AcquireParams): Promise<AcquireResult> {
    const mine = this.rows.filter((r) => this.k(r) === this.k(p));
    const active = mine.filter((r) => isActive(r, p.nowMs, p.staleMs));
    const latest = mine.length ? [...mine].sort((a, b) => Date.parse(b.startedAt) - Date.parse(a.startedAt))[0]! : null;
    if (active.length === 0) {
      this.rows.push({
        environment: p.environment,
        loaderName: p.loaderName,
        logicalPeriod: p.logicalPeriod,
        runId: p.runId,
        status: 'STARTED',
        startedAt: new Date(p.nowMs).toISOString(),
      });
      return { acquired: true, runId: p.runId, recovered: latest !== null };
    }
    return { acquired: false, reason: classifyReason(latest) };
  }
  async finalize(key: ManifestKey, runId: string, patch: FinalizePatch): Promise<void> {
    const r = this.rows.find((x) => this.k(x) === this.k(key) && x.runId === runId);
    if (r) r.status = patch.status;
  }
}

const NOW = Date.parse('2026-07-24T12:00:00Z');
const params = (env: string, runId: string, period = '2026-07-24'): AcquireParams => ({
  environment: env,
  loaderName: 'wb-stocks',
  logicalPeriod: period,
  runId,
  executionId: 'exec',
  imageDigest: 'sha256:abc',
  gitSha: 'deadbeef',
  nowMs: NOW,
  staleMs: DEFAULT_STALE_STARTED_MS,
});

describe('isActive', () => {
  const row = (status: ManifestRow['status'], startedAt: string): ManifestRow => ({
    environment: 'prod', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24', runId: 'r', status, startedAt,
  });
  it('COMPLETE активен', () => expect(isActive(row('COMPLETE', new Date(NOW).toISOString()), NOW, DEFAULT_STALE_STARTED_MS)).toBe(true));
  it('свежий STARTED активен', () => expect(isActive(row('STARTED', new Date(NOW - 60_000).toISOString()), NOW, DEFAULT_STALE_STARTED_MS)).toBe(true));
  it('устаревший STARTED НЕ активен', () => expect(isActive(row('STARTED', new Date(NOW - 60 * 60_000).toISOString()), NOW, DEFAULT_STALE_STARTED_MS)).toBe(false));
  it('ERROR НЕ активен', () => expect(isActive(row('ERROR', new Date(NOW).toISOString()), NOW, DEFAULT_STALE_STARTED_MS)).toBe(false));
});

describe('acquire (атомарный guard)', () => {
  it('первый запуск захватывает lock', async () => {
    const s = new MemManifestStore();
    const r = await s.acquire(params('prod', 'r1'));
    expect(r.acquired).toBe(true);
  });
  it('второй параллельный запуск не захватывает (ALREADY_RUNNING)', async () => {
    const s = new MemManifestStore();
    await s.acquire(params('prod', 'r1'));
    const r2 = await s.acquire(params('prod', 'r2'));
    expect(r2).toEqual({ acquired: false, reason: 'ALREADY_RUNNING' });
  });
  it('после COMPLETE — OK_NO_NEW (reason COMPLETE)', async () => {
    const s = new MemManifestStore();
    const r1 = await s.acquire(params('prod', 'r1'));
    if (r1.acquired) await s.finalize({ environment: 'prod', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24' }, r1.runId, { status: 'COMPLETE' });
    const r2 = await s.acquire(params('prod', 'r2'));
    expect(r2).toEqual({ acquired: false, reason: 'COMPLETE' });
  });
  it('РЕКАВЕРИ: после устаревшего STARTED новый запуск захватывает (recovered=true)', async () => {
    const s = new MemManifestStore();
    // старый STARTED, свежесть просрочена
    s.rows.push({ environment: 'prod', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24', runId: 'old', status: 'STARTED', startedAt: new Date(NOW - 60 * 60_000).toISOString() });
    const r = await s.acquire(params('prod', 'rNew'));
    expect(r).toEqual({ acquired: true, runId: 'rNew', recovered: true });
  });
  it('после ERROR — новый запуск захватывает (повтор)', async () => {
    const s = new MemManifestStore();
    s.rows.push({ environment: 'prod', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24', runId: 'old', status: 'ERROR', startedAt: new Date(NOW - 1000).toISOString() });
    const r = await s.acquire(params('prod', 'rNew'));
    expect(r.acquired).toBe(true);
  });
});

// PR-Mart3b-2: РЕКАВЕРИ по контракту — устаревший STARTED НЕ обновляется на месте, а
// ВСТАВЛЯЕТСЯ НОВАЯ строка со своим run_id (см. runManifest.ts fix №4). Ниже — явное
// доказательство «insert-new-row»: старая строка сохраняется как есть, finalize адресует
// ТОЛЬКО новую по run_id, поэтому история попыток за период не теряется.
describe('РЕКАВЕРИ вставляет НОВУЮ строку (не update-in-place)', () => {
  const key: ManifestKey = { environment: 'prod', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24' };
  const staleStartedAt = new Date(NOW - 60 * 60_000).toISOString();

  it('после устаревшего STARTED появляются ДВЕ строки; старая нетронута', async () => {
    const s = new MemManifestStore();
    s.rows.push({ ...key, runId: 'old', status: 'STARTED', startedAt: staleStartedAt });

    const r = await s.acquire(params('prod', 'rNew'));
    expect(r).toEqual({ acquired: true, runId: 'rNew', recovered: true });

    // именно ВСТАВКА: было 1 → стало 2; оба run_id присутствуют
    expect(s.rows).toHaveLength(2);
    expect(s.rows.map((x) => x.runId).sort()).toEqual(['old', 'rNew']);

    // старая строка не изменилась (status и started_at сохранены — не перезаписаны)
    const old = s.rows.find((x) => x.runId === 'old')!;
    expect(old).toMatchObject({ status: 'STARTED', startedAt: staleStartedAt });

    // новая строка — свежий STARTED со своим run_id
    const fresh = s.rows.find((x) => x.runId === 'rNew')!;
    expect(fresh.status).toBe('STARTED');
  });

  it('finalize адресует ТОЛЬКО новый run_id; старая строка остаётся STARTED', async () => {
    const s = new MemManifestStore();
    s.rows.push({ ...key, runId: 'old', status: 'STARTED', startedAt: staleStartedAt });
    const r = await s.acquire(params('prod', 'rNew'));
    if (!r.acquired) throw new Error('recovery must acquire');

    await s.finalize(key, r.runId, { status: 'COMPLETE' });

    expect(s.rows.find((x) => x.runId === 'rNew')!.status).toBe('COMPLETE');
    // «висящая» старая попытка НЕ трогается finalize'ом по новому run_id
    expect(s.rows.find((x) => x.runId === 'old')!.status).toBe('STARTED');
  });
});

describe('REQUIRED FIX 1 — окружение в ключе', () => {
  it('shadow COMPLETE НЕ блокирует prod за тот же период', async () => {
    const s = new MemManifestStore();
    const r1 = await s.acquire(params('shadow', 's1'));
    if (r1.acquired) await s.finalize({ environment: 'shadow', loaderName: 'wb-stocks', logicalPeriod: '2026-07-24' }, r1.runId, { status: 'COMPLETE' });
    const r2 = await s.acquire(params('prod', 'p1'));
    expect(r2.acquired).toBe(true);
  });
});

describe('normalizeBqTimestamp (фикс №9)', () => {
  it('строка → как есть', () => expect(normalizeBqTimestamp('2026-07-24T00:00:00Z')).toBe('2026-07-24T00:00:00Z'));
  it('BigQueryTimestamp { value } → value', () => expect(normalizeBqTimestamp({ value: '2026-07-24T06:00:00Z' })).toBe('2026-07-24T06:00:00Z'));
  it('Date → ISO', () => expect(normalizeBqTimestamp(new Date('2026-07-24T06:00:00Z'))).toBe('2026-07-24T06:00:00.000Z'));
  it('null/undefined → null', () => {
    expect(normalizeBqTimestamp(null)).toBeNull();
    expect(normalizeBqTimestamp(undefined)).toBeNull();
  });
});
