/**
 * Приёмка 20.10.2026 (F): мониторинг полезного результата писателей Юнитки и P&L.
 * Контракт infra/terraform/unitka_monitoring.tf: отсутствие ряда и ряд с нулями — НЕ исправная работа,
 * фильтры событий совпадают с тем, что пишет код, новые политики создаются выключенными.
 */
import { readdirSync, readFileSync } from 'node:fs';
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
  it('включение по писателю: к переходу месяца — только LCD WB и Ozon, P&L выключены', () => {
    expect(hb).toContain('enabled      = each.value.enabled');
    const on = (k: string) => new RegExp(`${k} = \\{\\n      job      = [^\\n]+\\n      enabled  = (true|false)`).exec(tf)?.[1];
    expect(on('unitka_wb_lcd_committed')).toBe('true');
    expect(on('unitka_ozon_lcd_committed')).toBe('true');
    expect(on('unitka_store_pnl_published')).toBe('false');
    expect(on('ozon_store_pnl_published')).toBe('false');
  });
  it('heartbeat: окно 24 ч + duration 1 ч = 25 ч у всех четырёх (duration ≤ 24 ч, сумма ≤ 25 ч)', () => {
    expect(hb).toContain('[24h])) or vector(0)) < 1');
    expect(hb).toContain('duration            = each.value.duration');
    for (const k of ['unitka_wb_lcd_committed', 'unitka_ozon_lcd_committed', 'unitka_store_pnl_published', 'ozon_store_pnl_published']) {
      const m = tf.match(new RegExp(`${k} = \\{[\\s\\S]*?duration\\s+=\\s+"(\\d+)s"`));
      expect(m).not.toBeNull();
      expect(24 * 3600 + Number(m![1])).toBeLessThanOrEqual(25 * 3600);
    }
  });
  it('ни одно PromQL-окно не длиннее 25 ч: предел пользовательских лог-метрик', () => {
    expect(tf).toContain('unitka_signal_window = "24h30m"');
    const ranges = [...tf.matchAll(/\[(\d+)h(\d+m)?\]/g)].map((m) => Number(m[1]) + (m[2] ? parseInt(m[2], 10) / 60 : 0));
    expect(ranges.length).toBeGreaterThan(0);
    for (const h of ranges) expect(h).toBeLessThanOrEqual(25);
    expect(tf).not.toMatch(/\[2[6-9]h\]/);
    // у PromQL с duration > 0 — только окно heartbeat; прочие условия без выдержки
    expect([...tf.matchAll(/duration\s+=\s+"(\d+)s"/g)].every((m) => m[1] === '0' || m[1] === '3600')).toBe(true);
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
  const metric = (name: string): string => {
    const i = tf.indexOf(`resource "google_logging_metric" "${name}"`);
    expect(i).toBeGreaterThanOrEqual(0);
    return tf.slice(i, tf.indexOf('\n}\n', i));
  };
  it('классы 1–4 — метрические PromQL-политики (письмо при открытии/закрытии, без повтора на каждое совпадение)', () => {
    for (const n of ['unitka_writers_failed', 'unitka_publication_blocked', 'unitka_owner_attention']) {
      const b = block(n);
      expect(b).toContain('condition_prometheus_query_language');
      expect(b).toContain('[${local.unitka_signal_window}])) > 0');
      expect(b).not.toContain('condition_matched_log');
    }
    // К переходу месяца включается только класс 2 (сбой писателя); 3 и 4 остаются выключенными.
    expect(block('unitka_writers_failed')).toMatch(/enabled\s+=\s+true/);
    expect(block('unitka_publication_blocked')).toMatch(/enabled\s+=\s+false/);
    expect(block('unitka_owner_attention')).toMatch(/enabled\s+=\s+false/);
    expect(block('unitka_writers_failed')).toContain('sum by (job_name) (');
    expect(block('unitka_publication_blocked')).toContain('sum by (job_name) (');
    expect(block('unitka_owner_attention')).toContain('sum by (job_name, event) (');
    expect(tf).not.toMatch(/key\s+=\s+"job"/); // ряд по писателю — ресурсная метка job_name
  });
  it('отказ: код приложения без сигналов внимания и блоков публикации; execution failed — только у писателей Юнитки', () => {
    const m = metric('unitka_writer_failures');
    expect(m).toContain('NOT jsonPayload.message=${local.unitka_attention_events} AND NOT jsonPayload.code=${local.unitka_blocked_codes}');
    expect(m).toContain('protoPayload.methodName="/Jobs.RunJob" AND NOT resource.labels.job_name=${local.unitka_pnl_jobs}');
    expect(m).not.toContain('error_code');
  });
  it('отказ P&L всегда пишет запись с кодом (fatalRecord) — системное событие P&L в класс 2 не нужно', () => {
    expect(readFileSync(new URL('../src/cli.ts', import.meta.url), 'utf8')).toContain('console.error(JSON.stringify(fatalRecord(e)));');
  });
  it('блок публикации: коды исключений гейта WB и Ozon совпадают с кодом и исключены из отказов', () => {
    expect(src('storepnl/index.ts')).toContain("'STORE_PNL_PUBLISH_GATE'");
    for (const c of ['OZON_STORE_PNL_PUBLISH_GATE', 'OZON_STORE_PNL_SOURCE_NOT_READY', 'STORE_PNL_PUBLISH_GATE']) {
      expect(tf).toMatch(new RegExp(`unitka_blocked_codes\\s+=\\s+".*\\\\"${c}\\\\"`));
    }
  });
  it('внимание: все события сверки по имени (6 кодов отказа controlled — один сигнал) и CPO Ozon', () => {
    expect(src('index.ts')).toContain("log.error('unitka_controlled_refused'");
    expect(src('index.ts')).toContain("log.error('unitka_recon_residual_persistent'");
    expect(src('index.ts')).toContain("a.log.error('unitka_integrity_data_error'");
    expect(src('ozon/loader.ts')).toContain("log.error('ozon_unitka_cpo_unresolved'");
    for (const e of ['unitka_controlled_refused', 'unitka_recon_residual_persistent', 'unitka_integrity_data_error', 'ozon_unitka_cpo_unresolved']) {
      expect(tf).toMatch(new RegExp(`unitka_attention_events = ".*\\\\"${e}\\\\"`));
    }
    expect(metric('unitka_owner_attention_events')).toContain('jsonPayload.message=${local.unitka_attention_events}');
  });
  it('конфигурация: пауза/изменение/удаление расписания и задания, кроме штатного выката sa-deployer', () => {
    const b = block('unitka_writer_config');
    for (const m of ['CloudScheduler.PauseJob', 'CloudScheduler.UpdateJob', 'CloudScheduler.DeleteJob',
      'google.cloud.run.v1.Jobs.ReplaceJob', 'google.cloud.run.v2.Jobs.UpdateJob', 'google.cloud.run.v1.Jobs.DeleteJob']) {
      expect(b).toContain(m);
    }
    expect(b).toContain('NOT protoPayload.authenticationInfo.principalEmail="${google_service_account.deployer.email}"');
  });
  it('старые политики не трогаются этим файлом', () => {
    expect(tf).not.toMatch(/resource "google_monitoring_alert_policy" "(unitka_engine_failed|unitka_platform_failed|ozon_unitka_failed|unitka_store_pnl_failed|ozon_store_pnl_failed)"/);
    for (const n of ['unitka_engine_failed', 'unitka_platform_failed', 'ozon_unitka_failed', 'unitka_store_pnl_failed', 'ozon_store_pnl_failed']) {
      expect(tf).toContain(n); // перечислены в порядке включения как снимаемые последними
    }
  });
});

describe('ограничения Cloud Monitoring на имена', () => {
  // apply 21.10.2026: «display_name must not be more than 100 characters» на имени в 107 байт —
  // предел считается в байтах UTF-8, кириллица занимает 2 байта.
  const dir = new URL('../../infra/terraform/', import.meta.url);
  const files = readdirSync(dir).filter((f) => f.endsWith('.tf'));
  const heartbeatKeys = [...tf.matchAll(/^ {4}(\w+) = \{\n {6}job /gm)].map((m) => m[1] as string);
  const render = (s: string): string[] => {
    const one = s.replace(/\$\{local\.unitka_signal_window\}/g, '24h30m').replace(/\$\{each\.value\.duration\}/g, '3600s');
    return one.includes('${each.key}') ? heartbeatKeys.map((k) => one.replace(/\$\{each\.key\}/g, k)) : [one];
  };
  it('у всех политик и условий display_name ≤ 100 байт (с подставленными переменными)', () => {
    expect(heartbeatKeys).toHaveLength(4);
    const names: string[] = [];
    for (const f of files) {
      const text = readFileSync(new URL(f, dir), 'utf8');
      for (const m of text.matchAll(/display_name\s*=\s*"((?:[^"\\]|\\.)*)"/g)) names.push(...render(m[1] as string));
      if (f === 'unitka_monitoring.tf') for (const m of text.matchAll(/title\s+=\s+"((?:[^"\\]|\\.)*)"/g)) names.push(m[1] as string);
    }
    expect(names.length).toBeGreaterThan(40);
    expect(names.filter((n) => n.includes('${'))).toEqual([]); // нераскрытая переменная — добавить подстановку в render
    expect(names.filter((n) => Buffer.byteLength(n, 'utf8') > 100)).toEqual([]);
  });
});

describe('пакет включения к 01.11: адреса expected_changes = адреса Terraform', () => {
  const doc = readFileSync(new URL('../../docs/ops/UNITKA_ALERTS_ENABLE_2026-11.md', import.meta.url), 'utf8');
  const expected = /`expected_changes=([^`]+)`/.exec(doc)?.[1] ?? '';
  const addrs = expected.split(',').map((p) => p.split('=')[0]);
  it('ровно три изменения: два update heartbeat LCD и create writers_failed', () => {
    expect(expected.split(',').map((p) => p.split('=')[1])).toEqual(['update', 'update', 'create']);
  });
  it('у ресурса с count адрес с индексом [0], у for_each — с ключом (иначе tf_plan_guard отказывает в apply)', () => {
    for (const a of addrs) {
      const name = /google_monitoring_alert_policy\.([a-z_]+)/.exec(a)?.[1] ?? '';
      const body = block(name);
      if (/\n {2}count\s+=/.test(body)) expect(a).toMatch(/\[0\]$/);
      else if (/\n {2}for_each\s+=/.test(body)) expect(a).toMatch(/\["[a-z_]+"\]$/);
    }
  });
});
