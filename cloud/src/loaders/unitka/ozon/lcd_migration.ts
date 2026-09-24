/**
 * OZON — ПЕРЕВОД ФОРМУЛ НА СОБСТВЕННЫЙ LCD (Gate 10). Чистый модуль без I/O.
 *
 * ЗАЧЕМ. До Gate 10 формулы листа Ozon ссылались на общий именованный диапазон
 * LAST_CLOSED_DATE (ZZ_CONFIG!B2), который принадлежит WB. Из-за этого 23.09.2026 сдвиг
 * даты по готовности WB немедленно объявил 22.09 закрытым и на листе Ozon — хотя писатель
 * Ozon в тот день вообще не запускался. Один параметр не может иметь двух владельцев.
 *
 * ЗАМЕР ПОВЕРХНОСТИ (живой лист, 23.09.2026): 32 967 ячеек со ссылкой на LAST_CLOSED_DATE.
 * Из них 32 966 — в шести секциях, которыми управляет движок (Апрель…Сентябрь 2026), и
 * ровно 1 — зеркало VA2 в хвосте владельца. В одиннадцати легаси-секциях (Май 2025…Март 2026)
 * ссылок НЕТ ВООБЩЕ: они старше эпохи Ozon (2026-04-17). Замороженную историю владельца
 * миграция не трогает — это не предположение, а результат полного обхода листа.
 *
 * ПОЧЕМУ ЭТО ПРОВЕРЯЕМО ДО ПОСЛЕДНЕЙ ЯЧЕЙКИ. Миграция — подстановка ОДНОГО идентификатора,
 * а новая ячейка засевается ТЕКУЩИМ значением старой. Значит после миграции каждая формула
 * обязана дать БАЙТ-В-БАЙТ то же значение. Любое расхождение — регрессия, а не «ожидаемый
 * эффект миграции». Это и есть FINANCIAL_REGRESSION=0, измеряемый, а не декларируемый.
 */

const WB_NAME = 'LAST_CLOSED_DATE';
const OZON_NAME = 'OZON_LAST_CLOSED_DATE';

/**
 * Границы идентификатора. Подменять надо ТОЛЬКО самостоятельное имя: внутри
 * OZON_LAST_CLOSED_DATE тоже есть подстрока LAST_CLOSED_DATE, и наивная замена
 * на повторном прогоне дала бы OZON_OZON_LAST_CLOSED_DATE.
 */
const TOKEN = /(^|[^A-Za-z0-9_])LAST_CLOSED_DATE(?![A-Za-z0-9_])/g;

export function repointFormula(formula: string): string {
  return formula.replace(TOKEN, (_m, pre: string) => `${pre}${OZON_NAME}`);
}

export function referencesWbLcd(formula: unknown): boolean {
  if (typeof formula !== 'string') return false;
  TOKEN.lastIndex = 0;
  return TOKEN.test(formula);
}

export interface MigrationCell {
  readonly row: number;
  readonly col: number;
  readonly before: string;
  readonly after: string;
}

export interface MigrationPlan {
  readonly cells: readonly MigrationCell[];
  /** Ячейки, уже переведённые ранее: повторный прогон обязан их пропустить. */
  readonly alreadyMigrated: number;
  /** Ссылки, попавшие в хвост владельца, — их надо видеть отдельно и глазами. */
  readonly inOwnerTail: readonly MigrationCell[];
  readonly scannedRows: number;
}

export interface MigrationScanInput {
  /** Сетка формул листа: grid[r][c], 0-based, начиная с firstRow/колонки A. */
  readonly grid: ReadonlyArray<ReadonlyArray<unknown>>;
  readonly firstRow: number;
  /** Первая колонка хвоста владельца (выводится из раскладки, не из окружения). */
  readonly tailFirstColumn: number;
  /** Строки секций, которыми движок НЕ управляет: их трогать запрещено. */
  readonly frozenRows?: (row: number) => boolean;
}

/**
 * План миграции. Ничего не меняет — только перечисляет ячейки и их новое содержимое,
 * чтобы владелец увидел объём до записи, а тест — сравнил до/после.
 */
export function planLcdMigration(inp: MigrationScanInput): MigrationPlan {
  const cells: MigrationCell[] = [];
  const tail: MigrationCell[] = [];
  let already = 0;
  for (let i = 0; i < inp.grid.length; i++) {
    const row = inp.firstRow + i;
    if (inp.frozenRows?.(row)) continue;
    const line = inp.grid[i] ?? [];
    for (let j = 0; j < line.length; j++) {
      const v = line[j];
      if (typeof v !== 'string' || !v.includes('CLOSED_DATE')) continue;
      if (!referencesWbLcd(v)) { if (v.includes(OZON_NAME)) already++; continue; }
      const cell: MigrationCell = { row, col: j + 1, before: v, after: repointFormula(v) };
      cells.push(cell);
      if (cell.col >= inp.tailFirstColumn) tail.push(cell);
    }
  }
  return { cells, alreadyMigrated: already, inOwnerTail: tail, scannedRows: inp.grid.length };
}

/**
 * Проверка плана перед записью. Миграция обязана менять ТОЛЬКО идентификатор: если из
 * формулы исчезло что-то ещё, план отвергается целиком, а не «частично применяется».
 */
export function verifyPlan(plan: MigrationPlan): { ok: true } | { ok: false; why: string } {
  for (const c of plan.cells) {
    if (c.before === c.after) return { ok: false, why: `${c.row}:${c.col} — формула не изменилась` };
    if (c.after.includes(`${OZON_NAME.slice(0, 5)}_${OZON_NAME}`)) {
      return { ok: false, why: `${c.row}:${c.col} — двойная подстановка имени` };
    }
    // единственная разрешённая разница — длина имени на каждое вхождение
    const hits = (c.before.match(TOKEN) ?? []).length;
    TOKEN.lastIndex = 0;
    const grew = c.after.length - c.before.length;
    if (grew !== hits * (OZON_NAME.length - WB_NAME.length)) {
      return { ok: false, why: `${c.row}:${c.col} — изменилось не только имя (Δ${grew} при ${hits} вхождениях)` };
    }
    if (c.before.replace(TOKEN, '§') !== c.after.replace(new RegExp(`(^|[^A-Za-z0-9_])${OZON_NAME}(?![A-Za-z0-9_])`, 'g'), '§')) {
      return { ok: false, why: `${c.row}:${c.col} — остальной текст формулы не совпал` };
    }
  }
  return { ok: true };
}
