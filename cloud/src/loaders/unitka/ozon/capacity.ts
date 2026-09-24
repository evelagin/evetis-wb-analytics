/**
 * OZON — ДИНАМИЧЕСКАЯ ЁМКОСТЬ ЛИСТА (Gate 10). Чистый модуль: ни Sheets, ни BigQuery.
 *
 * ЗАЧЕМ. До Gate 10 число слотов было КОНСТАНТОЙ окружения (OZON_UNITKA_BLOCK_SLOTS = 22), и
 * 23-й SKU упирался в NoFreeSkuSlotError. Это был честный safe-fail, но на 23.09.2026 занято
 * 22 из 22: следующий же новый товар остановил бы прогон. Ёмкость должна расти сама.
 *
 * ГЛАВНАЯ ЛОВУШКА — ХВОСТ ВЛАДЕЛЬЦА. Справа от блоков лежит ЕГО таблица («OZON выплаты 2025»,
 * колонки 562–566) и зеркало LCD. Вставка колонок сдвигает хвост вправо; Google Sheets сам
 * пересчитает формулы, именованные диапазоны и ссылки внутри хвоста. А вот ЧИСЛО 562,
 * записанное в переменной окружения, не сдвинется — и следующий прогон принял бы чужие
 * колонки за свои и затёр бы таблицу владельца.
 *
 * ПОЭТОМУ ГРАНИЦА ХВОСТА ВЫВОДИТСЯ ИЗ САМОГО ЛИСТА, а не из окружения:
 *
 *     tailFirst = BLOCK_FIRST_COLUMN + slots × BLOCK_WIDTH
 *
 * где slots — число РАЗМЕЧЕННЫХ слотов (у каждого в строке шапки стоит своя подпись). Сегодня
 * 12 + 22×25 = 562 — совпадает с конфигом до колонки. После расширения до 23 слотов формула
 * сама даёт 587. Значение из окружения остаётся ПЕРЕКРЁСТНОЙ ПРОВЕРКОЙ: расхождение без
 * запланированного расширения — это дрейф структуры, а не повод доверять конфигу.
 */

import { OZON_GEOMETRY, ozonSlotStart } from './contract.js';

/** Сколько слотов размечено в листе: по подписям шапки на старте каждого слота. */
export function markedSlots(headerRow: readonly unknown[], maxScan = 400): number {
  let n = 0;
  for (let slot = 0; slot < maxScan; slot++) {
    const c = ozonSlotStart(slot);
    const v = headerRow[c - 1];
    if (v === undefined || v === null || String(v).trim() === '') break;
    n++;
  }
  return n;
}

/** Первая колонка хвоста владельца — из геометрии листа, НЕ из окружения. */
export function tailFirstColumn(slots: number): number {
  if (!Number.isInteger(slots) || slots < 1) throw new RangeError(`слотов ${slots}`);
  return OZON_GEOMETRY.BLOCK_FIRST_COLUMN + slots * OZON_GEOMETRY.BLOCK_WIDTH;
}

export type ExpansionRefusal =
  | { readonly code: 'NO_SAFE_EXPANSION_PATH'; readonly why: string };

export interface ExpansionPlan {
  /** Сколько слотов добавляем. 0 — расширение не нужно. */
  readonly addSlots: number;
  /** 0-based индекс вставки = число колонок перед ней. Ровно граница хвоста. */
  readonly insertAt: number;
  /** Сколько колонок вставляем. */
  readonly insertCount: number;
  readonly slotsBefore: number;
  readonly slotsAfter: number;
  readonly tailFirstBefore: number;
  readonly tailFirstAfter: number;
  /** Колонки существующих блоков обязаны остаться на месте — здесь это видно числом. */
  readonly existingBlocksLastColumn: number;
}

export interface ExpansionInput {
  /** Размечено слотов сейчас (из листа). */
  readonly slotsNow: number;
  /** Сколько блоков потребуется после активации новых SKU. */
  readonly blocksNeeded: number;
  /** Граница хвоста, как её видит лист. */
  readonly tailFirstLive: number;
  /** Ширина листа в колонках: вставка не должна вылезти за пределы возможного. */
  readonly sheetColumnCount: number;
  /** Предохранитель: больше этого за один прогон не расширяемся. */
  readonly maxAddPerRun?: number;
}

/** Предел Google Sheets на число колонок листа. Вставка сверх него невозможна физически. */
export const SHEETS_MAX_COLUMNS = 18_278;

/**
 * План расширения. Либо доказуемо безопасный план, либо отказ — третьего не дано:
 * частичная мутация запрещена (§19).
 *
 * Проверяется ровно то, что может разрушить лист:
 *   • граница хвоста в листе обязана совпасть с выводимой из числа слотов — иначе мы не
 *     понимаем раскладку и не имеем права вставлять колонки;
 *   • вставка идёт СТРОГО на границе хвоста, поэтому ни один существующий блок не двигается;
 *   • итоговая ширина обязана уместиться в лимит Sheets.
 */
export function planExpansion(inp: ExpansionInput): ExpansionPlan | ExpansionRefusal {
  const { slotsNow, blocksNeeded, tailFirstLive, sheetColumnCount } = inp;
  const maxAdd = inp.maxAddPerRun ?? 8;

  if (!Number.isInteger(slotsNow) || slotsNow < 1) {
    return { code: 'NO_SAFE_EXPANSION_PATH', why: `в листе размечено ${slotsNow} слотов` };
  }
  const derived = tailFirstColumn(slotsNow);
  if (derived !== tailFirstLive) {
    return { code: 'NO_SAFE_EXPANSION_PATH',
      why: `граница хвоста в листе ${tailFirstLive}, а из ${slotsNow} слотов следует ${derived}: `
        + 'раскладка не опознана, вставка колонок могла бы затереть хвост владельца' };
  }
  const addSlots = blocksNeeded - slotsNow;
  if (addSlots <= 0) {
    return { addSlots: 0, insertAt: tailFirstLive - 1, insertCount: 0,
      slotsBefore: slotsNow, slotsAfter: slotsNow,
      tailFirstBefore: tailFirstLive, tailFirstAfter: tailFirstLive,
      existingBlocksLastColumn: tailFirstLive - 1 };
  }
  if (addSlots > maxAdd) {
    return { code: 'NO_SAFE_EXPANSION_PATH',
      why: `запрошено ${addSlots} новых слотов за один прогон при пределе ${maxAdd}: `
        + 'столько новых SKU сразу — скорее дефект справочника, чем ассортимент' };
  }
  const insertCount = addSlots * OZON_GEOMETRY.BLOCK_WIDTH;
  if (sheetColumnCount + insertCount > SHEETS_MAX_COLUMNS) {
    return { code: 'NO_SAFE_EXPANSION_PATH',
      why: `${sheetColumnCount} + ${insertCount} колонок превысит предел листа ${SHEETS_MAX_COLUMNS}` };
  }
  const slotsAfter = slotsNow + addSlots;
  return {
    addSlots,
    insertAt: tailFirstLive - 1,
    insertCount,
    slotsBefore: slotsNow,
    slotsAfter,
    tailFirstBefore: tailFirstLive,
    tailFirstAfter: tailFirstColumn(slotsAfter),
    existingBlocksLastColumn: tailFirstLive - 1,
  };
}

export function isRefusal(x: ExpansionPlan | ExpansionRefusal): x is ExpansionRefusal {
  return (x as ExpansionRefusal).code === 'NO_SAFE_EXPANSION_PATH';
}

/** Колонки слота ПОСЛЕ расширения — для проверки, что старые блоки не поехали. */
export function slotColumns(slot: number): { start: number; end: number } {
  const start = ozonSlotStart(slot);
  return { start, end: start + OZON_GEOMETRY.BLOCK_WIDTH - 1 };
}
