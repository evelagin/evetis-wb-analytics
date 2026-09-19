/**
 * UNITKA CALENDAR V2 — визуальный контракт (Phase 2C hardening): семантика колонок, нейтральный будущий
 * вид, правила УФ (выходные, будущее, заливка закрытого дня, уровни, пороги, шкалы), независимость от якоря.
 */
import { describe, it, expect } from 'vitest';
import { OFFSET, colA1 } from '../src/loaders/unitka/model.js';
import { geometryAt, layoutOf, slotStart, type BlockSlot } from '../src/loaders/unitka/calendar.js';
import {
  BLOCK_KIND, SUMMARY_KIND, COLOR, columnKind, neutralizeDayFormat, tierOf, turnoverBand, isWeekend, cancelsAlarm, zeroOrdersAlarm,
  closedFill, buildConditionalFormats, cfIdioms, cfRequests, relativeColumnRefs, hexOf, rgb, borderSpec, bordersJson, rowKindOf, SUMMARY_LAST_COLUMN, LINE,
} from '../src/loaders/unitka/visual.js';
import { fromLocaleFormula } from '../src/loaders/unitka/formulas.js';
import type { ConditionalFormatRule } from '../src/loaders/unitka/sheets.js';

const blocks = (slots: number[]): BlockSlot[] => slots.map((s, i) => ({ index: i, slot: s, start: slotStart(s), nmId: 100 + i, title: `${100 + i}` }));
const OCT25 = layoutOf(geometryAt({ year: 2026, month: 10 }, 769), blocks(Array.from({ length: 25 }, (_, i) => i)));
const ANCHOR = 624; // WZ — колонка якорей после вставки блока 25
const formulaOf = (r: ConditionalFormatRule): string => r.booleanRule?.condition.values?.[0]?.userEnteredValue ?? '';
const bgOf = (r: ConditionalFormatRule): string | null => hexOf(r.booleanRule?.format?.backgroundColor as never);
const fgOf = (r: ConditionalFormatRule): string | null => hexOf((r.booleanRule?.format?.textFormat as { foregroundColor?: never } | undefined)?.foregroundColor);
const cols = (r: ConditionalFormatRule): number[] => r.ranges.flatMap((g) => Array.from({ length: (g.endColumnIndex ?? 0) - (g.startColumnIndex ?? 0) }, (_, i) => (g.startColumnIndex ?? 0) + 1 + i));

describe('семантика колонок', () => {
  it('блок: дата/день недели, ручной ввод (блогеры, СПП), факт (9 колонок Engine), ставки, расчёт', () => {
    expect(BLOCK_KIND[OFFSET.date]).toBe('date'); expect(BLOCK_KIND[OFFSET.weekday]).toBe('weekday');
    expect([OFFSET.bloggers, OFFSET.spp].map((o) => BLOCK_KIND[o])).toEqual(['manual', 'manual']);
    expect([OFFSET.views, OFFSET.opens, OFFSET.orders, OFFSET.carts, OFFSET.cancels, OFFSET.stock, OFFSET.adsIn, OFFSET.price, OFFSET.storage].every((o) => BLOCK_KIND[o] === 'fact')).toBe(true);
    expect([OFFSET.commission, OFFSET.logistics].map((o) => BLOCK_KIND[o])).toEqual(['rate', 'rate']);
    expect([OFFSET.turnover, OFFSET.profit1, OFFSET.profitAll, OFFSET.adsOut, OFFSET.drr, OFFSET.priceSpp, OFFSET.priceMinusComm, OFFSET.tax, OFFSET.unitProfit].every((o) => BLOCK_KIND[o] === 'calc')).toBe(true);
    expect(BLOCK_KIND).toHaveLength(24);
  });
  it('сводка A..L и колонки по слотам; резервный слот — вне контракта', () => {
    expect([1, 2, 3, 4, 9, 10, 11, 12].map((c) => SUMMARY_KIND[c])).toEqual(['weekday', 'date', 'manual', 'fact', 'calc', 'fact', 'calc', 'weekday']);
    expect(columnKind(13 + OFFSET.spp, OCT25)).toBe('manual');
    expect(columnKind(589 + OFFSET.logistics, OCT25)).toBe('rate');
    expect(columnKind(600, OCT25)).toBe('fact'); // X блока 25
    expect(columnKind(613, OCT25)).toBeNull(); // хвост книги за последним блоком
  });
});

describe('нейтральный будущий вид строки дня', () => {
  const tpl = { backgroundColor: rgb(COLOR.factBg), backgroundColorStyle: { rgbColor: rgb(COLOR.factBg) }, numberFormat: { type: 'NUMBER', pattern: '#,##0' }, textFormat: { foregroundColor: rgb('#b7b7b7'), fontSize: 12 }, borders: { left: { style: 'SOLID' } } };
  it('факт/расчёт: без заливки, числовой формат и границы сохранены', () => {
    const f = neutralizeDayFormat(tpl, 'fact') as Record<string, unknown>;
    expect(f.backgroundColor).toBeUndefined(); expect(f.backgroundColorStyle).toBeUndefined();
    expect(f.numberFormat).toEqual(tpl.numberFormat); expect(f.borders).toEqual(tpl.borders);
    expect((f.textFormat as { fontSize: number }).fontSize).toBe(12);
  });
  it('ставка: без заливки и без статического серого текста (будущее — серым по УФ)', () => {
    const f = neutralizeDayFormat(tpl, 'rate') as { textFormat: Record<string, unknown> };
    expect(f.textFormat.foregroundColor).toBeUndefined(); expect(f.textFormat.fontSize).toBe(12);
  });
  it('ручной ввод, дата, день недели — как шаблон (зона ввода остаётся жёлтой)', () => {
    for (const k of ['manual', 'date', 'weekday'] as const) expect(neutralizeDayFormat(tpl, k)).toBe(tpl);
    expect(neutralizeDayFormat(null, 'fact')).toBeNull();
  });
});

describe('семантика правил: пусто, ноль, положительное, отрицательное, границы', () => {
  it('уровни доли от максимума: пусто/0 — нет; границы 25/50/75 % — строго больше', () => {
    expect(tierOf('', 10)).toBeNull(); expect(tierOf(null, 10)).toBeNull(); expect(tierOf(0, 10)).toBeNull();
    expect(tierOf(10, 10)).toBe(0); expect(tierOf(7.5, 10)).toBe(1); expect(tierOf(7.6, 10)).toBe(0);
    expect(tierOf(5, 10)).toBe(2); expect(tierOf(2.5, 10)).toBe(3); expect(tierOf(2.6, 10)).toBe(2); expect(tierOf(0.1, 10)).toBe(3);
  });
  it('оборачиваемость: ≤15 / 15–30 / 30–60 / >60; не число — нет', () => {
    expect([15, 15.01, 30, 30.01, 60, 60.01].map(turnoverBand)).toEqual([0, 1, 1, 2, 2, 3]);
    expect(turnoverBand('')).toBeNull(); expect(turnoverBand(null)).toBeNull(); expect(turnoverBand(0)).toBe(0);
  });
  it('выходные по дате: 03.10.2026 сб, 04.10 вс, 05.10 пн; 01.10 чт; 01.11 вс; 29.02.2028 вт', () => {
    expect(['2026-10-03', '2026-10-04', '2026-10-05', '2026-10-01', '2026-11-01', '2028-02-29'].map(isWeekend)).toEqual([true, true, false, false, true, false]);
  });
  it('отмены и «показы без заказов»: только закрытый день, пусто — нет', () => {
    expect(cancelsAlarm(true, 10, 3)).toBe(true); expect(cancelsAlarm(true, 10, 2)).toBe(false); expect(cancelsAlarm(true, 6, 2)).toBe(true);
    expect(cancelsAlarm(true, 0, 3)).toBe(false); expect(cancelsAlarm(false, 10, 3)).toBe(false); expect(cancelsAlarm(true, '', 3)).toBe(false);
    expect(zeroOrdersAlarm(true, 5, 0)).toBe(true); expect(zeroOrdersAlarm(true, 5, '')).toBe(false); expect(zeroOrdersAlarm(true, 0, 0)).toBe(false); expect(zeroOrdersAlarm(false, 5, 0)).toBe(false);
  });
  it('заливка закрытого дня: закрыт и есть значение (0 — значение; пропуск источника — нет)', () => {
    expect(closedFill(true, 0)).toBe(true); expect(closedFill(true, 5)).toBe(true); expect(closedFill(true, '')).toBe(false); expect(closedFill(true, null)).toBe(false); expect(closedFill(false, 5)).toBe(false);
  });
});

describe('правила УФ октября 2026 (25 блоков сплошь, якоря в WZ)', () => {
  const cf = buildConditionalFormats(OCT25, 7, 'SEMICOLON', ANCHOR);
  const rules = cf.rules;
  it('все диапазоны — строки 771..801, только сводка и колонки блоков; резерв не покрыт', () => {
    for (const r of rules) for (const g of r.ranges) {
      expect([g.startRowIndex, g.endRowIndex, g.sheetId]).toEqual([770, 801, 7]);
      expect(g.endColumnIndex).toBeLessThanOrEqual(612);
    }
  });
  it('ни одной относительной ссылки на колонку в формулах (независимость от якоря)', () => {
    const fs = rules.map(formulaOf).filter(Boolean);
    expect(fs.length).toBeGreaterThan(20);
    for (const f of fs) expect(relativeColumnRefs(fromLocaleFormula(f, 'SEMICOLON'))).toEqual([]);
    expect(relativeColumnRefs('=WEEKDAY(US771,2)>5')).toEqual(['US771']);
    expect(relativeColumnRefs('=AND($B771<=$WZ$736,INDEX($A771:$WN771,1,COLUMN())<>"")')).toEqual([]);
  });
  it('выходные: одна формула по дате строки ($B), заливка #fcefe3 на A, B, L и дате/дне недели каждого блока', () => {
    const wk = rules.filter((r) => formulaOf(r).includes('WEEKDAY'));
    expect(wk).toHaveLength(1);
    expect(formulaOf(wk[0]!)).toBe('=WEEKDAY($B771;2)>5');
    expect(bgOf(wk[0]!)).toBe(COLOR.weekendBg);
    const c = cols(wk[0]!);
    expect(c).toEqual(expect.arrayContaining([1, 2, 12, 13, 36, 589, 612]));
    expect(c).toHaveLength(3 + 25 * 2);
  });
  it('будущий день — серый текст: после выходных (первое правило побеждает), сводка A..L и весь блок; якорь — WZ', () => {
    const fut = rules.findIndex((r) => formulaOf(r) === '=$B771>$WZ$736');
    const wk = rules.findIndex((r) => formulaOf(r).includes('WEEKDAY'));
    expect(fut).toBeGreaterThan(wk);
    expect(fgOf(rules[fut]!)).toBe(COLOR.futureFg);
    expect(cols(rules[fut]!)).toHaveLength(12 + 25 * 24);
    expect(rules.some((r) => formulaOf(r).includes('$WB$736'))).toBe(false);
  });
  it('заливка закрытого дня: закрыт И значение; факт зелёная, цена жёлтая, ставки голубые; стоят после метрических правил', () => {
    const closed = rules.map((r, i) => [i, r] as const).filter(([, r]) => formulaOf(r) === '=AND($B771<=$WZ$736;INDEX($A771:$WN771;1;COLUMN())<>"")');
    expect(closed.map(([, r]) => bgOf(r))).toEqual([COLOR.factBg, COLOR.manualBg, COLOR.rateBg]);
    const green = cols(closed[0]![1]);
    expect(green).toEqual(expect.arrayContaining([4, 5, 6, 7, 8, 10, 15, 16, 17, 18, 19, 20, 24, 33, 589 + OFFSET.storage]));
    expect(green).not.toContain(3); expect(green).not.toContain(9); expect(green).not.toContain(14); expect(green).not.toContain(28);
    expect(cols(closed[1]![1])).toEqual(OCT25.blocks.map((b) => b.start + OFFSET.price));
    const lastMetric = rules.findIndex((r) => formulaOf(r).includes('>0,2'));
    expect(closed[0]![0]).toBeGreaterThan(lastMetric);
  });
  it('уровни: 3 метрики × 4 уровня, первые по приоритету; формула — «эта ячейка» и MAX колонки', () => {
    const S = 'INDEX($A771:$WN771;1;COLUMN())';
    const tier4 = [...['0,75', '0,5', '0,25'].map((c) => `=AND(${S}<>"";${S}>${c}*MAX(INDEX($A$771:$WN$801;0;COLUMN())))`), `=AND(${S}<>"";${S}>0)`];
    expect(rules.slice(0, 12).map(formulaOf)).toEqual([...tier4, ...tier4, ...tier4]);
    expect(rules.slice(0, 4).map(bgOf)).toEqual([...COLOR.tierCarts]);
    expect(cols(rules[0]!)).toEqual([7, ...OCT25.blocks.map((b) => b.start + OFFSET.carts)]);
    expect(cols(rules[8]!)).toEqual([6, ...OCT25.blocks.map((b) => b.start + OFFSET.orders)]);
  });
  it('оборачиваемость и знак доходности — цвета сентября; отмены и «показы без заказов» — сосед на 2 колонки левее', () => {
    const t = rules.filter((r) => /<=15\)$/.test(formulaOf(r)) || /<=30\)$/.test(formulaOf(r)) || /<=60\)$/.test(formulaOf(r)) || />60\)$/.test(formulaOf(r)));
    expect(t.map((r) => [bgOf(r), fgOf(r)])).toEqual(COLOR.turnover.map((x) => [...x]));
    const canc = rules.find((r) => formulaOf(r).includes('>=3'))!;
    expect(formulaOf(canc)).toContain('INDEX($A771:$WN771;1;COLUMN()-2)>0');
    expect(cols(canc)).toEqual([8, ...OCT25.blocks.map((b) => b.start + OFFSET.cancels)]);
    const zo = rules.find((r) => formulaOf(r).endsWith('=0)') && formulaOf(r).includes('$B771<=$WZ$736'))!;
    expect(bgOf(zo)).toBe(COLOR.zeroOrdersBg);
    expect(cols(zo)).toEqual([6, ...OCT25.blocks.map((b) => b.start + OFFSET.orders)]);
  });
  it('шкалы: по одному правилу на блок (min/50 %/max), сводка G и I; всего 108 правил', () => {
    const grads = rules.filter((r) => r.gradientRule);
    expect(grads).toHaveLength(25 * 3 + 2);
    expect(grads.every((r) => r.ranges.length === 1)).toBe(true);
    expect(cf.families).toMatchObject({ tier_carts: 4, tier_ads: 4, tier_orders: 4, weekend: 1, future: 1, grad_profit: 26, turnover: 4, profit1: 4, profit_sign: 3, cancels: 1, grad_carts: 26, grad_profit1: 25, drr: 1, zero_orders: 1, closed_fact: 1, closed_price: 1, closed_rate: 1 });
    expect(rules).toHaveLength(108);
    expect(cfRequests(cf, 7, 154).map((r) => (r.addConditionalFormatRule as { index: number }).index)).toEqual(rules.map((_, i) => 154 + i));
  });
  it('идиомы и ширина: последняя колонка секции — WN для 25 блоков, XL для 26; якорь по параметру', () => {
    expect(cfIdioms(OCT25, ANCHOR).SELF).toBe('INDEX($A771:$WN771,1,COLUMN())');
    expect(cfIdioms(OCT25, ANCHOR).CLOSED).toBe('$B771<=$WZ$736');
    const l26 = layoutOf(geometryAt({ year: 2026, month: 10 }, 769), blocks(Array.from({ length: 26 }, (_, i) => i)));
    expect(cfIdioms(l26, 648).COLMAX).toBe(`MAX(INDEX($A$771:$${colA1(636)}$801,0,COLUMN()))`);
    expect(buildConditionalFormats(l26, 7, 'COMMA', 648).rules).toHaveLength(108 + 3);
  });
  it('февраль 2027 (28 дней) и 24 блока: строки 910..937', () => {
    const feb = layoutOf(geometryAt({ year: 2027, month: 2 }, 908), blocks(Array.from({ length: 24 }, (_, i) => i)));
    const r = buildConditionalFormats(feb, 7, 'COMMA', 600).rules;
    for (const x of r) for (const g of x.ranges) expect([g.startRowIndex, g.endRowIndex]).toEqual([909, 937]);
    expect(formulaOf(r.find((x) => formulaOf(x).includes('WEEKDAY'))!)).toBe('=WEEKDAY($B910,2)>5');
    expect(r).toHaveLength(12 + 1 + 1 + 25 + 4 + 4 + 2 + 1 + 25 + 24 + 1 + 1 + 1 + 3);
  });
});

describe('контракт границ (borderSpec): сводка A..L — один контур, сетка дня тонкая, границы блоков средние серые', () => {
  const g = OCT25;
  const w = (k: 'title' | 'header' | 'day' | 'mtd' | 'plan', c: number) => { const s = borderSpec(k, c, g)!; return [s.top.w, s.bottom.w, s.left.w, s.right.w].join('/'); };
  it('строки секции по типу; вне блоков и сводки — нет контракта', () => {
    expect([769, 770, 771, 801, 802, 803, 768, 804].map((r) => rowKindOf(g, r))).toEqual(['title', 'header', 'day', 'day', 'mtd', 'plan', null, null]);
    expect(borderSpec('day', 613, g)).toBeNull(); // хвост книги
    expect(SUMMARY_LAST_COLUMN).toBe(12);
  });
  it('сводка: толстая рамка слева A и справа L (контур A..L), B|C — средний разделитель, день — тонкая сетка снизу; A и L без внутренних горизонталей', () => {
    expect(w('day', 1)).toBe('NONE/NONE/THICK/THIN');
    expect(w('day', 2)).toBe('NONE/THIN/THIN/MEDIUM');
    expect(w('day', 3)).toBe('NONE/THIN/MEDIUM/THIN');
    expect(w('day', 11)).toBe('NONE/THIN/THIN/THIN');   // K — правая грань больше не толстая
    expect(w('day', 12)).toBe('NONE/NONE/THIN/THICK');  // L внутри контура
    expect(w('header', 12)).toBe('MEDIUM/MEDIUM/THIN/THICK');
    expect(w('mtd', 1)).toBe('MEDIUM/THICK/THICK/THIN');
    expect(w('mtd', 12)).toBe('MEDIUM/THICK/THIN/THICK');
    expect(w('title', 1)).toBe('THICK/MEDIUM/THICK/NONE');
    expect(w('title', 12)).toBe('THICK/MEDIUM/NONE/THICK');
    expect(w('plan', 12)).toBe('NONE/MEDIUM/NONE/MEDIUM');
  });
  it('блок: дата — средняя серая слева и средняя чёрная справа, тело — тонкая сетка, день недели — только правая средняя; блок 25 = блок 1', () => {
    for (const st of [13, 589]) {
      expect(w('day', st)).toBe('NONE/THIN/MEDIUM/MEDIUM');
      expect(borderSpec('day', st, g)!.left.color).toBe(LINE.block);
      expect(w('day', st + OFFSET.views)).toBe('NONE/THIN/THIN/THIN');
      expect(w('day', st + OFFSET.weekday)).toBe('NONE/NONE/NONE/MEDIUM');
      expect(w('header', st + OFFSET.views)).toBe('NONE/THIN/THIN/THIN');
      expect(borderSpec('header', st + OFFSET.views, g)!.bottom.color).toBe(LINE.header);
      expect(w('mtd', st)).toBe('MEDIUM/MEDIUM/MEDIUM/MEDIUM');
      expect(w('mtd', st + OFFSET.weekday)).toBe('MEDIUM/MEDIUM/NONE/MEDIUM');
      expect(w('plan', st)).toBe('NONE/MEDIUM/MEDIUM/NONE');
      expect(w('title', st)).toBe('MEDIUM/MEDIUM/MEDIUM/NONE');
    }
    expect(Array.from({ length: 24 }, (_, o) => w('day', 13 + o))).toEqual(Array.from({ length: 24 }, (_, o) => w('day', 589 + o)));
  });
  it('день никогда не получает среднюю/толстую линию сверху (нет «клетки» по строкам); JSON — все четыре стороны явно', () => {
    for (let c = 1; c <= 612; c++) expect(borderSpec('day', c, g)!.top.w).toBe('NONE');
    const j = bordersJson(borderSpec('day', 13, g)!) as Record<string, { style: string; colorStyle?: { rgbColor: { red: number } } }>;
    expect(Object.keys(j).sort()).toEqual(['bottom', 'left', 'right', 'top']);
    expect(j.top).toEqual({ style: 'NONE' });
    expect(j.left!.style).toBe('SOLID_MEDIUM');
    expect(j.bottom!.style).toBe('SOLID');
  });
});
