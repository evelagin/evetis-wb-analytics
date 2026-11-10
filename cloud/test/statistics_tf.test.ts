/**
 * M2 — контракт Terraform загрузчиков statistics-api (только текст .tf, без облака):
 * shadow пишет в __CR, prod — в боевые таблицы, все расписания на паузе, число запусков
 * не больше Apps Script после PR #414 (заказы 4, продажи 4, сверка 1 в сутки).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const TF = readFileSync(join(fileURLToPath(new URL('.', import.meta.url)), '..', '..', 'infra', 'terraform', 'wb_statistics_loaders.tf'), 'utf8');

function block(type: string, name: string): string {
  const start = TF.indexOf(`resource "${type}" "${name}" {`);
  expect(start, `${type}.${name}`).toBeGreaterThanOrEqual(0);
  let depth = 0;
  for (let i = TF.indexOf('{', start); i < TF.length; i++) {
    if (TF[i] === '{') depth++;
    else if (TF[i] === '}' && --depth === 0) return TF.slice(start, i + 1);
  }
  throw new Error('незакрытый блок');
}

describe('Terraform M2', () => {
  it('shadow Job: SA shadow, таблицы __CR', () => {
    const b = block('google_cloud_run_v2_job', 'wb_statistics_shadow');
    expect(b).toMatch(/wb_statistics_shadow\[0\]\.email/);
    expect(b).toMatch(/ENVIRONMENT\s+= "shadow"/);
    expect(b).toMatch(/ORDERS_RAW_TABLE\s+= google_bigquery_table\.raw_wb_orders_cr\[0\]\.table_id/);
    expect(b).toMatch(/SALES_RAW_TABLE\s+= google_bigquery_table\.raw_wb_sales_returns_cr\[0\]\.table_id/);
    expect(TF).toMatch(/table_id\s+= "RAW_WB_ORDERS__CR"/);
    expect(TF).toMatch(/table_id\s+= "RAW_WB_SALES_RETURNS__CR"/);
  });

  it('prod Job: SA prod, ровно боевые таблицы', () => {
    const b = block('google_cloud_run_v2_job', 'wb_statistics_prod');
    expect(b).toMatch(/wb_statistics_prod\[0\]\.email/);
    expect(b).toMatch(/ENVIRONMENT\s+= "prod"/);
    expect(b).toMatch(/ORDERS_RAW_TABLE\s+= local\.statistics_prod_tables\.orders/);
    expect(b).toMatch(/SALES_RAW_TABLE\s+= local\.statistics_prod_tables\.sales/);
    expect(TF).toMatch(/statistics_prod_tables = \{ orders = "RAW_WB_ORDERS", sales = "RAW_WB_SALES_RETURNS" \}/);
    expect(b).not.toMatch(/__CR/);
  });

  it('IAM: свои SA; shadow пишет только __CR, prod — только боевые; sa-loaders-* не расширяются', () => {
    expect(TF).not.toMatch(/loaders_(shadow|prod)/);
    expect(block('google_bigquery_table_iam_member', 'shadow_write_orders_cr')).toMatch(/raw_wb_orders_cr\[0\][\s\S]*wb_statistics_shadow\[0\]\.email/);
    expect(block('google_bigquery_table_iam_member', 'shadow_write_sales_cr')).toMatch(/raw_wb_sales_returns_cr\[0\][\s\S]*wb_statistics_shadow\[0\]\.email/);
    expect(block('google_bigquery_table_iam_member', 'prod_write_orders')).toMatch(/statistics_prod_tables\.orders[\s\S]*wb_statistics_prod\[0\]\.email/);
    expect(block('google_bigquery_table_iam_member', 'prod_write_sales')).toMatch(/statistics_prod_tables\.sales[\s\S]*wb_statistics_prod\[0\]\.email/);
    for (const r of ['wb_statistics_jobuser', 'wb_statistics_write_runs', 'wb_statistics_read_ref', 'wb_statistics_actas_deployer', 'wb_statistics_actas_terraform']) {
      expect(TF, r).toMatch(new RegExp(`resource "[a-z_]+" "${r}"`));
    }
    expect(TF).toMatch(/account_id\s+= "sa-wb-statistics-shadow"/);
    expect(TF).toMatch(/account_id\s+= "sa-wb-statistics-prod"/);
  });

  it('весь prod-контур — только при m2_prod_enabled (по умолчанию false)', () => {
    expect(block('google_service_account', 'wb_statistics_prod')).toMatch(/count\s+= var\.m2_prod_enabled \? 1 : 0/);
    for (const n of ['prod_write_orders', 'prod_write_sales']) {
      expect(block('google_bigquery_table_iam_member', n)).toMatch(/count\s+= var\.m2_prod_enabled \? 1 : 0/);
    }
    expect(block('google_cloud_run_v2_job', 'wb_statistics_prod')).toMatch(/for_each\s+= var\.m2_prod_enabled \? local\.statistics_loaders : \{\}/);
    expect(block('google_cloud_run_v2_job_iam_member', 'scheduler_statistics_prod_invoke')).toMatch(/var\.m2_prod_enabled \?/);
    expect(block('google_cloud_scheduler_job', 'wb_statistics_prod')).toMatch(/var\.m2_prod_enabled \?/);
    // SA prod (и его права/секрет через local.statistics_sa) — только при флаге.
    expect(TF).toMatch(/var\.m2_prod_enabled \? \{ prod = google_service_account\.wb_statistics_prod\[0\] \} : \{\}/);
    const vars = readFileSync(join(fileURLToPath(new URL('.', import.meta.url)), '..', '..', 'infra', 'terraform', 'variables.tf'), 'utf8');
    expect(vars).toMatch(/variable "m2_prod_enabled" \{\s*type\s+= bool\s*default = false/);
  });

  it('весь shadow-контур — только при m2_shadow_enabled (по умолчанию false)', () => {
    expect(block('google_service_account', 'wb_statistics_shadow')).toMatch(/count\s+= var\.m2_shadow_enabled \? 1 : 0/);
    for (const [t, n] of [
      ['google_bigquery_table', 'raw_wb_orders_cr'], ['google_bigquery_table', 'raw_wb_sales_returns_cr'],
      ['google_bigquery_table_iam_member', 'shadow_write_orders_cr'], ['google_bigquery_table_iam_member', 'shadow_write_sales_cr'],
    ] as const) {
      expect(block(t, n), n).toMatch(/count\s+= var\.m2_shadow_enabled \? 1 : 0/);
    }
    expect(block('google_cloud_run_v2_job', 'wb_statistics_shadow')).toMatch(/for_each\s+= var\.m2_shadow_enabled \? local\.statistics_loaders : \{\}/);
    expect(block('google_cloud_run_v2_job_iam_member', 'scheduler_statistics_shadow_invoke')).toMatch(/for_each\s+= var\.m2_shadow_enabled \? local\.statistics_loaders : \{\}/);
    expect(block('google_cloud_scheduler_job', 'wb_statistics_shadow')).toMatch(/for_each\s+= var\.m2_shadow_enabled \? local\.statistics_loaders : \{\}/);
    // SA shadow (и его права/секрет через local.statistics_sa) — только при флаге.
    expect(TF).toMatch(/var\.m2_shadow_enabled \? \{ shadow = google_service_account\.wb_statistics_shadow\[0\] \} : \{\}/);
    // Секрет читается только при включённом контуре.
    expect(TF).toMatch(/data "google_secret_manager_secret" "wb_token_statistics" \{\s*count\s+= length\(local\.statistics_sa\) > 0 \? 1 : 0/);
    const vars = readFileSync(join(fileURLToPath(new URL('.', import.meta.url)), '..', '..', 'infra', 'terraform', 'variables.tf'), 'utf8');
    expect(vars).toMatch(/variable "m2_shadow_enabled" \{\s*type\s+= bool\s*default = false/);
  });

  it('каждый resource M2 — под флагом контура: без флагов ни одного ресурса', () => {
    const heads = [...TF.matchAll(/^resource "([a-z0-9_]+)" "([a-z0-9_]+)" \{\n([^\n]*)/gm)];
    expect(heads.length).toBeGreaterThan(10);
    for (const [, t, n, first] of heads) {
      const b = block(t!, n!);
      const gated = /(count|for_each)\s+= (var\.m2_(shadow|prod)_enabled \?|local\.statistics_(sa|ref_grants)\b)/.test(b);
      expect(gated, `${t}.${n}: ${first}`).toBe(true);
    }
  });

  it('таймауты Job не меньше худшего случая с повтором cli', () => {
    const t = (key: string) => Number(new RegExp(`${key} = \\{[\\s\\S]*?timeout\\s+= "(\\d+)s"`).exec(TF)![1]);
    expect(t('sales')).toBeGreaterThanOrEqual(900);
    expect(t('"sales-reconcile"')).toBeGreaterThanOrEqual(900);
    expect(t('orders')).toBeGreaterThanOrEqual(3600);
  });

  it('все расписания созданы на паузе', () => {
    for (const n of ['wb_statistics_shadow', 'wb_statistics_prod']) {
      expect(block('google_cloud_scheduler_job', n)).toMatch(/paused\s+= true/);
    }
  });

  it('prod: заказы и продажи по 4 запуска в сутки, сверка 1; заказы и продажи не в одну минуту', () => {
    const sched = (key: string) => new RegExp(`${key} = \\{[\\s\\S]*?prod_schedule\\s+= "([^"]+)"`).exec(TF)![1]!;
    const runs = (cron: string) => cron.split(' ')[1]!.split(',').length;
    expect(runs(sched('orders'))).toBe(4);
    expect(runs(sched('sales'))).toBe(4);
    expect(runs(sched('"sales-reconcile"'))).toBe(1);
    expect(sched('orders').split(' ')[0]).not.toBe(sched('sales').split(' ')[0]);
  });
});
