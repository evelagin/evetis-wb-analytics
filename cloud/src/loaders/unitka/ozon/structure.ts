/**
 * OZON UNITKA — структура секции и статический формат (Gate 5E).
 *
 * До Gate 5E структура набиралась copyPaste'ом «скопируй вон ту секцию», то есть оформление
 * существовало только внутри уже собранного листа и из Git не воспроизводилось. Здесь оно
 * выражено ДАННЫМИ: CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT + карта семантических ролей.
 */
import { OZON_GEOMETRY } from './contract.js';
import {
  OZON_FIELD_ROLES, OZON_SUMMARY_ROLES, ROLE_OFFSET, OZON_ROLE_CLASS, SKU_TITLE_MERGE_WIDTH,
  type FieldRole, type VisualClass,
} from './presentation.js';
import { CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT as C, type CellFormatSpec } from './wbcontract.js';
import type { SheetsRequest, SectionLayout } from './requests.js';

/** Четыре визуальных класса WB: ручной ввод, факт, тариф, расчёт. */
export const CLASS_BG: Readonly<Record<VisualClass, string>> = {
  MANUAL: '#fffbe8', FACT: '#f1f8f4', TARIFF: '#f3f8fd', CALC: '#ffffff',
};

function rgb(hex: string) {
  const h = hex.replace('#', '');
  return { red: parseInt(h.slice(0, 2), 16) / 255, green: parseInt(h.slice(2, 4), 16) / 255,
           blue: parseInt(h.slice(4, 6), 16) / 255 };
}

/** Спецификация формата → userEnteredFormat + список полей для repeatCell. */
export function toUserEnteredFormat(spec: CellFormatSpec, bgOverride?: string):
    { format: Record<string, unknown>; fields: string } {
  const tf: Record<string, unknown> = {};
  if (spec.fontFamily) tf['fontFamily'] = spec.fontFamily;
  if (spec.fontSize !== undefined) tf['fontSize'] = spec.fontSize;
  if (spec.bold) tf['bold'] = true;
  if (spec.italic) tf['italic'] = true;
  if (spec.fg) tf['foregroundColor'] = rgb(spec.fg);
  const f: Record<string, unknown> = {};
  if (Object.keys(tf).length) f['textFormat'] = tf;
  f['backgroundColor'] = rgb(bgOverride ?? spec.bg ?? '#ffffff');
  if (spec.ha) f['horizontalAlignment'] = spec.ha;
  if (spec.va) f['verticalAlignment'] = spec.va;
  if (spec.wrap) f['wrapStrategy'] = spec.wrap;
  if (spec.borders) {
    f['borders'] = Object.fromEntries(Object.entries(spec.borders).map(([k, v]) => [k, { style: v }]));
  }
  if (spec.numberFormat?.pattern) {
    f['numberFormat'] = { type: spec.numberFormat.type ?? 'NUMBER', pattern: spec.numberFormat.pattern };
  }
  const fields = ['textFormat', 'backgroundColor', 'horizontalAlignment', 'verticalAlignment',
    'wrapStrategy', 'borders', 'numberFormat']
    .filter((k) => f[k] !== undefined).map((k) => `userEnteredFormat.${k}`).join(',');
  return { format: f, fields };
}

type RowRoleName = 'title' | 'header' | 'day' | 'mtd';

/**
 * Фон ячейки дня у Ozon. Совпадает с WB везде, кроме доказанных семантических расхождений
 * (цена/СПП/комиссия/логистика — факт, а не ввод и не тариф; корзина и остаток — неизвестность).
 */
export function dayBackgroundFor(role: FieldRole): string {
  return CLASS_BG[OZON_ROLE_CLASS[role]];
}

/** Статический формат всех четырёх ролей строк по всем колонкам секции. */
export function staticFormatRequests(sheetId: number, sections: readonly SectionLayout[]): SheetsRequest[] {
  const out: SheetsRequest[] = [];
  const rowsOf = (s: SectionLayout): Array<[RowRoleName, number, number]> => [
    ['title', s.titleRow, s.titleRow], ['header', s.headerRow, s.headerRow],
    ['day', s.firstRow, s.lastRow], ['mtd', s.mtdRow, s.mtdRow],
  ];
  for (const s of sections) {
    for (const [kind, r0, r1] of rowsOf(s)) {
      for (const { col, role } of OZON_SUMMARY_ROLES) {
        const spec = C.rows[kind].summary[role]; if (!spec) continue;
        const bg = kind === 'day' ? dayBackgroundFor(role) : undefined;
        const { format, fields } = toUserEnteredFormat(spec, bg);
        out.push({ repeatCell: { range: { sheetId, startRowIndex: r0 - 1, endRowIndex: r1,
          startColumnIndex: col - 1, endColumnIndex: col }, cell: { userEnteredFormat: format }, fields } });
      }
      for (let b = 0; b < s.blockCount; b++) {
        const c0 = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
        for (const role of OZON_FIELD_ROLES) {
          const spec = C.rows[kind].block[role]; if (!spec) continue;
          const bg = kind === 'day' ? dayBackgroundFor(role) : undefined;
          const { format, fields } = toUserEnteredFormat(spec, bg);
          const col = c0 + ROLE_OFFSET[role];
          out.push({ repeatCell: { range: { sheetId, startRowIndex: r0 - 1, endRowIndex: r1,
            startColumnIndex: col - 1, endColumnIndex: col }, cell: { userEnteredFormat: format }, fields } });
        }
      }
    }
  }
  return out;
}

/**
 * Наблюдаемый остаток выглядит фактом поштучно (Gate 5C): там, где снимок доказан,
 * ячейка красится как FACT; где снимка нет — остаётся «неизвестно», а не зелёным нулём.
 */
export function observedStockFormatRequests(
  sheetId: number, observed: ReadonlyArray<{ row: number; block: number }>,
): SheetsRequest[] {
  const bg = rgb(CLASS_BG.FACT);
  return observed.map(({ row, block }) => {
    const col = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * block + ROLE_OFFSET.STOCK;
    return { repeatCell: { range: { sheetId, startRowIndex: row - 1, endRowIndex: row,
      startColumnIndex: col - 1, endColumnIndex: col },
      cell: { userEnteredFormat: { backgroundColor: bg } }, fields: 'userEnteredFormat.backgroundColor' } };
  });
}

/** Ширины колонок: сводка и каждый блок — из того же контракта WB. */
export function columnWidthRequests(sheetId: number, blocks: number): SheetsRequest[] {
  const out: SheetsRequest[] = [];
  for (const { col, role } of OZON_SUMMARY_ROLES) {
    const w = C.widths.summary[role]; if (w === undefined) continue;
    out.push({ updateDimensionProperties: { range: { sheetId, dimension: 'COLUMNS',
      startIndex: col - 1, endIndex: col }, properties: { pixelSize: w }, fields: 'pixelSize' } });
  }
  for (let b = 0; b < blocks; b++) {
    const c0 = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
    for (const role of OZON_FIELD_ROLES) {
      const w = C.widths.block[role]; if (w === undefined) continue;
      const col = c0 + ROLE_OFFSET[role];
      out.push({ updateDimensionProperties: { range: { sheetId, dimension: 'COLUMNS',
        startIndex: col - 1, endIndex: col }, properties: { pixelSize: w }, fields: 'pixelSize' } });
    }
  }
  return out;
}

/** Объединения: заголовок месяца по сводке и заголовок каждого SKU шириной 7 колонок. */
export function titleMergeRequests(
  sheetId: number, sections: readonly SectionLayout[], blockSlots: number,
): SheetsRequest[] {
  const out: SheetsRequest[] = [];
  for (const s of sections) {
    out.push({ mergeCells: { mergeType: 'MERGE_ALL', range: { sheetId,
      startRowIndex: s.titleRow - 1, endRowIndex: s.titleRow,
      startColumnIndex: 0, endColumnIndex: OZON_SUMMARY_ROLES.length } } });
    // сетка блоков одинакова во всех секциях: жизненный цикл SKU управляет СОДЕРЖИМЫМ,
    // а не разметкой. Поэтому объединение заголовка ставится на каждый слот блока листа.
    for (let b = 0; b < blockSlots; b++) {
      const c0 = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
      out.push({ mergeCells: { mergeType: 'MERGE_ALL', range: { sheetId,
        startRowIndex: s.titleRow - 1, endRowIndex: s.titleRow,
        startColumnIndex: c0 - 1, endColumnIndex: c0 - 1 + SKU_TITLE_MERGE_WIDTH } } });
    }
  }
  return out;
}

/**
 * Объединения, оставшиеся от сборки гейтов и не имеющие ни содержимого, ни смысла:
 * пустые полосы в первой строке листа и пара пустых ячеек в хвосте. В production их нет.
 * Движок создаёт их ТОЛЬКО по явному списку — чтобы можно было побайтно воспроизвести
 * принятую книгу, не втаскивая шум в перенос по умолчанию.
 */
export function residualMergeRequests(
  sheetId: number, merges: ReadonlyArray<readonly [number, number, number]>,
): SheetsRequest[] {
  return merges.map(([row, from, to]) => ({ mergeCells: { mergeType: 'MERGE_ALL',
    range: { sheetId, startRowIndex: row - 1, endRowIndex: row,
             startColumnIndex: from - 1, endColumnIndex: to } } }));
}

/**
 * Зеркало LAST_CLOSED_DATE. Sheets не принимает именованный диапазон внутри CUSTOM_FORMULA,
 * поэтому условное форматирование смотрит на ячейку, а ячейка — на именованный диапазон.
 */
export const OZON_LCD_MIRROR_NAME = 'OZON_LCD_MIRROR';
export function lcdMirrorRequests(sheetId: number, row: number, column: number): SheetsRequest[] {
  return [
    { updateCells: { range: { sheetId, startRowIndex: row - 1, endRowIndex: row,
        startColumnIndex: column - 1, endColumnIndex: column },
      rows: [{ values: [{ userEnteredValue: { formulaValue: '=LAST_CLOSED_DATE' } }] }],
      fields: 'userEnteredValue' } },
    { addNamedRange: { namedRange: { name: OZON_LCD_MIRROR_NAME, range: { sheetId,
      startRowIndex: row - 1, endRowIndex: row, startColumnIndex: column - 1, endColumnIndex: column } } } },
  ];
}

/**
 * Расширение сетки под новые SKU-блоки.
 *
 * Колонки ВСТАВЛЯЮТСЯ перед хвостом, а не дописываются в конец. За последним боевым
 * блоком в листе владельца лежит его собственная помесячная панель («Общее количество
 * заказов N SKU», «Общая доходность N SKU», «Реализация»): дописывание в конец накрыло бы
 * её новыми блоками и уничтожило бы. Вставка сдвигает панель вправе вместе с её
 * объединениями и форматами — ровно так собрана принятая книга.
 *
 * Строки дописываются в конец: новые секции идут ниже последней существующей.
 *
 * ПРЕДУСЛОВИЕ: `current` — ЖИВОЙ размер листа, прочитанный непосредственно перед сборкой
 * плана. Устаревшее значение — единственный способ сломать идемпотентность: план вставит
 * колонки и строки повторно.
 */
export interface GridGrowth {
  /** 1-based номер колонки, ПЕРЕД которой вставляются новые блоки (первая колонка хвоста). */
  readonly insertColumnsBefore: number;
  /** Сколько колонок блоков вставить (кратно ширине блока). */
  readonly insertColumnCount: number;
  /** Сколько колонок дописать в конец (место под зеркало LAST_CLOSED_DATE). */
  readonly appendColumnCount: number;
  /** Сколько строк дописать в конец (новые месячные секции). */
  readonly appendRowCount: number;
}

export function growGridRequests(
  sheetId: number, current: { rows: number; columns: number }, growth: GridGrowth,
): SheetsRequest[] {
  const { insertColumnsBefore, insertColumnCount, appendColumnCount, appendRowCount } = growth;
  if (insertColumnCount % OZON_GEOMETRY.BLOCK_WIDTH !== 0) {
    throw new Error('вставка колонок: не кратно ширине блока');
  }
  if (insertColumnCount < 0 || appendColumnCount < 0 || appendRowCount < 0) {
    throw new Error('сжатие листа движком запрещено');
  }
  const out: SheetsRequest[] = [];
  if (appendRowCount > 0) out.push({ appendDimension: { sheetId, dimension: 'ROWS', length: appendRowCount } });
  if (insertColumnCount > 0) {
    if (insertColumnsBefore < 1 || insertColumnsBefore > current.columns + 1) {
      throw new Error('вставка колонок: точка вставки вне листа');
    }
    out.push({ insertDimension: { range: { sheetId, dimension: 'COLUMNS',
      startIndex: insertColumnsBefore - 1, endIndex: insertColumnsBefore - 1 + insertColumnCount },
      inheritFromBefore: true } });
  }
  if (appendColumnCount > 0) out.push({ appendDimension: { sheetId, dimension: 'COLUMNS', length: appendColumnCount } });
  return out;
}

/** Размер листа после роста — выводится из той же спецификации, а не задаётся отдельно. */
export function gridAfterGrowth(
  current: { rows: number; columns: number }, growth: GridGrowth,
): { rows: number; columns: number } {
  return { rows: current.rows + growth.appendRowCount,
           columns: current.columns + growth.insertColumnCount + growth.appendColumnCount };
}

/* ── идемпотентность: повторный прогон обязан ничего не менять ───────────────── */

/**
 * Условное форматирование добавляется, а не «обновляется»: повторный прогон без очистки
 * удвоил бы правила. Поэтому запись УФ — это ЗАМЕНА: сначала снять все существующие
 * правила листа, потом поставить свои. Удаляем с конца, иначе индексы поедут.
 */
export function clearConditionalFormatRequests(sheetId: number, existing: number): SheetsRequest[] {
  return Array.from({ length: existing }, (_, k) => ({
    deleteConditionalFormatRule: { sheetId, index: existing - 1 - k },
  }));
}

/**
 * Объединение уже объединённого диапазона — ошибка API, поэтому сначала разъединяем.
 * Разъединяем ТОЛЬКО область сводки и блоков: хвост с помесячной панелью владельца
 * лежит правее и его объединения трогать нельзя.
 */
export function unmergeRequests(
  sheetId: number, sections: readonly SectionLayout[], blockSlots: number,
): SheetsRequest[] {
  return sections.map((s) => ({ unmergeCells: { range: { sheetId,
    startRowIndex: s.titleRow - 1, endRowIndex: s.titleRow, startColumnIndex: 0,
    endColumnIndex: OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * blockSlots - 1 } } }));
}

/**
 * Именованный диапазон создаётся один раз; при повторе — обновляется, а не дублируется.
 * Имя уникально в пределах КНИГИ, поэтому наличие проверяется по имени во всей книге,
 * а не по листу, и одноразовым копиям листа нужно своё имя.
 */
export function lcdMirrorRequestsIdempotent(
  sheetId: number, row: number, column: number, existingId: string | null,
  name: string = OZON_LCD_MIRROR_NAME,
): SheetsRequest[] {
  const range = { sheetId, startRowIndex: row - 1, endRowIndex: row,
                  startColumnIndex: column - 1, endColumnIndex: column };
  const write: SheetsRequest = { updateCells: { range,
    rows: [{ values: [{ userEnteredValue: { formulaValue: '=LAST_CLOSED_DATE' } }] }],
    fields: 'userEnteredValue' } };
  return existingId
    ? [write, { updateNamedRange: { namedRange: { namedRangeId: existingId, name, range }, fields: 'range,name' } }]
    : [write, { addNamedRange: { namedRange: { name, range } } }];
}
