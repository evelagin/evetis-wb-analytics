/**
 * Идемпотентность записи наблюдателя акций.
 *
 * Дефект, пойманный первым боевым прогоном 2026-09-22: исход REUSED считался по
 * ВСЕМ трём таблицам, а пустая запись возвращает LOADED не обращаясь к BigQuery.
 * Состав акций сегодня пуст всегда (автоакции его не отдают), поэтому повтор
 * слота навсегда оставался бы COMPLETE, хотя строки дедуплицировались. Данные
 * были целы, врал манифест — а он и есть операционный сигнал.
 */
import { describe, it, expect } from 'vitest';
import { PromoBq, isAlreadyExists, type BqLike } from '../src/loaders/promo/bq.js';

function fakeBq(onLoad: (table: string, jobId: string) => void, failWith?: unknown): BqLike {
  return {
    query: async () => [[]],
    dataset: () => ({
      table: (t: string) => ({
        load: async (_src: string, md: { jobId?: string }) => {
          onLoad(t, md.jobId ?? '');
          if (failWith) throw failWith;
          return undefined;
        },
      }),
    }),
  };
}

describe('isAlreadyExists — распознавание повтора load-джобы', () => {
  it('409 и текст BigQuery распознаются как повтор, а не как сбой', () => {
    expect(isAlreadyExists({ code: 409 })).toBe(true);
    expect(isAlreadyExists(new Error('Already Exists: Job project:EU.wbpromo_prod_x'))).toBe(true);
  });
  it('прочие ошибки повтором не считаются', () => {
    expect(isAlreadyExists(new Error('Quota exceeded'))).toBe(false);
    expect(isAlreadyExists({ code: 500 })).toBe(false);
  });
});

describe('appendRows', () => {
  it('пустой набор не обращается к BigQuery вовсе', async () => {
    const seen: string[] = [];
    const bq = new PromoBq('p', 'EU', 'wb_raw', fakeBq((t) => seen.push(t)));
    expect(await bq.appendRows('RAW_WB_PROMO_NOMENCLATURE', [], 'job-1')).toBe('LOADED');
    expect(seen).toEqual([]);
  });

  it('повтор того же jobId — REUSED, а не вторая копия строк', async () => {
    const bq = new PromoBq('p', 'EU', 'wb_raw', fakeBq(
      () => undefined,
      Object.assign(new Error('Already Exists: Job'), { code: 409 }),
    ));
    expect(await bq.appendRows('RAW_WB_PROMO_CALENDAR', [{ a: 1 }], 'job-1')).toBe('REUSED');
  });

  it('настоящая ошибка записи не маскируется под REUSED', async () => {
    const bq = new PromoBq('p', 'EU', 'wb_raw', fakeBq(() => undefined, new Error('Access Denied')));
    await expect(bq.appendRows('RAW_WB_PROMO_CALENDAR', [{ a: 1 }], 'job-1')).rejects.toThrow(/Access Denied/);
  });

  it('jobId уходит в load-джобу — на нём и держится дедупликация', async () => {
    const jobs: string[] = [];
    const bq = new PromoBq('p', 'EU', 'wb_raw', fakeBq((_t, j) => jobs.push(j)));
    await bq.appendRows('RAW_WB_PROMO_CALENDAR', [{ a: 1 }], 'wbpromo_prod_202609221400_raw_wb_promo_calendar');
    expect(jobs).toEqual(['wbpromo_prod_202609221400_raw_wb_promo_calendar']);
  });
});

describe('вычисление REUSED по непустым записям', () => {
  /** Та же логика, что в index.ts: исход пустой записи не участвует в решении. */
  const isReused = (appends: Array<[string, number]>): boolean => {
    const meaningful = appends.filter(([, n]) => n > 0);
    return meaningful.length > 0 && meaningful.every(([o]) => o === 'REUSED');
  };

  it('повтор слота при пустом составе акций всё равно даёт REUSED', () => {
    expect(isReused([['REUSED', 11], ['REUSED', 26], ['LOADED', 0]])).toBe(true);
  });

  it('первый прогон слота — не REUSED', () => {
    expect(isReused([['LOADED', 11], ['LOADED', 26], ['LOADED', 0]])).toBe(false);
  });

  it('частичный повтор не выдаётся за полный', () => {
    expect(isReused([['REUSED', 11], ['LOADED', 26], ['LOADED', 0]])).toBe(false);
  });

  it('снимок, в котором вообще нечего писать, REUSED не считается', () => {
    expect(isReused([['LOADED', 0], ['LOADED', 0], ['LOADED', 0]])).toBe(false);
  });
});
