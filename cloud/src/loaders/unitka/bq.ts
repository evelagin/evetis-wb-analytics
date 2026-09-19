/**
 * UNITKA ENGINE v1 — BQ-проводка. Engine НЕ содержит бизнес-SQL: читает только
 * подготовленный слой `wb_mart.V_UNITKA_*` (sql/unitka/engine_v1_views.sql) и пишет
 * журнал `wb_ops.UNITKA_ENGINE_RUNS` (таблица Terraform-owned, runtime её не создаёт).
 *
 * Инъекция QueryRunner (как у витрины) — слой тестируется без BigQuery.
 */
import type { QueryRunner } from '../mart/bq.js';
import { LoaderError } from '../../errors.js';
import type { IntegrityFactsRow, CogsCanonicalRow, PriceState, DivergenceClass } from './integrity.js';

const PRICE_STATES: ReadonlySet<string> = new Set(['PRESENT', 'MISSING_WITH_ACTIVITY', 'MISSING_NO_ACTIVITY']);
const DIVERGENCE_CLASSES: ReadonlySet<string> = new Set(['EXACT', 'FUNNEL_GT_FACT', 'FACT_GT_FUNNEL', 'ONLY_FUNNEL', 'ONLY_FACT', 'NO_FUNNEL_ROW']);

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
  /**
   * Поля слоя сверки (V_UNITKA_RECON_FACT). В режиме UNITKA_RECONCILE_MODE=off их нет (старая вью).
   * priceSource: ORDERS_API — FACT_ORDERS; FUNNEL_FALLBACK — сумма заказов той же строки воронки; null — не разрешено.
   */
  priceSource?: PriceSource | null;
  factOrderQty?: number | null;
  funnelOrders?: number | null;
  funnelOrdersSum?: number | null;
  skuActive?: boolean;
  /** Метка версии источника для журнала ремонта (observed_at воронки / built_at витрины / observed_at хранения). */
  funnelObservedAt?: string | null;
  ordersBuiltAt?: string | null;
  storageObservedAt?: string | null;
}

export type PriceSource = 'ORDERS_API' | 'FUNNEL_FALLBACK';
const PRICE_SOURCES: ReadonlySet<string> = new Set(['ORDERS_API', 'FUNNEL_FALLBACK']);

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

/** Строка журнала ремонта (wb_ops.UNITKA_REPAIR_LEDGER): одна фактическая поправка автоматической ячейки закрытого дня. */
export interface RepairRecord {
  repairId: string;
  runId: string;
  environment: string;
  engineVersion: string;
  gitSha: string;
  detectedAt: string;
  repairedAt: string | null;
  monthKey: string;
  businessDate: string;
  nmId: number;
  field: string;
  cellA1: string;
  oldValue: string | null;
  newValue: string | null;
  source: string;
  sourceAsOf: string | null;
  reason: string;
  status: 'REPAIRED' | 'PLANNED_NOT_WRITTEN';
}

/** Строка снимка issue (wb_ops.UNITKA_INTEGRITY_ISSUES). */
export interface IssueRecord {
  runId: string;
  environment: string;
  evaluatedAt: string;
  phase: string;
  /** NULL — строка-маркер прогона (code = RUN_MARKER): чистый прогон без issue тоже виден наблюдаемости. */
  issueKey: string | null;
  businessDate: string | null;
  nmId: number | null;
  field: string;
  code: string;
  state: string;
  severity: string;
  financialValid: boolean;
  source: string;
  sourceValue: string | null;
  diagnosticValue: string | null;
  message: string;
}

const LEDGER_BATCH = 200;

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
    private readonly ledgerTable: string = 'UNITKA_REPAIR_LEDGER',
    private readonly issuesTable: string = 'UNITKA_INTEGRITY_ISSUES',
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

  /* ── Слой сверки (sql/unitka/reconcile_v1.sql). Читается ТОЛЬКО при UNITKA_RECONCILE_MODE = observe | write. ── */

  /** Окно сверки: [window_from, window_to] ⊂ 35 календарных дней до LCD, не раньше нижней границы сверки. */
  async reconWindow(): Promise<{ lastClosedDate: string; from: string; to: string; days: number; floor: string }> {
    const rows = await this.runner.query(
      `SELECT last_closed_date, window_from, window_to, window_days, reconcile_floor FROM ${this.fqn(this.martDataset, 'V_UNITKA_RECON_WINDOW')}`,
    );
    const r = rows[0];
    const lcd = r ? date(r.last_closed_date) : null;
    const from = r ? date(r.window_from) : null;
    const to = r ? date(r.window_to) : null;
    const floor = r ? date(r.reconcile_floor) : null;
    if (!lcd || !from || !to || !floor) throw new LoaderError('V_UNITKA_RECON_WINDOW не вернула окно сверки', 'RECON_WINDOW_UNAVAILABLE');
    return { lastClosedDate: lcd, from, to, days: numReq(r!.window_days, 'window_days'), floor };
  }

  /** wb_mart.V_UNITKA_RECON_FACT — факты SKU × день за окно сверки с происхождением цены. NULL сохраняются. */
  async reconFacts(): Promise<FactRow[]> {
    const rows = await this.runner.query(
      `SELECT nm_id, date_msk, views, opens, carts, orders, cancels, stock, ads_in, price, storage, orders_source, cancels_source,
              price_source, fact_order_qty, funnel_orders, funnel_orders_sum, sku_active,
              funnel_observed_at, orders_built_at, storage_observed_at
       FROM ${this.fqn(this.martDataset, 'V_UNITKA_RECON_FACT')} ORDER BY nm_id, date_msk`,
    );
    return rows.map((r) => {
      const ps = str(r.price_source);
      if (ps !== null && !PRICE_SOURCES.has(ps)) throw new LoaderError(`V_UNITKA_RECON_FACT: неизвестный price_source '${ps}'`, 'BQ_SHAPE');
      const price = num(r.price);
      // Цена без происхождения или происхождение без цены — нарушение контракта слоя (fail-closed).
      if ((price === null) !== (ps === null)) throw new LoaderError(`V_UNITKA_RECON_FACT: price и price_source рассогласованы (${String(r.nm_id)} ${String(date(r.date_msk))})`, 'BQ_SHAPE');
      return {
        nmId: numReq(r.nm_id, 'nm_id'),
        date: date(r.date_msk) ?? '',
        views: num(r.views), opens: num(r.opens), carts: num(r.carts), orders: num(r.orders), cancels: num(r.cancels),
        stock: num(r.stock), adsIn: num(r.ads_in), price, storage: num(r.storage),
        ordersSource: String(r.orders_source ?? ''), cancelsSource: String(r.cancels_source ?? ''),
        priceSource: ps as PriceSource | null,
        factOrderQty: num(r.fact_order_qty), funnelOrders: num(r.funnel_orders), funnelOrdersSum: num(r.funnel_orders_sum),
        skuActive: r.sku_active === true || r.sku_active === 'true',
        funnelObservedAt: str(r.funnel_observed_at), ordersBuiltAt: str(r.orders_built_at), storageObservedAt: str(r.storage_observed_at),
      };
    });
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

  /* ── Integrity Guard V1. Вью применяются отдельным гейтом (1C2B). ──
   * jobTimeoutMs — серверный таймаут задания BigQuery: по его истечении BigQuery САМ отменяет
   * задание (реальная отмена, а не брошенный промис). Задаётся из бюджета Guard. */

  /** wb_mart.V_UNITKA_INTEGRITY — факты целостности SKU × день. NULL сохраняются как NULL. */
  async integrityFacts(jobTimeoutMs?: number, recon = false): Promise<IntegrityFactsRow[]> {
    // recon = true → слой сверки (окно 35 дней, происхождение цены, сумма воронки, покрытие снимка остатков).
    const extra = recon ? ', price_source, funnel_orders_sum, stock_date_covered, sku_active' : '';
    const rows = await this.runner.query(
      `SELECT marketplace, nm_id, internal_sku, product_name, day, last_closed_date, orders_unitka, cancels_unitka,
              orders_source, factual_order_price, orders_funnel, fact_order_rows, fact_order_qty,
              observed_price_diagnostic, observed_price_at, storage_value, storage_date_covered, price_state, divergence_class${extra}
       FROM ${this.fqn(this.martDataset, recon ? 'V_UNITKA_RECON_INTEGRITY' : 'V_UNITKA_INTEGRITY')} ORDER BY nm_id, day`,
      undefined, undefined, jobTimeoutMs === undefined ? undefined : { jobTimeoutMs },
    );
    return rows.map((r) => {
      const priceState = String(r.price_state);
      const divergence = String(r.divergence_class);
      if (!PRICE_STATES.has(priceState)) throw new LoaderError(`V_UNITKA_INTEGRITY: неизвестный price_state '${priceState}'`, 'BQ_SHAPE');
      if (!DIVERGENCE_CLASSES.has(divergence)) throw new LoaderError(`V_UNITKA_INTEGRITY: неизвестный divergence_class '${divergence}'`, 'BQ_SHAPE');
      return {
        marketplace: 'WB' as const,
        nmId: numReq(r.nm_id, 'nm_id'),
        internalSku: str(r.internal_sku),
        productName: str(r.product_name),
        day: date(r.day) ?? '',
        lastClosedDate: date(r.last_closed_date) ?? '',
        ordersUnitka: num(r.orders_unitka),
        cancelsUnitka: num(r.cancels_unitka),
        ordersSource: String(r.orders_source ?? ''),
        factualOrderPrice: num(r.factual_order_price),   // NULL ≠ 0
        ordersFunnel: num(r.orders_funnel),
        factOrderRows: num(r.fact_order_rows),
        factOrderQty: num(r.fact_order_qty),
        observedPriceDiagnostic: num(r.observed_price_diagnostic), // OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE
        observedPriceAt: str(r.observed_price_at),
        storageValue: num(r.storage_value),
        storageDateCovered: r.storage_date_covered === true || r.storage_date_covered === 'true',
        priceState: priceState as PriceState,
        divergenceClass: divergence as DivergenceClass,
        ...(recon ? {
          priceSource: str(r.price_source) as PriceSource | null,
          funnelOrdersSum: num(r.funnel_orders_sum),
          stockDateCovered: r.stock_date_covered === true || r.stock_date_covered === 'true',
          skuActive: r.sku_active === true || r.sku_active === 'true',
        } : {}),
      };
    });
  }

  /**
   * wb_mart.V_UNITKA_COGS_CANONICAL — канон COGS из ФИЗИЧЕСКОЙ копии wb_mart.UNITKA_COGS_EFFECTIVE.
   * Вью не читает evetis_ref. publishedAt — время последней успешной публикации копии (одинаково у всех строк).
   */
  async cogsCanonical(jobTimeoutMs?: number, recon = false): Promise<{ rows: CogsCanonicalRow[]; publishedAt: string | null; runId: string | null }> {
    const rows = await this.runner.query(
      `SELECT nm_id, internal_sku, day, cogs_interval_count, canonical_cogs, snapshot_published_at, snapshot_run_id
       FROM ${this.fqn(this.martDataset, recon ? 'V_UNITKA_RECON_COGS_CANONICAL' : 'V_UNITKA_COGS_CANONICAL')} ORDER BY nm_id, day`,
      undefined, undefined, jobTimeoutMs === undefined ? undefined : { jobTimeoutMs },
    );
    const first = rows[0];
    return {
      rows: rows.map((r) => ({
        nmId: numReq(r.nm_id, 'nm_id'),
        internalSku: str(r.internal_sku),
        day: date(r.day) ?? '',
        cogsIntervalCount: num(r.cogs_interval_count) ?? 0,
        canonicalCogs: num(r.canonical_cogs), // NULL ≠ 0
      })),
      publishedAt: first ? str(first.snapshot_published_at) : null,
      runId: first ? str(first.snapshot_run_id) : null,
    };
  }

  /**
   * Calendar V2 (Phase 2B): популяция активных SKU WB для подготовки месяца — то же определение, что
   * в V_UNITKA_DAILY_FACT (REF_SKU_MASTER: marketplace='WB' AND active). Только чтение wb_raw.
   */
  async activeSkus(): Promise<Array<{ nmId: number; name: string | null }>> {
    const rows = await this.runner.query(
      `SELECT nm_id, product_name_short FROM ${this.fqn('wb_raw', 'REF_SKU_MASTER')}
       WHERE marketplace = 'WB' AND active ORDER BY nm_id`,
    );
    return rows.map((r) => ({ nmId: numReq(r.nm_id, 'nm_id'), name: str(r.product_name_short) }));
  }

  /* ── Журнал ремонта и снимок issue (wb_ops; таблицы Terraform-owned, runtime их не создаёт). ── */

  /**
   * Доступность журнала ремонта — ДО любой записи исторических поправок (режим write). Ремонт без происхождения
   * не выполняется: нет таблицы / нет прав → LEDGER_UNAVAILABLE, fail-closed.
   */
  async ledgerProbe(): Promise<void> {
    try {
      await this.runner.query(`SELECT run_id FROM ${this.fqn(this.opsDataset, this.ledgerTable)} LIMIT 0`);
    } catch (e) {
      throw new LoaderError(`журнал ремонта ${this.opsDataset}.${this.ledgerTable} недоступен: ${e instanceof Error ? e.message : String(e)}`, 'LEDGER_UNAVAILABLE');
    }
  }

  /** Записи ремонта — append-only INSERT пачками. Пустой список — ни одного запроса. */
  async insertRepairs(rows: readonly RepairRecord[]): Promise<number> {
    const t = this.fqn(this.opsDataset, this.ledgerTable);
    for (let i = 0; i < rows.length; i += LEDGER_BATCH) {
      const payload = JSON.stringify(rows.slice(i, i + LEDGER_BATCH));
      await this.runner.query(
        `INSERT INTO ${t}
          (repair_id, run_id, environment, engine_version, git_sha, detected_at, repaired_at, month_key, business_date, nm_id,
           field, cell_a1, old_value, new_value, source, source_as_of, reason, status)
         SELECT JSON_VALUE(x, '$.repairId'), JSON_VALUE(x, '$.runId'), JSON_VALUE(x, '$.environment'), JSON_VALUE(x, '$.engineVersion'),
                JSON_VALUE(x, '$.gitSha'), TIMESTAMP(JSON_VALUE(x, '$.detectedAt')), SAFE.TIMESTAMP(JSON_VALUE(x, '$.repairedAt')),
                JSON_VALUE(x, '$.monthKey'), DATE(JSON_VALUE(x, '$.businessDate')), SAFE_CAST(JSON_VALUE(x, '$.nmId') AS INT64),
                JSON_VALUE(x, '$.field'), JSON_VALUE(x, '$.cellA1'), JSON_VALUE(x, '$.oldValue'), JSON_VALUE(x, '$.newValue'),
                JSON_VALUE(x, '$.source'), JSON_VALUE(x, '$.sourceAsOf'), JSON_VALUE(x, '$.reason'), JSON_VALUE(x, '$.status')
         FROM UNNEST(JSON_QUERY_ARRAY(@payload)) AS x`,
        { payload }, { payload: 'STRING' },
      );
    }
    return rows.length;
  }

  /** Снимок открытых issue прогона — append-only INSERT пачками (наблюдаемость: новые / решённые / самая старая). */
  async insertIssues(rows: readonly IssueRecord[]): Promise<number> {
    const t = this.fqn(this.opsDataset, this.issuesTable);
    for (let i = 0; i < rows.length; i += LEDGER_BATCH) {
      const payload = JSON.stringify(rows.slice(i, i + LEDGER_BATCH));
      await this.runner.query(
        `INSERT INTO ${t}
          (run_id, environment, evaluated_at, phase, issue_key, business_date, nm_id, field, code, state, severity,
           financial_valid, source, source_value, diagnostic_value, message)
         SELECT JSON_VALUE(x, '$.runId'), JSON_VALUE(x, '$.environment'), TIMESTAMP(JSON_VALUE(x, '$.evaluatedAt')), JSON_VALUE(x, '$.phase'),
                JSON_VALUE(x, '$.issueKey'), SAFE.DATE(JSON_VALUE(x, '$.businessDate')), SAFE_CAST(JSON_VALUE(x, '$.nmId') AS INT64),
                JSON_VALUE(x, '$.field'), JSON_VALUE(x, '$.code'), JSON_VALUE(x, '$.state'), JSON_VALUE(x, '$.severity'),
                SAFE_CAST(JSON_VALUE(x, '$.financialValid') AS BOOL), JSON_VALUE(x, '$.source'), JSON_VALUE(x, '$.sourceValue'),
                JSON_VALUE(x, '$.diagnosticValue'), JSON_VALUE(x, '$.message')
         FROM UNNEST(JSON_QUERY_ARRAY(@payload)) AS x`,
        { payload }, { payload: 'STRING' },
      );
    }
    return rows.length;
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
