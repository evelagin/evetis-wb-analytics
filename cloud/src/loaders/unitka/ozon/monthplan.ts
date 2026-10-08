/**
 * OZON UNITKA — сборка полного плана записи (Gate 5E).
 *
 * Один вход → один план. План детерминирован: те же входные данные дают тот же план,
 * поэтому повторный прогон ничего не меняет. Ни одной ссылки «скопируй с такой-то строки»:
 * оформление берётся из CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT.
 */
import { OZON_SUMMARY_ROLES } from './presentation.js';
import {
  composeMonth, buildGrid, buildHeaderRows, ozonMonthSpec,
  type OzonMonthSpec, type OzonFactRow, type CellValue,
  type OzonSheetInputs, type OzonMonthComposition, type OzonDayCell,
} from './month.js';
import { OZON_OFFSET } from './offsets.js';
import {
  rowHeightRequests, mtdBandRequests, cfAllRequests, futureDayRequests, columnName,
  layoutOf, type SheetsRequest, type SectionLayout,
} from './requests.js';
import {
  staticFormatRequests, observedStockFormatRequests, columnWidthRequests, titleMergeRequests,
  growGridRequests, gridAfterGrowth, lcdMirrorRequestsIdempotent, clearConditionalFormatRequests,
  unmergeRequests, residualMergeRequests, clearProvenanceNoteRequests, provenanceNoteRequests,
  provenanceNoteText, type GridGrowth, type ProvenanceNote,
} from './structure.js';

/**
 * Наблюдение СПП дня в листе (для покрытия СПП месяца по дням вне окна перезаписи).
 * Phase 6: `orders` (заказы, P), `price` (цена, Z) и `buyer` («цена с СПП», AB) — чтобы сутки до окна
 * вошли в базу ДРР месяца: оценённую (нет СПП) и фактическую (доля оценки в заметке).
 */
export interface SppDayEvidence {
  readonly units: number; readonly priced: boolean; readonly spp: boolean;
  readonly orders?: number; readonly price?: number; readonly buyer?: number;
}

/** Оценка СПП суток × SKU (bq.ts `ozonSppEstimateSql`, E1m5), ключ `iso|offer_id`. */
export interface SppEstimate { readonly pct: number; readonly level: 'SKU' | 'SHOP' }

/** Сутки × SKU, у которых знаменатель ДРР собран по ОЦЕНКЕ СПП, а не по факту. */
export interface DrrEstimatedDay {
  readonly offerId: string; readonly day: number; readonly row: number;
  /** (заказы − блогеры) × цена × (1 − оценка СПП), ₽. */
  readonly basis: number;
  readonly est: SppEstimate;
}

/** Фактическая база ДРР суток (заказы − блогеры) × «цена с СПП» — для доли оценки в заметке. */
export interface DrrActualDay { readonly offerId: string; readonly day: number; readonly basis: number }

const r2 = (x: number): number => Math.round(x * 100) / 100;
const r6e = (x: number): number => Math.round(x * 1e6) / 1e6;

/**
 * Базы ДРР закрытых суток секции. Сутки окна — из собранного месяца (comp), сутки до окна — из того,
 * что движок уже записал в лист (`live`) и блогеров листа. Оценка применяется ТОЛЬКО там, где у суток
 * нет фактической СПП, есть цена продавца и есть оценка; цена продавца знаменателем не становится никогда.
 */
export function ozonDrrBases(spec: OzonMonthSpec, comp: { cells: OzonMonthComposition['cells'] }, lcd: string | null,
  from: number, estimate: Readonly<Record<string, SppEstimate>> = {},
  live: Readonly<Record<string, SppDayEvidence>> = {}, bloggers: Readonly<Record<string, number>> = {},
): { estimated: DrrEstimatedDay[]; actual: DrrActualDay[] } {
  const estimated: DrrEstimatedDay[] = []; const actual: DrrActualDay[] = [];
  for (let i = 0; i < spec.days; i++) {
    const day = i + 1, row = spec.firstRow + i;
    const iso = `${spec.key}-${String(day).padStart(2, '0')}`;
    if (lcd !== null && iso > lcd) break;
    // Легаси-сутки до эпохи перезаписи (01–16.04.2026) заморожены: оценкой не дополняются никогда.
    if (iso < OZON_REWRITE_EPOCH) continue;
    for (const o of spec.blocks) {
      const key = `${iso}|${o}`;
      let orders: number, price: number, has: boolean, buyer: number | undefined, bl: number;
      if (day >= from) {
        const c = comp.cells[`${o}|${row}`] as Partial<OzonDayCell> | undefined;
        orders = c?.orders ?? 0; price = c?.price ?? 0; has = c?.spp !== undefined; bl = c?.bloggers ?? 0;
        buyer = has && price > 0 ? price - price * (c!.spp as number) / 100 : undefined;
      } else {
        const e = live[key];
        orders = e?.orders ?? 0; price = e?.price ?? 0; has = e?.spp ?? false; buyer = e?.buyer; bl = bloggers[key] ?? 0;
      }
      if (orders <= 0 || !(price > 0)) continue;
      if (has) { if (buyer !== undefined) actual.push({ offerId: o, day, basis: (orders - bl) * buyer }); continue; }
      const est = estimate[key];
      if (!est) continue;
      const basis = (orders - bl) * price * (1 - est.pct / 100);
      if (basis !== 0) estimated.push({ offerId: o, day, row, basis, est });
    }
  }
  return { estimated, actual };
}

/**
 * Phase B: управляемая строка заметки «Реклама внутренняя». Значение ячейки = CPC-атрибуция + «Оплата за
 * заказ»; разложение — этой строкой. Заметка владельца сохраняется ДОСЛОВНО: движок снимает и ставит только
 * свои строки (узнаёт их по форме), чужой текст не трогает.
 */
export const ADS_SPLIT_LINE = /^CPC \/ attributed Ads: -?[\d ]+,\d{2} \/ Оплата за заказ: -?[\d ]+,\d{2} \/ Итого: -?[\d ]+,\d{2}$/;
const rub2 = (x: number): string => (Math.round(x * 100) / 100).toFixed(2).replace('.', ',');
export function adsSplitLine(cpc: number, cpo: number): string {
  return `CPC / attributed Ads: ${rub2(cpc)} / Оплата за заказ: ${rub2(cpo)} / Итого: ${rub2(cpc + cpo)}`;
}
/** Заметка после замены управляемой строки. line = null — управляемую строку снять. */
export function mergeAdsNote(before: string, line: string | null): string {
  const lines = before.split('\n');
  const kept = lines.filter((l) => !ADS_SPLIT_LINE.test(l));
  if (kept.length === lines.length && line === null) return before;   // своей строки не было — ничего не меняем
  const owner = kept.length === lines.length ? before : kept.join('\n');
  return line === null ? owner : owner === '' ? line : `${owner}\n${line}`;
}

/** Текст заметки дневной ДРР SKU по оценке. */
export function drrDayEstimateNote(est: SppEstimate): string {
  return `ESTIMATED: цена покупателя оценена по СПП ${r6e(est.pct).toFixed(1)} % (${est.level === 'SKU' ? 'SKU 30 дн' : 'магазин 30 дн'}), `
    + 'заменится фактом после финансовой пары';
}

/** Текст заметки итоговой ДРР (MTD SKU, K): доля оценки в знаменателе; '' — оценки нет, заметку снять. */
export function drrShareNote(est: number, act: number): string {
  if (!(est > 0)) return '';
  const share = Math.round(est / (est + Math.max(0, act)) * 1000) / 10;
  return `ESTIMATED: ${share.toFixed(1)} % базы — оценка СПП (сутки без финансовой пары); заменится фактом автоматически`;
}

/**
 * Покрытие СПП месяца по блоку: единицы закрытых суток с ценой продавца, из них — с доказанной ценой
 * покупателя. Дни окна — из собранного месяца (comp), дни до окна — из того, что движок уже записал в лист.
 */
export function sppMonthCoverage(spec: OzonMonthSpec, comp: OzonMonthComposition, lcd: string, from: number,
  offerId: string, live: Readonly<Record<string, SppDayEvidence>> = {}): { units: number; covered: number } {
  let units = 0, covered = 0;
  for (let i = 0; i < spec.days; i++) {
    const iso = `${spec.key}-${String(i + 1).padStart(2, '0')}`;
    if (iso > lcd) break;
    let u: number, priced: boolean, has: boolean;
    if (i + 1 >= from) {
      const c = comp.cells[`${offerId}|${spec.firstRow + i}`] as Partial<OzonDayCell> | undefined;
      u = (c?.orders ?? 0) - (c?.cancel ?? 0); priced = (c?.price ?? 0) > 0; has = c?.spp !== undefined;
    } else {
      const e = live[`${iso}|${offerId}`];
      u = e?.units ?? 0; priced = e?.priced ?? false; has = e?.spp ?? false;
    }
    if (u <= 0 || !priced) continue;
    units += u;
    if (has) covered += u;
  }
  return { units, covered };
}

/** Текст заметки на ячейке СПП месяца; пустая строка — заметку снять (полное покрытие или нет единиц). */
export function sppCoverageNote(cov: { units: number; covered: number }): string {
  if (cov.units <= 0 || cov.covered === cov.units) return '';
  if (cov.covered === 0) return `СПП месяца не рассчитана: ни у одной из ${cov.units} ед. нет доказанной цены покупателя (финансовой пары). Цена продавца её не заменяет.`;
  const pct = Math.round(cov.covered / cov.units * 1000) / 10;
  return `ЧАСТИЧНО: СПП месяца рассчитана по ${cov.covered} из ${cov.units} ед. (${pct} %). У остальных ${cov.units - cov.covered} ед. цена покупателя ещё не доказана — значение не описывает весь месяц.`;
}

export interface OzonSectionInput {
  readonly spec: OzonMonthSpec;
  /** Покрытие СПП дней до окна перезаписи, ключ `iso|offer_id` (из живого листа). */
  readonly sppEvidence?: Readonly<Record<string, SppDayEvidence>>;
  /** Phase 6: оценка СПП для ДРР, ключ `iso|offer_id`. */
  readonly sppEstimate?: Readonly<Record<string, SppEstimate>>;
  readonly facts: readonly OzonFactRow[];
  /** Доказанные снимки остатка, ключ `iso|offer_id`. */
  readonly stock: Readonly<Record<string, number>>;
  readonly cart?: Readonly<Record<string, number>>;
  readonly sheetInputs?: OzonSheetInputs;
  /** Формулы секции от адаптера формул (ключ `строка:колонка`). */
  readonly formulas: { day: Record<string, string>; mtd: Record<string, string>; mtdBlank: readonly string[] };
  /** Заголовок и шапка секции-эталона + её якоря блоков. */
  readonly refTitle: readonly CellValue[];
  readonly refHeader: readonly CellValue[];
  readonly refAnchor: Readonly<Record<string, number>>;
  /** offer_id → ХВОСТ подписи дословно (с разделителем): подпись = offer_id + хвост. */
  readonly skuTitles?: Readonly<Record<string, string>>;
  /**
   * Апрель 2026 — гибридный месяц миграции: 01–16.04 посчитаны легаси-моделью и остаются
   * как есть, канонически переписаны только 17–30.04. Движок обязан знать об этом явно,
   * иначе он затрёт доканоническую половину месяца.
   */
  readonly fromDay?: number;
}

export interface OzonPlanInput {
  readonly sheetId: number;
  readonly sheetName: string;
  /** Все секции листа — нужны слою представления: высоты, полоса итога, УФ, «будущий день». */
  readonly allSections: readonly SectionLayout[];
  /** Секции, содержимое которых пишется этим прогоном. */
  readonly sections: readonly OzonSectionInput[];
  /**
   * `current` — ЖИВОЙ размер листа, прочитанный перед сборкой плана (не из конфигурации:
   * устаревшее значение приведёт к повторной вставке строк и колонок).
   * `growth` описывает, КУДА вставлять: колонки идут перед хвостом владельца, а не в конец.
   */
  readonly grid: { current: { rows: number; columns: number }; growth: GridGrowth };
  readonly lcd: string | null;
  readonly lcdMirror: { row: number; column: number } | null;
  /** Идентификатор уже существующего OZON_LCD_MIRROR, если он есть: тогда обновляем, а не дублируем. */
  readonly lcdMirrorId?: string | null;
  /** Имя зеркала; уникально в книге, поэтому одноразовые копии получают своё. */
  readonly lcdMirrorName?: string;
  /** Пустые объединения-следы принятой книги; по умолчанию не воспроизводятся. */
  readonly residualMerges?: ReadonlyArray<readonly [number, number, number]>;
  /** Сколько правил УФ сейчас на листе: все они снимаются перед постановкой своих. */
  readonly existingCfRules?: number;
  readonly lcdRef: string;
  readonly blocks: number;
  /**
   * Phase B: текущие заметки ячеек «Реклама внутренняя» записываемой области (ключ `строка|колонка`, пустые
   * не приходят). Нет поля — заметки рекламы не управляются (прежнее поведение).
   */
  readonly adsNotes?: ReadonlyMap<string, string>;
}

export interface ValueWrite { range: string; values: CellValue[][] }
export interface OzonWritePlan {
  values: ValueWrite[];
  structure: SheetsRequest[];
  presentation: SheetsRequest[];
  conditional: SheetsRequest[];
  totals: Record<string, ReturnType<typeof composeMonth>['totals']>;
}

const quote = (s: string) => `'${s.replace(/'/g, "''")}'`;

export function buildOzonPlan(input: OzonPlanInput): OzonWritePlan {
  const values: ValueWrite[] = [];
  const totals: OzonWritePlan['totals'] = {};
  const observed: Array<{ row: number; block: number }> = [];
  const notes: ProvenanceNote[] = [];
  const notesForEconomics: SheetsRequest[] = [];
  const q = quote(input.sheetName);

  for (const sec of input.sections) {
    const { spec } = sec;
    const from = Math.max(1, sec.fromDay ?? 1);
    const comp = composeMonth(spec, sec.facts, sec.stock, input.lcd, from, sec.cart, sec.sheetInputs);
    totals[spec.key] = comp.totals;
    const { title, header } = buildHeaderRows(spec, sec.refTitle, sec.refHeader, sec.refAnchor, sec.skuTitles ?? {});
    const grid = buildGrid(spec, comp.cells, sec.formulas);
    const last = columnName(spec.ncols);
    values.push({ range: `${q}!A${spec.titleRow}:${last}${spec.titleRow}`, values: [title as CellValue[]] });
    values.push({ range: `${q}!A${spec.headerRow}:${last}${spec.headerRow}`, values: [header as CellValue[]] });
    // Дневной ручной input и пока невосстановленный output НЕ входят в запись.
    // Это сохраняет также исходные формулы и ввод владельца после чтения before-image.
    const protectedCols = new Set(spec.blocks.flatMap((o) => [spec.anchor[o]! + 1, spec.anchor[o]! + 12]));
    let start = 1;
    for (let col = 1; col <= spec.ncols + 1; col++) {
      if (col <= spec.ncols && !protectedCols.has(col)) continue;
      if (start < col) values.push({
        range: `${q}!${columnName(start)}${spec.firstRow + from - 1}:${columnName(col - 1)}${spec.lastRow}`,
        values: grid.slice(from - 1, spec.days).map((r) => r.slice(start - 1, col - 1)),
      });
      start = col + 1;
    }
    // Phase 6: ДРР магазина (K) — новая колонка движка; сутки секции ДО окна перезаписи тоже получают
    // формулу (их база — из живого листа), иначе K месяца, частично попавшего в окно, была бы дырявой.
    // Легаси-сутки до эпохи (01–16.04.2026: в K там текст дня недели) не трогаются никогда (ревью #257, R1).
    const kFirst = spec.key === OZON_REWRITE_EPOCH.slice(0, 7) ? Number(OZON_REWRITE_EPOCH.slice(8, 10)) : 1;
    if (from > kFirst) values.push({
      range: `${q}!${columnName(OZON_SUMMARY.drr)}${spec.firstRow + kFirst - 1}:${columnName(OZON_SUMMARY.drr)}${spec.firstRow + from - 2}`,
      values: grid.slice(kFirst - 1, from - 1).map((r) => [r[OZON_SUMMARY.drr - 1] as CellValue]),
    });
    // MTD этих колонок — агрегаты, а не ручной ввод: их формулы остаются штатными.
    values.push({ range: `${q}!A${spec.mtdRow}:${last}${spec.mtdRow}`, values: [grid[spec.days]!] });
    spec.blocks.forEach((o, b) => {
      for (let i = 0; i < spec.days; i++) {
        const row = spec.firstRow + i;
        const cell = comp.cells[`${o}|${row}`] as { stock?: number };
        if (cell && cell.stock !== undefined) observed.push({ row, block: b });
      }
    });
    // провенанс: где стоит оценка, там остаётся пометка — иначе факт и оценка неразличимы
    const slot = new Map(spec.blocks.map((o, b) => [o, b]));
    for (const pv of comp.provenance) {
      const b = slot.get(pv.offerId);
      if (b === undefined) continue;
      notes.push({ row: pv.row, block: b, component: pv.component, text: provenanceNoteText(pv) });
    }
    const noteCols = [9, 10, 14, 23];
    for (const o of spec.blocks) for (const off of noteCols) notesForEconomics.push({
      repeatCell: { range: { sheetId: input.sheetId, startRowIndex: spec.firstRow + from - 2,
        endRowIndex: spec.mtdRow, startColumnIndex: spec.anchor[o]! + off - 1,
        endColumnIndex: spec.anchor[o]! + off }, cell: { note: '' }, fields: 'note' },
    });
    for (const p of comp.partialEconomics) for (const off of noteCols) notesForEconomics.push({
      repeatCell: { range: { sheetId: input.sheetId, startRowIndex: p.row - 1, endRowIndex: p.row,
        startColumnIndex: spec.anchor[p.offerId]! + off - 1, endColumnIndex: spec.anchor[p.offerId]! + off },
      cell: { note: 'PROVISIONAL_PARTIAL: расчёт калькулятора по известным компонентам, не окончательная прибыль. '
        + 'Признанная выручка/часть экономики не доказана. Цена заказа не подставляется в финансовую выручку.' }, fields: 'note' },
    });
    // СПП месяца: явное состояние покрытия (заметка ставится заменой — снимается при полном покрытии)
    for (const o of spec.blocks) {
      const text = sppCoverageNote(sppMonthCoverage(spec, comp, input.lcd ?? "", from, o, sec.sppEvidence));
      notesForEconomics.push({ repeatCell: { range: { sheetId: input.sheetId, startRowIndex: spec.mtdRow - 1, endRowIndex: spec.mtdRow,
        startColumnIndex: spec.anchor[o]! + OZON_OFFSET.spp - 1, endColumnIndex: spec.anchor[o]! + OZON_OFFSET.spp },
      cell: { note: text }, fields: 'note' } });
    }
    // ДРР: заметки об оценке СПП ставятся ЗАМЕНОЙ — снять со всей записанной области, потом поставить
    // там, где знаменатель (весь или частью) собран по оценке. Пришёл факт — заметка уходит сама.
    const ix = ozonDrrBaseIndex(spec, comp.cells, { lcd: input.lcd, from, estimate: sec.sppEstimate,
      evidence: sec.sppEvidence, bloggers: sec.sheetInputs?.bloggers });
    const note = (row: number, col: number, text: string): void => { notesForEconomics.push({ repeatCell: {
      range: { sheetId: input.sheetId, startRowIndex: row - 1, endRowIndex: row, startColumnIndex: col - 1, endColumnIndex: col },
      cell: { note: text }, fields: 'note' } }); };
    const clear = (r0: number, col: number): void => { notesForEconomics.push({ repeatCell: {
      range: { sheetId: input.sheetId, startRowIndex: r0 - 1, endRowIndex: spec.mtdRow, startColumnIndex: col - 1, endColumnIndex: col },
      cell: { note: '' }, fields: 'note' } }); };
    clear(spec.firstRow, OZON_SUMMARY.drr);
    for (const o of spec.blocks) clear(spec.firstRow + from - 1, spec.anchor[o]! + OZON_OFFSET.drr);
    for (const e of ix.cell.values()) if (e.day >= from) note(e.row, spec.anchor[e.offerId]! + OZON_OFFSET.drr, drrDayEstimateNote(e.est));
    for (const [o, est] of ix.estSku) {
      const t = drrShareNote(est, ix.actSku.get(o) ?? 0); if (t) note(spec.mtdRow, spec.anchor[o]! + OZON_OFFSET.drr, t);
    }
    for (const [d, est] of ix.estDay) {
      const t = drrShareNote(est, ix.actDay.get(d) ?? 0); if (t) note(spec.firstRow + d - 1, OZON_SUMMARY.drr, t);
    }
    { const t = drrShareNote(ix.estAll, ix.actAll); if (t) note(spec.mtdRow, OZON_SUMMARY.drr, t); }
    // Phase B: разложение «Реклама внутренняя» = CPC + «Оплата за заказ» — управляемой строкой заметки,
    // по всей записываемой области (там, где CPO ушёл, строка снимается; заметка владельца остаётся).
    if (input.adsNotes) {
      const split = new Map(comp.adsSplit.map((a) => [`${a.row}|${spec.anchor[a.offerId]! + OZON_OFFSET.adsIn}`, a]));
      for (const o of spec.blocks) for (let d = from; d <= spec.days; d++) {
        const row = spec.firstRow + d - 1, col = spec.anchor[o]! + OZON_OFFSET.adsIn, k = `${row}|${col}`;
        const a = split.get(k), before = input.adsNotes.get(k) ?? '';
        const want = mergeAdsNote(before, a ? adsSplitLine(a.cpc, a.cpo) : null);
        if (want !== before) note(row, col, want);
      }
    }
    for (const o of new Set(comp.partialEconomics.map((p) => p.offerId))) for (const off of [9, 10, 23]) notesForEconomics.push({
      repeatCell: { range: { sheetId: input.sheetId, startRowIndex: spec.mtdRow - 1, endRowIndex: spec.mtdRow,
        startColumnIndex: spec.anchor[o]! + off - 1, endColumnIndex: spec.anchor[o]! + off },
      cell: { note: 'Итог содержит PROVISIONAL_PARTIAL: условная экономика, не окончательная прибыль.' }, fields: 'note' },
    });
  }

  const secLayouts = input.sections.map((s) => layoutOf(s.spec));
  const structure = [
    ...growGridRequests(input.sheetId, input.grid.current, input.grid.growth),
    ...columnWidthRequests(input.sheetId, input.blocks),
    ...unmergeRequests(input.sheetId, secLayouts, input.blocks),
    ...titleMergeRequests(input.sheetId, secLayouts, input.blocks),
    ...residualMergeRequests(input.sheetId, input.residualMerges ?? []),
    ...(input.lcdMirror
      ? lcdMirrorRequestsIdempotent(input.sheetId, input.lcdMirror.row, input.lcdMirror.column,
                                    input.lcdMirrorId ?? null, input.lcdMirrorName)
      : []),
  ];
  const presentation = [
    ...staticFormatRequests(input.sheetId, secLayouts),
    ...observedStockFormatRequests(input.sheetId, observed),
    ...rowHeightRequests(input.sheetId, input.allSections),
    ...mtdBandRequests(input.sheetId, input.allSections),
    // пометки провенанса ставятся ЗАМЕНОЙ: снять со всей области, потом поставить заново,
    // иначе пометка «это оценка» пережила бы приход факта
    ...clearProvenanceNoteRequests(input.sheetId, secLayouts),
    ...provenanceNoteRequests(input.sheetId, notes),
    ...notesForEconomics,
  ];
  // УФ ставится ЗАМЕНОЙ: сначала снимаем всё, что есть, иначе повторный прогон удвоит правила
  const conditional = [
    ...clearConditionalFormatRequests(input.sheetId, input.existingCfRules ?? 0),
    ...cfAllRequests(input.sheetId, input.allSections, input.lcdRef),
    ...futureDayRequests(input.sheetId, input.allSections, input.lcdRef),
  ];
  return { values, structure, presentation, conditional, totals };
}

/**
 * ПОРЯДОК ФАЗ ЗАПИСИ. Это не стилистика: `values.batchUpdate` СБРАСЫВАЕТ у записанной ячейки
 * `userEnteredFormat.numberFormat`. Пока оформление шло до величин, боевой прогон стирал
 * формат «₽» ровно на своём окне перезаписи — граница потери совпадала с первым днём окна
 * день-в-день, а месяцы вне окна оставались целыми.
 *
 * Поэтому оформление идёт ПОСЛЕ величин и оказывается последним словом о виде ячейки.
 * Структура (геометрия, объединения) обязана идти ДО величин: без строк их некуда писать.
 */
export const OZON_WRITE_PHASES = ['structure', 'conditional', 'values', 'presentation'] as const;
export type OzonWritePhase = (typeof OZON_WRITE_PHASES)[number];


/** Ожидаемый размер сетки после переноса — выводится из роста, а не задаётся руками. */
export function expectedGrid(
  current: { rows: number; columns: number }, growth: GridGrowth,
): { rows: number; columns: number } {
  return gridAfterGrowth(current, growth);
}
export { ozonMonthSpec, OZON_SUMMARY_ROLES };

/* ── сборка формул секции из адаптера формул ─────────────────────────────────── */
import {
  ozonBlockDayFormulas, ozonSummaryDayFormulas, ozonSummaryMtdFormulas, ozonBlockMtdFormulas,
  ozonSummaryDrrDayFormula, ozonSummaryDrrMtdFormula,
  OZON_MTD_BLANK_OFFSETS, OZON_LEGACY_LCD_NAME,
} from './formulas.js';
import { OZON_SUMMARY } from './contract.js';
import { OZON_REWRITE_EPOCH } from './window.js';
import { toLocaleFormula, type FormulaStyle } from '../formulas.js';

/** en-форма числового литерала для формулы; `toLocaleFormula` сам переведёт в ru_RU. */
const term = (v: number | undefined): string | undefined =>
  v === undefined || v === 0 ? undefined : String(v);

/**
 * Формулы секции целиком: сутки и строка итога, сводка и все блоки.
 * Литералы себестоимости и прочих прямых приходят из раскладки месяца —
 * та же величина, что легла в контрольные суммы, без второго источника правды.
 */
/** Phase 6: входы оценённой базы ДРР секции (без них формулы ДРР — по факту, как прежде). */
export interface OzonDrrInputs {
  readonly lcd: string | null;
  /** Первый день окна перезаписи; сутки раньше берутся из живого листа. */
  readonly from: number;
  readonly estimate?: Readonly<Record<string, SppEstimate>>;
  readonly evidence?: Readonly<Record<string, SppDayEvidence>>;
  readonly bloggers?: Readonly<Record<string, number>>;
}

/** Базы ДРР секции, сгруппированные для формул и заметок: сутки × SKU, сутки, SKU, месяц. */
export function ozonDrrBaseIndex(spec: OzonMonthSpec, cells: OzonMonthComposition['cells'] | undefined, drr: OzonDrrInputs | undefined) {
  const b = drr && cells
    ? ozonDrrBases(spec, { cells }, drr.lcd, drr.from, drr.estimate, drr.evidence, drr.bloggers)
    : { estimated: [], actual: [] };
  return {
    cell: new Map(b.estimated.map((e) => [`${e.offerId}|${e.row}`, e])),
    estDay: sumBy(b.estimated, (x) => x.day), actDay: sumBy(b.actual, (x) => x.day),
    estSku: sumBy(b.estimated, (x) => x.offerId), actSku: sumBy(b.actual, (x) => x.offerId),
    estAll: b.estimated.reduce((n, e) => n + e.basis, 0), actAll: b.actual.reduce((n, e) => n + e.basis, 0),
  };
}
function sumBy<T extends { basis: number }, K>(xs: readonly T[], key: (x: T) => K): Map<K, number> {
  const m = new Map<K, number>();
  for (const x of xs) m.set(key(x), (m.get(key(x)) ?? 0) + x.basis);
  return m;
}

export function sectionFormulas(
  spec: OzonMonthSpec,
  comp: { cogs: Record<string, number>; other: Record<string, number>; cells?: OzonMonthComposition['cells'] },
  style: FormulaStyle = 'SEMICOLON',
  /** Имя LCD в формулах: прежнее по умолчанию, OZON_LAST_CLOSED_DATE — после миграции (Gate 10). */
  lcdName: string = OZON_LEGACY_LCD_NAME,
  /** Phase 6: оценка СПП для ДРР; без неё ДРР считается только по фактической цене покупателя. */
  drr?: OzonDrrInputs,
): { day: Record<string, string>; mtd: Record<string, string>; mtdBlank: string[] } {
  // Книга владельца в ru_RU: разделитель аргументов «;», десятичный — запятая.
  // Формулы собираются в канонической en-форме и переводятся ОДНИМ местом.
  const loc = (f: string): string => toLocaleFormula(f, style);
  const day: Record<string, string> = {}; const mtd: Record<string, string> = {}; const mtdBlank: string[] = [];
  const ix = ozonDrrBaseIndex(spec, comp.cells, drr);
  // литерал базы — r2 рубля: как и литерал COGS, он едет в формулу en-формой и переводится в ru_RU
  const estTerm = (v: number | undefined): string | undefined => term(v === undefined ? undefined : r2(v));
  for (let row = spec.firstRow; row <= spec.lastRow; row++) {
    for (const [col, f] of ozonSummaryDayFormulas(row, spec.blocks.length, lcdName)) day[`${row}:${col}`] = loc(f);
    day[`${row}:${OZON_SUMMARY.drr}`] = loc(ozonSummaryDrrDayFormula(row, spec.blocks.length, lcdName,
      estTerm(ix.estDay.get(row - spec.firstRow + 1))));
    for (const o of spec.blocks) {
      const start = spec.anchor[o] as number; const key = `${o}|${row}`;
      const e = ix.cell.get(key);
      const p = { start, cogsTerm: term(comp.cogs[key]) ?? '0',
                  otherDirectTerm: term(comp.other[key]) ?? '0',
                  ...(e ? { sppEstimateTerm: String(r6e(e.est.pct)) } : {}) };
      for (const [off, f] of ozonBlockDayFormulas(p, row, lcdName)) day[`${row}:${start + off}`] = loc(f);
    }
  }
  const geom = { firstDailyRow: spec.firstRow, lastDailyRow: spec.lastRow, mtdRow: spec.mtdRow };
  for (const [col, f] of ozonSummaryMtdFormulas(geom)) mtd[`${spec.mtdRow}:${col}`] = loc(f);
  mtd[`${spec.mtdRow}:${OZON_SUMMARY.drr}`] = loc(ozonSummaryDrrMtdFormula(geom, spec.blocks.length, estTerm(ix.estAll)));
  for (const o of spec.blocks) {
    const start = spec.anchor[o] as number;
    for (const [off, f] of ozonBlockMtdFormulas(start, geom, lcdName, estTerm(ix.estSku.get(o)))) mtd[`${spec.mtdRow}:${start + off}`] = loc(f);
    for (const off of OZON_MTD_BLANK_OFFSETS) mtdBlank.push(`${spec.mtdRow}:${start + off}`);
  }
  return { day, mtd, mtdBlank };
}
