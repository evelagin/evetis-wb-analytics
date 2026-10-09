/**
 * Phase C — P&L магазина WB: снимок компонент SKU из листа, запись снимка, план вкладки.
 * Сетка синтетическая (геометрия — живой лист WB_Юнит_2025), nmID вымышлены.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { parseUnitkaComponents, STORE_PNL_FROM } from '../src/loaders/unitka/storepnl/parse.js';
import { snapshotAppendSql, snapshotRecord, storePnlRaiseError, type PnlRow } from '../src/loaders/unitka/storepnl/bq.js';
import { buildTabPlan, toNum, TAB_HEADER, TAB_NAME } from '../src/loaders/unitka/storepnl/tabplan.js';
import { unitkaStorePnlLoader, snapshotIdOf, type StorePnlDeps } from '../src/loaders/unitka/storepnl/index.js';
import { addDaysIso, isoToSerial, OFFSET, SUMMARY, type CellValue } from '../src/loaders/unitka/model.js';
import { LoaderError } from '../src/errors.js';
import { Logger } from '../src/logging.js';
import { LOADERS } from '../src/loaders/registry.js';
import type { LoaderContext } from '../src/loaders/types.js';

const REV = 30;
const NM_A = 111111111, NM_B = 222222222;
const A = 13, B = 37; // слоты 0 и 1

/** Ячейки блока одних суток: Q=3, S=1, цена 1000, комиссия 30 %, логистика 80, налог 20, COGS 300 → W = 435. */
function block(row: CellValue[], start: number, day: string, o: Partial<Record<keyof typeof OFFSET, CellValue>> = {}): void {
  const v: Partial<Record<keyof typeof OFFSET, CellValue>> = {
    date: isoToSerial(day), orders: 3, cancels: 1, profitAll: 435, adsIn: 50, adsOut: 0, price: 1000, commission: 0.3,
    priceMinusComm: 700, logistics: 80, storage: 5, tax: 20, unitProfit: 300, ...o,
  };
  for (const [k, x] of Object.entries(v)) row[start - 1 + OFFSET[k as keyof typeof OFFSET]] = x as CellValue;
}

/** Все сутки [from, to] — секция листа всегда содержит месяц целиком. */
function daysOf(from: string, to: string): string[] {
  const out: string[] = [];
  for (let d = from; d <= to; d = addDaysIso(d, 1)) out.push(d);
  return out;
}

/** Секция: заголовки, строки суток (дата сводки B, итог I = Σ W блоков, если tweak не задал иное). */
function grid(days: string[], tweak?: (r: CellValue[], day: string) => void): CellValue[][] {
  const g: CellValue[][] = [];
  const title: CellValue[] = []; title[A - 1] = `${NM_A} Крем`; title[B - 1] = `${NM_B} Сыворотка`;
  const head: CellValue[] = []; head[A - 1] = 'Дата';
  g.push(title, head);
  for (const d of days) {
    const r: CellValue[] = [];
    block(r, A, d); block(r, B, d, { orders: 0, cancels: 0, profitAll: 0, adsIn: 0, storage: 0 });
    r[SUMMARY.date - 1] = isoToSerial(d);
    tweak?.(r, d);
    if (r[SUMMARY.profit - 1] === undefined) r[SUMMARY.profit - 1] = Number(r[A - 1 + OFFSET.profitAll] ?? 0) + Number(r[B - 1 + OFFSET.profitAll] ?? 0);
    g.push(r);
  }
  g.push(['итого']);
  return g;
}

const SEP = daysOf('2026-09-01', '2026-09-30');
const AUG = daysOf('2026-08-01', '2026-08-31');

describe('Phase C — снимок компонент SKU из листа', () => {
  it('тождество строки: COGS выводится, model_gap = 0, пустые SKU-сутки и сутки после LCD не попадают', () => {
    const p = parseUnitkaComponents(grid([...SEP, '2026-10-01']), '2026-09-30', REV);
    expect(p.sections).toBe(1);
    expect(p.rows).toHaveLength(30);
    const r = p.rows[0]!;
    expect(r).toMatchObject({ date_msk: '2026-09-01', nm_id: NM_A, sheet_row: 3, block_col: A, orders: 3, cancels: 1,
      cogs_per_unit: 300, tax_per_unit: 20, reverse_leg_rate: REV, profit: 435 });
    expect(Math.abs(r.model_gap)).toBeLessThan(1e-9);
    expect(p.months['2026-09']).toMatchObject({ days: 30, rows: 30, orders: 90, profit: 13050, summaryProfit: 13050 });
    expect(p.months['2026-10']).toBeUndefined();
  });

  it('месяц до LCD — сутки с 1-го по LCD; месяцы раньше окна Phase C не читаются', () => {
    expect(STORE_PNL_FROM).toBe('2026-08-01');
    const g = [...grid(daysOf('2026-07-01', '2026-07-31')), ...grid(daysOf('2026-10-01', '2026-10-08'))];
    const p = parseUnitkaComponents(g, '2026-10-08', REV);
    expect(Object.keys(p.months)).toEqual(['2026-10']);
    expect(p.months['2026-10']!.days).toBe(8);
  });

  it('ненулевой разрыв модели сохраняется как есть (наследие старых формул не чинится)', () => {
    const p = parseUnitkaComponents(grid(AUG, (r) => { r[A - 1 + OFFSET.profitAll] = 400; }), '2026-08-31', REV);
    expect(p.rows[0]!.model_gap).toBeCloseTo(-35, 9);
  });

  it('ошибка формулы и текст в числовой ячейке — отказ', () => {
    for (const bad of ['#REF!', 'n/a']) {
      expect(() => parseUnitkaComponents(grid(SEP, (r) => { r[A - 1 + OFFSET.storage] = bad; }), '2026-09-30', REV))
        .toThrow(expect.objectContaining({ code: 'STORE_PNL_SHEET_ERROR' }));
    }
  });

  it('дата блока ≠ дата строки у суток с данными — отказ (сдвиг геометрии)', () => {
    // колонка M — одновременно дата строки и дата блока A, поэтому сдвиг проверяется на блоке B с данными
    expect(() => parseUnitkaComponents(grid(SEP, (r, d) => { if (d === '2026-09-01') block(r, B, '2026-08-31'); }), '2026-09-30', REV))
      .toThrow(/строка 3, блок 222222222: дата блока/);
  });

  it('пустой выбывший блок с датами прошлого месяца пропускается (живой лист, август 2026)', () => {
    const p = parseUnitkaComponents(grid(AUG, (r) => { r[B - 1] = isoToSerial('2026-07-01'); }), '2026-08-31', REV);
    expect(new Set(p.rows.map((x) => x.nm_id))).toEqual(new Set([NM_A]));
  });

  it('Σ W блоков ≠ итог листа «Доходность (общая)» — отказ (блок вне снимка)', () => {
    expect(() => parseUnitkaComponents(grid(SEP, (r, d) => { if (d === '2026-09-15') r[SUMMARY.profit - 1] = 500; }), '2026-09-30', REV))
      .toThrow(/Σ блоков 435.00 ≠ итог листа 500.00/);
  });

  it('дата сводки ≠ дата строки — отказ', () => {
    expect(() => parseUnitkaComponents(grid(SEP, (r, d) => { if (d === '2026-09-15') r[SUMMARY.date - 1] = isoToSerial('2026-09-14'); }), '2026-09-30', REV))
      .toThrow(/дата сводки/);
  });

  it('пропущенные сутки месяца — отказ', () => {
    expect(() => parseUnitkaComponents(grid(SEP.filter((d) => d !== '2026-09-10')), '2026-09-30', REV)).toThrow(/нет суток 2026-09-10/);
  });

  it('одни и те же сутки × nm в двух секциях — отказ', () => {
    expect(() => parseUnitkaComponents([...grid(SEP), ...grid(SEP)], '2026-09-30', REV)).toThrow(/дубль/);
  });

  it('нет секций, плохие LCD / REVERSE_LEG_RATE — отказ', () => {
    expect(() => parseUnitkaComponents([['x']], '2026-09-30', REV)).toThrow(/нет ни одной секции/);
    expect(() => parseUnitkaComponents(grid([]), '30.09.2026', REV)).toThrow(/LAST_CLOSED_DATE/);
    expect(() => parseUnitkaComponents(grid([]), '2026-09-30', 0)).toThrow(/REVERSE_LEG_RATE/);
  });
});

describe('Phase C — запись снимка', () => {
  it('транзакция: проверки stage и порядка снимков до COMMIT, ROLLBACK при любом отказе', () => {
    const sql = snapshotAppendSql('`p.wb_ops.T`', '`p.wb_ops.T__STAGE`');
    const commit = sql.indexOf('COMMIT TRANSACTION');
    for (const code of ['STORE_PNL_STAGE_ROWS', 'STORE_PNL_STAGE_FOREIGN_RUN', 'STORE_PNL_STAGE_KEY', 'STORE_PNL_STAGE_AMOUNT',
      'STORE_PNL_SNAPSHOT_ORDER', 'STORE_PNL_TARGET_MISMATCH']) {
      expect(sql.indexOf(code)).toBeGreaterThan(0);
      expect(sql.indexOf(code)).toBeLessThan(commit);
    }
    expect(sql).toMatch(/ROLLBACK TRANSACTION/);
    expect(sql).not.toMatch(/DELETE|UPDATE|MERGE|TRUNCATE/);
    expect(sql.indexOf('INSERT INTO `p.wb_ops.T`')).toBeGreaterThan(sql.indexOf('STORE_PNL_SNAPSHOT_ORDER'));
  });

  it('код RAISE сохраняется в LoaderError', () => {
    const e = storePnlRaiseError(new Error('Query error: STORE_PNL_SNAPSHOT_ORDER: 5 строк'));
    expect(e).toBeInstanceOf(LoaderError);
    expect((e as LoaderError).code).toBe('STORE_PNL_SNAPSHOT_ORDER');
  });

  it('строка снимка = схема таблицы Terraform, в том же порядке (INSERT … SELECT *)', () => {
    const tf = readFileSync(new URL('../../infra/terraform/unitka_store_pnl.tf', import.meta.url), 'utf8');
    const schema = /store_pnl_snapshot_schema\s*=\s*\[([\s\S]*?)\n\s*\]/.exec(tf)![1]!;
    const cols = [...schema.matchAll(/name\s*=\s*"([a-z_]+)"/g)].map((m) => m[1]);
    const p = parseUnitkaComponents(grid(SEP), '2026-09-30', REV);
    const rec = snapshotRecord(p.rows[0]!, { snapshotId: 's', runId: 'r', snapshotAt: '2026-10-09T07:00:00Z', lcd: '2026-09-30', imageDigest: 'd', gitSha: 'g' });
    expect(Object.keys(rec)).toEqual(cols);
  });

  it('snapshot_id сортируется как время', () => {
    expect(snapshotIdOf(new Date('2026-10-09T07:40:01.234Z'))).toBe('20261009T074001234Z');
    expect(snapshotIdOf(new Date('2026-10-09T07:40:01.235Z')) > snapshotIdOf(new Date('2026-10-09T07:40:01.234Z'))).toBe(true);
  });
});

function pnlRow(o: Partial<Record<string, unknown>> = {}): PnlRow {
  return {
    month: '2026-09', revenue_rub: 1000, sku_contribution_rub: 100, minimum_payment_rub: 0, utilization_rub: 0, penalty_rub: 0,
    transit_rub: 0, acceptance_rub: 0, other_marketplace_cost_rub: 0, marketplace_income_rub: 0, ads_adjustment_rub: 10,
    storage_adjustment_rub: 1, commission_adjustment_rub: 5, price_adjustment_rub: 2, mature_cohort_adjustment_rub: 0,
    timing_bridge_logistics_rub: 40, timing_bridge_unsettled_margin_rub: 300, management_net_store_profit_rub: 118,
    management_net_store_margin: 0.118, financial_state: 'PARTIAL_AWAITING_ACCOUNT_INVOICE', ...o,
  };
}

describe('Phase C — план вкладки «WB Магазин P&L»', () => {
  it('строка складывается, мост сроков — справочно и вне прибыли, состояние по-русски', () => {
    const p = buildTabPlan([pnlRow()]);
    expect(p.tab).toBe(TAB_NAME);
    expect(p.header).toEqual([...TAB_HEADER]);
    expect(p.range).toBe(`'${TAB_NAME}'!A1:Q2`);
    expect(p.rows[0]).toEqual(['2026-09', 1000, 100, 0, 0, 0, 0, 10, 1, 7, 0, 0, 0, 118, 0.118, 'Ждём счета WB', -260]);
    expect(p.rowIdentityMaxAbs).toBe(0);
    expect(p.fingerprint).toMatch(/^[0-9a-f]{64}$/);
  });

  it('расход кабинета уменьшает прибыль, доход увеличивает; приёмка — в прочих', () => {
    const p = buildTabPlan([pnlRow({ minimum_payment_rub: 50, acceptance_rub: 3, other_marketplace_cost_rub: 2, marketplace_income_rub: 4,
      management_net_store_profit_rub: 118 - 50 - 5 + 4 })]);
    expect(p.rows[0]![11]).toBe(5);
    expect(p.rowIdentityMaxAbs).toBe(0);
  });

  it('несходящаяся строка видна в rowIdentityMaxAbs; неизвестное состояние — отказ', () => {
    expect(buildTabPlan([pnlRow({ management_net_store_profit_rub: 200 })]).rowIdentityMaxAbs).toBe(82);
    expect(() => buildTabPlan([pnlRow({ financial_state: 'OK' })])).toThrow(/неизвестное состояние/);
  });

  it('NUMERIC BigQuery (Big) и DATE-объекты приводятся явно', () => {
    expect(toNum({ toString: () => '12.34' })).toBe(12.34);
    expect(toNum({ value: '5' })).toBe(5);
    expect(toNum(null)).toBe(0);
    expect(() => toNum('abc')).toThrow();
  });

  it('отпечаток меняется при любом изменении значения', () => {
    expect(buildTabPlan([pnlRow()]).fingerprint).not.toBe(buildTabPlan([pnlRow({ revenue_rub: 1001 })]).fingerprint);
  });
});

describe('Phase C — загрузчик unitka-store-pnl', () => {
  const ctx = (c: Partial<LoaderContext['config']> = {}): LoaderContext => ({
    config: { environment: 'prod', rawDataset: 'wb_raw', unitkaOpsDataset: 'wb_ops', unitkaMartDataset: 'wb_mart',
      unitkaSheetName: 'WB_Юнит_2025', unitkaSpreadsheetId: 's', projectId: 'p', bqLocation: 'EU', imageDigest: 'd', gitSha: 'g',
      executionId: 'e', ...c } as LoaderContext['config'],
    logger: new Logger({}, 'error'), logicalPeriod: '2026-10-09T10', runId: 'run1', targetDate: '2026-10-09T10',
  });
  function deps(pnl: PnlRow[] = [pnlRow()]) {
    const calls: { appended: number; snapshotId?: string; lcd?: string; ranges?: string[]; metaAnchor?: boolean } = { appended: 0 };
    const d: StorePnlDeps = {
      now: () => new Date('2026-10-09T07:40:00Z'),
      makeSheets: () => ({
        readSheetMeta: async (_n: string, requireAnchor?: boolean) => { calls.metaAnchor = requireAnchor; return { sheetId: 1, rowCount: 10, columnCount: 60, anchorCol: 0, namedRanges: {} }; },
        readValues: async (ranges: string[]) => { calls.ranges = ranges; return [grid(SEP), [[isoToSerial('2026-09-30')]], [[REV]]]; },
      }),
      makeBq: () => ({
        appendSnapshot: async (rows, m) => { calls.appended = rows.length; calls.snapshotId = m.snapshotId; calls.lcd = m.lcd; },
        readPnl: async () => pnl,
        readNewOperations: async () => [],
      }),
    };
    return { d, calls };
  }

  it('лист читается без требования якоря, снимок пишется, план строится; записи в лист нет', async () => {
    const { d, calls } = deps();
    const r = await unitkaStorePnlLoader(ctx(), d);
    expect(r).toEqual({ rowsFetched: 30, rowsLoaded: 30 });
    expect(calls).toMatchObject({ appended: 30, snapshotId: '20261009T074000000Z', lcd: '2026-09-30', metaAnchor: false });
    expect(calls.ranges).toEqual(["'WB_Юнит_2025'!A1:BH10", 'LAST_CLOSED_DATE', 'REVERSE_LEG_RATE']);
  });

  it('только prod и только домен WB', async () => {
    await expect(unitkaStorePnlLoader(ctx({ environment: 'shadow' }), deps().d)).rejects.toMatchObject({ code: 'STORE_PNL_PROD_ONLY' });
    await expect(unitkaStorePnlLoader(ctx({ rawDataset: 'ozon_raw' }), deps().d)).rejects.toMatchObject({ code: 'STORE_PNL_CONFIG' });
    await expect(unitkaStorePnlLoader(ctx({ unitkaOpsDataset: 'evetis_ops' }), deps().d)).rejects.toMatchObject({ code: 'STORE_PNL_CONFIG' });
  });

  it('несходящаяся строка P&L — отказ STORE_PNL_IDENTITY (снимок уже лёг, он верен)', async () => {
    await expect(unitkaStorePnlLoader(ctx(), deps([pnlRow({ management_net_store_profit_rub: 500 })]).d))
      .rejects.toMatchObject({ code: 'STORE_PNL_IDENTITY' });
  });

  it('реестр: prodOnly, повтор транзиентного отказа', () => {
    expect(LOADERS['unitka-store-pnl']).toMatchObject({ prodOnly: true, retryTransient: true });
  });
});
