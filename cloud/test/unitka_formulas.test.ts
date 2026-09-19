/**
 * UNITKA CALENDAR V2 (Phase 2B) — построители формул: воспроизводят живой сентябрь 2026 символ в символ
 * и дают календарно-безопасные диапазоны для любого месяца (без утечки в соседние месяцы).
 */
import { describe, it, expect } from 'vitest';
import { OFFSET, SUMMARY, colA1 } from '../src/loaders/unitka/model.js';
import { geometryAt, slotStart } from '../src/loaders/unitka/calendar.js';
import {
  blockDayFormulas, blockProjectionFormulas, blockMtdFormulas, summaryDayFormulas, summaryMtdFormulas, normFormula,
  type BlockFormulaParams,
} from '../src/loaders/unitka/formulas.js';
import { SEPT_LIVE_FORMULAS as LIVE } from './unitka_sept_live_formulas.js';

const SEPT = geometryAt({ year: 2026, month: 9 }, 735);
const OCT = geometryAt({ year: 2026, month: 10 }, 769);
const FEB27 = geometryAt({ year: 2027, month: 2 }, 908);
const B1: BlockFormulaParams = { start: 13, cogsTerm: '$R$45', overhead: '100', stockProjection: 'guarded', storageProjection: 'guarded' };
const B1_EARLY: BlockFormulaParams = { ...B1, overhead: '10' }; // строки 737–738 блока 1 ещё с «+10»
const B2: BlockFormulaParams = { start: 37, cogsTerm: '0', overhead: '10', stockProjection: 'plain', storageProjection: 'none' };

/** Все ссылки на строки листа в формуле (A1-адреса), без имён диапазонов. */
function rowsReferenced(f: string): number[] {
  return [...f.matchAll(/\$?[A-Z]{1,3}\$?(\d+)/g)].map((m) => Number(m[1]));
}

describe('эталон сентября: построители = живые формулы листа', () => {
  it('в эталоне нет __xludf.DUMMYFUNCTION (обёртка XLSX снята)', () => {
    for (const f of Object.values(LIVE)) expect(f).not.toMatch(/__xludf|DUMMYFUNCTION/);
    expect(Object.keys(LIVE).length).toBeGreaterThan(100);
  });
  it.each([[737, B1_EARLY], [766, B1]])('блок 1 (M..AJ), строка %i: все расчётные колонки и день недели', (row, p) => {
    let n = 0;
    for (const [off, f] of blockDayFormulas(p, row)) { expect(f).toBe(LIVE[`${colA1(13 + off)}${row}`]); n++; }
    expect(n).toBe(10);
  });
  it('блок 2 (AK..BH), строка 766: расчётные колонки и проекция остатка без обёртки', () => {
    for (const [off, f] of blockDayFormulas(B2, 766)) expect(f).toBe(LIVE[`${colA1(37 + off)}766`]);
    const proj = blockProjectionFormulas(B2, 766, false);
    expect(proj.get(OFFSET.stock)).toBe(LIVE.AR766);
    expect(proj.has(OFFSET.storage)).toBe(false);
  });
  it('блок 1, строка 766: проекции остатка и хранения с обёрткой LAST_CLOSED_DATE', () => {
    const proj = blockProjectionFormulas(B1, 766, false);
    expect(proj.get(OFFSET.stock)).toBe(LIVE.T766);
    expect(proj.get(OFFSET.storage)).toBe(LIVE.AG766);
  });
  it.each([13, 37])('строка MTD 767, блок в колонке %i: все формулы', (start) => {
    let n = 0;
    for (const [off, f] of blockMtdFormulas(start, SEPT)) { expect(f).toBe(LIVE[`${colA1(start + off)}767`]); n++; }
    expect(n).toBe(21);
  });
  it.each([737, 766])('сводка A..L строки %i (24 блока: правая граница UT/UW/VI)', (row) => {
    for (const [col, f] of summaryDayFormulas(row, 23, 13)) expect(f).toBe(LIVE[`${colA1(col)}${row}`]);
  });
  it('сводка строки MTD 767 (C..K)', () => {
    for (const [col, f] of summaryMtdFormulas(SEPT, 23)) expect(f).toBe(LIVE[`${colA1(col)}767`]);
  });
});

describe('октябрь 2026 — 31 день, 25 блоков сплошь (блок 25 = слот 24, VQ..WN)', () => {
  const B25: BlockFormulaParams = { start: slotStart(24), cogsTerm: '426.735', overhead: '10', stockProjection: 'guarded', storageProjection: 'none' };
  it('день 31 (строка 801) и MTD (802): диапазоны 771..801', () => {
    expect(blockDayFormulas(B25, 801).get(OFFSET.unitProfit)).toBe('=IF($VQ801>LAST_CLOSED_DATE,"",WI801-WJ801-WL801-426.735)');
    const mtd = blockMtdFormulas(B25.start, OCT);
    expect(mtd.get(OFFSET.orders)).toBe('=SUMIF($VQ$771:$VQ$801,"<="&LAST_CLOSED_DATE,VU771:VU801)');
    expect(mtd.get(OFFSET.stock)).toBe('=ARRAY_CONSTRAIN(ARRAYFORMULA(IFERROR(INDEX(VX771:VX801,MATCH(LAST_CLOSED_DATE,$VQ$771:$VQ$801,0)),"")), 1, 1)');
    expect(summaryMtdFormulas(OCT, 24).get(SUMMARY.profit)).toBe('=SUM(I771:I801)');
  });
  it('сводка: правая граница — блок 25 (N→VR, X→WB, Q→VU, AC→WG), FILTER по MOD 24', () => {
    const s = summaryDayFormulas(771, 24, 13);
    expect(s.get(SUMMARY.bloggers)).toBe('=IF($B771>LAST_CLOSED_DATE,"",SUM(FILTER(N771:VR771,MOD(COLUMN(N771:VR771)-COLUMN(N771),24)=0)))');
    expect(s.get(SUMMARY.ads)).toContain('FILTER(X771:WB771,');
    expect(s.get(SUMMARY.drr)).toContain('FILTER($Q$771:$VU$771,');
    expect(s.get(SUMMARY.drr)).toContain('FILTER($AC$771:$WG$771,');
  });
  it('первый день без проекции остатка (никакой ссылки на сентябрь); второй — внутри секции', () => {
    expect(blockProjectionFormulas(B25, 771, true).has(OFFSET.stock)).toBe(false);
    expect(blockProjectionFormulas(B25, 772, false).get(OFFSET.stock)).toBe('=IF($VQ772>LAST_CLOSED_DATE,"",VX771-VU772+VW771)');
  });
  it('ни одна формула октября не ссылается на строки вне 771..802 (межмесячной утечки нет)', () => {
    const all: string[] = [];
    for (let r = OCT.firstDailyRow; r <= OCT.lastDailyRow; r++) {
      all.push(...blockDayFormulas(B25, r).values(), ...blockProjectionFormulas(B25, r, r === OCT.firstDailyRow).values(), ...summaryDayFormulas(r, 24, 13).values());
    }
    all.push(...blockMtdFormulas(B25.start, OCT).values(), ...summaryMtdFormulas(OCT, 24).values());
    const rows = all.flatMap(rowsReferenced);
    expect(rows.length).toBeGreaterThan(1000);
    expect(rows.every((r) => r >= OCT.firstDailyRow && r <= OCT.mtdRow)).toBe(true);
    expect(all.every((f) => !/__xludf|DUMMYFUNCTION/.test(f))).toBe(true);
    expect(rows.some((r) => r >= 735 && r <= 768)).toBe(false); // ни одной строки сентября
  });
  it('разрешённые глобальные ссылки: $R$45 (COGS блока 1) и имена LAST_CLOSED_DATE / REVERSE_LEG_RATE', () => {
    const f = blockDayFormulas({ ...B1, start: 13 }, 771);
    expect(f.get(OFFSET.unitProfit)).toBe('=IF($M771>LAST_CLOSED_DATE,"",AE771-AF771-AH771-$R$45)');
    expect(f.get(OFFSET.profitAll)).toContain('REVERSE_LEG_RATE');
  });
});

describe('февраль: последний день и MTD', () => {
  it('февраль 2027 (28 дней): строки 910..937, MTD 938', () => {
    const m = blockMtdFormulas(13, FEB27);
    expect(m.get(OFFSET.views)).toBe('=SUMIF($M$910:$M$937,"<="&LAST_CLOSED_DATE,O910:O937)');
    expect(summaryMtdFormulas(FEB27, 23).get(SUMMARY.drr)).toContain('$Q$910:$UW$937');
    expect(blockDayFormulas(B2, 937).get(OFFSET.weekday)).toBe('=IF($AK937="","",CHOOSE(WEEKDAY($AK937,2),"пн","вт","ср","чт","пт","сб","вс"))');
  });
  it('февраль 2028 (29 дней): 29.02 — строка 1351, MTD 1352', () => {
    const g = geometryAt({ year: 2028, month: 2 }, 1321);
    expect([g.lastDailyRow, g.mtdRow]).toEqual([1351, 1352]);
    expect(blockMtdFormulas(13, g).get(OFFSET.adsIn)).toBe('=SUMIF($M$1323:$M$1351,"<="&LAST_CLOSED_DATE,X1323:X1351)');
  });
  it('нормализация сравнения: пробелы и регистр не значимы', () => {
    expect(normFormula('=sum( a1 : a2 )')).toBe(normFormula('=SUM(A1:A2)'));
  });
});
