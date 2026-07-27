/**
 * Тонкая обёртка над BigQuery (PR-Mig0).
 * Инвариант: runtime НЕ создаёт таблицы — их создаёт Terraform. Здесь только
 * чтение и запись данных. Все задачи выполняются с явным location (обычно 'EU'):
 * location задачи ОБЯЗАН совпадать с location датасета.
 *
 * fix/mig1-bq-typed-null-params: добавлен третий аргумент `types` — карта
 * ИМЯ_ПАРАМЕТРА → тип BigQuery ('STRING' | 'INT64' | ...). Node-клиент BigQuery
 * НЕ умеет выводить тип параметра, значение которого null, поэтому для запросов
 * с потенциально-null параметрами (напр. UPDATE в run-manifest) тип надо задавать
 * явно, иначе query() падает с "Parameter types must be provided for null values".
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
    types?: Record<string, string>,
  ): Promise<T[]> {
    const [rows] = await this.bq.query({
      query,
      params: params ?? {},
      types,
      location: this.location,
    });
    return rows as T[];
  }
}
