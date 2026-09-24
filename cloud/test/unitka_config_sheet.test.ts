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
  parseLifecycleBlock, dateCellToIso, GATE10_BLOCK_RANGE, type ConfigParam,
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

describe('чтение блока цикла: три состояния, которые нельзя путать', () => {
  // строки 29..34 ровно в том виде, в каком их отдаёт Sheets API (UNFORMATTED_VALUE, SERIAL_NUMBER)
  const good = (over: Record<number, [unknown, unknown]> = {}): unknown[][] => {
    const base: Record<number, [unknown, unknown]> = {
      29: ['ЖИЗНЕННЫЙ ЦИКЛ — AUTO-LCD', ''], 30: ['OZON_LAST_CLOSED_DATE', 46287],
      31: ['WB_LCD_MODE', 'AUTO'], 32: ['WB_MANUAL_LCD', ''],
      33: ['OZON_LCD_MODE', 'AUTO'], 34: ['OZON_MANUAL_LCD', ''],
      ...over,
    };
    return [29, 30, 31, 32, 33, 34].map((r) => base[r] as unknown[]);
  };

  it('ABSENT: блока нет вовсе — это production до миграции', () => {
    expect(parseLifecycleBlock([])).toEqual({ state: 'ABSENT' });
    expect(parseLifecycleBlock([[], [], []])).toEqual({ state: 'ABSENT' });
  });

  it('PRESENT: AUTO по умолчанию, ручные даты пусты, B30 читается как дата', () => {
    const c = parseLifecycleBlock(good());
    expect(c.state).toBe('PRESENT');
    if (c.state !== 'PRESENT') return;
    expect(c.wb).toEqual({ mode: 'AUTO', manualLcd: undefined, manualRaw: null });
    expect(c.ozon.mode).toBe('AUTO');
    expect(c.ozonLcdCell).toBe('2026-09-22');       // 46287 — серийная дата
  });

  it('DRIFT: строки переставлены — fail-closed, режим из чужой ячейки не читаем', () => {
    const c = parseLifecycleBlock(good({ 31: ['OZON_LCD_MODE', 'MANUAL'], 33: ['WB_LCD_MODE', 'AUTO'] }));
    expect(c.state).toBe('DRIFT');
  });

  it('INVALID: непонятный режим — отказ, а НЕ молчаливый AUTO', () => {
    const c = parseLifecycleBlock(good({ 31: ['WB_LCD_MODE', 'ВКЛ'] }));
    expect(c.state).toBe('INVALID');
    if (c.state === 'INVALID') expect(c.issues[0]).toMatch(/B31/);
  });

  it('MANUAL-дата принимается во всех трёх видах, в которых её может ввести владелец', () => {
    for (const raw of [46286, '2026-09-21', '21.09.2026']) {
      const c = parseLifecycleBlock(good({ 31: ['WB_LCD_MODE', 'MANUAL'], 32: ['WB_MANUAL_LCD', raw] }));
      expect(c.state, String(raw)).toBe('PRESENT');
      if (c.state === 'PRESENT') expect(c.wb.manualLcd, String(raw)).toBe('2026-09-21');
    }
  });

  it('MANUAL-дата, заполненная НЕ датой, — null, а не «пусто»: это ошибка владельца, её видно', () => {
    const c = parseLifecycleBlock(good({ 32: ['WB_MANUAL_LCD', 'вчера'] }));
    expect(c.state).toBe('PRESENT');
    if (c.state === 'PRESENT') { expect(c.wb.manualLcd).toBeNull(); expect(c.wb.manualRaw).toBe('вчера'); }
  });

  it('ABSENT не путается с «блок есть, но всё пусто»: подписи — признак существования', () => {
    const labelsOnly = [29, 30, 31, 32, 33, 34].map((r) => [(GATE10_PARAMS as Record<string, { row: number; label: string }>)[
      Object.keys(GATE10_PARAMS).find((k) => (GATE10_PARAMS as Record<string, { row: number }>)[k]!.row === r)!]!.label, '']);
    expect(parseLifecycleBlock(labelsOnly).state).toBe('PRESENT');     // режим пуст = AUTO
  });

  it('читается ровно диапазон A29:B34 — ни строкой выше', () => {
    expect(GATE10_BLOCK_RANGE).toBe('ZZ_CONFIG!A29:B34');
  });
});

describe('dateCellToIso', () => {
  it.each([[46287, '2026-09-22'], [46023, '2026-01-01'], ['2028-02-29', '2028-02-29'], ['29.02.2028', '2028-02-29']])(
    '%s → %s', (v, want) => expect(dateCellToIso(v)).toBe(want));
  it.each([['', null], [null, null], ['2027-02-29', null], ['31.02.2026', null], [46287.5, null], [0, null], ['текст', null]])(
    '%s → %s', (v, want) => expect(dateCellToIso(v)).toBe(want));
});
