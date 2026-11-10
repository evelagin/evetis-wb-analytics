import { describe, it, expect } from 'vitest';
import { createHash } from 'node:crypto';
import {
  entityKey, toId, headerRow, goodsRow, selectTodo, dailyRunId, mskDate, duplicateBarcodes,
} from '../src/loaders/supplies/normalize.js';
import { SUPPLY_LIST, SUPPLY_CARD, SUPPLY_GOODS } from './supplies_fixture.js';

const md5 = (s: string) => createHash('md5').update(s, 'utf8').digest('hex');
const RUN = 'DL_20261111_022900';
const TS = '2026-11-10T23:29:00.000Z';

describe('поставки — ключ объекта (wbsId_/wbsEntityKey_)', () => {
  it('preorderID = 0 → NULL, а не 0 (иначе объекты слиплись бы в одну группу)', () => {
    expect(toId(0)).toBeNull();
    expect(toId('')).toBeNull();
    expect(toId(null)).toBeNull();
    expect(toId('77')).toBe(77);
  });
  it('P:<preorder> приоритетнее S:<supply>; без обоих — null', () => {
    expect(entityKey({ preorderID: 11, supplyID: 22 })).toBe('P:11');
    expect(entityKey({ preorderID: 0, supplyID: 22 })).toBe('S:22');
    expect(entityKey({ preorderID: 0, supplyID: 0 })).toBeNull();
  });
});

describe('поставки — строка шапки', () => {
  it('с карточкой: склады, коэффициенты строкой → число, is_completed, raw_json {list, card}', () => {
    const o = SUPPLY_LIST[0]!;
    const h = headerRow(o, SUPPLY_CARD, 'P:900001', RUN, TS);
    expect(h.supply_id).toBe(800001);
    expect(h.preorder_id).toBe(900001);
    expect(h.storage_coef).toBe(1.5);
    expect(h.delivery_coef).toBe(2);
    expect(h.warehouse_id).toBe(101);
    expect(h.is_completed).toBe(true);
    expect(h.card_read_ok).toBe(true);
    expect(h.can_show_quantity).toBe(true);
    expect(JSON.parse(h.raw_json)).toEqual({ list: o, card: SUPPLY_CARD });
    // row_hash: md5 склейки через '|', null → пусто — как Array.join в Apps Script.
    const parts = ['P:900001', 800001, 900001, 5, o.createDate, o.supplyDate, o.factDate, o.updatedDate,
      101, 202, 202, 30, 28, 28, 0, 0];
    expect(h.row_hash).toBe(md5(parts.join('|')));
  });
  it('без карточки: все поля карточки NULL, card_read_ok=FALSE, can_show_quantity=NULL', () => {
    const o = SUPPLY_LIST[1]!;
    const h = headerRow(o, null, 'P:900002', RUN, TS);
    expect(h.supply_id).toBeNull();
    expect(h.warehouse_id).toBeNull();
    expect(h.can_show_quantity).toBeNull();
    expect(h.card_read_ok).toBe(false);
    expect(h.is_completed).toBe(false);
    expect(JSON.parse(h.raw_json)).toEqual({ list: o, card: null });
    expect(h.row_hash).toBe(md5(['P:900002', null, 900002, 1, o.createDate, null, null, o.updatedDate,
      null, null, null, null, null, null, null, null].join('|')));
  });
  it('status 5 без factDate — не состоявшаяся (И3)', () => {
    expect(headerRow({ ...SUPPLY_LIST[0]!, factDate: null }, null, 'P:1', RUN, TS).is_completed).toBe(false);
  });
});

describe('поставки — строка позиции', () => {
  it('accepted_quantity_zero — наблюдение: accepted=0 при quantity>0', () => {
    const o = SUPPLY_LIST[0]!;
    const g0 = goodsRow(SUPPLY_GOODS[0]!, o, 'P:900001', RUN, TS);
    const g1 = goodsRow(SUPPLY_GOODS[1]!, o, 'P:900001', RUN, TS);
    expect(g0.accepted_quantity_zero).toBe(false);
    expect(g1.accepted_quantity_zero).toBe(true);
    expect(g0.supply_id).toBe(800001);
    expect(g0.need_kiz).toBe(true);
    expect(g0.acceptance_resolution).toBeNull();
    expect(JSON.parse(g0.raw_json)).toEqual(SUPPLY_GOODS[0]);
    expect(g0.row_hash).toBe(md5(['P:900001', '2000000000011', 10000001, 20, 18, 18, 0].join('|')));
  });
  it('И10: дубль штрихкода внутри объекта виден до записи', () => {
    const o = SUPPLY_LIST[0]!;
    const rows = [SUPPLY_GOODS[0]!, SUPPLY_GOODS[0]!].map((g) => goodsRow(g, o, 'P:900001', RUN, TS));
    expect(duplicateBarcodes(rows)).toEqual(['2000000000011']);
  });
});

describe('поставки — выбор объектов суточного прогона (§7.3)', () => {
  const now = Date.parse('2026-11-10T23:29:00Z'); // 11.11 02:29 МСК
  it('новый / не завершён / updated_dt новее / окно перекрытия / без причины', () => {
    const doneOld = { preorderID: 3, supplyID: 30, statusID: 5, factDate: '2026-10-01T10:00:00+03:00', updatedDate: '2026-10-01T10:00:00+03:00' };
    const doneInWindow = { preorderID: 4, supplyID: 40, statusID: 5, factDate: '2026-10-28T10:00:00+03:00', updatedDate: '2026-10-28T10:00:00+03:00' };
    const doneUpdated = { preorderID: 5, supplyID: 50, statusID: 5, factDate: '2026-10-01T10:00:00+03:00', updatedDate: '2026-11-05T10:00:00+03:00' };
    const pending = { preorderID: 6, statusID: 3, updatedDate: '2026-10-01T10:00:00+03:00' };
    const fresh = { preorderID: 7, statusID: 1 };
    const known = new Map<string, number>([
      ['P:3', Date.parse('2026-10-01T07:00:00Z')], ['P:4', Date.parse('2026-10-28T07:00:00Z')],
      ['P:5', Date.parse('2026-10-01T07:00:00Z')], ['P:6', Date.parse('2026-10-01T07:00:00Z')],
    ]);
    const r = selectTodo([doneOld, doneInWindow, doneUpdated, pending, fresh], known, now);
    expect(r.todo.map((t) => [t.ek, t.why])).toEqual([
      ['P:4', 'окно перекрытия'], ['P:5', 'updated_dt новее'], ['P:6', 'не завершён'], ['P:7', 'новый'],
    ]);
  });
  it('граница watermark: тот же момент в другом формате НЕ «новее» (сравнение epoch, а не строк)', () => {
    const o = { preorderID: 8, supplyID: 80, statusID: 5, factDate: '2026-09-01T10:00:00+03:00', updatedDate: '2026-09-01T10:00:00+03:00' };
    const same = new Map([['P:8', Date.parse('2026-09-01T07:00:00.000Z')]]);
    expect(selectTodo([o], same, now).todo).toEqual([]);
    const older = new Map([['P:8', Date.parse('2026-09-01T06:59:59.999Z')]]);
    expect(selectTodo([o], older, now).todo[0]?.why).toBe('updated_dt новее');
  });
  it('граница окна перекрытия: ровно 14 суток назад по МСК — входит, 15 — нет', () => {
    expect(mskDate(now - 14 * 86_400_000)).toBe('2026-10-28');
    const k = new Map([['P:9', Date.parse('2026-01-01T00:00:00Z')]]);
    const at = (d: string) => selectTodo([{ preorderID: 9, statusID: 5, factDate: `${d}T23:00:00+03:00`, updatedDate: '2025-12-31T00:00:00+03:00' }], k, now).todo.length;
    expect(at('2026-10-28')).toBe(1);
    expect(at('2026-10-27')).toBe(0);
  });
  it('без ключа — пропуск со счётчиком; дубль ключа — последняя запись с причиной', () => {
    const a = { preorderID: 10, statusID: 3, updatedDate: 'A' };
    const b = { preorderID: 10, statusID: 4, updatedDate: 'B' };
    const r = selectTodo([{ preorderID: 0, supplyID: 0 }, a, b], new Map(), now);
    expect(r.skippedNoKey).toBe(1);
    expect(r.duplicateKeys).toEqual(['P:10']);
    expect(r.todo).toHaveLength(1);
    expect(r.todo[0]!.o).toBe(b);
  });
  it('пустой список — пустой план', () => {
    expect(selectTodo([], new Map(), now)).toEqual({ todo: [], skippedNoKey: 0, duplicateKeys: [] });
  });
});

describe('поставки — run_id', () => {
  it('формат Apps Script DL_yyyyMMdd_HHmmss по МСК', () => {
    expect(dailyRunId(Date.parse('2026-11-10T23:29:05Z'))).toBe('DL_20261111_022905');
    expect(dailyRunId(Date.parse('2026-11-10T21:00:00Z'))).toBe('DL_20261111_000000');
  });
});
