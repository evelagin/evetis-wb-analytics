/**
 * OZON CPO (Phase B) — разбор CSV «Отчёт по заказам» двух семейств «Оплаты за заказ».
 *
 * Формат (живые выгрузки 2025-05…2026-10): UTF-8 с BOM, разделитель «;», десятичная запятая.
 *   строка 1  — «;<название>, Период ДД.ММ.ГГГГ-ДД.ММ.ГГГГ» (у SEARCH_PROMO «период» строчными);
 *   строка 2  — шапка; у SEARCH_PROMO есть колонка «Источник заказов» после «Артикул»;
 *   далее     — строки заказов; у SEARCH_PROMO последняя строка «Всего;…;<сумма расхода>».
 * Пустой отчёт = только две первые строки (нет заказов — законное состояние, а не сбой).
 *
 * FAIL-CLOSED: любое расхождение формы — отказ всего прогона, а не пропуск строки.
 *   • шапка не совпала дословно                 → CPO_REPORT_SCHEMA_CHANGED
 *   • период в заголовке ≠ запрошенному         → CPO_REPORT_PARTIAL (неполный отчёт не равен полному)
 *   • строка не разбирается / дата вне периода  → CPO_REPORT_ROW_INVALID
 *   • «Расход» ≠ «Ставка, ₽» × «Количество»     → CPO_REPORT_ROW_ARITHMETIC
 *   • сумма строк ≠ «Всего»                     → CPO_REPORT_TOTAL_MISMATCH
 * Название товара может содержать «;», поэтому поля читаются с краёв: идентификаторы слева,
 * числа справа, название — середина.
 */
import { LoaderError } from '../../errors.js';

export type CpoFamily = 'ALL_SKU_PROMO' | 'SEARCH_PROMO';

export const CPO_HEADERS: Record<CpoFamily, readonly string[]> = {
  ALL_SKU_PROMO: ['Дата', 'ID заказа', 'Номер заказа', 'SKU', 'SKU продвигаемого товара', 'Артикул',
    'Название товара', 'Количество', 'Стоимость продажи, ₽', 'Стоимость, ₽', 'Ставка, %', 'Ставка, ₽', 'Расход, ₽'],
  SEARCH_PROMO: ['Дата', 'ID заказа', 'Номер заказа', 'SKU', 'SKU продвигаемого товара', 'Артикул',
    'Источник заказов', 'Название товара', 'Количество', 'Стоимость продажи, ₽', 'Стоимость, ₽', 'Ставка, %',
    'Ставка, ₽', 'Расход, ₽'],
};

/** Допуск итога: отчёт округляет «Всего» сам (наблюдено 0,01 ₽ на 10 строках, 2025-06). */
export const CPO_TOTAL_TOLERANCE_RUB = 0.1;

export interface CpoReportRow {
  chargeDate: string;          // «Дата» — дата списания, сутки МСК
  orderId: string;
  orderNumber: string;
  orderedSku: string;          // «SKU» — заказанный (оплаченный) товар
  promotedSku: string;         // «SKU продвигаемого товара»
  orderedOfferId: string;      // «Артикул» заказанного товара, как его прислал Ozon
  orderSource: string | null;  // только SEARCH_PROMO
  productName: string;
  quantity: number;
  unitSalePriceRub: string;    // «Стоимость продажи» — цена единицы, десятичная строка
  saleValueRub: string;        // «Стоимость» — цена × количество
  ratePct: string;
  rateRub: string;             // за единицу
  expenseRub: string;          // «Расход» = ставка × количество
  rawLine: string;
}

export interface CpoReport {
  family: CpoFamily;
  title: string;
  periodFrom: string;
  periodTo: string;
  rows: CpoReportRow[];
  reportTotalRub: string | null;
  expenseCents: number;
}

const DMY = /^(\d{2})\.(\d{2})\.(\d{4})$/;
const dmyToIso = (s: string): string | null => { const m = DMY.exec(s.trim()); return m ? `${m[3]}-${m[2]}-${m[1]}` : null; };

/** «1 450,00» → копейки. Только явная десятичная форма; пусто и прочерк — null. */
export function parseRub(s: string): number | null {
  const t = s.replace(/[\s\u00a0]/g, '').replace(',', '.');
  if (!/^-?\d+(?:\.\d{1,4})?$/.test(t)) return null;
  const neg = t.startsWith('-');
  const [i, f = ''] = (neg ? t.slice(1) : t).split('.');
  const cents = Number(i) * 100 + Number((f + '00').slice(0, 2)) + (Number((f + '0000').slice(2, 4)) >= 50 ? 1 : 0);
  return neg ? -cents : cents;
}

/** Десятичная строка для NUMERIC: никаких float-артефактов на пути в BigQuery. */
export function centsToDecimal(c: number): string {
  const neg = c < 0, a = Math.abs(c);
  return `${neg ? '-' : ''}${Math.floor(a / 100)}.${String(a % 100).padStart(2, '0')}`;
}

/** Ставка, % — до 4 знаков после запятой, как в источнике. */
function parsePct(s: string): string | null {
  const t = s.replace(/[\s\u00a0]/g, '').replace(',', '.');
  return /^-?\d+(?:\.\d{1,4})?$/.test(t) ? t : null;
}

const fail = (msg: string, code: string): never => { throw new LoaderError(msg, code); };

export function parseCpoReport(family: CpoFamily, body: string, expected: { from: string; to: string }): CpoReport {
  const text = body.replace(/^\uFEFF/, '');
  const lines = text.split(/\r?\n/);
  const title = (lines[0] ?? '').replace(/^;/, '').trim();
  const pm = /период\s+(\d{2}\.\d{2}\.\d{4})-(\d{2}\.\d{2}\.\d{4})/i.exec(title);
  if (!pm) fail(`${family}: в заголовке нет периода: «${title.slice(0, 120)}»`, 'CPO_REPORT_SCHEMA_CHANGED');
  const periodFrom = dmyToIso(pm![1]!)!, periodTo = dmyToIso(pm![2]!)!;
  if (periodFrom !== expected.from || periodTo !== expected.to) {
    fail(`${family}: отчёт за ${periodFrom}..${periodTo}, запрошено ${expected.from}..${expected.to}`, 'CPO_REPORT_PARTIAL');
  }
  const header = (lines[1] ?? '').split(';').map((h) => h.trim());
  const want = CPO_HEADERS[family];
  if (header.length !== want.length || header.some((h, i) => h !== want[i])) {
    fail(`${family}: шапка изменилась: ${header.join(' | ').slice(0, 300)}`, 'CPO_REPORT_SCHEMA_CHANGED');
  }
  const left = family === 'SEARCH_PROMO' ? 7 : 6;   // поля до названия
  const right = 6;                                   // количество и пять денежных полей
  const rows: CpoReportRow[] = [];
  let total: string | null = null;
  let sum = 0;
  for (let i = 2; i < lines.length; i++) {
    const line = lines[i]!;
    if (!line.trim()) continue;
    const p = line.split(';');
    if (p[0] === 'Всего') {
      if (total !== null) fail(`${family}: две строки «Всего»`, 'CPO_REPORT_ROW_INVALID');
      const t = parseRub(p[p.length - 1] ?? '');
      if (t === null) fail(`${family}: итог не число: «${line.slice(0, 80)}»`, 'CPO_REPORT_ROW_INVALID');
      total = centsToDecimal(t!);
      continue;
    }
    if (total !== null) fail(`${family}: строка после «Всего» (${i + 1})`, 'CPO_REPORT_ROW_INVALID');
    if (p.length < left + right + 1) fail(`${family}: строка ${i + 1}: ${p.length} полей`, 'CPO_REPORT_ROW_INVALID');
    const date = dmyToIso(p[0]!);
    const nums = p.slice(p.length - right);
    const qty = Number(nums[0]);
    const unit = parseRub(nums[1]!), value = parseRub(nums[2]!), pct = parsePct(nums[3]!);
    const rate = parseRub(nums[4]!), exp = parseRub(nums[5]!);
    const ids = p.slice(1, 6).map((x) => x.trim());
    if (!date || date < expected.from || date > expected.to || !Number.isInteger(qty) || qty <= 0
        || unit === null || value === null || pct === null || rate === null || exp === null
        || !/^\d+$/.test(ids[0]!) || !ids[1] || !/^\d+$/.test(ids[2]!) || !/^\d+$/.test(ids[3]!)) {
      fail(`${family}: строка ${i + 1} не разбирается или вне периода`, 'CPO_REPORT_ROW_INVALID');
    }
    if (Math.abs(rate! * qty - exp!) > 1) {
      fail(`${family}: строка ${i + 1}: расход ${centsToDecimal(exp!)} ≠ ставка ${centsToDecimal(rate!)} × ${qty}`, 'CPO_REPORT_ROW_ARITHMETIC');
    }
    sum += exp!;
    rows.push({
      chargeDate: date!, orderId: ids[0]!, orderNumber: ids[1]!, orderedSku: ids[2]!, promotedSku: ids[3]!,
      orderedOfferId: ids[4]!, orderSource: family === 'SEARCH_PROMO' ? (p[6] ?? '').trim() || null : null,
      productName: p.slice(left, p.length - right).join(';').trim(),
      quantity: qty, unitSalePriceRub: centsToDecimal(unit!), saleValueRub: centsToDecimal(value!), ratePct: pct!,
      rateRub: centsToDecimal(rate!), expenseRub: centsToDecimal(exp!), rawLine: line,
    });
  }
  if (total !== null) {
    const diff = Math.abs(parseRub(total)! - sum) / 100;
    if (diff > CPO_TOTAL_TOLERANCE_RUB) {
      fail(`${family}: сумма строк ${centsToDecimal(sum)} ≠ «Всего» ${total}`, 'CPO_REPORT_TOTAL_MISMATCH');
    }
  } else if (family === 'SEARCH_PROMO' && rows.length > 0) {
    fail(`${family}: есть строки, но нет «Всего» — отчёт мог оборваться`, 'CPO_REPORT_PARTIAL');
  }
  return { family, title, periodFrom, periodTo, rows, reportTotalRub: total, expenseCents: sum };
}
