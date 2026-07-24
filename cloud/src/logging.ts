/**
 * Структурированный JSON-логгер (PR-Mig0). Формат совместим с Cloud Logging
 * (поле `severity`). Секреты логировать запрещено — пишем только метаданные.
 */
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

export type LogContext = Record<string, unknown>;

export class Logger {
  constructor(
    private readonly base: LogContext = {},
    private readonly min: LogLevel = 'info',
  ) {}

  child(ctx: LogContext): Logger {
    return new Logger({ ...this.base, ...ctx }, this.min);
  }

  private emit(level: LogLevel, message: string, ctx?: LogContext): void {
    if (ORDER[level] < ORDER[this.min]) return;
    const record = {
      severity: level.toUpperCase(),
      message,
      time: new Date().toISOString(),
      ...this.base,
      ...ctx,
    };
    const line = JSON.stringify(record);
    if (level === 'error' || level === 'warn') process.stderr.write(line + '\n');
    else process.stdout.write(line + '\n');
  }

  debug(message: string, ctx?: LogContext): void { this.emit('debug', message, ctx); }
  info(message: string, ctx?: LogContext): void { this.emit('info', message, ctx); }
  warn(message: string, ctx?: LogContext): void { this.emit('warn', message, ctx); }
  error(message: string, ctx?: LogContext): void { this.emit('error', message, ctx); }
}

export function parseLevel(v: string): LogLevel {
  return v === 'debug' || v === 'info' || v === 'warn' || v === 'error' ? v : 'info';
}
