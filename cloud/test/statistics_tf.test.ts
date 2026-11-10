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
    expect(b).toMatch(/loaders_shadow\.email/);
    expect(b).toMatch(/ENVIRONMENT\s+= "shadow"/);
    expect(b).toMatch(/ORDERS_RAW_TABLE\s+= google_bigquery_table\.raw_wb_orders_cr\.table_id/);
    expect(b).toMatch(/SALES_RAW_TABLE\s+= google_bigquery_table\.raw_wb_sales_returns_cr\.table_id/);
    expect(TF).toMatch(/table_id\s+= "RAW_WB_ORDERS__CR"/);
    expect(TF).toMatch(/table_id\s+= "RAW_WB_SALES_RETURNS__CR"/);
  });

  it('prod Job: SA prod, ровно боевые таблицы', () => {
    const b = block('google_cloud_run_v2_job', 'wb_statistics_prod');
    expect(b).toMatch(/loaders_prod\.email/);
    expect(b).toMatch(/ENVIRONMENT\s+= "prod"/);
    expect(b).toMatch(/ORDERS_RAW_TABLE\s+= "RAW_WB_ORDERS"/);
    expect(b).toMatch(/SALES_RAW_TABLE\s+= "RAW_WB_SALES_RETURNS"/);
    expect(b).not.toMatch(/__CR/);
  });

  it('IAM: shadow-SA пишет только __CR, prod-SA — только боевые', () => {
    expect(block('google_bigquery_table_iam_member', 'shadow_write_orders_cr')).toMatch(/loaders_shadow/);
    expect(block('google_bigquery_table_iam_member', 'shadow_write_sales_cr')).toMatch(/loaders_shadow/);
    expect(block('google_bigquery_table_iam_member', 'prod_write_orders')).toMatch(/table_id\s+= "RAW_WB_ORDERS"[\s\S]*loaders_prod\.email/);
    expect(block('google_bigquery_table_iam_member', 'prod_write_sales')).toMatch(/table_id\s+= "RAW_WB_SALES_RETURNS"[\s\S]*loaders_prod\.email/);
    for (const n of ['prod_write_orders', 'prod_write_sales']) expect(block('google_bigquery_table_iam_member', n)).not.toMatch(/loaders_shadow/);
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
