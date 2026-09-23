/**
 * AUTO-LCD — ЖИЗНЕННЫЙ ЦИКЛ ЗАКРЫТИЯ СУТОК (Gate 10). Чистый модуль: ни Sheets, ни BigQuery.
 *
 * ЧТО ТАКОЕ LAST_CLOSED_DATE. «Последний день, который успешно прошёл operational-цикл и
 * безопасно опубликован в книге». Это НЕ «бухгалтерски закрытый период»: у Ozon поздние
 * начисления приходят внутрь окна перезаписи 45 суток, и это нормально. LCD подтверждает
 * ПУБЛИКАЦИЮ, а не окончательную зрелость денег.
 *
 * УРОК ИНЦИДЕНТА 23.09.2026. Источники Ozon за 22.09 были готовы, расписание сработало, но
 * Job не был вызван (не выдан run.invoker) — записи не произошло. LCD при этом был передвинут
 * ВРУЧНУЮ на 22.09. В книге получилось состояние «день закрыт, но не записан»: строка 22.09
 * осталась скелетом, а формулы уже считали её закрытой. Отсюда главное правило модуля:
 *
 *     SOURCE_READY  — необходимое, но НЕ достаточное условие.
 *     Коммит разрешён только после WRITE + READBACK + INTEGRITY.
 *
 * ПОЧЕМУ СМЕЖНОСТЬ СЧИТАЕТСЯ ВПЕРЁД ОТ ЗАКОММИЧЕННОГО LCD, А НЕ ОТ ЭПОХИ. Журналы прогонов
 * хранят историю короче, чем сами данные (`LOADER_RUNS` по воронке начинается 2026-09-10,
 * тогда как данные воронки есть с 2026-09-04). Обход «с начала времён» объявил бы старые
 * сутки непокрытыми и УТАЩИЛ БЫ LCD НАЗАД. Обход вперёд от уже закоммиченного дня даёт
 * монотонность ПО ПОСТРОЕНИЮ: результат не может оказаться раньше текущего LCD.
 *
 * ПОЧЕМУ ПОКРЫТИЕ — ЭТО ПРОГОН, А НЕ БИЗНЕС-СТРОКИ. Сутки без продаж — законный ноль, а не
 * дырка в загрузке. Если считать покрытием наличие бизнес-строк, день без заказов навсегда
 * остановил бы LCD. Поэтому покрытие определяется журналами прогонов: у WB — завершённый
 * прогон на логический день, у Ozon — окно `source_from..source_to` успешного прогона.
 */

/** Режим вычисления LCD. AUTO — production-умолчание. */
export type LcdMode = 'AUTO' | 'MANUAL';

/** Платформа: у WB и Ozon независимые LCD и независимые источники. */
export type LcdPlatform = 'WB' | 'OZON';

/** Стадии цикла. Порядок значим: коммит разрешён только когда пройдены ВСЕ. */
export const LCD_STAGES = [
  'SOURCE_READINESS', 'STRUCTURE_PREPARE', 'UNITKA_WRITE', 'POST_WRITE_READBACK', 'INTEGRITY_CHECK',
] as const;
export type LcdStage = (typeof LCD_STAGES)[number];

export type StageOutcome = 'PASS' | 'FAIL' | 'SKIPPED';

/** Коды жизненного цикла для наблюдаемости (§25). */
export type LcdCode =
  | 'LCD_COMMITTED' | 'LCD_NOT_ADVANCED' | 'MANUAL_OVERRIDE_ACTIVE'
  | 'MANUAL_LCD_INVALID' | 'MANUAL_LCD_REGRESSION'
  | 'SOURCE_STALE' | 'INTEGRITY_FAILED' | 'WRITE_FAILED' | 'READBACK_FAILED'
  | 'LCD_MODE_INVALID';

const ISO = /^\d{4}-\d{2}-\d{2}$/;

export function isIsoDate(v: unknown): v is string {
  return typeof v === 'string' && ISO.test(v) && !Number.isNaN(Date.parse(`${v}T00:00:00Z`));
}

export function addDaysIso(iso: string, n: number): string {
  if (!isIsoDate(iso)) throw new RangeError(`дата ${iso}`);
  return new Date(Date.parse(`${iso}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
}

/* ─────────────────────────── режим ─────────────────────────── */

/**
 * Режим читается ЯВНО. Пустое значение = AUTO (production-умолчание), но любое непонятное
 * слово — отказ, а не молчаливый откат к AUTO: «не смогли прочитать режим» и «владелец выбрал
 * AUTO» — разные события. Эвристика «если дата заполнена, значит MANUAL» запрещена (§8):
 * ячейка LCD заполнена ВСЕГДА, поэтому такая эвристика означала бы вечный MANUAL.
 */
export function parseLcdMode(raw: unknown): { mode: LcdMode } | { code: 'LCD_MODE_INVALID'; got: string } {
  const s = String(raw ?? '').trim().toUpperCase();
  if (s === '') return { mode: 'AUTO' };
  if (s === 'AUTO' || s === 'MANUAL') return { mode: s };
  return { code: 'LCD_MODE_INVALID', got: String(raw ?? '') };
}

/* ─────────────────────────── смежность ─────────────────────────── */

export interface ContiguityInput {
  /** Уже закоммиченный LCD — точка отсчёта обхода. */
  readonly committed: string;
  /** D-1 МСК: сегодняшний день закрытым не бывает никогда. */
  readonly d1Msk: string;
  /** Покрыты ли сутки ВСЕМИ гейтящими источниками (журнал прогонов, не бизнес-строки). */
  readonly covered: (dateIso: string) => boolean;
  /** Предохранитель обхода: столько суток максимум за один прогон. */
  readonly maxStep?: number;
}

export interface ContiguityResult {
  /** Максимальная СМЕЖНАЯ безопасная дата. Равна committed, если следующий день не покрыт. */
  readonly candidate: string;
  /** Первая непокрытая дата — причина остановки; null, если дошли до d1Msk. */
  readonly gapAt: string | null;
  /** Сколько суток прибавлено. 0 — двигаться некуда. */
  readonly advancedDays: number;
  /** Сколько суток осталось за дыркой до d1Msk — диагностика «дырка, а не конец данных». */
  readonly blockedDays: number;
}

/**
 * Максимальная СМЕЖНАЯ безопасная дата. Обход строго вперёд, останов на первой непокрытой дате.
 * Дырку не перепрыгиваем: 20.09 READY, 21.09 MISSING, 22.09 READY ⇒ кандидат 20.09, а не 22.09.
 * Записать 22.09, оставив 21.09 скелетом, означало бы «закрытый» месяц с провалом внутри.
 */
export function contiguousCandidate(inp: ContiguityInput): ContiguityResult {
  if (!isIsoDate(inp.committed)) throw new RangeError(`committed ${inp.committed}`);
  if (!isIsoDate(inp.d1Msk)) throw new RangeError(`d1Msk ${inp.d1Msk}`);
  const maxStep = inp.maxStep ?? 400;
  let candidate = inp.committed;
  let gapAt: string | null = null;
  for (let i = 0; i < maxStep; i++) {
    const next = addDaysIso(candidate, 1);
    if (next > inp.d1Msk) break;
    if (!inp.covered(next)) { gapAt = next; break; }
    candidate = next;
  }
  let blockedDays = 0;
  if (gapAt) for (let d = gapAt; d <= inp.d1Msk; d = addDaysIso(d, 1)) blockedDays++;
  return {
    candidate,
    gapAt,
    advancedDays: Math.round((Date.parse(`${candidate}T00:00:00Z`) - Date.parse(`${inp.committed}T00:00:00Z`)) / 86_400_000),
    blockedDays,
  };
}

/* ─────────────────────────── выбор кандидата ─────────────────────────── */

export interface CandidateInput {
  readonly platform: LcdPlatform;
  readonly mode: LcdMode;
  /** Значение MANUAL_LCD — учитывается ТОЛЬКО в режиме MANUAL. */
  readonly manualLcd?: unknown;
  readonly committed: string;
  readonly d1Msk: string;
  readonly covered: (dateIso: string) => boolean;
  readonly maxStep?: number;
}

export interface CandidateDecision {
  readonly candidate: string;
  /** Двигаться некуда — кандидат равен закоммиченному. */
  readonly willAdvance: boolean;
  readonly code: LcdCode | null;
  readonly gapAt: string | null;
  readonly notes: readonly string[];
}

/**
 * Кандидат на закрытие. В AUTO — максимальная смежная безопасная дата. В MANUAL — дата
 * владельца, и она переопределяет ТОЛЬКО выбор кандидата: барьеры записи, readback и
 * целостности MANUAL не отменяет (§8, «не обходить hard safety constraints»).
 */
export function decideCandidate(inp: CandidateInput): CandidateDecision {
  const notes: string[] = [];
  if (inp.mode === 'MANUAL') {
    const m = inp.manualLcd;
    if (!isIsoDate(m)) {
      return { candidate: inp.committed, willAdvance: false, code: 'MANUAL_LCD_INVALID', gapAt: null,
        notes: [`MANUAL_LCD = ${JSON.stringify(m ?? null)}: ожидается YYYY-MM-DD`] };
    }
    if (m > inp.d1Msk) {
      return { candidate: inp.committed, willAdvance: false, code: 'MANUAL_LCD_INVALID', gapAt: null,
        notes: [`MANUAL_LCD ${m} позже D-1 МСК ${inp.d1Msk}: сегодняшний день закрытым не бывает`] };
    }
    notes.push(`MANUAL: владелец задал ${m} (AUTO дал бы ${contiguousCandidate(inp).candidate})`);
    if (m < inp.committed) {
      // Откат назад в MANUAL разрешён, но ГРОМКО: это не штатный путь, а аварийный.
      return { candidate: m, willAdvance: true, code: 'MANUAL_LCD_REGRESSION', gapAt: null,
        notes: [...notes, `откат назад: ${inp.committed} → ${m}`] };
    }
    return { candidate: m, willAdvance: m > inp.committed, code: 'MANUAL_OVERRIDE_ACTIVE', gapAt: null, notes };
  }

  const c = contiguousCandidate(inp);
  if (c.gapAt) {
    notes.push(`смежность: ${c.gapAt} не покрыта журналом прогонов, дальше не идём`);
    if (c.blockedDays > 1) notes.push(`за дыркой ещё ${c.blockedDays - 1} сут. до D-1 — это дырка, а не конец данных`);
  }
  if (c.advancedDays === 0) {
    return { candidate: inp.committed, willAdvance: false, code: 'LCD_NOT_ADVANCED', gapAt: c.gapAt,
      notes: notes.length ? notes : [`следующий день после ${inp.committed} ещё не закрыт (D-1 МСК = ${inp.d1Msk})`] };
  }
  return { candidate: c.candidate, willAdvance: true, code: null, gapAt: c.gapAt, notes };
}

/* ─────────────────────────── атомарный коммит ─────────────────────────── */

export type StageReport = Readonly<Record<LcdStage, StageOutcome>>;

export interface CommitDecision {
  readonly commit: boolean;
  readonly lcdAfter: string;
  readonly code: LcdCode;
  readonly failedStage: LcdStage | null;
}

/** Первая стадия, помеченная FAIL. SKIPPED провалом не считается: стадия могла быть не нужна. */
export function firstFailedStage(stages: StageReport): LcdStage | null {
  return LCD_STAGES.find((s) => stages[s] === 'FAIL') ?? null;
}

const STAGE_FAIL_CODE: Readonly<Record<LcdStage, LcdCode>> = {
  SOURCE_READINESS: 'SOURCE_STALE',
  STRUCTURE_PREPARE: 'WRITE_FAILED',
  UNITKA_WRITE: 'WRITE_FAILED',
  POST_WRITE_READBACK: 'READBACK_FAILED',
  INTEGRITY_CHECK: 'INTEGRITY_FAILED',
};

/**
 * Атомарность (§9). Коммит только если ни одна стадия не провалена. Любой провал —
 * и LCD_AFTER = LCD_BEFORE. Никакого оптимистичного продвижения: состояние
 * «LCD = 22.09, а строка 22.09 не записана» должно быть недостижимо.
 */
export function decideCommit(
  lcdBefore: string, candidate: string, willAdvance: boolean, stages: StageReport,
): CommitDecision {
  const failed = firstFailedStage(stages);
  if (failed) return { commit: false, lcdAfter: lcdBefore, code: STAGE_FAIL_CODE[failed], failedStage: failed };
  if (!willAdvance || candidate === lcdBefore) {
    return { commit: false, lcdAfter: lcdBefore, code: 'LCD_NOT_ADVANCED', failedStage: null };
  }
  return { commit: true, lcdAfter: candidate, code: 'LCD_COMMITTED', failedStage: null };
}

/* ─────────────────────────── покрытие суток ─────────────────────────── */

/** Завершённый прогон на логический день: WB (`LOADER_RUNS.logical_period` = дата). */
export function coveredByDailyRuns(
  runs: ReadonlyArray<{ readonly loader: string; readonly period: string }>, required: readonly string[],
): (dateIso: string) => boolean {
  const by = new Map<string, Set<string>>();
  for (const r of runs) {
    const d = r.period.slice(0, 10);
    if (!ISO.test(d)) continue;
    if (!by.has(d)) by.set(d, new Set());
    by.get(d)!.add(r.loader);
  }
  return (date) => {
    const got = by.get(date);
    return got ? required.every((x) => got.has(x)) : false;
  };
}

/** Окно успешного прогона `source_from..source_to`: Ozon (`OZON_INGESTION_RUNS`). */
export function coveredByRunWindows(
  runs: ReadonlyArray<{ readonly entity: string; readonly from: string; readonly to: string }>,
  required: readonly string[],
): (dateIso: string) => boolean {
  const by = new Map<string, Array<{ from: string; to: string }>>();
  for (const r of runs) {
    if (!ISO.test(r.from) || !ISO.test(r.to) || r.from > r.to) continue;
    if (!by.has(r.entity)) by.set(r.entity, []);
    by.get(r.entity)!.push({ from: r.from, to: r.to });
  }
  return (date) => required.every((e) => (by.get(e) ?? []).some((w) => w.from <= date && date <= w.to));
}
