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
  | 'LCD_MODE_INVALID' | 'LCD_COMMIT_CONFLICT' | 'LCD_WRITE_FAILED';

const ISO = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Настоящая календарная дата. Проверка через обратное преобразование обязательна: Date.parse
 * НЕ отвергает '2027-02-29' и '2026-02-31', а молча переносит их на 1–3 марта. Без этого
 * несуществующая дата, введённая владельцем в MANUAL_LCD, была бы принята как 01.03.
 */
export function isIsoDate(v: unknown): v is string {
  if (typeof v !== 'string' || !ISO.test(v)) return false;
  const t = Date.parse(`${v}T00:00:00Z`);
  return !Number.isNaN(t) && new Date(t).toISOString().slice(0, 10) === v;
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
  /**
   * Потолок канонической готовности платформы (у WB — V_UNITKA_LAST_CLOSED_DATE). Смежность
   * по журналу прогонов его НЕ заменяет, а дополняет: кандидат = min(смежный, потолок). Так
   * новый барьер может только придержать LCD относительно прежнего контракта, но никогда не
   * ускорить его. Не задан — потолка нет (у Ozon свежесть проверяется отдельным барьером).
   */
  readonly sourceCeiling?: string;
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
  const ceiling = inp.sourceCeiling;
  if (ceiling !== undefined && !isIsoDate(ceiling)) throw new RangeError(`sourceCeiling ${ceiling}`);
  if (inp.mode === 'MANUAL') {
    const m = inp.manualLcd;
    const hold = (code: LcdCode, why: string): CandidateDecision =>
      ({ candidate: inp.committed, willAdvance: false, code, gapAt: null, notes: [why] });
    if (!isIsoDate(m)) return hold('MANUAL_LCD_INVALID', `MANUAL_LCD = ${JSON.stringify(m ?? null)}: ожидается YYYY-MM-DD`);
    if (m > inp.d1Msk) return hold('MANUAL_LCD_INVALID', `MANUAL_LCD ${m} позже D-1 МСК ${inp.d1Msk}: сегодняшний день закрытым не бывает`);
    // MANUAL — это выбор кандидата, а не «записать что угодно»: данных, которых нет в источнике,
    // override не создаёт. Дата позже готовности источников записала бы пустые сутки как закрытые.
    if (ceiling !== undefined && m > ceiling) {
      return hold('MANUAL_LCD_INVALID', `MANUAL_LCD ${m} позже готовности источников ${ceiling}: закрыть день без данных нельзя`);
    }
    // Откат назад ОТВЕРГАЕТСЯ. Опубликованные сутки после новой даты стали бы «будущими»: WB
    // упал бы на FUTURE_LEAKAGE, а Ozon, который пишет окно целиком, СТЁР бы их. Исправление
    // истории делает окно перезаписи, а не откат LCD (§7).
    if (m < inp.committed) {
      return hold('MANUAL_LCD_REGRESSION', `MANUAL_LCD ${m} раньше закоммиченного ${inp.committed}: откат LCD стёр бы опубликованные сутки — отказ`);
    }
    notes.push(`MANUAL: владелец задал ${m} (AUTO дал бы ${decideCandidate({ ...inp, mode: 'AUTO' }).candidate})`);
    return { candidate: m, willAdvance: m > inp.committed, code: 'MANUAL_OVERRIDE_ACTIVE', gapAt: null, notes };
  }

  const c = contiguousCandidate(inp);
  let candidate = c.candidate;
  if (c.gapAt) {
    notes.push(`смежность: ${c.gapAt} не покрыта журналом прогонов, дальше не идём`);
    if (c.blockedDays > 1) notes.push(`за дыркой ещё ${c.blockedDays - 1} сут. до D-1 — это дырка, а не конец данных`);
  }
  if (ceiling !== undefined && candidate > ceiling) {
    // Потолок ниже закоммиченного — не повод откатываться: кандидат не опускается ниже committed.
    candidate = ceiling < inp.committed ? inp.committed : ceiling;
    notes.push(`потолок готовности источников ${ceiling}: смежный кандидат ${c.candidate} придержан до ${candidate}`);
  }
  if (candidate === inp.committed) {
    return { candidate: inp.committed, willAdvance: false, code: 'LCD_NOT_ADVANCED', gapAt: c.gapAt,
      notes: notes.length ? notes : [`следующий день после ${inp.committed} ещё не закрыт (D-1 МСК = ${inp.d1Msk})`] };
  }
  return { candidate, willAdvance: true, code: null, gapAt: c.gapAt, notes };
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

/* ─────────────────────────── протокол коммита ─────────────────────────── */

/**
 * Авторитетная ячейка LCD платформы. У WB — именованный диапазон LAST_CLOSED_DATE (+ зеркало
 * WB736 в той же записи), у Ozon — OZON_LAST_CLOSED_DATE. read() читает АВТОРИТЕТ, а не
 * зеркало: зеркало — производное состояние.
 */
export interface LcdCell {
  read(): Promise<string | null>;
  write(iso: string): Promise<void>;
}

export type CommitCode = 'LCD_COMMITTED' | 'LCD_NOT_ADVANCED' | 'LCD_COMMIT_CONFLICT' | 'LCD_WRITE_FAILED';

export interface CommitOutcome {
  readonly code: CommitCode;
  /** Что книга держит ПОСЛЕ протокола (по перечитыванию, а не по намерению). */
  readonly bookAfter: string | null;
  readonly bookBefore: string | null;
  readonly message: string;
}

/**
 * Коммит LCD с барьером compare-before-commit (optimistic concurrency).
 *
 *   1. перечитать авторитет; если он НЕ равен ожидаемому закоммиченному — кто-то изменил LCD
 *      между планированием и коммитом: LCD_COMMIT_CONFLICT, чужое значение НЕ перетирается;
 *   2. записать кандидата;
 *   3. перечитать и доказать равенство кандидату. Исход определяет ПЕРЕЧИТЫВАНИЕ, а не ответ
 *      API: запись может «упасть» по таймауту, уже применившись, — и наоборот.
 */
export async function commitLcd(
  cell: LcdCell, a: { readonly expectedCommitted: string; readonly candidate: string },
): Promise<CommitOutcome> {
  const before = await cell.read();
  if (before !== a.expectedCommitted) {
    return { code: 'LCD_COMMIT_CONFLICT', bookBefore: before, bookAfter: before,
      message: `LCD в книге ${before ?? '(пусто)'} ≠ ожидаемому ${a.expectedCommitted}: изменён между планированием и коммитом — не перетираем` };
  }
  if (a.candidate === a.expectedCommitted) {
    return { code: 'LCD_NOT_ADVANCED', bookBefore: before, bookAfter: before, message: `кандидат равен закоммиченному ${before}` };
  }
  let writeError: string | null = null;
  try { await cell.write(a.candidate); } catch (e) { writeError = e instanceof Error ? e.message : String(e); }
  let after: string | null;
  try { after = await cell.read(); } catch (e) {
    return { code: 'LCD_WRITE_FAILED', bookBefore: before, bookAfter: null,
      message: `перечитать LCD после записи не удалось: ${e instanceof Error ? e.message : String(e)}` };
  }
  if (after === a.candidate) {
    return { code: 'LCD_COMMITTED', bookBefore: before, bookAfter: after,
      message: writeError ? `запись ответила ошибкой (${writeError}), но перечитывание подтвердило ${after}` : `закоммичен ${after}` };
  }
  return { code: 'LCD_WRITE_FAILED', bookBefore: before, bookAfter: after,
    message: `после записи в книге ${after ?? '(пусто)'} вместо ${a.candidate}${writeError ? `: ${writeError}` : ''}` };
}

export type RevertCode = 'REVERTED' | 'REVERT_CONFLICT' | 'REVERT_FAILED';

/**
 * Компенсирующий откат СОБСТВЕННОГО коммита, если проверка после коммита не прошла. Это не
 * «откат LCD во времени» (§7), а отмена незавершённой транзакции того же прогона: книга
 * возвращается ровно к состоянию до цикла. Тот же барьер: откатываем, только если в книге
 * всё ещё наш кандидат, — чужое значение не трогаем.
 */
export async function revertLcd(
  cell: LcdCell, a: { readonly committed: string; readonly from: string },
): Promise<{ code: RevertCode; bookAfter: string | null }> {
  const cur = await cell.read();
  if (cur !== a.from) return { code: 'REVERT_CONFLICT', bookAfter: cur };
  try { await cell.write(a.committed); } catch { /* исход решает перечитывание */ }
  const after = await cell.read().catch(() => null);
  return { code: after === a.committed ? 'REVERTED' : 'REVERT_FAILED', bookAfter: after };
}
