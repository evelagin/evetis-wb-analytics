/**
 * Фикстуры Financial Integrity V1: книга в памяти из нескольких секций месяцев (сентябрь-наследие + месяцы Calendar V2),
 * эмулятор формул листа (зависимые финансовые поля пересчитываются, как это делает сам Google Sheets), фейк BigQuery со
 * слоем сверки, журналом ремонта и снимком issue. Ни Sheets, ни BigQuery.
 *
 * Эмулятор повторяет ровно канонические формулы блока (formulas.ts, сверены с живым листом Phase 2A):
 *   AC = AA − AA·AB%;  AE = AA − AA·AD;  AH = AA·2%;  AI = AE − AF − AH − COGS;
 *   Y = −(COGS + overhead + AF)·N;  W = Q·AI − X − AG − S·(AI + AF + REV) + Y;  V = W/Q;  Z = X/((Q − N)·AC);
 * сводка C..J = Σ блоков; MTD I = Σ дневных I. COGS и overhead берутся из ТЕКСТА формул секции.
 */
import { OFFSET, SUMMARY, SUMMARY_TO_OFFSET, BOOK_ANCHORS, colA1, isoToSerial, addDaysIso, type CellValue } from '../src/loaders/unitka/model.js';
import { dayRowOf, formatMonthKey, type MonthKey } from '../src/loaders/unitka/calendar.js';
import type { Snapshot } from '../src/loaders/unitka/plan.js';
import { findBlocks } from '../src/loaders/unitka/model.js';
import type { SheetsGateway, SheetMeta, SheetStructure, WriteRange, FormatWrite, FormatGrid } from '../src/loaders/unitka/sheets.js';
import type { QueryRunner } from '../src/loaders/mart/bq.js';
import type { FactRow, LogisticsRateRow, CommissionRateRow } from '../src/loaders/unitka/bq.js';
import { planMonthPrep, type PrepInputs } from '../src/loaders/unitka/monthprep.js';
import {
  sectionFromSpec, septemberSpec, applyPlan, insertColumnsInto, cogsSnapshot, septemberStructure, septemberRowFormats,
  SEPT_NMS, NEW_NM, WIDTH_SEPT, SHEET_ID,
} from './unitka_calendar_fixture.js';

export const SHEET = 'WB_Юнит_2025';
export const REV_RATE = 32.5;

const colNum = (letters: string): number => [...letters].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);

/** Книга: секции месяцев (Snapshot: grid topRow..mtdRow, formulas firstDailyRow..mtdRow), якоря и LAST_CLOSED_DATE. */
export class MemoryBook implements SheetsGateway {
  batchWrites: WriteRange[][] = [];
  formatWrites = 0;
  reads = 0;
  failNextWrite: Error | null = null;
  /** Лист ПОДТВЕРЖДАЕТ запись, но значения не применяет («потерянная запись») — ловится только перечитыванием. */
  dropNextWrite = false;
  /** Первое чтение ПОСЛЕ следующей успешной записи падает: запись состоялась, проверка — нет. */
  private readFailure: { error: Error; writesAtArm: number } | null = null;
  failReadAfterNextWrite(error: Error): void { this.readFailure = { error, writesAtArm: this.batchWrites.length }; }
  lcdSerial: number;
  /** Gate 10: строки ZZ_CONFIG!A29:B34. Пусто — блока нет (production до миграции). */
  lifecycle: CellValue[][] = [];
  constructor(public sections: Snapshot[], public rowCount: number, public columnCount: number, public anchorCol: number, lcdIso: string, private readonly readonlyScope = false) {
    this.lcdSerial = isoToSerial(lcdIso);
    this.recompute();
  }
  section(monthKey: string): Snapshot { const s = this.sections.find((x) => x.geometry.monthKey === monthKey); if (!s) throw new Error(`нет секции ${monthKey}`); return s; }
  private byRow(row: number): Snapshot | undefined { return this.sections.find((s) => row >= s.geometry.topRow && row <= s.geometry.mtdRow); }
  get(row: number, col: number): CellValue { const s = this.byRow(row); return s ? (s.grid[row - s.geometry.topRow]?.[col - 1] ?? '') : ''; }
  formula(row: number, col: number): CellValue { const s = this.byRow(row); if (!s || row < s.geometry.firstDailyRow) return ''; return s.formulas[row - s.geometry.firstDailyRow]?.[col - 1] ?? ''; }
  /** Запись значения (как values API): формула в ячейке заменяется значением. */
  set(row: number, col: number, v: CellValue): void {
    const s = this.byRow(row); if (!s) throw new Error(`строка ${row} вне секций`);
    const gr = s.grid[row - s.geometry.topRow]!; while (gr.length < col) gr.push(''); gr[col - 1] = v;
    if (row >= s.geometry.firstDailyRow) { const fr = s.formulas[row - s.geometry.firstDailyRow]!; while (fr.length < col) fr.push(''); fr[col - 1] = v; }
  }

  async readSheetMeta(): Promise<SheetMeta> { return { sheetId: SHEET_ID, rowCount: this.rowCount, columnCount: this.columnCount, locale: 'en_US', anchorCol: this.anchorCol }; }
  async readSheetStructure(): Promise<SheetStructure> { return septemberStructure(this.rowCount, this.columnCount); }
  async readRowFormats(): Promise<Map<number, Array<Record<string, unknown> | null>>> { return new Map(); }
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    this.reads++;
    if (this.readFailure && this.batchWrites.length > this.readFailure.writesAtArm) { const e = this.readFailure.error; this.readFailure = null; throw e; }
    return ranges.map((r) => {
      if (r === 'LAST_CLOSED_DATE') return [[this.lcdSerial]];
      if (r === 'ZZ_CONFIG!A29:B34') return this.lifecycle;
      const colA = /!A1:A(\d+)$/.exec(r);
      if (colA) {
        const a: CellValue[][] = Array.from({ length: Number(colA[1]) }, () => [null]);
        for (const s of this.sections) a[s.geometry.topRow - 1] = [s.grid[0]![0]!];
        return a;
      }
      const anch = /!([A-Z]+)(\d+):([A-Z]+)(\d+)$/.exec(r);
      if (anch && Number(anch[2]) === BOOK_ANCHORS.LCD_MIRROR_ROW && colNum(anch[1]!) === this.anchorCol) return [[this.lcdSerial], [REV_RATE]];
      const g = /!A(\d+):[A-Z]+(\d+)$/.exec(r);
      if (g) { const s = this.sections.find((x) => x.geometry.topRow === Number(g[1])); if (!s) throw new Error(`нет секции с заголовком в строке ${g[1]}`); return s.grid.map((row) => [...row]); }
      const single = /!([A-Z]+)(\d+)$/.exec(r);
      if (single) return [[this.get(Number(single[2]), colNum(single[1]!))]];
      throw new Error(`неожиданный диапазон ${r}`);
    });
  }
  async readFormulas(range: string): Promise<CellValue[][]> {
    const m = /!A(\d+):/.exec(range)!; const s = this.sections.find((x) => x.geometry.firstDailyRow === Number(m[1]));
    if (!s) throw new Error(`нет секции с первым днём в строке ${m[1]}`);
    return s.formulas.map((row) => [...row]);
  }
  async readFormats(): Promise<FormatGrid> { return { sheetId: SHEET_ID, rows: [] }; }
  async formatWrite(_id: number, w: FormatWrite[]): Promise<number> { this.formatWrites += w.length; return w.length; }
  async structureWrite(): Promise<number> { throw new Error('суточный Engine не делает структурных записей'); }
  async batchWrite(data: WriteRange[]): Promise<number> {
    if (this.readonlyScope) throw new Error('запись на readonly-шлюзе');
    if (this.failNextWrite) { const e = this.failNextWrite; this.failNextWrite = null; throw e; }
    this.batchWrites.push(data);
    if (this.dropNextWrite) { this.dropNextWrite = false; return data.reduce((k, d) => k + d.values.length, 0); }
    let n = 0;
    for (const d of data) {
      if (d.range === 'LAST_CLOSED_DATE') { this.lcdSerial = Number(d.values[0]![0]); n++; continue; }
      const m = /!([A-Z]+)(\d+):([A-Z]+)(\d+)$/.exec(d.range); if (!m) throw new Error(`диапазон записи ${d.range}`);
      const col = colNum(m[1]!); const r1 = Number(m[2]);
      d.values.forEach((row, i) => {
        const r = r1 + i;
        if (col === this.anchorCol && r === BOOK_ANCHORS.LCD_MIRROR_ROW) this.lcdSerial = Number(row[0]);
        else if (col === this.anchorCol && r === BOOK_ANCHORS.REVERSE_ROW) { /* ставка обратного плеча — константа фикстуры */ }
        else this.set(r, col, row[0] as CellValue);
        n++;
      });
    }
    this.recompute();
    return n;
  }

  /** Эмулятор формул листа: зависимые поля блоков, сводка и MTD I — для всех секций. */
  recompute(): void {
    const num = (v: CellValue): number => (typeof v === 'number' ? v : 0);
    for (const s of this.sections) {
      const g = s.geometry;
      const blocks = findBlocks(s.grid[0] ?? [], s.width);
      for (let i = 0; i < g.daysInMonth; i++) {
        const row = dayRowOf(g, i);
        const closed = (this.get(row, SUMMARY.date) as number) <= this.lcdSerial;
        const put = (col: number, v: CellValue): void => { const gr = s.grid[row - g.topRow]!; while (gr.length < col) gr.push(''); gr[col - 1] = v; };
        for (const b of blocks) {
          const at = (o: number): CellValue => this.get(row, b.start + o);
          const derived = [OFFSET.priceSpp, OFFSET.priceMinusComm, OFFSET.tax, OFFSET.unitProfit, OFFSET.adsOut, OFFSET.profitAll, OFFSET.profit1, OFFSET.drr];
          if (!closed) { for (const o of derived) put(b.start + o, ''); continue; }
          const ai = String(this.formula(row, b.start + OFFSET.unitProfit));
          const cm = /-(\d+(?:\.\d+)?|\$R\$45)\)$/.exec(ai.replace(/\s+/g, ''));
          const cogs = cm ? (cm[1] === '$R$45' ? 240 : Number(cm[1])) : 0;
          const om = /\)\+(\d+(?:\.\d+)?)-\(/.exec(String(this.formula(row, b.start + OFFSET.adsOut)).replace(/\s+/g, ''));
          const ov = om ? Number(om[1]) : 0;
          const Q = num(at(OFFSET.orders)), S = num(at(OFFSET.cancels)), N = num(at(OFFSET.bloggers)), AA = num(at(OFFSET.price)), AB = num(at(OFFSET.spp));
          const AD = num(at(OFFSET.commission)), AF = num(at(OFFSET.logistics)), AG = num(at(OFFSET.storage)), X = num(at(OFFSET.adsIn));
          const AC = AA - AA * AB / 100, AE = AA - AA * AD, AH = AA * 0.02, AI = AE - AF - AH - cogs;
          const Y = -(cogs + ov + AF) * N;
          const W = Q * AI - X - AG - S * (AI + AF + REV_RATE) + Y;
          put(b.start + OFFSET.priceSpp, AC); put(b.start + OFFSET.priceMinusComm, AE); put(b.start + OFFSET.tax, AH); put(b.start + OFFSET.unitProfit, AI);
          put(b.start + OFFSET.adsOut, Y); put(b.start + OFFSET.profitAll, W); put(b.start + OFFSET.profit1, Q ? W / Q : '');
          put(b.start + OFFSET.drr, (Q - N) * AC ? X / ((Q - N) * AC) : '');
        }
        for (const [sumCol, off] of SUMMARY_TO_OFFSET) put(sumCol, closed ? blocks.reduce((t, b) => t + num(this.get(row, b.start + off)), 0) : '');
      }
      let mtd = 0;
      for (let i = 0; i < g.daysInMonth; i++) mtd += num(this.get(dayRowOf(g, i), SUMMARY.profit));
      const mr = s.grid[g.mtdRow - g.topRow]!; while (mr.length < SUMMARY.profit) mr.push(''); mr[SUMMARY.profit - 1] = mtd;
    }
  }
}

/** Сентябрь (наследие, 24 блока) + октябрь Calendar V2 (25 блоков, вставка 23 колонок) [+ ноябрь V2]. */
export function buildBook(lcdIso: string, opts: { november?: boolean; readonly?: boolean; august?: boolean } = {}): MemoryBook {
  const sept = sectionFromSpec({ year: 2026, month: 9 }, 735, septemberSpec(), WIDTH_SEPT);
  const colA: CellValue[] = Array(768).fill(null); colA[734] = 'Сентябрь 2026';
  const population = [...SEPT_NMS, NEW_NM].map((nmId) => ({ nmId, name: `Товар ${nmId}` }));
  const prep = (target: MonthKey, predecessor: Snapshot, rowCount: number, columnCount: number, a: CellValue[]): ReturnType<typeof planMonthPrep> => {
    const pg = predecessor.geometry;
    const inp: PrepInputs = {
      target, meta: { sheetId: SHEET_ID, rowCount, columnCount, anchorCol: columnCount }, columnA: a, predecessor, existing: null, population,
      cogs: cogsSnapshot({ [NEW_NM]: 426.735 }), structure: septemberStructure(rowCount, columnCount),
      rowFormats: septemberRowFormats(columnCount, [pg.topRow, pg.headerRow, pg.firstDailyRow, pg.lastDailyRow, pg.mtdRow, pg.spacerRow]),
    };
    const p = planMonthPrep(inp);
    if (p.status !== 'PLAN_CREATE') throw new Error(`фикстура ${formatMonthKey(target)}: ${p.status} ${p.code} ${p.reasons.join(' | ')}`);
    return p;
  };
  const p10 = prep({ year: 2026, month: 10 }, sept, 768, WIDTH_SEPT, colA);
  const width = WIDTH_SEPT + (p10.insertColumns?.count ?? 0);
  const sections: Snapshot[] = [{ ...insertColumnsInto(sept, p10.insertColumns!.at, p10.insertColumns!.count), anchorCol: width }, { ...applyPlan(p10, width), anchorCol: width }];
  if (opts.august) {
    // Август 2026 — месяц ДО эпохи сверки (его вели прежние процессы): строки 700..734, геометрия как в production.
    const aug = sectionFromSpec({ year: 2026, month: 8 }, 700, septemberSpec(), WIDTH_SEPT);
    sections.unshift({ ...insertColumnsInto(aug, p10.insertColumns!.at, p10.insertColumns!.count), anchorCol: width });
  }
  let rows = 803;
  if (opts.november) {
    const a2: CellValue[] = Array(803).fill(null); a2[734] = 'Сентябрь 2026'; a2[768] = 'Октябрь 2026';
    const p11 = prep({ year: 2026, month: 11 }, sections[1]!, 803, width, a2);
    sections.push({ ...applyPlan(p11, width), anchorCol: width });
    rows = p11.geometry!.spacerRow;
  }
  // Ставки и якоря заполняются первым прогоном Engine; именованный LCD и зеркало — числовые с самого начала.
  return new MemoryBook(sections, rows, width, width, lcdIso, opts.readonly ?? false);
}

export const ALL_NMS = [...SEPT_NMS, NEW_NM];

/** Факты слоя сверки за окно: детерминированные; у каждого дня с заказами — цена Orders API 1000 + b. */
export function reconFactsFor(from: string, to: string, over: (f: FactRow) => Partial<FactRow> | null = () => null): FactRow[] {
  const out: FactRow[] = [];
  ALL_NMS.forEach((nm, b) => {
    for (let d = from; d <= to; d = addDaysIso(d, 1)) {
      const i = Number(d.slice(8));
      const orders = (i + b) % 3;
      const base: FactRow = {
        nmId: nm, date: d, views: 100 + b, opens: 10 + i, carts: 3, orders, cancels: 0, stock: 50 - b, adsIn: 5.5, price: orders ? 1000 + b : null, storage: 1.25,
        ordersSource: 'FUNNEL_API', cancelsSource: 'PROXY_FACT_ORDERS', priceSource: orders ? 'ORDERS_API' : null,
        factOrderQty: orders, funnelOrders: orders, funnelOrdersSum: orders ? orders * (1000 + b) : 0, skuActive: true,
        funnelObservedAt: `${addDaysIso(d, 1)} 06:31:00+00`, ordersBuiltAt: `${addDaysIso(d, 1)} 04:01:00+00`, storageObservedAt: `${addDaysIso(d, 1)} 08:45:00+00`,
      };
      out.push({ ...base, ...(over(base) ?? {}) });
    }
  });
  return out;
}

export function rates(lcd: string): { logistics: LogisticsRateRow[]; commission: CommissionRateRow[] } {
  const w = { windowFrom: addDaysIso(lcd, -29), windowTo: lcd };
  return {
    logistics: [{ nmId: 0, shipments: 600, directRate: 60.5, refusals: 50, reverseRate: REV_RATE, forwardSum: 1, reverseSum: 1, nLogistics: 1, nDelivery: 1, deliveryComponentSum: 0, sales: 550, ...w }],
    commission: [{ nmId: 0, sales: 550, logisticsPerUnit: 69, commissionRate: 0.45, ...w }],
  };
}

const bqDate = (iso: string): { value: string } => ({ value: iso });

/** Фейк BigQuery со слоем сверки. INSERT'ы копятся: journal (прогоны), ledger (ремонт), issues (снимок issue). */
export class ReconRunner implements QueryRunner {
  readonly projectId = 'proj';
  queries: string[] = [];
  journal: Array<Record<string, unknown>> = [];
  ledger: Array<Record<string, unknown>> = [];
  issues: Array<Record<string, unknown>> = [];
  ledgerAvailable = true;
  ledgerInsertFails = false;
  /** Снимок issue: число успешных INSERT до отказа (null — без отказа); issueStatements — размер каждой принятой пачки. */
  issuesFailAfter: number | null = null;
  issueStatements: number[] = [];
  /** Окно, которое «вернёт вью» (по умолчанию — согласованное с кодом). */
  windowOverride: { from: string; to: string; epoch: string } | null = null;
  integrityRows: Array<Record<string, unknown>> = [];
  /** Остаток сверки предыдущего production-прогона (previousReconResidual): число, null (нет) или 'fail' (чтение падает). */
  previousResidual: number | null | 'fail' = null;
  cogsRows: Array<Record<string, unknown>> = [];
  constructor(public lcd: string, public facts: FactRow[], public windowFrom: string) {}
  private factRow(f: FactRow, recon: boolean): Record<string, unknown> {
    const base = { nm_id: f.nmId, date_msk: bqDate(f.date), views: f.views, opens: f.opens, carts: f.carts, orders: f.orders, cancels: f.cancels, stock: f.stock, ads_in: f.adsIn,
      price: recon ? f.price : (f.priceSource === 'ORDERS_API' ? f.price : null), storage: f.storage, orders_source: f.ordersSource, cancels_source: f.cancelsSource };
    return recon ? { ...base, price_source: f.priceSource ?? null, fact_order_qty: f.factOrderQty ?? 0, funnel_orders: f.funnelOrders ?? null, funnel_orders_sum: f.funnelOrdersSum ?? null,
      sku_active: f.skuActive !== false, funnel_observed_at: f.funnelObservedAt ?? null, orders_built_at: f.ordersBuiltAt ?? null, storage_observed_at: f.storageObservedAt ?? null } : base;
  }
  async query<T = Record<string, unknown>>(sql: string, params?: Record<string, unknown>): Promise<T[]> {
    this.queries.push(sql.replace(/\s+/g, ' '));
    const r = rates(this.lcd);
    if (sql.includes('INSERT INTO') && sql.includes('UNITKA_REPAIR_LEDGER')) {
      if (this.ledgerInsertFails) throw new Error('ledger insert failed');
      this.ledger.push(...(JSON.parse(String(params!.payload)) as Array<Record<string, unknown>>)); return [] as T[];
    }
    if (sql.includes('INSERT INTO') && sql.includes('UNITKA_INTEGRITY_ISSUES')) {
      if (this.issuesFailAfter !== null && this.issueStatements.length >= this.issuesFailAfter) throw new Error('issues insert failed');
      const batch = JSON.parse(String(params!.payload)) as Array<Record<string, unknown>>;
      this.issueStatements.push(batch.length); this.issues.push(...batch); return [] as T[];
    }
    if (sql.includes('INSERT INTO') && sql.includes('UNITKA_ENGINE_RUNS')) { this.journal.push(params ?? {}); return [] as T[]; }
    if (sql.includes('UNITKA_ENGINE_RUNS') && sql.includes('repairs_residual')) {
      if (this.previousResidual === 'fail') throw new Error('runs table unavailable');
      return (this.previousResidual === null ? [] : [{ residual: this.previousResidual }]) as T[];
    }
    if (sql.includes('UNITKA_REPAIR_LEDGER')) { if (!this.ledgerAvailable) throw new Error('Not found: Table wb_ops.UNITKA_REPAIR_LEDGER'); return [] as T[]; }
    if (sql.includes('V_UNITKA_SOURCE_FRESHNESS')) return [] as T[];
    if (sql.includes('LOADER_RUNS')) {
      // Gate 10: журнал гейтящих загрузчиков — по умолчанию каждые сутки до канонического LCD покрыты
      const out: Array<{ loader_name: string; logical_period: string }> = [];
      for (let t = Date.parse(`${String(params?.since)}T00:00:00Z`) + 86_400_000; ; t += 86_400_000) {
        const iso = new Date(t).toISOString().slice(0, 10);
        if (iso > this.lcd) break;
        out.push({ loader_name: 'funnel', logical_period: iso }, { loader_name: 'mart', logical_period: iso });
      }
      return out as T[];
    }
    if (sql.includes('V_UNITKA_RECON_WINDOW')) {
      const w = this.windowOverride ?? { from: this.windowFrom, to: this.lcd, epoch: '2026-09-01' };
      return [{ last_closed_date: bqDate(this.lcd), window_from: bqDate(w.from), window_to: bqDate(w.to), window_days: 35, reconciliation_epoch: bqDate(w.epoch) }] as T[];
    }
    if (sql.includes('V_UNITKA_LAST_CLOSED_DATE')) return [{ last_closed_date: bqDate(this.lcd), d1_msk: bqDate(this.lcd) }] as T[];
    if (sql.includes('V_UNITKA_RECON_FACT')) return this.facts.map((f) => this.factRow(f, true)) as T[];
    if (sql.includes('V_UNITKA_DAILY_FACT')) return this.facts.filter((f) => f.date.slice(0, 7) === this.lcd.slice(0, 7) && f.skuActive !== false).map((f) => this.factRow(f, false)) as T[];
    if (sql.includes('V_UNITKA_LOGISTICS_RATES')) return r.logistics.map((x) => ({ nm_id: x.nmId, shipments: x.shipments, direct_rate: x.directRate, refusals: x.refusals, reverse_rate: x.reverseRate, forward_sum: x.forwardSum, reverse_sum: x.reverseSum, n_logistics: x.nLogistics, n_delivery: x.nDelivery, delivery_component_sum: x.deliveryComponentSum, sales: x.sales, window_from: bqDate(x.windowFrom), window_to: bqDate(x.windowTo) })) as T[];
    if (sql.includes('V_UNITKA_COMMISSION_RATES')) return r.commission.map((x) => ({ nm_id: x.nmId, sales: x.sales, logistics_per_unit: x.logisticsPerUnit, commission_rate: x.commissionRate, window_from: bqDate(x.windowFrom), window_to: bqDate(x.windowTo) })) as T[];
    if (sql.includes('V_UNITKA_RECON_INTEGRITY') || sql.includes('V_UNITKA_INTEGRITY')) return this.integrityRows as T[];
    if (sql.includes('COGS_CANONICAL')) return this.cogsRows as T[];
    throw new Error(`неожиданный SQL в тесте: ${sql.slice(0, 90)}`);
  }
}

export { colA1 };
