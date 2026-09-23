/**
 * Схема ZZ_CONFIG: блок Gate 10 не должен наезжать на живые финансовые параметры.
 *
 * В задании гейта Ozon-LCD был назван «ZZ_CONFIG!B3». Замер живого листа показал, что B3
 * ЗАНЯТА ставкой TOTAL_COMMISSION_RATE = 0,4532, которая через AD входит в AE = AA − AA×AD.
 * Тест фиксирует именно это: строки 2–4 неприкосновенны.
 */
import { describe, it, expect } from 'vitest';
import {
  GATE10_PARAMS, ZZ_CONFIG_RESERVED_ROWS, GATE10_BLOCK_ROW, assertNoCollision,
  gate10SeedCells, a1, labelA1, OZON_LCD_NAMED_RANGE, WB_LCD_NAMED_RANGE,
  type ConfigParam,
} from '../src/loaders/unitka/config_sheet.js';

const params = Object.values(GATE10_PARAMS) as ConfigParam[];

describe('блок Gate 10 не пересекается с живой экономикой', () => {
  it('B2/B3/B4 зарезервированы и ни один параметр цикла туда не метит', () => {
    expect(ZZ_CONFIG_RESERVED_ROWS[2]).toBe('LAST_CLOSED_DATE');
    expect(ZZ_CONFIG_RESERVED_ROWS[3]).toBe('TOTAL_COMMISSION_RATE');
    expect(ZZ_CONFIG_RESERVED_ROWS[4]).toBe('TAX_RESERVE_RATE');
    for (const p of params) expect(ZZ_CONFIG_RESERVED_ROWS[p.row], p.label).toBeUndefined();
  });

  it('🔴 Ozon-LCD НЕ в B3: там ставка комиссии, её затирание исказило бы AE', () => {
    expect(a1(GATE10_PARAMS.ozonLcd)).not.toBe('ZZ_CONFIG!B3');
    expect(a1(GATE10_PARAMS.ozonLcd)).toBe('ZZ_CONFIG!B30');
  });

  it('весь блок лежит ниже занятой области', () => {
    for (const p of params) expect(p.row, p.label).toBeGreaterThanOrEqual(GATE10_BLOCK_ROW);
    expect(() => assertNoCollision()).not.toThrow();
  });

  it('строки блока уникальны — два параметра в одной ячейке невозможны', () => {
    const rows = params.map((p) => p.row);
    expect(new Set(rows).size).toBe(rows.length);
  });

  it('режимы лежат ОТДЕЛЬНО от дат (§8): гадать по содержимому нельзя', () => {
    expect(a1(GATE10_PARAMS.wbMode)).not.toBe(a1(GATE10_PARAMS.wbManualLcd));
    expect(a1(GATE10_PARAMS.ozonMode)).not.toBe(a1(GATE10_PARAMS.ozonManualLcd));
    expect(a1(GATE10_PARAMS.wbMode)).not.toBe('ZZ_CONFIG!B2');
    expect(a1(GATE10_PARAMS.ozonMode)).not.toBe(a1(GATE10_PARAMS.ozonLcd));
  });

  it('WB остаётся на LAST_CLOSED_DATE, Ozon получает своё имя', () => {
    expect(WB_LCD_NAMED_RANGE).toBe('LAST_CLOSED_DATE');
    expect(OZON_LCD_NAMED_RANGE).toBe('OZON_LAST_CLOSED_DATE');
    expect(OZON_LCD_NAMED_RANGE).not.toBe(WB_LCD_NAMED_RANGE);
  });
});

describe('начальное наполнение блока', () => {
  const seed = gate10SeedCells('2026-09-22');

  it('AUTO — production-умолчание для обеих платформ', () => {
    expect(seed).toContainEqual({ range: 'ZZ_CONFIG!B31', value: 'AUTO' });
    expect(seed).toContainEqual({ range: 'ZZ_CONFIG!B33', value: 'AUTO' });
  });

  it('ручные даты остаются ПУСТЫМИ: случайное значение не должно включать override', () => {
    const ranges = seed.map((c) => c.range);
    expect(ranges).not.toContain(a1(GATE10_PARAMS.wbManualLcd));
    expect(ranges).not.toContain(a1(GATE10_PARAMS.ozonManualLcd));
    expect(ranges).toContain(labelA1(GATE10_PARAMS.wbManualLcd));   // подпись есть, значения нет
  });

  it('Ozon-LCD засевается переданной датой и только ею', () => {
    expect(seed).toContainEqual({ range: 'ZZ_CONFIG!B30', value: '2026-09-22' });
  });

  it('миграция НЕ трогает ни одну ячейку выше 29-й строки', () => {
    for (const c of seed) {
      const row = Number(/[AB](\d+)$/.exec(c.range)![1]);
      expect(row, c.range).toBeGreaterThanOrEqual(GATE10_BLOCK_ROW);
    }
  });

  it('пишем только колонки A и B — колонка C (пояснения) остаётся владельцу', () => {
    for (const c of seed) expect(c.range, c.range).toMatch(/!(A|B)\d+$/);
  });
});
