/**
 * Механическая проверка границы записи наблюдателя акций WB.
 *
 * Тест по ИСХОДНОМУ ТЕКСТУ, а не по поведению: он ловит опечатку раньше, чем она
 * доедет до площадки. Список запрещённых путей живёт здесь, а не в src, — иначе
 * тест нашёл бы собственное определение, а наблюдатель содержал бы мутирующий
 * путь в виде константы.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ALLOWED_PATHS } from '../src/loaders/promo/constants.js';

const PROMO_DIR = join(fileURLToPath(new URL('.', import.meta.url)), '..', 'src', 'loaders', 'promo');

/** Мутирующие пути обеих площадок. Ни один не должен встречаться в наблюдателе. */
const DENIED_PATHS: readonly string[] = [
  '/api/v1/calendar/promotions/upload',
  '/api/v2/upload/task',
  '/api/v2/upload/task/club-discount',
  '/v1/actions/products/activate',
  '/v1/actions/products/deactivate',
  '/v1/actions/auto-add/products/update',
  '/v1/actions/auto-add/products/delete',
  '/v1/actions/discounts-task/approve',
  '/v1/actions/discounts-task/decline',
  '/v1/seller-actions/create',
  '/v1/seller-actions/update',
  '/v1/seller-actions/products/add',
  '/v1/actions/hotsales/activate',
  '/v1/actions/hotsales/deactivate',
  '/v1/product/import/prices',
  '/v1/product/update/discount',
  '/v1/pricing-strategy/create',
  '/v1/pricing-strategy/update',
  '/v1/pricing-strategy/products/add',
];

/** Источник ПДн покупателей. В V1 не загружается по решению владельца. */
const PII_SOURCE = '/v1/actions/discounts-task/list';

function promoSources(): Array<{ name: string; text: string }> {
  return readdirSync(PROMO_DIR)
    .filter((f) => f.endsWith('.ts'))
    .map((f) => ({ name: f, text: readFileSync(join(PROMO_DIR, f), 'utf8') }));
}

describe('граница записи наблюдателя акций', () => {
  const files = promoSources();

  it('каталог наблюдателя вообще существует и непуст', () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it('ни один мутирующий путь не встречается в исходниках наблюдателя', () => {
    const hits: string[] = [];
    for (const f of files) {
      for (const p of DENIED_PATHS) {
        if (f.text.includes(p)) hits.push(`${f.name}: ${p}`);
      }
    }
    expect(hits).toEqual([]);
  });

  it('источник ПДн покупателей не упоминается нигде в наблюдателе', () => {
    const hits = files.filter((f) => f.text.includes(PII_SOURCE)).map((f) => f.name);
    expect(hits).toEqual([]);
  });

  it('разрешённый список состоит только из читающих путей календаря', () => {
    expect([...ALLOWED_PATHS].sort()).toEqual([
      '/api/v1/calendar/promotions',
      '/api/v1/calendar/promotions/details',
      '/api/v1/calendar/promotions/nomenclatures',
    ]);
  });

  it('в наблюдателе нет мутирующих HTTP-методов', () => {
    const offenders: string[] = [];
    for (const f of files) {
      for (const m of ["method: 'POST'", "method: 'PUT'", "method: 'PATCH'", "method: 'DELETE'"]) {
        if (f.text.includes(m)) offenders.push(`${f.name}: ${m}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('поля персональных данных покупателя не упоминаются в наблюдателе', () => {
    const piiFields = ['customer_name', 'patronymic', 'user_comment', 'first_name', 'last_name'];
    const hits: string[] = [];
    for (const f of files) {
      for (const p of piiFields) if (f.text.includes(p)) hits.push(`${f.name}: ${p}`);
    }
    expect(hits).toEqual([]);
  });
});
