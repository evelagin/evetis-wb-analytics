/**
 * UNITKA ENGINE v1 — BQ-проводка. Engine НЕ содержит бизнес-SQL: читает только
 * подготовленный слой `wb_mart.V_UNITKA_*` (sql/unitka/engine_v1_views.sql) и пишет
 * журнал `wb_ops.UNITKA_ENGINE_RUNS` (таблица Terraform-owned, runtime её не создаёт).
 *
 * Инъекция QueryRunner (как у витрины) — слой тестируется без BigQuery.
 */
import type { QueryRunner } from '../mart/bq.js';
import { LoaderError } from '../../errors.js';

export interface FreshnessRow {
  source: string;
  maxClosedDate: string | null; // YYYY-MM-DD
  gating: boolean;
  observedAt: string | null;    // ISO
}

export interface LcdRow {
  lastClosedDate: string; // YYYY-MM-DD
  d1Msk: string;          // YYYY-MM-DD
}

export interface FactRow {
  nmId: number;
  date: string; // YYYY-MM-DD
  views: number | null;
  opens: number | null;
  carts: number | null;
  orders: number | null;
  cancels: number | null;
  stock: number | null;
  adsIn: number | null;
  price: number | null;
  storage: number | null;
  ordersSource: string;
  cancelsSource: string;
}

export interface LogisticsRateRow {
  nmId: number;        // 0 = магазин
  shipments: number;   // уникальные srid с операцией IN ('Логистика','Доставка')
  directRate: number | null;
  refusals: number;
  reverseRate: number | null;
  forwardSum: number;
  reverseSum: number;
  nLogistics: number;
  nDelivery: number;
  deliveryComponentSum: number;
  sales: number | null; // только у магазина: уникальные srid с «Продажа»
  windowFrom: string;
  windowTo: string;
}

export interface CommissionRateRow {
  nmId: number; // 0 = магазин
  sales: number;
  logisticsPerUnit: number | null;
  commissionRate: number | null; // тариф + эквайринг, уже округлённые по слагаемым
  windowFrom: string;
  windowTo: string;
}

export interface EngineRunRecord {
  runId: string;
  environment: string;
  mode: 'SHADOW' | 'WRITE';
  startedAtIso: string;
  completedAtIso: string;
  lastClosedDate: string | null;
  sourceFreshnessJson: string;
  rowsRead: number;
  cellsPlanned: number;
  cellsWritten: number;
  qaStatus: string;
  qaJson: string;
  errorCode: string | null;
  errorMessage: string | null;
  gitSha: string;
  imageDigest: string;
  engineVersion: string;
}

/* ── нормализация типов BigQuery (DATE/TIMESTAMP → {value}, NUMERIC → Big) ── */
function str(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === 'object' && v !== null && 'value' in v) return String((v as { value: unknown }).value);
  return String(v);
}
function num(v: unknown): number | null {
  const s = str(v);
  if (s === null || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}
function numReq(v: unknown, what: string): number {
  const n = num(v);
  if (n === null) throw new LoaderError(`Подготовленный слой вернул пустое ${what}`, 'BQ_SHAPE');
  return n;
}
function date(v: unknown): string | null {
  const s = str(v);
  return s === null ? null : s.slice(0, 10);
}

export class UnitkaBq {
  constructor(
    private readonly runner: QueryRunner,
    private readonly martDataset: string,
    private readonly opsDataset: string,
    private readonly runsTable: string,
  ) {}

  private fqn(dataset: string, name: string): string {
    return `\`${this.runner.projectId}.${dataset}.${name}\``;
  }

  async freshness(): Promise<FreshnessRow[]> {
    const rows = await this.runner.query(
      `SELECT source, max_closed_date, gating, observed_at FROM ${this.fqn(this.martDataset, 'V_UNITKA_SOURCE_FRESHNESS')} ORDER BY source`,
    );
    return rows.map((r) => ({
      source: String(r.source),
      maxClosedDate: date(r.max_closed_date),
      gating: Boolean(r.gating),
      observedAt: str(r.observed_at),
    }));
  }

  async lastClosedDate(): Promise<LcdRow> {
    const rows = await this.runner.query(
      `SELECT last_closed_date, d1_msk FROM ${this.fqn(this.martDataset, 'V_UNITKA_LAST_CLOSED_DATE')}`,
    );
    const r = rows[0];
    const lcd = r ? date(r.last_closed_date) : null;
    const d1 = r ? date(r.d1_msk) : null;
    if (!lcd || !d1) throw new LoaderError('V_UNITKA_LAST_CLOSED_DATE не вернула дату — гейтящие источники пусты', 'SOURCE_STALE');
    return { lastClosedDate: lcd, d1Msk: d1 };
  }

  async facts(): Promise<FactRow[]> {
    const rows = await this.runner.query(
      `SELECT nm_id, date_msk, views, opens, carts, orders, cancels, stock, ads_in, price, storage, orders_source, cancels_source
       FROM ${this.fqn(this.martDataset, 'V_UNITKA_DAILY_FACT')} ORDER BY nm_id, date_msk`,
    );
    return rows.map((r) => ({
      nmId: numReq(r.nm_id, 'nm_id'),
      date: date(r.date_msk) ?? '',
      views: num(r.views),
      opens: num(r.opens),
      carts: num(r.carts),
      orders: num(r.orders),
      cancels: num(r.cancels),
      stock: num(r.stock),
      adsIn: num(r.ads_in),
      price: num(r.price),
      storage: num(r.storage),
      ordersSource: String(r.orders_source ?? ''),
      cancelsSource: String(r.cancels_source ?? ''),
    }));
  }

  async logisticsRates(): Promise<LogisticsRateRow[]> {
    const rows = await this.runner.query(
      `SELECT nm_id, shipments, direct_rate, refusals, reverse_rate, forward_sum, reverse_sum, n_logistics, n_delivery,
              delivery_component_sum, sales, window_from, window_to
       FROM ${this.fqn(this.martDataset, 'V_UNITKA_LOGISTICS_RATES')} ORDER BY nm_id`,
    );
    return rows.map((r) => ({
      nmId: numReq(r.nm_id, 'nm_id'),
      shipments: num(r.shipments) ?? 0,
      directRate: num(r.direct_rate),
      refusals: num(r.refusals) ?? 0,
      reverseRate: num(r.reverse_rate),
      forwardSum: num(r.forward_sum) ?? 0,
      reverseSum: num(r.reverse_sum) ?? 0,
      nLogistics: num(r.n_logistics) ?? 0,
      nDelivery: num(r.n_delivery) ?? 0,
      deliveryComponentSum: num(r.delivery_component_sum) ?? 0,
      sales: num(r.sales),
      windowFrom: date(r.window_from) ?? '',
      windowTo: date(r.window_to) ?? '',
    }));
  }

  async commissionRates(): Promise<CommissionRateRow[]> {
    const rows = await this.runner.query(
      `SELECT nm_id, sales, logistics_per_unit, commission_rate, window_from, window_to
       FROM ${this.fqn(this.martDataset, 'V_UNITKA_COMMISSION_RATES')} ORDER BY nm_id`,
    );
    return rows.map((r) => ({
      nmId: numReq(r.nm_id, 'nm_id'),
      sales: num(r.sales) ?? 0,
      logisticsPerUnit: num(r.logistics_per_unit),
      commissionRate: num(r.commission_rate),
      windowFrom: date(r.window_from) ?? '',
      windowTo: date(r.window_to) ?? '',
    }));
  }

  /** Журнал прогона — одна строка на прогон, append-only INSERT. */
  async insertRun(rec: EngineRunRecord): Promise<void> {
    const t = this.fqn(this.opsDataset, this.runsTable);
    await this.runner.query(
      `INSERT INTO ${t}
        (run_id, environment, mode, started_at, completed_at, last_closed_date, source_freshness_json,
         rows_read, cells_planned, cells_written, qa_status, qa_json, error_code, error_message,
         git_sha, image_digest, engine_version)
       VALUES
        (@runId, @environment, @mode, TIMESTAMP(@startedAt), TIMESTAMP(@completedAt), SAFE_CAST(@lcd AS DATE), @freshness,
         @rowsRead, @cellsPlanned, @cellsWritten, @qaStatus, @qaJson, @errorCode, @errorMessage,
         @gitSha, @imageDigest, @engineVersion)`,
      {
        runId: rec.runId,
        environment: rec.environment,
        mode: rec.mode,
        startedAt: rec.startedAtIso,
        completedAt: rec.completedAtIso,
        lcd: rec.lastClosedDate,
        freshness: rec.sourceFreshnessJson,
        rowsRead: rec.rowsRead,
        cellsPlanned: rec.cellsPlanned,
        cellsWritten: rec.cellsWritten,
        qaStatus: rec.qaStatus,
        qaJson: rec.qaJson,
        errorCode: rec.errorCode,
        errorMessage: rec.errorMessage,
        gitSha: rec.gitSha,
        imageDigest: rec.imageDigest,
        engineVersion: rec.engineVersion,
      },
      { lcd: 'STRING', errorCode: 'STRING', errorMessage: 'STRING' },
    );
  }
}
