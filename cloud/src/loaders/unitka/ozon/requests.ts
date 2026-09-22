/**
 * OZON UNITKA — построители запросов представления (Gate 5E: перенос слоя Gate 5D в боевой код).
 *
 * Функции чистые: принимают ГЕОМЕТРИЮ секции и КАРТУ РОЛЕЙ, отдают запросы Sheets API.
 * Ни одного «если месяц == …», ни одного вшитого списка диапазонов: всё выводится из геометрии,
 * поэтому тот же слой обслуживает и перенос в production, и каждый следующий месяц.
 */
import { OZON_GEOMETRY } from './contract.js';
import {
  OZON_SUMMARY_ROLES, ROLE_OFFSET, OZON_ROW_GEOMETRY, MTD_BAND_COLOUR,
  OPAQUE_BOOLEAN_ROLES, OZON_SCALE_TIERS, type FieldRole, type RowRole,
} from './presentation.js';

export type SheetsRequest = Record<string, unknown>;

/** Минимум, который нужен слою представления от секции. `OzonMonthSpec` ему удовлетворяет. */
export interface SectionLayout {
  readonly titleRow: number; readonly headerRow: number;
  readonly firstRow: number; readonly lastRow: number;
  readonly mtdRow: number; readonly spacerRow: number;
  /** Сколько SKU-блоков физически присутствует в секции. */
  readonly blockCount: number;
}
export const layoutOf = (s: {
  titleRow: number; headerRow: number; firstRow: number; lastRow: number; mtdRow: number;
  spacerRow: number; blocks: readonly unknown[];
}): SectionLayout => ({ titleRow: s.titleRow, headerRow: s.headerRow, firstRow: s.firstRow,
  lastRow: s.lastRow, mtdRow: s.mtdRow, spacerRow: s.spacerRow, blockCount: s.blocks.length });

export function columnName(n: number): string {
  let s = '';
  for (let x = n; x > 0;) { const r = (x - 1) % 26; s = String.fromCharCode(65 + r) + s; x = (x - 1 - r) / 26; }
  return s;
}
export function blockColumn(block: number, role: FieldRole): number {
  return OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * block + ROLE_OFFSET[role];
}
export function summaryColumn(role: FieldRole): number | null {
  return OZON_SUMMARY_ROLES.find((r) => r.role === role)?.col ?? null;
}
function rgb(hex: string): { red: number; green: number; blue: number } {
  const h = hex.replace('#', '');
  return { red: parseInt(h.slice(0, 2), 16) / 255, green: parseInt(h.slice(2, 4), 16) / 255,
           blue: parseInt(h.slice(4, 6), 16) / 255 };
}

/** Высоты строк по структурной роли. Соседние строки одной высоты склеиваются в один запрос. */
export function rowHeightRequests(sheetId: number, sections: readonly SectionLayout[]): SheetsRequest[] {
  const want = new Map<number, RowRole>();
  for (const s of sections) {
    want.set(s.titleRow, 'title'); want.set(s.headerRow, 'header');
    for (let r = s.firstRow; r <= s.lastRow; r++) want.set(r, 'day');
    want.set(s.mtdRow, 'mtd'); want.set(s.spacerRow, 'spacer');
  }
  const rows = [...want.keys()].sort((a, b) => a - b);
  const out: SheetsRequest[] = [];
  for (let i = 0; i < rows.length;) {
    let j = i;
    const role = want.get(rows[i] as number) as RowRole;
    while (j + 1 < rows.length && rows[j + 1] === (rows[j] as number) + 1 && want.get(rows[j + 1] as number) === role) j++;
    out.push({ updateDimensionProperties: {
      range: { sheetId, dimension: 'ROWS', startIndex: (rows[i] as number) - 1, endIndex: rows[j] as number },
      properties: { pixelSize: OZON_ROW_GEOMETRY[role] }, fields: 'pixelSize' } });
    i = j + 1;
  }
  return out;
}

/**
 * Сплошная серая полоса итога месяца. WB не красит колонку дня недели (A) — полоса
 * начинается с «Даты»; в блоках полосой накрываются все 24 колонки, включая день недели.
 */
export function mtdBandRequests(sheetId: number, sections: readonly SectionLayout[]): SheetsRequest[] {
  const cell = { userEnteredFormat: { backgroundColor: rgb(MTD_BAND_COLOUR) } };
  const out: SheetsRequest[] = [];
  for (const s of sections) {
    const spans: Array<[number, number]> = [[2, OZON_SUMMARY_ROLES.length]];
    for (let b = 0; b < s.blockCount; b++) {
      const c0 = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
      spans.push([c0, c0 + OZON_GEOMETRY.BLOCK_WIDTH - 1]);
    }
    for (const [a, b] of spans) {
      out.push({ repeatCell: { range: { sheetId, startRowIndex: s.mtdRow - 1, endRowIndex: s.mtdRow,
        startColumnIndex: a - 1, endColumnIndex: b }, cell, fields: 'userEnteredFormat.backgroundColor' } });
    }
  }
  return out;
}

interface RuleFormat { backgroundColor?: ReturnType<typeof rgb>; textFormat?: { foregroundColor: ReturnType<typeof rgb> } }
function fmt(bg?: string, fg?: string): RuleFormat {
  const f: RuleFormat = {};
  if (bg) f.backgroundColor = rgb(bg);
  if (fg) f.textFormat = { foregroundColor: rgb(fg) };
  return f;
}
function rule(sheetId: number, formula: string, format: RuleFormat,
              ranges: ReadonlyArray<[number, number, number]>): SheetsRequest {
  return { addConditionalFormatRule: { rule: {
    ranges: ranges.map(([a, b, c]) => ({ sheetId, startRowIndex: a - 1, endRowIndex: b,
      startColumnIndex: c - 1, endColumnIndex: c })),
    booleanRule: { condition: { type: 'CUSTOM_FORMULA', values: [{ userEnteredValue: formula }] }, format },
  }, index: 0 } };
}
type Scope = 'summary' | 'block' | 'both';
function columnsFor(sections: readonly SectionLayout[], role: FieldRole, scope: Scope): Array<[number, number, number]> {
  const out: Array<[number, number, number]> = [];
  for (const s of sections) {
    const sc = summaryColumn(role);
    if ((scope === 'summary' || scope === 'both') && sc !== null) out.push([s.firstRow, s.lastRow, sc]);
    if (scope === 'summary') continue;
    for (let b = 0; b < s.blockCount; b++) out.push([s.firstRow, s.lastRow, blockColumn(b, role)]);
  }
  return out;
}

/**
 * Семьи условного форматирования WB, перенесённые по ролям. Одно правило — много диапазонов
 * (урок E6: правило на ячейку убивает лист). Ссылки относительные, поэтому якорь — первый диапазон.
 */
export function cfFamilyRequests(
  sheetId: number, sections: readonly SectionLayout[], lcdRef: string,
): SheetsRequest[] {
  const out: SheetsRequest[] = [];
  const at = (rs: Array<[number, number, number]>) => {
    const [row, , col] = rs[0] as [number, number, number];
    return `${columnName(col)}${row}`;
  };
  for (const role of ['TOTAL_PROFIT', 'FINAL_UNIT_PROFIT'] as const) {
    const rs = columnsFor(sections, role, 'block'); if (!rs.length) continue;
    const ref = at(rs);
    out.push(rule(sheetId, `=AND(ISNUMBER(${ref});${ref}>0)`, fmt(undefined, '#38761d'), rs));
    out.push(rule(sheetId, role === 'TOTAL_PROFIT'
      ? `=AND(${ref}<>"";${ref}<0)` : `=AND(ISNUMBER(${ref});${ref}<0)`, fmt(undefined, '#cc0000'), rs));
  }
  const drr = columnsFor(sections, 'DRR', 'block');
  if (drr.length) { const ref = at(drr); out.push(rule(sheetId, `=AND(${ref}<>"";${ref}>0,2)`, fmt(undefined, '#cc0000'), drr)); }
  const dateSum = sections.map((s) => [s.firstRow, s.lastRow, summaryColumn('DATE') as number] as [number, number, number]);
  if (dateSum.length) { const ref = at(dateSum); out.push(rule(sheetId, `=WEEKDAY(${ref};2)>5`, fmt('#fcefe3'), dateSum)); }
  // смещения ролей одинаковы в сводке и в блоке: дата −6, заказы −2 от «отменили»
  const canc = columnsFor(sections, 'CANCELLATIONS', 'both');
  if (canc.length) {
    const [row, , col] = canc[0] as [number, number, number];
    const dt = `${columnName(col - 6)}${row}`, od = `${columnName(col - 2)}${row}`, cx = `${columnName(col)}${row}`;
    out.push(rule(sheetId, `=AND(${dt}<=${lcdRef};ISNUMBER(${od});${od}>0;ISNUMBER(${cx});`
      + `OR(${cx}>=3;AND(${cx}>=2;${cx}*10>=${od}*3)))`, fmt('#fce8e6', '#a61c00'), canc));
  }
  // дата −4, показы −2 от «заказов»
  const ord = columnsFor(sections, 'ORDERS', 'both');
  if (ord.length) {
    const [row, , col] = ord[0] as [number, number, number];
    const dt = `${columnName(col - 4)}${row}`, im = `${columnName(col - 2)}${row}`, od = `${columnName(col)}${row}`;
    out.push(rule(sheetId, `=AND(${dt}<=TODAY();${im}>0;${od}=0)`, fmt('#fce8b2'), ord));
  }
  // шкалы считают порог от MAX своей секции → правило на секцию; узкая ступень вставляется ПОСЛЕДНЕЙ,
  // чтобы после вставки в index 0 оказаться выше широкой
  for (const role of ['ORDERS', 'INTERNAL_ADS'] as const) {
    const col = summaryColumn(role); if (col === null) continue;
    for (const s of sections) {
      const ref = `${columnName(col)}${s.firstRow}`;
      const rng = `${columnName(col)}$${s.firstRow}:${columnName(col)}$${s.lastRow}`;
      for (const [share, colour] of OZON_SCALE_TIERS[role] ?? []) {
        const cond = share === 0 ? `${ref}>0` : `${ref}>${String(share).replace('.', ',')}*MAX(${rng})`;
        out.push(rule(sheetId, `=AND(${ref}<>"";${cond})`, fmt(colour), [[s.firstRow, s.lastRow, col]]));
      }
    }
  }
  return out;
}

/**
 * «Будущий день» — серый текст после LAST_CLOSED_DATE. Ссылка на колонку даты АБСОЛЮТНАЯ
 * (столбец фиксирован, строка относительна), поэтому одно правило накрывает одну колонку
 * даты сразу во всех секциях.
 */
export function futureDayRequests(
  sheetId: number, sections: readonly SectionLayout[], lcdRef: string, fg = '#c0c0c0',
): SheetsRequest[] {
  const groups: Array<{ dateCol: number; spans: Array<[SectionLayout, number, number]> }> = [
    { dateCol: summaryColumn('DATE') as number,
      spans: sections.map((s) => [s, 1, OZON_SUMMARY_ROLES.length] as [SectionLayout, number, number]) },
  ];
  const bmax = Math.max(0, ...sections.map((s) => s.blockCount));
  for (let b = 0; b < bmax; b++) {
    const secs = sections.filter((s) => s.blockCount > b);
    if (!secs.length) continue;
    const c0 = OZON_GEOMETRY.BLOCK_FIRST_COLUMN + OZON_GEOMETRY.BLOCK_WIDTH * b;
    groups.push({ dateCol: blockColumn(b, 'DATE'),
      spans: secs.map((s) => [s, c0, c0 + OZON_GEOMETRY.BLOCK_WIDTH - 1] as [SectionLayout, number, number]) });
  }
  return groups.map(({ dateCol, spans }) => {
    const anchor = (spans[0] as [SectionLayout, number, number])[0].firstRow;
    return { addConditionalFormatRule: { rule: {
      ranges: spans.map(([s, a, b]) => ({ sheetId, startRowIndex: s.firstRow - 1, endRowIndex: s.lastRow,
        startColumnIndex: a - 1, endColumnIndex: b })),
      booleanRule: { condition: { type: 'CUSTOM_FORMULA',
        values: [{ userEnteredValue: `=$${columnName(dateCol)}${anchor}>${lcdRef}` }] },
        format: { textFormat: { foregroundColor: rgb(fg) } } },
    }, index: 0 } };
  });
}


/* ── полный язык цвета: семьи, перенесённые из WB по ролям ───────────────────── */

/** Трёхточечный градиент WB: минимум красный, медиана жёлтая, максимум зелёный. */
function gradientRule(sheetId: number, ranges: ReadonlyArray<[number, number, number]>): SheetsRequest {
  const stop = (type: string, colour: string, value?: string) => ({
    type, ...(value !== undefined ? { value } : {}), colorStyle: { rgbColor: rgb(colour) } });
  return { addConditionalFormatRule: { rule: {
    ranges: ranges.map(([a, b, c]) => ({ sheetId, startRowIndex: a - 1, endRowIndex: b,
      startColumnIndex: c - 1, endColumnIndex: c })),
    gradientRule: { minpoint: stop('MIN', '#f8696b'), midpoint: stop('PERCENTILE', '#ffeb84', '50'),
                    maxpoint: stop('MAX', '#63be7b') },
  }, index: 0 } };
}

/** Четырёхступенчатая шкала от MAX своей секции: правило на секцию, ступени сверху вниз. */
function scaleRequests(
  sheetId: number, sections: readonly SectionLayout[], role: FieldRole, scope: Scope,
): SheetsRequest[] {
  const tiers = OZON_SCALE_TIERS[role as 'ORDERS' | 'INTERNAL_ADS' | 'CART'];
  if (!tiers) return [];
  const out: SheetsRequest[] = [];
  for (const s of sections) {
    const cols: number[] = [];
    const sc = summaryColumn(role);
    if ((scope === 'summary' || scope === 'both') && sc !== null) cols.push(sc);
    if (scope !== 'summary') for (let b = 0; b < s.blockCount; b++) cols.push(blockColumn(b, role));
    if (!cols.length) continue;
    const anchorCol = cols[0] as number;
    const ref = `${columnName(anchorCol)}${s.firstRow}`;
    const rng = `${columnName(anchorCol)}$${s.firstRow}:${columnName(anchorCol)}$${s.lastRow}`;
    const ranges = cols.map((c) => [s.firstRow, s.lastRow, c] as [number, number, number]);
    for (const [share, colour] of tiers) {
      const cond = share === 0 ? `${ref}>0` : `${ref}>${String(share).replace('.', ',')}*MAX(${rng})`;
      out.push(rule(sheetId, `=AND(${ref}<>"";${cond})`, fmt(colour), ranges));
    }
  }
  return out;
}

/** Ступени оборачиваемости WB: пороги — константы, поэтому одно правило на весь лист. */
const TURNOVER_TIERS: ReadonlyArray<[string, string, string]> = [
  ['<=15', '#fce8e6', '#a61c00'], ['>15;{X}<=30', '#fff2cc', '#7f6000'],
  ['>30;{X}<=60', '#e6f4ea', '#274e13'], ['>60', '#efefef', '#434343'],
];

/** Полный набор семей условного форматирования Ozon-Юнитки. */
export function cfAllRequests(
  sheetId: number, sections: readonly SectionLayout[], lcdRef: string,
): SheetsRequest[] {
  const out: SheetsRequest[] = [...cfFamilyRequests(sheetId, sections, lcdRef)];
  const at = (rs: Array<[number, number, number]>) => {
    const [row, , col] = rs[0] as [number, number, number];
    return `${columnName(col)}${row}`;
  };
  // выходные: колонка даты блока и колонка дня недели блока + день недели сводки
  for (const role of ['DATE', 'WEEKDAY'] as const) {
    const rs = columnsFor(sections, role, role === 'DATE' ? 'block' : 'both');
    if (!rs.length) continue;
    const [row, , col] = rs[0] as [number, number, number];
    const dateCol = role === 'DATE' ? col : col - ROLE_OFFSET.WEEKDAY + ROLE_OFFSET.DATE;
    out.push(rule(sheetId, `=WEEKDAY(${columnName(dateCol)}${row};2)>5`, fmt('#fcefe3'), rs));
  }
  // Зелёный «ожидаемый факт» у Ozon даётся СТАТИЧЕСКОЙ заливкой (staticFormatRequests),
  // а не правилом УФ, как в WB. Визуально это одно и то же — доказано машинной приёмкой
  // Gate 5D; отдельного правила заводить не нужно.
  // оборачиваемость: четыре ступени с постоянными порогами
  {
    const rs = columnsFor(sections, 'TURNOVER', 'block');
    if (rs.length) {
      const ref = at(rs);
      for (const [cond, bg, fg] of TURNOVER_TIERS) {
        const body = cond.includes('{X}') ? cond.replace('{X}', ref) : cond;
        out.push(rule(sheetId, `=AND(ISNUMBER(${ref});${ref}${body})`, fmt(bg, fg), rs));
      }
    }
  }
  // доходность на 1 шт: сама задаёт белый фон и поэтому стоит выше градиента
  {
    const rs = columnsFor(sections, 'UNIT_PROFIT', 'block');
    if (rs.length) {
      const ref = at(rs);
      out.push(rule(sheetId, `=NOT(ISNUMBER(${ref}))`, fmt('#ffffff'), rs));
      out.push(rule(sheetId, `=AND(ISNUMBER(${ref});${ref}=0)`, fmt('#ffffff'), rs));
      out.push(rule(sheetId, `=AND(ISNUMBER(${ref});${ref}>0)`, fmt('#ffffff', '#38761d'), rs));
      out.push(rule(sheetId, `=AND(ISNUMBER(${ref});${ref}<0)`, fmt('#ffffff', '#cc0000'), rs));
    }
  }
  // знак результата в сводке (в блоках он уже поставлен cfFamilyRequests)
  {
    const rs = columnsFor(sections, 'TOTAL_PROFIT', 'summary');
    if (rs.length) {
      const ref = at(rs);
      out.push(rule(sheetId, `=AND(ISNUMBER(${ref});${ref}>0)`, fmt(undefined, '#38761d'), rs));
      out.push(rule(sheetId, `=AND(${ref}<>"";${ref}<0)`, fmt(undefined, '#cc0000'), rs));
    }
  }
  // шкалы от MAX секции: заказы, корзина и реклама — и в сводке, и в блоках
  out.push(...scaleRequests(sheetId, sections, 'ORDERS', 'block'));
  out.push(...scaleRequests(sheetId, sections, 'CART', 'both'));
  out.push(...scaleRequests(sheetId, sections, 'INTERNAL_ADS', 'block'));
  // градиенты: на секцию, ниже непрозрачных булевых правил (порядок держит contract порядка)
  for (const s of sections) {
    for (const role of ['UNIT_PROFIT', 'TOTAL_PROFIT', 'CART'] as const) {
      const cols: Array<[number, number, number]> = [];
      const sc = summaryColumn(role);
      if (sc !== null) cols.push([s.firstRow, s.lastRow, sc]);
      for (let b = 0; b < s.blockCount; b++) cols.push([s.firstRow, s.lastRow, blockColumn(b, role)]);
      if (cols.length) out.push(gradientRule(sheetId, cols));
    }
  }
  return out;
}

/* ── контракты порядка правил ───────────────────────────────────────────────── */

export interface LiveRule {
  ranges?: Array<{ startColumnIndex?: number; endColumnIndex?: number }>;
  gradientRule?: unknown;
  booleanRule?: { condition?: { values?: Array<{ userEnteredValue?: string }> };
                  format?: { backgroundColor?: { red?: number; green?: number; blue?: number } } };
}
const hex = (c?: { red?: number; green?: number; blue?: number }): string =>
  (['red', 'green', 'blue'] as const).map((k) => Math.round(255 * (c?.[k] ?? 0)).toString(16).padStart(2, '0')).join('');

function covers(r: LiveRule, cols: ReadonlySet<number>): boolean {
  return (r.ranges ?? []).some((g) => {
    const a = (g.startColumnIndex ?? 0) + 1, b = g.endColumnIndex ?? 0;
    for (const c of cols) if (a <= c && c <= b) return true;
    return false;
  });
}

/**
 * Градиент и булево правило — разные слои, верхним оказывается тот, что выше в списке.
 * Роли, задающие собственный фон (OPAQUE_BOOLEAN_ROLES), обязаны стоять ВЫШЕ градиента:
 * иначе «Доходность на 1 шт» покрасится градиентом вместо белого, как у WB.
 */
export function gradientOrderViolations(
  sections: readonly SectionLayout[], rules: readonly LiveRule[],
): number[] {
  const cols = new Set<number>();
  for (const s of sections) for (let b = 0; b < s.blockCount; b++) for (const r of OPAQUE_BOOLEAN_ROLES) cols.add(blockColumn(b, r));
  const grads: number[] = []; const opaque: number[] = [];
  rules.forEach((r, i) => {
    if (!covers(r, cols)) return;
    if (r.gradientRule) grads.push(i);
    else if (r.booleanRule?.format?.backgroundColor) opaque.push(i);
  });
  return grads.filter((g) => opaque.some((o) => o > g));
}

const SCALE_COLOURS = new Set(Object.values(OZON_SCALE_TIERS).flatMap((t) => t.map(([, c]) => c.replace('#', ''))));
/** Порог ступени из формулы: `>0,25*MAX(…)` → 0.25; `>0` → 0. Запятая — локаль ru_RU. */
export const thresholdOf = (f: string): number => {
  const m = /<?>(\d+),(\d+)\*MAX/.exec(f);
  return m ? Number(`${m[1]}.${m[2]}`) : 0;
};

/**
 * Срабатывает ПЕРВОЕ подходящее булево правило, поэтому узкая ступень шкалы обязана стоять
 * выше широкой. Обратный порядок схлопывает четыре ступени в одну бледную.
 */
export function scaleOrderMoves(rules: readonly LiveRule[]): Array<[number, number]> {
  const groups = new Map<string, Array<[number, number]>>();
  rules.forEach((r, i) => {
    const bg = r.booleanRule?.format?.backgroundColor; if (!bg) return;
    const colour = hex(bg); if (!SCALE_COLOURS.has(colour)) return;
    const fam = Object.entries(OZON_SCALE_TIERS).find(([, t]) => t.some(([, c]) => c.replace('#', '') === colour))?.[0];
    const key = `${fam}|${JSON.stringify(r.ranges)}`;
    const f = r.booleanRule?.condition?.values?.[0]?.userEnteredValue ?? '';
    (groups.get(key) ?? groups.set(key, []).get(key) as Array<[number, number]>).push([i, thresholdOf(f)]);
  });
  const moves: Array<[number, number]> = [];
  for (const items of groups.values()) {
    const idx = items.map(([i]) => i);
    const want = [...items].sort((a, b) => b[1] - a[1]).map(([i]) => i);
    if (idx.join() === want.join()) continue;
    const cur = [...idx];
    want.forEach((target, pos) => {
      const j = cur.indexOf(target);
      if (j === pos) return;
      moves.push([idx[j] as number, idx[pos] as number]);
      cur.splice(pos, 0, ...cur.splice(j, 1));
    });
  }
  return moves;
}
