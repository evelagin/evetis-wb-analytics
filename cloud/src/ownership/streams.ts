/**
 * Единый writer-ownership gate (этап C, OWNER ACK 11.11.2026): поток → загрузчики.
 *
 * Владение — свойство ПОТОКА, а не загрузчика: заказы, продажи и ночная сверка делят один токен и одну
 * таблицу продаж, поэтому переключаются только вместе (MIG_FINAL_INTEGRATION_GATE §B.3 п.2). Карта —
 * единственный источник: cli.ts по ней решает, проверять ли владельца, координатор — какие строки
 * LOADER_RUNS считать писателем потока.
 */

export const STREAMS = ['stocks', 'statistics', 'ads', 'finance', 'supplies', 'ref'] as const;
export type Stream = (typeof STREAMS)[number];

export const OWNERS = ['APPS_SCRIPT', 'CLOUD_RUN', 'NONE'] as const;
export type Owner = (typeof OWNERS)[number];

/** loader_name Cloud Run → поток. Загрузчика нет в карте → gate его не касается. */
export const LOADER_STREAM: Readonly<Record<string, Stream>> = Object.freeze({
  stocks: 'stocks',
  orders: 'statistics',
  sales: 'statistics',
  'sales-reconcile': 'statistics',
  ads: 'ads',
  'ads-query': 'ads',
  'ads-costs-audit': 'ads',
  finance: 'finance',
  'finance-backfill': 'finance',
  supplies: 'supplies',
  'ref-sync': 'ref',
});

export function streamOfLoader(loaderName: string): Stream | undefined {
  return Object.prototype.hasOwnProperty.call(LOADER_STREAM, loaderName) ? LOADER_STREAM[loaderName] : undefined;
}

export function loadersOfStream(stream: Stream): string[] {
  return Object.keys(LOADER_STREAM).filter((l) => LOADER_STREAM[l] === stream).sort();
}

export function isStream(s: string): s is Stream {
  return (STREAMS as readonly string[]).includes(s);
}

export function isOwner(s: string): s is Owner {
  return (OWNERS as readonly string[]).includes(s);
}

/** Таблица владения: датасет — env WRITER_OWNERSHIP_DATASET (по умолчанию wb_ops). */
export const OWNERSHIP_TABLE = 'INGEST_WRITER_OWNERSHIP';
export const CUTOVER_LOG_TABLE = 'INGEST_CUTOVER_LOG';
export const QUALIFICATION_TABLE = 'INGEST_QUALIFICATION';
export const DEFAULT_OWNERSHIP_DATASET = 'wb_ops';
