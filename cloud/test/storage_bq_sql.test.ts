import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';
import { StorageBq, type StorageBqLike } from '../src/loaders/storage/bq.js';
import type { PaidStorageRow } from '../src/loaders/storage/normalize.js';

/**
 * Stage E4 — регрессии по двум прод-инцидентам 14.09.2026 (wb-paid-storage-prod-dxkt5):
 *
 *  1. `Syntax error: Unexpected keyword ROWS at [1:20]` — post-load QA собирал
 *     `SELECT COUNT(*) AS rows, …`, а ROWS зарезервировано в GoogleSQL. SQL лежит в шаблонной
 *     строке, поэтому ни tsc, ни eslint его не разбирают, а прежние тесты трогали только чистые
 *     функции и ни разу не конструировали StorageBq — хотя шов (bqClient) для этого есть.
 *
 *  2. Семантика fail-closed. Блокирующий QA шёл ОТДЕЛЬНЫМ запросом ПОСЛЕ COMMIT, поэтому его
 *     падение оставляло target уже изменённым при статусе ERROR. Теперь все блокирующие проверки
 *     живут внутри той же транзакции до COMMIT.
 *
 * Ограничение честности: офлайн нет парсера GoogleSQL и нет теневого контура (loader prodOnly),
 * поэтому поведение транзакции моделируется. Тесты доказывают КОНТРАКТ, на который опирается
 * loader: структуру генерируемого скрипта (что именно стоит до COMMIT) и реакцию кода на
 * успех/провал. Они не заменяют проверку самого BigQuery.
 */
const RESERVED_GOOGLESQL = new Set([
  'ALL', 'AND', 'ANY', 'ARRAY', 'AS', 'ASC', 'ASSERT_ROWS_MODIFIED', 'AT', 'BETWEEN', 'BY',
  'CASE', 'CAST', 'COLLATE', 'CONTAINS', 'CREATE', 'CROSS', 'CUBE', 'CURRENT', 'DEFAULT',
  'DEFINE', 'DESC', 'DISTINCT', 'ELSE', 'END', 'ENUM', 'ESCAPE', 'EXCEPT', 'EXCLUDE', 'EXISTS',
  'EXTRACT', 'FALSE', 'FETCH', 'FOLLOWING', 'FOR', 'FROM', 'FULL', 'GROUP', 'GROUPING', 'GROUPS',
  'HASH', 'HAVING', 'IF', 'IGNORE', 'IN', 'INNER', 'INTERSECT', 'INTERVAL', 'INTO', 'IS', 'JOIN',
  'LATERAL', 'LEFT', 'LIKE', 'LIMIT', 'LOOKUP', 'MERGE', 'NATURAL', 'NEW', 'NO', 'NOT', 'NULL',
  'NULLS', 'OF', 'ON', 'OR', 'ORDER', 'OUTER', 'OVER', 'PARTITION', 'PRECEDING', 'PROTO', 'RANGE',
  'RECURSIVE', 'RESPECT', 'RIGHT', 'ROLLUP', 'ROWS', 'SELECT', 'SET', 'SOME', 'STRUCT',
  'TABLESAMPLE', 'THEN', 'TO', 'TREAT', 'TRUE', 'UNBOUNDED', 'UNION', 'UNNEST', 'USING', 'WHEN',
  'WHERE', 'WINDOW', 'WITH', 'WITHIN',
]);

function reservedAliases(sql: string): string[] {
  const found: string[] = [];
  const re = /\bAS\s+([A-Za-z_][A-Za-z0-9_]*)/gi;
  let m: RegExpExecArray | null = re.exec(sql);
  while (m !== null) {
    if (RESERVED_GOOGLESQL.has(m[1].toUpperCase())) found.push(m[1]);
    m = re.exec(sql);
  }
  return found;
}

const round2 = (x: number): number => Math.round(x * 100) / 100;

interface QueryOpts {
  query: string;
  params?: Record<string, unknown>;
  types?: Record<string, string>;
  location?: string;
}

/**
 * Модель BigQuery: держит target и stage, читает РЕАЛЬНЫЙ JSONL, который записал продовый код,
 * и исполняет транзакционный скрипт по тем же правилам, что записаны в SQL (два гейта, DML
 * между ними, откат при провале любого гейта).
 */
class FakeBq implements StorageBqLike {
  readonly queries: string[] = [];
  stage: PaidStorageRow[] = [];
  committed = 0;
  rolledBack = 0;
  /** порча stage после загрузки — моделирует расхождение источника и stage */
  corruptStage: ((rows: PaidStorageRow[]) => PaidStorageRow[]) | null = null;
  /** потеря строки на INSERT — моделирует расхождение target и источника после DML */
  loseRowOnInsert = false;

  constructor(public target: PaidStorageRow[] = []) {}

  query(o: QueryOpts): Promise<[unknown[]]> {
    this.queries.push(o.query);
    const p = (o.params ?? {}) as Record<string, string & number>;

    if (o.query.includes('BEGIN TRANSACTION')) {
      this.runScript(String(p.start), String(p.end), Number(p.expectedRows), Number(p.expectedDays), Number(p.expectedRub));
      return Promise.resolve([[]]);
    }
    const onStage = /FROM `[^`]*__STAGE`/.test(o.query);
    const src = onStage ? this.stage : this.target;
    const win = src.filter((r) => r.date_msk >= String(p.start) && r.date_msk <= String(p.end));
    if (/AS row_count\b/.test(o.query)) {
      return Promise.resolve([[{
        row_count: win.length,
        days: new Set(win.map((r) => r.date_msk)).size,
        nm: new Set(win.map((r) => r.nm_id)).size,
        storage_rub: round2(win.reduce((a, r) => a + (r.warehouse_price ?? 0), 0)),
      }]]);
    }
    return Promise.resolve([[{ n: win.length }]]);
  }

  private runScript(start: string, end: string, expRows: number, expDays: number, expRub: number): void {
    const inWin = (r: PaidStorageRow): boolean => r.date_msk >= start && r.date_msk <= end;
    const sum = (rs: PaidStorageRow[]): number => round2(rs.reduce((a, r) => a + (r.warehouse_price ?? 0), 0));

    // ── Gate 1: до DELETE, target ещё не тронут ──
    if (this.stage.length !== expRows) throw new Error('WB_STORAGE_STAGE_ROWS');
    if (this.stage.some((r) => !inWin(r))) throw new Error('WB_STORAGE_STAGE_OUT_OF_WINDOW');
    if (new Set(this.stage.map((r) => r.date_msk)).size !== expDays) throw new Error('WB_STORAGE_STAGE_DAYS');
    if (Math.abs(sum(this.stage) - expRub) > 0.02) throw new Error('WB_STORAGE_STAGE_AMOUNT');

    const snapshot = this.target.map((r) => ({ ...r }));
    const inserted = this.loseRowOnInsert ? this.stage.slice(1) : this.stage;
    this.target = this.target.filter((r) => !inWin(r)).concat(inserted.map((r) => ({ ...r })));

    // ── Gate 2: после DML, но ДО COMMIT ──
    const tw = this.target.filter(inWin);
    const fail =
      tw.length !== expRows ||
      new Set(tw.map((r) => r.date_msk)).size !== expDays ||
      Math.abs(sum(tw) - expRub) > 0.02;
    if (fail) {
      this.target = snapshot; // ROLLBACK TRANSACTION
      this.rolledBack++;
      throw new Error('WB_STORAGE_TARGET_ROWS');
    }
    this.committed++;
  }

  dataset(): { table(t: string): { load(src: string, md: unknown): Promise<unknown> } } {
    const setStage = (rows: PaidStorageRow[]): void => {
      this.stage = this.corruptStage ? this.corruptStage(rows) : rows;
    };
    return {
      table() {
        return {
          load(src: string): Promise<unknown> {
            setStage(
              readFileSync(src, 'utf8')
                .split('\n')
                .filter((l) => l.length > 0)
                .map((l) => JSON.parse(l) as PaidStorageRow),
            );
            return Promise.resolve(undefined);
          },
        };
      },
    };
  }
}

function row(date: string, price = 12.34, nm = 252442517): PaidStorageRow {
  return {
    observation_id: 'WBPS_20260910_20260913', run_id: 'storage_test', observed_at: '2026-09-14T11:00:00.000Z',
    date_msk: date, nm_id: nm, chrt_id: 1, barcode: '4601', warehouse: 'Коледино', office_id: 507,
    warehouse_coef: 1, log_warehouse_coef: 1, subject: 'Крем', brand: 'EVETIS', vendor_code: 'SKU-1',
    volume: 0.3, calc_type: 'короб', warehouse_price: price, barcodes_count: 2, pallet_place_code: 0,
    pallet_count: 0, loyalty_discount: 0, tariff_fix_date: '', tariff_lower_date: '',
    raw_row_json: '{}', ingested_at: '2026-09-14T11:00:00.000Z',
  };
}

const WINDOW = ['2026-09-10', '2026-09-11', '2026-09-12', '2026-09-13'];
const newRows = (): PaidStorageRow[] => WINDOW.map((d) => row(d));
/** то, что лежало до прогона: 01–05.09 вне окна + старая версия окна */
const preExisting = (): PaidStorageRow[] => [
  row('2026-09-01', 140.33), row('2026-09-02', 136.82), row('2026-09-03', 134.69),
  row('2026-09-04', 130.49), row('2026-09-05', 129.11),
  ...WINDOW.map((d) => row(d, 999)),
];
const expectedOf = (rows: PaidStorageRow[]): { rows: number; days: number; storageRub: number } => ({
  rows: rows.length,
  days: new Set(rows.map((r) => r.date_msk)).size,
  storageRub: rows.reduce((a, r) => a + (r.warehouse_price ?? 0), 0),
});

function replace(bq: FakeBq, rows = newRows()): Promise<number> {
  return new StorageBq('p', 'EU', 'wb_raw', bq)
    .replaceWindow('RAW_WB_PAID_STORAGE', rows, '2026-09-10', '2026-09-13', 'job_test_1', expectedOf(rows));
}

describe('E4 StorageBq — SQL-пути и fail-closed', () => {
  it('qaWindow не использует зарезервированные слова GoogleSQL как алиасы (падение ROWS at [1:20])', async () => {
    const bq = new FakeBq();
    await new StorageBq('p', 'EU', 'wb_raw', bq).qaWindow('RAW_WB_PAID_STORAGE', '2026-09-10', '2026-09-13');
    const sql = bq.queries[0];
    expect(reservedAliases(sql)).toEqual([]);
    expect(sql).not.toMatch(/\bAS\s+rows\b/i);
    expect(sql.split('\n')[0].slice(19, 23)).not.toBe('rows');
  });

  it('qaWindow читает row_count в поле rows — контракт вызывающего не изменился', async () => {
    const bq = new FakeBq(newRows());
    const qa = await new StorageBq('p', 'EU', 'wb_raw', bq).qaWindow('RAW_WB_PAID_STORAGE', '2026-09-10', '2026-09-13');
    expect(qa).toEqual({ rows: 4, days: 4, nm: 1, storageRub: 49.36 });
  });

  it('ни один запрос класса не содержит зарезервированных алиасов', async () => {
    const bq = new FakeBq(preExisting());
    await replace(bq);
    for (const sql of bq.queries) expect(reservedAliases(sql)).toEqual([]);
  });

  it('структура скрипта: все блокирующие проверки стоят ДО COMMIT, обработчик делает ROLLBACK', async () => {
    const bq = new FakeBq(preExisting());
    await replace(bq);
    const tx = bq.queries.find((q) => q.includes('BEGIN TRANSACTION'));
    expect(tx).toBeDefined();
    const sql = tx as string;

    const iBegin = sql.indexOf('BEGIN TRANSACTION');
    const iDelete = sql.indexOf('DELETE FROM');
    const iInsert = sql.indexOf('INSERT INTO');
    const iCommit = sql.indexOf('COMMIT TRANSACTION');
    const iHandler = sql.indexOf('EXCEPTION WHEN ERROR THEN');
    const iRollback = sql.indexOf('ROLLBACK TRANSACTION');

    expect(iBegin).toBeGreaterThan(-1);
    expect(iDelete).toBeGreaterThan(iBegin);
    expect(iInsert).toBeGreaterThan(iDelete);
    expect(iCommit).toBeGreaterThan(iInsert);
    expect(iHandler).toBeGreaterThan(iCommit);
    expect(iRollback).toBeGreaterThan(iHandler);

    // каждая проверка, способная поднять ошибку, обязана стоять до COMMIT
    const gates = [...sql.matchAll(/RAISE USING MESSAGE = FORMAT\('([A-Z_]+)/g)];
    expect(gates.length).toBeGreaterThanOrEqual(7);
    for (const g of gates) expect(g.index).toBeLessThan(iCommit);
    expect(gates.map((g) => g[1])).toContain('WB_STORAGE_TARGET_ROWS');
    expect(gates.map((g) => g[1])).toContain('WB_STORAGE_STAGE_ROWS');

    // гранулярность замены — строго окно дат
    expect(sql).toContain('DELETE FROM `p.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(@start) AND DATE(@end)');
    expect(sql).toContain('INSERT INTO `p.wb_raw.RAW_WB_PAID_STORAGE` SELECT * FROM `p.wb_raw.RAW_WB_PAID_STORAGE__STAGE`');
    expect(sql.match(/COMMIT TRANSACTION/g)).toHaveLength(1);
  });

  it('провал блокирующей проверки ДО DML → нет COMMIT, target не изменён', async () => {
    const before = preExisting();
    const bq = new FakeBq(before.map((r) => ({ ...r })));
    // stage расходится с источником по сумме: число строк и дней прежнее, поэтому
    // предварительный count проходит и мы доходим именно до гейта внутри транзакции
    bq.corruptStage = (rows) => rows.map((r, i) => (i === 0 ? { ...r, warehouse_price: 777 } : r));

    await expect(replace(bq)).rejects.toThrow('WB_STORAGE_STAGE_AMOUNT');
    expect(bq.committed).toBe(0);
    expect(bq.target).toEqual(before);
  });

  it('провал блокирующей проверки ПОСЛЕ DML, но до COMMIT → ROLLBACK, target не изменён', async () => {
    const before = preExisting();
    const bq = new FakeBq(before.map((r) => ({ ...r })));
    bq.loseRowOnInsert = true; // target после INSERT не совпадёт с источником

    await expect(replace(bq)).rejects.toThrow('WB_STORAGE_TARGET_ROWS');
    expect(bq.committed).toBe(0);
    expect(bq.rolledBack).toBe(1);
    expect(bq.target).toEqual(before); // именно это ломалось 14.09.2026
  });

  it('успешный путь: заменено только окно [start,end], строки 01–05 вне окна не тронуты', async () => {
    const before = preExisting();
    const bq = new FakeBq(before.map((r) => ({ ...r })));
    const loaded = await replace(bq);

    expect(loaded).toBe(4);
    expect(bq.committed).toBe(1);

    const outside = bq.target.filter((r) => r.date_msk < '2026-09-10');
    expect(outside).toEqual(before.filter((r) => r.date_msk < '2026-09-10'));
    expect(outside.map((r) => r.warehouse_price)).toEqual([140.33, 136.82, 134.69, 130.49, 129.11]);

    const inside = bq.target.filter((r) => r.date_msk >= '2026-09-10');
    expect(inside.map((r) => r.date_msk)).toEqual(WINDOW);
    expect(inside.every((r) => r.warehouse_price === 12.34)).toBe(true); // старые 999 вытеснены
  });

  it('повтор того же окна идемпотентен: состояние target не меняется', async () => {
    const bq = new FakeBq(preExisting());
    await replace(bq);
    const afterFirst = bq.target.map((r) => ({ ...r }));
    const loaded = await replace(bq);

    expect(loaded).toBe(4);
    expect(bq.committed).toBe(2);
    expect(bq.target).toEqual(afterFirst);
    expect(bq.target.filter((r) => r.date_msk >= '2026-09-10')).toHaveLength(4); // без задвоения
  });
});
