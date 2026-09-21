/**
 * OZON UNITKA — ЕЖЕДНЕВНЫЙ ПРОГОН (Gate 8).
 *
 * Отдельный загрузчик, а не ветка внутри WB-загрузчика: домены Ozon и WB разделены жёстко,
 * и у них разные окна, разные источники и разные сроки прихода денег.
 *
 * Что делает прогон:
 *   1. читает ЖИВУЮ геометрию листа — секции, блоки, подписи, наблюдения корзины;
 *   2. берёт окно перезаписи (45 суток, раз в месяц — 120) и месяцы, которые оно пересекает;
 *   3. тянет факты ОПЕРАЦИОННОГО слоя: факт там, где Ozon уже опубликовал начисление,
 *      доказанная оценка там, где ещё нет;
 *   4. собирает канонический план и пишет его.
 *
 * Запись выключена по умолчанию (`OZON_UNITKA_WRITE_ENABLED`). Без неё прогон считает план
 * и возвращает его размер — этого достаточно для наблюдения и для проверки после деплоя.
 *
 * ЧЕГО ПРОГОН НЕ ДЕЛАЕТ: не меняет геометрию (рост колонок и строк — дело миграционного
 * гейта, не суточного прогона) и не трогает секции вне окна.
 */
import type { LoaderContext, LoaderResult } from '../../types.js';
import { LoaderError } from '../../../errors.js';
import { SheetsRest, type SheetsGateway } from '../sheets.js';
import { BqClient } from '../../../bq/client.js';
import { OZON_GEOMETRY } from './contract.js';
import { ozonMonthFactsSql, ozonProvenStockSql } from './bq.js';
import { ozonMonthSpec, composeMonth, type OzonFactRow, type CellValue } from './month.js';
import type { CellValue as SheetCell } from '../model.js';
import { buildOzonPlan, sectionFormulas } from './monthplan.js';
import { layoutOf, columnName, type SectionLayout } from './requests.js';
import {
  ozonRewriteWindow, ozonDeepWindow, ozonWindowMonths, ozonFromDayFor, isDeepReconciliationDay,
  type OzonRewriteWindow,
} from './window.js';

const MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август',
  'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'] as const;
const SHEET_EPOCH = Date.UTC(1899, 11, 30);

export interface OzonUnitkaDeps {
  makeSheets: (ctx: LoaderContext, readonly: boolean) => SheetsGateway;
  makeBq: (ctx: LoaderContext) => { query<T>(sql: string): Promise<T[]> };
  now: () => Date;
}
export const defaultOzonUnitkaDeps: OzonUnitkaDeps = {
  makeSheets: (ctx, ro) => new SheetsRest(ctx.config.unitkaSpreadsheetId, ro),
  makeBq: (ctx) => {
    const c = new BqClient(ctx.config.projectId, ctx.config.bqLocation);
    return { query: <T>(sql: string) => c.query<T>(sql) as Promise<T[]> };
  },
  now: () => new Date(),
};

/** Одна секция месяца, прочитанная из живого листа. */
export interface LiveSection {
  readonly titleRow: number; readonly headerRow: number;
  readonly blocks: string[]; readonly days: string[];
  readonly name: string; readonly monthKey: string;
}

const isoOfSerial = (n: number): string =>
  new Date(SHEET_EPOCH + n * 86_400_000).toISOString().slice(0, 10);

/**
 * Живая раскладка листа. Читается КАЖДЫЙ прогон: движок не имеет права полагаться на
 * запомненную геометрию — владелец мог добавить SKU или месяц между прогонами.
 */
export function parseLiveLayout(
  grid: readonly (readonly SheetCell[])[], tailFirstColumn: number,
  canonicalOffer: (token: string) => string | null,
): { sections: LiveSection[]; cart: Record<string, number> } {
  const { BLOCK_FIRST_COLUMN: BF, BLOCK_WIDTH: W } = OZON_GEOMETRY;
  const at = (r: number, c: number): SheetCell => {
    const row = grid[r - 1] ?? []; return (row[c - 1] ?? '') as SheetCell;
  };
  const sections: LiveSection[] = []; const cart: Record<string, number> = {};
  for (let r = 1; r <= grid.length; r++) {
    if (String(at(r, BF)).trim() !== 'Дата') continue;
    const titleRow = r - 1; const slots: Array<string | null> = [];
    for (let b = 0; b < 64; b++) {
      const c = BF + W * b; if (c >= tailFirstColumn) break;
      const txt = String(at(titleRow, c) ?? '').trim();
      slots.push(txt ? canonicalOffer(txt.split(/\s+/)[0] as string) : null);
    }
    const days: Array<{ row: number; iso: string }> = [];
    for (let i = r + 1; i <= grid.length; i++) {
      const v = at(i, BF);
      if (typeof v !== 'number') break;
      days.push({ row: i, iso: isoOfSerial(v) });
    }
    if (!days.length) continue;
    const name = String(at(titleRow, 1) || at(titleRow, 2) || '').trim();
    const mi = MONTHS.findIndex((m) => name.startsWith(m));
    const yr = /\d{4}/.exec(name)?.[0];
    sections.push({ titleRow, headerRow: r, blocks: slots.filter((x): x is string => !!x),
      days: days.map((d) => d.iso), name,
      monthKey: mi >= 0 && yr ? `${yr}-${String(mi + 1).padStart(2, '0')}` : '' });
    for (const { row, iso } of days) {
      slots.forEach((off, b) => {
        if (!off) return;
        const v = at(row, BF + W * b + 5);
        if (typeof v === 'number') cart[`${iso}|${off}`] = v;
      });
    }
  }
  return { sections, cart };
}

export interface OzonUnitkaPlanResult {
  readonly window: OzonRewriteWindow;
  readonly months: string[];
  readonly sections: string[];
  readonly cells: number;
  readonly requests: number;
  readonly estimatedRows: number;
  readonly written: boolean;
}

export async function ozonUnitkaLoader(
  ctx: LoaderContext, deps: OzonUnitkaDeps = defaultOzonUnitkaDeps,
): Promise<LoaderResult> {
  const write = ctx.config.ozonUnitkaWriteEnabled;
  const sheets = deps.makeSheets(ctx, !write);
  const bq = deps.makeBq(ctx);
  const name = ctx.config.ozonUnitkaSheetName;
  const meta = await sheets.readSheetMeta(name);
  const q = `'${name.replace(/'/g, "''")}'`;

  const lcd = ctx.config.ozonUnitkaLastClosedDate;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(lcd)) {
    throw new LoaderError('OZON_UNITKA_LCD', `LAST_CLOSED_DATE не задана: ${lcd || '(пусто)'}`);
  }
  const today = deps.now().toISOString().slice(0, 10);
  const w = isDeepReconciliationDay(today) ? ozonDeepWindow(lcd) : ozonRewriteWindow(lcd);
  const months = new Set(ozonWindowMonths(w));

  const tailFirst = ctx.config.ozonUnitkaTailFirstColumn;
  const [grid] = await sheets.readValues([`${q}!A1:${columnName(tailFirst - 1)}${meta.rowCount}`]);
  const canonSet = new Set(ctx.config.ozonUnitkaOffers);
  const alias = ctx.config.ozonUnitkaOfferAliases;
  const canonicalOffer = (t: string): string | null =>
    canonSet.has(t) ? t : (alias[t] ?? null);
  const { sections: live, cart } = parseLiveLayout(grid ?? [], tailFirst, canonicalOffer);
  if (!live.length) throw new LoaderError('OZON_UNITKA_LAYOUT', 'секций месяца в листе не найдено');

  const picked = live.filter((s) => months.has(s.monthKey));
  if (!picked.length) {
    throw new LoaderError('OZON_UNITKA_WINDOW',
      `окно ${w.from}..${w.to} не пересекает ни одной секции листа`);
  }
  const facts = await bq.query<OzonFactRow>(
    ozonMonthFactsSql({ project: ctx.config.projectId, from: w.from, to: w.to }));
  const stockRows = await bq.query<{ d: string; offer_id: string; units: number }>(
    ozonProvenStockSql({ project: ctx.config.projectId, from: w.from, to: w.to }));
  const stock: Record<string, number> = {};
  for (const s of stockRows) stock[`${s.d}|${s.offer_id}`] = s.units;

  const refFrom = live.find((s) => s.monthKey === [...months][0]) ?? (picked[0] as LiveSection);
  const [refRows] = await sheets.readValues(
    [`${q}!A${refFrom.titleRow}:${columnName(tailFirst - 1)}${refFrom.headerRow}`]);
  const refTitle = ((refRows ?? [])[0] ?? []) as unknown as CellValue[];
  const refHeader = ((refRows ?? [])[1] ?? []) as unknown as CellValue[];
  const refAnchor: Record<string, number> = {};
  refFrom.blocks.forEach((o, b) => {
    refAnchor[o] = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
  });

  const built = picked.map((s) => {
    const y = Number(s.monthKey.slice(0, 4)); const mo = Number(s.monthKey.slice(5, 7));
    const prev = live[live.indexOf(s) - 1];
    const prevDays = prev ? prev.days.length : 0;
    const spec = ozonMonthSpec(y, mo, s.titleRow - prevDays - 4, prevDays, s.blocks);
    if (spec.titleRow !== s.titleRow) {
      throw new LoaderError('OZON_UNITKA_GEOMETRY',
        `секция ${s.name}: расчётная строка ${spec.titleRow} против живой ${s.titleRow}`);
    }
    const from = ozonFromDayFor(s.monthKey, w);
    const inSection = facts.filter((f) => f.d >= (s.days[0] as string) && f.d <= (s.days[s.days.length - 1] as string));
    const comp = composeMonth(spec, inSection, stock, lcd, from, cart);
    return { spec, facts: inSection, stock, formulas: sectionFormulas(spec, comp),
             refTitle, refHeader, refAnchor, fromDay: from,
             estimated: comp.provenance.length };
  });

  const allSections: SectionLayout[] = live.map((s) => {
    const y = Number(s.monthKey.slice(0, 4)); const mo = Number(s.monthKey.slice(5, 7));
    const prev = live[live.indexOf(s) - 1]; const prevDays = prev ? prev.days.length : 0;
    return layoutOf(ozonMonthSpec(y, mo, s.titleRow - prevDays - 4, prevDays, s.blocks));
  });

  const plan = buildOzonPlan({
    sheetId: meta.sheetId, sheetName: name, allSections, sections: built,
    // геометрия суточным прогоном НЕ меняется: рост листа — дело миграционного гейта
    grid: { current: { rows: meta.rowCount, columns: meta.columnCount },
            growth: { widenBlockAt: [], insertColumnsBefore: tailFirst,
                      insertColumnCount: 0, appendColumnCount: 0, appendRowCount: 0 } },
    lcd, lcdMirror: null, lcdRef: ctx.config.ozonUnitkaLcdRef,
    blocks: ctx.config.ozonUnitkaBlockSlots,
    existingCfRules: ctx.config.ozonUnitkaExistingCfRules,
  });

  const cells = plan.values.reduce((n, v) => n + v.values.reduce((m, r) => m + r.length, 0), 0);
  const requests = plan.structure.length + plan.presentation.length + plan.conditional.length;
  const estimatedRows = built.reduce((n, b) => n + b.estimated, 0);
  ctx.logger.info('ozon-unitka: план собран', {
    window: `${w.from}..${w.to}`, days: w.days, deep: w.deep,
    sections: built.map((b) => b.spec.key).join(','), cells, requests, estimatedRows, write });

  if (!write) return { rowsFetched: facts.length, rowsLoaded: 0 };

  for (const group of [plan.structure, plan.conditional, plan.presentation]) {
    for (let i = 0; i < group.length; i += 400) {
      await sheets.structureWrite(group.slice(i, i + 400));
    }
  }
  const updated = await sheets.batchWrite(
    plan.values.map((v) => ({ range: v.range, values: v.values as unknown as SheetCell[][] })));
  ctx.logger.info('ozon-unitka: записано', { updated, estimatedRows });
  return { rowsFetched: facts.length, rowsLoaded: updated };
}
