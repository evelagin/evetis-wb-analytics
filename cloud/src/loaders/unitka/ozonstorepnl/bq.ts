/**
 * D4 — чтение канонического P&L магазина Ozon и входов гейта публикации. ТОЛЬКО чтение ozon_mart: загрузчик
 * не пишет в BigQuery ничего (ни снимков, ни журналов, кроме аренды guard'а в wb_raw.LOADER_RUNS — общий cli.ts).
 * Изоляция маркетплейсов: ни одной ссылки на wb_*; датасет зашит, не берётся из конфигурации.
 */
import { BigQuery } from '@google-cloud/bigquery';
import type { PnlRow } from './tabplan.js';

export const OZON_MART = 'ozon_mart';
export const PNL_VIEW = 'V_OZON_STORE_PNL_MONTHLY';
export const NEW_OPS_VIEW = 'V_OZON_FINANCE_NEW_OPERATIONS';
export const COVERAGE_VIEW = 'V_OZON_STORE_FINANCE_COVERAGE';

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
