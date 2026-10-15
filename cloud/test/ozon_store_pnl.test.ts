/**
 * D4 — публикация P&L магазина Ozon во вкладку «Ozon Магазин P&L». Строки P&L синтетические (тождества D3 выполнены),
 * книга — in-memory фейк, который применяет запросы batchUpdate (значения, заметки, developer metadata, размеры сетки).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { buildTabPlan, identityResidual, reasonText, TAB_COLS, TAB_HEADER, TAB_NAME, TAB_NOTES, COL, type PnlRow } from '../src/loaders/unitka/ozonstorepnl/tabplan.js';
import { assertOnlyTab, contractSha, CONTRACT_META_KEY, contentSha, decideContract, decidePublish, metaOnly, normalizeGrid, otherTabsFingerprint, publishGate,
  publishRequests, PUBLISH_META_KEY } from '../src/loaders/unitka/ozonstorepnl/publish.js';
import { ozonStorePnlLoader, publishEnv, type OzonStorePnlDeps } from '../src/loaders/unitka/ozonstorepnl/index.js';
import { gateSql, pnlSql, stateSql, type GateInputs, type QaResult } from '../src/loaders/unitka/ozonstorepnl/bq.js';
import { QA_SQL, qaChecks } from '../src/loaders/unitka/ozonstorepnl/qa.js';
import type { CellValue } from '../src/loaders/unitka/model.js';
import { Logger } from '../src/logging.js';
import { LOADERS } from '../src/loaders/registry.js';
import type { LoaderContext } from '../src/loaders/types.js';

/** Строка вью с выполненными тождествами D3: V = B − Σрасходов + U + X, W = 0,02·B, Y = V − W = AA + AB + AC + X. */
function pnlRow(o: Partial<Record<string, unknown>> = {}): PnlRow {
  const c: Record<string, number> = {
    seller_base_revenue_rub: 100000, product_cogs_rub: 20000, commission_rub: 45000, logistics_rub: 8000, last_mile_rub: 1000,
    return_logistics_rub: 500, acquiring_rub: 1100, sku_storage_rub: 0, sku_promotion_rub: 300, other_direct_rub: 50,
    cpc_billing_rub: 9000, cpo_billing_rub: 700, store_ads_rub: 0, crossdock_rub: 400, processing_rub: 300, fbo_placement_rub: 20,
    insurance_rub: 100, other_store_costs_rub: 0, premium_rub: 0, marketplace_compensation_rub: 0, provisional_open_rub: 0,
  };
  for (const k of Object.keys(c)) if (typeof o[k] === 'number') c[k] = o[k] as number;
  const costs = Object.entries(c).filter(([k]) => !['seller_base_revenue_rub', 'marketplace_compensation_rub', 'provisional_open_rub'].includes(k))
    .reduce((s, [, v]) => s + v, 0);
  const V = c.seller_base_revenue_rub! - costs + c.marketplace_compensation_rub! + c.provisional_open_rub!;
  const W = 0.02 * c.seller_base_revenue_rub!;
  const Y = V - W;
  const AC = c.marketplace_compensation_rub! - (c.crossdock_rub! + c.processing_rub! + c.fbo_placement_rub! + c.insurance_rub! + c.other_store_costs_rub! + c.premium_rub!);
  const AB = -1000;
  return {
    pnl_month: { value: '2026-07-01' }, ...c, store_contribution_pre_tax_rub: V, management_tax_rub: W, management_net_store_profit_rub: Y,
    management_net_store_margin: Y / c.seller_base_revenue_rub!, sku_contribution_rub: Y - AB - AC - c.provisional_open_rub!, ads_adjustment_rub: AB,
    unattributed_account_net_rub: AC, financial_state: 'FINANCIAL_COMPLETE', month_closure_status: 'CLOSED', source_freshness_state: 'FRESH',
    known_source_defects: null, provisional_units: 0, in_transit_units: 0, ...o,
  };
}
const GATE_OK: GateInputs = { new_operation_rows: 0, max_abs_conservation_residual_rub: 0, unconsumed_rub: 0, unlinked_posting_rub: 0 };
const QA_OK: QaResult[] = qaChecks().map((c) => ({ id: c.id, status: c.id.startsWith('Q16') || c.id.startsWith('Q20') ? 'WARN' : 'PASS', detail: '' }));
const PNL = [
  pnlRow({ pnl_month: { value: '2026-07-01' } }),
  pnlRow({ pnl_month: { value: '2026-08-01' }, financial_state: 'PARTIAL_SOURCE_MISSING', known_source_defects: 'M2_SALE_AMOUNT_REPRESENTATION' }),
  pnlRow({ pnl_month: { value: '2026-09-01' }, financial_state: 'PARTIAL_AWAITING_ACCRUALS', month_closure_status: 'AWAITING_LATE_ACCRUALS',
    provisional_open_rub: 575.5, provisional_units: 1, in_transit_units: 4 }),
];

describe('D4 — план вкладки', () => {
  it('26 колонок A:Z, строка на месяц, тождества D3 выполнены, отпечаток детерминирован', () => {
    const p = buildTabPlan(PNL);
    expect(TAB_COLS).toBe(26);
    expect(p.tab).toBe(TAB_NAME);
    expect(p.range).toBe(`'${TAB_NAME}'!A1:Z4`);
    expect(p.rows.every((r) => r.length === TAB_COLS)).toBe(true);
    expect(p.rowIdentityMaxAbs).toBe(0);
    expect(buildTabPlan(PNL).fingerprint).toBe(p.fingerprint);
    expect(p.rows.map((r) => r[0])).toEqual(['2026-07', '2026-08', '2026-09']);
  });

  it('прибыль складывается из колонок вкладки; «без X» = Y − X; X показан', () => {
    const p = buildTabPlan(PNL);
    for (const r of p.rows) {
      const n = (i: number): number => r[i] as number;
      const y = n(1) - [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15].reduce((s, i) => s + n(i), 0) + n(16) - n(17) + n(COL.X);
      expect(Math.abs(y - n(COL.NET))).toBeLessThanOrEqual(0.02);
      expect(Math.abs(n(COL.NET) - n(COL.X) - n(COL.NET_WITHOUT_X))).toBeLessThanOrEqual(0.01);
    }
    expect(p.rows[2]![COL.X]).toBe(575.5);
  });

  it('состояния и причины — только из D3, не окончательные месяцы так и подписаны', () => {
    const p = buildTabPlan(PNL);
    expect(p.rows.map((r) => r[COL.STATE])).toEqual(['Деньги Ozon сверены, себестоимость предварительная', 'Не хватает денег источника', 'Ждём начислений Ozon — предварительно']);
    expect(p.rows[0]![COL.REASON]).toBe('себестоимость предварительная: часть единиц на юридической базе импорта (D3.1)');
    expect(p.rows[1]![COL.REASON]).toBe('M2: сумма реализации хранится не полностью');
    expect(p.rows[2]![COL.REASON]).toBe('доставлено без начисления: 1 ед. (оценка X); в пути: 4 ед.');
    expect(p.rows[2]![COL.CLOSURE]).toBe('Ждём поздних начислений');
    expect(reasonText(pnlRow({ known_source_defects: 'D1_REVERSAL_NOT_LOADED', financial_state: 'PARTIAL_SOURCE_MISSING' }))).toBe('D1: не загружены сторно реализации при возвратах');
  });

  it('неизвестное состояние или нет колонки вью — отказ, а не молчаливый ноль', () => {
    expect(() => buildTabPlan([pnlRow({ financial_state: 'NEW_STATE' })])).toThrow(/неизвестное состояние/);
    const r = pnlRow(); delete (r as Record<string, unknown>).premium_rub;
    expect(() => buildTabPlan([r])).toThrow(/нет колонок premium_rub/);
  });

  it('заметки заголовка — контракт: 26 непустых, отпечаток контракта меняется при правке текста', () => {
    expect(TAB_NOTES).toHaveLength(TAB_COLS);
    expect(TAB_NOTES.every((n) => n.length > 20)).toBe(true);
    expect(new Set(TAB_HEADER).size).toBe(TAB_COLS);
    expect(contractSha()).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe('D4 — QA D3 в гейте и причины из ворот', () => {
  it('встроенный QA побайтно равен каноническому файлу D3 (иначе: node scripts/gen_ozon_store_pnl_qa.mjs)', () => {
    expect(QA_SQL).toBe(readFileSync(new URL('../../sql/ozon/qa_ozon_store_pnl_v1.sql', import.meta.url), 'utf8'));
    expect(qaChecks().map((c) => c.id.slice(0, 3))).toEqual(Array.from({ length: 23 }, (_, i) => `Q${String(i + 1).padStart(2, '0')}`));
    const body = (q: string): string => q.replace(/^(\s*--[^\n]*\n)*/, '').trim();
    expect(qaChecks().every((c) => /^(WITH|SELECT)\b/.test(body(c.sql)) && !c.sql.endsWith(';'))).toBe(true);
  });
  it('любой FAIL, ERROR, незнакомый статус или отсутствие QA — блок; WARN — нет', () => {
    expect(publishGate(PNL, GATE_OK, QA_OK).model).toEqual([]);
    const with_ = (id: string, status: string): QaResult[] => QA_OK.map((r) => (r.id.startsWith(id) ? { ...r, status, detail: 'x' } : r));
    expect(publishGate(PNL, GATE_OK, with_('Q06', 'FAIL')).model.join()).toMatch(/QA Q06_ADS_BILLING_IDENTITY: FAIL/);
    expect(publishGate(PNL, GATE_OK, with_('Q15', 'ERROR')).model.join()).toMatch(/QA Q15_L1_L4_UNCHANGED: ERROR/);
    expect(publishGate(PNL, GATE_OK, with_('Q21', 'NO_ROW')).model.join()).toMatch(/NO_ROW/);
    expect(publishGate(PNL, GATE_OK, []).model.join()).toMatch(/QA D3 не выполнен/);
    expect(publishGate(PNL, GATE_OK).model.join()).toMatch(/QA D3 не выполнен/);
  });
  it('причины неполноты — из ворот состояния D3, по строке', () => {
    const st = [{ pnl_month: { value: '2026-07-01' } },
      { pnl_month: '2026-08-01', unmapped_sku_rows: 2, cogs_gap_units: 3, reversal_weak_signal_postings: 1, finance_control_open_days: 4 },
      { pnl_month: '2026-09-01', cis_without_document_postings: 1, in_transit_without_accrual_units: 2, provisional_inputs_missing: 1 }];
    const p = buildTabPlan(PNL, st);
    expect(p.rows[1]![COL.REASON]).toBe('M2: сумма реализации хранится не полностью; контроль начислений D1: открытых суток 4; SKU вне карты: 2 строк; себестоимость не найдена: 3 ед.; слабый признак сторно, нужна сверка: 1 отпр.');
    expect(p.rows[2]![COL.REASON]).toBe('доставлено без начисления: 1 ед. (оценка X, из них выкуп СНГ без документа: 1); у оценки X нет входа: 1; в пути: 4 ед. (без начисления: 2)');
    expect(() => buildTabPlan(PNL, st.slice(0, 2))).toThrow(/нет строки состояния/);
  });
  it('NULL в деньгах — отказ; NULL маржи — пустая ячейка; NUMERIC как Big-объект читается', () => {
    expect(() => buildTabPlan([pnlRow({ commission_rub: null })])).toThrow(/NULL в денежных колонках commission_rub/);
    expect(buildTabPlan([pnlRow({ management_net_store_margin: null })]).rows[0]![COL.MARGIN]).toBe('');
    class Big { constructor(private v: string) {} toString(): string { return this.v; } }
    const r = pnlRow(); r.premium_rub = new Big('0'); r.seller_base_revenue_rub = new Big(String(r.seller_base_revenue_rub));
    expect(buildTabPlan([r]).rowIdentityMaxAbs).toBe(0);
  });
});

describe('D4 — гейты публикации', () => {
  it('чистые данные — гейтов нет', () => {
    expect(publishGate(PNL, GATE_OK, QA_OK)).toEqual({ source: [], model: [] });
  });
  it('неразобранные операции блокируют: состояние PENDING и строки детектора', () => {
    expect(publishGate([pnlRow({ financial_state: 'PENDING_CLASSIFICATION' })], GATE_OK, QA_OK).model.join()).toMatch(/PENDING_CLASSIFICATION/);
    expect(publishGate(PNL, { ...GATE_OK, new_operation_rows: 2 }, QA_OK).model.join()).toMatch(/неразобранных начислений Ozon: 2/);
  });
  it('нарушенное тождество, несошедшиеся деньги, «сверено» при открытых условиях — блок', () => {
    expect(publishGate([pnlRow({ management_net_store_profit_rub: 1 })], GATE_OK, QA_OK).model.join()).toMatch(/тождество D3/);
    expect(publishGate(PNL, { ...GATE_OK, max_abs_conservation_residual_rub: 0.5 }, QA_OK).model.join()).toMatch(/RAW ≠ леджер/);
    expect(publishGate(PNL, { ...GATE_OK, unconsumed_rub: 1 }, QA_OK).model.join()).toMatch(/без колонки/);
    expect(publishGate([pnlRow({ provisional_open_rub: 10, provisional_units: 1 })], GATE_OK, QA_OK).model.join()).toMatch(/при открытых условиях/);
    expect(publishGate([pnlRow({ known_source_defects: 'M2_SALE_AMOUNT_REPRESENTATION' })], GATE_OK, QA_OK).model.join()).toMatch(/при открытых условиях/);
    expect(publishGate([pnlRow({ month_closure_status: 'OPEN' })], GATE_OK, QA_OK).model.join()).toMatch(/при открытых условиях/);
  });
  it('пустая вью и пропуск месяца — блок', () => {
    expect(publishGate([], GATE_OK, QA_OK).model.join()).toMatch(/пусто/);
    expect(publishGate([PNL[0]!, PNL[2]!], GATE_OK, QA_OK).model.join()).toMatch(/не подряд/);
  });
  it('несвежие источники — отдельный гейт источников', () => {
    const g = publishGate([pnlRow({ source_freshness_state: 'STALE' })], GATE_OK, QA_OK);
    expect(g.model).toEqual([]);
    expect(g.source.join()).toMatch(/не свежие \(STALE\)/);
  });
  it('известные WARN (мост к L4, застывшие отправления) гейтом не являются: их колонок в гейте нет', () => {
    const src = readFileSync(new URL('../src/loaders/unitka/ozonstorepnl/publish.ts', import.meta.url), 'utf8');
    expect(src).not.toMatch(/l4_bridge|lifecycle/);
  });
});

describe('D4 — решение и запросы', () => {
  const plan = buildTabPlan(PNL);
  const grid: CellValue[][] = [[...plan.header], ...plan.rows];
  it('первая публикация в пустую вкладку — WRITE без ADOPT; контракт ставится без миграции', () => {
    const d = decidePublish({ current: [], plan });
    expect(d).toMatchObject({ action: 'WRITE', first: true, clearRows: 0 });
    expect(decideContract({ firstPublication: true })).toEqual({ ok: true, migrate: false });
    expect(decideContract({ firstPublication: false }).ok).toBe(false);
  });
  it('содержимое = план → NOOP; правка вручную → REFUSE; чужое содержимое без отпечатка → UNTRACKED; ADOPT разрешает', () => {
    const sha = contentSha(normalizeGrid(grid));
    expect(decidePublish({ current: grid, publishedSha: sha, plan })).toEqual({ action: 'NOOP', sha, setMeta: false });
    const edited = grid.map((r) => [...r]); edited[1]![1] = 1;
    expect(decidePublish({ current: edited, publishedSha: sha, plan })).toMatchObject({ action: 'REFUSE', code: 'OZON_STORE_PNL_TAB_EDITED' });
    expect(decidePublish({ current: [['чужое']], plan })).toMatchObject({ action: 'REFUSE', code: 'OZON_STORE_PNL_TAB_UNTRACKED' });
    expect(decidePublish({ current: [['чужое']], adoptSha: contentSha([['чужое']]), plan })).toMatchObject({ action: 'WRITE', adopted: true });
  });
  it('все запросы — только про sheetId вкладки; чужой sheetId — отказ', () => {
    const d = decidePublish({ current: [], plan }) as Extract<ReturnType<typeof decidePublish>, { action: 'WRITE' }>;
    const req = publishRequests(plan, 7, 1000, d);
    const ids = JSON.stringify(req).match(/"sheetId":\d+/g) ?? [];
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids)).toEqual(new Set(['"sheetId":7']));
    expect(() => metaOnly(7, d.sha)).not.toThrow();
    expect(() => assertOnlyTab(7, [...req, { updateCells: { range: { sheetId: 8 }, rows: [] } }])).toThrow(/вне вкладки/);
    expect(() => assertOnlyTab(7, [{ deleteSheet: { sheetId: 7 } }])).toThrow(/вне вкладки/);
  });
  it('отпечаток остальных вкладок не зависит от своей вкладки', () => {
    const a = [{ sheetId: 1, title: 'WB_Юнит_2025', index: 0, rowCount: 10, columnCount: 5 }, { sheetId: 7, title: TAB_NAME, index: 1, rowCount: 3, columnCount: 26 }];
    const b = [{ ...a[0]! }, { ...a[1]!, rowCount: 99 }];
    expect(otherTabsFingerprint(a, 7)).toBe(otherTabsFingerprint(b, 7));
    expect(otherTabsFingerprint(a, 7)).not.toBe(otherTabsFingerprint([{ ...a[0]!, rowCount: 11 }, a[1]!], 7));
  });
});

// ── In-memory книга: применяет запросы batchUpdate так же, как Sheets API (для полей, которые использует писатель). ──
type Meta = { id: number; key: string; value: string };
class FakeBook {
  tabs = new Map<number, { title: string; index: number; rows: number; cols: number; values: CellValue[][]; notes: Map<string, string>; meta: Meta[] }>();
  writes: string[] = [];
  nextMeta = 100;
  tamperOtherOnWrite = false;
  constructor() {
    this.tabs.set(1, { title: 'WB_Юнит_2025', index: 0, rows: 800, cols: 600, values: [['wb']], notes: new Map(), meta: [] });
    this.tabs.set(2, { title: 'OZON_Юнит_2025', index: 1, rows: 800, cols: 600, values: [['ozon']], notes: new Map(), meta: [] });
    this.tabs.set(3, { title: 'WB Магазин P&L', index: 2, rows: 30, cols: 21, values: [['wb p&l']], notes: new Map(), meta: [] });
    this.tabs.set(7, { title: TAB_NAME, index: 3, rows: 1000, cols: 26, values: [], notes: new Map(), meta: [] });
  }
  gateway(readonly: boolean): ReturnType<OzonStorePnlDeps['makeSheets']> {
    return {
      readSheetMetadata: async (id: number) => { const t = this.tabs.get(id); return t ? { title: t.title, rowCount: t.rows, columnCount: t.cols, metadata: t.meta.map((m) => ({ ...m })) } : null; },
      readValues: async (ranges: string[]) => ranges.map((r) => { const t = [...this.tabs.values()].find((x) => r.startsWith(`'${x.title}'`))!; return t.values.map((x) => [...x]); }),
      readNotes: async (title: string, cells: ReadonlyArray<{ row: number; col: number }>) => {
        const t = [...this.tabs.values()].find((x) => x.title === title)!; const m = new Map<string, string>();
        for (const c of cells) { const v = t.notes.get(`${c.row}|${c.col}`); if (v !== undefined) m.set(`${c.row}|${c.col}`, v); }
        return m;
      },
      readSheetsList: async () => [...this.tabs.entries()].map(([sheetId, t]) => ({ sheetId, title: t.title, index: t.index, rowCount: t.rows, columnCount: t.cols })),
      structureWrite: async (requests: unknown[]) => {
        if (readonly) throw new Error('readonly gateway');
        for (const r of requests as Record<string, Record<string, unknown>>[]) {
          const kind = Object.keys(r)[0]!; const b = r[kind]!; this.writes.push(kind);
          if (kind === 'appendDimension') { const t = this.tabs.get(b.sheetId as number)!; if (b.dimension === 'ROWS') t.rows += b.length as number; else t.cols += b.length as number; }
          if (kind === 'updateCells') {
            const rg = b.range as { sheetId: number; startRowIndex: number; startColumnIndex: number }; const t = this.tabs.get(rg.sheetId)!;
            const rows = b.rows as Array<{ values: Array<Record<string, unknown>> }>;
            rows.forEach((row, i) => row.values.forEach((cell, j) => {
              const R = rg.startRowIndex + i, C = rg.startColumnIndex + j;
              if (b.fields === 'note') { t.notes.set(`${R + 1}|${C + 1}`, String(cell.note)); return; }
              while (t.values.length <= R) t.values.push([]);
              const uv = cell.userEnteredValue as { numberValue?: number; stringValue?: string } | undefined;
              t.values[R]![C] = uv === undefined ? '' : uv.numberValue ?? uv.stringValue ?? '';
            }));
          }
          if (kind === 'createDeveloperMetadata') { const m = b.developerMetadata as { metadataKey: string; metadataValue: string; location: { sheetId: number } }; this.tabs.get(m.location.sheetId)!.meta.push({ id: this.nextMeta++, key: m.metadataKey, value: m.metadataValue }); }
          if (kind === 'updateDeveloperMetadata') {
            const f = (b.dataFilters as Array<{ developerMetadataLookup: { metadataId: number; metadataLocation: { sheetId: number } } }>)[0]!.developerMetadataLookup;
            const m = this.tabs.get(f.metadataLocation.sheetId)!.meta.find((x) => x.id === f.metadataId)!; m.value = (b.developerMetadata as { metadataValue: string }).metadataValue;
          }
        }
        if (this.tamperOtherOnWrite) this.tabs.get(2)!.rows += 1;
        return requests.length;
      },
    } as unknown as ReturnType<OzonStorePnlDeps['makeSheets']>;
  }
}

describe('D4 — загрузчик (репетиция на in-memory книге)', () => {
  const logs: Array<{ msg: string; ctx: Record<string, unknown> }> = [];
  const logger = new Logger({}, 'error');
  for (const lvl of ['info', 'warn'] as const) (logger as unknown as Record<string, unknown>)[lvl] = (msg: string, ctx: Record<string, unknown> = {}) => logs.push({ msg, ctx });
  const ctx = (): LoaderContext => ({
    config: { environment: 'prod', unitkaSpreadsheetId: 'test-book', projectId: 'project-fa311fc0-4d87-4781-986', bqLocation: 'EU' } as LoaderContext['config'],
    logger, logicalPeriod: '2026-10-15T13', runId: 'run1', targetDate: '2026-10-15T13',
  });
  const deps = (book: FakeBook, pnl: PnlRow[] = PNL, gi: GateInputs = GATE_OK): { d: OzonStorePnlDeps; ro: boolean[] } => {
    const ro: boolean[] = [];
    return { ro, d: { makeSheets: (_c, readonly) => { ro.push(readonly); return book.gateway(readonly); },
      makeBq: () => ({ readPnl: async () => pnl, readGateInputs: async () => gi, readStates: async () => [], runQa: async () => QA_OK }) } };
  };
  const withEnv = async (env: Record<string, string>, f: () => Promise<void>): Promise<void> => {
    const keep = { ...process.env }; Object.assign(process.env, env);
    try { await f(); } finally { for (const k of Object.keys(env)) delete process.env[k]; Object.assign(process.env, keep); }
  };
  const ON = { OZON_STORE_PNL_PUBLISH: '1', OZON_STORE_PNL_SHEET_ID: '7' };

  it('без флага: Sheets не открывается для записи, записей нет, план и гейты в журнале', async () => {
    const book = new FakeBook(); const a = deps(book);
    const r = await ozonStorePnlLoader(ctx(), a.d);
    expect(a.ro).toEqual([]);
    expect(book.writes).toEqual([]);
    expect(r).toEqual({ rowsFetched: 3, rowsLoaded: 0 });
    expect(logs.some((l) => l.msg === 'ozon_store_pnl_tab_plan' && l.ctx.write === 'DISABLED')).toBe(true);
  });

  it('первая публикация → значения = план, заметки, два отпечатка; повтор — NOOP без записей; другие вкладки не тронуты', async () => {
    const book = new FakeBook();
    const before = otherTabsFingerprint([...book.tabs.entries()].map(([sheetId, t]) => ({ sheetId, title: t.title, index: t.index, rowCount: t.rows, columnCount: t.cols })), 7);
    await withEnv(ON, async () => {
      await ozonStorePnlLoader(ctx(), deps(book).d);
      const tab = book.tabs.get(7)!;
      const plan = buildTabPlan(PNL);
      expect(normalizeGrid(tab.values)).toEqual(normalizeGrid([[...plan.header], ...plan.rows]));
      expect(TAB_NOTES.every((n, i) => tab.notes.get(`1|${i + 1}`) === n)).toBe(true);
      expect(tab.meta.find((m) => m.key === PUBLISH_META_KEY)?.value).toBe(contentSha(normalizeGrid(tab.values)));
      expect(tab.meta.find((m) => m.key === CONTRACT_META_KEY)?.value).toBe(contractSha());
      expect(logs.some((l) => l.msg === 'ozon_store_pnl_rollback_manifest' && l.ctx.previousRows === 0)).toBe(true);
      book.writes = [];
      await ozonStorePnlLoader(ctx(), deps(book).d);
      expect(book.writes).toEqual([]);
      expect(logs.filter((l) => l.msg === 'ozon_store_pnl_tab_published').at(-1)?.ctx.action).toBe('NOOP');
    });
    expect(book.tabs.get(1)!.values).toEqual([['wb']]);
    expect(book.tabs.get(3)!.values).toEqual([['wb p&l']]);
    const after = otherTabsFingerprint([...book.tabs.entries()].map(([sheetId, t]) => ({ sheetId, title: t.title, index: t.index, rowCount: t.rows, columnCount: t.cols })), 7);
    expect(after).toBe(before);
  });

  it('историческая корректировка в D3 → прошлый месяц перезаписывается сам (source-driven UPDATE), без ручной фиксации', async () => {
    const book = new FakeBook();
    await withEnv(ON, async () => {
      await ozonStorePnlLoader(ctx(), deps(book).d);
      const changed = [pnlRow({ pnl_month: { value: '2026-07-01' }, commission_rub: 44000 }), PNL[1]!, PNL[2]!];
      await ozonStorePnlLoader(ctx(), deps(book, changed).d);
      expect(normalizeGrid(book.tabs.get(7)!.values)).toEqual(normalizeGrid([[...TAB_HEADER], ...buildTabPlan(changed).rows]));
    });
  });

  it('план сократился (месяц исчез из вью) — лишняя строка очищается, дальше NOOP', async () => {
    const book = new FakeBook();
    await withEnv(ON, async () => {
      await ozonStorePnlLoader(ctx(), deps(book).d);
      await ozonStorePnlLoader(ctx(), deps(book, PNL.slice(0, 2)).d);
      expect(normalizeGrid(book.tabs.get(7)!.values)).toEqual(normalizeGrid([[...TAB_HEADER], ...buildTabPlan(PNL.slice(0, 2)).rows]));
      book.writes = [];
      await ozonStorePnlLoader(ctx(), deps(book, PNL.slice(0, 2)).d);
      expect(book.writes).toEqual([]);
    });
  });

  it('ADOPT владельца после ручной правки восстанавливает вкладку; заметка испорчена — принудительная запись', async () => {
    const book = new FakeBook();
    await withEnv(ON, async () => {
      await ozonStorePnlLoader(ctx(), deps(book).d);
      book.tabs.get(7)!.values[1]![1] = 1;
      const adopt = contentSha(normalizeGrid(book.tabs.get(7)!.values));
      await withEnv({ OZON_STORE_PNL_ADOPT_SHA: adopt }, async () => { await ozonStorePnlLoader(ctx(), deps(book).d); });
      expect(normalizeGrid(book.tabs.get(7)!.values)).toEqual(normalizeGrid([[...TAB_HEADER], ...buildTabPlan(PNL).rows]));
      book.tabs.get(7)!.notes.set('1|3', 'испорчено'); book.writes = [];
      await ozonStorePnlLoader(ctx(), deps(book).d);
      expect(book.writes).toContain('updateCells');
      expect(book.tabs.get(7)!.notes.get('1|3')).toBe(TAB_NOTES[2]);
    });
  });

  it('ручная правка вкладки → OZON_STORE_PNL_TAB_EDITED, записи нет', async () => {
    const book = new FakeBook();
    await withEnv(ON, async () => {
      await ozonStorePnlLoader(ctx(), deps(book).d);
      book.tabs.get(7)!.values[1]![1] = 1; book.writes = [];
      await expect(ozonStorePnlLoader(ctx(), deps(book).d)).rejects.toMatchObject({ code: 'OZON_STORE_PNL_TAB_EDITED' });
      expect(book.writes).toEqual([]);
    });
  });

  it('гейт модели и гейт источников блокируют до любого обращения к записи', async () => {
    const book = new FakeBook();
    await withEnv(ON, async () => {
      await expect(ozonStorePnlLoader(ctx(), deps(book, [pnlRow({ financial_state: 'PENDING_CLASSIFICATION' })]).d)).rejects.toMatchObject({ code: 'OZON_STORE_PNL_PUBLISH_GATE' });
      await expect(ozonStorePnlLoader(ctx(), deps(book, PNL, { ...GATE_OK, new_operation_rows: 1 }).d)).rejects.toMatchObject({ code: 'OZON_STORE_PNL_PUBLISH_GATE' });
      await expect(ozonStorePnlLoader(ctx(), deps(book, [pnlRow({ source_freshness_state: 'STALE' })]).d)).rejects.toMatchObject({ code: 'OZON_STORE_PNL_SOURCE_NOT_READY' });
    });
    expect(book.writes).toEqual([]);
    expect(book.tabs.get(7)!.values).toEqual([]);
  });

  it('вкладка с другим названием или без sheetId — отказ до записи', async () => {
    const book = new FakeBook(); book.tabs.get(7)!.title = 'Чужая';
    await withEnv(ON, async () => {
      await expect(ozonStorePnlLoader(ctx(), deps(book).d)).rejects.toMatchObject({ code: 'OZON_STORE_PNL_TAB_MISSING' });
    });
    expect(() => publishEnv({ OZON_STORE_PNL_PUBLISH: '1' } as NodeJS.ProcessEnv)).toThrow(/OZON_STORE_PNL_SHEET_ID/);
    expect(book.writes).toEqual([]);
  });

  it('изменение остальных вкладок во время записи → OZON_STORE_PNL_OTHER_TABS_CHANGED', async () => {
    const book = new FakeBook(); book.tamperOtherOnWrite = true;
    await withEnv(ON, async () => {
      await expect(ozonStorePnlLoader(ctx(), deps(book).d)).rejects.toMatchObject({ code: 'OZON_STORE_PNL_OTHER_TABS_CHANGED' });
    });
  });

  it('не prod — отказ', async () => {
    const c = ctx(); (c.config as { environment: string }).environment = 'shadow';
    await expect(ozonStorePnlLoader(c, deps(new FakeBook()).d)).rejects.toMatchObject({ code: 'OZON_STORE_PNL_PROD_ONLY' });
  });
});

describe('D4 — регистрация и изоляция', () => {
  it('загрузчик зарегистрирован, только prod, слот МСК-часа', () => {
    expect(LOADERS['ozon-store-pnl']).toMatchObject({ prodOnly: true, retryTransient: true });
  });
  it('SQL читает только ozon_mart: ни одной ссылки на wb_*', () => {
    for (const q of [pnlSql('project-fa311fc0-4d87-4781-986'), gateSql('project-fa311fc0-4d87-4781-986'), stateSql('project-fa311fc0-4d87-4781-986'), ...qaChecks().map((c) => c.sql)]) {
      expect(q).toMatch(/\.ozon_mart\./);
      // ссылка на таблицу WB — только полное имя в обратных кавычках; текст строк и комментариев не в счёт
      expect(q).not.toMatch(/`[^`]*\.wb_[A-Za-z0-9_]*\.[^`]*`/);
      expect([...q.matchAll(/`([^`]+)`/g)].every((m) => /\.(ozon_mart|ozon_raw|evetis_ref)\./.test(m[1]!) || !m[1]!.includes('.'))).toBe(true);
      expect(q.replace(/^(\s*--[^\n]*\n)*/, '').trim()).toMatch(/^(SELECT|WITH)\b/);
    }
  });
  it('тождество строки считается на неокруглённых значениях', () => {
    expect(identityResidual(pnlRow())).toBeLessThan(1e-9);
  });
});

describe('D4 — параллельный QA', () => {
  it('порядок результатов = порядок файла, не больше QA_CONCURRENCY одновременно, ошибка запроса → ERROR', async () => {
    const { OzonStorePnlBq, QA_CONCURRENCY } = await import('../src/loaders/unitka/ozonstorepnl/bq.js');
    let live = 0, peak = 0, calls = 0;
    const fake = { query: async (o: { query: string }): Promise<[unknown[]]> => {
      live++; peak = Math.max(peak, live); calls++;
      await new Promise((r) => setTimeout(r, 2));
      live--;
      if (o.query.includes('Q15') || o.query.includes('FCT_OZON_PNL_MONTHLY`\nWHERE')) throw new Error('boom');
      const id = /'(Q\d\d_[A-Z0-9_]+)' check_name/.exec(o.query)?.[1] ?? 'X';
      return [[{ check_name: id, status: 'PASS', detail: '' }]];
    } };
    const res = await new OzonStorePnlBq('project-fa311fc0-4d87-4781-986', 'EU', fake).runQa();
    expect(calls).toBe(23);
    expect(peak).toBeLessThanOrEqual(QA_CONCURRENCY);
    expect(res.map((r) => r.id)).toEqual(qaChecks().map((c) => c.id));
    expect(res.find((r) => r.id.startsWith('Q15'))?.status).toBe('ERROR');
  });
});
