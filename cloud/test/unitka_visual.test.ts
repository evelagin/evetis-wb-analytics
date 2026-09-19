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
const ANCHOR = 623; // WY — колонка якорей после вставки блока 25 (23 колонки: у последнего блока нет разделителя)
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

describe('правила УФ октября 2026 (25 блоков сплошь, якоря в WY)', () => {
  const cf = buildConditionalFormats(OCT25, 7, 'SEMICOLON', ANCHOR);
  const rules = cf.rules;
  it('все диапазоны — строки 771..801, только сводка и колонки блоков; резерв не покрыт', () => {
    for (const r of rules) for (const g of r.ranges) {
      expect([g.startRowIndex, g.endRowIndex, g.sheetId]).toEqual([770, 801, 7]);
      expect(g.endColumnIndex).toBeLessThanOrEqual(611);   // терминальная колонка WM: колонки-сироты за последним SKU нет
    }
  });
  it('ни одной относительной ссылки на колонку в формулах (независимость от якоря)', () => {
    const fs = rules.map(formulaOf).filter(Boolean);
    expect(fs.length).toBeGreaterThan(20);
    for (const f of fs) expect(relativeColumnRefs(fromLocaleFormula(f, 'SEMICOLON'))).toEqual([]);
    expect(relativeColumnRefs('=WEEKDAY(US771,2)>5')).toEqual(['US771']);
    expect(relativeColumnRefs('=AND($B771<=$WY$736,INDEX($A771:$WM771,1,COLUMN())<>"")')).toEqual([]);
  });
  it('выходные: одна формула по дате строки ($B), заливка #fcefe3 на A, B, L и дате/дне недели каждого блока', () => {
    const wk = rules.filter((r) => formulaOf(r).includes('WEEKDAY'));
    expect(wk).toHaveLength(1);
    expect(formulaOf(wk[0]!)).toBe('=WEEKDAY($B771;2)>5');
    expect(bgOf(wk[0]!)).toBe(COLOR.weekendBg);
    const c = cols(wk[0]!);
    expect(c).toEqual(expect.arrayContaining([1, 2, 12, 13, 36, 565, 588, 589]));
    expect(c).not.toContain(612);                         // у последнего блока нет колонки дня недели
    expect(c).toHaveLength(3 + 25 + 24);
  });
  it('будущий день — серый текст: после выходных (первое правило побеждает), сводка A..L и весь блок; якорь — WY', () => {
    const fut = rules.findIndex((r) => formulaOf(r) === '=$B771>$WY$736');
    const wk = rules.findIndex((r) => formulaOf(r).includes('WEEKDAY'));
    expect(fut).toBeGreaterThan(wk);
    expect(fgOf(rules[fut]!)).toBe(COLOR.futureFg);
    expect(cols(rules[fut]!)).toHaveLength(12 + 25 * 24 - 1);
    expect(Math.max(...cols(rules[fut]!))).toBe(611);
    expect(rules.some((r) => formulaOf(r).includes('$WB$736'))).toBe(false);
  });
  it('заливка закрытого дня: закрыт И значение; факт зелёная, цена жёлтая, ставки голубые; стоят после метрических правил', () => {
    const closed = rules.map((r, i) => [i, r] as const).filter(([, r]) => formulaOf(r) === '=AND($B771<=$WY$736;INDEX($A771:$WM771;1;COLUMN())<>"")');
    expect(closed.map(([, r]) => bgOf(r))).toEqual([COLOR.factBg, COLOR.manualBg, COLOR.rateBg]);
    const green = cols(closed[0]![1]);
    expect(green).toEqual(expect.arrayContaining([4, 5, 6, 7, 8, 10, 15, 16, 17, 18, 19, 20, 24, 33, 589 + OFFSET.storage]));
    expect(green).not.toContain(3); expect(green).not.toContain(9); expect(green).not.toContain(14); expect(green).not.toContain(28);
    expect(cols(closed[1]![1])).toEqual(OCT25.blocks.map((b) => b.start + OFFSET.price));
    const lastMetric = rules.findIndex((r) => formulaOf(r).includes('>0,2'));
    expect(closed[0]![0]).toBeGreaterThan(lastMetric);
  });
  it('уровни: 3 метрики × 4 уровня, первые по приоритету; формула — «эта ячейка» и MAX колонки', () => {
    const S = 'INDEX($A771:$WM771;1;COLUMN())';
    const tier4 = [...['0,75', '0,5', '0,25'].map((c) => `=AND(${S}<>"";${S}>${c}*MAX(INDEX($A$771:$WM$801;0;COLUMN())))`), `=AND(${S}<>"";${S}>0)`];
    expect(rules.slice(0, 12).map(formulaOf)).toEqual([...tier4, ...tier4, ...tier4]);
    expect(rules.slice(0, 4).map(bgOf)).toEqual([...COLOR.tierCarts]);
    expect(cols(rules[0]!)).toEqual([7, ...OCT25.blocks.map((b) => b.start + OFFSET.carts)]);
    expect(cols(rules[8]!)).toEqual([6, ...OCT25.blocks.map((b) => b.start + OFFSET.orders)]);
  });
  it('оборачиваемость и знак доходности — цвета сентября; отмены и «показы без заказов» — сосед на 2 колонки левее', () => {
    const t = rules.filter((r) => /<=15\)$/.test(formulaOf(r)) || /<=30\)$/.test(formulaOf(r)) || /<=60\)$/.test(formulaOf(r)) || />60\)$/.test(formulaOf(r)));
    expect(t.map((r) => [bgOf(r), fgOf(r)])).toEqual(COLOR.turnover.map((x) => [...x]));
    const canc = rules.find((r) => formulaOf(r).includes('>=3'))!;
    expect(formulaOf(canc)).toContain('INDEX($A771:$WM771;1;COLUMN()-2)>0');
    expect(cols(canc)).toEqual([8, ...OCT25.blocks.map((b) => b.start + OFFSET.cancels)]);
    const zo = rules.find((r) => formulaOf(r).endsWith('=0)') && formulaOf(r).includes('$B771<=$WY$736'))!;
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
  it('идиомы и ширина: последняя колонка секции — WM для 25 блоков, XK для 26; якорь по параметру', () => {
    expect(cfIdioms(OCT25, ANCHOR).SELF).toBe('INDEX($A771:$WM771,1,COLUMN())');
    expect(cfIdioms(OCT25, ANCHOR).CLOSED).toBe('$B771<=$WY$736');
    const l26 = layoutOf(geometryAt({ year: 2026, month: 10 }, 769), blocks(Array.from({ length: 26 }, (_, i) => i)));
    expect(cfIdioms(l26, 647).COLMAX).toBe(`MAX(INDEX($A$771:$${colA1(635)}$801,0,COLUMN()))`);
    expect(colA1(635)).toBe('XK');
    expect(buildConditionalFormats(l26, 7, 'COMMA', 647).rules).toHaveLength(108 + 3);
  });
  it('февраль 2027 (28 дней) и 24 блока: строки 910..937', () => {
    const feb = layoutOf(geometryAt({ year: 2027, month: 2 }, 908), blocks(Array.from({ length: 24 }, (_, i) => i)));
    const r = buildConditionalFormats(feb, 7, 'COMMA', 600).rules;
    for (const x of r) for (const g of x.ranges) expect([g.startRowIndex, g.endRowIndex]).toEqual([909, 937]);
    expect(formulaOf(r.find((x) => formulaOf(x).includes('WEEKDAY'))!)).toBe('=WEEKDAY($B910,2)>5');
    expect(r).toHaveLength(12 + 1 + 1 + 25 + 4 + 4 + 2 + 1 + 25 + 24 + 1 + 1 + 1 + 3);
  });
});

describe('контракт границ (borderSpec): лёгкое тело дня, один владелец у каждой грани, сводка | SKU, терминальный блок', () => {
  const g = OCT25;
  const END = g.lastBlockColumn;          // 611 (WM) — последняя метрика блока 25
  type K = 'title' | 'header' | 'dayFirst' | 'day' | 'mtd' | 'plan';
  const KINDS: K[] = ['title', 'header', 'dayFirst', 'day', 'mtd', 'plan'];
  const SIDES = ['top', 'bottom', 'left', 'right'] as const;
  const w = (k: K, c: number) => { const s = borderSpec(k, c, g)!; return [s.top.w, s.bottom.w, s.left.w, s.right.w].join('/'); };
  it('строки секции по типу; вне раскладки — нет контракта (в т.ч. колонка за последней метрикой последнего SKU)', () => {
    expect([769, 770, 771, 772, 801, 802, 803, 768, 804].map((r) => rowKindOf(g, r))).toEqual(['title', 'header', 'dayFirst', 'day', 'day', 'mtd', 'plan', null, null]);
    expect(END).toBe(611);
    for (const k of KINDS) { expect(borderSpec(k, 612, g)).toBeNull(); expect(borderSpec(k, 613, g)).toBeNull(); }
    expect(SUMMARY_LAST_COLUMN).toBe(12);
  });
  it('сводка: контур A..L толстый, B|C — средний разделитель; внутренние горизонтали дня — волосяные сверху; A и L без горизонталей', () => {
    expect(w('day', 1)).toBe('NONE/NONE/THICK/THIN');
    expect(w('day', 2)).toBe('THIN/NONE/THIN/MEDIUM');
    expect(w('day', 3)).toBe('THIN/NONE/MEDIUM/THIN');
    expect(w('day', 11)).toBe('THIN/NONE/THIN/THIN');
    expect(w('day', 12)).toBe('NONE/NONE/THIN/THICK');
    expect(w('dayFirst', 2)).toBe('NONE/NONE/THIN/MEDIUM');
    expect(w('header', 12)).toBe('MEDIUM/MEDIUM/THIN/THICK');
    expect(w('mtd', 1)).toBe('MEDIUM/THICK/THICK/THIN');
    expect(w('mtd', 12)).toBe('MEDIUM/THICK/THIN/THICK');
    expect(w('title', 1)).toBe('THICK/MEDIUM/THICK/NONE');
    expect(w('title', 12)).toBe('THICK/MEDIUM/NONE/THICK');
    expect(w('plan', 12)).toBe('NONE/MEDIUM/NONE/MEDIUM');
  });
  it('F1 · граница сводка → SKU: грань L|M принадлежит сводке (правая грань L), у первого блока своей левой грани нет', () => {
    for (const k of KINDS) {
      const L = borderSpec(k, 12, g)!.right, M = borderSpec(k, 13, g)!.left;
      expect(M).toEqual({ w: 'NONE' });                                          // не двойная: блок 1 грань не задаёт
      expect(L.w).not.toBe('NONE');                                             // и не пропавшая
      expect(L).toEqual(k === 'plan' ? { w: 'MEDIUM', color: LINE.block } : { w: 'THICK', color: LINE.black });
    }
    // у остальных блоков левая грань даты — их собственная граница SKU | SKU (она совпадает с правой гранью разделителя).
    for (const k of KINDS.filter((x) => x !== 'plan')) expect(borderSpec(k, 37, g)!.left).toEqual({ w: 'MEDIUM', color: LINE.block });
  });
  it('блок: дата — средняя чёрная справа (дата | данные), тело — волосяная сетка, разделитель — только правая средняя серая', () => {
    for (const st of [37, 565]) {
      expect(w('day', st)).toBe('THIN/NONE/MEDIUM/MEDIUM');
      expect(borderSpec('day', st, g)!.left.color).toBe(LINE.block);
      expect(borderSpec('day', st, g)!.right.color).toBe(LINE.black);
      expect(w('day', st + OFFSET.bloggers)).toBe('THIN/NONE/MEDIUM/THIN');
      expect(borderSpec('day', st + OFFSET.bloggers, g)!.left).toEqual(borderSpec('day', st, g)!.right);
      expect(w('day', st + OFFSET.views)).toBe('THIN/NONE/THIN/THIN');
      expect(w('dayFirst', st + OFFSET.views)).toBe('NONE/NONE/THIN/THIN');
      expect(w('day', st + OFFSET.weekday)).toBe('NONE/NONE/NONE/MEDIUM');
      expect(w('header', st + OFFSET.views)).toBe('NONE/THIN/THIN/THIN');
      expect(borderSpec('header', st + OFFSET.views, g)!.bottom.color).toBe(LINE.header);
      expect(w('mtd', st)).toBe('MEDIUM/MEDIUM/MEDIUM/MEDIUM');
      expect(w('mtd', st + OFFSET.weekday)).toBe('MEDIUM/MEDIUM/NONE/MEDIUM');
      expect(w('plan', st)).toBe('NONE/MEDIUM/MEDIUM/NONE');
      expect(w('title', st)).toBe('MEDIUM/MEDIUM/MEDIUM/NONE');
    }
    // все «средние» блоки одинаковы; первый отличается только левой гранью (она у сводки), последний — только концом.
    for (const k of KINDS) {
      expect(Array.from({ length: 24 }, (_, o) => borderSpec(k, 37 + o, g))).toEqual(Array.from({ length: 24 }, (_, o) => borderSpec(k, 565 + o, g)));
      expect(Array.from({ length: 23 }, (_, o) => borderSpec(k, 14 + o, g))).toEqual(Array.from({ length: 23 }, (_, o) => borderSpec(k, 38 + o, g)));
      expect(Array.from({ length: 22 }, (_, o) => borderSpec(k, 589 + o, g))).toEqual(Array.from({ length: 22 }, (_, o) => borderSpec(k, 37 + o, g)));
    }
  });
  it('F2 · терминальный блок: цепочку закрывает правая грань ПОСЛЕДНЕЙ МЕТРИКИ (средняя серая), колонки дня недели после неё нет', () => {
    for (const k of KINDS) {
      expect(borderSpec(k, END, g)!.right).toEqual({ w: 'MEDIUM', color: LINE.block });
      expect(borderSpec(k, END + 1, g)).toBeNull();
    }
    // у не-последнего блока последняя метрика закрыта волосяной/обычной линией, а блок закрывает разделитель.
    expect(borderSpec('day', 565 + 22, g)!.right).toEqual({ w: 'THIN', color: LINE.hair });
    expect(borderSpec('day', 565 + 23, g)!.right).toEqual({ w: 'MEDIUM', color: LINE.block });
    for (const n of [1, 24, 26]) {
      const l = layoutOf(geometryAt({ year: 2026, month: 10 }, 769), blocks(Array.from({ length: n }, (_, i) => i)));
      const end = slotStart(n - 1) + 22;
      expect(l.lastBlockColumn).toBe(end);
      for (const k of KINDS) { expect(borderSpec(k, end, l)!.right).toEqual({ w: 'MEDIUM', color: LINE.block }); expect(borderSpec(k, end + 1, l)).toBeNull(); }
    }
  });
  it('тело дня: каждая ТОНКАЯ линия — светлая волосяная; сильные линии — ровно смысловые разделители, и больше нигде', () => {
    expect(LINE.hair).toBe('#d9d9d9');
    for (const k of ['dayFirst', 'day'] as const) {
      const strong: string[] = [];
      for (let c = 1; c <= END; c++) {
        const s = borderSpec(k, c, g)!;
        for (const side of SIDES) {
          const x = s[side];
          if (x.w === 'NONE') continue;
          if (x.w === 'THIN') { expect(x.color).toBe(LINE.hair); continue; }
          strong.push(`${c}:${side}:${x.w}:${x.color}`);
        }
      }
      const want = [
        `1:left:THICK:${LINE.black}`, `12:right:THICK:${LINE.black}`,           // контур сводки (он же граница сводка | SKU)
        `2:right:MEDIUM:${LINE.black}`, `3:left:MEDIUM:${LINE.black}`,          // дата | данные сводки
        ...g.blocks.flatMap((b, i) => [
          ...(i === 0 ? [] : [`${b.start}:left:MEDIUM:${LINE.block}`]),                                          // SKU | SKU (у первого — грань сводки)
          i === g.blocks.length - 1 ? `${b.start + 22}:right:MEDIUM:${LINE.block}` : `${b.start + 23}:right:MEDIUM:${LINE.block}`,
          `${b.start}:right:MEDIUM:${LINE.black}`, `${b.start + 1}:left:MEDIUM:${LINE.black}`,                  // дата | данные блока
        ]),
      ];
      expect(strong.sort()).toEqual(want.sort());
    }
  });
  it('день не получает ни одной линии снизу и ни одной средней/толстой сверху: горизонталь принадлежит нижней строке, последнюю закрывает MTD', () => {
    for (let c = 1; c <= END; c++) for (const k of ['dayFirst', 'day'] as const) {
      const s = borderSpec(k, c, g)!;
      expect(s.bottom.w).toBe('NONE');
      expect(['NONE', 'THIN']).toContain(s.top.w);
    }
    for (let c = 1; c <= END; c++) expect(borderSpec('dayFirst', c, g)!.top.w).toBe('NONE');
  });
  it('у общей грани один владелец — БЕЗ исключений (грань L|M больше не задаётся дважды)', () => {
    const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
    const vertical: Array<[K, K]> = [['title', 'header'], ['header', 'dayFirst'], ['dayFirst', 'day'], ['day', 'day'], ['day', 'mtd'], ['mtd', 'plan']];
    for (const [up, down] of vertical) for (let c = 1; c <= END; c++) {
      const a = borderSpec(up, c, g)!.bottom, b = borderSpec(down, c, g)!.top;
      expect(a.w === 'NONE' || b.w === 'NONE' || same(a, b), `${up}→${down} колонка ${c}`).toBe(true);
    }
    for (const k of KINDS) for (let c = 1; c < END; c++) {
      const a = borderSpec(k, c, g)!.right, b = borderSpec(k, c + 1, g)!.left;
      expect(a.w === 'NONE' || b.w === 'NONE' || same(a, b), `${k} колонки ${c}|${c + 1}`).toBe(true);
    }
  });
  it('шапка, MTD и строка плана сохраняют структуру (не волосяные); JSON — все четыре стороны явно', () => {
    for (let c = 1; c <= END; c++) for (const k of ['title', 'header', 'mtd', 'plan'] as const) {
      const s = borderSpec(k, c, g)!;
      for (const side of SIDES) expect(s[side].color === LINE.hair).toBe(false);
    }
    const j = bordersJson(borderSpec('day', 14 + OFFSET.views, g)!) as Record<string, { style: string; colorStyle?: { rgbColor: { red: number } } }>;
    expect(Object.keys(j).sort()).toEqual(['bottom', 'left', 'right', 'top']);
    expect(j.bottom).toEqual({ style: 'NONE' });
    expect(j.top!.style).toBe('SOLID');
    expect(j.top!.colorStyle!.rgbColor.red).toBeCloseTo(0xd9 / 255, 5);
    expect((bordersJson(borderSpec('day', 37, g)!) as Record<string, { style: string }>).left!.style).toBe('SOLID_MEDIUM');
  });
  it('семантика колонок: у последнего блока нет колонки «день недели»', () => {
    expect(columnKind(565 + 23, g)).toBe('weekday');
    expect(columnKind(589 + 22, g)).toBe('calc');
    expect(columnKind(589 + 23, g)).toBeNull();
  });
});
