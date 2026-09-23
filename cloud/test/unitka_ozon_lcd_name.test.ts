/**
 * Имя LCD в генераторе формул Ozon (Gate 10).
 *
 * Суточный прогон Ozon ПЕРЕПИСЫВАЕТ формулы окна 45 суток. Если бы генератор знал только
 * LAST_CLOSED_DATE, то на следующее утро после миграции он вернул бы мигрированные формулы к
 * имени WB — и платформы снова оказались бы связаны, без единой ошибки в логе. Здесь доказано:
 *   • по умолчанию генератор даёт ровно прежние формулы (production до миграции не меняется);
 *   • с OZON_LAST_CLOSED_DATE он даёт РОВНО то, что делает с формулой миграция (repointFormula), —
 *     значит, после миграции суточный прогон не создаёт ни одного лишнего изменения;
 *   • в формулах с новым именем не остаётся ни одной ссылки на имя WB.
 */
import { describe, it, expect } from 'vitest';
import {
  ozonBlockDayFormulas, ozonSummaryDayFormulas, ozonBlockMtdFormulas,
  OZON_LEGACY_LCD_NAME, OZON_OWN_LCD_NAME,
} from '../src/loaders/unitka/ozon/formulas.js';
import { sectionFormulas, ozonMonthSpec } from '../src/loaders/unitka/ozon/monthplan.js';
import { repointFormula, referencesWbLcd } from '../src/loaders/unitka/ozon/lcd_migration.js';

const spec = ozonMonthSpec(2026, 9, 570, 31, ['252442517', '252442341', '930334397']);
const comp = { cogs: { '252442517|607': 231.38 }, other: {} };

describe('генератор формул Ozon: имя LCD — параметр', () => {
  it('по умолчанию — прежнее имя: production до миграции не меняется ни на символ', () => {
    const a = sectionFormulas(spec, comp);
    const b = sectionFormulas(spec, comp, 'SEMICOLON', OZON_LEGACY_LCD_NAME);
    expect(a).toEqual(b);
    expect(Object.values(a.day).some((f) => f.includes('LAST_CLOSED_DATE'))).toBe(true);
  });

  it('с OZON_LAST_CLOSED_DATE генератор даёт РОВНО результат миграции каждой формулы', () => {
    const legacy = sectionFormulas(spec, comp);
    const own = sectionFormulas(spec, comp, 'SEMICOLON', OZON_OWN_LCD_NAME);
    const keys = Object.keys(legacy.day);
    expect(keys.length).toBeGreaterThan(500);
    for (const k of keys) expect(own.day[k], k).toBe(repointFormula(legacy.day[k]!));
    for (const k of Object.keys(legacy.mtd)) expect(own.mtd[k], k).toBe(repointFormula(legacy.mtd[k]!));
  });

  it('в формулах с новым именем НЕТ ни одной ссылки на имя WB', () => {
    const own = sectionFormulas(spec, comp, 'SEMICOLON', OZON_OWN_LCD_NAME);
    const all = [...Object.values(own.day), ...Object.values(own.mtd)];
    expect(all.filter((f) => referencesWbLcd(f))).toEqual([]);
    expect(all.filter((f) => f.includes(OZON_OWN_LCD_NAME)).length).toBeGreaterThan(500);
  });

  it('все три зависящих от LCD построителя принимают имя; мусорное имя — отказ, а не формула', () => {
    const p = { start: 12, cogsTerm: '0', otherDirectTerm: '0' };
    for (const m of [ozonBlockDayFormulas(p, 607, OZON_OWN_LCD_NAME), ozonSummaryDayFormulas(607, 3, OZON_OWN_LCD_NAME),
                     ozonBlockMtdFormulas(12, { firstDailyRow: 607, lastDailyRow: 636, mtdRow: 637 }, OZON_OWN_LCD_NAME)]) {
      expect([...m.values()].some((f) => f.includes(OZON_OWN_LCD_NAME))).toBe(true);
      expect([...m.values()].some((f) => referencesWbLcd(f))).toBe(false);
    }
    expect(() => ozonBlockDayFormulas(p, 607, 'LAST_CLOSED_DATE)+1;(')).toThrow();
  });
});
