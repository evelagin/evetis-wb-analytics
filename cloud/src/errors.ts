/**
 * Контракт кодов выхода и типов ошибок (PR-Mig0).
 * Cloud Run Job фиксирует execution как OK (exit 0) или FAILED (exit 1).
 */
export const EXIT_OK = 0;
export const EXIT_ERROR = 1;

/** Терминальный статус строки манифеста в BQ. */
export type ManifestStatus = 'STARTED' | 'COMPLETE' | 'ERROR';

/** Решение execution-guard по логическому периоду. */
export type GuardAction = 'PROCEED' | 'OK_NO_NEW' | 'ALREADY_RUNNING' | 'RECOVER';

export class LoaderError extends Error {
  constructor(
    message: string,
    public readonly code: string = 'LOADER_ERROR',
  ) {
    super(message);
    this.name = 'LoaderError';
  }
}

export class ConfigError extends LoaderError {
  constructor(message: string) {
    super(message, 'CONFIG_ERROR');
    this.name = 'ConfigError';
  }
}
