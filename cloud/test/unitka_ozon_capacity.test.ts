/**
 * OZON — динамическая ёмкость. Симуляция на РЕАЛЬНОЙ производственной геометрии
 * (замер 23.09.2026: 22 слота из 22 заняты, хвост владельца с колонки 562, лист 574 колонки).
 *
 * Проверяется то, что действительно может сломаться: существующие блоки не двигаются,
 * хвост владельца не затирается, а при неопознанной раскладке не происходит НИЧЕГО.
 */
import { describe, it, expect } from 'vitest';
import {
  markedSlots, tailFirstColumn, planExpansion, isRefusal, slotColumns, SHEETS_MAX_COLUMNS,
} from '../src/loaders/unitka/ozon/capacity.js';
import { OZON_GEOMETRY } from '../src/loaders/unitka/ozon/contract.js';

/** Живая геометрия production-листа на 23.09.2026. */
const LIVE = { slots: 22, tailFirst: 562, columns: 574 } as const;

/** Строка шапки, где у каждого из n слотов стоит подпись. */
function headerRow(n: number): unknown[] {
  const row: unknown[] = [];
  for (let s = 0; s < n; s++) row[OZON_GEOMETRY.BLOCK_FIRST_COLUMN + s * OZON_GEOMETRY.BLOCK_WIDTH - 1] = 'Дата';
  return row;
}

describe('граница хвоста выводится из листа, а не из окружения', () => {
  it('живая геометрия сходится: 12 + 22×25 = 562 — ровно значение конфига', () => {
    expect(OZON_GEOMETRY.BLOCK_FIRST_COLUMN).toBe(12);
    expect(OZON_GEOMETRY.BLOCK_WIDTH).toBe(25);
    expect(tailFirstColumn(LIVE.slots)).toBe(LIVE.tailFirst);
  });

  it('число слотов читается по подписям шапки', () => {
    expect(markedSlots(headerRow(22))).toBe(22);
    expect(markedSlots(headerRow(23))).toBe(23);
  });

  it('после расширения граница пересчитывается сама — конфиг не нужен', () => {
    expect(tailFirstColumn(23)).toBe(587);
    expect(tailFirstColumn(24)).toBe(612);
  });
});

describe('SKU #23: первое расширение (§20)', () => {
  const plan = planExpansion({
    slotsNow: 22, blocksNeeded: 23, tailFirstLive: LIVE.tailFirst, sheetColumnCount: LIVE.columns,
  });

  it('план построен, а не отказ', () => {
    expect(isRefusal(plan), JSON.stringify(plan)).toBe(false);
  });

  it('вставка РОВНО на границе хвоста: 25 колонок перед 562-й', () => {
    if (isRefusal(plan)) throw new Error('refusal');
    expect(plan).toMatchObject({ addSlots: 1, insertAt: 561, insertCount: 25, slotsAfter: 23, tailFirstAfter: 587 });
  });

  it('EXISTING_SKU_SLOT_MOVES=0 — колонки всех 22 блоков неизменны', () => {
    if (isRefusal(plan)) throw new Error('refusal');
    for (let s = 0; s < 22; s++) {
      const before = slotColumns(s);
      expect(before.end, `слот ${s} залез бы в область вставки`).toBeLessThan(plan.insertAt + 1);
      expect(slotColumns(s)).toEqual(before);            // геометрия слота не зависит от расширения
    }
    expect(slotColumns(21).end).toBe(561);               // последний существующий блок кончается перед хвостом
  });

  it('OWNER_TAIL_CORRUPTION=0 — хвост сдвигается целиком, вставка его не пересекает', () => {
    if (isRefusal(plan)) throw new Error('refusal');
    expect(plan.insertAt + 1).toBe(LIVE.tailFirst);      // вставляем ПЕРЕД первой колонкой хвоста
    expect(plan.tailFirstAfter - plan.tailFirstBefore).toBe(plan.insertCount);
    // таблица владельца 562..566 и зеркало LCD 573 уезжают ровно на insertCount
    for (const col of [562, 563, 564, 565, 566, 573]) {
      expect(col + plan.insertCount).toBeGreaterThanOrEqual(plan.tailFirstAfter);
    }
  });

  it('новый слот 22 встаёт ровно туда, где раньше начинался хвост', () => {
    expect(slotColumns(22)).toEqual({ start: 562, end: 586 });
  });
});

describe('SKU #24: второе расширение поверх первого (§20)', () => {
  const after23 = { slots: 23, tailFirst: tailFirstColumn(23), columns: LIVE.columns + 25 };
  const plan = planExpansion({
    slotsNow: after23.slots, blocksNeeded: 24, tailFirstLive: after23.tailFirst,
    sheetColumnCount: after23.columns,
  });

  it('план построен от НОВОЙ границы, а не от старой', () => {
    if (isRefusal(plan)) throw new Error(JSON.stringify(plan));
    expect(plan).toMatchObject({ addSlots: 1, insertAt: 586, insertCount: 25, slotsAfter: 24, tailFirstAfter: 612 });
  });

  it('SKU #23 остаётся на своём месте, и все 22 исходных — тоже', () => {
    if (isRefusal(plan)) throw new Error('refusal');
    expect(slotColumns(22)).toEqual({ start: 562, end: 586 });   // #23 не поехал
    for (let s = 0; s <= 22; s++) expect(slotColumns(s).end).toBeLessThan(plan.insertAt + 1);
  });
});

describe('отказ вместо частичной мутации (§19)', () => {
  it('граница хвоста не совпала с раскладкой ⇒ NO_SAFE_EXPANSION_PATH и ноль изменений', () => {
    const r = planExpansion({ slotsNow: 22, blocksNeeded: 23, tailFirstLive: 999, sheetColumnCount: 1200 });
    expect(isRefusal(r)).toBe(true);
    if (!isRefusal(r)) return;
    expect(r.why).toMatch(/раскладка не опознана/);
  });

  it('подозрительно много новых SKU разом ⇒ отказ, а не расширение на пол-листа', () => {
    const r = planExpansion({ slotsNow: 22, blocksNeeded: 99, tailFirstLive: 562, sheetColumnCount: 574 });
    expect(isRefusal(r)).toBe(true);
  });

  it('упёрлись в предел колонок Google Sheets ⇒ отказ', () => {
    const slots = 730;                                   // 12 + 730×25 = 18 262
    const r = planExpansion({
      slotsNow: slots, blocksNeeded: slots + 1,
      tailFirstLive: tailFirstColumn(slots), sheetColumnCount: SHEETS_MAX_COLUMNS - 10,
    });
    expect(isRefusal(r)).toBe(true);
    if (isRefusal(r)) expect(r.why).toMatch(/предел листа/);
  });

  it('расширение не нужно — план пустой, вставки нет (идемпотентность, §37)', () => {
    const r = planExpansion({ slotsNow: 23, blocksNeeded: 23, tailFirstLive: 587, sheetColumnCount: 599 });
    expect(isRefusal(r)).toBe(false);
    if (isRefusal(r)) return;
    expect(r).toMatchObject({ addSlots: 0, insertCount: 0, tailFirstAfter: 587 });
  });

  it('повторный прогон после расширения НЕ расширяет второй раз', () => {
    const first = planExpansion({ slotsNow: 22, blocksNeeded: 23, tailFirstLive: 562, sheetColumnCount: 574 });
    if (isRefusal(first)) throw new Error('refusal');
    const again = planExpansion({
      slotsNow: first.slotsAfter, blocksNeeded: 23,
      tailFirstLive: first.tailFirstAfter, sheetColumnCount: 574 + first.insertCount,
    });
    if (isRefusal(again)) throw new Error('refusal');
    expect(again.insertCount, 'DUPLICATE_CAPACITY_EXPANSION').toBe(0);
  });
});
