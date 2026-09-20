/**
 * UNITKA INTEGRITY GUARD V1 — чистые правила (Phase 1C1).
 *
 * Сентябрьские контрольные случаи — это ИЗВЕСТНЫЕ исторические факты Phase 1A (live-проверка
 * 18.09.2026), зашитые как фикстуры регрессии. Продакшн здесь не читается и не пишется.
 * Счётчик СПП (14 на 17.09) НЕ зашит: СПП — ручное поле, владелец может дописать; живой счёт —
 * задача сухого прогона 1C3. Здесь СПП проверяется синтетическими случаями.
 */
import { describe, it, expect } from 'vitest';
import {
  evaluateIntegrity, aggregateStatus, storageSeverity, parseHhMm, parseCogsTerm, summarize, classifyCogsSnapshot, parseBqTimestamp,
  priceRules, sppRules, cogsRules, coverageRules, storageRules, divergenceRules, moscowParts,
  OBSERVED_PRICE_LABEL, ERROR_KEYS_LIMIT, COGS_STALE_THRESHOLD_HOURS, APPROVED_COGS_REFS,
  type IntegrityFactsRow, type CogsCanonicalRow, type CogsSnapshot, type IntegrityInputs, type IntegrityIssue, type IntegritySeverity,
} from '../src/loaders/unitka/integrity.js';
import { OFFSET, addDaysIso, colA1, type Block, type CellValue } from '../src/loaders/unitka/model.js';
import { blockStart, dayRow } from './unitka_fixture.js';

/* ───────────── сентябрьская фикстура (LCD 17.09, «сейчас» 18.09) ───────────── */

const MONTH = '2026-09-01';
const LCD = '2026-09-17';
const DAYS = 17;
const NOW_MORNING = new Date('2026-09-18T07:01:00Z'); // 10:01 МСК — утренний прогон
const NOW_RESERVE = new Date('2026-09-18T09:30:00Z'); // 12:30 МСК — резервный прогон
const DUE = parseHhMm('12:15')!;

const SKU = {
  HAND: 252442517,      // блок 1, $R$45 = 240 (неверно), канон 231.38
  BODY: 252442341,      // COGS 0 в листе, канона нет
  HAND_BODY: 252441968, // COGS 0 в листе, канона нет
  SERUM_ACNE: 305101361,
  HAND_AMBER: 930334396,
  NO_BLOCK: 909951444,  // активен, блока нет
} as const;

/** Порядок блоков в фикстуре (как в live: M, AK, BI, …); 909951444 блока не имеет. */
const BLOCK_NMS = [SKU.HAND, SKU.BODY, SKU.HAND_BODY, SKU.SERUM_ACNE, SKU.HAND_AMBER];
const blocks: Block[] = BLOCK_NMS.map((nm, i) => ({ index: i, slot: i, start: blockStart(i), nmId: nm, title: `${nm} тест` }));
const blockOf = (nm: number): Block => blocks.find((b) => b.nmId === nm)!;

/** Четыре известных случая PRICE_MISSING_WITH_ORDERS (Phase 1A, подтверждены BigQuery и листом). */
const KNOWN_PRICE_CASES: ReadonlyArray<readonly [string, number]> = [
  ['2026-09-08', SKU.SERUM_ACNE],
  ['2026-09-09', SKU.HAND_AMBER],
  ['2026-09-10', SKU.SERUM_ACNE],
  ['2026-09-17', SKU.HAND_AMBER],
];
const OBSERVED: Record<number, number> = { [SKU.SERUM_ACNE]: 805.6, [SKU.HAND_AMBER]: 1120 };

function baseRow(nmId: number, day: string): IntegrityFactsRow {
  return {
    marketplace: 'WB', nmId, internalSku: `SKU-${nmId}`, productName: `Товар ${nmId}`, day, lastClosedDate: LCD,
    ordersUnitka: 0, cancelsUnitka: 0, ordersSource: 'FUNNEL_API', factualOrderPrice: null,
    ordersFunnel: 0, factOrderRows: null, factOrderQty: null, observedPriceDiagnostic: null, observedPriceAt: null,
    storageValue: 0, storageDateCovered: true, priceState: 'MISSING_NO_ACTIVITY', divergenceClass: 'EXACT',
  };
}

function septemberFacts(o: { storage17Covered?: boolean } = {}): IntegrityFactsRow[] {
  const out: IntegrityFactsRow[] = [];
  for (const nm of [...BLOCK_NMS, SKU.NO_BLOCK]) {
    for (let i = 0; i < DAYS; i++) {
      const day = addDaysIso(MONTH, i);
      const r = baseRow(nm, day);
      if (day === '2026-09-17' && !o.storage17Covered) { r.storageDateCovered = false; r.storageValue = null; }
      // Нормальные дни заказов крема для рук (с фактической ценой).
      if (nm === SKU.HAND && (day === '2026-09-16' || day === '2026-09-17')) {
        Object.assign(r, { ordersUnitka: 1, factualOrderPrice: 600, priceState: 'PRESENT', ordersFunnel: 1, factOrderRows: 1, factOrderQty: 1 });
      }
      if (KNOWN_PRICE_CASES.some(([d, n]) => d === day && n === nm)) {
        Object.assign(r, {
          ordersUnitka: 1, priceState: 'MISSING_WITH_ACTIVITY', ordersFunnel: 1, factOrderRows: null, factOrderQty: null,
          divergenceClass: 'ONLY_FUNNEL', observedPriceDiagnostic: OBSERVED[nm], observedPriceAt: `${day}T20:40:00Z`,
        });
      }
      out.push(r);
    }
  }
  return out;
}

function cogsRow(nmId: number, day: string, cogs: number | null, n = cogs === null ? 0 : 1): CogsCanonicalRow {
  return { nmId, internalSku: `SKU-${nmId}`, day, cogsIntervalCount: n, canonicalCogs: cogs };
}

function septemberCogs(): CogsCanonicalRow[] {
  const canon: Record<number, number | null> = {
    [SKU.HAND]: 231.38, [SKU.BODY]: null, [SKU.HAND_BODY]: null,
    [SKU.SERUM_ACNE]: 135.78, [SKU.HAND_AMBER]: 406.651629, [SKU.NO_BLOCK]: 426.74,
  };
  const out: CogsCanonicalRow[] = [];
  for (const nm of [...BLOCK_NMS, SKU.NO_BLOCK]) for (let i = 0; i < DAYS; i++) out.push(cogsRow(nm, addDaysIso(MONTH, i), canon[nm]!));
  return out;
}

/** Свежая физическая копия COGS (публикация 09:50 МСК, «сейчас» 10:01). */
const PUBLISHED_FRESH = '2026-09-18T06:50:00Z';
function fresh(rows: CogsCanonicalRow[], publishedAt = PUBLISHED_FRESH, now = NOW_MORNING): CogsSnapshot {
  return classifyCogsSnapshot({ rows, publishedAt, runId: 'pub-run-1' }, now);
}

/** Формула AI по live-контракту блока; cogs — литерал или '$R$45'. */
function aiFormula(b: Block, row: number, cogs: string, sep: ',' | ';' = ','): string {
  const L = (o: number): string => colA1(b.start + o);
  return `=IF($${L(OFFSET.date)}${row}>LAST_CLOSED_DATE${sep}""${sep}${L(OFFSET.priceMinusComm)}${row}-${L(OFFSET.logistics)}${row}-${L(OFFSET.tax)}${row}-${cogs})`;
}

interface SheetFixture { values: Map<string, CellValue>; formulas: Map<string, CellValue> }

function septemberSheet(): SheetFixture {
  const values = new Map<string, CellValue>();
  const formulas = new Map<string, CellValue>();
  const sheetCogs: Record<number, string> = {
    [SKU.HAND]: '$R$45', [SKU.BODY]: '0', [SKU.HAND_BODY]: '0', [SKU.SERUM_ACNE]: '135.78', [SKU.HAND_AMBER]: '406.65',
  };
  for (const b of blocks) {
    for (let i = 0; i < 30; i++) {
      const row = dayRow(i);
      formulas.set(`${row}|${b.start + OFFSET.unitProfit}`, aiFormula(b, row, sheetCogs[b.nmId]!));
      values.set(`${row}|${b.start + OFFSET.spp}`, 20); // по умолчанию СПП заполнена
    }
  }
  const spp = (nm: number, day: string, v: CellValue | undefined): void => {
    const i = Math.round((Date.parse(day) - Date.parse(MONTH)) / 86_400_000);
    const key = `${dayRow(i)}|${blockOf(nm).start + OFFSET.spp}`;
    if (v === undefined) values.delete(key); else values.set(key, v);
  };
  spp(SKU.HAND_AMBER, '2026-09-17', undefined); // «рваная» строка API: элемента нет → пусто
  spp(SKU.HAND, '2026-09-16', 0);               // явный 0 = заполнено
  return { values, formulas };
}

function inputs(o: Partial<IntegrityInputs> & { sheet?: SheetFixture } = {}): IntegrityInputs {
  const sheet = o.sheet ?? septemberSheet();
  return {
    facts: septemberFacts(), cogs: fresh(septemberCogs()), blocks, lcd: LCD, monthStart: MONTH, firstDailyRow: 737,
    cellAt: (r, c) => sheet.values.get(`${r}|${c}`) ?? null,
    formulaAt: (r, c) => sheet.formulas.get(`${r}|${c}`) ?? null,
    refValues: { R45: 240 }, now: NOW_MORNING, storageDueMinutes: DUE,
    ...o,
  };
}

const byCode = (issues: IntegrityIssue[], code: string): IntegrityIssue[] => issues.filter((i) => i.code === code);

/* ───────────────────────── приёмка сентября ───────────────────────── */

describe('Integrity Guard V1 — сентябрьская приёмка (Phase 1A fixtures)', () => {
  const issues = evaluateIntegrity(inputs());

  it('KNOWN_PRICE_FIXTURES = 4/4: PRICE_MISSING_WITH_ORDERS, ERROR, blocking, financial_invalid; других нет', () => {
    const p = byCode(issues, 'PRICE_MISSING_WITH_ORDERS');
    const key = (d: string | null, n: number | null): string => `${d}/${n}`;
    expect(p.map((i) => key(i.day, i.nmId)).sort()).toEqual(KNOWN_PRICE_CASES.map(([d, n]) => key(d, n)).sort());
    for (const i of p) {
      expect(i).toMatchObject({ severity: 'ERROR', blocking: true, financialInvalid: true, field: 'price' });
      expect(i.dependentFields).toEqual(expect.arrayContaining(['AE', 'AH', 'AI', 'W', 'V', 'Z', 'SUMMARY_I', 'SUMMARY_K']));
    }
  });

  it('наблюдённая цена — ТОЛЬКО diagnostic_value с меткой; в source_value цены нет', () => {
    const i = byCode(issues, 'PRICE_MISSING_WITH_ORDERS').find((x) => x.nmId === SKU.HAND_AMBER && x.day === '2026-09-17')!;
    expect(i.diagnosticValue).toBe(`${OBSERVED_PRICE_LABEL}=1120 @ 2026-09-17T20:40:00Z`);
    expect(i.sourceValue).toContain('price=NULL');
    expect(i.sourceValue).not.toContain('1120');
  });

  it('COGS_252442517 = 240.00 → канон 231.38 → COGS_SOURCE_MISMATCH: ERROR и фин. недействительность дней с заказами (решение владельца, Financial Integrity V1)', () => {
    const m = byCode(issues, 'COGS_SOURCE_MISMATCH');
    expect(m).toHaveLength(1);
    expect(m[0]).toMatchObject({ nmId: SKU.HAND, severity: 'ERROR', blocking: true, financialInvalid: true, day: null });
    expect(m[0]!.invalidDays!.length).toBeGreaterThan(0);
    expect(m[0]!.sourceValue).toContain('$R$45=240');
    expect(m[0]!.sourceValue).toContain('canonical=231.38');
  });

  it('COGS_MISSING_SKUS = 2/2: 252442341, 252441968 → COGS_ZERO_OR_MISSING, WARNING latent (нет активности)', () => {
    const m = byCode(issues, 'COGS_ZERO_OR_MISSING');
    expect(m.map((i) => i.nmId).sort()).toEqual([SKU.HAND_BODY, SKU.BODY].sort());
    for (const i of m) {
      expect(i).toMatchObject({ severity: 'WARNING', blocking: false, financialInvalid: false });
      expect(i.sourceValue).toContain('SHEET_ZERO');
      expect(i.sourceValue).toContain('CANONICAL_MISSING');
      expect(i.diagnosticValue).toContain('latent');
    }
  });

  it('округление литерала (406.65 против 406.651629) — не расхождение; 135.78 = 135.78 — не расхождение', () => {
    const nms = [...byCode(issues, 'COGS_SOURCE_MISMATCH'), ...byCode(issues, 'COGS_ZERO_OR_MISSING')].map((i) => i.nmId);
    expect(nms).not.toContain(SKU.HAND_AMBER);
    expect(nms).not.toContain(SKU.SERUM_ACNE);
  });

  it('SKU_909951444 = SKU_WITHOUT_BLOCK (ERROR, coverage blocking); у SKU с блоками — нет', () => {
    const c = byCode(issues, 'SKU_WITHOUT_BLOCK');
    expect(c).toHaveLength(1);
    expect(c[0]).toMatchObject({ nmId: SKU.NO_BLOCK, severity: 'ERROR', blocking: true, financialInvalid: false, day: null });
  });

  it('SPP: пустая ячейка при Q>0 → MANUAL_REQUIRED; явный 0 и 20 — НЕ пропуск; день без заказов не проверяется', () => {
    const s = byCode(issues, 'SPP_MISSING');
    expect(s.map((i) => [i.day, i.nmId])).toEqual([['2026-09-17', SKU.HAND_AMBER]]);
    expect(s[0]).toMatchObject({ severity: 'MANUAL_REQUIRED', blocking: false, financialInvalid: false });
    expect(s[0]!.dependentFields).not.toContain('W'); // прибыль СПП не затрагивает
  });

  it('хранение D−1 утром → EXPECTED_DELAY; после прихода источника к резервному прогону — issue нет', () => {
    const st = byCode(issues, 'STORAGE_MISSING');
    expect(st).toHaveLength(1);
    expect(st[0]).toMatchObject({ day: '2026-09-17', nmId: null, severity: 'EXPECTED_DELAY' });
    const later = evaluateIntegrity(inputs({ facts: septemberFacts({ storage17Covered: true }), now: NOW_RESERVE }));
    expect(byCode(later, 'STORAGE_MISSING')).toHaveLength(0);
  });

  it('расхождение источников заказов — не ERROR (ONLY_FUNNEL → WARNING)', () => {
    const d = byCode(issues, 'ORDERS_SOURCE_DIVERGENCE');
    expect(d).toHaveLength(4);
    expect(d.every((i) => i.severity === 'WARNING' && !i.blocking && !i.financialInvalid)).toBe(true);
  });

  it('статус сентября = DATA_ERROR; INFO (дни без заказов) не поднимают статус', () => {
    expect(aggregateStatus(issues)).toBe('DATA_ERROR');
    const info = byCode(issues, 'PRICE_MISSING_NO_ORDERS');
    expect(info.length).toBeGreaterThan(50);
    expect(info.every((i) => i.severity === 'INFO' && !i.financialInvalid)).toBe(true);
    expect(aggregateStatus(info)).toBe('PASS');
  });

  it('сводка: 4 финансово недостоверных SKU-дня, error_keys без INFO', () => {
    const s = summarize(issues, 'observe', 'POST_WRITE', NOW_MORNING, fresh(septemberCogs()));
    expect(s.status).toBe('DATA_ERROR');
    const cogsDays = byCode(issues, 'COGS_SOURCE_MISMATCH')[0]!.invalidDays!.length;
    expect(s.financially_invalid_rows).toBe(4 + cogsDays); // 4 цены + дни 252442517 с заказами (COGS ≠ канону)
    expect(s.counts.ERROR).toBe(6); // 4 цены + 1 покрытие + 1 COGS
    expect(s.states).toMatchObject({ DATA_ERROR: 6 });
    expect(s.error_keys).toContain('2026-09-17/930334396/PRICE_MISSING_WITH_ORDERS');
    expect(s.error_keys).toContain('-/909951444/SKU_WITHOUT_BLOCK');
    expect(s.error_keys.some((k) => k.includes('PRICE_MISSING_NO_ORDERS'))).toBe(false);
  });
});

/* ───────────────────────── цена ───────────────────────── */

describe('priceRules', () => {
  const nm = new Set([SKU.HAND]);
  const row = (p: Partial<IntegrityFactsRow>): IntegrityFactsRow => ({ ...baseRow(SKU.HAND, '2026-09-05'), ...p });
  it('Q=0, S=0, price NULL → только INFO', () => {
    const r = priceRules([row({})], nm, LCD);
    expect(r).toHaveLength(1);
    expect(r[0]).toMatchObject({ code: 'PRICE_MISSING_NO_ORDERS', severity: 'INFO', blocking: false });
  });
  it('S>0 при Q=0 и NULL цене → ERROR (формула W использует S×AI)', () => {
    const r = priceRules([row({ cancelsUnitka: 1 })], nm, LCD);
    expect(r[0]).toMatchObject({ code: 'PRICE_MISSING_WITH_ORDERS', severity: 'ERROR', financialInvalid: true });
  });
  it('цена есть → issue нет; день > LCD не рассматривается; SKU без блока — не ценовое правило', () => {
    expect(priceRules([row({ ordersUnitka: 2, factualOrderPrice: 640 })], nm, LCD)).toHaveLength(0);
    expect(priceRules([row({ day: '2026-09-18', ordersUnitka: 1 })], nm, LCD)).toHaveLength(0);
    expect(priceRules([row({ nmId: 1, ordersUnitka: 1 })], nm, LCD)).toHaveLength(0);
  });
  it('без наблюдённой цены diagnostic_value = null (ничего не выдумываем)', () => {
    expect(priceRules([row({ ordersUnitka: 1 })], nm, LCD)[0]!.diagnosticValue).toBeNull();
  });
});

/* ───────────────────────── COGS ───────────────────────── */

describe('parseCogsTerm — контракт формулы AI', () => {
  const b = blocks[0]!; // M..AJ
  const row = 753;
  it('литерал через «,»', () => expect(parseCogsTerm(aiFormula(b, row, '406.65'), b, row)).toMatchObject({ kind: 'literal', value: 406.65 }));
  it('литерал через «;» с десятичной запятой', () => expect(parseCogsTerm(aiFormula(b, row, '406,65', ';'), b, row)).toMatchObject({ kind: 'literal', value: 406.65 }));
  it('разрешённая абсолютная ссылка $R$45', () => expect(parseCogsTerm(aiFormula(b, row, '$R$45'), b, row)).toMatchObject({ kind: 'ref', ref: 'R45' }));
  it('неразрешённая ссылка $R$46 → unrecognised', () => expect(parseCogsTerm(aiFormula(b, row, '$R$46'), b, row).kind).toBe('unrecognised'));
  it('относительная ссылка R45 → unrecognised', () => expect(parseCogsTerm(aiFormula(b, row, 'R45'), b, row).kind).toBe('unrecognised'));
  it('чужая строка → unrecognised', () => expect(parseCogsTerm(aiFormula(b, row + 1, '240'), b, row).kind).toBe('unrecognised'));
  it('чужой блок → unrecognised', () => expect(parseCogsTerm(aiFormula(blocks[1]!, row, '240'), b, row).kind).toBe('unrecognised'));
  it('лишнее слагаемое → unrecognised', () => expect(parseCogsTerm(aiFormula(b, row, '240-10'), b, row).kind).toBe('unrecognised'));
  it('пустая ячейка/значение → unrecognised', () => {
    expect(parseCogsTerm(null, b, row).kind).toBe('unrecognised');
    expect(parseCogsTerm(12.5, b, row).kind).toBe('unrecognised');
  });
  it('десятичная запятая при «,»-разделителе → unrecognised (неоднозначно)', () => {
    expect(parseCogsTerm(aiFormula(b, row, '406,65', ','), b, row).kind).toBe('unrecognised');
  });
});

/**
 * Две эпохи параметра себестоимости: $R$45 — замороженный параметр закрытых месяцев, $S$45 — текущий.
 * Разделение по ячейкам позволяет исправить сентябрь 2026 и дальше, не переписав 20 закрытых месяцев.
 * Белый список остаётся ЯВНЫМ: добавление S45 не открывает строку 45 целиком.
 */
describe('APPROVED_COGS_REFS — ровно две эпохи параметра, всё прочее fail-closed', () => {
  const b = blocks[0]!;
  const row = 753;
  it('белый список — ровно [R45, S45]', () => {
    expect([...APPROVED_COGS_REFS]).toEqual(['R45', 'S45']);
  });
  it('legacy $R$45 разбирается как прежде (закрытые месяцы не затронуты)', () => {
    expect(parseCogsTerm(aiFormula(b, row, '$R$45'), b, row)).toMatchObject({ kind: 'ref', ref: 'R45', text: '$R$45' });
    expect(parseCogsTerm(aiFormula(b, row, '$R$45', ';'), b, row)).toMatchObject({ kind: 'ref', ref: 'R45' });
  });
  it('новый $S$45 разбирается как ссылка', () => {
    expect(parseCogsTerm(aiFormula(b, row, '$S$45'), b, row)).toMatchObject({ kind: 'ref', ref: 'S45', text: '$S$45' });
    expect(parseCogsTerm(aiFormula(b, row, '$S$45', ';'), b, row)).toMatchObject({ kind: 'ref', ref: 'S45' });
  });
  it.each(['$T$45', '$Q$45', '$A$45', '$S$44', '$S$46', '$R$46', '$SS$45', 'S45', '$S45', 'S$45'])(
    'ссылка %s НЕ становится разрешённой из-за добавления S45 → unrecognised',
    (term) => expect(parseCogsTerm(aiFormula(b, row, term), b, row).kind).toBe('unrecognised'));
});

describe('cogsRules — значение берётся из ячейки, на которую ссылается формула AI', () => {
  const handSheet = (cogs: string): SheetFixture => {
    const s = septemberSheet();
    const hb = blockOf(SKU.HAND);
    for (let i = 0; i < 30; i++) s.formulas.set(`${dayRow(i)}|${hb.start + OFFSET.unitProfit}`, aiFormula(hb, dayRow(i), cogs));
    return s;
  };
  const hand = (o: Partial<IntegrityInputs> & { sheet?: SheetFixture }): IntegrityIssue[] =>
    cogsRules(inputs(o)).filter((i) => i.nmId === SKU.HAND);

  it('сегодняшнее production-состояние: AI на $R$45 = 240 при каноне 231,38 → COGS_SOURCE_MISMATCH', () => {
    const r = hand({ sheet: handSheet('$R$45'), refValues: { R45: 240, S45: 231.38 } });
    expect(r[0]).toMatchObject({ code: 'COGS_SOURCE_MISMATCH', severity: 'ERROR', financialInvalid: true });
  });
  it('после миграции: AI на $S$45 = 231,38 при том же каноне → расхождения НЕТ', () => {
    expect(hand({ sheet: handSheet('$S$45'), refValues: { R45: 240, S45: 231.38 } })).toHaveLength(0);
  });
  it('S45 читается по-настоящему: то же $S$45, но в ячейке 240 → расхождение остаётся', () => {
    const r = hand({ sheet: handSheet('$S$45'), refValues: { R45: 240, S45: 240 } });
    expect(r[0]).toMatchObject({ code: 'COGS_SOURCE_MISMATCH' });
    expect(r[0]!.sourceValue).toContain('$S$45=240');
  });
  it('$S$45 пуста → SHEET_BLANK, а не молчаливый ноль (fail-closed до записи параметра)', () => {
    const r = hand({ sheet: handSheet('$S$45'), refValues: { R45: 240, S45: '' } });
    expect(r[0]).toMatchObject({ code: 'COGS_ZERO_OR_MISSING', severity: 'ERROR' });
    expect(r[0]!.sourceValue).toContain('SHEET_BLANK');
  });
  it('неразрешённая ссылка в AI остаётся UNRECOGNISED даже при заполненной ячейке', () => {
    const r = hand({ sheet: handSheet('$T$45'), refValues: { R45: 240, S45: 231.38, T45: 231.38 } });
    expect(r[0]).toMatchObject({ code: 'COGS_ZERO_OR_MISSING' });
    expect(r[0]!.sourceValue).toContain('UNRECOGNISED_FORMULA');
  });
});

describe('cogsRules — варианты', () => {
  const only = (nm: number, o: Partial<IntegrityInputs>): IntegrityIssue[] =>
    cogsRules(inputs(o)).filter((i) => i.nmId === nm);
  const sheetWith = (nm: number, cogs: string): SheetFixture => {
    const s = septemberSheet();
    const b = blockOf(nm);
    for (let i = 0; i < 30; i++) s.formulas.set(`${dayRow(i)}|${b.start + OFFSET.unitProfit}`, aiFormula(b, dayRow(i), cogs));
    return s;
  };
  const cogsWith = (nm: number, v: number | null, n?: number): CogsCanonicalRow[] =>
    septemberCogs().map((c) => (c.nmId === nm ? cogsRow(nm, c.day, v, n) : c));

  it('канон отсутствует → COGS_ZERO_OR_MISSING (CANONICAL_MISSING)', () => {
    const r = only(SKU.SERUM_ACNE, { cogs: fresh(cogsWith(SKU.SERUM_ACNE, null)) });
    expect(r[0]).toMatchObject({ code: 'COGS_ZERO_OR_MISSING' });
    expect(r[0]!.sourceValue).toContain('CANONICAL_MISSING');
  });
  it('канон 0 → COGS_ZERO_OR_MISSING (CANONICAL_ZERO)', () => {
    expect(only(SKU.SERUM_ACNE, { cogs: fresh(cogsWith(SKU.SERUM_ACNE, 0)) })[0]!.sourceValue).toContain('CANONICAL_ZERO');
  });
  it('несколько интервалов → COGS_ZERO_OR_MISSING (CANONICAL_MULTIPLE)', () => {
    expect(only(SKU.SERUM_ACNE, { cogs: fresh(cogsWith(SKU.SERUM_ACNE, null, 2)) })[0]!.sourceValue).toContain('CANONICAL_MULTIPLE');
  });
  it('в листе 0 при активности → ERROR, blocking, financial_invalid, дни активности в diagnostic', () => {
    const r = only(SKU.SERUM_ACNE, { sheet: sheetWith(SKU.SERUM_ACNE, '0') });
    expect(r[0]).toMatchObject({ code: 'COGS_ZERO_OR_MISSING', severity: 'ERROR', blocking: true, financialInvalid: true });
    expect(r[0]!.diagnosticValue).toContain('2026-09-08');
  });
  it('ссылка $R$45 пуста → SHEET_BLANK', () => {
    const r = only(SKU.HAND, { refValues: { R45: '' } });
    expect(r[0]).toMatchObject({ code: 'COGS_ZERO_OR_MISSING', severity: 'ERROR' }); // у HAND есть заказы 16–17.09
    expect(r[0]!.sourceValue).toContain('SHEET_BLANK');
  });
  it('нераспознанная формула → fail-closed в COGS_ZERO_OR_MISSING (UNRECOGNISED_FORMULA), не угадывание', () => {
    const s = septemberSheet();
    const b = blockOf(SKU.SERUM_ACNE);
    s.formulas.set(`${dayRow(3)}|${b.start + OFFSET.unitProfit}`, '=IF(TRUE,1,2)');
    const r = only(SKU.SERUM_ACNE, { sheet: s });
    expect(r[0]!.sourceValue).toContain('UNRECOGNISED_FORMULA');
  });
  it('округление канона до копеек (live: 159.3↔159.295, 182.79↔182.785, 444.38↔444.375) — НЕ расхождение', () => {
    for (const [lit, canon] of [['159.3', 159.295], ['182.79', 182.785], ['444.38', 444.375], ['313.57', 313.565], ['175.27', 175.271629]] as const) {
      expect(only(SKU.SERUM_ACNE, { sheet: sheetWith(SKU.SERUM_ACNE, lit), cogs: fresh(cogsWith(SKU.SERUM_ACNE, canon)) })).toHaveLength(0);
    }
  });
  it('расхождение больше полкопейки ловится (159.31 против 159.295)', () => {
    const r = only(SKU.SERUM_ACNE, { sheet: sheetWith(SKU.SERUM_ACNE, '159.31'), cogs: fresh(cogsWith(SKU.SERUM_ACNE, 159.295)) });
    expect(r[0]).toMatchObject({ code: 'COGS_SOURCE_MISMATCH' });
  });
  it('совпадающий литерал и совпадающая ссылка → issue нет', () => {
    expect(only(SKU.SERUM_ACNE, {})).toHaveLength(0);
    expect(only(SKU.HAND, { refValues: { R45: 231.38 } })).toHaveLength(0);
  });
  it('копия COGS недоступна → только COGS_SNAPSHOT_UNAVAILABLE (WARNING), вердиктов COGS нет; остальные правила работают', () => {
    const all = evaluateIntegrity(inputs({ cogs: classifyCogsSnapshot({ error: 'Not found: Table wb_mart.UNITKA_COGS_EFFECTIVE' }, NOW_MORNING) }));
    const cogsCodes = all.filter((i) => i.code.startsWith('COGS_')).map((i) => i.code);
    expect(cogsCodes).toEqual(['COGS_SNAPSHOT_UNAVAILABLE']);
    expect(byCode(all, 'COGS_SNAPSHOT_UNAVAILABLE')[0]).toMatchObject({ severity: 'WARNING', blocking: false, financialInvalid: false, nmId: null });
    expect(byCode(all, 'PRICE_MISSING_WITH_ORDERS')).toHaveLength(4);
  });
});

/* ───────────────────────── покрытие / СПП / хранение / расхождение ───────────────────────── */

describe('coverageRules', () => {
  it('все активные SKU с блоками → issue нет', () => {
    expect(coverageRules(septemberFacts().filter((r) => r.nmId !== SKU.NO_BLOCK), blocks, LCD)).toHaveLength(0);
  });
});

describe('sppRules — пусто ≠ 0 (рваные строки Sheets API)', () => {
  const b = blocks[0]!;
  const facts = [baseRow(SKU.HAND, '2026-09-01')].map((r) => ({ ...r, ordersUnitka: 1 }));
  const run = (v: CellValue | undefined): IntegrityIssue[] =>
    sppRules({ facts, blocks: [b], lcd: LCD, monthStart: MONTH, firstDailyRow: 737, cellAt: () => v as CellValue });
  it('элемента массива нет (undefined) → пропуск', () => expect(run(undefined)).toHaveLength(1));
  it('null → пропуск', () => expect(run(null)).toHaveLength(1));
  it('пустая строка → пропуск', () => expect(run('')).toHaveLength(1));
  it('числовой 0 → заполнено', () => expect(run(0)).toHaveLength(0));
  it('20 → заполнено', () => expect(run(20)).toHaveLength(0));
  it('Q = 0 → не проверяется даже при пустой ячейке', () => {
    expect(sppRules({ facts: [baseRow(SKU.HAND, '2026-09-01')], blocks: [b], lcd: LCD, monthStart: MONTH, firstDailyRow: 737, cellAt: () => null })).toHaveLength(0);
  });
});

describe('storageSeverity — граница 12:15 МСК', () => {
  const d1 = '2026-09-17';
  it.each([
    ['2026-09-18T09:14:59Z', 'EXPECTED_DELAY'], // 12:14:59 МСК
    ['2026-09-18T09:15:00Z', 'WARNING'],        // 12:15:00 МСК
    ['2026-09-18T09:15:01Z', 'WARNING'],        // 12:15:01 МСК
    ['2026-09-18T07:01:00Z', 'EXPECTED_DELAY'], // утренний прогон 10:01
    ['2026-09-18T09:30:00Z', 'WARNING'],        // резервный прогон 12:30
    ['2026-09-17T21:30:00Z', 'EXPECTED_DELAY'], // 00:30 МСК 18.09 — сутки МСК уже сменились
  ])('D−1 при %s → %s', (iso, want) => {
    expect(storageSeverity(d1, new Date(iso), DUE)).toBe(want);
  });
  it('D−2 → WARNING; старше D−2 → ERROR; сегодня/будущее → null', () => {
    const now = new Date('2026-09-18T07:00:00Z');
    expect(storageSeverity('2026-09-16', now, DUE)).toBe('WARNING');
    expect(storageSeverity('2026-09-15', now, DUE)).toBe('ERROR');
    expect(storageSeverity('2026-09-18', now, DUE)).toBeNull();
  });
  it('moscowParts: UTC 21:00 = 00:00 МСК следующего дня', () => {
    expect(moscowParts(new Date('2026-09-17T21:00:00Z'))).toEqual({ date: '2026-09-18', minutes: 0 });
  });
  it('parseHhMm: мусор → null', () => {
    expect(parseHhMm('12:15')).toBe(735);
    expect(parseHhMm('25:00')).toBeNull();
    expect(parseHhMm('12.15')).toBeNull();
  });
  it('покрытая дата с нулём хранения — легитимно, issue нет', () => {
    const f = [{ ...baseRow(SKU.HAND, '2026-09-10'), storageValue: 0, storageDateCovered: true }];
    expect(storageRules(f, LCD, NOW_MORNING, DUE)).toHaveLength(0);
  });
  it('одна issue на ДАТУ, а не на каждый SKU', () => {
    const f = [SKU.HAND, SKU.BODY].map((nm) => ({ ...baseRow(nm, '2026-09-17'), storageDateCovered: false, storageValue: null }));
    expect(storageRules(f, LCD, NOW_MORNING, DUE)).toHaveLength(1);
  });
});

describe('divergenceRules (D9: не ERROR)', () => {
  const r = (p: Partial<IntegrityFactsRow>): IntegrityIssue[] => divergenceRules([{ ...baseRow(SKU.HAND, '2026-09-05'), ...p }], LCD);
  it('EXACT → нет issue', () => expect(r({})).toHaveLength(0));
  it('|Δ| = 1 → INFO', () => expect(r({ divergenceClass: 'FUNNEL_GT_FACT', ordersFunnel: 2, factOrderQty: 1 })[0]!.severity).toBe('INFO'));
  it('|Δ| = 2 → WARNING', () => expect(r({ divergenceClass: 'FUNNEL_GT_FACT', ordersFunnel: 3, factOrderQty: 1 })[0]!.severity).toBe('WARNING'));
  it('ONLY_FACT → WARNING', () => expect(r({ divergenceClass: 'ONLY_FACT', ordersFunnel: 0, factOrderQty: 1 })[0]!.severity).toBe('WARNING'));
  it('NO_FUNNEL_ROW → INFO', () => expect(r({ divergenceClass: 'NO_FUNNEL_ROW', ordersFunnel: null })[0]!.severity).toBe('INFO'));
});

/* ───────────────────────── статус и журнал ───────────────────────── */

describe('aggregateStatus — детерминированный приоритет', () => {
  const s = (...sev: IntegritySeverity[]) => sev.map((severity) => ({ severity }));
  it.each([
    [s('ERROR', 'MANUAL_REQUIRED'), 'DATA_ERROR'],
    [s('MANUAL_REQUIRED', 'WARNING'), 'MANUAL_REQUIRED'],
    [s('WARNING'), 'PASS_WITH_WARNINGS'],
    [s('INFO'), 'PASS'],
    [s('EXPECTED_DELAY'), 'PASS'],
    [s('INFO', 'EXPECTED_DELAY'), 'PASS'],
    [s(), 'PASS'],
  ])('%j → %s', (issues, want) => expect(aggregateStatus(issues)).toBe(want));
  it('сбой исполнения → SYSTEM_ERROR поверх всего', () => expect(aggregateStatus(s('ERROR'), true)).toBe('SYSTEM_ERROR'));
});

describe('сводка: ограничения', () => {
  it('error_keys ограничены', () => {
    const many: IntegrityIssue[] = Array.from({ length: ERROR_KEYS_LIMIT + 7 }, (_, k) => ({
      marketplace: 'WB', nmId: k, day: '2026-09-01', field: 'price', code: 'PRICE_MISSING_WITH_ORDERS', severity: 'ERROR',
      blocking: true, financialInvalid: true, source: 's', sourceValue: null, diagnosticValue: null, dependentFields: [], message: '',
    }));
    const s = summarize(many, 'observe', 'POST_WRITE', NOW_MORNING, fresh(septemberCogs()));
    expect(s.error_keys).toHaveLength(ERROR_KEYS_LIMIT);
    expect(s.error_keys_truncated).toBe(true);
  });
});

/* ───────────────────────── копия COGS: свежесть (F1/F2, решение D2/D3) ───────────────────────── */

describe('classifyCogsSnapshot — свежесть физической копии', () => {
  const rows = septemberCogs();
  const at = (hoursAgo: number): string => new Date(NOW_MORNING.getTime() - hoursAgo * 3_600_000).toISOString();
  it('штатно (≈1 ч) → AVAILABLE', () => expect(fresh(rows, at(1)).state).toBe('AVAILABLE'));
  it('25,99 ч → ещё AVAILABLE (26 ч — порог безопасности, а не цель)', () => expect(fresh(rows, at(25.99)).state).toBe('AVAILABLE'));
  it(`> ${COGS_STALE_THRESHOLD_HOURS} ч → STALE`, () => expect(fresh(rows, at(26.01)).state).toBe('STALE'));
  it('пустая копия → UNAVAILABLE (а не «канона нет»)', () => expect(fresh([], at(1)).state).toBe('UNAVAILABLE'));
  it('published_at NULL → UNAVAILABLE', () => expect(classifyCogsSnapshot({ rows, publishedAt: null, runId: null }, NOW_MORNING).state).toBe('UNAVAILABLE'));
  it('ошибка чтения → UNAVAILABLE с причиной', () => {
    const s = classifyCogsSnapshot({ error: 'Access Denied' }, NOW_MORNING);
    expect(s).toMatchObject({ state: 'UNAVAILABLE', reason: 'Access Denied' });
  });
});

describe('F2: нет «тихого» пропуска COGS', () => {
  const at = (hoursAgo: number): string => new Date(NOW_MORNING.getTime() - hoursAgo * 3_600_000).toISOString();
  it('устаревшая копия подавляет ВСЕ вердикты COGS, даже известное 240 ↔ 231,38 и нули — только COGS_SNAPSHOT_STALE', () => {
    const all = evaluateIntegrity(inputs({ cogs: fresh(septemberCogs(), at(30)) }));
    const cogsCodes = all.filter((i) => i.code.startsWith('COGS_')).map((i) => i.code);
    expect(cogsCodes).toEqual(['COGS_SNAPSHOT_STALE']);
    expect(byCode(all, 'COGS_SNAPSHOT_STALE')[0]!.severity).toBe('WARNING');
  });
  /** «Чистый» день без единой проблемы, кроме состояния копии. */
  const clean = (cogs: CogsSnapshot): IntegrityInputs => {
    const b = blockOf(SKU.SERUM_ACNE);
    return {
      ...inputs(), cogs, blocks: [b],
      facts: [{ ...baseRow(SKU.SERUM_ACNE, '2026-09-05'), ordersUnitka: 1, factualOrderPrice: 821, priceState: 'PRESENT', ordersFunnel: 1, factOrderRows: 1, factOrderQty: 1 }],
      lcd: '2026-09-05',
      now: new Date('2026-09-18T10:00:00Z'),
    };
  };
  const cogsFor = (hoursAgo: number, now = new Date('2026-09-18T10:00:00Z')): CogsSnapshot =>
    classifyCogsSnapshot({ rows: [cogsRow(SKU.SERUM_ACNE, '2026-09-05', 135.78)], publishedAt: new Date(now.getTime() - hoursAgo * 3_600_000).toISOString(), runId: 'r' }, now);
  const statusOf = (inp: IntegrityInputs): string => aggregateStatus(evaluateIntegrity({ ...inp, monthStart: '2026-09-05' }).filter((i) => i.code !== 'PRICE_MISSING_NO_ORDERS'));
  it('негатив: свежая копия и чистые данные → PASS', () => expect(statusOf(clean(cogsFor(1)))).toBe('PASS'));
  it('позитив: устаревшая копия → PASS_WITH_WARNINGS (никогда не PASS)', () => expect(statusOf(clean(cogsFor(40)))).toBe('PASS_WITH_WARNINGS'));
  it('позитив: недоступная копия → PASS_WITH_WARNINGS (никогда не PASS)', () => {
    expect(statusOf(clean(classifyCogsSnapshot({ error: 'Not found' }, new Date())))).toBe('PASS_WITH_WARNINGS');
  });
  it('копия недоступна при DATA_ERROR — DATA_ERROR остаётся главным', () => {
    expect(aggregateStatus(evaluateIntegrity(inputs({ cogs: classifyCogsSnapshot({ error: 'x' }, NOW_MORNING) })))).toBe('DATA_ERROR');
  });
});

describe('parseBqTimestamp — TIMESTAMP BigQuery всегда UTC', () => {
  it('ISO с Z и вид bq CLI без зоны дают один и тот же момент', () => {
    expect(parseBqTimestamp('2026-09-18 14:18:03')).toBe(Date.parse('2026-09-18T14:18:03Z'));
    expect(parseBqTimestamp('2026-09-18T14:18:03.000Z')).toBe(Date.parse('2026-09-18T14:18:03Z'));
    expect(parseBqTimestamp('2026-09-18T17:18:03+03:00')).toBe(Date.parse('2026-09-18T14:18:03Z'));
  });
  it('мусор/NULL → NaN → копия UNAVAILABLE', () => {
    expect(parseBqTimestamp(null)).toBeNaN();
    expect(classifyCogsSnapshot({ rows: septemberCogs(), publishedAt: 'вчера', runId: null }, NOW_MORNING).state).toBe('UNAVAILABLE');
  });
});
