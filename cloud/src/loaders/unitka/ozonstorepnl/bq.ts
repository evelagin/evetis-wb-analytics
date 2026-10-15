/**
 * D4 — чтение канонического P&L магазина Ozon и входов гейта публикации. ТОЛЬКО чтение ozon_mart: загрузчик
 * не пишет в BigQuery ничего (ни снимков, ни журналов, кроме аренды guard'а в wb_raw.LOADER_RUNS — общий cli.ts).
 * Изоляция маркетплейсов: ни одной ссылки на wb_*; датасет зашит, не берётся из конфигурации.
 */
import { BigQuery } from '@google-cloud/bigquery';
import type { PnlRow } from './tabplan.js';
import { qaChecks } from './qa.js';

export const OZON_MART = 'ozon_mart';
export const PNL_VIEW = 'V_OZON_STORE_PNL_MONTHLY';
export const NEW_OPS_VIEW = 'V_OZON_FINANCE_NEW_OPERATIONS';
export const COVERAGE_VIEW = 'V_OZON_STORE_FINANCE_COVERAGE';
export const STATE_VIEW = 'V_OZON_STORE_MONTH_STATE';
/** Одновременных запросов QA: ~23 проверки × ~30 с последовательно не уложились бы в таймаут Job. */
export const QA_CONCURRENCY = 6;

/** Входы ворот состояния месяца (D3) — только для раскрытия причин неполноты, без вычисления денег. */
export const STATE_REASON_COLUMNS = ['pnl_month', 'finance_control_open_days', 'unconsumed_rub', 'unlinked_posting_rub',
  'unmapped_sku_rows', 'cogs_gap_units', 'reversal_suspected_postings', 'reversal_weak_signal_postings', 'd1_repair_rows',
  'provisional_inputs_missing', 'cis_without_document_postings', 'in_transit_without_accrual_units'] as const;
export type StateRow = Record<string, unknown>;

/** Результат одной проверки QA D3: PASS / WARN / FAIL; ERROR — запрос не выполнился (для гейта = FAIL). */
export interface QaResult { id: string; status: string; detail: string }

export interface OzonStorePnlBqLike {
  query(o: { query: string; location?: string }): Promise<[unknown[]]>;
}

/** Сводка детектора и покрытия: всё, что модель D3 обещает держать нулём, кроме раскрытого в состояниях. */
export interface GateInputs {
  new_operation_rows: number;
  max_abs_conservation_residual_rub: number;
  unconsumed_rub: number;
  unlinked_posting_rub: number;
}

export function pnlSql(project: string): string {
  return `SELECT * FROM \`${project}.${OZON_MART}.${PNL_VIEW}\` ORDER BY pnl_month`;
}

/** Один запрос на вью (урок «query is too complex»): детектор и покрытие — отдельными подзапросами-скалярами. */
export function gateSql(project: string): string {
  return `SELECT
  (SELECT COUNT(*) FROM \`${project}.${OZON_MART}.${NEW_OPS_VIEW}\`) new_operation_rows,
  (SELECT IFNULL(MAX(ABS(conservation_residual_rub)), 0) FROM \`${project}.${OZON_MART}.${COVERAGE_VIEW}\`) max_abs_conservation_residual_rub,
  (SELECT IFNULL(SUM(ABS(unconsumed_rub)), 0) FROM \`${project}.${OZON_MART}.${COVERAGE_VIEW}\`) unconsumed_rub,
  (SELECT IFNULL(SUM(ABS(unlinked_posting_rub)), 0) FROM \`${project}.${OZON_MART}.${COVERAGE_VIEW}\`) unlinked_posting_rub`;
}

export function stateSql(project: string): string {
  return `SELECT ${STATE_REASON_COLUMNS.join(', ')} FROM \`${project}.${OZON_MART}.${STATE_VIEW}\` ORDER BY pnl_month`;
}

export class OzonStorePnlBq {
  private readonly bq: OzonStorePnlBqLike;
  constructor(private readonly projectId: string, private readonly location: string, bqClient?: OzonStorePnlBqLike) {
    if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(projectId)) throw new Error(`Invalid project '${projectId}'`);
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as OzonStorePnlBqLike);
  }

  async readPnl(): Promise<PnlRow[]> {
    const [rows] = await this.bq.query({ query: pnlSql(this.projectId), location: this.location });
    return rows as PnlRow[];
  }

  async readStates(): Promise<StateRow[]> {
    const [rows] = await this.bq.query({ query: stateSql(this.projectId), location: this.location });
    return rows as StateRow[];
  }

  /**
   * Канонический QA D3 по одному запросу на проверку (единый запрос упирается в «too complex»). Каждый запрос — ~20–40 с
   * по стопке вью, поэтому проверки идут параллельно (не больше QA_CONCURRENCY одновременно), порядок результатов —
   * порядок файла. Ошибка запроса — ERROR (гейт трактует как FAIL).
   */
  async runQa(concurrency: number = QA_CONCURRENCY): Promise<QaResult[]> {
    const checks = qaChecks();
    const out: QaResult[] = new Array(checks.length);
    let next = 0;
    const worker = async (): Promise<void> => {
      while (next < checks.length) {
        const i = next++;
        const c = checks[i]!;
        try {
          const [rows] = await this.bq.query({ query: c.sql, location: this.location });
          const r = (rows[0] ?? {}) as Record<string, unknown>;
          out[i] = { id: c.id, status: String(r.status ?? 'NO_ROW'), detail: String(r.detail ?? '') };
        } catch (e) {
          out[i] = { id: c.id, status: 'ERROR', detail: String((e as Error).message ?? e).slice(0, 300) };
        }
      }
    };
    await Promise.all(Array.from({ length: Math.max(1, Math.min(concurrency, checks.length)) }, worker));
    return out;
  }

  async readGateInputs(): Promise<GateInputs> {
    const [rows] = await this.bq.query({ query: gateSql(this.projectId), location: this.location });
    const r = (rows[0] ?? {}) as Record<string, unknown>;
    const n = (k: string): number => {
      const v = r[k];
      const x = typeof v === 'number' ? v : Number(v && typeof v === 'object' && 'value' in v ? (v as { value: unknown }).value : String(v));
      if (!Number.isFinite(x)) throw new Error(`гейт: ${k} не число (${String(v)})`);
      return x;
    };
    return { new_operation_rows: n('new_operation_rows'), max_abs_conservation_residual_rub: n('max_abs_conservation_residual_rub'),
      unconsumed_rub: n('unconsumed_rub'), unlinked_posting_rub: n('unlinked_posting_rub') };
  }
}
