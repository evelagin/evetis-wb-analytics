/**
 * OZON — авторитет LCD, геометрия из листа, счёт правил УФ и проверка публикации (Gate 10).
 *
 * Живые числа production (23–24.09.2026, только чтение) сверены отдельно и дали ровно то, что
 * здесь закреплено синтетически: 22 размеченных слота, 0 резервных, хвост 562, зеркало $VA$2,
 * движок генерирует 434 правила УФ = 434 правила в листе, сводка окна 09.08–22.09 сходится.
 */
import { describe, it, expect } from 'vitest';
import {
  resolveOzonAuthority, deriveOzonGeometry, engineCfRuleCount, verifyPublication, writtenCells, d1MoscowOf, completedWindowEnd,
  OZON_COVERAGE_ENTITIES,
} from '../src/loaders/unitka/ozon/ozon_lifecycle.js';
import { OZON_GEOMETRY, ozonSlotStart } from '../src/loaders/unitka/ozon/contract.js';
import { layoutOf } from '../src/loaders/unitka/ozon/requests.js';
import { ozonMonthSpec } from '../src/loaders/unitka/ozon/monthplan.js';
import { tailFirstColumn } from '../src/loaders/unitka/ozon/capacity.js';

/** Сетка листа: n размеченных слотов (шапка «Дата» в строке 606), содержимое хвоста с колонки tail. */
function sheetGrid(n: number, tail: number, width: number): unknown[][] {
  const g: unknown[][] = Array.from({ length: 650 }, () => Array<unknown>(width).fill(''));
  for (let s = 0; s < n; s++) g[605]![ozonSlotStart(s) - 1] = 'Дата';
  g[1]![tail - 1] = 'OZON выплаты 2025';                 // таблица владельца
  g[1]![tail + 10] = 46287;                               // зеркало LCD (VA2 в живом листе)
  return g;
}

describe('авторитет LCD Ozon: переключатель ДО миграции', () => {
  it('ZZ_CONFIG!B2 и LAST_CLOSED_DATE — LEGACY: писатель ведёт себя по-прежнему и LCD не пишет', () => {
    expect(resolveOzonAuthority('ZZ_CONFIG!B2')).toMatchObject({ kind: 'LEGACY', lcdName: 'LAST_CLOSED_DATE' });
    expect(resolveOzonAuthority('LAST_CLOSED_DATE').kind).toBe('LEGACY');
  });
  it('OZON_LAST_CLOSED_DATE — собственный цикл, формулы на собственном имени', () => {
    expect(resolveOzonAuthority('OZON_LAST_CLOSED_DATE')).toMatchObject({ kind: 'OWN', lcdName: 'OZON_LAST_CLOSED_DATE' });
  });
  it('всё прочее — отказ, а не догадка', () => {
    expect(() => resolveOzonAuthority('ZZ_CONFIG!B3')).toThrow(/OZON_LCD_AUTHORITY_INVALID|ожидается/);
  });
  it('prices в смежности НЕ участвует — это снимок', () => {
    expect(OZON_COVERAGE_ENTITIES).toEqual(['finance_accrual', 'fbo_postings', 'ads_sku_daily']);
  });
  it('D-1 по МСК, а не по UTC', () => {
    expect(d1MoscowOf(new Date('2026-09-24T07:00:00Z'))).toBe('2026-09-23');
    expect(d1MoscowOf(new Date('2026-09-23T22:30:00Z'))).toBe('2026-09-23');   // 01:30 МСК 24.09
  });
});

describe('геометрия выводится из листа', () => {
  it('живое состояние: 22 размеченных, 0 резервных, хвост 562, зеркало $VA$2', () => {
    const g = deriveOzonGeometry({ grid: sheetGrid(22, 562, 574), columnCount: 574, mirror: { row: 2, col: 573 }, envTailFirst: 562 });
    expect(g).toMatchObject({ markedSlots: 22, reservedSlots: 0, physicalSlots: 22, tailFirst: 562, lcdRef: '$VA$2' });
    expect(g.notes).toEqual([]);
  });

  it('ПОВТОР ПОСЛЕ ЧАСТИЧНОГО СБОЯ: колонки вставлены, блок не активирован → резервный слот, повторной вставки не будет', () => {
    const g = deriveOzonGeometry({ grid: sheetGrid(22, 587, 599), columnCount: 599, mirror: { row: 2, col: 598 }, envTailFirst: 562 });
    expect(g).toMatchObject({ markedSlots: 22, reservedSlots: 1, physicalSlots: 23, tailFirst: 587, lcdRef: '$VZ$2' });
    expect(g.notes.join(' ')).toMatch(/зарезервировано 1/);
    expect(g.notes.join(' ')).toMatch(/устарел/);           // 562 в окружении больше не правда
  });

  it('после активации 23-го блока: 23 размеченных, 0 резервных, хвост 587 — НЕ 562', () => {
    const g = deriveOzonGeometry({ grid: sheetGrid(23, 587, 599), columnCount: 599, mirror: { row: 2, col: 598 } });
    expect(g).toMatchObject({ markedSlots: 23, reservedSlots: 0, tailFirst: 587 });
    expect(g.tailFirst).toBe(tailFirstColumn(23));
  });

  it('зеркало LCD левее хвоста — раскладка не опознана, отказ', () => {
    expect(() => deriveOzonGeometry({ grid: sheetGrid(22, 562, 574), columnCount: 574, mirror: { row: 2, col: 500 } })).toThrow(/NO_SAFE_EXPANSION_PATH|левее хвоста/);
  });

  it('пустая полоса без хвоста за ней — НЕ резерв (лист просто кончился)', () => {
    const g0 = sheetGrid(22, 562, 600);
    g0[1]![561] = ''; g0[1]![572] = '';                    // хвоста нет вовсе
    const g = deriveOzonGeometry({ grid: g0, columnCount: 600 });
    expect(g.reservedSlots).toBe(0);
  });

  it('владелец правит СВОЮ таблицу в хвосте — геометрия блоков не меняется', () => {
    const g0 = sheetGrid(22, 562, 574);
    g0[5]![565] = 'правка владельца';
    expect(deriveOzonGeometry({ grid: g0, columnCount: 574 }).tailFirst).toBe(562);
  });
});

describe('правила УФ: счёт — тем же генератором, что пишет', () => {
  const blocks = (n: number) => Array.from({ length: n }, (_, i) => `sku${i}`);
  /** k секций подряд с n блоками каждая — как в листе. */
  const chain = (k: number, n: number) => {
    const out = [] as ReturnType<typeof layoutOf>[];
    let top = 50, days = 31, y = 2025, m = 5;                  // как в листе: май 2025 → …
    for (let i = 0; i < k; i++) {
      const spec = ozonMonthSpec(y, m, top, days, blocks(n));
      out.push(layoutOf(spec)); days = spec.days; top = spec.titleRow;
      m += 1; if (m === 13) { m = 1; y += 1; }
    }
    return out;
  };

  it('ЖИВОЙ СЧЁТ: 17 секций × 22 блока = 434 правила — ровно константа окружения сегодня', () => {
    expect(engineCfRuleCount(1, chain(17, 22), '$VA$2')).toBe(434);
  });

  it('🔴 каждый новый МЕСЯЦ добавляет 23 правила: N = 43 + 23 × секций (при 22 блоках)', () => {
    for (const k of [2, 17, 18, 19]) expect(engineCfRuleCount(1, chain(k, 22), '$VA$2'), `${k} секций`).toBe(43 + 23 * k);
  });

  it('🔴 РЕГРЕССИЯ 01.10.2026: с константой 434 правила копятся каждый день; с выводом из раскладки — нет', () => {
    // Ozon создаёт октябрь сам. Модель суточного прогона: удалить `del` первых правил, добавить сгенерированные.
    const run = (sheet: number, del: number, gen: number): number => sheet - Math.min(del, sheet) + gen;
    const gen18 = engineCfRuleCount(1, chain(18, 22), '$VA$2');     // октябрь создан: 457
    let constant = 434, derived = 434;
    // 01.10: октябрь создаётся; у обеих схем удаляется 434 — столько правил и было
    constant = run(constant, 434, gen18); derived = run(derived, engineCfRuleCount(1, chain(17, 22), '$VA$2'), gen18);
    for (let day = 2; day <= 31; day++) {
      constant = run(constant, 434, gen18);                         // окружение по-прежнему говорит 434
      derived = run(derived, engineCfRuleCount(1, chain(18, 22), '$VA$2'), gen18);   // живая раскладка
    }
    expect(derived, 'вывод из раскладки: число правил стабильно').toBe(gen18);
    expect(constant - gen18, 'константа: +23 лишних правила в сутки за октябрь').toBe(23 * 30);
  });

  it('23-й БЛОК добавляет правила тоже — счёт следует за раскладкой, а не за окружением', () => {
    expect(engineCfRuleCount(1, chain(17, 23), '$VZ$2')).toBeGreaterThan(engineCfRuleCount(1, chain(17, 22), '$VA$2'));
  });
});

describe('проверка публикации', () => {
  const W = OZON_GEOMETRY.BLOCK_WIDTH;
  const sec = { firstRow: 10, lastRow: 11, blockCount: 2 };
  const cells = new Map<string, unknown>();
  const put = (r: number, c: number, v: unknown) => cells.set(`${r}:${c}`, v);
  // сводка «views» (колонка D=4) = Σ блоков по смещению 2
  put(10, 4, 30); put(10, ozonSlotStart(0) + 2, 10); put(10, ozonSlotStart(1) + 2, 20);
  put(11, 4, ''); // 11-й ряд — день после LCD: блоки пусты, сводка пуста
  const values = (r: number, c: number) => cells.get(`${r}:${c}`) ?? '';
  const dateAt = (r: number) => (r === 10 ? '2026-09-21' : r === 11 ? '2026-09-22' : null);

  it('всё сходится — все проверки PASS', () => {
    const w = writtenCells([{ range: "'OZON_Юнит_2025'!D10:D10", values: [[30]] }]);
    const c = verifyPublication({ written: w, values, formulas: () => '', sections: [sec], dateAt, summaryUpTo: '2026-09-21' });
    expect(c.every((x) => x.pass), JSON.stringify(c)).toBe(true);
  });

  it('значение не легло («потерянная запись») — READBACK_VALUES FAIL', () => {
    const w = writtenCells([{ range: "'OZON_Юнит_2025'!D10:D10", values: [[31]] }]);
    const c = verifyPublication({ written: w, values, formulas: () => '', sections: [sec], dateAt, summaryUpTo: '2026-09-21' });
    expect(c.find((x) => x.name === 'READBACK_VALUES')!.pass).toBe(false);
  });

  it('формула легла ТЕКСТОМ (RAW вместо USER_ENTERED) — READBACK_FORMULAS FAIL', () => {
    const w = writtenCells([{ range: "'OZON_Юнит_2025'!E10:E10", values: [['=A1+1']] }]);
    const c = verifyPublication({ written: w, values: () => '=A1+1', formulas: () => "'=A1+1", sections: [], dateAt, summaryUpTo: '2026-09-21' });
    expect(c.find((x) => x.name === 'READBACK_FORMULAS')!.pass).toBe(false);
  });

  it('формула вычислилась в ошибку — FORMULA_ERRORS FAIL', () => {
    const w = writtenCells([{ range: "'OZON_Юнит_2025'!E10:E10", values: [['=1/0']] }]);
    const c = verifyPublication({ written: w, values: () => '#DIV/0!', formulas: () => '=1/0', sections: [], dateAt, summaryUpTo: '2026-09-21' });
    expect(c.find((x) => x.name === 'FORMULA_ERRORS')!.pass).toBe(false);
  });

  it('сводка не сошлась в закрытый день — SUMMARY_RECONCILIATION FAIL', () => {
    const bad = (r: number, c: number) => (r === 10 && c === 4 ? 29 : values(r, c));
    const c = verifyPublication({ written: new Map(), values: bad, formulas: () => '', sections: [sec], dateAt, summaryUpTo: '2026-09-21' });
    expect(c.find((x) => x.name === 'SUMMARY_RECONCILIATION')!.pass).toBe(false);
  });

  it('день ПОСЛЕ прежнего LCD до коммита не сверяется: его сводка честно пуста', () => {
    const withData = (r: number, c: number) => (r === 11 && c === ozonSlotStart(0) + 2 ? 5 : values(r, c));
    const pre = verifyPublication({ written: new Map(), values: withData, formulas: () => '', sections: [sec], dateAt, summaryUpTo: '2026-09-21' });
    expect(pre.find((x) => x.name === 'SUMMARY_RECONCILIATION')!.pass).toBe(true);
    const post = verifyPublication({ written: new Map(), values: withData, formulas: () => '', sections: [sec], dateAt, summaryUpTo: '2026-09-22' });
    expect(post.find((x) => x.name === 'SUMMARY_RECONCILIATION')!.pass, 'после коммита — сверяется и пустая сводка ловится').toBe(false);
    void W;
  });
});

describe('окно покрытия Ozon: прогон ручается только за ЗАКОНЧИВШИЕСЯ сутки', () => {
  it('прогон 23.09 06:32 МСК с source_to=23.09 ручается только по 22.09 (замер prod)', () => {
    expect(completedWindowEnd('2026-09-23', '2026-09-23')).toBe('2026-09-22');
  });
  it('прогон 24.09 06:30 закрывает 23.09 — штатное утро', () => {
    expect(completedWindowEnd('2026-09-24', '2026-09-24')).toBe('2026-09-23');
  });
  it('исторический прогон с окном, кончившимся раньше, — окно не удлиняется', () => {
    expect(completedWindowEnd('2026-09-10', '2026-09-23')).toBe('2026-09-10');
  });
  it('нет даты завершения — прогон не ручается ни за что', () => {
    expect(completedWindowEnd('2026-09-23', null)).toBe('');
  });
});

/* ── реальный путь вставки: книга в памяти с insertDimension, как у Sheets API ── */
import { expandOzonCapacity } from '../src/loaders/unitka/ozon/ozon_lifecycle.js';
import type { SheetsGateway, SheetMeta } from '../src/loaders/unitka/sheets.js';

class InsertBook {
  constructor(public grid: unknown[][], public mirror: { row: number; col: number }) {}
  get cols(): number { return this.grid[0]!.length; }
  meta(): SheetMeta { return { sheetId: 7, rowCount: this.grid.length, columnCount: this.cols, anchorCol: 0, namedRanges: { OZON_LCD_MIRROR: { ...this.mirror } } }; }
  gateway(o: { corruptTail?: boolean } = {}): SheetsGateway { return insertGateway(this, o); }
}
interface InsertReq { insertDimension?: { range: { startIndex: number; endIndex: number } } }
/** Шлюз книги в памяти: insertDimension сдвигает колонки и именованный диапазон, как Sheets API. */
function insertGateway(book: InsertBook, o: { corruptTail?: boolean }): SheetsGateway {
  return {
    async readSheetMeta() { return book.meta(); },
    async readValues(ranges: string[]) { return ranges.map(() => book.grid.map((r) => [...r])) as never; },
    async structureWrite(reqs: InsertReq[]) {
      for (const r of reqs) {
        const ins = r.insertDimension; if (!ins) continue;
        const { startIndex: at, endIndex } = ins.range; const n = endIndex - at;
        book.grid = book.grid.map((row) => [...row.slice(0, at), ...Array<unknown>(n).fill(''), ...row.slice(at)]);
        if (book.mirror.col > at) book.mirror = { ...book.mirror, col: book.mirror.col + n };
        if (o.corruptTail) book.grid[1]![at + n] = 'ПОВРЕЖДЕНО';
      }
      return reqs.length;
    },
  } as unknown as SheetsGateway;
}
const silent = { info() {}, warn() {}, error() {}, debug() {}, child() { return silent; } } as never;

describe('вставка слотов: доказательства после insertDimension', () => {
  it('SKU #23: 25 колонок перед хвостом, хвост побайтно на месте, геометрия ЗАНОВО из листа', async () => {
    const book = new InsertBook(sheetGrid(22, 562, 574), { row: 2, col: 573 });
    const geo = deriveOzonGeometry({ grid: book.grid as never, columnCount: 574, mirror: book.mirror });
    const ex = await expandOzonCapacity({ sheets: book.gateway(), sheetName: 'S', meta: book.meta(), geometry: geo, blocksNeeded: 23, log: silent });
    expect(ex.insertedColumns).toBe(25);
    expect(ex.geometry).toMatchObject({ markedSlots: 22, reservedSlots: 1, physicalSlots: 23, tailFirst: 587, lcdRef: '$VZ$2' });
    expect(book.cols).toBe(599);
    expect(ex.tailCellsVerified).toBe(2);                    // таблица владельца и зеркало
  });

  it('регрессия репетиции 24.09: ПУСТЫЕ вставленные колонки не объявляются непустыми', async () => {
    const book = new InsertBook(sheetGrid(22, 562, 574), { row: 2, col: 573 });
    const geo = deriveOzonGeometry({ grid: book.grid as never, columnCount: 574, mirror: book.mirror });
    await expect(expandOzonCapacity({ sheets: book.gateway(), sheetName: 'S', meta: book.meta(), geometry: geo, blocksNeeded: 23, log: silent }))
      .resolves.toMatchObject({ insertedColumns: 25 });
  });

  it('хвост после вставки не совпал со снимком → CAPACITY_EXPANSION_VERIFY_FAILED', async () => {
    const book = new InsertBook(sheetGrid(22, 562, 574), { row: 2, col: 573 });
    const geo = deriveOzonGeometry({ grid: book.grid as never, columnCount: 574, mirror: book.mirror });
    await expect(expandOzonCapacity({ sheets: book.gateway({ corruptTail: true }), sheetName: 'S', meta: book.meta(), geometry: geo, blocksNeeded: 23, log: silent }))
      .rejects.toMatchObject({ code: 'CAPACITY_EXPANSION_VERIFY_FAILED' });
  });

  it('ПОВТОР: колонки уже вставлены, блок не активирован → второй вставки НЕТ', async () => {
    const book = new InsertBook(sheetGrid(22, 562, 574), { row: 2, col: 573 });
    const g1 = deriveOzonGeometry({ grid: book.grid as never, columnCount: 574, mirror: book.mirror });
    await expandOzonCapacity({ sheets: book.gateway(), sheetName: 'S', meta: book.meta(), geometry: g1, blocksNeeded: 23, log: silent });
    // следующий прогон: заново из листа
    const g2 = deriveOzonGeometry({ grid: book.grid as never, columnCount: book.cols, mirror: book.mirror });
    expect(g2.physicalSlots).toBe(23);
    const again = await expandOzonCapacity({ sheets: book.gateway(), sheetName: 'S', meta: book.meta(), geometry: g2, blocksNeeded: 23, log: silent });
    expect(again.insertedColumns, 'DUPLICATE_CAPACITY_EXPANSION').toBe(0);
    expect(book.cols).toBe(599);
  });
});

import { engineCfPrefix } from '../src/loaders/unitka/ozon/ozon_lifecycle.js';
describe('правила УФ движка — фактический префикс в листе', () => {
  const eng = (end: number) => ({ ranges: [{ startColumnIndex: 11, endColumnIndex: end }] });
  it('все правила левее хвоста и в начале списка — это правила движка', () => {
    expect(engineCfPrefix([eng(561), eng(561), eng(300)], 562)).toBe(3);
  });
  it('правило, задевающее хвост владельца, прерывает префикс и НЕ удаляется', () => {
    expect(engineCfPrefix([eng(561), { ranges: [{ startColumnIndex: 565, endColumnIndex: 566 }] }, eng(561)], 562)).toBe(1);
  });
  it('после расширения хвост дальше — правила нового блока тоже в префиксе', () => {
    expect(engineCfPrefix([eng(586), eng(561)], 587)).toBe(2);
    expect(engineCfPrefix([eng(586), eng(561)], 562)).toBe(0);
  });
  it('промежуточное состояние (сбой между пачками по 400) лечится: удаляется ровно то, что есть', () => {
    const partial = Array.from({ length: 300 }, () => eng(561));
    expect(engineCfPrefix(partial, 562)).toBe(300);
  });
});
