/**
 * Токены WB: срок действия и диагностика отказа авторизации — общий механизм потоков миграции M2–M5.
 *
 * Инцидент 31.10.2026: токен поставок истёк, Apps Script получал 401 «token is expired» и молча
 * перестал грузить. Отсюда три правила для каждого загрузчика WB:
 *   1) срок токена проверяется ДО первого запроса: истёкший — детерминированный отказ
 *      `<PREFIX>_TOKEN_EXPIRED` (повтор не лечит), меньше `warnDays` до истечения — предупреждение
 *      с кодом WB_TOKEN_EXPIRING_SOON (на него смотрит алерт);
 *   2) 401/403 — отдельный детерминированный код `<PREFIX>_AUTH` с подсказкой, что делать
 *      (401 — токен истёк/отозван, 403 — у токена нет категории/подписки для метода);
 *   3) значение токена НИКОГДА не попадает в текст ошибки и в лог — только имя секрета, срок,
 *      HTTP-статус и обрезанная причина из тела ответа WB (из неё вырезаются похожие на токен строки).
 *
 * Токен WB — JWT; срок берётся из поля `exp` полезной нагрузки (без проверки подписи — это не
 * аутентификация, а только чтение даты). Токен без читаемого `exp` — состояние NO_EXPIRY: проверка
 * срока пропускается, отказ остаётся на 401/403.
 */
import { LoaderError } from '../errors.js';
import type { Logger } from '../logging.js';

export const WB_TOKEN_EXPIRY_WARN_DAYS = 14;
export const WB_TOKEN_EXPIRING_SOON = 'WB_TOKEN_EXPIRING_SOON';
const DAY_MS = 86_400_000;
const BODY_REASON_MAX = 200;

export type WbTokenState =
  | { kind: 'NO_EXPIRY' }
  | { kind: 'OK' | 'EXPIRING_SOON' | 'EXPIRED'; expiresAt: string; daysLeft: number };

/** Срок токена (мс эпохи) из `exp` JWT; null — не JWT или без `exp`. */
export function tokenExpiryMs(token: string): number | null {
  const parts = token.trim().replace(/^Bearer\s+/i, '').split('.');
  if (parts.length !== 3) return null;
  try {
    const json = Buffer.from(parts[1]!.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
    const exp = Number((JSON.parse(json) as { exp?: unknown }).exp);
    return Number.isFinite(exp) && exp > 0 ? exp * 1000 : null;
  } catch {
    return null;
  }
}

export function wbTokenState(token: string, nowMs: number, warnDays = WB_TOKEN_EXPIRY_WARN_DAYS): WbTokenState {
  const exp = tokenExpiryMs(token);
  if (exp === null) return { kind: 'NO_EXPIRY' };
  const daysLeft = Math.floor((exp - nowMs) / DAY_MS);
  const expiresAt = new Date(exp).toISOString();
  if (exp <= nowMs) return { kind: 'EXPIRED', expiresAt, daysLeft };
  return { kind: daysLeft < warnDays ? 'EXPIRING_SOON' : 'OK', expiresAt, daysLeft };
}

export interface WbTokenCheckOptions {
  /** Префикс кода потока: WB_STATS, WB_ADS, WB_FINANCE, WB_SUPPLIES … */
  codePrefix: string;
  nowMs?: number;
  warnDays?: number;
  logger?: Logger;
}

/**
 * Проверка перед первым запросом. Пустой токен → `<PREFIX>_AUTH`; истёкший → `<PREFIX>_TOKEN_EXPIRED`;
 * скоро истекает → warn с кодом WB_TOKEN_EXPIRING_SOON. Возвращает состояние (для лога прогона).
 */
export function assertWbTokenUsable(token: string, secretName: string, opts: WbTokenCheckOptions): WbTokenState {
  if (!token.trim()) {
    throw new LoaderError(`секрет ${secretName} пуст — добавьте версию секрета с токеном WB`, `${opts.codePrefix}_AUTH`);
  }
  const st = wbTokenState(token, opts.nowMs ?? Date.now(), opts.warnDays);
  if (st.kind === 'EXPIRED') {
    throw new LoaderError(
      `токен ${secretName} истёк ${st.expiresAt} — выпустите новый в ЛК WB и добавьте версию секрета ${secretName}`,
      `${opts.codePrefix}_TOKEN_EXPIRED`,
    );
  }
  if (st.kind === 'EXPIRING_SOON') {
    opts.logger?.warn('wb_token_expiring', {
      code: WB_TOKEN_EXPIRING_SOON, secret: secretName, daysLeft: st.daysLeft, expiresAt: st.expiresAt,
    });
  }
  return st;
}

export function isWbAuthStatus(status: number): boolean {
  return status === 401 || status === 403;
}

/** Вырезает из текста всё, похожее на токен: JWT (три base64url-части) и «Bearer …». */
export function redactTokenLike(text: string): string {
  return text
    .replace(/Bearer\s+\S+/gi, 'Bearer [скрыто]')
    .replace(/[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, '[токен скрыт]');
}

/**
 * 401/403 → детерминированный `<PREFIX>_AUTH` (повтор не лечит). В тексте — имя секрета, статус, метод
 * и причина WB из тела (обрезана, токеноподобное вырезано); значения токена нет.
 */
export function wbAuthError(codePrefix: string, secretName: string, status: number, what: string, body = ''): LoaderError {
  const hint = status === 401
    ? 'токен истёк или отозван — выпустите новый в ЛК WB и добавьте версию секрета'
    : 'у токена нет категории доступа (или подписки) для этого метода — проверьте категории токена в ЛК WB';
  const reason = redactTokenLike(body.replace(/\s+/g, ' ').trim()).slice(0, BODY_REASON_MAX);
  return new LoaderError(
    `WB ${what}: HTTP ${status} — токен ${secretName} отклонён: ${hint}${reason ? ` (ответ WB: ${reason})` : ''}`,
    `${codePrefix}_AUTH`,
  );
}
