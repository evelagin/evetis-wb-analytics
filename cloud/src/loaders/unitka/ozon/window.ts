/**
 * OZON UNITKA — ОКНО ЕЖЕДНЕВНОЙ ПЕРЕЗАПИСИ (Gate 8). Чистый модуль: ни Sheets, ни BigQuery.
 *
 * У WB своё окно (35 суток, `RECONCILE_WINDOW_DAYS`) и своя эпоха. Переиспользовать их
 * нельзя: сроки прихода денег у площадок разные, а домены разделены жёстко. Ozon получает
 * СВОЮ константу, выведенную из СВОЕГО замера.
 *
 * ЗАМЕР (Gate 8, 328 начислений, привязанных к отправлениям, после бэкфилла — то есть там,
 * где дата извлечения отражает реальный приход, а не разовую загрузку истории):
 *
 *   публикация Ozon (извлечено − дата операции):  p50 = p95 = max = 14 суток. ДЕТЕРМИНИРОВАНА.
 *   начисление (дата операции − дата заказа):     p50 = 5, p90 = 12, p95 = 13, p99 = 19, max = 25.
 *   ВИДИМОСТЬ (извлечено − дата заказа):          p50 = 18, p90 = 23, p95 = 25, p99 = 33, max = 36.
 *
 *   покрытие окна по видимости:  14 сут → 19,8 %   21 сут → 85,1 %   30 сут → 97,6 %
 *                                35 сут → 99,4 %   45 сут → 100 %    60 сут → 100 %
 *
 * ВЫБРАНО 45 СУТОК. Это наименьшее из рассмотренных окон, которое накрывает ВСЮ наблюдённую
 * задержку (максимум 36 суток) с запасом 9 суток. Окно 35 суток накрывает 99,4 % и выглядит
 * дешевле, но оставляет измеренный хвост СНАРУЖИ: день, чья комиссия пришла на 36-е сутки,
 * при окне 35 навсегда остался бы с оценкой вместо факта — ровно тот отказ, ради
 * предотвращения которого окно и существует.
 *
 * Окно пересекает границы месяцев: 45 суток — это всегда две, а при конце месяца три секции.
 *
 * ГЛУБОКАЯ СВЕРКА. Редкие поздние правки (возвраты, перерасчёты) приходят и позже 45 суток.
 * Раз в месяц пересматриваются 120 суток: тот же горизонт, на котором построен оценщик
 * логистики, и он заведомо перекрывает наблюдённый максимум втрое.
 *
 * ЭПОХА. 17.04.2026 — первый канонически пересчитанный день Ozon-Юнитки (апрель — гибридный
 * месяц миграции: 01–16.04 остаются легаси и не переписываются никогда).
 */
export const OZON_REWRITE_WINDOW_DAYS = 45;
export const OZON_DEEP_RECONCILIATION_DAYS = 120;
export const OZON_DEEP_RECONCILIATION_CADENCE = 'MONTHLY' as const;
export const OZON_REWRITE_EPOCH = '2026-04-17';
/** Максимальная НАБЛЮДЁННАЯ задержка видимости начисления. Окно обязано её накрывать. */
export const OZON_OBSERVED_MAX_VISIBILITY_DAYS = 36;

export interface OzonRewriteWindow {
  readonly from: string; readonly to: string; readonly days: number;
  readonly epoch: string; readonly rollingFrom: string; readonly deep: boolean;
}

function addDays(iso: string, n: number): string {
  const t = Date.parse(`${iso}T00:00:00Z`);
  if (Number.isNaN(t)) throw new RangeError(`дата ${iso}`);
  return new Date(t + n * 86_400_000).toISOString().slice(0, 10);
}

/**
 * Окно перезаписи: [max(LCD − (days − 1), эпоха), LCD].
 *
 * Окно короче наблюдённого максимума задержки запрещено: оно оставило бы измеренный хвост
 * поздних начислений снаружи, и оценка в этих сутках никогда не сменилась бы фактом.
 */
export function ozonRewriteWindow(
  lcd: string, days = OZON_REWRITE_WINDOW_DAYS, epoch = OZON_REWRITE_EPOCH, deep = false,
): OzonRewriteWindow {
  if (!Number.isInteger(days) || days < OZON_OBSERVED_MAX_VISIBILITY_DAYS) {
    throw new RangeError(`окно перезаписи ${days} сут.: короче наблюдённой задержки `
      + `${OZON_OBSERVED_MAX_VISIBILITY_DAYS} сут. — поздний факт не заменит оценку`);
  }
  if (lcd < epoch) throw new RangeError(`LCD ${lcd} раньше эпохи Ozon ${epoch}`);
  const rollingFrom = addDays(lcd, -(days - 1));
  return { from: rollingFrom > epoch ? rollingFrom : epoch, to: lcd, days, epoch, rollingFrom, deep };
}

/** Окно глубокой сверки: те же правила, горизонт 120 суток. */
export function ozonDeepWindow(lcd: string, epoch = OZON_REWRITE_EPOCH): OzonRewriteWindow {
  return ozonRewriteWindow(lcd, OZON_DEEP_RECONCILIATION_DAYS, epoch, true);
}

/** Глубокая сверка запускается в первый день месяца — раз в месяц, по календарю, а не по счётчику. */
export function isDeepReconciliationDay(today: string): boolean {
  return today.slice(8, 10) === '01';
}

/** Ключи месяцев, которые пересекает окно, от старого к новому. Последний — месяц LCD. */
export function ozonWindowMonths(w: OzonRewriteWindow): string[] {
  const out: string[] = [];
  let [y, m] = [Number(w.from.slice(0, 4)), Number(w.from.slice(5, 7))];
  const ly = Number(w.to.slice(0, 4)), lm = Number(w.to.slice(5, 7));
  for (;;) {
    out.push(`${y}-${String(m).padStart(2, '0')}`);
    if (y === ly && m === lm) break;
    if (m === 12) { y += 1; m = 1; } else { m += 1; }
    if (out.length > 6) throw new RangeError('окно перезаписи пересекает больше 6 месяцев');
  }
  return out;
}

/** Первый переписываемый день месяца: апрель 2026 — гибридный, его легаси-половина неприкосновенна. */
export function ozonFromDayFor(monthKey: string, w: OzonRewriteWindow): number {
  const first = monthKey === w.from.slice(0, 7) ? Number(w.from.slice(8, 10)) : 1;
  return monthKey === '2026-04' ? Math.max(first, 17) : first;
}
