/**
 * OZON UNITKA — ЖИЗНЕННЫЙ ЦИКЛ СЕКЦИЙ И SKU (Gate 9). Чистый модуль: ни Sheets, ни BigQuery.
 *
 * Суточный прогон обязан переживать две смены без единой ручной правки листа:
 *   • наступил новый месяц — секция создаётся сама, из геометрии предыдущей;
 *   • появился новый SKU  — блок активируется сам, в свободном слоте.
 *
 * Ни то, ни другое не берётся из подписи в листе: подпись — это текст, который владелец
 * может опечатать (и опечатал: слот 14 подписан «9099514444» при каноническом «909951444»).
 * Идентичность SKU приходит только из справочника каналов.
 *
 * ЁМКОСТЬ. Физических слотов блоков фиксированное число. 23-й SKU при 22 слотах — это
 * СОБЫТИЕ ЁМКОСТИ, а не рядовая активация. Молча переписать чужой блок нельзя ни при каких
 * условиях: лист потеряет историю SKU, которую никто не восстановит. Поэтому при нехватке
 * слота прогон ОТКАЗЫВАЕТСЯ (NO_FREE_SKU_SLOT) и не пишет ничего.
 */

/** Секция месяца, как она существует (или должна существовать) в листе. */
export interface SectionPlan {
  readonly monthKey: string;          // '2026-10'
  readonly titleRow: number;
  readonly headerRow: number;
  readonly days: number;
  readonly blocks: readonly string[];
  /** Секции ещё нет в листе — её надо создать. */
  readonly isNew: boolean;
}

export interface LiveSectionInput {
  readonly monthKey: string;
  readonly titleRow: number;
  readonly days: readonly string[];
  readonly blocks: readonly string[];
}

/** Ошибка ёмкости: свободных слотов нет. Прогон обязан упасть, а не переписать чужой блок. */
export class NoFreeSkuSlotError extends Error {
  readonly code = 'NO_FREE_SKU_SLOT';
  constructor(readonly needed: number, readonly available: number, readonly newSkus: readonly string[]) {
    super(`нужно слотов ${needed}, размечено ${available}; новые SKU: ${newSkus.join(', ')}. `
      + 'Запись отменена: переписать существующий блок нельзя — лист потеряет историю SKU.');
    this.name = 'NoFreeSkuSlotError';
  }
}

export function daysInMonth(monthKey: string): number {
  const y = Number(monthKey.slice(0, 4)), m = Number(monthKey.slice(5, 7));
  return new Date(Date.UTC(y, m, 0)).getUTCDate();
}

/** Шаг секции Calendar V2: заголовок + шапка + дни + итог + разделитель. */
export const sectionStep = (days: number): number => days + 4;

/**
 * Секции, которые должен обслужить прогон: существующие в листе + недостающие, созданные
 * из геометрии последней существующей.
 *
 * Состав блоков новой секции = блоки предыдущей ПЛЮС SKU, впервые появившиеся в этом месяце.
 * Порядок существующих блоков НИКОГДА не меняется: блок живёт в своём слоте всю жизнь листа,
 * иначе история SKU уехала бы в чужую колонку.
 */
export function resolveSections(a: {
  readonly live: readonly LiveSectionInput[];
  readonly windowMonths: readonly string[];
  /** offer_id → месяц первой активности ('2026-10'). Источник — витрина, не подпись листа. */
  readonly firstActivity: Readonly<Record<string, string>>;
  readonly blockSlots: number;
}): SectionPlan[] {
  const byMonth = new Map(a.live.map((s) => [s.monthKey, s]));
  const ordered = [...a.live].sort((x, y) => x.titleRow - y.titleRow);
  const last = ordered[ordered.length - 1];
  if (!last) throw new Error('в листе нет ни одной секции месяца — создавать не от чего');

  const out: SectionPlan[] = [];
  let tailRow = last.titleRow, tailDays = last.days.length;
  let carried: string[] = [...last.blocks];

  for (const monthKey of [...a.windowMonths].sort()) {
    const live = byMonth.get(monthKey);
    if (live) {
      out.push({ monthKey, titleRow: live.titleRow, headerRow: live.titleRow + 1,
        days: live.days.length, blocks: [...live.blocks], isNew: false });
      if (live.titleRow >= tailRow) { tailRow = live.titleRow; tailDays = live.days.length; carried = [...live.blocks]; }
      continue;
    }
    if (monthKey <= last.monthKey) {
      throw new Error(`секция ${monthKey} отсутствует в листе, но она НЕ новее последней `
        + `(${last.monthKey}): достраивать прошлое движок не имеет права`);
    }
    // новые SKU этого месяца — из справочника активности, а не из текста листа
    const fresh = Object.entries(a.firstActivity)
      .filter(([o, m]) => m <= monthKey && !carried.includes(o))
      .map(([o]) => o).sort();
    const blocks = [...carried, ...fresh];
    if (blocks.length > a.blockSlots) {
      throw new NoFreeSkuSlotError(blocks.length, a.blockSlots, fresh);
    }
    const titleRow = tailRow + sectionStep(tailDays);
    const days = daysInMonth(monthKey);
    out.push({ monthKey, titleRow, headerRow: titleRow + 1, days, blocks, isNew: true });
    tailRow = titleRow; tailDays = days; carried = blocks;
  }
  return out;
}

/**
 * Блоки существующей секции, дополненные SKU, которые начали продаваться внутри месяца.
 * Новый SKU активируется в ТЕКУЩЕМ месяце, не дожидаясь следующего.
 */
export function activateNewSkus(a: {
  readonly current: readonly string[];
  readonly monthKey: string;
  readonly firstActivity: Readonly<Record<string, string>>;
  readonly blockSlots: number;
}): { blocks: string[]; activated: string[] } {
  const activated = Object.entries(a.firstActivity)
    .filter(([o, m]) => m <= a.monthKey && !a.current.includes(o))
    .map(([o]) => o).sort();
  const blocks = [...a.current, ...activated];
  if (blocks.length > a.blockSlots) throw new NoFreeSkuSlotError(blocks.length, a.blockSlots, activated);
  return { blocks, activated };
}

/** Сколько строк нужно дописать, чтобы поместились все новые секции. */
export function rowsNeeded(plans: readonly SectionPlan[], currentRows: number): number {
  let last = currentRows;
  for (const p of plans) {
    if (!p.isNew) continue;
    last = Math.max(last, p.titleRow + sectionStep(p.days) - 1);
  }
  return Math.max(0, last - currentRows);
}
