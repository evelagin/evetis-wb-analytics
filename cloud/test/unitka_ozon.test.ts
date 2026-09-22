import { describe, it, expect } from 'vitest';
import { toLocaleFormula, formulaStyleOf } from '../src/loaders/unitka/formulas.js';
import { OFFSET as WB_OFFSET } from '../src/loaders/unitka/model.js';
import { OZON_OFFSET as OFFSET, OZON_BLOCK_WIDTH } from '../src/loaders/unitka/ozon/offsets.js';
import {
  OZON_GEOMETRY, OZON_SUMMARY, OZON_FIELD_SOURCE_MAP, MANAGEMENT_TAX_RESERVE_RATE, ozonSlotStart,
  TAX_RESERVE_BASE, STOCK_POLICY, CART_POLICY, stockAvailability,
  OZON_DIRECT_COST_GAP, directCostGapTotal, expectedSheetResult,
  OZON_LCD, COMMISSION_INCLUDES_ACQUIRING, otherDirectCostRub, canonicalUnitkaResult,
  APRIL_2026_IS_HYBRID_MIGRATION_MONTH, FIRST_FULLY_CANONICAL_MONTH,
  ozonSectionGeometry, resolveMonthBlocks, storageIsAttributable,
  classifyStockSnapshot, stockIsWritable, isClosedDay,
} from '../src/loaders/unitka/ozon/contract.js';
import {
  OZON_FIELD_ROLES, ROLE_OFFSET, OZON_ROLE_CLASS, OZON_SUMMARY_ROLES,
  OZON_VISUAL_DIVERGENCES, SKU_TITLE_MERGE_WIDTH, roleClassFor, OBSERVATION_DEPENDENT_ROLES,
  OZON_ROW_GEOMETRY, MTD_BAND_COLOUR, OPAQUE_BOOLEAN_ROLES, OZON_CF_FAMILIES,
  OZON_SCALE_TIERS, rowHeightFor, roleOffsetsAlign,
  OZON_STYLE_TEMPLATE, OZON_BORDER_OVERRIDES,
} from '../src/loaders/unitka/ozon/presentation.js';
import {
  blockSpecFor, blockWidthFor, unstyledBlockRoles, staticFormatRequests, columnWidthRequests,
} from '../src/loaders/unitka/ozon/structure.js';
import {
  OZON_UNITKA_SUMMARY_POLICY, unclassifiedSummaryRoles, summaryPolicyMap,
} from '../src/loaders/unitka/ozon/summary.js';
import {
  OZON_REWRITE_WINDOW_DAYS, OZON_REWRITE_EPOCH, OZON_OBSERVED_MAX_VISIBILITY_DAYS,
  OZON_DEEP_RECONCILIATION_DAYS, OZON_DEEP_RECONCILIATION_CADENCE,
  ozonRewriteWindow, ozonDeepWindow, ozonWindowMonths, ozonFromDayFor, isDeepReconciliationDay,
} from '../src/loaders/unitka/ozon/window.js';
import { RECONCILE_WINDOW_DAYS } from '../src/loaders/unitka/reconcile.js';
import {
  parseLiveLayout, assessFreshness, StaleSourceError, OZON_UNITKA_REQUIRED_SOURCES, isoFromSheetValue,
} from '../src/loaders/unitka/ozon/loader.js';
import {
  resolveSections, activateNewSkus, rowsNeeded, NoFreeSkuSlotError,
} from '../src/loaders/unitka/ozon/lifecycle.js';
import { LOADERS } from '../src/loaders/registry.js';
import { loadConfig } from '../src/config.js';
import {
  ozonBlockDayFormulas, ozonSummaryDayFormulas, ozonSummaryMtdFormulas, ozonBlockMtdFormulas,
  OZON_MTD_BLANK_OFFSETS,
} from '../src/loaders/unitka/ozon/formulas.js';

describe('OZON adapter — геометрия', () => {
  it('первый блок L, шаг 25 (WB начинается с M и шагает по 24)', () => {
    expect(OZON_GEOMETRY.BLOCK_FIRST_COLUMN).toBe(12);
    expect(ozonSlotStart(0)).toBe(12);
    expect(ozonSlotStart(15)).toBe(12 + 15 * 25); // 387 — 16-й блок апреля 2026
  });

  it('Gate 6A: блок Ozon шире блока WB ровно на «Прочие прямые»', () => {
    // Ширина берётся из числа смещений, а не из отдельного числа: иначе карта и геометрия
    // могли бы разойтись незаметно. Ширина WB при этом обязана остаться прежней.
    expect(OZON_BLOCK_WIDTH).toBe(25);
    expect(OZON_GEOMETRY.BLOCK_WIDTH).toBe(25);
    expect(OZON_GEOMETRY.BLOCK_BODY_WIDTH).toBe(24);
    expect(Object.keys(WB_OFFSET)).toHaveLength(24);
    expect((WB_OFFSET as Record<string, number>).otherDirect).toBeUndefined();
    expect(OZON_BLOCK_WIDTH - Object.keys(WB_OFFSET).length).toBe(1);
  });

  it('Gate 6A: «Прочие прямые» стоят в расходной части, а не в воронке', () => {
    expect(OFFSET.otherDirect).toBe(21);
    expect(OFFSET.storage).toBe(20);   // слева — Хранение
    expect(OFFSET.tax).toBe(22);       // справа — налоговый резерв
    // всё, что левее вставки, не сдвинулось: воронка и цена остались на своих местах
    for (const k of ['date','bloggers','views','opens','orders','carts','cancels','stock','turnover',
                     'profit1','profitAll','adsIn','adsOut','drr','price','spp','priceSpp',
                     'commission','priceMinusComm','logistics','storage'] as const) {
      expect(OFFSET[k]).toBe((WB_OFFSET as Record<string, number>)[k]);
    }
  });

  it('Gate 6A: «Положили в корзину» сохранена как KPI воронки и не занята расходом', () => {
    expect(OFFSET.carts).toBe(5);
    expect(OFFSET.otherDirect).not.toBe(OFFSET.carts);
    const cart = OZON_FIELD_SOURCE_MAP.find((f) => f.offset === OFFSET.carts)!;
    expect(cart.field).toBe('Положили в корзину');
  });
  it('в сводке Ozon нет ДРР (10 колонок против 11 у WB)', () => {
    expect(Object.keys(OZON_SUMMARY)).toHaveLength(10);
    expect((OZON_SUMMARY as Record<string, number>).drr).toBeUndefined();
  });
});

describe('OZON adapter — налоговый резерв', () => {
  it('2 % и это управленческий резерв, а не ставка УСН', () => {
    expect(MANAGEMENT_TAX_RESERVE_RATE).toBe(0.02);
    const tax = OZON_FIELD_SOURCE_MAP.find((f) => f.offset === OFFSET.tax)!;
    expect(tax.source).toContain('MANAGEMENT_TAX_RESERVE_RATE');
  });
});

describe('OZON adapter — правила доступности', () => {
  it('корзина, остатки, оборачиваемость и хранение остаются пустыми, а не нулевыми', () => {
    const blank = (o: number) => OZON_FIELD_SOURCE_MAP.find((f) => f.offset === o)!.availability;
    expect(blank(OFFSET.carts)).toBe('BLANK_SOURCE_ABSENT');
    expect(blank(OFFSET.stock)).toBe('BLANK_NOT_INGESTED');
    expect(blank(OFFSET.turnover)).toBe('BLANK_NOT_INGESTED');
    // Хранение больше не пустое: type 79 приходит со sku и пишется как факт (Gate 5L/6A).
    expect(blank(OFFSET.storage)).toBe('FACT');
  });
  it('комиссия и логистика — факт, а не легаси-константы 0,396 и 72 ₽', () => {
    const f = (o: number) => OZON_FIELD_SOURCE_MAP.find((x) => x.offset === o)!;
    expect(f(OFFSET.commission).availability).toBe('FACT');
    expect(f(OFFSET.commission).source).toContain('0,396');
    expect(f(OFFSET.logistics).availability).toBe('FACT');
    expect(f(OFFSET.logistics).source).toContain('72');
  });
});

describe('OZON adapter — формулы блока', () => {
  const m = ozonBlockDayFormulas({ start: 12, cogsTerm: '231.38' }, 456);

  it('отмены не штрафуются: экономика только по реализованным единицам', () => {
    const v = m.get(OFFSET.profitAll)!;
    expect(v).toBe('=IF($L456>LAST_CLOSED_DATE,"",(P456-R456)*N(AI456)-N(W456)-N(AF456)+N(X456)-N(AG456))');
    expect(v).not.toContain('$F$50');        // легаси-ячейка «Отмена товара» = 45 ₽
    expect(v).not.toMatch(/R456\s*\*/);       // легаси «− отмены × маржа» отдельным слагаемым
  });

  it('налог считается от ЦЕНЫ ПРОДАВЦА (Z), как в WB, а не от цены покупателя', () => {
    expect(m.get(OFFSET.tax)).toBe('=IF($L456>LAST_CLOSED_DATE,"",IF(N(Z456)=0,"",Z456*2%))');
    expect(m.get(OFFSET.tax)).not.toContain('AB456');   // цена с СПП больше НЕ база
    expect(TAX_RESERVE_BASE).toBe('SELLER_BASE_PRICE');
  });

  it('доходность 1 шт использует канонический COGS, а не строку 50', () => {
    const u = m.get(OFFSET.unitProfit)!;
    expect(u).toBe('=IF($L456>LAST_CLOSED_DATE,"",IF(N(AD456)=0,"",AD456-N(AE456)-N(AH456)-231.38))');
    expect(u).not.toContain('$50');
  });

  it('в ru-локали разделители и десятичная точка переводятся (иначе #ERROR!)', () => {
    const ru = toLocaleFormula(m.get(OFFSET.unitProfit)!, formulaStyleOf('ru_RU'));
    expect(ru).toContain('231,38');
    expect(ru).not.toContain('231.38');
    expect(ru).toContain(';');
  });
});

describe('OZON adapter — сводка', () => {
  const s = ozonSummaryDayFormulas(456, 16);
  it('заказы суммируются по всем 16 блокам с шагом 25', () => {
    expect(s.get(OZON_SUMMARY.orders)).toBe(
      '=IF($B456>LAST_CLOSED_DATE,"",IF(COUNT(FILTER(P456:OA456,MOD(COLUMN(P456:OA456)-COLUMN(P456),25)=0))=0,"",'
      + 'SUM(FILTER(P456:OA456,MOD(COLUMN(P456:OA456)-COLUMN(P456),25)=0))))');
  });
  it('доходность сводки берёт смещение +10 каждого блока', () => {
    expect(s.get(OZON_SUMMARY.profit)).toContain('V456:OG456');
  });
  it('диапазон растёт вместе с числом блоков', () => {
    expect(ozonSummaryDayFormulas(456, 22).get(OZON_SUMMARY.orders)).toContain('P456:TU456');
  });
});

describe('OZON adapter — политика остатков (Gate 4.1, закрыта)', () => {
  it('до 31.08.2026 остаток недоступен, с 31.08 — факт', () => {
    expect(stockAvailability('2026-04-30')).toBe('BLANK_NOT_INGESTED');
    expect(stockAvailability('2026-08-30')).toBe('BLANK_NOT_INGESTED');
    expect(stockAvailability('2026-08-31')).toBe('FACT');
    expect(stockAvailability('2026-09-20')).toBe('FACT');
  });
  it('синтетическая легаси-проекция запрещена', () => {
    expect(STOCK_POLICY.syntheticProjectionAllowed).toBe(false);
    expect(STOCK_POLICY.factualSource).toBe('ozon_raw.RAW_OZON_STOCKS');
  });
});

describe('OZON adapter — корзина', () => {
  it('остаётся недоказанной и НЕ подменяется рекламной атрибуцией', () => {
    expect(CART_POLICY.status).toBe('SOURCE_NOT_PROVEN');
    expect(CART_POLICY.substituteWithAdsCartAdds).toBe(false);
  });
});

describe('OZON adapter — разрыв прямых расходов не может исчезнуть молча', () => {
  it('перечень категорий с зерном и механизмом WB', () => {
    const k = OZON_DIRECT_COST_GAP.map((c) => c.key);
    expect(k).toContain('acquiring');
    expect(k).toContain('cancellation_logistics');
    expect(k).toContain('other_direct');
    const acq = OZON_DIRECT_COST_GAP.find((c) => c.key === 'acquiring')!;
    expect(acq.attribution).toBe('SKU_ATTRIBUTABLE');
    expect(acq.representableWithoutNewColumn).toBe(true);
    expect(acq.wbMechanism).toContain('TOTAL_COMMISSION_RATE');
    const st = OZON_DIRECT_COST_GAP.find((c) => c.key === 'storage_fbo')!;
    expect(st.attribution).toBe('ACCOUNT_ONLY');
    expect(st.belongsInUnitka).toBe(false);
  });
  it('тождество сверки апреля 17-30 сходится с фактическими числами', () => {
    const gap = directCostGapTotal({ acquiring: 412.57, cancellationLogistics: 303.00, otherDirect: 15.00 });
    expect(Number(gap.toFixed(2))).toBe(730.57);
    const r = expectedSheetResult({
      bqContributionAfterAds: -1446.65, acquiring: 412.57, cancellationLogistics: 303.00,
      otherDirect: 15.00, taxReserve: 1287.10,
    });
    expect(Number(r.toFixed(2))).toBe(-2003.18);
  });
});

describe('OZON adapter — строка MTD', () => {
  const G = { firstDailyRow: 434, lastDailyRow: 463, mtdRow: 464 };
  it('сводка месяца считается по ВСЕМУ месяцу динамически, без хардкода', () => {
    expect(ozonSummaryMtdFormulas(G).get(OZON_SUMMARY.orders))
      .toBe('=IF(COUNT(F434:F463)=0,"",SUM(F434:F463))');
    expect(ozonSummaryMtdFormulas(G).get(OZON_SUMMARY.profit))
      .toBe('=IF(COUNT(I434:I463)=0,"",SUM(I434:I463))');
  });
  it('легаси-вычет отмен 45 ₽ в итог месяца не переносится', () => {
    for (const f of ozonSummaryMtdFormulas(G).values()) {
      expect(f).not.toContain('$F$50');
      expect(f).not.toMatch(/\*\s*45\b/);
    }
  });
  it('MTD блока суммирует только закрытые дни', () => {
    const b = ozonBlockMtdFormulas(12, G);
    expect(b.get(OFFSET.orders))
      .toBe('=IF(COUNT(P434:P463)=0,"",SUMIF($L$434:$L$463,"<="&LAST_CLOSED_DATE,P434:P463))');
    expect(b.get(OFFSET.profit1)).toBe('=IFERROR(V464/(P464-R464),"")');
  });
  // Gate 8: строка итога перестала быть наполовину пустой. Остаток — запас, а не поток,
  // поэтому у него последнее наблюдение, а не сумма; хранение — абсолютная сумма суток.
  it('Gate 8: остаток берётся последним наблюдением, хранение суммируется', () => {
    const b = ozonBlockMtdFormulas(12, G);
    expect(b.get(OFFSET.stock)).toContain('FILTER(');
    expect(b.get(OFFSET.stock)).not.toContain('SUMIF');
    expect(b.get(OFFSET.storage)).toContain('SUMIF');
    expect(b.get(OFFSET.turnover)).toContain('/(');
    // пустыми остаются только подписи: у периода нет одной даты и одного дня недели
    expect([...OZON_MTD_BLANK_OFFSETS].sort((x, y) => x - y))
      .toEqual([OFFSET.date, OFFSET.weekday].sort((x, y) => x - y));
  });
});

describe('OZON adapter — Gate 5A: геометрия мая (31 день)', () => {
  const G = { firstDailyRow: 468, lastDailyRow: 498, mtdRow: 499 };
  it('21 блок: последний начинается в колонке 512, сводка тянется до него', () => {
    expect(ozonSlotStart(20)).toBe(512);
    expect(ozonSummaryDayFormulas(468, 21).get(OZON_SUMMARY.orders)).toContain('P468:SV468');
  });
  it('MTD мая охватывает все 31 сутки', () => {
    expect(ozonSummaryMtdFormulas(G).get(OZON_SUMMARY.orders))
      .toBe('=IF(COUNT(F468:F498)=0,"",SUM(F468:F498))');
  });
});

describe('OZON adapter — Gate 5A: зеркало LAST_CLOSED_DATE', () => {
  it('обновляется формулой от канонического источника, ищется по именованному диапазону', () => {
    expect(OZON_LCD.formula).toBe('=LAST_CLOSED_DATE');
    expect(OZON_LCD.namedRange).toBe('OZON_LCD_MIRROR');
    expect(OZON_LCD.source).toContain('ZZ_CONFIG');
    expect(OZON_LCD.resolveByNamedRange).toBe(true);
  });
  it('защита будущего дня в формулах опирается на LAST_CLOSED_DATE', () => {
    for (const f of ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 468).values()) {
      expect(f.startsWith('=IF($L468>LAST_CLOSED_DATE,"",')).toBe(true);
    }
  });
});

describe('OZON adapter — Gate 5A: комиссия, эквайринг, прочие прямые', () => {
  it('комиссия включает эквайринг (концепция WB, ставка своя)', () => {
    expect(COMMISSION_INCLUDES_ACQUIRING).toBe(true);
  });
  it('OTHER_DIRECT складывается из трёх доказанных слагаемых', () => {
    expect(otherDirectCostRub({ otherDirectFees: 30, logisticsUnrepresented: 1321.09, acquiringUnrepresented: 325.87 }))
      .toBeCloseTo(1676.96, 2);
  });
  it('эквайринг учитывается ровно один раз: в комиссии ЛИБО в прочих', () => {
    const acquiringTotal = 665.88, inCommission = 340.01, inOther = 325.87;
    expect(inCommission + inOther).toBeCloseTo(acquiringTotal, 2);
    // если выручки нет — эквайринг уходит в прочие, а не теряется
    expect(otherDirectCostRub({ otherDirectFees: 0, logisticsUnrepresented: 0, acquiringUnrepresented: inOther }))
      .toBeCloseTo(inOther, 2);
  });
  it('канонический результат мая сходится на фактических числах', () => {
    const r = canonicalUnitkaResult({
      sellerRevenue: 93801, cogs: 19546.60, commissionInclAcquiring: 38458.41 + 340.01,
      representedLogistics: 7974.62, otherDirectCost: 1676.96, internalAds: 25900.35, taxReserve: 1876.02,
    });
    expect(Number(r.toFixed(2))).toBe(-1971.97);
  });
  it('отмены дают реальную логистику, но не искусственный штраф', () => {
    const v = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 468).get(OFFSET.profitAll)!;
    // Gate 6A: величина прочих прямых больше НЕ вшивается литералом в формулу — она лежит
    // в своей колонке (AG) и вычитается ссылкой. Расход отражён ровно один раз и стал видимым.
    expect(v).toContain('-N(AG468)');         // реальный расход отражён ссылкой на колонку
    expect(v).not.toContain('135.5');         // литерала в формуле больше нет
    expect(v).not.toContain('$F$50');         // легаси-штраф 45 ₽ отсутствует
    expect(v).toContain('(P468-R468)');       // экономика только по реализованным единицам
  });
  it('формула прибыли не зависит от величины прочих прямых', () => {
    // Раньше нулевой расход требовал отдельной ветки, иначе в формулу попадал «-0».
    // Со ссылкой на колонку формула одна и та же при любом значении — ветки больше нет.
    const v = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 468).get(OFFSET.profitAll)!;
    expect(v).toBe('=IF($L468>LAST_CLOSED_DATE,"",(P468-R468)*N(AI468)-N(W468)-N(AF468)+N(X468)-N(AG468))');
  });
});

describe('OZON adapter — Gate 5A: ноль против пустого', () => {
  const G = { firstDailyRow: 468, lastDailyRow: 498, mtdRow: 499 };
  it('метрика без единого наблюдения даёт пусто, а не 0 — и в дне, и в итоге', () => {
    expect(ozonSummaryDayFormulas(468, 21).get(OZON_SUMMARY.carts)).toContain('IF(COUNT(');
    expect(ozonSummaryMtdFormulas(G).get(OZON_SUMMARY.carts)).toContain('IF(COUNT(');
    expect(ozonBlockMtdFormulas(12, G).get(OFFSET.carts)).toContain('IF(COUNT(');
  });
});

describe('OZON adapter — Gate 5A: апрель как гибридный месяц миграции', () => {
  it('апрель помечен гибридным, первый канонический месяц — май', () => {
    expect(APRIL_2026_IS_HYBRID_MIGRATION_MONTH).toBe(true);
    expect(FIRST_FULLY_CANONICAL_MONTH).toBe('2026-05');
  });
});

describe('OZON adapter — Gate 5B: геометрия произвольного месяца', () => {
  const MAY21 = Array.from({ length: 21 }, (_, i) => `sku${i}`);
  const B22 = [...MAY21, '1083392113'];
  it('воспроизводит ПРИНЯТУЮ майскую секцию (контроль движка)', () => {
    const may = ozonSectionGeometry(2026, 5, 432, 30, MAY21);
    expect([may.titleRow, may.headerRow, may.firstDailyRow, may.lastDailyRow, may.mtdRow])
      .toEqual([466, 467, 468, 498, 499]);
    expect(may.daysInMonth).toBe(31);
  });
  it('июнь — 30 дней', () => {
    const jun = ozonSectionGeometry(2026, 6, 466, 31, MAY21);
    expect(jun.daysInMonth).toBe(30);
    expect([jun.titleRow, jun.firstDailyRow, jun.lastDailyRow, jun.mtdRow]).toEqual([501, 503, 532, 533]);
    expect(jun.lastColumn).toBe(536);
  });
  it('июль — 31 день и 22 блока', () => {
    const jul = ozonSectionGeometry(2026, 7, 501, 30, B22);
    expect(jul.daysInMonth).toBe(31);
    expect([jul.titleRow, jul.firstDailyRow, jul.lastDailyRow, jul.mtdRow]).toEqual([535, 537, 567, 568]);
    expect(jul.lastColumn).toBe(561);
  });
  it('август — 31 день', () => {
    const aug = ozonSectionGeometry(2026, 8, 535, 31, B22);
    expect(aug.daysInMonth).toBe(31);
    expect([aug.titleRow, aug.firstDailyRow, aug.lastDailyRow, aug.mtdRow]).toEqual([570, 572, 602, 603]);
  });
  it('високосный февраль считается календарём, а не константой', () => {
    expect(ozonSectionGeometry(2028, 2, 100, 31, MAY21).daysInMonth).toBe(29);
    expect(ozonSectionGeometry(2026, 2, 100, 31, MAY21).daysInMonth).toBe(28);
  });
});

describe('OZON adapter — Gate 5B: жизненный цикл SKU', () => {
  const MAY21 = Array.from({ length: 21 }, (_, i) => `sku${i}`);
  const FA = { '1083392113': '2026-07-29' };
  it('1083392113 не существует в июне и появляется в июле', () => {
    expect(resolveMonthBlocks(MAY21, FA, '2026-06').added).toEqual([]);
    expect(resolveMonthBlocks(MAY21, FA, '2026-06').blocks).toHaveLength(21);
    const jul = resolveMonthBlocks(MAY21, FA, '2026-07');
    expect(jul.added).toEqual(['1083392113']);
    expect(jul.blocks).toHaveLength(22);
  });
  it('появившийся SKU не переписывает более ранние месяцы', () => {
    const jul = resolveMonthBlocks(MAY21, FA, '2026-07');
    const aug = resolveMonthBlocks(jul.blocks, FA, '2026-08');
    expect(aug.added).toEqual([]);          // повторно не добавляется
    expect(aug.blocks).toHaveLength(22);
    expect(resolveMonthBlocks(MAY21, FA, '2026-06').blocks).not.toContain('1083392113');
  });
});

describe('OZON adapter — Gate 5B: хранение и граница остатка', () => {
  it('хранение атрибутируемо только для type 79 с sku', () => {
    expect(storageIsAttributable(79, true)).toBe(true);
    expect(storageIsAttributable(79, false)).toBe(false);   // без sku — расход магазина
    expect(storageIsAttributable(46, true)).toBe(false);    // FBO-хранение приходит без sku
  });
  it('остаток пуст по всему августу: политика заканчивается 30.08, а 31.08 требует доказанной семантики', () => {
    for (const d of ['2026-08-01', '2026-08-15', '2026-08-30']) {
      expect(stockAvailability(d)).toBe('BLANK_NOT_INGESTED');
    }
    expect(stockAvailability('2026-08-31')).toBe('FACT');   // политика допускает
    expect(STOCK_POLICY.factualFrom).toBe('2026-08-31');
    expect(STOCK_POLICY.syntheticProjectionAllowed).toBe(false);
  });
});

describe('OZON adapter — Gate 5V: контракт представления', () => {
  it('роли покрывают все 25 смещений блока ровно один раз', () => {
    expect(OZON_FIELD_ROLES).toHaveLength(25);
    const offs = OZON_FIELD_ROLES.map((r) => ROLE_OFFSET[r]).sort((a, b) => a - b);
    expect(offs).toEqual(Array.from({ length: 25 }, (_, i) => i));
  });
  it('каждой роли назначен визуальный класс из палитры WB', () => {
    const allowed = new Set(['MANUAL', 'FACT', 'TARIFF', 'CALC']);
    for (const r of OZON_FIELD_ROLES) expect(allowed.has(OZON_ROLE_CLASS[r])).toBe(true);
  });
  it('поля ручного ввода отличимы от расчётных и от фактов', () => {
    expect(OZON_ROLE_CLASS.MANUAL_EXTERNAL).toBe('MANUAL');
    expect(OZON_ROLE_CLASS.EXTERNAL_ADS).toBe('MANUAL');
    expect(OZON_ROLE_CLASS.ORDERS).toBe('FACT');
    expect(OZON_ROLE_CLASS.TOTAL_PROFIT).toBe('CALC');
  });
  it('недоступные метрики НЕ окрашены как ожидаемый факт (иначе пусто читалось бы как ноль)', () => {
    expect(OZON_ROLE_CLASS.CART).toBe('CALC');
    expect(OZON_ROLE_CLASS.STOCK).toBe('CALC');
    expect(OZON_ROLE_CLASS.TURNOVER).toBe('CALC');
  });
  it('каждое расхождение с WB объявлено и обосновано семантикой', () => {
    for (const d of OZON_VISUAL_DIVERGENCES) {
      expect(OZON_ROLE_CLASS[d.role]).toBe(d.ozon);
      expect(d.wb).not.toBe(d.ozon);
      expect(d.why.length).toBeGreaterThan(20);
    }
    const declared = new Set(OZON_VISUAL_DIVERGENCES.map((d) => d.role));
    // цена, СПП, комиссия и логистика у Ozon — факты, а не ручной ввод/тариф как в WB
    for (const r of ['SELLER_PRICE', 'DISCOUNT', 'COMMISSION', 'LOGISTICS'] as const) {
      expect(declared.has(r)).toBe(true);
      expect(OZON_ROLE_CLASS[r]).toBe('FACT');
    }
  });
  it('в сводке Ozon 10 ролей и нет ДРР (у WB — 11 с ДРР)', () => {
    expect(OZON_SUMMARY_ROLES).toHaveLength(10);
    expect(OZON_SUMMARY_ROLES.map((s) => s.role)).not.toContain('DRR');
  });
  it('объединение заголовка SKU — 7 колонок, как в WB', () => {
    expect(SKU_TITLE_MERGE_WIDTH).toBe(7);
  });
});

describe('OZON adapter — Gate 5C: открытый месяц', () => {
  const B22 = Array.from({ length: 22 }, (_, i) => `sku${i}`);
  const LCD = '2026-09-19';
  it('сентябрь — 30 дней, выводится из августа тем же движком', () => {
    const sep = ozonSectionGeometry(2026, 9, 570, 31, B22);
    expect(sep.daysInMonth).toBe(30);
    expect([sep.titleRow, sep.headerRow, sep.firstDailyRow, sep.lastDailyRow, sep.mtdRow])
      .toEqual([605, 606, 607, 636, 637]);
  });
  it('LCD делит месяц: 19 закрытых суток и 11 будущих', () => {
    const days = Array.from({ length: 30 }, (_, i) => `2026-09-${String(i + 1).padStart(2, '0')}`);
    expect(days.filter((d) => isClosedDay(d, LCD))).toHaveLength(19);
    expect(days.filter((d) => !isClosedDay(d, LCD))).toHaveLength(11);
    expect(isClosedDay('2026-09-19', LCD)).toBe(true);
    expect(isClosedDay('2026-09-20', LCD)).toBe(false);
  });
  it('формулы будущего дня существуют, но под защитой LCD', () => {
    for (const f of ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 630).values()) {
      expect(f.startsWith('=IF($L630>LAST_CLOSED_DATE,"",')).toBe(true);
    }
  });
});

describe('OZON adapter — Gate 9: единицы в пути несут свою экономику', () => {
  it('формула прибыли — на ОЖИДАЕМО реализованных единицах, без терма «в пути»', () => {
    const v = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 468).get(OFFSET.profitAll)!;
    expect(v).toBe('=IF($L468>LAST_CLOSED_DATE,"",(P468-R468)*N(AI468)-N(W468)-N(AF468)+N(X468)-N(AG468))');
  });

  it('терма «в пути» в формулах больше нет ни в открытом, ни в закрытом месяце', () => {
    for (const row of [468, 619]) {
      const m = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, row);
      expect(m.get(OFFSET.profitAll)).toContain(`(P${row}-R${row})`);
      expect(m.get(OFFSET.profitAll)).not.toMatch(new RegExp(`P${row}-R${row}-`));
      expect(m.get(OFFSET.profit1)).toContain(`/(P${row}-R${row})`);
    }
  });

  it('заказ в пути даёт полную провизорную экономику, а не пустые ячейки', () => {
    // всё в пути: доставленных нет, но заказ известен — цена, комиссия и логистика обязаны быть
    const c = composeMonth(SEP, [{ d: '2026-09-01', offer_id: 'A', gross_qty: 2, cancelled_qty: 0,
      realized_qty: 0, in_transit_qty: 2, revenue: 0,
      expected_realized_qty: 2, provisional_revenue_rub: 2000, provisional_cogs_rub: 300,
      commission: 1040, logistics: 170 }]);
    const cell = c.cells['A|607'] as { price?: number; comm?: number; log?: number };
    expect(cell.price).toBe(1000);            // 2000 / 2 ожидаемых единицы
    expect(cell.comm).toBeCloseTo(0.52, 6);   // 1040 / 2000
    expect(cell.log).toBe(85);                // 170 / 2
    expect(c.cogs['A|607']).toBe(150);        // себестоимость единицы в пути не теряется
  });

  // ── Gate 9B: граница типов BigQuery ───────────────────────────────────────────
  // Первый боевой прогон выявил то, чего не видел ни один прежний тест: клиент BigQuery
  // отдаёт NUMERIC объектом Big, а не числом. Тесты кормили сборщик числами, harness
  // приводил типы сам — и обе проверки проходили мимо. Здесь источник ведёт себя ровно
  // как в production: `Big` — объект, истинный даже в нуле, и склеивающийся под `+`.
  const big = (x: string) => ({ toString: () => x }) as unknown as number;

  it('NUMERIC из BigQuery не создаёт выдуманных нулей и не склеивает суммы', () => {
    const raw = {
      d: { value: '2026-09-01' }, offer_id: 'A',
      gross_qty: 2, cancelled_qty: 0, realized_qty: 2,
      expected_realized_qty: 2,
      provisional_revenue_rub: big('2000'), revenue: big('2000'),
      provisional_cogs_rub: big('300'),
      commission: big('1000'), acquiring: big('40'),   // склейка дала бы "100040"/2000
      logistics: big('170'),
      storage: big('0'), other_direct: big('0'), promo: big('0'),
      commission_estimated_rub: big('0'), logistics_estimated_rub: big('0'),
    };
    const row = normalizeBqRow<Parameters<typeof composeMonth>[1][number]>(raw);

    expect(row.d).toBe('2026-09-01');                   // {value} развёрнут
    expect(typeof row.storage).toBe('number');
    expect(row.storage).toBe(0);

    const c = composeMonth(SEP, [row]);
    const cell = c.cells['A|607'] as { comm?: number; stor?: number; od?: number; price?: number };
    expect(cell.price).toBe(1000);
    // (1000 + 40) / 2000 — сумма, а не конкатенация
    expect(cell.comm).toBeCloseTo(0.52, 8);
    // ноль расхода — это ОТСУТСТВИЕ величины в ячейке, а не число 0 в листе
    expect(cell.stor).toBeUndefined();
    expect(cell.od).toBeUndefined();
    // на нулевой оценке пометки «ОЦЕНКА» быть не должно
    expect(c.provenance).toHaveLength(0);
  });

  it('оформление пишется ПОСЛЕ величин, структура — ДО', () => {
    // values.batchUpdate сбрасывает numberFormat записанной ячейки. Пока оформление шло
    // первым, боевой прогон стирал формат «₽» ровно на своём окне: день 6 сохранял формат,
    // день 7 (первый день окна) терял. Поэтому оформление обязано быть последним словом.
    const i = (p: string): number => OZON_WRITE_PHASES.indexOf(p as never);
    expect(i('structure')).toBeLessThan(i('values'));      // без строк величины писать некуда
    expect(i('values')).toBeLessThan(i('presentation'));   // формат ставится после величин
    expect([...OZON_WRITE_PHASES].sort()).toEqual(
      ['conditional', 'presentation', 'structure', 'values']);
  });

  it('формулы Ozon пишутся как формулы, а не как текст; WB остаётся на RAW', () => {
    const rows = [{ range: "OZON!A1:B1", values: [[42, '=IF(A1>LAST_CLOSED_DATE;"";A1*2%)']] }];
    // Ozon: без USER_ENTERED формула легла бы в лист строкой «=IF(…)» — ячейка «налог»
    // выглядела бы пустой, хотя цена известна. Это и случилось в первом боевом прогоне.
    expect(batchWriteBody(rows, 'USER_ENTERED').valueInputOption).toBe('USER_ENTERED');
    // WB пишет ТОЛЬКО величины, формулы там ставит monthprep: режим по умолчанию не меняется.
    expect(batchWriteBody(rows, 'RAW').valueInputOption).toBe('RAW');
    // сами величины режим не искажает
    expect(batchWriteBody(rows, 'USER_ENTERED').data[0]?.values[0]?.[0]).toBe(42);
  });

  it('ненулевой NUMERIC проходит границу без потерь', () => {
    const row = normalizeBqRow<Parameters<typeof composeMonth>[1][number]>({
      d: '2026-09-01', offer_id: 'A', gross_qty: 1, cancelled_qty: 0, realized_qty: 1,
      expected_realized_qty: 1, provisional_revenue_rub: big('1200'),
      commission: big('624'), logistics: big('86'),
      storage: big('6.04'), other_direct: big('2.5'), promo: big('0'),
    });
    const c = composeMonth(SEP, [row]);
    const cell = c.cells['A|607'] as { stor?: number; od?: number; log?: number };
    expect(cell.stor).toBe(6.04);
    expect(cell.od).toBe(2.5);
    expect(cell.log).toBe(86);
  });

  it('СПП и цена покупателя до доставки НЕ выдумываются, но цену продавца не обнуляют', () => {
    const c = composeMonth(SEP, [{ d: '2026-09-01', offer_id: 'A', gross_qty: 1, cancelled_qty: 0,
      realized_qty: 0, in_transit_qty: 1, revenue: 0,
      expected_realized_qty: 1, provisional_revenue_rub: 1200, provisional_cogs_rub: 100 }]);
    const cell = c.cells['A|607'] as { price?: number; spp?: number };
    expect(cell.price).toBe(1200);
    expect(cell.spp).toBeUndefined();         // источника нет — ячейка пуста, а не 0
  });

  it('полностью отменённые сутки экономики не получают', () => {
    const c = composeMonth(SEP, [{ d: '2026-09-01', offer_id: 'A', gross_qty: 2, cancelled_qty: 2,
      realized_qty: 0, in_transit_qty: 0, revenue: 0,
      expected_realized_qty: 0, provisional_revenue_rub: 0 }]);
    const cell = c.cells['A|607'] as { price?: number; comm?: number };
    expect(cell.price).toBeUndefined();
    expect(cell.comm).toBeUndefined();
  });

  it('созревшие сутки не двигаются: без единиц в пути база прежняя', () => {
    const fact = { d: '2026-09-01', offer_id: 'A', gross_qty: 3, cancelled_qty: 1,
      realized_qty: 2, in_transit_qty: 0, revenue: 2400, commission: 1000, logistics: 160 };
    const legacy = composeMonth(SEP, [fact]);
    const gate9 = composeMonth(SEP, [{ ...fact, expected_realized_qty: 2, provisional_revenue_rub: 2400 }]);
    expect(gate9.cells['A|607']).toEqual(legacy.cells['A|607']);
    expect(gate9.totals.revenue).toBe(legacy.totals.revenue);
  });
});

describe('OZON adapter — Gate 5C: доказанность снимка остатков', () => {
  it('снимок доказателен только при съёме в тот же день', () => {
    expect(classifyStockSnapshot('2026-09-19', '2026-09-19T16:01:13Z')).toBe('PROVEN_SNAPSHOT');
    expect(classifyStockSnapshot('2026-08-31', '2026-09-01T11:52:55Z')).toBe('UNPROVEN_SNAPSHOT');
    expect(classifyStockSnapshot('2026-09-01', null)).toBe('NO_SNAPSHOT');
    expect(classifyStockSnapshot(null, '2026-09-01T11:52:55Z')).toBe('NO_SNAPSHOT');
  });
  it('31.08 остаётся пустым без новой доказательной базы', () => {
    expect(stockIsWritable('2026-08-31', '2026-09-01T11:52:55Z', '2026-09-19')).toBe(false);
  });
  it('доказанный снимок закрытых суток пишется, будущих — нет', () => {
    expect(stockIsWritable('2026-09-19', '2026-09-19T16:01:13Z', '2026-09-19')).toBe(true);
    expect(stockIsWritable('2026-09-20', '2026-09-20T16:00:54Z', '2026-09-19')).toBe(false);
  });
});

describe('OZON adapter — Gate 5C: остаток и оборачиваемость', () => {
  it('наблюдаемый остаток выглядит как ФАКТ, неизвестный — как раньше', () => {
    expect(OBSERVATION_DEPENDENT_ROLES).toContain('STOCK');
    expect(roleClassFor('STOCK', true)).toBe('FACT');
    expect(roleClassFor('STOCK', false)).toBe('CALC');
    expect(roleClassFor('ORDERS', false)).toBe('FACT');   // роль без зависимости от наблюдения
  });
  it('оборачиваемость пуста без остатка или без знаменателя — но не ноль', () => {
    const t = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 610).get(OFFSET.turnover)!;
    expect(t).toBe('=IF($L610>LAST_CLOSED_DATE,"",IF(OR(P610=0,S610=""),"",S610/P610))');
    expect(t).toContain('S610=""');   // нет доказанного остатка -> пусто
    expect(t).toContain('P610=0');    // нет знаменателя -> пусто
  });
});

describe('GATE 5D — геометрия строк и язык цвета', () => {
  it('высота строки выводится из структурной роли, а не из месяца', () => {
    expect(rowHeightFor('title')).toBe(40);
    expect(rowHeightFor('header')).toBe(108);
    expect(rowHeightFor('day')).toBe(25);
    expect(rowHeightFor('mtd')).toBe(33);
    expect(rowHeightFor('spacer')).toBe(18);
  });

  it('высоты подчинены правилу «≈2,05 × кегль» живого WB', () => {
    // заголовок 20 пт, итог 16 пт, день 12 пт — те же значения хранит Октябрь 2026 у WB
    expect(OZON_ROW_GEOMETRY.title / 20).toBeCloseTo(2.0, 1);
    expect(OZON_ROW_GEOMETRY.mtd / 16).toBeCloseTo(2.06, 1);
    expect(OZON_ROW_GEOMETRY.day / 12).toBeCloseTo(2.08, 1);
    // шапка — четыре строки переноса
    expect(OZON_ROW_GEOMETRY.header).toBeGreaterThanOrEqual(4 * OZON_ROW_GEOMETRY.day);
  });

  it('строка дня выше прежних 18 px: иначе Sheets режет и суммы, и заголовок', () => {
    expect(OZON_ROW_GEOMETRY.day).toBeGreaterThan(18);
    expect(OZON_ROW_GEOMETRY.title).toBeGreaterThan(18);
    expect(OZON_ROW_GEOMETRY.mtd).toBeGreaterThan(18);
  });

  it('итог месяца — сплошная серая полоса WB', () => {
    expect(MTD_BAND_COLOUR).toBe('#e8eaed');
  });

  it('«Доходность на 1 шт» задаёт свой фон и обязана стоять выше градиента', () => {
    expect(OPAQUE_BOOLEAN_ROLES).toContain('UNIT_PROFIT');
    // «Доходность (общая)» фон не задаёт — там градиент виден, текст красится поверх
    expect(OPAQUE_BOOLEAN_ROLES).not.toContain('TOTAL_PROFIT');
  });

  it('ступени шкалы идут от узкой к широкой — иначе сработает только первая', () => {
    for (const tiers of Object.values(OZON_SCALE_TIERS)) {
      const th = tiers.map(([t]) => t);
      expect(th).toEqual([0, 0.25, 0.5, 0.75]);   // пороги
      // применять их обязано в обратном порядке
      expect([...th].reverse()).toEqual([0.75, 0.5, 0.25, 0]);
      expect(new Set(tiers.map(([, c]) => c)).size).toBe(4);
    }
  });

  it('палитра шкал взята у WB, своих цветов нет', () => {
    expect(OZON_SCALE_TIERS.INTERNAL_ADS.map(([, c]) => c))
      .toEqual(['#faf2e3', '#f5e8cd', '#efdcb4', '#e8cf99']);
    expect(OZON_SCALE_TIERS.ORDERS.map(([, c]) => c))
      .toEqual(['#e4f1e9', '#d2e8da', '#bfdecb', '#a9d4b8']);
  });

  it('знак результата передаётся цветом текста WB', () => {
    const pos = OZON_CF_FAMILIES.find((f) => f.id === 'profit.total.positive');
    const neg = OZON_CF_FAMILIES.find((f) => f.id === 'profit.total.negative');
    expect(pos?.fg).toBe('#38761d');
    expect(neg?.fg).toBe('#cc0000');
    expect(pos?.bg).toBeUndefined();            // фон остаётся за градиентом
  });

  it('ДРР выше 20 % — только в блоках: в сводке Ozon колонки ДРР нет', () => {
    const drr = OZON_CF_FAMILIES.find((f) => f.id === 'drr.above20');
    expect(drr?.scope).toBe('block');
    expect(OZON_SUMMARY_ROLES.some((r) => r.role === 'DRR')).toBe(false);
  });

  it('шкалы от MAX секции заводятся на секцию, а пороговые правила — нет', () => {
    for (const f of OZON_CF_FAMILIES) {
      expect(f.perSection).toBe(f.id.endsWith('.scale'));
    }
  });

  it('у каждой семьи есть обоснование', () => {
    for (const f of OZON_CF_FAMILIES) expect(f.why.length).toBeGreaterThan(10);
  });

  it('смещения ролей совпадают в сводке и в блоке — одно правило работает в обоих', () => {
    expect(roleOffsetsAlign()).toBe(true);
  });

  it('объединение заголовка SKU остаётся контрактом WB', () => {
    expect(SKU_TITLE_MERGE_WIDTH).toBe(7);
  });
});

/* ══════════════════════ GATE 5E — боевой код вместо стенда ══════════════════════ */
import {
  ozonMonthSpec, composeMonth, buildGrid, buildHeaderRows, serialOf, weekdayRu,
} from '../src/loaders/unitka/ozon/month.js';
import {
  rowHeightRequests, mtdBandRequests, cfFamilyRequests, futureDayRequests,
  columnName, blockColumn, summaryColumn, scaleOrderMoves, gradientOrderViolations, thresholdOf,
} from '../src/loaders/unitka/ozon/requests.js';
import {
  staticFormatRequests, columnWidthRequests, titleMergeRequests, growGridRequests, gridAfterGrowth,
  clearConditionalFormatRequests, lcdMirrorRequestsIdempotent, dayBackgroundFor, CLASS_BG,
  residualMergeRequests, unmergeRequests,
} from '../src/loaders/unitka/ozon/structure.js';
import { ozonMonthFactsSql, ozonProvenStockSql, normalizeBqRow } from '../src/loaders/unitka/ozon/bq.js';
import { batchWriteBody } from '../src/loaders/unitka/sheets.js';
import { CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT } from '../src/loaders/unitka/ozon/wbcontract.js';
import { sectionFormulas, expectedGrid, OZON_WRITE_PHASES } from '../src/loaders/unitka/ozon/monthplan.js';

const SEP = ozonMonthSpec(2026, 9, 570, 31, ['A', 'B']);
const layout = (s: typeof SEP) => ({ titleRow: s.titleRow, headerRow: s.headerRow, firstRow: s.firstRow,
  lastRow: s.lastRow, mtdRow: s.mtdRow, spacerRow: s.spacerRow, blockCount: s.blocks.length });

describe('GATE 5E — движок месяца в боевом коде', () => {
  it('состав блоков секции — префикс более поздних месяцев: слоты не переезжают', () => {
    // апрель получает слоты 16..20 тех же SKU, что и май: физических блоков не добавляется,
    // жизненный цикл мая–сентября не меняется
    const april = ozonMonthSpec(2026, 4, 397, 31, ['a', 'b', 'c']);
    const may = ozonMonthSpec(2026, 5, april.titleRow, april.days, ['a', 'b', 'c', 'd']);
    expect(may.blocks.slice(0, april.blocks.length)).toEqual([...april.blocks]);
    for (const o of april.blocks) expect(may.anchor[o]).toBe(april.anchor[o]);
  });

  it('геометрия секции выводится из предыдущей: шаг = дни + 4', () => {
    expect(SEP.titleRow).toBe(605);
    expect(SEP.firstRow).toBe(607); expect(SEP.lastRow).toBe(636);
    expect(SEP.mtdRow).toBe(637); expect(SEP.spacerRow).toBe(638);
    expect(ozonMonthSpec(2026, 10, SEP.titleRow, SEP.days, ['A']).titleRow).toBe(639);
  });

  it('серийная дата и день недели совпадают с представлением Sheets', () => {
    expect(serialOf('2026-09-01')).toBe(46266);
    expect(weekdayRu('2026-09-01')).toBe('вт');
    expect(weekdayRu('2026-09-05')).toBe('сб');
  });

  it('эквайринг попадает в экономику ровно один раз', () => {
    const facts = [
      { d: '2026-09-01', offer_id: 'A', gross_qty: 1, cancelled_qty: 0, realized_qty: 1,
        revenue: 1000, commission: 400, acquiring: 15 },
      { d: '2026-09-02', offer_id: 'A', gross_qty: 0, cancelled_qty: 0, realized_qty: 0,
        revenue: 0, commission: 0, acquiring: 7 },
    ];
    const c = composeMonth(SEP, facts);
    expect(c.totals.acqComm).toBe(15);     // была выручка → в комиссию
    expect(c.totals.acqOther).toBe(7);     // выручки нет → в прочие прямые
    expect(c.totals.acqComm + c.totals.acqOther).toBe(c.totals.acq);
  });

  it('резерв считается от цены продавца и только при реализации', () => {
    const c = composeMonth(SEP, [{ d: '2026-09-01', offer_id: 'A', gross_qty: 1, cancelled_qty: 0,
      realized_qty: 1, revenue: 1000 }]);
    expect(c.totals.tax).toBeCloseTo(20, 9);
    const none = composeMonth(SEP, [{ d: '2026-09-01', offer_id: 'A', gross_qty: 1, cancelled_qty: 1,
      realized_qty: 0, revenue: 0 }]);
    expect(none.totals.tax).toBe(0);
  });

  it('после LAST_CLOSED_DATE не пишется ни одного факта', () => {
    const c = composeMonth(SEP, [{ d: '2026-09-25', offer_id: 'A', gross_qty: 5, cancelled_qty: 0,
      realized_qty: 5, revenue: 100 }], {}, '2026-09-19');
    expect(c.cells['A|631']).toEqual({});          // 25.09 — будущее
    expect(c.totals.orders).toBe(0);               // ноль не наблюдался, он просто не записан
  });

  it('остаток пишется только при доказанном снимке; наблюдаемый ноль остаётся фактом', () => {
    const c = composeMonth(SEP, [{ d: '2026-09-03', offer_id: 'A', gross_qty: 0, cancelled_qty: 0,
      realized_qty: 0 }], { '2026-09-03|A': 0 });
    expect((c.cells['A|609'] as { stock?: number }).stock).toBe(0);
    expect((c.cells['A|610'] as { stock?: number }).stock).toBeUndefined();
  });

  it('Gate 9: база количества — ожидаемо реализованные единицы, а не только доставленные', () => {
    const c = composeMonth(SEP, [{ d: '2026-09-01', offer_id: 'A', gross_qty: 3, cancelled_qty: 0,
      realized_qty: 1, in_transit_qty: 2, revenue: 500,
      expected_realized_qty: 3, provisional_revenue_rub: 1500 }]);
    expect(c.totals.realized).toBe(3);          // 3 заказа, ни одной отмены
    expect((c.cells['A|607'] as { price?: number }).price).toBe(500);   // 1500 / 3
  });

  it('гибридный месяц: сутки до канонического окна не дают ни ячеек, ни итогов', () => {
    // апрель 2026 — 01–16 посчитаны легаси-моделью и не переписываются; если бы движок
    // считал их, контрольная сумма секции включила бы половину, которую он не пишет
    const facts = [
      { d: '2026-09-01', offer_id: 'A', gross_qty: 5, cancelled_qty: 0, realized_qty: 5, revenue: 1000 },
      { d: '2026-09-20', offer_id: 'A', gross_qty: 2, cancelled_qty: 0, realized_qty: 2, revenue: 500 },
    ];
    const all = composeMonth(SEP, facts);
    const win = composeMonth(SEP, facts, {}, null, 17);
    expect(all.totals.revenue).toBe(1500);
    expect(win.totals.revenue).toBe(500);              // 01.09 вне окна
    expect(win.cells['A|607']).toEqual({});            // ни факта, ни нуля
    expect((win.cells['A|626'] as { orders: number }).orders).toBe(2);
  });

  it('окно месяца и граница LAST_CLOSED_DATE действуют вместе', () => {
    const facts = [{ d: '2026-09-25', offer_id: 'A', gross_qty: 9, cancelled_qty: 0, realized_qty: 9, revenue: 900 }];
    const c = composeMonth(SEP, facts, {}, '2026-09-19', 17);
    expect(c.totals.revenue).toBe(0);
    expect(c.cells['A|631']).toEqual({});
  });

  it('сетка кладёт дату и день недели и в сводку, и в каждый блок', () => {
    const c = composeMonth(SEP, []);
    const g = buildGrid(SEP, c.cells, { day: {}, mtd: {}, mtdBlank: [] });
    expect(g.length).toBe(SEP.days + 1);
    expect(g[0]![0]).toBe('вт'); expect(g[0]![1]).toBe(serialOf('2026-09-01'));
    expect(g[0]![SEP.anchor['A']! - 1]).toBe(serialOf('2026-09-01'));
  });

  it('шапка объявляет фактическое число SKU секции', () => {
    const { header, title } = buildHeaderRows(SEP, [], [], {}, { A: ' Крем', B: ' Тоник' });
    expect(header[5]).toBe('Заказы 2 SKU');
    expect(header[6]).toBe('Положили в корзину 2 SKU');
    expect(title[0]).toBe('Сентябрь 2026');
  });

  it('подпись блока: идентификатор авторитетный, хвост подписи дословный', () => {
    // у владельца встречается двойной пробел после идентификатора — нормализовать его нельзя
    const { title } = buildHeaderRows(SEP, [], [], {}, { A: '  Набор  руки+тело', B: ' Крем' });
    expect(title[SEP.anchor['A']! - 1]).toBe('A  Набор  руки+тело');
    expect(title[SEP.anchor['B']! - 1]).toBe('B Крем');
  });

  it('без подписи из справочника берётся надпись эталона, а не выдумывается', () => {
    const { title } = buildHeaderRows(SEP, ['', 'эталонная надпись'], [], { A: 2 }, {});
    expect(title[SEP.anchor['A']! - 1]).toBe('эталонная надпись');
    expect(title[SEP.anchor['B']! - 1]).toBe('B');
  });

  it('формулы секции собираются в локали книги ru_RU', () => {
    const c = composeMonth(SEP, []);
    const f = sectionFormulas(SEP, c);
    const any = Object.values(f.day)[0] as string;
    expect(any).toContain(';');
    expect(f.mtdBlank.length).toBe(SEP.blocks.length * 2);   // Gate 8: пусты только дата и день недели
  });
});

describe('GATE 5E — запросы представления и структуры', () => {
  const L = [layout(SEP)];
  it('высоты строк склеиваются по роли, а не по одной строке', () => {
    const r = rowHeightRequests(1, L);
    expect(r.length).toBe(5);                       // title / header / days / mtd / spacer
  });

  it('полоса итога не красит колонку дня недели сводки', () => {
    const r = mtdBandRequests(1, L) as Array<{ repeatCell: { range: { startColumnIndex: number } } }>;
    expect(r[0]!.repeatCell.range.startColumnIndex).toBe(1);   // с колонки B
  });

  it('семьи УФ покрывают только доказанные роли', () => {
    const r = cfFamilyRequests(1, L, '$UE$2') as Array<{ addConditionalFormatRule: { rule: {
      booleanRule: { condition: { values: Array<{ userEnteredValue: string }> };
                     format: { backgroundColor?: { red: number; green: number; blue: number };
                               textFormat?: { foregroundColor: { red: number; green: number; blue: number } } } } } } }>;
    const f = r.map((x) => x.addConditionalFormatRule.rule.booleanRule);
    const green = f.find((x) => x.format.textFormat
      && Math.round(x.format.textFormat.foregroundColor.green * 255) === 0x76);
    expect(green).toBeDefined();                                    // положительный результат — зелёным
    expect(f.some((x) => x.condition.values[0]!.userEnteredValue.includes('>0,2)'))).toBe(true);  // ДРР 20 %
    expect(f.some((x) => x.condition.values[0]!.userEnteredValue.includes('TODAY()'))).toBe(true); // заказов нет
    // две шкалы по четыре ступени: три ступени с порогом от MAX, одна — просто «>0»
    expect(f.filter((x) => x.condition.values[0]!.userEnteredValue.includes('MAX(')).length).toBe(6);
    expect(f.filter((x) => x.format.backgroundColor && !x.format.textFormat).length).toBeGreaterThanOrEqual(8);
  });

  it('«будущий день» — одно правило на колонку даты, а не на секцию', () => {
    const two = [layout(SEP), layout(ozonMonthSpec(2026, 10, SEP.titleRow, SEP.days, ['A', 'B']))];
    expect(futureDayRequests(1, two, '$UE$2').length).toBe(1 + 2);   // сводка + два блока
  });

  it('имя колонки и адрес роли', () => {
    expect(columnName(12)).toBe('L'); expect(columnName(551)).toBe('UE');
    expect(blockColumn(0, 'DATE')).toBe(12);
    expect(summaryColumn('DRR')).toBeNull();        // у Ozon нет ДРР в сводке
  });

  it('порог ступени читается из формулы в локали ru_RU', () => {
    expect(thresholdOf('=AND(J1<>"";J1>0,25*MAX(J$1:J$30))')).toBe(0.25);
    expect(thresholdOf('=AND(J1<>"";J1>0)')).toBe(0);
  });

  it('перевёрнутый порядок ступеней обнаруживается', () => {
    const mk = (f: string, bg: string) => ({ ranges: [{ startColumnIndex: 9, endColumnIndex: 10 }],
      booleanRule: { condition: { values: [{ userEnteredValue: f }] },
      format: { backgroundColor: { red: parseInt(bg.slice(0, 2), 16) / 255,
        green: parseInt(bg.slice(2, 4), 16) / 255, blue: parseInt(bg.slice(4, 6), 16) / 255 } } } });
    const wrong = [mk('>0)', 'faf2e3'), mk('>0,25*MAX(a)', 'f5e8cd'),
                   mk('>0,5*MAX(a)', 'efdcb4'), mk('>0,75*MAX(a)', 'e8cf99')];
    expect(scaleOrderMoves(wrong).length).toBeGreaterThan(0);
    expect(scaleOrderMoves([...wrong].reverse())).toEqual([]);
  });

  it('градиент не должен стоять выше непрозрачного булева правила', () => {
    const col = blockColumn(0, 'UNIT_PROFIT');
    const rng = [{ startColumnIndex: col - 1, endColumnIndex: col }];
    const grad = { ranges: rng, gradientRule: {} };
    const opaque = { ranges: rng, booleanRule: { format: { backgroundColor: { red: 1, green: 1, blue: 1 } } } };
    expect(gradientOrderViolations(L, [grad, opaque])).toEqual([0]);
    expect(gradientOrderViolations(L, [opaque, grad])).toEqual([]);
  });
});

describe('GATE 5E — контракт оформления и идемпотентность', () => {
  it('визуальный контракт снят с секции текущего поколения WB', () => {
    expect(CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT.source).toContain('Сентябрь 2026');
    expect(CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT.rows.day.block['DATE']).toBeDefined();
    expect(CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT.widths.block['DATE']).toBe(84);
  });

  it('фон дня расходится с WB только там, где расхождение доказано семантикой', () => {
    expect(dayBackgroundFor('SELLER_PRICE')).toBe(CLASS_BG.FACT);
    expect(dayBackgroundFor('MANUAL_EXTERNAL')).toBe(CLASS_BG.MANUAL);
    expect(dayBackgroundFor('CART')).toBe(CLASS_BG.CALC);
  });

  // Gate 7: счёт ведётся от ШИРИНЫ БЛОКА, а не от литерала 24. Литерал описывал дефект —
  // «Прочие прямые» молча выпадали из оформления, и тест это фиксировал как норму.
  it('статический формат ставится на все четыре роли строк и на ВСЕ колонки блока', () => {
    const r = staticFormatRequests(1, [layout(SEP)]);
    expect(r.length).toBe(4 * (10 + 2 * OZON_BLOCK_WIDTH));
  });

  it('ширины колонок берутся из контракта для каждой колонки блока', () => {
    expect(columnWidthRequests(1, 1).length).toBe(10 + OZON_BLOCK_WIDTH);
  });

  it('объединения заголовков: сводка на 10 колонок, SKU — на 7, на КАЖДЫЙ слот блока', () => {
    const r = titleMergeRequests(1, [layout(SEP)], 22) as Array<{ mergeCells: { range: { startColumnIndex: number; endColumnIndex: number } } }>;
    expect(r.length).toBe(1 + 22);            // разметка одинакова во всех секциях
    expect(r[0]!.mergeCells.range.endColumnIndex - r[0]!.mergeCells.range.startColumnIndex).toBe(10);
    expect(r[1]!.mergeCells.range.endColumnIndex - r[1]!.mergeCells.range.startColumnIndex).toBe(7);
  });

  it('разъединяем только сводку и блоки — хвост владельца не трогаем', () => {
    const r = unmergeRequests(1, [layout(SEP)], 22) as Array<{ unmergeCells: { range: { endColumnIndex: number } } }>;
    expect(r[0]!.unmergeCells.range.endColumnIndex).toBe(12 + 25 * 22 - 1);   // до 561, хвост правее
  });

  // Gate 6A. Миграция ширины блока 24 -> 25 состоит из ДВУХ разных вставок, и путать их нельзя:
  //   1) каждый из 16 существующих блоков получает свою колонку ВНУТРИ себя (widenBlockAt),
  //      иначе легаси-апрель 01–16 окажется под чужими смещениями;
  //   2) только потом перед хвостом владельца вставляются 6 НОВЫХ блоков по 25.
  // Позиции расширения считаются по СТАРОЙ ширине (24), потому что вставляем в старую раскладку.
  const WIDEN = Array.from({ length: 16 }, (_, i) => 12 + i * 24 + 21);   // 33, 57, ..., 393
  const GROWTH = { widenBlockAt: WIDEN, insertColumnsBefore: 396 + WIDEN.length,
                   insertColumnCount: 6 * 25, appendColumnCount: 4, appendRowCount: 185 };

  it('колонки ВСТАВЛЯЮТСЯ перед хвостом, а не дописываются в конец', () => {
    const r = growGridRequests(1, { rows: 465, columns: 404 }, GROWTH) as Array<Record<string, any>>;
    const inserts = r.filter((x) => x['insertDimension']);
    // 16 расширений существующих блоков + 1 вставка шести новых
    expect(inserts).toHaveLength(17);
    const wide = inserts.filter((x) => x['insertDimension'].range.endIndex
                                     - x['insertDimension'].range.startIndex === 1);
    expect(wide).toHaveLength(16);
    // расширения идут СПРАВА НАЛЕВО: иначе каждая следующая позиция уехала бы на число вставок
    const starts = wide.map((x) => x['insertDimension'].range.startIndex);
    expect(starts).toEqual([...starts].sort((a: number, b: number) => b - a));
    expect(starts[0]).toBe(392);            // 393 - 1, самый правый блок
    expect(starts[15]).toBe(32);            // 33 - 1, самый левый блок
    const big = inserts.find((x) => x['insertDimension'].range.endIndex
                                  - x['insertDimension'].range.startIndex === 150)!;
    expect(big['insertDimension'].range.startIndex).toBe(411);   // 412 - 1: хвост уехал на 16
    // дописывание в конец накрыло бы помесячную панель владельца за последним блоком
    expect(r.filter((x) => x['appendDimension']?.dimension === 'COLUMNS').length).toBe(1);
    expect(r.find((x) => x['appendDimension']?.dimension === 'ROWS')!['appendDimension'].length).toBe(185);
    expect(gridAfterGrowth({ rows: 465, columns: 404 }, GROWTH)).toEqual({ rows: 650, columns: 574 });
  });

  it('Gate 6A: после миграции блоки 12..561, хвост владельца 562..570', () => {
    const after = gridAfterGrowth({ rows: 465, columns: 404 }, GROWTH);
    expect(ozonSlotStart(0)).toBe(12);
    expect(ozonSlotStart(21) + OZON_GEOMETRY.BLOCK_WIDTH - 1).toBe(561);  // 22 слота по 25
    expect(after.columns - 561).toBe(13);            // 9 колонок хвоста + 4 под зеркало LCD
  });

  it('Gate 6A: расширение блока не может повторить позицию и не может выйти за лист', () => {
    expect(() => growGridRequests(1, { rows: 465, columns: 404 },
      { ...GROWTH, widenBlockAt: [33, 33] })).toThrow(/позиция повторяется/);
    expect(() => growGridRequests(1, { rows: 465, columns: 404 },
      { ...GROWTH, widenBlockAt: [9999] })).toThrow(/вне листа/);
  });

  it('повторный прогон не растит сетку: нулевой рост даёт пустой план', () => {
    // размер читается перед каждой сборкой плана; устаревшее значение — единственный
    // способ сломать идемпотентность
    const none = { insertColumnsBefore: 396, insertColumnCount: 0, appendColumnCount: 0, appendRowCount: 0 };
    expect(growGridRequests(1, { rows: 650, columns: 552 }, none)).toEqual([]);
    expect(gridAfterGrowth({ rows: 650, columns: 552 }, none)).toEqual({ rows: 650, columns: 552 });
  });

  it('вставка обязана быть кратна ширине блока и не может сжимать лист', () => {
    expect(() => growGridRequests(1, { rows: 465, columns: 404 }, { ...GROWTH, insertColumnCount: 5 })).toThrow();
    expect(() => growGridRequests(1, { rows: 465, columns: 404 }, { ...GROWTH, appendRowCount: -1 })).toThrow();
    expect(() => growGridRequests(1, { rows: 465, columns: 404 }, { ...GROWTH, insertColumnsBefore: 9999 })).toThrow();
  });

  it('пустые объединения-следы создаются только по явному списку', () => {
    expect(residualMergeRequests(1, [])).toEqual([]);
    const r = residualMergeRequests(1, [[466, 540, 541]]) as Array<{ mergeCells: { range: { startRowIndex: number; startColumnIndex: number; endColumnIndex: number } } }>;
    expect(r[0]!.mergeCells.range).toMatchObject({ startRowIndex: 465, startColumnIndex: 539, endColumnIndex: 541 });
  });

  it('УФ ставится заменой: сначала снимаются все существующие правила, с конца', () => {
    const r = clearConditionalFormatRequests(1, 3) as Array<{ deleteConditionalFormatRule: { index: number } }>;
    expect(r.map((x) => x.deleteConditionalFormatRule.index)).toEqual([2, 1, 0]);
  });

  it('зеркало LCD при повторе обновляется, а не дублируется, и принимает своё имя', () => {
    expect(JSON.stringify(lcdMirrorRequestsIdempotent(1, 2, 551, null))).toContain('addNamedRange');
    expect(JSON.stringify(lcdMirrorRequestsIdempotent(1, 2, 551, 'nr1'))).toContain('updateNamedRange');
    // имя уникально в книге, поэтому одноразовым копиям листа нужно своё
    expect(JSON.stringify(lcdMirrorRequestsIdempotent(1, 2, 551, null, 'OZON_LCD_MIRROR_X'))).toContain('OZON_LCD_MIRROR_X');
  });

  it('ожидаемый размер сетки выводится из роста', () => {
    expect(expectedGrid({ rows: 465, columns: 404 },
      { insertColumnsBefore: 396, insertColumnCount: 144, appendColumnCount: 4, appendRowCount: 185 }))
      .toEqual({ rows: 650, columns: 552 });
  });
});

describe('GATE 5E — источник фактов', () => {
  it('SQL берёт витрину Ozon и справочник каналов, а не сырьё напрямую', () => {
    const sql = ozonMonthFactsSql({ project: 'p-1', from: '2026-09-01', to: '2026-09-30' });
    // Gate 8: источник — операционный слой над витриной, а не витрина напрямую.
    // Он добавляет эффективные величины там, где Ozon ещё не опубликовал начисление.
    expect(sql).toContain('ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL');
    expect(sql).toContain('evetis_ref.REF_SKU_CHANNEL_MAP');
    expect(sql).toContain("marketplace='OZON'");
    // факт не переопределяется: он едет рядом с эффективной величиной
    expect(sql).toContain('f.commission_rub commission_actual');
    expect(sql).toContain('f.commission_effective_rub commission');
    expect(sql).toContain('f.commission_state');
  });

  it('границы периода валидируются — в текст запроса не попадает произвольная строка', () => {
    expect(() => ozonMonthFactsSql({ project: 'p', from: "2026-09-01' OR 1=1--", to: '2026-09-30' })).toThrow();
    expect(() => ozonMonthFactsSql({ project: 'p', from: '2026-09-30', to: '2026-09-01' })).toThrow();
  });

  it('снимок остатка доказан только совпадением даты съёма', () => {
    expect(ozonProvenStockSql({ project: 'p', from: '2026-09-01', to: '2026-09-30' }))
      .toContain('s.extracted_day = s.d');
  });

  it('остаток берётся из фактической колонки RAW_OZON_STOCKS', () => {
    // Gate 5I: запрос ссылался на несуществующую колонку `present` и падал на живом BigQuery.
    // Стенд этого не ловил, потому что работал с заранее выгруженным JSON.
    const sql = ozonProvenStockSql({ project: 'p', from: '2026-09-01', to: '2026-09-30' });
    expect(sql).toContain('available_stock_count');
    expect(sql).not.toContain('SUM(present)');
  });
});

// ---------------------------------------------------------------------------------------------
// Gate 5K — контракт полноты: комиссия имеет ТРИ состояния, а не два.
// ---------------------------------------------------------------------------------------------
import { ozonCommissionState, type OzonFactRow } from '../src/loaders/unitka/ozon/month.js';

describe('контракт полноты Ozon (Gate 5K)', () => {
  const row = (o: Partial<OzonFactRow>): OzonFactRow =>
    ({ d: '2026-08-02', offer_id: '930334395', gross_qty: 1, cancelled_qty: 0, realized_qty: 1, ...o });

  it('обычная продажа с комиссией — PRESENT', () => {
    expect(ozonCommissionState(row({ commission: 510.45 }))).toBe('PRESENT');
  });

  it('обычная продажа без комиссии в источнике — MISSING', () => {
    expect(ozonCommissionState(row({ commission_missing_qty: 1 }))).toBe('MISSING');
  });

  it('выкуп товара — NOT_APPLICABLE, а не MISSING', () => {
    // Комиссии у выкупа не существует как факта хозяйственной жизни. Считать такую строку
    // неполной — значит сообщать о пробеле в данных, которого нет.
    const st = ozonCommissionState(row({ commission_not_applicable_qty: 1, commission: 0 }));
    expect(st).toBe('NOT_APPLICABLE');
    expect(st).not.toBe('MISSING');
  });

  it('MISSING имеет приоритет: настоящий пробел не маскируется неприменимостью', () => {
    expect(ozonCommissionState(row({ commission_missing_qty: 1, commission_not_applicable_qty: 1 })))
      .toBe('MISSING');
  });

  it('отсутствие флагов не выдумывает пробел', () => {
    expect(ozonCommissionState(row({}))).toBe('PRESENT');
  });

  it('SQL фактов несёт все три флага полноты и величину непроверенной выручки', () => {
    const sql = ozonMonthFactsSql({ project: 'p', from: '2026-08-01', to: '2026-08-31' });
    expect(sql).toContain('f.commission_missing_qty');
    expect(sql).toContain('f.commission_not_applicable_qty');
    expect(sql).toContain('f.buyout_revenue_unproven_qty');
    expect(sql).toContain('f.buyout_revenue_unproven_rub');
  });

  it('Юнитка не знает про Беларусь: никакой географии в контракте фактов', () => {
    // Тип операции — свойство витрины, а не листа. Если география просочится в загрузчик,
    // выкуп перестанет быть обычной строкой суток x SKU (Gate 5K, §10 и §14).
    const sql = ozonMonthFactsSql({ project: 'p', from: '2026-08-01', to: '2026-08-31' });
    for (const w of ['city', 'Беларус', 'Belarus', 'CIS_BUYOUT', 'payout_rub']) {
      expect(sql).not.toContain(w);
    }
  });
});

describe('выкуп меняет базу налогового резерва (Gate 5K)', () => {
  // Выручка и «комиссия» выкупа падают на одну и ту же величину, поэтому вклад не меняется.
  // Но налоговый резерв считается ОТ ВЫРУЧКИ, а не от вклада: 2 % с цены, которую продавец
  // никогда не получал, — это завышенный резерв. Канонический итог обязан это отразить.
  const spec = ozonMonthSpec(2026, 4, 1, 0, ['535580776']);
  const base = {
    d: '2026-04-25', offer_id: '535580776',
    gross_qty: 1, cancelled_qty: 0, realized_qty: 1,
    logistics: 0, acquiring: 0, storage: 0, other_direct: 0, cogs_amt: 0, ads_spend: 0,
  };
  const before = composeMonth(spec, [{ ...base, revenue: 998, commission: 439.12 }], {}, '2026-09-20', 17);
  const after = composeMonth(spec, [{ ...base, revenue: 558.88, commission: 0 }], {}, '2026-09-20', 17);

  it('вклад до налога не меняется: 998 − 439,12 = 558,88 − 0', () => {
    expect(before.totals.revenue - before.totals.comm).toBeCloseTo(558.88, 6);
    expect(after.totals.revenue - after.totals.comm).toBeCloseTo(558.88, 6);
  });

  it('налоговый резерв падает ровно на 2 % снятого дисконта', () => {
    expect(before.totals.tax - after.totals.tax).toBeCloseTo(439.12 * MANAGEMENT_TAX_RESERVE_RATE, 9);
    expect(before.totals.tax - after.totals.tax).toBeCloseTo(8.7824, 9);
  });

  it('канонический итог растёт ровно на эту же величину и ни на копейку больше', () => {
    expect(after.totals.canonical - before.totals.canonical).toBeCloseTo(8.7824, 9);
  });
});

describe('продвижение с привязкой к SKU (Gate 5L)', () => {
  const spec = ozonMonthSpec(2026, 8, 1, 0, ['930334395']);
  const base = {
    d: '2026-08-05', offer_id: '930334395',
    gross_qty: 1, cancelled_qty: 0, realized_qty: 1,
    revenue: 1000, commission: 400, logistics: 100, acquiring: 0, storage: 0,
    other_direct: 0, cogs_amt: 200, ads_spend: 0,
  };
  const without = composeMonth(spec, [base], {}, '2026-09-20', 1);
  const withPromo = composeMonth(spec, [{ ...base, promo: 250 }], {}, '2026-09-20', 1);

  it('расход попадает в прочие прямые и уменьшает итог ровно на свою величину', () => {
    expect(withPromo.totals.promo).toBeCloseTo(250, 9);
    expect(withPromo.totals.other - without.totals.other).toBeCloseTo(250, 9);
    expect(without.totals.canonical - withPromo.totals.canonical).toBeCloseTo(250, 9);
  });

  it('в колонку рекламы НЕ попадает: там атрибуция CPC, а не начисленная услуга', () => {
    expect(withPromo.totals.ads).toBe(0);
    const cell = withPromo.cells['930334395|' + (spec.firstRow + 4)] as Record<string, number>;
    expect(cell.adin).toBeUndefined();
  });

  it('провенанс сохраняется в аудите, а не растворяется в сумме', () => {
    const rec = withPromo.audit.find((a) => a.date === '2026-08-05');
    expect(rec).toBeDefined();
    expect(rec!.skuPromotion).toBeCloseTo(250, 9);
    expect(rec!.total).toBeCloseTo(250, 9);
    // сумма слагаемых аудита обязана совпасть с прочими прямыми: ничего не потеряно и не удвоено
    expect(rec!.otherFees + rec!.logisticsNonRealized + rec!.acquiringWithoutRevenue + rec!.skuPromotion)
      .toBeCloseTo(rec!.total, 9);
  });

  it('налоговый резерв считается от выручки и продвижением НЕ затрагивается', () => {
    expect(withPromo.totals.tax).toBeCloseTo(without.totals.tax, 9);
    expect(withPromo.totals.tax).toBeCloseTo(1000 * MANAGEMENT_TAX_RESERVE_RATE, 9);
  });

  it('отсутствие расхода не создаёт нулевую строку аудита', () => {
    expect(without.audit).toHaveLength(0);
  });

  it('SQL фактов запрашивает корзину продвижения', () => {
    const sql = ozonMonthFactsSql({ project: 'p', from: '2026-08-01', to: '2026-08-31' });
    expect(sql).toContain('f.sku_promotion_rub promo');
  });
});

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 7 — визуальная интеграция «Прочих прямых»
 * ════════════════════════════════════════════════════════════════════════════ */
describe('OZON adapter — Gate 7: оформление «Прочих прямых»', () => {
  const SEC = { titleRow: 10, headerRow: 11, firstRow: 12, lastRow: 41, mtdRow: 42, spacerRow: 43, blockCount: 3 };
  const KINDS = ['title', 'header', 'day', 'mtd'] as const;

  it('ни одна роль блока не остаётся без оформления и ширины', () => {
    expect(unstyledBlockRoles()).toEqual([]);
  });

  it('роль без спецификации и без образца — ошибка сборки, а не серая полоса', () => {
    expect(() => blockSpecFor('day', 'НЕТ_ТАКОЙ' as never)).toThrow(/не определено/);
    expect(() => blockWidthFor('НЕТ_ТАКОЙ' as never)).toThrow(/не определена/);
  });

  it('образец «Прочих прямых» — «Хранение»: тот же класс FACT, тот же начисленный расход', () => {
    expect(OZON_STYLE_TEMPLATE.OTHER_DIRECT).toBe('STORAGE');
    expect(OZON_ROLE_CLASS.OTHER_DIRECT).toBe(OZON_ROLE_CLASS.STORAGE);
  });

  it('оформление наследуется от «Хранения» во всех четырёх ролях строк', () => {
    for (const kind of KINDS) {
      const od = blockSpecFor(kind, 'OTHER_DIRECT');
      const st = blockSpecFor(kind, 'STORAGE');
      expect(od.fontFamily).toBe(st.fontFamily);
      expect(od.fontSize).toBe(st.fontSize);
      expect(od.bold).toBe(st.bold);
      expect(od.ha).toBe(st.ha);
      expect(od.va).toBe(st.va);
      expect(od.wrap).toBe(st.wrap);
      expect(od.numberFormat).toEqual(st.numberFormat);
      expect(od.bg).toBe(st.bg);
    }
  });

  it('ширина 84 — как у всех денежных колонок блока, а не 63', () => {
    expect(blockWidthFor('OTHER_DIRECT')).toBe(84);
    for (const r of ['COMMISSION', 'LOGISTICS', 'STORAGE', 'TAX_RESERVE'] as const) {
      expect(blockWidthFor('OTHER_DIRECT')).toBe(blockWidthFor(r));
    }
  });

  it('сутки и итог — рубли, а не безликое число', () => {
    for (const kind of ['day', 'mtd'] as const) {
      expect(blockSpecFor(kind, 'OTHER_DIRECT').numberFormat?.pattern).toBe('#,##0\\ "₽"');
      expect(blockSpecFor(kind, 'OTHER_DIRECT').ha).toBe('RIGHT');
    }
  });

  it('шапка: зелёный факт площадки, перенос строки, читаемая подпись', () => {
    const h = blockSpecFor('header', 'OTHER_DIRECT');
    expect(h.bg).toBe('#b7e1cd');          // «факт площадки» в языке шапок WB
    expect(h.wrap).toBe('WRAP');           // «Прочие прямые» переносятся, а не обрезаются
    expect(h.ha).toBe('CENTER');
  });

  it('заголовок SKU: колонка внутри полосы блока, а не белый разрыв с подчёркиванием', () => {
    const t = blockSpecFor('title', 'OTHER_DIRECT');
    expect(t.bg).toBe('#dce8f2');
    expect(t.va).toBe('MIDDLE');
    expect(t.borders).toEqual({ top: 'SOLID_MEDIUM', bottom: 'SOLID_MEDIUM' });
  });

  it('рамки замкнуты во всех четырёх ролях строк — открытой полосы нет', () => {
    for (const kind of KINDS) {
      const b = blockSpecFor(kind, 'OTHER_DIRECT').borders ?? {};
      expect(Object.keys(b).length).toBeGreaterThan(0);
      expect(b.top ?? b.bottom).toBeTruthy();
    }
    // сутки: колонка полностью в сетке блока
    expect(blockSpecFor('day', 'OTHER_DIRECT').borders)
      .toEqual({ top: 'SOLID', bottom: 'SOLID', left: 'SOLID', right: 'SOLID' });
  });

  it('короб прямых расходов в итоге растёт до трёх ячеек: логистика | хранение | прочие', () => {
    const log = blockSpecFor('mtd', 'LOGISTICS').borders ?? {};
    const sto = blockSpecFor('mtd', 'STORAGE').borders ?? {};
    const od = blockSpecFor('mtd', 'OTHER_DIRECT').borders ?? {};
    expect(log.left).toBe('SOLID_MEDIUM');   // левая стена короба
    expect(log.right).toBe('SOLID');         // внутренняя перегородка
    expect(sto.right).toBe('SOLID');         // стена СНЯТА: короб пошёл дальше
    expect(od.right).toBe('SOLID_MEDIUM');   // правая стена теперь здесь
    for (const b of [log, sto, od]) {
      expect(b.top).toBe('SOLID_MEDIUM');
      expect(b.bottom).toBe('SOLID_MEDIUM');
    }
  });

  it('правка рамки у соседа ровно одна и она объявлена', () => {
    expect(OZON_BORDER_OVERRIDES).toHaveLength(1);
    expect(OZON_BORDER_OVERRIDES[0]).toMatchObject({ row: 'mtd', role: 'STORAGE', side: 'right' });
  });

  it('новый месяц получает оформление из геометрии: запрос на каждый блок каждой секции', () => {
    const reqs = staticFormatRequests(7, [SEC, { ...SEC, titleRow: 50, headerRow: 51, firstRow: 52,
      lastRow: 82, mtdRow: 83, spacerRow: 84, blockCount: 3 }]);
    const odCols = [0, 1, 2].map((b) => 12 + 25 * b + OFFSET.otherDirect);
    for (const kind of KINDS) void kind;
    const touched = reqs.filter((r) => {
      const range = (r as { repeatCell: { range: { startColumnIndex: number } } }).repeatCell.range;
      return odCols.includes(range.startColumnIndex + 1);
    });
    expect(touched).toHaveLength(2 * 3 * 4);      // 2 секции × 3 блока × 4 роли строк
  });

  it('новый SKU-блок получает то же оформление: 22 слота — 22 запроса на роль строки', () => {
    const reqs = staticFormatRequests(7, [{ ...SEC, blockCount: 22 }]);
    const od = reqs.filter((r) => {
      const range = (r as { repeatCell: { range: { startColumnIndex: number } } }).repeatCell.range;
      return (range.startColumnIndex + 1 - 12 - OFFSET.otherDirect) % 25 === 0
        && range.startColumnIndex + 1 >= 12 + OFFSET.otherDirect;
    });
    expect(od).toHaveLength(22 * 4);
    const widths = columnWidthRequests(7, 22).filter((r) => {
      const range = (r as { updateDimensionProperties: { range: { startIndex: number } } })
        .updateDimensionProperties.range;
      return (range.startIndex + 1 - 12 - OFFSET.otherDirect) % 25 === 0
        && range.startIndex + 1 >= 12 + OFFSET.otherDirect;
    });
    expect(widths).toHaveLength(22);
    for (const w of widths) {
      expect((w as { updateDimensionProperties: { properties: { pixelSize: number } } })
        .updateDimensionProperties.properties.pixelSize).toBe(84);
    }
  });

  it('повторный прогон не дрейфует и не удваивает рамки', () => {
    const a = JSON.stringify(staticFormatRequests(7, [SEC]));
    const b = JSON.stringify(staticFormatRequests(7, [SEC]));
    expect(a).toBe(b);
    const wa = JSON.stringify(columnWidthRequests(7, 22));
    expect(wa).toBe(JSON.stringify(columnWidthRequests(7, 22)));
  });

  it('оформление пишется форматом: ни одного userEnteredValue в запросах', () => {
    const all = [...staticFormatRequests(7, [SEC]), ...columnWidthRequests(7, 22)];
    expect(JSON.stringify(all)).not.toContain('userEnteredValue');
    for (const r of staticFormatRequests(7, [SEC])) {
      expect((r as { repeatCell: { fields: string } }).repeatCell.fields)
        .toMatch(/^userEnteredFormat\./);
    }
  });

  it('ширина блока WB не задета: у WB по-прежнему 24 колонки', () => {
    expect(OZON_BLOCK_WIDTH).toBe(25);
    expect(Object.keys(WB_OFFSET)).toHaveLength(24);
  });
});

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 8 — политика строки итога месяца
 * ════════════════════════════════════════════════════════════════════════════ */
describe('OZON adapter — Gate 8: строка итога', () => {
  const G = { firstDailyRow: 434, lastDailyRow: 463, mtdRow: 464 };
  const B = ozonBlockMtdFormulas(12, G);

  it('все 25 смещений классифицированы — неклассифицированных нет', () => {
    expect(unclassifiedSummaryRoles()).toEqual([]);
    expect(summaryPolicyMap()).toHaveLength(25);
    expect(summaryPolicyMap().map((x) => x.offset)).toEqual([...Array(25).keys()]);
  });

  it('каждое содержательное смещение получило формулу; пусты только подписи', () => {
    for (const { offset, role, policy } of summaryPolicyMap()) {
      if (policy === 'NOT_APPLICABLE') {
        expect(B.has(offset), `${role} обязана остаться пустой`).toBe(false);
        expect(OZON_MTD_BLANK_OFFSETS).toContain(offset);
      } else {
        expect(B.has(offset), `${role} осталась без итога`).toBe(true);
      }
    }
    expect(OZON_MTD_BLANK_OFFSETS).toHaveLength(2);
  });

  it('НИ ОДНОГО AVERAGE по дневным ячейкам: среднее средних запрещено', () => {
    for (const [off, f] of B) {
      expect(f, `смещение ${off}`).not.toMatch(/\bAVERAGE\s*\(/);
      expect(f, `смещение ${off}`).not.toMatch(/\bAVERAGEIF/);
    }
  });

  it('все агрегаты отсечены по LAST_CLOSED_DATE — незакрытые сутки в итог не попадают', () => {
    for (const [off, f] of B) {
      if (off === OFFSET.profit1) continue;           // ссылается на уже отсечённые ячейки итога
      expect(f, `смещение ${off}`).toContain('LAST_CLOSED_DATE');
    }
  });

  it('цена — средняя реализации, взвешенная единицами, а не среднее по суткам', () => {
    const f = B.get(OFFSET.price) as string;
    expect(f).toContain('SUMPRODUCT');
    expect(f).toContain('(P434:P463-R434:R463)');     // вес = заказы − отмены
    expect(f).toContain('Z434:Z463');                 // сама цена
    expect(f).toContain('(Z434:Z463<>"")');           // пустые сутки не разбавляют
  });

  it('комиссия — эффективная ставка периода: вся комиссия / вся выручка', () => {
    const f = B.get(OFFSET.commission) as string;
    expect(f).toContain('*Z434:Z463*AC434:AC463');    // числитель: цена × ставка × единицы
    expect(f).toMatch(/\/SUMPRODUCT\(\(\$L\$434:\$L\$463<=LAST_CLOSED_DATE\)\*\(P434:P463-R434:R463\)\*Z434:Z463\)/);
  });

  it('логистика — средняя на реализованную единицу, а НЕ сумма логистики', () => {
    const f = B.get(OFFSET.logistics) as string;
    expect(f).toContain('SUMPRODUCT');
    expect(f).not.toContain('SUMIF');                 // сумма здесь была бы неверна
    expect(f).toContain('AE434:AE463');
  });

  it('хранение, прочие прямые и доходность общая — сумма периода', () => {
    for (const off of [OFFSET.storage, OFFSET.otherDirect, OFFSET.profitAll]) {
      expect(B.get(off)).toContain('SUMIF');
      expect(B.get(off)).not.toContain('SUMPRODUCT');
    }
  });

  it('налог — сумма периода, собранная взвешиванием: дневная ячейка поединична', () => {
    const f = B.get(OFFSET.tax) as string;
    expect(f).toContain('SUMPRODUCT');
    expect(f).toContain('AH434:AH463');
    expect(OZON_UNITKA_SUMMARY_POLICY.TAX_RESERVE.policy).toBe('SUM');
    expect(OZON_UNITKA_SUMMARY_POLICY.TAX_RESERVE.basis).toBe('PER_UNIT');
  });

  it('доходность 1 шт — вся доходность единиц / все реализованные единицы', () => {
    expect(B.get(OFFSET.unitProfit)).toContain('AI434:AI463');
    expect(B.get(OFFSET.unitProfit)).toContain('SUMPRODUCT');
    expect(OZON_UNITKA_SUMMARY_POLICY.FINAL_UNIT_PROFIT.policy).toBe('WEIGHTED_AVERAGE');
  });

  it('остаток — последний ДОКАЗАННЫЙ снимок, а не сумма и не легаси-проекция', () => {
    const f = B.get(OFFSET.stock) as string;
    expect(f).toContain('FILTER(');
    expect(f).toContain('INDEX(');
    // раньше STOCK_POLICY.factualFrom истории остатка не существует: отвергнутая
    // синтетическая проекция (местами отрицательная) в итог не попадает
    expect(f).toContain('>=DATE(2026,8,31)');
    expect(f).not.toContain('LOOKUP');          // идиома LOOKUP(2;1/…) в Sheets даёт ошибку
    expect(OZON_UNITKA_SUMMARY_POLICY.STOCK.policy).toBe('ENDING_BALANCE');
  });

  it('«неизвестно» не превращается в ноль: без наблюдений итог пуст', () => {
    for (const off of [OFFSET.price, OFFSET.commission, OFFSET.logistics, OFFSET.unitProfit,
                       OFFSET.tax, OFFSET.stock, OFFSET.turnover, OFFSET.spp]) {
      expect(B.get(off), `смещение ${off}`).toMatch(/IFERROR\(|IF\(COUNT/);
      expect(B.get(off), `смещение ${off}`).toContain('""');
    }
  });

  it('итог живой: ссылается на дневные ячейки, а не на замороженные числа', () => {
    for (const [off, f] of B) {
      expect(f.startsWith('='), `смещение ${off}`).toBe(true);
      expect(f, `смещение ${off}`).toMatch(/[A-Z]+4(34|63|64)/);   // адреса строк секции
    }
  });

  it('ru_RU: разделители переводятся, формула не ломается', () => {
    const loc = toLocaleFormula(B.get(OFFSET.price) as string, 'SEMICOLON');
    expect(loc).toContain(';');
    expect(loc).not.toMatch(/,(?=[A-Z$])/);
  });

  it('геометрия следует секции: формулы сентября ссылаются на строки сентября', () => {
    const S = ozonBlockMtdFormulas(12, { firstDailyRow: 607, lastDailyRow: 636, mtdRow: 637 });
    expect(S.get(OFFSET.price)).toContain('607:Z636');
    expect(S.get(OFFSET.price)).not.toContain('434');
  });
});

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 8 — окно ежедневной перезаписи
 * ════════════════════════════════════════════════════════════════════════════ */
describe('OZON adapter — Gate 8: окно перезаписи', () => {
  it('45 суток: наименьшее окно, накрывающее наблюдённый максимум задержки (36)', () => {
    expect(OZON_REWRITE_WINDOW_DAYS).toBe(45);
    expect(OZON_REWRITE_WINDOW_DAYS).toBeGreaterThan(OZON_OBSERVED_MAX_VISIBILITY_DAYS);
  });

  it('окно короче наблюдённой задержки запрещено — иначе оценка не сменится фактом', () => {
    expect(() => ozonRewriteWindow('2026-09-20', 35)).toThrow(/короче наблюдённой задержки/);
    expect(() => ozonRewriteWindow('2026-09-20', 30)).toThrow();
    expect(() => ozonRewriteWindow('2026-09-20', 36)).not.toThrow();
  });

  it('окно пересекает границы месяцев', () => {
    const w = ozonRewriteWindow('2026-09-20');
    expect(w.from).toBe('2026-08-07');
    expect(w.to).toBe('2026-09-20');
    expect(ozonWindowMonths(w)).toEqual(['2026-08', '2026-09']);
  });

  it('окно не уходит раньше эпохи Ozon: легаси-апрель неприкосновенен', () => {
    const w = ozonRewriteWindow('2026-05-01');
    expect(w.rollingFrom).toBe('2026-03-18');
    expect(w.from).toBe('2026-04-17');                 // обрезано эпохой
    expect(ozonFromDayFor('2026-04', w)).toBe(17);     // гибридный апрель: с 17-го
    expect(ozonFromDayFor('2026-05', w)).toBe(1);
  });

  it('апрель остаётся гибридным даже если окно начинается раньше 17-го', () => {
    const w = ozonRewriteWindow('2026-05-20', 45, '2026-04-01');
    expect(w.from).toBe('2026-04-06');
    expect(ozonFromDayFor('2026-04', w)).toBe(17);     // легаси 01–16 не переписывается никогда
  });

  it('LCD раньше эпохи — отказ, а не тихая пустая запись', () => {
    expect(() => ozonRewriteWindow('2026-04-01')).toThrow(/раньше эпохи/);
  });

  it('глубокая сверка: 120 суток, раз в месяц, первого числа', () => {
    expect(OZON_DEEP_RECONCILIATION_DAYS).toBe(120);
    expect(OZON_DEEP_RECONCILIATION_CADENCE).toBe('MONTHLY');
    expect(ozonDeepWindow('2026-09-20').days).toBe(120);
    expect(ozonDeepWindow('2026-09-20').deep).toBe(true);
    expect(isDeepReconciliationDay('2026-10-01')).toBe(true);
    expect(isDeepReconciliationDay('2026-09-20')).toBe(false);
  });

  it('окно Ozon независимо от окна WB: константы не общие', () => {
    expect(OZON_REWRITE_WINDOW_DAYS).not.toBe(RECONCILE_WINDOW_DAYS);
    expect(OZON_REWRITE_EPOCH).not.toBe('2026-09-01');
  });
});

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 8 — суточный загрузчик
 * ════════════════════════════════════════════════════════════════════════════ */
describe('OZON adapter — Gate 8: суточный загрузчик', () => {
  const CFG_BASE = { GCP_PROJECT_ID: 'project-x', BQ_RAW_DATASET: 'wb_raw', ENVIRONMENT: 'prod' };
  const canon = (t: string) => (/^\d{6,}$/.test(t) ? t : null);
  // лист: строка заголовка блока «Дата» в колонке L(12); блоки шагают по 25
  const mkGrid = () => {
    const g: (string | number)[][] = Array.from({ length: 40 }, () => Array(600).fill(''));
    const put = (r: number, c: number, v: string | number) => { (g[r - 1] as (string | number)[])[c - 1] = v; };
    put(1, 1, 'Сентябрь 2026');
    put(1, 12, '909951444 крем');
    put(1, 37, '438775437 тоник');
    put(2, 12, 'Дата');
    for (let d = 0; d < 3; d++) {
      put(3 + d, 12, 46266 + d);                 // 01..03.09.2026
      put(3 + d, 12 + 5, 10 + d);                // корзина блока 0
      put(3 + d, 37 + 5, 20 + d);                // корзина блока 1
    }
    return g;
  };

  it('раскладка читается из ЖИВОГО листа, а не из памяти', () => {
    const { sections, cart } = parseLiveLayout(mkGrid(), 562, canon);
    expect(sections).toHaveLength(1);
    expect(sections[0]!.blocks).toEqual(['909951444', '438775437']);
    expect(sections[0]!.monthKey).toBe('2026-09');
    expect(sections[0]!.days).toEqual(['2026-09-01', '2026-09-02', '2026-09-03']);
    expect(sections[0]!.titleRow).toBe(1);
  });

  it('наблюдения корзины собираются из листа и не выдумываются', () => {
    const { cart } = parseLiveLayout(mkGrid(), 562, canon);
    expect(cart['2026-09-01|909951444']).toBe(10);
    expect(cart['2026-09-03|438775437']).toBe(22);
    expect(Object.keys(cart)).toHaveLength(6);   // ровно наблюдённые, без нулей-призраков
  });

  it('панель владельца не читается как блок', () => {
    const g = mkGrid();
    (g[0] as (string | number)[])[561] = 'Сентябрь 2026';   // колонка 562 — хвост владельца
    const { sections } = parseLiveLayout(g, 562, canon);
    expect(sections[0]!.blocks).toHaveLength(2);
  });

  it('подпись блока не авторитетна: нераспознанный токен блоком не становится', () => {
    const g = mkGrid();
    (g[0] as (string | number)[])[61] = 'Апрель 2026';      // колонка 62 = слот 2
    const { sections } = parseLiveLayout(g, 562, canon);
    expect(sections[0]!.blocks).toEqual(['909951444', '438775437']);
  });

  it('загрузчик зарегистрирован и по умолчанию не пишет', () => {
    expect(Object.keys(LOADERS)).toContain('ozon-unitka');
    const c = loadConfig({ ...CFG_BASE });
    expect(c.ozonUnitkaWriteEnabled).toBe(false);
    expect(c.ozonUnitkaSheetName).toBe('OZON_Юнит_2025');
    // списка offer_id в окружении НЕТ: идентичность приходит из справочника каналов
    expect((c as Record<string, unknown>).ozonUnitkaOffers).toBeUndefined();
    expect(c.ozonUnitkaTailFirstColumn).toBe(562);
  });

  it('окружение Ozon не пересекается с окружением WB', () => {
    const c = loadConfig({ ...CFG_BASE, UNITKA_WRITE_ENABLED: '1', OZON_UNITKA_WRITE_ENABLED: '0' });
    expect(c.unitkaWriteEnabled).toBe(true);
    expect(c.ozonUnitkaWriteEnabled).toBe(false);          // включение WB не включает Ozon
    expect(c.unitkaSheetName).not.toBe(c.ozonUnitkaSheetName);
  });

  it('опечатка в псевдонимах не роняет конфигурацию', () => {
    const c = loadConfig({ ...CFG_BASE, OZON_UNITKA_OFFER_ALIASES: '{сломано' });
    expect(c.ozonUnitkaOfferAliases).toEqual({});
  });
});

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 9 — цена покупателя и СПП не выдумываются
 * ════════════════════════════════════════════════════════════════════════════ */
describe('OZON adapter — Gate 9: СПП и цена покупателя', () => {
  it('без СПП цена покупателя ПУСТА, а не равна цене продавца', () => {
    const f = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 625).get(OFFSET.priceSpp) as string;
    expect(f).toContain('AA625=""');            // пустая СПП — отдельная ветка
    expect(f).toContain('IF(OR(');
  });

  it('при известной СПП форма прежняя — цена минус скидка', () => {
    const f = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 625).get(OFFSET.priceSpp) as string;
    expect(f).toContain('Z625-Z625*N(AA625)%');
  });

  it('цена продавца от отсутствия СПП не страдает: это разные колонки', () => {
    const m = ozonBlockDayFormulas({ start: 12, cogsTerm: '0' }, 625);
    // цену продавца движок пишет значением, формулы для неё нет — она не зависит от СПП
    expect(m.has(OFFSET.price)).toBe(false);
    // «цена минус комиссия» тоже считается от цены продавца, а не от цены покупателя
    expect(m.get(OFFSET.priceMinusComm)).toContain('Z625-Z625*N(AC625)');
  });
});

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 9 §8–§10 — новый месяц, новый SKU, ёмкость слотов
 * ════════════════════════════════════════════════════════════════════════════ */
describe('OZON adapter — Gate 9: новый месяц создаётся сам', () => {
  // живой лист на 21.09.2026: последняя секция — сентябрь, titleRow 605, 30 дней, 22 блока
  const SKUS = Array.from({ length: 22 }, (_, i) => `sku${String(i).padStart(2, '0')}`);
  const LIVE = [
    { monthKey: '2026-08', titleRow: 570, days: Array(31).fill('d'), blocks: SKUS },
    { monthKey: '2026-09', titleRow: 605, days: Array(30).fill('d'), blocks: SKUS },
  ];
  const ACT = Object.fromEntries(SKUS.map((s) => [s, '2026-04']));

  it('ОКТЯБРЬ 2026: секция достраивается из геометрии сентября, без ручной правки', () => {
    const plans = resolveSections({ live: LIVE, windowMonths: ['2026-09', '2026-10'],
      firstActivity: ACT, blockSlots: 22 });
    expect(plans.map((p) => p.monthKey)).toEqual(['2026-09', '2026-10']);
    const oct = plans[1]!;
    expect(oct.isNew).toBe(true);
    expect(oct.titleRow).toBe(605 + 30 + 4);      // шаг Calendar V2 = дни + 4
    expect(oct.headerRow).toBe(oct.titleRow + 1);
    expect(oct.days).toBe(31);                     // октябрь — 31 день
    expect(oct.blocks).toEqual(SKUS);              // состав SKU переносится
  });

  it('сентябрь не задваивается и не пересоздаётся', () => {
    const plans = resolveSections({ live: LIVE, windowMonths: ['2026-09', '2026-10'],
      firstActivity: ACT, blockSlots: 22 });
    expect(plans.filter((p) => p.monthKey === '2026-09')).toHaveLength(1);
    expect(plans.find((p) => p.monthKey === '2026-09')!.isNew).toBe(false);
    expect(plans.find((p) => p.monthKey === '2026-09')!.titleRow).toBe(605);
  });

  it('ноябрь после октября встаёт на 31 день октября, а не на 30 сентября', () => {
    const plans = resolveSections({ live: LIVE, windowMonths: ['2026-10', '2026-11'],
      firstActivity: ACT, blockSlots: 22 });
    const [oct, nov] = plans as [typeof plans[0], typeof plans[0]];
    expect(nov.titleRow).toBe(oct.titleRow + 31 + 4);
    expect(nov.days).toBe(30);
  });

  it('февраль високосного 2028 года — 29 дней', () => {
    const live = [{ monthKey: '2028-01', titleRow: 100, days: Array(31).fill('d'), blocks: SKUS }];
    const plans = resolveSections({ live, windowMonths: ['2028-02'], firstActivity: ACT, blockSlots: 22 });
    expect(plans[0]!.days).toBe(29);
  });

  it('нужное число новых строк выводится из плана, а не задаётся руками', () => {
    const plans = resolveSections({ live: LIVE, windowMonths: ['2026-09', '2026-10'],
      firstActivity: ACT, blockSlots: 22 });
    // октябрь: заголовок 639, шапка 640, дни 641..671, итог 672, разделитель 673
    expect(rowsNeeded(plans, 650)).toBe(23);
    expect(rowsNeeded(plans, 700)).toBe(0);        // места уже хватает — не растём
  });

  it('достраивать ПРОШЛОЕ движок не имеет права', () => {
    expect(() => resolveSections({ live: LIVE, windowMonths: ['2026-07'],
      firstActivity: ACT, blockSlots: 22 })).toThrow(/НЕ новее последней/);
  });
});

describe('OZON adapter — Gate 9: новый SKU активируется сам', () => {
  const CUR = ['a', 'b', 'c'];
  it('SKU с первой активностью в этом месяце получает блок', () => {
    const r = activateNewSkus({ current: CUR, monthKey: '2026-10',
      firstActivity: { a: '2026-04', b: '2026-04', c: '2026-05', d: '2026-10' }, blockSlots: 22 });
    expect(r.activated).toEqual(['d']);
    expect(r.blocks).toEqual(['a', 'b', 'c', 'd']);
  });

  it('порядок существующих блоков не меняется: SKU живёт в своём слоте', () => {
    const r = activateNewSkus({ current: CUR, monthKey: '2026-10',
      firstActivity: { c: '2026-05', a: '2026-04', z: '2026-10', b: '2026-04' }, blockSlots: 22 });
    expect(r.blocks.slice(0, 3)).toEqual(CUR);     // прежние — на прежних местах
    expect(r.blocks[3]).toBe('z');
  });

  it('SKU из будущего месяца сейчас не активируется', () => {
    const r = activateNewSkus({ current: CUR, monthKey: '2026-09',
      firstActivity: { d: '2026-10' }, blockSlots: 22 });
    expect(r.activated).toEqual([]);
  });

  it('новый SKU получает блок ТОЙ ЖЕ ширины 25 и полное оформление', () => {
    const SEC = { titleRow: 10, headerRow: 11, firstRow: 12, lastRow: 41, mtdRow: 42,
                  spacerRow: 43, blockCount: 4 };
    const before = staticFormatRequests(7, [{ ...SEC, blockCount: 3 }]);
    const after = staticFormatRequests(7, [SEC]);
    // добавился ровно один блок: 25 колонок × 4 роли строк
    expect(after.length - before.length).toBe(OZON_BLOCK_WIDTH * 4);
    const widths = columnWidthRequests(7, 4);
    expect(widths.length - columnWidthRequests(7, 3).length).toBe(OZON_BLOCK_WIDTH);
    // и «Прочие прямые» нового блока оформлены как у всех
    const od = blockSpecFor('day', 'OTHER_DIRECT');
    expect(od.bg).toBe(blockSpecFor('day', 'STORAGE').bg);
  });
});

describe('OZON adapter — Gate 9: ёмкость слотов', () => {
  const S22 = Array.from({ length: 22 }, (_, i) => `s${i}`);
  it('23-й SKU при 22 слотах — ОТКАЗ, а не перезапись чужого блока', () => {
    expect(() => activateNewSkus({ current: S22, monthKey: '2026-10',
      firstActivity: { newbie: '2026-10' }, blockSlots: 22 })).toThrow(NoFreeSkuSlotError);
  });

  it('отказ называет причину, нехватку и виновный SKU', () => {
    try {
      activateNewSkus({ current: S22, monthKey: '2026-10',
        firstActivity: { newbie: '2026-10' }, blockSlots: 22 });
      throw new Error('должно было упасть');
    } catch (e) {
      const err = e as NoFreeSkuSlotError;
      expect(err.code).toBe('NO_FREE_SKU_SLOT');
      expect(err.needed).toBe(23);
      expect(err.available).toBe(22);
      expect(err.newSkus).toEqual(['newbie']);
      expect(err.message).toContain('переписать существующий блок нельзя');
    }
  });

  it('новый месяц с переполнением тоже отказывает, а не молча обрезает', () => {
    const live = [{ monthKey: '2026-09', titleRow: 605, days: Array(30).fill('d'), blocks: S22 }];
    expect(() => resolveSections({ live, windowMonths: ['2026-10'],
      firstActivity: { newbie: '2026-10' }, blockSlots: 22 })).toThrow(NoFreeSkuSlotError);
  });

  it('ровно 22 SKU при 22 слотах проходят', () => {
    expect(() => activateNewSkus({ current: S22.slice(0, 21), monthKey: '2026-10',
      firstActivity: { last: '2026-10' }, blockSlots: 22 })).not.toThrow();
  });
});

describe('OZON adapter — Gate 9: барьер готовности источников', () => {
  const REQ = ['finance_accrual', 'fbo_postings'];
  it('все источники свежие — писать можно', () => {
    const f = assessFreshness([{ entity: 'finance_accrual', last_ok_date: '2026-09-21' },
      { entity: 'fbo_postings', last_ok_date: '2026-09-21' }], REQ, '2026-09-21', 1);
    expect(f.every((x) => !x.stale)).toBe(true);
  });

  it('источник отстал — писать нельзя', () => {
    const f = assessFreshness([{ entity: 'finance_accrual', last_ok_date: '2026-09-18' },
      { entity: 'fbo_postings', last_ok_date: '2026-09-21' }], REQ, '2026-09-21', 1);
    expect(f.find((x) => x.entity === 'finance_accrual')!.stale).toBe(true);
    expect(f.find((x) => x.entity === 'fbo_postings')!.stale).toBe(false);
  });

  it('источник не загружался НИКОГДА — это тоже несвежесть, а не отсутствие проблемы', () => {
    const f = assessFreshness([], REQ, '2026-09-21', 1);
    expect(f.every((x) => x.stale)).toBe(true);
    expect(f[0]!.lastOkMoscowDate).toBeNull();
  });

  it('отказ называет каждый отставший источник и его последний успех', () => {
    const stale = assessFreshness([{ entity: 'finance_accrual', last_ok_date: '2026-09-10' }],
      REQ, '2026-09-21', 1).filter((x) => x.stale);
    const e = new StaleSourceError(stale);
    expect(e.code).toBe('SOURCE_STALE');
    expect(e.message).toContain('finance_accrual');
    expect(e.message).toContain('2026-09-10');
    expect(e.message).toContain('Запись отменена');
  });

  it('перечень обязательных источников включает деньги, отправления и рекламу', () => {
    expect(OZON_UNITKA_REQUIRED_SOURCES).toContain('finance_accrual');
    expect(OZON_UNITKA_REQUIRED_SOURCES).toContain('fbo_postings');
    expect(OZON_UNITKA_REQUIRED_SOURCES).toContain('ads_sku_daily');
  });
});

/* ════════════════════════════════════════════════════════════════════════════
 * GATE 9 §11 — политика итога распространяется на новый месяц и новый SKU
 * ════════════════════════════════════════════════════════════════════════════ */
describe('OZON adapter — Gate 9: итог нового месяца и нового SKU', () => {
  it('новый месяц получает ПОЛНУЮ строку итога — политика следует из геометрии', () => {
    // октябрь 2026: заголовок 639, дни 641..671, итог 672
    const oct = ozonBlockMtdFormulas(12, { firstDailyRow: 641, lastDailyRow: 671, mtdRow: 672 });
    for (const { offset, role, policy } of summaryPolicyMap()) {
      if (policy === 'NOT_APPLICABLE') expect(oct.has(offset), `${role}`).toBe(false);
      else expect(oct.has(offset), `${role} без итога в новом месяце`).toBe(true);
    }
    expect(oct.get(OFFSET.price)).toContain('641:Z671');    // адреса октября, не сентября
    expect(oct.get(OFFSET.price)).not.toContain('607');
  });

  it('новый SKU получает ту же политику: формулы зависят от начала блока', () => {
    const G = { firstDailyRow: 641, lastDailyRow: 671, mtdRow: 672 };
    const slot21 = ozonBlockMtdFormulas(12 + 25 * 21, G);
    for (const { offset, policy } of summaryPolicyMap()) {
      if (policy !== 'NOT_APPLICABLE') expect(slot21.has(offset)).toBe(true);
    }
    // ни одного AVERAGE и в новом блоке
    for (const [, f] of slot21) expect(f).not.toMatch(/\bAVERAGE\s*\(/);
  });

  it('методы итога в новом месяце те же, что приняты: взвешивание, а не среднее суток', () => {
    const oct = ozonBlockMtdFormulas(12, { firstDailyRow: 641, lastDailyRow: 671, mtdRow: 672 });
    expect(oct.get(OFFSET.price)).toContain('SUMPRODUCT');
    expect(oct.get(OFFSET.logistics)).toContain('SUMPRODUCT');
    expect(oct.get(OFFSET.storage)).toContain('SUMIF');
    expect(oct.get(OFFSET.otherDirect)).toContain('SUMIF');
    expect(oct.get(OFFSET.stock)).toContain('FILTER(');
  });
});

describe('OZON adapter — Gate 9: LAST_CLOSED_DATE читается из книги', () => {
  it('серийное число книги превращается в ISO', () => {
    expect(isoFromSheetValue(46285)).toBe('2026-09-20');
  });
  it('ru_RU-вид даты тоже понимается', () => {
    expect(isoFromSheetValue('20.09.2026')).toBe('2026-09-20');
  });
  it('ISO проходит как есть, мусор — пустая строка, а не выдуманная дата', () => {
    expect(isoFromSheetValue('2026-09-20')).toBe('2026-09-20');
    expect(isoFromSheetValue('')).toBe('');
    expect(isoFromSheetValue(null)).toBe('');
    expect(isoFromSheetValue('позавчера')).toBe('');
  });
  it('по умолчанию дата НЕ задаётся окружением — источник истины книга', () => {
    const c = loadConfig({ GCP_PROJECT_ID: 'p', BQ_RAW_DATASET: 'r', ENVIRONMENT: 'prod' });
    expect(c.ozonUnitkaLastClosedDate).toBe('');
    expect(c.ozonUnitkaLcdCell).toBe('ZZ_CONFIG!B2');
  });
});
