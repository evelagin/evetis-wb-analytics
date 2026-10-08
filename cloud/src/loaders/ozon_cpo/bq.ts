/**
 * OZON CPO (Phase B) — запись RAW и журнала. Шаблон StorageBq (E4): постоянный stage под
 * Terraform, загрузка WRITE_TRUNCATE и атомарная замена окна в multi-statement transaction,
 * где КАЖДАЯ блокирующая проверка выполняется до COMMIT (IF … RAISE → ROLLBACK).
 *
 * Гранулярность замены — блок окна (≤ 1 календарного месяца) × оба семейства сразу: если одно
 * семейство не пришло, не меняется ни одно. Всё вне [from, to] не затрагивается.
 *
 * Идемпотентность: повтор того же окна удаляет и вставляет те же строки (row_key стабилен);
 * перекрывающиеся окна не удваивают строки — после вставки проверяется уникальность row_key
 * по ВСЕЙ таблице, а не только по окну.
 *
 * Fail-closed против «пустого отчёта»: сутки, которые в RAW были, а в новом отчёте исчезли
 * целиком, — отказ (CPO_ROWS_WOULD_DISAPPEAR). Сторно у «Оплаты за заказ» не наблюдалось ни разу;
 * исчезновение суток — признак сбоя выгрузки, а не правды источника. Осознанный приём —
 * разовым запуском с OZON_CPO_ACCEPT_REMOVALS=1.
 */
import { BigQuery, type JobLoadMetadata } from '@google-cloud/bigquery';
import { writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { CpoRawRow } from './normalize.js';
import type { CpoFamily } from './parse.js';
import { LoaderError } from '../../errors.js';

export interface CpoBqLike {
  query(o: { query: string; params?: Record<string, unknown>; types?: Record<string, unknown>; location?: string }): Promise<[unknown[]]>;
  dataset(d: string): { table(t: string): { load(src: string, md: JobLoadMetadata): Promise<unknown> } };
}

export interface CpoChunkJournal {
  runId: string; mode: 'DAILY' | 'BACKFILL'; windowFrom: string; windowTo: string;
  rowsAllSku: number; rowsSearch: number; expenseAllSku: string; expenseSearch: string;
  reportUuids: string; imageDigest: string; gitSha: string; executionId: string; startedAt: string;
}

export interface CpoRunJournal {
  runId: string; mode: 'DAILY' | 'BACKFILL'; status: 'COMPLETE' | 'PARTIAL' | 'ERROR';
  windowFrom: string; windowTo: string; chunksTotal: number; chunksLoaded: number;
  rowsAllSku: number; rowsSearch: number; expenseAllSku: string; expenseSearch: string;
  errorCode: string | null; errorMessage: string | null;
  imageDigest: string; gitSha: string; executionId: string; startedAt: string;
}

export const CPO_RAW_TABLE = 'RAW_OZON_ADS_CPO_ORDERS';
export const CPO_RUNS_TABLE = 'OZON_CPO_ORDER_RUNS';
export const CPO_FAMILIES: readonly CpoFamily[] = ['ALL_SKU_PROMO', 'SEARCH_PROMO'];

/** SQL замены блока окна. Вынесен отдельно — его форма закреплена тестом. */
export function cpoReplaceSql(target: string, stage: string, runs: string): string {
  const win = 'charge_date BETWEEN DATE(@from) AND DATE(@to)';
  return `BEGIN
  DECLARE stage_rows, stage_outside, stage_foreign, stage_dup, stage_bad_family, lost_days, target_rows, target_dup INT64;
  DECLARE stage_cents, target_cents, old_rows INT64;
  DECLARE old_rub NUMERIC;
  DECLARE lost_sample STRING;

  BEGIN TRANSACTION;

  -- Gate 1: stage — ровно то, что прогон собирается вставить. target ещё не тронут.
  SET stage_rows = (SELECT COUNT(*) FROM ${stage});
  SET stage_outside = (SELECT COUNTIF(NOT (${win})) FROM ${stage});
  SET stage_foreign = (SELECT COUNTIF(run_id != @runId) FROM ${stage});
  SET stage_bad_family = (SELECT COUNTIF(report_family NOT IN ('ALL_SKU_PROMO', 'SEARCH_PROMO')) FROM ${stage});
  SET stage_dup = (SELECT COUNT(*) - COUNT(DISTINCT row_key) FROM ${stage});
  SET stage_cents = (SELECT CAST(ROUND(IFNULL(SUM(expense_rub), 0) * 100) AS INT64) FROM ${stage});
  IF stage_rows != @expectedRows THEN
    RAISE USING MESSAGE = FORMAT('CPO_STAGE_ROWS: ожидалось %t, в stage %t', @expectedRows, stage_rows);
  END IF;
  IF stage_foreign != 0 THEN
    RAISE USING MESSAGE = FORMAT('CPO_STAGE_FOREIGN_RUN: %t строк stage от другого прогона', stage_foreign);
  END IF;
  IF stage_outside != 0 OR stage_bad_family != 0 THEN
    RAISE USING MESSAGE = FORMAT('CPO_STAGE_OUT_OF_WINDOW: %t строк вне %t..%t, %t с неизвестным семейством', stage_outside, @from, @to, stage_bad_family);
  END IF;
  IF stage_dup != 0 THEN
    RAISE USING MESSAGE = FORMAT('CPO_STAGE_DUPLICATE_KEY: %t дублей естественного ключа', stage_dup);
  END IF;
  IF stage_cents != @expectedCents THEN
    RAISE USING MESSAGE = FORMAT('CPO_STAGE_AMOUNT: источник %t коп., stage %t коп.', @expectedCents, stage_cents);
  END IF;

  -- Gate 2: сутки, которые в RAW есть, а в новом отчёте исчезли целиком.
  SET (lost_days, lost_sample) = (
    SELECT AS STRUCT COUNT(*), STRING_AGG(CONCAT(t.report_family, ' ', CAST(t.charge_date AS STRING)), ', ' ORDER BY t.charge_date LIMIT 5)
    FROM (SELECT DISTINCT report_family, charge_date FROM ${target} WHERE ${win}) t
    LEFT JOIN (SELECT DISTINCT report_family, charge_date FROM ${stage}) s USING (report_family, charge_date)
    WHERE s.charge_date IS NULL);
  IF lost_days != 0 AND NOT @acceptRemovals THEN
    RAISE USING MESSAGE = FORMAT('CPO_ROWS_WOULD_DISAPPEAR: %t суток исчезли бы из RAW (%t); разовый приём — OZON_CPO_ACCEPT_REMOVALS=1', lost_days, lost_sample);
  END IF;

  SET (old_rows, old_rub) = (SELECT AS STRUCT COUNT(*), IFNULL(SUM(expense_rub), 0) FROM ${target} WHERE ${win});

  -- Атомарная замена блока: оба семейства, только окно [from, to].
  DELETE FROM ${target} WHERE ${win};
  INSERT INTO ${target} SELECT * FROM ${stage};

  -- Gate 3: то, что легло, и уникальность ключа по ВСЕЙ таблице (перекрытие окон не удваивает).
  SET target_rows = (SELECT COUNT(*) FROM ${target} WHERE ${win});
  SET target_cents = (SELECT CAST(ROUND(IFNULL(SUM(expense_rub), 0) * 100) AS INT64) FROM ${target} WHERE ${win});
  SET target_dup = (SELECT COUNT(*) - COUNT(DISTINCT row_key) FROM ${target});
  IF target_rows != @expectedRows OR target_cents != @expectedCents THEN
    RAISE USING MESSAGE = FORMAT('CPO_TARGET_MISMATCH: строк %t (ожидалось %t), коп. %t (ожидалось %t)', target_rows, @expectedRows, target_cents, @expectedCents);
  END IF;
  IF target_dup != 0 THEN
    RAISE USING MESSAGE = FORMAT('CPO_TARGET_DUPLICATE_KEY: %t дублей row_key в RAW', target_dup);
  END IF;

  -- Журнал блока — в той же транзакции: строка журнала есть тогда и только тогда, когда данные легли.
  INSERT INTO ${runs} (run_id, record_type, mode, status, window_from, window_to, chunk_from, chunk_to,
      rows_all_sku, rows_search, expense_all_sku_rub, expense_search_rub, rows_replaced, expense_replaced_rub,
      report_uuids, chunks_total, chunks_loaded, started_at, completed_at, error_code, error_message,
      image_digest, git_sha, execution_id)
  VALUES (@runId, 'CHUNK', @mode, 'COMPLETE', DATE(@windowFrom), DATE(@windowTo), DATE(@from), DATE(@to),
      @rowsAllSku, @rowsSearch, CAST(@expenseAllSku AS NUMERIC), CAST(@expenseSearch AS NUMERIC), old_rows, old_rub,
      @reportUuids, NULL, NULL, TIMESTAMP(@startedAt), CURRENT_TIMESTAMP(), NULL, NULL,
      @imageDigest, @gitSha, @executionId);

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;`;
}

/** RAISE из транзакции → LoaderError с кодом CPO_*; прочие ошибки (транзиентный BigQuery) — как есть. */
export function cpoRaiseError(e: unknown): unknown {
  const m = /\b(CPO_[A-Z_]+):/.exec(e instanceof Error ? e.message : String(e));
  return m ? new LoaderError(e instanceof Error ? e.message : String(e), m[1]!) : e;
}

export class CpoBq {
  private readonly bq: CpoBqLike;
  constructor(
    private readonly projectId: string,
    private readonly location: string,
    private readonly dataset: string,
    bqClient?: CpoBqLike,
  ) {
    if (!/^[A-Za-z0-9_]+$/.test(dataset)) throw new Error(`Invalid dataset '${dataset}'`);
    this.bq = bqClient ?? (new BigQuery({ projectId }) as unknown as CpoBqLike);
  }
  private fqn(t: string): string { return `\`${this.projectId}.${this.dataset}.${t}\``; }

  /** Замена блока окна обоими семействами. Возвращает число строк блока в RAW после COMMIT. */
  async replaceChunk(rows: readonly CpoRawRow[], chunk: { from: string; to: string }, jobId: string,
    j: CpoChunkJournal, acceptRemovals: boolean): Promise<void> {
    const stageTable = `${CPO_RAW_TABLE}__STAGE`;
    if (rows.length > 0) {
      const file = join(tmpdir(), `ozon_cpo_${jobId.replace(/[^A-Za-z0-9_]/g, '_').slice(-80)}.jsonl`);
      writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n') + '\n', 'utf8');
      try {
        await this.bq.dataset(this.dataset).table(stageTable).load(file, {
          sourceFormat: 'NEWLINE_DELIMITED_JSON', writeDisposition: 'WRITE_TRUNCATE', createDisposition: 'CREATE_NEVER',
          jobId, location: this.location,
        });
      } finally {
        try { rmSync(file, { force: true }); } catch { /* noop */ }
      }
    } else {
      // Пустой блок законен (нет заказов «Оплаты за заказ»): stage опустошается DML, без загрузки пустого файла.
      await this.bq.query({ query: `DELETE FROM ${this.fqn(stageTable)} WHERE TRUE`, location: this.location });
    }
    const cents = rows.reduce((n, r) => n + Math.round(Number(r.expense_rub) * 100), 0);
    try { await this.bq.query({
      query: cpoReplaceSql(this.fqn(CPO_RAW_TABLE), this.fqn(stageTable), this.fqn(CPO_RUNS_TABLE)),
      params: {
        from: chunk.from, to: chunk.to, runId: j.runId, mode: j.mode, windowFrom: j.windowFrom, windowTo: j.windowTo,
        expectedRows: rows.length, expectedCents: cents, acceptRemovals,
        rowsAllSku: j.rowsAllSku, rowsSearch: j.rowsSearch, expenseAllSku: j.expenseAllSku, expenseSearch: j.expenseSearch,
        reportUuids: j.reportUuids, startedAt: j.startedAt, imageDigest: j.imageDigest, gitSha: j.gitSha, executionId: j.executionId,
      },
      types: {
        from: 'STRING', to: 'STRING', runId: 'STRING', mode: 'STRING', windowFrom: 'STRING', windowTo: 'STRING',
        expectedRows: 'INT64', expectedCents: 'INT64', acceptRemovals: 'BOOL',
        rowsAllSku: 'INT64', rowsSearch: 'INT64', expenseAllSku: 'STRING', expenseSearch: 'STRING',
        reportUuids: 'STRING', startedAt: 'STRING', imageDigest: 'STRING', gitSha: 'STRING', executionId: 'STRING',
      },
      location: this.location,
    }); } catch (e) {
      // Отказ проверки транзакции (RAISE «CPO_…: …») сохраняет свой код — он идёт в журнал и метку алерта.
      throw cpoRaiseError(e);
    }
  }

  /** Итог прогона: COMPLETE / PARTIAL / ERROR. Отдельной вставкой — у ERROR данных нет. */
  async journalRun(j: CpoRunJournal): Promise<void> {
    await this.bq.query({
      query: `INSERT INTO ${this.fqn(CPO_RUNS_TABLE)} (run_id, record_type, mode, status, window_from, window_to,
          chunk_from, chunk_to, rows_all_sku, rows_search, expense_all_sku_rub, expense_search_rub, rows_replaced,
          expense_replaced_rub, report_uuids, chunks_total, chunks_loaded, started_at, completed_at, error_code,
          error_message, image_digest, git_sha, execution_id)
        VALUES (@runId, 'RUN', @mode, @status, DATE(@windowFrom), DATE(@windowTo), NULL, NULL, @rowsAllSku, @rowsSearch,
          CAST(@expenseAllSku AS NUMERIC), CAST(@expenseSearch AS NUMERIC), NULL, NULL, NULL, @chunksTotal, @chunksLoaded,
          TIMESTAMP(@startedAt), CURRENT_TIMESTAMP(), @errorCode, @errorMessage, @imageDigest, @gitSha, @executionId)`,
      params: { ...j, errorCode: j.errorCode, errorMessage: j.errorMessage },
      types: {
        runId: 'STRING', mode: 'STRING', status: 'STRING', windowFrom: 'STRING', windowTo: 'STRING',
        chunksTotal: 'INT64', chunksLoaded: 'INT64', rowsAllSku: 'INT64', rowsSearch: 'INT64',
        expenseAllSku: 'STRING', expenseSearch: 'STRING', errorCode: 'STRING', errorMessage: 'STRING',
        imageDigest: 'STRING', gitSha: 'STRING', executionId: 'STRING', startedAt: 'STRING',
      },
      location: this.location,
    });
  }
}
