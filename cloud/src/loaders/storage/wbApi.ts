/** UNITKA E4 — асинхронный отчёт WB «Платное хранение». */
import { wbFetch, type WbHttpOptions } from '../../http/wbHttp.js';
import { LoaderError } from '../../errors.js';
import type { Logger } from '../../logging.js';

export const PAID_STORAGE_PATH = '/api/v1/paid_storage';
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

export interface PaidStorageFetchResult {
  rows: unknown[];
  taskId: string;
  pollAttempts: number;
  httpStatus: number;
}

function parseJson(body: string, code: string): unknown {
  try { return JSON.parse(body); }
  catch { throw new LoaderError(`WB paid storage returned invalid JSON: ${body.slice(0, 300)}`, code); }
}

export async function fetchPaidStorage(
  host: string,
  token: string,
  startDate: string,
  endDate: string,
  opts: WbHttpOptions,
  logger?: Logger,
): Promise<PaidStorageFetchResult> {
  const create = await wbFetch(
    `${host}${PAID_STORAGE_PATH}?dateFrom=${encodeURIComponent(startDate)}&dateTo=${encodeURIComponent(endDate)}`,
    { method: 'GET', headers: { Authorization: token } }, opts, logger,
  );
  if (!create.ok) {
    throw new LoaderError(`GET ${PAID_STORAGE_PATH} -> HTTP ${create.status}: ${(create.body || create.error || '').slice(0, 500)}`, 'WB_STORAGE_CREATE_FAILED');
  }
  const createBody = parseJson(create.body, 'WB_STORAGE_CREATE_JSON') as { data?: { taskId?: unknown } };
  const taskId = String(createBody?.data?.taskId ?? '').trim();
  if (!taskId) throw new LoaderError('WB paid storage taskId is missing', 'WB_STORAGE_TASK_ID_MISSING');

  let pollAttempts = 0;
  for (; pollAttempts < 40; pollAttempts++) {
    if (pollAttempts > 0) await sleep(5000);
    const statusRes = await wbFetch(
      `${host}${PAID_STORAGE_PATH}/tasks/${encodeURIComponent(taskId)}/status`,
      { method: 'GET', headers: { Authorization: token } }, opts, logger,
    );
    if (!statusRes.ok) {
      throw new LoaderError(`paid_storage status -> HTTP ${statusRes.status}: ${(statusRes.body || statusRes.error || '').slice(0, 500)}`, 'WB_STORAGE_STATUS_FAILED');
    }
    const statusBody = parseJson(statusRes.body, 'WB_STORAGE_STATUS_JSON') as { data?: { status?: unknown } };
    const status = String(statusBody?.data?.status ?? '').toLowerCase();
    logger?.info('storage_poll', { taskId, attempt: pollAttempts + 1, status });
    if (status === 'done') break;
    if (status === 'canceled' || status === 'purged') {
      throw new LoaderError(`WB paid storage task ${taskId} ended with status ${status}`, 'WB_STORAGE_TASK_FAILED');
    }
  }
  if (pollAttempts >= 40) throw new LoaderError(`WB paid storage task ${taskId} timed out`, 'WB_STORAGE_TASK_TIMEOUT');

  const download = await wbFetch(
    `${host}${PAID_STORAGE_PATH}/tasks/${encodeURIComponent(taskId)}/download`,
    { method: 'GET', headers: { Authorization: token } }, opts, logger,
  );
  if (!download.ok) {
    throw new LoaderError(`paid_storage download -> HTTP ${download.status}: ${(download.body || download.error || '').slice(0, 500)}`, 'WB_STORAGE_DOWNLOAD_FAILED');
  }
  const data = parseJson(download.body, 'WB_STORAGE_DOWNLOAD_JSON');
  if (!Array.isArray(data) || data.length === 0) {
    // Fail closed: an empty report must never erase a previously valid window.
    throw new LoaderError('WB paid storage report is empty; refusing to replace existing facts', 'WB_STORAGE_EMPTY_REPORT');
  }
  return { rows: data, taskId, pollAttempts: pollAttempts + 1, httpStatus: download.status };
}
