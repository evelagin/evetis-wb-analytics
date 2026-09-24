/**
 * РАЗВЯЗКА ПЛАТФОРМ (Gate 10 §13) — структурный контракт.
 *
 * Репетиция на тестовой книге (24.09.2026, настоящий API) дала: A) WB успех + сбой Ozon →
 * B2 = D, B30 = D-1; B) успех Ozon + сбой WB → B30 = D, B2 = D-1; C) оба успешны → каждый
 * независимо. Здесь закреплено то, что делает это верным при любом будущем изменении кода:
 * каждый писатель знает только СВОЙ авторитет.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';

const src = (p: string) => readFileSync(new URL(`../src/loaders/unitka/${p}`, import.meta.url), 'utf8');
const code = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');   // без комментариев

describe('каждый писатель знает только свой LCD', () => {
  it('WB не ссылается на авторитет Ozon ни в одном модуле цикла', () => {
    for (const f of ['index.ts', 'wb_lifecycle.ts', 'plan.ts', 'qa.ts']) {
      expect(code(src(f)), f).not.toMatch(/OZON_LAST_CLOSED_DATE|OZON_LCD|B30/);
    }
  });

  it('Ozon пишет ТОЛЬКО OZON_LAST_CLOSED_DATE; имя WB для него — лишь режим LEGACY (чтение)', () => {
    const life = code(src('ozon/ozon_lifecycle.ts'));
    // единственная запись LCD — в OzonLcdCell, и она адресована собственному имени
    const writes = [...life.matchAll(/batchWrite\(\[\{ range: ([^,]+),/g)].map((m) => m[1]);
    expect(writes).toEqual(['OZON_OWN_LCD_NAME']);
    const loader = code(src('ozon/loader.ts'));
    expect(loader).not.toMatch(/range:\s*'LAST_CLOSED_DATE'/);
    expect(loader).toMatch(/new OzonLcdCell\(sheets\)/);
    expect(loader).not.toMatch(/WbLcdCell/);
  });

  it('коммит Ozon возможен только в собственном цикле: LEGACY-режим не создаёт OzonLcdCell для записи', () => {
    const loader = code(src('ozon/loader.ts'));
    const commitAt = loader.indexOf('commitLcd(cell');
    const guardAt = loader.lastIndexOf('if (cycle)', commitAt);
    expect(commitAt).toBeGreaterThan(0);
    expect(guardAt, 'коммит Ozon обязан стоять под if (cycle) — cycle есть только при авторитете OWN').toBeGreaterThan(0);
  });

  it('WB коммитит только B2 и своё зеркало', () => {
    const life = code(src('wb_lifecycle.ts'));
    expect(life).toMatch(/const WB_LCD_NAME = 'LAST_CLOSED_DATE'/);
    expect(life).not.toMatch(/OZON/);
  });
});
