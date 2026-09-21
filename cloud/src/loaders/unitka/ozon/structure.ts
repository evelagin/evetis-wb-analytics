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
  OZON_STYLE_TEMPLATE, OZON_BORDER_OVERRIDES, SECTION_ROW_ROLES,
  type FieldRole, type VisualClass, type SectionRowRole,
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

type RowRoleName = SectionRowRole;

/* ── Gate 7: разрешение оформления роли, которой нет в снятом контракте WB ──────
 *
 * Молчаливый пропуск роли без спецификации — та самая дыра, через которую «Прочие прямые»
 * уехали в production неоформленными. Здесь пропуска нет: роль либо описана контрактом,
 * либо указывает роль-образец, либо сборка плана падает.
 */

/** Спецификация формата роли блока: своя, либо унаследованная от роли-образца. */
export function blockSpecFor(kind: RowRoleName, role: FieldRole): CellFormatSpec {
  const own = C.rows[kind].block[role];
  const tpl = OZON_STYLE_TEMPLATE[role];
  const base = own ?? (tpl ? C.rows[kind].block[tpl] : undefined);
  if (!base) {
    throw new Error(`оформление роли ${role} (${kind}) не определено: нет ни своей спецификации, `
      + 'ни роли-образца в OZON_STYLE_TEMPLATE');
  }
  const over = OZON_BORDER_OVERRIDES.filter((o) => o.row === kind && o.role === role);
  if (!over.length) return base;
  const borders = { ...(base.borders ?? {}) };
  for (const o of over) borders[o.side] = o.style;
  return { ...base, borders };
}

/** Ширина колонки роли блока: своя, либо унаследованная от роли-образца. */
export function blockWidthFor(role: FieldRole): number {
  const own = C.widths.block[role];
  const tpl = OZON_STYLE_TEMPLATE[role];
  const w = own ?? (tpl ? C.widths.block[tpl] : undefined);
  if (w === undefined) {
    throw new Error(`ширина роли ${role} не определена: нет ни своей, ни роли-образца`);
  }
  return w;
}

/**
 * Каждая роль блока обязана иметь оформление во всех четырёх ролях строк и ширину.
 * Вызывается регрессией: добавление 26-й колонки без образца обязано падать на тесте,
 * а не проявляться серой полосой в боевом листе.
 */
export function unstyledBlockRoles(): string[] {
  const bad: string[] = [];
  for (const role of OZON_FIELD_ROLES) {
    for (const kind of SECTION_ROW_ROLES) {
      try { blockSpecFor(kind, role); } catch { bad.push(`${role}/${kind}`); }
    }
    try { blockWidthFor(role); } catch { bad.push(`${role}/width`); }
  }
  return bad;
}

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
          const spec = blockSpecFor(kind, role);
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
      const w = blockWidthFor(role);
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
  /**
   * Gate 6A. Колонки (1-based), ПЕРЕД которыми вставляется ровно одна колонка, чтобы расширить
   * УЖЕ СУЩЕСТВУЮЩИЙ блок с 24 до 25. Нужно потому, что смена ширины блока — это не дописывание
   * новых блоков: каждый старый блок обязан получить свою колонку ВНУТРИ себя, иначе легаси-данные
   * апреля 01–16 окажутся под чужими смещениями.
   *
   * Вставки выполняются СПРАВА НАЛЕВО: вставка сдвигает всё правее себя, и при обходе слева
   * направо каждая следующая позиция «уезжала» бы на число уже сделанных вставок.
   *
   * Позиция внутри блока выбрана так, что легаси-значения справа от неё сдвигаются ровно на одно
   * смещение и попадают туда, где им и место в новой карте (налог → 22, доходность → 23,
   * день недели → 24). Ничего не переписывается — данные едут вместе со своим блоком.
   */
  readonly widenBlockAt?: readonly number[];
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
  const widen = [...(growth.widenBlockAt ?? [])];
  if (insertColumnCount % OZON_GEOMETRY.BLOCK_WIDTH !== 0) {
    throw new Error('вставка колонок: не кратно ширине блока');
  }
  if (new Set(widen).size !== widen.length) throw new Error('расширение блоков: позиция повторяется');
  for (const c of widen) {
    if (!Number.isInteger(c) || c < 1 || c > current.columns + 1) {
      throw new Error(`расширение блоков: позиция ${c} вне листа`);
    }
  }
  if (insertColumnCount < 0 || appendColumnCount < 0 || appendRowCount < 0) {
    throw new Error('сжатие листа движком запрещено');
  }
  const out: SheetsRequest[] = [];
  if (appendRowCount > 0) out.push({ appendDimension: { sheetId, dimension: 'ROWS', length: appendRowCount } });
  // справа налево — иначе каждая следующая позиция уехала бы на число уже сделанных вставок
  for (const c of widen.slice().sort((a, b) => b - a)) {
    out.push({ insertDimension: { range: { sheetId, dimension: 'COLUMNS',
      startIndex: c - 1, endIndex: c }, inheritFromBefore: true } });
  }
  if (insertColumnCount > 0) {
    // расширения блоков выполняются РАНЬШЕ и уже сдвинули хвост вправо: точку вставки
    // проверяем относительно листа ПОСЛЕ расширения, иначе корректный план был бы отвергнут
    const columnsAfterWiden = current.columns + widen.length;
    if (insertColumnsBefore < 1 || insertColumnsBefore > columnsAfterWiden + 1) {
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
           columns: current.columns + (growth.widenBlockAt?.length ?? 0)
                    + growth.insertColumnCount + growth.appendColumnCount };
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

/* ── Gate 8: провенанс ячейки — факт или оценка ───────────────────────────────
 *
 * «Оценка обязана оставаться отличимой от факта» — в листе это ПРИМЕЧАНИЕ на ячейке.
 * Цвет для этого не используется сознательно: язык цвета Юнитки занят смыслом метрики,
 * и новый оттенок читался бы как новая экономическая категория (урок Gate 7).
 *
 * Примечания ставятся ЗАМЕНОЙ: сначала снимаются со всей области комиссии и логистики
 * записываемых секций, потом ставятся заново. Иначе пометка пережила бы приход факта
 * и утверждала бы «это оценка» про уже подтверждённое начисление.
 */
export interface ProvenanceNote {
  readonly row: number; readonly block: number;
  readonly component: 'COMMISSION' | 'LOGISTICS';
  readonly text: string;
}

/** Снять примечания со всех ячеек комиссии и логистики записываемых секций. */
export function clearProvenanceNoteRequests(
  sheetId: number, sections: readonly SectionLayout[],
): SheetsRequest[] {
  const out: SheetsRequest[] = [];
  for (const s of sections) {
    for (let b = 0; b < s.blockCount; b++) {
      for (const role of ['COMMISSION', 'LOGISTICS'] as const) {
        const col = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b + ROLE_OFFSET[role];
        out.push({ repeatCell: { range: { sheetId, startRowIndex: s.firstRow - 1, endRowIndex: s.lastRow,
          startColumnIndex: col - 1, endColumnIndex: col }, cell: { note: '' }, fields: 'note' } });
      }
    }
  }
  return out;
}

/** Поставить примечание на каждую ячейку, где стоит оценка. */
export function provenanceNoteRequests(
  sheetId: number, notes: readonly ProvenanceNote[],
): SheetsRequest[] {
  return notes.map(({ row, block, component, text }) => {
    const col = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * block + ROLE_OFFSET[component];
    return { repeatCell: { range: { sheetId, startRowIndex: row - 1, endRowIndex: row,
      startColumnIndex: col - 1, endColumnIndex: col }, cell: { note: text }, fields: 'note' } };
  });
}

/** Текст примечания: что это за величина, откуда она и чем будет заменена. */
export function provenanceNoteText(a: {
  component: 'COMMISSION' | 'LOGISTICS'; method: string | null; estimatedRub: number; actualRub: number;
}): string {
  const what = a.component === 'COMMISSION' ? 'Комиссия' : 'Логистика';
  const how = a.method === 'POSTING_PAYOUT_EXACT'
    ? 'из отчёта по отправлениям (цена − выплата), тождество проверено на 600 отправлениях из 600'
    : a.method === 'COMMISSION_POLICY_TARIFF' ? 'по действующему тарифу площадки'
    : a.method === 'SKU_P70_120D' ? 'оценщик SKU_P70_120D (70-й перцентиль по своему SKU за 120 суток)'
    : a.method === 'MARKETPLACE_P70_120D' ? 'оценщик MARKETPLACE_P70_120D (наблюдений по SKU недостаточно)'
    : 'доказанный оценщик';
  const part = a.actualRub > 0 ? ` Часть суток подтверждена фактом: ${a.actualRub.toFixed(2)} ₽.` : '';
  return `${what}: ОЦЕНКА, а не факт.\nOzon ещё не опубликовал начисление (обычно 18 суток после заказа, максимум 36).\n`
    + `Оценено ${a.estimatedRub.toFixed(2)} ₽ — ${how}.${part}\n`
    + 'Заменится фактом автоматически, как только начисление придёт.';
}
