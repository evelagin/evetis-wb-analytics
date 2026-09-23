/**
 * WB — ЁМКОСТЬ ЛИСТА (Gate 10 §7). Механизм вставки колонок уже существует
 * (calendar.insertGeometry, используется monthprep и monthappend) — его не переписываем,
 * а доказываем на ЖИВЫХ числах production-листа.
 *
 * ЗАМЕР 23.09.2026: якорь книги WY = колонка 623, хвост владельца 612..623 (TAIL_WIDTH 12),
 * блоки с колонки 13 шагом 24. Занято 24 слота (0..23), СВОБОДЕН РОВНО ОДИН — слот 24 (589).
 *
 * Отсюда настоящая последовательность, которую и проверяем:
 *   25-й SKU  → занимает свободный слот 24, вставка НЕ нужна;
 *   26-й SKU  → свободных нет, вставка 24 колонок перед хвостом.
 * Порядок важен: вставлять колонки, пока есть свободный слот, значило бы раздувать лист зря.
 */
import { describe, it, expect } from 'vitest';
import {
  BLOCK_FIRST_COLUMN, BLOCK_WIDTH, BLOCK_BODY_WIDTH, TAIL_WIDTH,
  slotStart, tailStartOf, insertGeometry,
} from '../src/loaders/unitka/calendar.js';

const ANCHOR = 623;            // колонка якоря (именованный диапазон REVERSE_LEG_RATE)
const OCCUPIED = 24;           // занятых слотов в живом листе
const FREE_SLOT = 24;          // единственный свободный

describe('живая геометрия WB сходится с замером листа', () => {
  it('шаг и старт блоков, ширина хвоста', () => {
    expect([BLOCK_FIRST_COLUMN, BLOCK_WIDTH, BLOCK_BODY_WIDTH, TAIL_WIDTH]).toEqual([13, 24, 23, 12]);
  });

  it('хвост владельца начинается на 612 — ровно как в листе', () => {
    expect(tailStartOf(ANCHOR)).toBe(612);
  });

  it('свободный слот 24 занимает колонки 589..611, вплотную к хвосту', () => {
    expect(slotStart(FREE_SLOT)).toBe(589);
    expect(slotStart(FREE_SLOT) + BLOCK_BODY_WIDTH - 1).toBe(611);
    expect(tailStartOf(ANCHOR)).toBe(612);
  });
});

describe('25-й SKU: занимает свободный слот, лист НЕ расширяется', () => {
  it('механизм вставки ОТКАЗЫВАЕТ, пока между блоками и хвостом есть свободный слот', () => {
    // Это не дефект, а предохранитель: insertGeometry рассчитан на хвост ВПЛОТНУЮ к последнему
    // блоку. Пока слот свободен, единственный правильный ход — занять его, а не вставлять колонки.
    for (const newCount of [0, 1, 2]) {
      expect(insertGeometry(OCCUPIED - 1, tailStartOf(ANCHOR), newCount), `newCount=${newCount}`)
        .toEqual({ error: 'TAIL_GEOMETRY_UNKNOWN' });
    }
  });

  it('при 24 занятых хвост ЕЩЁ НЕ вплотную к последнему блоку — это и есть свободный слот', () => {
    // последний занятый слот 23 кончается на 587, хвост на 612 → между ними целый слот
    expect(slotStart(23) + BLOCK_WIDTH - 1).toBe(588);
    expect(tailStartOf(ANCHOR) - (slotStart(23) + BLOCK_BODY_WIDTH - 1)).toBe(25);
    // поэтому механизм вставки честно отказывается: он рассчитан на хвост ВПЛОТНУЮ
    const ig = insertGeometry(23, tailStartOf(ANCHOR), 1);
    expect(ig).toEqual({ error: 'TAIL_GEOMETRY_UNKNOWN' });
  });
});

describe('26-й SKU: свободных слотов нет — вставка перед хвостом', () => {
  const lastSlot = FREE_SLOT;                      // 24 — теперь занят
  const tailStart = tailStartOf(ANCHOR);           // 612

  it('геометрия вставки: 24 колонки, хвост уезжает на 636', () => {
    const ig = insertGeometry(lastSlot, tailStart, 1);
    expect('error' in ig).toBe(false);
    if ('error' in ig) return;
    expect(ig).toEqual({ at: 611, count: 24, tailStartAfter: 636 });
  });

  it('EXISTING_SKU_SLOT_MOVES=0 — все 25 занятых блоков левее точки вставки', () => {
    const ig = insertGeometry(lastSlot, tailStart, 1);
    if ('error' in ig) throw new Error('refusal');
    for (let s = 0; s <= lastSlot; s++) {
      expect(slotStart(s) + BLOCK_BODY_WIDTH - 1, `слот ${s}`).toBeLessThanOrEqual(ig.at);
    }
  });

  it('OWNER_TAIL_CORRUPTION=0 — хвост сдвигается целиком, ровно на число вставленных колонок', () => {
    const ig = insertGeometry(lastSlot, tailStart, 1);
    if ('error' in ig) throw new Error('refusal');
    expect(ig.tailStartAfter - tailStart).toBe(ig.count);
    expect(ig.at + 1).toBe(tailStart);              // вставляем ПЕРЕД первой колонкой хвоста
  });

  it('новый блок встаёт туда, где начинался хвост', () => {
    expect(slotStart(lastSlot + 1)).toBe(613);
    const ig = insertGeometry(lastSlot, tailStart, 1);
    if ('error' in ig) throw new Error('refusal');
    expect(slotStart(lastSlot + 1) + BLOCK_BODY_WIDTH).toBe(ig.tailStartAfter);
  });

  it('сразу несколько новых SKU — одна вставка на всех', () => {
    const ig = insertGeometry(lastSlot, tailStart, 3);
    if ('error' in ig) throw new Error('refusal');
    expect(ig.count).toBe(3 * BLOCK_WIDTH);
    expect(ig.tailStartAfter).toBe(slotStart(lastSlot + 3) + BLOCK_BODY_WIDTH);
  });

  it('законны РОВНО два положения хвоста: вплотную за метрикой и через разделитель', () => {
    // 611 — последняя метрика слота 24; книга до Calendar V2 держит разделитель, после — нет
    for (const ok of [612, 613]) {
      expect('error' in insertGeometry(lastSlot, ok, 1), `tailStart=${ok}`).toBe(false);
    }
  });

  it('любое ДРУГОЕ положение хвоста — отказ, а не вставка наугад', () => {
    for (const bad of [500, 610, 611, 614, 700]) {
      expect(insertGeometry(lastSlot, bad, 1), `tailStart=${bad}`).toEqual({ error: 'TAIL_GEOMETRY_UNKNOWN' });
    }
  });

  it('следующий прогон считает хвост от НОВОГО якоря, а не от 612', () => {
    const ig = insertGeometry(lastSlot, tailStart, 1);
    if ('error' in ig) throw new Error('refusal');
    const anchorAfter = ANCHOR + ig.count;          // якорь уехал вместе с хвостом
    expect(tailStartOf(anchorAfter)).toBe(ig.tailStartAfter);
    expect(tailStartOf(anchorAfter)).not.toBe(tailStart);
  });
});
