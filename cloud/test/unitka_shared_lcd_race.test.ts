/**
 * SHARED_LCD_RACE_24_09_REGRESSION — обязательный production-контракт Gate 10.
 *
 * ИНЦИДЕНТ 24.09.2026 (production, pre-Gate-10). Расписания WB и Ozon стартуют в 10:00 МСК.
 * Ozon прочитал общий LAST_CLOSED_DATE (B2) = 22.09 в 07:00:55Z и записал окно по 22.09;
 * WB в 07:01:08Z сдвинул B2 на 23.09. Формулы листа Ozon ссылались на тот же B2, и 23.09 стал
 * на листе Ozon «закрытым» пустым днём (доходность 0 ₽), хотя писатель Ozon его не писал.
 *
 * КОНТРАКТ ПОСЛЕ GATE 10. У каждой платформы свой авторитет LCD; одновременный старт допустим;
 * порядок чтения/коммита одной платформы не влияет на LCD другой. Конечное состояние платформы
 * зависит ТОЛЬКО от её собственной готовности и её собственного LCD.
 *
 * Модель — настоящие функции цикла (resolveWbCandidate, resolveOzonCandidate, commitLcd,
 * WbLcdCell, OzonLcdCell, resolveOzonAuthority) над одной общей книгой; порядок шагов задан явно.
 * Контроль: в режиме LEGACY (до миграции) та же последовательность воспроизводит инцидент.
 */
import { describe, it, expect } from 'vitest';
import { resolveWbCandidate, WbLcdCell } from '../src/loaders/unitka/wb_lifecycle.js';
import { resolveOzonCandidate, resolveOzonAuthority, OzonLcdCell } from '../src/loaders/unitka/ozon/ozon_lifecycle.js';
import { commitLcd } from '../src/loaders/unitka/lcd.js';
import { isoToSerial, addDaysIso, type CellValue } from '../src/loaders/unitka/model.js';
import { dateCellToIso } from '../src/loaders/unitka/config_sheet.js';
import type { SheetsGateway, WriteRange } from '../src/loaders/unitka/sheets.js';
import type { UnitkaBq } from '../src/loaders/unitka/bq.js';
import { loadConfig } from '../src/config.js';
import type { Logger } from '../src/logging.js';

const silent = { info() {}, warn() {}, error() {}, debug() {}, child() { return silent; } } as unknown as Logger;
const config = loadConfig({ ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'proj', BQ_RAW_DATASET: 'wb_raw', GIT_SHA: 't' });
const WB_ANCHOR = 623;                                      // WY — якорь книги WB
const NOW = new Date('2026-09-24T07:00:05Z');               // 10:00:05 МСК, как 24.09

/** Общая книга: B2 (LAST_CLOSED_DATE), зеркало WB, B30 (OZON_LAST_CLOSED_DATE) и блок цикла. */
class SharedBook implements Partial<SheetsGateway> {
  cells = new Map<string, CellValue>();
  block: CellValue[][] = [];
  constructor(wbIso: string, ozonIso: string | null) {
    this.cells.set('LAST_CLOSED_DATE', isoToSerial(wbIso));
    if (ozonIso) {
      this.cells.set('OZON_LAST_CLOSED_DATE', isoToSerial(ozonIso));
      this.block = [['ЖИЗНЕННЫЙ ЦИКЛ — AUTO-LCD', ''], ['OZON_LAST_CLOSED_DATE', isoToSerial(ozonIso)], ['WB_LCD_MODE', 'AUTO'],
        ['WB_MANUAL_LCD', ''], ['OZON_LCD_MODE', 'AUTO'], ['OZON_MANUAL_LCD', '']];
    }
  }
  iso(name: string): string | null { return dateCellToIso(this.cells.get(name)); }
  async readValues(ranges: string[]): Promise<CellValue[][][]> {
    return ranges.map((r) => (r === 'ZZ_CONFIG!A29:B34' ? this.block.map((x) => [...x])
      : this.cells.has(r) ? [[this.cells.get(r)!]] : []));
  }
  async batchWrite(data: WriteRange[]): Promise<number> {
    for (const d of data) {
      this.cells.set(d.range, d.values[0]![0]!);
      if (d.range === 'OZON_LAST_CLOSED_DATE' && this.block[1]) this.block[1][1] = d.values[0]![0]!;
    }
    return data.length;
  }
}
const gw = (b: SharedBook): SheetsGateway => b as unknown as SheetsGateway;

/** Готовность WB: гейтящие загрузчики (funnel, mart) отработали по `readyTo` включительно. */
const wbBq = (readyTo: string): UnitkaBq => ({
  wbRunCoverage: async (_ds: string, since: string) => {
    const out: Array<{ loader: string; period: string }> = [];
    for (let d = addDaysIso(since, 1); d <= readyTo; d = addDaysIso(d, 1)) out.push({ loader: 'funnel', period: d }, { loader: 'mart', period: d });
    return out;
  },
}) as unknown as UnitkaBq;
/** Готовность Ozon: окна OZON_INGESTION_RUNS, прогон завершён утром следующего дня после `readyTo`. */
const ozonQuery = (readyTo: string) => async <T>(): Promise<T[]> =>
  ['finance_accrual', 'fbo_postings', 'ads_sku_daily'].map((entity) => ({
    entity, source_from: '2026-09-15', source_to: addDaysIso(readyTo, 1), completed_msk: addDaysIso(readyTo, 1),
  })) as T[];

interface Cycle { committed: string; candidate: string; willAdvance: boolean }
async function wbPlan(b: SharedBook, readyTo: string): Promise<Cycle> {
  const r = await resolveWbCandidate({ sheets: gw(b), bq: wbBq(readyTo), config, log: silent, canonical: { lastClosedDate: readyTo, d1Msk: '2026-09-23' } });
  return { committed: r.committed, candidate: r.candidate, willAdvance: r.candidate > r.committed };
}
async function ozonPlan(b: SharedBook, readyTo: string): Promise<Cycle> {
  const r = await resolveOzonCandidate({ sheets: gw(b), projectId: 'proj', log: silent, now: NOW, query: ozonQuery(readyTo) });
  return { committed: r.committed, candidate: r.candidate, willAdvance: r.decision.willAdvance };
}
const wbCommit = (b: SharedBook, c: Cycle) => commitLcd(new WbLcdCell(gw(b), 'WB_Юнит_2025', WB_ANCHOR), { expectedCommitted: c.committed, candidate: c.candidate });
const ozonCommit = (b: SharedBook, c: Cycle) => commitLcd(new OzonLcdCell(gw(b)), { expectedCommitted: c.committed, candidate: c.candidate });

/** День, который лист платформы считает закрытым: значение имени, на которое ссылаются её формулы. */
const ozonSheetClosed = (b: SharedBook, authorityCell: string) => b.iso(resolveOzonAuthority(authorityCell).lcdName);

describe('SHARED_LCD_RACE_24_09_REGRESSION', () => {
  for (const [ozonReady, wbReady] of [['2026-09-23', '2026-09-23'], ['2026-09-22', '2026-09-23'], ['2026-09-23', '2026-09-22']] as const) {
    const tag = `Ozon готов по ${ozonReady.slice(8)}.09, WB — по ${wbReady.slice(8)}.09`;

    it(`A: Ozon читает/кандидат → WB коммитит → Ozon завершает (${tag})`, async () => {
      const b = new SharedBook('2026-09-22', '2026-09-22');
      const oz = await ozonPlan(b, ozonReady);
      const wb = await wbPlan(b, wbReady);
      expect((await wbCommit(b, wb)).code).toBe(wb.willAdvance ? 'LCD_COMMITTED' : 'LCD_NOT_ADVANCED');
      expect((await ozonCommit(b, oz)).code).toBe(oz.willAdvance ? 'LCD_COMMITTED' : 'LCD_NOT_ADVANCED');
      expect(b.iso('OZON_LAST_CLOSED_DATE'), 'Ozon — только своя готовность').toBe(ozonReady);
      expect(b.iso('LAST_CLOSED_DATE'), 'WB — только своя готовность').toBe(wbReady);
      // лист Ozon считает закрытым ровно то, что писатель Ozon опубликовал и закоммитил
      expect(ozonSheetClosed(b, 'OZON_LAST_CLOSED_DATE')).toBe(oz.candidate);
    });

    it(`B: WB читает/кандидат → Ozon коммитит → WB завершает (${tag})`, async () => {
      const b = new SharedBook('2026-09-22', '2026-09-22');
      const wb = await wbPlan(b, wbReady);
      const oz = await ozonPlan(b, ozonReady);
      expect((await ozonCommit(b, oz)).code).toBe(oz.willAdvance ? 'LCD_COMMITTED' : 'LCD_NOT_ADVANCED');
      expect((await wbCommit(b, wb)).code).toBe(wb.willAdvance ? 'LCD_COMMITTED' : 'LCD_NOT_ADVANCED');
      expect(b.iso('OZON_LAST_CLOSED_DATE')).toBe(ozonReady);
      expect(b.iso('LAST_CLOSED_DATE')).toBe(wbReady);
      expect(ozonSheetClosed(b, 'OZON_LAST_CLOSED_DATE')).toBe(oz.candidate);
    });
  }

  it('полностью перемешанный порядок: оба читают, оба коммитят в обратном порядке — итог тот же', async () => {
    const b = new SharedBook('2026-09-22', '2026-09-22');
    const [oz, wb] = [await ozonPlan(b, '2026-09-22'), await wbPlan(b, '2026-09-23')];
    await ozonCommit(b, oz); await wbCommit(b, wb);
    expect([b.iso('LAST_CLOSED_DATE'), b.iso('OZON_LAST_CLOSED_DATE')]).toEqual(['2026-09-23', '2026-09-22']);
  });

  it('контроль: в режиме LEGACY (до миграции) порядок A воспроизводит инцидент 24.09 — пустой «закрытый» 23.09 у Ozon', async () => {
    const b = new SharedBook('2026-09-22', null);
    const ozonWroteThrough = b.iso('LAST_CLOSED_DATE');              // писатель LEGACY читает B2 в начале прогона
    const wb = await wbPlan(b, '2026-09-23');
    await wbCommit(b, wb);                                           // WB сдвигает общий B2 посреди прогона Ozon
    expect(ozonWroteThrough).toBe('2026-09-22');
    expect(ozonSheetClosed(b, 'ZZ_CONFIG!B2')).toBe('2026-09-23');   // лист Ozon считает закрытым день, которого не писал
  });
});
