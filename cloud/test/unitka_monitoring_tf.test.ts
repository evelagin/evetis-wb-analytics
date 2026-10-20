/**
 * Приёмка 20.10.2026 (F): мониторинг полезного результата писателей Юнитки и P&L.
 * Контракт infra/terraform/unitka_monitoring.tf: отсутствие ряда и ряд с нулями — НЕ исправная работа,
 * фильтры событий совпадают с тем, что пишет код, новые политики создаются выключенными.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const tf = readFileSync(new URL('../../infra/terraform/unitka_monitoring.tf', import.meta.url), 'utf8');
const src = (f: string): string => readFileSync(new URL(`../src/loaders/unitka/${f}`, import.meta.url), 'utf8');
const block = (name: string): string => {
  const i = tf.indexOf(`resource "google_monitoring_alert_policy" "${name}"`);
  expect(i).toBeGreaterThanOrEqual(0);
  return tf.slice(i, tf.indexOf('\n}\n', i));
};

describe('heartbeat: отсутствие события = сигнал', () => {
  const hb = block('unitka_heartbeat');
  it('нет ни одного ряда → vector(0) < 1 → срабатывание; ноль событий → срабатывание', () => {
    expect(hb).toContain('or vector(0)) < 1');
    expect(hb).toContain('increase(');
    expect(hb).not.toMatch(/condition_absent/); // окно absence ≤ 1 суток — короче суточного цикла
  });
  it('создаётся выключенной: метрика не заполняется задним числом', () => {
    expect(hb).toMatch(/enabled\s+=\s+false/);
  });
  it('окна: WB 27 ч (после резерва 09:30Z), остальные 26 ч', () => {
    expect(tf).toMatch(/unitka_wb_lcd_committed = \{[\s\S]*?window\s+=\s+"27h"/);
    for (const k of ['unitka_ozon_lcd_committed', 'unitka_store_pnl_published', 'ozon_store_pnl_published']) {
      expect(tf).toMatch(new RegExp(`${k} = \\{[\\s\\S]*?window\\s+=\\s+"26h"`));
    }
  });
});

describe('фильтры событий = то, что пишет код', () => {
  it('LCD_COMMITTED пишется через logLifecycle, который отбрасывает ctx.message (имя события не затирается)', () => {
    expect(src('lifecycle_log.ts')).toContain("filter(([k]) => k !== 'message')");
    expect(src('lifecycle_log.ts')).toContain("log[level]('unitka_lifecycle', { lifecycle_event: event, platform");
    expect(tf).toContain('jsonPayload.lifecycle_event=\\"LCD_COMMITTED\\"');
  });
  it('P&L: имена событий публикации совпадают с кодом', () => {
    expect(src('storepnl/index.ts')).toContain("logger.info('store_pnl_tab_published', { action: 'WRITE'");
    expect(src('storepnl/index.ts')).toContain("logger.info('store_pnl_tab_published', { action: 'NOOP'");
    expect(src('ozonstorepnl/index.ts')).toContain("logger.info('ozon_store_pnl_readback'");
  });
  it('NEXT_MONTH_SECTION_MISSING — не сигнал: месяц создаёт суточный прогон 2-го числа (Gate 10), до этого предупреждение верно всегда', () => {
    expect(src('index.ts')).toContain("log.warn('unitka_next_month_section', { code: precheck.code");
    expect(src('wb_lifecycle.ts')).toContain("lifecycleLog(a.log, 'NEW_MONTH_CREATED'");
    expect(tf).not.toMatch(/jsonPayload\.code="NEXT_MONTH_SECTION_MISSING"/);
  });
  it('коды блока публикации Ozon и предупреждения WB совпадают с кодом', () => {
    expect(src('ozonstorepnl/index.ts')).toContain("'OZON_STORE_PNL_PUBLISH_GATE'");
    expect(src('ozonstorepnl/index.ts')).toContain("'OZON_STORE_PNL_SOURCE_NOT_READY'");
    expect(src('ozonstorepnl/index.ts')).toContain("logger.warn('ozon_store_pnl_publish_gate'");
    expect(src('storepnl/index.ts')).toContain("logger.warn('store_pnl_new_operations'");
    expect(src('storepnl/index.ts')).toContain("logger.warn('store_pnl_publish_gate'");
  });
  it('ни одна новая политика не фильтрует по jsonPayload.message="loader_failed" (имя затирается ctx.message)', () => {
    expect(tf).not.toContain('loader_failed"');
  });
});

describe('классы: отказ / блок публикации / внимание / конфигурация', () => {
  it('отказ: код приложения без сигналов сверки и блоков публикации ИЛИ execution failed; без error_code в метках', () => {
    const b = block('unitka_writers_failed');
    expect(b).toContain('NOT jsonPayload.code=${local.unitka_attention_codes} AND NOT jsonPayload.code=${local.unitka_blocked_codes}');
    expect(b).toContain('protoPayload.methodName="/Jobs.RunJob"');
    expect(b).not.toContain('error_code');
    expect(b).toMatch(/enabled\s+=\s+false/);
  });
  it('блок публикации и внимание — метрические PromQL-политики (письмо при открытии/закрытии, без ежедневного повтора)', () => {
    for (const n of ['unitka_publication_blocked', 'unitka_owner_attention']) {
      const b = block(n);
      expect(b).toContain('condition_prometheus_query_language');
      expect(b).toContain('[26h])) > 0');
      expect(b).not.toContain('condition_matched_log');
      expect(b).toMatch(/enabled\s+=\s+false/);
    }
    expect(block('unitka_owner_attention')).toContain('sum by (job, code)');
    expect(block('unitka_publication_blocked')).toContain('sum by (job)');
  });
  it('пауза расписания и изменение задания писателя (PUBLISH=0, образ) видны', () => {
    const b = block('unitka_writer_config');
    expect(b).toContain('CloudScheduler.PauseJob');
    expect(b).toContain('google.cloud.run.v1.Jobs.ReplaceJob');
  });
  it('старые политики не трогаются этим файлом', () => {
    expect(tf).not.toMatch(/resource "google_monitoring_alert_policy" "(unitka_engine_failed|ozon_unitka_failed)"/);
  });
});
