/**
 * Phase B — «Оплата за заказ» в Ozon-Юнитке: «Реклама внутренняя» = CPC-атрибуция + CPO, каждый рубль один раз;
 * разложение — управляемой строкой заметки, заметка владельца сохраняется дословно.
 */
import { describe, expect, it } from 'vitest';
import { composeMonth, ozonMonthSpec, type OzonFactRow } from '../src/loaders/unitka/ozon/month.js';
import {
  buildOzonPlan, sectionFormulas, adsSplitLine, mergeAdsNote, ADS_SPLIT_LINE, type OzonSectionInput,
} from '../src/loaders/unitka/ozon/monthplan.js';
import { layoutOf } from '../src/loaders/unitka/ozon/requests.js';
import { ozonWrittenReader, verifyOzonModel } from '../src/loaders/unitka/ozon/model_qa.js';
import { ozonMonthFactsSql, ozonCpoFreshnessSql } from '../src/loaders/unitka/ozon/bq.js';
import { OZON_OFFSET } from '../src/loaders/unitka/ozon/offsets.js';
import { OZON_FIELD_SOURCE_MAP } from '../src/loaders/unitka/ozon/contract.js';
import { loadConfig } from '../src/config.js';
import { readFileSync } from 'node:fs';

const SKU = '305101272';
const SEP = ozonMonthSpec(2026, 9, 570, 31, ['fixture-a', 'fixture-b', 'fixture-c', SKU]);
const ROW = 626;                                                // 2026-09-20
const ADS_COL = SEP.anchor[SKU]! + OZON_OFFSET.adsIn;
const BASE: OzonFactRow = { d: '2026-09-20', offer_id: SKU, gross_qty: 1, cancelled_qty: 0, realized_qty: 1,
  expected_realized_qty: 1, revenue: 1287, provisional_revenue_rub: 1287, cogs_amt: 130.81, provisional_cogs_rub: 130.81,
  cogs_missing_qty: 0, logistics: 105, logistics_state: 'ACTUAL', commission: 669.24, commission_state: 'ACTUAL',
  commission_missing_qty: 0, commission_not_applicable_qty: 0, economics_completeness: 'ACTUAL',
  ads_spend: 336.64, impr: 1192, clicks: 28, buyer_amt: null, seller_amt: null };

function prepared(facts: OzonFactRow[], adsNotes?: ReadonlyMap<string, string>) {
  const lcd = '2026-09-30';
  const comp = composeMonth(SEP, facts, {}, lcd, 1, {}, {});
  const sec: OzonSectionInput = { spec: SEP, facts, stock: {}, cart: {}, sheetInputs: {}, fromDay: 1,
    formulas: sectionFormulas(SEP, comp, 'SEMICOLON', 'OZON_LAST_CLOSED_DATE'), refTitle: [], refHeader: [], refAnchor: {} };
  const plan = buildOzonPlan({ sheetId: 1, sheetName: 'OZON_Юнит_2025', sections: [sec], allSections: [layoutOf(SEP)],
    grid: { current: { rows: 800, columns: 600 }, growth: { widenBlockAt: [], insertColumnsBefore: 590, insertColumnCount: 0, appendColumnCount: 0, appendRowCount: 0 } },
    lcd, lcdMirror: null, lcdRef: '$VB$2', blocks: SEP.blocks.length, adsNotes });
  return { comp, plan, sec, lcd, read: ozonWrittenReader(plan.values) };
}

type NoteReq = { repeatCell: { range: { startRowIndex: number; startColumnIndex: number }; cell: { note: string } } };
const adsNoteWrites = (plan: ReturnType<typeof prepared>['plan']) => (plan.presentation as unknown as NoteReq[])
  .filter((r) => r.repeatCell?.cell && 'note' in r.repeatCell.cell && r.repeatCell.range.startColumnIndex === ADS_COL - 1)
  .map((r) => ({ row: r.repeatCell.range.startRowIndex + 1, note: r.repeatCell.cell.note }));

describe('Phase B — «Реклама внутренняя» = CPC + «Оплата за заказ»', () => {
  it('без CPO ячейка и итог прежние (регрессия)', () => {
    const p = prepared([BASE]);
    expect(p.read(ROW, ADS_COL)).toBe(336.64);
    expect(p.comp.adsSplit).toEqual([]);
    expect(p.comp.totals.ads).toBeCloseTo(336.64, 6);
  });

  it('CPC + CPO: сумма в ячейке, разложение в adsSplit, итог месяца включает CPO ровно один раз', () => {
    const p = prepared([{ ...BASE, cpo_spend: 72.5 }]);
    expect(p.read(ROW, ADS_COL)).toBe(409.14);
    expect(p.comp.adsSplit).toEqual([{ row: ROW, offerId: SKU, date: '2026-09-20', cpc: 336.64, cpo: 72.5 }]);
    expect(p.comp.totals.ads).toBeCloseTo(409.14, 6);
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
  });

  it('CPO без CPC-статистики: только CPO, показы и клики не выдумываются', () => {
    const p = prepared([{ ...BASE, impr: null, clicks: null, ads_spend: 0, cpo_spend: 53.5 }]);
    expect(p.read(ROW, ADS_COL)).toBe(53.5);
    expect(p.read(ROW, SEP.anchor[SKU]! + OZON_OFFSET.views)).toBe('');
    expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read: p.read, mode: 'PLAN' })).toEqual([]);
  });

  it('QA ловит потерю или удвоение CPO в ячейке (инвариант «один раз»)', () => {
    const p = prepared([{ ...BASE, cpo_spend: 72.5 }]);
    for (const wrong of [336.64, 481.64]) {
      const read = (r: number, c: number) => (r === ROW && c === ADS_COL ? wrong : p.read(r, c));
      expect(verifyOzonModel({ sections: [p.sec], lcd: p.lcd, read, mode: 'PLAN' }).map((i) => i.code)).toContain('ADS_IN_NOT_CPC_PLUS_CPO');
    }
  });
});

describe('Phase B — управляемая заметка «Реклама внутренняя»', () => {
  const LINE = 'CPC / attributed Ads: 336,64 / Оплата за заказ: 72,50 / Итого: 409,14';

  it('строка разложения — ровно формат ACK и узнаётся движком', () => {
    expect(adsSplitLine(336.64, 72.5)).toBe(LINE);
    expect(ADS_SPLIT_LINE.test(LINE)).toBe(true);
    expect(ADS_SPLIT_LINE.test('CPC / attributed Ads: заметка владельца')).toBe(false);
  });

  it('заметка владельца сохраняется дословно: добавление, замена, снятие', () => {
    expect(mergeAdsNote('', LINE)).toBe(LINE);
    expect(mergeAdsNote('Блогер Маша 20.09', LINE)).toBe(`Блогер Маша 20.09\n${LINE}`);
    const older = 'CPC / attributed Ads: 1,00 / Оплата за заказ: 2,00 / Итого: 3,00';
    expect(mergeAdsNote(`Блогер Маша 20.09\n${older}`, LINE)).toBe(`Блогер Маша 20.09\n${LINE}`);
    expect(mergeAdsNote(`Блогер Маша 20.09\n${LINE}`, null)).toBe('Блогер Маша 20.09');
    expect(mergeAdsNote('строка 1\n\nстрока 2\n', null)).toBe('строка 1\n\nстрока 2\n');       // чужое не трогаем
    const roundTrip = mergeAdsNote(mergeAdsNote('владелец\n', LINE), null);
    expect(roundTrip).toBe('владелец\n');
  });

  it('план: заметка ставится только на ячейки с CPO и снимается там, где CPO ушёл; владелец не теряется', () => {
    const notes = new Map([
      [`${ROW}|${ADS_COL}`, 'Блогер Маша 20.09'],
      [`${ROW + 1}|${ADS_COL}`, 'Комментарий\nCPC / attributed Ads: 1,00 / Оплата за заказ: 2,00 / Итого: 3,00'],
      [`${ROW + 2}|${ADS_COL}`, 'Только владелец'],
    ]);
    const p = prepared([{ ...BASE, cpo_spend: 72.5 }], notes);
    expect(adsNoteWrites(p.plan)).toEqual([
      { row: ROW, note: `Блогер Маша 20.09\n${LINE}` },
      { row: ROW + 1, note: 'Комментарий' },
    ]);
  });

  it('флаг выключен (CPO нет): снимаются только устаревшие управляемые строки, владелец остаётся', () => {
    const notes = new Map([[`${ROW}|${ADS_COL}`, `Блогер\n${LINE}`], [`${ROW + 1}|${ADS_COL}`, 'Только владелец']]);
    expect(adsNoteWrites(prepared([BASE], notes).plan)).toEqual([{ row: ROW, note: 'Блогер' }]);
  });

  it('без чтения заметок (шлюз без readNotes) заметки рекламы не трогаются', () => {
    expect(adsNoteWrites(prepared([{ ...BASE, cpo_spend: 72.5 }]).plan)).toEqual([]);
  });
});

describe('Phase B — источник фактов и свежесть', () => {
  it('факты читают cpo_expense_rub операционного слоя отдельной колонкой', () => {
    const sql = ozonMonthFactsSql({ project: 'p', from: '2026-09-01', to: '2026-09-30', cpo: true });
    expect(sql).toContain('f.cpo_expense_rub cpo_spend');
    expect(sql).toContain('f.ad_spend_attributed_rub ads_spend');
    // флаг выключен — колонка не упоминается: новый образ работает и на вью до Phase B (порядок выката)
    const off = ozonMonthFactsSql({ project: 'p', from: '2026-09-01', to: '2026-09-30' });
    expect(off).not.toContain('cpo_expense_rub');
    expect(off).toContain('CAST(NULL AS NUMERIC) cpo_spend');
  });

  it('свежесть — последний ПОЛНЫЙ прогон загрузчика, только чтение журнала Ozon', () => {
    const sql = ozonCpoFreshnessSql('p');
    expect(sql).toContain('`p.ozon_raw.OZON_CPO_ORDER_RUNS`');
    expect(sql).toMatch(/record_type = 'RUN' AND status = 'COMPLETE'/);
    expect(sql).not.toMatch(/\b(INSERT|UPDATE|DELETE|MERGE)\b/);
  });

  it('контракт поля 11 называет обе части и их авторитет', () => {
    const f = OZON_FIELD_SOURCE_MAP.find((x) => x.offset === 11)!;
    expect(f.source).toMatch(/ad_spend_attributed_rub/);
    expect(f.source).toMatch(/cpo_expense_rub/);
  });
});

describe('Phase B — флаг OZON_UNITKA_CPO (стоп до записи боевого листа)', () => {
  const env = { GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', ENVIRONMENT: 'prod' } as NodeJS.ProcessEnv;
  it('по умолчанию выключен; включается только явным "1"', () => {
    expect(loadConfig(env).ozonUnitkaCpo).toBe(false);
    expect(loadConfig({ ...env, OZON_UNITKA_CPO: 'true' }).ozonUnitkaCpo).toBe(false);
    expect(loadConfig({ ...env, OZON_UNITKA_CPO: '1' }).ozonUnitkaCpo).toBe(true);
  });
  it('выключенный флаг обнуляет CPO фактов до сборки и не читает заметки — запись побайтно прежняя', () => {
    const src = readFileSync(new URL('../src/loaders/unitka/ozon/loader.ts', import.meta.url), 'utf8');
    expect(src).toMatch(/if \(!cpoOn\) for \(const f of facts\) f\.cpo_spend = null;/);
    expect(src).toMatch(/ozonMonthFactsSql\(\{ project: ctx\.config\.projectId, from: w\.from, to: w\.to, cpo: ctx\.config\.ozonUnitkaCpo \}\)/);
    // и выключенный режим совпадает с поведением до Phase B
    const before = prepared([BASE]), off = prepared([{ ...BASE, cpo_spend: null }]);
    expect(off.plan.values).toEqual(before.plan.values);
  });
});
