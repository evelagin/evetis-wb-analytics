/**
 * События жизненного цикла Юнитки (Gate 10) — общие для WB и Ozon.
 *
 * Идентичность события — поле `lifecycle_event`, а НЕ message: logging.ts раскрывает ctx
 * последним, и любое поле `message` в контексте затёрло бы имя события (известный долг логгера,
 * см. PR #163). Поэтому поле message здесь отбрасывается до записи.
 *
 * Уровень ERROR вместе с полем `code` — это ровно то, что ловит действующая алерт-политика
 * (severity>=ERROR AND jsonPayload.code!=""). Штатное «двигаться некуда» пишется как info, без кода.
 */
import type { Logger } from '../../logging.js';

export type LifecyclePlatform = 'WB' | 'OZON';
export type LifecycleEvent =
  | 'LCD_CANDIDATE' | 'LCD_NOT_ADVANCED' | 'LCD_COMMIT_CONFLICT' | 'LCD_COMMITTED' | 'LCD_WRITE_FAILED'
  | 'LCD_REVERTED' | 'LCD_MIRROR_REPAIRED' | 'MANUAL_OVERRIDE_ACTIVE' | 'NEW_MONTH_CREATED'
  | 'NEW_SKU_ACTIVATED' | 'CAPACITY_EXPANDED' | 'NO_SAFE_EXPANSION_PATH' | 'INTEGRITY_FAILED'
  | 'LCD_LEGACY_READONLY';

export function logLifecycle(
  log: Logger, platform: LifecyclePlatform, event: LifecycleEvent, ctx: Record<string, unknown>,
  level: 'info' | 'warn' | 'error' = 'info',
): void {
  const safe = Object.fromEntries(Object.entries(ctx).filter(([k]) => k !== 'message'));
  log[level]('unitka_lifecycle', { lifecycle_event: event, platform, ...safe });
}
