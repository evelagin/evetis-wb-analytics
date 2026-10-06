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
import {
  resolveOzonAuthority, resolveOzonCandidate, deriveOzonGeometry, expandOzonCapacity, engineCfRuleCount, engineCfPrefix,
  writtenCells, readbackAndVerify, OzonLcdCell, type OzonCandidate,
} from './ozon_lifecycle.js';
import { commitLcd, revertLcd } from '../lcd.js';
import { logLifecycle } from '../lifecycle_log.js';
import { LoaderError } from '../../../errors.js';
import { SheetsRest, type SheetsGateway } from '../sheets.js';
import { BqClient } from '../../../bq/client.js';
import { OZON_GEOMETRY } from './contract.js';
import { ozonMonthFactsSql, ozonProvenStockSql, ozonSppEstimateSql, normalizeBqRow, type OzonSppEstimateRow } from './bq.js';
import { ozonMonthSpec, composeMonth, type OzonFactRow, type CellValue } from './month.js';
import type { CellValue as SheetCell } from '../model.js';
import { buildOzonPlan, sectionFormulas, OZON_WRITE_PHASES, type SppDayEvidence, type SppEstimate } from './monthplan.js';
import { OZON_OFFSET } from './offsets.js';
import { ozonSourceCompletenessIssues, ozonWrittenReader, verifyOzonModel } from './model_qa.js';
import { layoutOf, columnName, type SectionLayout } from './requests.js';
import {
  ozonRewriteWindow, ozonDeepWindow, ozonWindowMonths, ozonFromDayFor, isDeepReconciliationDay,
  type OzonRewriteWindow,
} from './window.js';
import {
  resolveSections, activateNewSkus, rowsNeeded, NoFreeSkuSlotError,
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
    // Нормализация — ЗДЕСЬ, на единственной границе с BigQuery, а не в сборщике месяца:
    // объекты Big и {value} не должны существовать нигде выше (см. bq.ts, «ГРАНИЦА ТИПОВ»).
    return { query: async <T>(sql: string) => (await c.query<Record<string, unknown>>(sql))
      .map((r) => normalizeBqRow<T>(r)) };
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
): { sections: LiveSection[]; cart: Record<string, number>;
     bloggers: Record<string, number>; externalAds: Record<string, CellValue>;
     sppEvidence: Record<string, SppDayEvidence> } {
  const { BLOCK_FIRST_COLUMN: BF, BLOCK_WIDTH: W } = OZON_GEOMETRY;
  const at = (r: number, c: number): SheetCell => {
    const row = grid[r - 1] ?? []; return (row[c - 1] ?? '') as SheetCell;
  };
  const sections: LiveSection[] = []; const cart: Record<string, number> = {};
  const bloggers: Record<string, number> = {}; const externalAds: Record<string, CellValue> = {};
  const sppEvidence: Record<string, SppDayEvidence> = {};
  for (let r = 1; r <= grid.length; r++) {
    if (String(at(r, BF)).trim() !== 'Дата') continue;
    const titleRow = r - 1; const slots: Array<string | null> = [];
    for (let b = 0; b < 64; b++) {
      const c = BF + W * b; if (c >= tailFirstColumn) break;
      const txt = String(at(titleRow, c) ?? '').trim();
      const offer = txt ? canonicalOffer(txt.split(/\s+/)[0] as string) : null;
      // Непустая подпись, не дающая offer_id, — отказ, а не тихое «сжатие» слотов: иначе все блоки
      // правее сдвинулись бы на 25 колонок, и запись (вместе с защищёнными колонками) легла бы в чужие блоки.
      if (txt && offer === null) {
        throw new LoaderError(`подпись блока «${txt.slice(0, 60)}» (строка ${titleRow}, слот ${b + 1}) не начинается с известного offer_id`, 'OZON_UNITKA_LABEL_UNRESOLVED');
      }
      slots.push(offer);
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
        const bq = at(row, BF + W * b + 1);
        if (typeof bq === 'number' && Number.isFinite(bq) && bq >= 0 && Number.isInteger(bq)) bloggers[`${iso}|${off}`] = bq;
        const ext = at(row, BF + W * b + 12);
        if (typeof ext === 'number' || (typeof ext === 'string' && ext !== '')) externalAds[`${iso}|${off}`] = ext;
        // Покрытие СПП месяца для дней ВНЕ окна перезаписи: значения, которые движок уже записал в лист.
        const n = (o: number): number => { const x = at(row, BF + W * b + o); return typeof x === 'number' && Number.isFinite(x) ? x : 0; };
        const price = at(row, BF + W * b + OZON_OFFSET.price), spp = at(row, BF + W * b + OZON_OFFSET.spp);
        const buyer = at(row, BF + W * b + OZON_OFFSET.priceSpp);
        // Phase 6: заказы, цена и «цена с СПП» — база ДРР месяца по суткам до окна перезаписи
        sppEvidence[`${iso}|${off}`] = { units: n(OZON_OFFSET.orders) - n(OZON_OFFSET.cancels), priced: typeof price === 'number' && price > 0,
          spp: typeof spp === 'number' && Number.isFinite(spp), orders: n(OZON_OFFSET.orders), price: n(OZON_OFFSET.price),
          ...(typeof buyer === 'number' && Number.isFinite(buyer) ? { buyer } : {}) };
      });
    }
  }
  return { sections, cart, bloggers, externalAds, sppEvidence };
}

/**
 * Хвосты подписей блоков из справочника — ТОЛЬКО для «голых» подписей (в эталонной секции стоит один
 * offer_id) и для новых блоков. Подпись, в которой владелец уже написал название, не трогается никогда:
 * она переносится дословно (buildHeaderRows). offer_id остаётся первым словом — по нему парсер находит блок.
 */
export function bareLabelTitles(blocks: readonly string[], refTitle: readonly CellValue[],
  refAnchor: Readonly<Record<string, number>>, shortNames: Readonly<Record<string, string>>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const o of blocks) {
    const name = shortNames[o];
    if (!name) continue;
    const src = refAnchor[o];
    const label = src === undefined ? '' : String(refTitle[src - 1] ?? '').trim();
    if (src === undefined || label === '' || label === o) out[o] = ` ${name}`;
  }
  return out;
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
  const log = ctx.logger;
  // якорь тарифов WB (REVERSE_LEG_RATE) листу Ozon не нужен: его тарифы живут в BigQuery
  let meta = await sheets.readSheetMeta(name, false);
  const q = `'${name.replace(/'/g, "''")}'`;

  // ── АВТОРИТЕТ LCD (Gate 10). LEGACY — до миграции: LCD читается из B2 (он принадлежит WB) и НЕ
  //    пишется никогда; OWN — собственный цикл на OZON_LAST_CLOSED_DATE с атомарным коммитом.
  const authority = resolveOzonAuthority(ctx.config.ozonUnitkaLcdCell);
  let cycle: OzonCandidate | null = null;
  let lcd: string;
  if (authority.kind === 'OWN') {
    if (ctx.config.ozonUnitkaLastClosedDate) {
      throw new LoaderError('OZON_UNITKA_LCD задан в окружении при собственном цикле: ручная дата задаётся режимом MANUAL в ZZ_CONFIG, а не переменной', 'OZON_LCD_ENV_OVERRIDE_FORBIDDEN');
    }
    cycle = await resolveOzonCandidate({ sheets, projectId: ctx.config.projectId, log, now: deps.now(), query: (sql) => bq.query(sql) });
    lcd = cycle.candidate;
  } else {
    // LAST_CLOSED_DATE читается ИЗ КНИГИ, а не из окружения.
    lcd = ctx.config.ozonUnitkaLastClosedDate || await readLcd(sheets, authority.read);
    logLifecycle(log, 'OZON', 'LCD_LEGACY_READONLY', { lcd, authority: authority.read, note: 'до миграции: LCD читается из B2 и не коммитится' });
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(lcd)) {
    throw new LoaderError(`LAST_CLOSED_DATE не прочитана из ${authority.read}: ${lcd || '(пусто)'}`, 'OZON_UNITKA_LCD');
  }
  const today = deps.now().toISOString().slice(0, 10);
  const w = isDeepReconciliationDay(today) ? ozonDeepWindow(lcd) : ozonRewriteWindow(lcd);
  const months = new Set(ozonWindowMonths(w));

  // ИДЕНТИЧНОСТЬ SKU — из справочника каналов, НЕ из подписи в листе и не из окружения.
  // Подпись — это текст, который владелец может опечатать (и опечатал: слот 14 подписан
  // «9099514444» при каноническом «909951444»). Единственная известная опечатка объявлена
  // явно в OZON_UNITKA_OFFER_ALIASES — остальное движок не угадывает.
  // Короткое название — из общего справочника продукта (evetis_ref, единственный общий слой WB/Ozon),
  // а не из маркетингового заголовка Ozon: тот длинный и меняется площадкой.
  const skuRef = await bq.query<{ offer_id: string; first_month: string | null; short_name: string | null; short_names: number | null }>(
    `WITH m AS (SELECT DISTINCT offer_id, internal_sku
                FROM \`${ctx.config.projectId}.evetis_ref.REF_SKU_CHANNEL_MAP\`
                WHERE marketplace = 'OZON'),
          a AS (SELECT internal_sku, MIN(fact_date) d
                FROM \`${ctx.config.projectId}.ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL\`
                WHERE gross_qty > 0 GROUP BY 1)
     SELECT m.offer_id, CAST(DATE_TRUNC(a.d, MONTH) AS STRING) first_month, p.short_name, p.short_names
     FROM m LEFT JOIN a USING (internal_sku)
     LEFT JOIN (SELECT internal_sku, MIN(product_name_short) short_name, COUNT(DISTINCT product_name_short) short_names
                FROM \`${ctx.config.projectId}.evetis_ref.REF_PRODUCT_MASTER\` GROUP BY 1) p USING (internal_sku)`);
  const shortNames: Record<string, string> = {};
  for (const r of skuRef) {
    // неоднозначное название (дубль internal_sku с разными именами) не выбирается порядком строк — подпись остаётся голой
    if (Number(r.short_names ?? 0) > 1) { log.warn('ozon_unitka_short_name_ambiguous', { offer_id: r.offer_id, names: r.short_names }); continue; }
    if (r.short_name && r.short_name.trim()) shortNames[r.offer_id] = r.short_name.trim();
  }
  const canonSet = new Set(skuRef.map((r) => r.offer_id));
  if (!canonSet.size) throw new LoaderError('справочник каналов Ozon пуст', 'OZON_UNITKA_REF');
  const firstActivity: Record<string, string> = {};
  for (const r of skuRef) if (r.first_month) firstActivity[r.offer_id] = r.first_month.slice(0, 7);

  // ── ГЕОМЕТРИЯ ИЗ ЛИСТА (Gate 10), а не из окружения: вставка колонок делает 562/22/$VA$2 ложью.
  let grid = (await sheets.readValues([`${q}!A1:${columnName(meta.columnCount)}${meta.rowCount}`]))[0] ?? [];
  let geo = deriveOzonGeometry({ grid: grid as SheetCell[][], columnCount: meta.columnCount,
    mirror: meta.namedRanges?.OZON_LCD_MIRROR ?? null, envTailFirst: ctx.config.ozonUnitkaTailFirstColumn });
  if (geo.notes.length) log.warn('ozon-unitka: геометрия', { notes: geo.notes });
  const alias = ctx.config.ozonUnitkaOfferAliases;
  const canonicalOffer = (t: string): string | null =>
    canonSet.has(t) ? t : (alias[t] ?? null);
  let { sections: live, cart, bloggers, externalAds, sppEvidence } = parseLiveLayout(grid as SheetCell[][], geo.tailFirst, canonicalOffer);
  if (!live.length) throw new LoaderError('секций месяца в листе не найдено', 'OZON_UNITKA_LAYOUT');

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
  const sourceIssues = ozonSourceCompletenessIssues(facts);
  if (sourceIssues.length) throw new LoaderError(JSON.stringify(sourceIssues.slice(0, 5)), 'OZON_MODEL_QA_FAILED');
  const stockRows = await bq.query<{ d: string; offer_id: string; units: number }>(
    ozonProvenStockSql({ project: ctx.config.projectId, from: w.from, to: w.to }));
  const stock: Record<string, number> = {};
  for (const s of stockRows) stock[`${s.d}|${s.offer_id}`] = s.units;
  // Phase 6: оценка СПП для ДРР — с первого дня месяца начала окна: сутки секции до окна входят в итог
  // ДРР месяца и в ДРР магазина, и их оценка обязана быть той же, что у суток окна (привязка к дате).
  // Оценка — улучшение ДРР, а не условие публикации: её сбой не останавливает прогон (ДРР остаётся фактической).
  let estRows: OzonSppEstimateRow[] = [];
  try {
    estRows = await bq.query<OzonSppEstimateRow>(
      ozonSppEstimateSql({ project: ctx.config.projectId, from: `${w.from.slice(0, 7)}-01`, to: w.to }));
  } catch (e) {
    log.warn('ozon_unitka_spp_estimate_unavailable', { reason: e instanceof Error ? e.message : String(e) });
  }
  const sppEstimate: Record<string, SppEstimate> = {};
  for (const e of estRows) {
    const pct = Number(e.spp_estimate_pct);
    if (Number.isFinite(pct) && (e.spp_estimate_level === 'SKU' || e.spp_estimate_level === 'SHOP')) {
      sppEstimate[`${e.d}|${e.offer_id}`] = { pct: Math.round(pct * 1e6) / 1e6, level: e.spp_estimate_level };
    }
  }

  // секции окна: существующие + недостающие, достроенные из геометрии последней.
  // Новый месяц и новый SKU появляются сами — ручной правки листа не требуется.
  const planSections = (capacity: number): SectionPlan[] => {
    const base = resolveSections({ live, windowMonths: [...months], firstActivity, blockSlots: capacity });
    return base.map((p) => p.isNew ? p : {
      ...p, blocks: activateNewSkus({ current: p.blocks, monthKey: p.monthKey, firstActivity, blockSlots: capacity }).blocks });
  };
  let plans: SectionPlan[];
  try {
    plans = planSections(geo.physicalSlots);
  } catch (e) {
    if (!(e instanceof NoFreeSkuSlotError)) throw e;
    // ── ДИНАМИЧЕСКАЯ ЁМКОСТЬ (Gate 10): свободного слота нет — лист расширяется ПЕРЕД хвостом.
    //    Без права записи — прежний безопасный отказ: переписать чужой блок нельзя.
    if (!write) {
      ctx.logger.error('ozon-unitka: NO_FREE_SKU_SLOT', { code: 'NO_FREE_SKU_SLOT', detail: e.message, note: 'в режиме записи лист был бы расширен' });
      throw new LoaderError(e.message, 'NO_FREE_SKU_SLOT');
    }
    const ex = await expandOzonCapacity({ sheets, sheetName: name, meta, geometry: geo, blocksNeeded: e.needed, log, envTailFirst: ctx.config.ozonUnitkaTailFirstColumn });
    meta = ex.meta; grid = ex.grid; geo = ex.geometry;                 // дальше — ТОЛЬКО новая геометрия
    ({ sections: live, cart, bloggers, externalAds, sppEvidence } = parseLiveLayout(grid as SheetCell[][], geo.tailFirst, canonicalOffer));
    plans = planSections(geo.physicalSlots);
  }
  const created = plans.filter((p) => p.isNew);
  const activated = plans.flatMap((p) => {
    const was = live.find((l) => l.monthKey === p.monthKey);
    return was ? p.blocks.filter((b) => !was.blocks.includes(b)).map((b) => `${p.monthKey}:${b}`) : [];
  });
  if (created.length) logLifecycle(log, 'OZON', 'NEW_MONTH_CREATED', { months: created.map((c) => c.monthKey), write });
  if (activated.length) logLifecycle(log, 'OZON', 'NEW_SKU_ACTIVATED', { skus: activated, write });

  // эталон подписи и шапки — ПОСЛЕДНЯЯ существующая секция: у неё самый полный состав блоков,
  // и именно её оформление наследует новый месяц
  const refFrom = [...live].sort((x, y2) => y2.titleRow - x.titleRow)[0] as LiveSection;
  const [refRows] = await sheets.readValues(
    [`${q}!A${refFrom.titleRow}:${columnName(geo.tailFirst - 1)}${refFrom.headerRow}`]);
  const refTitle = ((refRows ?? [])[0] ?? []) as unknown as CellValue[];
  const refHeader = ((refRows ?? [])[1] ?? []) as unknown as CellValue[];
  const refAnchor: Record<string, number> = {};
  refFrom.blocks.forEach((o, b) => {
    refAnchor[o] = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
  });

  const monthDays = (k: string) => new Date(Date.UTC(Number(k.slice(0, 4)), Number(k.slice(5, 7)), 0)).getUTCDate();
  const built = plans.map((p, i) => {
    const y = Number(p.monthKey.slice(0, 4)); const mo = Number(p.monthKey.slice(5, 7));
    const prevPlan = plans[i - 1];
    const prevDays = prevPlan ? prevPlan.days
      : (live.filter((l) => l.titleRow < p.titleRow).sort((a, b) => b.titleRow - a.titleRow)[0]?.days.length ?? 0);
    const spec = ozonMonthSpec(y, mo, p.titleRow - prevDays - 4, prevDays, p.blocks);
    if (spec.titleRow !== p.titleRow) {
      throw new LoaderError(`секция ${p.monthKey}: расчётная строка ${spec.titleRow} против ожидаемой ${p.titleRow}`, 'OZON_UNITKA_GEOMETRY');
    }
    const from = ozonFromDayFor(p.monthKey, w);
    const first = `${p.monthKey}-01`;
    const last = `${p.monthKey}-${String(monthDays(p.monthKey)).padStart(2, '0')}`;
    const inSection = facts.filter((f) => f.d >= first && f.d <= last);
    const sheetInputs = { bloggers, externalAds };
    const comp = composeMonth(spec, inSection, stock, lcd, from, cart, sheetInputs);
    const drr = { lcd, from, estimate: sppEstimate, evidence: sppEvidence, bloggers };
    return { spec, facts: inSection, stock, cart, sheetInputs, sppEvidence, sppEstimate,
             formulas: sectionFormulas(spec, comp, 'SEMICOLON', authority.lcdName, drr),
             refTitle, refHeader, refAnchor, fromDay: from, skuTitles: bareLabelTitles(spec.blocks, refTitle, refAnchor, shortNames),
             estimated: comp.provenance.length };
  });

  const liveLayouts: SectionLayout[] = live.map((s) => {
    const y = Number(s.monthKey.slice(0, 4)); const mo = Number(s.monthKey.slice(5, 7));
    const prev = live[live.indexOf(s) - 1]; const prevDays = prev ? prev.days.length : 0;
    return layoutOf(ozonMonthSpec(y, mo, s.titleRow - prevDays - 4, prevDays, s.blocks));
  });
  // Раскладка ПОСЛЕ записи: секции, которые прогон переписывает, берутся из ПЛАНА, а не из листа.
  // Иначе блок, активированный внутри существующей секции, не получил бы правил УФ, а следующий
  // прогон, увидев его в листе, насчитал бы правил больше, чем их есть.
  const builtByRow = new Map(built.map((b) => [b.spec.titleRow, layoutOf(b.spec)]));
  const allSections: SectionLayout[] = liveLayouts.map((x) => builtByRow.get(x.titleRow) ?? x);
  for (const [row, lay] of builtByRow) if (!allSections.some((x) => x.titleRow === row)) allSections.push(lay);
  allSections.sort((x, y2) => x.titleRow - y2.titleRow);
  const appendRowCount = rowsNeeded(plans, meta.rowCount);

  // Правила УФ, которые ЭТОТ движок сейчас держит в листе, — тем же генератором из ЖИВОЙ раскладки.
  // Константа 434 из окружения верна только при 17 секциях × 22 блока: с первым же новым месяцем
  // (01.10.2026) удаление по ней оставляло бы 23 правила в сутки.
  const lcdRef = geo.lcdRef ?? ctx.config.ozonUnitkaLcdRef;
  const structure = await sheets.readSheetStructure(name, meta.rowCount, meta.columnCount);
  const existingCfRules = engineCfPrefix(structure.conditionalFormats, geo.tailFirst);
  const derivedCf = engineCfRuleCount(meta.sheetId, liveLayouts, lcdRef);
  if (existingCfRules !== derivedCf || existingCfRules !== ctx.config.ozonUnitkaExistingCfRules) {
    log.warn('ozon-unitka: правила УФ', { in_sheet_engine_prefix: existingCfRules, generator_for_live_layout: derivedCf,
      env: ctx.config.ozonUnitkaExistingCfRules, total_in_sheet: structure.conditionalFormats.length,
      note: 'удаляется ФАКТИЧЕСКИЙ префикс правил движка; OZON_UNITKA_EXISTING_CF_RULES больше не авторитет' });
  }

  const plan = buildOzonPlan({
    sheetId: meta.sheetId, sheetName: name, allSections, sections: built,
    // ширина листа суточным прогоном меняется ТОЛЬКО расширением ёмкости выше (с проверкой хвоста).
    // Новые СТРОКИ дописываются: без них новый месяц некуда положить.
    grid: { current: { rows: meta.rowCount, columns: meta.columnCount },
            growth: { widenBlockAt: [], insertColumnsBefore: geo.tailFirst,
                      insertColumnCount: 0, appendColumnCount: 0, appendRowCount } },
    lcd, lcdMirror: null, lcdRef,
    blocks: geo.physicalSlots,
    existingCfRules,
  });

  const cells = plan.values.reduce((n, v) => n + v.values.reduce((m, r) => m + r.length, 0), 0);
  const modelIssues = verifyOzonModel({ sections: built, lcd, read: ozonWrittenReader(plan.values), mode: 'PLAN' });
  if (modelIssues.length) throw new LoaderError(JSON.stringify(modelIssues.slice(0, 5)), 'OZON_MODEL_QA_FAILED');
  const requests = plan.structure.length + plan.presentation.length + plan.conditional.length;
  const estimatedRows = built.reduce((n, b) => n + b.estimated, 0);
  ctx.logger.info('ozon-unitka: план собран', {
    window: `${w.from}..${w.to}`, days: w.days, deep: w.deep,
    sections: built.map((b) => b.spec.key).join(','), createdSections: created.length, sppEstimates: estRows.length,
    activatedSkus: activated.length, appendRowCount, cells, requests, estimatedRows, write,
    lcd_authority: authority.kind, committed_lcd: cycle?.committed ?? lcd, candidate_lcd: lcd,
    slots: geo.physicalSlots, tail_first: geo.tailFirst, cf_rules_existing: existingCfRules });

  if (!write) return { rowsFetched: facts.length, rowsLoaded: 0 };

  const send = async (group: readonly unknown[]): Promise<void> => {
    for (let i = 0; i < group.length; i += 400) {
      await sheets.structureWrite(group.slice(i, i + 400) as never);
    }
  };
  // ПОРЯДОК ФАЗ — не стилистика, а условие правильного листа (см. OZON_WRITE_PHASES).
  let updated = 0;
  for (const phase of OZON_WRITE_PHASES) {
    if (phase === 'values') {
      // USER_ENTERED, а не RAW: в плане Ozon формулы едут ВМЕСТЕ со значениями, и под RAW
      // они лягут в лист текстом «=IF(…)» вместо формулы. Числа уходят JSON-числами и по
      // локали не разбираются, режим безопасен для величин. WB это не касается: там
      // значение по умолчанию прежнее, RAW.
      updated = await sheets.batchWrite(
        plan.values.map((v) => ({ range: v.range, values: v.values as unknown as SheetCell[][] })),
        'USER_ENTERED');
    } else {
      await send(plan[phase]);
    }
  }
  ctx.logger.info('ozon-unitka: записано', { updated, estimatedRows });

  // ── ПЕРЕЧИТЫВАНИЕ И ЦЕЛОСТНОСТЬ (Gate 10). До Gate 10 их не было вовсе. До коммита сводка
  //    сверяется по дням ≤ закоммиченного LCD: формулы дня-кандидата честно пусты до коммита.
  const written = writtenCells(plan.values);
  const verifySections = built.map((b) => ({ titleRow: b.spec.titleRow, firstRow: b.spec.firstRow, lastRow: b.spec.lastRow, mtdRow: b.spec.mtdRow, blockCount: b.spec.blocks.length }));
  const committedLcd = cycle?.committed ?? lcd;
  // Барьер compare-before-commit — ПЕРВЫМ, до сверки сводки: если авторитет LCD изменили, пока
  // писались данные, сводка «разъедется» по чужой дате, и отказ получил бы неверное объяснение.
  if (cycle) {
    const now = await new OzonLcdCell(sheets).read();
    if (now !== cycle.committed) {
      logLifecycle(log, 'OZON', 'LCD_COMMIT_CONFLICT', { code: 'LCD_COMMIT_CONFLICT', stage: 'PRE_COMMIT', committed: cycle.committed, book_now: now, candidate: cycle.candidate }, 'error');
      throw new LoaderError(`OZON_LAST_CLOSED_DATE в книге ${now ?? '(пусто)'} ≠ ожидаемому ${cycle.committed}: изменён во время записи — не перетираем`, 'LCD_COMMIT_CONFLICT');
    }
  }
  const pre = await readbackAndVerify({ sheets, sheetName: name, written, sections: verifySections, tailFirst: geo.tailFirst, summaryUpTo: committedLcd,
    model: { sections: built, lcd } });
  ctx.logger.info('ozon-unitka: проверка', { phase: 'PRE_COMMIT', checks: pre.map((c) => `${c.name}:${c.pass ? 'PASS' : `FAIL(${c.count})`}`) });
  const preFailed = pre.filter((c) => !c.pass);
  if (preFailed.length) {
    logLifecycle(log, 'OZON', 'INTEGRITY_FAILED', { code: 'OZON_PUBLICATION_VERIFY_FAILED', phase: 'PRE_COMMIT', failed: preFailed.map((c) => ({ name: c.name, sample: c.sample.slice(0, 3) })) }, 'error');
    throw new LoaderError(`проверка после записи: ${preFailed.map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ')}`, 'OZON_PUBLICATION_VERIFY_FAILED');
  }

  // ── АТОМАРНЫЙ КОММИТ LCD — только в собственном цикле и только после проверки.
  if (cycle) {
    const cell = new OzonLcdCell(sheets);
    const commit = await commitLcd(cell, { expectedCommitted: cycle.committed, candidate: cycle.candidate });
    if (commit.code === 'LCD_COMMIT_CONFLICT' || commit.code === 'LCD_WRITE_FAILED') {
      logLifecycle(log, 'OZON', commit.code, { code: commit.code, committed: cycle.committed, candidate: cycle.candidate, book_after: commit.bookAfter, detail: commit.message }, 'error');
      throw new LoaderError(commit.message, commit.code);
    }
    if (commit.code === 'LCD_COMMITTED') {
      const post = await readbackAndVerify({ sheets, sheetName: name, written, sections: verifySections, tailFirst: geo.tailFirst, summaryUpTo: cycle.candidate,
        model: { sections: built, lcd } });
      const mirror = meta.namedRanges?.OZON_LCD_MIRROR;
      if (mirror) {
        const [mv] = await sheets.readValues([`${q}!${columnName(mirror.col)}${mirror.row}`]);
        const want = Math.round((Date.parse(`${cycle.candidate}T00:00:00Z`) - Date.UTC(1899, 11, 30)) / 86_400_000);
        const got = mv?.[0]?.[0];
        post.push({ name: 'LCD_MIRROR', pass: got === want, count: got === want ? 0 : 1, sample: got === want ? [] : [`зеркало ${String(got)} ≠ ${want}`] });
      }
      ctx.logger.info('ozon-unitka: проверка', { phase: 'POST_COMMIT', checks: post.map((c) => `${c.name}:${c.pass ? 'PASS' : `FAIL(${c.count})`}`) });
      const postFailed = post.filter((c) => !c.pass);
      if (postFailed.length) {
        const rv = await revertLcd(cell, { committed: cycle.committed, from: cycle.candidate });
        logLifecycle(log, 'OZON', 'LCD_REVERTED', { code: 'POST_COMMIT_QA_FAILED', revert: rv.code, book_after: rv.bookAfter, failed: postFailed.map((c) => c.name) }, 'error');
        throw new LoaderError(`проверка после коммита LCD: ${postFailed.map((c) => `${c.name}: ${c.sample.slice(0, 3).join('; ')}`).join(' | ')} (откат: ${rv.code})`,
          rv.code === 'REVERTED' ? 'POST_COMMIT_QA_FAILED' : 'POST_COMMIT_REVERT_FAILED');
      }
      logLifecycle(log, 'OZON', 'LCD_COMMITTED', { committed_before: cycle.committed, lcd_after: commit.bookAfter, mode: cycle.mode });
    } else {
      logLifecycle(log, 'OZON', 'LCD_NOT_ADVANCED', { committed: cycle.committed, notes: cycle.decision.notes });
    }
  }
  return { rowsFetched: facts.length, rowsLoaded: updated };
}
