import { describe, it, expect } from 'vitest';
import { parseTariffNumber, normalizeTariffRows, SENTINELS } from '../src/loaders/tariffs/normalize.js';
import { tariffsObservationId, tariffsLoadJobId, KINDS } from '../src/loaders/tariffs/index.js';
import { LOADERS, resolveLoader } from '../src/loaders/registry.js';
import { LoaderError } from '../src/errors.js';

const CTX = {
  observedAtIso: '2026-09-07T11:00:00.000Z', observationDate: '2026-09-07',
  observationId: 'WBTF_prod_20260907', environment: 'prod', runId: 'r1',
};

/** Реальные строки ответов WB, снятые 07.09.2026. */
const COMM = { parentID: 49, parentName: 'Красота', subjectID: 366, subjectName: 'Тоники',
  kgvpMarketplace: 46, kgvpSupplier: 45, kgvpBooking: 42.5, kgvpPickup: 42.5,
  kgvpSupplierExpress: 3, paidStorageKgvp: 41.5 };
const BOX_CIS = { warehouseName: 'Атакент', geoName: 'СНГ', boxDeliveryBase: '62,1',
  boxDeliveryLiter: '18,9', boxStorageBase: '0,08', boxStorageLiter: '0,08' };
const BOX_RU = { warehouseName: 'Свой склад РФ', geoName: 'Россия', boxDeliveryBase: '-',
  boxDeliveryLiter: '-', boxStorageBase: '-', boxStorageLiter: '-' };

describe('parseTariffNumber: сентинел никогда не становится нулём', () => {
  it('разбирает запятую как десятичный разделитель', () => {
    expect(parseTariffNumber('62,1')).toEqual({ value: 62.1, parsed: true });
    expect(parseTariffNumber('0,08')).toEqual({ value: 0.08, parsed: true });
  });
  it('разбирает пробел как разделитель тысяч', () => {
    expect(parseTariffNumber('1 039')).toEqual({ value: 1039, parsed: true });
  });
  it('сентинелы WB дают null, а НЕ 0', () => {
    for (const s of ['-', '—', 'не принимает', '']) {
      const r = parseTariffNumber(s);
      expect(r.value).toBeNull();
      expect(r.parsed).toBe(false);
      expect(r.value).not.toBe(0);
    }
  });
  it('настоящий ноль остаётся нулём и считается разобранным', () => {
    expect(parseTariffNumber('0')).toEqual({ value: 0, parsed: true });
    expect(parseTariffNumber(0)).toEqual({ value: 0, parsed: true });
  });
  it('числа проходят как есть', () => {
    expect(parseTariffNumber(41.5)).toEqual({ value: 41.5, parsed: true });
  });
  it('мусор не превращается в число', () => {
    expect(parseTariffNumber('абв').value).toBeNull();
  });
  it('список сентинелов закрыт и содержит оба тире', () => {
    expect(SENTINELS).toContain('-');
    expect(SENTINELS).toContain('не принимает');
  });
});

describe('normalizeTariffRows: длинный формат', () => {
  it('комиссия раскладывается на метрики без полей-идентификаторов', () => {
    const rows = normalizeTariffRows('COMMISSION', [COMM], 'GET /api/v1/tariffs/commission', CTX);
    const metrics = rows.map((r) => r.metric).sort();
    expect(metrics).toEqual(['kgvpBooking', 'kgvpMarketplace', 'kgvpPickup', 'kgvpSupplier', 'kgvpSupplierExpress', 'paidStorageKgvp']);
    expect(metrics).not.toContain('subjectID');
    expect(metrics).not.toContain('parentName');
  });

  it('идентификаторы попадают в ключи, а не в метрики', () => {
    const [r] = normalizeTariffRows('COMMISSION', [COMM], 'e', CTX);
    expect(r.entity_key).toBe('366');
    expect(r.entity_name).toBe('Тоники');
    expect(r.parent_key).toBe('49');
    expect(r.parent_name).toBe('Красота');
  });

  it('paidStorageKgvp сохраняется как 41.5 — база нашей комиссии', () => {
    const rows = normalizeTariffRows('COMMISSION', [COMM], 'e', CTX);
    const ps = rows.find((r) => r.metric === 'paidStorageKgvp');
    expect(ps?.value_num).toBe(41.5);
    expect(ps?.is_parsed).toBe(true);
  });

  it('российский склад с прочерками даёт NULL и is_parsed=false, а не нули', () => {
    const rows = normalizeTariffRows('BOX', [BOX_RU], 'e', CTX);
    expect(rows.every((r) => r.value_num === null)).toBe(true);
    expect(rows.every((r) => r.is_parsed === false)).toBe(true);
    expect(rows.every((r) => r.value_raw === '-')).toBe(true);
  });

  it('склад СНГ разбирается полностью', () => {
    const rows = normalizeTariffRows('BOX', [BOX_CIS], 'e', CTX);
    expect(rows.find((r) => r.metric === 'boxDeliveryBase')?.value_num).toBe(62.1);
    expect(rows.every((r) => r.is_parsed)).toBe(true);
  });

  it('новое поле WB автоматически становится метрикой и не теряется', () => {
    const rows = normalizeTariffRows('COMMISSION', [{ ...COMM, kgvpBrandNew: 7 }], 'e', CTX);
    expect(rows.find((r) => r.metric === 'kgvpBrandNew')?.value_num).toBe(7);
  });

  it('сохраняет исходную строку для форензики', () => {
    const [r] = normalizeTariffRows('BOX', [BOX_CIS], 'e', CTX);
    expect(JSON.parse(r.raw_row_json)).toEqual(BOX_CIS);
  });

  it('даты действия переносятся в строки', () => {
    const rows = normalizeTariffRows('BOX', [BOX_CIS], 'e', CTX, { next: '2026-09-15', tillMax: '2026-09-14' });
    expect(rows[0].effective_next).toBe('2026-09-15');
    expect(rows[0].effective_till_max).toBe('2026-09-14');
  });

  it('пустой массив → отказ, а не пустой снимок', () => {
    expect(() => normalizeTariffRows('COMMISSION', [], 'e', CTX)).toThrow(LoaderError);
  });
});

describe('идентификаторы и реестр', () => {
  it('observationId детерминирован и различает сутки', () => {
    expect(tariffsObservationId('prod', '2026-09-07')).toBe('WBTF_prod_20260907');
    expect(tariffsObservationId('prod', '2026-09-08')).not.toBe(tariffsObservationId('prod', '2026-09-07'));
  });
  it('jobId детерминирован внутри суток', () => {
    expect(tariffsLoadJobId('prod', '2026-09-07')).toBe('wbtariffs_prod_20260907');
  });
  it('загрузчик зарегистрирован, период — сутки UTC', () => {
    expect(resolveLoader('tariffs')).toBeDefined();
    expect(LOADERS.tariffs.logicalPeriod(new Date('2026-09-07T23:50:00Z'))).toBe('2026-09-07');
  });
  it('опрашиваются ровно четыре вида тарифа; отключённый acceptance отсутствует', () => {
    expect(KINDS.map((k) => k.kind)).toEqual(['COMMISSION', 'BOX', 'RETURN', 'PALLET']);
    expect(KINDS.some((k) => k.path('2026-09-07').includes('acceptance'))).toBe(false);
  });
  it('все пути — только чтение', () => {
    for (const k of KINDS) expect(k.path('2026-09-07')).toMatch(/^\/api\/(v1\/)?tariffs\//);
  });
});
