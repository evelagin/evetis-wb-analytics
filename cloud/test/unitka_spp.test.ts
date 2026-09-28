/**
 * SPP-3 — план колонки AB (СПП) из wb_mart.V_WB_SPP_DAILY: классы ячеек, границы дат, идемпотентность,
 * перечитывание (значение и формат числа), манифест отката. Чистые функции, без Sheets и BigQuery.
 */
import { describe, it, expect } from 'vitest';
import {
  planSpp, sppWindow, sppWriteValue, sppEqual, verifySppReadback, buildSppManifest, encodeSppManifest, parseSppManifest,
  sppWriteRanges, sppClearRanges, SPP_START, type SppPlan,
} from '../src/loaders/unitka/spp.js';
import { OFFSET, colA1, isoToSerial, addDaysIso, type CellValue } from '../src/loaders/unitka/model.js';
import type { Snapshot } from '../src/loaders/unitka/plan.js';
import type { SppDayRow } from '../src/loaders/unitka/bq.js';
import { snapshot, NM_IDS, GRID, dayRow } from './unitka_fixture.js';
import { sectionFromSpec, septemberSpec, WIDTH_SEPT } from './unitka_calendar_fixture.js';

const AB = (b: number): number => GRID.B0 + b * GRID.BW + OFFSET.spp;
const Q = (b: number): number => GRID.B0 + b * GRID.BW + OFFSET.orders;
const setCell = (s: Snapshot, row: number, col: number, v: CellValue): void => {
  const r = s.grid[row - s.geometry.topRow]!; while (r.length < col) r.push(''); r[col - 1] = v;
};
const day = (i: number): string => addDaysIso('2026-09-01', i);
const row = (nm: number, i: number, eff: number | null, status = 'OK'): SppDayRow =>
  ({ nmId: nm, date: day(i), effectiveSppPct: eff, status, ordersQty: 1 });
const CAND = '2026-09-10';

/** Книга: сентябрь с фактами по 10.09 и ручными AB, как в production до миграции. */
function book(): Snapshot {
  const s = snapshot({ lcdInSheet: CAND });
  for (let i = 0; i < 10; i++) setCell(s, dayRow(i), AB(0), 20);     // блок 1: ручные 20 % на все закрытые дни
  setCell(s, dayRow(2), Q(1), 0); setCell(s, dayRow(2), AB(1), 30);  // блок 2, 03.09: ручное при Q = 0
  setCell(s, dayRow(4), AB(2), 12);                                  // блок 3, 05.09: ручное при Q > 0 без строки вью (воронка)
  setCell(s, dayRow(20), AB(3), 40);                                 // блок 4, 21.09: будущий день с ручным значением
  return s;
}
function rows(): SppDayRow[] {
  const out: SppDayRow[] = [];
  for (let i = 0; i < 10; i++) out.push(row(NM_IDS[0]!, i, 41.84));   // блок 1: факт 41,84 → 41,8
  out.push(row(NM_IDS[3]!, 6, -7.65, 'NEGATIVE_MARKUP'));             // блок 4, 07.09: рассрочка
  out.push(row(NM_IDS[4]!, 7, null, 'PRICE_DATA_MISSING'));           // блок 5, 08.09: цены нет
  out.push(row(NM_IDS[5]!, 8, 0, 'ZERO_SPP'));                        // блок 6, 09.09: СПП 0 — факт
  out.push(row(NM_IDS[0]!, 15, 45));                                  // после кандидата — не пишется
  return out;
}
const byCls = (p: SppPlan) => p.counts.by_class;

describe('контракт записи AB', () => {
  it('окно: с 01.09.2026, не раньше окна сверки, по кандидата; до старта — нет окна', () => {
    expect(sppWindow('2026-08-31')).toBeNull();
    expect(sppWindow('2026-09-10')).toEqual({ from: SPP_START, to: '2026-09-10' });
    expect(sppWindow('2026-11-20')).toEqual({ from: addDaysIso('2026-11-20', -34), to: '2026-11-20' });
  });
  it('значение: п.п., MAX(effective, 0), шаг 0,1; NULL — не выдумывается', () => {
    expect(sppWriteValue({ effectiveSppPct: 41.84 })).toBe(41.8);
    expect(sppWriteValue({ effectiveSppPct: 41.85 })).toBe(41.9);
    expect(sppWriteValue({ effectiveSppPct: -7.65 })).toBe(0);
    expect(sppWriteValue({ effectiveSppPct: 0 })).toBe(0);
    expect(sppWriteValue({ effectiveSppPct: null })).toBeNull();
    expect(sppEqual('', null)).toBe(true);
    expect(sppEqual(0, null)).toBe(false);                            // 0 — факт, не пустота
    expect(sppEqual(41.8, 41.8)).toBe(true);
  });
});

describe('план: каждая изменяемая ячейка объяснена классом', () => {
  const p = planSpp({ sections: [book()], rows: rows(), candidate: CAND });
  const cell = (b: number, i: number) => p.cells.find((c) => c.col === AB(b) && c.row === dayRow(i));

  it('факт заменяет ручное значение; факт = 0 при рассрочке; 0 остаётся 0', () => {
    expect(cell(0, 0)).toMatchObject({ before: 20, want: 41.8, cls: 'ACTUAL_REPLACE', date: '2026-09-01', nmId: NM_IDS[0], a1: `${colA1(AB(0))}${dayRow(0)}` });
    expect(cell(3, 6)).toMatchObject({ before: '', want: 0, cls: 'NEGATIVE_MARKUP_TO_ZERO' });
    expect(cell(5, 8)).toMatchObject({ want: 0, cls: 'ACTUAL_FILL' });
    expect(cell(4, 7)).toBeUndefined();                              // PRICE_DATA_MISSING при пустой ячейке — нечего менять
  });
  it('ручное без строки вью очищается: при Q = 0 и при Q > 0 (воронка) — разные классы', () => {
    expect(cell(1, 2)).toMatchObject({ before: 30, want: null, cls: 'MANUAL_CLEAR_NO_ORDER' });
    expect(cell(2, 4)).toMatchObject({ before: 12, want: null, cls: 'MANUAL_CLEAR_FUNNEL_ONLY' });
  });
  it('будущее и даты после кандидата не трогаются; счётчики границ = 0', () => {
    expect(p.cells.every((c) => c.date >= SPP_START && c.date <= CAND)).toBe(true);
    expect(p.counts).toMatchObject({ dates_before_start_touched: 0, future_dates_touched: 0, dates_after_candidate_touched: 0, future_nonblank_untouched: 1 });
    expect(cell(0, 15)).toBeUndefined();
  });
  it('итоги классов и счётчики совпадают с ячейками плана', () => {
    expect(byCls(p)).toEqual({ ACTUAL_REPLACE: 10, NEGATIVE_MARKUP_TO_ZERO: 1, ACTUAL_FILL: 1, MANUAL_CLEAR_NO_ORDER: 1, MANUAL_CLEAR_FUNNEL_ONLY: 1 });
    expect(p.counts.cells_to_write).toBe(12);
    expect(p.counts.cells_to_clear).toBe(2);
    expect(p.counts.negative_markup_to_zero).toBe(1);
    // Q > 0 без строки вью — все блоки/дни, где заказы есть, а СПП нет (в фикстуре заказы у всех SKU)
    expect(p.counts.missing_with_q_gt_0).toBeGreaterThan(0);
  });
  it('запись — только RAW-числа п.п.; очистка — отдельными диапазонами batchClear (формат не стирается)', () => {
    const r = sppWriteRanges(p.cells, 'WB_Юнит_2025');
    expect(r).toContainEqual({ range: `'WB_Юнит_2025'!${colA1(AB(0))}${dayRow(0)}:${colA1(AB(0))}${dayRow(0)}`, values: [[41.8]] });
    expect(r.some((x) => x.values[0]![0] === '')).toBe(false);
    expect(r).toHaveLength(12);
    const cl = sppClearRanges(p.cells, 'WB_Юнит_2025');
    expect(cl).toContain(`'WB_Юнит_2025'!${colA1(AB(1))}${dayRow(2)}:${colA1(AB(1))}${dayRow(2)}`);
    expect(cl).toHaveLength(2);
  });
});

describe('границы: до 01.09 не трогается никогда', () => {
  it('август в окне сверки (кандидат 10.09) — ни одной ячейки, даже при ручных значениях и строках вью', () => {
    const aug = sectionFromSpec({ year: 2026, month: 8 }, 700, septemberSpec(), WIDTH_SEPT);
    const augRow = aug.geometry.firstDailyRow + 30;                    // 31.08
    setCell(aug, augRow, 13 + OFFSET.spp, 25);
    const r = [...rows(), { nmId: Number(String(aug.grid[0]![12]).split(' ')[0]), date: '2026-08-31', effectiveSppPct: 33, status: 'OK', ordersQty: 1 }];
    const p = planSpp({ sections: [aug, book()], rows: r, candidate: CAND });
    expect(p.cells.some((c) => c.date < SPP_START)).toBe(false);
    expect(p.counts.dates_before_start_touched).toBe(0);
  });
});

describe('идемпотентность и поздние заказы', () => {
  it('после применения плана повторный план пуст (SPP_VALUE_MUTATIONS = 0)', () => {
    const s = book();
    const p = planSpp({ sections: [s], rows: rows(), candidate: CAND });
    for (const c of p.cells) setCell(s, c.row, c.col, c.want === null ? '' : c.want);
    const again = planSpp({ sections: [s], rows: rows(), candidate: CAND });
    expect(again.cells).toEqual([]);
    expect(again.counts.already_correct).toBe(p.counts.cells_to_write);
  });
  it('WB изменил заказы прошлого закрытого дня → меняется ровно эта ячейка', () => {
    const s = book();
    for (const c of planSpp({ sections: [s], rows: rows(), candidate: CAND }).cells) setCell(s, c.row, c.col, c.want === null ? '' : c.want);
    const late = rows().map((r) => (r.nmId === NM_IDS[0] && r.date === '2026-09-03' ? { ...r, effectiveSppPct: 38.26 } : r));
    const p = planSpp({ sections: [s], rows: late, candidate: CAND });
    expect(p.cells.map((c) => [c.date, c.before, c.want, c.cls])).toEqual([['2026-09-03', 41.8, 38.3, 'ACTUAL_REPLACE']]);
  });
});

describe('перечитывание: значение и формат числа каждой ячейки', () => {
  const p = planSpp({ sections: [book()], rows: rows(), candidate: CAND });
  const applied = (): Snapshot => { const s = book(); for (const c of p.cells) setCell(s, c.row, c.col, c.want === null ? '' : c.want); return s; };
  it('всё легло — PASS', () => {
    const s = applied();
    expect(verifySppReadback(p.cells, () => s)).toMatchObject({ name: 'SPP_READBACK', pass: true, count: 0 });
  });
  it('ячейка не легла — FAIL с адресом', () => {
    const s = applied(); setCell(s, dayRow(0), AB(0), 20);
    const r = verifySppReadback(p.cells, () => s);
    expect(r.pass).toBe(false);
    expect(r.sample[0]).toContain(`${colA1(AB(0))}${dayRow(0)}`);
  });
  it('формат числа изменился (40,0 → 4000 %) — FAIL', () => {
    const s = applied();
    s.formats[0]![AB(0) - 1] = { bg: null, fg: null, numberFormat: { type: 'PERCENT', pattern: '0.0%' } };
    expect(verifySppReadback(p.cells, () => s).sample.join(' ')).toContain('PERCENT');
  });
});

describe('манифест отката', () => {
  const p = planSpp({ sections: [book()], rows: rows(), candidate: CAND });
  const m = buildSppManifest(p, { spreadsheetId: 'ssid', sheetId: 739487431, sheetName: 'WB_Юнит_2025' });
  it('каждая изменяемая ячейка: прежнее значение, формула, формат, новое значение; отпечаток устойчив', () => {
    expect(m.cells).toHaveLength(p.cells.length);
    expect(m.cells[0]).toMatchObject({ previousValue: 20, newValue: 41.8, cls: 'ACTUAL_REPLACE' });
    expect(buildSppManifest(p, { spreadsheetId: 'ssid', sheetId: 739487431, sheetName: 'WB_Юнит_2025' }).digest).toBe(m.digest);
    expect(m.digest).toMatch(/^[0-9a-f]{64}$/);
  });
  it('gzip-кодирование переживает разбор; подмена содержимого и чужая колонка — отказ', () => {
    const enc = encodeSppManifest(m);
    expect(enc.startsWith('gz.')).toBe(true);
    expect(parseSppManifest(enc)).toEqual(m);
    const tampered = { ...m, cells: m.cells.map((c, i) => (i === 0 ? { ...c, previousValue: 99 } : c)) };
    expect(parseSppManifest(JSON.stringify(tampered))).toMatchObject({ error: expect.stringContaining('отпечаток') });
    const foreign = { ...m, cells: [{ ...m.cells[0]!, col: 14, a1: `N${dayRow(0)}` }], digest: 'x' };
    expect(parseSppManifest(JSON.stringify(foreign))).toHaveProperty('error');
  });
  it('размер: манифест 300 ячеек в gzip помещается в переменную окружения (< 16 КБ)', () => {
    const big = { ...m, cells: Array.from({ length: 300 }, (_, i) => ({ ...m.cells[0]!, a1: `AB${737 + (i % 30)}`, date: addDaysIso(SPP_START, i % 30) })) };
    expect(encodeSppManifest(big).length).toBeLessThan(16_000);
  });
});

it('серийные даты фикстуры = календарь (страховка тестов)', () => {
  expect(isoToSerial('2026-09-01')).toBe(book().grid[dayRow(0) - GRID.TOP]![1]);
});

describe('активация: режим AB не включается доставкой', () => {
  it('ни один workflow и ни один Terraform-файл не задают UNITKA_SPP_MODE (по умолчанию в коде — off; режим ставит оператор)', async () => {
    const { readFileSync } = await import('node:fs');
    const { resolve } = await import('node:path');
    const root = resolve(__dirname, '../..');
    for (const f of ['.github/workflows/deploy-shadow.yml', '.github/workflows/deploy-prod.yml', '.github/workflows/infra.yml', 'infra/terraform/unitka_engine.tf']) {
      const code = readFileSync(resolve(root, f), 'utf8').split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');
      expect(code, f).not.toMatch(/UNITKA_SPP_MODE/);
    }
  });
});
