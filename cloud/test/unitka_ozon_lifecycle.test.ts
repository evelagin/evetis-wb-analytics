/**
 * OZON — авторитет LCD, геометрия из листа, счёт правил УФ и проверка публикации (Gate 10).
 *
 * Живые числа production (23–24.09.2026, только чтение) сверены отдельно и дали ровно то, что
 * здесь закреплено синтетически: 22 размеченных слота, 0 резервных, хвост 562, зеркало $VA$2,
 * движок генерирует 434 правила УФ = 434 правила в листе, сводка окна 09.08–22.09 сходится.
 */
import { describe, it, expect } from 'vitest';
import {
  resolveOzonAuthority, deriveOzonGeometry, engineCfRuleCount, verifyPublication, writtenCells, d1MoscowOf,
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
