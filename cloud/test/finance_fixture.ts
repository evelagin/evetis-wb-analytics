/**
 * M4 · Синтетические фикстуры финансового контура. Все номера отчётов, rrdId, srid и суммы
 * ВЫДУМАНЫ (репозиторий публичный): id начинаются с 9000…, srid — «synthetic-…».
 */
import type { FinanceStore, QueueItem, ManifestEntry, DiscoveredInput, WeeklyCandidate, DailySums, ReconRow } from '../src/loaders/finance/store.js';
import type { RawFinanceRow, RunCounters, RunStatus } from '../src/loaders/finance/normalize.js';
import type { Clock, FetchFn, HttpResponseLike } from '../src/loaders/finance/wbApi.js';
import { FIN_LIST_METRICS, FIN_DET_METRICS, type ListMetricCol, type DetMetricCol } from '../src/loaders/finance/constants.js';
import { weekStart } from '../src/loaders/finance/parse.js';
import { Logger } from '../src/logging.js';

/** Логгер без вывода (ожидаемые ERROR-события сценариев не засоряют вывод vitest). */
class SilentLogger extends Logger {
  override child(): Logger { return this; }
  override debug(): void {}
  override info(): void {}
  override warn(): void {}
  override error(): void {}
}
export const silentLogger: Logger = new SilentLogger();

/** Часы, у которых sleep двигает время мгновенно. */
export class FakeClock implements Clock {
  slept: number[] = [];
  constructor(public t: number) {}
  now(): number { return this.t; }
  async sleep(ms: number): Promise<void> { this.slept.push(ms); this.t += ms; }
}

// ── Синтетический WB Finance API ───────────────────────────────────────────

export interface SynthReport {
  reportId: string;        // строка цифр, может быть > 2^53
  reportType: number;
  period: 'daily' | 'weekly';
  dateFrom: string;
  dateTo: string;
  rows: Array<Record<string, unknown>>;
}

/** Строка detailed с BigInt-полями, записанными ЧИСЛОМ в JSON (как у WB). */
export function synthRow(rrdId: string, i: number, date: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    realizationreportId: 0,
    rrdId,              // будет сериализовано числом через rawJson()
    giId: '9100000000000000' + String(i).padStart(3, '0'),
    shkId: '9200000000000000' + String(i).padStart(3, '0'),
    srid: `synthetic-srid-${i}`,
    stickerId: String(70000 + i),
    docTypeName: 'Продажа',
    sellerOperName: 'Продажа',
    orderDt: `${date}T10:00:00+03:00`,
    saleDt: `${date}T12:00:00+03:00`,
    rrDate: date,
    createDate: date,
    nmId: 100000 + i,
    vendorCode: `SYN-${i}`,
    sku: `460000000000${i}`,
    techSize: '0',
    brandName: 'SYNTH',
    subjectName: 'Тест',
    officeName: 'Тестовый склад',
    country: 'Россия',
    retailPrice: 1000,
    retailAmount: 900.5,
    retailPriceWithDisc: 900.5,
    salePercent: 10,
    commissionPercent: 25.5,
    productDiscountForReport: 10,
    sellerPromo: 0,
    spp: 5,
    quantity: 1,
    forPay: 700.25,
    ppvzSalesCommission: 150,
    deliveryService: 50.1,
    paidStorage: 0,
    deduction: 0,
    penalty: 0,
    paidAcceptance: 0,
    additionalPayment: 0,
    acquiringFee: 10.2,
    currency: 'RUB',
    rebillLogisticCost: 0,
    ...extra,
  };
}

/** Сериализация как у WB: BigInt-поля без кавычек (числом). */
export function rawJson(v: unknown): string {
  return JSON.stringify(v).replace(/"(reportId|rrdId|giId|shkId)":"(-?\d+)"/g, '"$1":$2');
}

export function listSums(rows: Array<Record<string, unknown>>): Record<string, number> {
  const s = (k: string) => Math.round(rows.reduce((a, r) => a + Number(r[k] ?? 0), 0) * 100) / 100;
  return {
    forPaySum: s('forPay'), retailAmountSum: s('retailAmount'), deliveryServiceSum: s('deliveryService'),
    paidStorageSum: s('paidStorage'), paidAcceptanceSum: s('paidAcceptance'), deductionSum: s('deduction'), penaltySum: s('penalty'),
  };
}

type Responder = (path: string, body: string) => { status: number; body?: string; headers?: Record<string, string> } | undefined;

export class SynthFinanceApi {
  calls: Array<{ path: string; body: string; at: number; auth: string }> = [];
  /** Переопределение ответа (429/5xx/битое тело) на конкретный вызов. */
  overrides: Responder[] = [];
  listOverride: ((period: string) => Array<Record<string, unknown>>) | null = null;
  constructor(public reports: SynthReport[], private readonly clock: FakeClock, public pageLimit = 100000) {}

  fetch: FetchFn = async (url, init): Promise<HttpResponseLike> => {
    const path = url.replace(/^https?:\/\/[^/]+/, '');
    this.calls.push({ path, body: init.body, at: this.clock.now(), auth: init.headers.Authorization ?? '' });
    for (const o of this.overrides) {
      const r = o(path, init.body);
      if (r) return resp(r.status, r.body ?? '', r.headers ?? {});
    }
    if (path === '/api/finance/v1/sales-reports/list') {
      const req = JSON.parse(init.body) as { period: string; dateFrom: string; dateTo: string; offset: number };
      const items = this.listOverride
        ? this.listOverride(req.period)
        : this.reports
          .filter((r) => r.period === req.period && r.dateFrom <= req.dateTo && r.dateTo >= req.dateFrom)
          .map((r) => ({
            reportId: r.reportId, reportType: r.reportType, dateFrom: r.dateFrom, dateTo: r.dateTo,
            createDate: r.dateTo, ...listSums(r.rows),
          }));
      if (!items.length) return resp(204, '');
      return resp(200, rawJson({ data: items }));
    }
    const m = /^\/api\/finance\/v1\/sales-reports\/detailed\/(\d+)$/.exec(path);
    if (m) {
      const rep = this.reports.find((r) => r.reportId === m[1]);
      if (!rep) return resp(404, '{"error":"not found"}');
      const cursor = (/"rrdId":(\d+)/.exec(init.body) ?? [])[1] ?? '0';
      const rows = rep.rows.filter((r) => BigInt(String(r.rrdId)) > BigInt(cursor)).slice(0, this.pageLimit);
      if (!rows.length) return resp(204, '');
      return resp(200, rawJson(rows));
    }
    return resp(404, '');
  };
}

export function resp(status: number, body: string, headers: Record<string, string> = {}): HttpResponseLike {
  const h = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return { status, text: async () => body, headers: { get: (n: string) => h.get(n.toLowerCase()) ?? null } };
}

// ── Модель FinanceStore в памяти (семантика SQL bq.ts) ──────────────────────

interface LoadRow {
  report_id: string; report_period: 'DAILY' | 'WEEKLY'; report_type: number; date_from: string; date_to: string;
  status: string; discovered_run_id: string; processing_run_id: string | null; attempt_count: number;
  started_at: number | null; rows_fetched: number | null; rows_loaded: number | null;
  list: Record<ListMetricCol, number>; det: Record<DetMetricCol, number> | null; list_metrics_json: string; error_message: string | null;
}

export class MemFinanceStore implements FinanceStore {
  raw: RawFinanceRow[] = [];
  loads = new Map<string, LoadRow>();
  runs = new Map<string, { status: string; trigger_type: string; counters?: RunCounters; error_message?: string }>();
  weeks = new Map<string, { weekly_final: boolean; weekly_report_id: string | null; daily_days_loaded: number | null }>();
  recon = new Map<string, ReconRow & { recon_status: string }>();
  loadJobs = new Set<string>();
  ready = true;
  failOn: Partial<Record<keyof FinanceStore, Error>> = {};
  constructor(private readonly clock: FakeClock) {}

  private chk(name: keyof FinanceStore): void { const e = this.failOn[name]; if (e) throw e; }

  async assertReady(): Promise<void> { this.chk('assertReady'); if (!this.ready) throw new Error('not ready'); }
  async runOpen(runId: string, triggerType: string): Promise<void> { this.chk('runOpen'); this.runs.set(runId, { status: 'RUNNING', trigger_type: triggerType }); }
  async runClose(runId: string, status: RunStatus, c: RunCounters, errMsg: string): Promise<void> {
    const r = this.runs.get(runId); if (r) { r.status = status; r.counters = { ...c }; r.error_message = errMsg; }
  }
  async staleRecovery(staleMinutes: number): Promise<number> {
    let n = 0;
    for (const l of this.loads.values()) {
      if (l.status === 'STARTED' && l.started_at !== null && l.started_at < this.clock.now() - staleMinutes * 60_000) {
        l.status = 'DISCOVERED'; l.processing_run_id = null; l.started_at = null; l.error_message = 'stale STARTED recovered'; n++;
      }
    }
    return n;
  }
  async ensureWeekRows(cutover: string, last: string): Promise<void> {
    for (let d = new Date(`${cutover}T00:00:00Z`); d.toISOString().slice(0, 10) <= last; d = new Date(d.getTime() + 7 * 86400000)) {
      for (const rt of [1, 2]) {
        const k = `${d.toISOString().slice(0, 10)}|${rt}`;
        if (!this.weeks.has(k)) this.weeks.set(k, { weekly_final: false, weekly_report_id: null, daily_days_loaded: null });
      }
    }
  }
  async assertManifestPk(): Promise<void> { /* Map гарантирует уникальность */ }
  private item(l: LoadRow): QueueItem {
    return { report_id: l.report_id, report_period: l.report_period, report_type: l.report_type, date_from: l.date_from, date_to: l.date_to };
  }
  private sorted(f: (l: LoadRow) => boolean): QueueItem[] {
    return [...this.loads.values()].filter(f)
      .sort((a, b) => (a.date_from < b.date_from ? -1 : a.date_from > b.date_from ? 1 : a.report_id < b.report_id ? -1 : 1))
      .map((l) => this.item(l));
  }
  async queue(max: number): Promise<QueueItem[]> { return this.sorted((l) => l.status === 'DISCOVERED' || (l.status === 'ERROR' && l.attempt_count < max)); }
  async discovered(): Promise<QueueItem[]> { return this.sorted((l) => l.status === 'DISCOVERED'); }
  async finalWeeks(): Promise<Set<string>> { return new Set([...this.weeks].filter(([, w]) => w.weekly_final).map(([k]) => k)); }
  async hasOpenWeeks(): Promise<boolean> { return [...this.weeks.values()].some((w) => !w.weekly_final); }
  async manifestByIds(ids: string[]): Promise<Map<string, ManifestEntry>> {
    const m = new Map<string, ManifestEntry>();
    for (const id of ids) { const l = this.loads.get(id); if (l) m.set(id, { report_id: id, status: l.status, metrics: { ...l.list } }); }
    return m;
  }
  async mergeDiscovered(rep: DiscoveredInput, runId: string): Promise<void> {
    const ex = this.loads.get(rep.reportId);
    if (ex) { if (ex.status === 'DISCOVERED' || ex.status === 'ERROR') { ex.list = { ...rep.metrics }; ex.list_metrics_json = rep.metricsJson; } return; }
    this.loads.set(rep.reportId, {
      report_id: rep.reportId, report_period: rep.period, report_type: rep.reportType, date_from: rep.dateFrom, date_to: rep.dateTo,
      status: 'DISCOVERED', discovered_run_id: runId, processing_run_id: null, attempt_count: 0, started_at: null,
      rows_fetched: null, rows_loaded: null, list: { ...rep.metrics }, det: null, list_metrics_json: rep.metricsJson, error_message: null,
    });
  }
  async mergeStarted(id: string, runId: string, max: number): Promise<boolean> {
    const l = this.loads.get(id);
    if (!l || !(l.status === 'DISCOVERED' || l.status === 'ERROR') || !(l.attempt_count < max)) return false;
    l.status = 'STARTED'; l.processing_run_id = runId; l.started_at = this.clock.now(); l.attempt_count++; l.error_message = null;
    return true;
  }
  async mergeComplete(id: string, runId: string, rf: number, rl: number, det: Record<DetMetricCol, number>): Promise<boolean> {
    const l = this.loads.get(id);
    if (!l || l.status !== 'STARTED' || l.processing_run_id !== runId) return false;
    l.status = 'COMPLETE'; l.rows_fetched = rf; l.rows_loaded = rl; l.det = { ...det }; l.error_message = null;
    return true;
  }
  async mergeError(id: string, runId: string, msg: string): Promise<void> {
    const l = this.loads.get(id);
    if (l && l.status === 'STARTED' && l.processing_run_id === runId) { l.status = 'ERROR'; l.error_message = msg.substring(0, 900); }
  }
  async manifestDiag(id: string): Promise<string> { const l = this.loads.get(id); return `manifest: status=${l?.status}`; }
  async orphanPruned(id: string, runId: string, from: string, to: string): Promise<{ thisRun: number; otherRun: number }> {
    this.chk('orphanPruned');
    const rs = this.raw.filter((r) => r.report_id === id && typeof r._rr_date === 'string' && r._rr_date >= from && r._rr_date <= to);
    return { thisRun: rs.filter((r) => r.run_id === runId).length, otherRun: rs.filter((r) => r.run_id !== runId).length };
  }
  async orphanFull(id: string, from: string, to: string): Promise<{ full: number; nullRr: number; outside: number; runCount: number }> {
    const rs = this.raw.filter((r) => r.report_id === id);
    return {
      full: rs.length, nullRr: rs.filter((r) => r._rr_date === undefined).length,
      outside: rs.filter((r) => typeof r._rr_date === 'string' && (r._rr_date < from || r._rr_date > to)).length,
      runCount: new Set(rs.map((r) => r.run_id)).size,
    };
  }
  async loadRaw(rows: RawFinanceRow[], jobId: string): Promise<number> {
    this.chk('loadRaw');
    if (this.loadJobs.has(jobId)) return rows.length; // детерминированный jobId: повтор не дописывает
    this.loadJobs.add(jobId);
    // NDJSON-круг: как в BigQuery, строка проходит через JSON
    for (const r of rows) this.raw.push(JSON.parse(JSON.stringify(r)) as RawFinanceRow);
    return rows.length;
  }
  async postLoadThisRun(id: string, runId: string): Promise<{ count: number; distinct: number }> {
    const rs = this.raw.filter((r) => r.report_id === id && r.run_id === runId);
    return { count: rs.length, distinct: new Set(rs.map((r) => r.rrd_id)).size };
  }
  async postLoadCrossRun(id: string, from: string, to: string): Promise<{ count: number; distinct: number }> {
    const rs = this.raw.filter((r) => r.report_id === id && typeof r._rr_date === 'string' && r._rr_date >= from && r._rr_date <= to);
    return { count: rs.length, distinct: new Set(rs.map((r) => r.rrd_id)).size };
  }
  async finalizeCandidates(cutover: string): Promise<WeeklyCandidate[]> {
    return [...this.loads.values()]
      .filter((l) => l.status === 'COMPLETE' && l.report_period === 'WEEKLY' && l.date_from >= cutover
        && !this.weeks.get(`${weekStart(l.date_from)}|${l.report_type}`)?.weekly_final)
      .map((l) => ({ report_id: l.report_id, report_type: l.report_type, week_start: weekStart(l.date_from), metrics: { ...l.list } }));
  }
  async dailySums(rt: number, ws: string): Promise<DailySums> {
    const end = new Date(new Date(`${ws}T00:00:00Z`).getTime() + 6 * 86400000).toISOString().slice(0, 10);
    const ds = [...this.loads.values()].filter((l) => l.report_period === 'DAILY' && l.status === 'COMPLETE' && l.report_type === rt && l.date_from >= ws && l.date_from <= end);
    const metrics = {} as Record<ListMetricCol, number>;
    for (const [, col] of FIN_LIST_METRICS) metrics[col] = ds.reduce((a, l) => a + l.list[col], 0);
    return { days_loaded: ds.length, metrics };
  }
  async mergeRecon(ws: string, rt: number, rows: ReconRow[], tol: number): Promise<void> {
    for (const r of rows) this.recon.set(`${ws}|${rt}|${r.metric}`, { ...r, recon_status: Math.abs(r.sum_daily - r.sum_weekly) <= tol ? 'OK' : 'WARN' });
  }
  async markWeekFinal(ws: string, rt: number, rid: string, days: number): Promise<void> {
    this.weeks.set(`${ws}|${rt}`, { weekly_final: true, weekly_report_id: rid, daily_days_loaded: days });
  }
}

export const DET_COLS = FIN_DET_METRICS.map(([, c]) => c);
