import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import {
  martWatermark, stocksWatermark, stocksWatermarkSql, STOCKS_WATERMARK_VERSION, FINANCE_WATERMARK_TIMEOUT_MS,
} from '../src/loaders/mart/watermark.js';
import type { QueryRunner } from '../src/loaders/mart/bq.js';

/**
 * Версия источника витрины включает остатки (stk:v1). Инцидент 28.10.2026: снимок остатков восстановлен
 * после дневных слотов, guard сравнивал только финансы → guard_skip, FACT_STOCKS_SNAPSHOT ждал суток.
 * Живая read-only проверка 28.10: отпечаток без восстановленного манифеста ≠ отпечатку с ним
 * (docs/ops/MART_GUARD_STOCKS_WATERMARK_2026-10-28.md).
 */

const FIN = 'fin:v2:209492:' + 'a'.repeat(64);
const STK = 'stk:v1:94:' + 'b'.repeat(64);

const runner = (answers: unknown[][]): QueryRunner & { sqls: string[]; opts: unknown[] } => {
  const sqls: string[] = [];
  const opts: unknown[] = [];
  let i = 0;
  return {
    projectId: 'proj', sqls, opts,
    query: async (q: string, _p?: unknown, _t?: unknown, o?: unknown) => {
      sqls.push(q); opts.push(o);
      const a = answers[i++];
      if (a === undefined) throw new Error('лишний запрос');
      return a as never[];
    },
  };
};

describe('stk:v1 — SQL', () => {
  const sql = stocksWatermarkSql('proj');
  it('читает ровно журнал снимков остатков и ничего больше', () => {
    expect(sql.match(/`proj\.[a-z_]+\.[A-Z_]+`/g)).toEqual(['`proj.wb_raw.WB_STOCKS_SNAPSHOTS`']);
  });
  it('выбор снимка дословно как в sp_bootstrap_facts (FACT_STOCKS_SNAPSHOT__BUILD)', () => {
    const proc = readFileSync(new URL('../../sql/mart/pr_mart1_facts.sql', import.meta.url), 'utf8');
    const pick = "WHERE status = 'COMPLETE' AND period_to IS NOT NULL\n" +
      '  QUALIFY ROW_NUMBER() OVER (PARTITION BY period_to ORDER BY started_at DESC) = 1';
    expect(sql).toContain(pick);
    expect(proc.replace(/\n {8}/g, '\n  ')).toContain(pick);
  });
  it('строка выбора сериализуется целиком, агрегат упорядочен, без XOR', () => {
    expect(sql).toContain('TO_JSON_STRING(p)');
    expect(sql).toContain('ORDER BY p.period_to');
    expect(sql).not.toMatch(/BIT_XOR|FARM_FINGERPRINT/i);
  });
  it('только чтение; версия в префиксе', () => {
    expect(sql.trim()).toMatch(/^WITH /);
    expect(sql).not.toMatch(/\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|CALL)\b/i);
    expect(STOCKS_WATERMARK_VERSION).toBe('stk:v1');
    expect(sql).toContain("CONCAT('stk:v1:'");
  });
});

describe('версия источника витрины = финансы | остатки', () => {
  it('составная версия, оба зонда с серверным таймаутом', async () => {
    const r = runner([[{ watermark: FIN }], [{ watermark: STK }]]);
    await expect(martWatermark(r)).resolves.toBe(`${FIN}|${STK}`);
    expect(r.sqls[0]).toContain('V_WB_FINANCE_SEMANTIC');
    expect(r.sqls[1]).toContain('WB_STOCKS_SNAPSHOTS');
    expect(r.opts).toEqual([{ jobTimeoutMs: FINANCE_WATERMARK_TIMEOUT_MS }, { jobTimeoutMs: FINANCE_WATERMARK_TIMEOUT_MS }]);
  });
  it('новый снимок остатков при тех же финансах → другая версия (guard не пропустит слот)', async () => {
    const before = await martWatermark(runner([[{ watermark: FIN }], [{ watermark: STK }]]));
    const after = await martWatermark(runner([[{ watermark: FIN }], [{ watermark: 'stk:v1:95:' + 'c'.repeat(64) }]]));
    expect(after).not.toBe(before);
  });
  it('прежняя версия только финансов не равна новой — первый запуск после выпуска пересобирает один раз', async () => {
    expect(await martWatermark(runner([[{ watermark: FIN }], [{ watermark: STK }]]))).not.toBe(FIN);
  });
  it('отказ или неожиданная форма зонда остатков = отказ версии целиком', async () => {
    await expect(martWatermark(runner([[{ watermark: FIN }], []]))).rejects.toThrow();
    await expect(martWatermark(runner([[{ watermark: FIN }], [{ watermark: null }]]))).rejects.toThrow();
    await expect(stocksWatermark(runner([[{ watermark: 'stk:v1:1:' + 'F'.repeat(64) }]]))).rejects.toThrow();
    await expect(stocksWatermark(runner([[{ watermark: 'fin:v2:1:' + 'f'.repeat(64) }]]))).rejects.toThrow();
  });
});
