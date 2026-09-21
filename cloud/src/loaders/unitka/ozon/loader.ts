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
import {
  resolveSections, activateNewSkus, rowsNeeded, sectionStep, NoFreeSkuSlotError,
  type SectionPlan,
} from './lifecycle.js';

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

/**
 * БАРЬЕР ГОТОВНОСТИ (Gate 9 §14). Суточный прогон не имеет права переписать свежие сутки
 * листа, если источник не обновился: получится «обновлено» поверх устаревшего, и владелец
 * увидит вчерашнюю картину с сегодняшней датой. Временного зазора мало — загрузка может
 * упасть (наблюдено: 1 сбой из 32 у ads_sku_daily, 1 из 59 у fbo_postings).
 *
 * Свежесть НЕ выводится из дат заказов: заказ мог просто не случиться. Доказательством
 * служит журнал загрузок — успешный прогон сущности за текущий цикл.
 */
export interface SourceFreshness {
  readonly entity: string; readonly lastOkMoscowDate: string | null; readonly stale: boolean;
}

export function assessFreshness(
  rows: ReadonlyArray<{ entity: string; last_ok_date: string | null }>,
  required: readonly string[], today: string, maxLagDays: number,
): SourceFreshness[] {
  const by = new Map(rows.map((r) => [r.entity, r.last_ok_date]));
  const limit = new Date(Date.parse(`${today}T00:00:00Z`) - maxLagDays * 86_400_000)
    .toISOString().slice(0, 10);
  return required.map((entity) => {
    const last = by.get(entity) ?? null;
    return { entity, lastOkMoscowDate: last, stale: last === null || last < limit };
  });
}

export class StaleSourceError extends Error {
  readonly code = 'SOURCE_STALE';
  constructor(readonly stale: readonly SourceFreshness[]) {
    super('источники не обновлены: '
      + stale.map((s) => `${s.entity} (последний успех ${s.lastOkMoscowDate ?? 'никогда'})`).join(', ')
      + '. Запись отменена: обновить лист устаревшими данными хуже, чем не обновить.');
    this.name = 'StaleSourceError';
  }
}

/** Сущности, без свежести которых Ozon-Юнитка писать не имеет права. */
export const OZON_UNITKA_REQUIRED_SOURCES: readonly string[] =
  ['finance_accrual', 'fbo_postings', 'ads_sku_daily', 'prices'];

/** Серийная дата Sheets → ISO. Книга хранит LAST_CLOSED_DATE числом. */
export function isoFromSheetValue(v: unknown): string {
  if (typeof v === 'number') return new Date(SHEET_EPOCH + v * 86_400_000).toISOString().slice(0, 10);
  const s = String(v ?? '').trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  const m = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(s);          // 20.09.2026 — вид книги в ru_RU
  return m ? `${m[3]}-${m[2]}-${m[1]}` : '';
}

async function readLcd(sheets: SheetsGateway, cell: string): Promise<string> {
  const [grid] = await sheets.readValues([cell]);
  return isoFromSheetValue(((grid ?? [])[0] ?? [])[0]);
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

  // LAST_CLOSED_DATE читается ИЗ КНИГИ, а не из окружения: владелец двигает её в ZZ_CONFIG,
  // и статическая переменная окружения устарела бы на следующий же день.
  const lcd = ctx.config.ozonUnitkaLastClosedDate
    || await readLcd(sheets, ctx.config.ozonUnitkaLcdCell);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(lcd)) {
    throw new LoaderError('OZON_UNITKA_LCD',
      `LAST_CLOSED_DATE не прочитана из ${ctx.config.ozonUnitkaLcdCell}: ${lcd || '(пусто)'}`);
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

  // первая активность SKU — из ВИТРИНЫ, а не из подписи в листе: подпись может содержать
  // опечатку (слот 14 подписан «9099514444» при каноническом «909951444»)
  const firstActivity: Record<string, string> = {};
  for (const r of await bq.query<{ offer_id: string; first_month: string }>(
    `SELECT m.offer_id, CAST(DATE_TRUNC(MIN(f.fact_date), MONTH) AS STRING) first_month
     FROM \`${ctx.config.projectId}.ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL\` f
     JOIN (SELECT DISTINCT offer_id, internal_sku
           FROM \`${ctx.config.projectId}.evetis_ref.REF_SKU_CHANNEL_MAP\`
           WHERE marketplace = 'OZON') m USING (internal_sku)
     WHERE f.gross_qty > 0 GROUP BY 1`)) {
    firstActivity[r.offer_id] = r.first_month.slice(0, 7);
  }
  // ── барьер готовности: источники обязаны быть свежими ДО любой записи ────────────────
  const fresh = assessFreshness(
    await bq.query<{ entity: string; last_ok_date: string | null }>(
      `SELECT entity, CAST(MAX(DATE(completed_at, 'Europe/Moscow')) AS STRING) last_ok_date
       FROM \`${ctx.config.projectId}.ozon_raw.OZON_INGESTION_RUNS\`
       WHERE status = 'OK' GROUP BY entity`),
    OZON_UNITKA_REQUIRED_SOURCES, today, ctx.config.ozonUnitkaMaxSourceLagDays);
  const stale = fresh.filter((f) => f.stale);
  ctx.logger.info('ozon-unitka: свежесть источников', {
    fresh: fresh.map((f) => `${f.entity}=${f.lastOkMoscowDate ?? 'никогда'}`).join(' '),
    stale: stale.length });
  if (stale.length && write) throw new StaleSourceError(stale);

  const facts = await bq.query<OzonFactRow>(
    ozonMonthFactsSql({ project: ctx.config.projectId, from: w.from, to: w.to }));
  const stockRows = await bq.query<{ d: string; offer_id: string; units: number }>(
    ozonProvenStockSql({ project: ctx.config.projectId, from: w.from, to: w.to }));
  const stock: Record<string, number> = {};
  for (const s of stockRows) stock[`${s.d}|${s.offer_id}`] = s.units;

  // эталон подписи и шапки — ПОСЛЕДНЯЯ существующая секция: у неё самый полный состав блоков,
  // и именно её оформление наследует новый месяц
  const refFrom = [...live].sort((x, y2) => y2.titleRow - x.titleRow)[0] as LiveSection;
  const [refRows] = await sheets.readValues(
    [`${q}!A${refFrom.titleRow}:${columnName(tailFirst - 1)}${refFrom.headerRow}`]);
  const refTitle = ((refRows ?? [])[0] ?? []) as unknown as CellValue[];
  const refHeader = ((refRows ?? [])[1] ?? []) as unknown as CellValue[];
  const refAnchor: Record<string, number> = {};
  refFrom.blocks.forEach((o, b) => {
    refAnchor[o] = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
  });

  // секции окна: существующие + недостающие, достроенные из геометрии последней.
  // Новый месяц и новый SKU появляются сами — ручной правки листа не требуется.
  let plans: SectionPlan[];
  try {
    plans = resolveSections({ live, windowMonths: [...months], firstActivity,
                              blockSlots: ctx.config.ozonUnitkaBlockSlots });
    plans = plans.map((p) => p.isNew ? p : {
      ...p, blocks: activateNewSkus({ current: p.blocks, monthKey: p.monthKey, firstActivity,
                                      blockSlots: ctx.config.ozonUnitkaBlockSlots }).blocks });
  } catch (e) {
    if (e instanceof NoFreeSkuSlotError) {
      // ёмкость исчерпана: молча переписать чужой блок нельзя — лист потеряет историю SKU
      ctx.logger.error('ozon-unitka: NO_FREE_SKU_SLOT', { message: e.message });
      throw new LoaderError('NO_FREE_SKU_SLOT', e.message);
    }
    throw e;
  }
  const created = plans.filter((p) => p.isNew);
  const activated = plans.flatMap((p) => {
    const was = live.find((l) => l.monthKey === p.monthKey);
    return was ? p.blocks.filter((b) => !was.blocks.includes(b)).map((b) => `${p.monthKey}:${b}`) : [];
  });
  if (created.length) ctx.logger.info('ozon-unitka: созданы секции', { months: created.map((c) => c.monthKey) });
  if (activated.length) ctx.logger.info('ozon-unitka: активированы SKU', { skus: activated });

  const monthDays = (k: string) => new Date(Date.UTC(Number(k.slice(0, 4)), Number(k.slice(5, 7)), 0)).getUTCDate();
  const built = plans.map((p, i) => {
    const y = Number(p.monthKey.slice(0, 4)); const mo = Number(p.monthKey.slice(5, 7));
    const prevPlan = plans[i - 1];
    const prevDays = prevPlan ? prevPlan.days
      : (live.filter((l) => l.titleRow < p.titleRow).sort((a, b) => b.titleRow - a.titleRow)[0]?.days.length ?? 0);
    const spec = ozonMonthSpec(y, mo, p.titleRow - prevDays - 4, prevDays, p.blocks);
    if (spec.titleRow !== p.titleRow) {
      throw new LoaderError('OZON_UNITKA_GEOMETRY',
        `секция ${p.monthKey}: расчётная строка ${spec.titleRow} против ожидаемой ${p.titleRow}`);
    }
    const from = ozonFromDayFor(p.monthKey, w);
    const first = `${p.monthKey}-01`;
    const last = `${p.monthKey}-${String(monthDays(p.monthKey)).padStart(2, '0')}`;
    const inSection = facts.filter((f) => f.d >= first && f.d <= last);
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
  for (const b of built) if (!allSections.some((x) => x.titleRow === b.spec.titleRow)) allSections.push(layoutOf(b.spec));
  allSections.sort((x, y2) => x.titleRow - y2.titleRow);
  const appendRowCount = rowsNeeded(plans, meta.rowCount);

  const plan = buildOzonPlan({
    sheetId: meta.sheetId, sheetName: name, allSections, sections: built,
    // геометрия суточным прогоном НЕ меняется: рост листа — дело миграционного гейта
    grid: { current: { rows: meta.rowCount, columns: meta.columnCount },
            // ширина листа суточным прогоном НЕ меняется: новые КОЛОНКИ — дело миграционного
            // гейта. Новые СТРОКИ дописываются: без них новый месяц некуда положить.
            growth: { widenBlockAt: [], insertColumnsBefore: tailFirst,
                      insertColumnCount: 0, appendColumnCount: 0, appendRowCount } },
    lcd, lcdMirror: null, lcdRef: ctx.config.ozonUnitkaLcdRef,
    blocks: ctx.config.ozonUnitkaBlockSlots,
    existingCfRules: ctx.config.ozonUnitkaExistingCfRules,
  });

  const cells = plan.values.reduce((n, v) => n + v.values.reduce((m, r) => m + r.length, 0), 0);
  const requests = plan.structure.length + plan.presentation.length + plan.conditional.length;
  const estimatedRows = built.reduce((n, b) => n + b.estimated, 0);
  ctx.logger.info('ozon-unitka: план собран', {
    window: `${w.from}..${w.to}`, days: w.days, deep: w.deep,
    sections: built.map((b) => b.spec.key).join(','), createdSections: created.length,
    activatedSkus: activated.length, appendRowCount, cells, requests, estimatedRows, write });

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
