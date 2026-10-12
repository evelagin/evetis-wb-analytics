import { describe, it, expect } from 'vitest';
import { createHash } from 'node:crypto';
import {
  financeWatermark, financeWatermarkSql, FINANCE_WATERMARK_VERSION, FINANCE_WATERMARK_TIMEOUT_MS,
} from '../src/loaders/mart/watermark.js';
import { LOADERS } from '../src/loaders/registry.js';
import type { QueryRunner } from '../src/loaders/mart/bq.js';

/**
 * Версия финансов для guard витрины (fin:v2).
 *  - SQL: детерминирован, читает ровно вход FACT_FINANCE, сериализует строку целиком, без XOR.
 *  - Эталонная модель алгоритма на TS (тот же конвейер: SHA256(JSON строки) → сортировка →
 *    SHA256 по report_id → SHA256 по отсортированным партициям) и «нет ложного NOOP»:
 *    каждая денежная/полевая/составная правка меняет версию. Те же правки прогнаны живым
 *    read-only SELECT в BigQuery через CTE-инъекции (IMPLEMENTATION.txt §2).
 */

const sha = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex');

type FinRow = Record<string, string | number | null>;

/** Эталон SQL financeWatermarkSql: TO_JSON_STRING(строка) ≈ JSON.stringify с фиксированным порядком ключей. */
function modelWatermark(rows: FinRow[]): string {
  const parts = new Map<string, string[]>();
  for (const r of rows) {
    const key = JSON.stringify(r.report_id ?? null);
    const list = parts.get(key) ?? [];
    list.push(sha(JSON.stringify(r)));
    parts.set(key, list);
  }
  const entries = [...parts.entries()]
    .map(([k, ds]) => ({ k, n: ds.length, d: sha([...ds].sort().join('')) }))
    .sort((a, b) => (a.k < b.k ? -1 : a.k > b.k ? 1 : 0))
    .map((e) => `${e.k}=${e.n}:${e.d}`);
  return `fin:v2:${rows.length}:${sha(entries.join('\n'))}`;
}

const row = (o: Partial<FinRow>): FinRow => ({
  report_id: '869881826', rrd_id: '1', supplier_oper_name: 'Продажа', doc_type_name: 'Продажа',
  rr_dt: '2026-10-06', _rr_date: '2026-10-06', for_pay: '512.34', retail_price_withdisc_rub: '980.00',
  deduction: null, internal_sku: 'EV-001', source_layer: 'WEEKLY', finance_status: 'FINAL',
  marketplace_fee_gap_rub: 467.66, ...o,
});

const BASE: FinRow[] = [
  row({ rrd_id: '1' }),
  row({ rrd_id: '2', for_pay: '100.00', marketplace_fee_gap_rub: 50 }),
  row({ rrd_id: '3', supplier_oper_name: 'Логистика', for_pay: '0', marketplace_fee_gap_rub: null }),
  row({ report_id: '862314061', rrd_id: '9', rr_dt: '2026-09-29', _rr_date: '2026-09-29' }),
  row({ report_id: '409455520261011', rrd_id: '5', source_layer: 'DAILY', finance_status: 'PROVISIONAL' }),
];
const W_BASE = modelWatermark(BASE);
const edit = (i: number, o: Partial<FinRow>) => BASE.map((r, j) => (j === i ? { ...r, ...o } : r));

describe('fin:v2 — SQL', () => {
  const sql = financeWatermarkSql('proj');
  it('читает ровно вход FACT_FINANCE (wb_raw.V_WB_FINANCE_SEMANTIC) и ничего больше', () => {
    expect(sql).toContain('`proj.wb_raw.V_WB_FINANCE_SEMANTIC` s');
    expect(sql.match(/`proj\.[a-z_]+\.[A-Z_]+`/g)).toEqual(['`proj.wb_raw.V_WB_FINANCE_SEMANTIC`']);
  });
  it('строка сериализуется ЦЕЛИКОМ (без исключений колонок), хешируется SHA256', () => {
    expect(sql).toContain('TO_HEX(SHA256(TO_JSON_STRING(s)))');
    expect(sql).not.toMatch(/EXCEPT\s*\(/i);
  });
  it('без XOR/FARM_FINGERPRINT; агрегаты упорядочены (независимость от порядка строк)', () => {
    expect(sql).not.toMatch(/BIT_XOR|FARM_FINGERPRINT/i);
    expect(sql).toContain("STRING_AGG(row_digest, '' ORDER BY row_digest)");
    expect(sql).toContain('ORDER BY part_key');
    expect(sql).toContain('GROUP BY part_key');
    expect(sql).toContain('TO_JSON_STRING(s.report_id) AS part_key');
  });
  it('только чтение', () => {
    expect(sql.trim()).toMatch(/^WITH /);
    expect(sql).not.toMatch(/\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|CALL)\b/i);
  });
  it('детерминирован: одинаковый текст при повторной генерации; версия функции в префиксе', () => {
    expect(financeWatermarkSql('proj')).toBe(sql);
    expect(FINANCE_WATERMARK_VERSION).toBe('fin:v2');
    expect(sql).toContain("CONCAT('fin:v2:'");
  });
});

describe('fin:v2 — зонд', () => {
  const runner = (rows: unknown[]): QueryRunner & { opts: unknown[] } => {
    const opts: unknown[] = [];
    return {
      projectId: 'proj', opts,
      query: async (_q: string, _p?: unknown, _t?: unknown, o?: unknown) => { opts.push(o); return rows as never[]; },
    };
  };
  it('возвращает версию и задаёт серверный таймаут', async () => {
    const r = runner([{ watermark: 'fin:v2:208423:' + 'f'.repeat(64) }]);
    await expect(financeWatermark(r)).resolves.toBe('fin:v2:208423:' + 'f'.repeat(64));
    expect(r.opts[0]).toEqual({ jobTimeoutMs: FINANCE_WATERMARK_TIMEOUT_MS });
  });
  it('неожиданная форма ответа = отказ (не «версия»)', async () => {
    await expect(financeWatermark(runner([]))).rejects.toThrow();
    await expect(financeWatermark(runner([{ watermark: null }]))).rejects.toThrow();
    await expect(financeWatermark(runner([{ watermark: 'fin:v1:1:2' }]))).rejects.toThrow();
    await expect(financeWatermark(runner([{ watermark: 'fin:v2:1:' + 'F'.repeat(64) }]))).rejects.toThrow();
  });
});

describe('fin:v2 — «нет ложного NOOP» (эталонная модель)', () => {
  it('детерминизм и независимость от порядка строк', () => {
    expect(modelWatermark(BASE)).toBe(W_BASE);
    expect(modelWatermark([...BASE].reverse())).toBe(W_BASE);
    expect(W_BASE).toMatch(/^fin:v2:5:[0-9a-f]{64}$/);
  });
  it('сумма существующей операции (for_pay 512.34 → 512.35)', () => {
    expect(modelWatermark(edit(0, { for_pay: '512.35' }))).not.toBe(W_BASE);
  });
  it('финансово значимое поле (deduction NULL → 0)', () => {
    expect(modelWatermark(edit(2, { deduction: '0' }))).not.toBe(W_BASE);
  });
  it('дата операции', () => {
    expect(modelWatermark(edit(1, { rr_dt: '2026-10-07', _rr_date: '2026-10-07' }))).not.toBe(W_BASE);
  });
  it('категория операции', () => {
    expect(modelWatermark(edit(2, { supplier_oper_name: 'Хранение' }))).not.toBe(W_BASE);
  });
  it('привязка к SKU (справочник поменял internal_sku)', () => {
    expect(modelWatermark(edit(0, { internal_sku: 'EV-002' }))).not.toBe(W_BASE);
  });
  it('удаление + добавление при РАВНОМ числе строк', () => {
    const swapped = [...BASE.slice(1), row({ rrd_id: '77', for_pay: '512.34' })];
    expect(swapped).toHaveLength(BASE.length);
    expect(modelWatermark(swapped)).not.toBe(W_BASE);
  });
  it('дубль строки (XOR дал бы 0 и скрыл бы его)', () => {
    expect(modelWatermark([...BASE, BASE[0]!])).not.toBe(W_BASE);
    // два одинаковых дубля тоже видны (XOR пары = 0)
    expect(modelWatermark([...BASE, BASE[0]!, BASE[0]!])).not.toBe(W_BASE);
  });
  it('обмен значениями между двумя строками (сумма по столбцу та же)', () => {
    const x = BASE.map((r) => ({ ...r }));
    [x[0]!.for_pay, x[1]!.for_pay] = [x[1]!.for_pay!, x[0]!.for_pay!];
    expect(modelWatermark(x)).not.toBe(W_BASE);
  });
  it('поздняя корректировка давнего периода (строка отчёта сентября)', () => {
    expect(modelWatermark(edit(3, { for_pay: '1.00' }))).not.toBe(W_BASE);
    expect(modelWatermark([...BASE, row({ report_id: '862314061', rrd_id: '10', rr_dt: '2026-09-28' })])).not.toBe(W_BASE);
  });
  it('перенос строки в другой отчёт (DAILY → WEEKLY вытеснение)', () => {
    expect(modelWatermark(edit(4, { report_id: '869881826', source_layer: 'WEEKLY', finance_status: 'FINAL' }))).not.toBe(W_BASE);
  });
  it('NULL и пустая строка различимы', () => {
    expect(modelWatermark(edit(2, { deduction: '' }))).not.toBe(W_BASE);
  });
  it('пустой источник — валидная версия, отличная от непустого', () => {
    expect(modelWatermark([])).toMatch(/^fin:v2:0:[0-9a-f]{64}$/);
    expect(modelWatermark([])).not.toBe(W_BASE);
  });
});

describe('реестр: source-aware guard только у mart', () => {
  it('mart имеет sourceWatermark с непустым кодом отказа', () => {
    expect(LOADERS.mart.sourceWatermark).toBeDefined();
    expect(LOADERS.mart.sourceWatermark!.probeFailureCode).toBe('MART_SOURCE_WATERMARK_PROBE_FAILED');
    expect(LOADERS.mart.sourceWatermark!.changedDuringBuildCode).toBe('MART_SOURCE_CHANGED_DURING_BUILD');
  });
  it('у всех прочих загрузчиков (включая Ozon) его нет', () => {
    for (const [name, spec] of Object.entries(LOADERS)) {
      if (name === 'mart') continue;
      expect(spec.sourceWatermark, name).toBeUndefined();
    }
  });
});
