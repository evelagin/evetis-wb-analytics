/**
 * UNITKA CALENDAR V2 — контракт безопасности якорей книги.
 * Подготовка месяца вставляет колонки нового блока ПЕРЕД хвостом книги: якоря (зеркало LCD, REVERSE_LEG_RATE,
 * статус Guard) уезжают из WB (600) в WZ (624) и дальше. Engine 2.0 обязан находить колонку якорей только по
 * именованному диапазону REVERSE_LEG_RATE; константы колонки в активном коде быть не должно.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveAnchorCol } from '../src/loaders/unitka/sheets.js';
import { BOOK_ANCHORS, colA1, findBlocks, isBookAnchorCell, integrityStatusCells, type CellValue } from '../src/loaders/unitka/model.js';
import { buildPlan, toWriteRanges } from '../src/loaders/unitka/plan.js';
import { slotStart } from '../src/loaders/unitka/calendar.js';
import { LoaderError } from '../src/errors.js';
import { snapshot, lcdRow, facts, logistics, commission, LCD } from './unitka_fixture.js';

const SHEET = 917273744;
const named = (col0: number, over: Record<string, unknown> = {}) => [
  { name: 'LAST_CLOSED_DATE', range: { sheetId: 1978234470, startRowIndex: 1, endRowIndex: 2, startColumnIndex: 1, endColumnIndex: 2 } },
  { name: 'REVERSE_LEG_RATE', range: { sheetId: SHEET, startRowIndex: 736, endRowIndex: 737, startColumnIndex: col0, endColumnIndex: col0 + 1, ...over } },
];
const code = (fn: () => unknown): string | undefined => { try { fn(); } catch (e) { return e instanceof LoaderError ? e.code : String(e); } return undefined; };

describe('колонка якорей — только по именованному диапазону REVERSE_LEG_RATE', () => {
  it('до вставки блока 25 — WB (600); после вставки 24 колонок — WZ (624); после двух блоков — XX (648)', () => {
    expect(resolveAnchorCol(named(599), SHEET)).toBe(600);
    expect(resolveAnchorCol(named(623), SHEET)).toBe(624);
    expect(colA1(resolveAnchorCol(named(623), SHEET))).toBe('WZ');
    expect(colA1(resolveAnchorCol(named(647), SHEET))).toBe('XX');
  });
  it('нет диапазона, диапазон на другом листе, не одна ячейка, не строка 737 — отказ ANCHOR_UNRESOLVED (без догадок о колонке)', () => {
    expect(code(() => resolveAnchorCol([], SHEET))).toBe('ANCHOR_UNRESOLVED');
    expect(code(() => resolveAnchorCol(named(599).slice(0, 1), SHEET))).toBe('ANCHOR_UNRESOLVED');
    expect(code(() => resolveAnchorCol(named(599), SHEET + 1))).toBe('ANCHOR_UNRESOLVED');
    expect(code(() => resolveAnchorCol(named(599, { endColumnIndex: 601 }), SHEET))).toBe('ANCHOR_UNRESOLVED');
    expect(code(() => resolveAnchorCol(named(599, { endRowIndex: 738 }), SHEET))).toBe('ANCHOR_UNRESOLVED');
    expect(code(() => resolveAnchorCol(named(599, { startRowIndex: 735, endRowIndex: 736 }), SHEET))).toBe('ANCHOR_UNRESOLVED');
  });
  it('ячейки якорей и статуса Guard строятся от найденной колонки', () => {
    expect(isBookAnchorCell(BOOK_ANCHORS.LCD_MIRROR_ROW, 624, 624)).toBe(true);
    expect(isBookAnchorCell(BOOK_ANCHORS.LCD_MIRROR_ROW, 600, 624)).toBe(false);   // после сдвига WB736 — обычная ячейка
    expect(JSON.stringify(integrityStatusCells(624))).toContain('624');
    expect(JSON.stringify(integrityStatusCells(624))).not.toContain('600');
  });
});

describe('Engine пишет якоря в найденную колонку, а не в WB', () => {
  const run = (anchorCol: number) => {
    const snap = snapshot({ lcdInSheet: '2026-09-09' });
    snap.anchorCol = anchorCol;
    const plan = buildPlan({ snapshot: snap, lcd: lcdRow(), facts: facts(), logistics: logistics(), commission: commission(), minN: 10, maxLagDays: 2 });
    return { plan, ranges: toWriteRanges(plan.cells, 'WB_Юнит_2025').map((r) => r.range) };
  };
  it('якорь в WZ (после вставки блока): зеркало LCD → WZ736, ни одной записи в WB736/WB737', () => {
    const { plan, ranges } = run(624);
    const anchors = plan.cells.filter((c) => c.kind === 'lcd' && c.row === BOOK_ANCHORS.LCD_MIRROR_ROW);
    expect(anchors.map((c) => c.col)).toEqual([624]);
    expect(ranges.some((r) => /!WZ736:WZ73[67]$/.test(r))).toBe(true);
    expect(ranges.some((r) => /!WB\d/.test(r))).toBe(false);
  });
  it('якорь в WB (книга до вставки): то же поведение от той же функции — колонка только из снимка', () => {
    const { plan, ranges } = run(600);
    expect(plan.cells.filter((c) => c.kind === 'lcd' && c.row === BOOK_ANCHORS.LCD_MIRROR_ROW).map((c) => c.col)).toEqual([600]);
    expect(ranges.some((r) => /!WB736:WB73[67]$/.test(r))).toBe(true);
    expect(ranges.some((r) => /!WZ\d/.test(r))).toBe(false);
    expect(LCD).toBe('2026-09-10');
  });
});

describe('вставка 24 колонок перед хвостом книги не ломает поиск блоков', () => {
  it('сентябрь после вставки: 24 заголовка + подпись хвоста в WO (начало слота 25) → 24 блока; октябрь: 25 блоков сплошь', () => {
    const top: CellValue[] = Array(624).fill('');
    for (let s = 0; s < 24; s++) top[slotStart(s) - 1] = `${252442517 + s} Товар ${s + 1}`;
    top[613 - 1] = 'Средняя логистика (расчёт)';   // подпись хвоста: текст без nmID
    expect(findBlocks(top, 624).map((b) => b.slot)).toEqual(Array.from({ length: 24 }, (_, i) => i));
    top[slotStart(24) - 1] = '909951444 Набор';
    const blocks = findBlocks(top, 624);
    expect(blocks).toHaveLength(25);
    expect(blocks[24]).toMatchObject({ slot: 24, start: 589, nmId: 909951444 });
  });
});

describe('в активном коде Engine 2.0 нет константы колонки якорей', () => {
  const dir = join(__dirname, '..', 'src', 'loaders', 'unitka');
  const stripComments = (src: string): string => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/.*$/gm, '$1');
  it('ни 600, ни WB7xx / $WB$, ни MIR — вне комментариев (REVERSE_LEG_RATE — только имя диапазона)', () => {
    const hits: string[] = [];
    for (const f of readdirSync(dir).filter((x) => x.endsWith('.ts'))) {
      stripComments(readFileSync(join(dir, f), 'utf8')).split('\n').forEach((line, i) => {
        if (/\b600\b|\$?WB\$?7\d\d|\bMIR\b|\bWB\b(?!')/.test(line.replace(/'WB'/g, ''))) hits.push(`${f}:${i + 1}: ${line.trim().slice(0, 120)}`);
      });
    }
    expect(hits).toEqual([]);
  });
});
