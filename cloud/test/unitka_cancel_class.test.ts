/**
 * OWNER ACK «CANCEL RECONCILIATION» (02.11.2026) — классы отмен по присутствию в воронке и логистике.
 *
 * Два случая октября 2026 (адреса ячеек — как в живой книге; деньги синтетические: репозиторий публичный,
 * приёмка на живых значениях — в приватном архиве доказательств):
 *   18.10 / 535580776: отмена дня заказа без логистики осталась в воронке → S 0 → 1 и поправка W +1·(AF+REV):
 *                      ΔW = −AI ровно (а не стандартное −(AI+AF+REV));
 *   24.10 / 252442517: отмена заказа, которого в воронке нет (Q 0) → S 1 → 0: ΔW = +(AI+AF+REV).
 * Проверяются: SQL-контракт слоя и обеих вью фактов, формула W с поправкой, распознавание (шаблон месяца, Store P&L),
 * заметки S, план/манифест/запись/перечитывание/откат загрузчика unitka-cancel-restore.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { REFUSAL_MATURITY_DAYS } from '../src/loaders/unitka/integrity.js';
import { profitAllFormula, profitAllVoidQty, toLocaleFormula } from '../src/loaders/unitka/formulas.js';
import { CANCEL_NOTE_MARKER, REFUSAL_NOTE_MARKER, planRefusalNotes, noteKey } from '../src/loaders/unitka/refusal_notes.js';
import {
  cancelNoteText, planCancelRestore, modelW, verifyCancelCells, buildCancelManifest, encodeCancelManifest, parseCancelManifest,
  rollbackDrift, unitkaCancelRestoreLoader, cancelRestoreWriteAllowed, cancelWriteRequests, assertConfined, outcomeState, unacceptedAnomalies,
  type LiveRow,
} from '../src/loaders/unitka/cancel_restore.js';
import { UnitkaBq, type CancelClassRow, type FactRow } from '../src/loaders/unitka/bq.js';
import { parseUnitkaComponents, profitAllColumns, formulaGridFromColumns } from '../src/loaders/unitka/storepnl/parse.js';
import { snapshotRecord } from '../src/loaders/unitka/storepnl/bq.js';
import { LOADERS } from '../src/loaders/registry.js';
import { OFFSET, colA1, isoToSerial, type CellValue } from '../src/loaders/unitka/model.js';
import type { ExpectedCell } from '../src/loaders/unitka/plan.js';
import type { SheetsGateway, WriteRange, SheetMeta, StructureRequest } from '../src/loaders/unitka/sheets.js';
import type { UnitkaDeps } from '../src/loaders/unitka/index.js';
import type { LoaderContext } from '../src/loaders/types.js';
import type { Logger } from '../src/logging.js';
import { loadConfig } from '../src/config.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p: string): string => readFileSync(join(root, p), 'utf8');
const code = (s: string): string => s.split('\n').map((l) => { const i = l.indexOf('--'); return i >= 0 ? l.slice(0, i) : l; }).join('\n');
const flat = (s: string): string => code(s).replace(/\s+/g, ' ');
function viewBodies(sql: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of code(sql).matchAll(/CREATE OR REPLACE VIEW `[^`]+\.(\w+)` AS\n([\s\S]*?)(?=\nCREATE |$)/g)) out.set(m[1]!, m[2]!);
  return out;
}

const REV = 30;

/* ───────────────────────────── SQL ───────────────────────────── */

describe('SQL: слой классов отмен (cancel_class_v1.sql)', () => {
  const sql = read('sql/unitka/cancel_class_v1.sql');
  const v = viewBodies(sql);
  const d = flat(v.get('V_UNITKA_CANCEL_CLASS_DAILY') ?? '');
  it('четыре вью, только SELECT, только wb_raw / wb_mart (изоляция маркетплейсов)', () => {
    expect([...v.keys()].sort()).toEqual(['V_UNITKA_CANCEL_CLASS_DAILY', 'V_UNITKA_CANCEL_EVIDENCE', 'V_UNITKA_CANCEL_MATURITY', 'V_UNITKA_FINANCE_FINAL_INTERVALS']);
    expect(code(sql)).not.toMatch(/\b(CREATE\s+(OR\s+REPLACE\s+)?(TABLE|PROCEDURE|FUNCTION)|INSERT\s+INTO|UPDATE\s+`|DELETE\s+FROM|MERGE\s+|TRUNCATE|DROP\s+)/i);
    for (const body of v.values()) {
      expect(body).not.toMatch(/evetis_ref|ozon_/i);
      for (const m of body.matchAll(/`project-fa311fc0-4d87-4781-986\.(\w+)\./g)) expect(['wb_raw', 'wb_mart']).toContain(m[1]);
    }
  });
  it('логистика отмены — финотчёт по srid (тот же класс, что у отказов); начало — первый день воронки API', () => {
    const e = flat(v.get('V_UNITKA_CANCEL_EVIDENCE')!);
    expect(e).toContain('wb_mart.V_UNITKA_REFUSAL_EVIDENCE` e ON e.srid = o.srid');
    expect(e).toContain('IFNULL(e.logistics_legs, 0) > 0 AS has_logistics');
    expect(e).toContain("SELECT DATE '2026-09-04' AS funnel_api_from");
  });
  it('зрелость = непрерывное покрытие окончательным финотчётом на 46 суток (исполнение — tools/tests/test_unitka_cancel_maturity.py)', () => {
    expect(REFUSAL_MATURITY_DAYS).toBe(46);
    expect(d).toContain(`SELECT DATE '2026-09-04' AS funnel_api_from, ${REFUSAL_MATURITY_DAYS} AS maturity_days`);
    expect(flat(v.get('V_UNITKA_CANCEL_MATURITY')!)).toContain(`DATE_ADD(order_date, INTERVAL ${REFUSAL_MATURITY_DAYS} DAY) AS e`);
    const iv = flat(v.get('V_UNITKA_FINANCE_FINAL_INTERVALS')!);
    expect(iv).toContain("WHERE report_period = 'WEEKLY' AND report_type = 1 AND status = 'COMPLETE' AND date_from IS NOT NULL AND date_to IS NOT NULL");
    expect(iv).toContain('WHERE weekly_final = TRUE AND report_type IN (1, 2)');
    expect(iv).toContain('HAVING COUNT(DISTINCT w.report_type) = 2');
    expect(d).toContain("WHEN NOT IFNULL(mt.mature, FALSE) THEN 'IMMATURE'");
    expect(d).not.toContain('_rr_date');                                      // «последний FINAL _rr_date» больше не правило
  });
  it('места в Q: воронка минус заказы без отмены и доказанные заказы вне Orders API — это не S = MIN(S, Q)', () => {
    expect(d).toContain('j.funnel_orders - j.kept_qty - j.refusal_counted_qty - j.sold_not_in_api_qty AS room');
    expect(d).toContain('LEAST(k.cancel_log_qty, GREATEST(k.room, 0)) AS log_in_q');
    expect(d).toContain('LEAST(k.cancel_nolog_qty, GREATEST(k.room - LEAST(k.cancel_log_qty, GREATEST(k.room, 0)), 0)) AS nolog_in_q');
    expect(d).toContain('refusal_counted_qty + log_in_q + nolog_in_q AS final_cancels');
    expect(d).toContain('nolog_in_q AS void_qty');
    expect(d).not.toMatch(/LEAST\([^)]*funnel_orders/);                      // S никогда не режется по Q напрямую
  });
  it('прежний S = отмены следующих дней + доказанные отказы; аномалии лист не меняют', () => {
    expect(d).toContain("SUM(IF(is_cancel AND SAFE_CAST(SUBSTR(cancel_dt, 1, 10) AS DATE) > order_date, quantity, 0)) AS later_cancel_qty");
    expect(d).toContain('j.later_cancel_qty + j.refusal_counted_qty AS legacy_cancels');
    expect(d).toContain("WHEN a.room < 0 THEN 'ANOMALY_FUNNEL_BELOW_API'");
    expect(d).toContain("WHEN a.cancel_log_qty > a.log_in_q THEN 'ANOMALY_ABSENT_WITH_LOGISTICS'");
    expect(d).toContain("IF(cancel_class_status = 'FINAL', refusal_counted_qty + log_in_q + nolog_in_q, legacy_cancels) AS effective_cancels");
  });
});

describe('SQL: обе вью фактов берут S из класса отмен одним и тем же выражением', () => {
  const EXPR = "IF(cc.cancel_class_status = 'FINAL', cc.final_cancels, COALESCE(bf.canc, o.canc, 0) + IF(f.forders IS NOT NULL AND bf.canc IS NULL, IFNULL(rf.refusal_counted_qty, 0), 0)) AS cancels, IF(cc.cancel_class_status = 'FINAL', cc.void_qty, 0) AS cancel_void_qty, cc.cancel_class_status,";
  const bodies = [['sql/unitka/engine_v1_views.sql', 'V_UNITKA_DAILY_FACT', 'lcd'], ['sql/unitka/reconcile_v1.sql', 'V_UNITKA_RECON_FACT', 'w']] as const;
  it.each(bodies)('%s %s', (file, name, win) => {
    const body = flat(viewBodies(read(file)).get(name)!);
    expect(body).toContain(EXPR);
    expect(body).toContain(`FROM \`project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_CANCEL_CLASS_DAILY\`, ${win} WHERE date_msk BETWEEN ${win}.d1 AND ${win}.d2`);
    expect(body).toContain('LEFT JOIN cc ON cc.nm_id = g.nm_id AND cc.d = g.d');
    expect(body).toMatch(/refusal_proven_logistics_rub, cancel_void_qty, cancel_class_status FROM p;?\s*$/);
  });
  it('P&L магазина: логистика модели без плеч отмен без логистики (как формула W листа)', () => {
    expect(flat(read('sql/unitka/store_pnl_v1.sql'))).toContain(
      'SUM((orders - IFNULL(void_cancels, 0)) * logistics_per_unit + (cancels - IFNULL(void_cancels, 0)) * reverse_leg_rate) logistics_sku_rub');
  });
  it('откат: прежние тела трёх вью и снятие четырёх новых (потребители — раньше источников)', () => {
    const rb = code(read('sql/unitka/cancel_class_v1_rollback.sql'));
    const drops = [...rb.matchAll(/DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986\.wb_mart\.(\w+)`/g)].map((m) => m[1]);
    expect(drops).toEqual(['V_UNITKA_CANCEL_CLASS_DAILY', 'V_UNITKA_CANCEL_EVIDENCE', 'V_UNITKA_CANCEL_MATURITY', 'V_UNITKA_FINANCE_FINAL_INTERVALS']);
    for (const name of ['V_UNITKA_DAILY_FACT', 'V_UNITKA_RECON_FACT', 'V_WB_STORE_PNL_MONTHLY']) {
      const body = flat((viewBodies(rb).get(name) ?? '').split(/;\s*\n/)[0]!);
      expect(body, name).not.toContain('V_UNITKA_CANCEL_CLASS_DAILY');
      expect(body, name).not.toContain('void_cancels');
      expect(body.length, name).toBeGreaterThan(1000);
    }
    expect(rb.indexOf('V_UNITKA_DAILY_FACT')).toBeLessThan(rb.indexOf('DROP VIEW'));   // потребители — до снятия источника
  });
});

/* ───────────────────────────── формула W ───────────────────────────── */

describe('формула W с поправкой отмен без логистики', () => {
  it('каноническая форма — прежняя формула листа (живой октябрь, блок M)', () => {
    expect(profitAllFormula(13, 794)).toBe('=IF($M794>LAST_CLOSED_DATE,"",Q794*AI794-X794-AG794-S794*(AI794+AF794+REVERSE_LEG_RATE)+Y794)');
    expect(profitAllFormula(13, 794, 2)).toBe('=IF($M794>LAST_CLOSED_DATE,"",Q794*AI794-X794-AG794-S794*(AI794+AF794+REVERSE_LEG_RATE)+Y794+2*(AF794+REVERSE_LEG_RATE))');
    expect(() => profitAllFormula(13, 794, -1)).toThrow();
    expect(() => profitAllFormula(13, 794, 0.5)).toThrow();
  });
  it('распознавание: канон 0, поправка V, всё прочее null (локаль ru — тот же результат)', () => {
    const live = '=IF($JQ788>LAST_CLOSED_DATE;"";JU788*KM788-KB788-KK788-JW788*(KM788+KJ788+REVERSE_LEG_RATE)+KC788)';   // живая ячейка 18.10
    expect(profitAllVoidQty(live, 277, 788, 'SEMICOLON')).toBe(0);
    expect(profitAllVoidQty(toLocaleFormula(profitAllFormula(277, 788, 1), 'SEMICOLON'), 277, 788, 'SEMICOLON')).toBe(1);
    expect(profitAllVoidQty(profitAllFormula(277, 788, 3), 277, 788)).toBe(3);
    for (const bad of [
      profitAllFormula(277, 787, 1),                                   // другая строка
      profitAllFormula(253, 788, 1),                                   // другой блок
      `${profitAllFormula(277, 788).slice(0, -1)}+0*(KJ788+REVERSE_LEG_RATE))`, // V = 0 в хвосте — не канон
      `${profitAllFormula(277, 788, 1).slice(0, -1)}-1)`,              // лишний член
      '=JU788*KM788', 1.5, '', null,
    ]) expect(profitAllVoidQty(bad as CellValue, 277, 788), String(bad)).toBeNull();
  });
});

/* ───────────────────────────── заметки ───────────────────────────── */

const ROW_1810: CancelClassRow = {
  nmId: 535580776, date: '2026-10-18', funnelOrders: 3, keptQty: 2, cancelLogQty: 0, cancelNologQty: 1, refusalCountedQty: 0, soldNotInApiQty: 0,
  room: 1, logInQ: 0, nologInQ: 1, nologAbsentQty: 0, legacyCancels: 0, finalCancels: 1, voidQty: 1, status: 'FINAL', sheetChangeRequired: true,
  logSrids: [], nologSrids: ['srid-void-1'], nologCancelDates: ['2026-10-18'], financeFinalThrough: '2026-12-13', matureOn: '2026-12-03',
};
const ROW_2410: CancelClassRow = {
  nmId: 252442517, date: '2026-10-24', funnelOrders: 0, keptQty: 0, cancelLogQty: 0, cancelNologQty: 1, refusalCountedQty: 0, soldNotInApiQty: 0,
  room: 0, logInQ: 0, nologInQ: 0, nologAbsentQty: 1, legacyCancels: 1, finalCancels: 0, voidQty: 0, status: 'FINAL', sheetChangeRequired: true,
  logSrids: [], nologSrids: ['srid-absent-1'], nologCancelDates: ['2026-10-25'], financeFinalThrough: '2026-12-13', matureOn: '2026-12-09',
};

describe('заметка S: причина, даты, srid, источник', () => {
  it('18.10 — отмена в воронке без логистики; 24.10 — отмены в воронке нет', () => {
    const a = cancelNoteText(ROW_1810), b = cancelNoteText(ROW_2410);
    expect(a.startsWith(`${CANCEL_NOTE_MARKER} 2026-10-18: S 0 → 1`)).toBe(true);
    expect(a).toContain('поправка W +1·(AF+REVERSE_LEG_RATE)');
    expect(a).toContain('srid-void-1 (отмена 2026-10-18)');
    expect(a).toContain('финотчёт окончателен непрерывно с 2026-10-18 по 2026-12-03 (46 суток; недели FINANCE_WEEK_STATUS, отчёты FINANCE_REPORT_LOADS), последний окончательный отчёт — по 2026-12-13');
    expect(a).toContain('wb_mart.V_UNITKA_CANCEL_CLASS_DAILY');
    expect(b.startsWith(`${CANCEL_NOTE_MARKER} 2026-10-24: S 1 → 0`)).toBe(true);
    expect(b).toContain('1 шт. без логистики в воронке нет');
    expect(b).toContain('Воронка Q = 0; заказов Orders API без отмены 0');
  });
  it('суточный прогон сохраняет раздел класса отмен, а отказы вне Orders API ведёт как прежде', () => {
    const cancel = cancelNoteText(ROW_1810);
    const e: ExpectedCell = { row: 788, col: 283, want: 1, kind: 'fact', nmId: 535580776, key: 'cancels', date: '2026-10-18', source: 'x' };
    const f = (refusals: number): FactRow => ({ nmId: 535580776, date: '2026-10-18', views: 0, opens: 0, carts: 0, orders: 3, cancels: 1, stock: 0, adsIn: 0,
      price: 640, storage: 0, ordersSource: 'FUNNEL_API', cancelsSource: 'PROXY_FACT_ORDERS', refusalCountedQty: refusals, refusalProvenSrids: 'ebX.1', refusalProvenLogisticsRub: 90 });
    // заметка — только раздел класса: без отказов ничего не меняется
    expect(planRefusalNotes([e], () => f(0), new Map([[noteKey(788, 283), cancel]])).notes).toHaveLength(0);
    // появился отказ: заметка = раздел отказов + сохранённый раздел класса
    const p = planRefusalNotes([e], () => f(1), new Map([[noteKey(788, 283), cancel]]));
    expect(p.notes[0]!.note.startsWith(REFUSAL_NOTE_MARKER)).toBe(true);
    expect(p.notes[0]!.note.endsWith(cancel)).toBe(true);
    // заметка владельца не трогается, даже если в ней встречается метка
    expect(planRefusalNotes([e], () => f(1), new Map([[noteKey(788, 283), `моё: ${CANCEL_NOTE_MARKER}`]])).conflicts).toHaveLength(1);
  });
});

/* ───────────────────────────── Store P&L ───────────────────────────── */

describe('Store P&L: V из формулы W, модель без плеч отмен без логистики', () => {
  const M = 13;                                          // блок 1 — колонка M
  const D1 = isoToSerial('2026-10-01'), D2 = isoToSerial('2026-10-02');
  // сутки «отмена без логистики в Q» после восстановления (Q 3, S 1, V 1), перенесённые на 02.10 ради короткой сетки
  const cells = { orders: 3, cancels: 1, adsIn: 400, adsOut: 0, price: 600, commission: 0.45, priceMinusComm: 330, logistics: 50, storage: 15, tax: 12, unitProfit: 120 };
  const W = 3 * 120 - 400 - 15 - 1 * (120 + 50 + REV) + 0 + 1 * (50 + REV);
  function sheet(fW: string): { grid: CellValue[][]; formulas: CellValue[][] } {
    const width = M + 24;
    const row = (): CellValue[] => Array(width).fill('');
    const top = row(); top[0] = 'Октябрь 2026'; top[M - 1] = '535580776 Энзимная пудра';
    const hdr = row(); hdr[1] = 'Дата'; hdr[M - 1] = 'Дата';
    const d1 = row(); d1[1] = D1; d1[M - 1] = D1; d1[8] = 0;
    for (const k of Object.keys(cells)) d1[M - 1 + OFFSET[k as keyof typeof OFFSET]] = 0;
    const d2 = row(); d2[1] = D2; d2[M - 1] = D2; d2[8] = W;
    for (const [k, v] of Object.entries(cells)) d2[M - 1 + OFFSET[k as keyof typeof OFFSET]] = v;
    d2[M - 1 + OFFSET.profitAll] = W;
    const f2 = row(); f2[M - 1 + OFFSET.profitAll] = fW;
    return { grid: [top, hdr, d1, d2], formulas: [row(), row(), row(), f2] };
  }
  const parse = (fW: string, withFormulas = true) => {
    const s = sheet(fW);
    return parseUnitkaComponents(s.grid, '2026-10-02', REV, '2026-10-01', withFormulas ? s.formulas : undefined, 'SEMICOLON');
  };
  it('поправка распознана: void_cancels = 1, невязка модели 0, колонка снимка — последней', () => {
    const p = parse(toLocaleFormula(profitAllFormula(M, 4, 1), 'SEMICOLON'));
    expect(p.rows).toHaveLength(1);
    expect(p.rows[0]!.void_cancels).toBe(1);
    expect(Math.abs(p.rows[0]!.model_gap)).toBeLessThan(1e-9);
    const rec = snapshotRecord(p.rows[0]!, { snapshotId: 's', runId: 'r', snapshotAt: 't', lcd: '2026-10-02', imageDigest: 'd', gitSha: 'g' });
    expect(Object.keys(rec).at(-1)).toBe('void_cancels');
  });
  it('без формул (или канон W) — V = 0, и невязка честно показывает несписанные плечи (AF + REV)', () => {
    expect(parse('', false).rows[0]!.model_gap).toBeCloseTo(50 + REV, 6);
    expect(parse(toLocaleFormula(profitAllFormula(M, 4), 'SEMICOLON')).rows[0]!.void_cancels).toBe(0);
  });
  it('поправка-подобный хвост не в каноне или V > S — отказ STORE_PNL_SHEET_ERROR', () => {
    const bad = toLocaleFormula(profitAllFormula(M, 4).slice(0, -1) + '+1*(X4+REVERSE_LEG_RATE))', 'SEMICOLON');
    expect(() => parse(bad)).toThrow(expect.objectContaining({ code: 'STORE_PNL_SHEET_ERROR' }));
    expect(() => parse(toLocaleFormula(profitAllFormula(M, 4, 2), 'SEMICOLON'))).toThrow(expect.objectContaining({ code: 'STORE_PNL_SHEET_ERROR' }));
  });
});

/* ───────────────────────────── план и загрузчик ───────────────────────────── */

const TOP = 769;                                         // «Октябрь 2026» в живой книге
const RU = (f: string): string => toLocaleFormula(f, 'SEMICOLON');
const BLOCK = { 252442517: 13, 535580776: 277, 952068582: 469 } as const;   // слоты живой книги: M, JQ, RA
const ROWS = { 252442517: 794, 535580776: 788, 952068582: 797 } as const;
/** Ячейки строк (форма живых суток, деньги синтетические): AE = цена·(1 − комиссия), AI = AE − AF − налог − COGS. */
const LIVE = {
  252442517: { q: 0, s: 1, x: 0, ag: 20, y: 0, ai: 40, af: 55, price: 200, comm: 0.5, tax: 4, date: '2026-10-24' },
  535580776: { q: 3, s: 0, x: 400, ag: 15, y: 0, ai: 120, af: 50, price: 600, comm: 0.45, tax: 12, date: '2026-10-18' },
  952068582: { q: 1, s: 1, x: 10, ag: 5, y: 0, ai: 300, af: 57, price: 1300, comm: 0.45, tax: 26, date: '2026-10-27' },
} as const;
type Nm = keyof typeof BLOCK;

const colNum = (s: string): number => [...s].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);

/**
 * Книга-секция «Октябрь 2026» (769..802): значения, формулы, заметки. W и итог сводки I считаются формулой при чтении,
 * как в Sheets. structureWrite применяет пакет updateCells ЦЕЛИКОМ или (failNext) не применяет вовсе — как batchUpdate.
 */
class Book implements Partial<SheetsGateway> {
  v = new Map<string, CellValue>();
  f = new Map<string, string>();
  notes = new Map<string, string>();
  rev = REV;
  writes: Array<{ data: WriteRange[]; opt: string }> = [];
  structures: StructureRequest[][] = [];
  failNext = false;
  constructor(public readonly ro = false) {
    this.v.set(`${TOP}:1`, 'Октябрь 2026');
    this.v.set(`${TOP + 1}:13`, 'Дата');
    for (let i = 0; i < 31; i++) {
      const r = TOP + 2 + i, serial = isoToSerial(`2026-10-${String(i + 1).padStart(2, '0')}`);
      this.v.set(`${r}:2`, serial);
      for (const start of Object.values(BLOCK)) this.v.set(`${r}:${start + OFFSET.date}`, serial);
    }
    for (const [nm, start] of Object.entries(BLOCK)) {
      const l = LIVE[Number(nm) as Nm], r = ROWS[Number(nm) as Nm];
      this.v.set(`${TOP}:${start}`, `${nm} блок`);
      const put = (off: number, x: CellValue): void => { this.v.set(`${r}:${start + off}`, x); };
      put(OFFSET.orders, l.q); put(OFFSET.cancels, l.s); put(OFFSET.adsIn, l.x); put(OFFSET.storage, l.ag); put(OFFSET.adsOut, l.y);
      put(OFFSET.unitProfit, l.ai); put(OFFSET.logistics, l.af); put(OFFSET.price, l.price); put(OFFSET.commission, l.comm);
      put(OFFSET.priceMinusComm, l.price * (1 - l.comm)); put(OFFSET.tax, l.tax);
      this.f.set(`${r}:${start + OFFSET.profitAll}`, RU(profitAllFormula(start, r)));
    }
  }
  private value(r: number, c: number): CellValue {
    if (c === 9 && r >= TOP + 2 && r <= TOP + 32) {                       // сводка I = Σ W блоков строки
      return Object.values(BLOCK).reduce((t, st) => t + (Number(this.value(r, st + OFFSET.profitAll)) || 0), 0);
    }
    const f = this.f.get(`${r}:${c}`);
    if (f) {
      const start = c - OFFSET.profitAll;
      const V = profitAllVoidQty(f, start, r, 'SEMICOLON') ?? 0;
      const g = (off: number): CellValue => this.v.get(`${r}:${start + off}`) ?? '';
      return modelW({ q: g(OFFSET.orders), x: g(OFFSET.adsIn), ag: g(OFFSET.storage), y: g(OFFSET.adsOut), ai: g(OFFSET.unitProfit), af: g(OFFSET.logistics), s: g(OFFSET.cancels) }, V, this.rev);
    }
    return this.v.get(`${r}:${c}`) ?? '';
  }
  w(nm: Nm): number { return this.value(ROWS[nm], BLOCK[nm] + OFFSET.profitAll) as number; }
  private rect(range: string) {
    const m = /!\$?([A-Z]+)(\d+):\$?([A-Z]+)(\d+)$/.exec(range)!;
    return { r1: Number(m[2]), r2: Number(m[4]), c1: colNum(m[1]!), c2: colNum(m[3]!) };
  }
  async readSheetMeta(): Promise<SheetMeta> { return { sheetId: 739487431, rowCount: 837, columnCount: 623, anchorCol: 623, locale: 'ru_RU' } as SheetMeta; }
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    return ranges.map((range) => {
      if (range === 'REVERSE_LEG_RATE') return [[this.rev]];
      const { r1, r2, c1, c2 } = this.rect(range);
      return Array.from({ length: r2 - r1 + 1 }, (_, i) => Array.from({ length: c2 - c1 + 1 }, (_, j) => this.value(r1 + i, c1 + j)));
    });
  }
  async readFormulas(range: string): Promise<CellValue[][]> {
    const { r1, r2, c1, c2 } = this.rect(range);
    return Array.from({ length: r2 - r1 + 1 }, (_, i) => Array.from({ length: c2 - c1 + 1 }, (_, j) => this.f.get(`${r1 + i}:${c1 + j}`) ?? this.value(r1 + i, c1 + j)));
  }
  async readFormulasBatch(ranges: string[]): Promise<CellValue[][][]> { return Promise.all(ranges.map((r) => this.readFormulas(r))); }
  async readNotes(_n: string, cells: ReadonlyArray<{ row: number; col: number }>): Promise<Map<string, string>> {
    return new Map(cells.filter((c) => this.notes.has(noteKey(c.row, c.col))).map((c) => [noteKey(c.row, c.col), this.notes.get(noteKey(c.row, c.col))!]));
  }
  async batchWrite(data: WriteRange[], opt = 'RAW'): Promise<number> {
    this.writes.push({ data, opt });
    throw new Error('unitka-cancel-restore обязан писать одним batchUpdate, не values API');
  }
  async structureWrite(req: StructureRequest[]): Promise<number> {
    if (this.ro) throw new Error('403 readonly');
    this.structures.push(req);
    if (this.failNext) { this.failNext = false; throw new Error('502 Bad Gateway'); }   // пакет не применён целиком
    for (const q of req as Array<{ updateCells: { range: { sheetId: number; startRowIndex: number; startColumnIndex: number }; rows: Array<{ values: Array<{ userEnteredValue?: { numberValue?: number; formulaValue?: string }; note?: string }> }>; fields: string } }>) {
      const r = q.updateCells.range.startRowIndex + 1, c = q.updateCells.range.startColumnIndex + 1;
      const cell = q.updateCells.rows[0]!.values[0]!;
      for (const field of q.updateCells.fields.split(',')) {
        if (field === 'note') this.notes.set(noteKey(r, c), cell.note ?? '');
        if (field === 'userEnteredValue' && cell.userEnteredValue?.formulaValue !== undefined) this.f.set(`${r}:${c}`, cell.userEnteredValue.formulaValue);
        if (field === 'userEnteredValue' && cell.userEnteredValue?.numberValue !== undefined) { this.f.delete(`${r}:${c}`); this.v.set(`${r}:${c}`, cell.userEnteredValue.numberValue); }
      }
    }
    return req.length;
  }
}

const bqRow = (r: CancelClassRow): Record<string, unknown> => ({
  nm_id: r.nmId, date_msk: { value: r.date }, funnel_orders: r.funnelOrders, kept_qty: r.keptQty, cancel_log_qty: r.cancelLogQty,
  cancel_nolog_qty: r.cancelNologQty, refusal_counted_qty: r.refusalCountedQty, sold_not_in_api_qty: r.soldNotInApiQty, room: r.room,
  log_in_q: r.logInQ, nolog_in_q: r.nologInQ, nolog_absent_qty: r.nologAbsentQty, legacy_cancels: r.legacyCancels, final_cancels: r.finalCancels,
  void_qty: r.voidQty, cancel_class_status: r.status, sheet_change_required: r.sheetChangeRequired,
  cancel_log_srids: r.logSrids.join(',') || null, cancel_nolog_srids: r.nologSrids.join(','), cancel_nolog_dates: r.nologCancelDates.join(','),
  finance_final_through: { value: r.financeFinalThrough }, mature_on: { value: r.matureOn },
});
/** Как вью: IMMATURE-сутки без изменения листа запрос не возвращает (WHERE sheet_change_required OR ANOMALY). */
const runner = (rows: CancelClassRow[], windowFrom = '2026-11-09') => ({
  projectId: 'p',
  query: async (sql: string) => {
    if (sql.includes('V_UNITKA_CANCEL_CLASS_DAILY')) return rows.filter((r) => r.sheetChangeRequired || r.status.startsWith('ANOMALY')).map(bqRow);
    if (sql.includes('V_UNITKA_RECON_WINDOW')) return [{ last_closed_date: { value: '2026-12-13' }, window_from: { value: windowFrom }, window_to: { value: '2026-12-13' }, window_days: 35, reconciliation_epoch: { value: '2026-09-01' } }];
    throw new Error(`неожиданный запрос: ${sql.slice(0, 80)}`);
  },
});
type Ev = Array<{ event: string; data: Record<string, unknown> }>;
function logger() {
  const events: Ev = [];
  const push = (event: string, data: Record<string, unknown>) => events.push({ event, data });
  const l = { info: push, warn: push, error: push, debug() {}, child() { return l; } };
  return { l: l as unknown as Logger, events };
}
const run = (book: Book, env: Record<string, string>, rows: CancelClassRow[] = [ROW_1810, ROW_2410], windowFrom?: string) => {
  const lg = logger();
  const ctx = { config: loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'wb_raw', GIT_SHA: 't', UNITKA_SPREADSHEET_ID: 'ssid', UNITKA_SHEET_NAME: 'WB_Юнит_2025', ...env }),
    logger: lg.l, logicalPeriod: 'p', targetDate: 'p', runId: 'r' } as LoaderContext;
  const deps = { makeSheets: () => book as unknown as SheetsGateway, makeRunner: () => runner(rows, windowFrom), now: () => new Date('2026-12-14T08:00:00Z') } as unknown as UnitkaDeps;
  return { promise: unitkaCancelRestoreLoader(ctx, deps), events: lg.events };
};
const planOf = (events: Ev) => events.find((e) => e.event === 'unitka_cancel_restore_plan')!.data;
async function planDigest(rows: CancelClassRow[] = [ROW_1810, ROW_2410]): Promise<{ digest: string; manifest: string }> {
  const p = run(new Book(true), {}, rows);
  await p.promise;
  return { digest: String(planOf(p.events).manifest_digest), manifest: String(planOf(p.events).manifest_b64) };
}
const WRITE = (digest: string, extra: Record<string, string> = {}) => ({ UNITKA_CANCEL_RESTORE_WRITE: '1', UNITKA_CANCEL_RESTORE_DIGEST: digest, ...extra });
const ANOMALY: CancelClassRow = { ...ROW_2410, nmId: 438775617, date: '2026-10-09', status: 'ANOMALY_ABSENT_WITH_LOGISTICS', cancelLogQty: 1, cancelNologQty: 0, nologAbsentQty: 0,
  legacyCancels: 1, finalCancels: 0, sheetChangeRequired: false, logSrids: ['srid-log-1'], nologSrids: [], nologCancelDates: [] };

/** Снимок Store P&L из книги так, как его строит загрузчик: значения A1:… + формулы только колонок W. */
async function storePnl(book: Book) {
  const meta = await book.readSheetMeta();
  const [grid] = await book.readValues([`'WB_Юнит_2025'!A1:${colA1(meta.columnCount)}${TOP + 33}`]);
  const cols = profitAllColumns(meta.columnCount);
  const formulas = formulaGridFromColumns(cols, await book.readFormulasBatch(cols.map((c) => `'WB_Юнит_2025'!${colA1(c)}1:${colA1(c)}${TOP + 33}`)));
  const p = parseUnitkaComponents(grid!, '2026-10-31', REV, '2026-10-01', formulas, 'SEMICOLON');
  const row = (nm: Nm) => p.rows.find((r) => r.nm_id === nm && r.date_msk === LIVE[nm].date)!;
  return { p, row, profit: p.months['2026-10']!.profit, gapAbs: p.months['2026-10']!.modelGapAbs };
}

describe('unitka-cancel-restore: сценарии октября', () => {
  it('зарегистрирован; запись — только prod + флаг + без DRY_RUN', () => {
    expect(LOADERS['unitka-cancel-restore']).toBeDefined();
    expect(cancelRestoreWriteAllowed({ environment: 'prod', unitkaCancelRestoreWrite: true })).toBe(true);
    expect(cancelRestoreWriteAllowed({ environment: 'shadow', unitkaCancelRestoreWrite: true })).toBe(false);
    expect(cancelRestoreWriteAllowed({ environment: 'prod', unitkaCancelRestoreWrite: true, unitkaDryRun: true })).toBe(false);
    expect(cancelRestoreWriteAllowed({ environment: 'prod' })).toBe(false);
  });

  it('ПЛАН: ΔW 18.10 = −AI, 24.10 = +(AI+AF+REV); записи нет', async () => {
    const book = new Book(true);
    const { promise, events } = run(book, {});
    await expect(promise).resolves.toEqual({ rowsFetched: 2, rowsLoaded: 0 });
    const p = planOf(events);
    expect(p.expected_delta_w).toEqual([{ date: '2026-10-18', nmId: 535580776, deltaW: -120 }, { date: '2026-10-24', nmId: 252442517, deltaW: 125 }]);
    expect(p.expected_delta_w_total).toBe(5);
    expect(p.rows).toEqual(['2026-10-18 535580776 JW788: S 0 → 1, V 1', '2026-10-24 252442517 S794: S 1 → 0, V 0']);
    expect(p.refusals).toEqual([]);
    expect(book.structures).toHaveLength(0);
  });

  it('ЗАПИСЬ: без отпечатка просмотренного плана — отказ до записи', async () => {
    const book = new Book();
    await expect(run(book, WRITE('deadbeef')).promise).rejects.toMatchObject({ code: 'CANCEL_RESTORE_DIGEST_MISMATCH' });
    expect(book.structures).toHaveLength(0);
  });

  it('BLOCKER 3 — ОДИН batchUpdate: S+заметка и W обеих суток в одном пакете, только ячейки манифеста; values API не используется', async () => {
    const book = new Book();
    const { digest } = await planDigest();
    const { promise, events } = run(book, WRITE(digest));
    await expect(promise).resolves.toMatchObject({ rowsLoaded: 3 });
    expect(book.writes).toHaveLength(0);
    expect(book.structures).toHaveLength(1);
    const cells = (book.structures[0] as Array<{ updateCells: { range: { sheetId: number; startRowIndex: number; startColumnIndex: number }; fields: string } }>)
      .map((q) => [q.updateCells.range.sheetId, colA1(q.updateCells.range.startColumnIndex + 1) + (q.updateCells.range.startRowIndex + 1), q.updateCells.fields]);
    expect(cells).toEqual([
      [739487431, 'JW788', 'userEnteredValue,note'], [739487431, 'KA788', 'userEnteredValue'], [739487431, 'S794', 'userEnteredValue,note'],
    ]);
    expect(events.find((e) => e.event === 'unitka_cancel_restore_done')!.data.verify_failed).toBe(0);
  });

  it('BLOCKER 3 — сбой пакета: исход неизвестен → перечитывание, состояние ALL_BEFORE, повтора нет', async () => {
    const book = new Book();
    const { digest } = await planDigest();
    const w0 = book.w(535580776);
    book.failNext = true;
    const r = run(book, WRITE(digest));
    await expect(r.promise).rejects.toMatchObject({ code: 'CANCEL_RESTORE_UNKNOWN_OUTCOME' });
    expect(book.structures).toHaveLength(1);                                  // одна попытка, без повтора
    expect(r.events.find((e) => e.event === 'unitka_cancel_restore_unknown_outcome')!.data.state).toBe('ALL_BEFORE');
    expect(book.w(535580776)).toBe(w0);                                        // лист не изменён ни в одной ячейке
    // Store P&L видит прежний полный снимок: V = 0, невязка 0
    const s = await storePnl(book);
    expect([s.row(535580776).void_cancels, s.gapAbs < 1e-9]).toEqual([0, true]);
  });

  it('BLOCKER 3 — запрос вне ячеек манифеста или чужого листа не уходит', async () => {
    const plan = planCancelRestore({ rows: [ROW_1810], live: new Map([['535580776|2026-10-18', liveRow(535580776)]]), missing: [], windowFrom: '2026-11-09', reverseLegRate: REV, style: 'SEMICOLON' });
    const req = cancelWriteRequests(739487431, plan.cells, new Map([['535580776|2026-10-18', liveRow(535580776)]]), 'after');
    expect(() => assertConfined(req, 739487431, plan.cells)).not.toThrow();
    expect(() => assertConfined(req, 1, plan.cells)).toThrow(expect.objectContaining({ code: 'CANCEL_RESTORE_NOT_CONFINED' }));
    const foreign = [...req, { updateCells: { range: { sheetId: 739487431, startRowIndex: 787, endRowIndex: 788, startColumnIndex: 287, endColumnIndex: 288 }, rows: [], fields: 'userEnteredValue' } }];
    expect(() => assertConfined(foreign, 739487431, plan.cells)).toThrow(expect.objectContaining({ code: 'CANCEL_RESTORE_NOT_CONFINED' }));
    expect(() => assertConfined([{ deleteDimension: {} }], 739487431, plan.cells)).toThrow(expect.objectContaining({ code: 'CANCEL_RESTORE_NOT_CONFINED' }));
  });

  it('BLOCKER 2 — корректная строка + новая ANOMALY: запись остановлена ДО первой записи; принятая поимённо — запись идёт', async () => {
    const rows = [ROW_1810, ANOMALY];
    const { digest } = await planDigest(rows);
    const book = new Book();
    const r = run(book, WRITE(digest), rows);
    await expect(r.promise).rejects.toMatchObject({ code: 'CANCEL_RESTORE_ANOMALY' });
    expect(book.structures).toHaveLength(0);
    expect(planOf(r.events).anomalies_unaccepted).toEqual(['438775617|2026-10-09|ANOMALY_ABSENT_WITH_LOGISTICS']);
    // аномалия входит в отпечаток: тот же план без неё — другой отпечаток
    expect((await planDigest([ROW_1810])).digest).not.toBe(digest);
    // поимённое принятие владельцем
    const ok = run(book, WRITE(digest, { UNITKA_CANCEL_RESTORE_ACCEPT_ANOMALIES: '438775617|2026-10-09|ANOMALY_ABSENT_WITH_LOGISTICS' }), rows);
    await expect(ok.promise).resolves.toMatchObject({ rowsLoaded: 2 });
    // аномалия, появившаяся ПОСЛЕ просмотра плана: отпечаток другой — запись невозможна
    const fresh = new Book();
    const { digest: d0 } = await planDigest([ROW_1810]);
    await expect(run(fresh, WRITE(d0, { UNITKA_CANCEL_RESTORE_ACCEPT_ANOMALIES: '438775617|2026-10-09|ANOMALY_ABSENT_WITH_LOGISTICS' }), rows).promise)
      .rejects.toMatchObject({ code: 'CANCEL_RESTORE_DIGEST_MISMATCH' });
    expect(fresh.structures).toHaveLength(0);
  });

  it('DRIFT: S изменён вручную, заметка владельца, формула W правлена — отказ ДО записи', async () => {
    const cases: Array<[string, (b: Book) => void, RegExp]> = [
      ['S', (b) => b.v.set(`794:${13 + OFFSET.cancels}`, 2), /S S794 = 2/],
      ['заметка', (b) => b.notes.set(noteKey(788, 277 + OFFSET.cancels), 'проверить с менеджером'), /заметка владельца/],
      ['W', (b) => b.f.set(`794:${13 + OFFSET.profitAll}`, '=Q794*AI794'), /не каноническая/],
    ];
    for (const [name, spoil, msg] of cases) {
      const book = new Book();
      spoil(book);
      const r = run(book, {});
      await expect(r.promise, name).rejects.toMatchObject({ code: 'CANCEL_RESTORE_DRIFT' });
      expect((planOf(r.events).refusals as string[]).join(' | '), name).toMatch(msg);
      expect(book.structures, name).toHaveLength(0);
    }
  });

  it('сутки внутри окна сверки не восстанавливаются (их ещё ведёт писатель)', async () => {
    const r = run(new Book(), {}, [ROW_2410], '2026-10-20');
    await expect(r.promise).rejects.toMatchObject({ code: 'CANCEL_RESTORE_DRIFT' });
    expect((planOf(r.events).refusals as string[])[0]).toMatch(/внутри окна сверки/);
  });
});

describe('жизненный цикл: IMMATURE → FINAL → восстановление → Store P&L → откат', () => {
  it('приёмка: −AI / +(AI+AF+REV), невязка модели 0 на каждом шаге, откат возвращает снимок до копейки', async () => {
    const book = new Book();
    const s0 = await storePnl(book);
    expect(s0.gapAbs).toBeLessThan(1e-9);

    // 1. IMMATURE: финотчёт ещё не покрывает горизонт — вью не просит менять лист, план пуст, записей нет
    const immature = [ROW_1810, ROW_2410].map((r) => ({ ...r, status: 'IMMATURE', sheetChangeRequired: false, finalCancels: r.legacyCancels, voidQty: 0 }));
    const i = run(book, {}, immature);
    await expect(i.promise).resolves.toMatchObject({ rowsLoaded: 0 });
    expect(planOf(i.events).cells).toBe(0);
    expect(book.structures).toHaveLength(0);

    // 2. FINAL → план → ACK (отпечаток) → запись
    const { digest, manifest } = await planDigest();
    await expect(run(book, WRITE(digest)).promise).resolves.toMatchObject({ rowsLoaded: 3 });
    const d1 = book.w(535580776) - (s0.row(535580776).profit), d2 = book.w(252442517) - (s0.row(252442517).profit);
    expect(d1).toBeCloseTo(-120, 9);                                          // −AI
    expect(d2).toBeCloseTo(40 + 55 + REV, 9);                                 // +(AI + AF + REV)

    // 3. Store P&L на восстановленном листе: V из формулы W, невязка 0, вклад месяца = Σ ΔW
    const s1 = await storePnl(book);
    expect(s1.row(535580776).void_cancels).toBe(1);
    expect(s1.row(252442517).cancels).toBe(0);
    expect(s1.gapAbs).toBeLessThan(1e-9);
    expect(s1.profit - s0.profit).toBeCloseTo(d1 + d2, 9);

    // 4. Откат по манифесту плана → снимок Store P&L равен исходному
    await expect(run(book, { UNITKA_CANCEL_RESTORE_DIRECTION: 'rollback', UNITKA_CANCEL_RESTORE_MANIFEST: manifest, ...WRITE(digest) }).promise).resolves.toMatchObject({ rowsLoaded: 3 });
    const s2 = await storePnl(book);
    expect(s2.p.rows).toEqual(s0.p.rows);
    expect(book.notes.get(noteKey(788, 277 + OFFSET.cancels))).toBe('');
    expect(book.structures).toHaveLength(2);                                  // запись и откат — по одному пакету
  });

  it('кандидат 27.10 / 952068582: S остаётся 1, поправка V = 1 ⇒ ΔW = +(AF+REV), только формула W и заметка', async () => {
    const cand: CancelClassRow = { ...ROW_1810, nmId: 952068582, date: '2026-10-27', funnelOrders: 1, keptQty: 0, room: 1, legacyCancels: 1, finalCancels: 1,
      voidQty: 1, nologInQ: 1, nologSrids: ['srid-cand-1'], nologCancelDates: ['2026-11-02'], matureOn: '2026-12-12' };
    const book = new Book();
    const w0 = book.w(952068582);
    const { digest } = await planDigest([cand]);
    await expect(run(book, WRITE(digest), [cand]).promise).resolves.toMatchObject({ rowsLoaded: 2 });
    expect(book.w(952068582) - w0).toBeCloseTo(57 + REV, 9);
    expect(book.notes.get(noteKey(797, 469 + OFFSET.cancels))!.startsWith(`${CANCEL_NOTE_MARKER} 2026-10-27: S 1 → 1`)).toBe(true);
    const s = await storePnl(book);
    expect([s.row(952068582).cancels, s.row(952068582).void_cancels, s.gapAbs < 1e-9]).toEqual([1, 1, true]);
    // пока кандидат незрел (до 12.12), вью его не отдаёт — план пуст
    const r = run(new Book(true), {}, [{ ...cand, status: 'IMMATURE', sheetChangeRequired: false, voidQty: 0, finalCancels: 1 }]);
    await expect(r.promise).resolves.toMatchObject({ rowsLoaded: 0 });
    expect(planOf(r.events).cells).toBe(0);
  });
});

function liveRow(nm: Nm, o: Partial<LiveRow> = {}): LiveRow {
  const l = LIVE[nm], r = ROWS[nm], s = BLOCK[nm];
  return { nmId: nm, date: l.date, month: '2026-10', row: r, blockStart: s, dateSerial: isoToSerial(l.date), s: l.s,
    wFormula: RU(profitAllFormula(s, r)), w: 0, note: '', q: l.q, x: l.x, ag: l.ag, y: l.y, ai: l.ai, af: l.af, ...o };
}

describe('манифест и перечитывание — чистые функции', () => {
  const live = () => new Map([['535580776|2026-10-18', liveRow(535580776)], ['252442517|2026-10-24', liveRow(252442517)]]);
  const plan = (rev = REV) => planCancelRestore({ rows: [ROW_1810, ROW_2410], live: live(), missing: [], windowFrom: '2026-11-09', reverseLegRate: rev, style: 'SEMICOLON' });
  const book = { spreadsheetId: 's', sheetId: 1, sheetName: 'n', style: 'SEMICOLON' as const, financeFinalThrough: '2026-12-13' };
  it('отпечаток не зависит от REVERSE_LEG_RATE (суточное обновление WY737 не ломает просмотренный план), но включает аномалии', () => {
    const a = buildCancelManifest(plan().cells, book);
    expect(buildCancelManifest(plan(31.5).cells, book).digest).toBe(a.digest);
    expect(buildCancelManifest(plan().cells, { ...book, anomalies: ['1|2026-10-09|ANOMALY_FUNNEL_BELOW_API'] }).digest).not.toBe(a.digest);
    const parsed = parseCancelManifest(encodeCancelManifest(a));
    expect('error' in parsed ? parsed.error : parsed.digest).toBe(a.digest);
    const tampered = JSON.parse(JSON.stringify(a)); tampered.cells[0].after.s = 5;
    expect(parseCancelManifest(JSON.stringify(tampered))).toEqual({ error: expect.stringMatching(/отпечаток/) });
  });
  it('перечитывание ловит каждое расхождение: S, формулу, заметку, W ≠ модели; состояние неизвестного исхода', () => {
    const cells = plan().cells;
    const after = (o: Partial<LiveRow> = {}) => {
      const c = cells[0]!;
      const l = liveRow(535580776, { s: c.after.s, wFormula: c.after.wFormula, note: c.after.note, ...o });
      return new Map([['535580776|2026-10-18', { ...l, w: o.w ?? modelW(l, 1, REV) }]]);
    };
    expect(verifyCancelCells([cells[0]!], after(), 'after', REV, 'SEMICOLON')).toEqual([]);
    expect(verifyCancelCells([cells[0]!], after({ s: 0 }), 'after', REV, 'SEMICOLON').join()).toMatch(/S 0 ≠ 1/);
    expect(verifyCancelCells([cells[0]!], after({ wFormula: RU(profitAllFormula(277, 788)) }), 'after', REV, 'SEMICOLON').join()).toMatch(/поправкой 1/);
    expect(verifyCancelCells([cells[0]!], after({ note: 'x' }), 'after', REV, 'SEMICOLON').join()).toMatch(/заметка/);
    expect(verifyCancelCells([cells[0]!], after({ w: 1 }), 'after', REV, 'SEMICOLON').join()).toMatch(/≠ модели/);
    expect(rollbackDrift([cells[0]!], after({ s: 7 }), 'SEMICOLON').join()).toMatch(/ни до/);
    expect(outcomeState([cells[0]!], after(), 'SEMICOLON')).toBe('ALL_AFTER');
    expect(outcomeState([cells[0]!], live(), 'SEMICOLON')).toBe('ALL_BEFORE');
    expect(outcomeState([cells[0]!], after({ wFormula: RU(profitAllFormula(277, 788)) }), 'SEMICOLON')).toBe('MIXED');
    expect(unacceptedAnomalies(['a', 'b'], [' a '])).toEqual(['b']);
  });
});

describe('слой фактов: поправка W вне загрузчика восстановления — отказ', () => {
  const base = { nm_id: 1, date_msk: { value: '2026-10-18' }, views: 0, opens: 0, carts: 0, orders: 3, cancels: 1, stock: 0, ads_in: 0, price: 640, storage: 0,
    orders_source: 'FUNNEL_API', cancels_source: 'PROXY_FACT_ORDERS', refusal_counted_qty: 0, refusal_evidence_status: null, refusal_proven_srids: null, refusal_proven_logistics_rub: null };
  const bq = (o: Record<string, unknown>) => new UnitkaBq({ projectId: 'p', query: async () => [{ ...base, ...o }] } as never, 'wb_mart', 'wb_ops', 'UNITKA_ENGINE_RUNS');
  it('cancel_void_qty > 0 в окне писателя — CANCEL_VOID_OUTSIDE_RESTORE; 0 и незрелый класс — как прежде', async () => {
    await expect(bq({ cancel_void_qty: 1, cancel_class_status: 'FINAL' }).facts()).rejects.toMatchObject({ code: 'CANCEL_VOID_OUTSIDE_RESTORE' });
    await expect(bq({ cancel_void_qty: 0, cancel_class_status: 'BOGUS' }).facts()).rejects.toMatchObject({ code: 'BQ_SHAPE' });
    const [f] = await bq({ cancel_void_qty: 0, cancel_class_status: 'IMMATURE' }).facts();
    expect([f!.cancels, f!.cancelVoidQty, f!.cancelClassStatus]).toEqual([1, 0, 'IMMATURE']);
  });
});

it('колонки снимка: void_cancels — последняя (Terraform)', () => {
  const tf = read('infra/terraform/unitka_store_pnl.tf');
  const schema = /store_pnl_snapshot_schema\s*=\s*\[([\s\S]*?)\n\s*\]/.exec(tf)![1]!;
  expect([...schema.matchAll(/name\s*=\s*"([a-z_]+)"/g)].map((m) => m[1]).at(-1)).toBe('void_cancels');
  expect(colA1(277 + OFFSET.cancels)).toBe('JW');
});
