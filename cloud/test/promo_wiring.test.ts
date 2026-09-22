/**
 * Согласованность наблюдателя акций с тем, что реально развёрнуто.
 *
 * Расписание живёт в двух местах — Cloud Scheduler (Terraform) и политика
 * логического периода (slot.ts). Рассогласование не падает: часть слотов просто
 * никогда не наступит, либо два запуска попадут в один слот и второй молча
 * подавится execution-guard'ом. Данные окажутся неполными задним числом —
 * ровно тот класс дефекта, который уже ловит контракт развёртывания Ozon.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { LOADERS } from '../src/loaders/registry.js';
import { PROMO_SLOT_HOURS_UTC } from '../src/loaders/promo/slot.js';

const REPO = join(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const WB_TF = join(REPO, 'infra', 'terraform', 'wb_promo_observer.tf');
const OZON_TF = join(REPO, 'infra', 'terraform', 'ozon_ingestion.tf');

describe('регистрация загрузчика', () => {
  it('promo зарегистрирован и не является prodOnly', () => {
    expect(LOADERS.promo).toBeDefined();
    expect(LOADERS.promo?.prodOnly).toBeUndefined();
  });

  it('логический период promo — слот, а не сутки', () => {
    const at = (iso: string): string => LOADERS.promo!.logicalPeriod(new Date(iso));
    expect(at('2026-09-22T05:00:00Z')).not.toBe(at('2026-09-22T10:00:00Z'));
  });
});

describe('расписание Cloud Scheduler совпадает с политикой слотов', () => {
  it('WB: часы в Terraform равны PROMO_SLOT_HOURS_UTC и часовой пояс — UTC', () => {
    const tf = readFileSync(WB_TF, 'utf8');
    const cron = /schedule\s*=\s*"0 ([0-9,]+) \* \* \*"/.exec(tf);
    expect(cron, 'расписание wb-promo-prod не найдено').not.toBeNull();
    const hours = cron![1]!.split(',').map(Number).sort((a, b) => a - b);
    expect(hours).toEqual([...PROMO_SLOT_HOURS_UTC].sort((a, b) => a - b));
    expect(tf).toMatch(/time_zone\s*=\s*"Etc\/UTC"/);
  });

  it('WB: планировщик создаётся на паузе — снимается только после проверки токена', () => {
    expect(readFileSync(WB_TF, 'utf8')).toMatch(/paused\s*=\s*true/);
  });

  it('WB: job вызывает именно загрузчик promo', () => {
    expect(readFileSync(WB_TF, 'utf8')).toMatch(/args\s*=\s*\["promo"\]/);
  });

  it('Ozon наблюдается в ТЕ ЖЕ моменты времени: 04/09/14/19 UTC = 07/12/17/22 МСК', () => {
    const tf = readFileSync(OZON_TF, 'utf8');
    const block = /"ozon-runtime-promo"\s*=\s*\{[^}]*schedule\s*=\s*"0 ([0-9,]+) \* \* \*"/.exec(tf);
    expect(block, 'job ozon-runtime-promo не найден').not.toBeNull();
    const msk = block![1]!.split(',').map(Number).sort((a, b) => a - b);
    const utcFromMsk = msk.map((h) => (h - 3 + 24) % 24).sort((a, b) => a - b);
    expect(utcFromMsk).toEqual([...PROMO_SLOT_HOURS_UTC].sort((a, b) => a - b));
    expect(tf).toMatch(/time_zone\s*=\s*"Europe\/Moscow"/);
  });
});
