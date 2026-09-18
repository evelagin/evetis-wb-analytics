/**
 * UNITKA INTEGRITY GUARD V1 — статическая проверка границы доступа (Phase 1C2A).
 *
 * Решение владельца (1C1B/1C2A): runtime-учётки Unitka (sa-loaders-*) НЕ читают evetis_ref.
 * Канон COGS приходит из ФИЗИЧЕСКОЙ копии wb_mart.UNITKA_COGS_EFFECTIVE, которую публикует
 * sa-unitka-cogs-pub. Этот тест не даёт границе тихо разрушиться при правке SQL/Terraform/workflow.
 *
 * Ограничение честности: офлайн нет парсера GoogleSQL; проверяются тексты исходников. Живую цепочку
 * вложенных вью доказывает referenced_tables реального задания (1C2A/1C3), см. доку §6.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p: string): string => readFileSync(join(root, p), 'utf8');
/** Без строчных комментариев SQL/HCL/YAML: запреты проверяются по коду, а не по пояснениям. */
const code = (s: string, marker = '--'): string => s.split('\n').map((l) => { const i = l.indexOf(marker); return i >= 0 ? l.slice(0, i) : l; }).join('\n');

function viewBodies(sql: string): Map<string, string> {
  const out = new Map<string, string>();
  const re = /CREATE OR REPLACE VIEW `[^`]+\.(\w+)` AS\n([\s\S]*?)(?=\nCREATE |\n-- ─── |$)/g;
  for (const m of code(sql).matchAll(re)) out.set(m[1]!, m[2]!);
  return out;
}
const datasetsOf = (body: string): Set<string> =>
  new Set([...body.matchAll(/`project-fa311fc0-4d87-4781-986\.(\w+)\.\w+`/g)].map((m) => m[1]!));

describe('SQL: вью Guard не читают evetis_ref (runtime Unitka)', () => {
  const views = viewBodies(read('sql/unitka/integrity_v1.sql'));
  it('в файле ровно две вью Guard', () => {
    expect([...views.keys()].sort()).toEqual(['V_UNITKA_COGS_CANONICAL', 'V_UNITKA_INTEGRITY']);
  });
  it.each(['V_UNITKA_INTEGRITY', 'V_UNITKA_COGS_CANONICAL'])('%s: только wb_raw/wb_mart, evetis_ref нет', (name) => {
    const body = views.get(name)!;
    expect(body).not.toMatch(/evetis_ref/);
    expect([...datasetsOf(body)].every((d) => d === 'wb_raw' || d === 'wb_mart')).toBe(true);
  });
  it('V_UNITKA_COGS_CANONICAL читает ФИЗИЧЕСКУЮ копию UNITKA_COGS_EFFECTIVE и отдаёт метаданные свежести', () => {
    const body = views.get('V_UNITKA_COGS_CANONICAL')!;
    expect(body).toContain('wb_mart.UNITKA_COGS_EFFECTIVE');
    expect(body).toMatch(/snapshot_published_at/);
    expect(body).not.toMatch(/V_PRODUCT_COGS_EFFECTIVE/);
  });
});

describe('SQL: публикация COGS — fail-closed контракт', () => {
  const sql = code(read('sql/unitka/cogs_publication_v1.sql'));
  const proc = sql.slice(sql.indexOf('CREATE OR REPLACE PROCEDURE'));
  it('evetis_ref читается только внутри процедуры публикации (источник staging)', () => {
    // Ссылки на объекты — только в обратных кавычках; текст описаний (OPTIONS) не в счёт.
    const refs = (t: string): string[] => [...t.matchAll(/`[^`]*evetis_ref\.(\w+)`/g)].map((m) => m[1]!);
    expect(refs(sql.slice(0, sql.indexOf('CREATE OR REPLACE PROCEDURE')))).toEqual([]);
    expect(new Set(refs(proc))).toEqual(new Set(['V_PRODUCT_COGS_EFFECTIVE']));
  });
  it('инварианты P1–P8 ДО подмены, затем транзакция и SUCCESS; ROLLBACK/FAILED в обработчике', () => {
    const iAssert = proc.indexOf("AS 'UNITKA_COGS P8");
    const iTx = proc.indexOf('BEGIN TRANSACTION');
    for (let k = 1; k <= 8; k++) expect(proc).toContain(`'UNITKA_COGS P${k}:`);
    expect(iAssert).toBeGreaterThan(0);
    expect(iTx).toBeGreaterThan(iAssert);
    expect(proc).toMatch(/COMMIT TRANSACTION/);
    expect(proc).toMatch(/ROLLBACK TRANSACTION/);
    expect(proc).toMatch(/status = 'FAILED'/);
    expect(proc).toMatch(/status = 'SKIPPED_LOCKED'/);
  });
  it('процедура не создаёт постоянных таблиц (у SA нет права создания; staging — TEMP)', () => {
    expect(proc).not.toMatch(/CREATE (OR REPLACE )?TABLE/);
    expect(proc).toMatch(/CREATE TEMP TABLE s/);
  });
  it('таблица копии хранит интервалы и метаданные публикации', () => {
    for (const col of ['internal_sku', 'effective_from', 'effective_to', 'product_cogs_rub', 'published_at', 'publish_run_id', 'source_fingerprint']) {
      expect(sql).toMatch(new RegExp(`\\n  ${col}\\s`));
    }
  });
});

describe('Terraform: loaders не получают evetis_ref; публикация — отдельная учётка; журнал отложен', () => {
  const tf = ['unitka_engine.tf', 'unitka_cogs_publication.tf', 'iam.tf', 'bigquery.tf'].map((f) => code(read(`infra/terraform/${f}`), '#')).join('\n');
  const pub = code(read('infra/terraform/unitka_cogs_publication.tf'), '#');
  it('нет google_bigquery_dataset_access (смешение с dataset_iam_member снимает авторизации)', () => {
    expect(tf).not.toMatch(/google_bigquery_dataset_access/);
  });
  it('единственный новый читатель evetis_ref — sa-unitka-cogs-pub', () => {
    const blocks = [...pub.matchAll(/resource "google_bigquery_dataset_iam_member" "(\w+)" \{([\s\S]*?)\n\}/g)];
    expect(blocks.map((b) => b[1])).toEqual(['unitka_cogs_pub_read_ref']);
    expect(blocks[0]![2]).toMatch(/dataset_id = "evetis_ref"/);
    expect(blocks[0]![2]).toMatch(/unitka_cogs_pub\.email/);
    expect(pub).not.toMatch(/loaders_(prod|shadow)/);
  });
  it('dataset-wide dataEditor на wb_mart не выдаётся; запись — потабличная', () => {
    const dsBlocks = [...pub.matchAll(/resource "google_bigquery_dataset_iam_member" "\w+" \{([\s\S]*?)\n\}/g)].map((b) => b[1]!);
    expect(dsBlocks.some((b) => /dataEditor|dataOwner|admin/.test(b))).toBe(false);
    expect(pub).toMatch(/google_bigquery_table_iam_member" "unitka_cogs_pub_write"/);
  });
  it('журнал issue UNITKA_QA_ISSUES отложен (D4)', () => {
    expect(tf).not.toMatch(/UNITKA_QA_ISSUES/);
  });
  it('оба Job’а Unitka объявлены с UNITKA_INTEGRITY_MODE = "off" (значение при создании)', () => {
    const eng = code(read('infra/terraform/unitka_engine.tf'), '#');
    expect([...eng.matchAll(/UNITKA_INTEGRITY_MODE = "(\w+)"/g)].map((m) => m[1])).toEqual(['off', 'off']);
  });
  it('расписание публикации — :50, 07–23 МСК', () => {
    expect(pub).toMatch(/schedule\s+= "50 7-23 \* \* \*"/);
    expect(pub).toMatch(/time_zone = "Europe\/Moscow"/);
  });
});

describe('Workflow: владелец runtime-режима — deploy-shadow; prod не активируется', () => {
  it('deploy-shadow ставит unitka-engine-shadow UNITKA_INTEGRITY_MODE=observe', () => {
    const y = code(read('.github/workflows/deploy-shadow.yml'), '#');
    const step = y.slice(y.indexOf('gcloud run jobs update unitka-engine-shadow'));
    expect(step.split('\n').slice(0, 4).join('\n')).toMatch(/UNITKA_INTEGRITY_MODE=observe/);
    expect(y.match(/UNITKA_INTEGRITY_MODE=observe/g)).toHaveLength(1);
  });
  it('deploy-prod режим не задаёт (в коде по умолчанию off)', () => {
    expect(code(read('.github/workflows/deploy-prod.yml'), '#')).not.toMatch(/UNITKA_INTEGRITY_MODE/);
  });
});
