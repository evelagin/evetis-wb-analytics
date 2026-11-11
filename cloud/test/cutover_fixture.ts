/** Этап C: хранилище координатора в памяти — та же семантика CAS (epoch + state + revision), что у BigQuery. */
import type { Stream } from '../src/ownership/streams.js';
import type {
  CasExpect, CasNext, CloudRun, CutoverLogEntry, CutoverStore, OwnershipRow, PendingJob, RunRecord,
} from '../src/loaders/cutover/store.js';
import type { QualRow } from '../src/loaders/cutover/qualification.js';

export const T0 = Date.parse('2026-11-16T07:00:00Z'); // пн 10:00 МСК
export const MIN = 60_000;
export const H = 3_600_000;
export const DAY = 24 * H;

export function seedRow(stream: Stream, o: Partial<OwnershipRow> = {}): OwnershipRow {
  return {
    stream, owner: 'APPS_SCRIPT', state: 'LEGACY_ACTIVE', epoch: 1, revision: 0,
    legacyWatermark: null, cloudWatermark: null, autoCutoverEnabled: false,
    fencedAtMs: null, activeSinceMs: null, changedAtMs: T0 - 30 * DAY, changedBy: 'seed', reason: 'seed', ...o,
  };
}

export class MemCutoverStore implements CutoverStore {
  rows = new Map<Stream, OwnershipRow>();
  log: CutoverLogEntry[] = [];
  qual: Array<QualRow & { stream: Stream }> = [];
  legacy: Array<RunRecord & { stream: Stream }> = [];
  cloud: Array<CloudRun & { stream: Stream }> = [];
  watermark: Partial<Record<Stream, string | null>> = {};
  violations = 0;
  dups = 0;
  downstream = { mart: false, ref_active: false };
  casCalls = 0;
  /** Барьер: CAS ждёт, пока не наберётся столько чтений (гонка двух tick'ов). */
  readBarrier: { need: number; seen: number; release?: () => void; wait?: Promise<void> } | null = null;

  constructor(...rows: OwnershipRow[]) {
    for (const r of rows) this.rows.set(r.stream, { ...r });
  }

  armRace(need: number): void {
    const b: NonNullable<MemCutoverStore['readBarrier']> = { need, seen: 0 };
    b.wait = new Promise<void>((res) => { b.release = res; });
    this.readBarrier = b;
  }

  async read(stream: Stream): Promise<OwnershipRow | null> {
    const r = this.rows.get(stream);
    const snap = r ? { ...r } : null;
    const b = this.readBarrier;
    if (b) { b.seen++; if (b.seen >= b.need) b.release?.(); }
    return snap;
  }

  async cas(expect: CasExpect, next: CasNext, log: CutoverLogEntry): Promise<boolean> {
    this.casCalls++;
    if (this.readBarrier) await this.readBarrier.wait;
    const cur = this.rows.get(expect.stream);
    if (!cur || cur.epoch !== expect.epoch || cur.state !== expect.state || cur.revision !== expect.revision) return false;
    this.rows.set(expect.stream, {
      ...cur, owner: next.owner, state: next.state, epoch: next.epoch, revision: next.revision,
      legacyWatermark: next.legacyWatermark, cloudWatermark: next.cloudWatermark,
      fencedAtMs: next.fencedAtMs, activeSinceMs: next.activeSinceMs,
      changedAtMs: Date.now(), changedBy: next.changedBy, reason: next.reason,
    });
    this.log.push(log);
    if (this.loseNextCasAck) { this.loseNextCasAck = false; throw new Error('socket hang up'); }
    return true;
  }

  async qualification(stream: Stream, sinceDate: string): Promise<QualRow[]> {
    return this.qual.filter((q) => q.stream === stream && q.businessDate >= sinceDate);
  }

  async legacyRuns(stream: Stream, fromMs: number, includeShared: boolean): Promise<RunRecord[]> {
    return this.legacy.filter((r) => r.stream === stream && r.startMs >= fromMs && (includeShared || !r.shared));
  }

  async cloudRuns(stream: Stream, fromMs: number): Promise<CloudRun[]> {
    return this.cloud.filter((r) => r.stream === stream && r.startMs >= fromMs).sort((a, b) => a.startMs - b.startMs);
  }

  async currentWatermark(stream: Stream): Promise<string | null> {
    return this.watermark[stream] ?? null;
  }

  async continuityViolations(): Promise<number> { return this.violations; }
  async duplicates(): Promise<number> { return this.dups; }
  async downstreamAlive(kind: 'mart' | 'ref_active'): Promise<boolean> { return this.downstream[kind]; }
  jobs: Array<PendingJob & { stream: Stream }> = [];
  async pendingWriteJobs(stream: Stream, fromMs: number): Promise<PendingJob[]> {
    return this.jobs.filter((j) => j.stream === stream && j.createdMs >= fromMs && j.state !== 'DONE');
  }
  /** Отметка, которую прочитал бы прогон со стартом beforeMs (statistics). */
  seenBefore: (beforeMs: number) => string | null = () => this.watermark.statistics ?? null;
  async statsWatermarkBefore(beforeMs: number): Promise<string | null> { return this.seenBefore(beforeMs); }
  /** Потерянный ответ CAS: транзакция применяется, ответ — исключение. */
  loseNextCasAck = false;

  /** Вердикты PASS по всем группам за n суток до даты end включительно. */
  passDays(stream: Stream, groups: string[], end: string, n: number, opts: { digest?: string; verdict?: string } = {}): void {
    const endMs = Date.parse(`${end}T00:00:00Z`);
    for (let i = 0; i < n; i++) {
      const d = new Date(endMs - i * DAY).toISOString().slice(0, 10);
      for (const g of groups) this.qual.push({ stream, businessDate: d, checkGroup: g, verdict: opts.verdict ?? 'PASS', imageDigest: opts.digest ?? 'sha256:aa', atMs: endMs + H });
    }
  }
}
