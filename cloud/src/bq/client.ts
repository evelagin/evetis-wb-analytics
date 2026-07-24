/**
 * Тонкая обёртка над BigQuery (PR-Mig0).
 * Инвариант: runtime НЕ создаёт таблицы — их создаёт Terraform. Здесь только
 * чтение и запись данных. Все задачи выполняются с явным location (обычно 'EU'):
 * location задачи ОБЯЗАН совпадать с location датасета.
 */
import { BigQuery } from '@google-cloud/bigquery';

export class BqClient {
  readonly bq: BigQuery;

  constructor(
    readonly projectId: string,
    readonly location: string,
  ) {
    this.bq = new BigQuery({ projectId });
  }

  async query<T = Record<string, unknown>>(
    query: string,
    params?: Record<string, unknown>,
  ): Promise<T[]> {
    const [rows] = await this.bq.query({ query, params: params ?? {}, location: this.location });
    return rows as T[];
  }
}
