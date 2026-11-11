/**
 * Writer-ownership gate Cloud Run (этап C). Fencing-источник правды — строка потока в
 * wb_ops.INGEST_WRITER_OWNERSHIP; ему подчиняются ОБА писателя (Apps Script — WriterGate.gs).
 *
 * Решение gate (cli.ts зовёт его только при ENVIRONMENT=prod и только для загрузчиков из LOADER_STREAM,
 * ДО lease и ДО handler):
 *   owner = CLOUD_RUN            → ALLOW, запоминаем epoch (fencing-токен прогона);
 *   owner = APPS_SCRIPT | NONE   → FENCED: прогон не пишет НИЧЕГО (ни данных, ни LOADER_RUNS),
 *                                  лог writer_fenced с кодом WRITER_NOT_OWNER, exit 0 — штатный skip;
 *   ошибка чтения / нет строки /
 *   несколько строк / мусор      → UNAVAILABLE: fail-closed, код WRITER_GATE_UNAVAILABLE, exit 1 (алерт).
 *
 * Перед финальным commit-маркером прогона владелец и epoch перечитываются (WriterFence.assertEpoch):
 * сменился → WRITER_EPOCH_CHANGED, коммит не делается.
 */
import { LoaderError } from '../errors.js';
import type { BqClient } from '../bq/client.js';
import { isOwner, type Owner, type Stream, OWNERSHIP_TABLE } from './streams.js';

export const WRITER_NOT_OWNER = 'WRITER_NOT_OWNER';
export const WRITER_GATE_UNAVAILABLE = 'WRITER_GATE_UNAVAILABLE';
export const WRITER_EPOCH_CHANGED = 'WRITER_EPOCH_CHANGED';

export interface OwnershipSnapshot {
  owner: Owner;
  epoch: number;
  state: string;
}

/**
 * Чтение владельца. null — строки потока нет (fail-closed у вызывающего).
 * mark — метка в начале текста запроса (INFORMATION_SCHEMA.JOBS): WRITER_GATE:CR:<поток>:<загрузчик> у gate
 * входа (доказательство для координатора), WRITER_RECHECK:CR:… у перечитки перед COMPLETE.
 */
export interface OwnershipReader {
  read(stream: Stream, mark?: string): Promise<OwnershipSnapshot | null>;
}

export const gateMark = (stream: Stream, loader: string): string => `WRITER_GATE:CR:${stream}:${loader}`;
export const recheckMark = (stream: Stream, loader: string): string => `WRITER_RECHECK:CR:${stream}:${loader}`;

/** Серверный таймаут чтения: gate не должен съедать окно прогона. */
export const OWNERSHIP_READ_TIMEOUT_MS = 30_000;

export class BqOwnershipReader implements OwnershipReader {
  constructor(
    private readonly bq: Pick<BqClient, 'query' | 'projectId'>,
    private readonly dataset: string,
  ) {}

  async read(stream: Stream, mark?: string): Promise<OwnershipSnapshot | null> {
    const m = mark ?? `WRITER_READ:CR:${stream}`;
    if (!/^[A-Z_]+:CR:[a-z]+(:[a-z0-9-]+)?$/.test(m)) throw new Error(`невалидная метка запроса владения: ${m}`);
    const rows = await this.bq.query<{ owner?: unknown; epoch?: unknown; state?: unknown }>(
      // Метка — для координатора: по INFORMATION_SCHEMA.JOBS он доказывает, что prod-писатели под gate.
      `/* ${m}:v1 */ SELECT owner, epoch, state FROM \`${this.bq.projectId}.${this.dataset}.${OWNERSHIP_TABLE}\` WHERE stream = @stream`,
      { stream },
      { stream: 'STRING' },
      { jobTimeoutMs: OWNERSHIP_READ_TIMEOUT_MS },
    );
    if (rows.length === 0) return null;
    if (rows.length > 1) throw new Error(`${OWNERSHIP_TABLE}: ${rows.length} строк потока ${stream} — ожидалась одна`);
    return parseSnapshot(stream, rows[0] ?? {});
  }
}

export function parseSnapshot(stream: string, r: { owner?: unknown; epoch?: unknown; state?: unknown }): OwnershipSnapshot {
  const owner = String(r.owner ?? '');
  if (!isOwner(owner)) throw new Error(`${OWNERSHIP_TABLE}: неизвестный owner '${owner}' у потока ${stream}`);
  // BigQuery отдаёт INT64 строкой или числом — оба допустимы; дробь/мусор — отказ.
  const epoch = Number(typeof r.epoch === 'object' && r.epoch !== null ? String(r.epoch) : r.epoch);
  if (!Number.isSafeInteger(epoch) || epoch < 1) throw new Error(`${OWNERSHIP_TABLE}: невалидный epoch '${String(r.epoch)}' у потока ${stream}`);
  return { owner, epoch, state: String(r.state ?? '') };
}

export type GateDecision =
  | { kind: 'ALLOW'; stream: Stream; epoch: number; state: string }
  | { kind: 'FENCED'; stream: Stream; owner: Owner; epoch: number; state: string }
  | { kind: 'UNAVAILABLE'; stream: Stream; error: string };

/** Решение gate. Никогда не бросает: отказ чтения — UNAVAILABLE (fail-closed). */
export async function checkWriterGate(reader: OwnershipReader, stream: Stream, mark?: string): Promise<GateDecision> {
  let snap: OwnershipSnapshot | null;
  try {
    snap = await reader.read(stream, mark);
  } catch (e) {
    return { kind: 'UNAVAILABLE', stream, error: e instanceof Error ? e.message : String(e) };
  }
  if (!snap) return { kind: 'UNAVAILABLE', stream, error: `нет строки потока ${stream} в ${OWNERSHIP_TABLE}` };
  if (snap.owner !== 'CLOUD_RUN') return { kind: 'FENCED', stream, owner: snap.owner, epoch: snap.epoch, state: snap.state };
  return { kind: 'ALLOW', stream, epoch: snap.epoch, state: snap.state };
}

/**
 * Fencing-токен прогона. assertEpoch() — повторная проверка перед commit-маркером: владелец всё ещё
 * CLOUD_RUN и epoch тот же, что при входе. Иначе LoaderError(WRITER_EPOCH_CHANGED); отказ чтения —
 * LoaderError(WRITER_GATE_UNAVAILABLE): подтвердить владение нельзя → коммит не делается.
 */
export interface WriterFence {
  readonly stream: Stream;
  readonly epoch: number;
  assertEpoch(stage: string): Promise<void>;
}

export function makeWriterFence(reader: OwnershipReader, stream: Stream, epoch: number, mark?: string): WriterFence {
  return {
    stream,
    epoch,
    async assertEpoch(stage: string): Promise<void> {
      const d = await checkWriterGate(reader, stream, mark);
      if (d.kind === 'UNAVAILABLE') {
        throw new LoaderError(`${stage}: владение потоком ${stream} не подтверждено перед коммитом — ${d.error}`, WRITER_GATE_UNAVAILABLE);
      }
      if (d.kind === 'FENCED' || d.epoch !== epoch) {
        const owner = d.kind === 'FENCED' ? d.owner : 'CLOUD_RUN';
        throw new LoaderError(
          `${stage}: владение потоком ${stream} сменилось во время прогона (epoch ${epoch} → ${d.epoch}, owner ${owner}) — коммит не делается`,
          WRITER_EPOCH_CHANGED,
        );
      }
    },
  };
}

/**
 * Условная запись для DML загрузчика Cloud Run (дополнение владельца к этапу C): строка-ASSERT для
 * многооператорного скрипта ПЕРЕД DML, в той же транзакции:
 *   BEGIN TRANSACTION; ${ownershipAssertSql(...)}; MERGE …; COMMIT TRANSACTION;
 * Владелец сменился → ASSERT роняет скрипт с текстом WRITER_EPOCH_CHANGED, DML не применяется.
 * Для load-заданий условность невозможна: остаточное окно (задание принято до fence, дорабатывает после)
 * закрывает дренаж координатора — он ждёт DONE всех заданий в таблицы потока (JOBS_BY_PROJECT).
 * Значения подставляются литералами (поток — из фиксированного списка, epoch — целое), а не параметрами:
 * строку можно вставить в скрипт со своими параметрами.
 */
export function ownershipAssertSql(projectId: string, dataset: string, fence: Pick<WriterFence, 'stream' | 'epoch'>): string {
  if (!Number.isSafeInteger(fence.epoch) || fence.epoch < 1) throw new Error(`невалидный epoch ${fence.epoch}`);
  if (!/^[a-z]+$/.test(fence.stream)) throw new Error(`невалидный поток ${fence.stream}`);
  if (!/^[A-Za-z0-9_-]+$/.test(projectId) || !/^[A-Za-z0-9_]+$/.test(dataset)) throw new Error('невалидный проект/датасет');
  return `ASSERT (SELECT COUNT(*) FROM \`${projectId}.${dataset}.${OWNERSHIP_TABLE}\` WHERE stream = '${fence.stream}' AND owner = 'CLOUD_RUN' AND epoch = ${fence.epoch}) = 1 AS '${WRITER_EPOCH_CHANGED}: владение потоком ${fence.stream} (epoch ${fence.epoch}) утрачено — DML не применён'`;
}
