/**
 * OZON UNITKA — сборка полного плана записи (Gate 5E).
 *
 * Один вход → один план. План детерминирован: те же входные данные дают тот же план,
 * поэтому повторный прогон ничего не меняет. Ни одной ссылки «скопируй с такой-то строки»:
 * оформление берётся из CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT.
 */
import { OZON_GEOMETRY } from './contract.js';
import { OZON_SUMMARY_ROLES } from './presentation.js';
import {
  composeMonth, buildGrid, buildHeaderRows, ozonMonthSpec,
  type OzonMonthSpec, type OzonFactRow, type CellValue,
} from './month.js';
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

export interface OzonSectionInput {
  readonly spec: OzonMonthSpec;
  readonly facts: readonly OzonFactRow[];
  /** Доказанные снимки остатка, ключ `iso|offer_id`. */
  readonly stock: Readonly<Record<string, number>>;
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
  const q = quote(input.sheetName);

  for (const sec of input.sections) {
    const { spec } = sec;
    const from = Math.max(1, sec.fromDay ?? 1);
    const comp = composeMonth(spec, sec.facts, sec.stock, input.lcd, from);
    totals[spec.key] = comp.totals;
    const { title, header } = buildHeaderRows(spec, sec.refTitle, sec.refHeader, sec.refAnchor, sec.skuTitles ?? {});
    const grid = buildGrid(spec, comp.cells, sec.formulas);
    const last = columnName(spec.ncols);
    values.push({ range: `${q}!A${spec.titleRow}:${last}${spec.titleRow}`, values: [title as CellValue[]] });
    values.push({ range: `${q}!A${spec.headerRow}:${last}${spec.headerRow}`, values: [header as CellValue[]] });
    values.push({ range: `${q}!A${spec.firstRow + from - 1}:${last}${spec.mtdRow}`,
                  values: grid.slice(from - 1) });
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
  OZON_MTD_BLANK_OFFSETS,
} from './formulas.js';
import { toLocaleFormula, type FormulaStyle } from '../formulas.js';

/** en-форма числового литерала для формулы; `toLocaleFormula` сам переведёт в ru_RU. */
const term = (v: number | undefined): string | undefined =>
  v === undefined || v === 0 ? undefined : String(v);

/**
 * Формулы секции целиком: сутки и строка итога, сводка и все блоки.
 * Литералы себестоимости и прочих прямых приходят из раскладки месяца —
 * та же величина, что легла в контрольные суммы, без второго источника правды.
 */
export function sectionFormulas(
  spec: OzonMonthSpec,
  comp: { cogs: Record<string, number>; other: Record<string, number> },
  style: FormulaStyle = 'SEMICOLON',
): { day: Record<string, string>; mtd: Record<string, string>; mtdBlank: string[] } {
  // Книга владельца в ru_RU: разделитель аргументов «;», десятичный — запятая.
  // Формулы собираются в канонической en-форме и переводятся ОДНИМ местом.
  const loc = (f: string): string => toLocaleFormula(f, style);
  const day: Record<string, string> = {}; const mtd: Record<string, string> = {}; const mtdBlank: string[] = [];
  for (let row = spec.firstRow; row <= spec.lastRow; row++) {
    for (const [col, f] of ozonSummaryDayFormulas(row, spec.blocks.length)) day[`${row}:${col}`] = loc(f);
    for (const o of spec.blocks) {
      const start = spec.anchor[o] as number; const key = `${o}|${row}`;
      const p = { start, cogsTerm: term(comp.cogs[key]) ?? '0',
                  otherDirectTerm: term(comp.other[key]) ?? '0' };
      for (const [off, f] of ozonBlockDayFormulas(p, row)) day[`${row}:${start + off}`] = loc(f);
    }
  }
  const geom = { firstDailyRow: spec.firstRow, lastDailyRow: spec.lastRow, mtdRow: spec.mtdRow };
  for (const [col, f] of ozonSummaryMtdFormulas(geom)) mtd[`${spec.mtdRow}:${col}`] = loc(f);
  for (const o of spec.blocks) {
    const start = spec.anchor[o] as number;
    for (const [off, f] of ozonBlockMtdFormulas(start, geom)) mtd[`${spec.mtdRow}:${start + off}`] = loc(f);
    for (const off of OZON_MTD_BLANK_OFFSETS) mtdBlank.push(`${spec.mtdRow}:${start + off}`);
  }
  return { day, mtd, mtdBlank };
}
