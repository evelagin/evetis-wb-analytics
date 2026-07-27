import { describe, it, expect } from 'vitest';
import type { JobLoadMetadata } from '@google-cloud/bigquery';
import { StocksBq, type BqLike } from '../src/loaders/stocks/bq.js';
import type { RawStockRow } from '../src/loaders/stocks/normalize.js';

/**
 * Регрессия fix/mig1-stocks-load-create-never.
 *
 * Баг: appendRaw() строил load job с writeDisposition=WRITE_APPEND, но БЕЗ
 * createDisposition → BigQuery берёт дефолт CREATE_IF_NEEDED и требует dataset-level
 * bigquery.tables.create, даже когда целевая RAW_WB_STOCKS__CR уже существует
 * (создана Terraform). Итог: `Permission bigquery.tables.create denied on dataset wb_raw`.
 *
 * Контракт: таблицами владеет Terraform, runtime-SA НЕ создаёт таблицы →
 * createDisposition ОБЯЗАН быть CREATE_NEVER.
 */

interface LoadCall {
  table: string;
  source: string;
  metadata: JobLoadMetadata;
}

/** Фейк BqLike: перехватывает metadata, переданную table.load(). */
class CaptureBq implements BqLike {
  loads: LoadCall[] = [];
  async query(): Promise<[unknown[]]> {
    return [[]];
  }
  dataset() {
    return {
      table: (tableId: string) => ({
        // ВАЖНО: у BqLike НЕТ метода create() — код физически не может создать
        // таблицу; единственный путь записи — load() с нашей metadata.
        load: async (source: string, metadata: JobLoadMetadata): Promise<unknown> => {
          this.loads.push({ table: tableId, source, metadata });
          return {};
        },
      }),
    };
  }
  job() {
    return { get: async () => [{ metadata: { status: { state: 'DONE' } } }] as [{ metadata?: unknown }] };
  }
  last(): LoadCall {
    return this.loads[this.loads.length - 1]!;
  }
}

const DETERMINISTIC_JOB_ID = 'STOCK_LOAD_shadow_2026-07-27_RAW_WB_STOCKS__CR';

function makeSb() {
  const fake = new CaptureBq();
  return { fake, sb: new StocksBq('proj', 'EU', 'wb_raw', fake) };
}

describe('StocksBq.appendRaw — контракт load job (create-never)', () => {
  it('createDisposition === CREATE_NEVER (runtime-SA не создаёт таблицы)', async () => {
    const { fake, sb } = makeSb();
    const rows: RawStockRow[] = [];
    const res = await sb.appendRaw('RAW_WB_STOCKS__CR', rows, 'SNAP1', DETERMINISTIC_JOB_ID);
    expect(res).toBe('LOADED');
    expect(fake.loads).toHaveLength(1);
    expect(fake.last().metadata.createDisposition).toBe('CREATE_NEVER');
  });

  it('writeDisposition === WRITE_APPEND и sourceFormat = NDJSON', async () => {
    const { fake, sb } = makeSb();
    await sb.appendRaw('RAW_WB_STOCKS__CR', [], 'SNAP1', DETERMINISTIC_JOB_ID);
    expect(fake.last().metadata.writeDisposition).toBe('WRITE_APPEND');
    expect(fake.last().metadata.sourceFormat).toBe('NEWLINE_DELIMITED_JSON');
  });

  it('jobId остаётся детерминированным (передаётся как есть)', async () => {
    const { fake, sb } = makeSb();
    await sb.appendRaw('RAW_WB_STOCKS__CR', [], 'SNAP1', DETERMINISTIC_JOB_ID);
    expect(fake.last().metadata.jobId).toBe(DETERMINISTIC_JOB_ID);
    expect(fake.last().metadata.location).toBe('EU');
  });

  it('целевая таблица пишется через load, а не создаётся кодом', async () => {
    const { fake, sb } = makeSb();
    await sb.appendRaw('RAW_WB_STOCKS__CR', [], 'SNAP1', DETERMINISTIC_JOB_ID);
    // Записали ровно в переданную таблицу; создание отсутствует как операция
    // (createDisposition=CREATE_NEVER И в BqLike нет create()).
    expect(fake.last().table).toBe('RAW_WB_STOCKS__CR');
    expect(fake.last().metadata.createDisposition).toBe('CREATE_NEVER');
  });
});
