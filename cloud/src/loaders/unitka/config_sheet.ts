/**
 * ZZ_CONFIG — СХЕМА ПАРАМЕТРОВ ЖИЗНЕННОГО ЦИКЛА (Gate 10). Чистый модуль без I/O.
 *
 * 🔴 ПОЧЕМУ НЕ B3, КАК БЫЛО НАЗВАНО В ЗАДАНИИ. B3 ЗАНЯТА: там TOTAL_COMMISSION_RATE = 0,4532,
 * которая через колонку AD входит в AE = AA − AA×AD. Запись Ozon-LCD в B3 уничтожила бы
 * ставку комиссии WB и молча исказила всю экономику книги. Свободны строки 28+ (лист 77×3),
 * поэтому параметры цикла живут отдельным блоком с 29-й строки, а B2 остаётся в точности
 * тем, чем была, — LCD платформы WB.
 *
 * РАСКЛАДКА ZZ_CONFIG на 23.09.2026:
 *   2  LAST_CLOSED_DATE          ← WB, именованный диапазон, НЕ трогаем
 *   3  TOTAL_COMMISSION_RATE     ← финансовый параметр, НЕ трогаем
 *   4  TAX_RESERVE_RATE          ← финансовый параметр, НЕ трогаем
 *   6–27 блоки логистики/хранения/эквайринга
 *   28 пусто — разделитель
 *   29 заголовок блока Gate 10
 *   30 OZON_LAST_CLOSED_DATE     ← новый именованный диапазон, авторитет Ozon
 *   31 WB_LCD_MODE      32 WB_MANUAL_LCD
 *   33 OZON_LCD_MODE    34 OZON_MANUAL_LCD
 *
 * Режимы НЕ кладутся в ячейки LCD (§8): дата и режим — разные величины, и смешивать их
 * означало бы гадать по содержимому, а гадание здесь уже один раз стоило суток данных.
 */

import { parseLcdMode, isIsoDate, type LcdMode } from './lcd.js';

/** Строки ZZ_CONFIG, которые занимать нельзя: на них стоит живая экономика. */
export const ZZ_CONFIG_RESERVED_ROWS: Readonly<Record<number, string>> = {
  2: 'LAST_CLOSED_DATE',
  3: 'TOTAL_COMMISSION_RATE',
  4: 'TAX_RESERVE_RATE',
};

export const ZZ_CONFIG_SHEET = 'ZZ_CONFIG';

/** Параметр ZZ_CONFIG: строка листа + подпись в колонке A. */
export interface ConfigParam {
  readonly row: number;
  readonly label: string;
  readonly kind: 'DATE' | 'MODE' | 'HEADING';
}

export const GATE10_BLOCK_ROW = 29;

export const GATE10_PARAMS = {
  heading:      { row: 29, label: 'ЖИЗНЕННЫЙ ЦИКЛ — AUTO-LCD', kind: 'HEADING' },
  ozonLcd:      { row: 30, label: 'OZON_LAST_CLOSED_DATE', kind: 'DATE' },
  wbMode:       { row: 31, label: 'WB_LCD_MODE', kind: 'MODE' },
  wbManualLcd:  { row: 32, label: 'WB_MANUAL_LCD', kind: 'DATE' },
  ozonMode:     { row: 33, label: 'OZON_LCD_MODE', kind: 'MODE' },
  ozonManualLcd:{ row: 34, label: 'OZON_MANUAL_LCD', kind: 'DATE' },
} as const satisfies Readonly<Record<string, ConfigParam>>;

/** Именованный диапазон авторитетного LCD Ozon. WB продолжает жить на LAST_CLOSED_DATE. */
export const OZON_LCD_NAMED_RANGE = 'OZON_LAST_CLOSED_DATE';
export const WB_LCD_NAMED_RANGE = 'LAST_CLOSED_DATE';

export function a1(param: ConfigParam): string {
  return `${ZZ_CONFIG_SHEET}!B${param.row}`;
}
export function labelA1(param: ConfigParam): string {
  return `${ZZ_CONFIG_SHEET}!A${param.row}`;
}

/**
 * Проверка, что схема не наезжает на живые параметры. Вызывается тестом И перед записью:
 * ошибка здесь дешевле, чем затёртая ставка комиссии.
 */
export function assertNoCollision(): void {
  for (const p of Object.values(GATE10_PARAMS) as ConfigParam[]) {
    const taken = ZZ_CONFIG_RESERVED_ROWS[p.row];
    if (taken) {
      throw new Error(`ZZ_CONFIG строка ${p.row} занята параметром ${taken}: `
        + `размещать там ${p.label} нельзя — это молча исказило бы экономику книги`);
    }
    if (p.row < GATE10_BLOCK_ROW) {
      throw new Error(`ZZ_CONFIG строка ${p.row} (${p.label}) выше блока Gate 10 (${GATE10_BLOCK_ROW})`);
    }
  }
}

/** Ячейки, которые создаёт миграция: подпись в A, значение в B, пояснение в C. */
export interface ConfigSeedCell {
  readonly range: string;
  readonly value: string;
}

/**
 * Начальное наполнение блока. AUTO — production-умолчание; ручные даты остаются ПУСТЫМИ,
 * чтобы случайное значение не включило override.
 */
export function gate10SeedCells(ozonLcdIso: string): ConfigSeedCell[] {
  assertNoCollision();
  const P = GATE10_PARAMS;
  return [
    { range: labelA1(P.heading), value: P.heading.label },
    { range: labelA1(P.ozonLcd), value: P.ozonLcd.label },
    { range: a1(P.ozonLcd), value: ozonLcdIso },
    { range: labelA1(P.wbMode), value: P.wbMode.label },
    { range: a1(P.wbMode), value: 'AUTO' },
    { range: labelA1(P.wbManualLcd), value: P.wbManualLcd.label },
    { range: labelA1(P.ozonMode), value: P.ozonMode.label },
    { range: a1(P.ozonMode), value: 'AUTO' },
    { range: labelA1(P.ozonManualLcd), value: P.ozonManualLcd.label },
  ];
}

/* ─────────────────────────── чтение блока цикла ─────────────────────────── */


/** Серийная дата Sheets (эпоха 1899-12-30) → ISO. */
const SHEET_EPOCH_MS = Date.UTC(1899, 11, 30);

/**
 * Значение ячейки-даты → ISO. Владелец может ввести дату как дату (придёт серийным числом),
 * как ISO-текст или как «22.09.2026» (вид книги в ru_RU). Всё остальное — не дата.
 */
export function dateCellToIso(v: unknown): string | null {
  if (typeof v === 'number' && Number.isFinite(v) && Number.isInteger(v) && v > 0) {
    return new Date(SHEET_EPOCH_MS + v * 86_400_000).toISOString().slice(0, 10);
  }
  const s = String(v ?? '').trim();
  if (s === '') return null;
  if (isIsoDate(s)) return s;
  const m = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(s);
  if (m && isIsoDate(`${m[3]}-${m[2]}-${m[1]}`)) return `${m[3]}-${m[2]}-${m[1]}`;
  return null;
}

export interface PlatformLcdConfig {
  readonly mode: LcdMode;
  /** Значение MANUAL_LCD в ISO; undefined — ячейка пуста; null — заполнена НЕ датой. */
  readonly manualLcd: string | null | undefined;
  /** Сырое значение ручной даты — для журнала: «что именно ввёл владелец». */
  readonly manualRaw: unknown;
}

export type LifecycleConfig =
  | { readonly state: 'ABSENT' }
  | { readonly state: 'DRIFT'; readonly issues: readonly string[] }
  | { readonly state: 'INVALID'; readonly issues: readonly string[] }
  | { readonly state: 'PRESENT'; readonly wb: PlatformLcdConfig; readonly ozon: PlatformLcdConfig;
      /** Значение B30 (авторитет Ozon) — для сверки с именованным диапазоном. */
      readonly ozonLcdCell: string | null };

/** Диапазон, который читает писатель: подписи A и значения B строк 29..34. */
export const GATE10_BLOCK_RANGE = `${ZZ_CONFIG_SHEET}!A${GATE10_PARAMS.heading.row}:B${GATE10_PARAMS.ozonManualLcd.row}`;

/**
 * Разбор блока цикла. Три состояния, которые нельзя путать:
 *   ABSENT  — блока нет вовсе (production до миграции): писатель ведёт себя по-прежнему;
 *   DRIFT   — блок есть, но подписи не те: кто-то переставил строки — fail-closed, ведь
 *             прочитать «режим» из чужой ячейки хуже, чем не прочитать никакой;
 *   INVALID — подписи верны, но значение режима непонятно: отказ, а не молчаливый AUTO.
 */
export function parseLifecycleBlock(rows: ReadonlyArray<ReadonlyArray<unknown>>): LifecycleConfig {
  const at = (row: number): ReadonlyArray<unknown> => rows[row - GATE10_PARAMS.heading.row] ?? [];
  const params = Object.values(GATE10_PARAMS) as ConfigParam[];
  const labels = params.map((p) => String(at(p.row)[0] ?? '').trim());
  if (labels.every((l) => l === '') && params.every((p) => String(at(p.row)[1] ?? '').trim() === '')) {
    return { state: 'ABSENT' };
  }
  const drift = params.filter((p, i) => labels[i] !== p.label)
    .map((p) => `ZZ_CONFIG!A${p.row}: ожидается «${p.label}», в книге «${String(at(p.row)[0] ?? '')}»`);
  if (drift.length) return { state: 'DRIFT', issues: drift };

  const P = GATE10_PARAMS;
  const issues: string[] = [];
  const platform = (modeP: ConfigParam, manualP: ConfigParam): PlatformLcdConfig | null => {
    const m = parseLcdMode(at(modeP.row)[1]);
    if ('code' in m) { issues.push(`${a1(modeP)} = «${m.got}»: режим LCD — AUTO или MANUAL`); return null; }
    const raw = at(manualP.row)[1];
    const empty = raw === undefined || raw === null || String(raw).trim() === '';
    return { mode: m.mode, manualLcd: empty ? undefined : dateCellToIso(raw), manualRaw: empty ? null : raw };
  };
  const wb = platform(P.wbMode, P.wbManualLcd);
  const ozon = platform(P.ozonMode, P.ozonManualLcd);
  if (!wb || !ozon) return { state: 'INVALID', issues };
  return { state: 'PRESENT', wb, ozon, ozonLcdCell: dateCellToIso(at(P.ozonLcd.row)[1]) };
}

/** Минимальный шлюз чтения — чтобы модуль не тянул за собой весь SheetsGateway. */
export interface ValueReader {
  readValues(ranges: string[]): Promise<unknown[][][]>;
}

export async function readLifecycleConfig(sheets: ValueReader): Promise<LifecycleConfig> {
  const [grid] = await sheets.readValues([GATE10_BLOCK_RANGE]);
  return parseLifecycleBlock(grid ?? []);
}
