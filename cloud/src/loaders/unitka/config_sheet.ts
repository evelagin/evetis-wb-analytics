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
