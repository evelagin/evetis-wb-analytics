/**
 * Phase C — P&L магазина WB: снимок компонент SKU из листа, запись снимка, план вкладки.
 * Сетка синтетическая (геометрия — живой лист WB_Юнит_2025), nmID вымышлены.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { parseUnitkaComponents, STORE_PNL_FROM } from '../src/loaders/unitka/storepnl/parse.js';
import { snapshotAppendSql, snapshotRecord, storePnlRaiseError, type PnlRow } from '../src/loaders/unitka/storepnl/bq.js';
import { buildTabPlan, toNum, TAB_HEADER, TAB_NAME } from '../src/loaders/unitka/storepnl/tabplan.js';
import { unitkaStorePnlLoader, snapshotIdOf, activeNewOperations, publishEnv, type StorePnlDeps } from '../src/loaders/unitka/storepnl/index.js';
import { decidePublish, decideContract, contractSha, publishRequests, publishGate, metaOnly, contentSha, assertOnlyTab, assertOwnTab, normalizeGrid, PUBLISH_META_KEY, CONTRACT_META_KEY, SHEET_PROPERTY_FIELDS, STATE_RULES } from '../src/loaders/unitka/storepnl/publish.js';
import { TAB_NOTES, TAB_COLS, COL, FIRST_DATA_ROW, HEADER_ROW, KPI, KPI_ROWS, SUM_PARTS, DETAIL_GROUPS } from '../src/loaders/unitka/storepnl/tabplan.js';
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
const MAY = daysOf('2026-05-01', '2026-05-31');
const JUN = daysOf('2026-06-01', '2026-06-30');
const JUL = daysOf('2026-07-01', '2026-07-31');

describe('Phase C — снимок компонент SKU из листа', () => {
  it('тождество строки: COGS выводится, model_gap = 0, пустые SKU-сутки и сутки после LCD не попадают', () => {
    const p = parseUnitkaComponents(grid([...SEP, '2026-10-01']), '2026-09-30', REV, '2026-09-01');
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
    expect(STORE_PNL_FROM).toBe('2026-05-01');
    const g = [...grid(daysOf('2026-07-01', '2026-07-31')), ...grid(AUG), ...grid(SEP), ...grid(daysOf('2026-10-01', '2026-10-08'))];
    const p = parseUnitkaComponents(g, '2026-10-08', REV, '2026-08-01');
    expect(Object.keys(p.months).sort()).toEqual(['2026-08', '2026-09', '2026-10']);
    expect(p.months['2026-10']!.days).toBe(8);
  });

  it('ненулевой разрыв модели сохраняется как есть (наследие старых формул не чинится)', () => {
    const p = parseUnitkaComponents(grid(AUG, (r) => { r[A - 1 + OFFSET.profitAll] = 400; }), '2026-08-31', REV, '2026-08-01');
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
    const p = parseUnitkaComponents(grid(AUG, (r) => { r[B - 1] = isoToSerial('2026-07-01'); }), '2026-08-31', REV, '2026-08-01');
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

  it('месяц окна, которого нет в листе, — отказ (секция выпала бы молча)', () => {
    expect(() => parseUnitkaComponents(grid(SEP), '2026-10-08', REV, '2026-09-01')).toThrow(/2026-10: месяца нет в листе/);
    expect(() => parseUnitkaComponents(grid(SEP), '2026-09-30', REV)).toThrow(/2026-05: месяца нет в листе/);
  });

  it('пропущенные сутки месяца — отказ', () => {
    expect(() => parseUnitkaComponents(grid(SEP.filter((d) => d !== '2026-09-10')), '2026-09-30', REV, '2026-09-01')).toThrow(/нет суток 2026-09-10/);
  });

  it('одни и те же сутки × nm в двух секциях — отказ', () => {
    expect(() => parseUnitkaComponents([...grid(SEP), ...grid(SEP)], '2026-09-30', REV, '2026-09-01')).toThrow(/дубль/);
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
    const p = parseUnitkaComponents(grid(SEP), '2026-09-30', REV, '2026-09-01');
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
    timing_bridge_logistics_rub: 40, timing_bridge_unsettled_margin_rub: 300, management_net_store_profit_rub: 118, management_tax_rub: 20,
    management_net_store_margin: 0.118, financial_state: 'PARTIAL_AWAITING_ACCOUNT_INVOICE', double_count_residual_rub: 0,
    inventory_loss_writeoff_rub: 0, wb_loss_compensation_rub: 0, force_majeure_net_effect_rub: 0,
    inventory_loss_evidence_rows: 131, inventory_loss_evidence_runs: 1,
    service_month: `${String(o.month ?? '2026-09')}-01`,
    authority: `${String(o.month ?? '2026-09')}-01` < '2026-08-01' ? 'HISTORICAL_CANONICAL' : 'CURRENT_PHASE_C', ...o,
  };
}

describe('UX — шапка, группы, итоги по годам (2026-10-16)', () => {
  const MAYR = pnlRow({ month: '2026-05', financial_state: 'FINANCIAL_COMPLETE', sku_month_closed: true, month_finance_final: true, revenue_rub: 2000,
    management_net_store_profit_rub: 118, management_net_store_margin: 0.059 });
  const DEC = pnlRow({ month: '2025-12', revenue_rub: 500, management_net_store_margin: 0.236 });
  it('месяцы по годам и «Итого» года: суммы показанных ячеек, маржа = Σ прибыль ÷ Σ выручка', () => {
    const p = buildTabPlan([DEC, MAYR, pnlRow()]);
    expect(p.kinds.slice(FIRST_DATA_ROW)).toEqual(['month', 'total', 'month', 'month', 'total']);
    const t26 = p.grid[p.kinds.lastIndexOf('total')]!;
    expect(t26[COL.MONTH]).toBe('Итого 2026 · май–сен');
    for (const c of [COL.REVENUE, COL.SKU, COL.ADJ_SUM, COL.NET, COL.NET_EX_FM]) expect(t26[c]).toBe((p.rows[1]![c] as number) + (p.rows[2]![c] as number));
    expect(t26[COL.MARGIN]).toBeCloseTo(236 / 3000, 6);
    expect(t26[COL.STATE]).toBe('закрыт — 1 · ждём счета WB — 1');
    expect(p.yearGroups).toEqual([{ year: '2025', start: FIRST_DATA_ROW, end: FIRST_DATA_ROW + 1 }, { year: '2026', start: FIRST_DATA_ROW + 2, end: FIRST_DATA_ROW + 4 }]);
    for (const r of p.rows) for (const [sum, parts] of Object.entries(SUM_PARTS)) expect(r[Number(sum)]).toBeCloseTo(parts.reduce((x, j) => x + (r[j] as number), 0), 6);
  });
  it('показатели — над своими колонками и равны «Итого» года; основные колонки не свёрнуты', () => {
    const p = buildTabPlan([DEC, MAYR, pnlRow()]);
    const t26 = p.grid[p.kinds.lastIndexOf('total')]!;
    for (const k of KPI) expect(p.grid[KPI_ROWS[0]]![k.col]).toBe(t26[k.col]);
    expect(p.grid[KPI_ROWS[0]]![0]).toBe('2026 · май–сен');
    expect(p.grid[KPI_ROWS[1]]![0]).toBe('2025 · дек');
    for (const k of KPI) expect(DETAIL_GROUPS.some(([a, b]) => k.col >= a && k.col < b)).toBe(false);
  });
  it('«Итого» раскрывает справочный месяц наследия; неполный месяц без причины — явная пометка; суммы с запятой', () => {
    const AUG = pnlRow({ month: '2026-08', financial_state: 'LEGACY_PARTIAL_KNOWN_DEFECTS', management_net_store_profit_rub: 118 });
    const p = buildTabPlan([AUG, pnlRow({ unknown_rub: 1234.5, month_finance_final: true })]);
    const t = p.grid[p.kinds.lastIndexOf('total')]!;
    expect(String(t[COL.REASON])).toMatch(/Включает справочный месяц наследия: Август 2026 \(прибыль 118,00 ₽\)/);
    expect(String(p.rows[1]![COL.REASON])).toMatch(/неизвестные расходы: 1 234,50 ₽/);
    const bare = buildTabPlan([pnlRow({ month_finance_final: true, sku_month_closed: true, account_invoice_window_closed: true, financial_state: 'UNKNOWN_COST_PRESENT' })]);
    expect(bare.rows[0]![COL.REASON]).toBe('причина неполноты во вью не раскрыта');
  });
  it('три результата (решение владельца 16.10): A — полный, B — без чрезвычайных, C — без месяца наследия; без двойного учёта', () => {
    const JULR = pnlRow({ month: '2026-07', financial_state: 'FINANCIAL_COMPLETE', inventory_loss_writeoff_rub: 300, wb_loss_compensation_rub: 100,
      force_majeure_net_effect_rub: -200, management_net_store_profit_rub: 118 - 200, management_net_store_margin: -0.082 });
    const AUG = pnlRow({ month: '2026-08', financial_state: 'LEGACY_PARTIAL_KNOWN_DEFECTS', inventory_loss_writeoff_rub: 500,
      force_majeure_net_effect_rub: -500, management_net_store_profit_rub: 118 - 500 });
    const p = buildTabPlan([JULR, AUG, pnlRow()]);
    expect(p.rowIdentityMaxAbs).toBe(0);
    expect(p.rows[1]![COL.NET_COMPARABLE]).toBe('');                       // наследие — пусто, не 0
    expect(p.rows[0]![COL.NET_COMPARABLE]).toBe(p.rows[0]![COL.NET]);      // чрезвычайные июля в C включены
    const t = p.grid[p.kinds.lastIndexOf('total')]!;
    expect(t[COL.NET]).toBe(-82 - 382 + 118);                               // A: все месяцы
    expect(t[COL.NET_EX_FM]).toBe(118 * 3);                                 // B: без чистого эффекта (−200 −500 вернули)
    expect(t[COL.NET_COMPARABLE]).toBe(-82 + 118);                          // C: без августа-наследия
    expect(t[COL.NET]).toBe((t[COL.NET_EX_FM] as number) + (t[COL.FM_NET] as number)); // A = B + чистый эффект: эффект учтён один раз
    expect(p.grid[KPI_ROWS[0]]![COL.NET_COMPARABLE]).toBe(t[COL.NET_COMPARABLE]);
    expect(String(t[COL.REASON])).toMatch(/прибыль −382,00 ₽/);
  });
  it('правила состояния: красные раньше жёлтого и зелёного', () => {
    const first = (text: string): string | undefined => STATE_RULES.find((r) => text.toLowerCase().includes(r.contains.toLowerCase()))?.bg;
    expect(first('закрыт — 3 · неразобранные операции — 1')).toBe('F4CCCC');
    expect(first('Ждём счета WB')).toBe('FFF2CC');
    expect(first('Закрыт, когорта дозревает')).toBe('D9EAD3');
    expect(first('Справочно: наследие, известные дефекты')).toBe('EFEFEF');
  });
  it('NOOP после перераскладки: сетка равна плану (normalizeGrid)', () => {
    const p = buildTabPlan([pnlRow()]);
    expect(decidePublish({ current: normalizeGrid(p.grid), publishedSha: contentSha(normalizeGrid(p.grid)), plan: p })).toMatchObject({ action: 'NOOP' });
  });
});

describe('Phase C — план вкладки «WB Магазин P&L»', () => {
  it('строка складывается, мост сроков — справочно и вне прибыли, состояние по-русски', () => {
    const p = buildTabPlan([pnlRow()]);
    expect(p.tab).toBe(TAB_NAME);
    expect(p.header).toEqual([...TAB_HEADER]);
    expect(p.range).toBe(`'${TAB_NAME}'!A1:Z${FIRST_DATA_ROW + 2}`);
    const r = p.rows[0]!;
    const at = (h: string): unknown => r[TAB_HEADER.indexOf(h)];
    expect(r[COL.MONTH]).toBe('Сентябрь 2026');
    expect([r[COL.REVENUE], r[COL.SKU], r[COL.TAX], r[COL.ADJ_SUM], r[COL.CABINET_SUM], r[COL.INCOME]]).toEqual([1000, 100, 20, 18, 0, 0]);
    expect([at('Поправка рекламы'), at('Поправка хранения'), at('Поправка комиссии и цены'), at('Поправка зрелой когорты')]).toEqual([10, 1, 7, 0]);
    expect([r[COL.LOSS], r[COL.LOSS_COMP], r[COL.FM_NET], r[COL.NET], r[COL.MARGIN], r[COL.NET_EX_FM]]).toEqual([0, 0, 0, 118, 0.118, 118]);
    expect([r[COL.STATE], r[COL.BRIDGE]]).toEqual(['Ждём счета WB', -260]);
    expect(r[COL.REASON]).toMatch(/финотчёт месяца ещё не финальный/);
    // строка складывается из показанных колонок: вклад + поправки − кабинет + доходы − списание + компенсация
    expect((r[COL.SKU] as number) + (r[COL.ADJ_SUM] as number) - (r[COL.CABINET_SUM] as number) + (r[COL.INCOME] as number)
      - (r[COL.LOSS] as number) + (r[COL.LOSS_COMP] as number)).toBe(118);
    expect(p.rowIdentityMaxAbs).toBe(0);
    expect(p.fingerprint).toMatch(/^[0-9a-f]{64}$/);
  });

  it('расход кабинета уменьшает прибыль, доход увеличивает; приёмка — в прочих', () => {
    const p = buildTabPlan([pnlRow({ minimum_payment_rub: 50, acceptance_rub: 3, other_marketplace_cost_rub: 2, marketplace_income_rub: 4,
      management_net_store_profit_rub: 118 - 50 - 5 + 4 })]);
    expect(p.rows[0]![TAB_HEADER.indexOf('Прочие расходы WB')]).toBe(5);
    expect(p.rows[0]![COL.CABINET_SUM]).toBe(55);
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

function deps(pnl: PnlRow[] = [pnlRow()]) {
  const calls: { appended: number; snapshotId?: string; lcd?: string; ranges?: string[]; metaAnchor?: boolean; readonly?: boolean; writes: string[]; requests: unknown[] } = { appended: 0, writes: [], requests: [] };
  const d: StorePnlDeps = {
    now: () => new Date('2026-10-09T07:40:00Z'),
    makeSheets: (_c, readonly) => ({
      readSheetMeta: async (_n: string, requireAnchor?: boolean) => { calls.metaAnchor = requireAnchor; calls.readonly = readonly; return { sheetId: 1, rowCount: 10, columnCount: 60, anchorCol: 0, namedRanges: {} }; },
      readValues: async (ranges: string[]) => {
        if (ranges[0]!.startsWith(`'${TAB_NAME}'`)) return [tabState.values];
        calls.ranges = ranges; return [[...grid(MAY), ...grid(JUN), ...grid(JUL), ...grid(AUG), ...grid(SEP)], [[isoToSerial('2026-09-30')]], [[REV]]];
      },
      readSheetMetadata: async (id: number) => (id === TAB_ID ? { title: tabState.title, rowCount: 20, columnCount: tabState.cols, metadata: tabState.meta } : null),
      readNotes: async (_n: string, cells: ReadonlyArray<{ row: number; col: number }>) => {
        // как Sheets API: ячейка за пределами сетки — ошибка всего запроса
        if (cells.some((c) => c.col > tabState.cols)) throw new Error('exceeds grid limits');
        return new Map(cells.filter((c) => tabState.notes[c.col - 1]).map((c) => [`${c.row}|${c.col}`, tabState.notes[c.col - 1]!] as [string, string]));
      },
      readSheetsList: async () => [{ sheetId: 1, title: 'WB_Юнит_2025', index: 0, rowCount: 800, columnCount: 600 + tabState.otherTamper },
        { sheetId: TAB_ID, title: tabState.title, index: 2, rowCount: 20, columnCount: tabState.cols }],
      readSheetStructure: async () => ({ conditionalFormats: Array.from({ length: tabState.cf }, () => ({ ranges: [] })), merges: Array.from({ length: tabState.merges }, () => ({})),
        columnGroups: tabState.colGroups.map((x) => ({ ...x })), rowGroups: tabState.rowGroups.map((x) => ({ ...x })), rowMetadata: [], columnMetadata: [] }),
      structureWrite: async (reqs) => { applyRequests(reqs); calls.requests.push(...reqs); calls.writes.push(...reqs.filter((r) => 'updateCells' in r).map(() => 'updateCells')); return reqs.length; },
    }),
    makeBq: () => ({
      appendSnapshot: async (rows, m) => { calls.appended = rows.length; calls.snapshotId = m.snapshotId; calls.lcd = m.lcd; },
      readPnl: async () => pnl,
      readNewOperations: async () => [],
    }),
  };
  return { d, calls };
}

describe('Phase C — загрузчик unitka-store-pnl', () => {
  const ctx = (c: Partial<LoaderContext['config']> = {}): LoaderContext => ({
    config: { environment: 'prod', rawDataset: 'wb_raw', unitkaOpsDataset: 'wb_ops', unitkaMartDataset: 'wb_mart',
      unitkaSheetName: 'WB_Юнит_2025', unitkaSpreadsheetId: 's', projectId: 'p', bqLocation: 'EU', imageDigest: 'd', gitSha: 'g',
      executionId: 'e', ...c } as LoaderContext['config'],
    logger: new Logger({}, 'error'), logicalPeriod: '2026-10-09T10', runId: 'run1', targetDate: '2026-10-09T10',
  });

  it('лист читается без требования якоря, снимок пишется, план строится; записи в лист нет', async () => {
    const { d, calls } = deps();
    const r = await unitkaStorePnlLoader(ctx(), d);
    expect(r).toEqual({ rowsFetched: 153, rowsLoaded: 153 });
    expect(calls).toMatchObject({ appended: 153, snapshotId: '20261009T074000000Z', lcd: '2026-09-30', metaAnchor: false });
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

describe('Phase C — наследие августа', () => {
  it('август виден только справочно и никогда не «закрыт»', () => {
    const p = buildTabPlan([pnlRow({ month: '2026-08', financial_state: 'LEGACY_PARTIAL_KNOWN_DEFECTS' })]);
    expect(p.rows[0]![COL.STATE]).toBe('Справочно: наследие, известные дефекты');
  });
});

const TAB_ID = 989153123;
const tabState: { title: string; values: CellValue[][]; meta: Array<{ id: number; key: string; value: string }>; notes: string[]; cols: number;
  otherTamper: number; cf: number; merges: number; colGroups: Array<{ startIndex: number; endIndex: number; depth: number }>; rowGroups: Array<{ startIndex: number; endIndex: number; depth: number }> } =
  { title: TAB_NAME, values: [], meta: [], notes: [], cols: 26, otherTamper: 0, cf: 0, merges: 0, colGroups: [], rowGroups: [] };
let nextMetaId = 7;

/** Фейковый Sheets: применяет updateCells и metadata к состоянию вкладки так, как это сделал бы API. */
let tamperOnWrite = false;
function applyRequests(reqs: Record<string, unknown>[]): void {
  if (tamperOnWrite) tabState.otherTamper++;
  for (const r of reqs) {
    const u = (r as { updateCells?: { fields: string; rows: Array<{ values: Array<{ note?: string; userEnteredValue?: { numberValue?: number; stringValue?: string } }> }> } }).updateCells;
    if (u && u.fields === 'note') { if ((u as unknown as { range: { startRowIndex: number } }).range.startRowIndex === HEADER_ROW) tabState.notes = u.rows[0]!.values.map((c) => c.note ?? ''); else tabState.notes = []; }
    else if (u) {
      const next = u.rows.map((row) => row.values.map((c) => (c.userEnteredValue?.numberValue ?? c.userEnteredValue?.stringValue ?? '') as CellValue));
      for (let k = 0; k < next.length; k++) tabState.values[k] = next[k]!;
    }
    const kind = Object.keys(r)[0]!;
    const g = (x: unknown): { dimension: string; startIndex: number; endIndex: number } => (x as { range: { dimension: string; startIndex: number; endIndex: number } }).range;
    if (kind === 'addConditionalFormatRule') tabState.cf++;
    if (kind === 'deleteConditionalFormatRule') { if (tabState.cf === 0) throw new Error('No conditional format at index 0'); tabState.cf--; }
    if (kind === 'mergeCells') tabState.merges++;
    if (kind === 'unmergeCells') tabState.merges = 0;
    if (kind === 'addDimensionGroup' || kind === 'deleteDimensionGroup') {
      const x = g((r as Record<string, unknown>)[kind]); const list = x.dimension === 'ROWS' ? tabState.rowGroups : tabState.colGroups;
      const i = list.findIndex((y) => y.startIndex === x.startIndex && y.endIndex === x.endIndex);
      if (kind === 'addDimensionGroup') { if (i >= 0) throw new Error('группа уже есть — глубина выросла бы'); list.push({ startIndex: x.startIndex, endIndex: x.endIndex, depth: 1 }); }
      else { if (i < 0) throw new Error('No group to delete'); list.splice(i, 1); }
    }
    const ad = (r as { appendDimension?: { dimension: string; length: number } }).appendDimension;
    if (ad && ad.dimension === 'COLUMNS') tabState.cols += ad.length;
    const c = (r as { createDeveloperMetadata?: { developerMetadata: { metadataKey: string; metadataValue: string } } }).createDeveloperMetadata;
    if (c) tabState.meta.push({ id: nextMetaId++, key: c.developerMetadata.metadataKey, value: c.developerMetadata.metadataValue });
    const up = (r as { updateDeveloperMetadata?: { dataFilters: Array<{ developerMetadataLookup: { metadataId: number } }>; developerMetadata: { metadataValue: string } } }).updateDeveloperMetadata;
    if (up) {
      const m = tabState.meta.find((x) => x.id === up.dataFilters[0]!.developerMetadataLookup.metadataId)!;
      m.value = up.developerMetadata.metadataValue;
    }
  }
}

describe('WB STORE P&L AUTO-PUBLISH — решение, гейт и запросы', () => {
  const AUGR = pnlRow({ month: '2026-08', financial_state: 'LEGACY_PARTIAL_KNOWN_DEFECTS' });
  const plan = buildTabPlan([AUGR, pnlRow()]);
  const want = plan.grid.map((r) => [...r]);
  const old = buildTabPlan([AUGR, pnlRow({ revenue_rub: 999 })]);
  const oldGrid = old.grid.map((r) => [...r]);
  const shaOf = (g: CellValue[][]): string => contentSha(normalizeGrid(g));

  it('NOOP идемпотентно; отпечаток дописывается, если его нет', () => {
    expect(decidePublish({ current: want, plan })).toMatchObject({ action: 'NOOP', setMeta: true });
    expect(decidePublish({ current: want, publishedSha: shaOf(want), plan })).toMatchObject({ action: 'NOOP', setMeta: false });
  });

  it('вкладка = последней публикации → WRITE; ручная правка значения → отказ', () => {
    expect(decidePublish({ current: oldGrid, publishedSha: shaOf(oldGrid), plan })).toMatchObject({ action: 'WRITE', clearRows: 0, adopted: false });
    const edited = oldGrid.map((r) => [...r]); edited[FIRST_DATA_ROW]![1] = 12345;
    expect(decidePublish({ current: edited, publishedSha: shaOf(oldGrid), plan })).toMatchObject({ action: 'REFUSE', code: 'STORE_PNL_TAB_EDITED' });
  });

  it('ADOPT: первое подключение и восстановление только для ровно проверенного содержимого', () => {
    expect(decidePublish({ current: oldGrid, plan })).toMatchObject({ action: 'REFUSE', code: 'STORE_PNL_TAB_UNTRACKED' });
    expect(decidePublish({ current: oldGrid, adoptSha: shaOf(oldGrid), plan })).toMatchObject({ action: 'WRITE', adopted: true });
    expect(decidePublish({ current: oldGrid, adoptSha: 'f'.repeat(64), plan })).toMatchObject({ action: 'REFUSE', code: 'STORE_PNL_TAB_EDITED' });
    expect(decidePublish({ current: oldGrid, publishedSha: 'a'.repeat(64), adoptSha: shaOf(oldGrid), plan })).toMatchObject({ action: 'WRITE', adopted: true });
    expect(decidePublish({ current: oldGrid, publishedSha: 'a'.repeat(64), adoptSha: 'b'.repeat(64), plan })).toMatchObject({ action: 'REFUSE', code: 'STORE_PNL_TAB_EDITED' });
  });

  it('один атомарный batchUpdate: значения, раскладка, заметки строки 7, отпечатки; всё в sheetId вкладки', () => {
    const d = decidePublish({ current: oldGrid, publishedSha: shaOf(oldGrid), plan });
    if (d.action !== 'WRITE') throw new Error('ожидалась запись');
    const reqs = publishRequests(plan, TAB_ID, 20, d, 7);
    const kinds = reqs.map((r) => Object.keys(r)[0]);
    expect(kinds.slice(0, 3)).toEqual(['repeatCell', 'updateCells', 'updateCells']); // сброс форматов, снятие заметок, значения
    expect(kinds).toContain('updateSheetProperties');
    expect(kinds.filter((k) => k === 'addConditionalFormatRule').length).toBe(4 + STATE_RULES.length);
    expect(kinds.filter((k) => k === 'addDimensionGroup')).toHaveLength(DETAIL_GROUPS.length + plan.yearGroups.length);
    const ids = JSON.stringify(reqs).match(/"sheetId":\d+/g) ?? [];
    expect(new Set(ids)).toEqual(new Set([`"sheetId":${TAB_ID}`]));
    const notes = reqs.filter((r) => (r as { updateCells?: { fields: string } }).updateCells?.fields === 'note').at(-1) as { updateCells: { range: { startRowIndex: number } } };
    expect(notes.updateCells.range.startRowIndex).toBe(HEADER_ROW);
    expect(JSON.stringify(reqs.at(-2))).toMatch(/"metadataLocation":\{"sheetId":989153123\}/);
    expect(JSON.stringify(reqs.at(-1))).toContain(CONTRACT_META_KEY);
  });

  it('сетка короче плана → appendDimension той же вкладки; лишние строки чистятся', () => {
    const longer = [...oldGrid, ...Array.from({ length: 3 }, () => ['хвост', 1])];
    const d = decidePublish({ current: longer, publishedSha: shaOf(longer), plan });
    if (d.action !== 'WRITE') throw new Error('ожидалась запись');
    const reqs = publishRequests(plan, TAB_ID, 2, d);
    const rows = plan.grid.length + d.clearRows;
    expect(d.clearRows).toBe(longer.length - plan.grid.length);
    expect(reqs[0]).toEqual({ appendDimension: { sheetId: TAB_ID, dimension: 'ROWS', length: rows + 10 - 2 } });
    const uc = reqs.find((r) => (r as { updateCells?: { fields: string } }).updateCells?.fields === 'userEnteredValue') as { updateCells: { range: Record<string, number>; rows: unknown[] } };
    expect(uc.updateCells.range).toEqual({ sheetId: TAB_ID, startRowIndex: 0, endRowIndex: rows, startColumnIndex: 0, endColumnIndex: TAB_COLS });
    expect(uc.updateCells.rows).toHaveLength(rows);
  });

  it('в сетке меньше 26 колонок (живая A:U = 21) → appendDimension COLUMNS той же вкладки в том же batchUpdate', () => {
    const d = decidePublish({ current: oldGrid, publishedSha: shaOf(oldGrid), plan });
    if (d.action !== 'WRITE') throw new Error('ожидалась запись');
    const reqs = publishRequests(plan, TAB_ID, 20, d, 7, undefined, 21);
    expect(reqs[0]).toEqual({ appendDimension: { sheetId: TAB_ID, dimension: 'COLUMNS', length: TAB_COLS - 21 } });
    expect(publishRequests(plan, TAB_ID, 20, d, 7, undefined, TAB_COLS).some((r) => 'appendDimension' in r)).toBe(false);
  });

  it('повторная запись снимает прежнюю раскладку: УФ, объединения и группы удаляются до новой', () => {
    const d = decidePublish({ current: oldGrid, publishedSha: shaOf(oldGrid), plan });
    if (d.action !== 'WRITE') throw new Error('ожидалась запись');
    const reqs = publishRequests(plan, TAB_ID, 20, d, 7, undefined, TAB_COLS,
      { conditionalFormats: 3, merges: true, columnGroups: [{ startIndex: 3, endIndex: 4, depth: 1 }], rowGroups: [{ startIndex: 7, endIndex: 9, depth: 1 }] });
    const kinds = reqs.map((r) => Object.keys(r)[0]);
    expect(kinds.filter((k) => k === 'deleteConditionalFormatRule')).toHaveLength(3);
    expect(kinds.filter((k) => k === 'deleteDimensionGroup')).toHaveLength(2);
    expect(kinds.indexOf('unmergeCells')).toBeLessThan(kinds.indexOf('mergeCells'));
    expect(kinds.lastIndexOf('deleteDimensionGroup')).toBeLessThan(kinds.indexOf('addDimensionGroup'));
    expect(publishRequests(plan, TAB_ID, 20, d, 7).map((r) => Object.keys(r)[0])).not.toContain('unmergeCells');
  });

  it('assertOwnTab: раскладка только своей вкладки; название, индекс и размеры сетки не меняются', () => {
    expect(() => assertOwnTab(TAB_ID, [{ deleteSheet: { sheetId: TAB_ID } }])).toThrow(/вне вкладки/);
    expect(() => assertOwnTab(TAB_ID, [{ updateSheetProperties: { properties: { sheetId: TAB_ID, title: 'x' }, fields: 'title' } }])).toThrow(/вне вкладки/);
    expect(() => assertOwnTab(TAB_ID, [{ updateSheetProperties: { properties: { sheetId: TAB_ID, gridProperties: { rowCount: 1 } }, fields: 'gridProperties(rowCount)' } }])).toThrow(/вне вкладки/);
    expect(() => assertOwnTab(TAB_ID, [{ updateSheetProperties: { properties: { sheetId: TAB_ID, index: 0, gridProperties: {} }, fields: SHEET_PROPERTY_FIELDS } }])).toThrow(/вне вкладки/);
    expect(() => assertOwnTab(TAB_ID, [{ mergeCells: { range: { sheetId: 1 } } }])).toThrow(/вне вкладки/);
    expect(() => assertOwnTab(TAB_ID, [{ addConditionalFormatRule: { rule: { ranges: [{ sheetId: TAB_ID }, { sheetId: 1 }] } } }])).toThrow(/вне вкладки/);
    expect(() => assertOwnTab(TAB_ID, [{ deleteConditionalFormatRule: { sheetId: 1, index: 0 } }])).toThrow(/вне вкладки/);
    expect(() => assertOwnTab(TAB_ID, [{ updateDimensionGroup: { dimensionGroup: { range: { sheetId: 1 } } } }])).toThrow(/вне вкладки/);
  });

  it('assertOnlyTab: чужой sheetId, чужой тип запроса, чужой metadataId — отказ', () => {
    expect(() => assertOnlyTab(TAB_ID, [{ repeatCell: { range: { sheetId: 0 } } }])).toThrow(/вне вкладки/);
    expect(() => assertOnlyTab(TAB_ID, [{ deleteSheet: { sheetId: TAB_ID } }])).toThrow(/вне вкладки/);
    expect(() => assertOnlyTab(TAB_ID, [{ updateCells: { range: { sheetId: TAB_ID } }, repeatCell: {} }])).toThrow(/вне вкладки/);
    expect(() => assertOnlyTab(TAB_ID, [{ updateDeveloperMetadata: { dataFilters: [{ developerMetadataLookup: { metadataId: 7 } }] } }], 7)).toThrow(/вне вкладки/);
    expect(() => assertOnlyTab(TAB_ID, [{ createDeveloperMetadata: { developerMetadata: { location: { spreadsheet: true } } } }])).toThrow(/вне вкладки/);
    expect(() => metaOnly(TAB_ID, 'x', 7)).not.toThrow();
  });

  it('гейт: «закрыт» при открытых условиях, нарушенное тождество, неверное наследие — стоп; честный красный месяц — публикуется', () => {
    expect(publishGate([AUGR, pnlRow()])).toEqual([]);
    expect(publishGate([pnlRow({ financial_state: 'FINANCIAL_COMPLETE', pending_rows: 1 })])[0]).toMatch(/открытых условиях/);
    expect(publishGate([pnlRow({ double_count_residual_rub: 5 })])[0]).toMatch(/тождество/);
    expect(publishGate([pnlRow({ month: '2026-08' })])[0]).toMatch(/наследия/);
    expect(publishGate([pnlRow({ financial_state: 'UNKNOWN_COST_PRESENT', unconsumed_finance_rub: 100 })])).toEqual([]);
    // власть месяца обязательна: пустая или чужая — стоп
    expect(publishGate([pnlRow({ authority: '' })])[0]).toMatch(/власть месяца/);
    expect(publishGate([pnlRow({ authority: undefined })])[0]).toMatch(/власть месяца/);
    expect(publishGate([pnlRow({ month: '2026-07', authority: 'CURRENT_PHASE_C' })])[0]).toMatch(/власть месяца/);
  });

  it('флаги: по умолчанию выключено; включение требует sheetId; ADOPT — sha256', () => {
    expect(publishEnv({})).toEqual({ enabled: false, sheetId: undefined, adoptSha: undefined });
    expect(() => publishEnv({ UNITKA_STORE_PNL_PUBLISH: '1' })).toThrow(/SHEET_ID/);
    expect(publishEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: '989153123' })).toMatchObject({ enabled: true, sheetId: 989153123 });
    expect(() => publishEnv({ UNITKA_STORE_PNL_ADOPT_SHA: 'x' })).toThrow(/sha256/);
  });

  it('шум: исторические PENDING 2024–2025 не предупреждаются, новые и активного горизонта — да', () => {
    const rows = [
      { supplier_oper_name: 'Коррекция продаж', treatment: 'PENDING_CLASSIFICATION', rows_n: 8, last_seen: { value: '2025-07-01' } },
      { supplier_oper_name: 'X', treatment: 'PENDING_CLASSIFICATION', rows_n: 1, last_seen: '2026-09-15' },
      { supplier_oper_name: 'Новая', treatment: 'NEW_FINANCE_OPERATION', rows_n: 1, last_seen: '2025-01-01' },
    ];
    expect(activeNewOperations(rows).map((o) => o.supplier_oper_name)).toEqual(['X', 'Новая']);
  });
});

describe('WB STORE P&L AUTO-PUBLISH — загрузчик', () => {
  const ctx = (): LoaderContext => ({
    config: { environment: 'prod', rawDataset: 'wb_raw', unitkaOpsDataset: 'wb_ops', unitkaMartDataset: 'wb_mart', unitkaSheetName: 'WB_Юнит_2025',
      unitkaSpreadsheetId: 's', projectId: 'p', bqLocation: 'EU', imageDigest: 'd', gitSha: 'g', executionId: 'e' } as LoaderContext['config'],
    logger: new Logger({}, 'error'), logicalPeriod: '2026-10-09T10', runId: 'run1', targetDate: '2026-10-09T10',
  });
  const AUGR = pnlRow({ month: '2026-08', financial_state: 'LEGACY_PARTIAL_KNOWN_DEFECTS' });
  const PNL = [AUGR, pnlRow()];
  const withEnv = async (env: Record<string, string>, f: () => Promise<void>): Promise<void> => {
    const keep = { ...process.env }; Object.assign(process.env, env);
    try { await f(); } finally { for (const k of Object.keys(env)) delete process.env[k]; Object.assign(process.env, keep); }
  };

  it('сетка вкладки не прочитана (columnCount 0): STORE_PNL_TAB_MISSING, записей нет', async () => {
    const cols = tabState.cols; tabState.cols = 0;
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID) }, async () => {
      const a = deps(PNL);
      await expect(unitkaStorePnlLoader(ctx(), a.d)).rejects.toMatchObject({ code: 'STORE_PNL_TAB_MISSING' });
      expect(a.calls.writes).toEqual([]);
    });
    tabState.cols = cols;
  });

  it('без флага: шлюз readonly, записей нет', async () => {
    const { d, calls } = deps(PNL);
    await unitkaStorePnlLoader(ctx(), d);
    expect(calls.readonly).toBe(true);
    expect(calls.writes).toEqual([]);
  });

  it('схема A:Q без отпечатка контракта: плановый прогон НЕ мигрирует (STORE_PNL_CONTRACT_MIGRATION_REQUIRED), записей нет', async () => {
    const init = buildTabPlan([AUGR, pnlRow({ revenue_rub: 999 })]);
    // живая вкладка A:Q: сетка 17 колонок (11.10 чтение заметок R1..U1 падало «exceeds grid limits»)
    tabState.values = [[...init.header], ...init.rows]; tabState.meta = []; tabState.notes = []; tabState.title = TAB_NAME; tabState.cols = 21;
    const adopt = contentSha(tabState.values);
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID), UNITKA_STORE_PNL_ADOPT_SHA: adopt }, async () => {
      const a = deps(PNL);
      await expect(unitkaStorePnlLoader(ctx(), a.d)).rejects.toMatchObject({ code: 'STORE_PNL_CONTRACT_MIGRATION_REQUIRED' });
      expect(a.calls.writes).toEqual([]);
    });
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID), UNITKA_STORE_PNL_ADOPT_SHA: adopt,
      UNITKA_STORE_PNL_MIGRATE_CONTRACT: 'f'.repeat(64) }, async () => {
      await expect(unitkaStorePnlLoader(ctx(), deps(PNL).d)).rejects.toMatchObject({ code: 'STORE_PNL_CONTRACT_MIGRATION_REQUIRED' });
    });
  });

  it('управляемая миграция: точный отпечаток контракта → одна атомарная запись A:U, заметки, два отпечатка; дальше NOOP без допусков', async () => {
    // как в production 16.10: вкладка A:U (21 колонка, заметки в строке 1), свой отпечаток публикации и прежний контракт
    const init = buildTabPlan([AUGR, pnlRow({ revenue_rub: 999 })]);
    tabState.values = [init.header.slice(0, 21), ...init.rows.map((r) => r.slice(0, 21))]; tabState.notes = []; tabState.title = TAB_NAME; tabState.cols = 21;
    tabState.cf = 0; tabState.merges = 0; tabState.colGroups = []; tabState.rowGroups = [];
    tabState.meta = [{ id: nextMetaId++, key: PUBLISH_META_KEY, value: contentSha(tabState.values) }];
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID),
      UNITKA_STORE_PNL_MIGRATE_CONTRACT: contractSha() }, async () => {
      const a = deps(PNL);
      await unitkaStorePnlLoader(ctx(), a.d);
      expect(a.calls.readonly).toBe(false);
      expect(a.calls.writes).toEqual(['updateCells', 'updateCells', 'updateCells']); // снятие заметок, значения, заметки строки 7 — один batchUpdate
      expect(tabState.colGroups).toHaveLength(DETAIL_GROUPS.length);
      expect(tabState.rowGroups).toHaveLength(1);
      expect(tabState.values[0]).toHaveLength(TAB_COLS);
      expect(tabState.cols).toBe(TAB_COLS);                     // сетка расширена в том же batchUpdate
      expect(tabState.notes).toEqual([...TAB_NOTES]);
      expect(tabState.meta.map((m) => m.key).sort()).toEqual([CONTRACT_META_KEY, PUBLISH_META_KEY].sort());
      expect(tabState.meta.find((m) => m.key === PUBLISH_META_KEY)!.value).toBe(contentSha(normalizeGrid(tabState.values)));
      expect(tabState.meta.find((m) => m.key === CONTRACT_META_KEY)!.value).toBe(contractSha());
    });
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID) }, async () => {
      const c = deps(PNL);
      await unitkaStorePnlLoader(ctx(), c.d);
      expect(c.calls.writes).toEqual([]);
      expect(tabState.meta).toHaveLength(2);
    });
  });

  it('ручная правка заметки заголовка восстанавливается: значения равны → запись вместо NOOP; дальше снова NOOP', async () => {
    tabState.notes[3] = 'правка владельца';
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID) }, async () => {
      const a = deps(PNL);
      await unitkaStorePnlLoader(ctx(), a.d);
      expect(a.calls.writes).toEqual(['updateCells', 'updateCells', 'updateCells']);
      expect(tabState.notes).toEqual([...TAB_NOTES]);
      expect(tabState.colGroups).toHaveLength(DETAIL_GROUPS.length);
      const b = deps(PNL);
      await unitkaStorePnlLoader(ctx(), b.d);
      expect(b.calls.writes).toEqual([]);
    });
  });

  it('миграция при значениях, уже равных плану: пишется отпечаток контракта (не NOOP), иначе перечитывание падало бы всегда', async () => {
    tabState.meta = tabState.meta.filter((m) => m.key !== CONTRACT_META_KEY);
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID), UNITKA_STORE_PNL_MIGRATE_CONTRACT: contractSha() }, async () => {
      const a = deps(PNL);
      await unitkaStorePnlLoader(ctx(), a.d);
      expect(a.calls.writes).toEqual(['updateCells', 'updateCells', 'updateCells']);
      expect(tabState.meta.find((m) => m.key === CONTRACT_META_KEY)!.value).toBe(contractSha());
      expect(tabState.cf).toBe(4 + STATE_RULES.length);
    });
  });

  it('боевой случай: прежний контракт A:U и свой отпечаток, плановый прогон без допуска — отказ до записи', async () => {
    const init = buildTabPlan([AUGR, pnlRow({ revenue_rub: 999 })]);
    tabState.values = [init.header.slice(0, 21), ...init.rows.map((r) => r.slice(0, 21))]; tabState.notes = []; tabState.cols = 21;
    tabState.meta = [{ id: nextMetaId++, key: PUBLISH_META_KEY, value: contentSha(normalizeGrid(tabState.values)) },
      { id: nextMetaId++, key: CONTRACT_META_KEY, value: '77efab2db80ab5e3223b95f28c8d1c450c5dfadd859a06a4ce10ba6237fe0ad4' }];
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID) }, async () => {
      const a = deps(PNL);
      await expect(unitkaStorePnlLoader(ctx(), a.d)).rejects.toMatchObject({ code: 'STORE_PNL_CONTRACT_MIGRATION_REQUIRED' });
      expect(a.calls.requests).toEqual([]);
    });
  });

  it('остальные вкладки изменились во время записи → STORE_PNL_OTHER_TABS_CHANGED', async () => {
    tabState.meta = tabState.meta.filter((m) => m.key !== CONTRACT_META_KEY);
    tamperOnWrite = true;
    try {
      await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID), UNITKA_STORE_PNL_MIGRATE_CONTRACT: contractSha() }, async () => {
        await expect(unitkaStorePnlLoader(ctx(), deps(PNL).d)).rejects.toMatchObject({ code: 'STORE_PNL_OTHER_TABS_CHANGED' });
      });
    } finally { tamperOnWrite = false; }
  });

  it('контракт: совпадает → без миграции; иной → только по точному допуску', () => {
    expect(decideContract({ publishedContract: contractSha() })).toEqual({ ok: true, migrate: false });
    expect(decideContract({ publishedContract: undefined, allowMigrationTo: contractSha() })).toEqual({ ok: true, migrate: true });
    expect(decideContract({ publishedContract: 'a'.repeat(64) })).toMatchObject({ ok: false });
  });

  it('с флагом: ручная правка — STORE_PNL_TAB_EDITED без записи; чужое имя — STORE_PNL_TAB_MISSING; сломанная модель — STORE_PNL_PUBLISH_GATE', async () => {
    await withEnv({ UNITKA_STORE_PNL_PUBLISH: '1', UNITKA_STORE_PNL_SHEET_ID: String(TAB_ID) }, async () => {
      const saved = tabState.values.map((r) => [...r]);
      tabState.values[1]![1] = 1;
      const a = deps([AUGR, pnlRow({ revenue_rub: 5 })]);
      await expect(unitkaStorePnlLoader(ctx(), a.d)).rejects.toMatchObject({ code: 'STORE_PNL_TAB_EDITED' });
      expect(a.calls.writes).toEqual([]);
      tabState.values = saved;
      tabState.title = 'Другое';
      await expect(unitkaStorePnlLoader(ctx(), deps(PNL).d)).rejects.toMatchObject({ code: 'STORE_PNL_TAB_MISSING' });
      tabState.title = TAB_NAME;
      const g = deps([AUGR, pnlRow({ financial_state: 'FINANCIAL_COMPLETE', pending_rows: 2 })]);
      await expect(unitkaStorePnlLoader(ctx(), g.d)).rejects.toMatchObject({ code: 'STORE_PNL_PUBLISH_GATE' });
      expect(g.calls.writes).toEqual([]);
    });
  });
});

describe('Налог 2 % — расшифровка, не второй вычет (OWNER ACK 11.10)', () => {
  it('колонка D = management_tax_rub; чистая прибыль строки та же, с колонкой и без; повторный вычет поменял бы итог', () => {
    const row = pnlRow({ management_tax_rub: 21464.59 });
    const p = buildTabPlan([row]);
    expect(p.header[3]).toBe('Налог 2% (в составе вклада SKU)');
    expect(p.rows[0]![3]).toBe(21464.59);
    expect(p.rows[0]![COL.NET]).toBe(118);      // чистая прибыль из вью, без вычета налога
    expect(p.rowIdentityMaxAbs).toBe(0);        // строка складывается БЕЗ колонки налога
    const net = Number(p.rows[0]![COL.NET]);
    expect(net - Number(p.rows[0]![3])).not.toBe(net);
    expect(buildTabPlan([pnlRow({ management_tax_rub: 0 })]).rows[0]![COL.NET]).toBe(net);
  });

  it('26 колонок: у каждого заголовка есть заметка; заметки входят в отпечаток плана', () => {
    expect(TAB_COLS).toBe(26);
    expect(TAB_NOTES.every((n) => n.length > 20)).toBe(true);
    expect(TAB_NOTES[3]).toMatch(/повторно НЕ вычитается/);
    expect(buildTabPlan([pnlRow()]).notes).toEqual([...TAB_NOTES]);
  });
});

describe('Утрата товара: списание и компенсация — две строки, не неттуются (FIRE/LOST INVENTORY, OWNER ACK 11.10)', () => {
  const JUL = { month: '2026-07', marketplace_income_rub: 0, inventory_loss_writeoff_rub: 281074.51, wb_loss_compensation_rub: 69479.72,
    force_majeure_net_effect_rub: 69479.72 - 281074.51, management_net_store_profit_rub: 118 - 281074.51 + 69479.72 };
  it('колонки O/P/Q: списание (расход), компенсация (доход), чистый эффект справочно; строка складывается без Q', () => {
    const p = buildTabPlan([pnlRow(JUL)]);
    expect(p.header.slice(COL.LOSS, COL.FM_NET + 1)).toEqual(['Списание утраченного товара', 'Компенсация WB за утрату товара', 'Чистый эффект форс-мажора (справочно)']);
    expect([COL.LOSS, COL.LOSS_COMP, COL.FM_NET, COL.NET, COL.NET_EX_FM].map((c) => p.rows[0]![c])).toEqual([281074.51, 69479.72, -211594.79, -211476.79, 118]);
    expect(p.rowIdentityMaxAbs).toBe(0);
  });
  // Нетто в одной колонке арифметически не отличимо в строке — его исключает SQL (списание = Σ позиций отчётов WB,
  // компенсация = строки финотчёта; QA LOSS_EVIDENCE / LOSS_COMPENSATION_RECONCILED). Здесь — второй вычет эффекта.
  it('чистый эффект, вошедший в прибыль второй раз, или несогласованный с двумя строками, — строка не складывается', () => {
    expect(buildTabPlan([pnlRow({ ...JUL, management_net_store_profit_rub: JUL.management_net_store_profit_rub - 211594.79 })]).rowIdentityMaxAbs).toBeGreaterThan(0);
    expect(buildTabPlan([pnlRow({ ...JUL, force_majeure_net_effect_rub: 0 })]).rowIdentityMaxAbs).toBeGreaterThan(0);
  });
  it('гейт: «закрыт» при себестоимости утраты без доказательства или заказах со склада после события — стоп', () => {
    const closed = { month: '2026-07', financial_state: 'FINANCIAL_COMPLETE', sku_month_closed: true, month_finance_final: true, ads_days: 31,
      ads_final_days: 31, account_invoice_window_closed: true };
    expect(publishGate([pnlRow(closed)])).toEqual([]);
    expect(publishGate([pnlRow({ ...closed, inventory_loss_cogs_missing_qty: 1 })]).join()).toMatch(/открытых условиях/);
    expect(publishGate([pnlRow({ ...closed, inventory_loss_double_count_risk_qty: 2 })]).join()).toMatch(/открытых условиях/);
    expect(publishGate([pnlRow({ ...closed, loss_compensation_unreconciled_rows: 1 })]).join()).toMatch(/открытых условиях/);
  });
  it('доказательства утраты не загружены (пустая закрытая таблица) или загружены двумя импортами — стоп публикации', () => {
    expect(publishGate([pnlRow({ inventory_loss_evidence_rows: 0, inventory_loss_evidence_runs: 0 })]).join()).toMatch(/не загружены/);
    expect(publishGate([pnlRow({ inventory_loss_evidence_runs: 2 })]).join()).toMatch(/не одним импортом/);
    expect(publishGate([pnlRow()])).toEqual([]);
    expect(publishGate([]).join()).toMatch(/пусто/);
    expect(publishGate([pnlRow({ loss_compensation_documents_not_once: 1 })]).join()).toMatch(/не сверяются/);
  });
  it('вью без колонок утраты/налога (старая схема) — отказ, а не молчаливые нули', () => {
    const old = pnlRow();
    delete (old as Record<string, unknown>).inventory_loss_writeoff_rub;
    expect(() => buildTabPlan([old])).toThrow(/нет колонок inventory_loss_writeoff_rub/);
  });
});

describe('Гейт модели: власть месяца и наследие (H0)', () => {
  it('история 2026-05..07 — HISTORICAL_CANONICAL и не наследие; 2026-08 — наследие; чужая власть — отказ', () => {
    expect(publishGate([pnlRow({ month: '2026-05', authority: 'HISTORICAL_CANONICAL', financial_state: 'FINANCIAL_COMPLETE',
      sku_month_closed: true, month_finance_final: true, ads_days: 31, ads_final_days: 31, account_invoice_window_closed: true })])).toEqual([]);
    expect(publishGate([pnlRow({ month: '2026-05', authority: 'CURRENT_PHASE_C' })]).join()).toMatch(/власть месяца/);
    expect(publishGate([pnlRow({ month: '2026-05', financial_state: 'LEGACY_PARTIAL_KNOWN_DEFECTS' })]).join()).toMatch(/наследия/);
    expect(publishGate([pnlRow({ month: '2026-08', authority: 'CURRENT_PHASE_C', financial_state: 'LEGACY_PARTIAL_KNOWN_DEFECTS' })])).toEqual([]);
  });
});
