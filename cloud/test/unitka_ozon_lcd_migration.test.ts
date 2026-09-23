/**
 * Перевод формул Ozon на собственный LCD. Проверяется на РЕАЛЬНЫХ формулах, снятых с
 * production-листа (`fixtures/ozon_lcd_real_formulas.json`, замер 23.09.2026), а не на
 * придуманных: придуманная формула не поймает ни `$B470`, ни FILTER/MOD/COLUMN внутри.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  repointFormula, referencesWbLcd, planLcdMigration, verifyPlan,
} from '../src/loaders/unitka/ozon/lcd_migration.js';

const REAL: Array<{ row: number; col: number; f: string }> = JSON.parse(
  readFileSync(join(fileURLToPath(new URL('.', import.meta.url)), 'fixtures', 'ozon_lcd_real_formulas.json'), 'utf8'));

describe('подстановка имени на реальных формулах листа', () => {
  it('фикстура непуста и это действительно формулы со ссылкой на LCD', () => {
    expect(REAL.length).toBeGreaterThanOrEqual(10);
    for (const s of REAL) expect(referencesWbLcd(s.f), `r${s.row}c${s.col}`).toBe(true);
  });

  it('меняется ТОЛЬКО имя: всё остальное в формуле байт-в-байт прежнее', () => {
    for (const s of REAL) {
      const after = repointFormula(s.f);
      expect(after).not.toBe(s.f);
      expect(after.split('OZON_LAST_CLOSED_DATE').join('LAST_CLOSED_DATE'), `r${s.row}c${s.col}`).toBe(s.f);
    }
  });

  it('ссылки на даты и диапазоны не пострадали', () => {
    const one = REAL.find((s) => s.f.includes('FILTER'))!;
    const after = repointFormula(one.f);
    for (const frag of ['FILTER', 'MOD(COLUMN', '$B']) expect(after).toContain(frag);
  });

  it('ИДЕМПОТЕНТНОСТЬ: повторная подстановка не даёт OZON_OZON_…', () => {
    for (const s of REAL) {
      const once = repointFormula(s.f);
      expect(repointFormula(once)).toBe(once);
      expect(once).not.toContain('OZON_OZON');
    }
  });

  it('уже переведённая формула не считается требующей перевода', () => {
    for (const s of REAL) expect(referencesWbLcd(repointFormula(s.f))).toBe(false);
  });

  it('похожие имена не задеваются — подменяем самостоятельный идентификатор', () => {
    expect(repointFormula('=MY_LAST_CLOSED_DATE+1')).toBe('=MY_LAST_CLOSED_DATE+1');
    expect(repointFormula('=LAST_CLOSED_DATE_X')).toBe('=LAST_CLOSED_DATE_X');
    expect(repointFormula('=LAST_CLOSED_DATE')).toBe('=OZON_LAST_CLOSED_DATE');
  });
});

describe('план миграции', () => {
  const grid = [
    ['=IF($B470>LAST_CLOSED_DATE;"";1)', 'текст', 42],
    ['=OZON_LAST_CLOSED_DATE', '=SUMIF(A1:A9;"<="&LAST_CLOSED_DATE)', null],
  ];

  it('собирает ровно ячейки со ссылкой и считает уже переведённые', () => {
    const p = planLcdMigration({ grid, firstRow: 470, tailFirstColumn: 562 });
    expect(p.cells).toHaveLength(2);
    expect(p.cells[0]).toMatchObject({ row: 470, col: 1 });
    expect(p.cells[1]).toMatchObject({ row: 471, col: 2 });
    expect(p.alreadyMigrated).toBe(1);
  });

  it('ссылки в хвосте владельца показываются ОТДЕЛЬНО', () => {
    const wide: unknown[] = [];
    wide[570] = '=LAST_CLOSED_DATE';                 // колонка 571 — внутри хвоста
    const p = planLcdMigration({ grid: [wide], firstRow: 2, tailFirstColumn: 562 });
    expect(p.inOwnerTail).toHaveLength(1);
    expect(p.inOwnerTail[0]).toMatchObject({ row: 2, col: 571 });
  });

  it('замороженные секции не попадают в план', () => {
    const p = planLcdMigration({ grid, firstRow: 470, tailFirstColumn: 562, frozenRows: (r) => r === 470 });
    expect(p.cells.every((c) => c.row !== 470)).toBe(true);
  });

  it('повторный прогон по уже мигрированной сетке даёт ПУСТОЙ план (§37)', () => {
    const migrated = grid.map((r) => r.map((c) => (typeof c === 'string' ? repointFormula(c) : c)));
    const p = planLcdMigration({ grid: migrated, firstRow: 470, tailFirstColumn: 562 });
    expect(p.cells).toHaveLength(0);
  });
});

describe('проверка плана отвергает всё, кроме подстановки имени', () => {
  it('корректный план принимается', () => {
    const p = planLcdMigration({
      grid: [REAL.map((s) => s.f)], firstRow: 470, tailFirstColumn: 100_000,
    });
    expect(p.cells.length).toBe(REAL.length);
    expect(verifyPlan(p)).toEqual({ ok: true });
  });

  it('подмена чего-то ещё внутри формулы — отказ целиком', () => {
    const bad = {
      cells: [{ row: 1, col: 1, before: '=IF($B1>LAST_CLOSED_DATE;"";X1)', after: '=IF($B1>OZON_LAST_CLOSED_DATE;"";Y1)' }],
      alreadyMigrated: 0, inOwnerTail: [], scannedRows: 1,
    };
    const r = verifyPlan(bad);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.why).toMatch(/не только имя|текст формулы не совпал/);
  });

  it('формула без изменений в плане — отказ', () => {
    const r = verifyPlan({ cells: [{ row: 1, col: 1, before: '=A1', after: '=A1' }], alreadyMigrated: 0, inOwnerTail: [], scannedRows: 1 });
    expect(r.ok).toBe(false);
  });
});
